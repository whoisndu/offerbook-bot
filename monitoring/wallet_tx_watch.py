"""
Offerbook Wallet Transaction Watcher (GitHub Actions)
=======================================================
Polls a watchlist of Solana wallet addresses on a schedule (see
.github/workflows/wallet_watch.yml) and sends a Telegram alert the moment
any watched wallet has a new transaction — any transaction at all, not just
token transfers (unlike tg_deposit_watch.py, which only fires on USDC
balance increases and needs a live websocket connection to do it).

State (the watchlist + last-seen transaction signature per wallet + the
Telegram update offset) persists in wallet_watch_state.json, committed back
to the repo by the workflow after each run — same pattern as
loan_watch_notify.py / loan_watch_state.json.

Manage the watchlist two ways:

  1. CLI flags, from your machine:
       python wallet_tx_watch.py --watch <address> [--label <name>]
       python wallet_tx_watch.py --unwatch <address>
       python wallet_tx_watch.py --list

  2. Telegram commands, picked up on the next scheduled run (every ~15 min):
       /watch <address> [label]   - start watching a wallet
       /unwatch <address>         - stop watching a wallet
       /watchlist                 - show the current watchlist
       /help                      - show this command list
     (Deliberately named /watch, /unwatch, /watchlist rather than tg_deposit_watch.py's
     /add, /remove, /list — both scripts poll the SAME bot token, so distinct verbs
     mean a command meant for one script can't be misread by the other.)

Run with no flags to do one poll pass (checks Telegram commands, then
transactions) — this is what the scheduled workflow calls.

A wallet's FIRST poll after being watched never sends a notification for its
existing history — it just records the current newest signature as the
baseline, so watching a long-lived, active wallet doesn't flood you with
years of past transactions. Only transactions after that point are reported.

Required env vars (.env locally / GitHub Actions secrets):
  TELEGRAM_BOT_TOKEN  - from @BotFather
  TELEGRAM_CHAT_ID    - your chat id
  SOLANA_RPC          - optional, defaults to public mainnet RPC
"""
from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import base58
import requests
from dotenv import load_dotenv

load_dotenv()

SOLANA_RPC = os.getenv("SOLANA_RPC", "https://api.mainnet-beta.solana.com")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

STATE_PATH = Path(__file__).parent / "wallet_watch_state.json"

SIGNATURES_PER_WALLET = 20  # recent signatures fetched per poll — generous vs. a 15-min interval

SESSION = requests.Session()

HELP_TEXT = (
    "Commands:\n"
    "/watch <address> [label]  - start watching a wallet for new transactions\n"
    "/unwatch <address>        - stop watching a wallet\n"
    "/watchlist                - show the current watchlist\n"
    "/help                     - show this message\n\n"
    "Runs on a schedule (~every 15 min) — commands take effect on the next run."
)


def is_valid_pubkey(s: str) -> bool:
    """A Solana pubkey base58-decodes to exactly 32 bytes."""
    try:
        return len(base58.b58decode(s)) == 32
    except Exception:
        return False


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------

def load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return {}


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

def send_telegram(text: str) -> None:
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        print("[TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID not set — would have sent]:", text)
        return
    try:
        SESSION.post(f"{TELEGRAM_API}/sendMessage", json={"chat_id": TELEGRAM_CHAT_ID, "text": text}, timeout=10)
    except Exception as exc:
        print("Telegram send failed:", exc)


def fetch_telegram_updates(offset: int) -> list[dict]:
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        return []
    try:
        resp = SESSION.get(f"{TELEGRAM_API}/getUpdates", params={"offset": offset, "timeout": 0}, timeout=15)
        resp.raise_for_status()
        return resp.json().get("result", [])
    except Exception as exc:
        print("Telegram getUpdates failed:", exc)
        return []


# ---------------------------------------------------------------------------
# Solana
# ---------------------------------------------------------------------------

