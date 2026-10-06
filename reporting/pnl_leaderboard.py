"""
Offerbook Realized PNL Leaderboard
===================================
Ranks every Offerbook lender by all-time realized PNL. There's no "top by
PNL" endpoint on the API — only volume-based leaderboards (/metrics/top-
lenders) — so this pulls the full repaid + defaulted loan history platform-
wide (no borrower/lender filter) and aggregates client-side.

Realized PNL per lender =
  + net interest earned on REPAID loans. Interest is converted to USD via
    the platform's documented proportional formula (interest / principal-
    Amount * startPrincipalAmountUsd), then the actual protocol "repay" fee
    charged is subtracted — taken straight from metadata.fees.repay.amountUsd
    per loan, not assumed as a flat rate.
  + collateral kept on DEFAULTED loans, valued at default time
    (endCollateralAmountUsd), minus the principal that was lent out and not
    recovered (startPrincipalAmountUsd). This is a mark-to-market figure at
    the moment of default, not necessarily cash actually realized — if the
    lender is still holding the seized collateral, it's unrealized from here.
  + interest actually collected on in-place loan extensions/rollovers,
    across EVERY loan regardless of current status (repaid, defaulted, or
    still active). Offerbook lets a loan's term be extended in place — same
    pubkey, stays "active" through the extension, expiredAt pushed forward,
    extensionCount ticks up — and the borrower pays that completed term's
    full interest to the lender AT that moment (metadata.extensions[].
    interestPaid/repayFee), same as a real repayment. That's genuinely
    realized cash, not mark-to-market, so it counts here even for a loan
    that's still active and hasn't reached repaid/defaulted (confirmed
    against live data to affect ~12% of active loans at any given time —
    not an edge case worth excluding). See portfolio_health.py's
    compute_realized_pnl for the same fix, applied there first.

Also reports each lender's total volume — total USD principal (at
origination) of every SETTLED (repaid or defaulted) loan they've made. Volume
stays scoped to settled loans only (deliberately excludes active loans,
extended or not) — it's a distinct, unrelated metric from realized PNL and
isn't affected by the rollover-interest fix above. Shown alongside PNL, not
used to rank (ranking is still by realized PNL) — a high-volume lender isn't
necessarily a profitable one.

Also reports a second volume figure, "vol incl rollovers $", that treats
each in-place extension as its OWN distinct completed lending cycle, not
just the loan's final settlement. A loan extended 3 times before being
repaid is "1 loan" by the base volume metric above (same pubkey) but is
economically 4 separate completed terms — the borrower paid full interest
and the principal went back to work again at each one. Each extension's own
metadata.extensions[].principalAmountUsd is used (falling back to the loan's
startPrincipalAmountUsd if an individual extension is missing it) rather
than re-using the loan's original principal figure, since it's priced at the
moment that specific term actually completed. Unlike base volume, this also
picks up extensions on currently-ACTIVE loans (their completed prior terms
count even though the loan's current open term doesn't, same "already
realized, not mark-to-market" reasoning the rollover-interest PNL fix above
uses) — so a lender can show rollover volume here even with $0 in the base
volume column, if every one of their loans happens to still be active. The
accompanying "cycles" column is repaid + defaulted + rolled-over counts
combined — how many distinct completed lending cycles that total spans.

If OFFERBOOK_PORTFOLIO_WALLETS is set (.env — the same var portfolio_health.py
reads, comma-separated addresses), those specific lenders are merged into a
single combined row before ranking, labeled "YOUR WALLETS (N combined)"
rather than listed individually — so your own multi-wallet total ranks (and
reads) as one entity instead of being split across several rows. The actual
addresses never appear in the output or in this committed file; they only
ever come from your local, gitignored .env. Leave the var unset and every
wallet is ranked individually as before (the default for anyone else running
this public script).

YOUR SEIZURE OUTCOMES (only runs if OFFERBOOK_PORTFOLIO_WALLETS is set) —
the PNL leaderboard above values every default's collateral at a single
mark-to-market snapshot (endCollateralAmountUsd, priced the moment default
was recorded). That's a fiction the instant the lender doesn't sell right
then — the question "should I be holding collateral or liquidating it
immediately" can only be answered against what ACTUALLY happened to it
on-chain afterward, not that snapshot. So for each of YOUR defaulted loans,
this walks Solana directly (not the Offerbook API, which has no concept of
"what did you do with the collateral afterward") and reports:

  - SOLD: every token-account transaction since default that reduced your
    balance of that specific collateral mint, paired with whatever asset
    you received in the same transaction (USDC priced at exactly $1, never
    drifts; SOL or any other received token priced at today's live price as
    a proxy — flagged '(live-priced)' since that's not necessarily the
    price at the moment you actually sold, unlike the USDC case).
  - HOLDING: current live balance of that mint still sitting in your
    wallet, priced at today's live price — this is the number that answers
    "what is it worth RIGHT NOW", i.e. the live alternative to having sold
    at the mark-to-market snapshot.
  - PARTIAL: both of the above, if you've sold some and still hold the
    rest.
  - Δ vs mark-to-market = (realized + current value) − offerbook's
    endCollateralAmountUsd snapshot. Negative means you'd have done better
    selling immediately at default; positive means holding (so far) paid
    off. This is the actionable "hold vs. sell" signal this section exists
    to produce.

Implementation note: rather than scanning your whole wallet's transaction
history (slow and noisy on an active trading wallet — a busy lender can
have thousands of unrelated transactions between a default and today), this
derives the collateral mint's own Associated Token Account address (the
standard SPL/Token-2022 PDA — classic SPL vs. Token-2022 chosen from the
loan's own collateralTokenProgram field) and scans ONLY that account's
transaction history. A specific token account is touched by orders of
magnitude fewer transactions than the whole wallet, which is both faster
and more complete (verified directly against Solscan: wallet-level scanning
missed several small intermediate transfers that the token-account-level
scan caught). NFT collateral (no fungible mint) can't be traced this way
and is shown as a separate N/A row rather than silently skipped. Pass
--no-seizure-trace to skip this section (it costs several Solana RPC calls
per defaulted loan, against the free public mainnet-beta endpoint by
default — noticeably slower than the leaderboard above, which is a single
Offerbook API pass).

Read-only: never signs or submits anything.

Usage:
  python pnl_leaderboard.py              # top 25 by realized PNL
  python pnl_leaderboard.py --top 50
  python pnl_leaderboard.py --no-seizure-trace    # skip the on-chain seizure-outcome section
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
from solders.pubkey import Pubkey

# Shared modules live in ../lib — see README's repo-layout note.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))

import offerbook_common as _common

load_dotenv()

API_BASE = os.getenv("OFFERBOOK_API_BASE", "https://api.offerbook.jup.ag/api/v1")
PAGE_SIZE = 100

# Wallets to merge into a single combined row — see module docstring. Never
# hardcoded here: only ever comes from the local, gitignored .env, so this
# committed script never carries real addresses.
MERGE_WALLETS: set[str] = {
    w.strip() for w in os.getenv("OFFERBOOK_PORTFOLIO_WALLETS", "").split(",") if w.strip()
}
MERGE_LABEL = f"YOUR WALLETS ({len(MERGE_WALLETS)} combined)"

# --- On-chain seizure-outcome tracing (YOUR SEIZURE OUTCOMES section) ------
SOLANA_RPC = os.getenv("SOLANA_RPC", "https://api.mainnet-beta.solana.com")
JUPITER_PRICE_API = "https://api.jup.ag/price/v3"
JUPITER_API_KEY = os.getenv("JUPITER_API_KEY", "")
DEXSCREENER_API = "https://api.dexscreener.com/latest/dex/tokens"

USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
SOL_MINT = "So11111111111111111111111111111111111111112"
SOL_DECIMALS = 9
TOKEN_PROGRAM_ID = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
ASSOCIATED_TOKEN_PROGRAM_ID = Pubkey.from_string("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL")

KNOWN_DECIMALS = _common.KNOWN_DECIMALS
KNOWN_SYMBOLS = _common.KNOWN_SYMBOLS

# A current balance at/below this fraction of what was originally seized is
# treated as "fully sold" (dust from rounding/fees, not a real remainder); a
# current balance at/above this fraction of the original is treated as
# "never sold" — see classify_seizure_status().
DUST_FRACTION = 0.0005

SIGNATURE_PAGE_SIZE = 1000   # max getSignaturesForAddress allows per call
SIGNATURE_PAGE_CAP = 5       # x1000 = 5000 sigs ceiling per token account before giving up — a
                               # defaulted loan's collateral mint, scoped to its own ATA, should need
                               # nowhere near this many (see module docstring on ATA- vs wallet-level scanning)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("pnl_leaderboard")

SESSION = requests.Session()


def _fetch_all_pages(endpoint: str, params: dict | None = None) -> list[dict]:
    return _common.fetch_all_pages(SESSION, API_BASE, endpoint, params, PAGE_SIZE, sleep_secs=0.1)


def symbol_for(mint: str | None) -> str:
    if not mint:
        return "NFT"
    return KNOWN_SYMBOLS.get(mint, f"{mint[:6]}…{mint[-4:]}")


# ---------------------------------------------------------------------------
# Solana RPC helpers (YOUR SEIZURE OUTCOMES section only) — same retry/
# backoff + getTokenAccountsByOwner/getAccountInfo approach portfolio_health.py
# uses for the same reason (the free public mainnet-beta endpoint rate-limits
# aggressively), kept local to this script rather than shared, since it's a
# small, self-contained piece of logic specific to this one section.
# ---------------------------------------------------------------------------

SOLANA_RPC_MAX_RETRIES = 6


def _solana_rpc(payload: dict) -> dict:
    """POST to SOLANA_RPC with retry/backoff on 429. Honors a Retry-After
    header if present, otherwise backs off exponentially (1s, 2s, 4s, 8s,
    16s, 32s). Raises on the final attempt or any non-429 error."""
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
    on-chain Associated Token Account Program itself uses. Scanning THIS
    address's transaction history (rather than the owner wallet's) is what
    makes seizure-outcome tracing both fast and complete — see module
    docstring."""
    owner_pk = Pubkey.from_string(owner)
    mint_pk = Pubkey.from_string(mint)
    program_pk = Pubkey.from_string(token_program)
    ata, _bump = Pubkey.find_program_address(
        [bytes(owner_pk), bytes(program_pk), bytes(mint_pk)], ASSOCIATED_TOKEN_PROGRAM_ID,
    )
    return str(ata)


