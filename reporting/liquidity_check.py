"""
Offerbook Liquidity Check
==================================
How much can you ACTUALLY borrow against a given collateral right now?

A live lending offer's size is not a reliable answer on its own: Offerbook
lets (and this repo's own strategy.py deliberately does) a single lender
post several offers — same or different pairs — whose sizes sum to more
than that lender's real wallet+escrow balance ("rehypothecation": only one
offer can actually be filled with a given unit of capital, so posting
several is fine as long as you accept that not all of them can fill at
once). Naively summing every live offer's size overstates real liquidity
whenever this happens.

This script corrects for that: for each lender with a live offer on the
given (principal, collateral) pair, it caps that lender's contribution at
min(sum of their offer sizes on THIS pair, their actual current wallet +
escrow balance of the principal token) — the real amount a borrower could
pull from them on this pair right now, no more. Summed across every lender,
that's the real available liquidity, shown alongside the naive raw total so
the gap (if any) is obvious.

Usage:
  python liquidity_check.py --collateral USELESS
  python liquidity_check.py --collateral <mint address>
  python liquidity_check.py --collateral USELESS --principal SOL
  python liquidity_check.py                                        # prompts for collateral

Notes:
  - Defaults to USDC principal (the overwhelming majority of loans on this
    platform) — pass --principal to check a different principal mint.
  - includeUnderfunded=true is required when fetching offers — without it,
    exactly the stacked/rehypothecated offers this script exists to account
    for are invisible (same reason strategy.py's own offer-fetching sets it).
  - Only "active" and "partiallyFilled" offers count — expired/cancelled/
    filled offers aren't real liquidity.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time

import base58
import requests
from dotenv import load_dotenv

load_dotenv()

# Shared modules live in ../lib — see README's repo-layout note.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))

import offerbook_common as _common

API_BASE = os.getenv("OFFERBOOK_API_BASE", "https://api.offerbook.jup.ag/api/v1")
SOLANA_RPC = os.getenv("SOLANA_RPC", "https://api.mainnet-beta.solana.com")
PAGE_SIZE = 100

USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDC_DECIMALS = 6

# strategy.py's allocation_config.yaml lists far more tokens (with symbols in
# its own comments) than the curated KNOWN_SYMBOLS table here — used as a
# second-tier symbol->mint lookup below, same as strategy.py's own
# resolve_collateral_token does.
ALLOCATION_CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "strategy", "allocation_config.yaml",
)

KNOWN_DECIMALS = _common.KNOWN_DECIMALS
KNOWN_SYMBOLS = _common.KNOWN_SYMBOLS
SYMBOL_TO_MINT = {sym.upper(): mint for mint, sym in KNOWN_SYMBOLS.items()}
_allocation_symbol_to_mint_cache: dict[str, str] | None = None

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("liquidity_check")

SESSION = requests.Session()


def is_valid_pubkey(s: str) -> bool:
    """A Solana pubkey base58-decodes to exactly 32 bytes."""
    try:
        return len(base58.b58decode(s)) == 32
    except Exception:
        return False


def resolve_token(tok: str) -> str:
    """
    Resolve a symbol (e.g. "USELESS") or raw mint address to a mint address:
      1. SYMBOL_TO_MINT (curated table) — fastest, most common.
      2. allocation_config.yaml's own comments (scraped, same as
         strategy.py's resolve_collateral_token) — covers the much longer
         tail of tokens that are configured there but not in the curated
         table (e.g. "OTC").
      3. Otherwise assumed to already be a raw mint address — validated as a
         plausible base58 pubkey; exits with a clear error instead of
         sending a malformed value to the API (which just 400s opaquely).
    """
    global _allocation_symbol_to_mint_cache
    tok = tok.strip()
    upper = tok.upper()
    if upper in SYMBOL_TO_MINT:
        return SYMBOL_TO_MINT[upper]

    if _allocation_symbol_to_mint_cache is None:
        _allocation_symbol_to_mint_cache = _common.build_symbol_to_mint_from_allocation_config(ALLOCATION_CONFIG_PATH)
    if upper in _allocation_symbol_to_mint_cache:
        return _allocation_symbol_to_mint_cache[upper]

    if not is_valid_pubkey(tok):
        log.error(
            "%r isn't a known symbol or a valid mint address (checked the curated table and "
            "allocation_config.yaml's comments). If it's a typo, fix it; otherwise pass the raw mint.",
            tok,
        )
        sys.exit(1)
    return tok


def decimals_for(mint: str) -> int:
    if mint == USDC_MINT:
        return USDC_DECIMALS
    return KNOWN_DECIMALS.get(mint, 9)  # 9 is the common SPL default when unknown


def symbol_for(mint: str) -> str:
    return KNOWN_SYMBOLS.get(mint, mint[:6] + "…" + mint[-4:])


def _fetch_all_pages(endpoint: str, params: dict | None = None) -> list[dict]:
    return _common.fetch_all_pages(SESSION, API_BASE, endpoint, params, PAGE_SIZE)


def fetch_lending_offers(principal_mint: str, collateral_mint: str) -> list[dict]:
    """Every live (active or partiallyFilled) lending offer for this exact
    (principal, collateral) pair. includeUnderfunded=true is required — see
    module docstring."""
    offers: list[dict] = []
    for status in ("active", "partiallyFilled"):
        offers += _fetch_all_pages("/offers", {
            "offerType": "lending", "status": status, "hideExpired": "true",
            "showUnverified": "true", "includeUnderfunded": "true",
            "principalMint": principal_mint, "collateralMint": collateral_mint,
        })
    return offers


def fetch_wallet_token_balance(wallet: str, mint: str, attempts: int = 3) -> int | None:
    """Raw token units, summed across every token account this wallet holds
    for `mint`. Retries on failure (this public RPC rate-limits under load —
    this script makes one call per unique lender), and returns None (not 0)
    if every attempt fails — a transient RPC error must never be mistaken
    for a genuine zero balance, since that would misreport a lender's real
    liquidity as $0/overstated when we simply don't know it."""
    payload = {
        "jsonrpc": "2.0", "id": 1, "method": "getTokenAccountsByOwner",
        "params": [wallet, {"mint": mint}, {"encoding": "jsonParsed"}],
    }
    last_exc: Exception | None = None
    for i in range(attempts):
        try:
            resp = SESSION.post(SOLANA_RPC, json=payload, timeout=20)
            resp.raise_for_status()
            accounts = resp.json().get("result", {}).get("value", [])
            return sum(
                int(a.get("account", {}).get("data", {}).get("parsed", {}).get("info", {})
                     .get("tokenAmount", {}).get("amount", "0"))
                for a in accounts
            )
        except Exception as exc:
            last_exc = exc
            if i < attempts - 1:
                time.sleep(1.5 * (i + 1))
    log.warning("Couldn't fetch wallet balance for %s after %d attempts (%s).", wallet, attempts, last_exc)
    return None


