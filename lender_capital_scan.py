"""
Offerbook Lender Capital Scanner
================================
Reports the total USDC balance (wallet + escrow) of every lender who is or
has ever been one on the platform — "lender" means anyone who:

  - has an active loan out right now (given a loan, not yet resolved),
  - has an open lending offer right now (any status: active or partiallyFilled), or
  - appears as the lender on ANY resolved loan (repaid or defaulted) —
    a past lender is included even with no current activity at all.

For each, reports wallet USDC, escrow USDC, and the combined total — the
free/redeployable capital a competitor could bring to bear, not just what's
already committed.

Read-only: never signs or submits anything.

Usage:
  python lender_capital_scan.py                  # all lenders, sorted by total desc
  python lender_capital_scan.py --min-total 100   # only show lenders with > $100 total
  python lender_capital_scan.py --top 20          # limit output to the top 20 rows

Exit codes:
  0 — always (informational script, not a pass/fail check)
"""
from __future__ import annotations

import argparse
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import requests
from dotenv import load_dotenv

load_dotenv()

API_BASE = os.getenv("OFFERBOOK_API_BASE", "https://api.offerbook.jup.ag/api/v1")
SOLANA_RPC = os.getenv("SOLANA_RPC", "https://api.mainnet-beta.solana.com")
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
PAGE_SIZE = 100
MAX_WORKERS = 10

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("lender_capital_scan")

SESSION = requests.Session()


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
        time.sleep(0.1)
    return items


def fetch_all_lenders() -> set[str]:
    """Union of everyone who is or has ever been a lender: active-loan lenders,
    open-lending-offer creators, and lenders from ALL resolved loan history
    (repaid + defaulted) — so a past lender shows up even if they currently have
    no active loan or open offer at all."""
    log.info("Fetching active loans …")
    active_loans = _fetch_all_pages("/loans/status/active")
    log.info("  → %d active loan(s)", len(active_loans))
    lenders = {l["lender"] for l in active_loans if l.get("lender")}

    log.info("Fetching open lending offers …")
    open_offers: list[dict] = []
    for status in ("active", "partiallyFilled"):
        open_offers += _fetch_all_pages(
            "/offers",
            {"offerType": "lending", "status": status, "includeUnderfunded": "true", "showUnverified": "true"},
        )
    log.info("  → %d open lending offer(s)", len(open_offers))
    lenders |= {o["creator"] for o in open_offers if o.get("creator")}

    log.info("Fetching repaid-loan history …")
    repaid = _fetch_all_pages("/loans/status/repaid")
    log.info("  → %d repaid loan(s)", len(repaid))
    lenders |= {l["lender"] for l in repaid if l.get("lender")}

    log.info("Fetching defaulted-loan history …")
    defaulted = _fetch_all_pages("/loans/status/defaulted")
    log.info("  → %d defaulted loan(s)", len(defaulted))
    lenders |= {l["lender"] for l in defaulted if l.get("lender")}

    return lenders


def wallet_usdc(wallet: str) -> float:
    try:
        resp = SESSION.post(SOLANA_RPC, json={
            "jsonrpc": "2.0", "id": 1, "method": "getTokenAccountsByOwner",
            "params": [wallet, {"mint": USDC_MINT}, {"encoding": "jsonParsed"}],
        }, timeout=20)
        accounts = resp.json().get("result", {}).get("value", [])
        return sum(int(a["account"]["data"]["parsed"]["info"]["tokenAmount"]["amount"]) for a in accounts) / 1e6
    except Exception:
        return 0.0


def escrow_usdc(wallet: str) -> float:
    try:
        resp = SESSION.get(f"{API_BASE}/escrows/holdings/{wallet}", timeout=20)
        for holding in resp.json():
            if holding.get("asset", {}).get("mint") == USDC_MINT:
                return holding["amount"] / 1e6
        return 0.0
    except Exception:
        return 0.0


@dataclass
class LenderBalance:
    lender: str
    wallet_usd: float
    escrow_usd: float

    @property
    def total_usd(self) -> float:
        return self.wallet_usd + self.escrow_usd


def scan_balances(lenders: set[str]) -> list[LenderBalance]:
    def fetch_one(lender: str) -> LenderBalance:
        return LenderBalance(lender, wallet_usdc(lender), escrow_usdc(lender))

    log.info("Fetching wallet + escrow USDC balances for %d lender(s) …", len(lenders))
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        return list(ex.map(fetch_one, lenders))


def print_report(balances: list[LenderBalance], min_total: float, top: int) -> None:
    balances = [b for b in balances if b.total_usd > min_total]
    balances.sort(key=lambda b: -b.total_usd)

    log.info("")
    log.info("=" * 100)
    log.info("Lender capital (wallet + escrow USDC) — everyone who is or has ever been a lender")
    log.info("=" * 100)
    col = "{:<46}  {:>14}  {:>14}  {:>14}"
    log.info(col.format("lender", "wallet $", "escrow $", "total $"))
    log.info("-" * 92)
    for b in balances[:top]:
        log.info(col.format(b.lender, f"{b.wallet_usd:,.2f}", f"{b.escrow_usd:,.2f}", f"{b.total_usd:,.2f}"))
    log.info("-" * 92)

    total_wallet = sum(b.wallet_usd for b in balances)
    total_escrow = sum(b.escrow_usd for b in balances)
    log.info("")
    log.info("Lenders shown        : %d", len(balances[:top]))
    log.info("Total wallet USDC    : $%s", f"{total_wallet:,.2f}")
    log.info("Total escrow USDC    : $%s", f"{total_escrow:,.2f}")
    log.info("GRAND TOTAL USDC     : $%s", f"{total_wallet + total_escrow:,.2f}")
    log.info("=" * 100)


def main() -> None:
    parser = argparse.ArgumentParser(description="Report wallet+escrow USDC balances for every current Offerbook lender.")
    parser.add_argument("--min-total", type=float, default=0.0, help="Only show lenders with > this much total USDC (default: 0)")
    parser.add_argument("--top", type=int, default=1000, help="Limit the report to the top N rows by total (default: 1000, effectively all)")
    args = parser.parse_args()

    lenders = fetch_all_lenders()
    log.info("Distinct lenders (active loan, open offer, or resolved loan history): %d", len(lenders))
    balances = scan_balances(lenders)
    print_report(balances, args.min_total, args.top)


if __name__ == "__main__":
    main()
