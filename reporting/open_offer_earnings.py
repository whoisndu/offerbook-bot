"""
Offerbook Open-Offer Projected Earnings
==========================================
For your own OPEN lending offers (status active/partiallyFilled) — what each
one should earn if its still-unfilled portion gets fully matched by a
borrower, and the total across your whole book.

Per offer: remainingPrincipal * APY * (duration / 1 year) = gross interest
for the full term (interest on this platform is NOT prorated — a borrower
owes the full term's interest regardless of when they repay), then net of
the platform's 10% repay fee (INTEREST_REPAY_FEE_RATE — measured directly
off repaid loans, see portfolio_health.py's constant of the same name) to
get what you'd actually receive. This is a projection, not a guarantee nor
an expectation of WHEN: it assumes a borrower takes the whole remaining
size at today's posted APY/duration before expiry, which may never happen
for a given offer.

Only the REMAINING (not-yet-matched) principal is counted — a
partiallyFilled offer's already-matched portion is already an active loan,
tracked separately by portfolio_health.py's accrued-interest/projected-
weekly-earnings figures. Counting it here too would double-count.

Only USDC-principal offers are supported, matching every other script here
(breakeven_check.py, verify_offers.py, etc.) — principal_usd is read
straight off remainingPrincipal at 1:1, no price lookup needed.

Wallets to check come from OFFERBOOK_PORTFOLIO_WALLETS in .env (comma-
separated addresses), same as breakeven_check.py/portfolio_health.py.
Override with --wallets for a one-off check without touching .env.

Read-only: never signs or submits anything.

Usage:
  python open_offer_earnings.py
  python open_offer_earnings.py --wallets <addr1>,<addr2>
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv

load_dotenv()

# Shared modules live in ../lib — see README's repo-layout note.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))

import offerbook_common as _common
from offerbook_common import _mint_from_asset

API_BASE = os.getenv("OFFERBOOK_API_BASE", "https://api.offerbook.jup.ag/api/v1")
PAGE_SIZE = 100

USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDC_DECIMALS = 6
YEAR_SECS = 365 * 86400

INTEREST_REPAY_FEE_RATE = 0.10  # platform fee taken from interest at repayment —
                                  # see portfolio_health.py's constant of the same
                                  # name for how this was measured (flat 10%).

KNOWN_SYMBOLS = _common.KNOWN_SYMBOLS

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("open_offer_earnings")

SESSION = requests.Session()


def symbol_for(mint: str) -> str:
    return KNOWN_SYMBOLS.get(mint, mint[:6] + "…" if mint else "?")


def fetch_lending_offers_for(address: str) -> list[dict]:
    """Same approach as breakeven_check.py's function of the same name —
    the API's own creator filter, scoped to still-live offers."""
    return _common.fetch_all_pages(
        SESSION, API_BASE, "/offers",
        {
            "offerType": "lending", "status": "Active,PartiallyFilled", "creator": address,
            "showUnverified": "true", "includeUnderfunded": "true",
        },
        PAGE_SIZE,
    )


def build_row(offer: dict, wallet: str) -> dict | None:
    """One projected-earnings row for `offer`, or None if it's not a
    USDC-principal lending offer with a positive remaining size."""
    principal_mint = offer.get("principalMint") or _mint_from_asset(offer.get("principal", {}))
    if principal_mint != USDC_MINT:
        return None
    principal_raw = offer.get("remainingPrincipal", offer.get("principalAmount", 0))
    if principal_raw <= 0:
        return None

    collateral_mint = offer.get("collateralMint") or _mint_from_asset(offer.get("collateral", {}))
    apy_bps = offer.get("apy") or 0
    duration_secs = offer.get("duration") or 0
    principal_usd = principal_raw / 10 ** USDC_DECIMALS

    gross_interest_usd = principal_usd * (apy_bps / 10000) * (duration_secs / YEAR_SECS)
    net_interest_usd = gross_interest_usd * (1 - INTEREST_REPAY_FEE_RATE)

    expired_at = None
    try:
        expired_at = datetime.fromisoformat(offer["expiredAt"].replace("Z", "+00:00"))
    except (KeyError, ValueError, AttributeError):
        pass

    return {
        "wallet": wallet,
        "pubkey": offer.get("pubkey", ""),
        "status": offer.get("status", ""),
        "collateral_symbol": symbol_for(collateral_mint),
        "principal_usd": principal_usd,
        "apy_bps": apy_bps,
        "duration_secs": duration_secs,
        "gross_interest_usd": gross_interest_usd,
        "net_interest_usd": net_interest_usd,
        "expired_at": expired_at,
    }


