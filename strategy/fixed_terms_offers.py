"""
Offerbook Fixed-Terms Multi-Duration Offer Creator
====================================================
Creates up to 4 USDC/<collateral> lending offers per run, at fixed 1/3/5/7-
day durations — skipping any duration where a live (active or
partiallyFilled) offer of ours already exists on that exact pair+duration,
so re-running after e.g. a 7-day offer is already live only creates the
missing 1/3/5-day ones.

Unlike strategy.py, terms are FIXED rather than market-benchmarked:
  - LTV: FIXED_LTV (25%) — collateral required from the borrower =
    principal / FIXED_LTV, at the collateral's current live price.
  - APY: FIXED_APY_BPS (100.00%, i.e. 10000 bps) charged on every offer.
  - Principal: available USDC (wallet + escrow) / PRINCIPAL_DIVISOR (3),
    rounded to the NEAREST ROUND_STEP_USDC ($100 — nearest, not down, unlike
    strategy.py's rounding). Every missing duration gets this SAME amount
    (not split further by how many happen to be missing this run) — each
    offer independently draws on the same shared escrow/wallet pool
    (rehypothecation: only one can actually fill at a time, so sizing every
    offer off the same balance is intentional, same as strategy.py already
    does across pairs/durations — see that script's own docstring).

Usage:
  python fixed_terms_offers.py --collateral USELESS
  python fixed_terms_offers.py --collateral <mint address>
  python fixed_terms_offers.py                              # prompts for collateral
  DRY_RUN=true python fixed_terms_offers.py --collateral USELESS   # preview only, no submission
  python fixed_terms_offers.py --collateral USELESS --ledger-path "44'/501'/1'"

Notes:
  - Only USDC principal is supported (matches every other script here).
  - Offer listing itself always expires in 24h (OFFER_EXPIRY_SECS),
    regardless of loan duration — same convention as strategy.py.
"""
from __future__ import annotations

from dotenv import load_dotenv
load_dotenv()

import argparse
import base64
import logging
import os
import sys
from typing import Any

import base58
import requests

# Shared modules live in ../lib — see README's repo-layout note.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))

import offerbook_common as _common

API_BASE = os.getenv("OFFERBOOK_API_BASE", "https://api.offerbook.jup.ag/api/v1")
TX_API_BASE = os.getenv("OFFERBOOK_TX_API_BASE", "https://builder.offerbook.jup.ag/api/v1")
SOLANA_RPC = os.getenv("SOLANA_RPC", "https://api.mainnet-beta.solana.com")

WALLET_PUBKEY: str = os.getenv("OFFERBOOK_WALLET", "")
PRIVATE_KEY_B58: str = os.getenv("OFFERBOOK_PRIVATE_KEY", "")

SIGNING_MODE: str = os.getenv("OFFERBOOK_SIGNING_MODE", "ledger").strip().lower()
LEDGER_PATH: str = os.getenv("OFFERBOOK_LEDGER_PATH", "44'/501'/0'")

DRY_RUN: bool = os.getenv("DRY_RUN", "false").lower() in ("1", "true", "yes")

DURATIONS_DAYS = [1, 3, 5, 7]
FIXED_LTV = 0.25          # collateral required = principal / FIXED_LTV
FIXED_APY_BPS = 10_000    # 100.00%
PRINCIPAL_DIVISOR = 3     # every missing duration's principal = available USDC / this
ROUND_STEP_USDC = 100.0   # principal rounded to the NEAREST this — not down (see module docstring)
WALLET_BUFFER_USDC = 20.0  # always leave at least this much USDC unallocated, same as strategy.py

OFFER_EXPIRY_SECS = 1 * 24 * 60 * 60  # offer listing always expires in 24h, regardless of loan term
ALLOW_PARTIAL_FILL = True
MIN_FILL_USDC = 10.0

USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDC_DECIMALS = 6

JUPITER_PRICE_API = "https://api.jup.ag/price/v3"
JUPITER_API_KEY = os.getenv("JUPITER_API_KEY", "")
DEXSCREENER_API = "https://api.dexscreener.com/latest/dex/tokens"

