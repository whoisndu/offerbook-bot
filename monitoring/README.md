# Monitoring scripts

Watchers and scanners. Most run unattended via `.github/workflows/`; a couple (`tg_deposit_watch.py`) are meant to be left running in a terminal instead. All read `../lib/offerbook_common.py` for shared helpers. See the [root README](../README.md) for setup, environment variables, and the overall repo layout.

## Expiring/overdue loan scanner (`soon_to_expire.py`)

Scans all active loans **platform-wide** and surfaces the ones already past their `expiredAt` (still marked "active" — not yet repaid or defaulted) or expiring within a configurable window. Read-only, no signing.

```bash
# Default: next 48h + already-expired bucket
python monitoring/soon_to_expire.py

# Next 24h + already-expired
python monitoring/soon_to_expire.py --hours 24

# Only the soon-to-expire window, skip the already-expired bucket
python monitoring/soon_to_expire.py --no-expired
```

Exit code `1` if anything is in the expired/soon-to-expire window, `0` otherwise. `loan_watch_notify.py` (below) automates the "already expired" half of this on a schedule via email; this script is for an ad-hoc/manual look, including the "expiring soon but not yet due" window that the email watcher doesn't cover.

## Collateral-coverage watchlist (`defaulter_watch.py`)

A read-only analytics scanner over the platform's full loan history (defaulted and repaid), used to identify borrowers whose positions have historically been fully covered by collateral value from a lender's perspective — a more direct signal of downside risk than repayment punctuality alone. It combines two signals per borrower:

- **Defaulted loans** where collateral value at default exceeded the outstanding principal (full recovery for whoever held the loan).
- **Repaid loans that closed after their `expiredAt`** (a late repayment the original lender chose not to enforce) where collateral value also exceeded principal at the time — i.e. the lender's capital was covered throughout regardless of repayment timing.

```bash
# Any borrower with positive historical collateral coverage
python monitoring/defaulter_watch.py

# Only borrowers with more than $100 in aggregate historical surplus
python monitoring/defaulter_watch.py --min-surplus 100

# Limit the reference table to the top 15 rows
python monitoring/defaulter_watch.py --top 15
```

For each watchlisted borrower, the report flags two actionable conditions: an open borrow request right now, or an active loan expiring within 24h (they may return to borrow again). Never signs or submits anything — meant to be run periodically to catch these while they're still relevant. Exit code `1` if either condition applies to any watchlisted borrower, `0` otherwise.

It also surfaces first-time borrowers who have no resolved default/late-repay history yet but already have a loan sitting overdue right now — a signal the historical-surplus watchlist alone can't catch, since it only looks at loans that have already resolved. Every borrower who qualifies either way is upserted into `defaulter_config.yaml`, a private, ever-growing tracking ledger (first-seen date, defaults, late repayments, known surplus) that's gitignored and never committed — a personal risk record, not something published alongside the strategy code.

`../strategy/defaulter_capture.py` reacts to this script's actionable output directly (`from defaulter_watch import ...`) — see [strategy/README.md](../strategy/README.md#automated-capture-defaulter_capturepy).

## Loan expiry watch (`loan_watch_notify.py`)

A platform-wide (not just our own wallet) email notifier for loans going overdue and later resolving. Runs every 30 minutes via `.github/workflows/loan_watch.yml` — GitHub Actions, not a local process, so it keeps running whether or not any machine is on — free on a public repo regardless of frequency. GitHub's own scheduler can run a bit behind during platform-wide load, so "every 30 minutes" is a target, not a hard guarantee.

For every currently active loan, it checks whether the loan is past its `expiredAt` and, if so, whether the collateral is **currently** worth more than what's owed (principal + accrued interest), using live Jupiter/DexScreener prices — not the values at origination. Only loans clearing that bar get an email and get tracked; loans without a usable price feed (mostly NFT collateral) or without surplus are left untracked and re-checked every run, since surplus can emerge later as prices move. A tracked loan then gets a second email the moment it resolves, repaid or defaulted, with exactly how late/early that was.

