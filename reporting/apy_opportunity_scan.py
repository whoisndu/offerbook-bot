"""
Offerbook APY Opportunity Scan
==================================
"Am I maximizing every dollar I've got on offer?" — scans every currently
ACTIVE loan platform-wide (not resolved history; this is about what's live
right now) to answer that from a few angles:

  - Lender APY leaderboard: every lender ranked by the size-weighted average
    APY across their active loans (principal-weighted — a $10 loan at 200%
    APY shouldn't outrank a $10,000 loan at 60%). If OFFERBOOK_PORTFOLIO_WALLETS
    is set, those wallets are merged into a single "YOUR WALLETS (N combined)"
    row in the ranking (same trick ../reporting/pnl_leaderboard.py uses), so
    your own standing shows up inline against the real competition, plus its
    exact rank even if it falls outside --top.
  - Collateral-token APY breakdown: every collateral token currently in play,
    ranked by size-weighted average APY — directly answers "which tokens are
    paying the best right now, that I should be pointing more capital at."
  - Your token exposure vs. market: for every token you currently hold an
    active loan against, your own weighted-average APY on that token next to
    the platform-wide average for that same token — a same-token comparison
    flags whether you're underpriced relative to what others are actually
    getting filled at, not just a vague "the market pays more" feeling.
  - Missed opportunities: active loans that AREN'T yours, paying more than
    your own blended average APY (or --min-apy), ranked highest-APY first —
    concrete examples of terms you could have offered.
  - Poach candidates: same list, filtered further to loans expiring within
    --expiry-hours. A borrower paying someone else a high rate is about to be
    back in the market (an in-place extension can also happen without ever
    reopening the market — see portfolio_health.py's docstring on loan
    extensions — so this is a candidate list, not a guarantee) — worth having
    a competing offer ready, or watching that borrower via
    ../monitoring/wallet_tx_watch.py / borrow_offer_watch.py for when/if a
    fresh borrow request appears.

All dollar figures come from each loan's own metadata.startPrincipalAmountUsd
(priced by the platform at origination) — no live price-fetching needed, so
this only ever costs the one /loans/status/active fetch (paginated).

Usage:
  python apy_opportunity_scan.py
  python apy_opportunity_scan.py --wallets <addr1>,<addr2>
  python apy_opportunity_scan.py --min-principal 100       # noise filter for the lender leaderboard
  python apy_opportunity_scan.py --top 30 --top-opportunities 25
  python apy_opportunity_scan.py --expiry-hours 72         # widen the poach window
  python apy_opportunity_scan.py --min-apy 50              # override the opportunity/poach APY floor (percent, not bps)
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from collections import defaultdict
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

KNOWN_SYMBOLS = _common.KNOWN_SYMBOLS

MIN_PRINCIPAL_DEFAULT = 25.0   # lender-leaderboard noise filter — a single tiny loan at an
                                # extreme APY shouldn't outrank lenders with real capital deployed
TOP_DEFAULT = 20
TOP_OPPORTUNITIES_DEFAULT = 15
EXPIRY_HOURS_DEFAULT = 48       # same convention as soon_to_expire.py/portfolio_health.py

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("apy_opportunity_scan")

SESSION = requests.Session()


def _fetch_all_pages(endpoint: str) -> list[dict]:
    return _common.fetch_all_pages(SESSION, API_BASE, endpoint, None, PAGE_SIZE, sleep_secs=0.1)


def symbol_for(mint: str | None) -> str:
    if not mint:
        return "NFT"
    return KNOWN_SYMBOLS.get(mint, f"{mint[:6]}…{mint[-4:]}")


def _start_usd(l: dict) -> float:
    return (l.get("metadata") or {}).get("startPrincipalAmountUsd") or 0.0


def merge_label_for(wallets: set[str] | list[str]) -> str | None:
    """The single combined-row label `wallets`' own lenders get remapped to
    in the leaderboard — None if no wallets are configured. A shared helper
    so every function that needs to recognize "is this row mine" (build,
    print, filter) derives the exact same string instead of re-guessing it
    (e.g. via a fragile startswith() check)."""
    return f"YOUR WALLETS ({len(wallets)} combined)" if wallets else None


def _weighted_apy_stats(loans: list[dict]) -> dict:
    """Size-weighted average APY (weighted by startPrincipalAmountUsd) across
    `loans`, plus count, total principal, and the annualized $ interest
    these loans would generate at their current APY if held a full year
    (principal_usd * apy / 1.0) — a dollar-velocity figure alongside the
    rate, since "highest APY" and "highest $ earned" aren't always the same
    lender. avg_apy_bps is None (not 0) if there's no principal to weight by."""
    count = len(loans)
    principal_usd = sum(_start_usd(l) for l in loans)
    if principal_usd <= 0:
        return {"count": count, "principal_usd": 0.0, "avg_apy_bps": None, "annualized_interest_usd": 0.0}
    weighted_sum = sum(_start_usd(l) * (l.get("apy") or 0) for l in loans)
    annualized_interest_usd = sum(_start_usd(l) * (l.get("apy") or 0) / 10000 for l in loans)
    return {
        "count": count,
        "principal_usd": principal_usd,
        "avg_apy_bps": weighted_sum / principal_usd,
        "annualized_interest_usd": annualized_interest_usd,
    }


