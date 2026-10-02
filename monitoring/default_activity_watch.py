"""
Offerbook Default / Late-Repayment Digest
=============================================
Emails a digest of every loan that DEFAULTED or was REPAID LATE (closed
after its expiredAt) in the trailing window (default 48h), platform-wide —
every lender, not just our own wallets. Answers "what bad/late outcomes
happened recently on this platform", as a periodic push notification rather
than something you have to remember to go check.

This is a different question from defaulter_watch.py's collateral-coverage
watchlist, which only exists to answer "which past defaulters/late-payers
are worth targeting as a borrower" (filtered to collateral-covered cases,
scoped to building a reusable ledger of good counterparties). This script
has no such filter — "late" here means ANY repaid loan closed after its
expiredAt, regardless of whether collateral covered the principal, and
every default counts, not just collateral-covered ones. Two different tools
answering two different questions; defaulter_watch.py's own watchlist/
defaulter_config.yaml state is untouched by this script.

Each event is reported exactly once: state persists to
default_activity_watch_state.json (committed back to the repo by the
workflow — public on-chain loan data, no privacy concern, same treatment as
loan_watch_state.json) so the same loan is never re-reported across the 4x
overlap a 48h lookback run every 12h naturally creates. State entries are
pruned once they've aged out of the lookback window plus a safety margin,
so the file doesn't grow forever.

Meant to run on a schedule (see ../.github/workflows/default_activity_watch.yml,
every 12h) — costs nothing beyond GitHub Actions' free minutes. Only emails
when there's at least one new event since the last run — a quiet 12h window
sends nothing rather than an empty "nothing happened" email every run.

Usage:
  python default_activity_watch.py                  # normal run — email if anything new
  python default_activity_watch.py --hours-back 72   # override the lookback window
  python default_activity_watch.py --no-email        # console output only, skip email + state

Required env vars for email (set as GitHub Actions secrets — never committed):
  SMTP_FROM_EMAIL    - Gmail address to send from
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
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

# Shared modules live in ../lib — see README's repo-layout note.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))

import offerbook_common as _common
from offerbook_common import _mint_from_asset

API_BASE = os.getenv("OFFERBOOK_API_BASE", "https://api.offerbook.jup.ag/api/v1")
PAGE_SIZE = 100

WINDOW_HOURS_DEFAULT = 48
# How much longer than the lookback window a state entry is kept before
# being pruned — a cushion so a late-running/paused workflow (cadence drifts
# behind "every 12h") doesn't prune an entry right before it would've been
# re-skipped anyway, which is harmless, vs. pruning it too early and letting
# it re-trigger a duplicate email on a future run, which isn't.
STATE_PRUNE_MARGIN_HOURS = 24

STATE_PATH = Path(__file__).parent / "default_activity_watch_state.json"

KNOWN_SYMBOLS = _common.KNOWN_SYMBOLS

SMTP_FROM_EMAIL = os.getenv("SMTP_FROM_EMAIL")
SMTP_APP_PASSWORD = os.getenv("SMTP_APP_PASSWORD")
NOTIFY_EMAIL_TO = os.getenv("NOTIFY_EMAIL_TO")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("default_activity_watch")

SESSION = requests.Session()


def _fetch_all_pages(endpoint: str) -> list[dict]:
    return _common.fetch_all_pages(SESSION, API_BASE, endpoint, None, PAGE_SIZE, sleep_secs=0.1)


def symbol_for(mint: str | None) -> str:
    if not mint:
        return "NFT"
    return KNOWN_SYMBOLS.get(mint, f"{mint[:6]}…{mint[-4:]}")


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def find_recent_defaults(defaulted: list[dict], cutoff: datetime) -> list[dict]:
    """Defaulted loans resolved (updatedAt, the API's only proxy for when —
    same convention portfolio_health.py/pnl_leaderboard.py rely on) on/after
    `cutoff`."""
    out = []
    for l in defaulted:
        resolved = _parse(l.get("updatedAt"))
        if resolved and resolved >= cutoff:
            out.append(l)
    return out


def find_recent_late_repayments(repaid: list[dict], cutoff: datetime) -> list[dict]:
    """Repaid loans whose updatedAt is after their own expiredAt (closed
    late, regardless of whether collateral covered the principal — see
    module docstring for why this is deliberately broader than
    defaulter_watch.py's "fully covered" signal) AND resolved on/after
    `cutoff`."""
    out = []
    for l in repaid:
        expired = _parse(l.get("expiredAt"))
        resolved = _parse(l.get("updatedAt"))
        if not expired or not resolved or resolved <= expired:
            continue  # on-time (or unparseable — never silently treat as late)
        if resolved >= cutoff:
            out.append(l)
    return out


def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text())
        except json.JSONDecodeError:
            return {}
    return {}


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")


def prune_state(state: dict, now: datetime, window_hours: int) -> None:
    """Drops entries that have aged out of window_hours + the safety margin
    — they can never become a lookback-window candidate again on any future
    run, so there's no reason to keep remembering them."""
    cutoff = now - timedelta(hours=window_hours + STATE_PRUNE_MARGIN_HOURS)
    for pubkey in list(state.keys()):
        resolved = _parse(state[pubkey].get("resolved_at"))
        if resolved and resolved < cutoff:
            del state[pubkey]


def _loan_block(l: dict, kind: str) -> str:
    meta = l.get("metadata") or {}
    pmint = l.get("principalMint") or _mint_from_asset(l.get("principal", {}))
    cmint = l.get("collateralMint") or _mint_from_asset(l.get("collateral", {}))
    start_principal_usd = meta.get("startPrincipalAmountUsd") or 0.0
    expired = _parse(l.get("expiredAt"))
    resolved = _parse(l.get("updatedAt"))
    late_hrs = (resolved - expired).total_seconds() / 3600 if expired and resolved else None

    lines = [
        f"[{kind}] {l.get('pubkey', '')}",
        f"  borrower: {l.get('borrower', '')}   lender: {l.get('lender', '')}",
        f"  principal: ${start_principal_usd:,.2f} {symbol_for(pmint)}   collateral: {symbol_for(cmint)}",
        f"  apy: {l.get('apy', 0) / 100:.2f}%   duration: {(l.get('duration') or 0) / 86400:.1f}d",
    ]
    if kind == "DEFAULTED":
        end_collateral_usd = meta.get("endCollateralAmountUsd")
        if end_collateral_usd is None:
            end_collateral_usd = meta.get("startCollateralAmountUsd") or 0.0
        surplus = end_collateral_usd - start_principal_usd
        coverage = "surplus" if surplus >= 0 else "SHORTFALL"
        lines.append(
            f"  collateral seized worth ${end_collateral_usd:,.2f} vs ${start_principal_usd:,.2f} owed "
            f"({coverage} ${surplus:+,.2f})"
        )
    elif late_hrs is not None:
        lines.append(f"  repaid {late_hrs:.1f}h after expiry ({l.get('expiredAt')} -> {l.get('updatedAt')})")
    return "\n".join(lines)


def build_report(new_defaults: list[dict], new_late: list[dict], window_hours: int) -> str:
    total_defaulted_usd = sum((l.get("metadata") or {}).get("startPrincipalAmountUsd") or 0.0 for l in new_defaults)
    total_late_usd = sum((l.get("metadata") or {}).get("startPrincipalAmountUsd") or 0.0 for l in new_late)

    lines = [
        f"Offerbook default/late-payment digest — trailing {window_hours}h, platform-wide",
        "",
        f"{len(new_defaults)} new default(s) totaling ${total_defaulted_usd:,.2f} principal",
        f"{len(new_late)} new late repayment(s) totaling ${total_late_usd:,.2f} principal",
    ]
    if new_defaults:
        lines += ["", "=" * 70, "DEFAULTS", "=" * 70]
        for l in sorted(new_defaults, key=lambda l: l.get("updatedAt", ""), reverse=True):
            lines += ["", _loan_block(l, "DEFAULTED")]
    if new_late:
        lines += ["", "=" * 70, "LATE REPAYMENTS", "=" * 70]
        for l in sorted(new_late, key=lambda l: l.get("updatedAt", ""), reverse=True):
            lines += ["", _loan_block(l, "LATE REPAID")]
    return "\n".join(lines)


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


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--hours-back", type=int, default=WINDOW_HOURS_DEFAULT,
        help=f"Lookback window in hours (default {WINDOW_HOURS_DEFAULT}).",
    )
    parser.add_argument("--no-email", action="store_true", help="Console output only — skip email and state persistence.")
    args = parser.parse_args()

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=args.hours_back)

    log.info("Fetching defaulted + repaid loans platform-wide …")
    defaulted = _fetch_all_pages("/loans/status/defaulted")
    repaid = _fetch_all_pages("/loans/status/repaid")
    log.info("  → defaulted=%d  repaid=%d (platform-wide, all-time)", len(defaulted), len(repaid))

    recent_defaults = find_recent_defaults(defaulted, cutoff)
    recent_late = find_recent_late_repayments(repaid, cutoff)
    log.info(
        "In the last %dh: %d default(s), %d late repayment(s)",
        args.hours_back, len(recent_defaults), len(recent_late),
    )

    state = load_state() if not args.no_email else {}
    new_defaults = [l for l in recent_defaults if l.get("pubkey") not in state]
    new_late = [l for l in recent_late if l.get("pubkey") not in state]

    report_text = build_report(new_defaults, new_late, args.hours_back)
    log.info("")
    for line in report_text.splitlines():
        log.info(line)

    if args.no_email:
        log.info("--no-email set — skipped email and state persistence.")
        return

    if not new_defaults and not new_late:
        log.info("Nothing new since last run — skipping email.")
    else:
        subject = f"Offerbook: {len(new_defaults)} default(s), {len(new_late)} late repayment(s) — last {args.hours_back}h"
        send_email(subject, report_text)
        log.info("Emailed digest: %d new default(s), %d new late repayment(s)", len(new_defaults), len(new_late))

    for l in new_defaults + new_late:
        state[l["pubkey"]] = {"resolved_at": l.get("updatedAt")}
    prune_state(state, now, args.hours_back)
    save_state(state)


if __name__ == "__main__":
    main()
