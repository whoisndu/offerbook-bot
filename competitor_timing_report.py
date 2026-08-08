"""
Offerbook Competitor Posting-Time Distillation Report
========================================================
Since strategy.py posts ALL of its offers in one batch run rather than
trickling them out over the day, the question that matters isn't "when do
most offers get posted" for one collateral (see offer_posting_times.py for
that) — it's "when has essentially every top competitor across the WHOLE
market already posted for the day," so a single run can undercut
everyone's fresh pricing at once instead of some competitors' stale
yesterday offers.

This pulls every lending offer platform-wide (every collateral pair, not
one) over a rolling lookback window, ranks lenders by total USD principal
offered in that window (the "top lenders" — the ones whose pricing
actually matters to undercut), and profiles both the aggregate market
posting rhythm and each top lender's individual posting hours. Two local,
free (no external API, no billing) ML techniques then turn that into
genuine analysis rather than just a bigger table:

  - K-MEANS CLUSTERING (scikit-learn) groups top lenders by the SHAPE of
    their 24-hour USD-weighted posting profile — behavioral archetypes
    ("morning poster", "evening poster", etc.) rather than just a volume
    ranking. K is picked automatically via silhouette score. Clustering,
    not regression, is the right tool for hour-of-day: it's circular
    (23:00 and 00:00 are adjacent), which a scalar regression would
    mishandle, but a 24-dimensional distribution vector sidesteps that.
  - LINEAR REGRESSION (numpy least-squares) fits day-index vs. total daily
    competing USD volume over the lookback window, to report whether
    competition is intensifying or cooling off. Volume-over-time is a
    normal (non-circular) quantity, so a plain fit is valid here.

Both run locally in a few milliseconds — no external LLM call, no API key,
no billing dependency.

Also saves a heatmap PNG (hour-of-day x top-3-lenders, colored by each
lender's own % of daily volume in that hour — normalized per lender so the
#1 lender's raw dollar volume doesn't wash out everyone else's row) to
~/Desktop/competitor_top_lenders_heatmap.png. Best-effort: wrapped so a
save failure (e.g. no writable Desktop in CI) logs a warning instead of
failing the whole run — the email is the primary deliverable, the chart is
a local bonus.

A flat/continuous hourly posting shape (rather than one daily spike) is
ambiguous on its own — it could mean "many offers spread through the day"
or "fast continuous renewal with no real quiet window," and those two mean
opposite things for whether the recommended post-after hour actually buys
you a safe gap before that lender reprices. RENEWAL CADENCE ANALYSIS
resolves that ambiguity using data the heatmap doesn't: each offer's own
`duration`/`expiredAt`/`updatedAt` fields. For each top lender it groups
their offers into (creator, principal, collateral) slots, and for every
offer that closed via cancellation or expiry (not fulfillment — a fill is
a borrower taking the offer, not the lender repricing), looks for the next
offer created in that same slot to measure the actual renewal gap. A
lender with a short median duration and a short median renewal gap is
renewing continuously — no hour of the day is "after" them. A lender with
a long duration and a large renewal gap really does go quiet after their
daily wave, and posting after their recommended hour buys a real window.

Meant to run on a schedule (see
.github/workflows/competitor_timing_report.yml, every 2 days) — costs
only GitHub Actions minutes. Prior-run stats persist to
competitor_timing_state.json (gitignored — like lender_capital_state.json,
this is competitive-intelligence tracking, kept private and NOT committed
to the repo; the workflow persists it via actions/cache instead) so each
report can call out what changed since last time (recommended hour
drifting, a top lender's pattern shifting, a new name entering the top
ranks).

Usage:
  python competitor_timing_report.py                    # 14-day lookback, top 20 lenders, emails the report
  python competitor_timing_report.py --days-back 21
  python competitor_timing_report.py --top-lenders 15
  python competitor_timing_report.py --tz America/New_York
  python competitor_timing_report.py --coverage 0.9
  python competitor_timing_report.py --no-email          # console output only, skip email + state
  python competitor_timing_report.py --all-principals    # include non-USDC principal offers too
  python competitor_timing_report.py --heatmap-top 5     # chart more than the top 3 lenders
  python competitor_timing_report.py --no-chart          # skip the heatmap PNG entirely
  python competitor_timing_report.py --renewal-window-hours 48  # tighter "is this a renewal" cutoff

Required env vars:
  SMTP_FROM_EMAIL    - Gmail address to send from     (skipped with a warning if unset)
  SMTP_APP_PASSWORD  - Gmail App Password for that address
  NOTIFY_EMAIL_TO    - recipient address
  OFFERBOOK_WALLET   - used to exclude our own offers from "competitors"; a warning is
                        logged (not skipped) if unset, same as offer_posting_times.py
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import smtplib
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from statistics import median
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
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

KNOWN_SYMBOLS = _common.KNOWN_SYMBOLS

STATE_PATH = Path(__file__).parent / "competitor_timing_state.json"
DESKTOP_DIR = Path.home() / "Desktop"

SMTP_FROM_EMAIL = os.getenv("SMTP_FROM_EMAIL")
SMTP_APP_PASSWORD = os.getenv("SMTP_APP_PASSWORD")
NOTIFY_EMAIL_TO = os.getenv("NOTIFY_EMAIL_TO")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("competitor_timing_report")

SESSION = requests.Session()


def _parse_ts(t: str) -> datetime:
    return datetime.fromisoformat(t.replace("Z", "+00:00"))


def symbol_for(mint: str) -> str:
    return KNOWN_SYMBOLS.get(mint, mint[:6] + "…" if mint else "?")


# ---------------------------------------------------------------------------
# Fetching — market-wide (no collateral filter), mirrors offer_posting_times.py
# ---------------------------------------------------------------------------

def fetch_recent_offers(principal_mint: str | None, cutoff: datetime) -> list[dict]:
    """
    Fetch every lending offer (any status, any collateral) platform-wide,
    newest first, stopping once a page's offers are older than `cutoff` —
    sorted descending by createdAt, so once one offer is older than cutoff,
    everything after it is too.
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

