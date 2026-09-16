"""
Offerbook Address Snapshot
==================================
Quick lookup: given one or more addresses, how much do they currently owe
(or, in --role lender, are currently owed), broken down by counterparty,
plus collateral involved. No chart, no gap analysis — see
borrower_loan_timeline.py for that; this is the fast "how much is X into
the platform for" answer.

Multiple --addresses are combined into a single snapshot rather than
reported separately — handy when the same person/desk controls more than
one wallet and you want their combined exposure, not N separate numbers to
add up yourself.

  --role borrower (default): the given addresses are borrowers. Shows every
    loan they owe, grouped by LENDER — "who do they owe, and how much".
  --role lender: the given addresses are lenders. Shows every loan owed to
    them, grouped by BORROWER — "who owes them, and how much".

Usage:
  python address_snapshot.py --addresses 4nFMipa1LwA6QQiVk29YqZeCvHixbWMMjcBR1h7jDMrZ,Gk4T2iCaJ7JuKzsgnwuBZnBRcpdgHQRizX6zf2gM7eC5
  python address_snapshot.py --addresses 4nFMipa1LwA6QQiVk29YqZeCvHixbWMMjcBR1h7jDMrZ --status all
  python address_snapshot.py --addresses 8pXq...9nZ --role lender
  python address_snapshot.py                                                          # prompts for addresses

Notes:
  - Defaults to currently ACTIVE loans only ("how much do they currently
    owe/are owed" — not a lifetime total). Pass --status all/repaid/defaulted
    to widen scope.
  - "Loans" here means USDC-principal loans, same convention
    borrower_loan_timeline.py uses.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime

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

KNOWN_SYMBOLS = _common.KNOWN_SYMBOLS
_RESOLVED_SYMBOLS: dict[str, str] = {}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("address_snapshot")

SESSION = requests.Session()


def is_valid_pubkey(s: str) -> bool:
    """A Solana pubkey base58-decodes to exactly 32 bytes."""
    try:
        return len(base58.b58decode(s)) == 32
    except Exception:
        return False


def _fetch_all_pages(endpoint: str, params: dict | None = None) -> list[dict]:
    return _common.fetch_all_pages(SESSION, API_BASE, endpoint, params, PAGE_SIZE, sleep_secs=0.1)


def fetch_loans(statuses: list[str]) -> list[dict]:
    """Every USDC-principal loan across the given statuses, platform-wide."""
    loans: list[dict] = []
    for status in statuses:
        items = _fetch_all_pages(f"/loans/status/{status}")
        for l in items:
            pmint = l.get("principalMint") or _common._mint_from_asset(l.get("principal", {}))
            if pmint == USDC_MINT:
                l["_status"] = status
                loans.append(l)
    return loans


def collateral_symbol_for(loan: dict) -> str:
    cmint = loan.get("collateralMint") or _common._mint_from_asset(loan.get("collateral", {}))
    if cmint in KNOWN_SYMBOLS:
        return KNOWN_SYMBOLS[cmint]
    if cmint in _RESOLVED_SYMBOLS:
        return _RESOLVED_SYMBOLS[cmint]
    return cmint[:6] + "…" if cmint else "?"


def resolve_collateral_symbols(loans: list[dict]) -> None:
    """Look up real symbols (via Jupiter's token search API) for every
    collateral mint in `loans` that isn't already in KNOWN_SYMBOLS — same
    approach borrower_loan_timeline.py/update_config.py use, since Jupiter
    indexes far more of these than Offerbook's own registry."""
    mints = {
        loan.get("collateralMint") or _common._mint_from_asset(loan.get("collateral", {}))
        for loan in loans
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


def build_detail_rows(loans: list[dict]) -> list[dict]:
    """Per-loan detail: borrower, lender, amount owed, collateral posted.
    total_owed_usd = principal + the loan's full committed interest —
    interest on this platform is NOT prorated by elapsed time, the whole
    term's interest is due regardless of when (or whether, before default)
    the borrower repays, same convention portfolio_health.py uses."""
    rows = []
    for l in loans:
        meta = l.get("metadata") or {}
        principal_amount = l.get("principalAmount") or 0
        principal_usd = meta.get("startPrincipalAmountUsd") or 0.0
        interest = l.get("interest") or 0
        interest_usd = (interest / principal_amount) * principal_usd if principal_amount else 0.0
        collateral_usd = meta.get("startCollateralAmountUsd") or 0.0
        rows.append({
            "start": datetime.fromisoformat(l["createdAt"].replace("Z", "+00:00")),
            "collateral_symbol": collateral_symbol_for(l),
            "lender": l.get("lender", ""),
            "borrower": l.get("borrower", ""),
            "status": l.get("_status", ""),
            "principal_usd": principal_usd,
            "interest_usd": interest_usd,
            "total_owed_usd": principal_usd + interest_usd,
            "collateral_usd": collateral_usd,
        })
    rows.sort(key=lambda r: r["start"])
    return rows


def print_detail_table(rows: list[dict]) -> None:
    log.info("")
    log.info("Per-loan detail:")
    col = "{:<12}{:<10}{:<46}{:<46}{:<11}{:>12}{:>12}{:>14}{:>16}"
    log.info(col.format(
        "start", "collat", "borrower", "lender", "status", "principal", "interest", "total owed", "collateral $",
    ))
    for r in rows:
        log.info(col.format(
            r["start"].date().isoformat(), r["collateral_symbol"], r["borrower"], r["lender"], r["status"],
            f"${r['principal_usd']:,.2f}", f"${r['interest_usd']:,.2f}",
            f"${r['total_owed_usd']:,.2f}", f"${r['collateral_usd']:,.2f}",
        ))
    log.info(
        "TOTAL — owed: $%.2f   collateral: $%.2f",
        sum(r["total_owed_usd"] for r in rows), sum(r["collateral_usd"] for r in rows),
    )


def build_grouped_summary(rows: list[dict], group_key: str) -> list[dict]:
    """Roll the per-loan detail rows up into one row per `group_key`
    ("lender" or "borrower") — total owed and collateral posted, loan count.
    Sorted by total owed descending, largest exposure first."""
    grouped: dict[str, dict] = {}
    for r in rows:
        agg = grouped.setdefault(r[group_key], {group_key: r[group_key], "loans": 0, "total_owed_usd": 0.0, "total_collateral_usd": 0.0})
        agg["loans"] += 1
        agg["total_owed_usd"] += r["total_owed_usd"]
        agg["total_collateral_usd"] += r["collateral_usd"]
    return sorted(grouped.values(), key=lambda a: -a["total_owed_usd"])


def print_grouped_summary(summary_rows: list[dict], group_key: str) -> None:
    log.info("")
    log.info("Summary by %s:", group_key)
    col = "{:<46}{:>8}{:>16}{:>20}"
    log.info(col.format(group_key, "loans", "total owed", "total collateral $"))
    for a in summary_rows:
        log.info(col.format(
            a[group_key], a["loans"], f"${a['total_owed_usd']:,.2f}", f"${a['total_collateral_usd']:,.2f}",
        ))
    log.info(
        "TOTAL — owed: $%.2f   collateral: $%.2f   across %d %s(s)",
        sum(a["total_owed_usd"] for a in summary_rows), sum(a["total_collateral_usd"] for a in summary_rows),
        len(summary_rows), group_key,
    )


def prompt_for_addresses() -> list[str]:
    raw = input("Enter address(es), comma-separated: ").strip()
    while not raw:
        raw = input("Need at least one address: ").strip()
    return [a.strip() for a in raw.split(",") if a.strip()]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--addresses", default=None, help="Comma-separated address(es) to snapshot. Omit to be prompted.")
    parser.add_argument("--role", default="borrower", choices=["borrower", "lender"],
                         help='Whether the given addresses are "borrower" (default — shows who they owe, '
                              'grouped by lender) or "lender" (shows who owes them, grouped by borrower).')
    parser.add_argument("--status", default="active", choices=["active", "repaid", "defaulted", "all"],
                         help='Loan status to include (default "active" — currently outstanding only).')
    args = parser.parse_args()

    addresses = (
        [a.strip() for a in args.addresses.split(",") if a.strip()]
        if args.addresses else prompt_for_addresses()
    )
    invalid = [a for a in addresses if not is_valid_pubkey(a)]
    if invalid:
        log.error("Not a valid address: %s", ", ".join(invalid))
        sys.exit(1)
    address_set = set(addresses)

    own_field = "borrower" if args.role == "borrower" else "lender"
    group_key = "lender" if args.role == "borrower" else "borrower"

    statuses = ["active", "repaid", "defaulted"] if args.status == "all" else [args.status]
    log.info("Fetching loans (%s) platform-wide…", ", ".join(statuses))
    loans = fetch_loans(statuses)
    relevant = [l for l in loans if l.get(own_field) in address_set]
    if not relevant:
        log.error("No %s loans found for %s (%s).", args.status, args.role, ", ".join(addresses))
        sys.exit(1)

    resolve_collateral_symbols(relevant)
    log.info(
        "Snapshot — %d address(es) as %s, %d %s loan(s): %s",
        len(addresses), args.role, len(relevant), args.status, ", ".join(addresses),
    )

    rows = build_detail_rows(relevant)
    print_detail_table(rows)
    print_grouped_summary(build_grouped_summary(rows, group_key), group_key)


if __name__ == "__main__":
    main()
