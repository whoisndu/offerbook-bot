"""
Offerbook Manual Offer Creator
================================
Terminal replica of the Offerbook web UI's "You Lend" offer-creation flow —
for when the UI itself is down, flaky, or wallet-disconnecting, and you
just need to post the exact offer you already decided on (e.g. copied
straight off a UI preview screen that wouldn't submit). Unlike strategy.py/
fixed_terms_offers.py/defaulter_capture.py, this creates exactly ONE offer
from values YOU give it — no market benchmarking, no allocation config, no
looping over pairs/durations.

You specify collateral + principal + either an LTV% (collateral amount is
then computed from the live price, same as the UI does) or an exact
collateral amount (when you already have the precise figure, e.g. from a
UI screenshot) + APR + duration. Only USDC principal is supported (matches
every other script here).

Offer listing expiry defaults to the SAME length as the loan duration
(matching what the UI itself defaults to) — NOT the fixed 24h
(OFFER_EXPIRY_SECS) every other script in this repo uses, since those are
optimized for being re-run daily, and this one is a single deliberate
action. Override with --expiry-days if you want something else.

topup defaults to "minimum" (real funding now, so the offer is actually
isFunded:true and fillable immediately) rather than "none" — unlike the
automated scripts, this creates exactly one offer per run, so there's no
shared-balance-across-many-offers reason to leave it underfunded. Pass
--topup none if you specifically want to stack this alongside other
offers sharing the same balance (rehypothecation) instead.

Usage:
  python create_manual_offer.py --collateral USELESS --principal 3985 --ltv 25 --apr 80 --duration 7
  python create_manual_offer.py --collateral USELESS --principal 3985 --collateral-amount 114218.42 --apr 80 --duration 7
  python create_manual_offer.py   # prompts for every required value
  python create_manual_offer.py --collateral USELESS --principal 3985 --ltv 25 --apr 80 --duration 7 --ledger-path "44'/501'/1'"
  DRY_RUN=true python create_manual_offer.py ...   # preview only, no submission
"""
from __future__ import annotations

from dotenv import load_dotenv
load_dotenv()

import argparse
import base64
import logging
import os
import sys

import requests

# Shared modules live in ../lib — see README's repo-layout note.
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_THIS_DIR, "..", "lib"))

import offerbook_common as _common

# ---------------------------------------------------------------------------
# Configuration — same environment variables as strategy.py
# ---------------------------------------------------------------------------

API_BASE = os.getenv("OFFERBOOK_API_BASE", "https://api.offerbook.jup.ag/api/v1")
TX_API_BASE = os.getenv("OFFERBOOK_TX_API_BASE", "https://builder.offerbook.jup.ag/api/v1")
SOLANA_RPC = os.getenv("SOLANA_RPC", "https://api.mainnet-beta.solana.com")

WALLET_PUBKEY: str = os.getenv("OFFERBOOK_WALLET", "")
PRIVATE_KEY_B58: str = os.getenv("OFFERBOOK_PRIVATE_KEY", "")

SIGNING_MODE: str = os.getenv("OFFERBOOK_SIGNING_MODE", "ledger").strip().lower()
LEDGER_PATH: str = os.getenv("OFFERBOOK_LEDGER_PATH", "44'/501'/0'")

DRY_RUN: bool = os.getenv("DRY_RUN", "true").lower() in ("1", "true", "yes")

USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDC_DECIMALS = 6
MIN_FILL_USDC = 10.0

JUPITER_PRICE_API = "https://api.jup.ag/price/v3"
JUPITER_API_KEY = os.getenv("JUPITER_API_KEY", "")
DEXSCREENER_API = "https://api.dexscreener.com/latest/dex/tokens"

KNOWN_SYMBOLS = _common.KNOWN_SYMBOLS
KNOWN_DECIMALS = _common.KNOWN_DECIMALS
SYMBOL_TO_MINT = {sym.upper(): mint for mint, sym in KNOWN_SYMBOLS.items()}

_CONFIG_PATH = os.getenv(
    "ALLOCATION_CONFIG",
    os.path.join(os.path.dirname(__file__), "allocation_config.yaml"),
)
_allocation_symbol_to_mint_cache: dict[str, str] | None = None