def hourly_aggregates(hour_buckets: list[int], usd_amounts: list[float]) -> tuple[list[int], list[float]]:
    counts = [0] * 24
    usd_sums = [0.0] * 24
    for h, usd in zip(hour_buckets, usd_amounts):
        counts[h] += 1
        usd_sums[h] += usd
    return counts, usd_sums


def cumulative_pct(usd_sums: list[float]) -> list[float]:
    total = sum(usd_sums) or 1.0
    cum, running = [], 0.0
    for v in usd_sums:
        running += v
        cum.append(running / total * 100)
    return cum


def recommended_post_hour(cum_pct: list[float], coverage: float) -> int:
    threshold = coverage * 100
    for h, pct in enumerate(cum_pct):
        if pct >= threshold:
            return h
    return 23


def daily_volume_trend(daily_usd: dict) -> dict | None:
    """
    Linear regression (day-index vs. total daily competing USD volume, via
    numpy least-squares) over the lookback window — a coarse "is
    competition intensifying" signal. Volume-over-time is a normal
    (non-circular) quantity, unlike hour-of-day, so a plain fit is valid
    here without any circular-statistics handling. Returns None if there
    are too few distinct days for a trend to mean anything.
    """
    if len(daily_usd) < 3:
        return None

    import numpy as np

    ordered_dates = sorted(daily_usd)
    y = [daily_usd[d] for d in ordered_dates]
    x = list(range(len(y)))

    slope, intercept = np.polyfit(x, y, 1)
    mean_y = sum(y) / len(y)
    y_pred = [slope * xi + intercept for xi in x]
    ss_res = sum((yi - ypi) ** 2 for yi, ypi in zip(y, y_pred))
    ss_tot = sum((yi - mean_y) ** 2 for yi in y)
    r_squared = round(1 - ss_res / ss_tot, 3) if ss_tot > 0 else 0.0

    # "Flat" is scale-relative (2% of mean daily volume/day) rather than a
    # fixed dollar threshold — a $10/day drift is noise on a $500k/day
    # market and a real signal on a $200/day one.
    threshold = max(mean_y * 0.02, 1.0)
    direction = "increasing" if slope > threshold else "decreasing" if slope < -threshold else "flat"

    return {
        "slope_usd_per_day": round(float(slope), 2),
        "r_squared": r_squared,
        "direction": direction,
        "days_observed": len(y),
        "low_confidence": len(y) < 7 or r_squared < 0.3,
    }


