#!/usr/bin/env python3
"""
Real-time Telegram alert the moment any watched wallet's token balance goes UP
(a deposit) — via live websocket subscriptions to Solana RPC (accountSubscribe),
not polling, so detection is near-instant.

Unlike loan_watch_notify.py, this is NOT unattended: it holds an open
websocket connection and only works while it's actually running. Meant to be
left running in a terminal (or tmux/screen) on your machine.

The watchlist (which wallets to watch) persists in tg_watchlist.json
(gitignored — it reveals which wallets you're targeting, so it's kept private
the same way defaulter_config.yaml is). You can manage it three ways:

  1. CLI flags (one-shot, no need to run the watcher):
       python tg_deposit_watch.py --add <wallet> [--mint <mint>] [--label <name>]
       python tg_deposit_watch.py --remove <wallet>
       python tg_deposit_watch.py --list

  2. Interactive console menu (run with no args):
       python tg_deposit_watch.py

  3. Telegram commands, live, WHILE the watcher is running (python tg_deposit_watch.py --watch):
       /add <wallet> [mint]   - start watching a wallet (mint optional, defaults to USDC)
       /remove <wallet>       - stop watching a wallet
       /list                  - show the current watchlist
       /help                  - show this command list

Required env vars (.env, gitignored):
  TELEGRAM_BOT_TOKEN  - from @BotFather
  TELEGRAM_CHAT_ID    - your chat id
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import base58
import requests
import websockets
from dotenv import load_dotenv

load_dotenv()

SOLANA_RPC = os.getenv("SOLANA_RPC", "https://api.mainnet-beta.solana.com")
SOLANA_WS = SOLANA_RPC.replace("https://", "wss://").replace("http://", "ws://")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

WATCHLIST_PATH = Path(__file__).parent / "tg_watchlist.json"

USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDC_DECIMALS = 6

SESSION = requests.Session()


KNOWN_SYMBOLS: dict[str, str] = {
    USDC_MINT: "USDC",
}


def symbol_for(mint: str) -> str:
    return KNOWN_SYMBOLS.get(mint, f"{mint[:6]}…{mint[-4:]}")


def format_entry(e: dict) -> str:
    label = f" — {e['label']}" if e.get("label") else ""
    return f"{e['wallet']}{label}  [{symbol_for(e['mint'])}]"


def is_valid_pubkey(s: str) -> bool:
    """A Solana pubkey base58-decodes to exactly 32 bytes. Rejects things like a
    plain-English label typed where a wallet/mint address was expected — e.g.
    "competitor" happens to be valid base58 text, but decodes to 8 bytes, not 32."""
    try:
        return len(base58.b58decode(s)) == 32
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Watchlist persistence
# ---------------------------------------------------------------------------

def load_watchlist() -> list[dict]:
    if WATCHLIST_PATH.exists():
        return json.loads(WATCHLIST_PATH.read_text())
    return []


def save_watchlist(entries: list[dict]) -> None:
    WATCHLIST_PATH.write_text(json.dumps(entries, indent=2))


def find_entry(entries: list[dict], wallet: str) -> dict | None:
    return next((e for e in entries if e["wallet"] == wallet), None)


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


# ---------------------------------------------------------------------------
# Solana
# ---------------------------------------------------------------------------

class RpcError(Exception):
    """Raised when the RPC call itself failed (rate limit, network blip, etc.) —
    distinct from a clean response confirming no token account exists."""


def get_token_account(owner: str, mint: str) -> tuple[str, int] | None:
    """Return (tokenAccountAddress, currentRawAmount), or None if no account exists yet.
    Raises RpcError if the call/response itself was bad, so callers don't mistake a
    transient RPC failure (this public endpoint rate-limits under load) for "no account"."""
    resp = SESSION.post(SOLANA_RPC, json={
        "jsonrpc": "2.0", "id": 1, "method": "getTokenAccountsByOwner",
        "params": [owner, {"mint": mint}, {"encoding": "jsonParsed"}],
    }, timeout=20)
    body = resp.json()
    if "error" in body or "result" not in body:
        raise RpcError(body.get("error", body))
    accounts = body["result"].get("value", [])
    if not accounts:
        return None
    acct = accounts[0]
    amount = int(acct["account"]["data"]["parsed"]["info"]["tokenAmount"]["amount"])
    return acct["pubkey"], amount


def get_token_account_with_retry(owner: str, mint: str, attempts: int = 3) -> tuple[str, int] | None:
    """Same as get_token_account, but retries on RpcError (transient RPC failure) — only
    a genuinely clean response saying "no account" is trusted after the first attempt."""
    last_exc: Exception | None = None
    for i in range(attempts):
        try:
            return get_token_account(owner, mint)
        except RpcError as exc:
            last_exc = exc
            print(f"RPC error looking up token account for {owner} (attempt {i + 1}/{attempts}): {exc}")
            if i < attempts - 1:
                time.sleep(1.5)
    raise last_exc  # exhausted retries — let the caller decide how to report this


# ---------------------------------------------------------------------------
# Console helpers
# ---------------------------------------------------------------------------

def print_watchlist(entries: list[dict]) -> None:
    if not entries:
        print("  (watchlist is empty)")
        return
    for e in entries:
        print(f"  {format_entry(e)}")


def cli_add(entries: list[dict], wallet: str, mint: str, decimals: int, label: str | None) -> list[dict]:
    if not is_valid_pubkey(wallet):
        print(f"{wallet!r} doesn't look like a valid wallet address.")
        return entries
    if not is_valid_pubkey(mint):
        print(f"{mint!r} doesn't look like a valid token mint address.")
        return entries
    if find_entry(entries, wallet):
        print(f"{wallet} is already on the watchlist.")
        return entries
    entries.append({"wallet": wallet, "mint": mint, "decimals": decimals, "label": label})
    save_watchlist(entries)
    print(f"Added {wallet} to the watchlist.")
    return entries


def cli_remove(entries: list[dict], wallet: str) -> list[dict]:
    if not find_entry(entries, wallet):
        print(f"{wallet} isn't on the watchlist.")
        return entries
    entries = [e for e in entries if e["wallet"] != wallet]
    save_watchlist(entries)
    print(f"Removed {wallet} from the watchlist.")
    return entries


def interactive_menu() -> list[dict]:
    entries = load_watchlist()
    while True:
        print("\n=== USDC Deposit Watcher ===")
        print(f"Currently watching {len(entries)} wallet(s):")
        print_watchlist(entries)
        print("\n1) Add one wallet")
        print("2) Add multiple wallets (comma-separated)")
        print("3) Remove a wallet")
        print("4) Start watching")
        print("0) Exit without starting")
        choice = input("> ").strip()

        if choice == "1":
            wallet = input("Wallet address: ").strip()
            label = input("Label (optional, press enter to skip): ").strip() or None
            if wallet:
                entries = cli_add(entries, wallet, USDC_MINT, USDC_DECIMALS, label)
        elif choice == "2":
            raw = input("Wallet addresses (comma-separated): ").strip()
            for wallet in [w.strip() for w in raw.split(",") if w.strip()]:
                entries = cli_add(entries, wallet, USDC_MINT, USDC_DECIMALS, None)
        elif choice == "3":
            wallet = input("Wallet address to remove: ").strip()
            entries = cli_remove(entries, wallet)
        elif choice == "4":
            if not entries:
                print("Watchlist is empty — add at least one wallet first.")
                continue
            return entries
        elif choice == "0":
            sys.exit(0)
        else:
            print("Not a valid option.")


# ---------------------------------------------------------------------------
# Live watch loop
# ---------------------------------------------------------------------------

HELP_TEXT = (
    "Commands:\n"
    "/add <wallet> [mint]  - start watching a wallet (mint optional, defaults to USDC)\n"
    "/remove <wallet>      - stop watching a wallet\n"
    "/list                 - show the current watchlist\n"
    "/help                 - show this message"
)


async def telegram_command_poller(action_queue: asyncio.Queue) -> None:
    """Long-polls Telegram for /add, /remove, /list, /help commands from the configured chat only."""
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        return  # nothing to poll without credentials
    offset = 0
    loop = asyncio.get_event_loop()
    while True:
        try:
            resp = await loop.run_in_executor(
                None,
                lambda: SESSION.get(f"{TELEGRAM_API}/getUpdates", params={"offset": offset, "timeout": 25}, timeout=30),
            )
            data = resp.json()
            for update in data.get("result", []):
                offset = update["update_id"] + 1
                msg = update.get("message") or {}
                chat_id = str(msg.get("chat", {}).get("id", ""))
                text = (msg.get("text") or "").strip()
                print(f"[telegram update] chat_id={chat_id} text={text!r}")
                if chat_id != str(TELEGRAM_CHAT_ID) or not text.startswith("/"):
                    continue
                parts = text.split()
                cmd = parts[0].split("@")[0].lower()
                if cmd == "/add":
                    if len(parts) < 2:
                        send_telegram("Usage: /add <wallet> [mint]  (mint is optional, defaults to USDC — labels aren't supported via Telegram, only via the console --label flag)")
                    elif not is_valid_pubkey(parts[1]):
                        send_telegram(f"{parts[1]!r} doesn't look like a valid wallet address.")
                    elif len(parts) >= 3 and not is_valid_pubkey(parts[2]):
                        send_telegram(f"{parts[2]!r} doesn't look like a valid token mint address. Usage: /add <wallet> [mint] — the second word must be a mint, not a label.")
                    else:
                        wallet = parts[1]
                        mint = parts[2] if len(parts) >= 3 else USDC_MINT
                        await action_queue.put(("add", wallet, mint))
                elif cmd == "/remove":
                    if len(parts) < 2:
                        send_telegram("Usage: /remove <wallet>")
                    else:
                        await action_queue.put(("remove", parts[1], None))
                elif cmd == "/list":
                    await action_queue.put(("list", None, None))
                elif cmd in ("/help", "/start"):
                    send_telegram(HELP_TEXT)
                else:
                    send_telegram(f"Unknown command {cmd!r}.\n\n" + HELP_TEXT)
        except Exception as exc:
            print(f"Telegram poll error ({exc}), retrying in 5s...")
            await asyncio.sleep(5)


async def solana_watch_loop(action_queue: asyncio.Queue, entries: list[dict]) -> None:
    """Holds one websocket connection, subscribed to every watched wallet's token account.
    Reacts to ('add'/'remove'/'list', ...) actions pushed onto action_queue by the Telegram
    poller (or by the initial watchlist) to (un)subscribe live, without restarting."""
    while True:
        try:
            async with websockets.connect(SOLANA_WS, ping_interval=20, ping_timeout=20) as ws:
                sub_id_to_wallet: dict[int, dict] = {}   # subscription id -> entry (with last_amount)
                next_req_id = 1
                req_id_to_wallet: dict[int, str] = {}    # pending accountSubscribe request id -> wallet (to map the sub id once confirmed)

                async def subscribe(entry: dict) -> bool:
                    """Returns True if actually subscribed. False means the caller should
                    NOT report success — either no account exists yet, or the RPC lookup
                    itself failed after retries (this public endpoint rate-limits under load)."""
                    nonlocal next_req_id
                    loop = asyncio.get_event_loop()
                    try:
                        found = await loop.run_in_executor(
                            None, lambda: get_token_account_with_retry(entry["wallet"], entry["mint"])
                        )
                    except RpcError as exc:
                        send_telegram(f"Couldn't check {entry['wallet']} right now (RPC error: {exc}) — will retry on the next reconnect.")
                        return False
                    if not found:
                        send_telegram(f"No {entry['mint'][:8]}… token account found yet for {entry['wallet']} — will keep it on the list, but can't watch until one exists.")
                        return False
                    token_account, amount = found
                    entry["_token_account"] = token_account
                    entry["_last_amount"] = amount
                    req_id = next_req_id
                    next_req_id += 1
                    req_id_to_wallet[req_id] = entry["wallet"]
                    await ws.send(json.dumps({
                        "jsonrpc": "2.0", "id": req_id, "method": "accountSubscribe",
                        "params": [token_account, {"encoding": "jsonParsed", "commitment": "confirmed"}],
                    }))
                    return True

                # Subscribe everything currently on the list.
                for entry in entries:
                    await subscribe(entry)
                print(f"Watching {len(entries)} wallet(s). Waiting for balance changes / Telegram commands...")

                async def drain_actions() -> None:
                    while True:
                        action, wallet, mint = await action_queue.get()
                        print(f"[command received] action={action} wallet={wallet}")
                        if action == "add":
                            if find_entry(entries, wallet):
                                send_telegram(f"{wallet} is already being watched.")
                                continue
                            entry = {"wallet": wallet, "mint": mint, "decimals": USDC_DECIMALS if mint == USDC_MINT else 0, "label": None}
                            entries.append(entry)
                            save_watchlist([{k: v for k, v in e.items() if not k.startswith("_")} for e in entries])
                            ok = await subscribe(entry)
                            if ok:
                                send_telegram(f"Now watching {wallet}.")
                        elif action == "remove":
                            entry = find_entry(entries, wallet)
                            if not entry:
                                send_telegram(f"{wallet} isn't on the watchlist.")
                                continue
                            sub_id = next((sid for sid, e in sub_id_to_wallet.items() if e["wallet"] == wallet), None)
                            if sub_id is not None:
                                await ws.send(json.dumps({
                                    "jsonrpc": "2.0", "id": next_req_id, "method": "accountUnsubscribe", "params": [sub_id],
                                }))
                                del sub_id_to_wallet[sub_id]
                            entries.remove(entry)
                            save_watchlist([{k: v for k, v in e.items() if not k.startswith("_")} for e in entries])
                            send_telegram(f"Stopped watching {wallet}.")
                        elif action == "list":
                            if not entries:
                                send_telegram("Watchlist is empty.")
                            else:
                                lines = [format_entry(e) for e in entries]
                                send_telegram("Currently watching:\n" + "\n".join(lines))

                drain_task = asyncio.create_task(drain_actions())

                try:
                    async for message in ws:
                        data = json.loads(message)

                        # Subscription confirmation: map request id -> subscription id -> wallet entry.
                        if "result" in data and isinstance(data.get("result"), int) and data.get("id") in req_id_to_wallet:
                            wallet = req_id_to_wallet.pop(data["id"])
                            entry = find_entry(entries, wallet)
                            if entry:
                                sub_id_to_wallet[data["result"]] = entry
                            continue

                        result = data.get("params", {}).get("result")
                        sub_id = data.get("params", {}).get("subscription")
                        if not result or sub_id not in sub_id_to_wallet:
                            continue
                        entry = sub_id_to_wallet[sub_id]
                        info = result["value"]["data"]["parsed"]["info"]["tokenAmount"]
                        new_amount = int(info["amount"])
                        last_amount = entry.get("_last_amount", new_amount)
                        if new_amount > last_amount:
                            decimals = entry.get("decimals", USDC_DECIMALS)
                            delta = (new_amount - last_amount) / 10**decimals
                            now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
                            sym = symbol_for(entry["mint"])
                            label = f" ({entry['label']})" if entry.get("label") else ""
                            msg = (
                                f"Deposit detected!\n"
                                f"Wallet: {entry['wallet']}{label}\n"
                                f"Amount: +{delta:,.2f} {sym}\n"
                                f"New balance: {new_amount / 10**decimals:,.2f} {sym}\n"
                                f"Time: {now}"
                            )
                            print(msg)
                            send_telegram(msg)
                        entry["_last_amount"] = new_amount
                finally:
                    drain_task.cancel()
        except Exception as exc:
            print(f"Websocket error ({exc}), reconnecting in 5s...")
            await asyncio.sleep(5)


async def run_watch(entries: list[dict]) -> None:
    action_queue: asyncio.Queue = asyncio.Queue()
    await asyncio.gather(
        telegram_command_poller(action_queue),
        solana_watch_loop(action_queue, entries),
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Real-time Telegram alert on token deposits to one or more wallets.")
    parser.add_argument("--add", metavar="WALLET", help="Add a wallet to the watchlist and exit")
    parser.add_argument("--mint", default=USDC_MINT, help="Token mint for --add (default: USDC)")
    parser.add_argument("--decimals", type=int, default=USDC_DECIMALS, help="Decimals for --mint (default: 6, USDC)")
    parser.add_argument("--label", default=None, help="Optional label for --add")
    parser.add_argument("--remove", metavar="WALLET", help="Remove a wallet from the watchlist and exit")
    parser.add_argument("--list", action="store_true", help="Print the current watchlist and exit")
    parser.add_argument("--watch", action="store_true", help="Start watching the saved watchlist (long-running)")
    args = parser.parse_args()

    entries = load_watchlist()

    if args.add:
        cli_add(entries, args.add, args.mint, args.decimals, args.label)
        return
    if args.remove:
        cli_remove(entries, args.remove)
        return
    if args.list:
        print_watchlist(entries)
        return
    if args.watch:
        if not entries:
            print("Watchlist is empty — add a wallet first with --add <wallet>, or run with no flags for the interactive menu.")
            sys.exit(1)
        try:
            asyncio.run(run_watch(entries))
        except KeyboardInterrupt:
            pass
        return

    # No flags: interactive console menu, then start watching.
    entries = interactive_menu()
    try:
        asyncio.run(run_watch(entries))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
