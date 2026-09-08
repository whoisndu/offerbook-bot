"""
Offerbook Portfolio Health Check
==================================
Read-only lender-side report across one or more of your own wallets:

  - Open loan risk: live LTV (recomputed from current prices, not the
    stale LTV at origination) vs. AT_RISK_LTV / UNDERWATER_LTV thresholds,
    plus days-to-expiry (or already-overdue) flagging.
  - Realized performance: net interest earned on repaid loans (interest
    converted to USD via the platform's proportional formula, minus the
    actual repay fee charged) plus collateral kept on defaulted loans
    (valued at default time) minus the principal lost — same formula
    pnl_leaderboard.py uses platform-wide, scoped here to your wallets.
  - Wallet/escrow balances (SOL for gas, USDC for capital on hand).
  - Volume: total USD principal (at origination) of every loan lent,
    counted the moment it's created regardless of current status —
    VOLUME_WINDOW_DAYS (7) trailing-day rolling total and all-time, per
    wallet and combined across the portfolio.
  - Google Calendar reminder sync: diffs currently-active loans against
    portfolio_reminder_state.json (last-known set of tracked reminders)
    and, by default, directly syncs the result to your Google Calendar via
    google_calendar_client.py (its own OAuth client, independent of any
    Claude/MCP connector) — creating an "expires in 30min" popup reminder
    for every active loan that doesn't have one yet, and deleting the
    reminder for any previously-tracked loan that's since resolved
    (repaid/defaulted). Requires one-time setup — see
    google_calendar_client.py's docstring. Pass --no-calendar-sync to just
    refresh portfolio_reminder_sync_plan.json without touching Calendar
    (e.g. before you've done that setup).

Never signs or submits anything on Offerbook — read-only there, no private
key needed. (It does create/delete Google Calendar events, per the above.)

Wallets to check come from OFFERBOOK_PORTFOLIO_WALLETS in .env (comma-
separated addresses) so the addresses themselves never appear in this
file — .env is gitignored, so this script stays safe to commit publicly
even though it's your own wallets being reported on. Override with
--wallets for a one-off check without touching .env.

Usage:
  python portfolio_health.py
  python portfolio_health.py --wallets <addr1>,<addr2>
  python portfolio_health.py --risk-ltv 0.80 --expiry-hours 24
  python portfolio_health.py --no-calendar-sync
"""
from __future__ import annotations

import argparse
import json
import logging
import os
from datetime import datetime, timedelta, timezone

import requests
from dotenv import load_dotenv

load_dotenv()

import offerbook_common as _common
from offerbook_common import _mint_from_asset

API_BASE = os.getenv("OFFERBOOK_API_BASE", "https://api.offerbook.jup.ag/api/v1")
SOLANA_RPC = os.getenv("SOLANA_RPC", "https://api.mainnet-beta.solana.com")
JUPITER_PRICE_API = "https://api.jup.ag/price/v3"
JUPITER_API_KEY = os.getenv("JUPITER_API_KEY", "")
DEXSCREENER_API = "https://api.dexscreener.com/latest/dex/tokens"

USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDC_DECIMALS = 6
SOL_MINT = "So11111111111111111111111111111111111111112"
SOL_DECIMALS = 9
PAGE_SIZE = 100

KNOWN_DECIMALS = _common.KNOWN_DECIMALS
KNOWN_SYMBOLS = _common.KNOWN_SYMBOLS

AT_RISK_LTV = 0.85       # live LTV at/above this: approaching underwater, flagged as a warning
UNDERWATER_LTV = 1.0     # live LTV at/above this: collateral is worth less than principal
EXPIRY_WARNING_HOURS = 48  # active loans due within this window (or already overdue) get flagged
DISPLAY_TZ = timezone(timedelta(hours=1))  # exact expiry timestamps in the expiring-soon table are
                                             # shown in this timezone (UTC+1) alongside the relative
                                             # countdown, since "due in 4.2h" alone doesn't tell you
                                             # the actual clock time to plan around