def build_market_summary(
    offers: list[dict], tz: ZoneInfo, coverage: float,
) -> tuple[dict, dict[str, dict]]:
    """
    Returns (market_summary, per_lender_data). per_lender_data is keyed by
    creator address and used both to rank top lenders and to build each
    one's own hourly profile.
    """
    hours: list[int] = []
    usds: list[float] = []
    daily_usd: dict = defaultdict(float)
    per_lender: dict[str, dict] = defaultdict(lambda: {
        "total_usd": 0.0, "offer_count": 0, "hours": [], "usds": [],
        "collateral_usd": defaultdict(float),
    })

    for o in offers:
        created_utc = _parse_ts(o["createdAt"])
        local_dt = created_utc.astimezone(tz)
        local_hour = local_dt.hour
        usd = (o.get("metadata") or {}).get("principalAmountUsd") or 0.0
        creator = o.get("creator", "")
        cmint = o.get("collateralMint") or _mint_from_asset(o.get("collateral", {}))

        hours.append(local_hour)
        usds.append(usd)
        daily_usd[local_dt.date()] += usd

        ld = per_lender[creator]
        ld["total_usd"] += usd
        ld["offer_count"] += 1
        ld["hours"].append(local_hour)
        ld["usds"].append(usd)
        if cmint:
            ld["collateral_usd"][cmint] += usd

    counts, usd_sums = hourly_aggregates(hours, usds)
    cum_pct = cumulative_pct(usd_sums)
    rec_hour = recommended_post_hour(cum_pct, coverage)

    market_summary = {
        "total_offers": len(offers),
        "total_usd_volume": round(sum(usd_sums), 2),
        "hourly_counts": counts,
        "hourly_usd": [round(v, 2) for v in usd_sums],
        "cumulative_pct_of_volume": [round(v, 1) for v in cum_pct],
        "recommended_post_after_hour_local": f"{(rec_hour + 1) % 24:02d}:00",
        "coverage_at_recommended_hour_pct": round(cum_pct[rec_hour], 1),
        "volume_trend": daily_volume_trend(daily_usd),
    }
    return market_summary, per_lender


def top_lender_profiles(per_lender: dict[str, dict], top_n: int, coverage: float) -> list[dict]:
    ranked = sorted(per_lender.items(), key=lambda kv: -kv[1]["total_usd"])[:top_n]
    profiles = []
    for address, d in ranked:
        counts, usd_sums = hourly_aggregates(d["hours"], d["usds"])
        cum_pct = cumulative_pct(usd_sums)
        rec_hour = recommended_post_hour(cum_pct, coverage)
        top_collateral = sorted(d["collateral_usd"].items(), key=lambda kv: -kv[1])[:3]
        profiles.append({
            "address": address,
            "total_usd": round(d["total_usd"], 2),
            "offer_count": d["offer_count"],
            "hourly_counts": counts,
            "hourly_usd": [round(v, 2) for v in usd_sums],
            # Their single busiest hour (mode) — distinct from
            # own_recommended_post_after_hour_local below, which is a
            # cumulative-coverage threshold, not a peak.
            "own_peak_hour_local": f"{int(max(range(24), key=lambda h: usd_sums[h])):02d}:00",
            "own_recommended_post_after_hour_local": f"{(rec_hour + 1) % 24:02d}:00",
            "top_collateral": [
                {"symbol": symbol_for(m), "usd": round(u, 2)} for m, u in top_collateral
            ],
        })
    return profiles


# ---------------------------------------------------------------------------
# Renewal cadence — does a lender's flat/continuous hourly shape mean "many
# offers spread through the day" or "fast renewal with no real quiet window"?
# ---------------------------------------------------------------------------

def analyze_renewal_cadence(
    offers: list[dict], top_addresses: set[str], renewal_window_hours: float,
) -> dict[str, dict]:
    """
    For each top lender, groups their offers into (creator, principalMint,
    collateralMint) slots and, for every offer that closed via cancellation
    or expiry (fulfillment is excluded — a fill is a borrower taking the
    offer, not the lender repricing), finds the next offer created in that
    same slot to measure the actual gap between "this offer stopped being
    live" and "a replacement showed up." `expiredAt` is used as the close
    time for expired offers (the offer's own scheduled expiry) and
    `updatedAt` for cancelled ones (the best available proxy for when the
    lender pulled it, since the API doesn't expose a dedicated cancelledAt).

    This is what actually distinguishes the two readings a flat 24-hour
    posting shape is otherwise consistent with: a short median duration +
    short median renewal gap means the lender is renewing continuously, so
    there's no hour of the day that's genuinely "after" them. A long
    duration + large renewal gap means they really do go quiet after their
    daily wave.
    """
    by_slot: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
    for o in offers:
        creator = o.get("creator", "")
        if creator not in top_addresses:
            continue
        pmint = o.get("principalMint") or _mint_from_asset(o.get("principal", {}))
        cmint = o.get("collateralMint") or _mint_from_asset(o.get("collateral", {}))
        by_slot[(creator, pmint, cmint)].append(o)

    per_lender: dict[str, dict] = defaultdict(lambda: {
        "durations_hours": [],
        "actual_lifetimes_hours": [],
        "renewal_gaps_hours": [],
        "closed_count": 0,
        "cancelled_count": 0,
        "expired_count": 0,
        "renewed_count": 0,
    })

    for (creator, _pmint, _cmint), slot_offers in by_slot.items():
        slot_offers.sort(key=lambda o: _parse_ts(o["createdAt"]))
        ld = per_lender[creator]
        for o in slot_offers:
            dur = o.get("duration")
            if dur:
                ld["durations_hours"].append(dur / 3600)

        for i, o in enumerate(slot_offers):
            status = o.get("status")
            if status not in ("cancelled", "expired"):
                continue
            created = _parse_ts(o["createdAt"])
            end = _parse_ts(o["expiredAt"] if status == "expired" else o["updatedAt"])

            ld["closed_count"] += 1
            ld["cancelled_count" if status == "cancelled" else "expired_count"] += 1
            lifetime_h = (end - created).total_seconds() / 3600
            if lifetime_h >= 0:
                ld["actual_lifetimes_hours"].append(lifetime_h)

            nxt = next(
                (later for later in slot_offers[i + 1:] if _parse_ts(later["createdAt"]) > end),
                None,
            )
            if nxt:
                gap_h = (_parse_ts(nxt["createdAt"]) - end).total_seconds() / 3600
                if gap_h <= renewal_window_hours:
                    ld["renewal_gaps_hours"].append(gap_h)
                    ld["renewed_count"] += 1

    summaries: dict[str, dict] = {}
    for creator, d in per_lender.items():
        if not d["durations_hours"] and not d["closed_count"]:
            continue
        summaries[creator] = {
            "median_stated_duration_hours": round(median(d["durations_hours"]), 1) if d["durations_hours"] else None,
            "median_actual_lifetime_hours": round(median(d["actual_lifetimes_hours"]), 1) if d["actual_lifetimes_hours"] else None,
            "closed_count": d["closed_count"],
            "cancelled_count": d["cancelled_count"],
            "expired_count": d["expired_count"],
            "renewed_count": d["renewed_count"],
            "renewal_rate_pct": round(100 * d["renewed_count"] / d["closed_count"], 1) if d["closed_count"] else None,
            "median_renewal_gap_hours": round(median(d["renewal_gaps_hours"]), 2) if d["renewal_gaps_hours"] else None,
        }
    return summaries


