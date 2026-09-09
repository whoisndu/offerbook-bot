"""
Offerbook Lender Capital Scanner
================================
Reports the total USDC balance (wallet + escrow) of every lender who is or
has ever been one on the platform — "lender" means anyone who:

  - has an active loan out right now (given a loan, not yet resolved),
  - has an open lending offer right now (any status: active or partiallyFilled), or
  - appears as the lender on ANY resolved loan (repaid or defaulted) —
    a past lender is included even with no current activity at all.

For each, reports wallet USDC, escrow USDC, and the combined idle total — the
free/redeployable capital a competitor could bring to bear, not just what's
already committed — plus their currently-OUTSTANDING borrowed principal
(summed from active loans only) and their own utilization rate (borrowed /
(borrowed + idle)), and a "last seen" column: the most recent createdAt/
updatedAt across all of their Offerbook loan/offer records, i.e. purely
platform activity, not general wallet activity elsewhere.

The summary footer also reports a PROTOCOL UTILIZATION figure — total
borrowed / total capital (borrowed + idle) across every lender in the report
(after --min-total/staleness filtering, before --top truncates the printed
table) — the single-number answer to "how much of the protocol's lending
capital is currently deployed vs. sitting idle."

Also tracks each lender's all-time loan count (active + repaid + defaulted
loans where they're the lender — open offers that haven't been matched yet
don't count) and reports how many new loans they've opened since the
previous run, alongside the balance change.

Every run's balances are persisted to lender_capital_state.json (gitignored —
like defaulter_config.yaml/tg_watchlist.json, this reveals your own
competitive-intelligence tracking, so it's kept private) and compared against
the previous run, so each report also shows the change since last time.

Read-only: never signs or submits anything.

Usage:
  python lender_capital_scan.py                  # all lenders, sorted by total desc
  python lender_capital_scan.py --min-total 100   # only show lenders with > $100 total
  python lender_capital_scan.py --top 20          # limit output to the top 20 rows
  python lender_capital_scan.py --no-save         # compare against saved state but don't overwrite it

Exit codes:
  0 — always (informational script, not a pass/fail check)
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

# Shared modules live in ../lib — see README's repo-layout note.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))

import offerbook_common as _common

load_dotenv()

API_BASE = os.getenv("OFFERBOOK_API_BASE", "https://api.offerbook.jup.ag/api/v1")
SOLANA_RPC = os.getenv("SOLANA_RPC", "https://api.mainnet-beta.solana.com")
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
PAGE_SIZE = 100
MAX_WORKERS = 10

# Exclude lenders inactive longer than this AND with no funds currently sitting
# in escrow — a lender with escrow funds could fill an offer at any time
# regardless of how long since their last on-chain activity, so only the
# combination of "stale" and "no dry powder in escrow" is excluded.
INACTIVE_DAYS_THRESHOLD = 14

STATE_PATH = Path(__file__).parent / "lender_capital_state.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("lender_capital_scan")

SESSION = requests.Session()


def _fetch_all_pages(endpoint: str, params: dict | None = None) -> list[dict]:
    return _common.fetch_all_pages(SESSION, API_BASE, endpoint, params, PAGE_SIZE, sleep_secs=0.1)


def _parse_ts(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def _note_last_seen(last_seen: dict[str, datetime], addr: str, record: dict) -> None:
    """Track the most recent createdAt/updatedAt seen for this address across any record."""
    for field in ("updatedAt", "createdAt"):
        ts = _parse_ts(record.get(field))
        if ts is not None and (addr not in last_seen or ts > last_seen[addr]):
            last_seen[addr] = ts


def fetch_all_lenders() -> tuple[set[str], dict[str, datetime], dict[str, int], dict[str, float]]:
    """Union of everyone who is or has ever been a lender: active-loan lenders,
    open-lending-offer creators, and lenders from ALL resolved loan history
    (repaid + defaulted) — so a past lender shows up even if they currently have
    no active loan or open offer at all. Also returns each lender's most recent
    createdAt/updatedAt across all of those records, as a "last seen" signal,
    their all-time loan count (active + repaid + defaulted only — open
    offers aren't loans yet), and their currently-OUTSTANDING borrowed
    principal (USD) — summed only from active loans, since that's capital
    presently out with a borrower, not yet returned — all computed for free
    from data already being fetched, no extra API calls."""
    last_seen: dict[str, datetime] = {}
    loan_counts: Counter[str] = Counter()
    borrowed_usd: dict[str, float] = Counter()

    log.info("Fetching active loans …")
    active_loans = _fetch_all_pages("/loans/status/active")
    log.info("  → %d active loan(s)", len(active_loans))
    lenders = {l["lender"] for l in active_loans if l.get("lender")}
    for l in active_loans:
        if l.get("lender"):
            _note_last_seen(last_seen, l["lender"], l)
            loan_counts[l["lender"]] += 1
            borrowed_usd[l["lender"]] += (l.get("principalAmount") or 0) / 1e6

    log.info("Fetching open lending offers …")
    open_offers: list[dict] = []
    for status in ("active", "partiallyFilled"):
        open_offers += _fetch_all_pages(
            "/offers",
            {"offerType": "lending", "status": status, "includeUnderfunded": "true", "showUnverified": "true"},
        )
    log.info("  → %d open lending offer(s)", len(open_offers))
    lenders |= {o["creator"] for o in open_offers if o.get("creator")}
    for o in open_offers:
        if o.get("creator"):
            _note_last_seen(last_seen, o["creator"], o)

    log.info("Fetching repaid-loan history …")
    repaid = _fetch_all_pages("/loans/status/repaid")
    log.info("  → %d repaid loan(s)", len(repaid))
    lenders |= {l["lender"] for l in repaid if l.get("lender")}
    for l in repaid:
        if l.get("lender"):
            _note_last_seen(last_seen, l["lender"], l)
            loan_counts[l["lender"]] += 1

    log.info("Fetching defaulted-loan history …")
    defaulted = _fetch_all_pages("/loans/status/defaulted")
    log.info("  → %d defaulted loan(s)", len(defaulted))
    lenders |= {l["lender"] for l in defaulted if l.get("lender")}
    for l in defaulted:
        if l.get("lender"):
            _note_last_seen(last_seen, l["lender"], l)
            loan_counts[l["lender"]] += 1

    return lenders, last_seen, dict(loan_counts), dict(borrowed_usd)


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
    loan_count: int
    borrowed_usd: float = 0.0  # outstanding principal currently out on active loans

    @property
    def total_usd(self) -> float:
        """Idle, redeployable capital — wallet + escrow. Does NOT include
        borrowed_usd, which is already out with a borrower, not free to deploy."""
        return self.wallet_usd + self.escrow_usd

    @property
    def capital_usd(self) -> float:
        """Idle + currently-deployed capital combined — the denominator for
        this lender's own utilization rate."""
        return self.total_usd + self.borrowed_usd

    @property
    def utilization(self) -> float | None:
        """Fraction of this lender's total capital (idle + deployed) currently
        out on active loans. None if they have zero capital of either kind."""
        if self.capital_usd <= 0:
            return None
        return self.borrowed_usd / self.capital_usd


def load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return {}


def save_state(balances: list[LenderBalance], previous: dict) -> None:
    now_iso = datetime.now(timezone.utc).isoformat()
    state = dict(previous)  # keep entries for lenders not in this run (rare, but don't lose history)
    for b in balances:
        state[b.lender] = {
            "wallet_usd": b.wallet_usd,
            "escrow_usd": b.escrow_usd,
            "total_usd": b.total_usd,
            "borrowed_usd": b.borrowed_usd,
            "loan_count": b.loan_count,
            "last_updated": now_iso,
        }
    STATE_PATH.write_text(json.dumps(state, indent=2, sort_keys=True))


def scan_balances(lenders: set[str], loan_counts: dict[str, int], borrowed_usd: dict[str, float]) -> list[LenderBalance]:
    def fetch_one(lender: str) -> LenderBalance:
        return LenderBalance(
            lender, wallet_usdc(lender), escrow_usdc(lender),
            loan_counts.get(lender, 0), borrowed_usd.get(lender, 0.0),
        )

    log.info("Fetching wallet + escrow USDC balances for %d lender(s) …", len(lenders))
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        return list(ex.map(fetch_one, lenders))


def format_last_seen(ts: datetime | None) -> str:
    """Relative time since ts, e.g. '2.3h ago' / '5.1d ago' — or 'unknown' if we
    never saw a createdAt/updatedAt for this address in any Offerbook record."""
    if ts is None:
        return "unknown"
    hours = (datetime.now(timezone.utc) - ts).total_seconds() / 3600
    if hours < 48:
        return f"{hours:.1f}h ago"
    return f"{hours / 24:.1f}d ago"


def _is_stale(b: LenderBalance, last_seen: dict[str, datetime]) -> bool:
    """True if inactive for more than INACTIVE_DAYS_THRESHOLD days (or never
    seen at all) AND currently holding no escrow balance (dust below a cent
    doesn't count as "funds in escrow" — it wouldn't back a real offer)."""
    if b.escrow_usd >= 0.01:
        return False
    ts = last_seen.get(b.lender)
    if ts is None:
        return True
    days_inactive = (datetime.now(timezone.utc) - ts).total_seconds() / 86400
    return days_inactive > INACTIVE_DAYS_THRESHOLD


def print_report(
    balances: list[LenderBalance], previous: dict, last_seen: dict[str, datetime], min_total: float, top: int
) -> None:
    balances = [b for b in balances if b.total_usd > min_total]
    stale_count = sum(1 for b in balances if _is_stale(b, last_seen))
    if stale_count:
        log.info("Excluding %d lender(s) inactive > %d days with no escrow balance",
                  stale_count, INACTIVE_DAYS_THRESHOLD)
    balances = [b for b in balances if not _is_stale(b, last_seen)]
    balances.sort(key=lambda b: -b.total_usd)

    if previous:
        last_updated = next(iter(previous.values())).get("last_updated", "unknown")
        log.info("Comparing against previous run: %s", last_updated)
    else:
        log.info("No previous state found (first run) — every lender will show as NEW.")

    log.info("")
    log.info("=" * 100)
    log.info("Lender capital (wallet + escrow USDC) — everyone who is or has ever been a lender")
    log.info("=" * 100)
    col = "{:<46}  {:>14}  {:>14}  {:>14}  {:>14}  {:>8}  {:>14}  {:>11}  {:>14}"
    log.info(col.format(
        "lender", "wallet $", "escrow $", "idle $", "borrowed $", "util %",
        "Δ since last", "new loans", "last seen",
    ))
    log.info("-" * 155)
    for b in balances[:top]:
        prev = previous.get(b.lender)
        if prev is None:
            delta_str = "NEW"
            loan_delta_str = "NEW"
        else:
            delta = b.total_usd - prev.get("total_usd", 0.0)
            delta_str = f"{delta:+,.2f}"
            loan_delta_str = str(b.loan_count - prev.get("loan_count", 0))
        seen_str = format_last_seen(last_seen.get(b.lender))
        util_str = f"{b.utilization * 100:.1f}%" if b.utilization is not None else "n/a"
        log.info(col.format(
            b.lender, f"{b.wallet_usd:,.2f}", f"{b.escrow_usd:,.2f}", f"{b.total_usd:,.2f}",
            f"{b.borrowed_usd:,.2f}", util_str, delta_str, loan_delta_str, seen_str,
        ))
    log.info("-" * 155)

    total_wallet = sum(b.wallet_usd for b in balances)
    total_escrow = sum(b.escrow_usd for b in balances)
    total_borrowed = sum(b.borrowed_usd for b in balances)
    grand_total = total_wallet + total_escrow
    protocol_capital = grand_total + total_borrowed
    protocol_utilization = (total_borrowed / protocol_capital) if protocol_capital > 0 else None
    # Change for this same filtered set of lenders (not an independently-filtered
    # previous total, which could mismatch if --min-total excludes different lenders
    # than it did last run).
    change = sum(b.total_usd - previous.get(b.lender, {}).get("total_usd", 0.0) for b in balances)
    log.info("")
    log.info("Lenders shown        : %d", len(balances[:top]))
    log.info("Total wallet USDC    : $%s", f"{total_wallet:,.2f}")
    log.info("Total escrow USDC    : $%s", f"{total_escrow:,.2f}")
    log.info("Total idle USDC      : $%s", f"{grand_total:,.2f}")
    log.info("Total borrowed USDC  : $%s", f"{total_borrowed:,.2f}")
    log.info("Total capital        : $%s  (idle + borrowed)", f"{protocol_capital:,.2f}")
    log.info(
        "PROTOCOL UTILIZATION : %s",
        f"{protocol_utilization * 100:.1f}%" if protocol_utilization is not None else "n/a",
    )
    if previous:
        log.info("Change since last run: $%s", f"{change:+,.2f}")
    log.info("=" * 100)


def main() -> None:
    parser = argparse.ArgumentParser(description="Report wallet+escrow USDC balances for every current Offerbook lender.")
    parser.add_argument("--min-total", type=float, default=0.0, help="Only show lenders with > this much total USDC (default: 0)")
    parser.add_argument("--top", type=int, default=1000, help="Limit the report to the top N rows by total (default: 1000, effectively all)")
    parser.add_argument("--no-save", action="store_true", help="Compare against saved state but don't overwrite it with this run's results")
    args = parser.parse_args()

    previous = load_state()

    lenders, last_seen, loan_counts, borrowed_usd = fetch_all_lenders()
    log.info("Distinct lenders (active loan, open offer, or resolved loan history): %d", len(lenders))
    balances = scan_balances(lenders, loan_counts, borrowed_usd)
    print_report(balances, previous, last_seen, args.min_total, args.top)

    if not args.no_save:
        save_state(balances, previous)


if __name__ == "__main__":
    main()