REMINDER_MINUTES_BEFORE = 30  # how far ahead of a loan's expiry its calendar reminder should fire
REMINDER_STATE_PATH = os.path.join(os.path.dirname(__file__), "portfolio_reminder_state.json")
REMINDER_SYNC_PLAN_PATH = os.path.join(os.path.dirname(__file__), "portfolio_reminder_sync_plan.json")

VOLUME_WINDOW_DAYS = 7  # "this week" volume = loans originated in the trailing N days (rolling
                          # window from now, not calendar-week-aligned)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("portfolio_health")

SESSION = requests.Session()

# ---------------------------------------------------------------------------
# Fetch helpers
# ---------------------------------------------------------------------------

def _fetch_all_pages(endpoint: str, params: dict | None = None) -> list[dict]:
    return _common.fetch_all_pages(SESSION, API_BASE, endpoint, params, PAGE_SIZE, sleep_secs=0.1)


def symbol_for(mint: str | None) -> str:
    if not mint:
        return "NFT"
    return KNOWN_SYMBOLS.get(mint, f"{mint[:6]}…{mint[-4:]}")


def fetch_wallet_sol_balance(wallet: str) -> int:
    """Lamports."""
    payload = {"jsonrpc": "2.0", "id": 1, "method": "getBalance", "params": [wallet]}
    resp = requests.post(SOLANA_RPC, json=payload, timeout=15)
    resp.raise_for_status()
    return resp.json().get("result", {}).get("value", 0)


def fetch_wallet_token_balance(wallet: str, mint: str) -> int:
    """Raw token units, summed across every token account this wallet holds for `mint`."""
    payload = {
        "jsonrpc": "2.0", "id": 1, "method": "getTokenAccountsByOwner",
        "params": [wallet, {"mint": mint}, {"encoding": "jsonParsed"}],
    }
    resp = requests.post(SOLANA_RPC, json=payload, timeout=15)
    resp.raise_for_status()
    accounts = resp.json().get("result", {}).get("value", [])
    return sum(
        int(a.get("account", {}).get("data", {}).get("parsed", {}).get("info", {})
             .get("tokenAmount", {}).get("amount", "0"))
        for a in accounts
    )


def fetch_escrow_balance(wallet: str, mint: str) -> int:
    resp = SESSION.get(f"{API_BASE}/escrows/holdings/{wallet}", timeout=30)
    resp.raise_for_status()
    for entry in resp.json():
        if entry.get("asset", {}).get("mint") == mint:
            return int(entry.get("amount", 0))
    return 0


def fetch_current_prices(mints: list[str]) -> tuple[dict[str, float], dict[str, int]]:
    """({mint: usd_price_per_whole_token}, {mint: decimals}), Jupiter batch first,
    DexScreener per-mint fallback for anything Jupiter doesn't cover."""
    mints = [m for m in dict.fromkeys(mints) if m]  # de-dupe, preserve order
    if not mints:
        return {}, {}

    prices: dict[str, float] = {}
    decimals: dict[str, int] = {}
    try:
        headers = {"x-api-key": JUPITER_API_KEY} if JUPITER_API_KEY else {}
        resp = SESSION.get(JUPITER_PRICE_API, params={"ids": ",".join(mints)}, headers=headers, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        prices = {mint: float(info["usdPrice"]) for mint, info in data.items() if info.get("usdPrice")}
        decimals = {mint: int(info["decimals"]) for mint, info in data.items() if info.get("decimals") is not None}
    except Exception as exc:
        log.warning("Jupiter batch price fetch failed: %s", exc)

    for mint in mints:
        if mint in prices:
            continue
        try:
            resp = SESSION.get(f"{DEXSCREENER_API}/{mint}", timeout=10)
            if not resp.ok:
                continue
            pairs = [p for p in (resp.json().get("pairs") or []) if p.get("chainId") == "solana" and p.get("priceUsd")]
            if not pairs:
                continue
            pairs.sort(key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0), reverse=True)
            prices[mint] = float(pairs[0]["priceUsd"])
        except Exception:
            continue

    for mint in mints:
        if mint not in decimals:
            d = KNOWN_DECIMALS.get(mint)
            if d is not None:
                decimals[mint] = d
    return prices, decimals


