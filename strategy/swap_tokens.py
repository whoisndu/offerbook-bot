"""
Offerbook Wallet Token Swap
================================
Swaps one token for another directly in a wallet (NOT escrow — this never
touches Offerbook's API or your escrow balance at all) via Jupiter's swap
API. For topping up SOL for gas, converting idle USDC, or unwinding seized
default collateral into something spendable, without leaving the terminal.

Defaults to USDC -> SOL (the common "I need gas" case) — override with
--from/--to (symbol or mint address, case-insensitive for known symbols).
wrapAndUnwrapSol is always on, so swapping INTO SOL lands as native SOL in
the wallet (not a wrapped-SOL token account you'd then have to unwrap
yourself), and swapping FROM SOL spends native SOL directly.

Fetches a fresh quote right before building the transaction (so the preview
reflects the current price, not a stale one), prints input amount, expected
output, minimum received after slippage, price impact, and the route (which
DEX(s) it's filling through), and asks for confirmation before signing —
same safety pattern as every other script here. Refuses to proceed if the
wallet's balance of --from is less than --amount.

Same signing infrastructure as escrow_transfer.py (imports
defaulter_capture.py for it) — Ledger by default (interactively prompted
which account, unless --ledger-path is given) or --private-key for the
.env hot wallet. Only ever operates on ONE wallet per run.

Usage:
  python swap_tokens.py --amount 10                          # swap 10 USDC -> SOL, prompts for account
  python swap_tokens.py --amount 10 --from USDC --to SOL
  python swap_tokens.py --amount 0.5 --from SOL --to USDC
  python swap_tokens.py --amount 10 --slippage-bps 100       # 1% slippage tolerance (default 0.5%)
  python swap_tokens.py --amount 10 --ledger-path "44'/501'/1'"   # skip the account prompt
  python swap_tokens.py --amount 10 --private-key            # sign with the .env hot wallet instead
  python swap_tokens.py --amount 10 --yes                    # skip the confirmation prompt
  DRY_RUN=true python swap_tokens.py --amount 10              # preview only, no submission
"""
from __future__ import annotations

import argparse
import logging
import os
import sys

import requests
from dotenv import load_dotenv

load_dotenv()

# Shared modules live in ../lib — see README's repo-layout note.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))

import defaulter_capture as dc
import offerbook_common as _common

SOLANA_RPC = os.getenv("SOLANA_RPC", "https://api.mainnet-beta.solana.com")
JUPITER_SWAP_API = "https://api.jup.ag/swap/v1"
JUPITER_API_KEY = os.getenv("JUPITER_API_KEY", "")

DRY_RUN: bool = os.getenv("DRY_RUN", "true").lower() in ("1", "true", "yes")

SOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = dc.USDC_MINT

KNOWN_SYMBOLS = _common.KNOWN_SYMBOLS
KNOWN_DECIMALS = _common.KNOWN_DECIMALS
SYMBOL_TO_MINT = {sym.upper(): mint for mint, sym in KNOWN_SYMBOLS.items()}