def fetch_recent_signatures(address: str, limit: int = SIGNATURES_PER_WALLET) -> list[dict]:
    """Newest-first list of {signature, slot, err, blockTime, memo}."""
    payload = {
        "jsonrpc": "2.0", "id": 1,
        "method": "getSignaturesForAddress",
        "params": [address, {"limit": limit}],
    }
    resp = SESSION.post(SOLANA_RPC, json=payload, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    if "error" in data:
        raise RuntimeError(f"RPC error for {address}: {data['error']}")
    return data.get("result") or []


def describe_tx(address: str, label: str | None, sig_info: dict) -> str:
    sig = sig_info["signature"]
    when = (
        datetime.fromtimestamp(sig_info["blockTime"], tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        if sig_info.get("blockTime") else "unknown time"
    )
    status = "FAILED" if sig_info.get("err") else "success"
    who = f"{address} ({label})" if label else address
    return (
        f"New transaction — {who}\n"
        f"  {when}  [{status}]\n"
        f"  https://solscan.io/tx/{sig}"
    )


# ---------------------------------------------------------------------------
# Core poll
# ---------------------------------------------------------------------------

def poll(state: dict) -> int:
    """Check every watched wallet for new signatures since last poll. Returns
    the number of notifications sent."""
    watchlist = state.setdefault("watchlist", {})
    last_seen = state.setdefault("last_seen", {})
    notified = 0

    for address, meta in watchlist.items():
        label = meta.get("label")
        try:
            sigs = fetch_recent_signatures(address)
        except Exception as exc:
            print(f"  {address}: fetch failed — {exc}")
            continue

        if not sigs:
            continue

        newest_sig = sigs[0]["signature"]
        prev_seen = last_seen.get(address)

        if prev_seen is None:
            # First time watching this wallet — baseline only, no notification.
            last_seen[address] = newest_sig
            print(f"  {address}: baseline set (no notification for existing history)")
            continue

        if prev_seen == newest_sig:
            continue  # nothing new

        # Find where prev_seen sits in the (newest-first) page; everything
        # before it is new. If it's not in this page at all, more than
        # SIGNATURES_PER_WALLET transactions happened since the last check —
        # report what we have rather than guessing further back.
        idx = next((i for i, s in enumerate(sigs) if s["signature"] == prev_seen), None)
        if idx is None:
            new_sigs = sigs
            print(f"  {address}: more than {SIGNATURES_PER_WALLET} new tx since last check — showing most recent")
        else:
            new_sigs = sigs[:idx]

        for sig_info in reversed(new_sigs):  # oldest new -> newest
            send_telegram(describe_tx(address, label, sig_info))
            notified += 1

        last_seen[address] = newest_sig

    return notified


# ---------------------------------------------------------------------------
# Watchlist management (shared by CLI flags and Telegram commands)
# ---------------------------------------------------------------------------

def add_wallet(state: dict, address: str, label: str | None) -> str:
    if not is_valid_pubkey(address):
        return f"{address!r} doesn't look like a valid wallet address."
    watchlist = state.setdefault("watchlist", {})
    if address in watchlist:
        return f"{address} is already on the watchlist."
    watchlist[address] = {"label": label, "added_at": datetime.now(timezone.utc).isoformat()}
    return (
        f"Added {address}{f' ({label})' if label else ''} to the watchlist. "
        "It'll be baselined (no notification for existing history) on the next run."
    )


def remove_wallet(state: dict, address: str) -> str:
    watchlist = state.setdefault("watchlist", {})
    if address not in watchlist:
        return f"{address} isn't on the watchlist."
    del watchlist[address]
    state.get("last_seen", {}).pop(address, None)
    return f"Removed {address} from the watchlist."


def list_wallets(state: dict) -> str:
    watchlist = state.get("watchlist", {})
    if not watchlist:
        return "Watchlist is empty."
    lines = [f"Watching {len(watchlist)} wallet(s):"]
    for addr, meta in watchlist.items():
        label = f" — {meta['label']}" if meta.get("label") else ""
        lines.append(f"  {addr}{label}")
    return "\n".join(lines)


def process_telegram_commands(state: dict) -> None:
    """Check for and apply any /watch, /unwatch, /watchlist, /help commands
    sent to the bot since the last processed update. Only commands from
    TELEGRAM_CHAT_ID are honored."""
    offset = state.get("telegram_offset", 0)
    updates = fetch_telegram_updates(offset)
    for update in updates:
        state["telegram_offset"] = update["update_id"] + 1
        msg = update.get("message") or {}
        chat_id = str(msg.get("chat", {}).get("id", ""))
        text = (msg.get("text") or "").strip()
        if not text or not text.startswith("/") or chat_id != str(TELEGRAM_CHAT_ID):
            continue

        parts = text.split()
        cmd = parts[0].lower()

        if cmd == "/watch" and len(parts) >= 2:
            send_telegram(add_wallet(state, parts[1], " ".join(parts[2:]) or None))
        elif cmd == "/unwatch" and len(parts) >= 2:
            send_telegram(remove_wallet(state, parts[1]))
        elif cmd == "/watchlist":
            send_telegram(list_wallets(state))
        elif cmd == "/help":
            send_telegram(HELP_TEXT)
        elif cmd in ("/watch", "/unwatch"):
            send_telegram(f"Usage: {cmd} <address> [label]" if cmd == "/watch" else f"Usage: {cmd} <address>")
        elif cmd in ("/add", "/remove", "/list"):
            continue  # almost certainly meant for tg_deposit_watch.py, not us — ignore silently
        else:
            send_telegram(f"Unknown command {cmd!r}.\n\n" + HELP_TEXT)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--watch", metavar="ADDRESS", help="Add a wallet to the watchlist")
    parser.add_argument("--label", default=None, help="Optional label for --watch")
    parser.add_argument("--unwatch", metavar="ADDRESS", help="Remove a wallet from the watchlist")
    parser.add_argument("--list", action="store_true", help="Print the current watchlist")
    args = parser.parse_args()

    if args.watch:
        state = load_state()
        print(add_wallet(state, args.watch, args.label))
        save_state(state)
        return

    if args.unwatch:
        state = load_state()
        print(remove_wallet(state, args.unwatch))
        save_state(state)
        return

    if args.list:
        print(list_wallets(load_state()))
        return

    # Default: one poll pass — this is what the scheduled workflow calls.
    state = load_state()
    process_telegram_commands(state)
    watchlist = state.get("watchlist", {})
    if watchlist:
        print(f"Checking {len(watchlist)} wallet(s) for new transactions…")
        notified = poll(state)
        print(f"Notifications sent: {notified}")
    else:
        print("Watchlist is empty — nothing to check. Use --watch or /watch to start.")
    save_state(state)


if __name__ == "__main__":
    main()
