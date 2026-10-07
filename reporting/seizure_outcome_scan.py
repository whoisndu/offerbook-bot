"""
Offerbook Seizure Outcome Scan
==================================
`pnl_leaderboard.py` values every defaulted loan's collateral at a single
mark-to-market snapshot (metadata.endCollateralAmountUsd, priced the instant
default was recorded) — a fiction the moment the lender doesn't sell right
then. "Should a lender be holding seized collateral instead of liquidating
it immediately" can only be answered against what actually happened to it
on-chain afterward, not that snapshot. The Offerbook API has no concept of
"what did you do with it after" — only Solana itself does.

This takes the platform's top N most profitable lenders (same realized-PNL
formula pnl_leaderboard.py uses: repaid interest net of fee + defaulted
collateral mark-to-market + rollover interest net of fee — computed fresh
here rather than imported, to keep this script runnable standalone like
every other one in this repo) and, for EACH of their defaulted loans, walks
Solana directly to classify what actually happened to the seized collateral:

  - SOLD: every transaction since default that reduced that lender's balance
    of the collateral mint, paired with whatever asset they received in the
    same transaction (USDC priced at exactly $1, never drifts; SOL or any
    other received token priced at today's live rate as a proxy — flagged,
    since that's not necessarily the price at the moment of that specific
    sale).
  - HOLDING: current live balance of that mint still sitting in the
    lender's wallet, priced at today's live rate.
  - PARTIAL: both of the above.
  - Δ vs offerbook = (realized + current value) − the mark-to-market
    snapshot. Negative means they'd have done better selling immediately;
    positive means holding (so far) paid off.

SHARED-TOKEN-ACCOUNT WINDOWING — the one subtlety that actually matters:
a lender who's defaulted on the SAME collateral token more than once (not
rare among the top lenders — the same mint keeps showing up as lenders
specialize) ends up with ALL of those seizures landing in the SAME
Associated Token Account, since an ATA is keyed by (owner, mint), not by
loan. Tracing each default's history with an open-ended "since default, to
now" window double-counts: an EARLIER default's trace would also sweep up a
LATER default's own sale of its own (separately seized) tokens, inflating
the earlier default's "realized" figure by however much the later one sold.
(Caught exactly this during development — a July default's trace showed
$20,473 realized when the on-chain swap, confirmed on Solscan, was actually
$13,510; a September default sharing the same ATA had sold $6,962 that
bled into the July trace's unbounded scan.) The fix: group loans by
(lender, collateral mint), sort chronologically, and bound each one's scan
window to [this default's time, the NEXT default's time in that same group,
or now if it's the most recent]. Only the most recent loan in a group gets
a true "current live balance" figure — an earlier group member's unsold
remainder (if any) is, by now, commingled with a later default's own seized
tokens and can't be meaningfully priced as "this specific default's
leftovers" anymore; it's shown as ROLLED FORWARD instead, with the raw
unsold unit count noted but not double-valued.

Associated Token Account derivation (not wallet-level scanning): rather
than scanning a lender's WHOLE transaction history (slow and noisy on an
active trading wallet — thousands of unrelated transactions between a
default and today), this derives the collateral mint's own ATA (the
standard SPL/Token-2022 PDA, picking the right token program from the
loan's own collateralTokenProgram field) and scans ONLY that account's
history — orders of magnitude fewer transactions, and more complete (a
wallet-level scan capped at a few thousand signatures can miss older
activity on a busy wallet entirely; an ATA-level scan doesn't have that
problem since it's scoped to just the one token).

This is a genuinely heavy scan — potentially 100+ defaulted loans across
the top N lenders, several Solana RPC calls each, against the free public
mainnet-beta endpoint by default (set SOLANA_RPC to a paid endpoint for a
large speedup). Expect minutes, not seconds — this is a "run it and get
coffee" report, not something to loop on. --top/--min-seizure-usd narrow
the scope if a full run is more than you need.

RESUMABLE / CACHED: every lot's trace result (the expensive part — walking
its Associated Token Account's full transaction history) is persisted to
seizure_outcome_scan_state.json as soon as it's computed, keyed by the
exact inputs that produced it (lender, mint, its own since_ts/net_seized/
expected-arrival-count, and the next lot's since_ts/net_seized if any). If
the network drops mid-run, whatever finished is already saved — just rerun
the same command and it picks up where it left off, re-tracing only the
lots that never finished (a lot whose cache key changed since — e.g. a
newer default altered what counts as "the next lot" — is correctly treated
as a cache miss and retraced, not silently reused). Resolved on-chain
decimals for long-tail mints are cached the same way. A single lot's
failure (after its own internal RPC retries are exhausted) is logged and
skipped rather than crashing the whole run, so one bad account doesn't
cost you every lot after it — just rerun to pick up what's left. The ONLY
thing never cached is each lot's current live balance/price (for HOLDING/
PARTIAL rows) — those are re-fetched fresh every run since "what's it
worth right now" is only meaningful as of the actual run time.

Every run also saves the full result table to an Excel file (real numbers
and dates, not pre-formatted strings, so Excel's own sort/filter/formulas
work on it directly) — ~/Desktop/seizure_outcomes_<timestamp>.xlsx by
default, --output to pick a different path, --no-excel to skip it.

Usage:
  python seizure_outcome_scan.py                           # top 20 lenders by realized PNL
  python seizure_outcome_scan.py --top 10
  python seizure_outcome_scan.py --min-seizure-usd 50       # skip dust seizures (default: $10)
  python seizure_outcome_scan.py --lender <address>         # trace just one lender, any PNL rank
  python seizure_outcome_scan.py --output ~/Downloads/seizures.xlsx
  python seizure_outcome_scan.py --no-excel                # console output only

Read-only: never signs or submits anything.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import requests
from dotenv import load_dotenv
from openpyxl import Workbook
from openpyxl.styles import Font
from openpyxl.utils import get_column_letter
from solders.pubkey import Pubkey

# Shared modules live in ../lib — see README's repo-layout note.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))

import offerbook_common as _common

load_dotenv()

API_BASE = os.getenv("OFFERBOOK_API_BASE", "https://api.offerbook.jup.ag/api/v1")
PAGE_SIZE = 100

SOLANA_RPC = os.getenv("SOLANA_RPC", "https://api.mainnet-beta.solana.com")
JUPITER_PRICE_API = "https://api.jup.ag/price/v3"
JUPITER_API_KEY = os.getenv("JUPITER_API_KEY", "")
DEXSCREENER_API = "https://api.dexscreener.com/latest/dex/tokens"

USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
SOL_MINT = "So11111111111111111111111111111111111111112"
TOKEN_PROGRAM_ID = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
ASSOCIATED_TOKEN_PROGRAM_ID = Pubkey.from_string("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL")

KNOWN_DECIMALS = _common.KNOWN_DECIMALS
KNOWN_SYMBOLS = _common.KNOWN_SYMBOLS

TOP_DEFAULT = 20
MIN_SEIZURE_USD_DEFAULT = 10.0  # skip dust seizures not worth the RPC cost to trace

# A current/remaining balance at/below this fraction of what was originally
# seized counts as "fully sold" (dust from rounding/fees); at/above this
# fraction of the original counts as "never sold" — see classify_status().
DUST_FRACTION = 0.0005

SIGNATURE_PAGE_SIZE = 1000
SIGNATURE_PAGE_CAP = 5  # x1000 = 5000 sigs ceiling per token account before giving up

# Gitignored (like lender_capital_state.json) — caches the expensive part of
# each lot's trace (its on-chain history walk) so a network drop mid-run, or
# just wanting to re-run with a different --top/--min-seizure-usd, doesn't
# mean re-fetching every signature/transaction from scratch. See
# make_trace_cache_key() for why a stale entry is never silently reused.
STATE_PATH = Path(__file__).parent / "seizure_outcome_scan_state.json"

DESKTOP_DIR = Path.home() / "Desktop"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("seizure_outcome_scan")

SESSION = requests.Session()


def _fetch_all_pages(endpoint: str, params: dict | None = None) -> list[dict]:
    return _common.fetch_all_pages(SESSION, API_BASE, endpoint, params, PAGE_SIZE, sleep_secs=0.1)


def symbol_for(mint: str | None) -> str:
    if not mint:
        return "NFT"
    return KNOWN_SYMBOLS.get(mint, f"{mint[:6]}…{mint[-4:]}")


# ---------------------------------------------------------------------------
# Resumable state (trace cache + resolved decimals) — see module docstring
# ---------------------------------------------------------------------------

def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("Couldn't read %s (%s) — starting with an empty cache.", STATE_PATH, exc)
    return {"traces": {}, "decimals": {}}


def save_state(state: dict) -> None:
    """Called after every single lot (and every newly-resolved decimals
    lookup), not just at the end of the run — so whatever's finished is
    always safely on disk even if the network dies on the very next one."""
    try:
        STATE_PATH.write_text(json.dumps(state, indent=2, sort_keys=True))
    except OSError as exc:
        log.warning("Couldn't write %s (%s) — progress this run won't be cached for next time.", STATE_PATH, exc)


def make_trace_cache_key(
    lender: str, mint: str, since_ts: int, net_seized: float, expected_arrivals: int,
    next_lot_since_ts: int | None, next_lot_net_seized: float | None,
) -> str:
    """Deterministic key covering every input trace_collateral_outcome()
    actually depends on. If any of these change between runs — a newer
    default alters what counts as "the next lot", a merge pulls in one more
    loan, etc. — the key changes too, so a stale cache entry is a guaranteed
    miss (retraced fresh) rather than something that needs explicit
    invalidation logic."""
    next_part = f"{next_lot_since_ts}:{next_lot_net_seized:.6f}" if next_lot_since_ts is not None else "none"
    return f"{lender}:{mint}:{since_ts}:{net_seized:.6f}:{expected_arrivals}:{next_part}"

# ---------------------------------------------------------------------------
# Realized-PNL ranking (same formula pnl_leaderboard.py uses) — just enough
# to pick the top N lenders; no volume/rollover-volume/cycles bookkeeping,
# since nothing downstream here needs it.
# ---------------------------------------------------------------------------

def compute_pnl_ranking() -> tuple[list[tuple[str, float]], list[dict]]:
    """Returns (ranked [(lender, realized_pnl_usd), ...] desc, all_defaulted_loans).
    all_defaulted_loans is returned too so the caller doesn't need a second
    /loans/status/defaulted fetch to find the top lenders' own loans."""
    pnl: dict[str, float] = defaultdict(float)

    log.info("Fetching all repaid loans platform-wide …")
    repaid = _fetch_all_pages("/loans/status/repaid")
    log.info("  → %d repaid loan(s)", len(repaid))
    for l in repaid:
        lender = l.get("lender")
        principal_amount = l.get("principalAmount") or 0
        if not lender or principal_amount == 0:
            continue
        md = l.get("metadata") or {}
        start_principal_usd = md.get("startPrincipalAmountUsd") or 0.0
        interest = l.get("interest") or 0
        interest_usd_gross = (interest / principal_amount) * start_principal_usd
        repay_fee_usd = ((md.get("fees") or {}).get("repay") or {}).get("amountUsd") or 0.0
        pnl[lender] += interest_usd_gross - repay_fee_usd

    log.info("Fetching all defaulted loans platform-wide …")
    defaulted = _fetch_all_pages("/loans/status/defaulted")
    log.info("  → %d defaulted loan(s)", len(defaulted))
    for l in defaulted:
        lender = l.get("lender")
        if not lender:
            continue
        md = l.get("metadata") or {}
        start_principal_usd = md.get("startPrincipalAmountUsd") or 0.0
        end_collateral_usd = md.get("endCollateralAmountUsd")
        if end_collateral_usd is None:
            end_collateral_usd = md.get("startCollateralAmountUsd") or 0.0
        pnl[lender] += end_collateral_usd - start_principal_usd

    log.info("Fetching all active loans platform-wide (for rollover interest) …")
    active = _fetch_all_pages("/loans/status/active")
    log.info("  → %d active loan(s)", len(active))
    for l in repaid + defaulted + active:
        lender = l.get("lender")
        if not lender:
            continue
        for ext in (l.get("metadata") or {}).get("extensions") or []:
            interest_usd = ext.get("interestPaidUsd") or 0.0
            fee_usd = ((ext.get("repayFee") or {}).get("amountUsd")) or 0.0
            pnl[lender] += interest_usd - fee_usd

    ranked = sorted(pnl.items(), key=lambda kv: kv[1], reverse=True)
    return ranked, defaulted

