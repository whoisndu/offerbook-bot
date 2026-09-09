# Strategy scripts

Scripts that place, fill, or cancel live Offerbook offers. All of them read `../lib/offerbook_common.py` for shared helpers and `./allocation_config.yaml` (gitignored — see [repo root README](../README.md#security)) for per-token allocation. See the [root README](../README.md) for setup, environment variables, and the overall repo layout.

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

**Size-band preference.** Both the APY benchmark and the LTV benchmark (§4) also prefer offers whose principal is within **0.5×–2×** our own offer's size, falling back to the full (self-excluded) set only if nothing qualifies — a lender an order of magnitude smaller or larger than us isn't a realistic comparison for a borrower shopping our size. Shared via `size_filtered_volume_weighted_median()` in `../lib/offerbook_common.py`.

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

A hard floor $r^{\ast} \geq r_{\min} = 0.05$ (500 bps / 5%) prevents posting at zero, negative, or near-zero yield.

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

**Gitignored** — like `../monitoring/defaulter_config.yaml`/`../monitoring/tg_watchlist.json`, this reveals your actual per-token risk tolerance and position sizing, so it's kept off git entirely rather than committed with example values. Copy the shape below into your own local `strategy/allocation_config.yaml`; the bot will pick it up automatically (or point `ALLOCATION_CONFIG` at wherever you keep it).

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
python strategy/update_config.py --dry-run

# Add new tokens and resolve unknown-token comments
python strategy/update_config.py
```

`update_config.py` fetches all currently active collateral mints from Offerbook, resolves symbol/name for every mint (both new ones and any already in the file), and:
- Appends new tokens (allocation defaults to `0.0` — opt-in to enable)
- Updates comments for previously-unknown tokens where the symbol is now resolved

Symbol resolution tries **Jupiter's token search API** first (`api.jup.ag/tokens/v2/search`, batched) — it indexes far more long-tail/pump.fun/meme tokens than Offerbook's own `/tokens` endpoint — then falls back to Offerbook's registry, then the hardcoded `KNOWN_TOKENS` table (which always wins on conflict). Allocation values already set are never touched, regardless of source.

## Bulk offer cancellation (`cancel_offers.py`)

Cancels open offers for a specific strategy or all at once. Always cancel before re-running strategies to avoid duplicate PDA conflicts.

```bash
# Interactive prompt — asks which strategy to cancel
python strategy/cancel_offers.py

# Skip prompt via flag
python strategy/cancel_offers.py --days 1
python strategy/cancel_offers.py --days 3
python strategy/cancel_offers.py --days 7
python strategy/cancel_offers.py --days 15
python strategy/cancel_offers.py --days all

# Also withdraw funds back to wallet after cancellation
python strategy/cancel_offers.py --days all --withdraw

# Dry run — preview only
DRY_RUN=true python strategy/cancel_offers.py
```

The script identifies each strategy's offers by their `duration` field (86 400 / 259 200 / 604 800 / 1 296 000 seconds) so only the right orders are touched. Offers are cancelled in batches of **15** per transaction (`BATCH_SIZE`) — tested against the live builder API at 955 bytes/tx, comfortably under Solana's 1232-byte transaction limit (20/batch was tested too but left too little headroom, only 3%, given offers can vary slightly in account count).

## Fill a single offer (`fill_offer.py`)

Fully fills one live offer by pubkey — either a "borrowing" offer (someone posted collateral wanting principal; you fill it as the lender) or a "lending" offer (someone posted principal wanting collateral; you fill it as the borrower). Offer type is auto-detected from the live offer data.

Fetches the offer fresh right before building the fill transaction (so amounts reflect current `remainingPrincipal`/`remainingCollateral`, not whatever was seen earlier), prints a preview, and asks for confirmation before any signing — same safety pattern as the other scripts here.

In Ledger mode you're interactively prompted which account to sign with (unless `--ledger-path` is given) — this script has no "right" account, it depends what you're filling and with what. Same `KNOWN_LEDGER_ACCOUNTS` labels as `cancel_offers.py`'s picker (`44'/501'/0'` = general strategy, `44'/501'/1'` = targeted-offers), plus a custom-path option.

```bash
python strategy/fill_offer.py --offer <pubkey>
python strategy/fill_offer.py --offer <pubkey> --ledger-path "44'/501'/1'"   # skip the account prompt
python strategy/fill_offer.py --offer <pubkey> --private-key
python strategy/fill_offer.py --offer <pubkey> --yes
DRY_RUN=true python strategy/fill_offer.py --offer <pubkey>   # preview without submitting
```

## Automated capture (`defaulter_capture.py`)

Reacts to the actionable conditions from [`../monitoring/defaulter_watch.py`](../monitoring/README.md#collateral-coverage-watchlist-defaulter_watchpy) by posting a competitive lending offer into that same collateral pool — sized from `allocation_config.yaml` exactly like `strategy.py`, not a special override. Pricing targets the single largest live offer already in the pool (excluding our own) — the offer a borrower comparison-shopping the pool is actually most likely to pick, not a pool-wide average — and is bounded rather than maximally aggressive:

- **APY**: undercuts the largest offer's APY by a small, fixed margin.
- **LTV**: a small edge above the largest offer's LTV, capped by the same `effective_target_ltv()` safety ceiling used in `strategy.py` (§4) — a borrower's historical profitability never overrides this cap.
- **Duration**: matches the largest offer's own duration, since that's the specific listing being targeted.

A collateral not listed in `allocation_config.yaml` (or listed at 0%) is skipped, same as a normal strategy run.

```bash
# DRY_RUN is respected exactly like every other script here (see .env)
python strategy/defaulter_capture.py

# Only act on borrowers above a surplus threshold, matching defaulter_watch.py
python strategy/defaulter_capture.py --min-surplus 100

# Skip the signing-mode confirmation prompt
python strategy/defaulter_capture.py --yes
```

Every offer's principal, collateral, target LTV, and target APY is logged in full immediately before signing — one transaction at a time, so each can be verified before it lands on-chain. Exit code `1` if nothing was actionable (nothing to do), `0` otherwise.

Note: `defaulter_capture.py` depends on `../monitoring/defaulter_watch.py` directly (`from defaulter_watch import ...`) — its `sys.path` shim adds both `../lib` and `../monitoring`, since it's the one script in this folder that reaches across into another.

## Signing modes

Every script (`cancel_offers.py`, `strategy.py`, `fill_offer.py`) supports two signing modes —
**Ledger is the default**:

- `--ledger` (default): signs via a Ledger hardware wallet over USB. Requires
  the Solana app open on-device and blind signing enabled (Offerbook's
  program isn't in Ledger's known-instruction registry). You approve each
  transaction with a physical button press — the private key never touches
  this machine. See `../lib/ledger_signer.py`.
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
python strategy/cancel_offers.py                 # Ledger signing (default), prompts for strategy AND account
python strategy/cancel_offers.py --private-key   # hot wallet signing
python strategy/cancel_offers.py --ledger --days 7 --yes
python strategy/strategy.py --days 7                          # prompts which Ledger account to run on
python strategy/strategy.py --days 7 --ledger-path "44'/501'/1'"  # skip that prompt
python strategy/strategy.py --days 7 --private-key --yes
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
python strategy/strategy.py --days 1 --collateral HYPE --yes
python strategy/strategy.py --days 3 --collateral HYPE --yes
python strategy/strategy.py --days 3,7 --collateral HYPE --yes
python strategy/strategy.py --days 1,3,7 --collateral CARDS,ANSEM,URANUS --yes
MAX_OFFER_PRINCIPAL_USDC=50 python strategy/strategy.py --days 3 --collateral HYPE --yes
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
python strategy/strategy.py --days 1,3,7 --collateral CARDS,ANSEM,URANUS --full-alloc --yes
```

If `--collateral` is omitted (interactive mode), you're also prompted
"Override allocation to 100%... this run only?" right after naming specific
tokens. This prompt is skipped entirely when `--collateral` is passed on the
CLI, so scripted/cron runs never block on it — pass `--full-alloc` explicitly
if you want the override in that case.

## `create_targeted_offers.py` and `update_config.py`

`create_targeted_offers.py` is gitignored and deliberately undocumented here — see [root README's Security section](../README.md#security) for why. `update_config.py` is documented above under [Syncing the config](#syncing-the-config).
