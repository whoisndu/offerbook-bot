#!/usr/bin/env python3
"""
Scan every active loan on Offerbook (platform-wide, not just our own wallet)
and email a notification the moment one goes past its expiry while still
unresolved AND its collateral is currently worth more than what's owed
(principal + accrued interest) — i.e. only loans where letting it default
would leave the lender ahead. Loans without that surplus are left untracked
and re-checked every run (surplus can appear later as prices move) rather
than silently skipped forever. A tracked loan then gets a follow-up email
once it resolves, repaid or defaulted.

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
JUPITER_PRICE_API = "https://api.jup.ag/price/v3"
JUPITER_API_KEY = os.getenv("JUPITER_API_KEY", "")
DEXSCREENER_API = "https://api.dexscreener.com/latest/dex/tokens"

STATE_PATH = Path(__file__).parent / "loan_watch_state.json"
PAGE_SIZE = 100

USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

# Same curated table used by soon_to_expire.py — decimals aren't always
# derivable from the loan record itself, so known tokens get an exact
# decimals figure and anything else falls back to whatever Jupiter's price
# response reports (jupiter_decimals) for that mint.
KNOWN_DECIMALS: dict[str, int] = {
    "So11111111111111111111111111111111111111112": 9,
    "mSoLzYCxHdYgdzU16g5QSh3i5K3z3KZK7ytfqcJm7So": 9,
    "bSo13r4TkiE4KumL71LsHTPpL2euBYLFx6h9HP3piy1": 9,
    "jupSoLaHXQiZZTSfEWMTRRgpnyFm8f6sZdosWBjx93v": 9,
    "J1toso1uCk3RLmjorhTtrVwY9HJ7X8V9yYac6Y7kGCPn": 9,
    "Bybit2vBJGhPF52GBdNaQfUJ6ZpThSgHBobjWZpLPb4B": 9,
    "BNso1VUJnh4zcfpZa6986Ea66P6TCp59hvtNJ8b1X85": 9,
    "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN": 6,
    "4k3Dyjzvzp8eMZWUXbBCjEvwSkkk59S5iCNLY3QrkX6R": 6,
    "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263": 5,
    "hntyVP6YFm1Hg25TN9WGLqM12b8TQmcknKrdu1oxWux": 8,
    "nosXBVoaCTtYdLvKY6Csb4AC8JCdQKKAaWYtx2ZMoo7": 6,
    "WENWENvqNAA8883GttHGFApfgzGLtzHain8QxAwYQst": 5,
    "cbbtcf3aa214zXHbiAZQwf4122FBYbraNdFqgw4iMij": 8,
    "kyKYFGGhy5YAg6Yotedj7ZtByUBepsraT4BFkF3Uxmk": 6,
    "stke7uu3fXHsGqKVVjKnkmj65LRPVrqr4bLG2SJg7rh": 9,
    "pumpCmXqMfrsAkQ5r49WcJnRayYRqmXz6ae8H7H9Dfn": 6,
    "5z3EqYQo9HiCEs3R84RCDMu2n7anpDMxRhdK8PSWmrRC": 9,
    "Dz9mQ9NzkBcCsuGPFJ3r1bS4wgqKMHBPiVuniW8Mbonk": 6,
    "Ce2gx9KGXJ6C9Mp5b5x1sn9Mg87JwEbrQby4Zqo3pump": 6,
    "A7bdiYdS5GjqGFtxf17ppRHtDKPkkRqbKtR27dxvQXaS": 8,
    "98sMhvDwXj1RQi5c5Mndm3vPe9cBqPrbLaufMXFNMh5g": 9,
    USDC_MINT: 6,
}

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


def _start_usd(loan: dict) -> float:
    return (loan.get("metadata") or {}).get("startPrincipalAmountUsd") or 0.0


def _fetch_dexscreener_price(mint: str) -> float | None:
    try:
        resp = SESSION.get(f"{DEXSCREENER_API}/{mint}", timeout=10)
        if not resp.ok:
            return None
        pairs = resp.json().get("pairs") or []
        sol_pairs = [p for p in pairs if p.get("chainId") == "solana" and p.get("priceUsd")]
        if not sol_pairs:
            return None
        sol_pairs.sort(key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0), reverse=True)
        return float(sol_pairs[0]["priceUsd"])
    except Exception:
        return None


def fetch_prices(mints: set[str]) -> tuple[dict[str, float], dict[str, int]]:
    """Return ({mint: usd_price_per_whole_token}, {mint: decimals from Jupiter})."""
    prices: dict[str, float] = {}
    jupiter_decimals: dict[str, int] = {}
    mints = {m for m in mints if m and m != USDC_MINT}
    mints_list = list(mints)
    for i in range(0, len(mints_list), 50):
        chunk = mints_list[i : i + 50]
        try:
            headers = {"x-api-key": JUPITER_API_KEY} if JUPITER_API_KEY else {}
            resp = SESSION.get(JUPITER_PRICE_API, params={"ids": ",".join(chunk)}, headers=headers, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            prices.update({m: float(info["usdPrice"]) for m, info in data.items() if info.get("usdPrice")})
            jupiter_decimals.update(
                {m: int(info["decimals"]) for m, info in data.items() if info.get("decimals") is not None}
            )
        except Exception:
            pass

    for mint in [m for m in mints if m not in prices]:
        price = _fetch_dexscreener_price(mint)
        if price:
            prices[mint] = price

    prices[USDC_MINT] = 1.0
    return prices, jupiter_decimals


def usd_value(mint: str, raw: int, prices: dict[str, float], decimals_map: dict[str, int]) -> float | None:
    dec = KNOWN_DECIMALS.get(mint, decimals_map.get(mint))
    price = prices.get(mint)
    if dec is None or not price:
        return None
    return (raw / 10**dec) * price


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
        f"principal: {loan['principalAmount'] / 10**6:.2f} (~${_start_usd(loan):.2f} at origination)\n"
        f"collateralMint: {loan['collateralMint']}\n"
        f"apy: {loan['apy'] / 100:.2f}%   duration: {loan['duration'] / 86400:.1f}d\n"
        f"expiredAt: {loan['expiredAt']}\n"
    )


def main() -> None:
    state = load_state()
    now = datetime.now(timezone.utc)

    active_loans = _fetch_all_pages("/loans/status/active")
    active_by_pubkey = {l["pubkey"]: l for l in active_loans}

    # Candidates: past due, still active, not yet tracked. Only fetch prices
    # for the mints these actually need.
    candidates = [
        l for pubkey, l in active_by_pubkey.items()
        if pubkey not in state and _parse(l["expiredAt"]) <= now
    ]
    mints_needed = {l["principalMint"] for l in candidates} | {l["collateralMint"] for l in candidates}
    prices, jupiter_decimals = fetch_prices(mints_needed) if candidates else ({}, {})

    newly_expired = 0
    resolved = 0

    for loan in candidates:
        pubkey = loan["pubkey"]
        expired_at = _parse(loan["expiredAt"])
        owed_usd = usd_value(loan["principalMint"], loan["repaymentAmount"], prices, jupiter_decimals)
        collateral_usd = usd_value(loan["collateralMint"], loan["collateralAmount"], prices, jupiter_decimals)
        if owed_usd is None or collateral_usd is None:
            # No live price for one side — leave untracked, try again next run.
            continue
        if collateral_usd <= owed_usd:
            # No surplus right now — leave untracked so surplus emerging
            # later (price moves) still gets caught on a future run.
            continue

        surplus_usd = collateral_usd - owed_usd
        state[pubkey] = {
            "expiredAt": loan["expiredAt"],
            "borrower": loan["borrower"],
            "lender": loan["lender"],
            "principal_usd": _start_usd(loan),
        }
        hrs_late = (now - expired_at).total_seconds() / 3600
        send_email(
            f"Offerbook: loan expired w/ surplus ({hrs_late:.1f}h late) — ${surplus_usd:+.2f} surplus",
            "This loan is past its due date, still unresolved, and its collateral "
            f"is currently worth ${surplus_usd:.2f} more than what's owed:\n\n"
            + describe(loan)
            + f"\nOwed (principal+interest): ${owed_usd:.2f}\n"
            + f"Collateral value now: ${collateral_usd:.2f}\n"
            + f"Surplus: ${surplus_usd:+.2f}\n"
            + f"Late by: {hrs_late:.1f}h so far.",
        )
        newly_expired += 1

    # Previously-flagged (surplus-confirmed) loans that have since dropped
    # out of the active list — always follow up on these, no re-filtering.
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
            f"Offerbook: loan {verb} — ${_start_usd(loan):.2f}",
            f"{verb}\n\n" + describe(loan) + f"\nResolved at: {loan['updatedAt']}  ({delta_hrs:+.2f}h vs expiry)",
        )
        del state[pubkey]
        resolved += 1

    save_state(state)
    print(f"Newly expired (surplus) notified: {newly_expired}  Resolved notified: {resolved}  Currently tracked: {len(state)}")


if __name__ == "__main__":
    main()