# ---------------------------------------------------------------------------
# Solana RPC helpers
# ---------------------------------------------------------------------------

SOLANA_RPC_MAX_RETRIES = 6


def _solana_rpc(payload: dict) -> dict:
    """POST to SOLANA_RPC with retry/backoff on 429 — same approach
    portfolio_health.py uses (the free public mainnet-beta endpoint
    rate-limits aggressively)."""
    delay = 1.0
    for attempt in range(SOLANA_RPC_MAX_RETRIES):
        resp = requests.post(SOLANA_RPC, json=payload, timeout=30)
        if resp.status_code == 429 and attempt < SOLANA_RPC_MAX_RETRIES - 1:
            retry_after = resp.headers.get("Retry-After")
            wait = float(retry_after) if retry_after else delay
            log.warning("Solana RPC rate-limited (429) — retrying in %.1fs (attempt %d/%d) …", wait, attempt + 1, SOLANA_RPC_MAX_RETRIES)
            time.sleep(wait)
            delay *= 2
            continue
        resp.raise_for_status()
        data = resp.json()
        if "error" in data:
            raise RuntimeError(data["error"])
        return data["result"]
    raise AssertionError("unreachable")  # loop always returns or raises above


def derive_ata(owner: str, mint: str, token_program: str) -> str:
    """Deterministically derive the standard SPL Associated Token Account
    address for (owner, mint, token_program) — the same PDA derivation the
    on-chain Associated Token Account Program itself uses."""
    owner_pk, mint_pk, program_pk = Pubkey.from_string(owner), Pubkey.from_string(mint), Pubkey.from_string(token_program)
    ata, _bump = Pubkey.find_program_address([bytes(owner_pk), bytes(program_pk), bytes(mint_pk)], ASSOCIATED_TOKEN_PROGRAM_ID)
    return str(ata)


