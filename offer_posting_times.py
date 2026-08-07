"""
Offerbook Competing-Offer Posting-Time Chart
==============================================
Charts WHEN competing lenders post their offers for a given collateral, so
you can pick a time to run strategy.py that lands after most of today's
competing volume is already on the book — its undercut benchmark then prices
against fresh, representative competitor data instead of whatever's left
over from yesterday.

Pulls every lending offer Offerbook has recorded for the chosen collateral
across every status (active/partiallyFilled/fulfilled/cancelled/expired) —
not just what's live right now, since currently-live offers alone are capped
by the platform's 24h expiry and would only show a partial day's postings —
within a recent lookback window (--days-back, default 30; older postings may
not reflect who's actually competing today).

Each offer's `createdAt` is converted to local time and:
  1. Charted as a scatter (date vs. hour-of-day, colored by status) so you
     can see whether the daily posting rhythm is consistent or drifting.
  2. Aggregated into an hourly histogram (offer count) with a cumulative
     %-of-volume line on a twin axis, so the hour by which most of a typical
     day's competing USD volume has already posted is obvious at a glance.

Your own offers are excluded by default (--include-self to keep them) — the
point is to see when COMPETITORS post, not to have your own bot's activity
dilute that signal.

Usage:
  python offer_posting_times.py                      # prompts for collateral
  python offer_posting_times.py --collateral PUMP
  python offer_posting_times.py --collateral all      # every collateral together
  python offer_posting_times.py --collateral PUMP --days-back 14
  python offer_posting_times.py --collateral PUMP --tz America/New_York
  python offer_posting_times.py --collateral PUMP --coverage 0.9
  python offer_posting_times.py --collateral PUMP --include-self
  python offer_posting_times.py --collateral PUMP --all-principals
  python offer_posting_times.py --collateral PUMP --output /some/other/path.png

Notes:
  - Principal is scoped to USDC by default (matches strategy.py's own
    lending — a non-USDC lending offer isn't really a competitor for that
    business) — pass --all-principals to include every principal token.
  - Local time defaults to this machine's system timezone; pass --tz for an
    explicit IANA zone name (e.g. "UTC", "America/New_York").
  - --coverage (default 0.80) sets the "recommended post-after" hour: the
    first hour by whose end at least that fraction of a typical day's
    competing USD volume has historically posted.
  - Read-only, no signing. Self-exclusion needs OFFERBOOK_WALLET (or
    --wallet) set — if neither is set, self-exclusion silently has nothing
    to exclude, so a warning is logged.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests
from dotenv import load_dotenv

load_dotenv()

import offerbook_common as _common
from offerbook_common import _mint_from_asset

API_BASE = os.getenv("OFFERBOOK_API_BASE", "https://api.offerbook.jup.ag/api/v1")
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
PAGE_SIZE = 100

JUPITER_TOKEN_SEARCH_API = "https://api.jup.ag/tokens/v2/search"
JUPITER_SEARCH_BATCH_SIZE = 50

KNOWN_SYMBOLS = _common.KNOWN_SYMBOLS
SYMBOL_TO_MINT = {sym.upper(): mint for mint, sym in KNOWN_SYMBOLS.items()}

# Populated once per run by resolve_symbols() below — mints not in the
# curated KNOWN_SYMBOLS table (long-tail/pump.fun tokens) get their real
# symbol looked up via Jupiter instead of a truncated address.
_RESOLVED_SYMBOLS: dict[str, str] = {}

DESKTOP_DIR = Path.home() / "Desktop"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("offer_posting_times")

SESSION = requests.Session()

STATUS_COLORS = {
    "active": "#3b82f6",
    "partiallyFilled": "#0ea5e9",
    "fulfilled": "#22c55e",
    "cancelled": "#9ca3af",
    "expired": "#f97316",
}


def _parse_ts(t: str) -> datetime:
    return datetime.fromisoformat(t.replace("Z", "+00:00"))


def symbol_for(mint: str) -> str:
    if mint in KNOWN_SYMBOLS:
        return KNOWN_SYMBOLS[mint]
    if mint in _RESOLVED_SYMBOLS:
        return _RESOLVED_SYMBOLS[mint]
    return mint[:6] + "…" if mint else "?"


def resolve_symbols(mints: set[str]) -> None:
    """Look up real symbols (via Jupiter's token search API) for any mint in
    `mints` that isn't already in KNOWN_SYMBOLS — covers long-tail/pump.fun
    tokens the curated table doesn't have. Same approach as
    borrower_loan_timeline.py's resolve_collateral_symbols()."""
    unresolved = sorted(m for m in mints if m and m not in KNOWN_SYMBOLS and m not in _RESOLVED_SYMBOLS)
    if not unresolved:
        return
    for i in range(0, len(unresolved), JUPITER_SEARCH_BATCH_SIZE):
        chunk = unresolved[i : i + JUPITER_SEARCH_BATCH_SIZE]
        try:
            resp = SESSION.get(JUPITER_TOKEN_SEARCH_API, params={"query": ",".join(chunk)}, timeout=15)
            resp.raise_for_status()
            for t in resp.json():
                mint, sym = t.get("id"), t.get("symbol")
                if mint and sym:
                    _RESOLVED_SYMBOLS[mint] = sym
        except Exception as exc:
            log.warning("Jupiter token symbol lookup failed for a batch: %s", exc)


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------

def fetch_recent_offers(
    collateral_mint: str | None, principal_mint: str | None, cutoff: datetime,
) -> list[dict]:
    """
    Fetch every lending offer (any status) for the given scope, newest first,
    stopping as soon as a page's offers are older than `cutoff` — sorted
    descending by createdAt, so once one offer is older than the cutoff every
    offer after it is too. Avoids paging through a token's entire history
    (some collateral pairs have thousands of historical offers) just to look
    at the last `--days-back` days of it.
    """
    params = {
        "offerType": "lending",
        "status": "active,partiallyFilled,fulfilled,cancelled,expired",
        "sort": "createdAt",
        "sortDirection": "desc",
        "showUnverified": "true",
        "includeUnderfunded": "true",
        "limit": PAGE_SIZE,
        "offset": 0,
    }
    if collateral_mint:
        params["collateralMint"] = collateral_mint
    if principal_mint:
        params["principalMint"] = principal_mint

    items: list[dict] = []
    while True:
        data = _common.api_get(SESSION, API_BASE, "/offers", params)
        batch = data.get("data", [])
        if not batch:
            break
        reached_cutoff = False
        for o in batch:
            if _parse_ts(o["createdAt"]) < cutoff:
                reached_cutoff = True
                break
            items.append(o)
        if reached_cutoff or not data.get("pagination", {}).get("hasMore", False):
            break
        params["offset"] += PAGE_SIZE
        time.sleep(0.15)
    return items


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------

def build_rows(offers: list[dict], tz: ZoneInfo) -> list[dict]:
    rows = []
    for o in offers:
        created_utc = _parse_ts(o["createdAt"])
        local = created_utc.astimezone(tz)
        meta = o.get("metadata") or {}
        cmint = o.get("collateralMint") or _mint_from_asset(o.get("collateral", {}))
        rows.append({
            "created_utc": created_utc,
            "date": local.date(),
            "hour_frac": local.hour + local.minute / 60,
            "hour_bucket": local.hour,
            "usd": meta.get("principalAmountUsd") or 0.0,
            "status": o.get("status", "?"),
            "collateral_symbol": symbol_for(cmint),
        })
    rows.sort(key=lambda r: r["created_utc"])
    return rows


def hourly_aggregates(rows: list[dict]) -> tuple[list[int], list[float]]:
    counts = [0] * 24
    usd_sums = [0.0] * 24
    for r in rows:
        counts[r["hour_bucket"]] += 1
        usd_sums[r["hour_bucket"]] += r["usd"]
    return counts, usd_sums


def cumulative_pct(usd_sums: list[float]) -> list[float]:
    total = sum(usd_sums) or 1.0
    cum, running = [], 0.0
    for v in usd_sums:
        running += v
        cum.append(running / total * 100)
    return cum


def recommended_post_hour(cum_pct: list[float], coverage: float) -> int:
    """First hour (0-23) by whose END at least `coverage` of a typical day's
    competing USD volume has historically posted — i.e. post AFTER this hour."""
    threshold = coverage * 100
    for h, pct in enumerate(cum_pct):
        if pct >= threshold:
            return h
    return 23


def print_summary(rows: list[dict], counts: list[int], usd_sums: list[float],
                   cum_pct: list[float], coverage: float, tz_name: str) -> None:
    log.info("Offers analyzed: %d", len(rows))
    log.info("Date range: %s to %s (local tz: %s)", rows[0]["date"], rows[-1]["date"], tz_name)
    log.info("Total competing USD volume: $%.2f", sum(usd_sums))

    from collections import Counter
    status_counts = Counter(r["status"] for r in rows)
    log.info("By status: %s", ", ".join(f"{s}={c}" for s, c in sorted(status_counts.items())))

    log.info("")
    log.info("Hourly breakdown (local time):")
    log.info("  %-6s  %6s  %12s  %8s", "hour", "count", "usd volume", "cum %")
    for h in range(24):
        log.info("  %02d:00   %6d  %12.2f  %7.1f%%", h, counts[h], usd_sums[h], cum_pct[h])

    top_by_count = sorted(range(24), key=lambda h: counts[h], reverse=True)[:3]
    top_by_usd = sorted(range(24), key=lambda h: usd_sums[h], reverse=True)[:3]
    log.info("")
    log.info("Busiest hours by offer count: %s", ", ".join(f"{h:02d}:00 ({counts[h]})" for h in top_by_count))
    log.info("Busiest hours by USD volume:  %s", ", ".join(f"{h:02d}:00 (${usd_sums[h]:,.0f})" for h in top_by_usd))

    rec_hour = recommended_post_hour(cum_pct, coverage)
    log.info("")
    log.info(
        "Recommended: post after %02d:00 local — by then ~%.0f%% of a typical day's "
        "competing USD volume has historically already posted (coverage=%.0f%%).",
        (rec_hour + 1) % 24, cum_pct[rec_hour], coverage * 100,
    )


# ---------------------------------------------------------------------------
# Plotting
# ---------------------------------------------------------------------------

def plot(rows: list[dict], counts: list[int], usd_sums: list[float], cum_pct: list[float],
         coverage: float, label: str, principal_label: str, days_back: int,
         tz_name: str, output_path: str) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt

    rec_hour = recommended_post_hour(cum_pct, coverage)

    fig, (ax1, ax2) = plt.subplots(
        2, 1, figsize=(14, 9), gridspec_kw={"height_ratios": [2, 1.3]},
    )

    # --- ax1: scatter of (date, hour-of-day), colored by status, sized by USD ---
    seen_statuses = sorted({r["status"] for r in rows})
    for status in seen_statuses:
        subset = [r for r in rows if r["status"] == status]
        sizes = [8 + min(r["usd"], 20000) / 400 for r in subset]
        ax1.scatter(
            [r["date"] for r in subset], [r["hour_frac"] for r in subset],
            s=sizes, color=STATUS_COLORS.get(status, "#000000"), alpha=0.6,
            label=status, edgecolors="black", linewidths=0.2,
        )
    ax1.axhline(rec_hour + 1, color="red", linestyle="--", linewidth=1,
                label=f"recommended post-after ({(rec_hour + 1) % 24:02d}:00)")
    ax1.set_ylim(0, 24)
    ax1.set_yticks(range(0, 25, 2))
    ax1.set_ylabel(f"Hour of day ({tz_name})")
    ax1.set_title(
        f"Offerbook competing-offer posting times — {label} ({principal_label} principal)\n"
        f"{len(rows)} offers over the last {days_back}d"
    )
    ax1.legend(loc="upper left", fontsize=8, ncol=len(seen_statuses) + 1)
    ax1.xaxis.set_major_locator(mdates.AutoDateLocator())
    ax1.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m-%d"))

    # --- ax2: hourly count bars + cumulative %-of-volume line ---
    hours = list(range(24))
    ax2.bar(hours, counts, color="#3b82f6", alpha=0.7, label="offer count")
    ax2.set_xlabel(f"Hour of day ({tz_name})")
    ax2.set_ylabel("Offer count", color="#3b82f6")
    ax2.set_xticks(range(0, 24, 2))
    ax2.set_xlim(-0.5, 23.5)

    ax2b = ax2.twinx()
    ax2b.plot(hours, cum_pct, color="#111827", linewidth=1.8, marker="o", markersize=3,
              label="cumulative % of USD volume")
    ax2b.axhline(coverage * 100, color="grey", linestyle=":", linewidth=1)
    ax2b.axvline(rec_hour + 1, color="red", linestyle="--", linewidth=1,
                 label=f"recommended post-after ({(rec_hour + 1) % 24:02d}:00)")
    ax2b.set_ylabel("Cumulative % of USD volume")
    ax2b.set_ylim(0, 105)

    lines1, labels1 = ax2.get_legend_handles_labels()
    lines2, labels2 = ax2b.get_legend_handles_labels()
    ax2.legend(lines1 + lines2, labels1 + labels2, loc="upper left", fontsize=8)

    plt.tight_layout()
    plt.savefig(output_path, dpi=130)
    log.info("Saved chart to %s", output_path)


# ---------------------------------------------------------------------------
# Prompts / entry point
# ---------------------------------------------------------------------------

def prompt_for_collateral() -> str:
    raw = input(
        'Enter collateral symbol (e.g. PUMP) or mint address, '
        'or leave blank for ALL collateral: '
    ).strip()
    return raw or "all"


def resolve_tz(tz_arg: str | None) -> ZoneInfo:
    if tz_arg:
        try:
            return ZoneInfo(tz_arg)
        except ZoneInfoNotFoundError:
            log.error("Unknown timezone %r — use an IANA name, e.g. 'UTC' or 'America/New_York'.", tz_arg)
            sys.exit(1)
    local_tz = datetime.now().astimezone().tzinfo
    return local_tz  # type: ignore[return-value]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--collateral", default=None,
                         help='Collateral symbol, mint address, or "all". Omit to be prompted.')
    parser.add_argument("--days-back", type=int, default=30,
                         help="How many days of offer history to analyze (default 30).")
    parser.add_argument("--tz", default=None,
                         help="IANA timezone name for hour-of-day bucketing (default: this machine's local tz).")
    parser.add_argument("--coverage", type=float, default=0.80,
                         help="Fraction of a typical day's USD volume that should have posted "
                              "before the recommended post time (default 0.80).")
    parser.add_argument("--all-principals", action="store_true",
                         help="Include offers of every principal token, not just USDC.")
    parser.add_argument("--include-self", action="store_true",
                         help="Include your own offers instead of excluding them.")
    parser.add_argument("--wallet", default=None,
                         help="Wallet pubkey to exclude as 'self' (default: OFFERBOOK_WALLET env var).")
    parser.add_argument("--output", default=None, help="PNG output path (default ~/Desktop/offer_posting_times_<label>.png).")
    args = parser.parse_args()

    if not (0 < args.coverage <= 1):
        log.error("--coverage must be between 0 (exclusive) and 1 (inclusive).")
        sys.exit(1)

    tz = resolve_tz(args.tz)
    tz_name = args.tz or str(tz)

    collateral_arg = args.collateral if "--collateral" in sys.argv else prompt_for_collateral()
    collateral_arg = (collateral_arg or "all").strip()
    all_collateral = collateral_arg.upper() == "ALL"
    collateral_mint = None if all_collateral else SYMBOL_TO_MINT.get(collateral_arg.upper(), collateral_arg)
    label = "ALL collateral" if all_collateral else symbol_for(collateral_mint) if collateral_mint in KNOWN_SYMBOLS else collateral_arg

    principal_mint = None if args.all_principals else USDC_MINT
    principal_label = "ALL" if args.all_principals else "USDC"

    wallet = args.wallet or os.getenv("OFFERBOOK_WALLET", "")
    if not args.include_self and not wallet:
        log.warning("No OFFERBOOK_WALLET/--wallet set — self-exclusion has nothing to exclude; "
                    "your own offers (if any) will be included in the chart.")

    cutoff = datetime.now(timezone.utc) - timedelta(days=args.days_back)
    log.info("Fetching lending offers for %s (%s principal) since %s …",
             label, principal_label, cutoff.date())
    raw_offers = fetch_recent_offers(collateral_mint, principal_mint, cutoff)
    log.info("  → %d offer(s) fetched", len(raw_offers))

    if not args.include_self and wallet:
        before = len(raw_offers)
        raw_offers = [o for o in raw_offers if o.get("creator") != wallet]
        log.info("  → excluded %d of your own offer(s)", before - len(raw_offers))

    if not raw_offers:
        log.error("No offers found for this scope — try a wider --days-back, a different "
                   "--collateral, or --all-principals.")
        sys.exit(1)

    if all_collateral:
        resolve_symbols({o.get("collateralMint") or _mint_from_asset(o.get("collateral", {})) for o in raw_offers})

    rows = build_rows(raw_offers, tz)
    counts, usd_sums = hourly_aggregates(rows)
    cum_pct = cumulative_pct(usd_sums)

    print_summary(rows, counts, usd_sums, cum_pct, args.coverage, tz_name)

    safe_label = label.replace("/", "-").replace(" ", "_")
    output_path = args.output or str(DESKTOP_DIR / f"offer_posting_times_{safe_label}.png")
    plot(rows, counts, usd_sums, cum_pct, args.coverage, label, principal_label,
         args.days_back, tz_name, output_path)


if __name__ == "__main__":
    main()
