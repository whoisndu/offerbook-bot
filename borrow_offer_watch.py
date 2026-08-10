"""
Offerbook New Borrow Request Watch
======================================
Emails on every newly-appearing open borrow request platform-wide — every
principal, every collateral type (including NFTs), no profitability filter.
This is the raw feed; for "is this one worth acting on" see
arbitrage_scanner.py, which only alerts on borrow requests that clear a
profitable spread against a live lending offer for the same collateral.

Dedup works the same way as arbitrage_scanner.py: state is the set of
currently-open borrow-offer pubkeys, persisted to
borrow_offer_watch_state.json (committed back to the repo by the workflow —
these are public open offers, not competitive intel, so unlike
competitor_timing_state.json there's nothing here worth keeping private).
Each run only emails offers not in that set, then overwrites the state with
exactly this run's live set — so anything no longer open (filled, cancelled,
expired) simply stops appearing next run, no separate cleanup needed.

Meant to run on a schedule (see .github/workflows/borrow_offer_watch.yml,
every 15 min) — costs only GitHub Actions minutes.

Usage:
  python borrow_offer_watch.py                     # all open borrow requests, email if new ones found
  python borrow_offer_watch.py --min-size 20        # ignore requests under $20 principal
  python borrow_offer_watch.py --principal-mint <mint>   # only this principal token
  python borrow_offer_watch.py --no-email           # console output only, skip email + state

Required env vars for email (set as GitHub Actions secrets — never committed):
  SMTP_FROM_EMAIL    - Gmail address to send from
  SMTP_APP_PASSWORD  - Gmail App Password for that address
  NOTIFY_EMAIL_TO    - recipient address
  OFFERBOOK_WALLET   - used to exclude our own borrow requests, if any; a warning is
                        logged (not skipped) if unset, same as other scan scripts
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import smtplib
from email.mime.text import MIMEText
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

import offerbook_common as _common
from offerbook_common import _mint_from_asset

API_BASE = os.getenv("OFFERBOOK_API_BASE", "https://api.offerbook.jup.ag/api/v1")
PAGE_SIZE = 100

JUPITER_TOKEN_SEARCH_API = "https://api.jup.ag/tokens/v2/search"
JUPITER_SEARCH_BATCH_SIZE = 50

KNOWN_SYMBOLS = _common.KNOWN_SYMBOLS

# Populated once per run by resolve_symbols() below — mints not in the
# curated KNOWN_SYMBOLS table (long-tail/pump.fun tokens) get their real
# symbol looked up via Jupiter instead of a truncated address. Same approach
# as offer_posting_times.py's resolve_symbols()/borrower_loan_timeline.py's
# resolve_collateral_symbols().
_RESOLVED_SYMBOLS: dict[str, str] = {}

STATE_PATH = Path(__file__).parent / "borrow_offer_watch_state.json"

SMTP_FROM_EMAIL = os.getenv("SMTP_FROM_EMAIL")
SMTP_APP_PASSWORD = os.getenv("SMTP_APP_PASSWORD")
NOTIFY_EMAIL_TO = os.getenv("NOTIFY_EMAIL_TO")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("borrow_offer_watch")

SESSION = requests.Session()


def symbol_for(mint: str) -> str:
    if not mint:
        return "unknown"
    if mint in KNOWN_SYMBOLS:
        return KNOWN_SYMBOLS[mint]
    if mint in _RESOLVED_SYMBOLS:
        return _RESOLVED_SYMBOLS[mint]
    return f"{mint[:6]}…{mint[-4:]}"


def resolve_symbols(mints: set[str]) -> None:
    """Look up real symbols (via Jupiter's token search API) for any mint in
    `mints` that isn't already in KNOWN_SYMBOLS — covers long-tail/pump.fun
    tokens the curated table doesn't have."""
    unresolved = sorted(m for m in mints if m and m not in KNOWN_SYMBOLS and m not in _RESOLVED_SYMBOLS)
    if not unresolved:
        return
    for i in range(0, len(unresolved), JUPITER_SEARCH_BATCH_SIZE):
        chunk = unresolved[i : i + JUPITER_SEARCH_BATCH_SIZE]
        try:
            resp = SESSION.get(JUPITER_TOKEN_SEARCH_API, params={"query": ",".join(chunk)}, timeout=15)
            resp.raise_for_status()
            for t in resp.json():
                mint, sym = t.get("id"), t.get("symbol")
                if mint and sym:
                    _RESOLVED_SYMBOLS[mint] = sym
        except Exception as exc:
            log.warning("Jupiter token symbol lookup failed for a batch: %s", exc)


# "kind" is the API's own discriminator for what the collateral actually is
# (see OfferAsset in api-1.json) — classicNft/programmableNft offers DO carry
# a mint field (just like a fungible token), so detecting "NFT" by an empty
# mint alone (the old approach) missed those two kinds. Using `kind` directly
# is what arbitrage_scanner.py's collateral_key() already does, for the same
# reason.
NFT_COLLATERAL_KINDS = {"classicNft", "programmableNft", "coreNft"}


def collateral_label(collateral: dict) -> tuple[str, bool]:
    """Returns (display label, is_nft)."""
    kind = collateral.get("kind", "")
    if kind not in NFT_COLLATERAL_KINDS:
        return symbol_for(_mint_from_asset(collateral)), False
    ident = collateral.get("mint") or collateral.get("asset") or ""
    return (f"NFT ({ident[:6]}…{ident[-4:]})" if ident else "NFT"), True


def load_state() -> set[str]:
    if STATE_PATH.exists():
        return set(json.loads(STATE_PATH.read_text()))
    return set()


def save_state(pubkeys: set[str]) -> None:
    STATE_PATH.write_text(json.dumps(sorted(pubkeys), indent=2) + "\n")


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


def fetch_open_borrow_offers(principal_mint: str | None) -> list[dict]:
    """Open (active/partiallyFilled) borrow requests platform-wide, any collateral type."""
    params = {"offerType": "borrowing", "hideExpired": "true",
              "showUnverified": "true", "includeUnderfunded": "true"}
    if principal_mint:
        params["principalMint"] = principal_mint
    raw: list[dict] = []
    for status in ("active", "partiallyFilled"):
        raw += _common.fetch_all_pages(SESSION, API_BASE, "/offers", {**params, "status": status}, PAGE_SIZE)
    return raw


def describe_offer(o: dict) -> dict:
    meta = o.get("metadata") or {}
    p_usd = meta.get("principalAmountUsd") or 0.0
    c_usd = meta.get("collateralAmountUsd") or 0.0
    p_mint = o.get("principalMint") or _mint_from_asset(o.get("principal", {}))
    c_label, is_nft = collateral_label(o.get("collateral") or {})
    return {
        "pubkey": o["pubkey"],
        "creator": o.get("creator", ""),
        "principal_symbol": symbol_for(p_mint),
        "principal_usd": p_usd,
        "collateral_symbol": c_label,
        "collateral_usd": c_usd,
        "is_nft": is_nft,
        "ltv": p_usd / c_usd if c_usd else None,
        "apy_bps": o.get("apy", 0),
        "duration_secs": o.get("duration", 0),
        "created_at": o.get("createdAt", ""),
    }


def format_offer_line(d: dict) -> str:
    ltv = f"{d['ltv']*100:.0f}%" if d["ltv"] else "n/a"
    return (
        f"Borrow request: wants {d['principal_symbol']} ${d['principal_usd']:,.0f} "
        f"against {d['collateral_symbol']} (${d['collateral_usd']:,.0f}), "
        f"offering {d['apy_bps']/100:.1f}% APY, LTV {ltv}, "
        f"{d['duration_secs']/86400:.1f}d term\n"
        f"  creator {d['creator']}  offer {d['pubkey']}  created {d['created_at']}"
    )


def split_by_collateral_type(offers: list[dict]) -> tuple[list[dict], list[dict]]:
    """(token_backed, nft_backed) — both ranked largest to smallest loan size.
    Assumes `offers` is already sorted by principal_usd descending; filtering
    preserves that order in each sublist rather than re-sorting."""
    token_offers = [d for d in offers if not d["is_nft"]]
    nft_offers = [d for d in offers if d["is_nft"]]
    return token_offers, nft_offers


def format_section(title: str, items: list[dict]) -> str:
    header = f"{title} ({len(items)}):"
    if not items:
        return header
    return header + "\n" + "\n\n".join(format_offer_line(d) for d in items)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--min-size", type=float, default=0.0, help="Ignore requests with less than this many USD principal requested (default 0 — no filter).")
    parser.add_argument("--principal-mint", default=None, help="Only include this principal token mint (default: all principals).")
    parser.add_argument("--wallet", default=None, help="Wallet pubkey to exclude as 'self' (default: OFFERBOOK_WALLET env var).")
    parser.add_argument("--include-self", action="store_true", help="Include our own borrow requests instead of excluding them.")
    parser.add_argument("--no-email", action="store_true", help="Console output only — skip email and state persistence.")
    args = parser.parse_args()

    wallet = args.wallet or os.getenv("OFFERBOOK_WALLET", "")
    if not args.include_self and not wallet:
        log.warning("No OFFERBOOK_WALLET/--wallet set — self-exclusion has nothing to exclude; "
                    "our own borrow requests (if any) will be included.")

    log.info("Fetching open borrow requests market-wide …")
    raw = fetch_open_borrow_offers(args.principal_mint)
    log.info("  → %d open borrow request(s) fetched", len(raw))

    if not args.include_self and wallet:
        before = len(raw)
        raw = [o for o in raw if o.get("creator") != wallet]
        log.info("  → excluded %d of our own borrow request(s)", before - len(raw))

    # Resolve real symbols for principal mints and fungible (kind="token")
    # collateral mints before building descriptions — NFT mints are each a
    # unique one-of-one, not a fungible symbol, so they're left alone here
    # and handled by collateral_label()'s truncated-identifier fallback.
    mints_to_resolve = set()
    for o in raw:
        mints_to_resolve.add(o.get("principalMint") or _mint_from_asset(o.get("principal", {})))
        if (o.get("collateral") or {}).get("kind") not in NFT_COLLATERAL_KINDS:
            mints_to_resolve.add(_mint_from_asset(o.get("collateral") or {}))
    resolve_symbols(mints_to_resolve)

    offers = [describe_offer(o) for o in raw]
    offers = [d for d in offers if d["principal_usd"] >= args.min_size]
    offers.sort(key=lambda d: -d["principal_usd"])
    log.info("  → %d open borrow request(s) after --min-size filter", len(offers))

    if not args.no_email:
        previously_notified = load_state()
        new_offers = [d for d in offers if d["pubkey"] not in previously_notified]
        if new_offers:
            log.info("New borrow request%s since last run: %d", "" if len(new_offers) == 1 else "s", len(new_offers))
            new_token, new_nft = split_by_collateral_type(new_offers)
            subject = (
                f"Offerbook: new borrow request — {new_offers[0]['principal_symbol']} "
                f"${new_offers[0]['principal_usd']:,.0f}"
                if len(new_offers) == 1
                else f"Offerbook: {len(new_offers)} new borrow requests"
            )
            body = "\n\n".join([
                format_section("TOKEN-BACKED", new_token),
                format_section("NFT-BACKED", new_nft),
            ])
            send_email(subject, body)
        else:
            log.info("No new borrow requests since last run (nothing to email).")
        save_state({d["pubkey"] for d in offers})

    if not offers:
        log.info("No open borrow requests match the current filters.")
        return

    token_offers, nft_offers = split_by_collateral_type(offers)
    log.info("")
    log.info("=" * 100)
    log.info("OPEN BORROW REQUESTS — %d total, largest first within each group", len(offers))
    log.info("=" * 100)
    for title, items in (("TOKEN-BACKED", token_offers), ("NFT-BACKED", nft_offers)):
        log.info("--- %s (%d) ---", title, len(items))
        for d in items:
            for line in format_offer_line(d).splitlines():
                log.info(line)
    log.info("=" * 100)


if __name__ == "__main__":
    main()