def fetch_current_token_balance_raw(owner: str, mint: str) -> int:
    result = _solana_rpc({
        "jsonrpc": "2.0", "id": 1, "method": "getTokenAccountsByOwner",
        "params": [owner, {"mint": mint}, {"encoding": "jsonParsed"}],
    })
    return sum(
        int(a.get("account", {}).get("data", {}).get("parsed", {}).get("info", {})
             .get("tokenAmount", {}).get("amount", "0"))
        for a in result.get("value", [])
    )


def fetch_mint_decimals_onchain(mint: str) -> int | None:
    try:
        result = _solana_rpc({
            "jsonrpc": "2.0", "id": 1, "method": "getAccountInfo",
            "params": [mint, {"encoding": "jsonParsed"}],
        })
        value = (result or {}).get("value")
        if value:
            return value["data"]["parsed"]["info"]["decimals"]
    except Exception as exc:
        log.warning("Couldn't fetch decimals on-chain for %s…: %s", mint[:8], exc)
    return None


def fetch_live_prices(mints: list[str]) -> dict[str, float]:
    """{mint: usd_price_per_whole_token} — Jupiter batch first, DexScreener
    per-mint fallback, same approach portfolio_health.py uses."""
    mints = [m for m in dict.fromkeys(mints) if m]
    if not mints:
        return {}
    prices: dict[str, float] = {}
    try:
        headers = {"x-api-key": JUPITER_API_KEY} if JUPITER_API_KEY else {}
        resp = SESSION.get(JUPITER_PRICE_API, params={"ids": ",".join(mints)}, headers=headers, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        prices = {mint: float(info["usdPrice"]) for mint, info in data.items() if info.get("usdPrice")}
    except Exception as exc:
        log.warning("Jupiter batch price fetch failed: %s", exc)
    for mint in mints:
        if mint in prices:
            continue
        try:
            resp = SESSION.get(f"{DEXSCREENER_API}/{mint}", timeout=10)
            if not resp.ok:
                continue
            pairs = [p for p in (resp.json().get("pairs") or []) if p.get("chainId") == "solana" and p.get("priceUsd")]
            if not pairs:
                continue
            pairs.sort(key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0), reverse=True)
            prices[mint] = float(pairs[0]["priceUsd"])
        except Exception:
            continue
    return prices


def fetch_signatures_window(address: str, since_ts: int, until_ts: int | None) -> list[dict]:
    """Every signature touching `address` with since_ts <= blockTime <
    until_ts (until_ts=None means open-ended / up to now), paginated newest
    first via the `before` cursor. Pagination always walks back from the
    absolute newest signature regardless of until_ts — only the final
    filter is two-sided — since `before` has no way to start mid-history;
    stops once a page's oldest blockTime < since_ts, same as before."""
    all_sigs: list[dict] = []
    before = None
    for _ in range(SIGNATURE_PAGE_CAP):
        params: dict = {"limit": SIGNATURE_PAGE_SIZE}
        if before:
            params["before"] = before
        batch = _solana_rpc({"jsonrpc": "2.0", "id": 1, "method": "getSignaturesForAddress", "params": [address, params]})
        if not batch:
            break
        all_sigs.extend(batch)
        before = batch[-1]["signature"]
        oldest_ts = batch[-1].get("blockTime")
        if oldest_ts is not None and oldest_ts < since_ts:
            break
    return [
        s for s in all_sigs
        if not s.get("err") and (s.get("blockTime") or 0) >= since_ts and (until_ts is None or (s.get("blockTime") or 0) < until_ts)
    ]


