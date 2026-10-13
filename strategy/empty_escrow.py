"""
Offerbook Empty Escrow — Withdraw Everything in Escrow Back to Wallet
==================================
Offerbook holds funds in two places per wallet: your wallet itself, and an
on-chain escrow account the protocol uses to avoid a fresh deposit tx on
every offer/fill. Money accumulates in escrow from loan repayments,
interest, origination-fee rebates, and defaulted-loan collateral — nothing
automatically sweeps it back to your wallet. This does that sweep: fetches
every nonzero escrow holding for one wallet and withdraws it in full, one
asset at a time.

Covers both fungible tokens (USDC, SOL, any other SPL/Token-2022 token —
via POST /escrow-withdraw) and NFTs (classic/programmable/core — via their
own dedicated withdraw endpoints, auto-detected from each holding's own
`asset.kind`). A holding this script doesn't recognize is skipped with a
warning rather than guessed at.

Same signing infrastructure as the rest of strategy/ (imports
defaulter_capture.py for it, like fill_offer.py does) — Ledger by default
(interactively prompted which account, unless --ledger-path is given;
there's no "right" account here, it depends whose escrow you're emptying)
or --private-key for the .env hot wallet. Only ever operates on ONE wallet
per run — if you run multiple wallets (e.g. OFFERBOOK_PORTFOLIO_WALLETS),
run this once per wallet, selecting the matching signer each time.

Prints every nonzero holding (symbol, kind, amount, ~$ value where a live
price is resolvable) and asks for ONE confirmation covering the whole
batch before signing anything — same safety pattern as cancel_offers.py's
batch cancellation. A single asset's withdrawal failing doesn't abort the
rest of the batch; failures are collected and reported in the summary so
you know exactly what still needs a rerun.

Usage:
  python empty_escrow.py                        # prompts which Ledger account, then confirms
  python empty_escrow.py --ledger-path "44'/501'/1'"   # skip the account prompt
  python empty_escrow.py --private-key           # sign with the .env hot wallet instead
  python empty_escrow.py --min-usd 1             # skip pure-dust holdings below $1 (default: 0, sweep everything)
  python empty_escrow.py --yes                   # skip the confirmation prompt
  DRY_RUN=true python empty_escrow.py            # preview without submitting
"""
from __future__ import annotations

import argparse
import logging
import os
import sys

import requests

# Shared modules live in ../lib — see README's repo-layout note.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))

import defaulter_capture as dc
import offerbook_common as _common

API_BASE = os.getenv("OFFERBOOK_API_BASE", "https://api.offerbook.jup.ag/api/v1")
DRY_RUN = os.getenv("DRY_RUN", "false").lower() in ("1", "true", "yes")

KNOWN_SYMBOLS = _common.KNOWN_SYMBOLS
KNOWN_DECIMALS = _common.KNOWN_DECIMALS