def fetch_escrow_balance(wallet: str, mint: str, attempts: int = 3) -> int | None:
    """Same None-on-failure convention as fetch_wallet_token_balance — see
    that function's docstring for why."""
    last_exc: Exception | None = None
    for i in range(attempts):
        try:
            resp = SESSION.get(f"{API_BASE}/escrows/holdings/{wallet}", timeout=30)
            resp.raise_for_status()
            for entry in resp.json():
                if entry.get("asset", {}).get("mint") == mint:
                    return int(entry.get("amount", 0))
            return 0
        except Exception as exc:
            last_exc = exc
            if i < attempts - 1:
                time.sleep(1.5 * (i + 1))
    log.warning("Couldn't fetch escrow balance for %s after %d attempts (%s).", wallet, attempts, last_exc)
    return None


def compute_liquidity(offers: list[dict], principal_mint: str) -> list[dict]:
    """Group offers by lender (creator), and for each lender fetch their real
    wallet+escrow balance of `principal_mint` — capping their contribution
    to this pair's liquidity at min(offered, actual balance). Returns rows
    sorted by real available amount descending. A lender whose balance
    couldn't be fetched (RPC failure after retries) gets balance/available
    of None rather than 0 — see fetch_wallet_token_balance."""
    by_lender: dict[str, int] = {}
    offer_counts: dict[str, int] = {}
    for o in offers:
        creator = o.get("creator", "")
        amount = o.get("remainingPrincipal", o.get("principalAmount", 0))
        by_lender[creator] = by_lender.get(creator, 0) + amount
        offer_counts[creator] = offer_counts.get(creator, 0) + 1

    rows = []
    for lender, offered_raw in by_lender.items():
        wallet_raw = fetch_wallet_token_balance(lender, principal_mint)
        escrow_raw = fetch_escrow_balance(lender, principal_mint)
        if wallet_raw is None or escrow_raw is None:
            balance_raw = available_raw = None
        else:
            balance_raw = wallet_raw + escrow_raw
            available_raw = min(offered_raw, balance_raw)
        rows.append({
            "lender": lender,
            "offers": offer_counts[lender],
            "offered_raw": offered_raw,
            "balance_raw": balance_raw,
            "available_raw": available_raw,
        })
    # Unknown-balance rows always sort last (regardless of how small/large
    # known rows are) — they're not confirmed to be worth 0, just unknown.
    rows.sort(key=lambda r: (r["available_raw"] is None, -(r["available_raw"] or 0)))
    return rows