def _renewal_interpretation(summary: dict) -> str:
    """
    Deliberately keyed off ACTUAL lifetime, not the offer's stated
    `duration` field — the stated value is often just the lender's ceiling
    (e.g. maxed at 168h/7 days) regardless of how briefly they actually
    let the offer run before cancelling, so using it here would mislabel a
    fast-cycling lender as "slow to renew" just because they set a long
    nominal duration.
    """
    gap = summary.get("median_renewal_gap_hours")
    rate = summary.get("renewal_rate_pct")
    lifetime = summary.get("median_actual_lifetime_hours")
    if gap is None or rate is None:
        return "not enough closed offers in this window to tell"
    if gap <= 3 and rate >= 50:
        return "fast/continuous renewal — no real quiet window after any single hour"
    if lifetime is not None and lifetime >= 24 and (gap >= 12 or rate < 30):
        return "long actual lifetime, slow to renew — a real quiet window exists after their wave"
    return "moderate renewal pace"


def _hour_archetype_label(avg_peak_hour: float) -> str:
    h = avg_peak_hour % 24
    if h < 6:
        return "late-night / dawn poster"
    if h < 12:
        return "morning poster"
    if h < 18:
        return "afternoon poster"
    return "evening / night poster"


# A lender with only a couple of offers in the window doesn't have a
# meaningful 24-hour "shape" — clustering on that is closer to fitting
# noise than behavior, so they're excluded from clustering specifically
# (they still appear in the ranked top-lenders list with their own
# post-after time; they just don't get an archetype label).
MIN_OFFERS_FOR_CLUSTERING = 3
MAX_CLUSTERS = 6


