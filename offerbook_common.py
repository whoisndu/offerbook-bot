"""
Offerbook Shared Helpers
=========================
Small helpers that were duplicated verbatim (or near-verbatim) across the
standalone scripts in this repo: Ledger wallet resolution/confirmation, and
OfferAsset mint extraction.

Each script keeps its own config as module globals (SIGNING_MODE,
WALLET_PUBKEY, LEDGER_PATH, DRY_RUN) — these functions are stateless and take
that config as arguments rather than reading another module's globals. Where
a script is itself imported elsewhere for its signing helpers (e.g.
create_targeted_offers.py calling defaulter_capture.resolve_signer_wallet()
with no arguments), that script keeps a thin zero-arg wrapper around the
functions here so its existing call sites don't need to change.
"""
from __future__ import annotations

import argparse
import logging
import sys
import time

import requests

log = logging.getLogger("offerbook_common")

_ledger_signer_cache: dict[str, object] = {}


def get_ledger_signer(ledger_path: str):
    """Lazily construct (and cache per-path) a LedgerSigner. Exits on missing deps."""
    if ledger_path in _ledger_signer_cache:
        return _ledger_signer_cache[ledger_path]
    try:
        from ledger_signer import LedgerSigner
    except ImportError:
        log.error("Missing dependencies: pip install ledgerblue hidapi base58")
        sys.exit(1)
    signer = LedgerSigner(path=ledger_path)
    _ledger_signer_cache[ledger_path] = signer
    return signer


def resolve_signer_wallet(signing_mode: str, wallet_pubkey: str, ledger_path: str) -> str:
    """
    Resolve the active wallet pubkey for `signing_mode`. For Ledger, the
    device is the source of truth (overrides/fills in `wallet_pubkey`); for
    private-key mode, `wallet_pubkey` is returned as-is.
    """
    if signing_mode != "ledger":
        return wallet_pubkey

    from ledger_signer import LedgerError

    try:
        device_pubkey = get_ledger_signer(ledger_path).get_pubkey()
    except LedgerError as exc:
        log.error(str(exc))
        sys.exit(1)
    if wallet_pubkey and wallet_pubkey != device_pubkey:
        log.warning(
            "OFFERBOOK_WALLET (%s) does not match the Ledger address (%s) at path %s — using the Ledger address.",
            wallet_pubkey, device_pubkey, ledger_path,
        )
    return device_pubkey


def confirm_signing_mode(
    signing_mode: str, wallet_pubkey: str, ledger_path: str, dry_run: bool, skip_prompt: bool,
) -> None:
    """Print the resolved signing mode/wallet and ask for confirmation unless skip_prompt."""
    label = "LEDGER (hardware wallet)" if signing_mode == "ledger" else "PRIVATE KEY (.env hot wallet)"
    log.info("=" * 60)
    log.info("  Signing mode : %s", label)
    log.info("  Wallet       : %s", wallet_pubkey)
    if signing_mode == "ledger":
        log.info("  Derivation   : %s", ledger_path)
    log.info("  Dry run      : %s", dry_run)
    log.info("=" * 60)
    if skip_prompt:
        return
    choice = input("Continue with this signing mode? [y/N] ").strip().lower()
    if choice not in ("y", "yes"):
        log.info("Aborted by user.")
        sys.exit(0)


def _mint_from_asset(asset: dict) -> str:
    """Extract mint address from an OfferAsset (direct mint field or legacy nested data field)."""
    return asset.get("mint") or asset.get("data", {}).get("mint") or asset.get("data", {}).get("asset") or ""


