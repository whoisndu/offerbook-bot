"""
Offerbook Update Offer Terms
================================
Lets you change the terms of one of your own still-live lending offers
(APR, principal, collateral/LTV, duration, expiry, partial-fill settings)
from the terminal — the console equivalent of the UI's "Update offer"
action.

There's no dedicated "update" instruction on-chain. What the UI almost
certainly does under the hood (confirmed against the Transaction Builder
API's own OpenAPI schema, and verified live by building an actual unsigned
transaction) is pass `offerToCancel: <old offer pubkey>` to the SAME
POST /create-principal-offer endpoint strategy.py/create_manual_offer.py
already use — the builder bundles the old offer's cancellation and the
new offer's creation into ONE atomic transaction, so either both happen
or neither does (no window where you're exposed with neither offer live).
This script does exactly that.

You only need to specify what's actually CHANGING — every field you don't
pass defaults to that offer's current value, fetched fresh via
GET /offer/{pubkey} right before building the transaction (so you're
always updating off the live state, not a stale snapshot). If you change
--principal without also specifying --ltv or --collateral-amount, the
collateral amount is automatically rescaled to PRESERVE the offer's
current LTV (at today's live price) rather than silently leaving the raw
collateral amount fixed, which would otherwise change your LTV as a side
effect of a principal change you didn't ask for.

Run with NO override flags at all and you're asked interactively which
terms to change (current values shown alongside each option) rather than
silently proceeding with a no-op "nothing changed" update — same "never
guess" convention the rest of this repo's interactive prompts use. Pass
any override flag and that prompt is skipped entirely (power-user path).
The Ledger account picker/signing step comes LAST, after you've decided
what's actually changing, not first. The interactive menu also offers
"cancel this offer outright" as one of the options — for when you started
out meaning to update it and realized you'd rather just kill it; this
path uses the plain POST /cancel-offer endpoint (cancel_offers.py's own
endpoint), not offerToCancel, since there's no replacement to bundle with.

Refuses to run if the resolved signer wallet isn't the offer's own
creator (wrong account selected), and if the offer is no longer live
(already filled/cancelled/expired — nothing to update). Only USDC-
principal offers are supported, matching every other script here.

topup defaults to "none", same as every other offer-creation script in
this repo (see strategy/README.md's "Offer funding: topup modes") — pass
--topup minimum if you want the new offer guaranteed-funded immediately.

Usage:
  python update_offer_terms.py --offer <pubkey>                               # prompts for what to change
  python update_offer_terms.py --offer <pubkey> --apr 90                      # just reprice
  python update_offer_terms.py --offer <pubkey> --principal 5000              # LTV auto-preserved
  python update_offer_terms.py --offer <pubkey> --principal 5000 --ltv 30     # explicit new LTV
  python update_offer_terms.py --offer <pubkey> --duration 7 --apr 60
  python update_offer_terms.py   # prompts for the offer pubkey, shows current terms, asks what to change
  DRY_RUN=true python update_offer_terms.py --offer <pubkey> --apr 90   # preview only, no submission
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
# Configuration — same environment variables as create_manual_offer.py
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

JUPITER_PRICE_API = "https://api.jup.ag/price/v3"
JUPITER_API_KEY = os.getenv("JUPITER_API_KEY", "")
DEXSCREENER_API = "https://api.dexscreener.com/latest/dex/tokens"

KNOWN_SYMBOLS = _common.KNOWN_SYMBOLS
KNOWN_DECIMALS = _common.KNOWN_DECIMALS

# Same account picker as cancel_offers.py/fill_offer.py/create_manual_offer.py
# — this script has no "right" account either, it depends whose offer you're
# updating.
KNOWN_LEDGER_ACCOUNTS: dict[str, tuple[str, str]] = {
    "1": ("44'/501'/0'", "Original / general strategy account"),
    "2": ("44'/501'/1'", "Targeted-offers account"),
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("update_offer_terms")

SESSION = requests.Session()


def symbol_for(mint: str) -> str:
    return KNOWN_SYMBOLS.get(mint, mint[:6] + "…" if mint else "?")


# ---------------------------------------------------------------------------
# Price lookup — same approach as create_manual_offer.py
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
        log.warning("Jupiter price fetch failed for %s: %s", mint[:8] if mint else mint, exc)

    price = _fetch_dexscreener_price(mint)
    return price, KNOWN_DECIMALS.get(mint)


# ---------------------------------------------------------------------------
# Offer lookup
# ---------------------------------------------------------------------------

def fetch_offer(pubkey: str) -> dict | None:
    try:
        return _common.api_get(SESSION, API_BASE, f"/offer/{pubkey}")
    except requests.HTTPError as exc:
        if exc.response is not None and exc.response.status_code == 404:
            return None
        raise


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
# Signing — identical plumbing to create_manual_offer.py
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


def submit_update(params: dict) -> bool:
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


def submit_cancel(offer_pubkey: str) -> bool:
    """Plain cancellation, no replacement offer — POST /cancel-offer, same
    endpoint cancel_offers.py uses. Response key name varies
    (transactions/transaction/tx depending on batch size), handled the same
    flexible way cancel_offers.py's cancel_batch() does."""
    payload = {"signer": WALLET_PUBKEY, "offer": offer_pubkey, "withdraw": "none"}
    if DRY_RUN:
        log.info("  [DRY RUN] Would POST /cancel-offer with: %s", payload)
        return True

    try:
        data = _common.post_tx(SESSION, TX_API_BASE, "/cancel-offer", payload)
    except requests.HTTPError as exc:
        body = exc.response.text if exc.response else ""
        log.error("  TX builder error: %s\n  body: %s", exc, body or "(empty)")
        return False
    except requests.ConnectionError as exc:
        log.error("  TX builder connection error: %s", exc)
        return False

    if data.get("transactions"):
        transactions = data["transactions"]
    elif data.get("transaction"):
        transactions = [data["transaction"]]
    elif data.get("tx"):
        transactions = [data["tx"]]
    else:
        log.error("  Unexpected cancel-offer response: %s", data)
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
# Interactive prompts
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