def compute_live_ltv(
    principal_mint: str, collateral_mint: str, principal_raw: int, collateral_raw: int,
    prices: dict[str, float], decimals: dict[str, int],
) -> tuple[float | None, float | None, float | None]:
    """(ltv, principal_usd, collateral_usd) at CURRENT market prices — None,None,None
    if a price/decimals figure is missing for either side."""
    p_price = 1.0 if principal_mint == USDC_MINT else prices.get(principal_mint)
    c_price = prices.get(collateral_mint)
    p_decimals = USDC_DECIMALS if principal_mint == USDC_MINT else decimals.get(principal_mint)
    c_decimals = decimals.get(collateral_mint)
    if p_price is None or c_price is None or p_decimals is None or c_decimals is None:
        return None, None, None

    principal_usd = (principal_raw / 10 ** p_decimals) * p_price
    collateral_usd = (collateral_raw / 10 ** c_decimals) * c_price
    if collateral_usd <= 0:
        return None, principal_usd, collateral_usd
    return principal_usd / collateral_usd, principal_usd, collateral_usd

# ---------------------------------------------------------------------------
# Per-loan / per-wallet stats
# ---------------------------------------------------------------------------

def compute_realized_pnl(repaid: list[dict], defaulted: list[dict], wallet: str) -> dict:
    """Realized PNL for one wallet as lender — same formula as pnl_leaderboard.py:
    net interest on repaid loans (interest/principalAmount * startPrincipalAmountUsd,
    minus the actual repay fee charged), plus kept collateral on defaults (valued at
    default time) minus the principal lost."""
    mine_repaid = [l for l in repaid if l.get("lender") == wallet]
    mine_defaulted = [l for l in defaulted if l.get("lender") == wallet]

    interest_usd = fees_usd = defaulted_pnl_usd = principal_repaid_usd = 0.0
    for l in mine_repaid:
        principal_amount = l.get("principalAmount") or 0
        if principal_amount == 0:
            continue
        meta = l.get("metadata") or {}
        start_principal_usd = meta.get("startPrincipalAmountUsd") or 0.0
        principal_repaid_usd += start_principal_usd
        interest = l.get("interest") or 0
        gross = (interest / principal_amount) * start_principal_usd
        repay_fee_usd = ((meta.get("fees") or {}).get("repay") or {}).get("amountUsd") or 0.0
        interest_usd += gross
        fees_usd += repay_fee_usd

    for l in mine_defaulted:
        meta = l.get("metadata") or {}
        start_principal_usd = meta.get("startPrincipalAmountUsd") or 0.0
        end_collateral_usd = meta.get("endCollateralAmountUsd")
        if end_collateral_usd is None:
            end_collateral_usd = meta.get("startCollateralAmountUsd") or 0.0
        defaulted_pnl_usd += end_collateral_usd - start_principal_usd

    net_pnl_usd = interest_usd - fees_usd + defaulted_pnl_usd
    resolved_count = len(mine_repaid) + len(mine_defaulted)
    default_rate = (len(mine_defaulted) / resolved_count * 100) if resolved_count else None

    return {
        "repaid_count": len(mine_repaid),
        "defaulted_count": len(mine_defaulted),
        "principal_repaid_usd": principal_repaid_usd,
        "interest_usd": interest_usd,
        "fees_usd": fees_usd,
        "defaulted_pnl_usd": defaulted_pnl_usd,
        "net_pnl_usd": net_pnl_usd,
        "default_rate": default_rate,
    }