KNOWN_DECIMALS = _common.KNOWN_DECIMALS
KNOWN_SYMBOLS = _common.KNOWN_SYMBOLS
SYMBOL_TO_MINT = {sym.upper(): mint for mint, sym in KNOWN_SYMBOLS.items()}

# strategy.py's allocation_config.yaml lists far more tokens (with symbols in
# its own comments) than the curated KNOWN_SYMBOLS table — second-tier
# symbol->mint lookup, same approach reporting/liquidity_check.py uses.
ALLOCATION_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "allocation_config.yaml")
_allocation_symbol_to_mint_cache: dict[str, str] | None = None

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("fixed_terms_offers")

SESSION = requests.Session()
SESSION.headers.update({"Content-Type": "application/json"})


def is_valid_pubkey(s: str) -> bool:
    """A Solana pubkey base58-decodes to exactly 32 bytes."""
    try:
        return len(base58.b58decode(s)) == 32
    except Exception:
        return False


def resolve_collateral_token(tok: str) -> str:
    """
    Resolve a symbol (e.g. "USELESS") or raw mint address to a mint address:
      1. SYMBOL_TO_MINT (curated table) — fastest, most common.
      2. allocation_config.yaml's own comments (scraped, same as
         strategy.py's resolve_collateral_token) — covers the longer tail of
         tokens configured there but not in the curated table.
      3. Otherwise assumed to already be a raw mint address — validated as a
         plausible base58 pubkey; exits with a clear error instead of
         building a transaction around a malformed mint.
    """
    global _allocation_symbol_to_mint_cache
    tok = tok.strip()
    upper = tok.upper()
    if upper in SYMBOL_TO_MINT:
        return SYMBOL_TO_MINT[upper]

    if _allocation_symbol_to_mint_cache is None:
        _allocation_symbol_to_mint_cache = _common.build_symbol_to_mint_from_allocation_config(ALLOCATION_CONFIG_PATH)
    if upper in _allocation_symbol_to_mint_cache:
        return _allocation_symbol_to_mint_cache[upper]

    if not is_valid_pubkey(tok):
        log.error(
            "%r isn't a known symbol or a valid mint address (checked the curated table and "
            "allocation_config.yaml's comments). If it's a typo, fix it; otherwise pass the raw mint.",
            tok,
        )
        sys.exit(1)
    return tok


def symbol_for(mint: str) -> str:
    return KNOWN_SYMBOLS.get(mint, mint[:6] + "…" + mint[-4:])


# ---------------------------------------------------------------------------
# API helpers
# ---------------------------------------------------------------------------

def _get(endpoint: str, params: dict | None = None) -> dict:
    return _common.api_get(SESSION, API_BASE, endpoint, params)


def _post_tx(endpoint: str, payload: dict) -> dict:
    return _common.post_tx(SESSION, TX_API_BASE, endpoint, payload)


def _fetch_all_pages(endpoint: str, params: dict | None = None) -> list[dict]:
    return _common.fetch_all_pages(SESSION, API_BASE, endpoint, params, 100)


def fetch_wallet_token_balance(mint: str) -> int:
    payload = {
        "jsonrpc": "2.0", "id": 1, "method": "getTokenAccountsByOwner",
        "params": [WALLET_PUBKEY, {"mint": mint}, {"encoding": "jsonParsed"}],
    }
    resp = requests.post(SOLANA_RPC, json=payload, timeout=30)
    resp.raise_for_status()
    accounts = resp.json().get("result", {}).get("value", [])
    return sum(
        int(a.get("account", {}).get("data", {}).get("parsed", {}).get("info", {})
             .get("tokenAmount", {}).get("amount", "0"))
        for a in accounts
    )


def fetch_escrow_balance(mint: str) -> int:
    holdings = _get(f"/escrows/holdings/{WALLET_PUBKEY}")
    for entry in holdings:
        if entry.get("asset", {}).get("mint") == mint:
            return int(entry.get("amount", 0))
    return 0


def fetch_available_balance(mint: str, decimals: int) -> tuple[int, int, int]:
    """(wallet_raw, escrow_raw, total_raw) for `mint`, logging the breakdown."""
    wallet_raw = fetch_wallet_token_balance(mint)
    escrow_raw = fetch_escrow_balance(mint)
    total_raw = wallet_raw + escrow_raw
    scale = 10 ** decimals
    log.info(
        "%-22s wallet=%10.2f  escrow=%10.2f  total=%10.2f",
        mint[:8] + "… balance:", wallet_raw / scale, escrow_raw / scale, total_raw / scale,
    )
    return wallet_raw, escrow_raw, total_raw


