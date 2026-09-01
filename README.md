# Offerbook Competitive Lending Bot

An automated lending bot for the [Offerbook](https://offerbook.jup.ag) protocol on Solana. It scans active lending offers, posts competitive lending offers sized to your per-collateral allocation, and sizes collateral against a **dynamic, per-token LTV target** using real-time prices from Jupiter and DexScreener.

## Strategies

One script, `strategy.py`, covering four calibrated loan durations — it prompts for which one(s) to run (or accepts `--days`), and can run more than one in the same invocation (e.g. `--days 1,3,7`). It also prompts for whether to run across every allocated pair or target specific token(s) only (or accepts `--collateral`) — see [Targeting specific collateral](#targeting-specific-collateral) below. Each offer listing expires after **24 hours** and is re-posted on the next run.

| Duration | LTV floor (thin market data) | LTV hard ceiling | APY target |
|---|---|---|---|
| 1 day | **70%** | 75% | Benchmark − 5% |
| 3 days | **65%** | 75% | Benchmark − 5% |
| 7 days | **45%** | 75% | Benchmark − 10% |
| 15 days | **25%**, plus an extra 25% collateral premium (see §4) | 75% | Benchmark − 12% |

Only these four durations are supported. A new tier only gets added once its own LTV/discount are explicitly chosen and calibrated against that duration's actual live market, same as these four were — the 1-day floor (70%) in particular came from the live 1-day market's own volume-weighted median LTV at the time, not extrapolation from the 3/7/15-day trend (which would have wrongly suggested an even looser floor).

LTV is no longer a single fixed ceiling — it's computed per collateral token from that token's own live market data, bounded by the floor and ceiling above. See [§4](#4-dynamic-ltv-target-and-safe-collateral-sizing) for the full rule.

If a pair already has a live (active/partially-filled) offer of ours at the exact same duration, it's skipped for that run rather than stacking a duplicate offer on top — re-running the same `--days` selection is safe and idempotent.

### How it works

1. Fetch all active lending offers and loans from the Offerbook API
2. Group by `(principalMint, collateralMint)` pair
3. Compute the **volume-weighted median APY** from live offers of the same duration — the price level where the largest cluster of real market volume sits, not a mean (which one large outlier offer can drag far from where borrowers are actually transacting). If no same-duration offers exist for a pair, fall back to the global median across all durations. The log shows which source was used: `[from live offers (same duration)]` or `[from live offers (global)]`
4. Fetch real-time collateral prices from **Jupiter Price API** (primary) with **DexScreener** as fallback
5. For each pair, compute the token's **dynamic LTV target** (§4) from its own market data and current token age, then size `collateralAmount` to hit that target at **current prices** — not stale prices from other lenders' old offers. If no live price is available from either source, the pair is skipped entirely rather than sized off a stale pool-implied price (see below)
6. **Cross-validate the live price** against the pool-implied price from existing loans. If the two differ enough that the offer's true LTV would exceed the dynamic target, skip the pair and log a warning (guards against bad price feeds)
7. Set `principalAmount` to your configured allocation fraction of your total USDC balance (wallet + escrow), rounded down to the nearest $500 (or $100 if the allocation is under one $500 step — see `ROUND_STEP_USDC`/`ROUND_SMALL_STEP_USDC`) so offer sizes read as round figures instead of odd cents
8. Post the offer with `allowPartialFill = true` and a fixed `minFillAmount` of **$10** (`MIN_FILL_USDC`) so borrowers can take any amount from $10 up to the full offer

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

$$\tilde{r}_{vw}(\mathcal{S}) = r_{(j^{\ast})}, \qquad j^{\ast} = \min\left\lbrace j : \sum_{k=1}^{j} p_{(k)} \geq \frac{W}{2} \right\rbrace, \qquad \mathcal{S} \neq \emptyset$$

Unlike a mean, one very large offer can only shift the median by contributing weight toward whichever side of the distribution it sits on — it can never single-handedly drag the benchmark toward its own extreme rate.

**Self-exclusion.** $\mathcal{O}$ (and every set derived from it below — $\mathcal{O}_d$, a token's own market $\mathcal{S}_k$, the largest-offer trackers) always excludes our own wallet's offers and loans. Otherwise, once we have a live offer in a pair, it counts as "the market" for computing our own *next* target — our own large offer skews the benchmark toward itself, and the next run targets even further in that direction. This was a real, measured bug: on one collateral, including our own $29K offers pulled the LTV benchmark to 73.6% and APY to 36.0%, versus 58.8%/12.0% once self-excluded, since our own offers dwarfed every other lender's size.

**Size-band preference.** Both the APY benchmark and the LTV benchmark (§4) also prefer offers whose principal is within **0.5×–2×** our own offer's size, falling back to the full (self-excluded) set only if nothing qualifies — a lender an order of magnitude smaller or larger than us isn't a realistic comparison for a borrower shopping our size. Shared via `size_filtered_volume_weighted_median()` in `offerbook_common.py`.

---

### 2. Duration-Stratified Benchmarking with Fallback

Offers of different durations reflect different risk premia and should not be pooled blindly. The benchmark APY for strategy $d$ is:

$$\tilde{r}^{(d)} = \begin{cases} \tilde{r}_{vw}(\mathcal{O}_d) & \text{if } \mathcal{O}_d \neq \emptyset \\ \tilde{r}_{vw}(\mathcal{O}) & \text{otherwise} \end{cases}$$

The log records which branch was taken (`[from live offers (same duration)]` vs `[from live offers (global)]`).

The LTV benchmark $\tilde\ell_k$ (§4) uses the identical same-duration/global-fallback split — it's at least as duration-sensitive as APY (the LTV floor alone spans 70% at 1 day down to 25% at 15 days), so pooling every duration's LTV together risked dragging, say, a 1-day target toward unrelated 15-day-style offers just because they happened to have more volume.

---

### 3. APY Target

Each strategy positions itself relative to the benchmark by applying a scalar adjustment $\delta$:

$$r^{\ast} = \tilde{r}^{(d)} \cdot (1 + \delta)$$

| Duration | $d$ | $\delta$ | Rationale |
|---|---|---|---|
| 1 day | 1 day | $-0.05$ | Same shallow undercut as 3-day — thinnest data of any tier, so a noisy benchmark shouldn't be undercut aggressively |
| 3 days | 3 days | $-0.05$ | Shallower undercut than 7/15-day — this strategy's higher LTV floor already compensates for its risk, so it doesn't also need to price above market |
| 7 days | 7 days | $-0.10$ | Mid duration; slight undercut to attract flow |
| 15 days | 15 days | $-0.12$ | Long duration; deeper undercut offsets illiquidity |

A hard floor $r^{\ast} \geq r_{\min} = 0.001$ (10 bps) prevents posting at zero or negative yield.

**Cheapest-comparable-offer guardrail.** Let $\mathcal{C} \subseteq \mathcal{O}_d$ be the same-duration, size-band-preferred set from §1 (excluding our own offers), and $r_{\min}(\mathcal{C}) = \min_{i \in \mathcal{C}} r_i$ the lowest (cheapest, most borrower-friendly) APY among them. The final target is capped **down**, never up:

$$r^{\ast} \leftarrow \min(r^{\ast},\ r_{\min}(\mathcal{C}))$$

This replaced an earlier guardrail that floored $r^{\ast}$ **up** to the pool's single largest live offer's APY regardless of duration or size — measured to be actively counterproductive: on one collateral it forced a 40% APY floor onto every duration because one unrelated large offer happened to charge 40%, even when same-duration, similarly-sized competitors charged as little as 8–17%. The goal is to compete to be the cheapest (or at least on par with the cheapest) real comparable offer, not to avoid undercutting whichever offer happens to be biggest.

---

### 4. Dynamic LTV Target and Safe Collateral Sizing

The **loan-to-value ratio** of a proposed offer is:

$$\text{LTV} = \frac{P \cdot \pi_p}{C \cdot \pi_c}$$

Unlike a single fixed ceiling, the target LTV $L_k$ for collateral token $k$ is computed from **that token's own live market data** — every other lender's open offers/loans against the same token (always vs. USDC, the only principal Offerbook supports). Let $\mathcal{S}_k$ be that set, with per-entry LTV $\ell_i$ and principal-USD weight $p_i$; the volume-weighted median market LTV $\tilde\ell_k$ is computed the same way as $\tilde{r}_{vw}$ in §1 (weighted by $p_i$ instead of over APYs), so a single large outlier LTV can't drag the benchmark away from where the bulk of market volume actually sits.

"Enough data" to trust $\mathcal{S}_k$ means **either** $|\mathcal{S}_k| \geq 5$, **or** the total volume $V_k = \sum_{i \in \mathcal{S}_k} p_i$ is at least $2\times$ our own offer's principal $P$ — a few small dust offers shouldn't qualify, but one large, capital-backed offer can stand on its own even with fewer than 5 orders on the book (Offerbook's escrow model means posting a lending offer requires the lender to actually fund the principal, so a large offer costs real capital to fake). $\mathcal{S}_k$ itself is duration-stratified exactly like §2 (same-duration preferred, falling back to all-duration only if none exists at that exact duration) and self-excluded/size-band-preferred exactly like §1. $L_k$ is then set by three rules, applied in order:

1. **Not enough data** (neither condition above holds): fall back to the strategy's flat floor, $L_k = L_{\text{floor}}$ (70% / 65% / 45% / 25% — see the table above).
2. **Young token** (enough data, and the token's earliest known trading pool is under 60 days old, or its age can't be determined at all — treated as young, fail-safe): $L_k = \tilde\ell_k - 0.05$, i.e. 5 points more conservative than the token's own market.
3. **Mature token** (enough data, and age $\geq$ 60 days): $L_k = \tilde\ell_k / 0.9$ — the bot accepts 10% less collateral than the market median implies, making its offer more attractive to borrowers than the going rate for tokens with an established track record.

**Largest-offer guardrail (unchanged from earlier — LTV, unlike APY in §3, still caps down against the single largest live offer).** Let $i^{\ast} = \arg\max_{i \in \mathcal{O}} p_i$ be the pair's single largest live offer (any duration, self-excluded). If its LTV $\ell_{i^{\ast}}$ is *more* conservative (lower) than whatever $L_k$ would otherwise be, the target is capped down to match it:

$$L_k \leftarrow \min(L_k,\ \ell_{i^{\ast}})$$

This can only make the result safer, never looser — it guards against the volume-weighted benchmark being skewed by several small, thin offers into a target more permissive than what the market's most prominent participant actually accepts. This guardrail is deliberately left duration-agnostic and untouched even though §3's APY guardrail was reworked — a conservative cap on collateral risk is a different judgment call than a competitiveness target, and was kept as-is on request.

**15-day collateral premium.** The 15-day tier carries more price-movement/default exposure over its longer window than any other duration. On top of everything above, for $d \geq 15$ days the result is additionally divided by a fixed premium factor $\rho = 1.25$:

$$L_k \leftarrow L_k / \rho$$

i.e. 15-day always requires **25% more collateral** than the pool's typical LTV implies — dynamic, not a fixed ceiling, since it scales with whatever the pool actually trades at rather than pinning 15-day to one hardcoded number. (Measured effect: on one collateral this took the 15-day target from 39.0% — previously identical to 7-day, since both were capped by the same duration-agnostic largest-offer guardrail — down to 31.2%.)

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

**Gitignored** — like `defaulter_config.yaml`/`tg_watchlist.json`, this reveals your actual per-token risk tolerance and position sizing, so it's kept off git entirely rather than committed with example values. Copy the shape below into your own local `allocation_config.yaml`; the bot will pick it up automatically (or point `ALLOCATION_CONFIG` at wherever you keep it).

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

Run this immediately after placing orders to confirm every live offer has correct LTV, APY, duration, and principal volume — it recomputes the exact same dynamic LTV target `strategy.py` would (same-duration/size-band preference, self-exclusion, the 15-day collateral premium, all of it) against fresh market data, not a cached copy of the logic.

```bash
# Interactive prompt — asks which strategy to check
python verify_offers.py

# Skip the prompt via flag
python verify_offers.py --days 1
python verify_offers.py --days 3
python verify_offers.py --days 7
python verify_offers.py --days 15
python verify_offers.py --days all
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
python strategy.py --days 3,7,15 --yes
python verify_offers.py   # non-zero exit = something is wrong
```

## Bulk offer cancellation (`cancel_offers.py`)

Cancels open offers for a specific strategy or all at once. Always cancel before re-running strategies to avoid duplicate PDA conflicts.

```bash
# Interactive prompt — asks which strategy to cancel
python cancel_offers.py

# Skip prompt via flag
python cancel_offers.py --days 1
python cancel_offers.py --days 3
python cancel_offers.py --days 7
python cancel_offers.py --days 15
python cancel_offers.py --days all

# Also withdraw funds back to wallet after cancellation
python cancel_offers.py --days all --withdraw

# Dry run — preview only
DRY_RUN=true python cancel_offers.py
```

The script identifies each strategy's offers by their `duration` field (86 400 / 259 200 / 604 800 / 1 296 000 seconds) so only the right orders are touched. Offers are cancelled in batches of **15** per transaction (`BATCH_SIZE`) — tested against the live builder API at 955 bytes/tx, comfortably under Solana's 1232-byte transaction limit (20/batch was tested too but left too little headroom, only 3%, given offers can vary slightly in account count).

## Fill a single offer (`fill_offer.py`)

Fully fills one live offer by pubkey — either a "borrowing" offer (someone posted collateral wanting principal; you fill it as the lender) or a "lending" offer (someone posted principal wanting collateral; you fill it as the borrower). Offer type is auto-detected from the live offer data.

Fetches the offer fresh right before building the fill transaction (so amounts reflect current `remainingPrincipal`/`remainingCollateral`, not whatever was seen earlier), prints a preview, and asks for confirmation before any signing — same safety pattern as the other scripts here.

In Ledger mode you're interactively prompted which account to sign with (unless `--ledger-path` is given) — this script has no "right" account, it depends what you're filling and with what. Same `KNOWN_LEDGER_ACCOUNTS` labels as `cancel_offers.py`'s picker (`44'/501'/0'` = general strategy, `44'/501'/1'` = targeted-offers), plus a custom-path option.

```bash
python fill_offer.py --offer <pubkey>
python fill_offer.py --offer <pubkey> --ledger-path "44'/501'/1'"   # skip the account prompt
python fill_offer.py --offer <pubkey> --private-key
python fill_offer.py --offer <pubkey> --yes
DRY_RUN=true python fill_offer.py --offer <pubkey>   # preview without submitting
```

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

Reacts to the actionable conditions from `defaulter_watch.py` by posting a competitive lending offer into that same collateral pool — sized from `allocation_config.yaml` exactly like `strategy.py`, not a special override. Pricing targets the single largest live offer already in the pool (excluding our own) — the offer a borrower comparison-shopping the pool is actually most likely to pick, not a pool-wide average — and is bounded rather than maximally aggressive:

- **APY**: undercuts the largest offer's APY by a small, fixed margin.
- **LTV**: a small edge above the largest offer's LTV, capped by the same `effective_target_ltv()` safety ceiling used in `strategy.py` (§4) — a borrower's historical profitability never overrides this cap.
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

## New borrow-request watch (`borrow_offer_watch.py`)

Emails on every newly-appearing open borrow request platform-wide — every principal, every collateral type (including NFTs), no profitability filter. This is the raw feed; for "is this one worth acting on" see `arbitrage_scanner.py` below, which only alerts on borrow requests that clear a profitable spread against a live lending offer for the same collateral.

Runs every 15 minutes via `.github/workflows/borrow_offer_watch.yml`. Dedup works like `arbitrage_scanner.py`'s: state is the set of currently-open borrow-offer pubkeys, persisted to `borrow_offer_watch_state.json` (committed back to the repo by the workflow — these are public open offers, not competitive intel, so unlike `competitor_timing_state.json` there's nothing here worth keeping private). Each run only emails offers not already in that set, then overwrites the state with exactly this run's live set — anything no longer open (filled, cancelled, expired) simply stops appearing next run.

```bash
python borrow_offer_watch.py                        # all open borrow requests, email if new ones found
python borrow_offer_watch.py --min-size 20           # ignore requests under $20 principal
python borrow_offer_watch.py --principal-mint <mint> # only this principal token
python borrow_offer_watch.py --no-email              # console output only, skip email + state
```

Required env vars for email (GitHub Actions secrets, shared with `loan_watch_notify.py`): `SMTP_FROM_EMAIL`, `SMTP_APP_PASSWORD`, `NOTIFY_EMAIL_TO`. `OFFERBOOK_WALLET` is used to exclude our own borrow requests, if any — a warning is logged (not skipped) if unset, same as the other scan scripts.

## Wallet transaction watch (`wallet_tx_watch.py`)

Polls a watchlist of arbitrary Solana addresses via `.github/workflows/wallet_watch.yml` (every ~15 min) and sends a Telegram alert on **any** new transaction for a watched wallet — not just token transfers, unlike `tg_deposit_watch.py` below, and unattended, unlike it too.

State (the watchlist + last-seen transaction signature per wallet + a Telegram update offset) lives in `wallet_watch_state.json`, committed back by the workflow — same pattern as `loan_watch_state.json`. A wallet's first poll after being watched only records a baseline (no notification for its pre-existing history), so watching a long-lived active wallet doesn't flood you with years of past transactions.

Manage the watchlist two ways — from your machine, or live from Telegram (commands land on the next scheduled run, so there's up to ~15 min latency):

```bash
python wallet_tx_watch.py --watch <address> [--label <name>]
python wallet_tx_watch.py --unwatch <address>
python wallet_tx_watch.py --list
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
python arbitrage_scanner.py                  # top 15 spreads, email if new ones found
python arbitrage_scanner.py --top 30
python arbitrage_scanner.py --min-spread 20   # only spreads >= 20 APY points
python arbitrage_scanner.py --min-size 50     # ignore legs under $50 available
python arbitrage_scanner.py --no-email        # console output only
```

Runs every ~15 min via `.github/workflows/arbitrage_scan.yml`. Emails (reusing `loan_watch_notify.py`'s SMTP secrets) only for **newly appearing** spreads — deduped by the exact `(borrow-offer, lend-offer)` pubkey pair in `arbitrage_scanner_state.json` (committed back by the workflow), so a still-open opportunity doesn't re-email every run; state resets to exactly what's currently live each run, so filled/cancelled/expired offers drop out automatically.

A flagged spread is a market scan, not a recommendation — read it with the same caveats it prints: size at the best rate is often small, the two legs' durations may not line up, and a high lend-side APY usually exists because that specific token/position carries real default risk, not because it's mispriced.

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

## Realized PNL leaderboard (`pnl_leaderboard.py`)

Ranks every Offerbook lender by all-time realized PNL. There's no "top by PNL" endpoint on the API — only volume-based leaderboards (`/metrics/top-lenders`) — so this pulls the full repaid + defaulted loan history platform-wide (no borrower/lender filter) and aggregates client-side.

Realized PNL per lender =

- **+ net interest earned on repaid loans.** Interest is converted to USD via the platform's documented proportional formula (`interest / principalAmount * startPrincipalAmountUsd`), then the actual protocol "repay" fee charged is subtracted — taken straight from `metadata.fees.repay.amountUsd` per loan, not assumed as a flat rate.
- **+ collateral kept on defaulted loans**, valued at default time (`endCollateralAmountUsd`), minus the principal that was lent out and not recovered (`startPrincipalAmountUsd`). This is a mark-to-market figure at the moment of default, not necessarily cash actually realized — if the lender is still holding the seized collateral, it's unrealized from here.

```bash
python pnl_leaderboard.py              # top 25 by realized PNL
python pnl_leaderboard.py --top 50
```

Read-only, never signs or submits anything.

## Borrower loan timeline (`borrower_loan_timeline.py`)

Plots one borrower's full loan history for a given collateral as a Gantt-style timeline (one bar per loan, colored by outcome — on-time/late/defaulted/active, labeled with each loan's principal size) plus a concurrent-open-loans step chart underneath, so gaps in activity are easy to spot and label with their length in days directly on the chart. Useful for answering "does this borrower take breaks, and how often" at a glance rather than by reading a table.

```bash
python borrower_loan_timeline.py                      # interactive prompts for collateral/borrower
python borrower_loan_timeline.py --collateral USELESS
python borrower_loan_timeline.py --collateral USELESS --borrower 4nFMipa1LwA6QQiVk29YqZeCvHixbWMMjcBR1h7jDMrZ
python borrower_loan_timeline.py --collateral USELESS --output /some/other/path.png
```

Omitting `--borrower` auto-picks the largest borrower (by total USD principal) for that collateral. Charts save to `~/Desktop/borrower_timeline_<borrower8>.png` by default. Read-only, no signing.

## Competing-offer posting-time chart (`offer_posting_times.py`)

Charts WHEN competing lenders post their offers for a given collateral, so you can time `strategy.py` runs to land after most of the day's competing volume is already on the book, instead of undercutting a thin, partially-posted market. Pulls every lending offer for that collateral across every status (active/partiallyFilled/fulfilled/cancelled/expired) over a lookback window — not just what's live right now, since currently-live offers alone are capped by the platform's 24h expiry and only show a partial day. Your own offers are excluded by default.

```bash
python offer_posting_times.py                      # prompts for collateral
python offer_posting_times.py --collateral PUMP
python offer_posting_times.py --collateral all      # every collateral together
python offer_posting_times.py --collateral PUMP --days-back 14
python offer_posting_times.py --collateral PUMP --tz America/New_York
python offer_posting_times.py --collateral PUMP --coverage 0.9
```

Plots a scatter of posting time (date vs. hour-of-day, colored by status) to show whether the daily rhythm is consistent, plus an hourly histogram with a cumulative-%-of-USD-volume line to make the busiest posting hours obvious. Prints a recommended "post after HH:00" time — the first hour by whose end `--coverage` (default 80%) of a typical day's competing USD volume has historically posted. Charts save to `~/Desktop/offer_posting_times_<label>.png` by default. Read-only, no signing.

## Competitor posting-time distillation report (`competitor_timing_report.py`)

`strategy.py` posts ALL of its offers in one batch run rather than trickling them out — so the timing question that matters isn't "when do most offers for one token get posted" (that's `offer_posting_times.py`), it's "when has essentially every top competitor across the WHOLE market already posted for the day," so a single run can undercut everyone's fresh pricing at once. This pulls every lending offer platform-wide (every collateral pair) over a rolling lookback window, ranks lenders by total USD volume in that window, and profiles both the aggregate market rhythm and each top lender's individual posting hours. Two local ML techniques (no external API, no billing) turn that into more than a bigger table: **k-means clustering** (scikit-learn) groups top lenders by the *shape* of their 24-hour posting profile into behavioral archetypes ("morning poster", "evening poster", etc. — K chosen automatically via silhouette score, scaling up to 6 clusters as more lenders become available rather than a fixed small ceiling), and **linear regression** (numpy) fits day-index vs. daily competing USD volume to report whether competition is intensifying or cooling off. Clustering is used for the hour-of-day question specifically because it's circular (23:00 and 00:00 are adjacent) — a plain regression would mishandle that, and even the archetype label itself is derived from each cluster's centroid rather than averaging members' individual peak hours, for the same reason. Lenders with fewer than 3 offers in the window are excluded from clustering (not enough data for a meaningful shape) but still appear in the ranked list with their own post-after time.

```bash
python competitor_timing_report.py                    # 14-day lookback, top 20 lenders, emails the report
python competitor_timing_report.py --days-back 21
python competitor_timing_report.py --top-lenders 15
python competitor_timing_report.py --tz America/New_York
python competitor_timing_report.py --no-email          # console output only, skip email + state
python competitor_timing_report.py --heatmap-top 5     # chart more than the top 3 lenders
python competitor_timing_report.py --no-chart          # skip the heatmap PNG
```

Also saves a heatmap PNG (hour-of-day x top-3-lenders by default, `~/Desktop/competitor_top_lenders_heatmap.png`) — each row is normalized to that lender's own daily volume (not raw dollars), so the #1 lender's much larger absolute volume doesn't wash out everyone else's row, and a dashed line marks the market-wide recommended post-after hour for direct comparison. Chart saving is best-effort — a failure (e.g. no writable Desktop) logs a warning rather than failing the run, since the email is the primary deliverable.

Runs every 2 days via `.github/workflows/competitor_timing_report.yml`, which explicitly pins `--tz Africa/Lagos` (WAT, fixed UTC+1, no DST) since the GitHub Actions runner defaults to UTC — run locally without `--tz` and it uses the machine's own local timezone instead, which only matches if that machine is also on WAT/UTC+1. The workflow saves the heatmap to a repo-relative path (not `~/Desktop`, which doesn't exist on the runner) and uploads it as a downloadable build artifact on the run's summary page. Prior-run stats persist to `competitor_timing_state.json` so each report can call out drift — the recommended hour shifting, a top lender's own timing changing, new names entering the top ranks — but that file is gitignored and **never committed** (it's competitive-intelligence tracking, same reasoning as `lender_capital_state.json`); the workflow persists it across runs via `actions/cache` instead. Needs only the existing `SMTP_*` secrets and `OFFERBOOK_WALLET` (used only to exclude our own offers, never to sign) — no external API key, since the clustering/regression run locally. Read-only, no signing.

## Shared code (`offerbook_common.py`)

Logic that used to be copy-pasted across scripts now lives in one place and gets imported, not duplicated:

- **Ledger signing** — `get_ledger_signer()`, `resolve_signer_wallet()`, `confirm_signing_mode()`. Used by `strategy.py`, `cancel_offers.py`, `fill_offer.py`, `defaulter_capture.py`, `create_targeted_offers.py` (via `defaulter_capture.py`), and `verify_offers.py` (read-only wallet resolution only, never signs).
- **`prompt_for_ledger_path()`** — the interactive "which Ledger account do you want to run on?" prompt (see "Signing modes"), accepting a bare account index or a full derivation path. Used by `strategy.py`; `cancel_offers.py`/`fill_offer.py` use their own labeled-picker variant of the same idea (`KNOWN_LEDGER_ACCOUNTS`) since those two have no "right" default account.
- **HTTP client + pagination** — `api_get()`, `post_tx()`, `fetch_all_pages()`. Used by every script that talks to the Offerbook API.
- **CLI signing flags** — `add_signing_args()` / `resolve_signing_mode()` add and validate the `--ledger`/`--private-key`/`--yes` flags shared by every signing-capable script.
- **`KNOWN_DECIMALS` / `KNOWN_SYMBOLS`** — the canonical token → decimals / display-symbol tables (24 tokens). Every script that needs either imports these instead of keeping its own copy, so a token added here is immediately recognized everywhere (`strategy.py`, `verify_offers.py`, `underwater.py`, `defaulter_capture.py`, `defaulter_watch.py`, `soon_to_expire.py`, `loan_watch_notify.py`).
- **`_volume_weighted_median()`** — the same volume-weighted-median helper `strategy.py`, `defaulter_capture.py`, and `verify_offers.py` all use for LTV/APY benchmarking (§1, §4), so all three price and risk-check offers off the exact same statistic.
- **`size_filtered_volume_weighted_median()`** — wraps the above with the 0.5×–2× size-band preference from §1, shared by `strategy.py`'s APY and LTV benchmarks.
- **`round_principal_raw()`** — rounds a raw principal amount down to a round whole-dollar figure ($500 step, or $100 if under one step), used by `strategy.py` so offer sizes read like 11,500.00 rather than 11,800.35.
- **`_mint_from_asset()`** — extracts a mint address from an OfferAsset, used anywhere offer/loan JSON needs parsing.

Each script that's itself imported elsewhere for these helpers (e.g. `create_targeted_offers.py` calling `defaulter_capture.resolve_signer_wallet()`) keeps a thin same-signature wrapper around the shared function, so nothing calling into it had to change.

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
DRY_RUN=true python strategy.py --days 7

# Live — cancel first, then run all four durations in one invocation
python cancel_offers.py --days all
python strategy.py --days 1,3,7,15

# Omit --days and --collateral to be prompted interactively for both instead
python strategy.py

# Prefer the hot wallet key instead of the Ledger?
python strategy.py --days 7 --private-key
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
| `OFFERBOOK_SIGNING_MODE` | No | `ledger` | `ledger` or `private_key` — used by `cancel_offers.py`, `strategy.py`, `fill_offer.py`, and `defaulter_capture.py` |
| `OFFERBOOK_LEDGER_PATH` | No | `44'/501'/0'` | BIP32 derivation path for Ledger signing — in `strategy.py`, `cancel_offers.py`, and `fill_offer.py` this is only the fallback offered at the interactive account prompt (see "Signing modes" below), not used silently |
| `TELEGRAM_BOT_TOKEN` | No | — | Bot token from @BotFather — used by `wallet_tx_watch.py` and `tg_deposit_watch.py` |
| `TELEGRAM_CHAT_ID` | No | — | Your chat id — same two scripts |

`SMTP_FROM_EMAIL` / `SMTP_APP_PASSWORD` / `NOTIFY_EMAIL_TO` (used by `loan_watch_notify.py`, `borrow_offer_watch.py`, `arbitrage_scanner.py`, and `competitor_timing_report.py`) are **not** meant to go in `.env` — they live only as GitHub Actions secrets (`gh secret set <NAME>`), since those scripts are meant to run unattended on a schedule, not locally.

## Signing modes

Every script (`cancel_offers.py`, `strategy.py`, `fill_offer.py`) supports two signing modes —
**Ledger is the default**:

- `--ledger` (default): signs via a Ledger hardware wallet over USB. Requires
  the Solana app open on-device and blind signing enabled (Offerbook's
  program isn't in Ledger's known-instruction registry). You approve each
  transaction with a physical button press — the private key never touches
  this machine. See `ledger_signer.py`.
- `--private-key`: signs with `OFFERBOOK_PRIVATE_KEY` from `.env` (hot wallet).

**Which Ledger account:** in Ledger mode, `strategy.py`, `cancel_offers.py`,
and `fill_offer.py` all interactively ask which account (derivation path) to
sign with — there's no silent default, so a run never quietly lands on the
wrong account. `cancel_offers.py`/`fill_offer.py` show a small labeled picker
(`KNOWN_LEDGER_ACCOUNTS`, e.g. "Original / general strategy account" for
`44'/501'/0'`, "Targeted-offers account" for `44'/501'/1'`) plus a custom-path
option; `strategy.py` uses the equivalent shared prompt in
`offerbook_common.prompt_for_ledger_path()`, which accepts either a bare
account index (`0`, `1`, ...) or a full derivation path. Pass `--ledger-path
"44'/501'/N'"` on any of them to skip the prompt entirely (e.g. for cron/
automation).

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

The console output also prints a **Message Hash** (SHA-256 of the exact
message bytes about to be signed, base58-encoded) matching what the device
itself shows during blind signing — confirmed against the `LedgerHQ/app-solana`
firmware source (`handle_sign_message.c`) and a real device screen, so the
encoding is verified, not guessed. Compare it against your Ledger screen
before approving: a mismatch means the bytes about to be signed aren't the
ones printed to the console.

```bash
python cancel_offers.py                 # Ledger signing (default), prompts for strategy AND account
python cancel_offers.py --private-key   # hot wallet signing
python cancel_offers.py --ledger --days 7 --yes
python strategy.py --days 7                          # prompts which Ledger account to run on
python strategy.py --days 7 --ledger-path "44'/501'/1'"  # skip that prompt
python strategy.py --days 7 --private-key --yes
```

## Targeting specific collateral

`strategy.py` accepts `--collateral <SYMBOL|mint>[,<SYMBOL|mint>...]` to scope
a run to one or more specific collateral pairs instead of every allocated
market — useful for testing signing or sizing changes without touching the
rest of your allocation, or for a quick run on just a few tokens. Omit it and
you're prompted interactively (run across everything, or name specific
tokens); pass it and every pair in `allocation_config.yaml` is processed as
usual.

```bash
python strategy.py --days 1 --collateral HYPE --yes
python strategy.py --days 3 --collateral HYPE --yes
python strategy.py --days 3,7 --collateral HYPE --yes
python strategy.py --days 1,3,7 --collateral CARDS,ANSEM,URANUS --yes
MAX_OFFER_PRINCIPAL_USDC=50 python strategy.py --days 3 --collateral HYPE --yes
```

Note: with specific pairs selected, the full per-pair allocation budget
(`allocation_config.yaml`) goes to each of those markets — use
`MAX_OFFER_PRINCIPAL_USDC` to size down a genuine test.

### One-time 100% allocation override (`--full-alloc`)

`--full-alloc` overrides `allocation_config.yaml` to 100% for just the
token(s) named in `--collateral`, for that run only — `allocation_config.yaml`
itself is never modified. Useful when running a few specific tokens on a low
balance, where the configured fractions (e.g. 60%) would split it too thin.
Requires `--collateral` — it refuses to blanket-override every token in the
config at once.

```bash
python strategy.py --days 1,3,7 --collateral CARDS,ANSEM,URANUS --full-alloc --yes
```

If `--collateral` is omitted (interactive mode), you're also prompted
"Override allocation to 100%... this run only?" right after naming specific
tokens. This prompt is skipped entirely when `--collateral` is passed on the
CLI, so scripted/cron runs never block on it — pass `--full-alloc` explicitly
if you want the override in that case.

## Security

- Never commit your `.env` file — it is listed in `.gitignore`
- Always do a dry run first before going live
- `api-1.json` and `api-1 (2).json` are gitignored (internal API docs)
- `allocation_config.yaml` is gitignored — it reveals your actual per-token risk tolerance and position sizing
- `defaulter_config.yaml` (`defaulter_watch.py`'s private borrower-tracking ledger) is gitignored — it's a personal risk record, never published
- `tg_watchlist.json` (`tg_deposit_watch.py`'s watchlist) is gitignored for the same reason
- `create_targeted_offers.py` is gitignored — it's a borrower-specific targeted-offer tool built around one counterparty's historical repayment pattern, deliberately kept out of the public, general-purpose strategy code
- `loan_watch_notify.py`'s and `arbitrage_scanner.py`'s email credentials (`SMTP_FROM_EMAIL`, `SMTP_APP_PASSWORD`, `NOTIFY_EMAIL_TO`) live only in GitHub Actions secrets, never in a committed file
- `wallet_watch_state.json` and `arbitrage_scanner_state.json` **are** committed (unlike the gitignored files above) — they only ever hold public on-chain data (pubkeys, signatures, amounts) or, for the wallet watchlist, addresses you've chosen to track. If that watchlist itself needs to stay private, don't commit it — ask before adding a sensitive address to a tracked state file
- `main` has branch protection blocking force-pushes and branch deletion. It does **not** require PR review — that was tried and reverted after it broke the GitHub Actions state-commit workflows (`GH006: Protected branch update failed`, since `enforce_admins=false` doesn't exempt the `github-actions[bot]` identity, only human admin accounts). The only collaborator with push access is the repo owner, and no workflow triggers on `pull_request`/`pull_request_target`, so a third party's PR can't be merged or executed automatically regardless
