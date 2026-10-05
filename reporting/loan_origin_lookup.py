"""
Offerbook Loan Origin Lookup
==================================
Takes one address (borrower or lender, either role — both are checked, no
--role flag needed) and drills into HOW each of their loans came to exist —
not just the loan's own locked-in terms (apy/duration), but the ORIGINATING
OFFER those terms were filled from: who posted it (a `lending` offer from
the lender, or a `borrowing` request from the borrower), when, whether the
posted terms match what the loan actually settled at, whether that standing
offer has ever been filled by anyone else (`fillCounter` is platform-wide,
not scoped to this address), and — when the offer was itself a counter in a
back-and-forth negotiation (`counteredOffer`) — the full chain of prior asks
it was negotiated down/up from.

Companion to apy_opportunity_scan.py's "missed opportunities"/"poach
candidates" lists (and address_snapshot.py/borrower_loan_timeline.py):
once one of those surfaces a borrower paying someone else an eye-catching
APY, this answers "how did they end up on those terms" — did the BORROWER
ask for that rate themselves (posted a `borrowing` offer), or did a LENDER
set it and the borrower just accepted a standing `lending` offer sitting on
the book? That's a very different read on the same number.

There's no GET /offers/:pubkey endpoint, so a loan's offer isn't directly
fetchable by pubkey — it's resolved by pulling the FULL offer history (every
status: active/partiallyFilled/fulfilled/cancelled/expired) of BOTH parties
to the loan, lender and borrower, then matching by pubkey. Both sides are
fetched (not just "whichever isn't --address") because a negotiation chain
can alternate between them — each counter in `counteredOffer` is itself
created by whichever side is countering. Each address's offer history is
fetched once and cached, even if they recur across several of --address's
loans.

Usage:
  python loan_origin_lookup.py --address FRLXeUieHrAnuQHmVqimSjPktg9mbJtADb14aG6sYr8P
  python loan_origin_lookup.py --address <addr> --status all         # include repaid/defaulted, not just active (slower — many more counterparties to resolve offer histories for)
  python loan_origin_lookup.py --address <addr> --status repaid
  python loan_origin_lookup.py --address <addr> --verbose             # add the full per-loan detail block below the table
  python loan_origin_lookup.py                                        # prompts for address

Notes:
  - Defaults to currently ACTIVE loans only, both roles --address is party
    to (as borrower AND as lender), same USDC-principal convention every
    other reporting script here uses.
  - Always prints a one-row-per-loan summary table (created date, role,
    status, collateral, principal $, LTV% at origination, APY, duration,
    counterparty, the originating offer's own status, negotiation hop
    count, loan pubkey) — pass --verbose to additionally print the full
    per-loan detail block (min-fill %/remaining %/allow-extend, and the
    full negotiation chain if one exists) underneath.
  - A negotiation chain is walked as far back as still resolves within the
    two parties' own offer histories; if a hop's `counteredOffer` isn't
    found there (a third party was involved somewhere upstream, or the
    chain reaches further back than either party's own data covers), the
    chain is shown truncated with a note, not silently cut off.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys

import base58
import requests
from dotenv import load_dotenv

load_dotenv()

# Shared modules live in ../lib — see README's repo-layout note.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))

import offerbook_common as _common

API_BASE = os.getenv("OFFERBOOK_API_BASE", "https://api.offerbook.jup.ag/api/v1")
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
PAGE_SIZE = 100

JUPITER_TOKEN_SEARCH_API = "https://api.jup.ag/tokens/v2/search"
JUPITER_SEARCH_BATCH_SIZE = 50

# Every status an offer can ever be in — needed up front since a loan's
# originating offer could now be sitting in any of them (almost always
# "fulfilled" by the time a loan exists from it, but cancelled/expired
# offers can still show up as the `counteredOffer` link in a negotiation
# chain that was never itself filled).
OFFER_ALL_STATUSES = "active,partiallyFilled,fulfilled,cancelled,expired"

KNOWN_SYMBOLS = _common.KNOWN_SYMBOLS
_RESOLVED_SYMBOLS: dict[str, str] = {}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("loan_origin_lookup")

SESSION = requests.Session()

# address -> {offer_pubkey: offer dict}. Populated lazily, once per address,
# the first time any loan needs that address's offer history resolved —
# shared across every loan in the run, not refetched per loan.
_OFFERS_BY_CREATOR: dict[str, dict[str, dict]] = {}


def is_valid_pubkey(s: str) -> bool:
    """A Solana pubkey base58-decodes to exactly 32 bytes."""
    try:
        return len(base58.b58decode(s)) == 32
    except Exception:
        return False


def _fetch_all_pages(endpoint: str, params: dict | None = None) -> list[dict]:
    return _common.fetch_all_pages(SESSION, API_BASE, endpoint, params, PAGE_SIZE, sleep_secs=0.1)


def fetch_loans(statuses: list[str]) -> list[dict]:
    """Every USDC-principal loan across the given statuses, platform-wide —
    same convention every other reporting script in this repo uses."""
    loans: list[dict] = []
    for status in statuses:
        items = _fetch_all_pages(f"/loans/status/{status}")
        for l in items:
            pmint = l.get("principalMint") or _common._mint_from_asset(l.get("principal", {}))
            if pmint == USDC_MINT:
                l["_status"] = status
                loans.append(l)
    return loans


def symbol_for(mint: str | None) -> str:
    if not mint:
        return "NFT"
    if mint in KNOWN_SYMBOLS:
        return KNOWN_SYMBOLS[mint]
    if mint in _RESOLVED_SYMBOLS:
        return _RESOLVED_SYMBOLS[mint]
    return mint[:6] + "…" + mint[-4:]


def resolve_collateral_symbols(loans: list[dict]) -> None:
    """Look up real symbols (via Jupiter's token search API) for every
    collateral mint in `loans` that isn't already in KNOWN_SYMBOLS — same
    approach address_snapshot.py/borrower_loan_timeline.py/update_config.py
    use, since Jupiter indexes far more long-tail/pump.fun tokens than
    Offerbook's own registry."""
    mints = {
        l.get("collateralMint") or _common._mint_from_asset(l.get("collateral", {}))
        for l in loans
    }
    unresolved = sorted(m for m in mints if m and m not in KNOWN_SYMBOLS and m not in _RESOLVED_SYMBOLS)
    if not unresolved:
        return
    for i in range(0, len(unresolved), JUPITER_SEARCH_BATCH_SIZE):
        chunk = unresolved[i : i + JUPITER_SEARCH_BATCH_SIZE]
        try:
            resp = SESSION.get(JUPITER_TOKEN_SEARCH_API, params={"query": ",".join(chunk)}, timeout=15)
            resp.raise_for_status()
            for t in resp.json():
                mint, symbol = t.get("id"), t.get("symbol")
                if mint and symbol:
                    _RESOLVED_SYMBOLS[mint] = symbol
        except Exception as exc:
            log.warning("Jupiter token symbol lookup failed for a batch: %s", exc)


def fetch_offers_by_creator(address: str) -> dict[str, dict]:
    """Every offer `address` has ever created (any status), keyed by pubkey.
    Cached — a counterparty recurring across several of the inspected
    address's loans only gets fetched once per run, success OR failure: a
    counterparty whose offer history 504s (seen in practice for a lender
    with an unusually large history) fails the same way on every retry
    within a run, so caching the empty result avoids hammering the same
    doomed request once per loan and spamming the same warning dozens of
    times. Any loan whose offer lived only in that address's history just
    shows up as "not found" instead of losing every other loan's result."""
    if address in _OFFERS_BY_CREATOR:
        return _OFFERS_BY_CREATOR[address]
    try:
        offers = _fetch_all_pages("/offers", {"creator": address, "status": OFFER_ALL_STATUSES})
    except requests.exceptions.RequestException as exc:
        log.warning("Could not fetch offer history for %s (%s) — its loans' originating offers may show as not found.", address, exc)
        _OFFERS_BY_CREATOR[address] = {}
        return {}
    by_pubkey = {o["pubkey"]: o for o in offers if o.get("pubkey")}
    _OFFERS_BY_CREATOR[address] = by_pubkey
    return by_pubkey


def resolve_offer_pool(loan: dict) -> dict[str, dict]:
    """Combined {pubkey: offer} pool across BOTH parties to `loan` — enough
    to resolve the loan's own originating offer plus walk a negotiation
    chain that alternates between them."""
    pool: dict[str, dict] = {}
    for party in (loan.get("lender"), loan.get("borrower")):
        if party:
            pool.update(fetch_offers_by_creator(party))
    return pool


def walk_negotiation_chain(offer: dict, pool: dict[str, dict]) -> tuple[list[dict], bool]:
    """chain[0] is the offer that actually got filled into the loan;
    chain[-1] is the earliest ask resolvable from `pool`. Returns
    (chain, truncated) — truncated is True if a `counteredOffer` link
    pointed somewhere not in `pool` (a third party upstream, or data this
    run's two-party offer history just doesn't cover)."""
    chain = [offer]
    seen = {offer["pubkey"]}
    current = offer
    while current.get("counteredOffer"):
        prev = pool.get(current["counteredOffer"])
        if not prev or prev["pubkey"] in seen:
            return chain, bool(current["counteredOffer"])
        chain.append(prev)
        seen.add(prev["pubkey"])
        current = prev
    return chain, False


def _fmt_apy_bps(bps) -> str:
    return f"{(bps or 0) / 100:.2f}%"


def _fmt_duration(seconds) -> str:
    if not seconds:
        return "n/a"
    days = seconds / 86400
    return f"{days:.1f}d" if days >= 1 else f"{seconds / 3600:.1f}h"


def _fmt_ts(t: str | None) -> str:
    if not t:
        return "n/a"
    from datetime import datetime
    return datetime.fromisoformat(t.replace("Z", "+00:00")).strftime("%Y-%m-%d %H:%M UTC")


def _fmt_usd(v) -> str:
    return f"${(v or 0):,.2f}"


def build_loan_record(loan: dict, address: str) -> dict:
    """Resolve everything a row (table or verbose) needs for `loan`, once —
    the offer pool / matched offer / negotiation chain are all fetched here
    so both print_table() and print_loan_detail() read off the same
    precomputed data instead of re-walking the chain twice."""
    role = "borrower" if loan.get("borrower") == address else "lender"
    counterparty_field = "lender" if role == "borrower" else "borrower"
    cmint = loan.get("collateralMint") or _common._mint_from_asset(loan.get("collateral", {}))
    meta = loan.get("metadata") or {}
    principal_usd = meta.get("startPrincipalAmountUsd")
    collateral_usd = meta.get("startCollateralAmountUsd")
    ltv_pct = principal_usd / collateral_usd * 100 if principal_usd is not None and collateral_usd else None

    offer_pubkey = loan.get("offer")
    pool = resolve_offer_pool(loan)
    offer = pool.get(offer_pubkey) if offer_pubkey else None
    chain, truncated = walk_negotiation_chain(offer, pool) if offer else ([], False)

    return {
        "loan": loan, "role": role, "counterparty_field": counterparty_field,
        "counterparty": loan.get(counterparty_field, ""), "cmint": cmint,
        "principal_usd": principal_usd, "collateral_usd": collateral_usd, "ltv_pct": ltv_pct,
        "offer_pubkey": offer_pubkey, "offer": offer, "pool": pool, "chain": chain, "truncated": truncated,
    }


def print_table(records: list[dict]) -> None:
    log.info("")
    log.info("=" * 160)
    log.info("LOAN SUMMARY (%d loan(s)) — pass --verbose for full per-loan detail (min-fill %%/remaining %%/allow-extend/negotiation hops)", len(records))
    log.info("=" * 160)
    col = "{:<11}{:<9}{:<10}{:<10}{:>12}{:>8}{:>8}{:>6}  {:<46}{:>16}{:>4}  {:<46}"
    log.info(col.format(
        "created", "role", "status", "collat", "principal $", "LTV%", "APY", "dur", "counterparty", "offer status", "neg", "loan",
    ))
    log.info("-" * 160)
    for r in records:
        loan = r["loan"]
        offer = r["offer"]
        log.info(col.format(
            (loan.get("createdAt") or "")[:10],
            r["role"],
            loan.get("_status", loan.get("status", "?")),
            symbol_for(r["cmint"]),
            f"{r['principal_usd']:,.2f}" if r["principal_usd"] is not None else "n/a",
            f"{r['ltv_pct']:.2f}%" if r["ltv_pct"] is not None else "n/a",
            _fmt_apy_bps(loan.get("apy")),
            _fmt_duration(loan.get("duration")),
            r["counterparty"],
            offer.get("status", "?") if offer else "NOT FOUND",
            str(len(r["chain"]) - 1) if r["chain"] else "-",
            loan.get("pubkey", ""),
        ))
    log.info("=" * 160)


def print_offer_block(record: dict, address: str) -> None:
    loan, offer, pool = record["loan"], record["offer"], record["pool"]
    role_of_creator = "borrower" if offer.get("creator") == loan.get("borrower") else (
        "lender" if offer.get("creator") == loan.get("lender") else "?"
    )
    you_tag = "  <-- YOU" if offer.get("creator") == address else ""
    log.info(
        "  ORIGINATING OFFER  %s   (%s offer, posted by %s %s%s)",
        offer["pubkey"], offer.get("offerType", "?"), role_of_creator, offer.get("creator", ""), you_tag,
    )
    terms_match = (offer.get("apy") == loan.get("apy")) and (offer.get("duration") == loan.get("duration"))
    log.info(
        "    posted APY: %s   posted duration: %s   (matches loan's locked-in terms: %s)",
        _fmt_apy_bps(offer.get("apy")), _fmt_duration(offer.get("duration")), "yes" if terms_match else "NO",
    )
    log.info(
        "    posted: %s   offer last updated: %s   status: %s",
        _fmt_ts(offer.get("createdAt")), _fmt_ts(offer.get("updatedAt")), offer.get("status", "?"),
    )
    principal_amount = offer.get("principalAmount") or 0
    min_fill = offer.get("minFillAmount") or 0
    min_fill_pct = f"{min_fill / principal_amount * 100:.1f}% of offer size" if principal_amount else "n/a"
    log.info(
        "    allow partial fill: %s (min fill: %s)   allow extend: %s",
        "yes" if offer.get("allowPartialFill") else "no", min_fill_pct,
        "yes" if offer.get("allowExtend") else "no",
    )
    fill_index = loan.get("fillIndex")
    fill_index_str = f"fill #{fill_index + 1}" if isinstance(fill_index, int) else "fill #?"
    remaining = offer.get("remainingPrincipal") or 0
    remaining_pct = f"{remaining / principal_amount * 100:.1f}% of offer size still open" if principal_amount else "n/a"
    log.info(
        "    this loan is %s of this standing offer   fill count (platform-wide, all fillers): %d   remaining: %s",
        fill_index_str, offer.get("fillCounter") or 0, remaining_pct,
    )

    chain, truncated = record["chain"], record["truncated"]
    if len(chain) == 1:
        log.info("    negotiation: none (direct fill, no counter-offer chain)")
        return
    log.info("    negotiation chain (%d hop(s), most recent first — this offer countered the one below it, etc.):", len(chain) - 1)
    for i, hop in enumerate(chain):
        marker = "filled offer" if i == 0 else f"counter #{i}"
        log.info(
            "      [%s] %s  creator=%s  apy=%s  duration=%s  posted=%s",
            marker, hop["pubkey"], hop.get("creator", ""), _fmt_apy_bps(hop.get("apy")),
            _fmt_duration(hop.get("duration")), _fmt_ts(hop.get("createdAt")),
        )
    if truncated:
        log.info("      ... chain continues further back, but the next counter isn't in either party's resolvable offer history")


def print_loan_detail(record: dict, address: str) -> None:
    loan = record["loan"]
    log.info("=" * 100)
    log.info(
        "LOAN  %s   [as %s]   status: %s   type: %s",
        loan.get("pubkey", ""), record["role"], loan.get("_status", loan.get("status", "?")), loan.get("loanType", "?"),
    )
    log.info("=" * 100)
    log.info("  counterparty (%s): %s", record["counterparty_field"], record["counterparty"])
    log.info(
        "  collateral: %-10s (contract: %s)",
        symbol_for(record["cmint"]), record["cmint"] or "n/a (NFT — see collateral.asset)",
    )
    log.info(
        "  principal: %s   loan APY: %s   duration: %s   created: %s   expires: %s",
        _fmt_usd(record["principal_usd"]), _fmt_apy_bps(loan.get("apy")),
        _fmt_duration(loan.get("duration")), _fmt_ts(loan.get("createdAt")), _fmt_ts(loan.get("expiredAt")),
    )
    ltv_str = f"{record['ltv_pct']:.2f}%" if record["ltv_pct"] is not None else "n/a"
    log.info(
        "  LTV at origination: %s   (collateral posted: %s)",
        ltv_str, _fmt_usd(record["collateral_usd"]),
    )
    log.info("")

    if record["offer"]:
        print_offer_block(record, address)
    else:
        log.info(
            "  ORIGINATING OFFER %s: not found — outside what's resolvable from either party's own offer history.",
            record["offer_pubkey"] or "(none recorded on this loan)",
        )
    log.info("")


def prompt_for_address() -> str:
    raw = input("Enter the address to investigate: ").strip()
    while not raw:
        raw = input("Address can't be blank — enter the address: ").strip()
    return raw


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--address", default=None, help="Wallet address to investigate (as borrower and/or lender). Omit to be prompted.")
    parser.add_argument("--status", default="active", choices=["active", "repaid", "defaulted", "all"],
                         help='Loan status to include (default "active"). Widening scope means many more '
                              'counterparties to resolve offer histories for, so "all" is noticeably slower.')
    parser.add_argument("--verbose", action="store_true",
                         help="Print the full per-loan detail block (min-fill %%/remaining %%/allow-extend/full "
                              "negotiation chain) for every loan, in addition to the summary table.")
    args = parser.parse_args()

    address = args.address or prompt_for_address()
    if not is_valid_pubkey(address):
        log.error("Not a valid address: %s", address)
        sys.exit(1)

    statuses = ["active", "repaid", "defaulted"] if args.status == "all" else [args.status]
    log.info("Fetching loans (%s) platform-wide…", ", ".join(statuses))
    all_loans = fetch_loans(statuses)
    loans = [l for l in all_loans if l.get("borrower") == address or l.get("lender") == address]
    if not loans:
        log.error("No %s loans found where %s is borrower or lender.", args.status, address)
        sys.exit(1)
    loans.sort(key=lambda l: l.get("createdAt", ""))

    resolve_collateral_symbols(loans)

    as_borrower = [l for l in loans if l.get("borrower") == address]
    as_lender = [l for l in loans if l.get("lender") == address]
    log.info(
        "Found %d loan(s) for %s — %d as borrower, %d as lender. Resolving originating offers…",
        len(loans), address, len(as_borrower), len(as_lender),
    )

    records = [build_loan_record(l, address) for l in loans]
    print_table(records)
    if args.verbose:
        log.info("")
        for r in records:
            print_loan_detail(r, address)


if __name__ == "__main__":
    main()