# Named Ledger accounts in use, for the interactive account-picker prompt
# below — same labeling convenience cancel_offers.py/fill_offer.py use. Any
# derivation path works via --ledger-path or the prompt's "custom" option.
KNOWN_LEDGER_ACCOUNTS: dict[str, tuple[str, str]] = {
    "1": ("44'/501'/0'", "Original / general strategy account"),
    "2": ("44'/501'/1'", "Targeted-offers account"),
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("create_manual_offer")

SESSION = requests.Session()


# ---------------------------------------------------------------------------
# Collateral resolution — identical 3-step lookup to strategy.py/
# fixed_terms_offers.py's resolve_collateral_token()
# ---------------------------------------------------------------------------

def resolve_collateral_token(tok: str) -> str:
    global _allocation_symbol_to_mint_cache
    tok = tok.strip()
    upper = tok.upper()
    if upper in SYMBOL_TO_MINT:
        return SYMBOL_TO_MINT[upper]

    if _allocation_symbol_to_mint_cache is None:
        _allocation_symbol_to_mint_cache = _common.build_symbol_to_mint_from_allocation_config(_CONFIG_PATH)
    if upper in _allocation_symbol_to_mint_cache:
        return _allocation_symbol_to_mint_cache[upper]

    log.warning(
        "Collateral token '%s' isn't in SYMBOL_TO_MINT or named in allocation_config.yaml's "
        "comments — treating it as a raw mint address. If that's a typo, this will silently "
        "fail to resolve a price/decimals.", tok,
    )
    return tok


def symbol_for(mint: str) -> str:
    return KNOWN_SYMBOLS.get(mint, mint[:6] + "…" if mint else "?")


# ---------------------------------------------------------------------------
# Price lookup — same approach as defaulter_capture.py's fetch_current_price
# ---------------------------------------------------------------------------

def _fetch_dexscreener_price(mint: str) -> float | None:
    try:
        resp = SESSION.get(f"{DEXSCREENER_API}/{mint}", timeout=10)
        if not resp.ok:
            return None
        pairs = resp.json().get("pairs") or []
        sol_pairs = [p for p in pairs if p.get("chainId") == "solana" and p.get("priceUsd")]
        if not sol_pairs:
            return None
        sol_pairs.sort(key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0), reverse=True)
        return float(sol_pairs[0]["priceUsd"])
    except Exception:
        return None


def fetch_current_price(mint: str) -> tuple[float | None, int | None]:
    """Return (usd_price_per_whole_token, decimals) for a single collateral mint."""
    try:
        headers = {"x-api-key": JUPITER_API_KEY} if JUPITER_API_KEY else {}
        resp = SESSION.get(JUPITER_PRICE_API, params={"ids": mint}, headers=headers, timeout=15)
        resp.raise_for_status()
        info = resp.json().get(mint)
        if info and info.get("usdPrice"):
            return float(info["usdPrice"]), int(info["decimals"]) if info.get("decimals") is not None else KNOWN_DECIMALS.get(mint)
    except Exception as exc:
        log.warning("Jupiter price fetch failed for %s: %s", mint[:8], exc)

    price = _fetch_dexscreener_price(mint)
    return price, KNOWN_DECIMALS.get(mint)


# ---------------------------------------------------------------------------
# Balance lookup — same approach as every other script here
# ---------------------------------------------------------------------------

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
    holdings = _common.api_get(SESSION, API_BASE, f"/escrows/holdings/{WALLET_PUBKEY}")
    for entry in holdings:
        if entry.get("asset", {}).get("mint") == mint:
            return int(entry.get("amount", 0))
    return 0


# ---------------------------------------------------------------------------
# Signing — identical plumbing to defaulter_capture.py/fill_offer.py
# ---------------------------------------------------------------------------

def resolve_signer_wallet() -> str:
    global WALLET_PUBKEY
    WALLET_PUBKEY = _common.resolve_signer_wallet(SIGNING_MODE, WALLET_PUBKEY, LEDGER_PATH)
    return WALLET_PUBKEY


def confirm_signing_mode(skip_prompt: bool) -> None:
    _common.confirm_signing_mode(SIGNING_MODE, WALLET_PUBKEY, LEDGER_PATH, DRY_RUN, skip_prompt)


def _broadcast(signed_b64: str) -> str:
    payload = {
        "jsonrpc": "2.0", "id": 1, "method": "sendTransaction",
        "params": [signed_b64, {"encoding": "base64", "skipPreflight": False}],
    }
    resp = requests.post(SOLANA_RPC, json=payload, timeout=30)
    resp.raise_for_status()
    result = resp.json()
    if "error" in result:
        log.error("  RPC error: %s", result["error"])
        return ""
    return result["result"]


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
        return _broadcast(signed_b64)
    else:
        import base58
        from solders.keypair import Keypair
        from solders.transaction import VersionedTransaction
        keypair = Keypair.from_bytes(base58.b58decode(PRIVATE_KEY_B58))
        raw_tx = base64.b64decode(tx_b64)
        tx = VersionedTransaction.from_bytes(raw_tx)
        signed_tx = VersionedTransaction(tx.message, [keypair])
        return _broadcast(base64.b64encode(bytes(signed_tx)).decode())


