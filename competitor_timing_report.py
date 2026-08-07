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
posting rhythm and each top lender's individual posting hours. That
aggregated (not raw per-offer) data is then handed to Moonshot's Kimi model
to distill into a short, actionable recommendation — this is a single
summarization API call, not a multi-turn autonomous agent: the task is
"read these stats and write the takeaway," which doesn't need tool use or
an agentic loop. Uses Moonshot's OpenAI-compatible chat completions API
(https://api.moonshot.ai/v1) via the `openai` SDK pointed at that base URL.

Meant to run on a schedule (see
.github/workflows/competitor_timing_report.yml, every 2 days) — costs
GitHub Actions minutes plus one Kimi API call per run. Prior-run stats
persist to competitor_timing_state.json (gitignored — like
lender_capital_state.json, this is competitive-intelligence tracking, kept
private and NOT committed to the repo; the workflow persists it via
actions/cache instead) so each report can call out what changed since last
time (recommended hour drifting, a top lender's pattern shifting, a new
name entering the top ranks).

Usage:
  python competitor_timing_report.py                    # 14-day lookback, top 10 lenders, emails the report
  python competitor_timing_report.py --days-back 21
  python competitor_timing_report.py --top-lenders 15
  python competitor_timing_report.py --tz America/New_York
  python competitor_timing_report.py --coverage 0.9
  python competitor_timing_report.py --no-email          # console output only, skip email + state
  python competitor_timing_report.py --all-principals    # include non-USDC principal offers too

Required env vars:
  MOONSHOT_API_KEY   - Kimi API key from platform.moonshot.ai, for the distillation call
  MOONSHOT_BASE_URL  - optional override (default: https://api.moonshot.ai/v1 — the
                        international platform; use https://api.moonshot.cn/v1 only if
                        signed up on the China platform instead)
  KIMI_MODEL         - optional override (default: kimi-k2-0711-preview — check
                        platform.moonshot.ai's model list if this 404s, model IDs shift)
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

SMTP_FROM_EMAIL = os.getenv("SMTP_FROM_EMAIL")
SMTP_APP_PASSWORD = os.getenv("SMTP_APP_PASSWORD")
NOTIFY_EMAIL_TO = os.getenv("NOTIFY_EMAIL_TO")

MOONSHOT_API_KEY = os.getenv("MOONSHOT_API_KEY")
MOONSHOT_BASE_URL = os.getenv("MOONSHOT_BASE_URL", "https://api.moonshot.ai/v1")
KIMI_MODEL = os.getenv("KIMI_MODEL", "kimi-k2-0711-preview")

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
    per_lender: dict[str, dict] = defaultdict(lambda: {
        "total_usd": 0.0, "offer_count": 0, "hours": [], "usds": [],
        "collateral_usd": defaultdict(float),
    })

    for o in offers:
        created_utc = _parse_ts(o["createdAt"])
        local_hour = created_utc.astimezone(tz).hour
        usd = (o.get("metadata") or {}).get("principalAmountUsd") or 0.0
        creator = o.get("creator", "")
        cmint = o.get("collateralMint") or _mint_from_asset(o.get("collateral", {}))

        hours.append(local_hour)
        usds.append(usd)

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
            "own_recommended_post_after_hour_local": f"{(rec_hour + 1) % 24:02d}:00",
            "top_collateral": [
                {"symbol": symbol_for(m), "usd": round(u, 2)} for m, u in top_collateral
            ],
        })
    return profiles


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
# Distillation — single Kimi (Moonshot) API call, not a multi-turn agent
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """\
You are a market-timing analyst for a competitive USDC lending bot on the \
Offerbook protocol (Solana). The bot posts ALL of its lending offers in one \
batch run rather than trickling them out over the day, so it wants to run \
AFTER most competing capital across the whole market has already posted for \
the period — that way its undercut pricing benchmarks against fresh, \
representative competitor data instead of yesterday's stragglers.

You will be given aggregated (not raw per-offer) platform-wide statistics: \
an hourly posting-time histogram and cumulative-volume curve for the whole \
market, individual profiles for the top lenders by USD volume, and — when \
available — a delta versus the previous report.

Write a short, distilled report a trader can act on before the next run. \
Do not restate the raw numbers back verbatim — synthesize them into a \
recommendation. Structure:
1. The single recommended post-after time (local), one sentence on why.
2. Notable competitor timing behavior — who posts latest/earliest among the \
top lenders, and whether any of them alone would justify posting later.
3. What changed since the last report, if delta data is present — otherwise \
omit this section entirely (don't say "no delta data").
4. Any data-quality caveats worth flagging (thin sample, one dominant \
outlier skewing the average, etc.) — omit if none.

Keep it tight: a few short paragraphs or a tight bullet list. No preamble, \
no restating the task back."""