def fetch_current_price(mint: str) -> tuple[float | None, int | None]:
    """(usd_price_per_whole_token, decimals) for `mint`. Jupiter first
    (also returns decimals, covering mints outside KNOWN_DECIMALS), falling
    back to DexScreener (price only — decimals must come from KNOWN_DECIMALS
    in that case) — same two-source approach strategy.py uses."""
    try:
        headers = {"x-api-key": JUPITER_API_KEY} if JUPITER_API_KEY else {}
        resp = SESSION.get(JUPITER_PRICE_API, params={"ids": mint}, headers=headers, timeout=15)
        resp.raise_for_status()
        info = resp.json().get(mint)
        if info and info.get("usdPrice"):
            decimals = int(info["decimals"]) if info.get("decimals") is not None else KNOWN_DECIMALS.get(mint)
            return float(info["usdPrice"]), decimals
    except Exception as exc:
        log.warning("Jupiter price fetch failed for %s…: %s", mint[:8], exc)

    try:
        resp = SESSION.get(f"{DEXSCREENER_API}/{mint}", timeout=10)
        if resp.ok:
            pairs = [p for p in (resp.json().get("pairs") or []) if p.get("chainId") == "solana" and p.get("priceUsd")]
            if pairs:
                pairs.sort(key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0), reverse=True)
                return float(pairs[0]["priceUsd"]), KNOWN_DECIMALS.get(mint)
    except Exception as exc:
        log.warning("DexScreener price fetch failed for %s…: %s", mint[:8], exc)

    return None, KNOWN_DECIMALS.get(mint)


def fetch_own_live_offer_durations(collateral_mint: str) -> set[int]:
    """Durations (seconds) of our own live (active/partiallyFilled)
    USDC/<collateral_mint> lending offers — used to skip a duration that
    already has one of ours live, same idempotency check strategy.py's
    own_open_pairs makes (see that script's docstring)."""
    durations: set[int] = set()
    for status in ("active", "partiallyFilled"):
        offers = _fetch_all_pages("/offers", {
            "offerType": "lending", "status": status, "hideExpired": "true",
            "showUnverified": "true", "includeUnderfunded": "true",
            "principalMint": USDC_MINT, "collateralMint": collateral_mint,
        })
        durations |= {o["duration"] for o in offers if o.get("creator") == WALLET_PUBKEY and o.get("duration") is not None}
    return durations


def round_to_nearest(usdc: float, step: float) -> float:
    """Round `usdc` to the NEAREST `step` (not down) — see module docstring
    for why this differs from strategy.py's round-down convention."""
    if step <= 0:
        return usdc
    return round(usdc / step) * step


def compute_collateral_amount(principal_raw: int, price_per_whole_token: float, collateral_decimals: int) -> int:
    """Collateral raw amount so that LTV == FIXED_LTV at the given live price."""
    principal_usdc = principal_raw / 10 ** USDC_DECIMALS
    required_collateral_usdc = principal_usdc / FIXED_LTV
    price_per_raw = price_per_whole_token / (10 ** collateral_decimals)
    return int(required_collateral_usdc / price_per_raw)


# ---------------------------------------------------------------------------
# Signer / transaction submission — same pattern as strategy.py
# ---------------------------------------------------------------------------

def resolve_signer_wallet() -> str:
    global WALLET_PUBKEY
    WALLET_PUBKEY = _common.resolve_signer_wallet(SIGNING_MODE, WALLET_PUBKEY, LEDGER_PATH)
    return WALLET_PUBKEY


def confirm_signing_mode(skip_prompt: bool) -> None:
    _common.confirm_signing_mode(SIGNING_MODE, WALLET_PUBKEY, LEDGER_PATH, DRY_RUN, skip_prompt)


