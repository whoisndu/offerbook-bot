"""
Offerbook Fill Offer
=====================
Fully fills a single live offer by pubkey — either a "borrowing" offer
(someone posted collateral wanting principal; you fill it as the lender via
/fill-collateral-offer) or a "lending" offer (someone posted principal
wanting collateral; you fill it as the borrower via /fill-principal-offer).
Offer type is auto-detected from the live offer data.

Fetches the offer fresh right before building the fill transaction (so the
amounts reflect current remainingPrincipal/remainingCollateral, not whatever
was seen earlier), prints a preview, and asks for confirmation before any
signing happens — same safety pattern as the other scripts in this repo.

In Ledger mode you're interactively prompted which account to sign with
(unless --ledger-path is given) — same reasoning as cancel_offers.py: this
script has no "right" account, it depends what you're filling and with what.

Usage:
  python fill_offer.py --offer <pubkey>
  python fill_offer.py --offer <pubkey> --ledger-path "44'/501'/1'"
  python fill_offer.py --offer <pubkey> --private-key
  python fill_offer.py --offer <pubkey> --yes
  DRY_RUN=true python fill_offer.py --offer <pubkey>   # preview without submitting
"""
from __future__ import annotations

import argparse
import logging
import os
import sys

import requests

import defaulter_capture as dc
import offerbook_common as _common

log = logging.getLogger("fill_offer")

SOLANA_RPC = os.getenv("SOLANA_RPC", "https://api.mainnet-beta.solana.com")
DRY_RUN = os.getenv("DRY_RUN", "false").lower() in ("1", "true", "yes")

# Same known-accounts convenience as cancel_offers.py — labeling only, "c" for custom.
KNOWN_LEDGER_ACCOUNTS: dict[str, tuple[str, str]] = {
    "1": ("44'/501'/0'", "Original / general strategy account"),
    "2": ("44'/501'/1'", "Targeted-offers account"),
}


def prompt_ledger_path() -> str:
    print()
    print("Which account do you want to fill this offer from?")
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
            custom = input("Enter derivation path (e.g. \"44'/501'/2'\"): ").strip()
            if custom:
                return custom
            print("  Path can't be empty")
            continue
        print(f"  Please enter one of: {', '.join(KNOWN_LEDGER_ACCOUNTS)}, or 'c' for custom")


def prompt_offer_pubkey() -> str:
    while True:
        pubkey = input("Offer pubkey to fill: ").strip()
        if pubkey:
            return pubkey
        print("  Can't be empty")


def fetch_offer(offer_pubkey: str) -> dict | None:
    """The API has no single-offer-by-pubkey lookup, so scan live offers
    (both types) for a matching pubkey — refetched fresh each call so
    remainingPrincipal/remainingCollateral reflect the current state."""
    for offer_type in ("borrowing", "lending"):
        for item in dc._fetch_all_pages("/offers", {
            "offerType": offer_type, "status": "Active,PartiallyFilled",
            "includeUnderfunded": "true", "showUnverified": "true",
        }):
            if item.get("pubkey") == offer_pubkey:
                return item
    return None


def fetch_current_slot() -> int:
    resp = requests.post(SOLANA_RPC, json={"jsonrpc": "2.0", "id": 1, "method": "getSlot"}, timeout=30)
    resp.raise_for_status()
    result = resp.json()
    if "error" in result:
        raise RuntimeError(f"RPC error fetching slot: {result['error']}")
    return result["result"]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--offer", default=None, help="Offer account pubkey to fill (omit to be prompted)")
    parser.add_argument("--ledger-path", default=None, help="Ledger derivation path (skips the account prompt)")
    _common.add_signing_args(parser, yes_help="Skip the preview confirmation prompt")
    args = parser.parse_args()

    offer_pubkey = args.offer or prompt_offer_pubkey()

    dc.SIGNING_MODE = _common.resolve_signing_mode(args.signing_mode, dc.SIGNING_MODE)
    if dc.SIGNING_MODE == "ledger":
        dc.LEDGER_PATH = args.ledger_path or prompt_ledger_path()

    dc.resolve_signer_wallet()
    dc.confirm_signing_mode(skip_prompt=args.yes)

    offer = fetch_offer(offer_pubkey)
    if not offer:
        log.error("Offer %s not found (already filled/cancelled/expired?). Aborting.", offer_pubkey)
        sys.exit(1)

    offer_type = offer["offerType"]
    principal_amount = offer["remainingPrincipal"]
    collateral_amount = offer["remainingCollateral"]
    md = offer.get("metadata", {})

    log.info("")
    log.info("=" * 90)
    log.info("PREVIEW — offer to be filled (review before signing)")
    log.info("=" * 90)
    log.info("Offer        : %s (%s)", offer_pubkey, offer_type)
    log.info("Creator      : %s", offer["creator"])
    log.info("Principal    : %s raw units  (~$%.2f)", principal_amount, md.get("availablePrincipalUsd", 0.0))
    log.info("Collateral   : %s raw units  (~$%.2f)", collateral_amount, md.get("availableCollateralUsd", md.get("collateralAmountUsd", 0.0)))
    log.info("APY          : %.2f%%", offer["apy"] / 100)
    log.info("Duration     : %d days", offer["duration"] // 86400)
    log.info("Signer       : %s", dc.WALLET_PUBKEY)
    log.info("=" * 90)

    if not args.yes:
        choice = input("\nFill this offer FULLY? [y/N] ").strip().lower()
        if choice not in ("y", "yes"):
            log.info("Aborted by user.")
            sys.exit(0)

    slot = fetch_current_slot()

    if offer_type == "borrowing":
        endpoint = "/fill-collateral-offer"
        payload = {
            "signer": dc.WALLET_PUBKEY,
            "offer": offer_pubkey,
            "collateralFillAmount": collateral_amount,
            "maxPrincipal": principal_amount,
            "topup": "minimum",  # use escrow first, only pull the shortfall from wallet
            "slot": slot,
        }
    elif offer_type == "lending":
        endpoint = "/fill-principal-offer"
        payload = {
            "signer": dc.WALLET_PUBKEY,
            "offer": offer_pubkey,
            "principalFillAmount": principal_amount,
            "maxCollateral": collateral_amount,
            "topup": "minimum",  # use escrow first, only pull the shortfall from wallet
            "slot": slot,
        }
    else:
        log.error("Unsupported offerType %r — only 'lending' and 'borrowing' token offers are handled.", offer_type)
        sys.exit(1)

    if DRY_RUN:
        log.info("[DRY RUN] Would POST %s with: %s", endpoint, payload)
        return

    try:
        tx_data = dc._post_tx(endpoint, payload)
    except requests.HTTPError as exc:
        body = exc.response.text if exc.response else ""
        log.error("TX builder error: %s\n  body: %s", exc, body or "(empty)")
        sys.exit(1)

    txs = tx_data.get("transactions", [])
    if not txs:
        log.error("Unexpected fill response: %s", tx_data)
        sys.exit(1)

    for tx_b64 in txs:
        sig = dc.sign_and_send_transaction(tx_b64)
        log.info("Fill tx submitted — sig: %s", sig)

    log.info("Done.")


if __name__ == "__main__":
    main()
