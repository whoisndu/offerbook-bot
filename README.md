# Offerbook Competitive Lending Bot

An automated lending bot for the [Offerbook](https://offerbook.jup.ag) protocol on Solana. It scans active lending offers, posts competitive lending offers sized to your per-collateral allocation, and sizes collateral against a **dynamic, per-token LTV target** using real-time prices from Jupiter and DexScreener.

## Strategies

Three independent scripts — run whichever suits your risk appetite. Each offer listing expires after **24 hours** and is re-posted on the next run.

| Script | Loan term | LTV floor (thin market data) | LTV hard ceiling | APY target |
|---|---|---|---|---|
| `strategy_3_days.py` | 3 days | **60%** | 75% | Benchmark − 5% |
| `strategy_7_days.py` | 7 days | **45%** | 75% | Benchmark − 10% |
| `strategy_15_days.py` | 15 days | **25%** | 75% | Benchmark − 12% |

LTV is no longer a single fixed ceiling — it's computed per collateral token from that token's own live market data, bounded by the floor and ceiling above. See [§4](#4-dynamic-ltv-target-and-safe-collateral-sizing) for the full rule.

### How it works

1. Fetch all active lending offers and loans from the Offerbook API
2. Group by `(principalMint, collateralMint)` pair
3. Compute the **volume-weighted median APY** from live offers of the same duration — the price level where the largest cluster of real market volume sits, not a mean (which one large outlier offer can drag far from where borrowers are actually transacting). If no same-duration offers exist for a pair, fall back to the global median across all durations. The log shows which source was used: `[from live offers (same duration)]` or `[from live offers (global)]`
4. Fetch real-time collateral prices from **Jupiter Price API** (primary) with **DexScreener** as fallback
5. For each pair, compute the token's **dynamic LTV target** (§4) from its own market data and current token age, then size `collateralAmount` to hit that target at **current prices** — not stale prices from other lenders' old offers. If no live price is available from either source, the pair is skipped entirely rather than sized off a stale pool-implied price (see below)
6. **Cross-validate the live price** against the pool-implied price from existing loans. If the two differ enough that the offer's true LTV would exceed the dynamic target, skip the pair and log a warning (guards against bad price feeds)
7. Set `principalAmount` to your configured allocation fraction of your total USDC balance (wallet + escrow)
8. Post the offer with `allowPartialFill = true` so borrowers can take any amount up to the full offer

### Price feed safety

Prices are fetched from Jupiter first, DexScreener second. After computing the required collateral amount, the bot cross-checks it against the **pool-implied price** — the price inferred from existing loans and offers for the same collateral. If the live price is stale or wrong (e.g. a DexScreener pool with low liquidity returning an anomalous price), the collateral requirement will be far too low at real market prices. The bot detects this and skips rather than posting an undercollateralised offer.

`safe_collateral_amount()` only ever sizes an offer off a genuine live price — if Jupiter and DexScreener both have nothing for a mint, the pair is skipped, full stop. It deliberately does **not** fall back to the pool-implied price in that case: doing so would size the offer using that same price and then "cross-validate" against it, which can never disagree with itself and provides no real protection. This is also why token decimals aren't limited to the small hardcoded `KNOWN_DECIMALS` table — Jupiter's price response includes each token's `decimals`, and that's merged in as a fallback, so any token Jupiter actually prices (which is nearly everything) is fully usable regardless of whether it's in that curated list.

## Mathematical Formulation

This section formalises the pricing, risk, and allocation decisions made by the bot.

### Notation

| Symbol | Meaning |
|---|---|
| $\mathcal{O}$ | Set of all active lending offers fetched from Offerbook |
| $\mathcal{O}_d \subseteq \mathcal{O}$ | Subset of offers whose duration matches strategy duration $d$ |
| $r_i \in \mathbb{R}_{>0}$ | Annualised percentage yield (APY) of offer $i$ |
| $p_i \in \mathbb{R}_{>0}$ | Principal amount (in USDC) of offer $i$ |
| $\delta \in \mathbb{R}$ | Strategy-specific APY adjustment factor |
| $P$ | Principal amount posted by the bot for a given offer |
| $C$ | Collateral amount required for that offer |
| $\pi_c$ | Live USD price of the collateral token (from Jupiter / DexScreener) |
| $\pi_p$ | Live USD price of the principal token (USDC, $\pi_p \approx 1$) |
| $L_k$ | Dynamic LTV target for collateral token $k$ (§4) |
| $\tilde\ell_k$ | Volume-weighted median LTV of token $k$'s own live market offers/loans |
| $\alpha_k \in [0,1]$ | Allocation fraction for collateral token $k$ |
| $B$ | Combined lender budget: $B = B_{\text{wallet}} + B_{\text{escrow}}$ |

---

### 1. Volume-Weighted Median APY

A naïve arithmetic mean over APYs — even volume-weighted — can be dragged far from where real trading volume actually sits by a single large outlier offer. The bot instead computes a **volume-weighted median**: sort offers by APY ascending, then accumulate principal-USD weight until it first reaches half of the total; the APY at that point is the benchmark, since it's the price level where the largest cluster of real market volume sits.

Formally, let $(r_{(1)}, p_{(1)}), \dots, (r_{(n)}, p_{(n)})$ be the offers in $\mathcal{S}$ sorted so $r_{(1)} \leq \dots \leq r_{(n)}$, and let $W = \sum_i p_{(i)}$. Then:

$$\tilde{r}_{vw}(\mathcal{S}) = r_{(j^{\ast})}, \qquad j^{\ast} = \min\left\{ j : \sum_{k=1}^{j} p_{(k)} \geq \frac{W}{2} \right\}, \qquad \mathcal{S} \neq \emptyset$$

Unlike a mean, one very large offer can only shift the median by contributing weight toward whichever side of the distribution it sits on — it can never single-handedly drag the benchmark toward its own extreme rate.

---

### 2. Duration-Stratified Benchmarking with Fallback

Offers of different durations reflect different risk premia and should not be pooled blindly. The benchmark APY for strategy $d$ is:

$$\tilde{r}^{(d)} = \begin{cases} \tilde{r}_{vw}(\mathcal{O}_d) & \text{if } \mathcal{O}_d \neq \emptyset \\ \tilde{r}_{vw}(\mathcal{O}) & \text{otherwise} \end{cases}$$

The log records which branch was taken (`[from live offers (same duration)]` vs `[from live offers (global)]`).

---

### 3. APY Target

Each strategy positions itself relative to the benchmark by applying a scalar adjustment $\delta$:

$$r^{\ast} = \tilde{r}^{(d)} \cdot (1 + \delta)$$

| Strategy | $d$ | $\delta$ | Rationale |
|---|---|---|---|
| `strategy_3_days.py` | 3 days | $-0.05$ | Shallower undercut than 7/15-day — this strategy's higher LTV floor already compensates for its risk, so it doesn't also need to price above market |
| `strategy_7_days.py` | 7 days | $-0.10$ | Mid duration; slight undercut to attract flow |
| `strategy_15_days.py` | 15 days | $-0.12$ | Long duration; deeper undercut offsets illiquidity |

A hard floor $r^{\ast} \geq r_{\min} = 0.001$ (10 bps) prevents posting at zero or negative yield.

**Largest-offer guardrail.** Let $i^{\ast} = \arg\max_{i \in \mathcal{O}} p_i$ be the pair's single largest live lending offer (any duration, excluding our own), with APY $r_{i^{\ast}}$ and LTV $\ell_{i^{\ast}}$. This is the offer a borrower comparison-shopping the pool is most likely to pick, and the volume-weighted median can still be skewed looser than what that specific offer actually accepts. The final APY target is capped up, never down:

$$r^{\ast} \leftarrow \max(r^{\ast},\ r_{i^{\ast}})$$

This only ever raises the bar — never undercuts the market's most prominent participant — and applies purely as a safety guardrail, not a competitiveness driver; the volume-weighted median from §1-§3 remains the primary target whenever it's already at least as conservative as $i^{\ast}$.

---

### 4. Dynamic LTV Target and Safe Collateral Sizing

The **loan-to-value ratio** of a proposed offer is:

$$\text{LTV} = \frac{P \cdot \pi_p}{C \cdot \pi_c}$$

Unlike a single fixed ceiling, the target LTV $L_k$ for collateral token $k$ is computed from **that token's own live market data** — every other lender's open offers/loans against the same token (always vs. USDC, the only principal Offerbook supports). Let $\mathcal{S}_k$ be that set, with per-entry LTV $\ell_i$ and principal-USD weight $p_i$; the volume-weighted median market LTV $\tilde\ell_k$ is computed the same way as $\tilde{r}_{vw}$ in §1 (weighted by $p_i$ instead of over APYs), so a single large outlier LTV can't drag the benchmark away from where the bulk of market volume actually sits.

"Enough data" to trust $\mathcal{S}_k$ means **either** $|\mathcal{S}_k| \geq 5$, **or** the total volume $V_k = \sum_{i \in \mathcal{S}_k} p_i$ is at least $2\times$ our own offer's principal $P$ — a few small dust offers shouldn't qualify, but one large, capital-backed offer can stand on its own even with fewer than 5 orders on the book (Offerbook's escrow model means posting a lending offer requires the lender to actually fund the principal, so a large offer costs real capital to fake). $L_k$ is then set by three rules, applied in order:

1. **Not enough data** (neither condition above holds): fall back to the strategy's flat floor, $L_k = L_{\text{floor}}$ (60% / 45% / 25% — see the table above).
2. **Young token** (enough data, and the token's earliest known trading pool is under 60 days old, or its age can't be determined at all — treated as young, fail-safe): $L_k = \tilde\ell_k - 0.05$, i.e. 5 points more conservative than the token's own market.
3. **Mature token** (enough data, and age $\geq$ 60 days): $L_k = \tilde\ell_k / 0.9$ — the bot accepts 10% less collateral than the market median implies, making its offer more attractive to borrowers than the going rate for tokens with an established track record.

**Largest-offer guardrail.** Using the same $i^{\ast}$ (the pair's single largest live offer) from §3, if its LTV $\ell_{i^{\ast}}$ is *more* conservative (lower) than whatever $L_k$ would otherwise be, the target is capped down to match it:

$$L_k \leftarrow \min(L_k,\ \ell_{i^{\ast}})$$

This can only make the result safer, never looser — it guards against the volume-weighted benchmark being skewed by several small, thin offers into a target more permissive than what the market's most prominent participant actually accepts.

Finally, $L_k$ is clamped to $[0.05,\ 0.75]$ — the 75% hard ceiling applies no matter how loose an established token's market looks, since an entire market trading at very high LTV is itself a warning sign rather than something to mirror.

Given $L_k$ (after both guardrails and the clamp), the minimum collateral the borrower must post is:

$$C_{\min} = \frac{P \cdot \pi_p}{L_k \cdot \pi_c}$$

The bot sets $C = C_{\min}$, using prices fetched at offer-posting time (not prices embedded in stale third-party offers).

---

### 5. Price Cross-Validation

Even after computing $C_{\min}$ from a live price feed, that price may itself be unreliable (stale oracle, thin pool). The bot cross-validates by computing the **pool-implied collateral price** from existing on-chain loans for the same pair:

$$\hat{\pi}_c = \frac{P_{\text{ref}} \cdot \pi_p}{C_{\text{ref}}}$$

where $(P_{\text{ref}}, C_{\text{ref}})$ are the principal and collateral from a reference loan. The implied LTV under the live price is then:

$$\widehat{\text{LTV}}_{\text{live}} = \frac{P_{\text{ref}} \cdot \pi_p}{C_{\text{ref}} \cdot \pi_c}$$

If $\widehat{\text{LTV}}_{\text{live}} > 2 \cdot L_k$ (using the same per-token dynamic target from §4), the live price is inconsistent with market-observed collateralisation — the bot skips the pair and logs a warning. This guards against posting an under-collateralised offer when a price feed returns an anomalously high $\pi_c$. The threshold is set at $2\times$ rather than $1\times$ to avoid false positives from minor price divergence between the live feed and pool-implied prices.

---

### 6. Budget Allocation

Let $K$ be the set of eligible collateral tokens for a given strategy run. The principal for pair $k$ is:

$$P_k = \alpha_k \cdot B, \qquad \alpha_k \in [0, 1]$$

The bot uses a `topup: minimum` escrow strategy: it draws from on-chain escrow first and pulls from the wallet only the shortfall $\max(0,\ P_k - B_{\text{escrow},k})$. This minimises unnecessary wallet-to-escrow transfers.

If `MAX_OFFER_PRINCIPAL_USDC` $= M > 0$, the effective principal is capped:

$$P_k^{\text{eff}} = \min(P_k,\ M)$$

---

## Allocation config (`allocation_config.yaml`)

Controls what fraction of your total USDC balance you're willing to offer per collateral token. Tokens listed here also **bypass the market-participation LTV filter** (you're explicitly trusting them); unlisted tokens must have a market LTV (from existing offers/loans, at their stale creation-time prices) at or below the **75% hard ceiling** to be considered at all. Passing that filter only means the pair is eligible — the actual collateral sizing still goes through the dynamic per-token target in [§4](#4-dynamic-ltv-target-and-safe-collateral-sizing).

```yaml
allocations:
  So11111111111111111111111111111111111111112: 1.0   # SOL  — up to 100% of balance
  JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN: 0.5  # JUP  — up to 50%
  DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263: 0.5 # BONK — up to 50%
  # ... add any collateral mint you want to lend against

default: 0.0   # skip any token not explicitly listed
```

Set a token to `0.0` to skip it entirely. Since Offerbook uses a **shared escrow model** (first borrower to fill wins), you can safely set multiple tokens to `1.0` — only one loan fills at a time.

### Syncing the config

```bash
# Preview new tokens discovered on Offerbook without writing
python update_config.py --dry-run

# Add new tokens and resolve unknown-token comments
python update_config.py
```

`update_config.py` fetches all currently active collateral mints from Offerbook, resolves symbol/name for every mint (both new ones and any already in the file), and:
- Appends new tokens (allocation defaults to `0.0` — opt-in to enable)
- Updates comments for previously-unknown tokens where the symbol is now resolved

Symbol resolution tries **Jupiter's token search API** first (`api.jup.ag/tokens/v2/search`, batched) — it indexes far more long-tail/pump.fun/meme tokens than Offerbook's own `/tokens` endpoint — then falls back to Offerbook's registry, then the hardcoded `KNOWN_TOKENS` table (which always wins on conflict). Allocation values already set are never touched, regardless of source.

## Expiring/overdue loan scanner (`soon_to_expire.py`)

Scans all active loans **platform-wide** and surfaces the ones already past their `expiredAt` (still marked "active" — not yet repaid or defaulted) or expiring within a configurable window. Read-only, no signing.

```bash
# Default: next 48h + already-expired bucket
python soon_to_expire.py

# Next 24h + already-expired
python soon_to_expire.py --hours 24

# Only the soon-to-expire window, skip the already-expired bucket
python soon_to_expire.py --no-expired
```

Exit code `1` if anything is in the expired/soon-to-expire window, `0` otherwise. `loan_watch_notify.py` (below) automates the "already expired" half of this on a schedule via email; this script is for an ad-hoc/manual look, including the "expiring soon but not yet due" window that the email watcher doesn't cover.

## Post-placement sanity check (`verify_offers.py`)

Run this immediately after placing orders to confirm every live offer has correct LTV, APY, duration, and principal volume.

```bash
# Check all three strategies
python verify_offers.py

# Check a specific strategy only
python verify_offers.py --days 3
python verify_offers.py --days 7
python verify_offers.py --days 15
```

For each offer the script prints a table row:

| Column | What is checked |
|---|---|
| `APY bps` / `APY %` | Must be ≥ 10 bps (the floor) |
| `LTV %` | Recomputed from fresh Jupiter/DexScreener prices; must be ≤ `Target%` |
| `Target%` | The dynamic LTV target ([§4](#4-dynamic-ltv-target-and-safe-collateral-sizing)) recomputed *right now* from fresh market-wide offer/loan data for that collateral token — not a fixed per-strategy number, and not necessarily the same value the strategy script computed at offer-creation time, since market conditions move |
| `Vol USDC` | Principal amount; flagged if dust (< 1 000 raw units) |
| `status` | `PASS` / `WARN` (non-critical) / `FAIL` (LTV violation) |

Exit code is `1` if any LTV violations are found, `0` otherwise — safe to use in shell pipelines:

```bash
python cancel_offers.py --days all
python strategy_3_days.py && python strategy_7_days.py && python strategy_15_days.py
python verify_offers.py   # non-zero exit = something is wrong
```

## Kill switch (`cancel_offers.py`)

Cancels open offers for a specific strategy or all at once. Always cancel before re-running strategies to avoid duplicate PDA conflicts.

```bash
# Interactive prompt — asks which strategy to cancel
python cancel_offers.py

# Skip prompt via flag
python cancel_offers.py --days 3
python cancel_offers.py --days 7
python cancel_offers.py --days 15
python cancel_offers.py --days all

# Also withdraw funds back to wallet after cancellation
python cancel_offers.py --days all --withdraw

# Dry run — preview only
DRY_RUN=true python cancel_offers.py
```

The script identifies each strategy's offers by their `duration` field (259 200 / 604 800 / 1 296 000 seconds) so only the right orders are touched.

## Collateral-coverage watchlist (`defaulter_watch.py`)

A read-only analytics scanner over the platform's full loan history (defaulted and repaid), used to identify borrowers whose positions have historically been fully covered by collateral value from a lender's perspective — a more direct signal of downside risk than repayment punctuality alone. It combines two signals per borrower:

- **Defaulted loans** where collateral value at default exceeded the outstanding principal (full recovery for whoever held the loan).
- **Repaid loans that closed after their `expiredAt`** (a late repayment the original lender chose not to enforce) where collateral value also exceeded principal at the time — i.e. the lender's capital was covered throughout regardless of repayment timing.

```bash
# Any borrower with positive historical collateral coverage
python defaulter_watch.py

# Only borrowers with more than $100 in aggregate historical surplus
python defaulter_watch.py --min-surplus 100

# Limit the reference table to the top 15 rows
python defaulter_watch.py --top 15
```

For each watchlisted borrower, the report flags two actionable conditions: an open borrow request right now, or an active loan expiring within 24h (they may return to borrow again). Never signs or submits anything — meant to be run periodically to catch these while they're still relevant. Exit code `1` if either condition applies to any watchlisted borrower, `0` otherwise.

It also surfaces first-time borrowers who have no resolved default/late-repay history yet but already have a loan sitting overdue right now — a signal the historical-surplus watchlist alone can't catch, since it only looks at loans that have already resolved. Every borrower who qualifies either way is upserted into `defaulter_config.yaml`, a private, ever-growing tracking ledger (first-seen date, defaults, late repayments, known surplus) that's gitignored and never committed — a personal risk record, not something published alongside the strategy code.

## Automated capture (`defaulter_capture.py`)

Reacts to the actionable conditions from `defaulter_watch.py` by posting a competitive lending offer into that same collateral pool — sized from `allocation_config.yaml` exactly like the strategy scripts, not a special override. Pricing targets the single largest live offer already in the pool (excluding our own) — the offer a borrower comparison-shopping the pool is actually most likely to pick, not a pool-wide average — and is bounded, not a race to win at any cost:

- **APY**: undercuts the largest offer's APY by a small, fixed margin.
- **LTV**: a small edge above the largest offer's LTV, capped by the same `effective_target_ltv()` safety ceiling used in the strategy scripts (§4) — a borrower's historical profitability never overrides this cap.
- **Duration**: matches the largest offer's own duration, since that's the specific listing being targeted.

A collateral not listed in `allocation_config.yaml` (or listed at 0%) is skipped, same as a normal strategy run.

```bash
# DRY_RUN is respected exactly like every other script here (see .env)
python defaulter_capture.py

# Only act on borrowers above a surplus threshold, matching defaulter_watch.py
python defaulter_capture.py --min-surplus 100

# Skip the signing-mode confirmation prompt
python defaulter_capture.py --yes
```

Every offer's principal, collateral, target LTV, and target APY is logged in full immediately before signing — one transaction at a time, so each can be verified before it lands on-chain. Exit code `1` if nothing was actionable (nothing to do), `0` otherwise.

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
python loan_watch_notify.py

# Manually trigger the GitHub Actions workflow instead of waiting for its schedule
gh workflow run loan_watch.yml
```

## Real-time deposit watch (`tg_deposit_watch.py`)

Unlike everything else above, this one is **not unattended**. It holds a live websocket subscription to Solana RPC (`accountSubscribe`) so a deposit to a watched wallet triggers a Telegram message within seconds — genuinely real-time, not polling — but only while it's actually running on your machine. Meant to be left open in a terminal (or `tmux`/`screen`) rather than deployed anywhere.

Watches any number of wallets at once. The watchlist persists in `tg_watchlist.json` (gitignored — reveals which wallets you're targeting, kept private the same way `defaulter_config.yaml` is) and can be managed three ways:

```bash
# One-shot CLI (no need to have the watcher running)
python tg_deposit_watch.py --add <wallet> [--mint <mint>] [--label <name>]
python tg_deposit_watch.py --remove <wallet>
python tg_deposit_watch.py --list

# Interactive console menu
python tg_deposit_watch.py

# Start the live watcher (long-running)
python tg_deposit_watch.py --watch
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

## Lender capital scanner (`lender_capital_scan.py`)

Reports wallet + escrow USDC for every wallet that is or has ever been a lender — anyone with an active loan out, an open lending offer (any status), or a lender on any resolved (repaid/defaulted) loan in the platform's full history, so a past lender with no current activity still shows up. A one-shot report, not a watcher: useful for sizing up how much free/redeployable capital your competition actually has before you post a big offer.

```bash
# Everyone, sorted by total descending
python lender_capital_scan.py

# Only lenders with more than $5,000 total
python lender_capital_scan.py --min-total 5000

# Limit the printed table to the top 20 rows
python lender_capital_scan.py --top 20

# Compare against saved state without overwriting it
python lender_capital_scan.py --no-save
```

Every run's balances persist to `lender_capital_state.json` (gitignored, like `defaulter_config.yaml`/`tg_watchlist.json` — this reveals your own competitive-intelligence tracking) and are compared against the previous run, so each report shows a **Δ since last** column per lender plus an overall change in the grand total. The very first run has nothing to compare against, so every lender shows `NEW`.

Also shows **last seen**: the most recent `createdAt`/`updatedAt` across all of a lender's Offerbook loan/offer records (active, open offer, repaid, or defaulted) — purely platform activity, not general wallet activity elsewhere. Computed for free from data already being fetched, no extra API calls. Distinguishes a currently-dominant lender from one who's actually gone quiet (e.g. large balance, but last active weeks ago).

Read-only, no signing. Exit code is always `0` — this is an informational report, not a pass/fail check.

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
MAX_OFFER_PRINCIPAL_USDC=50    # cap each offer at 50 USDC (0 = use full allocation)
ALLOCATION_CONFIG=path/to/allocation_config.yaml
```

### 3. Run

```bash
# Safe preview — no transactions submitted (Ledger signing by default, see below)
DRY_RUN=true python strategy_7_days.py

# Live — cancel first, then run all three strategies
python cancel_offers.py --days all
python strategy_3_days.py && python strategy_7_days.py && python strategy_15_days.py

# Prefer the hot wallet key instead of the Ledger?
python strategy_7_days.py --private-key
```

## Environment variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `OFFERBOOK_WALLET` | Yes | — | Your wallet public key |
| `OFFERBOOK_PRIVATE_KEY` | Yes (live) | — | Base58-encoded private key for signing |
| `DRY_RUN` | No | `false` | `true` to preview without submitting |
| `OFFERBOOK_API_BASE` | No | `https://api.offerbook.jup.ag/api/v1` | Read API base URL |
| `OFFERBOOK_TX_API_BASE` | No | — | Transaction builder API base URL |
| `SOLANA_RPC` | No | `https://api.mainnet-beta.solana.com` | Solana RPC endpoint |
| `MAX_OFFER_PRINCIPAL_USDC` | No | `0` | Per-offer USDC cap (0 = full allocation) |
| `ALLOCATION_CONFIG` | No | `allocation_config.yaml` | Path to allocation config file |
| `OFFERBOOK_SIGNING_MODE` | No | `ledger` | `ledger` or `private_key` — used by `cancel_offers.py` and the strategy scripts |
| `OFFERBOOK_LEDGER_PATH` | No | `44'/501'/0'` | BIP32 derivation path for Ledger signing |

## Signing modes

Every script (`cancel_offers.py`, `strategy_3_days.py`, `strategy_7_days.py`,
`strategy_15_days.py`) supports two signing modes — **Ledger is the default**:

- `--ledger` (default): signs via a Ledger hardware wallet over USB. Requires
  the Solana app open on-device and blind signing enabled (Offerbook's
  program isn't in Ledger's known-instruction registry). You approve each
  transaction with a physical button press — the private key never touches
  this machine. See `ledger_signer.py`.
- `--private-key`: signs with `OFFERBOOK_PRIVATE_KEY` from `.env` (hot wallet).

Every run prints the resolved signing mode and wallet address and asks for
confirmation before doing anything, so you always know which wallet/mode
you're about to act with. Pass `--yes` to skip that prompt.

**Review before you approve (Ledger mode):** since the Ledger's own screen
can't render Offerbook's custom instructions (blind signing), and a signed +
broadcast Ledger transaction can't be walked back the way a hot-wallet tx
sometimes can, every transaction's full detail — fee payer, every account
touched (with signer flags), and each instruction's program, accounts, and
data — is printed to the console right before the on-device approval prompt.
Read it before pressing the button.

```bash
python cancel_offers.py                 # Ledger signing (default), interactive
python cancel_offers.py --private-key   # hot wallet signing
python cancel_offers.py --ledger --days 7 --yes
python strategy_7_days.py --private-key --yes
```

## Testing against a single collateral

All three strategy scripts accept `--collateral <SYMBOL|mint>` to scope a run
to one collateral pair instead of every allocated market — useful for testing
signing or sizing changes without touching the rest of your allocation. Omit
it and every pair in `allocation_config.yaml` is processed as usual.

```bash
python strategy_3_days.py --collateral HYPE --yes
python strategy_7_days.py --collateral HYPE --yes
python strategy_15_days.py --collateral HYPE --yes
MAX_OFFER_PRINCIPAL_USDC=50 python strategy_3_days.py --collateral HYPE --yes
```

Note: with a single pair selected, the full per-pair allocation budget
(`allocation_config.yaml`) goes to that one market — use
`MAX_OFFER_PRINCIPAL_USDC` to size down a genuine test.

## Security

- Never commit your `.env` file — it is listed in `.gitignore`
- Always do a dry run first before going live
- `api-1.json` and `api-1 (2).json` are gitignored (internal API docs)
- `defaulter_config.yaml` (`defaulter_watch.py`'s private borrower-tracking ledger) is gitignored — it's a personal risk record, never published
- `loan_watch_notify.py`'s email credentials (`SMTP_FROM_EMAIL`, `SMTP_APP_PASSWORD`, `NOTIFY_EMAIL_TO`) live only in GitHub Actions secrets, never in a committed file
