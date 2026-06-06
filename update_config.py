"""
update_config.py — Sync allocation_config.yaml with live Offerbook markets
===========================================================================
Fetches all currently active collateral tokens from the Offerbook API,
resolves their symbols and names, then:
  • Adds NEW tokens not yet in the YAML (allocation defaults to 0.0)
  • Updates the comment for any existing entry that still says "unknown"
    once the symbol has been resolved

Allocation values you have already set are NEVER changed.

Usage:
  python update_config.py           # apply changes
  python update_config.py --dry-run # preview only, no file write
"""
from __future__ import annotations

import argparse
import os
import re

import requests
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

API_BASE    = os.getenv("OFFERBOOK_API_BASE", "https://api.offerbook.jup.ag/api/v1")
USDC_MINT   = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
CONFIG_PATH = os.path.join(os.path.dirname(__file__), "allocation_config.yaml")

# ---------------------------------------------------------------------------
# Built-in symbol registry for well-known Solana tokens.
# Used as a fallback when the Offerbook /tokens API doesn't know a mint.
# Add any token you recognise here: mint -> (symbol, human-readable name)
# ---------------------------------------------------------------------------
KNOWN_TOKENS: dict[str, tuple[str, str]] = {
    "So11111111111111111111111111111111111111112":   ("SOL",     "Wrapped SOL"),
    "mSoLzYCxHdYgdzU16g5QSh3i5K3z3KZK7ytfqcJm7So": ("mSOL",    "Marinade staked SOL"),
    "bSo13r4TkiE4KumL71LsHTPpL2euBYLFx6h9HP3piy1": ("bSOL",    "BlazeStake staked SOL"),
    "jupSoLaHXQiZZTSfEWMTRRgpnyFm8f6sZdosWBjx93v": ("JupSOL",  "Jupiter staked SOL"),
    "J1toso1uCk3RLmjorhTtrVwY9HJ7X8V9yYac6Y7kGCPn":("jitoSOL", "Jito staked SOL"),
    "Bybit2vBJGhPF52GBdNaQfUJ6ZpThSgHBobjWZpLPb4B":("bbSOL",   "Bybit staked SOL"),
    "BNso1VUJnh4zcfpZa6986Ea66P6TCp59hvtNJ8b1X85": ("bnSOL",   "Binance staked SOL"),
    "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN": ("JUP",     "Jupiter"),
    "4k3Dyjzvzp8eMZWUXbBCjEvwSkkk59S5iCNLY3QrkX6R":("JLP",     "Jupiter Liquidity Pool"),
    "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263":("BONK",    "Bonk"),
    "hntyVP6YFm1Hg25TN9WGLqM12b8TQmcknKrdu1oxWux": ("HNT",     "Helium"),
    "nosXBVoaCTtYdLvKY6Csb4AC8JCdQKKAaWYtx2ZMoo7": ("NOS",     "Nosana"),
    "WENWENvqNAA8883GttHGFApfgzGLtzHain8QxAwYQst":  ("WEN",     "Wen"),
    "cbbtcf3aa214zXHbiAZQwf4122FBYbraNdFqgw4iMij":  ("cbBTC",   "Coinbase Wrapped BTC"),
    "kyKYFGGhy5YAg6Yotedj7ZtByUBepsraT4BFkF3Uxmk": ("kyKYROS", "kyKYROS"),
    "stke7uu3fXHsGqKVVjKnkmj65LRPVrqr4bLG2SJg7rh": ("stKE",    "Stakenet staked SOL"),
    "pumpCmXqMfrsAkQ5r49WcJnRayYRqmXz6ae8H7H9Dfn":  ("PUMP",    "pump.fun utility token"),
    "5z3EqYQo9HiCEs3R84RCDMu2n7anpDMxRhdK8PSWmrRC": ("SMRT",   "Streamflow SMRT"),
}

# ---------------------------------------------------------------------------
# API helpers
# ---------------------------------------------------------------------------

