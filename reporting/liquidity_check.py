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

Each lender row also shows their size-weighted average APY, average LTV, and
the distinct durations across their live offers on this pair — not just how
much liquidity they represent, but at what price, risk, and term, for a
fuller picture of who you'd actually be borrowing from and on what the
market is pricing right now.

It also answers a different question — not "how much liquidity is
available" but "what do borrowers on this pair actually take, and at what
price" — from that pair's full loan history (active + repaid + defaulted,
platform-wide, any lender):
  - Biggest loans EVER taken against this collateral (top 10).
  - The single biggest calendar day EVER, by total USD originated.
  - Size vs. APY over a recent window (quartiles by loan size, --days-back):
    are the biggest loans paying MORE than the smallest ones right now (a
    sign of price-insensitive large borrowers — room to push APY higher on
    size) or LESS (large borrowers are shopping around — pushing APY too
    high on a big offer risks it going unfilled)? This is the actionable
    signal for tuning your own offer's APY against what this specific
    market's big players are actually willing to pay, not a guess from a
    handful of recent fills.
  This section runs even if the pair currently has zero live offers — loan
  history still exists, and still tells you something, even when nobody's
  actively quoting right now.

If OFFERBOOK_PORTFOLIO_WALLETS (.env, comma-separated — same var
breakeven_check.py/portfolio_health.py read) is set, a final section shows
YOUR active loans on this exact pair (principal locked, days until each
matures) and, the moment that capital is freed, 5 concrete offers to post
immediately: that freed principal split into 5 tranches, sized proportional
to recent real demand at each size band and priced/dated at what borrowers
in that band actually paid/took (median APY, modal duration, median LTV) —
same recent-activity window as the quartile breakdown above (--days-back),
so sizing reflects actual borrower behavior on this pair rather than a
guess. Skipped entirely if the var is unset.

Usage:
  python liquidity_check.py --collateral USELESS
  python liquidity_check.py --collateral <mint address>
  python liquidity_check.py --collateral USELESS --principal SOL
  python liquidity_check.py --collateral USELESS --days-back 30
  python liquidity_check.py                                        # prompts for collateral

