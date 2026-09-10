# Shared library

Modules that are imported, never run directly. Every script elsewhere in the repo that needs one of these has a one-line `sys.path` shim near its imports pointing back at this folder — see the [root README](../README.md#repository-layout).

## Shared helpers (`offerbook_common.py`)

Logic that used to be copy-pasted across scripts now lives in one place and gets imported, not duplicated:

- **Ledger signing** — `get_ledger_signer()`, `resolve_signer_wallet()`, `confirm_signing_mode()`. Used by `strategy.py`, `cancel_offers.py`, `fill_offer.py`, `defaulter_capture.py`, `create_targeted_offers.py` (via `defaulter_capture.py`), and `verify_offers.py` (read-only wallet resolution only, never signs).
- **`prompt_for_ledger_path()`** — the interactive "which Ledger account do you want to run on?" prompt (see [strategy/README.md's Signing modes](../strategy/README.md#signing-modes)), accepting a bare account index or a full derivation path. Used by `strategy.py`; `cancel_offers.py`/`fill_offer.py` use their own labeled-picker variant of the same idea (`KNOWN_LEDGER_ACCOUNTS`) since those two have no "right" default account.
- **HTTP client + pagination** — `api_get()`, `post_tx()`, `fetch_all_pages()`. Used by every script that talks to the Offerbook API.
- **CLI signing flags** — `add_signing_args()` / `resolve_signing_mode()` add and validate the `--ledger`/`--private-key`/`--yes` flags shared by every signing-capable script.
- **`KNOWN_DECIMALS` / `KNOWN_SYMBOLS`** — the canonical token → decimals / display-symbol tables (24+ tokens). Every script that needs either imports these instead of keeping its own copy, so a token added here is immediately recognized everywhere (`strategy.py`, `verify_offers.py`, `underwater.py`, `defaulter_capture.py`, `defaulter_watch.py`, `soon_to_expire.py`, `loan_watch_notify.py`, `portfolio_health.py`).
- **`_volume_weighted_median()`** — the same volume-weighted-median helper `strategy.py`, `defaulter_capture.py`, and `verify_offers.py` all use for LTV/APY benchmarking (see [strategy/README.md §1/§4](../strategy/README.md#1-volume-weighted-median-apy)), so all three price and risk-check offers off the exact same statistic.
- **`size_filtered_volume_weighted_median()`** — wraps the above with the 0.5×–2× size-band preference, shared by `strategy.py`'s APY and LTV benchmarks.
- **`round_principal_raw()`** — rounds a raw principal amount down to a round whole-dollar figure ($500 step, or $100 if under one step), used by `strategy.py` so offer sizes read like 11,500.00 rather than 11,800.35.
- **`_mint_from_asset()`** — extracts a mint address from an OfferAsset, used anywhere offer/loan JSON needs parsing.
- **`parse_allocation_config_symbols()`** / **`build_symbol_to_mint_from_allocation_config()`** — scrapes `# SYMBOL — Description (ltv ~x%)`-style comments out of an `allocation_config.yaml` (the YAML loader itself strips comments). `create_targeted_offers.py` uses the mint→symbol direction for display; `strategy.py`'s `resolve_collateral_token()` uses the reverse (symbol→mint) as a fallback when a `--collateral` ticker isn't in its own hardcoded `SYMBOL_TO_MINT` table — see [strategy/README.md's Targeting specific collateral](../strategy/README.md#targeting-specific-collateral).

Each script that's itself imported elsewhere for these helpers (e.g. `create_targeted_offers.py` calling `defaulter_capture.resolve_signer_wallet()`) keeps a thin same-signature wrapper around the shared function, so nothing calling into it had to change.

## Ledger hardware wallet signer (`ledger_signer.py`)

Talks to the Ledger Solana app directly over USB HID (via `ledgerblue.comm`), independent of any official Solana Ledger SDK. Exposes a `LedgerSigner` class:

- `get_pubkey(display=False)` — reads the device's public key for a given derivation path; `display=True` also shows it on-device for manual verification.
- `sign_transaction(tx_b64, expected_signer=None)` — sends a base64-encoded transaction for blind signing (Offerbook's program isn't in Ledger's known-instruction registry, so blind signing must be enabled on-device), returns the signature. If `expected_signer` is given, verifies the device's pubkey matches before signing, so a wrong-account mistake fails fast instead of producing a validly-signed transaction from the wrong wallet.
- `get_app_configuration()` — reads the installed Solana app's version, mainly used to sanity-check the device is unlocked with the right app open before attempting anything else.

Raises `LedgerError` (not a raw `CommException`) for anything a caller should show the user directly — device locked, wrong app open, blind signing not enabled, user rejected on-device, etc. — see `_friendly_error()` for the exact mapping. See [strategy/README.md's Signing modes](../strategy/README.md#signing-modes) for the user-facing flow (account picker, transaction preview, message-hash verification) built on top of this.

## Google Calendar client (`google_calendar_client.py`)

Thin OAuth wrapper around the Google Calendar API, used by `../reporting/portfolio_health.py` to create/delete loan-expiry reminder events — fully independent of any Claude/MCP connector, so it works standalone.

**One-time setup** (see the module's own docstring for full step-by-step Google Cloud Console instructions):

1. Create a Google Cloud project, enable the **Google Calendar API**.
2. Create an OAuth client ID (**Desktop app** type), download the credentials JSON.
3. Save it as `lib/google_calendar_credentials.json` (gitignored — never commit it).
4. The first time `create_event`/`delete_event` is called, a browser window opens for one-time consent; after that, a refresh token is cached in `lib/google_calendar_token.json` (also gitignored) and reused/refreshed silently.

Override the default file locations via `GOOGLE_CALENDAR_CREDENTIALS_PATH` / `GOOGLE_CALENDAR_TOKEN_PATH`. Exposes two functions: `create_event(summary, description, start_iso, end_iso, popup_minutes_before=0)` → event ID, and `delete_event(event_id)` (no-ops quietly if the event's already gone).