def fetch_transaction(signature: str) -> dict | None:
    return _solana_rpc({
        "jsonrpc": "2.0", "id": 1, "method": "getTransaction",
        "params": [signature, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 1}],
    })


def _token_balance(balances: list[dict], mint: str, owner: str) -> float | None:
    for b in balances:
        if b.get("mint") == mint and b.get("owner") == owner:
            amt = b.get("uiTokenAmount") or {}
            return amt.get("uiAmount", 0.0) if amt.get("uiAmountString") is None else float(amt["uiAmountString"])
    return None


def _sol_balance(tx: dict, owner: str) -> tuple[float | None, float | None]:
    try:
        accounts = tx["transaction"]["message"]["accountKeys"]
        idx = next(i for i, a in enumerate(accounts) if a.get("pubkey") == owner)
        meta = tx["meta"]
        return meta["preBalances"][idx] / 1e9, meta["postBalances"][idx] / 1e9
    except (KeyError, IndexError, StopIteration, TypeError):
        return None, None

# ---------------------------------------------------------------------------
# Per-lot trace — chronological, contamination-aware (see module docstring:
# TWO distinct failure modes found during development, both fixed here)
# ---------------------------------------------------------------------------

MERGE_GAP_SECONDS = 3600  # see merge_near_simultaneous_loans() below

CONTAMINATION_MATCH_SECONDS = 300   # a receipt within this long of the next lot's own
                                       # since_ts, and within CONTAMINATION_MATCH_FRACTION
                                       # of its net_seized, is that lot's own expected
                                       # arrival ("rolled_forward"), not contamination
CONTAMINATION_MATCH_FRACTION = 0.02
# Known edge case: if the NEXT lot is itself a merge of 2+ near-simultaneous
# loans, its own arrival lands as several separate receipts too, and this
# check only matches against its FULL combined net_seized — so the first of
# those separate receipts can be misclassified "contaminated" rather than
# "rolled_forward". Safe failure direction (excludes the ambiguous remainder
# from totals rather than mis-crediting it), just more conservative than
# ideal; hasn't been worth generalizing further given how rare 3+ chained
# near-simultaneous defaults on one token account are in practice.


def trace_collateral_outcome(
    lender: str, mint: str, token_program: str, since_ts: int, net_seized: float, expected_arrivals: int,
    next_lot_since_ts: int | None, next_lot_net_seized: float | None,
) -> dict:
    """Walks every transaction touching the (lender, mint) Associated Token
    Account from `since_ts` (this lot's own seizure) forward, IN
    CHRONOLOGICAL ORDER, and tallies what happened:

      sold_amount       - units of `mint` sold, up to the point the trace
                           stops (see trace_status below)
      sold_usd_exact    - sale proceeds received as USDC (exact, $1 always)
      live_priced_legs  - [(mint, amount received)] for SOL/other-token
                           proceeds, priced later at today's rate
      last_sale_ts      - blockTime of the most recent counted sale
      remaining_at_stop - net_seized minus sold_amount at the point tracing
                           stopped (0 if fully sold)
      trace_status      - "clean" (reached the end of available history, or
                           the account hit zero, with no ambiguity) /
                           "rolled_forward" (a LATER receipt matched the
                           next lot in this group arriving — expected, not
                           suspicious) / "contaminated" (a LATER receipt
                           did NOT match any known next lot — an unrelated
                           deposit, e.g. the lender independently trading
                           the same token — everything from that point on
                           is unattributable and excluded)

    The account's first `expected_arrivals` receipts are always this lot's
    OWN seizure(s) landing (expected, not contamination) — a merged lot
    (see merge_near_simultaneous_loans) combines several original loans
    whose tokens each arrive in their OWN separate on-chain transaction, so
    this must be the merged loan count, not always 1, or a merged lot's
    own 2nd/3rd arrival gets mistaken for contamination. Only a receipt
    AFTER all `expected_arrivals` have landed ever triggers rolled_forward/
    contaminated classification — and even then, only if it's bigger than
    DUST_FRACTION of net_seized (confirmed against a real case: a lender's
    USELESS seizure got a ~$1.60 inflow between two of its own legitimate
    sales, almost certainly a referral-fee rebate — without this threshold
    that single dust receipt would have wrongly voided $35,874 of sales
    later confirmed correct on Solscan). A receipt that arrives only after
    this lot's own tokens are already fully sold doesn't taint anything (nothing
    left to be ambiguous about) — trace_status stays 'clean' and tracing
    simply stops there."""
    ata = derive_ata(lender, mint, token_program)
    # Fetch bound is just an efficiency hint (stop well past wherever a
    # rolled_forward match would land) — correctness comes from the
    # chronological contamination check below, not from this bound.
    fetch_until = (next_lot_since_ts + 86400) if next_lot_since_ts is not None else None
    sigs = sorted(fetch_signatures_window(ata, since_ts, fetch_until), key=lambda s: s.get("blockTime") or 0)

    sold_amount = 0.0
    sold_usd_exact = 0.0
    live_priced_legs: list[tuple[str, float]] = []
    last_sale_ts: int | None = None
    arrivals_seen = 0
    trace_status = "clean"

    for s in sigs:
        tx = fetch_transaction(s["signature"])
        time.sleep(0.1)  # be polite to the public RPC endpoint
        if not tx:
            continue
        meta = tx.get("meta") or {}
        pre_bals, post_bals = meta.get("preTokenBalances") or [], meta.get("postTokenBalances") or []
        pre_amt = _token_balance(pre_bals, mint, lender) or 0.0
        post_amt = _token_balance(post_bals, mint, lender) or 0.0
        delta = pre_amt - post_amt  # positive = sold, negative = received
        bt = s.get("blockTime") or 0

        if delta < 0:  # a receipt
            if arrivals_seen < expected_arrivals:
                arrivals_seen += 1  # this lot's own seizure landing — expected
                continue
            received_here = -delta
            if received_here <= net_seized * DUST_FRACTION:
                continue  # negligible (e.g. a referral-fee rebate) — ignore, not contamination
            # An unexpected, non-trivial receipt. Harmless if this lot is
            # already fully sold — there's nothing left to be ambiguous about.
            if (net_seized - sold_amount) <= net_seized * DUST_FRACTION:
                break  # trace_status stays "clean"
            if next_lot_since_ts is not None and next_lot_net_seized:
                time_close = abs(bt - next_lot_since_ts) <= CONTAMINATION_MATCH_SECONDS
                amount_close = abs(received_here - next_lot_net_seized) <= next_lot_net_seized * CONTAMINATION_MATCH_FRACTION
                if time_close and amount_close:
                    trace_status = "rolled_forward"
                    break
            trace_status = "contaminated"
            break

        if delta <= 0:
            continue  # delta == 0, nothing happened to this mint
        # A sale.
        sold_amount += delta
        last_sale_ts = bt
        other_mints = {b.get("mint") for b in pre_bals + post_bals if b.get("owner") == lender and b.get("mint") != mint}
        for other_mint in other_mints:
            pre_o = _token_balance(pre_bals, other_mint, lender) or 0.0
            post_o = _token_balance(post_bals, other_mint, lender) or 0.0
            received = post_o - pre_o
            if received <= 0:
                continue
            if other_mint == USDC_MINT:
                sold_usd_exact += received
            else:
                live_priced_legs.append((other_mint, received))
        pre_sol, post_sol = _sol_balance(tx, lender)
        if pre_sol is not None and post_sol is not None:
            sol_received = post_sol - pre_sol
            if sol_received > 0.0001:
                live_priced_legs.append((SOL_MINT, sol_received))

    return {
        "sold_amount": sold_amount, "sold_usd_exact": sold_usd_exact,
        "live_priced_legs": live_priced_legs, "last_sale_ts": last_sale_ts,
        "trace_status": trace_status, "remaining_at_stop": max(net_seized - sold_amount, 0.0),
    }