def cluster_lender_profiles(top_lenders: list[dict]) -> tuple[dict[str, dict], dict | None]:
    """
    K-means clustering (scikit-learn) on each sufficiently-sampled top
    lender's normalized 24-hour USD-share posting profile — groups
    competitors by the SHAPE of when their money shows up, not just how
    much. K is chosen automatically via silhouette score, scaling up to
    MAX_CLUSTERS as more candidates become available (a fixed small ceiling
    would waste the extra resolution once --top-lenders is raised). Returns
    ({}, None) if there aren't enough well-sampled top lenders to cluster
    meaningfully, or if no candidate K produced a stable (>=2 cluster)
    grouping.
    """
    candidates = [p for p in top_lenders if p["offer_count"] >= MIN_OFFERS_FOR_CLUSTERING]
    if len(candidates) < 4:
        return {}, None

    import numpy as np
    from sklearn.cluster import KMeans
    from sklearn.metrics import silhouette_score

    vectors = []
    for p in candidates:
        usd = np.array(p["hourly_usd"], dtype=float)
        total = usd.sum()
        vectors.append(usd / total if total > 0 else usd)
    X = np.array(vectors)

    max_k = min(MAX_CLUSTERS, len(candidates) - 1)
    best_k, best_score, best_labels = None, -1.0, None
    for k in range(2, max_k + 1):
        labels = KMeans(n_clusters=k, n_init=10, random_state=0).fit_predict(X)
        if len(set(labels)) < 2:
            continue
        score = silhouette_score(X, labels)
        if score > best_score:
            best_k, best_score, best_labels = k, score, labels

    if best_labels is None:
        return {}, None

    groups: dict[int, list[dict]] = defaultdict(list)
    group_vectors: dict[int, list] = defaultdict(list)
    for label, p, vec in zip(best_labels, candidates, X):
        groups[int(label)].append(p)
        group_vectors[int(label)].append(vec)

    cluster_map: dict[str, dict] = {}
    for label, members in groups.items():
        if len(members) == 1:
            # A singleton "cluster" isn't a shared archetype — it's one
            # lender whose shape didn't match anyone else's. Label it as
            # that, rather than an hour-bucket name implying a pattern
            # other lenders share.
            archetype = "distinct pattern (doesn't match other top lenders)"
        else:
            # Archetype hour comes from the CLUSTER CENTROID's peak, not
            # from averaging each member's own peak hour — hour-of-day is
            # circular (23:00 and 01:00 average to ~00:00, not ~12:00 as a
            # naive arithmetic mean of the hour numbers would give), and
            # the centroid is already a well-defined vector average with no
            # wraparound issue.
            centroid = np.mean(group_vectors[label], axis=0)
            archetype = _hour_archetype_label(int(np.argmax(centroid)))
        for m in members:
            cluster_map[m["address"]] = {
                "cluster": label,
                "cluster_size": len(members),
                "archetype": archetype,
            }

    meta = {"k": best_k, "silhouette_score": round(float(best_score), 3)}
    return cluster_map, meta


# ---------------------------------------------------------------------------
# State (delta since last report) — gitignored, private competitive intel
# ---------------------------------------------------------------------------

def load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return {}


def save_state(market_summary: dict, top_lenders: list[dict]) -> None:
    state = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "recommended_post_after_hour_local": market_summary["recommended_post_after_hour_local"],
        "top_lenders": {
            p["address"]: {
                "total_usd": p["total_usd"],
                "own_recommended_post_after_hour_local": p["own_recommended_post_after_hour_local"],
            }
            for p in top_lenders
        },
    }
    STATE_PATH.write_text(json.dumps(state, indent=2))


def build_delta(previous: dict, market_summary: dict, top_lenders: list[dict]) -> dict | None:
    if not previous:
        return None
    delta: dict = {
        "previous_report_at": previous.get("generated_at"),
        "recommended_hour_changed": (
            previous.get("recommended_post_after_hour_local") != market_summary["recommended_post_after_hour_local"]
        ),
        "previous_recommended_post_after_hour_local": previous.get("recommended_post_after_hour_local"),
    }
    prev_lenders = previous.get("top_lenders", {})
    current_addrs = {p["address"] for p in top_lenders}
    delta["new_entrants_to_top_ranks"] = sorted(current_addrs - set(prev_lenders))
    delta["dropped_out_of_top_ranks"] = sorted(set(prev_lenders) - current_addrs)
    shifts = []
    for p in top_lenders:
        prev = prev_lenders.get(p["address"])
        if prev and prev.get("own_recommended_post_after_hour_local") != p["own_recommended_post_after_hour_local"]:
            shifts.append({
                "address": p["address"],
                "from": prev["own_recommended_post_after_hour_local"],
                "to": p["own_recommended_post_after_hour_local"],
            })
    delta["lenders_whose_own_timing_shifted"] = shifts
    return delta


# ---------------------------------------------------------------------------
# Chart — hour-of-day x top lenders heatmap
# ---------------------------------------------------------------------------

