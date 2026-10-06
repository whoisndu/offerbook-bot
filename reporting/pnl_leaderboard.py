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
  + interest actually collected on in-place loan extensions/rollovers,
    across EVERY loan regardless of current status (repaid, defaulted, or
    still active). Offerbook lets a loan's term be extended in place — same
    pubkey, stays "active" through the extension, expiredAt pushed forward,
    extensionCount ticks up — and the borrower pays that completed term's
    full interest to the lender AT that moment (metadata.extensions[].
    interestPaid/repayFee), same as a real repayment. That's genuinely
    realized cash, not mark-to-market, so it counts here even for a loan
    that's still active and hasn't reached repaid/defaulted (confirmed
    against live data to affect ~12% of active loans at any given time —
    not an edge case worth excluding). See portfolio_health.py's
    compute_realized_pnl for the same fix, applied there first.

Also reports each lender's total volume — total USD principal (at
origination) of every SETTLED (repaid or defaulted) loan they've made. Volume
stays scoped to settled loans only (deliberately excludes active loans,
extended or not) — it's a distinct, unrelated metric from realized PNL and
isn't affected by the rollover-interest fix above. Shown alongside PNL, not
used to rank (ranking is still by realized PNL) — a high-volume lender isn't
necessarily a profitable one.

Also reports a second volume figure, "vol incl rollovers $", that treats
each in-place extension as its OWN distinct completed lending cycle, not
just the loan's final settlement. A loan extended 3 times before being
repaid is "1 loan" by the base volume metric above (same pubkey) but is
economically 4 separate completed terms — the borrower paid full interest
and the principal went back to work again at each one. Each extension's own
metadata.extensions[].principalAmountUsd is used (falling back to the loan's
startPrincipalAmountUsd if an individual extension is missing it) rather
than re-using the loan's original principal figure, since it's priced at the
moment that specific term actually completed. Unlike base volume, this also
picks up extensions on currently-ACTIVE loans (their completed prior terms
count even though the loan's current open term doesn't, same "already
realized, not mark-to-market" reasoning the rollover-interest PNL fix above
uses) — so a lender can show rollover volume here even with $0 in the base
volume column, if every one of their loans happens to still be active. The
accompanying "cycles" column is repaid + defaulted + rolled-over counts
combined — how many distinct completed lending cycles that total spans.

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