def compute_volume(all_loans: list[dict], wallet: str, since: datetime | None) -> float:
    """
    Total USD principal (at origination) of every loan this wallet has ever
    lent, optionally restricted to loans CREATED on/after `since`. A loan
    counts toward volume the moment it's originated, regardless of its
    current status (active/repaid/defaulted) — this measures capital put to
    work, not capital currently at risk or already resolved. `all_loans`
    should be active+defaulted+repaid combined for a true total.
    """
    total = 0.0
    for l in all_loans:
        if l.get("lender") != wallet:
            continue
        if since is not None:
            try:
                created_at = datetime.fromisoformat(l["createdAt"].replace("Z", "+00:00"))
            except (KeyError, ValueError):
                continue
            if created_at < since:
                continue
        meta = l.get("metadata") or {}
        total += meta.get("startPrincipalAmountUsd") or 0.0
    return total


def build_active_loan_rows(active: list[dict], wallet: str, prices: dict, decimals: dict, now: datetime) -> list[dict]:
    rows = []
    for l in active:
        if l.get("lender") != wallet:
            continue
        pmint = l.get("principalMint") or _mint_from_asset(l.get("principal", {}))
        cmint = l.get("collateralMint") or _mint_from_asset(l.get("collateral", {}))
        principal_raw = l.get("principalAmount") or 0
        collateral_raw = l.get("collateralAmount") or 0

        live_ltv, live_principal_usd, _live_collateral_usd = compute_live_ltv(
            pmint, cmint, principal_raw, collateral_raw, prices, decimals
        )
        meta = l.get("metadata") or {}
        start_principal_usd = meta.get("startPrincipalAmountUsd")
        start_collateral_usd = meta.get("startCollateralAmountUsd")
        origination_ltv = (
            start_principal_usd / start_collateral_usd
            if start_principal_usd and start_collateral_usd else None
        )

        expired_at = None
        hrs_left = None
        try:
            expired_at = datetime.fromisoformat(l["expiredAt"].replace("Z", "+00:00"))
            hrs_left = (expired_at - now).total_seconds() / 3600.0
        except (KeyError, ValueError):
            pass

        # Unrealized profit: interest accrued so far but not yet collected
        # (only realized on repayment; lost — replaced by whatever collateral
        # is seized instead — if the loan defaults first). Pro-rated by
        # elapsed time vs. the loan's full duration, converted to USD via the
        # same proportional formula compute_realized_pnl uses for actually-
        # collected interest.
        accrued_interest_usd = None
        try:
            created_at = datetime.fromisoformat(l["createdAt"].replace("Z", "+00:00"))
            duration_secs = l.get("duration") or 0
            interest = l.get("interest") or 0
            if duration_secs > 0 and principal_raw > 0 and start_principal_usd:
                fraction = max(0.0, min(1.0, (now - created_at).total_seconds() / duration_secs))
                accrued_interest_usd = (interest * fraction / principal_raw) * start_principal_usd
        except (KeyError, ValueError):
            pass

        rows.append({
            "loan_id": l.get("pubkey", ""),
            "wallet": wallet,
            "borrower": l.get("borrower", ""),
            "collateral_symbol": symbol_for(cmint),
            "principal_usd": live_principal_usd if live_principal_usd is not None else start_principal_usd,
            "apy_bps": l.get("apy", 0),
            "origination_ltv": origination_ltv,
            "live_ltv": live_ltv,
            "expired_at": expired_at,
            "hrs_left": hrs_left,
            "accrued_interest_usd": accrued_interest_usd,
        })
    return rows

# ---------------------------------------------------------------------------
# Calendar reminder sync plan
# ---------------------------------------------------------------------------
#
# This script never talks to Google Calendar directly — it has no OAuth
# credentials of its own. Instead it computes what WOULD need to change
# (given the loans that are currently active vs. REMINDER_STATE_PATH, the
# record of reminders already created) and writes that as a plan to
# REMINDER_SYNC_PLAN_PATH. Whatever actually has Calendar access reads that
# plan, creates/deletes the events, and is responsible for writing the
# updated REMINDER_STATE_PATH back with the real event IDs — this script
# only ever reads that state file, never writes it.

