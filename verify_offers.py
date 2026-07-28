"""
Offerbook Post-Placement Sanity Check
======================================
Run this after placing orders to verify that every live offer has:
  - LTV within the dynamic per-token target the strategy would compute right now
    (using fresh on-chain prices and fresh market data — mirrors effective_target_ltv()
    in strategy.py)
  - APY above the 10 bps floor
  - Correct duration for the chosen strategy
  - Non-dust principal amount

Usage:
  python verify_offers.py --days 7        # check 7-day strategy offers
  python verify_offers.py --days 3        # check 3-day strategy offers
  python verify_offers.py --days 15       # check 15-day strategy offers
  python verify_offers.py --days all      # check all three strategies
  python verify_offers.py                 # same as --days all

Exit codes:
  0 — all offers passed (or only warnings)
  1 — one or more LTV violations detected
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from collections import defaultdict

import requests
from dotenv import load_dotenv

load_dotenv()

import offerbook_common as _common
from offerbook_common import _mint_from_asset

# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------

API_BASE        = os.getenv("OFFERBOOK_API_BASE", "https://api.offerbook.jup.ag/api/v1")
SOLANA_RPC      = os.getenv("SOLANA_RPC", "https://api.mainnet-beta.solana.com")
WALLET_PUBKEY   = os.getenv("OFFERBOOK_WALLET", "")

# "ledger" or "private_key" — Ledger is the default signing mode, matching strategy.py.
SIGNING_MODE: str = os.getenv("OFFERBOOK_SIGNING_MODE", "ledger").strip().lower()
LEDGER_PATH: str = os.getenv("OFFERBOOK_LEDGER_PATH", "44'/501'/0'")

JUPITER_PRICE_API = "https://api.jup.ag/price/v3"
JUPITER_API_KEY   = os.getenv("JUPITER_API_KEY", "")
DEXSCREENER_API   = "https://api.dexscreener.com/latest/dex/tokens"

USDC_MINT     = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDC_DECIMALS = 6
MIN_APY_BPS   = 10
PAGE_SIZE     = 100

# ---------------------------------------------------------------------------
# Per-strategy constants
# ---------------------------------------------------------------------------
# Mirrors effective_target_ltv() in strategy.py — see that file (and README §4)
# for the full rule.
# Only fallback_ltv (the thin-market-data floor) differs by strategy; the
# rest of the formula is shared.

STRATEGY_PARAMS: dict[int, dict] = {
    3:  {"fallback_ltv": 0.65, "duration_secs": 3  * 86400},
    7:  {"fallback_ltv": 0.45, "duration_secs": 7  * 86400},
    15: {"fallback_ltv": 0.25, "duration_secs": 15 * 86400},
}

YOUNG_TOKEN_AGE_DAYS = 60
MIN_MARKET_SAMPLES = 5
MARKET_VOLUME_MULTIPLIER = 2.0             # OR: market volume >= this x our own offer size
YOUNG_TOKEN_DISCOUNT = 0.05                # percentage points below the token's own market LTV
MATURE_TOKEN_COLLATERAL_DISCOUNT = 0.10    # accept this much less collateral than the market average
HARD_LTV_CEILING = 0.75                    # never exceeded, no matter what

KNOWN_DECIMALS = _common.KNOWN_DECIMALS

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("verify_offers")

# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

SESSION = requests.Session()
SESSION.headers.update({"Content-Type": "application/json"})


def _fetch_all_pages(endpoint: str, params: dict | None = None) -> list[dict]:
    return _common.fetch_all_pages(SESSION, API_BASE, endpoint, params, PAGE_SIZE)

# ---------------------------------------------------------------------------
# Price helpers
# ---------------------------------------------------------------------------

def _fetch_dexscreener_price(mint: str) -> float | None:
    try:
        resp = SESSION.get(f"{DEXSCREENER_API}/{mint}", timeout=10)
        if not resp.ok:
            return None
        pairs = resp.json().get("pairs") or []
        sol_pairs = [p for p in pairs if p.get("chainId") == "solana" and p.get("priceUsd")]
        if not sol_pairs:
            return None
        sol_pairs.sort(key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0), reverse=True)
        return float(sol_pairs[0]["priceUsd"])
    except Exception:
        return None


def fetch_current_prices(mints: list[str]) -> tuple[dict[str, float], dict[str, int]]:
    """
    Return ({mint: usd_price_per_whole_token}, {mint: decimals}).

    Jupiter's price response includes each token's `decimals` — we capture that
    here so mints missing from the hardcoded KNOWN_DECIMALS table still get a
    usable price instead of being misreported as having no live price.
    """
    if not mints:
        return {}, {}
    prices: dict[str, float] = {}
    jupiter_decimals: dict[str, int] = {}
    try:
        headers = {"x-api-key": JUPITER_API_KEY} if JUPITER_API_KEY else {}
        resp = SESSION.get(JUPITER_PRICE_API, params={"ids": ",".join(mints)}, headers=headers, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        prices = {m: float(info["usdPrice"]) for m, info in data.items() if info.get("usdPrice")}
        jupiter_decimals = {m: int(info["decimals"]) for m, info in data.items() if info.get("decimals") is not None}
    except Exception as exc:
        log.warning("Jupiter price fetch failed: %s", exc)

    for mint in [m for m in mints if m not in prices]:
        price = _fetch_dexscreener_price(mint)
        if price:
            prices[mint] = price
    return prices, jupiter_decimals

# ---------------------------------------------------------------------------
# Dynamic LTV target — mirrors effective_target_ltv() in strategy.py
# ---------------------------------------------------------------------------

_token_age_cache: dict[str, float | None] = {}


def _token_age_days(mint: str) -> float | None:
    """Days since `mint`'s earliest known DexScreener pool was created. None if unknown."""
    if mint in _token_age_cache:
        return _token_age_cache[mint]
    age_days = None
    try:
        resp = SESSION.get(f"{DEXSCREENER_API}/{mint}", timeout=10)
        if resp.ok:
            created = [p["pairCreatedAt"] for p in (resp.json().get("pairs") or []) if p.get("pairCreatedAt")]
            if created:
                age_days = (time.time() * 1000 - min(created)) / 86_400_000
    except Exception:
        pass
    _token_age_cache[mint] = age_days
    return age_days


