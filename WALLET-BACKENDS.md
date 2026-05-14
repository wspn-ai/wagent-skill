# Wallet Backends

> **中文 → [WALLET-BACKENDS.zh-CN.md](WALLET-BACKENDS.zh-CN.md)**

`wagent` supports two swappable backends for signing on-chain payments. **Recommended pairing: `local` for test/dev, `okx` for production.**

## TL;DR

| | `PAYMENT_BACKEND=local` (default) | `PAYMENT_BACKEND=okx` |
|---|---|---|
| **Use when** | Dev / fast iteration / CI | Real-money production demo |
| **Chain** | Sepolia (default) or Ethereum mainnet (`LOCAL_NETWORK=mainnet`) | Base (default) / Ethereum / BSC / Arbitrum / Polygon (`OKX_NETWORK`). All mainnet — OKX has no testnet. |
| **Real money?** | No (Sepolia) / Yes (mainnet) | **Yes** |
| **Custody** | Plaintext private key in env | MPC (key shards) + Policy controls |
| **Auth** | env `AGENT_WALLET_PRIVATE_KEY` | `onchainos wallet login` (OTP) or `OKX_API_KEY` env |
| **Setup time** | 30 sec | ~5 min (portal + phone app) |

## How to switch

```bash
export PAYMENT_BACKEND=local      # default — Sepolia
# OR
export PAYMENT_BACKEND=okx        # Base mainnet, real money
```

Auto-detected at signing time. No restart, no rebuild. Keys/state are preserved when you switch back.

---

## Backend 1 — `local` (default)

web3.py-based local signing. **Defaults to Sepolia testnet** (free, safe). Set `LOCAL_NETWORK=mainnet` to sign on Ethereum mainnet (real money).

### Required env

```bash
PAYMENT_BACKEND=local                      # default if unset
AGENT_WALLET_PRIVATE_KEY=0xabcdef...       # your wallet private key
LOCAL_NETWORK=sepolia                      # sepolia (default) | mainnet

# Sepolia path:
SEPOLIA_RPC_URL=https://eth-sepolia.g.alchemy.com/v2/<your_key>

# Mainnet path:
MAINNET_RPC_URL=https://eth-mainnet.g.alchemy.com/v2/<your_key>
# Token contracts default to canonical USDC/USDT/WUSD addresses. Override only
# if you need a non-canonical contract — see "Token contracts" table below.
```

### Token contracts (built-in defaults)

The `local` backend resolves `--token` to one of these chain-native ERC-20s. **You shouldn't need to set the env overrides unless you're testing against a non-canonical deployment.**

| `--token`    | Sepolia (test)                                | Ethereum mainnet                             | Env override                |
|---           |---                                            |---                                           |---                          |
| `ETH_USDC`   | `0x1c7D4B196Cb0C7B01d743Fbc6116a902379C7238` | `0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48` | `ETH_USDC_CONTRACT`         |
| `ETH_WUSD`   | `0x371607f7463d27ae9deaf64ae00da9cbd4cf0065` | `0x7Cd017ca5ddb86861FA983a34b5F495C6F898c41` | `ETH_WUSD_CONTRACT`         |
| `ETH_USDT`   | *(no canonical Sepolia USDT — set `ETH_USDT_CONTRACT_SEPOLIA` to your test deploy)* | `0xdAC17F958D2ee523a2206206994597C13D831ec7` | `ETH_USDT_CONTRACT`         |

**WUSD specifically** — Sepolia and mainnet WUSD have different contract addresses (above). The skill picks the right one automatically based on `LOCAL_NETWORK`. Always confirm on a block explorer (Etherscan / Sepolia Etherscan) before sending real value, since WUSD contracts can be redeployed.

### Verify connectivity

```bash
python3 -c "from web3 import Web3; print(Web3(Web3.HTTPProvider('$SEPOLIA_RPC_URL')).eth.block_number)"
```

### Risks

- **Plaintext private key in env** — any process / log dump exposes it. Use a throwaway wallet.
- **No spend caps** — the agent can drain the wallet up to gas limit. Keep only what you're willing to lose.

### Sepolia faucets

| Source | Notes |
|---|---|
| <https://sepoliafaucet.com> | Alchemy faucet, 0.5 ETH per day |
| <https://www.infura.io/faucet/sepolia> | Requires Infura account |
| <https://faucet.quicknode.com/ethereum/sepolia> | QuickNode faucet |
| USDC faucet → <https://faucet.circle.com/> | Circle Sepolia USDC |

---

## Backend 2 — `okx` (production)

OKX Agentic Wallet — multi-chain mainnet, MPC custody + portal-side Policy controls. **Every chain is real money — OKX has no testnets.**

### Required env

```bash
PAYMENT_BACKEND=okx
OKX_NETWORK=base                          # base (default) | ethereum | bsc | arbitrum | polygon
OKX_TOKEN_CONTRACT=                       # optional — overrides chain's default USDC

# Either silent login:
OKX_API_KEY=oak_xxxxxxxxxxxxxxxx
# Or interactive (no env needed):
#   onchainos wallet login your-email@example.com
#   onchainos wallet verify <otp>
```

### Required setup

1. **OKX onboarding** — sign up at <https://web3.okx.com>, enable Agentic Wallet, complete phone-app verification. **Full step-by-step guide:** <https://web3.okx.com/zh-hans/onchainos/dev-docs/home/install-your-agentic-wallet>
2. **Spend Policies** — set per-tx and daily limits on <https://web3.okx.com/portfolio/agentic-wallet-policy>. Recommended starter: per-tx $5, daily $20, USDC-only.
3. **Fund the wallet** — top up the smallest amount you'd be willing to lose (e.g. $5 USDC on Base).

### Verify

```bash
onchainos wallet status
# → shows balance, default network, policy summary
```

### Risks

- **Real money.** A bug in `wagent` or W Connector can lose USDC. Set tight Policy limits before first use.
- **Mainnet gas.** Keep a small ETH/native buffer for fees. The skill will surface "insufficient gas" rather than silently failing.

---

## Backend selection cheatsheet

```
Test / dev (recommended)         → local (Sepolia default)
Self-custody on Eth mainnet      → local + LOCAL_NETWORK=mainnet (real money)
Production (recommended)         → okx (Base default) — see OKX onchainos setup link below
WUSD on Eth mainnet              → okx + OKX_NETWORK=ethereum + OKX_TOKEN_CONTRACT=0x7Cd017ca...
```

**Default progression:** start on `local` + Sepolia for testing. Switch to `okx` before going to production — MPC custody plus on-portal Policy controls (per-tx + daily caps) is a much better security posture than a hot private key in a server env.

OKX setup reference (Chinese, official): <https://web3.okx.com/zh-hans/onchainos/dev-docs/home/install-your-agentic-wallet>