def create_offer(params: dict) -> bool:
    if DRY_RUN:
        log.info("  [DRY RUN] Skipping transaction submission.")
        return True

    try:
        tx_data = _common.post_tx(SESSION, TX_API_BASE, "/create-principal-offer", params)
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
        log.warning("  OFFERBOOK_PRIVATE_KEY not set – cannot sign.")
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
# Interactive prompts — same "omit to be prompted" convention as every
# other script here
# ---------------------------------------------------------------------------

def prompt_str(label: str) -> str:
    raw = input(f"{label}: ").strip()
    while not raw:
        raw = input(f"{label} (required): ").strip()
    return raw


def prompt_float(label: str) -> float:
    while True:
        raw = input(f"{label}: ").strip()
        try:
            return float(raw)
        except ValueError:
            print("  Enter a number.")


def prompt_int(label: str) -> int:
    while True:
        raw = input(f"{label}: ").strip()
        try:
            return int(raw)
        except ValueError:
            print("  Enter a whole number.")


def prompt_ledger_path() -> str:
    print()
    print("Which account do you want to create this offer from?")
    print()
    for key, (path, label) in KNOWN_LEDGER_ACCOUNTS.items():
        print(f"  [{key}]  {label}  ({path})")
    print(f"  [c]  Custom derivation path")
    print()
    while True:
        choice = input("Choice: ").strip().lower()
        if choice in KNOWN_LEDGER_ACCOUNTS:
            return KNOWN_LEDGER_ACCOUNTS[choice][0]
        if choice == "c":
            return input("Derivation path (e.g. 44'/501'/2'): ").strip()
        print(f"  Please enter one of: {', '.join(KNOWN_LEDGER_ACCOUNTS)}, or 'c' for custom")