def sign_and_send_transaction(tx_b64: str) -> str:
    if SIGNING_MODE == "ledger":
        from ledger_signer import LedgerError

        signer = _common.get_ledger_signer(LEDGER_PATH)
        log.info("  Awaiting approval on Ledger device …")
        try:
            signed_b64 = signer.sign_transaction(tx_b64, expected_signer=WALLET_PUBKEY)
        except LedgerError as exc:
            log.error("  %s", exc)
            return ""
    else:
        try:
            from solders.keypair import Keypair  # type: ignore
            from solders.transaction import VersionedTransaction  # type: ignore
            import base58  # type: ignore
        except ImportError:
            log.error("solders / base58 not installed.  Run:  pip install solders base58\nTransaction NOT submitted.")
            return ""

        secret_bytes = base58.b58decode(PRIVATE_KEY_B58)
        keypair = Keypair.from_bytes(secret_bytes)
        raw_tx = base64.b64decode(tx_b64)
        tx = VersionedTransaction.from_bytes(raw_tx)
        signed_tx = VersionedTransaction(tx.message, [keypair])
        signed_b64 = base64.b64encode(bytes(signed_tx)).decode()

    payload = {
        "jsonrpc": "2.0", "id": 1, "method": "sendTransaction",
        "params": [signed_b64, {"encoding": "base64", "preflightCommitment": "confirmed"}],
    }
    rpc_resp = requests.post(SOLANA_RPC, json=payload, timeout=30)
    rpc_resp.raise_for_status()
    result = rpc_resp.json()
    if "error" in result:
        log.error("RPC error: %s", result["error"])
        return ""
    return result.get("result", "")


def create_offer(params: dict[str, Any]) -> bool:
    log.info(
        "  Creating lending offer: principal=%s…  collateral=%s…  apy=%d bps (%.2f%%)  duration=%dd",
        params["principalMint"][:8], params["collateralMint"][:8],
        params["apy"], params["apy"] / 100, params["duration"] // 86400,
    )

    if DRY_RUN:
        log.info("  [DRY RUN] Skipping transaction submission.")
        return True

    try:
        tx_data = _post_tx("/create-principal-offer", params)
    except requests.HTTPError as exc:
        body = exc.response.text if exc.response else ""
        log.error("  TX builder error: %s\n  body: %s", exc, body or "(empty)")
        return False
    except requests.ConnectionError as exc:
        log.error("  TX builder connection error: %s", exc)
        return False

    transactions: list[str] = tx_data.get("transactions", [])
    if not transactions:
        log.error("  TX builder returned no transactions!")
        return False

    if SIGNING_MODE == "private_key" and not PRIVATE_KEY_B58:
        log.warning("  OFFERBOOK_PRIVATE_KEY not set – cannot sign.  Transaction bytes (base64):\n%s",
                    transactions[0][:80] + "…")
        return False

    for tx_b64 in transactions:
        sig = sign_and_send_transaction(tx_b64)
        if sig:
            log.info("  ✓ Submitted: https://solscan.io/tx/%s", sig)
        else:
            log.error("  ✗ Failed to submit transaction.")
            return False

    return True


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def prompt_for_collateral() -> str:
    raw = input("Enter collateral symbol (e.g. USELESS) or mint address: ").strip()
    while not raw:
        raw = input("Collateral can't be blank: ").strip()
    return raw


