"""
Offerbook Kill Switch — Cancel Open Offers
==========================================
Asks which strategy to cancel (1-day, 3-day, 7-day, 15-day, or all), then
fetches and cancels matching offers in batches.

Handles "underfunded" offers (orphaned on-chain PDAs that the API won't list)
by scanning on-chain accounts directly via getProgramAccounts.

Signing modes:
  --ledger        Sign with a Ledger hardware wallet (default). Requires the
                   Solana app open on-device; you approve each transaction
                   with a button press. Path: OFFERBOOK_LEDGER_PATH (default
                   44'/501'/0').
  --private-key   Sign with OFFERBOOK_PRIVATE_KEY from .env (hot wallet).

Usage:
  python cancel_offers.py                   # interactive strategy prompt, Ledger signing
  python cancel_offers.py --private-key     # same, but sign with the hot wallet key
  python cancel_offers.py --days 7          # skip prompt, cancel 7-day offers
  python cancel_offers.py --days all        # skip prompt, cancel everything
  python cancel_offers.py --withdraw        # also pull funds back to wallet
  python cancel_offers.py --yes             # skip the signing-mode confirmation prompt
  DRY_RUN=true python cancel_offers.py      # preview without submitting
"""
from __future__ import annotations

from dotenv import load_dotenv
load_dotenv()

import argparse
import base64
import logging
import os
import re
import sys
import time
from typing import Any

import requests

import offerbook_common as _common

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

API_BASE      = os.getenv("OFFERBOOK_API_BASE",    "https://api.offerbook.jup.ag/api/v1")
TX_API_BASE   = os.getenv("OFFERBOOK_TX_API_BASE", "https://builder.offerbook.jup.ag/api/v1")
SOLANA_RPC    = os.getenv("SOLANA_RPC",            "https://api.mainnet-beta.solana.com")
WALLET_PUBKEY = os.getenv("OFFERBOOK_WALLET",      "")
PRIVATE_KEY_B58 = os.getenv("OFFERBOOK_PRIVATE_KEY", "")
DRY_RUN       = os.getenv("DRY_RUN", "false").lower() in ("1", "true", "yes")

# "ledger" or "private_key" — Ledger is the default signing mode.
SIGNING_MODE   = os.getenv("OFFERBOOK_SIGNING_MODE", "ledger").strip().lower()
LEDGER_PATH    = os.getenv("OFFERBOOK_LEDGER_PATH", "44'/501'/0'")

BATCH_SIZE = 5    # offers per cancel tx (keep conservative)
PAGE_SIZE  = 100

# Duration in seconds for each strategy — used to filter offers by type.
STRATEGIES: dict[str, int | None] = {
    "1":   1  * 24 * 60 * 60,   # 86 400
    "3":   3  * 24 * 60 * 60,   # 259 200
    "7":   7  * 24 * 60 * 60,   # 604 800
    "15":  15 * 24 * 60 * 60,   # 1 296 000
    "all": None,                 # no filter — cancel everything
}

STRATEGY_LABELS = {
    "1":   "1-day  strategy  (70% max LTV, 86 400 s duration)",
    "3":   "3-day  strategy  (65% max LTV, 259 200 s duration)",
    "7":   "7-day  strategy  (45% max LTV, 604 800 s duration)",
    "15":  "15-day strategy  (25% max LTV, 1 296 000 s duration)",
    "all": "ALL open offers  (every strategy)",
}

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

SESSION = requests.Session()
SESSION.headers["Content-Type"] = "application/json"


def _get(path: str, **params) -> Any:
    return _common.api_get(SESSION, API_BASE, path, params)


def _post_tx(endpoint: str, payload: dict) -> dict:
    return _common.post_tx(SESSION, TX_API_BASE, endpoint, payload)

# ---------------------------------------------------------------------------
# Signer (lazy — only needed when DRY_RUN=false)
# ---------------------------------------------------------------------------

_keypair = None