def merge_near_simultaneous_loans(loans: list[dict], decimals: int) -> list[dict]:
    """Consecutive loans in the SAME (lender, mint) group whose defaults
    landed within MERGE_GAP_SECONDS of each other get combined into one
    "lot" (summed net_seized/offerbook_value_usd, loans list kept for
    display). See module docstring: several near-simultaneous defaults'
    seized tokens land in the same Associated Token Account before any sale
    is physically possible, and a later combined sale can't be split back
    apart per-loan — merging first means the trace sees ONE lot with the
    combined total, which (unlike guessing a split) is always correct, just
    coarser-grained than per-loan would ideally be."""
    lots = []
    for l in loans:
        ts = int(datetime.fromisoformat(l["updatedAt"].replace("Z", "+00:00")).timestamp())
        md = l.get("metadata") or {}
        offerbook_value_usd = md.get("endCollateralAmountUsd")
        if offerbook_value_usd is None:
            offerbook_value_usd = md.get("startCollateralAmountUsd") or 0.0
        liquidation_fee_raw = (((md.get("fees") or {}).get("liquidation") or {}).get("amount")) or 0
        net_seized = ((l.get("collateralAmount") or 0) - liquidation_fee_raw) / 10 ** decimals

        if lots and ts - lots[-1]["since_ts"] <= MERGE_GAP_SECONDS:
            lots[-1]["loans"].append(l)
            lots[-1]["net_seized"] += net_seized
            lots[-1]["offerbook_value_usd"] += offerbook_value_usd
        else:
            lots.append({"loans": [l], "since_ts": ts, "net_seized": net_seized, "offerbook_value_usd": offerbook_value_usd})
    return lots


def classify_status(trace_status: str, sold_amount: float, remaining: float, net_seized: float, is_latest_in_group: bool) -> str:
    """'sold' / 'holding' / 'partial' / 'rolled_forward' / 'contaminated'."""
    if net_seized <= 0:
        return "n/a"
    if trace_status == "contaminated":
        return "contaminated"
    if trace_status == "rolled_forward":
        return "rolled_forward"
    # trace_status == "clean"
    if remaining <= net_seized * DUST_FRACTION:
        return "sold"
    if not is_latest_in_group:
        # Reached the end of available history (or the fetch bound) without
        # ever seeing the next lot's own arrival — conservatively treat the
        # same as rolled_forward rather than guessing a "holding" value for
        # tokens that are, structurally, about to be joined by more.
        return "rolled_forward"
    if sold_amount <= net_seized * DUST_FRACTION:
        return "holding"
    return "partial"


