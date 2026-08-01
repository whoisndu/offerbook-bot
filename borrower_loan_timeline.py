"""
Offerbook Borrower Loan Timeline
==================================
Plots one borrower's full loan history as a Gantt-style timeline (one bar
per loan, colored by outcome), plus a concurrent-open-loans step chart
underneath — so you can see at a glance whether a borrower takes breaks
between loans, how often, and whether their activity is ramping up or down
over time. Scope it to a single collateral token, or to every token that
borrower has ever borrowed against at once.

Each loan bar is labeled with its principal size (USD) — and, when scoped to
"all" collateral, which token it was against, since bars can now span
several different tokens. Every detected gap (a stretch with zero loans
open) is labeled with its length in days directly on the chart, so both are
easy to eyeball without reading the console output.

If --collateral/--borrower are omitted, prompts interactively for them.
--collateral accepts a symbol, a mint address, or "all" (or just leave it
blank at the prompt) for every collateral that borrower has ever used.
Leaving --borrower blank auto-picks the largest borrower (by total USD
principal) within whatever --collateral scope was chosen.

Usage:
  python borrower_loan_timeline.py                      # prompts for borrower, then collateral
  python borrower_loan_timeline.py --borrower 4nFMipa1LwA6QQiVk29YqZeCvHixbWMMjcBR1h7jDMrZ --collateral all
  python borrower_loan_timeline.py --borrower 4nFMipa1LwA6QQiVk29YqZeCvHixbWMMjcBR1h7jDMrZ --collateral USELESS
  python borrower_loan_timeline.py --collateral USELESS                      # auto-picks that token's largest borrower
  python borrower_loan_timeline.py --collateral USELESS --output /some/other/path.png

Charts always save to ~/Desktop/borrower_timeline_<borrower8>.png by default
(pass --output to save elsewhere instead).

Notes:
  - "Loans" here means USDC-principal loans, across all statuses
    (active/repaid/defaulted), scoped to one collateral or all of them.
  - A "gap" is a stretch where the borrower has zero loans open at all
    (concurrent-open-loans hits 0), not merely between individual loan
    originations — a borrower can open loan #2 before loan #1 closes, and
    that isn't a break. In "all collateral" mode this is measured across
    every token together — a loan against token A overlapping one against
    token B still counts as continuously busy, not a gap.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

import offerbook_common as _common

API_BASE = os.getenv("OFFERBOOK_API_BASE", "https://api.offerbook.jup.ag/api/v1")
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
PAGE_SIZE = 100

JUPITER_TOKEN_SEARCH_API = "https://api.jup.ag/tokens/v2/search"
JUPITER_SEARCH_BATCH_SIZE = 50  # keep query strings reasonably sized

KNOWN_SYMBOLS = _common.KNOWN_SYMBOLS
SYMBOL_TO_MINT = {sym.upper(): mint for mint, sym in KNOWN_SYMBOLS.items()}

# Populated once per run by resolve_collateral_symbols() below — mints not in
# the curated KNOWN_SYMBOLS table (long-tail/pump.fun tokens) get their real
# symbol looked up via Jupiter instead of falling back to a truncated address.
_RESOLVED_SYMBOLS: dict[str, str] = {}

DESKTOP_DIR = Path.home() / "Desktop"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("borrower_loan_timeline")

SESSION = requests.Session()

COLOR_ON_TIME = "#22c55e"
COLOR_LATE = "#f97316"
COLOR_DEFAULTED = "#ef4444"
COLOR_ACTIVE = "#3b82f6"

MIN_GAP_DAYS_TO_LABEL = 0.05  # ignore sub-hour rounding noise between adjacent loans


def _parse_ts(t: str) -> datetime:
    return datetime.fromisoformat(t.replace("Z", "+00:00"))


def _fetch_all_pages(endpoint: str, params: dict | None = None) -> list[dict]:
    return _common.fetch_all_pages(SESSION, API_BASE, endpoint, params, PAGE_SIZE)


def fetch_all_loans() -> list[dict]:
    """Every USDC-principal loan (active/repaid/defaulted) platform-wide,
    regardless of collateral. Fetched once and filtered in-memory by
    collateral/borrower as needed, rather than re-fetching per scope."""
    loans: list[dict] = []
    for status in ("active", "repaid", "defaulted"):
        items = _fetch_all_pages(f"/loans/status/{status}")
        for l in items:
            pmint = l.get("principalMint") or _common._mint_from_asset(l.get("principal", {}))
            if pmint == USDC_MINT:
                l["_status"] = status
                loans.append(l)
    return loans


def filter_by_collateral(loans: list[dict], collateral_mint: str) -> list[dict]:
    return [
        l for l in loans
        if (l.get("collateralMint") or _common._mint_from_asset(l.get("collateral", {}))) == collateral_mint
    ]


def filter_by_borrower(loans: list[dict], borrower: str) -> list[dict]:
    return [l for l in loans if l.get("borrower") == borrower]


def collateral_symbol_for(loan: dict) -> str:
    cmint = loan.get("collateralMint") or _common._mint_from_asset(loan.get("collateral", {}))
    if cmint in KNOWN_SYMBOLS:
        return KNOWN_SYMBOLS[cmint]
    if cmint in _RESOLVED_SYMBOLS:
        return _RESOLVED_SYMBOLS[cmint]
    return cmint[:6] + "…" if cmint else "?"


def resolve_collateral_symbols(loans: list[dict]) -> None:
    """Look up real symbols (via Jupiter's token search API) for every
    collateral mint in `loans` that isn't already in KNOWN_SYMBOLS, and cache
    them in _RESOLVED_SYMBOLS — covers long-tail/pump.fun tokens the curated
    table doesn't have. Same approach update_config.py uses for the same
    reason: Jupiter indexes far more of these than Offerbook's own registry.
    NFT collateral (no fungible mint) is left alone; collateral_symbol_for()
    falls back to a truncated address for those."""
    mints = {
        loan.get("collateralMint") or _common._mint_from_asset(loan.get("collateral", {}))
        for loan in loans
    }
    unresolved = sorted(m for m in mints if m and m not in KNOWN_SYMBOLS and m not in _RESOLVED_SYMBOLS)
    if not unresolved:
        return
    for i in range(0, len(unresolved), JUPITER_SEARCH_BATCH_SIZE):
        chunk = unresolved[i : i + JUPITER_SEARCH_BATCH_SIZE]
        try:
            resp = SESSION.get(JUPITER_TOKEN_SEARCH_API, params={"query": ",".join(chunk)}, timeout=15)
            resp.raise_for_status()
            for t in resp.json():
                mint, symbol = t.get("id"), t.get("symbol")
                if mint and symbol:
                    _RESOLVED_SYMBOLS[mint] = symbol
        except Exception as exc:
            log.warning("Jupiter token symbol lookup failed for a batch: %s", exc)


def find_largest_borrower(loans: list[dict]) -> str:
    """Borrower with the highest total USD principal across the given loans."""
    totals: dict[str, float] = defaultdict(float)
    for l in loans:
        b = l.get("borrower")
        usd = (l.get("metadata") or {}).get("startPrincipalAmountUsd") or 0.0
        totals[b] += usd
    return max(totals.items(), key=lambda kv: kv[1])[0]


def build_rows(loans: list[dict], now: datetime) -> list[dict]:
    rows = []
    for l in loans:
        start = _parse_ts(l["createdAt"])
        expired = _parse_ts(l["expiredAt"])
        status = l["_status"]
        if status == "active":
            end = now
            color, label = COLOR_ACTIVE, "active"
        elif status == "defaulted":
            end = _parse_ts(l["updatedAt"]) if l.get("updatedAt") else expired
            color, label = COLOR_DEFAULTED, "defaulted"
        else:  # repaid
            end = _parse_ts(l["updatedAt"])
            if end > expired:
                color, label = COLOR_LATE, "late repaid"
            else:
                color, label = COLOR_ON_TIME, "on-time repaid"
        usd = (l.get("metadata") or {}).get("startPrincipalAmountUsd") or 0.0
        rows.append({
            "start": start, "end": end, "color": color, "label": label, "usd": usd,
            "collateral_symbol": collateral_symbol_for(l),
        })
    rows.sort(key=lambda r: r["start"])
    return rows


def concurrent_open_series(rows: list[dict], now: datetime) -> tuple[list[datetime], list[int]]:
    """Step-function series of how many loans were open at any instant."""
    events = []
    for r in rows:
        events.append((r["start"], 1))
        events.append((r["end"], -1))
    events.sort(key=lambda e: e[0])

    times = [rows[0]["start"]]
    counts = [0]
    running = 0
    for t, delta in events:
        times.append(t)
        counts.append(running)
        running += delta
        times.append(t)
        counts.append(running)
    times.append(now)
    counts.append(running)
    return times, counts


def find_gaps(rows: list[dict]) -> list[tuple[datetime, datetime, float]]:
    """Merge overlapping/adjacent loan intervals, then return the gaps between
    the resulting busy stretches as (gap_start, gap_end, gap_days)."""
    merged = sorted((r["start"], r["end"]) for r in rows)
    intervals = [merged[0]]
    for s, e in merged[1:]:
        last_s, last_e = intervals[-1]
        if s <= last_e:
            intervals[-1] = (last_s, max(last_e, e))
        else:
            intervals.append((s, e))

    gaps = []
    for i in range(len(intervals) - 1):
        gap_start = intervals[i][1]
        gap_end = intervals[i + 1][0]
        gap_days = (gap_end - gap_start).total_seconds() / 86400
        if gap_days > MIN_GAP_DAYS_TO_LABEL:
            gaps.append((gap_start, gap_end, gap_days))
    return gaps, intervals


def print_summary(rows: list[dict], gaps: list[tuple[datetime, datetime, float]], intervals, now: datetime) -> None:
    log.info("Total loans: %d", len(rows))
    log.info("Date range: %s to %s", rows[0]["start"].date(), rows[-1]["end"].date())
    log.info("Total principal borrowed: $%.2f", sum(r["usd"] for r in rows))
    log.info("Busy intervals (continuous stretches with >=1 loan open): %d", len(intervals))
    log.info("Gaps (zero loans open): %d", len(gaps))
    if gaps:
        gap_lengths = [g[2] for g in gaps]
        log.info("  avg gap: %.1fd   median: %.1fd   longest: %.1fd   shortest: %.1fd",
                  sum(gap_lengths) / len(gap_lengths), sorted(gap_lengths)[len(gap_lengths) // 2],
                  max(gap_lengths), min(gap_lengths))
    trailing_gap_days = (now - intervals[-1][1]).total_seconds() / 86400
    log.info("Trailing gap (since last loan closed, up to now): %.1fd", trailing_gap_days)
    log.info("")
    for gs, ge, gd in gaps:
        log.info("  gap: %s -> %s  (%.1fd)", gs.date(), ge.date(), gd)


def plot(rows: list[dict], gaps: list[tuple[datetime, datetime, float]], borrower: str,
         collateral_label: str, now: datetime, output_path: str) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt

    times, counts = concurrent_open_series(rows, now)

    fig, (ax1, ax2) = plt.subplots(
        2, 1, figsize=(14, max(6, len(rows) * 0.16 + 2)), sharex=True,
        gridspec_kw={"height_ratios": [3, 1]},
    )

    span_days = max((rows[-1]["end"] - rows[0]["start"]).total_seconds() / 86400, 1)
    label_offset = timedelta(days=span_days * 0.004)

    for i, r in enumerate(rows):
        ax1.barh(i, (r["end"] - r["start"]).total_seconds() / 86400,
                  left=r["start"], color=r["color"], edgecolor="black", linewidth=0.3, height=0.8)
        ax1.text(r["end"] + label_offset, i, f"${r['usd']:,.0f} {r['collateral_symbol']}",
                  va="center", ha="left", fontsize=6)

    ax1.set_ylabel("Loan (chronological)")
    ax1.set_title(
        f"USDC/{collateral_label} loan timeline — {borrower}\n"
        f"{len(rows)} loans, ${sum(r['usd'] for r in rows):,.0f} total principal borrowed"
    )
    ax1.set_yticks([])
    ax1.invert_yaxis()

    legend_handles = [
        plt.Rectangle((0, 0), 1, 1, color=COLOR_ON_TIME, label="on-time repaid"),
        plt.Rectangle((0, 0), 1, 1, color=COLOR_LATE, label="late repaid"),
        plt.Rectangle((0, 0), 1, 1, color=COLOR_DEFAULTED, label="defaulted"),
        plt.Rectangle((0, 0), 1, 1, color=COLOR_ACTIVE, label="active"),
    ]
    ax1.legend(handles=legend_handles, loc="upper left")

    gap_label_trans = ax2.get_xaxis_transform()  # x in data coords, y in axes fraction (0-1)
    for gs, ge, gd in gaps:
        mid = gs + (ge - gs) / 2
        ax1.axvspan(gs, ge, color="grey", alpha=0.15)
        ax2.axvspan(gs, ge, color="grey", alpha=0.15)
        ax2.text(mid, 0.5, f"{gd:.1f}d", transform=gap_label_trans,
                  ha="center", va="center", fontsize=8, rotation=90, color="dimgrey")

    ax2.plot(times, counts, color="black", drawstyle="steps-post", linewidth=1.2)
    ax2.fill_between(times, counts, step="post", alpha=0.2, color="black")
    ax2.set_ylabel("Concurrent\nopen loans")
    ax2.set_xlabel("Date")
    ax2.set_ylim(bottom=0)

    ax2.xaxis.set_major_locator(mdates.AutoDateLocator())
    ax2.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m-%d"))
    fig.autofmt_xdate()

    plt.tight_layout()
    plt.savefig(output_path, dpi=130)
    log.info("Saved chart to %s", output_path)


def prompt_for_borrower() -> str | None:
    """Interactively ask for a borrower address; blank means auto-pick the largest
    (within whatever collateral scope is chosen next)."""
    raw = input("Enter borrower address (leave blank to auto-pick the largest borrower): ").strip()
    return raw or None


def prompt_for_collateral() -> str:
    """Interactively ask for a collateral symbol/mint, or blank/'all' for every
    collateral the borrower has ever used."""
    raw = input(
        'Enter collateral symbol (e.g. USELESS) or mint address, '
        'or leave blank for ALL collateral this borrower has used: '
    ).strip()
    return raw or "all"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--borrower", default=None, help="Borrower wallet address. Omit to be prompted (blank there auto-picks the largest borrower).")
    parser.add_argument("--collateral", default=None, help='Collateral symbol, mint address, or "all". Omit to be prompted (blank there means all).')
    parser.add_argument("--output", default=None, help="PNG output path. Defaults to ~/Desktop/borrower_timeline_<borrower8>.png")
    args = parser.parse_args()

    # A flag not passed on the command line at all → ask interactively.
    borrower_arg = args.borrower if "--borrower" in sys.argv else prompt_for_borrower()
    collateral_arg = (args.collateral if "--collateral" in sys.argv else prompt_for_collateral()) or "all"

    log.info("Fetching loan history platform-wide…")
    all_loans = fetch_all_loans()
    if not all_loans:
        log.error("No USDC-principal loans found platform-wide.")
        sys.exit(1)

    all_collateral = collateral_arg.strip().upper() == "ALL"
    if all_collateral:
        scoped_loans = all_loans
        collateral_label = "ALL collateral"
    else:
        collateral_mint = SYMBOL_TO_MINT.get(collateral_arg.upper(), collateral_arg)
        collateral_label = KNOWN_SYMBOLS.get(collateral_mint, collateral_mint[:8] + "…")
        scoped_loans = filter_by_collateral(all_loans, collateral_mint)
        if not scoped_loans:
            log.error("No loans found for collateral %s — check the mint/symbol.", collateral_arg)
            sys.exit(1)

    borrower = borrower_arg or find_largest_borrower(scoped_loans)
    if not borrower_arg:
        log.info("No borrower given — auto-selected largest borrower: %s", borrower)

    loans = filter_by_borrower(scoped_loans, borrower)
    if not loans:
        log.error("No loans found for borrower %s with collateral scope %s.", borrower, collateral_label)
        sys.exit(1)

    resolve_collateral_symbols(loans)

    if all_collateral:
        used = sorted({collateral_symbol_for(l) for l in loans})
        log.info("Collateral tokens used by this borrower: %s", ", ".join(used))

    now = datetime.now(timezone.utc)
    rows = build_rows(loans, now)
    gaps, intervals = find_gaps(rows)
    print_summary(rows, gaps, intervals, now)

    output_path = args.output or str(DESKTOP_DIR / f"borrower_timeline_{borrower[:8]}.png")
    plot(rows, gaps, borrower, collateral_label, now, output_path)


if __name__ == "__main__":
    main()