def fetch_market_ltv_stats(
    collateral_mints: set[str], raw_offers: list[dict], raw_loans: list[dict]
) -> dict[str, tuple[float | None, int, float, float | None]]:
    """
    For each collateral mint, return (volume-weighted-median LTV, sample count,
    total volume USD, largest live offer's LTV) computed from ALL active lending
    offers + active loans market-wide (every lender, not just us) — the same
    data source strategy.py's PairStats draws from.

    Median, not mean: robust to a single large outlier offer dragging the
    benchmark, matching strategy.py's ltv_benchmark_usd.

    largest_offer_ltv is the LTV of the single largest (by principal USD) live
    OFFER for that mint — loans are excluded here, same as strategy.py's
    largest_offer_ltv, since a borrower comparison-shopping the pool is
    choosing between current listings, not past loans. Used to cap the target
    down (never loosen it) the same way effective_target_ltv() below does, so
    this check reflects the exact same safety ceiling strategy.py would apply
    if run right now.

    Takes already-fetched market data (raw_offers from fetch_all_lending_offers(),
    raw_loans from /loans/status/active) so callers can fetch once per run and
    reuse across every strategy checked, rather than refetching per duration.
    """
    ltvs: dict[str, list[float]] = defaultdict(list)
    weights: dict[str, list[float]] = defaultdict(list)
    largest_principal_usd: dict[str, float] = defaultdict(float)
    largest_offer_ltv: dict[str, float] = {}

    for r in raw_offers:
        cmint = r.get("collateralMint") or _mint_from_asset(r.get("collateral", {}))
        if cmint not in collateral_mints:
            continue
        meta = r.get("metadata") or {}
        p_usd, c_usd = meta.get("principalAmountUsd"), meta.get("collateralAmountUsd")
        if p_usd and c_usd and c_usd > 0:
            ltv = p_usd / c_usd
            ltvs[cmint].append(ltv)
            weights[cmint].append(p_usd)
            if p_usd > largest_principal_usd[cmint]:
                largest_principal_usd[cmint] = p_usd
                largest_offer_ltv[cmint] = ltv

    for r in raw_loans:
        cmint = r.get("collateralMint") or _mint_from_asset(r.get("collateral", {}))
        if cmint not in collateral_mints:
            continue
        meta = r.get("metadata") or {}
        p_usd, c_usd = meta.get("startPrincipalAmountUsd"), meta.get("startCollateralAmountUsd")
        if p_usd and c_usd and c_usd > 0:
            ltvs[cmint].append(p_usd / c_usd)
            weights[cmint].append(p_usd)

    stats: dict[str, tuple[float | None, int, float, float | None]] = {}
    for mint in collateral_mints:
        vals, wts = ltvs.get(mint, []), weights.get(mint, [])
        if not vals:
            stats[mint] = (None, 0, 0.0, None)
            continue
        median_ltv = _common._volume_weighted_median(vals, wts)
        stats[mint] = (median_ltv, len(vals), sum(wts), largest_offer_ltv.get(mint))
    return stats