def analyze_seizures(defaulted_loans: list[dict], min_seizure_usd: float, use_cache: bool = True) -> list[dict]:
    """One row per LOT in `defaulted_loans` — a lot is one or more loans
    merged together (see merge_near_simultaneous_loans). Groups by (lender,
    collateral mint), since that's what shares an Associated Token Account;
    each lot's on-chain scan is a chronological, contamination-aware walk
    (see trace_collateral_outcome) rather than a naive bounded window — the
    two failure modes found during development (near-simultaneous defaults
    sharing one sale; a lender independently trading the same token later)
    are both handled there, not by guessing a split here."""
    state = load_state()
    traces_cache: dict[str, dict] = state.setdefault("traces", {})
    decimals_cache: dict[str, int] = state.setdefault("decimals", {})

    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    skipped = []
    for l in defaulted_loans:
        cmint = l.get("collateralMint")
        if not cmint:
            skipped.append({"loan": l, "status": "nft"})
            continue
        if not l.get("updatedAt"):
            skipped.append({"loan": l, "status": "n/a"})
            continue
        groups[(l["lender"], cmint)].append(l)

    rows: list[dict] = []
    rows.extend(skipped)

    for (lender, cmint), loans in groups.items():
        decimals = KNOWN_DECIMALS.get(cmint) or decimals_cache.get(cmint)
        if decimals is None:
            decimals = fetch_mint_decimals_onchain(cmint)
            if decimals is not None:
                decimals_cache[cmint] = decimals
                save_state(state)
        if decimals is None:
            log.warning("No decimals resolvable for %s… — skipping %d loan(s) for lender %s…", cmint[:8], len(loans), lender[:8])
            for l in loans:
                rows.append({"loan": l, "status": "unresolvable"})
            continue

        loans.sort(key=lambda l: l["updatedAt"])
        lots = merge_near_simultaneous_loans(loans, decimals)
        token_program = loans[0].get("collateralTokenProgram") or TOKEN_PROGRAM_ID

        for i, lot in enumerate(lots):
            offerbook_value_usd = lot["offerbook_value_usd"]
            net_seized = lot["net_seized"]
            if offerbook_value_usd < min_seizure_usd:
                rows.append({"loan": lot["loans"][0], "loans": lot["loans"], "status": "skipped_dust", "cmint": cmint, "offerbook_value_usd": offerbook_value_usd})
                continue

            is_latest = i == len(lots) - 1
            next_lot = lots[i + 1] if not is_latest else None
            next_since_ts = next_lot["since_ts"] if next_lot else None
            next_net_seized = next_lot["net_seized"] if next_lot else None
            merge_note = f" ({len(lot['loans'])} loans merged)" if len(lot["loans"]) > 1 else ""
            lot_label = f"{symbol_for(cmint)}… seizure for lot {lot['loans'][0].get('pubkey', '')[:8]}{merge_note} (lender {lender[:8]}…, {'latest' if is_latest else 'superseded'})"

            try:
                cache_key = make_trace_cache_key(lender, cmint, lot["since_ts"], net_seized, len(lot["loans"]), next_since_ts, next_net_seized)
                cached = traces_cache.get(cache_key) if use_cache else None
                if cached is not None:
                    log.info("  using cached trace for %s …", lot_label)
                    trace = cached
                else:
                    log.info("  tracing %s …", lot_label)
                    trace = trace_collateral_outcome(
                        lender, cmint, token_program, lot["since_ts"], net_seized, len(lot["loans"]),
                        next_since_ts, next_net_seized,
                    )
                    traces_cache[cache_key] = trace
                    save_state(state)

                sold_amount = trace["sold_amount"]
                trace_status = trace["trace_status"]
                fully_resolved = trace_status == "clean" and trace["remaining_at_stop"] <= net_seized * DUST_FRACTION
                current_balance = fetch_current_token_balance_raw(lender, cmint) / 10 ** decimals if (is_latest and trace_status == "clean") else None
                unsold_remainder = None if (fully_resolved or current_balance is not None) else trace["remaining_at_stop"]
                status = classify_status(trace_status, sold_amount, current_balance if current_balance is not None else trace["remaining_at_stop"], net_seized, is_latest)

                rows.append({
                    "loan": lot["loans"][0], "loans": lot["loans"], "status": status, "cmint": cmint, "decimals": decimals, "lender": lender,
                    "offerbook_value_usd": offerbook_value_usd, "net_seized": net_seized,
                    "sold_amount": sold_amount, "sold_usd_exact": trace["sold_usd_exact"],
                    "live_priced_legs": trace["live_priced_legs"], "last_sale_ts": trace["last_sale_ts"],
                    "current_balance": current_balance, "unsold_remainder": unsold_remainder,
                    "is_latest_in_group": is_latest,
                })
            except Exception as exc:
                log.warning("  FAILED tracing %s — %s — skipping for this run, rerun to retry", lot_label, exc)
                rows.append({
                    "loan": lot["loans"][0], "loans": lot["loans"], "status": "error", "cmint": cmint,
                    "offerbook_value_usd": offerbook_value_usd, "error": str(exc),
                })

    # One batched live-price fetch for every mint any row still needs priced.
    mints_needing_price: set[str] = set()
    for r in rows:
        if r["status"] in ("holding", "partial") and r.get("current_balance", 0):
            mints_needing_price.add(r["cmint"])
        for other_mint, _amt in r.get("live_priced_legs", []):
            mints_needing_price.add(other_mint)
    prices = fetch_live_prices(list(mints_needing_price))

    for r in rows:
        if r["status"] not in ("sold", "holding", "partial", "rolled_forward", "contaminated"):
            continue
        live_usd = 0.0
        unpriced_legs: list[tuple[str, float]] = []
        for other_mint, amt in r.get("live_priced_legs", []):
            price = prices.get(other_mint)
            if price is None:
                unpriced_legs.append((other_mint, amt))
            else:
                live_usd += amt * price
        r["sold_usd_live"] = live_usd
        r["sold_usd_total"] = r["sold_usd_exact"] + live_usd
        r["unpriced_legs"] = unpriced_legs

        current_price = prices.get(r["cmint"]) if r["current_balance"] else None
        r["current_value_usd"] = (r["current_balance"] * current_price) if (r["current_balance"] and current_price is not None) else None
        r["current_price_missing"] = bool(r["current_balance"]) and current_price is None

        if r["status"] in ("rolled_forward", "contaminated"):
            # Unsold/unattributable remainder deliberately excluded — see
            # classify_status / trace_collateral_outcome's module docstring.
            r["total_captured_usd"] = r["sold_usd_total"]
        else:
            r["total_captured_usd"] = r["sold_usd_total"] + (r["current_value_usd"] or 0.0)
        r["delta_vs_offerbook_usd"] = r["total_captured_usd"] - r["offerbook_value_usd"]

    return rows


# ---------------------------------------------------------------------------
# Printing
# ---------------------------------------------------------------------------

