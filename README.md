# Offerbook Competitive Lending Bot

An automated lending bot for the [Offerbook](https://offerbook.jup.ag) protocol on Solana. It scans active lending offers, posts competitive lending offers sized to your per-collateral allocation, and sizes collateral against a **dynamic, per-token LTV target** using real-time prices from Jupiter and DexScreener.

This file is a summary and index. Each subfolder has its own README with full detail on the scripts it contains.

## Repository layout

Scripts are grouped by what they do, so every `python <folder>/<script>.py` command in this repo is a repo-root-relative path:

- **[`lib/`](lib/README.md)** — shared modules, never run directly: `offerbook_common.py` (helpers used by nearly everything), `ledger_signer.py` (Ledger hardware wallet signing), `google_calendar_client.py` (OAuth wrapper for `portfolio_health.py`'s reminder sync).
- **[`strategy/`](strategy/README.md)** — scripts that place, fill, or cancel live offers: `strategy.py` (the main bot — see its README for the full pricing/risk math), `defaulter_capture.py`, `create_targeted_offers.py` (gitignored), `cancel_offers.py`, `fill_offer.py`, `update_config.py`. Also holds `allocation_config.yaml` (gitignored).
- **[`monitoring/`](monitoring/README.md)** — watchers and scanners, mostly run unattended via `.github/workflows/`: `arbitrage_scanner.py`, `borrow_offer_watch.py`, `loan_watch_notify.py`, `wallet_tx_watch.py`, `tg_deposit_watch.py`, `defaulter_watch.py`, `soon_to_expire.py`, `underwater.py` (gitignored). Also holds `defaulter_config.yaml`/`tg_watchlist.json` (gitignored).
- **[`reporting/`](reporting/README.md)** — read-only analytics, never signs anything: `pnl_leaderboard.py`, `lender_capital_scan.py`, `competitor_timing_report.py`, `offer_posting_times.py`, `borrower_loan_timeline.py`, `verify_offers.py`, `portfolio_health.py`.

Each script that imports a `lib/` module has a one-line `sys.path` shim near its imports pointing at `../lib` (and, for `defaulter_capture.py` specifically, also `../monitoring` — it depends on `defaulter_watch.py`). Each config/state file is loaded relative to its owning script's own location (`Path(__file__).parent / "..."`), so it lives alongside that script in the same folder, not at the repo root.

## Setup

### 1. Install dependencies

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure environment

Create a `.env` file in the project root:

```env
OFFERBOOK_WALLET=<your-wallet-pubkey>
OFFERBOOK_PRIVATE_KEY=<your-base58-private-key>
DRY_RUN=true
```

Optional overrides:

```env
OFFERBOOK_API_BASE=https://api.offerbook.jup.ag/api/v1
OFFERBOOK_TX_API_BASE=https://builder.offerbook.jup.ag/api/v1
SOLANA_RPC=https://api.mainnet-beta.solana.com
MAX_OFFER_PRINCIPAL_USDC=50    # cap each offer at 50 USDC (default 10000; 0 = uncapped)
ALLOCATION_CONFIG=path/to/allocation_config.yaml
```

### 3. Run

```bash
# Safe preview — no transactions submitted (Ledger signing by default, see below)
DRY_RUN=true python strategy/strategy.py --days 7

# Live — cancel first, then run all four durations in one invocation
python strategy/cancel_offers.py --days all
python strategy/strategy.py --days 1,3,7,15

# Omit --days and --collateral to be prompted interactively for both instead
python strategy/strategy.py
```

See [strategy/README.md](strategy/README.md) for the full strategy breakdown, the pricing/LTV math, signing modes (Ledger vs. hot wallet), and collateral targeting — and the other three folder READMEs for everything else in the repo.

## Environment variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `OFFERBOOK_WALLET` | Yes | — | Your wallet public key |
| `OFFERBOOK_PRIVATE_KEY` | Yes (live) | — | Base58-encoded private key for signing |
| `DRY_RUN` | No | `false` | `true` to preview without submitting |
| `OFFERBOOK_API_BASE` | No | `https://api.offerbook.jup.ag/api/v1` | Read API base URL |
| `OFFERBOOK_TX_API_BASE` | No | — | Transaction builder API base URL |
| `SOLANA_RPC` | No | `https://api.mainnet-beta.solana.com` | Solana RPC endpoint |
| `MAX_OFFER_PRINCIPAL_USDC` | No | `10000` | Per-offer USDC cap in `strategy.py` (0 = uncapped). `defaulter_capture.py` and `create_targeted_offers.py` each enforce their own hardcoded $10,000 per-offer cap, not driven by this env var. |
| `ALLOCATION_CONFIG` | No | `strategy/allocation_config.yaml` | Path to allocation config file |
| `OFFERBOOK_SIGNING_MODE` | No | `ledger` | `ledger` or `private_key` — used by `cancel_offers.py`, `strategy.py`, `fill_offer.py`, and `defaulter_capture.py` (all in `strategy/`) |
| `OFFERBOOK_LEDGER_PATH` | No | `44'/501'/0'` | BIP32 derivation path for Ledger signing — in `strategy.py`, `cancel_offers.py`, and `fill_offer.py` this is only the fallback offered at the interactive account prompt (see [strategy/README.md's Signing modes](strategy/README.md#signing-modes)), not used silently |
| `TELEGRAM_BOT_TOKEN` | No | — | Bot token from @BotFather — used by `wallet_tx_watch.py` and `tg_deposit_watch.py` (both in `monitoring/`) |
| `TELEGRAM_CHAT_ID` | No | — | Your chat id — same two scripts |
| `OFFERBOOK_PORTFOLIO_WALLETS` | No | — | Comma-separated wallet addresses for `portfolio_health.py` (in `reporting/`) to report on |

`SMTP_FROM_EMAIL` / `SMTP_APP_PASSWORD` / `NOTIFY_EMAIL_TO` (used by `loan_watch_notify.py`, `borrow_offer_watch.py`, `arbitrage_scanner.py`, and `competitor_timing_report.py`, all in `monitoring/`/`reporting/`) are **not** meant to go in `.env` — they live only as GitHub Actions secrets (`gh secret set <NAME>`), since those scripts are meant to run unattended on a schedule, not locally.

## Security

- Never commit your `.env` file — it is listed in `.gitignore`
- Always do a dry run first before going live
- `api-1.json` and `api-1 (2).json` are gitignored (internal API docs)
- `strategy/allocation_config.yaml` is gitignored — it reveals your actual per-token risk tolerance and position sizing
- `monitoring/defaulter_config.yaml` (`defaulter_watch.py`'s private borrower-tracking ledger) is gitignored — it's a personal risk record, never published
- `monitoring/tg_watchlist.json` (`tg_deposit_watch.py`'s watchlist) is gitignored for the same reason
- `strategy/create_targeted_offers.py` is gitignored — it's a borrower-specific targeted-offer tool built around one counterparty's historical repayment pattern, deliberately kept out of the public, general-purpose strategy code
- `monitoring/underwater.py` is gitignored
- `lib/google_calendar_credentials.json` / `lib/google_calendar_token.json` (OAuth client secret + cached token for `portfolio_health.py`'s Calendar sync) are gitignored
- `reporting/lender_capital_state.json`, `reporting/portfolio_reminder_state.json`, `reporting/portfolio_reminder_sync_plan.json`, `reporting/competitor_timing_state.json` are gitignored — competitive intelligence / private tracking state
- `loan_watch_notify.py`'s and `arbitrage_scanner.py`'s email credentials (`SMTP_FROM_EMAIL`, `SMTP_APP_PASSWORD`, `NOTIFY_EMAIL_TO`) live only in GitHub Actions secrets, never in a committed file
- `monitoring/wallet_watch_state.json`, `monitoring/arbitrage_scanner_state.json`, `monitoring/borrow_offer_watch_state.json`, and `monitoring/loan_watch_state.json` **are** committed (unlike the gitignored files above) — they only ever hold public on-chain data (pubkeys, signatures, amounts) or, for the wallet watchlist, addresses you've chosen to track. If that watchlist itself needs to stay private, don't commit it — ask before adding a sensitive address to a tracked state file
- `main` has branch protection blocking force-pushes and branch deletion. It does **not** require PR review — that was tried and reverted after it broke the GitHub Actions state-commit workflows (`GH006: Protected branch update failed`, since `enforce_admins=false` doesn't exempt the `github-actions[bot]` identity, only human admin accounts). The only collaborator with push access is the repo owner, and no workflow triggers on `pull_request`/`pull_request_target`, so a third party's PR can't be merged or executed automatically regardless
