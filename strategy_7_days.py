"""
Offerbook Competitive Lending Bot — 7-Day Strategy
====================================================
Loan term : 7 days   |   Max LTV : 45%   |   Offer listing expires : 24 h
Collateral must be worth ≥ 2.22× the loan at current prices.

Strategy:
  1. Fetch all active lending offers (from all pools/pairs).
  2. Fetch all active loans.
  3. For each unique (principalMint, collateralMint) pair that has open loans or
     open lending offers, compute the mean APY of existing active lending offers.
  4. Post a new lending offer at mean_apy * 0.90 (10% below mean) to be competitive.
  5. Enforce:
       - duration  <= 7 days (604,800 seconds)
       - LTV       <= 45%  (collateral must be ≥ 2.22× loan value at current prices)

Usage:
  pip install requests solders base58
  export OFFERBOOK_WALLET=<your-base58-wallet-pubkey>
  export OFFERBOOK_PRIVATE_KEY=<your-base58-private-key>   # for signing txns
  python strategy_7_days.py

Notes:
  - The Offerbook transaction API returns a base64-encoded Solana transaction.
    This script signs and submits it via the Solana RPC.
  - Set DRY_RUN=true to preview offers without submitting.
  - APY is stored in basis points (1% = 100 bps).  LTV uses the USD metadata
    values the API enriches; if USD values are absent we fall back to a raw
    token-amount ratio (same token pair assumed to be same price units).
"""
from __future__ import annotations

from dotenv import load_dotenv
load_dotenv()

import argparse
import base64
import logging
import os
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

import requests
import yaml  # pip install pyyaml

# ---------------------------------------------------------------------------
# Configuration – edit here or override via environment variables
# ---------------------------------------------------------------------------

API_BASE = os.getenv("OFFERBOOK_API_BASE", "https://api.offerbook.jup.ag/api/v1")
TX_API_BASE = os.getenv("OFFERBOOK_TX_API_BASE", "https://builder.offerbook.jup.ag/api/v1")
SOLANA_RPC = os.getenv("SOLANA_RPC", "https://api.mainnet-beta.solana.com")

WALLET_PUBKEY: str = os.getenv("OFFERBOOK_WALLET", "")
PRIVATE_KEY_B58: str = os.getenv("OFFERBOOK_PRIVATE_KEY", "")  # base58 private key

# "ledger" or "private_key" — Ledger is the default signing mode.
SIGNING_MODE: str = os.getenv("OFFERBOOK_SIGNING_MODE", "ledger").strip().lower()
LEDGER_PATH: str = os.getenv("OFFERBOOK_LEDGER_PATH", "44'/501'/0'")

# Strategy parameters
APY_DISCOUNT = 0.10          # undercut market mean by 10%
MAX_DURATION_DAYS = 7
MAX_DURATION_SECS = MAX_DURATION_DAYS * 24 * 60 * 60   # 604 800
OFFER_EXPIRY_SECS = 1 * 24 * 60 * 60  # offer listing expires in 24 h; loan term is still 7 days

# LTV target is computed per-token from that token's own market data (see
# effective_target_ltv() below) rather than a single fixed ceiling:
#   - Not enough data to trust the token's own market → use FALLBACK_LTV.
#     "Enough data" means EITHER >= MIN_MARKET_SAMPLES offers/loans, OR total
#     market volume >= MARKET_VOLUME_MULTIPLIER x our own offer size — a few
#     small dust offers don't count, but one large, capital-backed offer can
#     qualify on its own even without 5 orders.
#   - Token younger than YOUNG_TOKEN_AGE_DAYS (or of unknown age, fail safe) →
#     target = token's own weighted-mean market LTV - YOUNG_TOKEN_DISCOUNT.
#   - Token older than YOUNG_TOKEN_AGE_DAYS → target = weighted-mean market LTV
#     / (1 - MATURE_TOKEN_COLLATERAL_DISCOUNT), i.e. we require
#     MATURE_TOKEN_COLLATERAL_DISCOUNT less collateral than the market average
#     to be more attractive to borrowers.
# HARD_LTV_CEILING always applies on top, regardless of which branch produced
# the target.
FALLBACK_LTV = 0.45                        # used when there's not enough market data for the token
YOUNG_TOKEN_AGE_DAYS = 60
MIN_MARKET_SAMPLES = 5
MARKET_VOLUME_MULTIPLIER = 2.0             # OR: market volume >= this x our own offer size
YOUNG_TOKEN_DISCOUNT = 0.05                # percentage points below the token's own market LTV
MATURE_TOKEN_COLLATERAL_DISCOUNT = 0.10    # accept this much less collateral than the market average
HARD_LTV_CEILING = 0.75                    # never exceeded, no matter what

MIN_APY_BPS = 10             # never go below 0.10% APY (10 bps) – sanity floor
ALLOW_PARTIAL_FILL = True    # let borrowers partially fill our offer

DRY_RUN: bool = os.getenv("DRY_RUN", "false").lower() in ("1", "true", "yes")

PAGE_SIZE = 100              # items per API page

# USDC mint (principal token for most pairs)
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDC_DECIMALS = 6

# Optional hard cap per individual offer, in whole USDC (0 = use market median)
_cap_env = os.getenv("MAX_OFFER_PRINCIPAL_USDC", "0")
MAX_OFFER_PRINCIPAL_USDC: int | None = int(_cap_env) if _cap_env.strip() not in ("", "0") else None

# Jupiter Price API — used to get real-time collateral token prices so that
# collateralAmount is always sized to enforce effective_target_ltv() at current market
# prices, not at whatever stale price the market's existing offers were created with.
JUPITER_PRICE_API = "https://api.jup.ag/price/v3"
JUPITER_API_KEY   = os.getenv("JUPITER_API_KEY", "")
DEXSCREENER_API = "https://api.dexscreener.com/latest/dex/tokens"

# Decimals for collateral tokens.  Needed to convert Jupiter's per-whole-token
# price into a per-raw-unit price that the Offerbook API uses.
# Add any token you trade here.  Unknown tokens fall back to a stale implied
# price inferred from offer metadata (less accurate, but better than nothing).
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