def print_ranking(ranked: list[tuple[str, float]], top: int) -> None:
    log.info("")
    log.info("=" * 80)
    log.info("TOP %d LENDERS BY REALIZED PNL (scope for the seizure trace below)", top)
    log.info("=" * 80)
    col = "{:<4}{:<46}{:>16}"
    log.info(col.format("#", "lender", "realized PNL $"))
    log.info("-" * 80)
    for i, (lender, amount) in enumerate(ranked[:top], 1):
        log.info(col.format(i, lender, f"{amount:,.2f}"))
    log.info("=" * 80)


FLAG_LONG = {
    "CONTAMINATED": "CONTAMINATED — unrelated deposit detected mid-trace, numbers reflect ONLY pre-contamination activity",
    "HELD TOO LONG": "HELD TOO LONG",
    "HOLDING PAID OFF": "HOLDING PAID OFF",
    "UNDERWATER": "UNDERWATER vs. default-time value",
}


def flag_for_status(status: str, delta: float) -> tuple[str, str]:
    """(short flag label, 'better'/'worse'/'') — shared between the console
    table and the Excel export so the two can never drift apart. The
    console print decorates the short label via FLAG_LONG; Excel just
    wants the short, filterable label in its own column."""
    if status == "contaminated":
        return "CONTAMINATED", ""
    if status in ("sold", "partial") and delta < -1.0:
        return "HELD TOO LONG", "worse"
    if status in ("sold", "partial") and delta > 1.0:
        return "HOLDING PAID OFF", "better"
    if status == "holding" and delta < -1.0:
        return "UNDERWATER", "worse"
    if status == "holding" and delta > 1.0:
        return "", "better"
    return "", ""


def print_seizure_outcomes(rows: list[dict]) -> None:
    log.info("")
    log.info("=" * 165)
    log.info("SEIZURE OUTCOMES (defaulted-loan collateral, mark-to-market vs. what actually happened on-chain)")
    log.info("=" * 165)
    if not rows:
        log.info("No defaulted loans in scope.")
        return

    col = "{:<46}{:<12}{:<14}{:<14}{:>16}{:>16}{:>16}{:>16}{:>16}  {:<46}"
    log.info(col.format(
        "lender", "defaulted", "token", "status", "offerbook $", "realized $", "current $", "captured $", "Δ vs offerbook", "loan",
    ))
    log.info("-" * 165)

    total_offerbook = total_captured = 0.0
    untraced_count = contaminated_count = error_count = 0
    better_held, worse_held = 0, 0
    for r in rows:
        loans = r.get("loans") or [r["loan"]]
        l = r["loan"]
        date_str = (l.get("updatedAt") or "")[:10]
        loan_str = l.get("pubkey", "") if len(loans) == 1 else f"{len(loans)} loans merged: " + "+".join(x.get("pubkey", "")[:8] for x in loans)
        lender = r.get("lender", l.get("lender", ""))
        cmint = r.get("cmint")
        token = symbol_for(cmint) if cmint else "NFT"
        status = r["status"]

        if status in ("nft", "unresolvable", "n/a", "skipped_dust", "error"):
            log.info(col.format(
                lender, date_str, token, status.upper(),
                f"{r.get('offerbook_value_usd', 0.0):,.2f}", "n/a", "n/a", "n/a", "n/a", loan_str,
            ))
            if status == "error":
                log.info("    (%s — rerun to retry; already-finished lots stay cached)", r.get("error", "unknown error"))
                error_count += 1
            untraced_count += 1
            continue

        realized_str = f"{r['sold_usd_total']:,.2f}" if r["sold_usd_total"] or status in ("sold", "partial") else "n/a"
        current_str = "NO PRICE" if r["current_price_missing"] else (
            f"{r['current_value_usd']:,.2f}" if r["current_value_usd"] is not None else "n/a"
        )
        if status == "rolled_forward":
            current_str = f"({r['unsold_remainder']:.2f} units rolled into next default)"
        elif status == "contaminated":
            current_str = f"({r['unsold_remainder']:.2f} units unattributable)"
        delta = r["delta_vs_offerbook_usd"]
        delta_str = f"{delta:+,.2f}"
        short_flag, direction = flag_for_status(status, delta)
        if status == "contaminated":
            contaminated_count += 1
        elif direction == "better":
            better_held += 1
        elif direction == "worse":
            worse_held += 1
        flag = f"  *** {FLAG_LONG.get(short_flag, short_flag)} ***" if short_flag else ""

        log.info(col.format(
            lender, date_str, token, status.upper(), f"{r['offerbook_value_usd']:,.2f}",
            realized_str, current_str, f"{r['total_captured_usd']:,.2f}", delta_str, loan_str,
        ) + flag)

        for other_mint, amt in r.get("unpriced_legs", []):
            log.info("    (received %.4f of %s on sale — no live price resolvable, excluded from realized $ above)", amt, symbol_for(other_mint))

        total_offerbook += r["offerbook_value_usd"]
        total_captured += r["total_captured_usd"]

    log.info("-" * 165)
    delta_total = total_captured - total_offerbook
    log.info(
        "TOTAL (traced rows only) — offerbook mark-to-market: $%s   actually captured: $%s   Δ: %s",
        f"{total_offerbook:,.2f}", f"{total_captured:,.2f}", f"{delta_total:+,.2f}",
    )
    log.info("Across traced rows: %d better off holding, %d worse off holding (vs. selling immediately at default)", better_held, worse_held)
    if contaminated_count:
        log.info("%d row(s) marked CONTAMINATED — included in the total above using ONLY their pre-contamination sales (no remainder priced)", contaminated_count)
    if untraced_count:
        log.info("(%d row(s) excluded from the totals above — NFT collateral, unresolvable decimals, or below --min-seizure-usd)", untraced_count)
    if error_count:
        log.info("%d lot(s) FAILED (network/RPC error) and were skipped — rerun the same command to retry just those (everything else is cached)", error_count)
    log.info("=" * 165)


MONEY_COLUMNS = ("offerbook_usd", "realized_usd", "current_usd", "captured_usd", "delta_usd")