def distill_report(market_summary: dict, top_lenders: list[dict], delta: dict | None,
                    days_back: int, tz_name: str, coverage: float) -> str:
    """Calls Moonshot's Kimi model via its OpenAI-compatible chat completions
    API (https://platform.moonshot.ai/docs) — the `openai` SDK works against
    any OpenAI-compatible endpoint by pointing base_url at it."""
    from openai import OpenAI

    if not MOONSHOT_API_KEY:
        raise RuntimeError("MOONSHOT_API_KEY not set")

    client = OpenAI(api_key=MOONSHOT_API_KEY, base_url=MOONSHOT_BASE_URL)

    payload = {
        "lookback_days": days_back,
        "timezone": tz_name,
        "coverage_threshold_pct": coverage * 100,
        "market": market_summary,
        "top_lenders": top_lenders,
        "delta_since_last_report": delta,
    }

    response = client.chat.completions.create(
        model=KIMI_MODEL,
        max_tokens=4096,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(payload, indent=2)},
        ],
    )

    choice = response.choices[0]
    if choice.finish_reason == "content_filter":
        raise RuntimeError("Kimi declined to generate the report (finish_reason=content_filter)")

    return choice.message.content or ""


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------

def send_email(subject: str, body: str) -> None:
    if not (SMTP_FROM_EMAIL and SMTP_APP_PASSWORD and NOTIFY_EMAIL_TO):
        log.warning("SMTP env vars not set — skipping email: %s", subject)
        return
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
    parser.add_argument("--top-lenders", type=int, default=10, help="How many top lenders (by USD volume) to profile (default 10).")
    parser.add_argument("--coverage", type=float, default=0.80, help="Fraction of volume that should post before the recommended hour (default 0.80).")
    parser.add_argument("--tz", default=None, help="IANA timezone for hour bucketing (default: this machine's local tz).")
    parser.add_argument("--all-principals", action="store_true", help="Include every principal token, not just USDC.")
    parser.add_argument("--include-self", action="store_true", help="Include our own offers instead of excluding them.")
    parser.add_argument("--wallet", default=None, help="Wallet pubkey to exclude as 'self' (default: OFFERBOOK_WALLET env var).")
    parser.add_argument("--no-email", action="store_true", help="Console output only — skip email and state persistence.")
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
        log.info("  %s  $%.2f  (%d offers)  own post-after: %s",
                  p["address"], p["total_usd"], p["offer_count"],
                  p["own_recommended_post_after_hour_local"])

    previous_state = load_state() if not args.no_email else {}
    delta = build_delta(previous_state, market_summary, top_lenders)

    log.info("Calling Kimi (%s) to distill the report …", KIMI_MODEL)
    try:
        report_text = distill_report(market_summary, top_lenders, delta, args.days_back, tz_name, args.coverage)
    except Exception as exc:
        log.error("Distillation call failed: %s", exc)
        sys.exit(1)

    log.info("")
    log.info("=" * 78)
    log.info("DISTILLED REPORT")
    log.info("=" * 78)
    for line in report_text.splitlines():
        log.info(line)
    log.info("=" * 78)

    if not args.no_email:
        subject = f"Offerbook competitor timing report — post after {market_summary['recommended_post_after_hour_local']} local"
        send_email(subject, report_text)
        save_state(market_summary, top_lenders)
    else:
        log.info("--no-email set — skipped email and state persistence.")


if __name__ == "__main__":
    main()
