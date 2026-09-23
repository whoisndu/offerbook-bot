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
    Also broken out into trailing realized-earnings windows (last 24h/7d/
    14d/YTD) using each resolved loan's updatedAt as a proxy for when it was
    repaid/defaulted (the API has no dedicated repaidAt/defaultedAt field),
    alongside the existing all-time total.
  - ROI: realized PNL (all-time and YTD) as a % of net capital deposited —
    from Offerbook's own /users/{address}/escrow-summary endpoint
    (netDepositedUsd = lifetime deposits minus withdrawals, in USD at each
    movement's own price; interest credited to a lender is deliberately
    excluded from this figure by the platform itself, so yield never gets
    counted as capital). Summed across every wallet checked this run. If any
    wallet has unpriced capital movements (a deposit/withdrawal the platform
    couldn't price), that wallet's contribution is flagged as an
    understatement rather than silently treated as complete. YTD = resolved
    since Jan 1 of the current year — currently equal to all-time, since
    Offerbook itself is younger than a year, but computed properly so it's
    correct once that stops being true.
  - Portfolio size: open principal (live value) + accrued interest owed on
    active loans - current underwater losses + idle balance (USDC, SOL, and
    every OTHER token sitting in wallet + escrow — e.g. collateral kept
    after a default, priced live). A single mark-to-market figure for total
    value under your control right now — capital actively lent out plus
    capital just sitting idle, in whatever form — not just the raw
    active-loan principal.
  - Wallet/escrow balances (SOL for gas, USDC for capital on hand), plus a
    breakdown of every OTHER token held (any nonzero balance, wallet or
    escrow, that isn't SOL/USDC) with its live USD value — this is what
    picks up default-seized collateral you're holding onto rather than
    immediately selling.
  - Capital freeing up: principal (USD) of active loans due within the next
    24h / 48h / 72h — an optimistic estimate (assumes on-schedule repayment,
    not default) of how much capital should become available to redeploy,
    for planning ahead on new lending.
  - Volume: total USD principal (at origination) of every loan lent,
    counted the moment it's created regardless of current status —
    VOLUME_WINDOW_DAYS (7) trailing-day rolling total and all-time, per
    wallet and combined across the portfolio.
  - Google Calendar reminder sync: diffs currently-active loans against
    portfolio_reminder_state.json (last-known set of tracked reminders)
    and, by default, directly syncs the result to your Google Calendar via
    google_calendar_client.py (its own OAuth client, independent of any
    Claude/MCP connector) — creating an "expires in 30min" popup reminder
    for every active loan that doesn't have one yet, and marking the
    reminder done (relabeled, popup cleared — not deleted, so there's still
    a trail of past loans on the calendar) for any previously-tracked loan
    that's since resolved (repaid/defaulted). Requires one-time setup — see
    google_calendar_client.py's docstring. Pass --no-calendar-sync to just
    refresh portfolio_reminder_sync_plan.json without touching Calendar
    (e.g. before you've done that setup).

Never signs or submits anything on Offerbook — read-only there, no private
key needed. (It does create/update Google Calendar events, per the above.)

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
import sys
from datetime import datetime, timedelta, timezone

import requests
from dotenv import load_dotenv

load_dotenv()

# Shared modules live in ../lib (including google_calendar_client, imported
# lazily further down) — see README's repo-layout note.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))

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

# Both classic SPL and Token-2022 mints show up as "other holdings" (e.g. a
# defaulted loan's seized collateral) — getTokenAccountsByOwner needs a
# separate call per token program, there's no single filter that covers both.
TOKEN_PROGRAM_ID = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM_ID = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"

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

INTEREST_REPAY_FEE_RATE = 0.10  # platform fee taken from interest at repayment — measured directly
                                  # off 158 of your own repaid loans (mean/median both 0.100001,
                                  # range 0.09992-0.10005 — a flat 10%, not APY/market dependent).
                                  # Applied to active loans' committed interest since Offerbook makes
                                  # the full term's interest due regardless of early repayment.

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


def fetch_wallet_all_token_balances(wallet: str) -> dict[str, int]:
    """{mint: raw_amount} for every SPL/Token-2022 token account this wallet
    holds, any mint — used to value "other holdings" (e.g. collateral kept
    after a default) that aren't SOL or USDC. Zero-balance accounts are
    skipped (a closed/emptied token account still shows up from the RPC
    otherwise)."""
    balances: dict[str, int] = {}
    for program_id in (TOKEN_PROGRAM_ID, TOKEN_2022_PROGRAM_ID):
        payload = {
            "jsonrpc": "2.0", "id": 1, "method": "getTokenAccountsByOwner",
            "params": [wallet, {"programId": program_id}, {"encoding": "jsonParsed"}],
        }
        resp = requests.post(SOLANA_RPC, json=payload, timeout=15)
        resp.raise_for_status()
        for a in resp.json().get("result", {}).get("value", []):
            info = a.get("account", {}).get("data", {}).get("parsed", {}).get("info", {})
            mint = info.get("mint")
            amount = int(info.get("tokenAmount", {}).get("amount", "0"))
            if mint and amount > 0:
                balances[mint] = balances.get(mint, 0) + amount
    return balances


def fetch_escrow_all_holdings(wallet: str) -> dict[str, int]:
    """{mint: raw_amount} for every asset this wallet has sitting in
    Offerbook escrow, any mint. In practice this is only ever non-USDC if
    you've posted a borrowing-type offer (collateral backing a borrow
    proposal) — a lender who never borrows will only ever see USDC/SOL here,
    same as fetch_escrow_balance's single-mint version, just all at once."""
    resp = SESSION.get(f"{API_BASE}/escrows/holdings/{wallet}", timeout=30)
    resp.raise_for_status()
    balances: dict[str, int] = {}
    for entry in resp.json():
        mint = entry.get("asset", {}).get("mint")
        amount = int(entry.get("amount", 0))
        if mint and amount > 0:
            balances[mint] = balances.get(mint, 0) + amount
    return balances


def fetch_escrow_summary(wallet: str) -> dict | None:
    """Offerbook's own rollup of this wallet's lifetime capital movements —
    GET /users/{address}/escrow-summary. netDepositedUsd = lifetime deposits
    minus withdrawals in USD (interest credited to a lender is deliberately
    excluded from this figure by the platform itself, so it's a genuine
    cost-basis figure, not conflated with yield). Returns None (not zeros)
    if the request fails, so a transient error can't be mistaken for a
    wallet that's never deposited anything."""
    try:
        resp = SESSION.get(f"{API_BASE}/users/{wallet}/escrow-summary", timeout=30)
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        log.warning("Couldn't fetch escrow summary for %s: %s", wallet, exc)
        return None


def fetch_mint_decimals_onchain(mint: str) -> int | None:
    """Last-resort decimals lookup straight from the mint account, for a
    token neither Jupiter's price response nor KNOWN_DECIMALS covers —
    common for a long-tail/pump.fun token you only hold because you seized
    it as collateral. Works for both classic SPL and Token-2022 mints."""
    payload = {
        "jsonrpc": "2.0", "id": 1, "method": "getAccountInfo",
        "params": [mint, {"encoding": "jsonParsed"}],
    }
    try:
        resp = requests.post(SOLANA_RPC, json=payload, timeout=15)
        resp.raise_for_status()
        value = (resp.json().get("result") or {}).get("value")
        if value:
            return value["data"]["parsed"]["info"]["decimals"]
    except Exception as exc:
        log.warning("Couldn't fetch decimals on-chain for %s…: %s", mint[:8], exc)
    return None


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

def _resolved_at(l: dict) -> datetime | None:
    """When a repaid/defaulted loan was resolved. The API has no dedicated
    repaidAt/defaultedAt field, so updatedAt is used as the proxy — same
    convention defaulter_watch.py relies on for repaid loans."""
    try:
        return datetime.fromisoformat(l["updatedAt"].replace("Z", "+00:00"))
    except (KeyError, ValueError, AttributeError):
        return None


def compute_realized_pnl(repaid: list[dict], defaulted: list[dict], wallet: str, since: datetime | None = None) -> dict:
    """Realized PNL for one wallet as lender — same formula as pnl_leaderboard.py:
    net interest on repaid loans (interest/principalAmount * startPrincipalAmountUsd,
    minus the actual repay fee charged), plus kept collateral on defaults (valued at
    default time) minus the principal lost.

    If `since` is given, only counts loans resolved (see _resolved_at) on/after
    that time — used for the trailing 24h/7d/14d realized-earnings windows,
    as opposed to the default all-time figure (since=None)."""
    mine_repaid = [l for l in repaid if l.get("lender") == wallet]
    mine_defaulted = [l for l in defaulted if l.get("lender") == wallet]
    if since is not None:
        mine_repaid = [l for l in mine_repaid if (ts := _resolved_at(l)) is not None and ts >= since]
        mine_defaulted = [l for l in mine_defaulted if (ts := _resolved_at(l)) is not None and ts >= since]

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

        live_ltv, live_principal_usd, live_collateral_usd = compute_live_ltv(
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

        # Unrealized profit: interest on this platform is NOT prorated by
        # elapsed time — a borrower owes the FULL interest for the loan's
        # whole term regardless of when (or whether, before default) they
        # repay. So the entire committed interest counts from the moment the
        # loan is active, net of the platform's repay fee (a flat 10% of
        # gross interest — confirmed empirically across historical repaid
        # loans, see INTEREST_REPAY_FEE_RATE), only lost — replaced by
        # whatever collateral is seized instead — if the loan defaults.
        interest = l.get("interest") or 0
        accrued_interest_usd = None
        if principal_raw > 0 and start_principal_usd and interest:
            gross_interest_usd = (interest / principal_raw) * start_principal_usd
            accrued_interest_usd = gross_interest_usd * (1 - INTEREST_REPAY_FEE_RATE)

        principal_usd = live_principal_usd if live_principal_usd is not None else start_principal_usd

        # Current underwater loss: if this position had to be closed out right
        # now (borrower defaults, collateral seized at today's price), this is
        # how much of the principal wouldn't be covered by the collateral —
        # zero for healthy (non-underwater) positions. Only computable when a
        # live collateral price is available.
        underwater_loss_usd = 0.0
        if principal_usd is not None and live_collateral_usd is not None:
            underwater_loss_usd = max(0.0, principal_usd - live_collateral_usd)

        rows.append({
            "loan_id": l.get("pubkey", ""),
            "wallet": wallet,
            "borrower": l.get("borrower", ""),
            "collateral_symbol": symbol_for(cmint),
            "principal_usd": principal_usd,
            "apy_bps": l.get("apy", 0),
            "origination_ltv": origination_ltv,
            "live_ltv": live_ltv,
            "expired_at": expired_at,
            "hrs_left": hrs_left,
            "accrued_interest_usd": accrued_interest_usd,
            "underwater_loss_usd": underwater_loss_usd,
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
# plan, creates new events and marks resolved loans' events done (never
# deletes them — see mark_event_done), and is responsible for writing the
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


def build_reminder_sync_plan(all_active_rows: list[dict], state: dict, now: datetime, resolutions: dict[str, str] | None = None) -> dict:
    """
    Diff every currently-active loan (across all wallets checked this run)
    against the last-known reminder state:
      - "create": active loans with no tracked reminder yet, whose
        (expiry - REMINDER_MINUTES_BEFORE) hasn't already passed — creating
        a reminder for a window that's already gone would just be a
        calendar event that never fires.
      - "resolve": previously-tracked loans no longer in the active list —
        they must have resolved (repaid or defaulted) since the state was
        last updated, so their reminder is now stale. Rather than deleting
        it, it gets relabeled done on the calendar (see mark_event_done) so
        there's still a visible trail of past loans instead of the event
        just disappearing. `resolutions` (loan_id -> "repaid"/"defaulted"),
        when given, tags each entry with how it actually resolved.
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

    resolutions = resolutions or {}
    to_resolve = [
        {
            "loan_id": loan_id, "event_id": entry.get("event_id"), "wallet": entry.get("wallet"),
            "resolution": resolutions.get(loan_id),
        }
        for loan_id, entry in state.items()
        if loan_id not in active_by_id
    ]

    return {"generated_at": now.isoformat(), "create": to_create, "resolve": to_resolve}


def write_reminder_sync_plan(plan: dict) -> None:
    with open(REMINDER_SYNC_PLAN_PATH, "w") as fh:
        json.dump(plan, fh, indent=2)


def save_reminder_state(state: dict) -> None:
    with open(REMINDER_STATE_PATH, "w") as fh:
        json.dump(state, fh, indent=2)


def sync_reminders_to_calendar(plan: dict, state: dict) -> None:
    """
    Actually creates new Google Calendar events and relabels resolved ones
    for `plan` (see build_reminder_sync_plan) via google_calendar_client,
    then updates and persists REMINDER_STATE_PATH to match — this is the
    only place that file gets written. Requires google_calendar_client's
    one-time OAuth setup (see that module's docstring); the very first call
    may open a browser window for you to grant access.

    Resolved loans are marked done (relabeled, reminder popup cleared) via
    mark_event_done rather than deleted — the event stays on the calendar
    as a trail of past loans instead of disappearing. Either way the loan
    is dropped from `state` once handled, since it no longer needs diffing
    against future active-loan snapshots.
    """
    try:
        import google_calendar_client as gcal
    except ImportError as exc:
        log.error(
            "Can't sync to Google Calendar — missing dependency (%s). Run: "
            "pip install google-auth google-auth-oauthlib google-api-python-client", exc,
        )
        return

    created = resolved = errors = 0
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

    for entry in plan["resolve"]:
        try:
            if entry.get("event_id"):
                gcal.mark_event_done(entry["event_id"], entry.get("resolution"))
            state.pop(entry["loan_id"], None)
            resolved += 1
        except Exception as exc:
            log.error("Failed to mark reminder done for loan %s: %s", entry["loan_id"], exc)
            errors += 1

    save_reminder_state(state)
    log.info("Calendar sync: created=%d  marked done=%d  errors=%d", created, resolved, errors)

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


def compute_capital_freeing_up(active_rows: list[dict], window_hours: float) -> float:
    """Total principal (USD) of active loans due within `window_hours` (an
    already-overdue loan counts too, since it's due now) — an estimate of how
    much capital should become available to redeploy if those loans resolve
    on schedule. Doesn't distinguish repayment from default: a defaulted loan
    returns seized collateral, not principal, so this is an optimistic
    estimate for any at-risk loans caught in the window."""
    return sum(
        r["principal_usd"] or 0 for r in active_rows
        if r["hrs_left"] is not None and r["hrs_left"] <= window_hours
    )


def print_other_holdings_table(other_holdings: list[dict]) -> None:
    """`other_holdings` entries: {mint, symbol, raw_amount, decimals, price, usd}
    — usd/price/decimals may be None (unpriced). Skips printing entirely if
    there's nothing to show."""
    if not other_holdings:
        return
    log.info("")
    log.info("Other holdings (non-SOL/USDC, wallet + escrow):")
    col = "{:<16}{:<46}{:>18}{:>14}{:>16}"
    log.info(col.format("symbol", "mint", "amount", "price $", "value $"))
    unpriced = 0
    for h in other_holdings:
        amount_str = f"{h['raw_amount'] / 10 ** h['decimals']:,.4f}" if h["decimals"] is not None else f"{h['raw_amount']} (raw)"
        if h["usd"] is not None:
            log.info(col.format(h["symbol"], h["mint"], amount_str, f"{h['price']:,.6g}", f"{h['usd']:,.2f}"))
        else:
            log.info(col.format(h["symbol"], h["mint"], amount_str, "?", "?") + "  *** NO PRICE ***")
            unpriced += 1
    total_usd = sum(h["usd"] or 0 for h in other_holdings)
    log.info("Other holdings total: $%.2f%s", total_usd, f"  ({unpriced} unpriced, excluded)" if unpriced else "")


def print_wallet_report(
    wallet: str, sol_balance: int, sol_escrow: int, usdc_wallet: int, usdc_escrow: int,
    other_holdings: list[dict], idle_balance_usd: float,
    active_rows: list[dict], pnl: dict, risk_ltv: float, underwater_ltv: float, expiry_hours: float,
    volume_week_usd: float, volume_all_time_usd: float,
    pnl_24h: dict, pnl_7d: dict, pnl_14d: dict, pnl_ytd: dict,
) -> None:
    log.info("")
    log.info("=" * 100)
    log.info("WALLET: %s", wallet)
    log.info("=" * 100)
    log.info(
        "Balances — SOL wallet: %.4f   SOL escrow: %.4f   SOL total: %.4f   "
        "USDC wallet: %.2f   USDC escrow: %.2f   USDC total: %.2f",
        sol_balance / 10 ** SOL_DECIMALS, sol_escrow / 10 ** SOL_DECIMALS,
        (sol_balance + sol_escrow) / 10 ** SOL_DECIMALS,
        usdc_wallet / 10 ** USDC_DECIMALS,
        usdc_escrow / 10 ** USDC_DECIMALS, (usdc_wallet + usdc_escrow) / 10 ** USDC_DECIMALS,
    )
    print_other_holdings_table(other_holdings)
    log.info("Idle balance (USDC + SOL + other holdings, wallet + escrow, at current prices): $%.2f", idle_balance_usd)
    log.info(
        "Volume — last %d days: $%.2f   all-time: $%.2f",
        VOLUME_WINDOW_DAYS, volume_week_usd, volume_all_time_usd,
    )

    total_active_principal = sum(r["principal_usd"] or 0 for r in active_rows)
    total_unrealized_usd = sum(r["accrued_interest_usd"] or 0 for r in active_rows)
    total_underwater_loss_usd = sum(r["underwater_loss_usd"] or 0 for r in active_rows)
    freeing_24h_usd = compute_capital_freeing_up(active_rows, 24)
    freeing_48h_usd = compute_capital_freeing_up(active_rows, 48)
    freeing_72h_usd = compute_capital_freeing_up(active_rows, 72)
    log.info("Active loans: %d   Outstanding principal: $%.2f", len(active_rows), total_active_principal)
    log.info(
        "Capital freeing up — next 24h: $%.2f   next 48h: $%.2f   next 72h: $%.2f",
        freeing_24h_usd, freeing_48h_usd, freeing_72h_usd,
    )
    log.info("Unrealized profit (interest owed on active loans, net of repay fee, not yet collected): $%.2f", total_unrealized_usd)
    log.info(
        "Unrealized profit net of current underwater losses (-$%.2f): $%.2f",
        total_underwater_loss_usd, total_unrealized_usd - total_underwater_loss_usd,
    )
    log.info(
        "Portfolio size (open principal + accrued interest - underwater losses + idle balance): $%.2f",
        total_active_principal + total_unrealized_usd - total_underwater_loss_usd + idle_balance_usd,
    )

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
    log.info(
        "  Realized earnings — last 24h: $%.2f (%d resolved)   last 7d: $%.2f (%d resolved)   last 14d: $%.2f (%d resolved)   YTD: $%.2f (%d resolved)",
        pnl_24h["net_pnl_usd"], pnl_24h["repaid_count"] + pnl_24h["defaulted_count"],
        pnl_7d["net_pnl_usd"], pnl_7d["repaid_count"] + pnl_7d["defaulted_count"],
        pnl_14d["net_pnl_usd"], pnl_14d["repaid_count"] + pnl_14d["defaulted_count"],
        pnl_ytd["net_pnl_usd"], pnl_ytd["repaid_count"] + pnl_ytd["defaulted_count"],
    )


def print_portfolio_summary(per_wallet: list[dict]) -> None:
    log.info("")
    log.info("=" * 100)
    log.info("PORTFOLIO SUMMARY (%d wallet(s))", len(per_wallet))
    log.info("=" * 100)
    total_usdc = sum(w["usdc_wallet"] + w["usdc_escrow"] for w in per_wallet)
    total_idle_balance = sum(w["idle_balance_usd"] for w in per_wallet)
    total_other_holdings_usd = sum(w["other_holdings_usd"] for w in per_wallet)
    total_active = sum(len(w["active_rows"]) for w in per_wallet)
    total_outstanding = sum(sum(r["principal_usd"] or 0 for r in w["active_rows"]) for w in per_wallet)
    total_unrealized = sum(sum(r["accrued_interest_usd"] or 0 for r in w["active_rows"]) for w in per_wallet)
    total_underwater_loss = sum(sum(r["underwater_loss_usd"] or 0 for r in w["active_rows"]) for w in per_wallet)
    total_freeing_24h = sum(compute_capital_freeing_up(w["active_rows"], 24) for w in per_wallet)
    total_freeing_48h = sum(compute_capital_freeing_up(w["active_rows"], 48) for w in per_wallet)
    total_freeing_72h = sum(compute_capital_freeing_up(w["active_rows"], 72) for w in per_wallet)
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
    total_pnl_24h = sum(w["pnl_24h"]["net_pnl_usd"] for w in per_wallet)
    total_pnl_7d = sum(w["pnl_7d"]["net_pnl_usd"] for w in per_wallet)
    total_pnl_14d = sum(w["pnl_14d"]["net_pnl_usd"] for w in per_wallet)
    total_pnl_ytd = sum(w["pnl_ytd"]["net_pnl_usd"] for w in per_wallet)

    log.info("Total USDC on hand (wallet + escrow): $%.2f", total_usdc / 10 ** USDC_DECIMALS)
    log.info("Total other holdings (non-SOL/USDC, wallet + escrow, at current prices): $%.2f", total_other_holdings_usd)
    log.info("Total idle balance (USDC + SOL + other holdings, wallet + escrow, at current prices): $%.2f", total_idle_balance)
    log.info(
        "Total volume — last %d days: $%.2f   all-time: $%.2f",
        VOLUME_WINDOW_DAYS, total_volume_week, total_volume_all_time,
    )
    log.info("Total active loans: %d   Outstanding principal: $%.2f   At-risk: %d", total_active, total_outstanding, total_at_risk)
    log.info(
        "Total capital freeing up — next 24h: $%.2f   next 48h: $%.2f   next 72h: $%.2f",
        total_freeing_24h, total_freeing_48h, total_freeing_72h,
    )
    log.info("Total unrealized profit (interest owed on active loans, net of repay fee): $%.2f", total_unrealized)
    log.info(
        "Total unrealized profit net of current underwater losses (-$%.2f): $%.2f",
        total_underwater_loss, total_unrealized - total_underwater_loss,
    )
    log.info(
        "TOTAL PORTFOLIO SIZE (open principal + accrued interest - underwater losses + idle balance): $%.2f",
        total_outstanding + total_unrealized - total_underwater_loss + total_idle_balance,
    )
    log.info(
        "All-time: repaid=%d  defaulted=%d  default rate=%s",
        total_repaid, total_defaulted, f"{default_rate:.1f}%" if default_rate is not None else "n/a",
    )
    log.info("TOTAL NET REALIZED PNL ACROSS PORTFOLIO: $%.2f", total_net_pnl)
    log.info(
        "TOTAL realized earnings — last 24h: $%.2f   last 7d: $%.2f   last 14d: $%.2f   YTD: $%.2f",
        total_pnl_24h, total_pnl_7d, total_pnl_14d, total_pnl_ytd,
    )
    log.info("=" * 100)


def print_roi_summary(per_wallet: list[dict], total_net_pnl_usd: float, total_pnl_ytd_usd: float) -> None:
    """Realized PNL as a % of net capital deposited (Offerbook's own
    escrow-summary rollup — see fetch_escrow_summary), summed across every
    wallet checked this run. Skipped entirely if no wallet's escrow summary
    could be fetched, or if net deposited is <= 0 (a portfolio that's a net
    withdrawer, or brand new, doesn't have a meaningful ROI %% yet)."""
    known = [w for w in per_wallet if w.get("net_deposited_usd") is not None]
    if not known:
        log.info("")
        log.info("ROI: couldn't fetch escrow-summary for any wallet this run — skipping.")
        return

    total_net_deposited = sum(w["net_deposited_usd"] for w in known)
    unpriced_wallets = [w["wallet"] for w in known if w.get("unpriced_movement_count")]
    missing_wallets = [w["wallet"] for w in per_wallet if w.get("net_deposited_usd") is None]

    log.info("")
    log.info("=" * 100)
    log.info("ROI (net capital deposited, per Offerbook's own escrow-summary)")
    log.info("=" * 100)
    log.info("Net capital deposited (all-time, deposits - withdrawals): $%.2f", total_net_deposited)
    if missing_wallets:
        log.info("  (%d wallet(s) excluded above — escrow-summary fetch failed: %s)", len(missing_wallets), ", ".join(missing_wallets))
    if unpriced_wallets:
        log.info(
            "  *** %d wallet(s) have unpriced capital movements — the figure above UNDERSTATES: %s ***",
            len(unpriced_wallets), ", ".join(unpriced_wallets),
        )

    if total_net_deposited <= 0:
        log.info("Net deposited is <= $0 (net withdrawer, or nothing deposited yet) — ROI %% isn't meaningful.")
        log.info("=" * 100)
        return

    roi_all_time = total_net_pnl_usd / total_net_deposited * 100
    roi_ytd = total_pnl_ytd_usd / total_net_deposited * 100
    log.info("Realized PNL (all-time): $%.2f   -> ROI: %.1f%%", total_net_pnl_usd, roi_all_time)
    log.info("Realized PNL (YTD):      $%.2f   -> ROI: %.1f%%", total_pnl_ytd_usd, roi_ytd)
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

    now = datetime.now(timezone.utc)
    all_loans = active + defaulted + repaid
    volume_since = now - timedelta(days=VOLUME_WINDOW_DAYS)
    since_ytd = datetime(now.year, 1, 1, tzinfo=timezone.utc)

    # First pass: fetch every wallet's raw balances (SOL, USDC, and every
    # OTHER token held in wallet+escrow — e.g. collateral kept after a
    # default) before pricing anything, so the batch price/decimals call
    # below can cover every mint across every wallet in one shot instead of
    # re-fetching prices per wallet.
    log.info("Fetching wallet + escrow balances for %d wallet(s) …", len(wallets))
    raw_balances: dict[str, dict] = {}
    other_mints: set[str] = set()
    for wallet in wallets:
        sol_balance = fetch_wallet_sol_balance(wallet)
        usdc_wallet = fetch_wallet_token_balance(wallet, USDC_MINT)
        wallet_tokens = fetch_wallet_all_token_balances(wallet)
        escrow_tokens = fetch_escrow_all_holdings(wallet)
        sol_escrow = escrow_tokens.get(SOL_MINT, 0)
        usdc_escrow = escrow_tokens.get(USDC_MINT, 0)

        other_raw: dict[str, int] = {
            mint: wallet_tokens.get(mint, 0) + escrow_tokens.get(mint, 0)
            for mint in set(wallet_tokens) | set(escrow_tokens)
            if mint not in (SOL_MINT, USDC_MINT)
        }
        other_mints.update(other_raw.keys())

        raw_balances[wallet] = {
            "sol_balance": sol_balance, "sol_escrow": sol_escrow,
            "usdc_wallet": usdc_wallet, "usdc_escrow": usdc_escrow,
            "other_raw": other_raw,
        }

    # SOL_MINT and every "other holdings" mint are fetched alongside
    # collateral mints (one batch call) so idle SOL / non-SOL-non-USDC
    # balances can be valued in USD for the idle-balance / portfolio-size
    # figures below — not because SOL is ever a collateral mint itself.
    prices, decimals = fetch_current_prices(list(collateral_mints | other_mints | {SOL_MINT}))
    sol_price = prices.get(SOL_MINT)
    if sol_price is None:
        log.warning("No live SOL price — idle SOL balances will be valued at $0 in the idle-balance/portfolio-size figures.")

    # Any "other holdings" mint neither Jupiter nor KNOWN_DECIMALS covered —
    # fall back to reading decimals straight off the mint account. A
    # long-tail/pump.fun token you only hold because you seized it as
    # collateral is exactly the case Jupiter's curated price list is least
    # likely to cover.
    for mint in other_mints:
        if mint not in decimals:
            d = fetch_mint_decimals_onchain(mint)
            if d is not None:
                decimals[mint] = d

    per_wallet = []
    for wallet in wallets:
        rb = raw_balances[wallet]
        sol_balance, sol_escrow = rb["sol_balance"], rb["sol_escrow"]
        usdc_wallet, usdc_escrow = rb["usdc_wallet"], rb["usdc_escrow"]
        idle_sol_usd = (sol_balance + sol_escrow) / 10 ** SOL_DECIMALS * (sol_price or 0.0)
        idle_usdc_usd = (usdc_wallet + usdc_escrow) / 10 ** USDC_DECIMALS

        other_holdings = []
        for mint, raw_amount in rb["other_raw"].items():
            price = prices.get(mint)
            dec = decimals.get(mint)
            usd = raw_amount / 10 ** dec * price if price is not None and dec is not None else None
            other_holdings.append({
                "mint": mint, "symbol": symbol_for(mint), "raw_amount": raw_amount,
                "decimals": dec, "price": price, "usd": usd,
            })
        other_holdings.sort(key=lambda h: -(h["usd"] or 0))
        other_holdings_usd = sum(h["usd"] or 0 for h in other_holdings)

        idle_balance_usd = idle_sol_usd + idle_usdc_usd + other_holdings_usd
        active_rows = build_active_loan_rows(active, wallet, prices, decimals, now)
        pnl = compute_realized_pnl(repaid, defaulted, wallet)
        pnl_24h = compute_realized_pnl(repaid, defaulted, wallet, since=now - timedelta(hours=24))
        pnl_7d = compute_realized_pnl(repaid, defaulted, wallet, since=now - timedelta(days=7))
        pnl_14d = compute_realized_pnl(repaid, defaulted, wallet, since=now - timedelta(days=14))
        pnl_ytd = compute_realized_pnl(repaid, defaulted, wallet, since=since_ytd)
        volume_week_usd = compute_volume(all_loans, wallet, volume_since)
        volume_all_time_usd = compute_volume(all_loans, wallet, None)

        escrow_summary = fetch_escrow_summary(wallet)
        net_deposited_usd = escrow_summary.get("netDepositedUsd") if escrow_summary else None
        unpriced_movement_count = escrow_summary.get("unpricedMovementCount", 0) if escrow_summary else 0

        print_wallet_report(
            wallet, sol_balance, sol_escrow, usdc_wallet, usdc_escrow, other_holdings, idle_balance_usd,
            active_rows, pnl,
            args.risk_ltv, args.underwater_ltv, args.expiry_hours,
            volume_week_usd, volume_all_time_usd,
            pnl_24h, pnl_7d, pnl_14d, pnl_ytd,
        )
        per_wallet.append({
            "wallet": wallet, "usdc_wallet": usdc_wallet, "usdc_escrow": usdc_escrow,
            "other_holdings_usd": other_holdings_usd, "idle_balance_usd": idle_balance_usd,
            "active_rows": active_rows, "pnl": pnl, "risk_ltv": args.risk_ltv,
            "volume_week_usd": volume_week_usd, "volume_all_time_usd": volume_all_time_usd,
            "pnl_24h": pnl_24h, "pnl_7d": pnl_7d, "pnl_14d": pnl_14d, "pnl_ytd": pnl_ytd,
            "net_deposited_usd": net_deposited_usd, "unpriced_movement_count": unpriced_movement_count,
        })

    if len(per_wallet) > 1:
        print_portfolio_summary(per_wallet)

    print_roi_summary(
        per_wallet,
        sum(w["pnl"]["net_pnl_usd"] for w in per_wallet),
        sum(w["pnl_ytd"]["net_pnl_usd"] for w in per_wallet),
    )

    all_active_rows = [r for w in per_wallet for r in w["active_rows"]]
    resolutions = {l["pubkey"]: "repaid" for l in repaid if l.get("pubkey")}
    resolutions.update({l["pubkey"]: "defaulted" for l in defaulted if l.get("pubkey")})
    reminder_state = load_reminder_state()
    reminder_plan = build_reminder_sync_plan(all_active_rows, reminder_state, now, resolutions)
    write_reminder_sync_plan(reminder_plan)
    log.info(
        "Reminder sync plan: %d to create, %d to mark done.",
        len(reminder_plan["create"]), len(reminder_plan["resolve"]),
    )

    if args.no_calendar_sync:
        log.info("Skipping Calendar sync (--no-calendar-sync).")
    else:
        sync_reminders_to_calendar(reminder_plan, reminder_state)


if __name__ == "__main__":
    main()
