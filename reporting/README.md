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

Ranks every Offerbook lender by all-time realized PNL. There's no "top by PNL" endpoint on the API — only volume-based leaderboards (`/metrics/top-lenders`) — so this pulls the full repaid + defaulted (+ active, for the rollover bullet below) loan history platform-wide (no borrower/lender filter) and aggregates client-side.

Realized PNL per lender =

- **+ net interest earned on repaid loans.** Interest is converted to USD via the platform's documented proportional formula (`interest / principalAmount * startPrincipalAmountUsd`), then the actual protocol "repay" fee charged is subtracted — taken straight from `metadata.fees.repay.amountUsd` per loan, not assumed as a flat rate.
- **+ collateral kept on defaulted loans**, valued at default time (`endCollateralAmountUsd`), minus the principal that was lent out and not recovered (`startPrincipalAmountUsd`). This is a mark-to-market figure at the moment of default, not necessarily cash actually realized — if the lender is still holding the seized collateral, it's unrealized from here.
- **+ interest collected on in-place loan extensions/rollovers**, across every loan regardless of current status. Offerbook lets a loan's term be extended in place (same pubkey, stays `active`, `expiredAt` pushed forward, `extensionCount` ticks up) and pays that completed term's full interest to the lender AT that moment (`metadata.extensions[].interestPaid`/`repayFee`) — real cash, not mark-to-market, so it counts even for a loan that's still active and hasn't reached repaid/defaulted. This is why active loans get fetched here too, even though this leaderboard is otherwise scoped to settled outcomes. Confirmed to affect ~12% of active loans at any given time — without this, every earlier term's interest on a loan that was rolled over before its final resolution was silently dropped, forever, not just delayed.

Also reports each lender's **total volume** — total USD principal (at origination) of every SETTLED (repaid or defaulted) loan they've made. Volume stays settled-only (deliberately excludes active loans, extended or not) — it's a distinct, unrelated metric from realized PNL and isn't affected by the rollover-interest bullet above. Shown alongside PNL, not used for ranking — a high-volume lender isn't necessarily a profitable one. The printed table also breaks out a **rolled over** count per lender (extensions contributing to PNL) alongside the existing repaid/defaulted counts.

The table also always carries **vol incl rollovers $** and **cycles**. Base volume above counts a loan's principal once at settlement no matter how many times it was extended in place (same pubkey = "one loan"). But each extension is its own completed term — the borrower paid full interest again and the principal went back to work for another cycle — so this treats each one as distinct volume, priced at that specific extension's own `metadata.extensions[].principalAmountUsd` (falling back to the loan's `startPrincipalAmountUsd` only if an individual extension is missing it). Unlike base volume, this also counts extensions on currently-**active** loans (their already-completed prior terms, not the still-open current one) — so a lender can show rollover volume here even with $0 in the base volume column, if every one of their loans happens to still be active. `cycles` = repaid + defaulted + rolled over counts combined, i.e. how many distinct completed lending cycles that total spans.

```bash
python reporting/pnl_leaderboard.py              # top 25 by realized PNL
python reporting/pnl_leaderboard.py --top 50
```

If `OFFERBOOK_PORTFOLIO_WALLETS` is set (`.env` — the same var `portfolio_health.py` reads, comma-separated addresses), those specific lenders are merged into a single combined row before ranking, labeled `YOUR WALLETS (N combined)` rather than listed individually. The remap happens at the point each loan is aggregated, so the real addresses never become dict keys, let alone reach the printed output or the committed script — anyone else running this public script with the var unset gets every wallet ranked individually, unchanged.

Read-only, never signs or submits anything.

## Seizure outcome scan (`seizure_outcome_scan.py`)

`pnl_leaderboard.py` values every defaulted loan's collateral at a single mark-to-market snapshot (`endCollateralAmountUsd`, priced the instant default was recorded) — a fiction the moment the lender doesn't sell right then. "Should a lender be holding seized collateral instead of liquidating it immediately" can only be answered against what actually happened to it on-chain afterward. This takes the platform's top N most profitable lenders (same realized-PNL formula `pnl_leaderboard.py` uses, computed fresh here so this stays runnable standalone) and, for each of their defaulted loans, walks Solana directly to classify the real outcome:

- **SOLD** — every transaction since default that reduced that lender's balance of the collateral mint, paired with whatever they received in the same transaction (USDC priced at exactly $1; SOL/other tokens priced at today's live rate as a proxy, flagged).
- **HOLDING** — current live balance of that mint still in the lender's wallet, priced at today's live rate.
- **PARTIAL** — both of the above.
- **Δ vs offerbook** = (realized + current value) − the mark-to-market snapshot — negative means they'd have done better selling immediately, positive means holding paid off, flagged `*** HOLDING PAID OFF ***` / `*** HELD TOO LONG ***` per row plus a totals line.