def _get_keypair():
    global _keypair
    if _keypair is not None:
        return _keypair
    try:
        import base58
        from solders.keypair import Keypair  # type: ignore
    except ImportError:
        log.error("Missing dependencies: pip install solders base58")
        sys.exit(1)
    _keypair = Keypair.from_bytes(base58.b58decode(PRIVATE_KEY_B58))
    return _keypair


def resolve_signer_wallet() -> str:
    """Thin wrapper: delegates to offerbook_common, updates this module's WALLET_PUBKEY."""
    global WALLET_PUBKEY
    WALLET_PUBKEY = _common.resolve_signer_wallet(SIGNING_MODE, WALLET_PUBKEY, LEDGER_PATH)
    return WALLET_PUBKEY


def confirm_signing_mode(skip_prompt: bool) -> None:
    _common.confirm_signing_mode(SIGNING_MODE, WALLET_PUBKEY, LEDGER_PATH, DRY_RUN, skip_prompt)

# ---------------------------------------------------------------------------
# Strategy selection prompt
# ---------------------------------------------------------------------------

def prompt_strategy() -> str:
    """
    Interactively ask which strategy to cancel.
    Returns one of: '3', '7', '15', 'all'.
    """
    print()
    print("Which offers do you want to cancel?")
    print()
    for key, label in STRATEGY_LABELS.items():
        print(f"  [{key:3s}]  {label}")
    print()
    while True:
        choice = input("Choice: ").strip().lower()
        if choice in STRATEGIES:
            return choice
        print("  Please enter 1, 3, 7, 15, or all")

# ---------------------------------------------------------------------------
# Fetch offers
# ---------------------------------------------------------------------------

OFFERBOOK_PROGRAM_ID = "offerbkFMvVfpQhL8ZQ5iromnjct5rz3r52B9ewu3ie"


def fetch_my_offers(duration_filter: int | None) -> list[dict]:
    """
    Return active + partiallyFilled lending offers for WALLET_PUBKEY.
    If duration_filter is set, only return offers whose duration matches
    that strategy's loan term (in seconds).

    showUnverified=true and includeUnderfunded=true are both required — the API
    hides offers whose collateral isn't Jupiter-verified, and separately hides
    underfunded ones, by default. Missing either would mean "cancel all my
    offers" silently leaves some of them live. (fetch_onchain_orphans() below
    also catches whatever this still misses, as a second line of defense.)
    """
    offers: list[dict] = []
    offset = 0
    while True:
        page = _get(
            "/offers",
            creator=WALLET_PUBKEY,
            offerType="lending",
            status="Active,PartiallyFilled",
            showUnverified="true",
            includeUnderfunded="true",
            limit=PAGE_SIZE,
            offset=offset,
        )
        items = page.get("data", page) if isinstance(page, dict) else page
        if not items:
            break
        offers.extend(items)
        if len(items) < PAGE_SIZE:
            break
        offset += PAGE_SIZE

    if duration_filter is not None:
        before = len(offers)
        offers = [o for o in offers if o.get("duration") == duration_filter]
        log.info(
            "Filtered to %d offer(s) with duration=%d s  (%d total fetched)",
            len(offers), duration_filter, before,
        )
    return offers


def fetch_onchain_orphans(api_pubkeys: set[str]) -> list[str]:
    """
    Use getProgramAccounts to find offer PDAs on-chain that the API doesn't
    list (underfunded / orphaned accounts). Returns pubkeys not in api_pubkeys.
    """
    payload = {
        "jsonrpc": "2.0", "id": 1,
        "method": "getProgramAccounts",
        "params": [
            OFFERBOOK_PROGRAM_ID,
            {
                "encoding": "base64",
                "filters": [
                    # Offerbook offer accounts store the creator pubkey at offset 8
                    # (after the 8-byte Anchor discriminator).
                    {"memcmp": {"offset": 8, "bytes": WALLET_PUBKEY}},
                ],
            },
        ],
    }
    try:
        resp = requests.post(SOLANA_RPC, json=payload, timeout=30)
        resp.raise_for_status()
        accounts = resp.json().get("result", [])
    except Exception as exc:
        log.warning("On-chain scan failed: %s", exc)
        return []

    orphans = [
        acct["pubkey"]
        for acct in accounts
        if acct["pubkey"] not in api_pubkeys
    ]
    if orphans:
        log.info("Found %d on-chain orphan offer(s) not in API:", len(orphans))
        for pk in orphans:
            log.info("  %s", pk)
    return orphans

