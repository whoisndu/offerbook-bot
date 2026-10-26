"""
Offerbook Breakeven Check
==================================
For your own lending exposure — live (unfilled/partially-filled) lending
offers, plus active loans where you're the lender — shows how far the
collateral price can fall before the position "breaks even": the price at
which collateral value drops to equal your PRINCIPAL. Below that price, a
default leaves you with collateral worth less than what you actually lent
— same `LTV >= 100%` threshold `portfolio_health.py`'s UNDERWATER_LTV
uses, just expressed as a price level and a % drop instead of an LTV
percentage.

Deliberately measured against PRINCIPAL ONLY, not principal + interest.
The interest-inclusive price is a higher threshold reached FIRST as
collateral falls, and it marks something different: the point past which
it stops being rational for the BORROWER to repay at all (repaying costs
more than redeeming the collateral is worth, so a rational borrower walks
instead). By the time price reaches that line, you should already expect
non-repayment regardless of how much further it falls — it doesn't tell
you anything new about YOUR capital. What actually matters for your own
risk is the principal line: that's the point where a default would mean
losing part of what you originally put in, not just missing out on the
interest you were hoping to earn.

Scoped to your LENDING side only (offerType="lending" offers you created,
and loans where you're the `lender`) — collateral-price risk is a lender
concern; a borrowing-side offer/loan has the opposite exposure (you'd be
the one posting collateral) and isn't what this answers. Only USDC-
principal positions are supported, matching every other script here.

Read-only: never signs or submits anything.

Usage:
  python breakeven_check.py --offer <pubkey>      # a single offer OR loan pubkey, auto-detected
  python breakeven_check.py --address <wallet>    # every live offer + active loan for this wallet
  python breakeven_check.py                       # defaults to every wallet in OFFERBOOK_PORTFOLIO_WALLETS
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv

load_dotenv()

# Shared modules live in ../lib — see README's repo-layout note.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))

import offerbook_common as _common
from offerbook_common import _mint_from_asset

API_BASE = os.getenv("OFFERBOOK_API_BASE", "https://api.offerbook.jup.ag/api/v1")
PAGE_SIZE = 100

JUPITER_PRICE_API = "https://api.jup.ag/price/v3"
JUPITER_API_KEY = os.getenv("JUPITER_API_KEY", "")
DEXSCREENER_API = "https://api.dexscreener.com/latest/dex/tokens"

USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDC_DECIMALS = 6

KNOWN_SYMBOLS = _common.KNOWN_SYMBOLS
KNOWN_DECIMALS = _common.KNOWN_DECIMALS

PORTFOLIO_WALLETS = [w.strip() for w in os.getenv("OFFERBOOK_PORTFOLIO_WALLETS", "").split(",") if w.strip()]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("breakeven_check")

SESSION = requests.Session()


def symbol_for(mint: str) -> str:
    return KNOWN_SYMBOLS.get(mint, mint[:6] + "…" if mint else "?")


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


def fetch_current_prices_batched(mints: list[str]) -> tuple[dict[str, float], dict[str, int]]:
    """Same approach as defaulter_capture.py's helper of the same name —
    one batched Jupiter request, DexScreener fallback for whatever Jupiter
    doesn't have."""
    if not mints:
        return {}, {}
    prices: dict[str, float] = {}
    decimals: dict[str, int] = {}
    try:
        headers = {"x-api-key": JUPITER_API_KEY} if JUPITER_API_KEY else {}
        resp = SESSION.get(JUPITER_PRICE_API, params={"ids": ",".join(mints)}, headers=headers, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        prices = {mint: float(info["usdPrice"]) for mint, info in data.items() if info.get("usdPrice")}
        decimals = {mint: int(info["decimals"]) for mint, info in data.items() if info.get("decimals") is not None}
    except Exception as exc:
        log.warning("Could not fetch batched Jupiter prices: %s", exc)

    missing = [m for m in mints if m not in prices]
    for mint in missing:
        price = _fetch_dexscreener_price(mint)
        if price:
            prices[mint] = price
        decimals.setdefault(mint, KNOWN_DECIMALS.get(mint))
    return prices, decimals


# ---------------------------------------------------------------------------
# Fetch helpers
# ---------------------------------------------------------------------------

def fetch_offer(pubkey: str) -> dict | None:
    try:
        return _common.api_get(SESSION, API_BASE, f"/offer/{pubkey}")
    except requests.HTTPError as exc:
        if exc.response is not None and exc.response.status_code == 404:
            return None
        raise


def fetch_loan(pubkey: str) -> dict | None:
    try:
        return _common.api_get(SESSION, API_BASE, f"/loan/{pubkey}")
    except requests.HTTPError as exc:
        if exc.response is not None and exc.response.status_code == 404:
            return None
        raise


def fetch_lending_offers_for(address: str) -> list[dict]:
    return _common.fetch_all_pages(
        SESSION, API_BASE, "/offers",
        {
            "offerType": "lending", "status": "Active,PartiallyFilled", "creator": address,
            "showUnverified": "true", "includeUnderfunded": "true",
        },
        PAGE_SIZE,
    )


def fetch_active_loans_for(address: str) -> list[dict]:
    """Platform-wide active loans, filtered client-side to this address as
    lender — same approach portfolio_health.py/address_snapshot.py use
    (no creator/lender filter exists on this endpoint)."""
    loans = _common.fetch_all_pages(SESSION, API_BASE, "/loans/status/active", None, PAGE_SIZE)
    return [l for l in loans if l.get("lender") == address]


# ---------------------------------------------------------------------------
# Row construction
# ---------------------------------------------------------------------------

def _mint_of(obj: dict, field: str, asset_key: str) -> str:
    return obj.get(field) or _mint_from_asset(obj.get(asset_key, {}))


def row_from_offer(offer: dict) -> dict | None:
    if offer.get("offerType") != "lending":
        return None
    principal_mint = _mint_of(offer, "principalMint", "principal")
    if principal_mint != USDC_MINT:
        return None
    collateral_mint = _mint_of(offer, "collateralMint", "collateral")
    principal_raw = offer.get("remainingPrincipal", offer.get("principalAmount", 0))
    collateral_raw = offer.get("remainingCollateral", offer.get("collateralAmount", 0))
    if principal_raw <= 0 or collateral_raw <= 0:
        return None
    return {
        "kind": "OFFER",
        "pubkey": offer.get("pubkey", ""),
        "collateral_mint": collateral_mint,
        "principal_usd": principal_raw / 10 ** USDC_DECIMALS,
        "collateral_raw": collateral_raw,
        "due": offer.get("expiredAt", ""),
    }


def row_from_loan(loan: dict) -> dict | None:
    principal_mint = _mint_of(loan, "principalMint", "principal")
    if principal_mint != USDC_MINT:
        return None
    collateral_mint = _mint_of(loan, "collateralMint", "collateral")
    principal_raw = loan.get("outstandingPrincipal", loan.get("principalAmount", 0))
    collateral_raw = loan.get("outstandingCollateral", loan.get("collateralAmount", 0))
    if principal_raw <= 0 or collateral_raw <= 0:
        return None
    return {
        "kind": "LOAN",
        "pubkey": loan.get("pubkey", ""),
        "collateral_mint": collateral_mint,
        "principal_usd": principal_raw / 10 ** USDC_DECIMALS,
        "collateral_raw": collateral_raw,
        "due": loan.get("expiredAt", ""),
    }


def finalize_row(row: dict, price: float | None, decimals: int | None) -> dict:
    """Fill in the derived fields (collateral whole amount, current value,
    breakeven price vs principal, % drop needed) once a live price is
    known. Mutates and returns `row`."""
    collateral_whole = row["collateral_raw"] / 10 ** decimals if decimals is not None else None
    row["collateral_symbol"] = symbol_for(row["collateral_mint"])
    row["collateral_whole"] = collateral_whole

    if price and collateral_whole:
        row["collateral_usd_now"] = collateral_whole * price
        row["current_ltv_pct"] = row["principal_usd"] / row["collateral_usd_now"] * 100
        row["price_now"] = price
        row["breakeven_principal"] = row["principal_usd"] / collateral_whole
        row["drop_pct_principal"] = (1 - row["breakeven_principal"] / price) * 100
    else:
        for k in ("collateral_usd_now", "current_ltv_pct", "price_now",
                   "breakeven_principal", "drop_pct_principal"):
            row[k] = None
    return row


def _fmt_due(due: str) -> str:
    if not due:
        return "—"
    try:
        dt = datetime.fromisoformat(due.replace("Z", "+00:00"))
        hrs = (dt - datetime.now(timezone.utc)).total_seconds() / 3600
        return f"{hrs:.0f}h" if hrs >= 0 else "overdue"
    except ValueError:
        return "—"


def print_rows(rows: list[dict]) -> None:
    if not rows:
        log.info("No live lending offers or active loans found.")
        return

    priced = [r for r in rows if r["price_now"] is not None]
    unpriced = [r for r in rows if r["price_now"] is None]
    # Most at-risk first — smallest cushion to the principal-only breakeven.
    priced.sort(key=lambda r: r["drop_pct_principal"])

    log.info("")
    col = "{:<6}{:<10}{:>11}{:>10}{:>11}{:>12}{:>8}{:>8}"
    log.info(col.format(
        "type", "collat", "principal", "price", "LTV", "breakeven", "drop%", "due",
    ))
    for r in priced:
        log.info(
            "{:<6}{:<10}{:>11}{:>10}{:>10.1f}%{:>12}{:>7.1f}%{:>8}".format(
                r["kind"], r["collateral_symbol"],
                f"${r['principal_usd']:,.0f}",
                f"${r['price_now']:.4g}", r["current_ltv_pct"],
                f"${r['breakeven_principal']:.4g}", r["drop_pct_principal"],
                _fmt_due(r["due"]),
            )
        )
    if unpriced:
        log.info("")
        log.info("No live price available (skipped breakeven calc) for:")
        for r in unpriced:
            log.info("  %s  %s  principal $%s  (%s)", r["kind"], r["collateral_symbol"], f"{r['principal_usd']:,.2f}", r["pubkey"])

    log.info("")
    log.info("Sorted by least cushion first (smallest % drop to breakeven) — these are the most exposed to a price drop.")


def process_pubkey(pubkey: str) -> list[dict]:
    log.info("Looking up %s …", pubkey)
    offer = fetch_offer(pubkey)
    if offer is not None:
        if offer.get("status") not in ("active", "partiallyFilled"):
            log.error("Offer %s is %s, not live.", pubkey, offer.get("status"))
            return []
        row = row_from_offer(offer)
        if row is None:
            log.error("Offer %s isn't a USDC-principal lending offer — out of scope for this tool.", pubkey)
            return []
        return [row]

    loan = fetch_loan(pubkey)
    if loan is not None:
        if loan.get("status") != "active":
            log.error("Loan %s is %s, not active.", pubkey, loan.get("status"))
            return []
        row = row_from_loan(loan)
        if row is None:
            log.error("Loan %s isn't a USDC-principal loan — out of scope for this tool.", pubkey)
            return []
        return [row]

    log.error("%s wasn't found as either an offer or a loan.", pubkey)
    return []


def process_address(address: str) -> list[dict]:
    log.info("Fetching live lending offers and active loans for %s …", address)
    offers = fetch_lending_offers_for(address)
    loans = fetch_active_loans_for(address)
    log.info("  → %d live lending offer(s), %d active loan(s) as lender", len(offers), len(loans))

    rows = []
    for o in offers:
        r = row_from_offer(o)
        if r:
            rows.append(r)
    for l in loans:
        r = row_from_loan(l)
        if r:
            rows.append(r)
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    target = parser.add_mutually_exclusive_group()
    target.add_argument("--offer", default=None, help="A single offer or loan pubkey (auto-detected).")
    target.add_argument("--address", default=None, help="A wallet address — every live offer + active loan for it.")
    args = parser.parse_args()

    if args.offer:
        rows = process_pubkey(args.offer)
    elif args.address:
        rows = process_address(args.address)
    else:
        if not PORTFOLIO_WALLETS:
            log.error("No --offer/--address given and OFFERBOOK_PORTFOLIO_WALLETS isn't set in .env.")
            sys.exit(1)
        rows = []
        for w in PORTFOLIO_WALLETS:
            rows.extend(process_address(w))

    if not rows:
        log.info("Nothing to check.")
        return

    mints = sorted({r["collateral_mint"] for r in rows if r["collateral_mint"]})
    prices, decimals = fetch_current_prices_batched(mints)
    log.info("Live prices resolved for %d/%d collateral mint(s)", len(prices), len(mints))

    finalized = [finalize_row(r, prices.get(r["collateral_mint"]), decimals.get(r["collateral_mint"])) for r in rows]
    print_rows(finalized)


if __name__ == "__main__":
    main()