```bash
python reporting/seizure_outcome_scan.py                      # top 20 lenders by realized PNL
python reporting/seizure_outcome_scan.py --top 10
python reporting/seizure_outcome_scan.py --min-seizure-usd 50  # skip dust seizures (default: $10)
python reporting/seizure_outcome_scan.py --lender <address>    # trace just one lender, any PNL rank
```

Three correctness issues surfaced during development, all now handled rather than worked around:

- **Shared token accounts.** A lender who's defaulted on the same collateral token more than once has ALL of those seizures land in the SAME Associated Token Account (keyed by owner+mint, not by loan). Near-simultaneous defaults (seen in practice: three defaults 15 seconds apart) get merged into one combined "lot" before tracing — any later combined sale can't be split back apart per-loan after the fact, so merging first is the only way to avoid crediting 100% of a joint sale to whichever loan's window happened to still be open. Merged rows show as `N loans merged: <pubkey8>+<pubkey8>+...` in the loan column.
- **Account contamination.** A lender can also independently deposit or trade the SAME token in that account for unrelated reasons (seen in practice: a lender cycling $4,000+ of an unrelated token through the exact account that also held a $10 dust seizure from months earlier). The trace walks chronologically and treats each lot's own expected arrival(s) as legitimate, but any further receipt that doesn't match a known later lot's own arrival (time + amount) is flagged `CONTAMINATED` — everything from that point on is excluded from the total rather than silently mis-attributed. This is what catches would-be outliers like "$10 seizure, $32,000 realized."
- **Dust receipts shouldn't trigger contamination.** A receipt smaller than `DUST_FRACTION` of the lot's own seized amount (e.g. a small referral-fee rebate) is ignored rather than treated as suspicious — confirmed against a real case where a ~$1.60 inflow sat between two of a lender's own legitimate sales; without this threshold, that single dust receipt would have wrongly voided $35,874 of sales later independently confirmed correct on Solscan.

