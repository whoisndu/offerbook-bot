# Reporting scripts

Read-only analytics — none of these sign or submit anything. All read `../lib/offerbook_common.py` for shared helpers. See the [root README](../README.md) for setup, environment variables, and the overall repo layout.

## Post-placement sanity check (`verify_offers.py`)

Run this immediately after placing orders to confirm every live offer has correct LTV, APY, duration, and principal volume — it recomputes the exact same dynamic LTV target `strategy.py` would (same-duration/size-band preference, self-exclusion, the 15-day collateral premium, all of it) against fresh market data, not a cached copy of the logic.

```bash
# Interactive prompt — asks which strategy to check
python reporting/verify_offers.py

# Skip the prompt via flag
python reporting/verify_offers.py --days 1
python reporting/verify_offers.py --days 3
python reporting/verify_offers.py --days 7
python reporting/verify_offers.py --days 15
python reporting/verify_offers.py --days all
```

For each offer the script prints a table row:

| Column | What is checked |
|---|---|
| `APY bps` / `APY %` | Must be ≥ 500 bps / 5% (the floor) |
| `LTV %` | Recomputed from fresh Jupiter/DexScreener prices; must be ≤ `Target%` |
| `Target%` | The dynamic LTV target ([§4 in strategy/README.md](../strategy/README.md#4-dynamic-ltv-target-and-safe-collateral-sizing)) recomputed *right now* from fresh market-wide offer/loan data for that collateral token — not a fixed per-strategy number, and not necessarily the same value the strategy script computed at offer-creation time, since market conditions move |
| `Vol USDC` | Principal amount; flagged if dust (< 1 000 raw units) |
| `status` | `PASS` / `WARN` (non-critical) / `FAIL` (LTV violation) |

Exit code is `1` if any LTV violations are found, `0` otherwise — safe to use in shell pipelines:

```bash
python strategy/cancel_offers.py --days all
python strategy/strategy.py --days 3,7,15 --yes
python reporting/verify_offers.py   # non-zero exit = something is wrong
```

## Lender capital scanner (`lender_capital_scan.py`)

Reports wallet + escrow USDC for every wallet that is or has ever been a lender — anyone with an active loan out, an open lending offer (any status), or a lender on any resolved (repaid/defaulted) loan in the platform's full history, so a past lender with no current activity still shows up. A one-shot report, not a watcher: useful for sizing up how much free/redeployable capital your competition actually has before you post a big offer.

```bash
# Everyone, sorted by total descending
python reporting/lender_capital_scan.py

# Only lenders with more than $5,000 total
python reporting/lender_capital_scan.py --min-total 5000

# Limit the printed table to the top 20 rows
python reporting/lender_capital_scan.py --top 20

# Compare against saved state without overwriting it
python reporting/lender_capital_scan.py --no-save
```

Every run's balances persist to `lender_capital_state.json` (gitignored, like `../monitoring/defaulter_config.yaml`/`../monitoring/tg_watchlist.json` — this reveals your own competitive-intelligence tracking) and are compared against the previous run, so each report shows a **Δ since last** column per lender plus an overall change in the grand total. The very first run has nothing to compare against, so every lender shows `NEW`.

Also shows **last seen**: the most recent `createdAt`/`updatedAt` across all of a lender's Offerbook loan/offer records (active, open offer, repaid, or defaulted) — purely platform activity, not general wallet activity elsewhere. Computed for free from data already being fetched, no extra API calls. Distinguishes a currently-dominant lender from one who's actually gone quiet (e.g. large balance, but last active weeks ago).

**Borrowed $ / util %** — each lender's currently-outstanding principal (summed from their active loans only — repaid/defaulted principal is no longer outstanding) alongside their own utilization rate: `borrowed / (borrowed + idle)`. The summary footer reports a single **PROTOCOL UTILIZATION** figure the same way, aggregated across every lender in the report (after `--min-total`/staleness filtering, before `--top` truncates the printed table) — how much of the protocol's total lending capital is currently deployed vs. sitting idle.

Read-only, no signing. Exit code is always `0` — this is an informational report, not a pass/fail check.

## Realized PNL leaderboard (`pnl_leaderboard.py`)

Ranks every Offerbook lender by all-time realized PNL. There's no "top by PNL" endpoint on the API — only volume-based leaderboards (`/metrics/top-lenders`) — so this pulls the full repaid + defaulted loan history platform-wide (no borrower/lender filter) and aggregates client-side.

Realized PNL per lender =

- **+ net interest earned on repaid loans.** Interest is converted to USD via the platform's documented proportional formula (`interest / principalAmount * startPrincipalAmountUsd`), then the actual protocol "repay" fee charged is subtracted — taken straight from `metadata.fees.repay.amountUsd` per loan, not assumed as a flat rate.
- **+ collateral kept on defaulted loans**, valued at default time (`endCollateralAmountUsd`), minus the principal that was lent out and not recovered (`startPrincipalAmountUsd`). This is a mark-to-market figure at the moment of default, not necessarily cash actually realized — if the lender is still holding the seized collateral, it's unrealized from here.

```bash
python reporting/pnl_leaderboard.py              # top 25 by realized PNL
python reporting/pnl_leaderboard.py --top 50
```

Read-only, never signs or submits anything.

## Borrower loan timeline (`borrower_loan_timeline.py`)

Plots one borrower's full loan history for a given collateral as a Gantt-style timeline (one bar per loan, colored by outcome, labeled with each loan's principal size) plus a concurrent-open-loans step chart underneath, so gaps in activity are easy to spot and label with their length in days directly on the chart. Useful for answering "does this borrower take breaks, and how often" at a glance rather than by reading a table.

Repaid loans get 3 colors (early / on-time / late); **defaulted loans get 2** — `underwater` (the seized collateral's value at default time was less than the debt owed: a *rational* default, repaying would have cost more than walking away) vs. `collateral covered debt` (collateral was still worth at least the debt: an *irrational* default — the lender still comes out fine, but it's worth telling apart from the "priced out by the market" case). Both the chart legend and the console summary (`Defaults: N total — X underwater, Y collateral covered debt`) break these out separately.

```bash
python reporting/borrower_loan_timeline.py                      # interactive prompts for collateral/borrower
python reporting/borrower_loan_timeline.py --collateral USELESS
python reporting/borrower_loan_timeline.py --collateral USELESS --borrower 4nFMipa1LwA6QQiVk29YqZeCvHixbWMMjcBR1h7jDMrZ
python reporting/borrower_loan_timeline.py --collateral USELESS --output /some/other/path.png
```

Omitting `--borrower` auto-picks the largest borrower (by total USD principal) for that collateral. Charts save to `~/Desktop/borrower_timeline_<borrower8>.png` by default. Every run also prints a **per-loan detail table** (lender, principal, interest, total owed, collateral posted) and a **summary-by-lender table**, both scoped to that borrower's currently OPEN loans only (not their repaid/defaulted history — the chart above covers that separately).

### Counterparty mode (`--address`)

Instead of one borrower, give an address and whether it's a `--role lender` or `--role borrower` (prompted if omitted). Every OTHER party it currently shares an active loan with is treated as a counterparty.

```bash
python reporting/borrower_loan_timeline.py --address 8pXq...9nZ --role lender     # one PNG per borrower it's actively lending to, plus an aggregate summary
python reporting/borrower_loan_timeline.py --address 4nFM...DMrZ --role borrower  # one PNG per lender it's actively borrowing from
```

Each counterparty still gets its own full-history PNG either way (same chart as single-borrower mode). `--role lender` additionally prints one extra section at the end: a combined per-loan table + a **summary-by-borrower table** — total owed, total collateral, loan count, per borrower — scoped to currently OPEN loans between *this lender specifically* and each borrower (not those borrowers' history with other lenders). This is the "who currently owes me what" answer without reading through every individual chart/table above it.

Read-only, no signing.

## Address snapshot (`address_snapshot.py`)

Fast, chart-free lookup: given one or more addresses (treated as a **single combined entity** — handy when the same person/desk controls more than one wallet), how much do they currently owe (or are owed), broken down by counterparty. No Gantt chart, no gap analysis — that's `borrower_loan_timeline.py`; this is the quick "how much is X into the platform for" answer.

```bash
python reporting/address_snapshot.py --addresses 4nFMipa1LwA6QQiVk29YqZeCvHixbWMMjcBR1h7jDMrZ,Gk4T2iCaJ7JuKzsgnwuBZnBRcpdgHQRizX6zf2gM7eC5 --role borrower
python reporting/address_snapshot.py --addresses 4nFMipa1LwA6QQiVk29YqZeCvHixbWMMjcBR1h7jDMrZ --role borrower --status all
python reporting/address_snapshot.py --addresses 8pXq...9nZ --role lender
python reporting/address_snapshot.py                                        # prompts for addresses, then role
```

`--role borrower` (the given addresses are borrowers): shows every loan they owe, grouped by **lender**. `--role lender`: shows every loan owed to them, grouped by **borrower**. `--role` is never silently defaulted — omitting it prompts, since guessing wrong just returns a meaningless zero-loans result instead of an answer. Defaults to currently **active** loans only (`--status all`/`repaid`/`defaulted` to widen scope). Read-only, no signing.

## Liquidity check (`liquidity_check.py`)

How much can you ACTUALLY borrow against a given collateral right now? A live lending offer's size alone overstates real liquidity whenever a lender has posted several offers (same or different pairs) whose sizes sum to more than their real wallet+escrow balance — Offerbook's rehypothecation model allows this deliberately (only one offer can actually fill at a time), and `strategy.py` itself does it. This script corrects for it: for each lender with a live offer on the given pair, it caps their contribution at `min(sum of their offer sizes on THIS pair, their actual current wallet+escrow balance)`, summed across lenders — the real amount a borrower could pull right now, shown alongside the naive raw total so the gap is obvious.

```bash
python reporting/liquidity_check.py --collateral USELESS
python reporting/liquidity_check.py --collateral <mint address>
python reporting/liquidity_check.py --collateral USELESS --principal SOL
python reporting/liquidity_check.py                                        # prompts for collateral
```

Defaults to USDC principal. Flags any lender whose live offers exceed their real balance as `*** OVERSTATED ***`. Balance-check failures (RPC errors) are retried and, if still unresolved, excluded from totals and flagged `BALANCE CHECK FAILED` rather than silently counted as a confirmed $0. Read-only, no signing.

## Competing-offer posting-time chart (`offer_posting_times.py`)

Charts WHEN competing lenders post their offers for a given collateral, so you can time `strategy.py` runs to land after most of the day's competing volume is already on the book, instead of undercutting a thin, partially-posted market. Pulls every lending offer for that collateral across every status (active/partiallyFilled/fulfilled/cancelled/expired) over a lookback window — not just what's live right now, since currently-live offers alone are capped by the platform's 24h expiry and only show a partial day. Your own offers are excluded by default.

```bash
python reporting/offer_posting_times.py                      # prompts for collateral
python reporting/offer_posting_times.py --collateral PUMP
python reporting/offer_posting_times.py --collateral all      # every collateral together
python reporting/offer_posting_times.py --collateral PUMP --days-back 14
python reporting/offer_posting_times.py --collateral PUMP --tz America/New_York
python reporting/offer_posting_times.py --collateral PUMP --coverage 0.9
```

Plots a scatter of posting time (date vs. hour-of-day, colored by status) to show whether the daily rhythm is consistent, plus an hourly histogram with a cumulative-%-of-USD-volume line to make the busiest posting hours obvious. Prints a recommended "post after HH:00" time — the first hour by whose end `--coverage` (default 80%) of a typical day's competing USD volume has historically posted. Charts save to `~/Desktop/offer_posting_times_<label>.png` by default. Read-only, no signing.

## Competitor posting-time distillation report (`competitor_timing_report.py`)

`strategy.py` posts ALL of its offers in one batch run rather than trickling them out — so the timing question that matters isn't "when do most offers for one token get posted" (that's `offer_posting_times.py`), it's "when has essentially every top competitor across the WHOLE market already posted for the day," so a single run can undercut everyone's fresh pricing at once. This pulls every lending offer platform-wide (every collateral pair) over a rolling lookback window, ranks lenders by total USD volume in that window, and profiles both the aggregate market rhythm and each top lender's individual posting hours. Two local ML techniques (no external API, no billing) turn that into more than a bigger table: **k-means clustering** (scikit-learn) groups top lenders by the *shape* of their 24-hour posting profile into behavioral archetypes ("morning poster", "evening poster", etc. — K chosen automatically via silhouette score, scaling up to 6 clusters as more lenders become available rather than a fixed small ceiling), and **linear regression** (numpy) fits day-index vs. daily competing USD volume to report whether competition is intensifying or cooling off. Clustering is used for the hour-of-day question specifically because it's circular (23:00 and 00:00 are adjacent) — a plain regression would mishandle that, and even the archetype label itself is derived from each cluster's centroid rather than averaging members' individual peak hours, for the same reason. Lenders with fewer than 3 offers in the window are excluded from clustering (not enough data for a meaningful shape) but still appear in the ranked list with their own post-after time.

```bash
python reporting/competitor_timing_report.py                    # 14-day lookback, top 20 lenders, emails the report
python reporting/competitor_timing_report.py --days-back 21
python reporting/competitor_timing_report.py --top-lenders 15
python reporting/competitor_timing_report.py --tz America/New_York
python reporting/competitor_timing_report.py --no-email          # console output only, skip email + state
python reporting/competitor_timing_report.py --heatmap-top 5     # chart more than the top 3 lenders
python reporting/competitor_timing_report.py --no-chart          # skip the heatmap PNG
```

Also saves a heatmap PNG (hour-of-day x top-3-lenders by default, `~/Desktop/competitor_top_lenders_heatmap.png`) — each row is normalized to that lender's own daily volume (not raw dollars), so the #1 lender's much larger absolute volume doesn't wash out everyone else's row, and a dashed line marks the market-wide recommended post-after hour for direct comparison. Chart saving is best-effort — a failure (e.g. no writable Desktop) logs a warning rather than failing the run, since the email is the primary deliverable.

Runs every 2 days via `.github/workflows/competitor_timing_report.yml`, which explicitly pins `--tz Africa/Lagos` (WAT, fixed UTC+1, no DST) since the GitHub Actions runner defaults to UTC — run locally without `--tz` and it uses the machine's own local timezone instead, which only matches if that machine is also on WAT/UTC+1. The workflow saves the heatmap to a repo-relative path (not `~/Desktop`, which doesn't exist on the runner) and uploads it as a downloadable build artifact on the run's summary page. Prior-run stats persist to `competitor_timing_state.json` so each report can call out drift — the recommended hour shifting, a top lender's own timing changing, new names entering the top ranks — but that file is gitignored and **never committed** (it's competitive-intelligence tracking, same reasoning as `lender_capital_state.json`); the workflow persists it across runs via `actions/cache` instead. Needs only the existing `SMTP_*` secrets and `OFFERBOOK_WALLET` (used only to exclude our own offers, never to sign) — no external API key, since the clustering/regression run locally. Read-only, no signing.

## Portfolio health check (`portfolio_health.py`)

Read-only lender-side report across one or more of your own wallets — the answer to "how is my actual lending book doing right now."

```bash
python reporting/portfolio_health.py
python reporting/portfolio_health.py --wallets <addr1>,<addr2>
python reporting/portfolio_health.py --risk-ltv 0.80 --expiry-hours 24
python reporting/portfolio_health.py --no-calendar-sync
```

Wallets to check come from `OFFERBOOK_PORTFOLIO_WALLETS` in `.env` (comma-separated addresses), not a CLI default — so the addresses themselves never appear in this script, keeping it safe to commit despite reporting on your own private wallets. Override with `--wallets` for a one-off check.

Per wallet, and combined across the portfolio:

- **Open loan risk** — live LTV recomputed from current prices (not the stale LTV at origination) vs. `--risk-ltv`/`--underwater-ltv` thresholds, plus a days-to-expiry (or already-overdue) table.
- **Realized PNL** — same formula as `pnl_leaderboard.py`, scoped to just these wallets. Also broken out into trailing realized-earnings windows (last 24h / 7d / 14d), alongside the all-time total — each resolved loan's `updatedAt` is used as a proxy for when it was repaid/defaulted (the API has no dedicated `repaidAt`/`defaultedAt` field).
- **Unrealized profit** — interest owed on active loans, net of Offerbook's flat 10% repay fee (measured empirically off real repaid loans, not assumed). Offerbook charges the full term's interest regardless of early repayment, so this is the full committed amount, not a naive time-prorated fraction. Shown two ways: raw, and net of any *current* underwater loss (`principal − live collateral value`, zero for healthy positions) — the second number is the one that reflects real risk.
- **Portfolio size** — open principal (live value) + accrued interest owed on active loans − current underwater losses + idle balance (USDC and SOL, wallet + escrow, SOL valued at its current price). A single mark-to-market figure for total value under your control right now, not just the raw active-loan principal.
- **Capital freeing up** — principal of active loans due within the next 24h / 48h / 72h (optimistic: assumes on-schedule repayment, not default), for planning how much you'll have free to redeploy.
- **Volume** — total USD principal lent, counted the moment a loan originates regardless of outcome; trailing-7-day and all-time.
- **Wallet/escrow balances** — SOL (both wallet and escrow) and USDC (both wallet and escrow), plus the combined idle-balance USD figure that feeds into Portfolio size above.

Also syncs Google Calendar reminders (a popup 30 minutes before each active loan's expiry, so a default is never missed), via `../lib/google_calendar_client.py` — see [lib/README.md](../lib/README.md#google-calendar-client-google_calendar_clientpy) for the one-time OAuth setup and the refresh-token self-heal behavior. Resolved loans' reminders are relabeled done (not deleted) so Calendar stays a visible trail. Pass `--no-calendar-sync` to just refresh the local sync-plan file without touching Calendar. State for both the reminder tracking and the sync plan lives in `portfolio_reminder_state.json`/`portfolio_reminder_sync_plan.json` (both gitignored).
