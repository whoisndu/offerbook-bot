# Offerbook Competitive Lending Bot

An automated lending bot for the [Offerbook](https://offerbook.jup.ag) protocol on Solana. It scans active lending offers, posts competitive lending offers sized to your per-collateral allocation, and sizes collateral against a **dynamic, per-token LTV target** using real-time prices from Jupiter and DexScreener.

## Strategies

Three independent scripts — run whichever suits your risk appetite. Each offer listing expires after **24 hours** and is re-posted on the next run.

| Script | Loan term | LTV floor (thin market data) | LTV hard ceiling | APY target |
|---|---|---|---|---|
| `strategy_3_days.py` | 3 days | **60%** | 75% | Mean + 10% |
| `strategy_7_days.py` | 7 days | **45%** | 75% | Mean − 10% |
| `strategy_15_days.py` | 15 days | **25%** | 75% | Mean − 12% |

LTV is no longer a single fixed ceiling — it's computed per collateral token from that token's own live market data, bounded by the floor and ceiling above. See [§4](#4-dynamic-ltv-target-and-safe-collateral-sizing) for the full rule.

### How it works

1. Fetch all active lending offers and loans from the Offerbook API
2. Group by `(principalMint, collateralMint)` pair
3. Compute the **volume-weighted mean APY** from live offers of the same duration — large offers carry more weight than small high-APY outliers. If no same-duration offers exist for a pair, fall back to the global mean across all durations. The log shows which source was used: `[from live offers (same duration)]` or `[from live offers (global)]`
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
| $\bar\ell_k$ | Volume-weighted mean LTV of token $k$'s own live market offers/loans |
| $\alpha_k \in [0,1]$ | Allocation fraction for collateral token $k$ |
| $B$ | Combined lender budget: $B = B_{\text{wallet}} + B_{\text{escrow}}$ |

---

### 1. Volume-Weighted Mean APY

A naïve arithmetic mean over APYs is distorted by small, high-yield fringe offers. The bot instead computes a **volume-weighted mean** so that larger, more liquid offers dominate the benchmark:

$$\bar{r}_{vw}(\mathcal{S}) = \frac{\displaystyle\sum_{i \in \mathcal{S}} r_i \cdot p_i}{\displaystyle\sum_{i \in \mathcal{S}} p_i}, \qquad \mathcal{S} \neq \emptyset$$

---

### 2. Duration-Stratified Benchmarking with Fallback

Offers of different durations reflect different risk premia and should not be pooled blindly. The benchmark APY for strategy $d$ is:

$$\bar{r}^{(d)} = \begin{cases} \bar{r}_{vw}(\mathcal{O}_d) & \text{if } \mathcal{O}_d \neq \emptyset \\ \bar{r}_{vw}(\mathcal{O}) & \text{otherwise} \end{cases}$$

The log records which branch was taken (`[from live offers (same duration)]` vs `[from live offers (global)]`).

---

### 3. APY Target

Each strategy positions itself relative to the benchmark by applying a scalar adjustment $\delta$:

$$r^* = \bar{r}^{(d)} \cdot (1 + \delta)$$

| Strategy | $d$ | $\delta$ | Rationale |
|---|---|---|---|
| `strategy_3_days.py` | 3 days | $+0.10$ | Short duration; higher yield acceptable |
| `strategy_7_days.py` | 7 days | $-0.10$ | Mid duration; slight undercut to attract flow |
| `strategy_15_days.py` | 15 days | $-0.12$ | Long duration; deeper undercut offsets illiquidity |

A hard floor $r^* \geq r_{\min} = 0.001$ (10 bps) prevents posting at zero or negative yield.

---

### 4. Dynamic LTV Target and Safe Collateral Sizing

The **loan-to-value ratio** of a proposed offer is:

$$\text{LTV} = \frac{P \cdot \pi_p}{C \cdot \pi_c}$$

Unlike a single fixed ceiling, the target LTV $L_k$ for collateral token $k$ is computed from **that token's own live market data** — every other lender's open offers/loans against the same token (always vs. USDC, the only principal Offerbook supports). Let $\mathcal{S}_k$ be that set, with per-entry LTV $\ell_i$ and principal-USD weight $p_i$; the volume-weighted market LTV is:

$$\bar\ell_k = \frac{\sum_{i \in \mathcal{S}_k} \ell_i \, p_i}{\sum_{i \in \mathcal{S}_k} p_i}$$

"Enough data" to trust $\mathcal{S}_k$ means **either** $|\mathcal{S}_k| \geq 5$, **or** the total volume $V_k = \sum_{i \in \mathcal{S}_k} p_i$ is at least $2\times$ our own offer's principal $P$ — a few small dust offers shouldn't qualify, but one large, capital-backed offer can stand on its own even with fewer than 5 orders on the book (Offerbook's escrow model means posting a lending offer requires the lender to actually fund the principal, so a large offer costs real capital to fake). $L_k$ is then set by three rules, applied in order:

1. **Not enough data** (neither condition above holds): fall back to the strategy's flat floor, $L_k = L_{\text{floor}}$ (60% / 45% / 25% — see the table above).
2. **Young token** (enough data, and the token's earliest known trading pool is under 60 days old, or its age can't be determined at all — treated as young, fail-safe): $L_k = \bar\ell_k - 0.05$, i.e. 5 points more conservative than the token's own market.
3. **Mature token** (enough data, and age $\geq$ 60 days): $L_k = \bar\ell_k / 0.9$ — the bot accepts 10% less collateral than the market average implies, making its offer more attractive to borrowers than the going rate for tokens with an established track record.

In every case, $L_k$ is clamped to $[0.05,\ 0.75]$ — the 75% hard ceiling applies no matter how loose an established token's market looks, since an entire market trading at very high LTV is itself a warning sign rather than something to mirror.

Given $L_k$, the minimum collateral the borrower must post is:

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
