"""
Offerbook Post-Placement Sanity Check
======================================
Run this after placing orders to verify that every live offer has:
  - LTV within the strategy's limit (using fresh on-chain prices)
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

import requests
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------

API_BASE        = os.getenv("OFFERBOOK_API_BASE", "https://api.offerbook.jup.ag/api/v1")
SOLANA_RPC      = os.getenv("SOLANA_RPC", "https://api.mainnet-beta.solana.com")
WALLET_PUBKEY   = os.getenv("OFFERBOOK_WALLET", "")

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

STRATEGY_PARAMS: dict[int, dict] = {
    3:  {"max_ltv": 0.65, "duration_secs": 3  * 86400},
    7:  {"max_ltv": 0.45, "duration_secs": 7  * 86400},
    15: {"max_ltv": 0.25, "duration_secs": 15 * 86400},
}

KNOWN_DECIMALS: dict[str, int] = {
    "So11111111111111111111111111111111111111112":   9,  # wSOL
    "mSoLzYCxHdYgdzU16g5QSh3i5K3z3KZK7ytfqcJm7So": 9,  # mSOL
    "bSo13r4TkiE4KumL71LsHTPpL2euBYLFx6h9HP3piy1": 9,  # bSOL
    "jupSoLaHXQiZZTSfEWMTRRgpnyFm8f6sZdosWBjx93v": 9,  # JupSOL
    "J1toso1uCk3RLmjorhTtrVwY9HJ7X8V9yYac6Y7kGCPn":9,  # jitoSOL
    "Bybit2vBJGhPF52GBdNaQfUJ6ZpThSgHBobjWZpLPb4B":9,  # bbSOL
    "BNso1VUJnh4zcfpZa6986Ea66P6TCp59hvtNJ8b1X85": 9,  # bnSOL
    "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN": 6,  # JUP
    "4k3Dyjzvzp8eMZWUXbBCjEvwSkkk59S5iCNLY3QrkX6R":6,  # JLP
    "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263":5,  # BONK
    "hntyVP6YFm1Hg25TN9WGLqM12b8TQmcknKrdu1oxWux": 8,  # HNT
    "nosXBVoaCTtYdLvKY6Csb4AC8JCdQKKAaWYtx2ZMoo7": 6,  # NOS
    "WENWENvqNAA8883GttHGFApfgzGLtzHain8QxAwYQst":  5,  # WEN
    "cbbtcf3aa214zXHbiAZQwf4122FBYbraNdFqgw4iMij":  8,  # cbBTC
    "kyKYFGGhy5YAg6Yotedj7ZtByUBepsraT4BFkF3Uxmk": 6,  # kyKYROS
    "stke7uu3fXHsGqKVVjKnkmj65LRPVrqr4bLG2SJg7rh": 9,  # stKE
    "pumpCmXqMfrsAkQ5r49WcJnRayYRqmXz6ae8H7H9Dfn":  6,  # PUMP
    "5z3EqYQo9HiCEs3R84RCDMu2n7anpDMxRhdK8PSWmrRC": 9,  # SMRT
    "Dz9mQ9NzkBcCsuGPFJ3r1bS4wgqKMHBPiVuniW8Mbonk": 6,  # USELESS
    "Ce2gx9KGXJ6C9Mp5b5x1sn9Mg87JwEbrQby4Zqo3pump": 6,  # NEET
    "A7bdiYdS5GjqGFtxf17ppRHtDKPkkRqbKtR27dxvQXaS": 8,  # ZEC
    "98sMhvDwXj1RQi5c5Mndm3vPe9cBqPrbLaufMXFNMh5g": 9,  # HYPE
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v": 6,  # USDC
}

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


def _get(endpoint: str, params: dict | None = None) -> dict:
    url = f"{API_BASE}{endpoint}"
    resp = SESSION.get(url, params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


def _fetch_all_pages(endpoint: str, params: dict | None = None) -> list[dict]:
    params = dict(params or {})
    params["limit"] = PAGE_SIZE
    params["offset"] = 0
    items: list[dict] = []
    while True:
        data = _get(endpoint, params)
        page = data.get("data", [])
        items.extend(page)
        if not data.get("pagination", {}).get("hasMore", False):
            break
        params["offset"] += PAGE_SIZE
        time.sleep(0.15)
    return items

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


def fetch_current_prices(mints: list[str]) -> dict[str, float]:
    if not mints:
        return {}
    prices: dict[str, float] = {}
    try:
        headers = {"x-api-key": JUPITER_API_KEY} if JUPITER_API_KEY else {}
        resp = SESSION.get(JUPITER_PRICE_API, params={"ids": ",".join(mints)}, headers=headers, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        prices = {m: float(info["usdPrice"]) for m, info in data.items() if info.get("usdPrice")}
    except Exception as exc:
        log.warning("Jupiter price fetch failed: %s", exc)

    for mint in [m for m in mints if m not in prices]:
        price = _fetch_dexscreener_price(mint)
        if price:
            prices[mint] = price
    return prices

# ---------------------------------------------------------------------------
# Offer fetching
# ---------------------------------------------------------------------------

def _mint_from_asset(asset: dict) -> str:
    return asset.get("mint") or asset.get("data", {}).get("mint") or asset.get("data", {}).get("asset") or ""


def fetch_our_offers_for_duration(duration_secs: int) -> list[dict]:
    """Return raw offer dicts belonging to this wallet with the given duration."""
    raw: list[dict] = []
    for status in ("active", "partiallyFilled"):
        raw += _fetch_all_pages(
            "/offers",
            {"offerType": "lending", "status": status, "hideExpired": "true"},
        )
    return [
        r for r in raw
        if r.get("creator") == WALLET_PUBKEY and r.get("duration") == duration_secs
    ]

# ---------------------------------------------------------------------------
# Core check
# ---------------------------------------------------------------------------

def check_strategy(days: int) -> tuple[int, int, int]:
    """
    Verify all live offers for the given strategy duration.
    Returns (pass_count, warn_count, fail_count).
    """
    params  = STRATEGY_PARAMS[days]
    max_ltv = params["max_ltv"]
    dur_sec = params["duration_secs"]

    log.info("")
    log.info("=" * 68)
    log.info("Strategy: %d-day  |  max LTV: %.0f%%  |  min APY: %d bps", days, max_ltv * 100, MIN_APY_BPS)
    log.info("=" * 68)

    raw_offers = fetch_our_offers_for_duration(dur_sec)
    log.info("Found %d offer(s) on-chain for this strategy.", len(raw_offers))

    if not raw_offers:
        log.warning("  No offers found — they may still be propagating, or none were placed.")
        return 0, 0, 0

    collateral_mints = list({
        _mint_from_asset(r.get("collateral", {})) or r.get("collateralMint", "")
        for r in raw_offers
    })
    log.info("Fetching live prices for %d collateral token(s) …", len(collateral_mints))
    live_prices = fetch_current_prices(collateral_mints)

    pass_count = warn_count = fail_count = 0

    # Header
    col = "{:<10}  {:<10}  {:>8}  {:>6}  {:>7}  {:>7}  {:>10}  {}"
    log.info(col.format("principal", "collateral", "APY bps", "APY %", "LTV %", "MaxLTV", "Vol USDC", "status"))
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

        # --- LTV (live prices) ---
        collateral_price = live_prices.get(collateral_mint)
        decimals         = KNOWN_DECIMALS.get(collateral_mint)
        live_ltv: float | None = None
        vol_usdc: float | None = None

        if collateral_price and decimals is not None and collateral_raw > 0:
            principal_usdc  = principal_raw / 10 ** USDC_DECIMALS
            collateral_usdc = (collateral_raw / 10 ** decimals) * collateral_price
            vol_usdc = principal_usdc
            if collateral_usdc > 0:
                live_ltv = principal_usdc / collateral_usdc
            if live_ltv is not None and live_ltv > max_ltv * 1.02:  # 2 % tolerance
                issues.append(f"LTV {live_ltv*100:.1f}% > limit {max_ltv*100:.0f}%")
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
                f"{max_ltv*100:.0f}",
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

    if not WALLET_PUBKEY:
        log.error("OFFERBOOK_WALLET env var not set. Exiting.")
        sys.exit(1)

    log.info("Offerbook Offer Sanity Check")
    log.info("Wallet : %s", WALLET_PUBKEY)

    strategies = [3, 7, 15] if args.days == "all" else [int(args.days)]

    total_fail = 0
    for days in strategies:
        _, _, fail = check_strategy(days)
        total_fail += fail

    log.info("")
    if total_fail:
        log.error("OVERALL: %d LTV violation(s) found across all strategies. Review immediately.", total_fail)
        sys.exit(1)
    else:
        log.info("OVERALL: All checked offers passed sanity checks.")


if __name__ == "__main__":
    main()
