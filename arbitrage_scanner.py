"""
Offerbook Same-Token Arbitrage Scanner
=========================================
The platform-wide "Spread" stat (Best Lend APY - Best Borrow APY) mixes
completely different collateral quality tiers — e.g. 9% to borrow against
JitoSOL vs. 90% to lend against an illiquid NFT. That's not a capturable
arbitrage, just the market's risk curve.

This scans for the real thing: collateral where you could BORROW cheaply
(a live lending offer, low APY) and simultaneously LEND into an existing
BORROW REQUEST for that *same* collateral at a materially higher APY — an
apples-to-apples spread on one collateral type, not two unrelated ones.

  "Borrow USDC" side = live LENDING offers (offerType=lending). Taking one
  makes you the borrower — this is the rate you'd pay.
  "Lend USDC" side = live BORROWING offers/requests (offerType=borrowing).
  Filling one makes you the lender — this is the rate you'd earn.

For each collateral appearing on both sides, reports:
  best_borrow_apy = cheapest live lending offer for that collateral (MIN apy)
  best_lend_apy   = richest live borrow request for that collateral (MAX apy)
  spread          = best_lend_apy - best_borrow_apy

Fungible tokens only (grouped by mint) — NFT/coreNft collateral (e.g.
Phygitals) is deliberately excluded, not just because it lacks a shared
mint, but because that's out of scope here.

This is a market scan only — it does NOT size, price, or execute the trade
for you. Read the docstring's caveats in the module or ask before treating
a flagged spread as free money: size at the best rate is usually tiny,
duration between the two legs may not line up, and the high lend-side APY
usually exists because that collateral carries real default risk.

Meant to run on a schedule (see .github/workflows/arbitrage_scan.yml, every
15 min) so it costs nothing beyond GitHub Actions' free minutes. Email is
sent only for NEWLY appearing (borrow-offer, lend-offer) pubkey pairs —
state is persisted to arbitrage_scanner_state.json (committed back to the
repo by the workflow) so the same still-open opportunity doesn't re-email
every run. State is reset to exactly the current run's reported pairs each
time, so anything no longer live (filled/cancelled/expired) is dropped
automatically — no separate "resolved" cleanup needed.

Usage:
  python arbitrage_scanner.py                  # top 15 spreads, email if new ones found
  python arbitrage_scanner.py --top 30
  python arbitrage_scanner.py --min-spread 20   # only spreads >= 20 points
  python arbitrage_scanner.py --min-size 50     # ignore legs under $50 available
  python arbitrage_scanner.py --no-email        # console output only, skip email + state

Required env vars for email (set as GitHub Actions secrets — never committed):
  SMTP_FROM_EMAIL    - Gmail address to send from
  SMTP_APP_PASSWORD  - Gmail App Password for that address
  NOTIFY_EMAIL_TO    - recipient address
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import smtplib
from collections import defaultdict
from dataclasses import dataclass, field
from email.mime.text import MIMEText
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

import offerbook_common as _common

API_BASE = os.getenv("OFFERBOOK_API_BASE", "https://api.offerbook.jup.ag/api/v1")
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
PAGE_SIZE = 100

KNOWN_SYMBOLS = _common.KNOWN_SYMBOLS

STATE_PATH = Path(__file__).parent / "arbitrage_scanner_state.json"

SMTP_FROM_EMAIL = os.getenv("SMTP_FROM_EMAIL")
SMTP_APP_PASSWORD = os.getenv("SMTP_APP_PASSWORD")
NOTIFY_EMAIL_TO = os.getenv("NOTIFY_EMAIL_TO")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("arbitrage_scanner")

SESSION = requests.Session()


def load_state() -> set[str]:
    if STATE_PATH.exists():
        return set(json.loads(STATE_PATH.read_text()))
    return set()


def save_state(ids: set[str]) -> None:
    STATE_PATH.write_text(json.dumps(sorted(ids), indent=2) + "\n")


def send_email(subject: str, body: str) -> None:
    if not (SMTP_FROM_EMAIL and SMTP_APP_PASSWORD and NOTIFY_EMAIL_TO):
        log.warning("SMTP env vars not set — skipping email: %s", subject)
        return
    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = SMTP_FROM_EMAIL
    msg["To"] = NOTIFY_EMAIL_TO
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(SMTP_FROM_EMAIL, SMTP_APP_PASSWORD)
        server.send_message(msg)


def _fetch_all_pages(endpoint: str, params: dict | None = None) -> list[dict]:
    return _common.fetch_all_pages(SESSION, API_BASE, endpoint, params, PAGE_SIZE)


def collateral_key(raw_offer: dict) -> str:
    """Grouping key for 'same collateral' — mint, fungible tokens only.
    Returns "" for NFT/coreNft collateral so it gets filtered out entirely."""
    collateral = raw_offer.get("collateral") or {}
    if collateral.get("kind") != "token":
        return ""
    return collateral.get("mint") or ""


def display_name(key: str) -> str:
    return KNOWN_SYMBOLS.get(key, key[:8] + "…" if len(key) > 12 else key)


@dataclass
class Leg:
    apy_bps: int
    principal_usd: float
    ltv: float | None
    duration_secs: int
    pubkey: str


@dataclass
class CollateralBook:
    lending: list[Leg] = field(default_factory=list)   # you can borrow here
    borrowing: list[Leg] = field(default_factory=list)  # you can lend here


def fetch_offers(offer_type: str) -> list[dict]:
    raw: list[dict] = []
    for status in ("active", "partiallyFilled"):
        raw += _fetch_all_pages(
            "/offers",
            {
                "offerType": offer_type, "status": status, "hideExpired": "true",
                "principalMint": USDC_MINT, "showUnverified": "true", "includeUnderfunded": "true",
            },
        )
    return raw


def build_books() -> dict[str, CollateralBook]:
    books: dict[str, CollateralBook] = defaultdict(CollateralBook)

    log.info("Fetching live lending offers (the 'borrow' side)…")
    for o in fetch_offers("lending"):
        meta = o.get("metadata") or {}
        p_usd, c_usd = meta.get("principalAmountUsd"), meta.get("collateralAmountUsd")
        if not p_usd:
            continue
        key = collateral_key(o)
        if not key:
            continue
        ltv = p_usd / c_usd if c_usd else None
        books[key].lending.append(Leg(o["apy"], p_usd, ltv, o.get("duration", 0), o["pubkey"]))

    log.info("Fetching live borrow requests (the 'lend' side)…")
    for o in fetch_offers("borrowing"):
        meta = o.get("metadata") or {}
        p_usd, c_usd = meta.get("principalAmountUsd"), meta.get("collateralAmountUsd")
        if not p_usd:
            continue
        key = collateral_key(o)
        if not key:
            continue
        ltv = p_usd / c_usd if c_usd else None
        books[key].borrowing.append(Leg(o["apy"], p_usd, ltv, o.get("duration", 0), o["pubkey"]))

    return books


def find_spreads(books: dict[str, CollateralBook], min_size_usd: float) -> list[dict]:
    results = []
    for key, book in books.items():
        borrow_legs = [l for l in book.lending if l.principal_usd >= min_size_usd]
        lend_legs = [l for l in book.borrowing if l.principal_usd >= min_size_usd]
        if not borrow_legs or not lend_legs:
            continue
        best_borrow = min(borrow_legs, key=lambda l: l.apy_bps)
        best_lend = max(lend_legs, key=lambda l: l.apy_bps)
        spread_bps = best_lend.apy_bps - best_borrow.apy_bps
        if spread_bps <= 0:
            continue
        results.append({
            "key": key, "spread_bps": spread_bps,
            "borrow": best_borrow, "lend": best_lend,
        })
    results.sort(key=lambda r: -r["spread_bps"])
    return results


def spread_id(s: dict) -> str:
    """Identity for dedup: the exact pair of live offers making up this
    spread. Offer pubkeys are never reused, so once either leg is filled,
    cancelled, or expired this id simply stops appearing in future runs."""
    return f"{s['borrow'].pubkey}:{s['lend'].pubkey}"


def describe_spread(s: dict) -> str:
    b, l = s["borrow"], s["lend"]
    b_ltv = f"{b.ltv*100:.0f}%" if b.ltv else "n/a"
    l_ltv = f"{l.ltv*100:.0f}%" if l.ltv else "n/a"
    return (
        f"{display_name(s['key'])}  —  spread {s['spread_bps']/100:.1f}%\n"
        f"  Borrow @ {b.apy_bps/100:.1f}% APY, ${b.principal_usd:,.0f} available, "
        f"LTV {b_ltv}, {b.duration_secs/86400:.0f}d term (offer {b.pubkey})\n"
        f"  Lend   @ {l.apy_bps/100:.1f}% APY, ${l.principal_usd:,.0f} requested, "
        f"LTV {l_ltv}, {l.duration_secs/86400:.0f}d term (offer {l.pubkey})"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--top", type=int, default=15, help="How many spreads to show (default 15)")
    parser.add_argument("--min-spread", type=float, default=1.0, help="Minimum spread in APY points to report (default 1.0)")
    parser.add_argument("--min-size", type=float, default=20.0, help="Ignore legs with less than this many USD available (default 20)")
    parser.add_argument("--no-email", action="store_true", help="Console output only — skip email and state persistence")
    args = parser.parse_args()

    # TEMPORARY: one-off email-delivery check, not real logic. Remove after
    # confirming the email arrives — see conversation, not meant to ship.
    if os.getenv("FAKE_TEST_SPREAD", "").lower() == "true":
        log.warning("FAKE_TEST_SPREAD=true — sending a one-off TEST email, not touching real state")
        fake = {
            "key": "So11111111111111111111111111111111111111112",
            "spread_bps": 3600,
            "borrow": Leg(900, 5000, 0.45, 604800, "TEST-FAKE-BORROW-PUBKEY-NOT-REAL"),
            "lend": Leg(4500, 3000, 0.60, 604800, "TEST-FAKE-LEND-PUBKEY-NOT-REAL"),
        }
        send_email(
            "[TEST] Offerbook arbitrage scanner — email delivery check",
            "This is a SYNTHETIC test spread sent to confirm email delivery works.\n"
            "No real arbitrage opportunity exists right now — safe to ignore.\n\n"
            "Example of what a real alert would look like:\n\n" + describe_spread(fake),
        )
        return

    books = build_books()
    log.info("Collateral types with a live offer on at least one side: %d", len(books))

    spreads = find_spreads(books, args.min_size)
    spreads = [s for s in spreads if s["spread_bps"] / 100 >= args.min_spread]
    log.info("Collateral types with BOTH sides live and a positive spread >= %.1f pts: %d", args.min_spread, len(spreads))

    if not args.no_email:
        previously_notified = load_state()
        new_spreads = [s for s in spreads if spread_id(s) not in previously_notified]
        if new_spreads:
            log.info("New arbitrage opportunit%s since last run: %d", "y" if len(new_spreads) == 1 else "ies", len(new_spreads))
            subject = (
                f"Offerbook arbitrage: {display_name(new_spreads[0]['key'])} "
                f"{new_spreads[0]['spread_bps']/100:.1f}% spread"
                if len(new_spreads) == 1
                else f"Offerbook arbitrage: {len(new_spreads)} new opportunities"
            )
            body = "\n\n".join(describe_spread(s) for s in new_spreads)
            send_email(subject, body)
        else:
            log.info("No new opportunities since last run (nothing to email).")
        save_state({spread_id(s) for s in spreads})

    if not spreads:
        log.info("Nothing to show — try lowering --min-spread or --min-size.")
        return

    log.info("")
    log.info("=" * 100)
    log.info("SAME-COLLATERAL ARBITRAGE — top %d by spread", min(args.top, len(spreads)))
    log.info("=" * 100)
    col = "{:<22}{:>9}{:>10}{:>9}{:>9}{:>10}{:>9}{:>9}"
    log.info(col.format("collateral", "spread", "borrow@", "b.size$", "b.ltv", "lend@", "l.size$", "l.ltv"))
    log.info("-" * 100)
    for s in spreads[: args.top]:
        b, l = s["borrow"], s["lend"]
        log.info(col.format(
            display_name(s["key"]),
            f"{s['spread_bps']/100:.1f}%",
            f"{b.apy_bps/100:.1f}%", f"{b.principal_usd:,.0f}",
            f"{b.ltv*100:.0f}%" if b.ltv else "n/a",
            f"{l.apy_bps/100:.1f}%", f"{l.principal_usd:,.0f}",
            f"{l.ltv*100:.0f}%" if l.ltv else "n/a",
        ))
    log.info("-" * 100)
    log.info("borrow@ = cheapest live lending offer you could take (what you'd pay)")
    log.info("lend@   = richest live borrow request you could fund (what you'd earn)")
    log.info("Reminder: size at the best rate is often small, legs may have different")
    log.info("durations, and the high lend-side APY usually reflects real default risk")
    log.info("on that specific collateral — this is a scan, not a recommendation.")
    log.info("=" * 100)


if __name__ == "__main__":
    main()