def main() -> None:
    global SIGNING_MODE, WALLET_PUBKEY, LEDGER_PATH, DRY_RUN

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--collateral", default=None, help="Collateral symbol or mint address. Omit to be prompted.")
    parser.add_argument("--principal", type=float, default=None, help="USDC principal to lend. Omit to be prompted.")
    sizing = parser.add_mutually_exclusive_group()
    sizing.add_argument("--ltv", type=float, default=None, help="Target LTV as a percent (e.g. 25) — collateral amount is computed from the live price.")
    sizing.add_argument("--collateral-amount", type=float, default=None, help="Exact collateral amount (whole tokens) — use when you already know the precise figure.")
    parser.add_argument("--apr", type=float, default=None, help="APR as a percent (e.g. 80). Omit to be prompted.")
    parser.add_argument("--duration", type=int, default=None, help="Loan duration in days. Omit to be prompted.")
    parser.add_argument("--expiry-days", type=int, default=None, help="Offer listing expiry in days. Defaults to the same value as --duration.")
    parser.add_argument("--no-partial-fill", action="store_true", help="Require a single full fill instead of allowing partial fills.")
    parser.add_argument("--min-fill", type=float, default=MIN_FILL_USDC, help=f"Minimum USDC a partial fill must take (default ${MIN_FILL_USDC:.0f}).")
    parser.add_argument("--topup", default="minimum", choices=["minimum", "full", "none"],
                         help='Escrow funding mode at creation (default "minimum" — fully funds this single offer now).')
    parser.add_argument("--ledger-path", default=None, help="Ledger derivation path (skips the account prompt)")
    _common.add_signing_args(parser)
    args = parser.parse_args()

    SIGNING_MODE = _common.resolve_signing_mode(args.signing_mode, SIGNING_MODE)
    if SIGNING_MODE == "ledger":
        LEDGER_PATH = args.ledger_path or prompt_ledger_path()
    if SIGNING_MODE == "private_key" and not PRIVATE_KEY_B58:
        log.error("OFFERBOOK_PRIVATE_KEY not set in .env — required for --private-key mode.")
        sys.exit(1)

    collateral_tok = args.collateral or prompt_str("Collateral symbol or mint address")
    collateral_mint = resolve_collateral_token(collateral_tok)

    principal_usdc = args.principal if args.principal is not None else prompt_float("Principal to lend (USDC)")
    if principal_usdc <= 0:
        log.error("Principal must be positive.")
        sys.exit(1)

    ltv_pct = args.ltv
    collateral_amount_whole = args.collateral_amount
    if ltv_pct is None and collateral_amount_whole is None:
        choice = input("Specify sizing by [L]TV% (computed from live price) or [A]mount (exact collateral tokens)? [L/a]: ").strip().lower()
        if choice == "a":
            collateral_amount_whole = prompt_float("Exact collateral amount (whole tokens)")
        else:
            ltv_pct = prompt_float("Target LTV (percent, e.g. 25)")

    price = decimals = None
    if collateral_amount_whole is not None:
        # Still need decimals to convert to raw units, and price for the preview.
        price, decimals = fetch_current_price(collateral_mint)
        if decimals is None:
            log.error("Could not resolve decimals for %s — pass a known SYMBOL or set KNOWN_DECIMALS.", collateral_tok)
            sys.exit(1)
    else:
        price, decimals = fetch_current_price(collateral_mint)
        if not price or decimals is None:
            log.error("Could not fetch a live price for %s — can't compute collateral amount from LTV. "
                      "Pass --collateral-amount instead.", collateral_tok)
            sys.exit(1)
        collateral_usd = principal_usdc / (ltv_pct / 100)
        collateral_amount_whole = collateral_usd / price

    apr_pct = args.apr if args.apr is not None else prompt_float("APR (percent, e.g. 80)")
    apy_bps = round(apr_pct * 100)

    duration_days = args.duration if args.duration is not None else prompt_int("Loan duration (days)")
    if duration_days <= 0:
        log.error("Duration must be positive.")
        sys.exit(1)
    expiry_days = args.expiry_days if args.expiry_days is not None else duration_days

    principal_raw = round(principal_usdc * 10 ** USDC_DECIMALS)
    collateral_raw = round(collateral_amount_whole * 10 ** decimals)
    duration_secs = duration_days * 86400
    expiry_secs = expiry_days * 86400
    allow_partial_fill = not args.no_partial_fill
    min_fill_raw = max(1001, round(args.min_fill * 10 ** USDC_DECIMALS))
    if allow_partial_fill and min_fill_raw * 2 > principal_raw:
        min_fill_raw = principal_raw // 2
        log.warning("--min-fill was more than half the principal — capped to $%.2f (API requires minFillAmount <= 50%% of principal).",
                    min_fill_raw / 10 ** USDC_DECIMALS)

    resolved_ltv_pct = ltv_pct if ltv_pct is not None else (
        (principal_usdc / (collateral_amount_whole * price) * 100) if price else None
    )

    resolve_signer_wallet()
    confirm_signing_mode(skip_prompt=args.yes)

    wallet_raw = fetch_wallet_token_balance(USDC_MINT)
    escrow_raw = fetch_escrow_balance(USDC_MINT)
    total_raw = wallet_raw + escrow_raw
    log.info("USDC balance — wallet=%.2f  escrow=%.2f  total=%.2f", wallet_raw / 1e6, escrow_raw / 1e6, total_raw / 1e6)
    if args.topup != "none" and principal_raw > total_raw:
        log.warning("Principal ($%.2f) exceeds your total wallet+escrow balance ($%.2f) — this will likely fail "
                    "unless you lower the amount or pass --topup none to post it as underfunded instead.",
                    principal_usdc, total_raw / 1e6)

    interest_usdc = principal_usdc * (apr_pct / 100) * (duration_days / 365)
    log.info("=" * 60)
    log.info("PREVIEW")
    log.info("  Signer         : %s", WALLET_PUBKEY)
    log.info("  You lend       : %.2f USDC", principal_usdc)
    collateral_display = f"{collateral_amount_whole:,.2f}" if collateral_amount_whole >= 1 else f"{collateral_amount_whole:,.6g}"
    log.info("  Backed by      : %s %s", collateral_display, symbol_for(collateral_mint))
    log.info("  LTV            : %s", f"{resolved_ltv_pct:.2f}%" if resolved_ltv_pct is not None else "n/a (no live price)")
    log.info("  APR            : %.2f%%", apr_pct)
    log.info("  Duration       : %d day(s)", duration_days)
    log.info("  Expires in     : %d day(s)", expiry_days)
    log.info("  Interest       : ~%.2f USDC (est.)", interest_usdc)
    log.info("  Repay at maturity (est.) : ~%.2f USDC", principal_usdc + interest_usdc)
    log.info("  Partial fill   : %s%s", "allowed" if allow_partial_fill else "not allowed",
              f", min ${min_fill_raw / 1e6:,.2f}" if allow_partial_fill else "")
    log.info("  Topup mode     : %s", args.topup)
    log.info("=" * 60)

    if not args.yes:
        choice = input("Create this offer? [y/N] ").strip().lower()
        if choice not in ("y", "yes"):
            log.info("Aborted by user.")
            sys.exit(0)

    params = {
        "signer": WALLET_PUBKEY,
        "principalMint": USDC_MINT,
        "collateralMint": collateral_mint,
        "principalAmount": principal_raw,
        "collateralAmount": collateral_raw,
        "apy": apy_bps,
        "duration": duration_secs,
        "expiry": expiry_secs,
        "allowPartialFill": allow_partial_fill,
        "minFillAmount": min_fill_raw,
        "topup": args.topup,
    }

    ok = create_offer(params)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
