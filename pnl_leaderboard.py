"""
Offerbook Realized PNL Leaderboard
===================================
Ranks every Offerbook lender by all-time realized PNL. There's no "top by
PNL" endpoint on the API — only volume-based leaderboards (/metrics/top-
lenders) — so this pulls the full repaid + defaulted loan history platform-
wide (no borrower/lender filter) and aggregates client-side.

Realized PNL per lender =
  + net interest earned on REPAID loans. Interest is converted to USD via
    the platform's documented proportional formula (interest / principal-
    Amount * startPrincipalAmountUsd), then the actual protocol "repay" fee
    charged is subtracted — taken straight from metadata.fees.repay.amountUsd
    per loan, not assumed as a flat rate.
  + collateral kept on DEFAULTED loans, valued at default time
    (endCollateralAmountUsd), minus the principal that was lent out and not
    recovered (startPrincipalAmountUsd). This is a mark-to-market figure at
    the moment of default, not necessarily cash actually realized — if the
    lender is still holding the seized collateral, it's unrealized from here.

Read-only: never signs or submits anything.

Usage:
  python pnl_leaderboard.py              # top 25 by realized PNL
  python pnl_leaderboard.py --top 50
"""
from __future__ import annotations

import argparse
import logging
import os

import requests
from dotenv import load_dotenv

import offerbook_common as _common

load_dotenv()

API_BASE = os.getenv("OFFERBOOK_API_BASE", "https://api.offerbook.jup.ag/api/v1")
PAGE_SIZE = 100

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("pnl_leaderboard")

SESSION = requests.Session()


def _fetch_all_pages(endpoint: str, params: dict | None = None) -> list[dict]:
    return _common.fetch_all_pages(SESSION, API_BASE, endpoint, params, PAGE_SIZE, sleep_secs=0.1)


def compute_pnl() -> tuple[dict[str, float], dict[str, dict[str, int]]]:
    """Returns (pnl_by_lender, counts_by_lender) where counts tracks how many
    repaid/defaulted loans backed each lender's total, for context."""
    pnl: dict[str, float] = {}
    counts: dict[str, dict[str, int]] = {}

    def bump(lender: str, amount: float, kind: str) -> None:
        pnl[lender] = pnl.get(lender, 0.0) + amount
        c = counts.setdefault(lender, {"repaid": 0, "defaulted": 0})
        c[kind] += 1

    log.info("Fetching all repaid loans platform-wide …")
    repaid = _fetch_all_pages("/loans/status/repaid")
    log.info("  → %d repaid loan(s)", len(repaid))
    for l in repaid:
        lender = l.get("lender")
        principal_amount = l.get("principalAmount") or 0
        if not lender or principal_amount == 0:
            continue
        md = l.get("metadata") or {}
        start_principal_usd = md.get("startPrincipalAmountUsd") or 0.0
        interest = l.get("interest") or 0
        interest_usd_gross = (interest / principal_amount) * start_principal_usd
        repay_fee_usd = ((md.get("fees") or {}).get("repay") or {}).get("amountUsd") or 0.0
        bump(lender, interest_usd_gross - repay_fee_usd, "repaid")

    log.info("Fetching all defaulted loans platform-wide …")
    defaulted = _fetch_all_pages("/loans/status/defaulted")
    log.info("  → %d defaulted loan(s)", len(defaulted))
    for l in defaulted:
        lender = l.get("lender")
        if not lender:
            continue
        md = l.get("metadata") or {}
        start_principal_usd = md.get("startPrincipalAmountUsd") or 0.0
        end_collateral_usd = md.get("endCollateralAmountUsd")
        if end_collateral_usd is None:
            end_collateral_usd = md.get("startCollateralAmountUsd") or 0.0
        bump(lender, end_collateral_usd - start_principal_usd, "defaulted")

    return pnl, counts


def print_leaderboard(pnl: dict[str, float], counts: dict[str, dict[str, int]], top: int) -> None:
    ranked = sorted(pnl.items(), key=lambda kv: kv[1], reverse=True)[:top]

    log.info("")
    log.info("=" * 90)
    log.info("Realized PNL leaderboard — repaid interest (net of fees) + kept collateral on defaults")
    log.info("=" * 90)
    col = "{:<4}{:<46}{:>16}{:>9}{:>11}"
    log.info(col.format("#", "lender", "realized PNL $", "repaid", "defaulted"))
    log.info("-" * 90)
    for i, (lender, amount) in enumerate(ranked, 1):
        c = counts[lender]
        log.info(col.format(i, lender, f"{amount:,.2f}", c["repaid"], c["defaulted"]))
    log.info("=" * 90)


def main() -> None:
    parser = argparse.ArgumentParser(description="Rank Offerbook lenders by all-time realized PNL.")
    parser.add_argument("--top", type=int, default=25, help="Number of top wallets to show (default: 25)")
    args = parser.parse_args()

    pnl, counts = compute_pnl()
    log.info("Distinct lenders with resolved (repaid or defaulted) loan history: %d", len(pnl))
    print_leaderboard(pnl, counts, args.top)


if __name__ == "__main__":
    main()