Associated Token Account derivation (not wallet-level scanning): rather than scanning a lender's whole transaction history (slow and noisy on an active trading wallet), this derives the collateral mint's own ATA (the standard SPL/Token-2022 PDA, picking the right token program from the loan's own `collateralTokenProgram` field) and scans only that account's history.

This is a genuinely heavy scan — potentially 100+ defaulted loans across the top N lenders, several Solana RPC calls each, against the free public mainnet-beta endpoint by default (set `SOLANA_RPC` to a paid endpoint for a large speedup). Expect minutes, not seconds —`--top`/`--min-seizure-usd` narrow the scope if a full run is more than you need.

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
python reporting/liquidity_check.py --collateral USELESS --days-back 30
python reporting/liquidity_check.py                                        # prompts for collateral
```

Defaults to USDC principal. Flags any lender whose live offers exceed their real balance as `*** OVERSTATED ***`. Balance-check failures (RPC errors) are retried and, if still unresolved, excluded from totals and flagged `BALANCE CHECK FAILED` rather than silently counted as a confirmed $0.

Each lender row also shows their **avg APY** and **avg LTV** (both size-weighted across their live offers on this pair — a lender's small high-APY/high-LTV offer doesn't drag the average up past what their much larger offer is actually pricing) and **durations** (the distinct set of durations across those offers, e.g. `7d,15d,30d` — kept as a set rather than averaged, since durations are normally a handful of fixed choices, not a continuum). LTV comes from each offer's own priced metadata (the LTV the lender set the offer at), not recomputed from live prices — offers missing that metadata are excluded from the LTV average specifically (shown as `n/a` if none of a lender's offers have it) rather than silently counted as 0%.

Also reports **loan size & pricing history** for the pair, from its full loan history (active + repaid + defaulted, platform-wide, any lender — not just live offers, so this section still runs even when the pair currently has zero live offers):

- **Biggest loans ever** (top 10) — date, borrower, size, APY, duration, status.
- **Biggest single day ever** — the one calendar day with the most total USD originated.
- **Size vs. APY, last N days** (`--days-back`, default 14) — loans split into 4 equal-count quartiles by size, each showing count/total volume/median APY/max APY. This is the actionable signal for tuning your own offer's APY: if the biggest quartile's *median* APY is higher than the smallest's, big borrowers on this pair are price-insensitive and you likely have room to push APY higher on a large offer; if it's lower, they're shopping around and an aggressively-priced big offer risks sitting unfilled. Median and max are shown separately on purpose — a single large outlier fill (e.g. a whale who took one loan at 130% APY) can make the *max* column look great while the *median* tells you that's not representative of what similarly-sized borrowers typically pay.

Read-only, no signing.

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
- **Realized PNL** — same formula as `pnl_leaderboard.py` (including the rollover-interest bullet above — see that section for why), scoped to just these wallets. Also broken out into trailing realized-earnings windows (last 24h / 7d / 14d / YTD), alongside the all-time total — each resolved loan's `updatedAt` (repaid/defaulted) or each extension's own `at` timestamp (rollovers) is used as a proxy for when it was actually earned (the API has no dedicated `repaidAt`/`defaultedAt` field). YTD = resolved since Jan 1 of the current year. The printed breakdown shows a `rolled over: N for $X` figure per window alongside the repaid/defaulted counts.
- **ROI** — realized PNL (all-time and YTD) as a % of your capital base, from Offerbook's own `GET /users/{address}/escrow-summary` (`netDepositedUsd` = lifetime deposits − withdrawals, USD at each movement's own price; interest credited to a lender is deliberately excluded by the platform itself, so yield is never counted as capital). Summed across every wallet checked this run. Skipped if the capital base is <= $0; any wallet with unpriced capital movements is flagged rather than silently understating the total.

  If you also spend out of the same wallet(s), raw `netDepositedUsd` is misleading — every personal withdrawal shrinks it, inflating PNL/deposited even though nothing about trading performance changed. `--reset-capital-baseline` locks in a snapshot: from that point on, withdrawals are treated as coming out of profit rather than capital (genuine new deposits still count on top), until you reset again — e.g. run it right after a withdrawal you actually *do* want to count as pulling capital out. State lives in `portfolio_capital_baseline.json` (gitignored, wallet-keyed).

  Every run also auto-ratchets an *existing* baseline forward on its own, no flag needed: if live `netDepositedUsd` has climbed above the currently locked `baseline_net_usd`, the only way that's possible is a genuine new deposit outpacing withdrawals since the last lock (withdrawals alone can only ever push `netDepositedUsd` down), so it's always safe to fold in automatically. This doesn't change the computed capital-base number for that run — the baseline-plus-new-deposits formula already credits new deposits either way — it just keeps the persisted baseline file from drifting further behind reality with every deposit. A wallet with no baseline yet is left alone (opting in is still an explicit `--reset-capital-baseline` decision).
- **Unrealized profit** — interest owed on active loans, net of Offerbook's flat 10% repay fee (measured empirically off real repaid loans, not assumed). Offerbook charges the full term's interest regardless of early repayment, so this is the full committed amount, not a naive time-prorated fraction. Shown two ways: raw, and net of any *current* underwater loss (`principal − live collateral value`, zero for healthy positions) — the second number is the one that reflects real risk.
- **Portfolio size** — open principal (live value) + accrued interest owed on active loans − current underwater losses + idle balance (USDC, SOL, and every OTHER token sitting in wallet + escrow — e.g. collateral kept after a default — all priced live). A single mark-to-market figure for total value under your control right now, not just the raw active-loan principal.
- **Capital freeing up** — principal of active loans due within the next 24h / 48h / 72h (optimistic: assumes on-schedule repayment, not default), for planning how much you'll have free to redeploy.
- **Volume** — total USD principal lent, counted the moment a loan originates regardless of outcome; trailing-7-day and all-time.
- **Wallet/escrow balances** — SOL (both wallet and escrow) and USDC (both wallet and escrow), plus an **other holdings** table: any nonzero balance of any OTHER token (any mint, classic SPL or Token-2022, wallet or escrow) with its live price and USD value — this is what picks up collateral you keep after a default instead of immediately selling. A token with no resolvable live price is shown with its raw amount and flagged `NO PRICE` rather than silently valued at $0 (excluded from the dollar totals, not zeroed). Decimals fall back three ways: Jupiter's price response, then the curated `KNOWN_DECIMALS` table, then reading straight off the mint account on-chain — a long-tail/pump.fun token you only hold because you seized it as collateral is exactly the case the first two are least likely to cover. Feeds into the combined idle-balance USD figure that Portfolio size above uses.

Also syncs Google Calendar reminders (a popup 30 minutes before each active loan's expiry, so a default is never missed), via `../lib/google_calendar_client.py` — see [lib/README.md](../lib/README.md#google-calendar-client-google_calendar_clientpy) for the one-time OAuth setup and the refresh-token self-heal behavior. Resolved loans' reminders are relabeled done (not deleted) so Calendar stays a visible trail. **Renewed loans are handled too**: Offerbook lets a loan's term be extended in place (same pubkey, stays `active`, `expiredAt` moves forward) — confirmed to affect ~12% of active loans at any given time. When a tracked loan's expiry no longer matches its current `expiredAt`, the old reminder is marked done (tagged `renewed`) and a fresh one created for the new deadline, so the reminder never silently points at a stale date. Pass `--no-calendar-sync` to just refresh the local sync-plan file without touching Calendar. State for both the reminder tracking and the sync plan lives in `portfolio_reminder_state.json`/`portfolio_reminder_sync_plan.json` (both gitignored).

## APY opportunity scan (`apy_opportunity_scan.py`)

"Am I maximizing every dollar I've got on offer?" — scans every currently ACTIVE loan platform-wide (live state, not resolved history) and answers that from a few angles, all from data already on each loan (`metadata.startPrincipalAmountUsd`, `apy`) — no live price-fetching needed, just the one paginated `/loans/status/active` call.

```bash
python reporting/apy_opportunity_scan.py
python reporting/apy_opportunity_scan.py --wallets <addr1>,<addr2>
python reporting/apy_opportunity_scan.py --min-principal 100       # noise filter for the lender leaderboard
python reporting/apy_opportunity_scan.py --top 30 --top-opportunities 25
python reporting/apy_opportunity_scan.py --expiry-hours 72         # widen the poach window
python reporting/apy_opportunity_scan.py --min-apy 50              # override the opportunity/poach APY floor (percent)
```

- **Your wallets** — per-wallet and combined active-loan count, principal, size-weighted average APY, and annualized interest run-rate (principal × APY, what a full year at this rate would pay) — from `OFFERBOOK_PORTFOLIO_WALLETS` (`.env`) or `--wallets`.
- **Lender APY leaderboard** — every lender ranked by size-weighted average APY across their active loans (weighted by principal, so a $10 loan at 200% can't outrank a $10,000 loan at 60%). Your wallets are merged into one `YOUR WALLETS (N combined)` row (same trick `pnl_leaderboard.py` uses) so your standing shows up inline against the real competition — printed with its exact rank even if `--min-principal`/`--top` would otherwise push it out of the table.
- **Collateral-token APY breakdown** — every token currently in play, ranked by size-weighted average APY, flagged with whether you currently hold any exposure to it — directly answers "which tokens are paying best right now that I should point more capital at."
- **Your token exposure vs. market** — for every token you currently hold an active loan against, your own weighted-average APY on that token next to the platform-wide average for that same token, with the gap called out in percentage points and flagged `BELOW MARKET` when negative — a same-token comparison, not a vague "the market pays more" feeling.
- **Missed opportunities** — active loans that aren't yours, paying at/above your combined weighted-average APY (override with `--min-apy`), highest APY first — concrete terms you could have offered instead.
- **Poach candidates** — the same list, filtered to loans expiring within `--expiry-hours` (default 48h) — a borrower paying someone else a high rate who's about to be back in the market. Not a guarantee: an in-place extension (see `portfolio_health.py`'s docstring on loan rollovers) can renew a loan without ever reopening the market, so treat this as a candidate list worth watching (e.g. via `../monitoring/wallet_tx_watch.py`/`borrow_offer_watch.py`), not a sure thing.

Every token column shows a real symbol wherever one is resolvable — the curated `KNOWN_SYMBOLS` table first, then a live Jupiter token-search lookup for anything not in it (same approach `address_snapshot.py`/`borrower_loan_timeline.py` use) — falling back to a truncated mint address only if Jupiter has no record either. Every table also carries a trailing **contract** column with the full, untruncated mint address (or a `(NFT — no fungible mint)` placeholder for NFT collateral), so there's always something copy-pasteable regardless of whether a symbol resolved.

Read-only, no signing. Once a missed-opportunity/poach-candidate lead looks interesting, `loan_origin_lookup.py` (below) answers "how did that borrower end up on those terms" for any address involved.

## Loan origin lookup (`loan_origin_lookup.py`)

Takes one address (borrower or lender — both roles are checked automatically, no `--role` flag) and drills into HOW each of their loans came to exist, not just the loan's own locked-in APY/duration. For every loan, it resolves the ORIGINATING OFFER those terms were filled from and reports: who posted it (a `lending` offer from the lender, or a `borrowing` request from the borrower — a very different read on the same APY number), when, whether the posted terms match what the loan actually settled at, whether that standing offer has ever been filled by anyone else (`fillCounter` is platform-wide, not scoped to this address), and — when the offer was itself a counter in a back-and-forth negotiation — the full chain of prior asks it was negotiated down/up from.

```bash
python reporting/loan_origin_lookup.py --address FRLXeUieHrAnuQHmVqimSjPktg9mbJtADb14aG6sYr8P
python reporting/loan_origin_lookup.py --address <addr> --status all        # include repaid/defaulted, not just active (slower)
python reporting/loan_origin_lookup.py --address <addr> --status repaid
python reporting/loan_origin_lookup.py --address <addr> --verbose           # add the full per-loan detail block below the table
python reporting/loan_origin_lookup.py                                       # prompts for the address
```

Always prints a one-row-per-loan summary table first — created date, role, loan status, collateral, principal $, **LTV % at origination** (`startPrincipalAmountUsd` / `startCollateralAmountUsd`, not a live recomputation), APY, duration, counterparty, the originating offer's own status, a negotiation-hop count, and the loan pubkey — built for scanning dozens of loans at a glance. Pass `--verbose` to additionally print the full per-loan detail block underneath (min-fill %/remaining %/allow-extend, and the full negotiation chain when one exists).

There's no `GET /offers/:pubkey` endpoint, so a loan's offer isn't directly fetchable by pubkey — it's resolved by pulling the FULL offer history (every status) of BOTH parties to the loan and matching by pubkey, since a negotiation chain can alternate between whichever side is countering. Each address's offer history is fetched once and cached for the run, even if it recurs across several loans. A counterparty whose offer history fails to load (seen in practice: a 504 from the API on an unusually large history) is cached as empty and logged once, not retried per loan — any loan whose offer lived only in that history just shows up as "not found" instead of losing the rest of the report.

Natural companion to `apy_opportunity_scan.py`'s missed-opportunities/poach-candidates lists and to `address_snapshot.py`/`borrower_loan_timeline.py` — once one of those surfaces an address worth a closer look, this is the next step. Read-only, no signing.