State lives in `loan_watch_state.json` (committed back to the repo by the workflow itself) so the same loan is never emailed twice for the same event — it holds only public on-chain data (pubkeys, addresses, amounts), never anything sensitive.

Required GitHub Actions secrets (set via `gh secret set`, never committed):

```
SMTP_FROM_EMAIL     - Gmail address to send from
SMTP_APP_PASSWORD   - Gmail App Password for that address
NOTIFY_EMAIL_TO     - recipient address
```

```bash
# Manual local run (uses the same env vars, or logs "skipping email" if unset)
python monitoring/loan_watch_notify.py

# Manually trigger the GitHub Actions workflow instead of waiting for its schedule
gh workflow run loan_watch.yml
```

## New borrow-request watch (`borrow_offer_watch.py`)

Emails on every newly-appearing open borrow request platform-wide — every principal, every collateral type (including NFTs), no profitability filter. This is the raw feed; for "is this one worth acting on" see `arbitrage_scanner.py` below, which only alerts on borrow requests that clear a profitable spread against a live lending offer for the same collateral.

Runs every 15 minutes via `.github/workflows/borrow_offer_watch.yml`. Dedup works like `arbitrage_scanner.py`'s: state is the set of currently-open borrow-offer pubkeys, persisted to `borrow_offer_watch_state.json` (committed back to the repo by the workflow — these are public open offers, not competitive intel, so unlike `../reporting/competitor_timing_state.json` there's nothing here worth keeping private). Each run only emails offers not already in that set, then overwrites the state with exactly this run's live set — anything no longer open (filled, cancelled, expired) simply stops appearing next run.

```bash
python monitoring/borrow_offer_watch.py                        # all open borrow requests, email if new ones found
python monitoring/borrow_offer_watch.py --min-size 20           # ignore requests under $20 principal
python monitoring/borrow_offer_watch.py --principal-mint <mint> # only this principal token
python monitoring/borrow_offer_watch.py --no-email              # console output only, skip email + state
```

Required env vars for email (GitHub Actions secrets, shared with `loan_watch_notify.py`): `SMTP_FROM_EMAIL`, `SMTP_APP_PASSWORD`, `NOTIFY_EMAIL_TO`. `OFFERBOOK_WALLET` is used to exclude our own borrow requests, if any — a warning is logged (not skipped) if unset, same as the other scan scripts.

## Wallet transaction watch (`wallet_tx_watch.py`)

Polls a watchlist of arbitrary Solana addresses via `.github/workflows/wallet_watch.yml` (every ~15 min) and sends a Telegram alert on **any** new transaction for a watched wallet — not just token transfers, unlike `tg_deposit_watch.py` below, and unattended, unlike it too.

State (the watchlist + last-seen transaction signature per wallet + a Telegram update offset) lives in `wallet_watch_state.json`, committed back by the workflow — same pattern as `loan_watch_state.json`. A wallet's first poll after being watched only records a baseline (no notification for its pre-existing history), so watching a long-lived active wallet doesn't flood you with years of past transactions.

