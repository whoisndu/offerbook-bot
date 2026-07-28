"""
Offerbook Defaulter Capture — Competitive Offers for Watchlisted Borrowers
======================================
Reacts to actionable entries from defaulter_watch.py (a watchlisted borrower
has an open borrow request right now, or an active loan expiring within 24h)
by posting a competitive lending offer into that same collateral pool —
sized from allocation_config.yaml exactly like the strategy.py scripts, and
priced to modestly beat (not wildly outbid) the single largest live offer
already in that pool (excluding our own), since that's the offer a borrower
comparison-shopping the pool is actually most likely to pick — not some
average of everything listed, including small offers they'd never consider:

  - APY: undercut the largest offer's APY by APY_UNDERCUT.
  - LTV: a small edge above the largest offer's LTV, capped by the same
    effective_target_ltv() safety ceiling used in strategy.py (young/
    mature-token rules, 75% hard ceiling) — historical profitability on
    other loans never overrides this cap.
  - Duration: matches the largest offer's own duration, since that's the
    specific listing being targeted.

Every offer's principal/collateral/APY/LTV/duration is printed in full
before signing, one transaction at a time — the same pre-sign detail dump
ledger_signer.py already shows for every script in this repo, plus a
human-readable summary line specific to this one.

Never trusts allocation_config.yaml implicitly: a collateral not listed
there (or listed at 0%) is skipped, same as a normal strategy run — a
borrower's watchlist history does not bypass your own configured risk
tolerance per token.

Usage:
  python defaulter_capture.py                  # DRY_RUN by default (see .env)
  DRY_RUN=false python defaulter_capture.py    # actually sign and submit
  python defaulter_capture.py --min-surplus 100
  python defaulter_capture.py --yes            # skip the signing-mode confirmation prompt

Exit codes:
  0 — ran (created, skipped, or previewed offers; see summary)
  1 — nothing actionable right now (nothing to do)
"""
from __future__ import annotations

from dotenv import load_dotenv
load_dotenv()

import argparse
import logging
import os
import sys
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import requests
import yaml  # pip install pyyaml

from defaulter_watch import (
    compute_defaulted_stats,
    compute_late_repayer_stats,
    merge_target_borrowers,
    fetch_open_borrow_offers,
    fetch_all_active_loans,
    group_active_loans_by_borrower,
    symbol_for,
)
import offerbook_common as _common
from offerbook_common import _mint_from_asset

# ---------------------------------------------------------------------------
# Configuration — same environment variables as strategy.py
# ---------------------------------------------------------------------------

API_BASE = os.getenv("OFFERBOOK_API_BASE", "https://api.offerbook.jup.ag/api/v1")
TX_API_BASE = os.getenv("OFFERBOOK_TX_API_BASE", "https://builder.offerbook.jup.ag/api/v1")
SOLANA_RPC = os.getenv("SOLANA_RPC", "https://api.mainnet-beta.solana.com")

WALLET_PUBKEY: str = os.getenv("OFFERBOOK_WALLET", "")
PRIVATE_KEY_B58: str = os.getenv("OFFERBOOK_PRIVATE_KEY", "")

SIGNING_MODE: str = os.getenv("OFFERBOOK_SIGNING_MODE", "ledger").strip().lower()
LEDGER_PATH: str = os.getenv("OFFERBOOK_LEDGER_PATH", "44'/501'/0'")

DRY_RUN: bool = os.getenv("DRY_RUN", "true").lower() in ("1", "true", "yes")

OFFER_EXPIRY_SECS = 1 * 24 * 60 * 60  # offer listing expires in 24h
MIN_APY_BPS = 10
ALLOW_PARTIAL_FILL = True
PAGE_SIZE = 100

