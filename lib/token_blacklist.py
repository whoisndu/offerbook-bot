"""
Offerbook Collateral Blacklist
=================================
Shared blacklist checked by the strategy scripts that decide on their own
whether/how to post a NEW lending offer (strategy.py, fixed_terms_offers.py,
defaulter_capture.py) before they ever price or size one against a given
collateral mint.

Deliberately NOT consulted by create_targeted_offers.py, create_manual_offer.py,
or fill_offer.py — those always act on one specific pair/offer you've
already chosen by hand, and using one of them on a blacklisted pair IS the
deliberate override (see each script's own docstring).

Edit ../strategy/blacklist_config.yaml to add/remove tokens — no code
change needed, every caller re-reads it at startup (see load_blacklist).
"""
from __future__ import annotations

import os

import yaml

_CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "strategy", "blacklist_config.yaml",
)


def load_blacklist(path: str = _CONFIG_PATH) -> set[str]:
    """Mint addresses listed under `blacklist:` in the yaml at `path`.
    Returns an empty set (not an error) if the file is missing — an absent
    blacklist means nothing is blacklisted, same convention as
    allocation_config.yaml's FileNotFoundError handling elsewhere."""
    try:
        with open(path) as fh:
            raw = yaml.safe_load(fh) or {}
    except FileNotFoundError:
        return set()
    return {str(m) for m in (raw.get("blacklist") or [])}


def is_blacklisted(mint: str, blacklist: set[str] | None = None) -> bool:
    """`blacklist` lets a caller load once (e.g. at startup) and check many
    mints without re-reading the file each time; omit it to load fresh."""
    if blacklist is None:
        blacklist = load_blacklist()
    return mint in blacklist