def effective_target_ltv(
    collateral_mint: str,
    fallback_ltv: float,
    market_weighted_ltv: float | None,
    market_sample_count: int,
    market_total_volume_usd: float = 0.0,
    our_offer_usdc: float = 0.0,
    largest_offer_ltv: float | None = None,
) -> float:
    """Recomputes the same dynamic LTV target strategy.py uses — see
    effective_target_ltv() in strategy.py and README §4 for the full rule.
    "Enough data" means EITHER >= MIN_MARKET_SAMPLES orders OR market volume
    >= MARKET_VOLUME_MULTIPLIER x our_offer_usdc (our_offer_usdc must be > 0
    for the volume path to apply). largest_offer_ltv, when known, caps the
    result down (never loosens it) to the pair's single largest live offer's
    LTV — same guardrail strategy.py applies, guarding against the
    weighted-median benchmark being looser than what the market's most
    prominent participant actually accepts."""
    has_enough_volume = our_offer_usdc > 0 and market_total_volume_usd >= MARKET_VOLUME_MULTIPLIER * our_offer_usdc
    has_enough_data = market_sample_count >= MIN_MARKET_SAMPLES or has_enough_volume

    if not market_weighted_ltv or not has_enough_data:
        target = fallback_ltv
    else:
        age_days = _token_age_days(collateral_mint)
        if age_days is not None and age_days >= YOUNG_TOKEN_AGE_DAYS:
            target = market_weighted_ltv / (1 - MATURE_TOKEN_COLLATERAL_DISCOUNT)
        else:
            target = market_weighted_ltv - YOUNG_TOKEN_DISCOUNT

    if largest_offer_ltv is not None:
        target = min(target, largest_offer_ltv)

    return max(min(target, HARD_LTV_CEILING), 0.05)

# ---------------------------------------------------------------------------
# Offer fetching
# ---------------------------------------------------------------------------

def resolve_signer_wallet() -> str:
    """
    Resolve WALLET_PUBKEY for the active signing mode — matches
    resolve_signer_wallet() in strategy.py so this checks the same wallet it
    signs offers from. Read-only: only queries the device's pubkey, never signs
    anything. Thin wrapper: delegates to offerbook_common.
    """
    global WALLET_PUBKEY
    WALLET_PUBKEY = _common.resolve_signer_wallet(SIGNING_MODE, WALLET_PUBKEY, LEDGER_PATH)
    return WALLET_PUBKEY


def fetch_all_lending_offers() -> list[dict]:
    """
    Return every lending offer on the market (fetched once per run and reused
    across all strategies checked, instead of once per duration).

    showUnverified=true and includeUnderfunded=true are both set — we want to see
    ALL offers regardless of collateral verification or funding status; missing
    one because of either flag would defeat the point of a sanity check (and
    "underfunded" mostly just means "one of several rehypothecated-escrow
    offers" here, not a fake/unbacked one).
    """
    raw: list[dict] = []
    for status in ("active", "partiallyFilled"):
        raw += _fetch_all_pages(
            "/offers",
            {
                "offerType": "lending", "status": status, "hideExpired": "true",
                "showUnverified": "true", "includeUnderfunded": "true",
            },
        )
    return raw