# How much better than the pool's single largest live offer to target — a
# small, bounded edge, not an attempt to win at any cost. The largest offer
# is what a borrower comparison-shopping the pool is most likely to pick
# (it's the one most likely to actually cover what they want to borrow), so
# it's the one worth beating specifically — not a pool-wide average, which a
# borrower has no particular reason to prefer over the single best deal
# actually on offer.
APY_UNDERCUT = 0.05          # 5% below the largest live offer's APY
LTV_EDGE_PTS = 0.02          # 2 percentage points above the largest live offer's LTV

# Same effective_target_ltv() constants as strategy.py — this is the safety
# ceiling that always applies, regardless of how attractive a target borrower
# looks historically.
YOUNG_TOKEN_AGE_DAYS = 60
MIN_MARKET_SAMPLES = 5
MARKET_VOLUME_MULTIPLIER = 2.0
YOUNG_TOKEN_DISCOUNT = 0.05
MATURE_TOKEN_COLLATERAL_DISCOUNT = 0.10
HARD_LTV_CEILING = 0.75


def fallback_ltv_for_duration(duration_secs: int) -> float:
    """Matches strategy.py's DURATION_CONFIGS fallback_ltv, generalized to any duration."""
    days = duration_secs / 86400
    if days <= 3:
        return 0.65
    if days <= 7:
        return 0.45
    return 0.25


USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDC_DECIMALS = 6

JUPITER_PRICE_API = "https://api.jup.ag/price/v3"
JUPITER_API_KEY = os.getenv("JUPITER_API_KEY", "")
DEXSCREENER_API = "https://api.dexscreener.com/latest/dex/tokens"

KNOWN_DECIMALS = _common.KNOWN_DECIMALS

# ---------------------------------------------------------------------------
# Allocation config — identical loader to strategy.py
# ---------------------------------------------------------------------------

def _load_allocation_config(path: str) -> dict[str, float]:
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
log = logging.getLogger("defaulter_capture")

SESSION = requests.Session()
SESSION.headers.update({"Content-Type": "application/json"})

# ---------------------------------------------------------------------------
# API helpers
# ---------------------------------------------------------------------------

def _get(endpoint: str, params: dict | None = None) -> dict:
    return _common.api_get(SESSION, API_BASE, endpoint, params)


def _post_tx(endpoint: str, payload: dict) -> dict:
    return _common.post_tx(SESSION, TX_API_BASE, endpoint, payload)


def _fetch_all_pages(endpoint: str, params: dict | None = None) -> list[dict]:
    return _common.fetch_all_pages(SESSION, API_BASE, endpoint, params, PAGE_SIZE)


def fetch_wallet_token_balance(mint: str) -> int:
    payload = {
        "jsonrpc": "2.0", "id": 1, "method": "getTokenAccountsByOwner",
        "params": [WALLET_PUBKEY, {"mint": mint}, {"encoding": "jsonParsed"}],
    }
    resp = requests.post(SOLANA_RPC, json=payload, timeout=30)
    resp.raise_for_status()
    accounts = resp.json().get("result", {}).get("value", [])
    return sum(
        int(a.get("account", {}).get("data", {}).get("parsed", {}).get("info", {})
             .get("tokenAmount", {}).get("amount", "0"))
        for a in accounts
    )


def fetch_escrow_balance(mint: str) -> int:
    holdings = _get(f"/escrows/holdings/{WALLET_PUBKEY}")
    for entry in holdings:
        if entry.get("asset", {}).get("mint") == mint:
            return int(entry.get("amount", 0))
    return 0


def fetch_available_balance(mint: str, decimals: int) -> int:
    wallet_raw = fetch_wallet_token_balance(mint)
    escrow_raw = fetch_escrow_balance(mint)
    total_raw = wallet_raw + escrow_raw
    scale = 10 ** decimals
    log.info("Available USDC: wallet=%.2f  escrow=%.2f  total=%.2f",
              wallet_raw / scale, escrow_raw / scale, total_raw / scale)
    return total_raw


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