def print_report(rows: list[dict], principal_mint: str, collateral_mint: str) -> None:
    decimals = decimals_for(principal_mint)
    scale = 10 ** decimals
    sym = symbol_for(principal_mint)

    log.info("")
    log.info("=" * 100)
    log.info("LIQUIDITY — lend %s against %s collateral", sym, symbol_for(collateral_mint))
    log.info("=" * 100)
    col = "{:<46}{:>8}{:>18}{:>18}{:>18}"
    log.info(col.format("lender", "offers", "offered", "actual balance", "REAL available"))
    unknown_count = 0
    for r in rows:
        if r["balance_raw"] is None:
            unknown_count += 1
            log.info(
                col.format(r["lender"], r["offers"], f"{r['offered_raw'] / scale:,.2f}", "?", "?")
                + " *** BALANCE CHECK FAILED (excluded from totals below) ***"
            )
            continue
        overstated = " *** OVERSTATED ***" if r["offered_raw"] > r["balance_raw"] else ""
        log.info(
            col.format(
                r["lender"], r["offers"],
                f"{r['offered_raw'] / scale:,.2f}", f"{r['balance_raw'] / scale:,.2f}",
                f"{r['available_raw'] / scale:,.2f}",
            ) + overstated
        )

    known_rows = [r for r in rows if r["balance_raw"] is not None]
    total_offered = sum(r["offered_raw"] for r in known_rows) / scale
    total_available = sum(r["available_raw"] for r in known_rows) / scale
    log.info("")
    if unknown_count:
        log.info(
            "%d lender(s) excluded from totals below — balance check failed after retries (see above).",
            unknown_count,
        )
    log.info("Naive sum of live offers  : %s %s", f"{total_offered:,.2f}", sym)
    log.info("REAL available liquidity  : %s %s", f"{total_available:,.2f}", sym)
    if total_offered > total_available:
        log.info(
            "  → %.2f %s (%.1f%%) of the naive total is NOT actually available "
            "(rehypothecated across multiple offers, wallet+escrow can't cover it all).",
            total_offered - total_available, sym,
            (total_offered - total_available) / total_offered * 100 if total_offered else 0,
        )


def prompt_for_collateral() -> str:
    raw = input("Enter collateral symbol (e.g. USELESS) or mint address: ").strip()
    while not raw:
        raw = input("Collateral can't be blank: ").strip()
    return raw


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--collateral", default=None, help="Collateral symbol or mint address. Omit to be prompted.")
    parser.add_argument("--principal", default="USDC", help='Principal symbol or mint address (default "USDC").')
    args = parser.parse_args()

    collateral_mint = resolve_token(args.collateral or prompt_for_collateral())
    principal_mint = resolve_token(args.principal)

    log.info(
        "Fetching live lending offers for %s/%s …",
        symbol_for(principal_mint), symbol_for(collateral_mint),
    )
    offers = fetch_lending_offers(principal_mint, collateral_mint)
    if not offers:
        log.error(
            "No live lending offers found for principal=%s collateral=%s — nothing to check.",
            args.principal, args.collateral or collateral_mint,
        )
        sys.exit(1)
    log.info("  → %d live offer(s) from %d unique lender(s)", len(offers), len({o.get("creator") for o in offers}))

    log.info("Checking each lender's actual wallet+escrow balance …")
    rows = compute_liquidity(offers, principal_mint)
    print_report(rows, principal_mint, collateral_mint)


if __name__ == "__main__":
    main()
