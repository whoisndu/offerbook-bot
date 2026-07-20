"""
Offerbook Repeat-Defaulter Watchlist
======================================
Scans the full defaulted-loan history to find borrowers whose collateral has
historically been worth more than the principal they defaulted on ("surplus"
to whichever lender seized it) — i.e. borrowers worth lending to again, since
history suggests a default nets the lender a profit rather than a loss.

For each such borrower, checks whether they currently:
  - have an open borrow request (actionable now — you could fill it directly)
  - have an active loan (watch its expiry — they may come back to reborrow)

Read-only: never signs or submits anything. Meant to be run periodically
(manually, via cron, or the /loop skill) to catch borrow requests from known
repeat defaulters while they're still open.

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

import requests
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

API_BASE = os.getenv("OFFERBOOK_API_BASE", "https://api.offerbook.jup.ag/api/v1")
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
PAGE_SIZE = 100

KNOWN_SYMBOLS: dict[str, str] = {
    "So11111111111111111111111111111111111111112": "SOL",
    "mSoLzYCxHdYgdzU16g5QSh3i5K3z3KZK7ytfqcJm7So": "mSOL",
    "bSo13r4TkiE4KumL71LsHTPpL2euBYLFx6h9HP3piy1": "bSOL",
    "jupSoLaHXQiZZTSfEWMTRRgpnyFm8f6sZdosWBjx93v": "JupSOL",
    "J1toso1uCk3RLmjorhTtrVwY9HJ7X8V9yYac6Y7kGCPn": "jitoSOL",
    "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN": "JUP",
    "4k3Dyjzvzp8eMZWUXbBCjEvwSkkk59S5iCNLY3QrkX6R": "JLP",
    "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263": "BONK",
    "Dz9mQ9NzkBcCsuGPFJ3r1bS4wgqKMHBPiVuniW8Mbonk": "USELESS",
    "Ce2gx9KGXJ6C9Mp5b5x1sn9Mg87JwEbrQby4Zqo3pump": "NEET",
    "98sMhvDwXj1RQi5c5Mndm3vPe9cBqPrbLaufMXFNMh5g": "HYPE",
    "TUNAfXDZEdQizTMTh3uEvNvYqJmqFHZbEJt8joP4cyx": "TUNA",
    USDC_MINT: "USDC",
}

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
    params = dict(params or {})
    params["limit"] = PAGE_SIZE
    params["offset"] = 0
    items: list[dict] = []
    while True:
        resp = SESSION.get(f"{API_BASE}{endpoint}", params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        items.extend(data.get("data", []))
        if not data.get("pagination", {}).get("hasMore", False):
            break
        params["offset"] += PAGE_SIZE
        time.sleep(0.15)
    return items


def _mint_from_asset(asset: dict) -> str:
    return asset.get("mint") or asset.get("data", {}).get("mint") or asset.get("data", {}).get("asset") or ""


def symbol_for(mint: str) -> str:
    if not mint:
        return "NFT"
    if mint in KNOWN_SYMBOLS:
        return KNOWN_SYMBOLS[mint]
    return f"{mint[:6]}…{mint[-4:]}"

# ---------------------------------------------------------------------------
# Watchlist computation
# ---------------------------------------------------------------------------

def compute_target_borrowers(min_surplus: float) -> list[dict]:
    """
    Return borrowers whose total historical (collateral - principal) surplus
    across all their defaulted loans exceeds min_surplus, sorted by surplus
    descending.

    Uses each defaulted loan's USD value AT THE TIME OF DEFAULT (metadata's
    "end" fields, falling back to "start" if a loan closed before end values
    were recorded) — this is a snapshot of historical outcomes, not a live
    repricing, which is fine for a watchlist but shouldn't be treated as a
    live LTV figure.
    """
    log.info("Fetching full defaulted-loan history …")
    defaulted = _fetch_all_pages("/loans/status/defaulted")
    log.info("  → %d defaulted loan(s) fetched", len(defaulted))

    by_borrower: dict[str, dict] = defaultdict(lambda: {
        "count": 0, "principal_usd": 0.0, "collateral_usd": 0.0, "collateral_mints": defaultdict(int),
    })

    for l in defaulted:
        borrower = l.get("borrower")
        if not borrower:
            continue
        meta = l.get("metadata") or {}
        p_usd = meta.get("endPrincipalAmountUsd") or meta.get("startPrincipalAmountUsd") or 0
        c_usd = meta.get("endCollateralAmountUsd") or meta.get("startCollateralAmountUsd") or 0
        cmint = l.get("collateralMint") or _mint_from_asset(l.get("collateral", {}))

        entry = by_borrower[borrower]
        entry["count"] += 1
        entry["principal_usd"] += p_usd
        entry["collateral_usd"] += c_usd
        entry["collateral_mints"][cmint] += 1

    targets = []
    for borrower, s in by_borrower.items():
        surplus = s["collateral_usd"] - s["principal_usd"]
        if surplus > min_surplus:
            main_mint = max(s["collateral_mints"], key=s["collateral_mints"].get)
            targets.append({
                "borrower": borrower,
                "defaults": s["count"],
                "principal_usd": s["principal_usd"],
                "collateral_usd": s["collateral_usd"],
                "surplus_usd": surplus,
                "main_collateral_mint": main_mint,
            })

    targets.sort(key=lambda t: -t["surplus_usd"])
    return targets


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


def fetch_active_loans_as_borrower(target_addrs: set[str]) -> dict[str, list[dict]]:
    """Currently active loans where the borrower is one of the target addresses."""
    log.info("Fetching active loans market-wide …")
    raw = _fetch_all_pages("/loans/status/active")
    log.info("  → %d active loan(s) market-wide", len(raw))

    by_addr: dict[str, list[dict]] = defaultdict(list)
    for l in raw:
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
    top: int,
) -> bool:
    """Prints the report. Returns True if anything is actionable right now."""
    now = datetime.now(timezone.utc)
    actionable = False

    log.info("")
    log.info("=" * 100)
    log.info("ACTIONABLE — open borrow requests from known repeat/profitable defaulters")
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
            log.info("  borrower        : %s  (%.0f historical defaults, $%.2f known surplus)",
                      t["borrower"], t["defaults"], t["surplus_usd"])
            log.info("  offer           : %s", o.get("pubkey"))
            log.info("  wants to borrow : %s%s", symbol_for(pmint), f"  (~${p_usd:,.2f})" if p_usd else "")
            log.info("  collateral      : %s%s", symbol_for(cmint), f"  (~${c_usd:,.2f})" if c_usd else "")
            log.info("  apy offered     : %.2f%%   duration: %.1fd", o.get("apy", 0) / 100, o.get("duration", 0) / 86400)
            log.info("  status          : %s", o.get("status"))
    if not any_open:
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
    log.info("Full watchlist (top %d by historical surplus)", top)
    log.info("=" * 100)
    col = "{:<45}  {:>9}  {:>14}  {:>14}  {:>12}  {}"
    log.info(col.format("borrower", "defaults", "principal $", "collateral $", "surplus $", "main collateral"))
    log.info("-" * 120)
    for t in targets[:top]:
        log.info(col.format(
            t["borrower"], t["defaults"],
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
        description="Watch for borrow requests from Offerbook borrowers whose past defaults netted lenders a surplus."
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

    targets = compute_target_borrowers(args.min_surplus)
    log.info("Watchlist: %d borrower(s) with > $%.2f historical surplus", len(targets), args.min_surplus)

    if not targets:
        log.info("Nothing to watch — no borrower has a positive historical default surplus above the threshold.")
        sys.exit(0)

    target_addrs = {t["borrower"] for t in targets}
    open_offers_by_addr = fetch_open_borrow_offers(target_addrs)
    active_loans_by_addr = fetch_active_loans_as_borrower(target_addrs)

    actionable = print_report(targets, open_offers_by_addr, active_loans_by_addr, args.top)
    sys.exit(1 if actionable else 0)


if __name__ == "__main__":
    main()