# Same account picker as cancel_offers.py/fill_offer.py — this script has no
# "right" account either, it depends whose escrow you're emptying.
KNOWN_LEDGER_ACCOUNTS: dict[str, tuple[str, str]] = {
    "1": ("44'/501'/0'", "Original / general strategy account"),
    "2": ("44'/501'/1'", "Targeted-offers account"),
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("empty_escrow")

SESSION = requests.Session()


def prompt_ledger_path() -> str:
    print()
    print("Which account's escrow do you want to empty?")
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


def symbol_for(mint: str | None) -> str:
    if not mint:
        return "NFT"
    return KNOWN_SYMBOLS.get(mint, f"{mint[:6]}…{mint[-4:]}")


def fetch_escrow_holdings(wallet: str) -> list[dict]:
    resp = SESSION.get(f"{API_BASE}/escrows/holdings/{wallet}", timeout=30)
    resp.raise_for_status()
    return resp.json()


def classify_asset(asset: dict) -> tuple[str, str]:
    """(kind, identifier) — kind is normalized to lowercase
    ("token"/"classicnft"/"programmablenft"/"corenft"/"none"); identifier is
    the mint (fungible/classic/programmable) or the asset pubkey (core).
    Matches live API casing seen elsewhere in this repo (loan collateral
    objects use e.g. "token"/"coreNft"), case-insensitively so a casing
    quirk in the escrow response specifically doesn't silently drop a
    holding."""
    kind = (asset.get("kind") or "none").lower()
    if kind == "corenft":
        return kind, asset.get("asset", "")
    return kind, asset.get("mint", "")


def build_rows(holdings: list[dict], min_usd: float) -> list[dict]:
    token_mints = []
    rows = []
    for h in holdings:
        amount_raw = h.get("amount") or 0
        if amount_raw <= 0:
            continue
        kind, identifier = classify_asset(h.get("asset") or {})
        if kind == "none" or not identifier:
            continue
        rows.append({"kind": kind, "identifier": identifier, "amount_raw": amount_raw})
        if kind == "token":
            token_mints.append(identifier)

    prices, live_decimals = dc.fetch_current_prices_batched(list(dict.fromkeys(token_mints))) if token_mints else ({}, {})

    out = []
    for r in rows:
        if r["kind"] != "token":
            out.append({**r, "symbol": symbol_for(None) if r["kind"] == "corenft" else symbol_for(r["identifier"]), "decimals": None, "usd": None})
            continue
        mint = r["identifier"]
        decimals = live_decimals.get(mint, KNOWN_DECIMALS.get(mint))
        amount_ui = r["amount_raw"] / 10 ** decimals if decimals is not None else None
        price = prices.get(mint)
        usd = amount_ui * price if (amount_ui is not None and price is not None) else None
        if usd is not None and usd < min_usd:
            continue
        out.append({**r, "symbol": symbol_for(mint), "decimals": decimals, "amount_ui": amount_ui, "usd": usd})
    return out


def print_preview(rows: list[dict], wallet: str) -> None:
    log.info("")
    log.info("=" * 90)
    log.info("ESCROW HOLDINGS — %s", wallet)
    log.info("=" * 90)
    if not rows:
        log.info("Nothing to withdraw — escrow is already empty (or everything's below --min-usd).")
        return
    col = "{:<14}{:<10}{:>22}{:>14}  {:<46}"
    log.info(col.format("symbol", "kind", "amount", "~$ value", "mint / asset"))
    log.info("-" * 90)
    total_usd = 0.0
    for r in rows:
        if r["kind"] == "token":
            amount_str = f"{r['amount_ui']:,.6f}" if r.get("amount_ui") is not None else f"{r['amount_raw']} (raw, decimals unknown)"
            usd_str = f"${r['usd']:,.2f}" if r.get("usd") is not None else "NO PRICE"
            if r.get("usd") is not None:
                total_usd += r["usd"]
        else:
            amount_str = "1"
            usd_str = "n/a (nft)"
        log.info(col.format(r["symbol"], r["kind"], amount_str, usd_str, r["identifier"]))
    log.info("-" * 90)
    log.info("Total priceable value: ~$%s  (NFTs and unpriced tokens not included in this figure)", f"{total_usd:,.2f}")
    log.info("=" * 90)


def withdraw_one(row: dict) -> list[str]:
    """Returns the base64 transaction(s) for withdrawing `row` in full."""
    kind, identifier = row["kind"], row["identifier"]
    if kind == "token":
        endpoint, payload = "/escrow-withdraw", {
            "signer": dc.WALLET_PUBKEY, "mint": identifier, "amount": row["amount_raw"],
        }
    elif kind == "classicnft":
        endpoint, payload = "/escrow-classic-nft-withdraw", {"signer": dc.WALLET_PUBKEY, "mint": identifier}
    elif kind == "programmablenft":
        endpoint, payload = "/escrow-programmable-nft-withdraw", {"signer": dc.WALLET_PUBKEY, "mint": identifier}
    elif kind == "corenft":
        endpoint, payload = "/escrow-core-nft-withdraw", {"signer": dc.WALLET_PUBKEY, "asset": identifier}
    else:
        raise ValueError(f"Unrecognized escrow asset kind: {kind!r}")

    if DRY_RUN:
        log.info("  [DRY RUN] Would POST %s with: %s", endpoint, payload)
        return []

    tx_data = dc._post_tx(endpoint, payload)
    return tx_data.get("transactions", [])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--ledger-path", default=None, help="Ledger derivation path (skips the account prompt)")
    parser.add_argument("--min-usd", type=float, default=0.0, help="Skip token holdings worth less than this (default: 0 — sweep everything, including dust)")
    _common.add_signing_args(parser, yes_help="Skip the confirmation prompt")
    args = parser.parse_args()

    dc.SIGNING_MODE = _common.resolve_signing_mode(args.signing_mode, dc.SIGNING_MODE)
    if dc.SIGNING_MODE == "ledger":
        dc.LEDGER_PATH = args.ledger_path or prompt_ledger_path()

    dc.resolve_signer_wallet()
    dc.confirm_signing_mode(skip_prompt=args.yes)

    log.info("Fetching escrow holdings for %s …", dc.WALLET_PUBKEY)
    holdings = fetch_escrow_holdings(dc.WALLET_PUBKEY)
    rows = build_rows(holdings, args.min_usd)
    print_preview(rows, dc.WALLET_PUBKEY)

    if not rows:
        sys.exit(0)

    if not args.yes:
        choice = input(f"\nWithdraw all {len(rows)} holding(s) above to wallet {dc.WALLET_PUBKEY}? [y/N] ").strip().lower()
        if choice not in ("y", "yes"):
            log.info("Aborted by user.")
            sys.exit(0)

    succeeded, failed = [], []
    for r in rows:
        label = f"{r['symbol']} ({r['identifier']})"
        log.info("Withdrawing %s …", label)
        try:
            txs = withdraw_one(r)
        except requests.HTTPError as exc:
            body = exc.response.text if exc.response else ""
            log.error("  TX builder error for %s: %s\n  body: %s", label, exc, body or "(empty)")
            failed.append(label)
            continue
        except Exception as exc:
            log.error("  Failed to build withdraw tx for %s: %s", label, exc)
            failed.append(label)
            continue

        if DRY_RUN:
            continue

        if not txs:
            log.error("  Unexpected empty transaction list for %s.", label)
            failed.append(label)
            continue

        ok = True
        for tx_b64 in txs:
            sig = dc.sign_and_send_transaction(tx_b64)
            if not sig:
                ok = False
                continue
            log.info("  Submitted — sig: %s", sig)
        (succeeded if ok else failed).append(label)

    log.info("")
    if DRY_RUN:
        log.info("[DRY RUN] Done — nothing was actually submitted.")
        return
    log.info("Done — %d succeeded, %d failed.", len(succeeded), len(failed))
    if failed:
        log.info("Failed (rerun this script to retry — wallet/escrow state is independent per asset):")
        for label in failed:
            log.info("  - %s", label)
        sys.exit(1)


if __name__ == "__main__":
    main()