# Same account picker as escrow_transfer.py/cancel_offers.py/fill_offer.py —
# this script has no "right" account either, it depends which wallet you're
# swapping in.
KNOWN_LEDGER_ACCOUNTS: dict[str, tuple[str, str]] = {
    "1": ("44'/501'/0'", "Original / general strategy account"),
    "2": ("44'/501'/1'", "Targeted-offers account"),
    "3": ("44'/501'/2'", "Third wallet account"),
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("swap_tokens")

SESSION = requests.Session()


def resolve_token(tok: str) -> str:
    """Known symbol (case-insensitive) -> mint, else treated as a raw mint
    address directly (same fallback convention create_manual_offer.py's
    resolve_collateral_token uses, minus the allocation_config.yaml scrape —
    not relevant here, this isn't a lending-collateral pick)."""
    tok = tok.strip()
    return SYMBOL_TO_MINT.get(tok.upper(), tok)


def symbol_for(mint: str) -> str:
    return KNOWN_SYMBOLS.get(mint, f"{mint[:6]}…{mint[-4:]}" if mint else "?")


def decimals_for(mint: str) -> int | None:
    if mint in KNOWN_DECIMALS:
        return KNOWN_DECIMALS[mint]
    _, live_decimals = dc.fetch_current_prices_batched([mint])
    return live_decimals.get(mint)


def fetch_wallet_sol_balance(wallet: str) -> int:
    """Lamports — native SOL isn't an SPL token account, so this needs a
    plain getBalance call rather than dc.fetch_wallet_token_balance (which
    only covers SPL/Token-2022 token accounts)."""
    payload = {"jsonrpc": "2.0", "id": 1, "method": "getBalance", "params": [wallet]}
    resp = requests.post(SOLANA_RPC, json=payload, timeout=30)
    resp.raise_for_status()
    return resp.json().get("result", {}).get("value", 0)


def fetch_wallet_balance(wallet: str, mint: str) -> int:
    """Raw units, native-SOL-aware (see fetch_wallet_sol_balance)."""
    if mint == SOL_MINT:
        return fetch_wallet_sol_balance(wallet)
    return dc.fetch_wallet_token_balance(mint)


def fetch_quote(input_mint: str, output_mint: str, amount_raw: int, slippage_bps: int) -> dict:
    headers = {"x-api-key": JUPITER_API_KEY} if JUPITER_API_KEY else {}
    resp = SESSION.get(
        f"{JUPITER_SWAP_API}/quote",
        params={
            "inputMint": input_mint, "outputMint": output_mint,
            "amount": amount_raw, "slippageBps": slippage_bps,
        },
        headers=headers, timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def build_swap_tx(quote: dict, wallet: str) -> str:
    headers = {"x-api-key": JUPITER_API_KEY} if JUPITER_API_KEY else {}
    payload = {
        "quoteResponse": quote,
        "userPublicKey": wallet,
        "wrapAndUnwrapSol": True,
        "dynamicComputeUnitLimit": True,
        "prioritizationFeeLamports": "auto",
    }
    resp = SESSION.post(f"{JUPITER_SWAP_API}/swap", json=payload, headers=headers, timeout=20)
    if not resp.ok:
        raise requests.HTTPError(f"Jupiter /swap error {resp.status_code}: {resp.text}", response=resp)
    return resp.json()["swapTransaction"]


def prompt_float(label: str) -> float:
    while True:
        raw = input(f"{label}: ").strip()
        try:
            return float(raw)
        except ValueError:
            print("  Enter a number.")


def prompt_ledger_path() -> str:
    print()
    print("Which account do you want to swap in?")
    print()
    for key, (path, label) in KNOWN_LEDGER_ACCOUNTS.items():
        print(f"  [{key}]  {label}  ({path})")
    print(f"  [c]  Custom derivation path")
    print()
    while True:
        choice = input("Choice: ").strip().lower()
        if choice in KNOWN_LEDGER_ACCOUNTS:
            return KNOWN_LEDGER_ACCOUNTS[choice][0]
        if choice == "c":
            custom = input("Enter derivation path (e.g. \"44'/501'/2'\"): ").strip()
            if custom:
                return custom
            print("  Path can't be empty")
            continue
        print(f"  Please enter one of: {', '.join(KNOWN_LEDGER_ACCOUNTS)}, or 'c' for custom")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--from", dest="from_token", default="USDC", help="Token to swap FROM — symbol or mint address (default USDC).")
    parser.add_argument("--to", dest="to_token", default="SOL", help="Token to swap TO — symbol or mint address (default SOL).")
    parser.add_argument("--amount", type=float, default=None, help="Amount of --from to swap (whole tokens, e.g. 10 for 10 USDC). Omit to be prompted.")
    parser.add_argument("--slippage-bps", type=int, default=50, help="Max slippage in basis points (default 50 = 0.5%%).")
    parser.add_argument("--ledger-path", default=None, help="Ledger derivation path (skips the account prompt)")
    _common.add_signing_args(parser)
    args = parser.parse_args()

    from_mint = resolve_token(args.from_token)
    to_mint = resolve_token(args.to_token)
    from_decimals = decimals_for(from_mint)
    to_decimals = decimals_for(to_mint)
    if from_decimals is None:
        log.error("Could not resolve decimals for --from token %r.", args.from_token)
        sys.exit(1)
    if to_decimals is None:
        log.error("Could not resolve decimals for --to token %r.", args.to_token)
        sys.exit(1)

    amount = args.amount if args.amount is not None else prompt_float(
        f"Amount of {symbol_for(from_mint)} to swap (-> {symbol_for(to_mint)})"
    )
    if amount <= 0:
        log.error("--amount must be positive.")
        sys.exit(1)

    dc.SIGNING_MODE = _common.resolve_signing_mode(args.signing_mode, dc.SIGNING_MODE)
    if dc.SIGNING_MODE == "ledger":
        dc.LEDGER_PATH = args.ledger_path or prompt_ledger_path()
    if dc.SIGNING_MODE == "private_key" and not dc.PRIVATE_KEY_B58:
        log.error("OFFERBOOK_PRIVATE_KEY not set in .env — required for --private-key mode.")
        sys.exit(1)

    dc.resolve_signer_wallet()
    dc.confirm_signing_mode(skip_prompt=args.yes)

    amount_raw = round(amount * 10 ** from_decimals)
    wallet_balance_raw = fetch_wallet_balance(dc.WALLET_PUBKEY, from_mint)
    log.info(
        "%s balance: %s  (swapping %s)",
        symbol_for(from_mint), f"{wallet_balance_raw / 10 ** from_decimals:,.6f}", f"{amount:,.6f}",
    )
    if amount_raw > wallet_balance_raw:
        log.error(
            "Requested %s %s but wallet only has %s — nothing swapped.",
            f"{amount:,.6f}", symbol_for(from_mint), f"{wallet_balance_raw / 10 ** from_decimals:,.6f}",
        )
        sys.exit(1)

    log.info("Fetching quote …")
    quote = fetch_quote(from_mint, to_mint, amount_raw, args.slippage_bps)

    out_amount = int(quote["outAmount"]) / 10 ** to_decimals
    min_received = int(quote["otherAmountThreshold"]) / 10 ** to_decimals
    price_impact_pct = float(quote.get("priceImpactPct", 0)) * 100
    route = " -> ".join(dict.fromkeys(leg["swapInfo"]["label"] for leg in quote.get("routePlan", [])))

    log.info("=" * 70)
    log.info("SWAP PREVIEW — %s", dc.WALLET_PUBKEY)
    log.info("=" * 70)
    log.info("  Swap           : %s %s  ->  ~%s %s", f"{amount:,.6f}", symbol_for(from_mint),
              f"{out_amount:,.6f}", symbol_for(to_mint))
    log.info("  Min received   : %s %s  (at %.2f%% slippage tolerance)",
              f"{min_received:,.6f}", symbol_for(to_mint), args.slippage_bps / 100)
    log.info("  Price impact   : %.4f%%", price_impact_pct)
    log.info("  Route          : %s", route or "n/a")
    log.info("=" * 70)

    if not args.yes:
        choice = input("Proceed with this swap? [y/N] ").strip().lower()
        if choice not in ("y", "yes"):
            log.info("Aborted by user.")
            sys.exit(0)

    if DRY_RUN:
        log.info("[DRY RUN] Skipping transaction submission.")
        sys.exit(0)

    log.info("Building swap transaction …")
    tx_b64 = build_swap_tx(quote, dc.WALLET_PUBKEY)

    sig = dc.sign_and_send_transaction(tx_b64)
    if sig:
        log.info("✓ Submitted: https://solscan.io/tx/%s", sig)
        sys.exit(0)
    else:
        log.error("✗ Failed to submit transaction.")
        sys.exit(1)


if __name__ == "__main__":
    main()