def build_lender_leaderboard(active: list[dict], wallets: set[str]) -> tuple[list[dict], dict[str, int]]:
    """Every lender's weighted APY stats, ranked descending. If `wallets` is
    non-empty, those lenders are merged into one "YOUR WALLETS (N combined)"
    row (never ranked individually alongside it) — same remap trick
    pnl_leaderboard.py uses for the same reason: your own combined standing
    should show up inline against the real competition, not split into
    several smaller rows. Returns (ranked rows, {lender_or_label: rank}) —
    the rank map covers EVERY lender, not just whatever --top later trims
    the printed table to, so "your rank" is always knowable even outside the
    top N."""
    merge_label = merge_label_for(wallets)

    def remap(lender: str) -> str:
        return merge_label if wallets and lender in wallets else lender

    by_lender: dict[str, list[dict]] = defaultdict(list)
    for l in active:
        lender = l.get("lender")
        if lender:
            by_lender[remap(lender)].append(l)

    rows = []
    for lender, loans in by_lender.items():
        stats = _weighted_apy_stats(loans)
        rows.append({"lender": lender, **stats})
    rows.sort(key=lambda r: (r["avg_apy_bps"] is None, -(r["avg_apy_bps"] or 0)))

    rank_by_lender = {r["lender"]: i + 1 for i, r in enumerate(rows)}
    return rows, rank_by_lender


def build_token_breakdown(active: list[dict], wallets: set[str]) -> list[dict]:
    """Every collateral token's weighted APY stats across ALL active loans
    (every lender, including `wallets`' own — this is "what's the market
    paying", not "what's everyone else paying"), ranked descending, plus
    whether any of `wallets` currently holds an active loan against that
    token (your_exposure)."""
    by_mint: dict[str, list[dict]] = defaultdict(list)
    for l in active:
        cmint = l.get("collateralMint") or _mint_from_asset(l.get("collateral", {}))
        by_mint[cmint].append(l)

    rows = []
    for cmint, loans in by_mint.items():
        stats = _weighted_apy_stats(loans)
        max_apy_bps = max((l.get("apy") or 0) for l in loans)
        your_exposure = any(l.get("lender") in wallets for l in loans)
        rows.append({
            "collateral_mint": cmint, "symbol": symbol_for(cmint),
            "max_apy_bps": max_apy_bps, "your_exposure": your_exposure, **stats,
        })
    rows.sort(key=lambda r: (r["avg_apy_bps"] is None, -(r["avg_apy_bps"] or 0)))
    return rows


