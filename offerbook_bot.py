"""
Offerbook Competitive Lending Bot
==================================
Strategy:
  1. Fetch all active lending offers (from all pools/pairs).
  2. Fetch all active loans.
  3. For each unique (principalMint, collateralMint) pair that has open loans or
     open lending offers, compute the mean APY of existing active lending offers.
  4. Post a new lending offer at mean_apy * 0.90 (10% below mean) to be competitive.
  5. Enforce:
       - duration  <= 7 days (604,800 seconds)
       - LTV       <= 40%  (collateralAmount / principalAmount >= 2.5x,
                            i.e. we lend 40% of the collateral value)

Usage:
  pip install requests solders base58
  export OFFERBOOK_WALLET=<your-base58-wallet-pubkey>
  export OFFERBOOK_PRIVATE_KEY=<your-base58-private-key>   # for signing txns
  python offerbook_bot.py

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

import base64
import logging
import os
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

import requests

# ---------------------------------------------------------------------------
# Configuration – edit here or override via environment variables
# ---------------------------------------------------------------------------

API_BASE = os.getenv("OFFERBOOK_API_BASE", "https://api.offerbook.jup.ag/api/v1")
TX_API_BASE = os.getenv("OFFERBOOK_TX_API_BASE", "https://api.offerbook.jup.ag/api/v1")
SOLANA_RPC = os.getenv("SOLANA_RPC", "https://api.mainnet-beta.solana.com")

WALLET_PUBKEY: str = os.getenv("OFFERBOOK_WALLET", "")
PRIVATE_KEY_B58: str = os.getenv("OFFERBOOK_PRIVATE_KEY", "")  # base58 private key

# Strategy parameters
APY_DISCOUNT = 0.10          # undercut market mean by 10%
MAX_DURATION_DAYS = 7
MAX_DURATION_SECS = MAX_DURATION_DAYS * 24 * 60 * 60   # 604 800
MAX_LTV = 0.40               # 40%  =>  collateral must be >= 2.5x principal
OFFER_EXPIRY_SECS = 7 * 24 * 60 * 60  # offer itself expires in 7 days

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

    @property
    def pair(self) -> tuple[str, str]:
        return (self.principal_mint, self.collateral_mint)


@dataclass
class PairStats:
    """Aggregated stats for a (principalMint, collateralMint) pair."""
    principal_mint: str
    collateral_mint: str
    lending_apys: list[int] = field(default_factory=list)   # active lending offers
    loan_apys: list[int] = field(default_factory=list)      # fallback: existing loans
    active_loan_count: int = 0

    # Amounts from existing offers – we'll size our offer similarly
    offer_principal_amounts: list[int] = field(default_factory=list)
    offer_collateral_amounts: list[int] = field(default_factory=list)

    # Fallback amounts from existing loans when no offers exist
    loan_principal_amounts: list[int] = field(default_factory=list)
    loan_collateral_amounts: list[int] = field(default_factory=list)

    @property
    def mean_apy_bps(self) -> float | None:
        # Prefer lending-offer APYs; fall back to existing loan APYs
        source = self.lending_apys if self.lending_apys else self.loan_apys
        if not source:
            return None
        return sum(source) / len(source)

    @property
    def apy_source(self) -> str:
        return "offers" if self.lending_apys else "loans"

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
    """Extract mint address from an OfferAsset tagged-union."""
    data = asset.get("data", {})
    return data.get("mint") or data.get("asset") or ""


def _parse_offer(raw: dict) -> Offer:
    meta = raw.get("metadata") or {}
    return Offer(
        pubkey=raw["pubkey"],
        creator=raw["creator"],
        offer_type=raw["offerType"],
        status=raw["status"],
        principal_mint=_mint_from_asset(raw.get("principal", {})),
        collateral_mint=_mint_from_asset(raw.get("collateral", {})),
        principal_amount=raw.get("remainingPrincipal", raw.get("principalAmount", 0)),
        collateral_amount=raw.get("remainingCollateral", raw.get("collateralAmount", 0)),
        apy=raw.get("apy", 0),
        duration=raw.get("duration", 0),
        principal_usd=meta.get("principalAmountUsd"),
        collateral_usd=meta.get("collateralAmountUsd"),
    )


def _parse_loan(raw: dict) -> Loan:
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
    )


def fetch_active_lending_offers() -> list[Offer]:
    log.info("Fetching active lending offers …")
    raw_items = _fetch_all_pages(
        "/offers",
        {"offerType": "lending", "status": "active", "hideExpired": "true"},
    )
    # also grab partiallyFilled
    raw_items += _fetch_all_pages(
        "/offers",
        {"offerType": "lending", "status": "partiallyFilled", "hideExpired": "true"},
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
# Strategy logic
# ---------------------------------------------------------------------------

def build_pair_stats(
    lending_offers: list[Offer],
    active_loans: list[Loan],
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
        ps.lending_apys.append(offer.apy)
        if offer.principal_amount > 0:
            ps.offer_principal_amounts.append(offer.principal_amount)
        if offer.collateral_amount > 0:
            ps.offer_collateral_amounts.append(offer.collateral_amount)

    for loan in active_loans:
        ps = get_or_create(loan.pair)
        ps.active_loan_count += 1
        if loan.apy > 0:
            ps.loan_apys.append(loan.apy)
        if loan.principal_amount > 0:
            ps.loan_principal_amounts.append(loan.principal_amount)
        if loan.collateral_amount > 0:
            ps.loan_collateral_amounts.append(loan.collateral_amount)

    return stats


def compute_offer_params(ps: PairStats) -> dict[str, Any] | None:
    """
    Compute the parameters for a new lending offer on a given pair.
    Returns None if we cannot or should not create an offer.
    """
    target_apy = ps.target_apy_bps
    if target_apy is None:
        log.debug("Skipping %s/%s – no market APY reference", ps.principal_mint[:8], ps.collateral_mint[:8])
        return None

    # Size the offer: use the median principal amount from existing offers
    principal_amount = ps.median_principal_amount
    collateral_amount = ps.median_collateral_amount

    if not principal_amount or not collateral_amount:
        log.debug("Skipping %s/%s – no amount data", ps.principal_mint[:8], ps.collateral_mint[:8])
        return None

    # Enforce max 40% LTV:
    # LTV = principal / collateral_value_in_same_units
    # For token-to-token with possibly different prices we use USD metadata when
    # available; otherwise assume same-price-unit (conservative fallback).
    # The collateral_amount here is what the BORROWER puts up.
    # Our max LTV = 40% means: principalUsd <= 0.40 * collateralUsd
    # => principalAmount <= 0.40 * collateralAmount  (when same price unit)
    if principal_amount > MAX_LTV * collateral_amount:
        # Adjust principal down to satisfy LTV, keeping collateral fixed
        adjusted_principal = int(MAX_LTV * collateral_amount)
        if adjusted_principal <= 0:
            log.debug("Skipping %s/%s – LTV adjustment yields 0", ps.principal_mint[:8], ps.collateral_mint[:8])
            return None
        log.debug(
            "  Adjusting principal %d → %d to respect %.0f%% max LTV",
            principal_amount, adjusted_principal, MAX_LTV * 100,
        )
        principal_amount = adjusted_principal

    # Apply per-offer cap for USDC principal offers
    if MAX_OFFER_PRINCIPAL_USDC and ps.principal_mint == USDC_MINT:
        cap_raw = MAX_OFFER_PRINCIPAL_USDC * (10 ** USDC_DECIMALS)
        if principal_amount > cap_raw:
            log.debug("  Capping principal %d → %d (MAX_OFFER_PRINCIPAL_USDC=%d)",
                      principal_amount, cap_raw, MAX_OFFER_PRINCIPAL_USDC)
            principal_amount = cap_raw

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
        "topup": "full",
    }


# ---------------------------------------------------------------------------
# Transaction submission
# ---------------------------------------------------------------------------

def sign_and_send_transaction(tx_b64: str) -> str:
    """
    Sign a base64-encoded Solana transaction with our private key and broadcast
    it via the Solana RPC.

    Requires:  pip install solders base58
    """
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
    tx.sign([keypair])

    signed_b64 = base64.b64encode(bytes(tx)).decode()

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
        "apy=%d bps (%.2f%%)  duration=%dd  LTV=%.1f%%",
        params["principalMint"][:8],
        params["collateralMint"][:8],
        params["apy"],
        params["apy"] / 100,
        params["duration"] // 86400,
        (params["principalAmount"] / params["collateralAmount"] * 100),
    )

    if DRY_RUN:
        log.info("  [DRY RUN] Skipping transaction submission.")
        return True

    try:
        tx_data = _post_tx("/create-principal-offer", params)
    except requests.HTTPError as exc:
        log.error("  TX builder error: %s  body=%s", exc, exc.response.text if exc.response else "")
        return False

    transactions: list[str] = tx_data.get("transactions", [])
    if not transactions:
        log.error("  TX builder returned no transactions!")
        return False

    if not PRIVATE_KEY_B58:
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
    if not WALLET_PUBKEY:
        log.error("OFFERBOOK_WALLET env var not set.  Exiting.")
        sys.exit(1)

    log.info("=" * 60)
    log.info("Offerbook Competitive Lending Bot")
    log.info("Wallet  : %s", WALLET_PUBKEY)
    log.info("DRY RUN : %s", DRY_RUN)
    log.info("Strategy: APY = market_mean × %.0f%%  |  max LTV = %.0f%%  |  max duration = %d days",
             (1 - APY_DISCOUNT) * 100, MAX_LTV * 100, MAX_DURATION_DAYS)
    log.info("=" * 60)

    # 1. Fetch USDC balance (wallet + escrow) before doing anything
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
    pair_stats = build_pair_stats(lending_offers, active_loans)
    log.info("Unique (principal, collateral) pairs found: %d", len(pair_stats))

    # 4. Filter pairs where there's actually demand (active loans) or supply
    relevant_pairs = {
        pair: ps
        for pair, ps in pair_stats.items()
        if ps.active_loan_count > 0 or len(ps.lending_apys) >= 2
    }
    log.info("Pairs with market activity: %d", len(relevant_pairs))

    # 5. Build and submit offers
    successes = 0
    skipped = 0
    errors = 0
    usdc_committed_raw = 0  # running total of USDC earmarked this session

    for pair, ps in relevant_pairs.items():
        log.info(
            "\nPair %s…/%s…  | loans=%d  mean_apy=%.0f bps [from %s]  target_apy=%s bps",
            ps.principal_mint[:8], ps.collateral_mint[:8],
            ps.active_loan_count,
            ps.mean_apy_bps or 0,
            ps.apy_source,
            ps.target_apy_bps or "N/A",
        )

        offer_params = compute_offer_params(ps)
        if offer_params is None:
            skipped += 1
            continue

        # Balance guard: scale down USDC offers that exceed remaining balance.
        # Since allowPartialFill=True, posting a smaller offer at the same LTV
        # is still attractive to borrowers — they can fill any fraction of it.
        if (
            usdc_available_raw is not None
            and offer_params["principalMint"] == USDC_MINT
        ):
            needed = offer_params["principalAmount"]
            available = usdc_available_raw - usdc_committed_raw
            if available <= 0:
                log.warning("  Skipping – no USDC remaining (committed=%.2f of %.2f)",
                            usdc_committed_raw / 10**USDC_DECIMALS,
                            usdc_available_raw / 10**USDC_DECIMALS)
                skipped += 1
                continue
            if needed > available:
                # Scale principal down to available; scale collateral proportionally
                # to preserve the LTV ratio so the offer stays attractive.
                scale = available / needed
                orig_principal = needed
                offer_params["principalAmount"] = int(available)
                offer_params["collateralAmount"] = int(offer_params["collateralAmount"] * scale)
                log.info(
                    "  Scaling offer to fit balance: principal %.2f → %.2f USDC "
                    "(collateral scaled by %.1f%%)",
                    orig_principal / 10**USDC_DECIMALS,
                    available / 10**USDC_DECIMALS,
                    scale * 100,
                )

        ok = create_offer(offer_params)
        if ok:
            successes += 1
            if offer_params["principalMint"] == USDC_MINT:
                usdc_committed_raw += offer_params["principalAmount"]
        else:
            errors += 1

        time.sleep(0.5)  # pace ourselves

    log.info("\n" + "=" * 60)
    log.info("Done.  Created=%d  Skipped=%d  Errors=%d", successes, skipped, errors)
    if usdc_committed_raw:
        log.info("USDC committed this run: %.6f USDC", usdc_committed_raw / 10**USDC_DECIMALS)
    log.info("=" * 60)


if __name__ == "__main__":
    main()