def _volume_weighted_median(values: list[float], weights: list[float]) -> float | None:
    """Volume-weighted median: the value at which cumulative weight first reaches
    half of total weight — the price level where the largest cluster of real
    market volume sits. Robust to a single large outlier value, unlike a
    volume-weighted mean, which that one outlier can drag far from where most
    trading actually happens."""
    if not values:
        return None
    if not weights or len(weights) != len(values) or sum(weights) <= 0:
        s = sorted(values)
        return s[len(s) // 2]
    pairs = sorted(zip(values, weights), key=lambda p: p[0])
    half = sum(w for _, w in pairs) / 2
    cumulative = 0.0
    for value, weight in pairs:
        cumulative += weight
        if cumulative >= half:
            return value
    return pairs[-1][0]


# Canonical decimals/symbol tables for every collateral token this bot has
# ever traded or watched. This is the union of what used to be six separately
# maintained copies (some subsets, some with extra tokens) — see each mint's
# comment for context. Add new tokens here, not in an individual script.
KNOWN_DECIMALS: dict[str, int] = {
    "So11111111111111111111111111111111111111112": 9,   # SOL
    "mSoLzYCxHdYgdzU16g5QSh3i5K3z3KZK7ytfqcJm7So": 9,     # mSOL
    "bSo13r4TkiE4KumL71LsHTPpL2euBYLFx6h9HP3piy1": 9,     # bSOL
    "jupSoLaHXQiZZTSfEWMTRRgpnyFm8f6sZdosWBjx93v": 9,     # JupSOL
    "J1toso1uCk3RLmjorhTtrVwY9HJ7X8V9yYac6Y7kGCPn": 9,    # jitoSOL
    "Bybit2vBJGhPF52GBdNaQfUJ6ZpThSgHBobjWZpLPb4B": 9,    # bbSOL
    "BNso1VUJnh4zcfpZa6986Ea66P6TCp59hvtNJ8b1X85": 9,     # bnSOL
    "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN": 6,     # JUP
    "4k3Dyjzvzp8eMZWUXbBCjEvwSkkk59S5iCNLY3QrkX6R": 6,    # JLP
    "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263": 5,    # BONK
    "hntyVP6YFm1Hg25TN9WGLqM12b8TQmcknKrdu1oxWux": 8,     # HNT
    "nosXBVoaCTtYdLvKY6Csb4AC8JCdQKKAaWYtx2ZMoo7": 6,     # NOS
    "WENWENvqNAA8883GttHGFApfgzGLtzHain8QxAwYQst": 5,     # WEN
    "cbbtcf3aa214zXHbiAZQwf4122FBYbraNdFqgw4iMij": 8,     # cbBTC
    "kyKYFGGhy5YAg6Yotedj7ZtByUBepsraT4BFkF3Uxmk": 6,     # kyKYROS
    "stke7uu3fXHsGqKVVjKnkmj65LRPVrqr4bLG2SJg7rh": 9,     # stKE
    "pumpCmXqMfrsAkQ5r49WcJnRayYRqmXz6ae8H7H9Dfn": 6,     # PUMP
    "5z3EqYQo9HiCEs3R84RCDMu2n7anpDMxRhdK8PSWmrRC": 9,    # SMRT
    "Dz9mQ9NzkBcCsuGPFJ3r1bS4wgqKMHBPiVuniW8Mbonk": 6,    # USELESS
    "Ce2gx9KGXJ6C9Mp5b5x1sn9Mg87JwEbrQby4Zqo3pump": 6,    # NEET
    "A7bdiYdS5GjqGFtxf17ppRHtDKPkkRqbKtR27dxvQXaS": 8,    # ZEC
    "98sMhvDwXj1RQi5c5Mndm3vPe9cBqPrbLaufMXFNMh5g": 9,    # HYPE
    "TUNAfXDZEdQizTMTh3uEvNvYqJmqFHZbEJt8joP4cyx": 6,     # TUNA — previously only in defaulter_capture.py
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v": 6,    # USDC
}

# Display symbols for the same set of mints, for scripts that print friendly
# token names (defaulter_watch.py's symbol_for(), soon_to_expire.py's
# fmt_amount()) instead of/alongside a raw decimals figure.
KNOWN_SYMBOLS: dict[str, str] = {
    "So11111111111111111111111111111111111111112": "SOL",
    "mSoLzYCxHdYgdzU16g5QSh3i5K3z3KZK7ytfqcJm7So": "mSOL",
    "bSo13r4TkiE4KumL71LsHTPpL2euBYLFx6h9HP3piy1": "bSOL",
    "jupSoLaHXQiZZTSfEWMTRRgpnyFm8f6sZdosWBjx93v": "JupSOL",
    "J1toso1uCk3RLmjorhTtrVwY9HJ7X8V9yYac6Y7kGCPn": "jitoSOL",
    "Bybit2vBJGhPF52GBdNaQfUJ6ZpThSgHBobjWZpLPb4B": "bbSOL",
    "BNso1VUJnh4zcfpZa6986Ea66P6TCp59hvtNJ8b1X85": "bnSOL",
    "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN": "JUP",
    "4k3Dyjzvzp8eMZWUXbBCjEvwSkkk59S5iCNLY3QrkX6R": "JLP",
    "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263": "BONK",
    "hntyVP6YFm1Hg25TN9WGLqM12b8TQmcknKrdu1oxWux": "HNT",
    "nosXBVoaCTtYdLvKY6Csb4AC8JCdQKKAaWYtx2ZMoo7": "NOS",
    "WENWENvqNAA8883GttHGFApfgzGLtzHain8QxAwYQst": "WEN",
    "cbbtcf3aa214zXHbiAZQwf4122FBYbraNdFqgw4iMij": "cbBTC",
    "kyKYFGGhy5YAg6Yotedj7ZtByUBepsraT4BFkF3Uxmk": "kyKYROS",
    "stke7uu3fXHsGqKVVjKnkmj65LRPVrqr4bLG2SJg7rh": "stKE",
    "pumpCmXqMfrsAkQ5r49WcJnRayYRqmXz6ae8H7H9Dfn": "PUMP",
    "5z3EqYQo9HiCEs3R84RCDMu2n7anpDMxRhdK8PSWmrRC": "SMRT",
    "Dz9mQ9NzkBcCsuGPFJ3r1bS4wgqKMHBPiVuniW8Mbonk": "USELESS",
    "Ce2gx9KGXJ6C9Mp5b5x1sn9Mg87JwEbrQby4Zqo3pump": "NEET",
    "A7bdiYdS5GjqGFtxf17ppRHtDKPkkRqbKtR27dxvQXaS": "ZEC",
    "98sMhvDwXj1RQi5c5Mndm3vPe9cBqPrbLaufMXFNMh5g": "HYPE",
    "TUNAfXDZEdQizTMTh3uEvNvYqJmqFHZbEJt8joP4cyx": "TUNA",
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v": "USDC",
}


def api_get(session: requests.Session, api_base: str, endpoint: str, params: dict | None = None) -> dict:
    resp = session.get(f"{api_base}{endpoint}", params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


def post_tx(session: requests.Session, tx_api_base: str, endpoint: str, payload: dict) -> dict:
    resp = session.post(f"{tx_api_base}{endpoint}", json=payload, timeout=30)
    if not resp.ok:
        # Read body before raise_for_status consumes it
        try:
            detail = resp.json().get("message", resp.text)
        except Exception:
            detail = resp.text or "(empty)"
        resp.reason = detail  # attach to the exception message
        resp.raise_for_status()
    return resp.json()


def fetch_all_pages(
    session: requests.Session,
    api_base: str,
    endpoint: str,
    params: dict | None = None,
    page_size: int = 100,
    sleep_secs: float = 0.15,
) -> list[dict]:
    """Auto-paginate through all pages of a paginated Offerbook endpoint."""
    params = dict(params or {})
    params["limit"] = page_size
    params["offset"] = 0
    items: list[dict] = []
    while True:
        data = api_get(session, api_base, endpoint, params)
        items.extend(data.get("data", []))
        if not data.get("pagination", {}).get("hasMore", False):
            break
        params["offset"] += page_size
        time.sleep(sleep_secs)   # be polite to the API
    return items


def add_signing_args(
    parser: argparse.ArgumentParser,
    yes_help: str = "Skip the signing-mode confirmation prompt",
) -> None:
    """Add the shared --ledger/--private-key/--yes signing-mode CLI flags to `parser`."""
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--ledger", action="store_const", dest="signing_mode", const="ledger",
        help="Sign with a Ledger hardware wallet (default)",
    )
    mode_group.add_argument(
        "--private-key", action="store_const", dest="signing_mode", const="private_key",
        help="Sign with OFFERBOOK_PRIVATE_KEY from .env",
    )
    parser.add_argument("--yes", "-y", action="store_true", help=yes_help)


def resolve_signing_mode(cli_signing_mode: str | None, current_signing_mode: str) -> str:
    """Apply an optional --ledger/--private-key CLI override and validate the result."""
    signing_mode = cli_signing_mode or current_signing_mode
    if signing_mode not in ("ledger", "private_key"):
        log.error("Invalid signing mode %r — must be 'ledger' or 'private_key'", signing_mode)
        sys.exit(1)
    return signing_mode