def main() -> None:
    global SIGNING_MODE, WALLET_PUBKEY, LEDGER_PATH

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    _common.add_signing_args(parser)
    parser.add_argument("--ledger-path", default=None, help="Ledger derivation path — skips the interactive account prompt.")
    parser.add_argument("--collateral", default=None, help="Collateral symbol or mint address. Omit to be prompted.")
    args = parser.parse_args()

    collateral_mint = resolve_collateral_token(args.collateral or prompt_for_collateral())

    SIGNING_MODE = _common.resolve_signing_mode(args.signing_mode, SIGNING_MODE)
    if SIGNING_MODE == "private_key":
        if not WALLET_PUBKEY:
            log.error("OFFERBOOK_WALLET env var not set.  Exiting.")
            sys.exit(1)
        if not DRY_RUN and not PRIVATE_KEY_B58:
            log.error("OFFERBOOK_PRIVATE_KEY is not set (required for live private-key mode)")
            sys.exit(1)
    else:
        LEDGER_PATH = args.ledger_path or _common.prompt_for_ledger_path(LEDGER_PATH)
        resolve_signer_wallet()

    confirm_signing_mode(skip_prompt=args.yes)

    log.info("=" * 60)
    log.info("Offerbook Fixed-Terms Multi-Duration Offer Creator")
    log.info("Wallet     : %s", WALLET_PUBKEY)
    log.info("Collateral : %s (%s)", symbol_for(collateral_mint), collateral_mint)
    log.info("DRY RUN    : %s", DRY_RUN)
    log.info("Durations  : %s day(s)  |  LTV = %.0f%%  |  APY = %.2f%%",
              ", ".join(str(d) for d in DURATIONS_DAYS), FIXED_LTV * 100, FIXED_APY_BPS / 100)
    log.info("=" * 60)

    # 1. Available USDC (wallet + escrow), same shared-pool snapshot convention as strategy.py.
    _, _, usdc_available_raw = fetch_available_balance(USDC_MINT, USDC_DECIMALS)
    buffer_raw = int(WALLET_BUFFER_USDC * 10 ** USDC_DECIMALS)
    usdc_available_raw = max(0, usdc_available_raw - buffer_raw)
    usdc_available = usdc_available_raw / 10 ** USDC_DECIMALS
    log.info("USDC available (after $%.2f buffer): %.2f", WALLET_BUFFER_USDC, usdc_available)

    principal_usdc = round_to_nearest(usdc_available / PRINCIPAL_DIVISOR, ROUND_STEP_USDC)
    principal_raw = int(principal_usdc * 10 ** USDC_DECIMALS)
    log.info("Principal per offer (available / %d, rounded to nearest $%.0f): %.2f USDC",
              PRINCIPAL_DIVISOR, ROUND_STEP_USDC, principal_usdc)
    if principal_raw <= 0:
        log.error("Principal per offer rounds to 0 — not enough available balance. Aborting.")
        sys.exit(1)

    # 2. Live collateral price, for sizing collateralAmount at FIXED_LTV.
    price, collateral_decimals = fetch_current_price(collateral_mint)
    if not price or price <= 0 or collateral_decimals is None:
        log.error(
            "No live price/decimals for %s… — can't size collateral at a fixed LTV without one. Aborting.",
            collateral_mint[:8],
        )
        sys.exit(1)
    collateral_raw = compute_collateral_amount(principal_raw, price, collateral_decimals)
    log.info("Collateral per offer at %.0f%% LTV, price $%.6g: %.4f %s",
              FIXED_LTV * 100, price, collateral_raw / 10 ** collateral_decimals, symbol_for(collateral_mint))

    # 3. Skip durations that already have a live offer of ours on this exact pair.
    already_open_secs = fetch_own_live_offer_durations(collateral_mint)
    to_create = [d for d in DURATIONS_DAYS if d * 86400 not in already_open_secs]
    already_open_days = [d for d in DURATIONS_DAYS if d not in to_create]
    if already_open_days:
        log.info("Skipping %s — already have a live offer of ours at that duration.",
                  ", ".join(f"{d}d" for d in already_open_days))
    if not to_create:
        log.info("Nothing to do — every duration (%s) already has a live offer.",
                  ", ".join(f"{d}d" for d in DURATIONS_DAYS))
        return

    min_fill = max(1001, int(MIN_FILL_USDC * 10 ** USDC_DECIMALS))
    log.info("=" * 60)
    log.info("Creating %d offer(s): %s", len(to_create), ", ".join(f"{d}d" for d in to_create))
    log.info("=" * 60)

    successes = errors = 0
    for days in to_create:
        params = {
            "signer": WALLET_PUBKEY,
            "principalMint": USDC_MINT,
            "collateralMint": collateral_mint,
            "principalAmount": principal_raw,
            "collateralAmount": collateral_raw,
            "apy": FIXED_APY_BPS,
            "duration": days * 86400,
            "expiry": OFFER_EXPIRY_SECS,
            "allowPartialFill": ALLOW_PARTIAL_FILL,
            "minFillAmount": min_fill,
            "topup": "minimum",
        }
        if create_offer(params):
            successes += 1
        else:
            errors += 1

    log.info("=" * 60)
    log.info("Done.  Created=%d  Skipped=%d  Errors=%d", successes, len(already_open_days), errors)
    log.info("=" * 60)


if __name__ == "__main__":
    main()