def load_reminder_state() -> dict:
    """{loan_id: {"event_id": ..., "wallet": ..., "expires_at": ...}} for
    every loan that currently has a tracked Google Calendar reminder."""
    if not os.path.exists(REMINDER_STATE_PATH):
        return {}
    try:
        with open(REMINDER_STATE_PATH) as fh:
            return json.load(fh)
    except (json.JSONDecodeError, OSError):
        return {}


def build_reminder_sync_plan(all_active_rows: list[dict], state: dict, now: datetime) -> dict:
    """
    Diff every currently-active loan (across all wallets checked this run)
    against the last-known reminder state:
      - "create": active loans with no tracked reminder yet, whose
        (expiry - REMINDER_MINUTES_BEFORE) hasn't already passed — creating
        a reminder for a window that's already gone would just be a
        calendar event that never fires.
      - "delete": previously-tracked loans no longer in the active list —
        they must have resolved (repaid or defaulted) since the state was
        last updated, so their reminder is now stale and should be removed.
    """
    active_by_id = {r["loan_id"]: r for r in all_active_rows if r.get("loan_id")}

    to_create = []
    for loan_id, r in active_by_id.items():
        if loan_id in state or r["expired_at"] is None:
            continue
        reminder_time = r["expired_at"] - timedelta(minutes=REMINDER_MINUTES_BEFORE)
        if reminder_time <= now:
            continue
        borrower = r["borrower"]
        borrower_short = f"{borrower[:6]}…{borrower[-4:]}" if borrower else "unknown"
        to_create.append({
            "loan_id": loan_id,
            "wallet": r["wallet"],
            "summary": f"Offerbook loan expiring in {REMINDER_MINUTES_BEFORE}min — {r['collateral_symbol']} ({borrower_short})",
            "description": (
                f"Wallet: {r['wallet']}\nBorrower: {borrower}\nCollateral: {r['collateral_symbol']}\n"
                f"Principal: ${r['principal_usd']:.2f}\nAPY: {r['apy_bps']/100:.2f}%\n"
                f"Live LTV: {_fmt_pct(r['live_ltv'])}\nExpires: {_fmt_utc1(r['expired_at'])}"
            ),
            "reminder_time_utc": reminder_time.isoformat(),
            "expires_at_utc": r["expired_at"].isoformat(),
        })

    to_delete = [
        {"loan_id": loan_id, "event_id": entry.get("event_id"), "wallet": entry.get("wallet")}
        for loan_id, entry in state.items()
        if loan_id not in active_by_id
    ]

    return {"generated_at": now.isoformat(), "create": to_create, "delete": to_delete}


def write_reminder_sync_plan(plan: dict) -> None:
    with open(REMINDER_SYNC_PLAN_PATH, "w") as fh:
        json.dump(plan, fh, indent=2)


def save_reminder_state(state: dict) -> None:
    with open(REMINDER_STATE_PATH, "w") as fh:
        json.dump(state, fh, indent=2)


def sync_reminders_to_calendar(plan: dict, state: dict) -> None:
    """
    Actually creates/deletes the Google Calendar events for `plan` (see
    build_reminder_sync_plan) via google_calendar_client, then updates and
    persists REMINDER_STATE_PATH to match — this is the only place that
    file gets written. Requires google_calendar_client's one-time OAuth
    setup (see that module's docstring); the very first call may open a
    browser window for you to grant access.
    """
    try:
        import google_calendar_client as gcal
    except ImportError as exc:
        log.error(
            "Can't sync to Google Calendar — missing dependency (%s). Run: "
            "pip install google-auth google-auth-oauthlib google-api-python-client", exc,
        )
        return

    created = deleted = errors = 0
    for entry in plan["create"]:
        try:
            start_dt = datetime.fromisoformat(entry["reminder_time_utc"])
            end_dt = start_dt + timedelta(minutes=5)
            event_id = gcal.create_event(entry["summary"], entry["description"], start_dt.isoformat(), end_dt.isoformat())
            state[entry["loan_id"]] = {
                "event_id": event_id, "wallet": entry["wallet"], "expires_at": entry["expires_at_utc"],
            }
            created += 1
        except Exception as exc:
            log.error("Failed to create reminder for loan %s: %s", entry["loan_id"], exc)
            errors += 1

    for entry in plan["delete"]:
        try:
            if entry.get("event_id"):
                gcal.delete_event(entry["event_id"])
            state.pop(entry["loan_id"], None)
            deleted += 1
        except Exception as exc:
            log.error("Failed to delete reminder for loan %s: %s", entry["loan_id"], exc)
            errors += 1

    save_reminder_state(state)
    log.info("Calendar sync: created=%d  deleted=%d  errors=%d", created, deleted, errors)