def plot_top_lenders_heatmap(top_lenders: list[dict], market_summary: dict, tz_name: str,
                              output_path: str, top_n: int = 3) -> None:
    """
    Heatmap of hour-of-day (x) vs. the top N lenders (y), colored by each
    lender's own SHARE of their daily USD volume posted in that hour — each
    row normalized to sum to 100% of THAT lender's own volume, not the raw
    dollar amount. Without that normalization the #1 lender's much larger
    absolute volume would wash out every other row's color scale, defeating
    the point of comparing WHEN each of them posts. A dashed line marks the
    market-wide recommended post-after hour for direct context.
    """
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np

    subset = top_lenders[:top_n]
    if not subset:
        return

    matrix = []
    labels = []
    for p in subset:
        usd = np.array(p["hourly_usd"], dtype=float)
        total = usd.sum()
        share_pct = (usd / total * 100) if total > 0 else usd
        matrix.append(share_pct)
        labels.append(f"{p['address'][:6]}…{p['address'][-4:]}  (${p['total_usd']:,.0f})")
    matrix = np.array(matrix)

    fig, ax = plt.subplots(figsize=(14, 1.3 * len(subset) + 2))
    im = ax.imshow(matrix, aspect="auto", cmap="YlOrRd")

    ax.set_xticks(range(24))
    ax.set_xticklabels([f"{h:02d}" for h in range(24)], fontsize=8)
    ax.set_yticks(range(len(subset)))
    ax.set_yticklabels(labels, fontsize=9)
    ax.set_xlabel(f"Hour of day ({tz_name})")
    ax.set_title(f"Top {len(subset)} lenders — % of their own daily USD volume posted per hour")

    # market_summary's "post after HH:00" is already (threshold_hour + 1) —
    # the boundary sits between the threshold column and the next one.
    rec_hour = int(market_summary["recommended_post_after_hour_local"].split(":")[0])
    ax.axvline(rec_hour - 0.5, color="#2563eb", linestyle="--", linewidth=1.5,
               label=f"market recommended post-after ({market_summary['recommended_post_after_hour_local']})")
    ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.25 if len(subset) <= 3 else -0.15), fontsize=8)

    cbar = fig.colorbar(im, ax=ax, fraction=0.03, pad=0.02)
    cbar.set_label("% of that lender's own daily volume")

    plt.tight_layout()
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    plt.savefig(output_path, dpi=130, bbox_inches="tight")
    plt.close(fig)


# ---------------------------------------------------------------------------
# Report formatting — local only, no external API call
# ---------------------------------------------------------------------------

def build_local_report(market_summary: dict, top_lenders: list[dict], delta: dict | None,
                        cluster_map: dict[str, dict], cluster_meta: dict | None,
                        renewal_stats: dict[str, dict], renewal_window_hours: float,
                        days_back: int, tz_name: str) -> str:
    """Formats the already-computed stats — including the K-means archetype
    clusters and the linear volume trend — into a readable plain-text
    report. Everything here was computed locally; there's no external call
    to fail or bill for."""
    lines: list[str] = [
        f"Offerbook competitor posting-time report — {days_back}-day lookback, {tz_name}",
        "",
        f"RECOMMENDATION: post after {market_summary['recommended_post_after_hour_local']} local — "
        f"by then ~{market_summary['coverage_at_recommended_hour_pct']:.0f}% of typical competing "
        f"USD volume (${market_summary['total_usd_volume']:,.0f} total, {market_summary['total_offers']} "
        f"offers CREATED in this window, not necessarily still open) has historically posted.",
        "",
        "Legend: \"offers\" = offer-creation events in the window (most have since expired/cancelled/"
        "filled — this is activity volume, not live inventory). \"busiest hour\" = their single "
        "highest-USD hour. \"80%-by\" = the hour by which ~80% of their typical daily volume has "
        "historically posted (a cumulative threshold, NOT the same as busiest hour). \"[archetype]\" "
        "describes the whole cluster's shared shape, not this lender individually.",
    ]

    trend = market_summary.get("volume_trend")
    if trend:
        confidence = "low-confidence" if trend["low_confidence"] else "confidence"
        lines += [
            "",
            f"TREND ({confidence}; linear regression, R²={trend['r_squared']}, "
            f"{trend['days_observed']} days observed): competing volume is {trend['direction']} "
            f"at roughly ${abs(trend['slope_usd_per_day']):,.0f}/day.",
        ]

    lines += ["", f"TOP {len(top_lenders)} LENDERS BY USD VOLUME:"]
    if cluster_meta:
        quality = " — weak separation, treat these as loose groupings" if cluster_meta["silhouette_score"] < 0.4 else ""
        lines.append(f"  (clustered into {cluster_meta['k']} posting-time archetypes via k-means, "
                      f"silhouette={cluster_meta['silhouette_score']}{quality})")
    for p in top_lenders:
        info = cluster_map.get(p["address"])
        cluster_str = f"  [{info['archetype']}, n={info['cluster_size']}]" if info else ""
        lines.append(
            f"  {p['address']}  ${p['total_usd']:,.0f}  ({p['offer_count']} offers)  "
            f"busiest hour: {p['own_peak_hour_local']}  "
            f"80%-by: {p['own_recommended_post_after_hour_local']}{cluster_str}"
        )
    if not cluster_meta and len(top_lenders) >= 4:
        lines.append("  (clustering found no stable grouping — top lenders' posting shapes "
                      "don't separate cleanly into distinct archetypes this run)")

    lines += [
        "",
        f"RENEWAL CADENCE (per top lender, offers that closed via cancel/expiry only — "
        f"fulfilled offers excluded since a fill is demand-side, not repricing; a next post "
        f"in the same creator+principal+collateral slot within {renewal_window_hours:.0f}h "
        f"of closing counts as a renewal):",
        "  A flat/continuous hourly posting shape is ambiguous on its own — this is what "
        "resolves it: short duration + short renewal gap means the lender reprices "
        "continuously, so no hour of the day is genuinely \"after\" them; long duration + "
        "large renewal gap means posting after their recommended hour buys a real quiet window.",
    ]
    for p in top_lenders:
        rs = renewal_stats.get(p["address"])
        if not rs:
            lines.append(f"  {p['address']}  — no closed (cancelled/expired) offers in this window to analyze")
            continue
        dur = f"{rs['median_stated_duration_hours']:.1f}h" if rs["median_stated_duration_hours"] is not None else "n/a"
        lifetime = f"{rs['median_actual_lifetime_hours']:.1f}h" if rs["median_actual_lifetime_hours"] is not None else "n/a"
        gap = f"{rs['median_renewal_gap_hours']:.1f}h" if rs["median_renewal_gap_hours"] is not None else "n/a"
        rate = f"{rs['renewal_rate_pct']:.0f}%" if rs["renewal_rate_pct"] is not None else "n/a"
        lines.append(
            f"  {p['address']}  stated duration (median): {dur}  actual lifetime (median): {lifetime}  "
            f"closed: {rs['closed_count']} ({rs['cancelled_count']} cancelled, {rs['expired_count']} expired)  "
            f"renewed within {renewal_window_hours:.0f}h: {rate} (gap median {gap})  "
            f"→ {_renewal_interpretation(rs)}"
        )

    if delta:
        lines += ["", "CHANGES SINCE LAST REPORT:"]
        changed = False
        if delta["recommended_hour_changed"]:
            lines.append(
                f"  Recommended post-after hour moved from "
                f"{delta['previous_recommended_post_after_hour_local']} to "
                f"{market_summary['recommended_post_after_hour_local']}."
            )
            changed = True
        if delta["new_entrants_to_top_ranks"]:
            lines.append(f"  New to top ranks: {', '.join(delta['new_entrants_to_top_ranks'])}")
            changed = True
        if delta["dropped_out_of_top_ranks"]:
            lines.append(f"  Dropped out of top ranks: {', '.join(delta['dropped_out_of_top_ranks'])}")
            changed = True
        for shift in delta["lenders_whose_own_timing_shifted"]:
            lines.append(f"  {shift['address']} shifted own post-after: {shift['from']} → {shift['to']}")
            changed = True
        if not changed:
            lines.append("  No material changes since last report.")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------

