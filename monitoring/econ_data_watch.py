"""
US Economic Data Watch — Instant Release Alerts + Daily Digest
==================================================================
Two things, one script, run on a schedule:

  1. INSTANT ALERT: the moment any tracked metric prints a new reading
     (PCE, CPI, jobs report, GDP, etc.), emails the new value, the prior
     value, the change, and a short directional read on what it might mean
     — e.g. "today is PCE" -> as soon as the Fed's preferred inflation
     gauge updates, you get an email with the number, not a reminder to
     go check it yourself.

  2. DAILY DIGEST: once per UTC calendar day, emails a compact table of
     every tracked metric's most recent known reading (whether or not it
     updated today) — so you always have today's starting picture, not
     just reactions to whatever happened to release that day.

Data comes from the St. Louis Fed's FRED API (api.stlouisfed.org) — free,
official, no scraping. Requires a free FRED_API_KEY (sign up at
https://fred.stlouisfed.org/docs/api/api_key.html — this script can't get
one for you). US-only by design: FRED covers US releases well; most
non-US releases (ECB, China PMI, etc.) aren't on FRED and would need a
separate, less consistent source per region.

Tracked metrics (see TRACKED_METRICS below to add/remove):
  - PCE Price Index, Core PCE (YoY %)       — the Fed's preferred gauge
  - CPI, Core CPI (YoY %)
  - Nonfarm Payrolls (monthly change, '000s), Unemployment Rate (%)
  - Real GDP (annualized QoQ growth %)
  - PPI, All Commodities (YoY %) — FRED's longest-standing PPI series;
    NOT exactly the "Final Demand" headline number financial media quotes,
    noted as a caveat rather than guessing at an unverified series ID
  - Retail Sales (MoM %)
  - Fed Funds Rate target, upper bound (%) — alerts on a VALUE change
    only (an FOMC decision), not on every daily republish of an unchanged
    rate between meetings, unlike every other metric here which alerts on
    a new observation DATE regardless of whether the value moved

NOT covered: ISM Manufacturing/Services PMI. ISM licenses that data and it
isn't published on FRED — tracking it would need a different (likely
paid) source, so it's left out rather than silently wrong.

Detection has no separate "release calendar" to get out of sync with
reality: each run just asks FRED for each series' latest (and prior)
observation and compares the latest's date (or value, for the Fed funds
rate) against what was seen last run. Whatever actually updated IS what
released — no hardcoded release-ID/date table that could drift.

Meant to run on a schedule (see ../.github/workflows/econ_data_watch.yml,
every 15 min) — same cadence as borrow_offer_watch.py. "Instant" is bounded
by both that interval and FRED's own latency in mirroring BEA/BLS releases
(typically minutes), not true real-time.

Usage:
  python econ_data_watch.py                  # normal run — alert on new prints, digest once/day
  python econ_data_watch.py --force-digest    # also send the daily digest even if already sent today
  python econ_data_watch.py --no-email        # console output only, skip email + state

Required env vars (set as GitHub Actions secrets — never committed):
  FRED_API_KEY       - free key from https://fred.stlouisfed.org/docs/api/api_key.html
  SMTP_FROM_EMAIL    - Gmail address to send from (same as borrow_offer_watch.py)
  SMTP_APP_PASSWORD  - Gmail App Password for that address
  NOTIFY_EMAIL_TO    - recipient address
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import smtplib
import sys
from datetime import datetime, timezone
from email.mime.text import MIMEText
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

FRED_API_BASE = "https://api.stlouisfed.org/fred"
FRED_API_KEY = os.getenv("FRED_API_KEY", "")

SMTP_FROM_EMAIL = os.getenv("SMTP_FROM_EMAIL")
SMTP_APP_PASSWORD = os.getenv("SMTP_APP_PASSWORD")
NOTIFY_EMAIL_TO = os.getenv("NOTIFY_EMAIL_TO")

STATE_PATH = Path(__file__).parent / "econ_data_watch_state.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("econ_data_watch")

SESSION = requests.Session()

# ---------------------------------------------------------------------------
# Tracked metrics
# ---------------------------------------------------------------------------
# trigger="new_observation": alert whenever FRED's latest observation DATE
#   for this series advances past what was seen last run (a new monthly/
#   quarterly print landed) — the normal case for everything except the
#   Fed funds rate below.
# trigger="value_change": alert only when the latest VALUE differs from
#   what was seen last run, regardless of date — DFEDTARU republishes the
#   same unchanged rate every single business day between FOMC meetings,
#   so "new date" would falsely fire daily; only an actual rate move (an
#   FOMC decision) is newsworthy here.
# "good_direction": which way is conventionally read as economically
# "strong"/"hawkish" for the interpretation heuristic below — "up" for
# inflation/jobs/growth/rates (higher reading = hotter economy, more
# likely Fed stays restrictive), "down" for unemployment (higher = weaker
# labor market).
TRACKED_METRICS: list[dict] = [
    {"series_id": "PCEPILFE", "label": "Core PCE Price Index (YoY)", "units": "pc1", "trigger": "new_observation", "good_direction": "up", "pct": True},
    {"series_id": "PCEPI", "label": "PCE Price Index (YoY)", "units": "pc1", "trigger": "new_observation", "good_direction": "up", "pct": True},
    {"series_id": "CPILFESL", "label": "Core CPI (YoY)", "units": "pc1", "trigger": "new_observation", "good_direction": "up", "pct": True},
    {"series_id": "CPIAUCSL", "label": "CPI (YoY)", "units": "pc1", "trigger": "new_observation", "good_direction": "up", "pct": True},
    {"series_id": "PAYEMS", "label": "Nonfarm Payrolls (monthly change, '000s)", "units": "chg", "trigger": "new_observation", "good_direction": "up", "pct": False},
    {"series_id": "UNRATE", "label": "Unemployment Rate", "units": "lin", "trigger": "new_observation", "good_direction": "down", "pct": True},
    {"series_id": "GDPC1", "label": "Real GDP (annualized QoQ growth)", "units": "pca", "trigger": "new_observation", "good_direction": "up", "pct": True},
    {"series_id": "PPIACO", "label": "PPI, All Commodities (YoY) — not the 'Final Demand' headline", "units": "pc1", "trigger": "new_observation", "good_direction": "up", "pct": True},
    {"series_id": "RSAFS", "label": "Retail Sales (MoM)", "units": "pch", "trigger": "new_observation", "good_direction": "up", "pct": True},
    {"series_id": "DFEDTARU", "label": "Fed Funds Rate target (upper bound)", "units": "lin", "trigger": "value_change", "good_direction": "up", "pct": True},
]


def send_email(subject: str, body: str) -> None:
    """Identical pattern to borrow_offer_watch.py's send_email — same SMTP
    secrets, so no new email infrastructure for this script specifically."""
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


def load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return {"series": {}, "last_digest_date": None}


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=2) + "\n")


FRED_RAW_FETCH_LIMIT = 6  # see docstring below — padding against null placeholder rows


def fetch_latest_two(series_id: str, units: str) -> list[dict]:
    """Up to [latest, previous] valid observations (newest first) for
    `series_id`, already transformed by FRED itself (units=pc1/pch/pca/
    chg/lin — see TRACKED_METRICS) so this script never reimplements YoY/
    MoM/annualized math by hand. Fetches both the latest AND prior point
    in one call so the prior-value comparison doesn't depend on local
    state having seen it before (works correctly on a cold/empty state
    file too). Returns [] on any fetch failure or if FRED hasn't got 2
    valid points at all yet.

    Requests FRED_RAW_FETCH_LIMIT (6) raw rows, not just 2, before
    filtering out nulls — confirmed live that FRED can carry a dated
    placeholder row with value "." for a period whose data hasn't
    actually been released yet (e.g. CPILFESL/CPIAUCSL showed an empty
    "2026-10-01" row alongside September's real one), which would
    otherwise leave fewer than 2 valid points after filtering even though
    plenty of real history exists just one row further back."""
    try:
        resp = SESSION.get(
            f"{FRED_API_BASE}/series/observations",
            params={
                "series_id": series_id, "api_key": FRED_API_KEY, "file_type": "json",
                "units": units, "sort_order": "desc", "limit": FRED_RAW_FETCH_LIMIT,
            },
            timeout=20,
        )
        resp.raise_for_status()
        obs = resp.json().get("observations", [])
        return [o for o in obs if o.get("value") not in (None, ".")][:2]
    except Exception as exc:
        log.warning("FRED fetch failed for %s: %s", series_id, exc)
        return []


def interpret(metric: dict, latest: float, prev: float) -> str:
    """One-line, clearly-heuristic (not investment advice) directional
    read: compares latest vs prior and whether that direction is
    conventionally "hotter economy / more hawkish Fed" or the reverse,
    per the metric's own good_direction (see TRACKED_METRICS)."""
    if latest == prev:
        return "Unchanged from the prior reading."
    moved_up = latest > prev
    hot = moved_up if metric["good_direction"] == "up" else not moved_up
    if metric["series_id"] in ("PCEPI", "PCEPILFE", "CPIAUCSL", "CPILFESL", "PPIACO"):
        return (
            "Inflation running HOTTER than the prior reading — modestly hawkish "
            "(less room for the Fed to cut)." if hot else
            "Inflation running COOLER than the prior reading — modestly dovish "
            "(more room for the Fed to cut)."
        )
    if metric["series_id"] == "PAYEMS":
        return (
            "Job growth ACCELERATED vs. the prior month — strong labor market, "
            "modestly hawkish." if hot else
            "Job growth SLOWED vs. the prior month — softer labor market, modestly dovish."
        )
    if metric["series_id"] == "UNRATE":
        return (
            "Unemployment ROSE — a weakening labor market, modestly dovish." if hot else
            "Unemployment FELL — a tightening labor market, modestly hawkish."
        )
    if metric["series_id"] == "GDPC1":
        return (
            "Growth ACCELERATED vs. the prior quarter — a stronger economy, "
            "modestly hawkish." if hot else
            "Growth SLOWED vs. the prior quarter — a weaker economy, modestly dovish."
        )
    if metric["series_id"] == "RSAFS":
        return (
            "Consumer spending ACCELERATED — resilient demand, modestly hawkish." if hot else
            "Consumer spending SLOWED — softening demand, modestly dovish."
        )
    if metric["series_id"] == "DFEDTARU":
        return (
            "FOMC RAISED the target rate." if moved_up else
            "FOMC CUT the target rate."
        )
    return "Moved {}.".format("up" if moved_up else "down")


def fmt_value(metric: dict, value: float) -> str:
    """A forced +/- sign makes sense for a change/rate transform (YoY%,
    MoM%, annualized%, absolute chg) but not for a plain level (units=
    "lin", e.g. UNRATE/DFEDTARU are just "4.10%", not "a positive 4.10%
    move") — signed only for the former."""
    signed = metric["units"] != "lin"
    if metric["pct"]:
        return f"{value:+.2f}%" if signed else f"{value:.2f}%"
    return f"{value:+,.1f}" if signed else f"{value:,.1f}"


def check_metric(metric: dict, obs: list[dict], state: dict) -> dict | None:
    """Given `metric`'s already-fetched latest-two observations (see
    main() — fetched ONCE per metric and shared with the digest/display
    path, not re-fetched here; an earlier version called
    fetch_latest_two() a second time inside this function, which silently
    dropped a series from state entirely whenever that second, redundant
    call happened to fail transiently while the first succeeded), checks
    whether its trigger condition fires (see TRACKED_METRICS docstring)
    and, if so, returns an alert dict and updates
    `state["series"][series_id]` in place. Returns None (no alert)
    otherwise — including when `obs` is incomplete (fetch failure
    upstream) or on a cold-start first run, where there's nothing to
    compare against yet."""
    series_id = metric["series_id"]
    if len(obs) < 2:
        log.warning("  %s: incomplete FRED data this run — not updating state for it.", series_id)
        return None
    latest, prev = obs[0], obs[1]
    latest_date, latest_value = latest["date"], float(latest["value"])
    prev_value = float(prev["value"])

    prior_state = state["series"].get(series_id, {})
    state["series"][series_id] = {"last_date": latest_date, "last_value": latest_value}

    if not prior_state:
        return None  # cold start — nothing to compare against, don't alert on the very first run

    if metric["trigger"] == "value_change":
        fired = latest_value != prior_state.get("last_value")
    else:
        fired = latest_date != prior_state.get("last_date")
    if not fired:
        return None

    return {
        "metric": metric, "date": latest_date,
        "latest_value": latest_value, "prev_value": prev_value,
    }


def format_alert(alert: dict) -> str:
    metric, latest, prev = alert["metric"], alert["latest_value"], alert["prev_value"]
    return (
        f"{metric['label']}: {fmt_value(metric, latest)}  (prior: {fmt_value(metric, prev)})\n"
        f"  Release date: {alert['date']}\n"
        f"  {interpret(metric, latest, prev)}"
    )


def build_digest(all_latest: list[tuple[dict, dict | None]]) -> str:
    lines = [f"Daily economic data digest — {datetime.now(timezone.utc).date().isoformat()} UTC", ""]
    for metric, obs in all_latest:
        if obs is None:
            lines.append(f"{metric['label']}: n/a (fetch failed)")
            continue
        lines.append(f"{metric['label']}: {fmt_value(metric, float(obs['value']))}  (as of {obs['date']})")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--force-digest", action="store_true", help="Send the daily digest even if one's already gone out today.")
    parser.add_argument("--no-email", action="store_true", help="Console output only — skip email and state persistence.")
    args = parser.parse_args()

    if not FRED_API_KEY:
        log.error(
            "FRED_API_KEY not set — sign up free at "
            "https://fred.stlouisfed.org/docs/api/api_key.html and set it in .env. Aborting."
        )
        sys.exit(1)

    state = load_state()
    alerts: list[dict] = []
    all_latest: list[tuple[dict, dict | None]] = []

    for metric in TRACKED_METRICS:
        log.info("Checking %s …", metric["label"])
        obs = fetch_latest_two(metric["series_id"], metric["units"])
        all_latest.append((metric, obs[0] if obs else None))
        alert = check_metric(metric, obs, state)
        if alert:
            alerts.append(alert)
            log.info("  -> NEW: %s", format_alert(alert).splitlines()[0])

    if alerts:
        subject = (
            f"Econ data: {alerts[0]['metric']['label']} just printed"
            if len(alerts) == 1
            else f"Econ data: {len(alerts)} new releases"
        )
        body = "\n\n".join(format_alert(a) for a in alerts)
        log.info("Sending instant alert for %d new release(s) …", len(alerts))
        if not args.no_email:
            send_email(subject, body)
    else:
        log.info("No new releases since last run.")

    today = datetime.now(timezone.utc).date().isoformat()
    if args.force_digest or state.get("last_digest_date") != today:
        digest = build_digest(all_latest)
        log.info("Sending daily digest …")
        if not args.no_email:
            send_email(f"Econ data — daily digest ({today})", digest)
        state["last_digest_date"] = today
    else:
        log.info("Daily digest already sent today — skipping (use --force-digest to resend).")

    if not args.no_email:
        save_state(state)

    log.info("")
    log.info("=" * 90)
    log.info("LATEST READINGS")
    log.info("=" * 90)
    for metric, obs in all_latest:
        if obs is None:
            log.info("%-55s n/a", metric["label"])
        else:
            log.info("%-55s %-10s  (as of %s)", metric["label"], fmt_value(metric, float(obs["value"])), obs["date"])


if __name__ == "__main__":
    main()