def prompt_what_to_change(current: dict) -> dict:
    """Shown when no override flags were given at all — asks which terms to
    change and their new values, rather than silently proceeding with a
    no-op "CURRENT == NEW" update. Returns a dict of overrides for
    whichever fields were picked (empty if the user picked nothing)."""
    ltv_str = f"{current['ltv_pct']:.2f}%" if current["ltv_pct"] is not None else "n/a"
    print()
    print("What do you want to change? (current values shown)")
    print()
    print(f"  [1]  Principal                — {current['principal_usd']:,.2f} USDC")
    print(f"  [2]  Collateral / LTV         — {current['collateral_whole']:,.2f} {current['collateral_symbol']}  (LTV {ltv_str})")
    print(f"  [3]  APR                      — {current['apr_pct']:.2f}%")
    print(f"  [4]  Duration                 — {current['duration_days']:.0f} day(s)")
    print(f"  [5]  Expiry                   — defaults to the (possibly new) duration unless changed here")
    print(f"  [6]  Partial fill / min fill  — {'allowed' if current['allow_partial_fill'] else 'not allowed'}, min ${current['min_fill_usd']:,.2f}")
    print(f"  [7]  Cancel this offer        — no replacement, just cancel it outright")
    print()
    raw = input("Enter numbers to change, comma-separated (e.g. 1,3), or press Enter to abort without changes: ").strip()
    if not raw:
        return {}

    picks = {p.strip() for p in raw.split(",") if p.strip()}
    if "7" in picks:
        if len(picks) > 1:
            print("  '7' (cancel) was picked along with other options — cancelling takes priority; ignoring the rest.")
        return {"cancel_only": True}

    overrides: dict = {}
    if "1" in picks:
        overrides["principal"] = prompt_float(f"  New principal (USDC), currently {current['principal_usd']:,.2f}")
    if "2" in picks:
        choice = input(
            "  Specify by [L]TV% or [A]mount (exact collateral tokens)? Enter 'L'/'A', or type "
            f"the LTV number directly (currently {ltv_str}) [L/a]: "
        ).strip().lower()
        if choice == "a":
            overrides["collateral_amount"] = prompt_float("  New collateral amount (whole tokens)")
        elif choice not in ("", "l"):
            # Not a recognized letter — try it as the LTV value itself, same
            # fix as create_manual_offer.py's identical prompt.
            try:
                overrides["ltv"] = float(choice)
            except ValueError:
                print(f"    '{choice}' isn't 'L', 'A', or a number — asking separately.")
                overrides["ltv"] = prompt_float(f"  New LTV (percent), currently {ltv_str}")
        else:
            overrides["ltv"] = prompt_float(f"  New LTV (percent), currently {ltv_str}")
    if "3" in picks:
        overrides["apr"] = prompt_float(f"  New APR (percent), currently {current['apr_pct']:.2f}")
    if "4" in picks:
        overrides["duration"] = prompt_int(f"  New duration (days), currently {current['duration_days']:.0f}")
    if "5" in picks:
        overrides["expiry_days"] = prompt_int("  New expiry (days)")
    if "6" in picks:
        pf_choice = input("  Allow partial fill? [y/N]: ").strip().lower()
        overrides["allow_partial_fill"] = pf_choice in ("y", "yes")
        if overrides["allow_partial_fill"]:
            overrides["min_fill"] = prompt_float("  Minimum USDC a partial fill must take")
    return overrides


