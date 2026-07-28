"""
Offerbook Collateral-Coverage Watchlist
======================================
Scans the full loan history (defaulted and repaid) to identify borrowers
whose positions have historically been well-collateralized from a lender's
perspective, based on two signals:

  - DEFAULTED loans where the seized collateral was worth more than the
    outstanding principal at the time of default (full principal recovery
    for the lender), and
  - REPAID loans that closed after their stipulated expiredAt (a late
    repayment, tolerated by the original lender rather than enforced) where
    collateral value also exceeded principal at the time — i.e. positions
    where the lender's capital was fully covered by collateral throughout,
    independent of whether the repayment was timely.

Repayment timeliness on this protocol carries no penalty beyond the
lender's discretion to enforce the expiry, so a borrower's on-time-repayment
rate isn't necessarily predictive of future timeliness. This watchlist
instead ranks borrowers by demonstrated collateral coverage — a more
directly relevant signal for a lender's downside risk than repayment
punctuality alone.

For each such borrower, checks whether they currently:
  - have an open borrow request (actionable now — you could fill it directly)
  - have an active loan (watch its expiry — they may return to borrow again)

Read-only: never signs or submits anything. Meant to be run periodically
(manually, via cron, or the /loop skill) to surface borrow requests from
watchlisted borrowers while they're still open.

Usage:
  python defaulter_watch.py                    # any positive historical surplus
  python defaulter_watch.py --min-surplus 100   # only show borrowers with >$100 total surplus
  python defaulter_watch.py --top 15            # limit the reference watchlist table to 15 rows

Exit codes:
  0 — no open borrow requests from a watchlisted borrower right now
  1 — one or more watchlisted borrowers have an open borrow request right now
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import requests
import yaml
from dotenv import load_dotenv

import offerbook_common as _common
from offerbook_common import _mint_from_asset  # noqa: F401 — re-exported for defaulter_capture.py

load_dotenv()

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

API_BASE = os.getenv("OFFERBOOK_API_BASE", "https://api.offerbook.jup.ag/api/v1")
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
PAGE_SIZE = 100

# Private, gitignored, ever-growing ledger of borrowers worth watching — see
# load_defaulter_config()/save_defaulter_config() below. Never committed:
# this is a personal risk-tracking record, not something to publish alongside
# the public strategy code.
DEFAULTER_CONFIG_PATH = Path(__file__).parent / "defaulter_config.yaml"

KNOWN_SYMBOLS = _common.KNOWN_SYMBOLS

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("defaulter_watch")

SESSION = requests.Session()

# ---------------------------------------------------------------------------
# Fetch helpers
# ---------------------------------------------------------------------------

def _fetch_all_pages(endpoint: str, params: dict | None = None) -> list[dict]:
    return _common.fetch_all_pages(SESSION, API_BASE, endpoint, params, PAGE_SIZE)


def symbol_for(mint: str) -> str:
    if not mint:
        return "NFT"
    if mint in KNOWN_SYMBOLS:
        return KNOWN_SYMBOLS[mint]
    return f"{mint[:6]}…{mint[-4:]}"

# ---------------------------------------------------------------------------
# Watchlist computation
# ---------------------------------------------------------------------------

def _new_borrower_entry() -> dict:
    return {"count": 0, "principal_usd": 0.0, "collateral_usd": 0.0, "collateral_mints": defaultdict(int)}


def _loan_usd_values(l: dict) -> tuple[float, float]:
    """(principal_usd, collateral_usd) at loan close, falling back to origination values."""
    meta = l.get("metadata") or {}
    p_usd = meta.get("endPrincipalAmountUsd") or meta.get("startPrincipalAmountUsd") or 0
    c_usd = meta.get("endCollateralAmountUsd") or meta.get("startCollateralAmountUsd") or 0
    return p_usd, c_usd


def compute_defaulted_stats() -> dict[str, dict]:
    """
    Per-borrower stats from ALL defaulted loans (no surplus filtering yet — see
    merge_target_borrowers). Uses USD values AT THE TIME OF DEFAULT (metadata's
    "end" fields, falling back to "start"): a snapshot of historical outcomes,
    not a live repricing.
    """
    log.info("Fetching full defaulted-loan history …")
    defaulted = _fetch_all_pages("/loans/status/defaulted")
    log.info("  → %d defaulted loan(s) fetched", len(defaulted))

    by_borrower: dict[str, dict] = defaultdict(_new_borrower_entry)
    for l in defaulted:
        borrower = l.get("borrower")
        if not borrower:
            continue
        p_usd, c_usd = _loan_usd_values(l)
        cmint = l.get("collateralMint") or _mint_from_asset(l.get("collateral", {}))

        entry = by_borrower[borrower]
        entry["count"] += 1
        entry["principal_usd"] += p_usd
        entry["collateral_usd"] += c_usd
        entry["collateral_mints"][cmint] += 1

    return by_borrower


def compute_late_repayer_stats() -> dict[str, dict]:
    """
    Per-borrower stats from REPAID loans that closed after their expiredAt
    (the original lender chose not to enforce the expiry) AND where
    collateral value individually exceeded principal at the time — i.e.
    loans where the lender's downside was fully covered by collateral
    regardless of the late repayment. Loans failing either condition are
    not counted; a borrower with no qualifying loan contributes nothing here.
    """
    log.info("Fetching full repaid-loan history …")
    repaid = _fetch_all_pages("/loans/status/repaid")
    log.info("  → %d repaid loan(s) fetched", len(repaid))

    by_borrower: dict[str, dict] = defaultdict(_new_borrower_entry)
    for l in repaid:
        borrower = l.get("borrower")
        if not borrower:
            continue
        try:
            expired = datetime.fromisoformat(l["expiredAt"].replace("Z", "+00:00"))
            updated = datetime.fromisoformat(l["updatedAt"].replace("Z", "+00:00"))
        except (KeyError, ValueError):
            continue
        if updated <= expired:
            continue  # repaid on time — no signal to capture here

        p_usd, c_usd = _loan_usd_values(l)
        if c_usd <= p_usd:
            continue  # late, but collateral didn't cover principal — not a fully-covered position
        cmint = l.get("collateralMint") or _mint_from_asset(l.get("collateral", {}))

        entry = by_borrower[borrower]
        entry["count"] += 1
        entry["principal_usd"] += p_usd
        entry["collateral_usd"] += c_usd
        entry["collateral_mints"][cmint] += 1

    return by_borrower


def merge_target_borrowers(
    defaulted_stats: dict[str, dict], late_stats: dict[str, dict], min_surplus: float
) -> list[dict]:
    """Combine defaulted + late-repayer stats per borrower, filter by total surplus, sort descending."""
    all_borrowers = set(defaulted_stats) | set(late_stats)
    targets = []
    for borrower in all_borrowers:
        d = defaulted_stats.get(borrower, _new_borrower_entry())
        r = late_stats.get(borrower, _new_borrower_entry())
        principal_usd = d["principal_usd"] + r["principal_usd"]
        collateral_usd = d["collateral_usd"] + r["collateral_usd"]
        surplus = collateral_usd - principal_usd
        if surplus <= min_surplus:
            continue

        mints: dict[str, int] = defaultdict(int)
        for mint, n in d["collateral_mints"].items():
            mints[mint] += n
        for mint, n in r["collateral_mints"].items():
            mints[mint] += n
        main_mint = max(mints, key=mints.get)

        targets.append({
            "borrower": borrower,
            "defaults": d["count"],
            "late_repays": r["count"],
            "principal_usd": principal_usd,
            "collateral_usd": collateral_usd,
            "surplus_usd": surplus,
            "main_collateral_mint": main_mint,
        })

    targets.sort(key=lambda t: -t["surplus_usd"])
    return targets


def find_newly_overdue_first_timers(
    all_active_loans: list[dict], known_addrs: set[str], now: datetime
) -> dict[str, list[dict]]:
    """
    Borrowers with an active loan already past its expiredAt right now, who
    have NO resolved default/late-repay history yet (so compute_defaulted_stats/
    compute_late_repayer_stats can't see them at all). A borrower's first-ever
    loan sitting overdue is itself a signal worth tracking, even before we know
    how it resolves — this is what surfaces cases like a first-time borrower
    stuck at 96h overdue that the historical-surplus watchlist has no way to
    catch on its own.
    """
    by_addr: dict[str, list[dict]] = defaultdict(list)
    for l in all_active_loans:
        borrower = l.get("borrower")
        if not borrower or borrower in known_addrs:
            continue
        try:
            expired_at = datetime.fromisoformat(l["expiredAt"].replace("Z", "+00:00"))
        except (KeyError, ValueError):
            continue
        if expired_at <= now:
            by_addr[borrower].append(l)
    return by_addr


def load_defaulter_config() -> dict:
    if DEFAULTER_CONFIG_PATH.exists():
        return yaml.safe_load(DEFAULTER_CONFIG_PATH.read_text()) or {}
    return {}


def save_defaulter_config(config: dict) -> None:
    header = (
        "# Personal defaulter/late-payer watchlist — auto-maintained by defaulter_watch.py.\n"
        "# NEVER commit this file (see .gitignore) — it's a private risk-tracking ledger, not\n"
        "# something to publish alongside the public strategy code.\n"
        "#\n"
        "# Entries are upserted every run for whichever borrowers currently qualify (proven\n"
        "# historical surplus, or a first-ever loan sitting overdue with no history yet).\n"
        "# first_seen is preserved across runs. Borrowers who don't qualify on a given run are\n"
        "# left untouched rather than removed, so last_updated/currently_overdue can go stale —\n"
        "# this file is a permanent ledger, not a live status board.\n\n"
    )
    DEFAULTER_CONFIG_PATH.write_text(header + yaml.safe_dump(config, sort_keys=True, default_flow_style=False))


def upsert_defaulter_config(
    config: dict,
    targets: list[dict],
    active_loans_by_addr: dict[str, list[dict]],
    newly_overdue_by_addr: dict[str, list[dict]],
    now: datetime,
) -> int:
    """Upsert config entries for every borrower qualifying this run. Returns count of brand-new entries."""
    now_iso = now.isoformat()
    new_count = 0

    for t in targets:
        borrower = t["borrower"]
        existing = config.get(borrower, {})
        if not existing:
            new_count += 1
        is_overdue = False
        for l in active_loans_by_addr.get(borrower, []):
            try:
                expired_at = datetime.fromisoformat(l["expiredAt"].replace("Z", "+00:00"))
            except (KeyError, ValueError):
                continue
            if expired_at <= now:
                is_overdue = True
                break
        config[borrower] = {
            "first_seen": existing.get("first_seen", now_iso),
            "last_updated": now_iso,
            "defaults": t["defaults"],
            "late_repayments": t["late_repays"],
            "principal_usd": round(t["principal_usd"], 2),
            "collateral_usd": round(t["collateral_usd"], 2),
            "surplus_usd": round(t["surplus_usd"], 2),
            "main_collateral": t["main_collateral_mint"],
            "currently_overdue": is_overdue,
            "source": "historical",
        }

    for borrower, loans in newly_overdue_by_addr.items():
        if borrower in config and config[borrower].get("source") == "historical":
            continue  # already tracked with real history — don't downgrade the entry
        existing = config.get(borrower, {})
        if not existing:
            new_count += 1
        principal_usd = sum((l.get("metadata") or {}).get("startPrincipalAmountUsd") or 0 for l in loans)
        collateral_usd = sum((l.get("metadata") or {}).get("startCollateralAmountUsd") or 0 for l in loans)
        cmint = loans[0].get("collateralMint") or _mint_from_asset(loans[0].get("collateral", {}))
        config[borrower] = {
            "first_seen": existing.get("first_seen", now_iso),
            "last_updated": now_iso,
            "defaults": 0,
            "late_repayments": 0,
            "principal_usd": round(principal_usd, 2),
            "collateral_usd": round(collateral_usd, 2),
            "surplus_usd": 0.0,
            "main_collateral": cmint,
            "currently_overdue": True,
            "source": "currently_overdue_first_time",
        }

    return new_count


def fetch_open_borrow_offers(target_addrs: set[str]) -> dict[str, list[dict]]:
    """Open (active/partiallyFilled) borrow offers from any of the target addresses."""
    log.info("Fetching open borrow offers market-wide …")
    raw: list[dict] = []
    for status in ("active", "partiallyFilled"):
        raw += _fetch_all_pages(
            "/offers",
            {
                "offerType": "borrowing", "status": status, "hideExpired": "true",
                "showUnverified": "true", "includeUnderfunded": "true",
            },
        )
    log.info("  → %d open borrow offer(s) market-wide", len(raw))

    by_addr: dict[str, list[dict]] = defaultdict(list)
    for o in raw:
        creator = o.get("creator")
        if creator in target_addrs:
            by_addr[creator].append(o)
    return by_addr


def fetch_all_active_loans() -> list[dict]:
    log.info("Fetching active loans market-wide …")
    raw = _fetch_all_pages("/loans/status/active")
    log.info("  → %d active loan(s) market-wide", len(raw))
    return raw


def group_active_loans_by_borrower(all_active_loans: list[dict], target_addrs: set[str]) -> dict[str, list[dict]]:
    """Currently active loans where the borrower is one of the target addresses."""
    by_addr: dict[str, list[dict]] = defaultdict(list)
    for l in all_active_loans:
        borrower = l.get("borrower")
        if borrower in target_addrs:
            by_addr[borrower].append(l)
    return by_addr

# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def print_report(
    targets: list[dict],
    open_offers_by_addr: dict[str, list[dict]],
    active_loans_by_addr: dict[str, list[dict]],
    newly_overdue_by_addr: dict[str, list[dict]],
    top: int,
) -> bool:
    """Prints the report. Returns True if anything is actionable right now."""
    now = datetime.now(timezone.utc)
    actionable = False

    log.info("")
    log.info("=" * 100)
    log.info("ACTIONABLE — open borrow requests from watchlisted borrowers")
    log.info("=" * 100)
    any_open = False
    for t in targets:
        offers = open_offers_by_addr.get(t["borrower"], [])
        for o in offers:
            any_open = True
            actionable = True
            meta = o.get("metadata") or {}
            pmint = o.get("principalMint") or _mint_from_asset(o.get("principal", {}))
            cmint = o.get("collateralMint") or _mint_from_asset(o.get("collateral", {}))
            p_usd = meta.get("principalAmountUsd")
            c_usd = meta.get("collateralAmountUsd")
            log.info("")
            log.info("  borrower        : %s  (%.0f defaults, %.0f late repayments, $%.2f known surplus)",
                      t["borrower"], t["defaults"], t["late_repays"], t["surplus_usd"])
            log.info("  offer           : %s", o.get("pubkey"))
            log.info("  wants to borrow : %s%s", symbol_for(pmint), f"  (~${p_usd:,.2f})" if p_usd else "")
            log.info("  collateral      : %s%s", symbol_for(cmint), f"  (~${c_usd:,.2f})" if c_usd else "")
            log.info("  apy offered     : %.2f%%   duration: %.1fd", o.get("apy", 0) / 100, o.get("duration", 0) / 86400)
            log.info("  status          : %s", o.get("status"))
    if not any_open:
        log.info("  none right now")

    log.info("")
    log.info("=" * 100)
    log.info("ACTIONABLE — watchlisted borrowers with a loan expiring within 24h (or already overdue)")
    log.info("=" * 100)
    any_expiring_soon = False
    for t in targets:
        for l in active_loans_by_addr.get(t["borrower"], []):
            expired_at = datetime.fromisoformat(l["expiredAt"].replace("Z", "+00:00"))
            hrs_left = (expired_at - now).total_seconds() / 3600.0
            if hrs_left > 24:
                continue
            any_expiring_soon = True
            actionable = True
            cmint = l.get("collateralMint") or _mint_from_asset(l.get("collateral", {}))
            meta = l.get("metadata") or {}
            p_usd = meta.get("startPrincipalAmountUsd")
            when = f"overdue by {abs(hrs_left):.1f}h" if hrs_left < 0 else f"expires in {hrs_left:.1f}h"
            log.info("")
            log.info("  borrower        : %s  (%.0f defaults, %.0f late repayments, $%.2f known surplus)",
                      t["borrower"], t["defaults"], t["late_repays"], t["surplus_usd"])
            log.info("  loan            : %s", l.get("pubkey"))
            log.info("  borrowed        : %s against %s", f"${p_usd:,.2f}" if p_usd else "n/a", symbol_for(cmint))
            log.info("  current lender  : %s", l.get("lender"))
            log.info("  status          : %s", when)
    if not any_expiring_soon:
        log.info("  none right now")

    log.info("")
    log.info("=" * 100)
    log.info("Currently active loans by watchlisted borrowers (watch expiry — they may reborrow)")
    log.info("=" * 100)
    any_active = False
    for t in targets:
        loans = active_loans_by_addr.get(t["borrower"], [])
        for l in loans:
            any_active = True
            cmint = l.get("collateralMint") or _mint_from_asset(l.get("collateral", {}))
            expired_at = datetime.fromisoformat(l["expiredAt"].replace("Z", "+00:00"))
            hrs_left = (expired_at - now).total_seconds() / 3600.0
            meta = l.get("metadata") or {}
            p_usd = meta.get("startPrincipalAmountUsd")
            log.info(
                "  %s  borrowed %s against %s  |  lender=%s  |  expires in %.1fh",
                t["borrower"], f"${p_usd:,.2f}" if p_usd else "n/a", symbol_for(cmint),
                l.get("lender"), hrs_left,
            )
    if not any_active:
        log.info("  none right now")

    log.info("")
    log.info("=" * 100)
    log.info("NEWLY OBSERVED — first-time borrowers with a loan already overdue, no prior history yet")
    log.info("=" * 100)
    if not newly_overdue_by_addr:
        log.info("  none right now")
    else:
        for borrower, loans in newly_overdue_by_addr.items():
            for l in loans:
                expired_at = datetime.fromisoformat(l["expiredAt"].replace("Z", "+00:00"))
                hrs_late = (now - expired_at).total_seconds() / 3600.0
                cmint = l.get("collateralMint") or _mint_from_asset(l.get("collateral", {}))
                meta = l.get("metadata") or {}
                p_usd = meta.get("startPrincipalAmountUsd")
                log.info("")
                log.info("  borrower        : %s  (no resolved history — first loan overdue)", borrower)
                log.info("  loan            : %s", l.get("pubkey"))
                log.info("  borrowed        : %s against %s", f"${p_usd:,.2f}" if p_usd else "n/a", symbol_for(cmint))
                log.info("  current lender  : %s", l.get("lender"))
                log.info("  overdue by      : %.1fh", hrs_late)
                log.info("  -> added to defaulter_config.yaml for future tracking")

    log.info("")
    log.info("=" * 100)
    log.info("Full watchlist (top %d by historical surplus)", top)
    log.info("=" * 100)
    col = "{:<45}  {:>9}  {:>12}  {:>14}  {:>14}  {:>12}  {}"
    log.info(col.format("borrower", "defaults", "late repays", "principal $", "collateral $", "surplus $", "main collateral"))
    log.info("-" * 135)
    for t in targets[:top]:
        log.info(col.format(
            t["borrower"], t["defaults"], t["late_repays"],
            f"{t['principal_usd']:,.2f}", f"{t['collateral_usd']:,.2f}", f"{t['surplus_usd']:,.2f}",
            symbol_for(t["main_collateral_mint"]),
        ))
    log.info("=" * 100)

    return actionable

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Watch for borrow requests from Offerbook borrowers whose past defaults or late "
                    "repayments were fully covered by collateral value."
    )
    parser.add_argument(
        "--min-surplus", type=float, default=0.0,
        help="Only watch borrowers with total historical surplus (collateral - principal) above this USD amount (default: 0 — any positive surplus)",
    )
    parser.add_argument(
        "--top", type=int, default=30,
        help="How many rows to show in the full reference watchlist table (default: 30)",
    )
    args = parser.parse_args()

    defaulted_stats = compute_defaulted_stats()
    late_stats = compute_late_repayer_stats()
    targets = merge_target_borrowers(defaulted_stats, late_stats, args.min_surplus)
    log.info("Watchlist: %d borrower(s) with > $%.2f historical surplus", len(targets), args.min_surplus)

    target_addrs = {t["borrower"] for t in targets}
    all_active_loans = fetch_all_active_loans()
    active_loans_by_addr = group_active_loans_by_borrower(all_active_loans, target_addrs)

    now = datetime.now(timezone.utc)
    newly_overdue_by_addr = find_newly_overdue_first_timers(all_active_loans, target_addrs, now)

    open_offers_by_addr = fetch_open_borrow_offers(target_addrs) if target_addrs else {}

    config = load_defaulter_config()
    new_count = upsert_defaulter_config(config, targets, active_loans_by_addr, newly_overdue_by_addr, now)
    save_defaulter_config(config)
    log.info(
        "defaulter_config.yaml: %d borrower(s) tracked total (%d new this run)",
        len(config), new_count,
    )

    if not targets and not newly_overdue_by_addr:
        log.info("Nothing to watch — no historical surplus and no first-time overdue borrower right now.")
        sys.exit(0)

    actionable = print_report(targets, open_offers_by_addr, active_loans_by_addr, newly_overdue_by_addr, args.top)
    sys.exit(1 if actionable else 0)


if __name__ == "__main__":
    main()