def filter_our_offers(all_offers: list[dict], duration_secs: int) -> list[dict]:
    """Filter already-fetched market offers down to ours, for the given duration."""
    return [
        r for r in all_offers
        if r.get("creator") == WALLET_PUBKEY and r.get("duration") == duration_secs
    ]

# ---------------------------------------------------------------------------
# Core check
# ---------------------------------------------------------------------------

def check_strategy(
    days: int,
    all_offers: list[dict],
    live_prices: dict[str, float],
    decimals_map: dict[str, int],
    market_ltv_stats: dict[str, tuple[float | None, int, float, float | None]],
) -> tuple[int, int, int]:
    """
    Verify all live offers for the given strategy duration.
    Returns (pass_count, warn_count, fail_count).

    Takes market data pre-fetched once in main() (all_offers, live_prices,
    decimals_map, market_ltv_stats) instead of fetching it per duration — with
    --days all this used to refetch the same ~250+ offer market-wide dataset,
    the full active-loans list, and DexScreener prices independently 3 times.
    """
    params       = STRATEGY_PARAMS[days]
    fallback_ltv = params["fallback_ltv"]
    dur_sec      = params["duration_secs"]

    log.info("")
    log.info("=" * 68)
    log.info(
        "Strategy: %d-day  |  LTV floor: %.0f%%, hard ceiling: %.0f%%  |  min APY: %d bps",
        days, fallback_ltv * 100, HARD_LTV_CEILING * 100, MIN_APY_BPS,
    )
    log.info("=" * 68)

    raw_offers = filter_our_offers(all_offers, dur_sec)
    log.info("Found %d offer(s) on-chain for this strategy.", len(raw_offers))

    if not raw_offers:
        log.warning("  No offers found — they may still be propagating, or none were placed.")
        return 0, 0, 0

    pass_count = warn_count = fail_count = 0

    # Header
    col = "{:<10}  {:<10}  {:>8}  {:>6}  {:>7}  {:>7}  {:>10}  {}"
    log.info(col.format("principal", "collateral", "APY bps", "APY %", "LTV %", "Target%", "Vol USDC", "status"))
    log.info("-" * 80)

    for r in raw_offers:
        principal_mint   = r.get("principalMint") or _mint_from_asset(r.get("principal", {}))
        collateral_mint  = r.get("collateralMint") or _mint_from_asset(r.get("collateral", {}))
        principal_raw    = r.get("remainingPrincipal", r.get("principalAmount", 0))
        collateral_raw   = r.get("remainingCollateral", r.get("collateralAmount", 0))
        apy_bps          = r.get("apy", 0)
        duration         = r.get("duration", 0)
        issues: list[str] = []

        # --- APY ---
        if apy_bps < MIN_APY_BPS:
            issues.append(f"APY {apy_bps} bps < floor {MIN_APY_BPS}")

        # --- Duration ---
        if duration != dur_sec:
            issues.append(f"duration {duration}s != expected {dur_sec}s")

        # --- Principal dust ---
        if principal_raw < 1000:
            issues.append(f"dust principal ({principal_raw} raw units)")

        # --- LTV (live prices, against the recomputed dynamic target) ---
        collateral_price = live_prices.get(collateral_mint)
        decimals         = decimals_map.get(collateral_mint)
        live_ltv: float | None = None
        vol_usdc: float | None = None

        principal_usdc = principal_raw / 10 ** USDC_DECIMALS
        market_weighted_ltv, market_sample_count, market_total_volume_usd, largest_offer_ltv = market_ltv_stats.get(
            collateral_mint, (None, 0, 0.0, None)
        )
        target_ltv = effective_target_ltv(
            collateral_mint, fallback_ltv, market_weighted_ltv, market_sample_count,
            market_total_volume_usd, principal_usdc, largest_offer_ltv,
        )

        if collateral_price and decimals is not None and collateral_raw > 0:
            collateral_usdc = (collateral_raw / 10 ** decimals) * collateral_price
            vol_usdc = principal_usdc
            if collateral_usdc > 0:
                live_ltv = principal_usdc / collateral_usdc
            if live_ltv is not None and live_ltv > target_ltv * 1.02:  # 2 % tolerance
                issues.append(f"LTV {live_ltv*100:.1f}% > target {target_ltv*100:.0f}%")
        elif collateral_raw > 0:
            issues.append(f"no live price for collateral {collateral_mint[:8]}…")

        # --- Verdict ---
        if any("LTV" in i for i in issues):
            verdict = "FAIL"
            fail_count += 1
        elif issues:
            verdict = "WARN"
            warn_count += 1
        else:
            verdict = "PASS"
            pass_count += 1

        ltv_str = f"{live_ltv*100:.1f}" if live_ltv is not None else "n/a"
        vol_str = f"{vol_usdc:.2f}"      if vol_usdc  is not None else "n/a"
        flag    = ("  !! " + " | ".join(issues)) if issues else ""

        log.info(
            col.format(
                (principal_mint[:8]  + "…") if len(principal_mint)  > 8 else principal_mint,
                (collateral_mint[:8] + "…") if len(collateral_mint) > 8 else collateral_mint,
                apy_bps,
                f"{apy_bps/100:.2f}",
                ltv_str,
                f"{target_ltv*100:.0f}",
                vol_str,
                verdict + flag,
            )
        )

    log.info("-" * 80)
    log.info(
        "Result: PASS=%d  WARN=%d  FAIL=%d  (of %d offer(s))",
        pass_count, warn_count, fail_count, len(raw_offers),
    )

    if fail_count:
        log.error("CRITICAL: %d offer(s) exceeded LTV limit — cancel and re-post immediately!", fail_count)
    elif warn_count:
        log.warning("%d offer(s) flagged with warnings — review above.", warn_count)
    else:
        log.info("All offers passed.")

    return pass_count, warn_count, fail_count

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Verify live Offerbook lending offers.")
    parser.add_argument(
        "--days",
        default="all",
        choices=["3", "7", "15", "all"],
        help="Which strategy to check (default: all)",
    )
    args = parser.parse_args()

    resolve_signer_wallet()
    if not WALLET_PUBKEY:
        log.error("OFFERBOOK_WALLET env var not set (and no Ledger available). Exiting.")
        sys.exit(1)

    log.info("Offerbook Offer Sanity Check")
    log.info("Wallet : %s  (%s)", WALLET_PUBKEY, SIGNING_MODE)

    strategies = [3, 7, 15] if args.days == "all" else [int(args.days)]

    # Fetch market-wide data once and reuse across every strategy checked below,
    # instead of refetching independently per duration.
    log.info("Fetching all lending offers market-wide …")
    all_offers = fetch_all_lending_offers()
    log.info("  → %d lending offer(s)", len(all_offers))
    log.info("Fetching all active loans market-wide …")
    all_loans = _fetch_all_pages("/loans/status/active")
    log.info("  → %d active loan(s)", len(all_loans))

    our_offers = [r for r in all_offers if r.get("creator") == WALLET_PUBKEY]
    collateral_mints = list({
        _mint_from_asset(r.get("collateral", {})) or r.get("collateralMint", "")
        for r in our_offers
    })
    log.info("Fetching live prices for %d collateral token(s) …", len(collateral_mints))
    live_prices, jupiter_decimals = fetch_current_prices(collateral_mints)
    decimals_map = {**jupiter_decimals, **KNOWN_DECIMALS}  # KNOWN_DECIMALS (curated) wins on conflict

    log.info("Computing market-wide LTV data for %d collateral token(s) …", len(collateral_mints))
    market_ltv_stats = fetch_market_ltv_stats(set(collateral_mints), all_offers, all_loans)

    total_fail = 0
    for days in strategies:
        _, _, fail = check_strategy(days, all_offers, live_prices, decimals_map, market_ltv_stats)
        total_fail += fail

    log.info("")
    if total_fail:
        log.error("OVERALL: %d LTV violation(s) found across all strategies. Review immediately.", total_fail)
        sys.exit(1)
    else:
        log.info("OVERALL: All checked offers passed sanity checks.")


if __name__ == "__main__":
    main()
