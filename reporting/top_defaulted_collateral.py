"""
Offerbook Top Defaulted Collateral
==================================
Platform-wide, all-time: which collateral TOKENS have the most USD value
behind DEFAULTED loans — every lender, not just our own wallets. Ranked by
total USD lost at default-time mark-to-market per collateral mint (not
default count — a token defaulted on twice at $5,000 ranks above one
defaulted on 50 times at $50 a pop, since the question here is "where's
the biggest dollar exposure", not "which collateral keeps failing"; see
pnl_leaderboard.py/seizure_outcome_scan.py for more dollar-denominated
views).

Prints a ranked table (symbol, total USD lost at default-time mark-to-
market, default count) followed by the plain comma-separated symbol list
on its own line, for scripting/paste-elsewhere use.

Read-only: never signs or submits anything.

Usage:
  python top_defaulted_collateral.py              # top 20
  python top_defaulted_collateral.py --top 50
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from collections import Counter, defaultdict

import requests
from dotenv import load_dotenv

load_dotenv()

# Shared modules live in ../lib — see README's repo-layout note.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))

import offerbook_common as _common

API_BASE = os.getenv("OFFERBOOK_API_BASE", "https://api.offerbook.jup.ag/api/v1")
PAGE_SIZE = 100

JUPITER_TOKEN_SEARCH_API = "https://api.jup.ag/tokens/v2/search"
JUPITER_SEARCH_BATCH_SIZE = 50

KNOWN_SYMBOLS = _common.KNOWN_SYMBOLS
_RESOLVED_SYMBOLS: dict[str, str] = {}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("top_defaulted_collateral")

SESSION = requests.Session()


def _fetch_all_pages(endpoint: str, params: dict | None = None) -> list[dict]:
    return _common.fetch_all_pages(SESSION, API_BASE, endpoint, params, PAGE_SIZE, sleep_secs=0.1)


def fetch_defaulted_loans() -> list[dict]:
    return _fetch_all_pages("/loans/status/defaulted")


def resolve_collateral_symbols(mints: set[str]) -> None:
    """Look up real symbols (via Jupiter's token search API) for every mint
    not already in KNOWN_SYMBOLS — same approach address_snapshot.py/
    borrower_loan_timeline.py/update_config.py use, since Jupiter indexes
    far more of these than Offerbook's own registry."""
    unresolved = sorted(m for m in mints if m and m not in KNOWN_SYMBOLS and m not in _RESOLVED_SYMBOLS)
    if not unresolved:
        return
    for i in range(0, len(unresolved), JUPITER_SEARCH_BATCH_SIZE):
        chunk = unresolved[i : i + JUPITER_SEARCH_BATCH_SIZE]
        try:
            resp = SESSION.get(JUPITER_TOKEN_SEARCH_API, params={"query": ",".join(chunk)}, timeout=15)
            resp.raise_for_status()
            for t in resp.json():
                mint, symbol = t.get("id"), t.get("symbol")
                if mint and symbol:
                    _RESOLVED_SYMBOLS[mint] = symbol
        except Exception as exc:
            log.warning("Jupiter token symbol lookup failed for a batch: %s", exc)


def symbol_for(mint: str) -> str:
    if mint in KNOWN_SYMBOLS:
        return KNOWN_SYMBOLS[mint]
    if mint in _RESOLVED_SYMBOLS:
        return _RESOLVED_SYMBOLS[mint]
    return mint[:6] + "…" if mint else "?"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--top", type=int, default=20, help="How many collateral types to show (default 20).")
    args = parser.parse_args()

    log.info("Fetching defaulted loans platform-wide…")
    loans = fetch_defaulted_loans()
    log.info("  → %d defaulted loan(s)", len(loans))

    counts: Counter[str] = Counter()
    usd_lost: defaultdict[str, float] = defaultdict(float)
    for l in loans:
        cmint = l.get("collateralMint") or _common._mint_from_asset(l.get("collateral", {}))
        if not cmint:
            continue
        counts[cmint] += 1
        meta = l.get("metadata") or {}
        usd_lost[cmint] += meta.get("endCollateralAmountUsd") or meta.get("startCollateralAmountUsd") or 0.0

    resolve_collateral_symbols(set(counts))

    ranked = sorted(usd_lost.items(), key=lambda kv: -kv[1])[: args.top]

    log.info("")
    log.info("Top %d defaulted collateral (by USD lost):", len(ranked))
    col = "{:<10}{:>16}{:>10}"
    log.info(col.format("collat", "collateral $", "defaults"))
    for mint, usd in ranked:
        log.info(col.format(symbol_for(mint), f"${usd:,.2f}", counts[mint]))

    log.info("")
    log.info(", ".join(symbol_for(mint) for mint, _ in ranked))


if __name__ == "__main__":
    main()