def fetch_current_token_balance_raw(owner: str, mint: str) -> int:
    """Current raw balance of `mint` for `owner`, summed across every token
    account that owner holds for it (should only ever be one in practice,
    but matches portfolio_health.py's fetch_wallet_token_balance for
    robustness). 0 if the account was fully drained and closed, or never
    existed."""
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
    """Last-resort decimals lookup straight from the mint account, for a
    long-tail token KNOWN_DECIMALS doesn't cover. Works for both classic SPL
    and Token-2022 mints."""
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
    per-mint fallback for anything Jupiter doesn't cover. Same approach
    portfolio_health.py's fetch_current_prices uses; trimmed to just prices
    since decimals are already resolved separately in this script."""
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


def fetch_signatures_since(address: str, since_ts: int) -> list[dict]:
    """Every signature touching `address` with blockTime >= since_ts, newest
    first, paginated via the `before` cursor. Capped at SIGNATURE_PAGE_CAP
    pages as a runaway-safety measure — see module docstring on why this is
    scoped to a token ATA, not a wallet, so it should never come close."""
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
    return [s for s in all_sigs if (s.get("blockTime") or 0) >= since_ts and not s.get("err")]


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
    """(pre, post) native SOL balance for `owner` within `tx`, or
    (None, None) if `owner` isn't one of the transaction's accounts."""
    try:
        accounts = tx["transaction"]["message"]["accountKeys"]
        idx = next(i for i, a in enumerate(accounts) if a.get("pubkey") == owner)
        meta = tx["meta"]
        return meta["preBalances"][idx] / 1e9, meta["postBalances"][idx] / 1e9
    except (KeyError, IndexError, StopIteration, TypeError):
        return None, None


def trace_collateral_outcome(lender: str, mint: str, token_program: str, net_seized_raw: int, since_ts: int) -> dict:
    """Walks every transaction since `since_ts` (the default) touching the
    (lender, mint) Associated Token Account, and classifies what actually
    happened to the seized collateral:

      sold_amount       - total units of `mint` sold across all matching txs
      sold_usd_exact    - the portion of sale proceeds received as USDC
                           (priced at exactly $1, never drifts)
      sold_usd_live     - the portion received as SOL or any other token,
                           priced at TODAY's live price (a proxy, not
                           necessarily the price at the moment of that sale
                           — see module docstring)
      sold_usd_unpriced - raw units of some other token received that
                           fetch_live_prices() couldn't price at all (shown
                           as a caveat, never silently dropped)
      last_sale_ts      - blockTime of the most recent sale, or None
      current_balance_raw - live on-chain balance right now (ground truth,
                           queried directly rather than inferred by
                           subtraction, so it self-corrects for any activity
                           this trace didn't itself observe)

    Returns a dict rather than a flat tuple since callers need to carry this
    straight through to pricing/printing without re-deriving anything."""
    ata = derive_ata(lender, mint, token_program)
    sigs = fetch_signatures_since(ata, since_ts)

    sold_amount = 0.0
    sold_usd_exact = 0.0
    live_priced_legs: list[tuple[str, float]] = []  # (mint, amount received) needing a live price
    last_sale_ts: int | None = None

    for s in sigs:
        tx = fetch_transaction(s["signature"])
        time.sleep(0.1)  # be polite to the public RPC endpoint
        if not tx:
            continue
        meta = tx.get("meta") or {}
        pre_bals, post_bals = meta.get("preTokenBalances") or [], meta.get("postTokenBalances") or []
        pre_amt = _token_balance(pre_bals, mint, lender) or 0.0
        post_amt = _token_balance(post_bals, mint, lender) or 0.0
        delta = pre_amt - post_amt
        if delta <= 0:
            continue  # not a sale of this mint (a receipt, or unrelated to this account)

        sold_amount += delta
        last_sale_ts = s.get("blockTime")

        # What did the lender receive in this SAME transaction? Check every
        # other mint this owner's balance increased for, plus native SOL —
        # Jupiter settles a route atomically in one tx regardless of how
        # many hops it took, so this captures the true final proceeds.
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
            if sol_received > 0.0001:  # ignore sub-fee noise, not a real proceeds leg
                live_priced_legs.append((SOL_MINT, sol_received))

    current_balance_raw = fetch_current_token_balance_raw(lender, mint)

    return {
        "sold_amount": sold_amount,
        "sold_usd_exact": sold_usd_exact,
        "live_priced_legs": live_priced_legs,
        "last_sale_ts": last_sale_ts,
        "current_balance_raw": current_balance_raw,
        "scanned_tx_count": len(sigs),
    }


def classify_seizure_status(sold_amount: float, current_balance: float, net_seized: float) -> str:
    """'sold' / 'holding' / 'partial' — a current balance within
    DUST_FRACTION of zero (rounding/fee dust) still counts as fully sold; a
    current balance within DUST_FRACTION of the original counts as never
    sold at all. net_seized is the original net-of-fee amount, used as the
    scale for both thresholds."""
    if net_seized <= 0:
        return "n/a"
    if current_balance <= net_seized * DUST_FRACTION:
        return "sold"
    if sold_amount <= net_seized * DUST_FRACTION:
        return "holding"
    return "partial"


def analyze_seizures(defaulted_loans: list[dict]) -> list[dict]:
    """One row per defaulted loan in `defaulted_loans` (already pre-filtered
    to your own wallets by the caller) — NFT-collateral loans get a row
    flagged 'nft' (can't be swap-traced the same way a fungible mint can)
    rather than being silently dropped. Live prices for current holdings
    and any non-USDC sale proceeds are batched into ONE fetch_live_prices()
    call at the end, after every loan's on-chain trace is done, rather than
    one price call per loan."""
    rows = []
    for l in defaulted_loans:
        md = l.get("metadata") or {}
        cmint = l.get("collateralMint")
        offerbook_value_usd = md.get("endCollateralAmountUsd")
        if offerbook_value_usd is None:
            offerbook_value_usd = md.get("startCollateralAmountUsd") or 0.0

        if not cmint:
            rows.append({
                "loan": l, "status": "nft", "cmint": None, "decimals": None,
                "offerbook_value_usd": offerbook_value_usd,
            })
            continue

        decimals = KNOWN_DECIMALS.get(cmint)
        if decimals is None:
            decimals = fetch_mint_decimals_onchain(cmint)
        if decimals is None:
            log.warning("No decimals resolvable for %s… — skipping seizure trace for loan %s", cmint[:8], l.get("pubkey"))
            rows.append({
                "loan": l, "status": "unresolvable", "cmint": cmint, "decimals": None,
                "offerbook_value_usd": offerbook_value_usd,
            })
            continue

        liquidation_fee_raw = (((md.get("fees") or {}).get("liquidation") or {}).get("amount")) or 0
        net_seized_raw = (l.get("collateralAmount") or 0) - liquidation_fee_raw
        net_seized = net_seized_raw / 10 ** decimals

        token_program = l.get("collateralTokenProgram") or TOKEN_PROGRAM_ID
        since_ts = int(datetime.fromisoformat(l["updatedAt"].replace("Z", "+00:00")).timestamp()) if l.get("updatedAt") else 0

        log.info("  tracing %s… seizure for loan %s (lender %s…) …", symbol_for(cmint), l.get("pubkey", "")[:8], l.get("lender", "")[:8])
        trace = trace_collateral_outcome(l["lender"], cmint, token_program, net_seized_raw, since_ts)

        sold_amount = trace["sold_amount"]
        current_balance = trace["current_balance_raw"] / 10 ** decimals
        status = classify_seizure_status(sold_amount, current_balance, net_seized)

        rows.append({
            "loan": l, "status": status, "cmint": cmint, "decimals": decimals,
            "offerbook_value_usd": offerbook_value_usd, "net_seized": net_seized,
            "sold_amount": sold_amount, "sold_usd_exact": trace["sold_usd_exact"],
            "live_priced_legs": trace["live_priced_legs"], "last_sale_ts": trace["last_sale_ts"],
            "current_balance": current_balance,
        })

    # One batched live-price fetch for every mint any row still needs priced
    # (current holdings + non-USDC sale proceeds), instead of one call per loan.
    mints_needing_price: set[str] = set()
    for r in rows:
        if r["status"] in ("holding", "partial") and r.get("current_balance", 0) > 0:
            mints_needing_price.add(r["cmint"])
        for other_mint, _amt in r.get("live_priced_legs", []):
            mints_needing_price.add(other_mint)
    prices = fetch_live_prices(list(mints_needing_price))

    for r in rows:
        if r["status"] in ("nft", "unresolvable", "n/a"):
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

        current_price = prices.get(r["cmint"])
        r["current_value_usd"] = (r["current_balance"] * current_price) if current_price is not None else None
        r["current_price_missing"] = r["current_balance"] > 0 and current_price is None

        realized = r["sold_usd_total"]
        current = r["current_value_usd"] or 0.0
        r["total_captured_usd"] = realized + current
        r["delta_vs_offerbook_usd"] = r["total_captured_usd"] - r["offerbook_value_usd"]

    return rows


def print_seizure_outcomes(rows: list[dict]) -> None:
    log.info("")
    log.info("=" * 150)
    log.info("YOUR SEIZURE OUTCOMES (defaulted-loan collateral, mark-to-market vs. what actually happened on-chain)")
    log.info("=" * 150)
    if not rows:
        log.info("No defaulted loans on your wallets.")
        return

    col = "{:<12}{:<14}{:<10}{:>16}{:>16}{:>16}{:>16}{:>16}  {:<46}"
    log.info(col.format(
        "defaulted", "token", "status", "offerbook $", "realized $", "current $", "captured $", "Δ vs offerbook", "loan",
    ))
    log.info("-" * 150)

    total_offerbook = total_captured = 0.0
    untraced_count = 0
    for r in rows:
        l = r["loan"]
        date_str = (l.get("updatedAt") or "")[:10]
        token = symbol_for(r["cmint"]) if r["cmint"] else "NFT"

        if r["status"] in ("nft", "unresolvable", "n/a"):
            log.info(col.format(
                date_str, token, r["status"].upper(), f"{r['offerbook_value_usd']:,.2f}",
                "n/a", "n/a", "n/a", "n/a", l.get("pubkey", ""),
            ))
            untraced_count += 1  # excluded from the totals below — no traced counterpart to compare against
            continue

        realized_str = f"{r['sold_usd_total']:,.2f}" if r["status"] in ("sold", "partial") else "n/a"
        current_str = "NO PRICE" if r["current_price_missing"] else (
            f"{r['current_value_usd']:,.2f}" if r["status"] in ("holding", "partial") else "n/a"
        )
        delta = r["delta_vs_offerbook_usd"]
        delta_str = f"{delta:+,.2f}"
        flag = ""
        if r["status"] == "sold" and delta < -1.0:
            flag = "  *** HELD TOO LONG — would've done better selling at default ***"
        elif r["status"] == "sold" and delta > 1.0:
            flag = "  *** HOLDING PAID OFF ***"
        elif r["status"] == "holding" and delta < -1.0:
            flag = "  *** UNDERWATER vs. default-time value — consider selling ***"

        log.info(col.format(
            date_str, token, r["status"].upper(), f"{r['offerbook_value_usd']:,.2f}",
            realized_str, current_str, f"{r['total_captured_usd']:,.2f}", delta_str, l.get("pubkey", ""),
        ) + flag)

        if r.get("unpriced_legs"):
            for other_mint, amt in r["unpriced_legs"]:
                log.info("    (received %.4f of %s on sale — no live price resolvable, excluded from realized $ above)", amt, symbol_for(other_mint))
        if r.get("live_priced_legs") and not r["current_price_missing"]:
            non_usdc = [m for m, _ in r["live_priced_legs"]]
            if non_usdc:
                log.info("    (some proceeds received as %s, priced at TODAY's rate, not the price at time of sale)", ", ".join(sorted({symbol_for(m) for m in non_usdc})))

        total_offerbook += r["offerbook_value_usd"]
        total_captured += r["total_captured_usd"]

    log.info("-" * 150)
    delta_total = total_captured - total_offerbook
    log.info(
        "TOTAL (traced rows only) — offerbook mark-to-market: $%s   actually captured (realized + current holdings): $%s   Δ: %s",
        f"{total_offerbook:,.2f}", f"{total_captured:,.2f}", f"{delta_total:+,.2f}",
    )
    if untraced_count:
        log.info("(%d row(s) excluded from the total above — NFT collateral or unresolvable decimals, can't be swap-traced)", untraced_count)
    log.info("=" * 150)


def compute_pnl() -> tuple[dict[str, float], dict[str, dict[str, int]], dict[str, float], dict[str, float], list[dict]]:
    """Returns (pnl_by_lender, counts_by_lender, volume_by_lender,
    rollover_volume_by_lender, your_defaulted_loans). counts tracks how many
    repaid/defaulted/rolled-over loans backed each lender's total, for
    context. volume is total USD principal (at origination) of every
    SETTLED (repaid or defaulted) loan that lender has made — deliberately
    excludes active loans, unrelated to the rollover-interest PNL below (see
    module docstring for why). rollover_volume is the EXTRA distinct volume
    from each in-place extension (see module docstring) — kept separate from
    `volume` rather than folded in, since base volume is a distinct metric
    callers may still want to see on its own. your_defaulted_loans is the
    RAW (unmapped) defaulted-loan records for MERGE_WALLETS specifically —
    fed to analyze_seizures() so that section doesn't need its own extra
    /loans/status/defaulted fetch.

    Any lender address in MERGE_WALLETS is remapped to MERGE_LABEL before
    ever being used as a dict key in pnl/counts/volume/rollover_volume — so
    a merged wallet's real address never appears in those, or in the final
    printed table. your_defaulted_loans is the one deliberate exception:
    analyze_seizures() needs the real addresses to drive on-chain lookups,
    and that section's own output never prints a bare lender address either
    (every row is already scoped to "YOUR" wallets)."""
    pnl: dict[str, float] = {}
    counts: dict[str, dict[str, int]] = {}
    volume: dict[str, float] = {}
    rollover_volume: dict[str, float] = {}

    def _remap(lender: str) -> str:
        return MERGE_LABEL if lender in MERGE_WALLETS else lender

    def bump(lender: str, amount: float, kind: str) -> None:
        lender = _remap(lender)
        pnl[lender] = pnl.get(lender, 0.0) + amount
        c = counts.setdefault(lender, {"repaid": 0, "defaulted": 0, "rolled_over": 0})
        c[kind] += 1

    def bump_volume(lender: str, start_principal_usd: float) -> None:
        lender = _remap(lender)
        volume[lender] = volume.get(lender, 0.0) + start_principal_usd

    def bump_rollover_volume(lender: str, principal_usd: float) -> None:
        lender = _remap(lender)
        rollover_volume[lender] = rollover_volume.get(lender, 0.0) + principal_usd

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
        bump(lender, interest_usd_gross - repay_fee_usd, "repaid")
        bump_volume(lender, start_principal_usd)

    log.info("Fetching all defaulted loans platform-wide …")
    defaulted = _fetch_all_pages("/loans/status/defaulted")
    log.info("  → %d defaulted loan(s)", len(defaulted))
    your_defaulted_loans = [l for l in defaulted if l.get("lender") in MERGE_WALLETS]
    for l in defaulted:
        lender = l.get("lender")
        if not lender:
            continue
        md = l.get("metadata") or {}
        start_principal_usd = md.get("startPrincipalAmountUsd") or 0.0
        end_collateral_usd = md.get("endCollateralAmountUsd")
        if end_collateral_usd is None:
            end_collateral_usd = md.get("startCollateralAmountUsd") or 0.0
        bump(lender, end_collateral_usd - start_principal_usd, "defaulted")
        bump_volume(lender, start_principal_usd)

    # Interest collected on in-place extensions/rollovers — real cash paid to
    # the lender the moment a term completes, whether or not the loan itself
    # has reached repaid/defaulted yet (see module docstring). Repaid/
    # defaulted loans already carry their own extension history in
    # metadata.extensions[], so only ACTIVE needs a fresh fetch here.
    log.info("Fetching all active loans platform-wide (for rollover interest) …")
    active = _fetch_all_pages("/loans/status/active")
    log.info("  → %d active loan(s)", len(active))
    for l in repaid + defaulted + active:
        lender = l.get("lender")
        if not lender:
            continue
        start_principal_usd = (l.get("metadata") or {}).get("startPrincipalAmountUsd") or 0.0
        for ext in (l.get("metadata") or {}).get("extensions") or []:
            interest_usd = ext.get("interestPaidUsd") or 0.0
            fee_usd = ((ext.get("repayFee") or {}).get("amountUsd")) or 0.0
            bump(lender, interest_usd - fee_usd, "rolled_over")
            # Each extension is priced at its own completion moment
            # (principalAmountUsd) — falls back to the loan's own
            # origination price only if that specific extension is missing
            # it, same convention the rest of this script uses.
            bump_rollover_volume(lender, ext.get("principalAmountUsd") or start_principal_usd)

    return pnl, counts, volume, rollover_volume, your_defaulted_loans


def print_leaderboard(
    pnl: dict[str, float], counts: dict[str, dict[str, int]], volume: dict[str, float],
    rollover_volume: dict[str, float], top: int,
) -> None:
    ranked = sorted(pnl.items(), key=lambda kv: kv[1], reverse=True)[:top]

    log.info("")
    log.info("=" * 115)
    log.info("Realized PNL leaderboard — repaid interest + rollover interest (net of fees) + kept collateral on defaults")
    log.info("=" * 115)
    col = "{:<4}{:<46}{:>16}{:>9}{:>11}{:>13}{:>19}{:>22}{:>9}"
    log.info(col.format(
        "#", "lender", "realized PNL $", "repaid", "defaulted", "rolled over", "total volume $",
        "vol incl rollovers $", "cycles",
    ))
    log.info("-" * 115)
    for i, (lender, amount) in enumerate(ranked, 1):
        c = counts[lender]
        base_volume = volume.get(lender, 0.0)
        total_volume = base_volume + rollover_volume.get(lender, 0.0)
        cycles = c["repaid"] + c["defaulted"] + c["rolled_over"]
        log.info(col.format(
            i, lender, f"{amount:,.2f}", c["repaid"], c["defaulted"], c["rolled_over"], f"{base_volume:,.2f}",
            f"{total_volume:,.2f}", cycles,
        ))
    log.info("=" * 115)
    log.info(
        "'vol incl rollovers $' counts each in-place extension as its own distinct completed lending cycle, "
        "not just the loan's final settlement — see the module docstring. 'cycles' = repaid + defaulted + "
        "rolled over counts combined."
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Rank Offerbook lenders by all-time realized PNL.")
    parser.add_argument("--top", type=int, default=25, help="Number of top wallets to show (default: 25)")
    parser.add_argument(
        "--no-seizure-trace", action="store_true",
        help="Skip the YOUR SEIZURE OUTCOMES section (on-chain tracing of what actually happened to your "
             "defaulted loans' seized collateral — costs several Solana RPC calls per default, noticeably "
             "slower than the leaderboard above). Only ever ran if OFFERBOOK_PORTFOLIO_WALLETS is set.",
    )
    args = parser.parse_args()

    pnl, counts, volume, rollover_volume, your_defaulted_loans = compute_pnl()
    log.info("Distinct lenders with realized PNL (repaid, defaulted, or rolled-over interest): %d", len(pnl))
    print_leaderboard(pnl, counts, volume, rollover_volume, args.top)

    if MERGE_WALLETS and not args.no_seizure_trace:
        log.info("Tracing on-chain outcome of %d defaulted loan(s) on your wallets …", len(your_defaulted_loans))
        seizure_rows = analyze_seizures(your_defaulted_loans)
        print_seizure_outcomes(seizure_rows)


if __name__ == "__main__":
    main()