def fetch_current_price(mint: str) -> tuple[float | None, int | None]:
    """Return (usd_price_per_whole_token, decimals) for a single collateral mint."""
    try:
        headers = {"x-api-key": JUPITER_API_KEY} if JUPITER_API_KEY else {}
        resp = SESSION.get(JUPITER_PRICE_API, params={"ids": mint}, headers=headers, timeout=15)
        resp.raise_for_status()
        info = resp.json().get(mint)
        if info and info.get("usdPrice"):
            return float(info["usdPrice"]), int(info["decimals"]) if info.get("decimals") is not None else KNOWN_DECIMALS.get(mint)
    except Exception as exc:
        log.warning("Jupiter price fetch failed for %s: %s", mint[:8], exc)

    price = _fetch_dexscreener_price(mint)
    return price, KNOWN_DECIMALS.get(mint)


_token_age_cache: dict[str, float | None] = {}


def _token_age_days(mint: str) -> float | None:
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
    market_weighted_ltv: float | None,
    market_sample_count: int,
    market_total_volume_usd: float,
    our_offer_usdc: float,
    fallback_ltv: float,
) -> float:
    """Identical rule to strategy.py's effective_target_ltv(), parameterized by fallback_ltv."""
    has_enough_volume = our_offer_usdc > 0 and market_total_volume_usd >= MARKET_VOLUME_MULTIPLIER * our_offer_usdc
    has_enough_data = market_sample_count >= MIN_MARKET_SAMPLES or has_enough_volume

    if not market_weighted_ltv or not has_enough_data:
        return min(fallback_ltv, HARD_LTV_CEILING)

    age_days = _token_age_days(collateral_mint)
    if age_days is not None and age_days >= YOUNG_TOKEN_AGE_DAYS:
        target = market_weighted_ltv / (1 - MATURE_TOKEN_COLLATERAL_DISCOUNT)
    else:
        target = market_weighted_ltv - YOUNG_TOKEN_DISCOUNT

    return max(min(target, HARD_LTV_CEILING), 0.05)

# ---------------------------------------------------------------------------
# Target pool identification
# ---------------------------------------------------------------------------

@dataclass
class CapturePool:
    collateral_mint: str
    duration_secs: int
    reasons: list[str]


def get_actionable_pools(min_surplus: float) -> list[CapturePool]:
    """
    Combine defaulter_watch's watchlist + live state to find (collateral, duration)
    pools worth posting a competitive offer into right now: a watchlisted borrower
    has an open borrow request, or an active loan expiring within 24h, in that pool.
    """
    defaulted_stats = compute_defaulted_stats()
    late_stats = compute_late_repayer_stats()
    targets = merge_target_borrowers(defaulted_stats, late_stats, min_surplus)
    if not targets:
        return []

    target_addrs = {t["borrower"] for t in targets}
    by_borrower = {t["borrower"]: t for t in targets}
    open_offers_by_addr = fetch_open_borrow_offers(target_addrs)
    all_active_loans = fetch_all_active_loans()
    active_loans_by_addr = group_active_loans_by_borrower(all_active_loans, target_addrs)

    now = datetime.now(timezone.utc)
    pools: dict[tuple[str, int], list[str]] = defaultdict(list)

    for borrower, offers in open_offers_by_addr.items():
        t = by_borrower[borrower]
        for o in offers:
            cmint = o.get("collateralMint") or _mint_from_asset(o.get("collateral", {}))
            duration = o.get("duration", 0)
            if not cmint or not duration:
                continue  # NFT collateral or malformed — not this script's scope
            pools[(cmint, duration)].append(
                f"{borrower} has an open borrow request ({t['defaults']} defaults, "
                f"{t['late_repays']} late repayments, ${t['surplus_usd']:,.2f} known surplus)"
            )

    for borrower, loans in active_loans_by_addr.items():
        t = by_borrower[borrower]
        for l in loans:
            expired_at = datetime.fromisoformat(l["expiredAt"].replace("Z", "+00:00"))
            hrs_left = (expired_at - now).total_seconds() / 3600.0
            if hrs_left > 24:
                continue
            cmint = l.get("collateralMint") or _mint_from_asset(l.get("collateral", {}))
            duration = l.get("duration", 0)
            if not cmint or not duration:
                continue
            when = f"overdue by {abs(hrs_left):.1f}h" if hrs_left < 0 else f"expires in {hrs_left:.1f}h"
            pools[(cmint, duration)].append(
                f"{borrower}'s active loan {when} ({t['defaults']} defaults, "
                f"{t['late_repays']} late repayments, ${t['surplus_usd']:,.2f} known surplus)"
            )

    return [CapturePool(cmint, duration, reasons) for (cmint, duration), reasons in pools.items()]