def print_rows(rows: list[dict]) -> None:
    if not rows:
        log.info("No open USDC-principal lending offers found.")
        return

    rows = sorted(rows, key=lambda r: -r["net_interest_usd"])

    log.info("")
    col = "{:<10}{:<10}{:<16}{:>14}{:>8}{:>10}{:>14}{:>14}  {:<20}"
    log.info(col.format(
        "wallet", "status", "collateral", "principal$", "apy%", "duration", "gross$", "net$", "pubkey",
    ))
    for r in rows:
        wallet_short = f"{r['wallet'][:4]}…{r['wallet'][-4:]}"
        pubkey_short = f"{r['pubkey'][:6]}…{r['pubkey'][-4:]}" if r["pubkey"] else ""
        duration_str = f"{r['duration_secs'] / 86400:.1f}d" if r["duration_secs"] else "n/a"
        log.info(col.format(
            wallet_short, r["status"], r["collateral_symbol"],
            f"{r['principal_usd']:,.2f}", f"{r['apy_bps'] / 100:.2f}", duration_str,
            f"{r['gross_interest_usd']:,.2f}", f"{r['net_interest_usd']:,.2f}", pubkey_short,
        ))

    total_principal = sum(r["principal_usd"] for r in rows)
    total_gross = sum(r["gross_interest_usd"] for r in rows)
    total_net = sum(r["net_interest_usd"] for r in rows)
    log.info("-" * 130)
    log.info(
        "TOTAL  %d open offer(s)  —  remaining principal $%s  →  if fully filled: gross $%s, net (after %.0f%% repay fee) $%s",
        len(rows), f"{total_principal:,.2f}", f"{total_gross:,.2f}",
        INTEREST_REPAY_FEE_RATE * 100, f"{total_net:,.2f}",
    )

    by_wallet: dict[str, list[dict]] = {}
    for r in rows:
        by_wallet.setdefault(r["wallet"], []).append(r)
    if len(by_wallet) > 1:
        log.info("")
        log.info("By wallet:")
        for wallet, wrows in by_wallet.items():
            wallet_short = f"{wallet[:4]}…{wallet[-4:]}"
            log.info(
                "  %s  —  %d offer(s)  net if fully filled: $%s",
                wallet_short, len(wrows), f"{sum(r['net_interest_usd'] for r in wrows):,.2f}",
            )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--wallets", default=None,
        help="Comma-separated wallet addresses to check. Defaults to OFFERBOOK_PORTFOLIO_WALLETS in .env.",
    )
    args = parser.parse_args()

    raw_wallets = args.wallets or os.getenv("OFFERBOOK_PORTFOLIO_WALLETS", "")
    wallets = [w.strip() for w in raw_wallets.split(",") if w.strip()]
    if not wallets:
        log.error(
            "No wallets to check. Set OFFERBOOK_PORTFOLIO_WALLETS=addr1,addr2 in .env, "
            "or pass --wallets addr1,addr2. Aborting."
        )
        sys.exit(1)

    rows: list[dict] = []
    for wallet in wallets:
        log.info("Fetching open lending offers for %s …", wallet)
        offers = fetch_lending_offers_for(wallet)
        log.info("  → %d live offer(s)", len(offers))
        for offer in offers:
            row = build_row(offer, wallet)
            if row is not None:
                rows.append(row)

    print_rows(rows)


if __name__ == "__main__":
    main()