def send_email(subject: str, body: str, image_path: str | None = None) -> None:
    """Sends `body` as the email text; if `image_path` exists, attaches it
    (e.g. the heatmap PNG) as a regular file attachment — most mail clients
    show a PNG attachment inline in the preview pane, so this is enough to
    "see the chart" without switching the whole email to HTML."""
    if not (SMTP_FROM_EMAIL and SMTP_APP_PASSWORD and NOTIFY_EMAIL_TO):
        log.warning("SMTP env vars not set — skipping email: %s", subject)
        return

    if image_path and Path(image_path).exists():
        msg = MIMEMultipart()
        msg.attach(MIMEText(body))
        with open(image_path, "rb") as f:
            image = MIMEImage(f.read())
        image.add_header("Content-Disposition", "attachment", filename=Path(image_path).name)
        msg.attach(image)
    else:
        msg = MIMEText(body)

    msg["Subject"] = subject
    msg["From"] = SMTP_FROM_EMAIL
    msg["To"] = NOTIFY_EMAIL_TO
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(SMTP_FROM_EMAIL, SMTP_APP_PASSWORD)
        server.send_message(msg)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def resolve_tz(tz_arg: str | None) -> ZoneInfo:
    if tz_arg:
        try:
            return ZoneInfo(tz_arg)
        except ZoneInfoNotFoundError:
            log.error("Unknown timezone %r — use an IANA name, e.g. 'UTC' or 'America/New_York'.", tz_arg)
            sys.exit(1)
    return datetime.now().astimezone().tzinfo  # type: ignore[return-value]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--days-back", type=int, default=14, help="Rolling lookback window in days (default 14).")
    parser.add_argument("--top-lenders", type=int, default=20, help="How many top lenders (by USD volume) to profile (default 20).")
    parser.add_argument("--coverage", type=float, default=0.80, help="Fraction of volume that should post before the recommended hour (default 0.80).")
    parser.add_argument("--tz", default=None, help="IANA timezone for hour bucketing (default: this machine's local tz).")
    parser.add_argument("--all-principals", action="store_true", help="Include every principal token, not just USDC.")
    parser.add_argument("--include-self", action="store_true", help="Include our own offers instead of excluding them.")
    parser.add_argument("--wallet", default=None, help="Wallet pubkey to exclude as 'self' (default: OFFERBOOK_WALLET env var).")
    parser.add_argument("--no-email", action="store_true", help="Console output only — skip email and state persistence.")
    parser.add_argument("--heatmap-top", type=int, default=3, help="How many top lenders to chart in the heatmap (default 3).")
    parser.add_argument("--no-chart", action="store_true", help="Skip saving the heatmap PNG.")
    parser.add_argument("--chart-output", default=None,
                         help="Heatmap PNG output path (default ~/Desktop/competitor_top_lenders_heatmap.png).")
    parser.add_argument("--renewal-window-hours", type=float, default=72.0,
                         help="Max hours after a closed offer to still count the next same-slot post as a renewal (default 72).")
    args = parser.parse_args()

    if not (0 < args.coverage <= 1):
        log.error("--coverage must be between 0 (exclusive) and 1 (inclusive).")
        sys.exit(1)

    tz = resolve_tz(args.tz)
    tz_name = args.tz or str(tz)

    principal_mint = None if args.all_principals else USDC_MINT
    wallet = args.wallet or os.getenv("OFFERBOOK_WALLET", "")
    if not args.include_self and not wallet:
        log.warning("No OFFERBOOK_WALLET/--wallet set — self-exclusion has nothing to exclude; "
                    "our own offers (if any) will be included as a 'competitor'.")

    cutoff = datetime.now(timezone.utc) - timedelta(days=args.days_back)
    log.info("Fetching platform-wide lending offers since %s …", cutoff.date())
    offers = fetch_recent_offers(principal_mint, cutoff)
    log.info("  → %d offer(s) fetched", len(offers))

    if not args.include_self and wallet:
        before = len(offers)
        offers = [o for o in offers if o.get("creator") != wallet]
        log.info("  → excluded %d of our own offer(s)", before - len(offers))

    if not offers:
        log.error("No offers found in this window — try a wider --days-back.")
        sys.exit(1)

    market_summary, per_lender = build_market_summary(offers, tz, args.coverage)
    top_lenders = top_lender_profiles(per_lender, args.top_lenders, args.coverage)

    log.info("Market recommendation: post after %s local (%.1f%% coverage)",
              market_summary["recommended_post_after_hour_local"],
              market_summary["coverage_at_recommended_hour_pct"])
    log.info("Top %d lender(s) by USD volume:", len(top_lenders))
    for p in top_lenders:
        log.info("  %s  $%.2f  (%d offers)  busiest: %s  80%%-by: %s",
                  p["address"], p["total_usd"], p["offer_count"],
                  p["own_peak_hour_local"], p["own_recommended_post_after_hour_local"])

    chart_path = None
    if not args.no_chart:
        candidate_path = args.chart_output or str(DESKTOP_DIR / "competitor_top_lenders_heatmap.png")
        try:
            plot_top_lenders_heatmap(top_lenders, market_summary, tz_name, candidate_path, args.heatmap_top)
            log.info("Saved top-%d lenders heatmap to %s", args.heatmap_top, candidate_path)
            chart_path = candidate_path
        except Exception as exc:
            log.warning("Could not save heatmap chart (non-fatal, continuing): %s", exc)

    previous_state = load_state() if not args.no_email else {}
    delta = build_delta(previous_state, market_summary, top_lenders)

    log.info("Clustering top lenders by posting-time shape (k-means) and fitting the volume trend …")
    cluster_map, cluster_meta = cluster_lender_profiles(top_lenders)
    if cluster_meta:
        log.info("  → k=%d clusters, silhouette=%.3f", cluster_meta["k"], cluster_meta["silhouette_score"])
    else:
        log.info("  → no stable clustering found")

    log.info("Correlating closed offers with renewals (renewal window: %.0fh) …", args.renewal_window_hours)
    renewal_stats = analyze_renewal_cadence(
        offers, {p["address"] for p in top_lenders}, args.renewal_window_hours,
    )

    report_text = build_local_report(market_summary, top_lenders, delta, cluster_map, cluster_meta,
                                      renewal_stats, args.renewal_window_hours, args.days_back, tz_name)

    log.info("")
    log.info("=" * 78)
    log.info("DISTILLED REPORT")
    log.info("=" * 78)
    for line in report_text.splitlines():
        log.info(line)
    log.info("=" * 78)

    if not args.no_email:
        subject = f"Offerbook competitor timing report — post after {market_summary['recommended_post_after_hour_local']} local"
        send_email(subject, report_text, chart_path)
        save_state(market_summary, top_lenders)
    else:
        log.info("--no-email set — skipped email and state persistence.")


if __name__ == "__main__":
    main()
