#!/usr/bin/env python3
"""
Scan every active loan on Offerbook (platform-wide, not just our own wallet)
and email a notification the moment one goes past its expiry while still
unresolved, then a follow-up email once that same loan is repaid or
defaulted (collateral claimed).

Meant to run on a schedule (see .github/workflows/loan_watch.yml, every 2h)
so it costs nothing beyond GitHub Actions' free minutes. State is persisted
to loan_watch_state.json (committed back to the repo by the workflow) so
the same loan is never emailed twice for the same event — the file holds
only public on-chain data (loan pubkeys, borrower/lender addresses,
amounts), never the recipient email or any credentials.

Required env vars (set as GitHub Actions secrets — never committed):
  SMTP_FROM_EMAIL    - Gmail address to send from
  SMTP_APP_PASSWORD  - Gmail App Password for that address
  NOTIFY_EMAIL_TO    - recipient address
"""

import json
import os
import smtplib
import time
from datetime import datetime, timezone
from email.mime.text import MIMEText
from pathlib import Path

import requests

API_BASE = os.getenv("OFFERBOOK_API_BASE", "https://api.offerbook.jup.ag/api/v1")
STATE_PATH = Path(__file__).parent / "loan_watch_state.json"
PAGE_SIZE = 100

SMTP_FROM_EMAIL = os.getenv("SMTP_FROM_EMAIL")
SMTP_APP_PASSWORD = os.getenv("SMTP_APP_PASSWORD")
NOTIFY_EMAIL_TO = os.getenv("NOTIFY_EMAIL_TO")

SESSION = requests.Session()


def _fetch_all_pages(endpoint: str) -> list[dict]:
    items: list[dict] = []
    params = {"limit": PAGE_SIZE, "offset": 0}
    while True:
        resp = SESSION.get(f"{API_BASE}{endpoint}", params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        items.extend(data.get("data", []))
        if not data.get("pagination", {}).get("hasMore", False):
            break
        params["offset"] += PAGE_SIZE
        time.sleep(0.1)
    return items


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _usd(loan: dict) -> float:
    return (loan.get("metadata") or {}).get("startPrincipalAmountUsd") or 0.0


def load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return {}


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")


def send_email(subject: str, body: str) -> None:
    if not (SMTP_FROM_EMAIL and SMTP_APP_PASSWORD and NOTIFY_EMAIL_TO):
        print("SMTP env vars not set — skipping email:", subject)
        return
    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = SMTP_FROM_EMAIL
    msg["To"] = NOTIFY_EMAIL_TO
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(SMTP_FROM_EMAIL, SMTP_APP_PASSWORD)
        server.send_message(msg)


def describe(loan: dict) -> str:
    return (
        f"pubkey: {loan['pubkey']}\n"
        f"borrower: {loan['borrower']}\n"
        f"lender: {loan['lender']}\n"
        f"principal: {loan['principalAmount'] / 10**6:.2f} (~${_usd(loan):.2f})\n"
        f"collateralMint: {loan['collateralMint']}\n"
        f"apy: {loan['apy'] / 100:.2f}%   duration: {loan['duration'] / 86400:.1f}d\n"
        f"expiredAt: {loan['expiredAt']}\n"
    )


def main() -> None:
    state = load_state()
    now = datetime.now(timezone.utc)

    active_loans = _fetch_all_pages("/loans/status/active")
    active_by_pubkey = {l["pubkey"]: l for l in active_loans}

    newly_expired = 0
    resolved = 0

    # 1. Loans that are past due, still active, and not yet flagged.
    for pubkey, loan in active_by_pubkey.items():
        if pubkey in state:
            continue
        expired_at = _parse(loan["expiredAt"])
        if expired_at > now:
            continue
        state[pubkey] = {
            "expiredAt": loan["expiredAt"],
            "borrower": loan["borrower"],
            "lender": loan["lender"],
            "principal_usd": _usd(loan),
        }
        hrs_late = (now - expired_at).total_seconds() / 3600
        send_email(
            f"Offerbook: loan expired ({hrs_late:.1f}h late) — ${_usd(loan):.2f}",
            "This loan is past its due date and still unresolved:\n\n"
            + describe(loan)
            + f"\nLate by: {hrs_late:.1f}h so far.",
        )
        newly_expired += 1

    # 2. Previously-flagged loans that have since dropped out of the active list.
    for pubkey in list(state.keys()):
        if pubkey in active_by_pubkey:
            continue
        resp = SESSION.get(f"{API_BASE}/loan/{pubkey}", timeout=30)
        if resp.status_code != 200:
            continue
        loan = resp.json()
        status = loan.get("status")
        if status not in ("repaid", "defaulted"):
            continue
        expired_at = _parse(loan["expiredAt"])
        resolved_at = _parse(loan["updatedAt"])
        delta_hrs = (resolved_at - expired_at).total_seconds() / 3600
        verb = "REPAID" if status == "repaid" else "DEFAULTED (collateral claimed)"
        send_email(
            f"Offerbook: loan {verb} — ${_usd(loan):.2f}",
            f"{verb}\n\n" + describe(loan) + f"\nResolved at: {loan['updatedAt']}  ({delta_hrs:+.2f}h vs expiry)",
        )
        del state[pubkey]
        resolved += 1

    save_state(state)
    print(f"Newly expired notified: {newly_expired}  Resolved notified: {resolved}  Currently tracked: {len(state)}")


if __name__ == "__main__":
    main()