Manage the watchlist two ways — from your machine, or live from Telegram (commands land on the next scheduled run, so there's up to ~15 min latency):

```bash
python monitoring/wallet_tx_watch.py --watch <address> [--label <name>]
python monitoring/wallet_tx_watch.py --unwatch <address>
python monitoring/wallet_tx_watch.py --list
```

```
/watch <address> [label]   - start watching a wallet
/unwatch <address>         - stop watching a wallet
/watchlist                 - show the current watchlist
/help                      - show this command list
```

Deliberately named `/watch`/`/unwatch`/`/watchlist` rather than `tg_deposit_watch.py`'s `/add`/`/remove`/`/list` — both scripts poll the *same* Telegram bot token, so a command meant for one can't be misread by the other (verified: a stray `/add` or `/list` is silently ignored here rather than misfiring).

Required env vars (GitHub Actions secrets, shared with `tg_deposit_watch.py` below):

```
TELEGRAM_BOT_TOKEN  - from @BotFather
TELEGRAM_CHAT_ID    - your chat id
```

## Same-token arbitrage scanner (`arbitrage_scanner.py`)

The platform's own "Spread" stat (Best Lend APY − Best Borrow APY) mixes completely different collateral quality tiers — e.g. 9% to borrow against a blue-chip token vs. 90% to lend against an illiquid one. That's not a capturable arbitrage, just the market's risk curve. This scans for the real thing: **fungible tokens** where you could borrow cheaply (a live lending offer, low APY) and simultaneously lend into an existing borrow request for that *same* token at a materially higher APY.

```bash
python monitoring/arbitrage_scanner.py                  # top 15 spreads, email if new ones found
python monitoring/arbitrage_scanner.py --top 30
python monitoring/arbitrage_scanner.py --min-spread 20   # only spreads >= 20 APY points
python monitoring/arbitrage_scanner.py --min-size 50     # ignore legs under $50 available
python monitoring/arbitrage_scanner.py --no-email        # console output only
```

Runs every ~15 min via `.github/workflows/arbitrage_scan.yml`. Emails (reusing `loan_watch_notify.py`'s SMTP secrets) only for **newly appearing** spreads — deduped by the exact `(borrow-offer, lend-offer)` pubkey pair in `arbitrage_scanner_state.json` (committed back by the workflow), so a still-open opportunity doesn't re-email every run; state resets to exactly what's currently live each run, so filled/cancelled/expired offers drop out automatically.

A flagged spread is a market scan, not a recommendation — read it with the same caveats it prints: size at the best rate is often small, the two legs' durations may not line up, and a high lend-side APY usually exists because that specific token/position carries real default risk, not because it's mispriced.

## Real-time deposit watch (`tg_deposit_watch.py`)

Unlike everything else above, this one is **not unattended**. It holds a live websocket subscription to Solana RPC (`accountSubscribe`) so a deposit to a watched wallet triggers a Telegram message within seconds — genuinely real-time, not polling — but only while it's actually running on your machine. Meant to be left open in a terminal (or `tmux`/`screen`) rather than deployed anywhere.

Watches any number of wallets at once. The watchlist persists in `tg_watchlist.json` (gitignored — reveals which wallets you're targeting, kept private the same way `defaulter_config.yaml` is) and can be managed three ways:

```bash
# One-shot CLI (no need to have the watcher running)
python monitoring/tg_deposit_watch.py --add <wallet> [--mint <mint>] [--label <name>]
python monitoring/tg_deposit_watch.py --remove <wallet>
python monitoring/tg_deposit_watch.py --list

# Interactive console menu
python monitoring/tg_deposit_watch.py

# Start the live watcher (long-running)
python monitoring/tg_deposit_watch.py --watch
```

While `--watch` is running, the bot also takes live commands sent to it on Telegram, so you can manage the watchlist from your phone without touching a terminal:

```
/add <wallet> [mint]   - start watching a wallet (mint optional, defaults to USDC)
/remove <wallet>       - stop watching a wallet
/list                  - show the current watchlist
/help                  - show this command list
```

Every added/removed wallet is validated as a real Solana pubkey (base58-decodes to exactly 32 bytes) before being accepted — a plain-English label typed where an address was expected (e.g. trying `/add <wallet> mylabel`) gets rejected with an error rather than silently treated as a token mint.

Required env vars (`.env`, gitignored):

```
TELEGRAM_BOT_TOKEN  - from @BotFather
TELEGRAM_CHAT_ID    - your chat id (message your bot once, then check https://api.telegram.org/bot<TOKEN>/getUpdates for "chat":{"id":...})
```

## Underwater position scanner (`underwater.py`)

Gitignored — see [root README's Security section](../README.md#security). Scans all open borrow offers and active loans for positions where LTV exceeds a given threshold at current market prices, using the same live-price/decimals machinery as everything else here.