# ---------------------------------------------------------------------------
# Printing
# ---------------------------------------------------------------------------

def _fmt_hrs(hrs: float | None) -> str:
    if hrs is None:
        return "n/a"
    if hrs < 0:
        return f"OVERDUE {abs(hrs):.1f}h"
    if hrs < 48:
        return f"{hrs:.1f}h"
    return f"{hrs / 24:.1f}d"


def _fmt_pct(x: float | None) -> str:
    return f"{x * 100:.1f}%" if x is not None else "n/a"


def _fmt_utc1(dt: datetime | None) -> str:
    if dt is None:
        return "n/a"
    return dt.astimezone(DISPLAY_TZ).strftime("%Y-%m-%d %H:%M") + " UTC+1"


def print_wallet_report(
    wallet: str, sol_balance: int, usdc_wallet: int, usdc_escrow: int,
    active_rows: list[dict], pnl: dict, risk_ltv: float, underwater_ltv: float, expiry_hours: float,
    volume_week_usd: float, volume_all_time_usd: float,
) -> None:
    log.info("")
    log.info("=" * 100)
    log.info("WALLET: %s", wallet)
    log.info("=" * 100)
    log.info(
        "Balances — SOL: %.4f   USDC wallet: %.2f   USDC escrow: %.2f   USDC total: %.2f",
        sol_balance / 10 ** SOL_DECIMALS, usdc_wallet / 10 ** USDC_DECIMALS,
        usdc_escrow / 10 ** USDC_DECIMALS, (usdc_wallet + usdc_escrow) / 10 ** USDC_DECIMALS,
    )
    log.info(
        "Volume — last %d days: $%.2f   all-time: $%.2f",
        VOLUME_WINDOW_DAYS, volume_week_usd, volume_all_time_usd,
    )

    total_active_principal = sum(r["principal_usd"] or 0 for r in active_rows)
    total_unrealized_usd = sum(r["accrued_interest_usd"] or 0 for r in active_rows)
    log.info("Active loans: %d   Outstanding principal: $%.2f", len(active_rows), total_active_principal)
    log.info("Unrealized profit (interest accrued on active loans, not yet collected): $%.2f", total_unrealized_usd)

    at_risk = [r for r in active_rows if r["live_ltv"] is not None and r["live_ltv"] >= risk_ltv]
    expiring = [r for r in active_rows if r["hrs_left"] is not None and r["hrs_left"] <= expiry_hours]
    if at_risk:
        log.info("")
        log.info("AT-RISK active loans (live LTV >= %.0f%%, underwater >= %.0f%%):", risk_ltv * 100, underwater_ltv * 100)
        col = "{:<14}{:<46}{:>10}{:>12}{:>12}{:>10}"
        log.info(col.format("collateral", "borrower", "APY", "orig LTV", "live LTV", "due"))
        for r in sorted(at_risk, key=lambda r: -(r["live_ltv"] or 0)):
            flag = " *** UNDERWATER ***" if (r["live_ltv"] or 0) >= underwater_ltv else ""
            log.info(
                col.format(
                    r["collateral_symbol"], r["borrower"], f"{r['apy_bps']/100:.2f}%",
                    _fmt_pct(r["origination_ltv"]), _fmt_pct(r["live_ltv"]), _fmt_hrs(r["hrs_left"]),
                ) + flag
            )
    else:
        log.info("No active loans at/above the %.0f%% risk LTV threshold.", risk_ltv * 100)

    if expiring:
        log.info("")
        log.info("EXPIRING SOON / OVERDUE (within %.0fh):", expiry_hours)
        col = "{:<14}{:<46}{:>10}{:>12}{:>10}  {:>22}"
        log.info(col.format("collateral", "borrower", "APY", "live LTV", "due", "expires at"))
        for r in sorted(expiring, key=lambda r: (r["hrs_left"] if r["hrs_left"] is not None else 1e9)):
            log.info(col.format(
                r["collateral_symbol"], r["borrower"], f"{r['apy_bps']/100:.2f}%",
                _fmt_pct(r["live_ltv"]), _fmt_hrs(r["hrs_left"]), _fmt_utc1(r["expired_at"]),
            ))

    log.info("")
    log.info(
        "Realized PNL — repaid: %d   defaulted: %d   default rate: %s",
        pnl["repaid_count"], pnl["defaulted_count"],
        f"{pnl['default_rate']:.1f}%" if pnl["default_rate"] is not None else "n/a",
    )
    log.info(
        "  Interest earned (gross): $%.2f   Repay fees paid: $%.2f   Collateral-kept-on-default net: $%.2f",
        pnl["interest_usd"], pnl["fees_usd"], pnl["defaulted_pnl_usd"],
    )
    log.info("  NET REALIZED PNL: $%.2f", pnl["net_pnl_usd"])