# ---------------------------------------------------------------------------
# Sign + submit
# ---------------------------------------------------------------------------

def _sign_and_send(tx_b64: str) -> str:
    if SIGNING_MODE == "ledger":
        from ledger_signer import LedgerError

        signer = _common.get_ledger_signer(LEDGER_PATH)
        log.info("  Awaiting approval on Ledger device …")
        try:
            signed_b64 = signer.sign_transaction(tx_b64, expected_signer=WALLET_PUBKEY)
        except LedgerError as exc:
            raise RuntimeError(str(exc)) from exc
    else:
        from solders.transaction import VersionedTransaction  # type: ignore
        keypair = _get_keypair()
        raw_tx = base64.b64decode(tx_b64)
        tx = VersionedTransaction.from_bytes(raw_tx)
        signed_tx = VersionedTransaction(tx.message, [keypair])
        signed_b64 = base64.b64encode(bytes(signed_tx)).decode()

    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "sendTransaction",
        "params": [signed_b64, {"encoding": "base64", "skipPreflight": False}],
    }
    resp = requests.post(SOLANA_RPC, json=payload, timeout=30)
    resp.raise_for_status()
    result = resp.json()
    if "error" in result:
        raise RuntimeError(f"RPC error: {result['error']}")
    return result["result"]

# ---------------------------------------------------------------------------
# Cancel batch
# ---------------------------------------------------------------------------

# The builder API names the specific offer in its error, e.g. "Offer <pubkey>
# does not exist anymore" — used to drop just that one and re-batch the rest
# instead of falling back to fully-individual retries for the whole batch.
_MISSING_OFFER_RE = re.compile(r"Offer\s+([1-9A-HJ-NP-Za-km-z]{32,44})\s+does not exist")


def _extract_missing_offer(exc: Exception) -> str | None:
    m = _MISSING_OFFER_RE.search(str(exc))
    return m.group(1) if m else None