def _get(url: str, **params) -> dict | list:
    resp = requests.get(url, params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


def _fetch_all_pages(endpoint: str, **params) -> list[dict]:
    params["limit"] = 100
    params["offset"] = 0
    items: list[dict] = []
    while True:
        data = _get(f"{API_BASE}{endpoint}", **params)
        page = data.get("data", []) if isinstance(data, dict) else data
        items.extend(page)
        if not data.get("pagination", {}).get("hasMore"):
            break
        params["offset"] += 100
    return items


def fetch_offerbook_token_registry() -> dict[str, tuple[str, str]]:
    """Pull symbol + name from the Offerbook /tokens endpoint."""
    try:
        data = _get(f"{API_BASE}/tokens", limit=500)
        tokens = data.get("data", []) if isinstance(data, dict) else data
        return {t["mint"]: (t["symbol"], t.get("name", "")) for t in tokens if "mint" in t}
    except Exception as exc:
        print(f"  [warn] Could not fetch Offerbook token registry: {exc}")
        return {}


def resolve_symbol(mint: str, registry: dict[str, tuple[str, str]]) -> tuple[str, str]:
    """Return (symbol, name) for a mint, or ('???', 'unknown token')."""
    if mint in registry:
        return registry[mint]
    if mint in KNOWN_TOKENS:
        return KNOWN_TOKENS[mint]
    return ("???", "unknown token")


def fetch_active_collateral_mints() -> dict[str, float]:
    """
    Return {collateral_mint: median_ltv} for every pair active on Offerbook.
    LTV is computed from loan creation USD values (approximate but useful for
    labelling). Pairs without USD data get ltv=None stored as -1.
    """
    ltv_samples: dict[str, list[float]] = {}

    def _add(mint: str, pusd: float | None, cusd: float | None) -> None:
        if not mint or mint == USDC_MINT:
            return
        if pusd and cusd and cusd > 0:
            ltv_samples.setdefault(mint, []).append(pusd / cusd)
        else:
            ltv_samples.setdefault(mint, [])   # ensure mint is present

    print("Fetching active lending offers …")
    for offer in _fetch_all_pages("/offers", offerType="lending", status="active,partiallyFilled", hideExpired="true"):
        cmint = (offer.get("collateral") or {}).get("mint", "")
        meta  = offer.get("metadata") or {}
        _add(cmint, meta.get("principalAmountUsd"), meta.get("collateralAmountUsd"))

    print("Fetching active loans …")
    for loan in _fetch_all_pages("/loans/status/active"):
        cmint = loan.get("collateralMint") or (loan.get("collateral") or {}).get("mint", "")
        meta  = loan.get("metadata") or {}
        _add(cmint, meta.get("startPrincipalAmountUsd"), meta.get("startCollateralAmountUsd"))

    result: dict[str, float] = {}
    for mint, samples in ltv_samples.items():
        if samples:
            s = sorted(samples)
            result[mint] = round(s[len(s) // 2] * 100, 1)   # median as %
        else:
            result[mint] = -1.0   # sentinel: no USD data
    return result

# ---------------------------------------------------------------------------
# YAML file manipulation
# We edit the file as plain text so user comments and formatting are preserved.
# ---------------------------------------------------------------------------

# Matches an allocation line like:  So11111...: 1.0
_MINT_LINE_RE = re.compile(r"^  ([A-Za-z0-9]{30,})\s*:\s*(.+)$")


def load_file_lines(path: str) -> list[str]:
    with open(path) as fh:
        return fh.readlines()


def write_file_lines(path: str, lines: list[str]) -> None:
    with open(path, "w") as fh:
        fh.writelines(lines)


def build_mint_index(lines: list[str]) -> dict[str, int]:
    """Return {mint: line_index} for every allocation entry in the file."""
    index: dict[str, int] = {}
    for i, line in enumerate(lines):
        m = _MINT_LINE_RE.match(line)
        if m:
            index[m.group(1)] = i
    return index


def find_comment_line(lines: list[str], mint_line_idx: int) -> int | None:
    """Return the index of the comment line immediately above a mint line."""
    for i in range(mint_line_idx - 1, -1, -1):
        stripped = lines[i].strip()
        if stripped.startswith("#"):
            return i
        if stripped == "":
            continue
        break
    return None


def make_comment(symbol: str, name: str, ltv_pct: float) -> str:
    ltv_str = f"ltv ~{ltv_pct:.0f}%" if ltv_pct >= 0 else "ltv unknown"
    if symbol == "???":
        return f"  # ({ltv_str})  — unknown token\n"
    return f"  # {symbol} — {name}  ({ltv_str})\n"


def make_entry(mint: str, symbol: str, name: str, ltv_pct: float) -> list[str]:
    """Two lines: comment + allocation (defaulting to 0.0)."""
    return ["\n", make_comment(symbol, name, ltv_pct), f"  {mint}: 0.0\n"]


def find_default_line(lines: list[str]) -> int:
    """Return the index of the 'default:' line at the bottom of the file."""
    for i, line in enumerate(lines):
        if re.match(r"^default\s*:", line.strip()):
            return i
    return len(lines)   # fallback: append at end

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Sync allocation_config.yaml with live Offerbook markets")
    parser.add_argument("--dry-run", action="store_true", help="Preview changes without writing the file")
    args = parser.parse_args()

    print(f"Config file: {CONFIG_PATH}\n")

    # 1. Resolve token symbols
    print("Loading token registry …")
    registry = {**fetch_offerbook_token_registry(), **KNOWN_TOKENS}  # KNOWN_TOKENS wins on conflict
    print(f"  {len(registry)} tokens known\n")

    # 2. Fetch active collateral mints + their LTV
    active_mints = fetch_active_collateral_mints()
    print(f"\nFound {len(active_mints)} active collateral mints on Offerbook\n")

    # 3. Load the current config file
    lines = load_file_lines(CONFIG_PATH)
    mint_index = build_mint_index(lines)

    already_in_config  = set(mint_index.keys())
    new_mints          = {m for m in active_mints if m not in already_in_config}
    updatable_mints    = {}   # mint -> new comment line

    # 4. Find existing entries whose comment says "unknown" but we now know the symbol
    for mint, line_idx in mint_index.items():
        sym, name = resolve_symbol(mint, registry)
        if sym == "???":
            continue   # still unknown — nothing to update
        comment_idx = find_comment_line(lines, line_idx)
        if comment_idx is None:
            continue
        current_comment = lines[comment_idx]
        if "unknown" in current_comment.lower() or "???" in current_comment:
            ltv = active_mints.get(mint, -1.0)
            updatable_mints[mint] = (comment_idx, make_comment(sym, name, ltv))

    # 5. Report
    print("=" * 60)
    if updatable_mints:
        print(f"Comments to update ({len(updatable_mints)}):")
        for mint, (_, new_comment) in updatable_mints.items():
            old_idx = find_comment_line(lines, mint_index[mint])
            old_txt = lines[old_idx].strip() if old_idx is not None else "(none)"
            print(f"  {mint[:8]}…  {old_txt}  →  {new_comment.strip()}")
    else:
        print("No existing comments need updating.")

    print()
    if new_mints:
        print(f"New tokens to add ({len(new_mints)}):")
        for mint in sorted(new_mints, key=lambda m: active_mints.get(m, 0)):
            sym, name = resolve_symbol(mint, registry)
            ltv = active_mints.get(mint, -1.0)
            ltv_str = f"{ltv:.1f}%" if ltv >= 0 else "?"
            print(f"  {mint[:8]}…  {sym:20s}  {name}  (ltv ~{ltv_str})")
    else:
        print("No new tokens found — config is up to date.")
    print("=" * 60)

    if not updatable_mints and not new_mints:
        print("\nNothing to do.")
        return

    if args.dry_run:
        print("\n[DRY RUN] No changes written.")
        return

    # 6. Apply comment updates (iterate in reverse so line indices stay valid)
    for mint in sorted(updatable_mints, key=lambda m: mint_index[m], reverse=True):
        comment_idx, new_comment = updatable_mints[mint]
        lines[comment_idx] = new_comment

    # 7. Insert new tokens before the 'default:' line
    if new_mints:
        # Find insertion point: look for the 'default:' line, insert just above it
        # (walking backwards past any blank/comment lines)
        default_idx = find_default_line(lines)

        new_lines: list[str] = []
        new_lines.append("\n")
        new_lines.append("  # ---------------------------------------------------------------------------\n")
        new_lines.append("  # Newly discovered tokens (allocation defaults to 0.0 — set your % to enable)\n")
        new_lines.append("  # ---------------------------------------------------------------------------\n")

        for mint in sorted(new_mints, key=lambda m: active_mints.get(m, 0)):
            sym, name = resolve_symbol(mint, registry)
            ltv = active_mints.get(mint, -1.0)
            new_lines.extend(make_entry(mint, sym, name, ltv))

        lines = lines[:default_idx] + new_lines + lines[default_idx:]

    write_file_lines(CONFIG_PATH, lines)
    print(f"\nDone. Updated {CONFIG_PATH}")
    print(f"  {len(updatable_mints)} comment(s) updated, {len(new_mints)} token(s) added.")


if __name__ == "__main__":
    main()
