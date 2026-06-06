"""
Offerbook Kill Switch — Cancel All Open Offers
===============================================
Fetches all active and partially-filled lending offers created by your wallet,
then cancels them in batches.  Funds remain in escrow by default; pass
--withdraw to pull them back to your wallet after cancellation.

Usage:
  python cancel_offers.py            # cancel only, leave funds in escrow
  python cancel_offers.py --withdraw # cancel and withdraw funds to wallet
  DRY_RUN=true python cancel_offers.py  # preview without submitting
"""
from __future__ import annotations

from dotenv import load_dotenv
load_dotenv()

import argparse
import base64
import logging
import os
import sys
import time

import requests

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

API_BASE    = os.getenv("OFFERBOOK_API_BASE",    "https://api.offerbook.jup.ag/api/v1")
TX_API_BASE = os.getenv("OFFERBOOK_TX_API_BASE", "https://builder.offerbook.jup.ag/api/v1")
SOLANA_RPC  = os.getenv("SOLANA_RPC",            "https://api.mainnet-beta.solana.com")
WALLET_PUBKEY = os.getenv("OFFERBOOK_WALLET",    "")
PRIVATE_KEY_B58 = os.getenv("OFFERBOOK_PRIVATE_KEY", "")
DRY_RUN     = os.getenv("DRY_RUN", "false").lower() in ("1", "true", "yes")

BATCH_SIZE  = 5   # max offers per cancel transaction (conservative; adjust if API allows more)
PAGE_SIZE   = 100

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
    resp = SESSION.get(f"{API_BASE}{path}", params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()

def _post_tx(endpoint: str, payload: dict) -> dict:
    url = f"{TX_API_BASE}{endpoint}"
    resp = SESSION.post(url, json=payload, timeout=30)
    if not resp.ok:
        try:
            detail = resp.json().get("message", resp.text)
        except Exception:
            detail = resp.text or "(empty)"
        resp.reason = detail
        resp.raise_for_status()
    return resp.json()

# ---------------------------------------------------------------------------
# Keypair (lazy init — only needed when DRY_RUN=false)
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
    raw = base58.b58decode(PRIVATE_KEY_B58)
    _keypair = Keypair.from_bytes(raw)
    return _keypair

# ---------------------------------------------------------------------------
# Fetch my open offers
# ---------------------------------------------------------------------------

def fetch_my_offers() -> list[dict]:
    """Return all active + partiallyFilled lending offers for WALLET_PUBKEY."""
    offers: list[dict] = []
    offset = 0
    while True:
        page = _get(
            "/offers",
            creator=WALLET_PUBKEY,
            offerType="lending",
            status="Active,PartiallyFilled",
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
    return offers

# ---------------------------------------------------------------------------
# Sign + submit transaction
# ---------------------------------------------------------------------------

def _sign_and_send(tx_b64: str) -> str:
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
# Cancel a batch of offers
# ---------------------------------------------------------------------------

def cancel_batch(pubkeys: list[str], withdraw_mode: str) -> None:
    """Build, sign, and submit a cancel transaction for up to BATCH_SIZE offers."""
    payload: dict = {"signer": WALLET_PUBKEY, "withdraw": withdraw_mode}
    if len(pubkeys) == 1:
        payload["offer"] = pubkeys[0]
    else:
        payload["offers"] = pubkeys

    if DRY_RUN:
        log.info("[DRY RUN] Would cancel %d offer(s): %s", len(pubkeys), pubkeys)
        return

    data = _post_tx("/cancel-offer", payload)
    tx_b64 = data.get("transaction") or data.get("tx") or data.get("data", {}).get("transaction")
    if not tx_b64:
        raise ValueError(f"Unexpected cancel-offer response: {data}")

    sig = _sign_and_send(tx_b64)
    log.info("Cancelled %d offer(s) — sig: %s", len(pubkeys), sig)
    time.sleep(0.5)  # brief pause between batch submissions

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Cancel all open Offerbook lending offers")
    parser.add_argument(
        "--withdraw",
        action="store_true",
        help="Withdraw funds back to wallet after cancellation (default: leave in escrow)",
    )
    args = parser.parse_args()
    withdraw_mode = "offerAmount" if args.withdraw else "none"

    if not WALLET_PUBKEY:
        log.error("OFFERBOOK_WALLET is not set")
        sys.exit(1)
    if not DRY_RUN and not PRIVATE_KEY_B58:
        log.error("OFFERBOOK_PRIVATE_KEY is not set (required for live mode)")
        sys.exit(1)

    log.info("Fetching open offers for %s …", WALLET_PUBKEY)
    offers = fetch_my_offers()

    if not offers:
        log.info("No open offers found — nothing to cancel.")
        return

    pubkeys = [o["pubkey"] for o in offers if o.get("pubkey")]
    log.info("Found %d open offer(s) to cancel (withdraw=%s, dry_run=%s)",
             len(pubkeys), withdraw_mode, DRY_RUN)

    cancelled = 0
    errors    = 0
    for i in range(0, len(pubkeys), BATCH_SIZE):
        batch = pubkeys[i : i + BATCH_SIZE]
        try:
            cancel_batch(batch, withdraw_mode)
            cancelled += len(batch)
        except Exception as exc:
            log.error("Failed to cancel batch %s: %s", batch, exc)
            errors += len(batch)

    log.info("Done.  Cancelled=%d  Errors=%d", cancelled, errors)

if __name__ == "__main__":
    main()