Notes:
  - Defaults to USDC principal (the overwhelming majority of loans on this
    platform) — pass --principal to check a different principal mint.
  - includeUnderfunded=true is required when fetching offers — without it,
    exactly the stacked/rehypothecated offers this script exists to account
    for are invisible (same reason strategy.py's own offer-fetching sets it).
  - Only "active" and "partiallyFilled" offers count for the liquidity
    section — expired/cancelled/filled offers aren't real liquidity.
  - The /loans/status/* endpoint ignores collateralMint/principalMint query
    params server-side (confirmed: identical result totals with or without
    them) — same limitation borrower_loan_timeline.py works around — so loan
    history is fetched in full and filtered to this exact pair client-side.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone

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
DAYS_BACK_DEFAULT = 14  # lookback window for the size-vs-APY quartile breakdown

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

# Same var breakeven_check.py/portfolio_health.py read — "your" wallets for
# the locked-capital/reoffer-recommendation section. Comma-separated, .env.
PORTFOLIO_WALLETS = [w.strip() for w in os.getenv("OFFERBOOK_PORTFOLIO_WALLETS", "").split(",") if w.strip()]
REOFFER_TRANCHES = 5

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


def fetch_loans_for_pair(principal_mint: str, collateral_mint: str) -> list[dict]:
    """Every loan (active/repaid/defaulted) ever matched for this exact
    (principal, collateral) pair, platform-wide, any lender — see module
    docstring for why this fetches everything and filters client-side
    rather than relying on a server-side filter."""
    loans: list[dict] = []
    for status in ("active", "repaid", "defaulted"):
        for l in _fetch_all_pages(f"/loans/status/{status}"):
            pmint = l.get("principalMint") or _common._mint_from_asset(l.get("principal", {}))
            cmint = l.get("collateralMint") or _common._mint_from_asset(l.get("collateral", {}))
            if pmint == principal_mint and cmint == collateral_mint:
                l["_status"] = status
                loans.append(l)
    return loans


def biggest_loans_ever(loans: list[dict], top_n: int = 10) -> list[dict]:
    """Top `top_n` loans by USD principal at origination, across all
    statuses — origination size/price is real market behavior regardless of
    whether the loan was later repaid or defaulted."""
    rows = []
    for l in loans:
        meta = l.get("metadata") or {}
        rows.append({
            "created_at": (l.get("createdAt") or "")[:10],
            "borrower": l.get("borrower", ""),
            "principal_usd": meta.get("startPrincipalAmountUsd") or 0.0,
            "apy_bps": l.get("apy", 0),
            "duration_days": (l.get("duration") or 0) / 86400,
            "status": l.get("_status", ""),
        })
    rows.sort(key=lambda r: -r["principal_usd"])
    return rows[:top_n]


def biggest_day_ever(loans: list[dict]) -> dict | None:
    """The single calendar day (UTC) with the most total USD principal
    originated against this pair, across all of history. None if no loan
    has a usable createdAt."""
    by_day: dict[str, dict] = {}
    for l in loans:
        day = (l.get("createdAt") or "")[:10]
        if not day:
            continue
        meta = l.get("metadata") or {}
        d = by_day.setdefault(day, {"day": day, "total_usd": 0.0, "count": 0})
        d["total_usd"] += meta.get("startPrincipalAmountUsd") or 0.0
        d["count"] += 1
    if not by_day:
        return None
    return max(by_day.values(), key=lambda d: d["total_usd"])


def _recent_loans_by_size(loans: list[dict], days_back: int) -> list[dict]:
    """Loans originated in the last `days_back` days with usable USD
    principal, sorted ascending by size — the shared basis for both the
    quartile breakdown and the reoffer-sizing recommendations below, so
    both read off exactly the same borrower-activity window. Each row also
    carries duration (days) and origination LTV (startPrincipalAmountUsd /
    startCollateralAmountUsd, None if either's missing) for the
    recommendations' duration/LTV signal."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)
    recent = []
    for l in loans:
        try:
            created = datetime.fromisoformat(l["createdAt"].replace("Z", "+00:00"))
        except (KeyError, ValueError, AttributeError):
            continue
        if created < cutoff:
            continue
        meta = l.get("metadata") or {}
        principal_usd = meta.get("startPrincipalAmountUsd") or 0.0
        if principal_usd <= 0:
            continue
        collateral_usd = meta.get("startCollateralAmountUsd") or 0.0
        recent.append({
            "principal_usd": principal_usd,
            "apy_bps": l.get("apy", 0),
            "duration_days": round((l.get("duration") or 0) / 86400),
            "ltv": principal_usd / collateral_usd if collateral_usd else None,
        })
    recent.sort(key=lambda r: r["principal_usd"])
    return recent


def _bucket_by_size(recent: list[dict], n: int) -> list[dict]:
    """Split `recent` (already sorted ascending by principal_usd) into `n`
    equal-COUNT groups, each reporting size range, loan count, total
    volume, and median/max APY, modal duration, and median LTV actually
    seen at that size — generalized so the quartile breakdown (n=4) and
    the reoffer-sizing recommendations (n=5) share one implementation and
    agree on what "this size band" paid. Returns [] if `recent` is
    empty."""
    if not recent:
        return []
    count = len(recent)
    step = max(1, count // n)

    buckets = []
    for i in range(n):
        start = i * step
        end = count if i == n - 1 else min((i + 1) * step, count)
        chunk = recent[start:end]
        if not chunk:
            continue
        apys = sorted(c["apy_bps"] for c in chunk)
        durations = [c["duration_days"] for c in chunk]
        ltvs = sorted(c["ltv"] for c in chunk if c["ltv"] is not None)
        buckets.append({
            "label": f"Q{i + 1}",
            "size_min": chunk[0]["principal_usd"],
            "size_max": chunk[-1]["principal_usd"],
            "count": len(chunk),
            "total_usd": sum(c["principal_usd"] for c in chunk),
            "median_apy_bps": apys[len(apys) // 2],
            "max_apy_bps": max(apys),
            "mode_duration_days": max(set(durations), key=durations.count),
            "median_ltv": ltvs[len(ltvs) // 2] if ltvs else None,
        })
    return buckets


def size_vs_apy_quartiles(loans: list[dict], days_back: int) -> list[dict]:
    """Loans originated in the last `days_back` days, split into 4 equal-
    COUNT groups by USD principal (smallest to largest), each reporting its
    size range, loan count, total volume, and median/max APY paid. Directly
    answers "do bigger loans on this pair pay more or less than smaller
    ones right now" — the size-vs-price-sensitivity signal, not just a
    single number. Returns [] if nothing originated in the window."""
    return _bucket_by_size(_recent_loans_by_size(loans, days_back), 4)


def my_active_loans(loans: list[dict], wallets: list[str]) -> list[dict]:
    """This pair's active loans where one of `wallets` is the lender — the
    capital that's currently tied down on this exact pair and will become
    free as each one matures/repays. `loans` is the already-fetched
    platform-wide loans-for-this-pair list (see fetch_loans_for_pair);
    filtered client-side the same way breakeven_check.py's
    fetch_active_loans_for does (no lender filter exists server-side)."""
    if not wallets:
        return []
    wallet_set = set(wallets)
    return [l for l in loans if l.get("_status") == "active" and l.get("lender") in wallet_set]


def recommend_reoffers(loans: list[dict], days_back: int, available_raw: int, n: int = REOFFER_TRANCHES) -> list[dict]:
    """`n` concrete tranches to re-post the instant `available_raw` (your
    locked principal on this pair, once it's released) is free to deploy
    again: split recent borrower activity (same window as the quartile
    breakdown, `_recent_loans_by_size`) into `n` equal-count size bands,
    then carve `available_raw` across those bands proportional to each
    band's share of total $ volume actually borrowed recently — not an
    even split — and price/date each tranche at that band's own median
    APY, modal duration, and median LTV, i.e. exactly what borrowers at
    that size actually took, not a guess. A lender re-entering with sizing
    that matches real demand at each tier (priced at what that tier
    actually paid) should fill faster than one lump sum guessed at a
    single APY. Returns [] if there's no recent activity to size off of,
    or nothing to recommend (available_raw <= 0)."""
    if available_raw <= 0:
        return []
    buckets = _bucket_by_size(_recent_loans_by_size(loans, days_back), n)
    total_usd = sum(b["total_usd"] for b in buckets)
    if not buckets or total_usd <= 0:
        return []

    recs = []
    allocated = 0
    for i, b in enumerate(buckets):
        amount_raw = round(available_raw * (b["total_usd"] / total_usd))
        allocated += amount_raw
        recs.append({
            "tranche": i + 1,
            "size_range": (b["size_min"], b["size_max"]),
            "based_on_count": b["count"],
            "amount_raw": amount_raw,
            "apy_bps": b["median_apy_bps"],
            "duration_days": b["mode_duration_days"],
            "ltv": b["median_ltv"],
        })
    # Per-tranche rounding can leave the total a few raw units off
    # `available_raw` — correct it on the largest tranche so the
    # recommendations always sum to exactly what's being released.
    diff = available_raw - allocated
    max(recs, key=lambda r: r["amount_raw"])["amount_raw"] += diff
    return recs


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
    of None rather than 0 — see fetch_wallet_token_balance.

    Also reports, per lender, the size-weighted average APY, average LTV,
    and the distinct durations across their live offers on this pair — a
    lender offering $50k at 8% should show as ~8%-ish even alongside a $500
    offer at 40%, not a naive 24% average of the two, since the $50k is what's
    actually pricing this market (same reasoning applies to LTV). LTV comes
    straight from each offer's own metadata.principalAmountUsd /
    collateralAmountUsd — the LTV the lender itself priced the offer at, not
    recomputed from live prices, so it reflects their actual risk appetite at
    post time rather than how that offer happens to be priced right now.
    Offers missing one of those USD fields are excluded from the LTV average
    specifically (but still count fully toward offered/APY/durations) rather
    than silently treated as 0% LTV. Durations are kept as the full distinct
    set (not averaged) since they're normally a handful of fixed choices
    (7d/15d/30d...), not a continuum — an "average duration" would land on a
    number nobody's actually offering."""
    by_lender: dict[str, int] = {}
    offer_counts: dict[str, int] = {}
    apy_weighted_sum: dict[str, float] = {}
    ltv_weighted_sum: dict[str, float] = {}
    ltv_weight_total: dict[str, int] = {}
    durations_by_lender: dict[str, set[float]] = {}
    for o in offers:
        creator = o.get("creator", "")
        amount = o.get("remainingPrincipal", o.get("principalAmount", 0))
        by_lender[creator] = by_lender.get(creator, 0) + amount
        offer_counts[creator] = offer_counts.get(creator, 0) + 1
        apy_weighted_sum[creator] = apy_weighted_sum.get(creator, 0.0) + amount * o.get("apy", 0)
        durations_by_lender.setdefault(creator, set()).add(round((o.get("duration") or 0) / 86400))

        meta = o.get("metadata") or {}
        p_usd, c_usd = meta.get("principalAmountUsd"), meta.get("collateralAmountUsd")
        if p_usd is not None and c_usd:
            ltv_weighted_sum[creator] = ltv_weighted_sum.get(creator, 0.0) + amount * (p_usd / c_usd)
            ltv_weight_total[creator] = ltv_weight_total.get(creator, 0) + amount

    rows = []
    for lender, offered_raw in by_lender.items():
        wallet_raw = fetch_wallet_token_balance(lender, principal_mint)
        escrow_raw = fetch_escrow_balance(lender, principal_mint)
        if wallet_raw is None or escrow_raw is None:
            balance_raw = available_raw = None
        else:
            balance_raw = wallet_raw + escrow_raw
            available_raw = min(offered_raw, balance_raw)
        ltv_weight = ltv_weight_total.get(lender, 0)
        rows.append({
            "lender": lender,
            "offers": offer_counts[lender],
            "offered_raw": offered_raw,
            "balance_raw": balance_raw,
            "available_raw": available_raw,
            "avg_apy_bps": apy_weighted_sum[lender] / offered_raw if offered_raw else 0.0,
            "avg_ltv": ltv_weighted_sum[lender] / ltv_weight if ltv_weight else None,
            "durations_days": sorted(durations_by_lender[lender]),
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
    col = "{:<46}{:>8}{:>18}{:>18}{:>18}{:>10}{:>10}  {:<16}"
    log.info(col.format("lender", "offers", "offered", "actual balance", "REAL available", "avg APY", "avg LTV", "durations"))
    unknown_count = 0
    for r in rows:
        durations_str = ",".join(f"{d:.0f}d" for d in r["durations_days"])
        avg_apy_str = f"{r['avg_apy_bps'] / 100:.2f}%"
        avg_ltv_str = f"{r['avg_ltv'] * 100:.1f}%" if r["avg_ltv"] is not None else "n/a"
        if r["balance_raw"] is None:
            unknown_count += 1
            log.info(
                col.format(r["lender"], r["offers"], f"{r['offered_raw'] / scale:,.2f}", "?", "?", avg_apy_str, avg_ltv_str, durations_str)
                + " *** BALANCE CHECK FAILED (excluded from totals below) ***"
            )
            continue
        overstated = " *** OVERSTATED ***" if r["offered_raw"] > r["balance_raw"] else ""
        log.info(
            col.format(
                r["lender"], r["offers"],
                f"{r['offered_raw'] / scale:,.2f}", f"{r['balance_raw'] / scale:,.2f}",
                f"{r['available_raw'] / scale:,.2f}", avg_apy_str, avg_ltv_str, durations_str,
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
            "  → %s %s (%.1f%%) of the naive total is NOT actually available "
            "(rehypothecated across multiple offers, wallet+escrow can't cover it all).",
            f"{total_offered - total_available:,.2f}", sym,
            (total_offered - total_available) / total_offered * 100 if total_offered else 0,
        )


def print_loan_history_report(loans: list[dict], principal_mint: str, collateral_mint: str, days_back: int) -> None:
    sym = symbol_for(principal_mint)

    log.info("")
    log.info("=" * 100)
    log.info(
        "LOAN SIZE & PRICING HISTORY — %s/%s collateral (%d loan(s) ever matched, any lender)",
        sym, symbol_for(collateral_mint), len(loans),
    )
    log.info("=" * 100)

    if not loans:
        log.info("No loan history at all for this pair — nobody's ever borrowed against it.")
        return

    top = biggest_loans_ever(loans)
    log.info("")
    log.info("Biggest loans EVER (top %d):", len(top))
    col = "{:<12}{:<46}{:>16}{:>10}{:>10}  {:<10}"
    log.info(col.format("date", "borrower", f"principal {sym}", "APY", "duration", "status"))
    for r in top:
        log.info(col.format(
            r["created_at"], r["borrower"], f"{r['principal_usd']:,.2f}",
            f"{r['apy_bps'] / 100:.2f}%", f"{r['duration_days']:.0f}d", r["status"],
        ))

    day = biggest_day_ever(loans)
    log.info("")
    if day:
        log.info("Biggest single day EVER: %s — $%s across %d loan(s)", day["day"], f"{day['total_usd']:,.2f}", day["count"])

    quartiles = size_vs_apy_quartiles(loans, days_back)
    log.info("")
    log.info("Size vs. APY — last %d day(s):", days_back)
    if not quartiles:
        log.info("  No loans originated in this window.")
        return

    col2 = "{:<8}{:>24}{:>8}{:>16}{:>14}{:>14}"
    log.info(col2.format("quartile", "size range $", "count", "total vol $", "median APY", "max APY"))
    for q in quartiles:
        log.info(col2.format(
            q["label"], f"{q['size_min']:,.0f} - {q['size_max']:,.0f}", q["count"],
            f"{q['total_usd']:,.2f}", f"{q['median_apy_bps'] / 100:.2f}%", f"{q['max_apy_bps'] / 100:.2f}%",
        ))

    if len(quartiles) >= 2:
        smallest, biggest = quartiles[0], quartiles[-1]
        if biggest["median_apy_bps"] > smallest["median_apy_bps"]:
            log.info(
                "  → Biggest loans (%s, median $%s+) are paying MORE than the smallest (%s) — consistent with "
                "price-insensitive large borrowers here. You may have room to push APY higher on a large offer.",
                biggest["label"], f"{biggest['size_min']:,.0f}", smallest["label"],
            )
        elif biggest["median_apy_bps"] < smallest["median_apy_bps"]:
            log.info(
                "  → Biggest loans (%s, median $%s+) are paying LESS than the smallest (%s) — large borrowers "
                "here are shopping around. Pushing APY too high on a big offer risks it sitting unfilled.",
                biggest["label"], f"{biggest['size_min']:,.0f}", smallest["label"],
            )
        else:
            log.info("  → No meaningful difference in APY paid between smallest and biggest loans in this window.")


def print_reoffer_report(
    my_loans: list[dict], recs: list[dict], principal_mint: str, collateral_mint: str, days_back: int,
) -> None:
    decimals = decimals_for(principal_mint)
    scale = 10 ** decimals
    sym = symbol_for(principal_mint)

    log.info("")
    log.info("=" * 100)
    log.info("YOUR LOCKED CAPITAL — %s/%s, released as your active loans mature", sym, symbol_for(collateral_mint))
    log.info("=" * 100)

    if not my_loans:
        log.info(
            "None of your wallet(s) in OFFERBOOK_PORTFOLIO_WALLETS have an active loan on this exact pair — "
            "nothing of yours is tied down here right now."
        )
        return

    now = datetime.now(timezone.utc)
    total_locked_raw = 0
    col = "{:<46}{:>18}{:>14}"
    log.info(col.format("loan", "principal locked", "releases in"))
    for l in my_loans:
        principal_raw = l.get("outstandingPrincipal", l.get("principalAmount", 0))
        total_locked_raw += principal_raw
        try:
            expired = datetime.fromisoformat(l["expiredAt"].replace("Z", "+00:00"))
            releases_in = "overdue" if expired <= now else f"{(expired - now).total_seconds() / 86400:.1f}d"
        except (KeyError, ValueError, AttributeError, TypeError):
            releases_in = "?"
        log.info(col.format(l.get("pubkey", ""), f"{principal_raw / scale:,.2f} {sym}", releases_in))

    log.info("")
    log.info(
        "Total locked on this pair: %s %s across %d active loan(s) — frees up incrementally as each one "
        "matures/repays, not all at once.",
        f"{total_locked_raw / scale:,.2f}", sym, len(my_loans),
    )

    log.info("")
    if not recs:
        log.info(
            "No loan activity on this pair in the last %d day(s) to size recommendations off of — "
            "rerun with a larger --days-back once some exists.", days_back,
        )
        return

    log.info(
        "%d offers to post the moment that capital is released, sized/priced off real borrower activity "
        "in the last %d day(s):", len(recs), days_back,
    )
    col2 = "{:<8}{:>24}{:>18}{:>10}{:>10}{:>10}"
    log.info(col2.format("tranche", "sized off loans $", "post amount", "APY", "duration", "LTV"))
    for r in recs:
        size_range = f"{r['size_range'][0]:,.0f}-{r['size_range'][1]:,.0f} ({r['based_on_count']})"
        ltv_str = f"{r['ltv'] * 100:.1f}%" if r["ltv"] is not None else "n/a"
        log.info(col2.format(
            r["tranche"], size_range, f"{r['amount_raw'] / scale:,.2f} {sym}",
            f"{r['apy_bps'] / 100:.2f}%", f"{r['duration_days']:.0f}d", ltv_str,
        ))
    log.info(
        "APY/duration/LTV are the median/mode actually paid/taken by loans in that size band in the window "
        "above — not a guess. Amounts sum to the full %s %s above (post all 5 as soon as it's freed, not "
        "a single lump sum, so sizing matches real demand at each tier).",
        f"{total_locked_raw / scale:,.2f}", sym,
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
    parser.add_argument(
        "--days-back", type=int, default=DAYS_BACK_DEFAULT,
        help=f"Lookback window in days for the size-vs-APY quartile breakdown (default {DAYS_BACK_DEFAULT}).",
    )
    args = parser.parse_args()

    collateral_mint = resolve_token(args.collateral or prompt_for_collateral())
    principal_mint = resolve_token(args.principal)

    log.info(
        "Fetching live lending offers for %s/%s …",
        symbol_for(principal_mint), symbol_for(collateral_mint),
    )
    offers = fetch_lending_offers(principal_mint, collateral_mint)
    if not offers:
        log.warning(
            "No live lending offers found for principal=%s collateral=%s — skipping the liquidity "
            "section, but still checking loan history below.",
            args.principal, args.collateral or collateral_mint,
        )
    else:
        log.info("  → %d live offer(s) from %d unique lender(s)", len(offers), len({o.get("creator") for o in offers}))
        log.info("Checking each lender's actual wallet+escrow balance …")
        rows = compute_liquidity(offers, principal_mint)
        print_report(rows, principal_mint, collateral_mint)

    log.info("Fetching loan history for %s/%s …", symbol_for(principal_mint), symbol_for(collateral_mint))
    loans = fetch_loans_for_pair(principal_mint, collateral_mint)
    print_loan_history_report(loans, principal_mint, collateral_mint, args.days_back)

    if PORTFOLIO_WALLETS:
        mine = my_active_loans(loans, PORTFOLIO_WALLETS)
        locked_raw = sum(l.get("outstandingPrincipal", l.get("principalAmount", 0)) for l in mine)
        recs = recommend_reoffers(loans, args.days_back, locked_raw)
        print_reoffer_report(mine, recs, principal_mint, collateral_mint, args.days_back)
    else:
        log.info("")
        log.info("Set OFFERBOOK_PORTFOLIO_WALLETS in .env to get reoffer recommendations for capital you have locked on this pair.")

    if not offers and not loans:
        log.error("No live offers AND no loan history for this pair at all — nothing to check.")
        sys.exit(1)


if __name__ == "__main__":
    main()