# Symbol -> mint, for the --collateral CLI filter (accepts either).
SYMBOL_TO_MINT: dict[str, str] = {
    "SOL": "So11111111111111111111111111111111111111112",
    "MSOL": "mSoLzYCxHdYgdzU16g5QSh3i5K3z3KZK7ytfqcJm7So",
    "BSOL": "bSo13r4TkiE4KumL71LsHTPpL2euBYLFx6h9HP3piy1",
    "JUPSOL": "jupSoLaHXQiZZTSfEWMTRRgpnyFm8f6sZdosWBjx93v",
    "JITOSOL": "J1toso1uCk3RLmjorhTtrVwY9HJ7X8V9yYac6Y7kGCPn",
    "BBSOL": "Bybit2vBJGhPF52GBdNaQfUJ6ZpThSgHBobjWZpLPb4B",
    "BNSOL": "BNso1VUJnh4zcfpZa6986Ea66P6TCp59hvtNJ8b1X85",
    "JUP": "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN",
    "JLP": "4k3Dyjzvzp8eMZWUXbBCjEvwSkkk59S5iCNLY3QrkX6R",
    "BONK": "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263",
    "HNT": "hntyVP6YFm1Hg25TN9WGLqM12b8TQmcknKrdu1oxWux",
    "NOS": "nosXBVoaCTtYdLvKY6Csb4AC8JCdQKKAaWYtx2ZMoo7",
    "WEN": "WENWENvqNAA8883GttHGFApfgzGLtzHain8QxAwYQst",
    "CBBTC": "cbbtcf3aa214zXHbiAZQwf4122FBYbraNdFqgw4iMij",
    "KYKYROS": "kyKYFGGhy5YAg6Yotedj7ZtByUBepsraT4BFkF3Uxmk",
    "STKE": "stke7uu3fXHsGqKVVjKnkmj65LRPVrqr4bLG2SJg7rh",
    "PUMP": "pumpCmXqMfrsAkQ5r49WcJnRayYRqmXz6ae8H7H9Dfn",
    "SMRT": "5z3EqYQo9HiCEs3R84RCDMu2n7anpDMxRhdK8PSWmrRC",
    "USELESS": "Dz9mQ9NzkBcCsuGPFJ3r1bS4wgqKMHBPiVuniW8Mbonk",
    "NEET": "Ce2gx9KGXJ6C9Mp5b5x1sn9Mg87JwEbrQby4Zqo3pump",
    "ZEC": "A7bdiYdS5GjqGFtxf17ppRHtDKPkkRqbKtR27dxvQXaS",
    "HYPE": "98sMhvDwXj1RQi5c5Mndm3vPe9cBqPrbLaufMXFNMh5g",
}

# ---------------------------------------------------------------------------
# Allocation config  (allocation_config.yaml)
# ---------------------------------------------------------------------------

def _load_allocation_config(path: str) -> dict[str, float]:
    """
    Load per-collateral allocation fractions from YAML.
    Returns a dict keyed by collateral mint address, plus a 'default' key.
    Tokens listed here bypass the USD LTV filter; unlisted tokens still go
    through the LTV filter and then use 'default' if they pass.
    """
    try:
        with open(path) as fh:
            raw = yaml.safe_load(fh)
        result: dict[str, float] = {}
        for mint, fraction in (raw.get("allocations") or {}).items():
            result[str(mint)] = float(fraction)
        result["default"] = float(raw.get("default", 0.0))
        return result
    except FileNotFoundError:
        return {"default": 0.0}
    except Exception as exc:
        raise RuntimeError(f"Failed to load allocation_config.yaml: {exc}") from exc

_CONFIG_PATH = os.getenv(
    "ALLOCATION_CONFIG",
    os.path.join(os.path.dirname(__file__), "allocation_config.yaml"),
)
ALLOCATION_CONFIG: dict[str, float] = _load_allocation_config(_CONFIG_PATH)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("offerbook_bot")


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class Offer:
    pubkey: str
    creator: str
    offer_type: str          # "lending" | "borrowing"
    status: str
    principal_mint: str
    collateral_mint: str
    principal_amount: int    # raw token units
    collateral_amount: int
    apy: int                 # basis points
    duration: int            # seconds
    principal_usd: float | None
    collateral_usd: float | None

    @property
    def ltv(self) -> float | None:
        """Loan-to-value: principalUsd / collateralUsd."""
        if self.principal_usd and self.collateral_usd and self.collateral_usd > 0:
            return self.principal_usd / self.collateral_usd
        return None

    @property
    def pair(self) -> tuple[str, str]:
        return (self.principal_mint, self.collateral_mint)


@dataclass
class Loan:
    pubkey: str
    lender: str
    borrower: str
    status: str
    principal_mint: str
    collateral_mint: str
    principal_amount: int
    collateral_amount: int
    apy: int
    duration: int
    principal_usd: float | None = None   # USD value at loan creation
    collateral_usd: float | None = None  # USD value at loan creation

    @property
    def ltv(self) -> float | None:
        """Loan-to-value at creation: principalUsd / collateralUsd."""
        if self.principal_usd and self.collateral_usd and self.collateral_usd > 0:
            return self.principal_usd / self.collateral_usd
        return None

    @property
    def pair(self) -> tuple[str, str]:
        return (self.principal_mint, self.collateral_mint)