def cancel_batch(pubkeys: list[str], withdraw_mode: str) -> None:
    payload: dict = {"signer": WALLET_PUBKEY, "withdraw": withdraw_mode}
    if len(pubkeys) == 1:
        payload["offer"] = pubkeys[0]
    else:
        payload["offers"] = pubkeys

    if DRY_RUN:
        log.info("[DRY RUN] Would cancel %d offer(s): %s", len(pubkeys), pubkeys)
        return

    data = _post_tx("/cancel-offer", payload)

    txs: list[str] = []
    if data.get("transactions"):
        txs = data["transactions"]
    elif data.get("transaction"):
        txs = [data["transaction"]]
    elif data.get("tx"):
        txs = [data["tx"]]
    else:
        raise ValueError(f"Unexpected cancel-offer response: {data}")

    for tx_b64 in txs:
        sig = _sign_and_send(tx_b64)
        log.info("Cancel tx submitted — sig: %s", sig)
        time.sleep(0.3)
    log.info("Cancelled batch of %d offer(s)", len(pubkeys))

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    global SIGNING_MODE, WALLET_PUBKEY

    parser = argparse.ArgumentParser(description="Cancel Offerbook lending offers by strategy")
    parser.add_argument(
        "--days",
        choices=["1", "3", "7", "15", "all"],
        default=None,
        help="Strategy to cancel (1, 3, 7, 15, or all). Omit to be prompted interactively.",
    )
    parser.add_argument(
        "--withdraw",
        action="store_true",
        help="Withdraw funds back to wallet after cancellation (default: leave in escrow)",
    )
    _common.add_signing_args(parser)
    args = parser.parse_args()

    SIGNING_MODE = _common.resolve_signing_mode(args.signing_mode, SIGNING_MODE)

    if SIGNING_MODE == "private_key":
        if not WALLET_PUBKEY:
            log.error("OFFERBOOK_WALLET is not set")
            sys.exit(1)
        if not DRY_RUN and not PRIVATE_KEY_B58:
            log.error("OFFERBOOK_PRIVATE_KEY is not set (required for live private-key mode)")
            sys.exit(1)
    else:
        resolve_signer_wallet()  # queries the Ledger device, sets WALLET_PUBKEY

    confirm_signing_mode(skip_prompt=args.yes)

    # Determine strategy
    strategy_key = args.days if args.days else prompt_strategy()
    duration_filter = STRATEGIES[strategy_key]
    withdraw_mode   = "offerAmount" if args.withdraw else "none"

    log.info("Strategy : %s", STRATEGY_LABELS[strategy_key])
    log.info("Withdraw : %s", withdraw_mode)
    log.info("Dry run  : %s", DRY_RUN)
    log.info("Fetching open offers for %s …", WALLET_PUBKEY)

    offers = fetch_my_offers(duration_filter)
    pubkeys = [o["pubkey"] for o in offers if o.get("pubkey")]

    # Also scan on-chain for underfunded/orphaned PDAs that the API won't list.
    # These arise when a strategy run crashes or when USDC balance drops below
    # the offer's minimum — the account stays on-chain but the API hides it.
    # When cancelling all strategies we always scan; for a specific strategy we
    # still scan (the orphan count is small and cancelling extras is harmless).
    api_pubkey_set = set(pubkeys)
    orphans = fetch_onchain_orphans(api_pubkey_set)
    # For orphans we can't filter by duration (account data not decoded), so we
    # include them when --days all is selected or when there are no API offers
    # (meaning everything is orphaned and belongs to us).
    if orphans:
        if duration_filter is None or not pubkeys:
            pubkeys.extend(orphans)
            log.info("Added %d orphan(s) to cancel list", len(orphans))
        else:
            log.info(
                "Skipping %d orphan(s) — run with --days all to cancel them too",
                len(orphans),
            )

    if not pubkeys:
        log.info("No matching offers found — nothing to cancel.")
        return

    log.info("Found %d offer(s) to cancel", len(pubkeys))

    cancelled = 0
    errors    = 0
    for i in range(0, len(pubkeys), BATCH_SIZE):
        batch = pubkeys[i : i + BATCH_SIZE]
        while batch:
            try:
                cancel_batch(batch, withdraw_mode)
                cancelled += len(batch)
                break
            except Exception as exc:
                bad_pk = _extract_missing_offer(exc)
                if bad_pk and bad_pk in batch:
                    # The API told us exactly which offer is gone — drop just that one
                    # and re-batch the rest, instead of falling back to fully-individual
                    # retries (and Ledger prompts) for the whole batch.
                    log.warning(
                        "Skipping %s — no longer exists or already cancelled (retrying remaining %d in batch)",
                        bad_pk, len(batch) - 1,
                    )
                    batch = [pk for pk in batch if pk != bad_pk]
                    errors += 1
                    continue
                if len(batch) == 1:
                    log.warning("Skipping offer %s — no longer exists or already cancelled: %s", batch[0], exc)
                    errors += 1
                    break
                log.warning("Batch of %d failed (%s) — retrying individually …", len(batch), exc)
                for pk in batch:
                    try:
                        cancel_batch([pk], withdraw_mode)
                        cancelled += 1
                    except Exception as inner:
                        log.warning("Skipping %s — no longer exists or already cancelled: %s", pk, inner)
                        errors += 1
                break

    log.info("Done.  Cancelled=%d  Errors=%d", cancelled, errors)


if __name__ == "__main__":
    main()
