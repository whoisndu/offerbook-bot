# Offerbook Competitive Lending Bot

An automated lending bot for the [Offerbook](https://offerbook.jup.ag) protocol on Solana. It scans active lending offers, posts competitive lending offers sized to your per-collateral allocation, and enforces LTV limits using real-time prices from Jupiter and DexScreener.

## Strategies

Three independent scripts — run whichever suits your risk appetite. Each offer listing expires after **24 hours** and is re-posted on the next run.

| Script | Loan term | Max LTV | APY target | Collateral ratio |
|---|---|---|---|---|
| `strategy_3_days.py` | 3 days | **65%** | Mean + 10% | ≥ 1.54× loan |
| `strategy_7_days.py` | 7 days | **45%** | Mean − 10% | ≥ 2.22× loan |
| `strategy_15_days.py` | 15 days | **25%** | Mean − 12% | ≥ 4× loan |

### How it works

1. Fetch all active lending offers and loans from the Offerbook API
2. Group by `(principalMint, collateralMint)` pair
3. Compute the **volume-weighted mean APY** from live offers of the same duration — large offers carry more weight than small high-APY outliers. If no same-duration offers exist for a pair, fall back to the global mean across all durations. The log shows which source was used: `[from live offers (same duration)]` or `[from live offers (global)]`
4. Fetch real-time collateral prices from **Jupiter Price API** (primary) with **DexScreener** as fallback
5. For each pair, compute `collateralAmount` to enforce the strategy's max LTV at **current prices** — not stale prices from other lenders' old offers
6. **Cross-validate the live price** against the pool-implied price from existing loans. If the two differ enough that the offer's true LTV would exceed `MAX_LTV`, skip the pair and log a warning (guards against bad price feeds)
7. Set `principalAmount` to your configured allocation fraction of your total USDC balance (wallet + escrow)
8. Post the offer with `allowPartialFill = true` so borrowers can take any amount up to the full offer

### Price feed safety

Prices are fetched from Jupiter first, DexScreener second. After computing the required collateral amount, the bot cross-checks it against the **pool-implied price** — the price inferred from existing loans and offers for the same collateral. If the live price is stale or wrong (e.g. a DexScreener pool with low liquidity returning an anomalous price), the collateral requirement will be far too low at real market prices. The bot detects this and skips rather than posting an undercollateralised offer.

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
| $L_{\max}$ | Maximum loan-to-value ratio permitted by the strategy |
| $\alpha_k \in [0,1]$ | Allocation fraction for collateral token $k$ |
| $B$ | Combined lender budget: $B = B_{\text{wallet}} + B_{\text{escrow}}$ |

---

### 1. Volume-Weighted Mean APY

A naïve arithmetic mean over APYs is distorted by small, high-yield fringe offers. The bot instead computes a **volume-weighted mean** so that larger, more liquid offers dominate the benchmark:

$$\bar{r}_{vw}(\mathcal{S}) = \frac{\displaystyle\sum_{i \in \mathcal{S}} r_i \cdot p_i}{\displaystyle\sum_{i \in \mathcal{S}} p_i}, \qquad \mathcal{S} \neq \emptyset$$

---

### 2. Duration-Stratified Benchmarking with Fallback

Offers of different durations reflect different risk premia and should not be pooled blindly. The benchmark APY for strategy $d$ is:

$$\bar{r}^{(d)} = \begin{cases} \bar{r}_{vw}(\mathcal{O}_d) & \text{if } \mathcal{O}_d \neq \emptyset \\[6pt] \bar{r}_{vw}(\mathcal{O}) & \text{otherwise} \end{cases}$$

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

### 4. LTV Enforcement and Safe Collateral Sizing

The **loan-to-value ratio** of a proposed offer is:

$$\text{LTV} = \frac{P \cdot \pi_p}{C \cdot \pi_c}$$

To guarantee $\text{LTV} \leq L_{\max}$, the minimum collateral the borrower must post is:

$$C_{\min} = \frac{P \cdot \pi_p}{L_{\max} \cdot \pi_c}$$

The bot sets $C = C_{\min}$, using prices fetched at offer-posting time (not prices embedded in stale third-party offers).

| Strategy | $L_{\max}$ | Implied collateral ratio $C/P$ at $\pi_p = \pi_c = 1$ |
|---|---|---|
| 3 days | 0.65 | $\approx 1.54\times$ |
| 7 days | 0.45 | $\approx 2.22\times$ |
| 15 days | 0.25 | $4\times$ |

---

### 5. Price Cross-Validation

Even after computing $C_{\min}$ from a live price feed, that price may itself be unreliable (stale oracle, thin pool). The bot cross-validates by computing the **pool-implied collateral price** from existing on-chain loans for the same pair:

$$\hat{\pi}_c = \frac{P_{\text{ref}} \cdot \pi_p}{C_{\text{ref}}}$$

where $(P_{\text{ref}}, C_{\text{ref}})$ are the principal and collateral from a reference loan. The implied LTV under the live price is then:

$$\widehat{\text{LTV}}_{\text{live}} = \frac{P_{\text{ref}} \cdot \pi_p}{C_{\text{ref}} \cdot \pi_c}$$

If $\widehat{\text{LTV}}_{\text{live}} > L_{\max}$, the live price is inconsistent with market-observed collateralisation — the bot skips the pair and logs a warning. This guards against posting an under-collateralised offer when a price feed returns an anomalously high $\pi_c$.

---

### 6. Budget Allocation

Let $K$ be the set of eligible collateral tokens for a given strategy run. The principal for pair $k$ is:

$$P_k = \alpha_k \cdot B, \qquad \alpha_k \in [0, 1]$$

The bot uses a `topup: minimum` escrow strategy: it draws from on-chain escrow first and pulls from the wallet only the shortfall $\max(0,\ P_k - B_{\text{escrow},k})$. This minimises unnecessary wallet-to-escrow transfers.

If `MAX_OFFER_PRINCIPAL_USDC` $= M > 0$, the effective principal is capped:

$$P_k^{\text{eff}} = \min(P_k,\ M)$$

---

## Allocation config (`allocation_config.yaml`)

Controls what fraction of your total USDC balance you're willing to offer per collateral token. Tokens listed here also **bypass the LTV filter** (you're explicitly trusting them); unlisted tokens go through the LTV filter first.

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

`update_config.py` fetches all currently active collateral mints from Offerbook, looks up their symbol/name, and:
- Appends new tokens (allocation defaults to `0.0` — opt-in to enable)
- Updates comments for previously-unknown tokens where the symbol is now resolved

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
pip install requests python-dotenv solders base58 pyyaml
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
# Safe preview — no transactions submitted
DRY_RUN=true python strategy_7_days.py

# Live — cancel first, then run all three strategies
python cancel_offers.py --days all
python strategy_3_days.py && python strategy_7_days.py && python strategy_15_days.py
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

## Security

- Never commit your `.env` file — it is listed in `.gitignore`
- Always do a dry run first before going live
- `api-1.json` and `api-1 (2).json` are gitignored (internal API docs)