def build_your_token_exposure(active: list[dict], wallets: set[str], token_breakdown: list[dict]) -> list[dict]:
    """For every collateral token where any of `wallets` holds an active
    loan, your own weighted-average APY on that token next to the
    platform-wide (token_breakdown) figure for the same token — a same-token
    comparison, not just "the market pays more in general." gap_bps > 0
    means you're earning MORE than the platform average on that token;
    negative means you're leaving yield on the table specifically there."""
    market_by_mint = {r["collateral_mint"]: r["avg_apy_bps"] for r in token_breakdown}
    by_mint: dict[str, list[dict]] = defaultdict(list)
    for l in active:
        if l.get("lender") not in wallets:
            continue
        cmint = l.get("collateralMint") or _mint_from_asset(l.get("collateral", {}))
        by_mint[cmint].append(l)

    rows = []
    for cmint, loans in by_mint.items():
        stats = _weighted_apy_stats(loans)
        market_apy_bps = market_by_mint.get(cmint)
        gap_bps = (stats["avg_apy_bps"] - market_apy_bps) if stats["avg_apy_bps"] is not None and market_apy_bps is not None else None
        rows.append({"collateral_mint": cmint, "symbol": symbol_for(cmint), "market_avg_apy_bps": market_apy_bps, "gap_bps": gap_bps, **stats})
    rows.sort(key=lambda r: (r["gap_bps"] is None, r["gap_bps"] if r["gap_bps"] is not None else 0))
    return rows


def find_opportunities(active: list[dict], wallets: set[str], min_apy_bps: float, now: datetime) -> list[dict]:
    """Active loans NOT lent by `wallets`, paying at/above min_apy_bps,
    highest APY first — concrete terms you could have offered instead."""
    rows = []
    for l in active:
        if l.get("lender") in wallets:
            continue
        apy_bps = l.get("apy") or 0
        if apy_bps < min_apy_bps:
            continue
        expired_at = None
        hrs_left = None
        try:
            expired_at = datetime.fromisoformat(l["expiredAt"].replace("Z", "+00:00"))
            hrs_left = (expired_at - now).total_seconds() / 3600.0
        except (KeyError, ValueError):
            pass
        cmint = l.get("collateralMint") or _mint_from_asset(l.get("collateral", {}))
        rows.append({
            "loan_id": l.get("pubkey", ""), "lender": l.get("lender", ""), "borrower": l.get("borrower", ""),
            "collateral_symbol": symbol_for(cmint), "principal_usd": _start_usd(l),
            "apy_bps": apy_bps, "hrs_left": hrs_left,
        })
    rows.sort(key=lambda r: -r["apy_bps"])
    return rows

# ---------------------------------------------------------------------------
# Printing
# ---------------------------------------------------------------------------

def _fmt_apy(bps: float | None) -> str:
    return f"{bps / 100:.2f}%" if bps is not None else "n/a"


def _fmt_hrs(hrs: float | None) -> str:
    if hrs is None:
        return "n/a"
    if hrs < 0:
        return f"OVERDUE {abs(hrs):.1f}h"
    if hrs < 48:
        return f"{hrs:.1f}h"
    return f"{hrs / 24:.1f}d"


def print_your_wallets(active: list[dict], wallets: list[str]) -> dict:
    """Prints per-wallet + combined stats. Returns the combined stats dict
    (used downstream as the default opportunity/poach APY floor)."""
    log.info("")
    log.info("=" * 100)
    log.info("YOUR WALLETS")
    log.info("=" * 100)
    if not wallets:
        log.info("No wallets configured — set OFFERBOOK_PORTFOLIO_WALLETS in .env or pass --wallets.")
        return _weighted_apy_stats([])

    for w in wallets:
        loans = [l for l in active if l.get("lender") == w]
        stats = _weighted_apy_stats(loans)
        log.info(
            "  %s — %d active loan(s), $%s principal, weighted avg APY: %s, annualized interest run-rate: $%s",
            w, stats["count"], f"{stats['principal_usd']:,.2f}", _fmt_apy(stats["avg_apy_bps"]),
            f"{stats['annualized_interest_usd']:,.2f}",
        )

    combined_loans = [l for l in active if l.get("lender") in set(wallets)]
    combined = _weighted_apy_stats(combined_loans)
    if len(wallets) > 1:
        log.info(
            "  COMBINED — %d active loan(s), $%s principal, weighted avg APY: %s, annualized interest run-rate: $%s",
            combined["count"], f"{combined['principal_usd']:,.2f}", _fmt_apy(combined["avg_apy_bps"]),
            f"{combined['annualized_interest_usd']:,.2f}",
        )
    return combined


