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

If OFFERBOOK_PORTFOLIO_WALLETS is set (.env — the same var portfolio_health.py
reads, comma-separated addresses), those specific lenders are merged into a
single combined row before ranking, labeled "YOUR WALLETS (N combined)"
rather than listed individually — so your own multi-wallet total ranks (and
reads) as one entity instead of being split across several rows. The actual
addresses never appear in the output or in this committed file; they only
ever come from your local, gitignored .env. Leave the var unset and every
wallet is ranked individually as before (the default for anyone else running
this public script).

Read-only: never signs or submits anything.

Usage:
  python pnl_leaderboard.py              # top 25 by realized PNL
  python pnl_leaderboard.py --top 50
"""
from __future__ import annotations

import argparse
import logging
import os
import sys

import requests
from dotenv import load_dotenv

# Shared modules live in ../lib — see README's repo-layout note.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))

import offerbook_common as _common

load_dotenv()

API_BASE = os.getenv("OFFERBOOK_API_BASE", "https://api.offerbook.jup.ag/api/v1")
PAGE_SIZE = 100

# Wallets to merge into a single combined row — see module docstring. Never
# hardcoded here: only ever comes from the local, gitignored .env, so this
# committed script never carries real addresses.
MERGE_WALLETS: set[str] = {
    w.strip() for w in os.getenv("OFFERBOOK_PORTFOLIO_WALLETS", "").split(",") if w.strip()
}
MERGE_LABEL = f"YOUR WALLETS ({len(MERGE_WALLETS)} combined)"

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
    repaid/defaulted loans backed each lender's total, for context.

    Any lender address in MERGE_WALLETS is remapped to MERGE_LABEL before
    ever being used as a dict key — so a merged wallet's real address never
    appears anywhere in pnl/counts, not just in the final printed table."""
    pnl: dict[str, float] = {}
    counts: dict[str, dict[str, int]] = {}

    def bump(lender: str, amount: float, kind: str) -> None:
        if lender in MERGE_WALLETS:
            lender = MERGE_LABEL
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
