"""
Offerbook Soon-to-Expire Loan Scanner
======================================
Scans all active loans on Offerbook and surfaces the ones that are already
past their expiry (still marked "active" — not yet repaid/defaulted) or
expiring within a configurable window.

Usage:
  python soon_to_expire.py                      # default: next 48h + already expired
  python soon_to_expire.py --hours 24            # next 24h + already expired
  python soon_to_expire.py --no-expired          # skip the already-expired bucket

Exit codes:
  0 — nothing in the expired/soon-to-expire window
  1 — one or more loans found
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

API_BASE = os.getenv("OFFERBOOK_API_BASE", "https://api.offerbook.jup.ag/api/v1")
JUPITER_PRICE_API = "https://api.jup.ag/price/v3"
JUPITER_API_KEY = os.getenv("JUPITER_API_KEY", "")
DEXSCREENER_API = "https://api.dexscreener.com/latest/dex/tokens"

USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
PAGE_SIZE = 100

KNOWN_DECIMALS: dict[str, tuple[int, str]] = {
    "So11111111111111111111111111111111111111112": (9, "SOL"),
    "mSoLzYCxHdYgdzU16g5QSh3i5K3z3KZK7ytfqcJm7So": (9, "mSOL"),
    "bSo13r4TkiE4KumL71LsHTPpL2euBYLFx6h9HP3piy1": (9, "bSOL"),
    "jupSoLaHXQiZZTSfEWMTRRgpnyFm8f6sZdosWBjx93v": (9, "JupSOL"),
    "J1toso1uCk3RLmjorhTtrVwY9HJ7X8V9yYac6Y7kGCPn": (9, "jitoSOL"),
    "Bybit2vBJGhPF52GBdNaQfUJ6ZpThSgHBobjWZpLPb4B": (9, "bbSOL"),
    "BNso1VUJnh4zcfpZa6986Ea66P6TCp59hvtNJ8b1X85": (9, "bnSOL"),
    "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN": (6, "JUP"),
    "4k3Dyjzvzp8eMZWUXbBCjEvwSkkk59S5iCNLY3QrkX6R": (6, "JLP"),
    "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263": (5, "BONK"),
    "hntyVP6YFm1Hg25TN9WGLqM12b8TQmcknKrdu1oxWux": (8, "HNT"),
    "nosXBVoaCTtYdLvKY6Csb4AC8JCdQKKAaWYtx2ZMoo7": (6, "NOS"),
    "WENWENvqNAA8883GttHGFApfgzGLtzHain8QxAwYQst": (5, "WEN"),
    "cbbtcf3aa214zXHbiAZQwf4122FBYbraNdFqgw4iMij": (8, "cbBTC"),
    "kyKYFGGhy5YAg6Yotedj7ZtByUBepsraT4BFkF3Uxmk": (6, "kyKYROS"),
    "stke7uu3fXHsGqKVVjKnkmj65LRPVrqr4bLG2SJg7rh": (9, "stKE"),
    "pumpCmXqMfrsAkQ5r49WcJnRayYRqmXz6ae8H7H9Dfn": (6, "PUMP"),
    "5z3EqYQo9HiCEs3R84RCDMu2n7anpDMxRhdK8PSWmrRC": (9, "SMRT"),
    "Dz9mQ9NzkBcCsuGPFJ3r1bS4wgqKMHBPiVuniW8Mbonk": (6, "USELESS"),
    "Ce2gx9KGXJ6C9Mp5b5x1sn9Mg87JwEbrQby4Zqo3pump": (6, "NEET"),
    "A7bdiYdS5GjqGFtxf17ppRHtDKPkkRqbKtR27dxvQXaS": (8, "ZEC"),
    "98sMhvDwXj1RQi5c5Mndm3vPe9cBqPrbLaufMXFNMh5g": (9, "HYPE"),
    USDC_MINT: (6, "USDC"),
}

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("soon_to_expire")

SESSION = requests.Session()

# ---------------------------------------------------------------------------
# Fetch helpers
# ---------------------------------------------------------------------------

def fetch_all_active_loans() -> list[dict]:
    items: list[dict] = []
    offset = 0
    while True:
        resp = SESSION.get(
            f"{API_BASE}/loans/status/active",
            params={"limit": PAGE_SIZE, "offset": offset},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        page = data.get("data", [])
        items.extend(page)
        if not data.get("pagination", {}).get("hasMore", False):
            break
        offset += PAGE_SIZE
        time.sleep(0.15)
    return items


def _asset_mint(asset: dict) -> str | None:
    if asset.get("kind") == "token":
        return asset.get("mint")
    return None  # NFT (coreNft / classicNft / programmableNft)


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


def fetch_prices(mints: set[str]) -> tuple[dict[str, float], dict[str, int]]:
    """
    Return ({mint: usd_price_per_whole_token}, {mint: decimals}).

    Jupiter's price response includes each token's `decimals` — we capture that
    here so mints missing from the hardcoded KNOWN_DECIMALS table (which also
    carries a display symbol) can still be formatted and priced, just without
    a friendly symbol.
    """
    prices: dict[str, float] = {}
    jupiter_decimals: dict[str, int] = {}
    mints = {m for m in mints if m and m != USDC_MINT}
    mints_list = list(mints)
    for i in range(0, len(mints_list), 50):
        chunk = mints_list[i : i + 50]
        try:
            headers = {"x-api-key": JUPITER_API_KEY} if JUPITER_API_KEY else {}
            resp = SESSION.get(JUPITER_PRICE_API, params={"ids": ",".join(chunk)}, headers=headers, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            prices.update({m: float(info["usdPrice"]) for m, info in data.items() if info.get("usdPrice")})
            jupiter_decimals.update(
                {m: int(info["decimals"]) for m, info in data.items() if info.get("decimals") is not None}
            )
        except Exception as exc:
            log.warning("Jupiter price batch %d failed: %s", i // 50, exc)

    missing = [m for m in mints if m not in prices]
    for mint in missing:
        price = _fetch_dexscreener_price(mint)
        if price:
            prices[mint] = price

    prices[USDC_MINT] = 1.0
    return prices, jupiter_decimals


def fmt_amount(mint: str | None, raw: int, decimals_map: dict[str, int]) -> str:
    if mint and mint in KNOWN_DECIMALS:
        dec, sym = KNOWN_DECIMALS[mint]
        return f"{raw / 10**dec:,.4f} {sym}"
    if mint and mint in decimals_map:
        return f"{raw / 10**decimals_map[mint]:,.4f} ({mint[:6]}…{mint[-4:]})"
    if mint:
        return f"{raw} raw ({mint[:6]}…{mint[-4:]})"
    return "1 NFT"


def usd_value(mint: str | None, raw: int, prices: dict[str, float], decimals_map: dict[str, int]) -> float | None:
    if not mint:
        return None
    if mint in KNOWN_DECIMALS:
        dec = KNOWN_DECIMALS[mint][0]
    elif mint in decimals_map:
        dec = decimals_map[mint]
    else:
        return None
    price = prices.get(mint)
    if not price:
        return None
    return (raw / 10**dec) * price

# ---------------------------------------------------------------------------
# Scan
# ---------------------------------------------------------------------------

def bucket_loans(loans: list[dict], now: datetime, window_hours: float) -> dict[str, list[dict]]:
    buckets: dict[str, list[dict]] = {"expired": [], "soon": []}
    for loan in loans:
        expired_at = datetime.fromisoformat(loan["expiredAt"].replace("Z", "+00:00"))
        delta_hours = (expired_at - now).total_seconds() / 3600.0
        loan["_expired_at"] = expired_at
        loan["_delta_hours"] = delta_hours
        if delta_hours < 0:
            buckets["expired"].append(loan)
        elif delta_hours <= window_hours:
            buckets["soon"].append(loan)
    return buckets


def print_bucket(label: str, rows: list[dict], prices: dict[str, float], decimals_map: dict[str, int]) -> None:
    rows = sorted(rows, key=lambda l: l["_delta_hours"])
    log.info("")
    log.info("=" * 100)
    log.info("%s  (%d loan%s)", label, len(rows), "" if len(rows) == 1 else "s")
    log.info("=" * 100)

    if not rows:
        log.info("  none")
        return

    for loan in rows:
        pmint = loan.get("principalMint") or _asset_mint(loan.get("principal", {}))
        cmint = loan.get("collateralMint") or _asset_mint(loan.get("collateral", {}))
        p_amt = fmt_amount(pmint, loan["principalAmount"], decimals_map)
        c_amt = fmt_amount(cmint, loan["collateralAmount"], decimals_map)
        p_usd = usd_value(pmint, loan["principalAmount"], prices, decimals_map)
        c_usd = usd_value(cmint, loan["collateralAmount"], prices, decimals_map)

        hrs = loan["_delta_hours"]
        when = f"expired {abs(hrs):.1f}h ago" if hrs < 0 else f"expires in {hrs:.1f}h"

        log.info("")
        log.info("  pubkey     : %s", loan["pubkey"])
        log.info("  %s  (expiredAt %s)", when, loan["_expired_at"].isoformat())
        log.info("  loan       : %s%s", p_amt, f"  (~${p_usd:,.2f})" if p_usd else "")
        log.info("  collateral : %s%s", c_amt, f"  (~${c_usd:,.2f})" if c_usd else "")
        log.info("  apy        : %.2f%%   duration: %.1fd", loan["apy"] / 100, loan["duration"] / 86400)
        log.info("  lender     : %s", loan["lender"])
        log.info("  borrower   : %s", loan["borrower"])


def main() -> None:
    parser = argparse.ArgumentParser(description="Surface Offerbook loans that are expiring soon or already expired.")
    parser.add_argument(
        "--hours", type=float, default=48.0,
        help="Look-ahead window in hours for 'soon to expire' (default: 48)",
    )
    parser.add_argument(
        "--no-expired", action="store_true",
        help="Skip the already-expired bucket, only show the soon-to-expire window",
    )
    args = parser.parse_args()

    now = datetime.now(timezone.utc)

    log.info("Fetching active loans …")
    loans = fetch_all_active_loans()
    log.info("  → %d active loan(s) fetched", len(loans))

    buckets = bucket_loans(loans, now, args.hours)
    relevant = buckets["soon"] + ([] if args.no_expired else buckets["expired"])

    all_mints: set[str] = set()
    for loan in relevant:
        pmint = loan.get("principalMint") or _asset_mint(loan.get("principal", {}))
        cmint = loan.get("collateralMint") or _asset_mint(loan.get("collateral", {}))
        all_mints.add(pmint or "")
        all_mints.add(cmint or "")
    all_mints.discard("")

    log.info("Fetching live prices for %d unique token(s) …", len(all_mints))
    prices, decimals_map = fetch_prices(all_mints)

    if not args.no_expired:
        print_bucket("ALREADY EXPIRED (still active)", buckets["expired"], prices, decimals_map)
    print_bucket(f"EXPIRING WITHIN {args.hours:.0f}H", buckets["soon"], prices, decimals_map)

    sys.exit(1 if relevant else 0)


if __name__ == "__main__":
    main()