def write_excel(rows: list[dict], path: str) -> None:
    """One row per `rows` entry, same data the console table shows — but as
    real numbers/dates, not pre-formatted strings, so sorting, filtering,
    and formulas all work natively once it's open. Untraced rows (NFT /
    unresolvable / skipped-dust / error) are included too, with their
    numeric columns left blank rather than zeroed, so they're visually and
    formula-wise distinguishable from a genuine $0 outcome."""
    headers = [
        "lender", "defaulted", "token", "contract", "status",
        "offerbook_usd", "realized_usd", "current_usd", "captured_usd", "delta_usd",
        "flag", "loans_merged", "loan_pubkeys", "notes",
    ]

    wb = Workbook()
    ws = wb.active
    ws.title = "Seizure Outcomes"
    ws.append(headers)
    for cell in ws[1]:
        cell.font = Font(bold=True)

    for r in rows:
        loans = r.get("loans") or [r["loan"]]
        l = r["loan"]
        lender = r.get("lender", l.get("lender", ""))
        cmint = r.get("cmint")
        token = symbol_for(cmint) if cmint else "NFT"
        status = r["status"]
        loan_pubkeys = "+".join(x.get("pubkey", "") for x in loans)

        try:
            defaulted_date = datetime.fromisoformat((l.get("updatedAt") or "").replace("Z", "+00:00")).date()
        except ValueError:
            defaulted_date = None

        notes = []
        if status == "error":
            notes.append(r.get("error", "unknown error"))
        for other_mint, amt in r.get("unpriced_legs", []) or []:
            notes.append(f"received {amt:.4f} {symbol_for(other_mint)} on sale, no live price resolvable")
        if status == "rolled_forward":
            notes.append(f"{r.get('unsold_remainder', 0.0):.2f} units rolled into next default")
        elif status == "contaminated":
            notes.append(f"{r.get('unsold_remainder', 0.0):.2f} units unattributable after contamination")
        if r.get("current_price_missing"):
            notes.append("current holding has no resolvable live price")

        if status in ("nft", "unresolvable", "n/a", "skipped_dust", "error"):
            ws.append([
                lender, defaulted_date, token, cmint or "", status.upper(),
                r.get("offerbook_value_usd", 0.0), None, None, None, None,
                "", len(loans), loan_pubkeys, "; ".join(notes),
            ])
            continue

        delta = r["delta_vs_offerbook_usd"]
        short_flag, _direction = flag_for_status(status, delta)
        current_usd = r["current_value_usd"] if not r["current_price_missing"] else None
        ws.append([
            lender, defaulted_date, token, cmint or "", status.upper(),
            r["offerbook_value_usd"], r["sold_usd_total"], current_usd, r["total_captured_usd"], delta,
            short_flag, len(loans), loan_pubkeys, "; ".join(notes),
        ])

    for col_idx, header in enumerate(headers, start=1):
        letter = get_column_letter(col_idx)
        if header in MONEY_COLUMNS:
            for row_idx in range(2, ws.max_row + 1):
                ws.cell(row=row_idx, column=col_idx).number_format = "#,##0.00"
        elif header == "defaulted":
            for row_idx in range(2, ws.max_row + 1):
                ws.cell(row=row_idx, column=col_idx).number_format = "yyyy-mm-dd"
        width = {
            "lender": 46, "contract": 46, "loan_pubkeys": 60, "notes": 60,
            "token": 14, "status": 14, "flag": 18, "defaulted": 12,
        }.get(header, 14)
        ws.column_dimensions[letter].width = width

    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions

    Path(path).parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)
    log.info("Saved %d row(s) to %s", ws.max_row - 1, path)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--top", type=int, default=TOP_DEFAULT, help=f"Trace defaults for the top N lenders by realized PNL (default {TOP_DEFAULT}).")
    parser.add_argument("--min-seizure-usd", type=float, default=MIN_SEIZURE_USD_DEFAULT, help=f"Skip seizures below this mark-to-market $ value — dust not worth the RPC cost to trace (default {MIN_SEIZURE_USD_DEFAULT}).")
    parser.add_argument("--lender", default=None, help="Trace just this one lender's defaults, regardless of PNL rank (skips the ranking fetch/print).")
    parser.add_argument(
        "--no-cache", action="store_true",
        help="Ignore seizure_outcome_scan_state.json and retrace every lot from scratch, even ones already cached "
             "from a prior run. Results are still written back to the cache for next time.",
    )
    parser.add_argument(
        "--output", default=None,
        help="Excel (.xlsx) output path (default ~/Desktop/seizure_outcomes_<timestamp>.xlsx). "
             "Pass --no-excel to skip writing it entirely.",
    )
    parser.add_argument("--no-excel", action="store_true", help="Skip writing the Excel file.")
    args = parser.parse_args()

    if args.lender:
        log.info("Fetching defaulted loans for %s …", args.lender)
        all_defaulted = _fetch_all_pages("/loans/status/defaulted")
        target_loans = [l for l in all_defaulted if l.get("lender") == args.lender]
        if not target_loans:
            log.error("No defaulted loans found for %s.", args.lender)
            sys.exit(1)
    else:
        ranked, all_defaulted = compute_pnl_ranking()
        print_ranking(ranked, args.top)
        top_lenders = {lender for lender, _pnl in ranked[: args.top]}
        target_loans = [l for l in all_defaulted if l.get("lender") in top_lenders]

    log.info("")
    log.info("Tracing on-chain outcome of %d defaulted loan(s) (>= $%.2f mark-to-market) …", len(target_loans), args.min_seizure_usd)
    rows = analyze_seizures(target_loans, args.min_seizure_usd, use_cache=not args.no_cache)
    print_seizure_outcomes(rows)

    if not args.no_excel:
        output_path = args.output or str(DESKTOP_DIR / f"seizure_outcomes_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx")
        write_excel(rows, output_path)


if __name__ == "__main__":
    main()