def prompt_ledger_path() -> str:
    print()
    print("Which account does this offer belong to?")
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


def resolve_and_verify_signer(args: argparse.Namespace, offer: dict) -> None:
    """Shared by both the update and cancel-only paths: resolve the signer
    (Ledger account prompt or hot wallet), confirm the signing mode, and
    refuse to continue if the resolved wallet isn't this offer's own
    creator (wrong account selected)."""
    global SIGNING_MODE, WALLET_PUBKEY, LEDGER_PATH

    SIGNING_MODE = _common.resolve_signing_mode(args.signing_mode, SIGNING_MODE)
    if SIGNING_MODE == "ledger":
        LEDGER_PATH = args.ledger_path or prompt_ledger_path()
    if SIGNING_MODE == "private_key" and not PRIVATE_KEY_B58:
        log.error("OFFERBOOK_PRIVATE_KEY not set in .env — required for --private-key mode.")
        sys.exit(1)

    resolve_signer_wallet()
    confirm_signing_mode(skip_prompt=args.yes)

    creator = offer.get("creator", "")
    if WALLET_PUBKEY != creator:
        log.error(
            "This offer belongs to %s, not the resolved signer %s — wrong account? "
            "Pick the account matching this offer's creator.", creator, WALLET_PUBKEY,
        )
        sys.exit(1)


def run_cancel_only(args: argparse.Namespace, offer_pubkey: str, offer: dict, current: dict) -> None:
    """Picked [7] in the interactive menu — cancel the offer outright, no
    replacement created. Separate code path from the update flow: this
    hits POST /cancel-offer (cancel_offers.py's endpoint), not
    /create-principal-offer with offerToCancel."""
    resolve_and_verify_signer(args, offer)

    log.info("=" * 78)
    log.info("CANCEL OFFER — %s", offer_pubkey)
    log.info("=" * 78)
    collateral_str = f"{current['collateral_whole']:,.2f}" if current["collateral_whole"] >= 1 else f"{current['collateral_whole']:,.6g}"
    log.info("Principal   : %.2f USDC", current["principal_usd"])
    log.info("Collateral  : %s %s", collateral_str, current["collateral_symbol"])
    log.info("APR         : %.2f%%", current["apr_pct"])
    log.info("Duration    : %d day(s)", round(current["duration_days"]))
    log.info("=" * 78)
    log.info("This cancels the offer outright — no replacement will be created.")

    if not args.yes:
        choice = input("\nCancel this offer? [y/N] ").strip().lower()
        if choice not in ("y", "yes"):
            log.info("Aborted by user.")
            sys.exit(0)

    ok = submit_cancel(offer_pubkey)
    sys.exit(0 if ok else 1)


