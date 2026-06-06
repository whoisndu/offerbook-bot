# Offerbook Competitive Lending Bot

An automated lending bot for the [Offerbook](https://offerbook.jup.ag) protocol on Solana. It scans active lending offers and loans, then posts competitive lending offers at 10% below the market mean APY.

## Strategy

1. Fetch all active lending offers and loans from the Offerbook API
2. Group them by `(principalMint, collateralMint)` pair
3. For each pair, compute the mean APY from active lending offers — falling back to existing loan APYs if no offers exist yet (letting the bot enter loan-only markets)
4. Post a new lending offer at `mean APY × 90%` to stay competitive
5. Size each offer using the median principal/collateral amounts from the market, scaled down proportionally if the offer exceeds your available balance

## Constraints

| Parameter | Value |
|---|---|
| Max loan duration | 7 days |
| Max LTV | 40% |
| Min APY floor | 10 bps (0.10%) |
| APY undercut | 10% below market mean |
| Partial fills | Allowed |

## Setup

### 1. Install dependencies

```bash
python -m venv venv
source venv/bin/activate
pip install requests python-dotenv solders base58
```

### 2. Configure environment

Copy the example below into a `.env` file in the project root:

```env
OFFERBOOK_WALLET=<your-wallet-pubkey>
OFFERBOOK_PRIVATE_KEY=<your-base58-private-key>
DRY_RUN=true
```

Optional overrides:

```env
OFFERBOOK_API_BASE=https://api.offerbook.jup.ag/api/v1
OFFERBOOK_TX_API_BASE=<tx-api-url>        # contact Offerbook team for current URL
SOLANA_RPC=https://api.mainnet-beta.solana.com
MAX_OFFER_PRINCIPAL_USDC=50               # cap each offer at 50 USDC (0 = use market median)
```

### 3. Run

```bash
# Safe preview — no transactions submitted
DRY_RUN=true python offerbook_bot.py

# Live mode
DRY_RUN=false python offerbook_bot.py
```

## Balance management

At startup the bot fetches your **wallet USDC balance + Offerbook escrow balance** and uses the combined total as the available budget. Offers are sized to fit within that budget:

- If a market's median offer size exceeds your remaining balance, the offer is scaled down proportionally (preserving the LTV ratio) rather than skipped
- Set `MAX_OFFER_PRINCIPAL_USDC` to cap individual offer sizes and spread your balance across more pairs

## Environment variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `OFFERBOOK_WALLET` | Yes | — | Your wallet public key |
| `OFFERBOOK_PRIVATE_KEY` | Yes (live mode) | — | Base58-encoded private key for signing |
| `DRY_RUN` | No | `false` | Set to `true` to preview without submitting |
| `OFFERBOOK_API_BASE` | No | `https://api.offerbook.jup.ag/api/v1` | Read API base URL |
| `OFFERBOOK_TX_API_BASE` | No | — | Transaction builder API base URL |
| `SOLANA_RPC` | No | `https://api.mainnet-beta.solana.com` | Solana RPC endpoint |
| `MAX_OFFER_PRINCIPAL_USDC` | No | `0` (market median) | Per-offer USDC cap in whole USDC |

## Security

- Never commit your `.env` file — it is listed in `.gitignore`
- Always do a dry run first to verify offer parameters before going live