@dataclass
class PairStats:
    """Aggregated stats for a (principalMint, collateralMint) pair."""
    principal_mint: str
    collateral_mint: str
    lending_apys: list[int] = field(default_factory=list)               # same-duration offers
    lending_offer_amounts: list[int] = field(default_factory=list)      # principal per same-duration offer
    global_lending_apys: list[int] = field(default_factory=list)        # all-duration offers (fallback)
    global_lending_offer_amounts: list[int] = field(default_factory=list)
    loan_apys: list[int] = field(default_factory=list)                  # existing loans
    active_loan_count: int = 0

    # Amounts from existing offers – we'll size our offer similarly
    offer_principal_amounts: list[int] = field(default_factory=list)
    offer_collateral_amounts: list[int] = field(default_factory=list)

    # USD LTV values (principal_usd / collateral_usd) from market offers
    offer_ltv_usds: list[float] = field(default_factory=list)
    # principal_usd weight for each entry in offer_ltv_usds (same index), for volume-weighting
    offer_ltv_weights_usd: list[float] = field(default_factory=list)

    # Implied collateral price per raw unit from offer/loan USD metadata.
    # Used as fallback when Jupiter price is unavailable (stale but self-consistent).
    offer_collateral_prices_per_raw: list[float] = field(default_factory=list)

    # Fallback amounts from existing loans when no offers exist
    loan_principal_amounts: list[int] = field(default_factory=list)
    loan_collateral_amounts: list[int] = field(default_factory=list)

    # The single largest live lending offer for this pair (any duration), by
    # principal USD — a borrower comparison-shopping the pool is most likely
    # to pick this one. Used as an extra safety guardrail (never a driver of
    # aggressiveness): our own target can be tightened toward this offer's
    # terms if they're more conservative than the weighted-mean-derived
    # target, but never loosened past it.
    largest_offer_principal_usd: float = 0.0
    largest_offer_apy_bps: int | None = None
    largest_offer_ltv: float | None = None

    @property
    def mean_apy_bps(self) -> float | None:
        # Prefer same-duration offers; fall back to global cross-duration mean.
        # Volume-weighted so large offers dominate over small high-APY outliers.
        apys = self.lending_apys if self.lending_apys else self.global_lending_apys
        amounts = self.lending_offer_amounts if self.lending_apys else self.global_lending_offer_amounts
        if not apys:
            return None
        if amounts:
            total = sum(amounts)
            if total > 0:
                return sum(apy * amt for apy, amt in zip(apys, amounts)) / total
        return sum(apys) / len(apys)

    @property
    def apy_source(self) -> str:
        if self.lending_apys:
            return "live offers (same duration)"
        if self.global_lending_apys:
            return "live offers (global)"
        return "none"

    @property
    def target_apy_bps(self) -> int | None:
        mean = self.mean_apy_bps
        if mean is None:
            return None
        raw = mean * (1 - APY_DISCOUNT)
        return max(MIN_APY_BPS, round(raw))

    @property
    def median_principal_amount(self) -> int | None:
        source = self.offer_principal_amounts if self.offer_principal_amounts else self.loan_principal_amounts
        if not source:
            return None
        s = sorted(source)
        return s[len(s) // 2]

    @property
    def median_collateral_amount(self) -> int | None:
        source = self.offer_collateral_amounts if self.offer_collateral_amounts else self.loan_collateral_amounts
        if not source:
            return None
        s = sorted(source)
        return s[len(s) // 2]

    @property
    def median_ltv_usd(self) -> float | None:
        if not self.offer_ltv_usds:
            return None
        s = sorted(self.offer_ltv_usds)
        return s[len(s) // 2]

    @property
    def weighted_mean_ltv_usd(self) -> float | None:
        """Principal-USD-weighted mean LTV across this token's own market offers/loans —
        large offers dominate over small high-LTV fringe outliers."""
        if not self.offer_ltv_usds:
            return None
        total_weight = sum(self.offer_ltv_weights_usd)
        if total_weight > 0:
            return sum(ltv * w for ltv, w in zip(self.offer_ltv_usds, self.offer_ltv_weights_usd)) / total_weight
        return sum(self.offer_ltv_usds) / len(self.offer_ltv_usds)

    @property
    def median_collateral_price_per_raw(self) -> float | None:
        """Stale USD price per raw collateral unit, inferred from offer/loan metadata."""
        if not self.offer_collateral_prices_per_raw:
            return None
        s = sorted(self.offer_collateral_prices_per_raw)
        return s[len(s) // 2]



# ---------------------------------------------------------------------------
# API client
# ---------------------------------------------------------------------------

SESSION = requests.Session()
SESSION.headers.update({"Content-Type": "application/json"})


def fetch_wallet_token_balance(mint: str) -> int:
    """Return the raw token balance for `mint` in our on-chain wallet."""
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getTokenAccountsByOwner",
        "params": [
            WALLET_PUBKEY,
            {"mint": mint},
            {"encoding": "jsonParsed"},
        ],
    }
    resp = requests.post(SOLANA_RPC, json=payload, timeout=30)
    resp.raise_for_status()
    accounts = resp.json().get("result", {}).get("value", [])
    total = 0
    for acct in accounts:
        amount_str = (
            acct.get("account", {})
                .get("data", {})
                .get("parsed", {})
                .get("info", {})
                .get("tokenAmount", {})
                .get("amount", "0")
        )
        total += int(amount_str)
    return total


def fetch_escrow_balance(mint: str) -> int:
    """Return the raw token balance for `mint` held in our Offerbook escrow."""
    holdings = _get(f"/escrows/holdings/{WALLET_PUBKEY}")
    for entry in holdings:
        if entry.get("asset", {}).get("mint") == mint:
            return int(entry.get("amount", 0))
    return 0


def fetch_available_balance(mint: str, decimals: int) -> tuple[int, int, int]:
    """
    Return (wallet_raw, escrow_raw, total_raw) for `mint`.
    Logs the breakdown so it's always visible.
    """
    wallet_raw = fetch_wallet_token_balance(mint)
    escrow_raw = fetch_escrow_balance(mint)
    total_raw = wallet_raw + escrow_raw
    scale = 10 ** decimals
    log.info(
        "%-22s wallet=%10.2f  escrow=%10.2f  total=%10.2f",
        mint[:8] + "… balance:",
        wallet_raw / scale,
        escrow_raw / scale,
        total_raw / scale,
    )
    return wallet_raw, escrow_raw, total_raw


def _get(endpoint: str, params: dict | None = None) -> dict:
    url = f"{API_BASE}{endpoint}"
    resp = SESSION.get(url, params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


def _post_tx(endpoint: str, payload: dict) -> dict:
    url = f"{TX_API_BASE}{endpoint}"
    resp = SESSION.post(url, json=payload, timeout=30)
    if not resp.ok:
        # Read body before raise_for_status consumes it
        try:
            detail = resp.json().get("message", resp.text)
        except Exception:
            detail = resp.text or "(empty)"
        resp.reason = detail  # attach to the exception message
        resp.raise_for_status()
    return resp.json()


def _fetch_all_pages(endpoint: str, params: dict | None = None) -> list[dict]:
    """Auto-paginate through all pages of a paginated endpoint."""
    params = dict(params or {})
    params["limit"] = PAGE_SIZE
    params["offset"] = 0
    items: list[dict] = []
    while True:
        data = _get(endpoint, params)
        page = data.get("data", [])
        items.extend(page)
        pagination = data.get("pagination", {})
        if not pagination.get("hasMore", False):
            break
        params["offset"] += PAGE_SIZE
        time.sleep(0.15)   # be polite to the API
    return items


# ---------------------------------------------------------------------------
# Fetch helpers
# ---------------------------------------------------------------------------

def _mint_from_asset(asset: dict) -> str:
    """Extract mint address from an OfferAsset (direct mint field or legacy nested data field)."""
    return asset.get("mint") or asset.get("data", {}).get("mint") or asset.get("data", {}).get("asset") or ""


def _parse_offer(raw: dict) -> Offer:
    meta = raw.get("metadata") or {}
    return Offer(
        pubkey=raw["pubkey"],
        creator=raw["creator"],
        offer_type=raw["offerType"],
        status=raw["status"],
        principal_mint=raw.get("principalMint") or _mint_from_asset(raw.get("principal", {})),
        collateral_mint=raw.get("collateralMint") or _mint_from_asset(raw.get("collateral", {})),
        principal_amount=raw.get("remainingPrincipal", raw.get("principalAmount", 0)),
        collateral_amount=raw.get("remainingCollateral", raw.get("collateralAmount", 0)),
        apy=raw.get("apy", 0),
        duration=raw.get("duration", 0),
        principal_usd=meta.get("principalAmountUsd"),
        collateral_usd=meta.get("collateralAmountUsd"),
    )


def _parse_loan(raw: dict) -> Loan:
    meta = raw.get("metadata") or {}
    return Loan(
        pubkey=raw["pubkey"],
        lender=raw.get("lender", ""),
        borrower=raw.get("borrower", ""),
        status=raw.get("status", ""),
        principal_mint=raw.get("principalMint") or _mint_from_asset(raw.get("principal", {})),
        collateral_mint=raw.get("collateralMint") or _mint_from_asset(raw.get("collateral", {})),
        principal_amount=raw.get("principalAmount", 0),
        collateral_amount=raw.get("collateralAmount", 0),
        apy=raw.get("apy", 0),
        duration=raw.get("duration", 0),
        principal_usd=meta.get("startPrincipalAmountUsd"),
        collateral_usd=meta.get("startCollateralAmountUsd"),
    )


def fetch_active_lending_offers() -> list[Offer]:
    """
    includeUnderfunded=true is required. On this platform, "underfunded" mostly
    means "one of several offers sharing a rehypothecated escrow pool" (the same
    intentional strategy this bot itself uses — post many offers whose sizes sum
    to more than the actual escrow balance, since only one can be filled at a
    time). It is NOT a signal of a fake/unbacked offer. Without this flag, the
    vast majority of the live market — including most meme/pump.fun/tokenized-
    stock offers — is invisible and there's nothing to benchmark against.

    showUnverified=true is also set (hides non-Jupiter-verified collateral by
    default) — harmless to include even though it had no measurable effect when
    last checked.
    """
    log.info("Fetching active lending offers …")
    raw_items = _fetch_all_pages(
        "/offers",
        {
            "offerType": "lending", "status": "active", "hideExpired": "true",
            "showUnverified": "true", "includeUnderfunded": "true",
        },
    )
    # also grab partiallyFilled
    raw_items += _fetch_all_pages(
        "/offers",
        {
            "offerType": "lending", "status": "partiallyFilled", "hideExpired": "true",
            "showUnverified": "true", "includeUnderfunded": "true",
        },
    )
    offers = [_parse_offer(r) for r in raw_items]
    log.info("  → %d active lending offers", len(offers))
    return offers


def fetch_active_loans() -> list[Loan]:
    log.info("Fetching active loans …")
    raw_items = _fetch_all_pages("/loans/status/active")
    loans = [_parse_loan(r) for r in raw_items]
    log.info("  → %d active loans", len(loans))
    return loans


# ---------------------------------------------------------------------------
# Price helpers
# ---------------------------------------------------------------------------

def _fetch_dexscreener_price(mint: str) -> float | None:
    """Per-token price fallback for mints not listed on Jupiter."""
    try:
        resp = SESSION.get(f"{DEXSCREENER_API}/{mint}", timeout=10)
        if not resp.ok:
            return None
        pairs = resp.json().get("pairs") or []
        sol_pairs = [p for p in pairs if p.get("chainId") == "solana" and p.get("priceUsd")]
        if not sol_pairs:
            return None
        sol_pairs.sort(
            key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0),
            reverse=True,
        )
        return float(sol_pairs[0]["priceUsd"])
    except Exception:
        return None


def fetch_current_prices(collateral_mints: list[str]) -> tuple[dict[str, float], dict[str, int]]:
    """
    Return ({mint: usd_price_per_whole_token}, {mint: decimals}) for each collateral mint.
    Tries Jupiter first (batch); falls back to DexScreener per-token for any
    mint Jupiter doesn't cover (e.g. niche Bonk-ecosystem tokens).

    Jupiter's price response includes each token's `decimals` — we capture that
    here so mints missing from the hardcoded KNOWN_DECIMALS table (anything not
    in our curated top-20) still get a usable price instead of being silently
    treated as priceless.
    """
    if not collateral_mints:
        return {}, {}

    prices: dict[str, float] = {}
    jupiter_decimals: dict[str, int] = {}

    # 1. Jupiter — batch lookup, covers most major tokens
    try:
        headers = {"x-api-key": JUPITER_API_KEY} if JUPITER_API_KEY else {}
        resp = SESSION.get(
            JUPITER_PRICE_API,
            params={"ids": ",".join(collateral_mints)},
            headers=headers,
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        prices = {mint: float(info["usdPrice"]) for mint, info in data.items() if info.get("usdPrice")}
        jupiter_decimals = {
            mint: int(info["decimals"]) for mint, info in data.items() if info.get("decimals") is not None
        }
        log.info("Jupiter prices fetched for %d/%d collateral mints", len(prices), len(collateral_mints))
    except Exception as exc:
        log.warning("Could not fetch Jupiter prices: %s", exc)

    # 2. DexScreener — individual lookup for any mint Jupiter didn't return
    missing = [m for m in collateral_mints if m not in prices]
    if missing:
        log.info("Trying DexScreener for %d mint(s) not on Jupiter …", len(missing))
        for mint in missing:
            price = _fetch_dexscreener_price(mint)
            if price:
                prices[mint] = price
                log.info("  DexScreener  %s…  $%.6g", mint[:8], price)
            else:
                log.warning("  No live price for %s… (Jupiter + DexScreener both failed)", mint[:8])

    return prices, jupiter_decimals


# ---------------------------------------------------------------------------
# Strategy logic
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


def effective_target_ltv(
    collateral_mint: str,
    market_weighted_ltv: float | None = None,
    market_sample_count: int = 0,
    market_total_volume_usd: float = 0.0,
    our_offer_usdc: float = 0.0,
    largest_offer_ltv: float | None = None,
) -> float:
    """
    Per-token dynamic LTV target — see the constants block above for the full rule.

    "Enough data" to trust the token's own market (rather than FALLBACK_LTV) means
    EITHER >= MIN_MARKET_SAMPLES offers/loans, OR market_total_volume_usd >=
    MARKET_VOLUME_MULTIPLIER x our_offer_usdc — a handful of dust offers shouldn't
    qualify, but one large, capital-backed offer can stand on its own even with
    fewer than 5 orders on the book. our_offer_usdc must be > 0 for the volume path
    to apply (a missing/zero value can't silently satisfy "volume >= 0").

    Otherwise young tokens (< YOUNG_TOKEN_AGE_DAYS, or of unknown age — fail safe)
    target a discount below their own market rate; mature tokens target a premium
    above it (less collateral required than the market average, to be more
    competitive). HARD_LTV_CEILING always applies on top.

    largest_offer_ltv, when provided, is the LTV of this pair's single largest
    live lending offer (see PairStats.largest_offer_ltv). If it's MORE
    conservative (lower) than whatever this function would otherwise target,
    the result is capped down to match it — this can only make the result
    safer, never more aggressive, guarding against the weighted-mean benchmark
    being looser than what the market's most prominent participant actually
    accepts (e.g. because it's skewed by several small, thin offers).
    """
    has_enough_volume = our_offer_usdc > 0 and market_total_volume_usd >= MARKET_VOLUME_MULTIPLIER * our_offer_usdc
    has_enough_data = market_sample_count >= MIN_MARKET_SAMPLES or has_enough_volume

    if not market_weighted_ltv or not has_enough_data:
        target = FALLBACK_LTV
    else:
        age_days = _token_age_days(collateral_mint)
        if age_days is not None and age_days >= YOUNG_TOKEN_AGE_DAYS:
            target = market_weighted_ltv / (1 - MATURE_TOKEN_COLLATERAL_DISCOUNT)
        else:
            target = market_weighted_ltv - YOUNG_TOKEN_DISCOUNT

    if largest_offer_ltv is not None:
        target = min(target, largest_offer_ltv)

    return max(min(target, HARD_LTV_CEILING), 0.05)


def safe_collateral_amount(
    principal_raw: int,
    collateral_mint: str,
    current_prices: dict[str, float],
    decimals_map: dict[str, int],
    market_weighted_ltv: float | None = None,
    market_sample_count: int = 0,
    market_total_volume_usd: float = 0.0,
    largest_offer_ltv: float | None = None,
) -> int | None:
    """
    Return the collateral raw amount required so that LTV == effective_target_ltv(...) at
    current prices. Returns None if no live price is available (caller should skip the offer).

    `decimals_map` should be KNOWN_DECIMALS merged with any decimals Jupiter's
    price response supplied for mints outside that curated table — otherwise a
    perfectly valid live price for an unlisted token gets discarded for lack of
    a decimals figure to convert it with.

    Deliberately does NOT fall back to the pool-implied/stale price when a live
    price is missing: sizing off that price and then "cross-validating" against
    the same price is a no-op (it can never disagree with itself), so a stale
    price for an illiquid/moved token would silently produce a bad LTV.
    """
    principal_usdc = principal_raw / 10 ** USDC_DECIMALS
    required_collateral_usdc = principal_usdc / effective_target_ltv(
        collateral_mint, market_weighted_ltv, market_sample_count, market_total_volume_usd,
        principal_usdc, largest_offer_ltv,
    )
    price_per_token = current_prices.get(collateral_mint)
    decimals = decimals_map.get(collateral_mint)
    if price_per_token and price_per_token > 0 and decimals is not None:
        price_per_raw = price_per_token / (10 ** decimals)
        return int(required_collateral_usdc / price_per_raw)
    return None


def build_pair_stats(
    lending_offers: list[Offer],
    active_loans: list[Loan],
    duration_secs: int,
) -> dict[tuple[str, str], PairStats]:
    """Aggregate per-pair statistics from offers and loans."""
    stats: dict[tuple[str, str], PairStats] = {}

    def get_or_create(pair: tuple[str, str]) -> PairStats:
        if pair not in stats:
            stats[pair] = PairStats(
                principal_mint=pair[0],
                collateral_mint=pair[1],
            )
        return stats[pair]

    for offer in lending_offers:
        ps = get_or_create(offer.pair)
        ps.global_lending_apys.append(offer.apy)
        ps.global_lending_offer_amounts.append(offer.principal_amount)
        if offer.duration == duration_secs:
            ps.lending_apys.append(offer.apy)
            ps.lending_offer_amounts.append(offer.principal_amount)
        if offer.principal_amount > 0:
            ps.offer_principal_amounts.append(offer.principal_amount)
        if offer.collateral_amount > 0:
            ps.offer_collateral_amounts.append(offer.collateral_amount)
        if offer.ltv is not None:
            ps.offer_ltv_usds.append(offer.ltv)
            ps.offer_ltv_weights_usd.append(offer.principal_usd or 0)
        if offer.collateral_usd and offer.collateral_amount > 0:
            ps.offer_collateral_prices_per_raw.append(offer.collateral_usd / offer.collateral_amount)
        if offer.principal_usd and offer.principal_usd > ps.largest_offer_principal_usd:
            ps.largest_offer_principal_usd = offer.principal_usd
            ps.largest_offer_apy_bps = offer.apy
            ps.largest_offer_ltv = offer.ltv

    for loan in active_loans:
        ps = get_or_create(loan.pair)
        ps.active_loan_count += 1
        if loan.apy > 0:
            ps.loan_apys.append(loan.apy)
        if loan.principal_amount > 0:
            ps.loan_principal_amounts.append(loan.principal_amount)
        if loan.collateral_amount > 0:
            ps.loan_collateral_amounts.append(loan.collateral_amount)
        if loan.ltv is not None:
            ps.offer_ltv_usds.append(loan.ltv)
            ps.offer_ltv_weights_usd.append(loan.principal_usd or 0)
        if loan.collateral_usd and loan.collateral_amount > 0:
            ps.offer_collateral_prices_per_raw.append(loan.collateral_usd / loan.collateral_amount)

    return stats


def compute_offer_params(ps: PairStats) -> dict[str, Any] | None:
    """
    Compute the parameters for a new lending offer on a given pair.
    Returns None if we cannot or should not create an offer.
    """
    if not ps.principal_mint or not ps.collateral_mint:
        log.debug("Skipping pair with empty mint address")
        return None

    target_apy = ps.target_apy_bps
    if target_apy is None:
        log.debug("Skipping %s/%s – no market APY reference", ps.principal_mint[:8], ps.collateral_mint[:8])
        return None

    # Guardrail: never undercut the single largest live offer's APY, even if
    # the weighted-mean target would otherwise be lower — that offer is the
    # one a borrower comparison-shopping the pool is most likely to pick.
    if ps.largest_offer_apy_bps is not None:
        target_apy = max(target_apy, ps.largest_offer_apy_bps)

    # Size the offer: use the median principal amount from existing offers
    principal_amount = ps.median_principal_amount
    collateral_amount = ps.median_collateral_amount

    if not principal_amount or not collateral_amount:
        log.debug("Skipping %s/%s – no amount data", ps.principal_mint[:8], ps.collateral_mint[:8])
        return None

    # Allocation config lookup.
    # If the collateral mint is explicitly listed in allocation_config.yaml it
    # bypasses the USD LTV filter (the user trusts it); otherwise the LTV filter
    # runs first and the 'default' fraction is used if the pair passes.
    collateral_in_config = ps.collateral_mint in ALLOCATION_CONFIG
    allocation_fraction = ALLOCATION_CONFIG.get(ps.collateral_mint, ALLOCATION_CONFIG.get("default", 0.0))

    if collateral_in_config:
        if allocation_fraction == 0.0:
            log.info("  Skipping %s/%s — allocation set to 0 in config",
                     ps.principal_mint[:8], ps.collateral_mint[:8])
            return None
        # Trusted token: skip the stale-LTV filter
    else:
        # Not in config: enforce the hard LTV ceiling using loan/offer creation prices.
        median_ltv = ps.median_ltv_usd
        if median_ltv is not None:
            if median_ltv > HARD_LTV_CEILING:
                log.info(
                    "  Skipping %s/%s — market USD LTV %.1f%% > hard ceiling %.0f%%  "
                    "(not in allocation config and collateral appears undervalued)",
                    ps.principal_mint[:8], ps.collateral_mint[:8],
                    median_ltv * 100, HARD_LTV_CEILING * 100,
                )
                return None
        if allocation_fraction == 0.0:
            log.debug("  Skipping %s/%s — passes LTV filter but default allocation is 0",
                      ps.principal_mint[:8], ps.collateral_mint[:8])
            return None

    # Apply per-offer cap for USDC principal offers
    if MAX_OFFER_PRINCIPAL_USDC and ps.principal_mint == USDC_MINT:
        cap_raw = MAX_OFFER_PRINCIPAL_USDC * (10 ** USDC_DECIMALS)
        if principal_amount > cap_raw:
            log.debug("  Capping principal %d → %d (MAX_OFFER_PRINCIPAL_USDC=%d)",
                      principal_amount, cap_raw, MAX_OFFER_PRINCIPAL_USDC)
            principal_amount = cap_raw

    median_ltv = ps.median_ltv_usd
    if median_ltv is not None:
        log.debug("  USD LTV = %.1f%% (market median, hard ceiling = %.0f%%)",
                  median_ltv * 100, HARD_LTV_CEILING * 100)

    # minFillAmount: minimum a borrower must take in a partial fill.
    # API requires this to be > 1000 raw units. Use 1% of principal, floored at 1001.
    min_fill = max(1001, principal_amount // 100)

    return {
        "signer": WALLET_PUBKEY,
        "principalMint": ps.principal_mint,
        "collateralMint": ps.collateral_mint,
        "principalAmount": principal_amount,
        "collateralAmount": collateral_amount,
        "apy": target_apy,
        "duration": MAX_DURATION_SECS,
        "expiry": OFFER_EXPIRY_SECS,
        "allowPartialFill": ALLOW_PARTIAL_FILL,
        "minFillAmount": min_fill,
        "topup": "minimum",  # pull only the shortfall from wallet; use escrow first
    }


# ---------------------------------------------------------------------------
# Signer (lazy — only needed when DRY_RUN=false)
# ---------------------------------------------------------------------------

_ledger_signer = None


def _get_ledger_signer():
    global _ledger_signer
    if _ledger_signer is not None:
        return _ledger_signer
    try:
        from ledger_signer import LedgerSigner
    except ImportError:
        log.error("Missing dependencies: pip install ledgerblue hidapi base58")
        sys.exit(1)
    _ledger_signer = LedgerSigner(path=LEDGER_PATH)
    return _ledger_signer


def resolve_signer_wallet() -> str:
    """
    Resolve WALLET_PUBKEY for the active signing mode. For Ledger, the device
    is the source of truth (overrides/fills in OFFERBOOK_WALLET).
    """
    global WALLET_PUBKEY
    if SIGNING_MODE == "ledger":
        from ledger_signer import LedgerError

        try:
            signer = _get_ledger_signer()
            device_pubkey = signer.get_pubkey()
        except LedgerError as exc:
            log.error(str(exc))
            sys.exit(1)
        if WALLET_PUBKEY and WALLET_PUBKEY != device_pubkey:
            log.warning(
                "OFFERBOOK_WALLET (%s) does not match the Ledger address (%s) at path %s — using the Ledger address.",
                WALLET_PUBKEY, device_pubkey, LEDGER_PATH,
            )
        WALLET_PUBKEY = device_pubkey
    return WALLET_PUBKEY


def confirm_signing_mode(skip_prompt: bool) -> None:
    label = "LEDGER (hardware wallet)" if SIGNING_MODE == "ledger" else "PRIVATE KEY (.env hot wallet)"
    log.info("=" * 60)
    log.info("  Signing mode : %s", label)
    log.info("  Wallet       : %s", WALLET_PUBKEY)
    if SIGNING_MODE == "ledger":
        log.info("  Derivation   : %s", LEDGER_PATH)
    log.info("  Dry run      : %s", DRY_RUN)
    log.info("=" * 60)
    if skip_prompt:
        return
    choice = input("Continue with this signing mode? [y/N] ").strip().lower()
    if choice not in ("y", "yes"):
        log.info("Aborted by user.")
        sys.exit(0)

# ---------------------------------------------------------------------------
# Transaction submission
# ---------------------------------------------------------------------------

def sign_and_send_transaction(tx_b64: str) -> str:
    """
    Sign a base64-encoded Solana transaction (Ledger or private key, per
    SIGNING_MODE) and broadcast it via the Solana RPC.

    Requires:  pip install solders base58   (+ ledgerblue hidapi for --ledger)
    """
    if SIGNING_MODE == "ledger":
        from ledger_signer import LedgerError

        signer = _get_ledger_signer()
        log.info("  Awaiting approval on Ledger device …")
        try:
            signed_b64 = signer.sign_transaction(tx_b64, expected_signer=WALLET_PUBKEY)
        except LedgerError as exc:
            log.error("  %s", exc)
            return ""
    else:
        try:
            from solders.keypair import Keypair  # type: ignore
            from solders.transaction import VersionedTransaction  # type: ignore
            import base58  # type: ignore
        except ImportError:
            log.error(
                "solders / base58 not installed.  Run:  pip install solders base58\n"
                "Transaction NOT submitted."
            )
            return ""

        # Decode the keypair
        secret_bytes = base58.b58decode(PRIVATE_KEY_B58)
        keypair = Keypair.from_bytes(secret_bytes)

        # Deserialise and sign
        raw_tx = base64.b64decode(tx_b64)
        tx = VersionedTransaction.from_bytes(raw_tx)
        signed_tx = VersionedTransaction(tx.message, [keypair])

        signed_b64 = base64.b64encode(bytes(signed_tx)).decode()

    # Send via JSON-RPC
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "sendTransaction",
        "params": [signed_b64, {"encoding": "base64", "preflightCommitment": "confirmed"}],
    }
    rpc_resp = requests.post(SOLANA_RPC, json=payload, timeout=30)
    rpc_resp.raise_for_status()
    result = rpc_resp.json()
    if "error" in result:
        log.error("RPC error: %s", result["error"])
        return ""
    sig = result.get("result", "")
    return sig


def create_offer(params: dict[str, Any]) -> bool:
    """
    Hit the Offerbook TX builder, sign, and send.
    Returns True on success.
    """
    log.info(
        "  Creating lending offer: principal=%s…  collateral=%s…  "
        "apy=%d bps (%.2f%%)  duration=%dd",
        params["principalMint"][:8],
        params["collateralMint"][:8],
        params["apy"],
        params["apy"] / 100,
        params["duration"] // 86400,
    )

    if DRY_RUN:
        log.info("  [DRY RUN] Skipping transaction submission.")
        return True

    try:
        tx_data = _post_tx("/create-principal-offer", params)
    except requests.HTTPError as exc:
        body = exc.response.text if exc.response else ""
        log.error("  TX builder error: %s\n  body: %s", exc, body or "(empty)")
        return False
    except requests.ConnectionError as exc:
        log.error("  TX builder connection error: %s", exc)
        return False

    transactions: list[str] = tx_data.get("transactions", [])
    if not transactions:
        log.error("  TX builder returned no transactions!")
        return False

    if SIGNING_MODE == "private_key" and not PRIVATE_KEY_B58:
        log.warning(
            "  OFFERBOOK_PRIVATE_KEY not set – cannot sign.  "
            "Transaction bytes (base64):\n%s",
            transactions[0][:80] + "…",
        )
        return False

    for tx_b64 in transactions:
        sig = sign_and_send_transaction(tx_b64)
        if sig:
            log.info("  ✓ Submitted: https://solscan.io/tx/%s", sig)
        else:
            log.error("  ✗ Failed to submit transaction.")
            return False

    return True


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    global SIGNING_MODE, WALLET_PUBKEY

    parser = argparse.ArgumentParser(description="Offerbook competitive lending strategy")
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--ledger", action="store_const", dest="signing_mode", const="ledger",
        help="Sign with a Ledger hardware wallet (default)",
    )
    mode_group.add_argument(
        "--private-key", action="store_const", dest="signing_mode", const="private_key",
        help="Sign with OFFERBOOK_PRIVATE_KEY from .env",
    )
    parser.add_argument(
        "--yes", "-y", action="store_true",
        help="Skip the signing-mode confirmation prompt",
    )
    parser.add_argument(
        "--collateral", default=None,
        help="Only create an offer for this collateral (symbol like HYPE, or a mint address). "
             "Omit to run the full strategy across every allocated pair.",
    )
    args = parser.parse_args()

    collateral_filter = None
    if args.collateral:
        collateral_filter = SYMBOL_TO_MINT.get(args.collateral.upper(), args.collateral)

    if args.signing_mode:
        SIGNING_MODE = args.signing_mode
    if SIGNING_MODE not in ("ledger", "private_key"):
        log.error("Invalid signing mode %r — must be 'ledger' or 'private_key'", SIGNING_MODE)
        sys.exit(1)

    if SIGNING_MODE == "private_key":
        if not WALLET_PUBKEY:
            log.error("OFFERBOOK_WALLET env var not set.  Exiting.")
            sys.exit(1)
        if not DRY_RUN and not PRIVATE_KEY_B58:
            log.error("OFFERBOOK_PRIVATE_KEY is not set (required for live private-key mode)")
            sys.exit(1)
    else:
        resolve_signer_wallet()  # queries the Ledger device, sets WALLET_PUBKEY

    confirm_signing_mode(skip_prompt=args.yes)

    log.info("=" * 60)
    log.info("Offerbook Competitive Lending Bot")
    log.info("Wallet  : %s", WALLET_PUBKEY)
    log.info("DRY RUN : %s", DRY_RUN)
    log.info("Strategy: APY = market_mean × %.0f%%  |  LTV floor = %.0f%%, hard ceiling = %.0f%%  |  max duration = %d days",
             (1 - APY_DISCOUNT) * 100, FALLBACK_LTV * 100, HARD_LTV_CEILING * 100, MAX_DURATION_DAYS)
    log.info("=" * 60)

    # 1. Fetch USDC balance (wallet + escrow) before doing anything.
    # topup:"minimum" draws only the shortfall from wallet, so the full
    # combined balance (wallet + escrow) is available as budget.
    try:
        _, _, usdc_available_raw = fetch_available_balance(USDC_MINT, USDC_DECIMALS)
    except Exception as exc:
        log.warning("Could not fetch USDC balance: %s  (proceeding anyway)", exc)
        usdc_available_raw = None

    if MAX_OFFER_PRINCIPAL_USDC:
        log.info("Per-offer USDC cap  : %d USDC  (MAX_OFFER_PRINCIPAL_USDC)", MAX_OFFER_PRINCIPAL_USDC)
    log.info("=" * 60)

    # 2. Fetch market data
    lending_offers = fetch_active_lending_offers()
    active_loans = fetch_active_loans()

    # 3. Aggregate pair statistics
    pair_stats = build_pair_stats(lending_offers, active_loans, MAX_DURATION_SECS)
    log.info("Unique (principal, collateral) pairs found: %d", len(pair_stats))

    # 4. Only include pairs that have at least one live lending offer to benchmark APY against
    relevant_pairs = {
        pair: ps
        for pair, ps in pair_stats.items()
        if ps.lending_apys or ps.global_lending_apys
    }
    log.info("Pairs with live offers (APY benchmark available): %d", len(relevant_pairs))

    if collateral_filter:
        relevant_pairs = {
            pair: ps for pair, ps in relevant_pairs.items() if pair[1] == collateral_filter
        }
        log.info("Filtered to collateral %s: %d pair(s)", args.collateral, len(relevant_pairs))
        if not relevant_pairs:
            log.error("No benchmarkable pair found for collateral %s (%s) — nothing to do.",
                      args.collateral, collateral_filter)
            return

    # 5. Compute offer params (APY, duration, etc.) and allocation budgets.
    pair_offer_params: dict = {}
    pair_budgets_raw: dict = {}

    for pair, ps in relevant_pairs.items():
        params = compute_offer_params(ps)
        if params is None:
            continue
        pair_offer_params[pair] = params

        if usdc_available_raw is not None and params["principalMint"] == USDC_MINT:
            fraction = ALLOCATION_CONFIG.get(ps.collateral_mint, ALLOCATION_CONFIG.get("default", 0.0))
            pair_budgets_raw[pair] = int(usdc_available_raw * fraction)

    log.info("=" * 60)
    log.info("Allocation config: %s", _CONFIG_PATH)
    for pair, budget_raw in pair_budgets_raw.items():
        ps = relevant_pairs[pair]
        fraction = ALLOCATION_CONFIG.get(ps.collateral_mint, ALLOCATION_CONFIG.get("default", 0.0))
        log.info("  %s…  →  %.0f%%  =  %.2f USDC",
                 ps.collateral_mint[:8], fraction * 100, budget_raw / 10**USDC_DECIMALS)
    log.info("=" * 60)

    # 5b. Fetch real-time prices for every collateral mint we're going to offer on.
    #     These are used to compute collateralAmount so that LTV matches
    #     effective_target_ltv() at current market prices — NOT at whatever stale
    #     price market offers were created with.
    collateral_mints = list({relevant_pairs[p].collateral_mint for p in pair_offer_params})
    current_prices, jupiter_decimals = fetch_current_prices(collateral_mints)
    decimals_map = {**jupiter_decimals, **KNOWN_DECIMALS}  # KNOWN_DECIMALS (curated) wins on conflict

    # 6. Build and submit offers
    successes = 0
    skipped = 0
    errors = 0
    usdc_committed_raw = 0

    for pair, ps in relevant_pairs.items():
        log.info(
            "\nPair %s…/%s…  | loans=%d  mean_apy=%.0f bps [from %s]  target_apy=%s bps",
            ps.principal_mint[:8], ps.collateral_mint[:8],
            ps.active_loan_count,
            ps.mean_apy_bps or 0,
            ps.apy_source,
            ps.target_apy_bps or "N/A",
        )

        offer_params = pair_offer_params.get(pair)
        if offer_params is None:
            skipped += 1
            continue

        if usdc_available_raw is not None and offer_params["principalMint"] == USDC_MINT:
            pair_budget_raw = pair_budgets_raw.get(pair, 0)

            if pair_budget_raw <= 1000:
                log.info("  Skipping – allocation too small (%.2f USDC)",
                         pair_budget_raw / 10**USDC_DECIMALS)
                skipped += 1
                continue

            # Always offer the full allocation budget (not the market median).
            # allowPartialFill=True means borrowers can take any fraction of it.
            offer_params["principalAmount"] = pair_budget_raw

            # Compute collateral required so that LTV = effective_target_ltv() at current
            # prices. This is the critical fix: we do NOT copy collateralAmount from other
            # lenders' offers because those offers may have been created when token
            # prices were very different.
            collateral_raw = safe_collateral_amount(
                principal_raw=pair_budget_raw,
                collateral_mint=ps.collateral_mint,
                current_prices=current_prices,
                decimals_map=decimals_map,
                market_weighted_ltv=ps.weighted_mean_ltv_usd,
                market_sample_count=len(ps.offer_ltv_usds),
                market_total_volume_usd=sum(ps.offer_ltv_weights_usd),
                largest_offer_ltv=ps.largest_offer_ltv,
            )

            if collateral_raw is None:
                log.warning(
                    "  %s: no usable live price (Jupiter + DexScreener both failed, or "
                    "decimals unknown) — skipping to avoid sizing off a stale price",
                    ps.collateral_mint[:8],
                )
                skipped += 1
                continue

            principal_usdc = pair_budget_raw / 10 ** USDC_DECIMALS

            # Cross-validate the live price against the pool-implied price from existing
            # loans/offers. If the price feed is wrong (e.g. DexScreener returning 5000×
            # the real price), collateral_raw will be far too low and our offer's true LTV
            # will exceed the target — skip rather than create an undercollateralised offer.
            pool_price = ps.median_collateral_price_per_raw
            pair_max_ltv = effective_target_ltv(
                ps.collateral_mint, ps.weighted_mean_ltv_usd, len(ps.offer_ltv_usds),
                sum(ps.offer_ltv_weights_usd), principal_usdc, ps.largest_offer_ltv,
            )
            if pool_price and pool_price > 0 and collateral_raw > 0:
                ltv_at_pool_price = principal_usdc / (collateral_raw * pool_price)
                if ltv_at_pool_price > pair_max_ltv * 2.0:
                    log.warning(
                        "  %s: skipping — live price requires %.4g tokens but pool-implied "
                        "price gives LTV %.0f%% (2× limit %.0f%%) — likely bad price feed",
                        ps.collateral_mint[:8], collateral_raw,
                        ltv_at_pool_price * 100, pair_max_ltv * 100,
                    )
                    skipped += 1
                    continue

            offer_params["collateralAmount"] = collateral_raw
            offer_params["minFillAmount"] = max(1001, pair_budget_raw // 100)

            collateral_price = current_prices.get(ps.collateral_mint)
            decimals = decimals_map.get(ps.collateral_mint)
            if collateral_price and decimals is not None:
                collateral_usdc = collateral_raw / (10 ** decimals) * collateral_price
                actual_ltv = principal_usdc / collateral_usdc if collateral_usdc else 0
                log.info(
                    "  Sizing: %.2f USDC / %.4g tokens (collateral ~$%.2f)  →  LTV %.1f%%",
                    principal_usdc,
                    collateral_raw / (10 ** decimals),
                    collateral_usdc,
                    actual_ltv * 100,
                )

        ok = create_offer(offer_params)
        if ok:
            successes += 1
            if offer_params["principalMint"] == USDC_MINT:
                usdc_committed_raw += offer_params["principalAmount"]
        else:
            errors += 1

        time.sleep(0.5)

    log.info("\n" + "=" * 60)
    log.info("Done.  Created=%d  Skipped=%d  Errors=%d", successes, skipped, errors)
    if usdc_committed_raw:
        log.info("USDC committed this run: %.6f USDC", usdc_committed_raw / 10**USDC_DECIMALS)
    log.info("=" * 60)


if __name__ == "__main__":
    main()