def print_lender_leaderboard(rows: list[dict], rank_by_lender: dict[str, int], merge_label: str | None, top: int) -> None:
    log.info("")
    log.info("=" * 100)
    log.info("LENDER APY LEADERBOARD (active loans, platform-wide, weighted by principal $)")
    log.info("=" * 100)
    col = "{:<4}{:<46}{:>9}{:>16}{:>10}  {:>22}"
    log.info(col.format("#", "lender", "loans", "principal $", "avg APY", "annualized interest $"))
    log.info("-" * 100)
    shown = rows[:top]
    for r in shown:
        rank = rank_by_lender[r["lender"]]
        tag = "  <-- YOU" if r["lender"] == merge_label else ""
        log.info(col.format(
            rank, r["lender"], r["count"], f"{r['principal_usd']:,.2f}",
            _fmt_apy(r["avg_apy_bps"]), f"{r['annualized_interest_usd']:,.2f}",
        ) + tag)

    if merge_label and merge_label not in {r["lender"] for r in shown}:
        your_row = next(r for r in rows if r["lender"] == merge_label)
        rank = rank_by_lender[merge_label]
        log.info("-" * 100)
        log.info(col.format(
            rank, your_row["lender"], your_row["count"], f"{your_row['principal_usd']:,.2f}",
            _fmt_apy(your_row["avg_apy_bps"]), f"{your_row['annualized_interest_usd']:,.2f}",
        ) + "  <-- YOU (outside top %d)" % top)
    log.info("=" * 100)


def print_token_breakdown(rows: list[dict], top: int) -> None:
    log.info("")
    log.info("=" * 100)
    log.info("COLLATERAL TOKEN APY BREAKDOWN (active loans, platform-wide, weighted by principal $)")
    log.info("=" * 100)
    col = "{:<16}{:>9}{:>16}{:>12}{:>12}{:>12}"
    log.info(col.format("token", "loans", "principal $", "avg APY", "max APY", "you?"))
    log.info("-" * 100)
    for r in rows[:top]:
        log.info(col.format(
            r["symbol"], r["count"], f"{r['principal_usd']:,.2f}",
            _fmt_apy(r["avg_apy_bps"]), _fmt_apy(r["max_apy_bps"]), "yes" if r["your_exposure"] else "",
        ))
    log.info("=" * 100)


def print_your_token_exposure(rows: list[dict]) -> None:
    log.info("")
    log.info("=" * 100)
    log.info("YOUR TOKEN EXPOSURE vs. MARKET (your weighted avg APY per token vs. the platform-wide figure)")
    log.info("=" * 100)
    if not rows:
        log.info("No active loans on any of your wallets — nothing to compare.")
        return
    col = "{:<16}{:>9}{:>16}{:>12}{:>14}{:>12}"
    log.info(col.format("token", "loans", "principal $", "your APY", "market APY", "gap"))
    log.info("-" * 100)
    for r in rows:
        gap_str = f"{r['gap_bps'] / 100:+.2f}pp" if r["gap_bps"] is not None else "n/a"
        flag = "  *** BELOW MARKET ***" if r["gap_bps"] is not None and r["gap_bps"] < 0 else ""
        log.info(col.format(
            r["symbol"], r["count"], f"{r['principal_usd']:,.2f}",
            _fmt_apy(r["avg_apy_bps"]), _fmt_apy(r["market_avg_apy_bps"]), gap_str,
        ) + flag)
    log.info("=" * 100)