# ---------------------------------------------------------------------------
# Pool pricing
# ---------------------------------------------------------------------------

def fetch_pool_offers(collateral_mint: str) -> list[dict]:
    """Live lending offers (any duration) for principal=USDC / this collateral."""
    raw: list[dict] = []
    for status in ("active", "partiallyFilled"):
        raw += _fetch_all_pages(
            "/offers",
            {
                "offerType": "lending", "status": status, "hideExpired": "true",
                "principalMint": USDC_MINT, "collateralMint": collateral_mint,
                "showUnverified": "true", "includeUnderfunded": "true",
            },
        )
    return raw


def _principal_usd(o: dict) -> float:
    return (o.get("metadata") or {}).get("principalAmountUsd") or 0.0


def _offer_ltv(o: dict) -> float | None:
    meta = o.get("metadata") or {}
    p_usd, c_usd = meta.get("principalAmountUsd"), meta.get("collateralAmountUsd")
    if p_usd and c_usd and c_usd > 0:
        return p_usd / c_usd
    return None


def compute_competitive_terms(pool: CapturePool, our_offer_usdc: float) -> dict[str, Any] | None:
    """
    Returns dict with target_apy_bps, target_ltv, target_duration, and the
    benchmark used to derive them — or None if there's no live offer to size
    a target from.

    Targets the SINGLE LARGEST live offer in the pool (by principal USD, any
    duration, excluding our own existing offers) rather than a pool-wide
    average: a borrower comparison-shopping the pool picks the best actual
    alternative available to them, not the average of everything listed —
    including a handful of small ones they'd never consider. We adopt that
    offer's duration too, since that's the specific listing we're competing
    against for the same borrower.

    LTV is still bounded by effective_target_ltv() (the same safety ceiling
    strategy.py uses) computed from the whole pool's volume-weighted median —
    beating the largest offer never overrides this cap.
    """
    offers = fetch_pool_offers(pool.collateral_mint)
    others = [o for o in offers if o.get("creator") != WALLET_PUBKEY and _principal_usd(o) > 0]
    if not others:
        return None  # nothing live (of ours excluded) to benchmark against

    largest = max(others, key=_principal_usd)
    target_duration = largest.get("duration") or pool.duration_secs
    largest_apy_bps = largest.get("apy", 0)
    largest_ltv = _offer_ltv(largest)

    # Safety ceiling still comes from the whole pool's volume-weighted MEDIAN LTV,
    # same as strategy.py — beating the largest offer never bypasses this. Median,
    # not mean: robust to a single large outlier offer dragging the benchmark.
    ltvs, weights = [], []
    for o in offers:
        ltv = _offer_ltv(o)
        if ltv is not None:
            ltvs.append(ltv)
            weights.append(_principal_usd(o))
    weighted_median_ltv = _common._volume_weighted_median(ltvs, weights)
    sample_count = len(ltvs)
    total_volume = sum(weights)
    fallback = fallback_ltv_for_duration(target_duration)
    safety_ceiling = effective_target_ltv(
        pool.collateral_mint, weighted_median_ltv, sample_count, total_volume, our_offer_usdc, fallback,
    )

    target_ltv = min(largest_ltv + LTV_EDGE_PTS, safety_ceiling) if largest_ltv is not None else safety_ceiling
    target_ltv = max(target_ltv, 0.05)
    target_apy_bps = max(MIN_APY_BPS, round(largest_apy_bps * (1 - APY_UNDERCUT)))

    return {
        "target_apy_bps": target_apy_bps,
        "target_ltv": target_ltv,
        "target_duration": target_duration,
        "largest_offer_usd": _principal_usd(largest),
        "largest_apy_bps": largest_apy_bps,
        "largest_ltv": largest_ltv,
        "safety_ceiling": safety_ceiling,
        "pool_sample_count": sample_count,
    }

