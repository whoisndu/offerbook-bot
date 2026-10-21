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

Prints a ranked table (symbol, TOKEN/NFT, total USD lost at default-time
mark-to-market, default count) followed by the plain comma-separated
symbol list on its own line, for scripting/paste-elsewhere use (symbols
only there — no kind label, so the list stays a clean paste target).
Collateral isn't only fungible tokens — Offerbook also accepts NFT
collateral (classic/programmable/core), straight off the API's own
collateral.kind field on each loan, not inferred from whichever symbol
resolver happened to work.

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
SOLANA_RPC = os.getenv("SOLANA_RPC", "https://api.mainnet-beta.solana.com")
PAGE_SIZE = 100

JUPITER_TOKEN_SEARCH_API = "https://api.jup.ag/tokens/v2/search"
JUPITER_SEARCH_BATCH_SIZE = 50
DAS_BATCH_SIZE = 50  # getAssetBatch technically allows up to 1000, but kept small/consistent with Jupiter's batching

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


def resolve_nft_symbols(mints: list[str]) -> None:
    """Fallback for whatever Jupiter's (fungible-token-focused) search
    couldn't resolve — Offerbook also accepts NFT collateral (classic/
    programmable/core, per empty_escrow.py's docstring), which Jupiter's
    token search and DexScreener both have no concept of, so an NFT mint
    would otherwise always fall through to the raw-mint display. Uses
    Solana's DAS getAssetBatch (confirmed supported on the default public
    RPC, not just paid providers) to pull each asset's on-chain metadata
    symbol — falling back to a truncated name if symbol is missing/empty,
    since NFT collections often reuse one generic symbol (e.g. a tokenized
    trading-card platform using "COLLECTOR" for every card) where the name
    is what's actually distinguishing."""
    for i in range(0, len(mints), DAS_BATCH_SIZE):
        chunk = mints[i : i + DAS_BATCH_SIZE]
        try:
            resp = SESSION.post(
                SOLANA_RPC,
                json={"jsonrpc": "2.0", "id": 1, "method": "getAssetBatch", "params": {"ids": chunk}},
                timeout=15,
            )
            resp.raise_for_status()
            for asset in resp.json().get("result") or []:
                if not asset:
                    continue
                mint = asset.get("id")
                meta = (asset.get("content") or {}).get("metadata") or {}
                symbol = meta.get("symbol")
                name = meta.get("name")
                label = symbol or (name[:10] if name else None)
                if mint and label:
                    _RESOLVED_SYMBOLS[mint] = label
        except Exception as exc:
            log.warning("DAS NFT metadata lookup failed for a batch: %s", exc)


def resolve_collateral_symbols(mints: set[str]) -> None:
    """Look up real symbols (via Jupiter's token search API, falling back to
    on-chain NFT metadata for whatever that misses) for every mint not
    already in KNOWN_SYMBOLS — same approach address_snapshot.py/
    borrower_loan_timeline.py/update_config.py use for the fungible-token
    lookup, since Jupiter indexes far more of those than Offerbook's own
    registry."""
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

    still_unresolved = [m for m in unresolved if m not in _RESOLVED_SYMBOLS]
    if still_unresolved:
        resolve_nft_symbols(still_unresolved)


def symbol_for(mint: str) -> str:
    if mint in KNOWN_SYMBOLS:
        return KNOWN_SYMBOLS[mint]
    if mint in _RESOLVED_SYMBOLS:
        return _RESOLVED_SYMBOLS[mint]
    return mint[:6] + "…" if mint else "?"


def kind_label(asset_kind: str | None) -> str:
    """The API's own collateral.kind tells us this directly — observed
    values are "token" and three NFT standards ("classicNft",
    "programmableNft", "coreNft"); Offerbook has no other asset kinds, so
    anything that isn't literally "token" is an NFT."""
    return "TOKEN" if asset_kind == "token" else "NFT"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--top", type=int, default=20, help="How many collateral types to show (default 20).")
    args = parser.parse_args()

    log.info("Fetching defaulted loans platform-wide…")
    loans = fetch_defaulted_loans()
    log.info("  → %d defaulted loan(s)", len(loans))

    counts: Counter[str] = Counter()
    usd_lost: defaultdict[str, float] = defaultdict(float)
    kind_for: dict[str, str | None] = {}
    for l in loans:
        collateral = l.get("collateral") or {}
        cmint = l.get("collateralMint") or _common._mint_from_asset(collateral)
        if not cmint:
            continue
        counts[cmint] += 1
        meta = l.get("metadata") or {}
        usd_lost[cmint] += meta.get("endCollateralAmountUsd") or meta.get("startCollateralAmountUsd") or 0.0
        kind_for.setdefault(cmint, collateral.get("kind"))

    resolve_collateral_symbols(set(counts))

    ranked = sorted(usd_lost.items(), key=lambda kv: -kv[1])[: args.top]

    log.info("")
    log.info("Top %d defaulted collateral (by USD lost):", len(ranked))
    col = "{:<10}{:<7}{:>16}{:>10}"
    log.info(col.format("collat", "kind", "collateral $", "defaults"))
    for mint, usd in ranked:
        log.info(col.format(symbol_for(mint), kind_label(kind_for.get(mint)), f"${usd:,.2f}", counts[mint]))

    log.info("")
    log.info(", ".join(symbol_for(mint) for mint, _ in ranked))


if __name__ == "__main__":
    main()