def print_opportunities(rows: list[dict], min_apy_bps: float, title: str, top: int) -> None:
    log.info("")
    log.info("=" * 100)
    log.info("%s (APY >= %s, not yours) — top %d", title, _fmt_apy(min_apy_bps), top)
    log.info("=" * 100)
    if not rows:
        log.info("None right now.")
        return
    col = "{:<46}{:<16}{:>12}{:>10}  {:>16}"
    log.info(col.format("lender", "collateral", "principal $", "APY", "due"))
    log.info("-" * 100)
    for r in rows[:top]:
        log.info(col.format(
            r["lender"], r["collateral_symbol"], f"{r['principal_usd']:,.2f}",
            _fmt_apy(r["apy_bps"]), _fmt_hrs(r["hrs_left"]),
        ))
    log.info("=" * 100)

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--wallets", default=None, help="Comma-separated wallet addresses. Defaults to OFFERBOOK_PORTFOLIO_WALLETS in .env.")
    parser.add_argument("--min-principal", type=float, default=MIN_PRINCIPAL_DEFAULT, help=f"Noise filter for the lender leaderboard — exclude lenders with less than this much active principal (default {MIN_PRINCIPAL_DEFAULT}). Your own wallets' row is never filtered out by this.")
    parser.add_argument("--top", type=int, default=TOP_DEFAULT, help=f"Rows to show in the leaderboard/token-breakdown tables (default {TOP_DEFAULT}).")
    parser.add_argument("--top-opportunities", type=int, default=TOP_OPPORTUNITIES_DEFAULT, help=f"Rows to show in the missed-opportunities/poach-candidates tables (default {TOP_OPPORTUNITIES_DEFAULT}).")
    parser.add_argument("--expiry-hours", type=float, default=EXPIRY_HOURS_DEFAULT, help=f"Poach-candidate window — active loans due within this many hours (default {EXPIRY_HOURS_DEFAULT}).")
    parser.add_argument("--min-apy", type=float, default=None, help="APY floor (percent, e.g. 60 for 60%%) for the opportunities/poach sections. Default: your own combined weighted-average APY — i.e. only shows what's beating what you're already getting.")
    args = parser.parse_args()

    raw_wallets = args.wallets or os.getenv("OFFERBOOK_PORTFOLIO_WALLETS", "")
    wallets = [w.strip() for w in raw_wallets.split(",") if w.strip()]
    wallet_set = set(wallets)

    now = datetime.now(timezone.utc)

    log.info("Fetching active loans platform-wide …")
    active = _fetch_all_pages("/loans/status/active")
    log.info("  → %d active loan(s) platform-wide", len(active))

    combined_stats = print_your_wallets(active, wallets)

    min_apy_bps = args.min_apy * 100 if args.min_apy is not None else (combined_stats["avg_apy_bps"] or 0.0)
    if args.min_apy is None:
        log.info("")
        log.info("Opportunity/poach APY floor defaulting to your combined weighted avg: %s (override with --min-apy)", _fmt_apy(min_apy_bps))

    leaderboard_rows, rank_by_lender = build_lender_leaderboard(active, wallet_set)
    merge_label = merge_label_for(wallets)
    filtered_leaderboard = [
        r for r in leaderboard_rows
        if r["principal_usd"] >= args.min_principal or r["lender"] == merge_label
    ]
    print_lender_leaderboard(filtered_leaderboard, rank_by_lender, merge_label, args.top)

    token_rows = build_token_breakdown(active, wallet_set)
    print_token_breakdown(token_rows, args.top)

    exposure_rows = build_your_token_exposure(active, wallet_set, token_rows)
    print_your_token_exposure(exposure_rows)

    opportunity_rows = find_opportunities(active, wallet_set, min_apy_bps, now)
    print_opportunities(opportunity_rows, min_apy_bps, "MISSED OPPORTUNITIES", args.top_opportunities)

    poach_rows = [r for r in opportunity_rows if r["hrs_left"] is not None and r["hrs_left"] <= args.expiry_hours]
    poach_rows.sort(key=lambda r: -r["apy_bps"])
    print_opportunities(poach_rows, min_apy_bps, f"POACH CANDIDATES (expiring within {args.expiry_hours:.0f}h)", args.top_opportunities)


if __name__ == "__main__":
    main()