def print_portfolio_summary(per_wallet: list[dict]) -> None:
    log.info("")
    log.info("=" * 100)
    log.info("PORTFOLIO SUMMARY (%d wallet(s))", len(per_wallet))
    log.info("=" * 100)
    total_usdc = sum(w["usdc_wallet"] + w["usdc_escrow"] for w in per_wallet)
    total_active = sum(len(w["active_rows"]) for w in per_wallet)
    total_outstanding = sum(sum(r["principal_usd"] or 0 for r in w["active_rows"]) for w in per_wallet)
    total_unrealized = sum(sum(r["accrued_interest_usd"] or 0 for r in w["active_rows"]) for w in per_wallet)
    total_at_risk = sum(
        len([r for r in w["active_rows"] if r["live_ltv"] is not None and r["live_ltv"] >= w["risk_ltv"]])
        for w in per_wallet
    )
    total_net_pnl = sum(w["pnl"]["net_pnl_usd"] for w in per_wallet)
    total_repaid = sum(w["pnl"]["repaid_count"] for w in per_wallet)
    total_defaulted = sum(w["pnl"]["defaulted_count"] for w in per_wallet)
    resolved = total_repaid + total_defaulted
    default_rate = (total_defaulted / resolved * 100) if resolved else None
    total_volume_week = sum(w["volume_week_usd"] for w in per_wallet)
    total_volume_all_time = sum(w["volume_all_time_usd"] for w in per_wallet)

    log.info("Total USDC on hand (wallet + escrow): $%.2f", total_usdc / 10 ** USDC_DECIMALS)
    log.info(
        "Total volume — last %d days: $%.2f   all-time: $%.2f",
        VOLUME_WINDOW_DAYS, total_volume_week, total_volume_all_time,
    )
    log.info("Total active loans: %d   Outstanding principal: $%.2f   At-risk: %d", total_active, total_outstanding, total_at_risk)
    log.info("Total unrealized profit (accrued interest on active loans): $%.2f", total_unrealized)
    log.info(
        "All-time: repaid=%d  defaulted=%d  default rate=%s",
        total_repaid, total_defaulted, f"{default_rate:.1f}%" if default_rate is not None else "n/a",
    )
    log.info("TOTAL NET REALIZED PNL ACROSS PORTFOLIO: $%.2f", total_net_pnl)
    log.info("=" * 100)

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--wallets", default=None,
        help="Comma-separated wallet addresses to report on. Defaults to OFFERBOOK_PORTFOLIO_WALLETS in .env.",
    )
    parser.add_argument("--risk-ltv", type=float, default=AT_RISK_LTV, help=f"Flag active loans at/above this live LTV (default {AT_RISK_LTV})")
    parser.add_argument("--underwater-ltv", type=float, default=UNDERWATER_LTV, help=f"Live LTV considered fully underwater (default {UNDERWATER_LTV})")
    parser.add_argument("--expiry-hours", type=float, default=EXPIRY_WARNING_HOURS, help=f"Flag active loans due within this many hours, or already overdue (default {EXPIRY_WARNING_HOURS})")
    parser.add_argument(
        "--no-calendar-sync", action="store_true",
        help="Skip syncing reminders to Google Calendar — just refresh the local plan file.",
    )
    args = parser.parse_args()

    raw_wallets = args.wallets or os.getenv("OFFERBOOK_PORTFOLIO_WALLETS", "")
    wallets = [w.strip() for w in raw_wallets.split(",") if w.strip()]
    if not wallets:
        log.error(
            "No wallets to check. Set OFFERBOOK_PORTFOLIO_WALLETS=addr1,addr2 in .env, "
            "or pass --wallets addr1,addr2. Aborting."
        )
        raise SystemExit(1)

    log.info("Checking portfolio health for %d wallet(s) …", len(wallets))
    log.info("Fetching full platform-wide loan history (active + defaulted + repaid) …")
    active = _fetch_all_pages("/loans/status/active")
    defaulted = _fetch_all_pages("/loans/status/defaulted")
    repaid = _fetch_all_pages("/loans/status/repaid")
    log.info("  → active=%d  defaulted=%d  repaid=%d (platform-wide)", len(active), len(defaulted), len(repaid))

    wallet_set = set(wallets)
    my_active = [l for l in active if l.get("lender") in wallet_set]
    collateral_mints = {
        l.get("collateralMint") or _mint_from_asset(l.get("collateral", {})) for l in my_active
    }
    collateral_mints.discard(None)
    prices, decimals = fetch_current_prices(list(collateral_mints))

    now = datetime.now(timezone.utc)
    all_loans = active + defaulted + repaid
    volume_since = now - timedelta(days=VOLUME_WINDOW_DAYS)

    per_wallet = []
    for wallet in wallets:
        sol_balance = fetch_wallet_sol_balance(wallet)
        usdc_wallet = fetch_wallet_token_balance(wallet, USDC_MINT)
        usdc_escrow = fetch_escrow_balance(wallet, USDC_MINT)
        active_rows = build_active_loan_rows(active, wallet, prices, decimals, now)
        pnl = compute_realized_pnl(repaid, defaulted, wallet)
        volume_week_usd = compute_volume(all_loans, wallet, volume_since)
        volume_all_time_usd = compute_volume(all_loans, wallet, None)

        print_wallet_report(
            wallet, sol_balance, usdc_wallet, usdc_escrow, active_rows, pnl,
            args.risk_ltv, args.underwater_ltv, args.expiry_hours,
            volume_week_usd, volume_all_time_usd,
        )
        per_wallet.append({
            "wallet": wallet, "usdc_wallet": usdc_wallet, "usdc_escrow": usdc_escrow,
            "active_rows": active_rows, "pnl": pnl, "risk_ltv": args.risk_ltv,
            "volume_week_usd": volume_week_usd, "volume_all_time_usd": volume_all_time_usd,
        })

    if len(per_wallet) > 1:
        print_portfolio_summary(per_wallet)

    all_active_rows = [r for w in per_wallet for r in w["active_rows"]]
    reminder_state = load_reminder_state()
    reminder_plan = build_reminder_sync_plan(all_active_rows, reminder_state, now)
    write_reminder_sync_plan(reminder_plan)
    log.info(
        "Reminder sync plan: %d to create, %d to delete.",
        len(reminder_plan["create"]), len(reminder_plan["delete"]),
    )

    if args.no_calendar_sync:
        log.info("Skipping Calendar sync (--no-calendar-sync).")
    else:
        sync_reminders_to_calendar(reminder_plan, reminder_state)


if __name__ == "__main__":
    main()