# ---------------------------------------------------------------------------
# Signer
# ---------------------------------------------------------------------------

def resolve_signer_wallet() -> str:
    """Thin wrapper: delegates to offerbook_common, updates this module's WALLET_PUBKEY."""
    global WALLET_PUBKEY
    WALLET_PUBKEY = _common.resolve_signer_wallet(SIGNING_MODE, WALLET_PUBKEY, LEDGER_PATH)
    return WALLET_PUBKEY


def confirm_signing_mode(skip_prompt: bool) -> None:
    _common.confirm_signing_mode(SIGNING_MODE, WALLET_PUBKEY, LEDGER_PATH, DRY_RUN, skip_prompt)


def sign_and_send_transaction(tx_b64: str) -> str:
    if SIGNING_MODE == "ledger":
        from ledger_signer import LedgerError
        signer = _common.get_ledger_signer(LEDGER_PATH)
        log.info("  Awaiting approval on Ledger device …")
        try:
            signed_b64 = signer.sign_transaction(tx_b64, expected_signer=WALLET_PUBKEY)
        except LedgerError as exc:
            log.error("  %s", exc)
            return ""
        return _broadcast(signed_b64)
    else:
        import base58
        from solders.keypair import Keypair
        from solders.transaction import VersionedTransaction
        import base64
        keypair = Keypair.from_bytes(base58.b58decode(PRIVATE_KEY_B58))
        raw_tx = base64.b64decode(tx_b64)
        tx = VersionedTransaction.from_bytes(raw_tx)
        signed_tx = VersionedTransaction(tx.message, [keypair])
        signed_b64 = base64.b64encode(bytes(signed_tx)).decode()
        return _broadcast(signed_b64)


def _broadcast(signed_b64: str) -> str:
    payload = {
        "jsonrpc": "2.0", "id": 1, "method": "sendTransaction",
        "params": [signed_b64, {"encoding": "base64", "skipPreflight": False}],
    }
    resp = requests.post(SOLANA_RPC, json=payload, timeout=30)
    resp.raise_for_status()
    result = resp.json()
    if "error" in result:
        log.error("  RPC error: %s", result["error"])
        return ""
    return result["result"]

# ---------------------------------------------------------------------------
# Offer creation
# ---------------------------------------------------------------------------