def main() -> None:
    global SIGNING_MODE, WALLET_PUBKEY, LEDGER_PATH, DRY_RUN

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--offer", default=None, help="Pubkey of the live offer to update. Omit to be prompted.")
    parser.add_argument("--principal", type=float, default=None, help="New USDC principal. Default: keep current.")
    sizing = parser.add_mutually_exclusive_group()
    sizing.add_argument("--ltv", type=float, default=None, help="New target LTV as a percent (e.g. 30). Default: preserve current LTV at today's price.")
    sizing.add_argument("--collateral-amount", type=float, default=None, help="New exact collateral amount (whole tokens). Default: preserve current LTV.")
    parser.add_argument("--apr", type=float, default=None, help="New APR as a percent (e.g. 90). Default: keep current.")
    parser.add_argument("--duration", type=int, default=None, help="New loan duration in days. Default: keep current.")
    parser.add_argument("--expiry-days", type=int, default=None, help="New offer listing expiry in days. Default: same as the (possibly new) duration.")
    partial = parser.add_mutually_exclusive_group()
    partial.add_argument("--allow-partial-fill", action="store_true", help="Force partial fill on, regardless of the current setting.")
    partial.add_argument("--no-partial-fill", action="store_true", help="Force partial fill off, regardless of the current setting.")
    parser.add_argument("--min-fill", type=float, default=None, help="New minimum USDC a partial fill must take. Default: keep current.")
    parser.add_argument("--topup", default="none", choices=["minimum", "full", "none"],
                         help='Escrow funding mode for the replacement offer (default "none" — see '
                              'strategy/README.md\'s "Offer funding: topup modes").')
    parser.add_argument("--ledger-path", default=None, help="Ledger derivation path (skips the account prompt)")
    _common.add_signing_args(parser)
    args = parser.parse_args()

    offer_pubkey = args.offer or prompt_str("Pubkey of the offer to update")

    log.info("Fetching current terms for %s …", offer_pubkey)
    offer = fetch_offer(offer_pubkey)
    if offer is None:
        log.error("Offer %s not found.", offer_pubkey)
        sys.exit(1)
    if offer.get("status") not in ("active", "partiallyFilled"):
        log.error("Offer %s is %s, not live — nothing to update.", offer_pubkey, offer.get("status"))
        sys.exit(1)
    if offer.get("offerType") != "lending":
        log.error("Only lending offers are supported by this script (this one is %s).", offer.get("offerType"))
        sys.exit(1)

    principal_mint = offer.get("principalMint") or (offer.get("principal") or {}).get("mint")
    collateral_mint = offer.get("collateralMint") or (offer.get("collateral") or {}).get("mint")
    if principal_mint != USDC_MINT:
        log.error("Only USDC-principal offers are supported by this script (this one lends %s).", principal_mint)
        sys.exit(1)

    # Current state — fetched fresh above, so this reflects any fills that
    # have already happened, not a stale snapshot.
    current_principal_raw = offer.get("remainingPrincipal", offer.get("principalAmount", 0))
    current_collateral_raw = offer.get("remainingCollateral", offer.get("collateralAmount", 0))
    current_apy_bps = offer.get("apy", 0)
    current_duration_secs = offer.get("duration", 0)
    current_allow_partial_fill = offer.get("allowPartialFill", True)
    current_min_fill_raw = offer.get("minFillAmount", 0)

    price, decimals = fetch_current_price(collateral_mint)
    if decimals is None:
        log.error("Could not resolve decimals for %s — can't size collateral. Add it to "
                  "offerbook_common.KNOWN_DECIMALS and retry.", symbol_for(collateral_mint))
        sys.exit(1)

    current_principal_usd = current_principal_raw / 10 ** USDC_DECIMALS
    current_collateral_whole = current_collateral_raw / 10 ** decimals
    current_collateral_usd = current_collateral_whole * price if price else None
    current_ltv_pct = (current_principal_usd / current_collateral_usd * 100) if current_collateral_usd else None

    # --- What's changing: explicit CLI flags, or (if none given at all) an
    # interactive prompt asking what to change — never silently proceeds
    # with a no-op "nothing changed" update. ---
    principal_override = args.principal
    ltv_override = args.ltv
    collateral_amount_override = args.collateral_amount
    apr_override = args.apr
    duration_override = args.duration
    expiry_override = args.expiry_days
    allow_partial_fill_override = True if args.allow_partial_fill else (False if args.no_partial_fill else None)
    min_fill_override = args.min_fill

    any_cli_override = any(v is not None for v in (
        principal_override, ltv_override, collateral_amount_override, apr_override,
        duration_override, expiry_override, min_fill_override,
    )) or allow_partial_fill_override is not None

    current = {
        "principal_usd": current_principal_usd,
        "collateral_whole": current_collateral_whole,
        "collateral_symbol": symbol_for(collateral_mint),
        "ltv_pct": current_ltv_pct,
        "apr_pct": current_apy_bps / 100,
        "duration_days": current_duration_secs / 86400,
        "allow_partial_fill": current_allow_partial_fill,
        "min_fill_usd": current_min_fill_raw / 10 ** USDC_DECIMALS,
    }

    if not any_cli_override:
        overrides = prompt_what_to_change(current)
        if not overrides:
            log.info("Nothing to change — aborting.")
            sys.exit(0)
        if overrides.get("cancel_only"):
            run_cancel_only(args, offer_pubkey, offer, current)
            return  # run_cancel_only always sys.exit()s; return is unreachable but keeps intent clear
        principal_override = overrides.get("principal")
        ltv_override = overrides.get("ltv")
        collateral_amount_override = overrides.get("collateral_amount")
        apr_override = overrides.get("apr")
        duration_override = overrides.get("duration")
        expiry_override = overrides.get("expiry_days")
        if "allow_partial_fill" in overrides:
            allow_partial_fill_override = overrides["allow_partial_fill"]
        min_fill_override = overrides.get("min_fill")

    # --- Resolve new terms: explicit override, else keep current ---
    new_principal_usd = principal_override if principal_override is not None else current_principal_usd

    if collateral_amount_override is not None:
        new_collateral_whole = collateral_amount_override
    elif ltv_override is not None:
        if not price:
            log.error("No live price for %s — can't size by --ltv. Pass --collateral-amount instead.", symbol_for(collateral_mint))
            sys.exit(1)
        new_collateral_whole = new_principal_usd / (ltv_override / 100) / price
    elif new_principal_usd != current_principal_usd and current_ltv_pct is not None and price:
        # Principal changed but sizing wasn't specified — preserve the
        # offer's current LTV rather than silently leaving collateral fixed
        # (which would change LTV as an unintended side effect).
        new_collateral_whole = new_principal_usd / (current_ltv_pct / 100) / price
    else:
        new_collateral_whole = current_collateral_whole

    new_apr_pct = apr_override if apr_override is not None else current_apy_bps / 100
    new_apy_bps = round(new_apr_pct * 100)

    new_duration_days = duration_override if duration_override is not None else current_duration_secs / 86400
    if new_duration_days <= 0:
        log.error("Duration must be positive.")
        sys.exit(1)
    new_expiry_days = expiry_override if expiry_override is not None else new_duration_days

    new_allow_partial_fill = allow_partial_fill_override if allow_partial_fill_override is not None else current_allow_partial_fill

    new_min_fill_usd = min_fill_override if min_fill_override is not None else current_min_fill_raw / 10 ** USDC_DECIMALS

    principal_raw = round(new_principal_usd * 10 ** USDC_DECIMALS)
    collateral_raw = round(new_collateral_whole * 10 ** decimals)
    duration_secs = round(new_duration_days * 86400)
    expiry_secs = round(new_expiry_days * 86400)
    min_fill_raw = max(1001, round(new_min_fill_usd * 10 ** USDC_DECIMALS))
    if new_allow_partial_fill and min_fill_raw * 2 > principal_raw:
        min_fill_raw = principal_raw // 2
        log.warning("min-fill was more than half the new principal — capped to $%.2f.", min_fill_raw / 10 ** USDC_DECIMALS)

    new_ltv_pct = (new_principal_usd / (new_collateral_whole * price) * 100) if (price and new_collateral_whole) else None

    # Now that we know exactly what's changing, resolve the signer — Ledger
    # interaction is the last thing before the final confirm, not the first
    # thing you're asked for.
    resolve_and_verify_signer(args, offer)

    if args.topup != "none":
        wallet_raw = fetch_wallet_token_balance(USDC_MINT)
        escrow_raw = fetch_escrow_balance(USDC_MINT)
        total_raw = wallet_raw + escrow_raw
        log.info("USDC balance — wallet=%.2f  escrow=%.2f  total=%.2f", wallet_raw / 1e6, escrow_raw / 1e6, total_raw / 1e6)
        if principal_raw > total_raw:
            log.warning("New principal ($%.2f) exceeds your total wallet+escrow balance ($%.2f) — this will likely "
                       "fail unless you lower it or pass --topup none.", new_principal_usd, total_raw / 1e6)

    def _fmt_collateral(whole: float) -> str:
        return f"{whole:,.2f}" if whole >= 1 else f"{whole:,.6g}"

    log.info("=" * 78)
    log.info("UPDATE OFFER — %s", offer_pubkey)
    log.info("=" * 78)
    col = "{:<16}{:>26}{:>26}"
    log.info(col.format("", "CURRENT", "NEW"))
    log.info(col.format("Principal", f"{current_principal_usd:,.2f} USDC", f"{new_principal_usd:,.2f} USDC"))
    log.info(col.format(
        "Collateral",
        f"{_fmt_collateral(current_collateral_whole)} {symbol_for(collateral_mint)}",
        f"{_fmt_collateral(new_collateral_whole)} {symbol_for(collateral_mint)}",
    ))
    log.info(col.format(
        "LTV",
        f"{current_ltv_pct:.2f}%" if current_ltv_pct is not None else "n/a",
        f"{new_ltv_pct:.2f}%" if new_ltv_pct is not None else "n/a",
    ))
    log.info(col.format("APR", f"{current_apy_bps / 100:.2f}%", f"{new_apr_pct:.2f}%"))
    log.info(col.format("Duration", f"{current_duration_secs / 86400:.0f} day(s)", f"{new_duration_days:.0f} day(s)"))
    log.info(col.format("Expires in", "—", f"{new_expiry_days:.0f} day(s)"))
    log.info(col.format(
        "Partial fill",
        f"{'allowed' if current_allow_partial_fill else 'not allowed'}, min ${current_min_fill_raw / 1e6:,.2f}",
        f"{'allowed' if new_allow_partial_fill else 'not allowed'}, min ${min_fill_raw / 1e6:,.2f}",
    ))
    log.info(col.format("Topup mode", "—", args.topup))
    log.info("=" * 78)
    log.info("This builds ONE transaction: cancels %s and creates its replacement atomically.", offer_pubkey)

    if not args.yes:
        choice = input("\nUpdate this offer? [y/N] ").strip().lower()
        if choice not in ("y", "yes"):
            log.info("Aborted by user.")
            sys.exit(0)

    params = {
        "signer": WALLET_PUBKEY,
        "principalMint": USDC_MINT,
        "collateralMint": collateral_mint,
        "principalAmount": principal_raw,
        "collateralAmount": collateral_raw,
        "apy": new_apy_bps,
        "duration": duration_secs,
        "expiry": expiry_secs,
        "allowPartialFill": new_allow_partial_fill,
        "minFillAmount": min_fill_raw,
        "topup": args.topup,
        "offerToCancel": offer_pubkey,
    }

    ok = submit_update(params)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