def compute_pnl() -> tuple[dict[str, float], dict[str, dict[str, int]], dict[str, float], dict[str, float]]:
    """Returns (pnl_by_lender, counts_by_lender, volume_by_lender,
    rollover_volume_by_lender). counts tracks how many repaid/defaulted/
    rolled-over loans backed each lender's total, for context. volume is
    total USD principal (at origination) of every SETTLED (repaid or
    defaulted) loan that lender has made — deliberately excludes active
    loans, unrelated to the rollover-interest PNL below (see module
    docstring for why). rollover_volume is the EXTRA distinct volume from
    each in-place extension (see module docstring) — kept separate from
    `volume` rather than folded in, since base volume is a distinct metric
    callers may still want to see on its own.

    Any lender address in MERGE_WALLETS is remapped to MERGE_LABEL before
    ever being used as a dict key — so a merged wallet's real address never
    appears anywhere in pnl/counts/volume/rollover_volume, not just in the
    final printed table."""
    pnl: dict[str, float] = {}
    counts: dict[str, dict[str, int]] = {}
    volume: dict[str, float] = {}
    rollover_volume: dict[str, float] = {}

    def _remap(lender: str) -> str:
        return MERGE_LABEL if lender in MERGE_WALLETS else lender

    def bump(lender: str, amount: float, kind: str) -> None:
        lender = _remap(lender)
        pnl[lender] = pnl.get(lender, 0.0) + amount
        c = counts.setdefault(lender, {"repaid": 0, "defaulted": 0, "rolled_over": 0})
        c[kind] += 1

    def bump_volume(lender: str, start_principal_usd: float) -> None:
        lender = _remap(lender)
        volume[lender] = volume.get(lender, 0.0) + start_principal_usd

    def bump_rollover_volume(lender: str, principal_usd: float) -> None:
        lender = _remap(lender)
        rollover_volume[lender] = rollover_volume.get(lender, 0.0) + principal_usd

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
        bump_volume(lender, start_principal_usd)

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
        bump_volume(lender, start_principal_usd)

    # Interest collected on in-place extensions/rollovers — real cash paid to
    # the lender the moment a term completes, whether or not the loan itself
    # has reached repaid/defaulted yet (see module docstring). Repaid/
    # defaulted loans already carry their own extension history in
    # metadata.extensions[], so only ACTIVE needs a fresh fetch here.
    log.info("Fetching all active loans platform-wide (for rollover interest) …")
    active = _fetch_all_pages("/loans/status/active")
    log.info("  → %d active loan(s)", len(active))
    for l in repaid + defaulted + active:
        lender = l.get("lender")
        if not lender:
            continue
        start_principal_usd = (l.get("metadata") or {}).get("startPrincipalAmountUsd") or 0.0
        for ext in (l.get("metadata") or {}).get("extensions") or []:
            interest_usd = ext.get("interestPaidUsd") or 0.0
            fee_usd = ((ext.get("repayFee") or {}).get("amountUsd")) or 0.0
            bump(lender, interest_usd - fee_usd, "rolled_over")
            # Each extension is priced at its own completion moment
            # (principalAmountUsd) — falls back to the loan's own
            # origination price only if that specific extension is missing
            # it, same convention the rest of this script uses.
            bump_rollover_volume(lender, ext.get("principalAmountUsd") or start_principal_usd)

    return pnl, counts, volume, rollover_volume


def print_leaderboard(
    pnl: dict[str, float], counts: dict[str, dict[str, int]], volume: dict[str, float],
    rollover_volume: dict[str, float], top: int,
) -> None:
    ranked = sorted(pnl.items(), key=lambda kv: kv[1], reverse=True)[:top]

    log.info("")
    log.info("=" * 115)
    log.info("Realized PNL leaderboard — repaid interest + rollover interest (net of fees) + kept collateral on defaults")
    log.info("=" * 115)
    col = "{:<4}{:<46}{:>16}{:>9}{:>11}{:>13}{:>19}{:>22}{:>9}"
    log.info(col.format(
        "#", "lender", "realized PNL $", "repaid", "defaulted", "rolled over", "total volume $",
        "vol incl rollovers $", "cycles",
    ))
    log.info("-" * 115)
    for i, (lender, amount) in enumerate(ranked, 1):
        c = counts[lender]
        base_volume = volume.get(lender, 0.0)
        total_volume = base_volume + rollover_volume.get(lender, 0.0)
        cycles = c["repaid"] + c["defaulted"] + c["rolled_over"]
        log.info(col.format(
            i, lender, f"{amount:,.2f}", c["repaid"], c["defaulted"], c["rolled_over"], f"{base_volume:,.2f}",
            f"{total_volume:,.2f}", cycles,
        ))
    log.info("=" * 115)
    log.info(
        "'vol incl rollovers $' counts each in-place extension as its own distinct completed lending cycle, "
        "not just the loan's final settlement — see the module docstring. 'cycles' = repaid + defaulted + "
        "rolled over counts combined."
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Rank Offerbook lenders by all-time realized PNL.")
    parser.add_argument("--top", type=int, default=25, help="Number of top wallets to show (default: 25)")
    args = parser.parse_args()

    pnl, counts, volume, rollover_volume = compute_pnl()
    log.info("Distinct lenders with realized PNL (repaid, defaulted, or rolled-over interest): %d", len(pnl))
    print_leaderboard(pnl, counts, volume, rollover_volume, args.top)


if __name__ == "__main__":
    main()