def create_offer(params: dict[str, Any]) -> bool:
    log.info(
        "  Creating lending offer: principal=%s…  collateral=%s…  apy=%d bps (%.2f%%)  duration=%dd",
        params["principalMint"][:8], params["collateralMint"][:8],
        params["apy"], params["apy"] / 100, params["duration"] // 86400,
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
        log.warning("  OFFERBOOK_PRIVATE_KEY not set – cannot sign.")
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
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    global SIGNING_MODE, WALLET_PUBKEY

    parser = argparse.ArgumentParser(
        description="Post competitive lending offers into collateral pools where a watchlisted "
                    "borrower is actionable right now (open request, or a loan expiring within 24h)."
    )
    _common.add_signing_args(parser)
    parser.add_argument(
        "--min-surplus", type=float, default=0.0,
        help="Same threshold as defaulter_watch.py — only act on borrowers with total historical "
             "surplus above this USD amount (default: 0)",
    )
    args = parser.parse_args()

    SIGNING_MODE = _common.resolve_signing_mode(args.signing_mode, SIGNING_MODE)
    if SIGNING_MODE == "private_key" and not PRIVATE_KEY_B58:
        log.error("OFFERBOOK_PRIVATE_KEY not set in .env — required for --private-key mode.")
        sys.exit(1)

    resolve_signer_wallet()
    confirm_signing_mode(skip_prompt=args.yes)

    pools = get_actionable_pools(args.min_surplus)
    if not pools:
        log.info("Nothing actionable right now — no watchlisted borrower has an open request or a "
                  "loan expiring within 24h. Nothing to do.")
        sys.exit(1)

    log.info("Found %d actionable pool(s) to target.", len(pools))
    available_usdc_raw = fetch_available_balance(USDC_MINT, USDC_DECIMALS)

    created = skipped = errors = 0
    for pool in pools:
        log.info("")
        log.info("=" * 78)
        log.info("Pool: USDC / %s", symbol_for(pool.collateral_mint))
        for reason in pool.reasons:
            log.info("  trigger: %s", reason)

        fraction = ALLOCATION_CONFIG.get(pool.collateral_mint, ALLOCATION_CONFIG.get("default", 0.0))
        if fraction <= 0.0:
            log.info("  Skipping — %s has no allocation in allocation_config.yaml", symbol_for(pool.collateral_mint))
            skipped += 1
            continue

        principal_raw = int(available_usdc_raw * fraction)
        principal_usdc = principal_raw / 10 ** USDC_DECIMALS
        # minFillAmount must be >= 1001 raw units AND <= half of principalAmount (API constraint) —
        # so principal_raw must be >= 2002 for any valid minFillAmount to exist at all.
        if principal_raw < 2002:
            log.info("  Skipping — allocation too small (%.6f USDC)", principal_usdc)
            skipped += 1
            continue

        terms = compute_competitive_terms(pool, principal_usdc)
        if terms is None:
            log.info("  Skipping — no live offer in this pool to benchmark a competitive APY against")
            skipped += 1
            continue

        price, decimals = fetch_current_price(pool.collateral_mint)
        if not price or decimals is None:
            log.warning("  Skipping — no usable live price for %s", symbol_for(pool.collateral_mint))
            skipped += 1
            continue

        required_collateral_usdc = principal_usdc / terms["target_ltv"]
        collateral_raw = int(required_collateral_usdc / (price / 10 ** decimals))

        log.info(
            "  Largest live offer to beat : $%.2f  |  APY %.2f%%  |  LTV %s  |  duration %.0fd  (safety ceiling %.1f%%, n=%d offers pool-wide)",
            terms["largest_offer_usd"], terms["largest_apy_bps"] / 100,
            f"{terms['largest_ltv']*100:.1f}%" if terms["largest_ltv"] is not None else "n/a",
            terms["target_duration"] / 86400, terms["safety_ceiling"] * 100, terms["pool_sample_count"],
        )
        log.info(
            "  Our offer                  : %.2f USDC  →  LTV %.1f%%  APY %.2f%%  duration %.0fd",
            principal_usdc, terms["target_ltv"] * 100, terms["target_apy_bps"] / 100, terms["target_duration"] / 86400,
        )

        offer_params = {
            "signer": WALLET_PUBKEY,
            "principalMint": USDC_MINT,
            "collateralMint": pool.collateral_mint,
            "principalAmount": principal_raw,
            "collateralAmount": collateral_raw,
            "apy": terms["target_apy_bps"],
            "duration": terms["target_duration"],
            "expiry": OFFER_EXPIRY_SECS,
            "allowPartialFill": ALLOW_PARTIAL_FILL,
            "minFillAmount": max(1001, principal_raw // 100),
            "topup": "minimum",
        }

        if create_offer(offer_params):
            created += 1
        else:
            errors += 1
        time.sleep(0.5)

    log.info("")
    log.info("=" * 60)
    log.info("Done.  Created=%d  Skipped=%d  Errors=%d", created, skipped, errors)
    log.info("=" * 60)


if __name__ == "__main__":
    main()
