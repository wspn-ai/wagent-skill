# wagent — pay-per-call AI agent skill

> **中文版本 → [README.zh-CN.md](README.zh-CN.md)**

`wagent` is a Claude / agent skill that lets your AI agent **discover**, **pay for**, and **invoke** paid API tools — using stablecoin on Ethereum (or Sepolia testnet) — without you stepping in.

The agent thinks → finds a tool that solves the user's task → pays USDC/USDT/WUSD → gets a single-use bearer token → calls the tool → returns the result.

```
"Check if 0x...abcd is sanctioned" ─┐
                                    ▼
                      agent (Claude + wagent skill)
                                    │
                          1. discover marketplace
                          2. pick a sanctions tool
                          3. pay $0.30 USDC
                          4. call the tool
                          5. return: "no OFAC hit"
```

## What you get

- One [Anthropic-style skill](https://docs.claude.com/en/docs/agents-and-tools/agent-skills/overview) (`SKILL.md` + `scripts/`)
- Two CLI scripts:
  - `scripts/discover.py` — list every paid tool available on the W Connector network
  - `scripts/buy.py` — purchase + invoke one tool call, end-to-end
- Two wallet backends — **recommended pairing: `local` for test, `okx` for production**:
  - **`local`** (default) — sign locally with a private key on **Sepolia testnet** (free, safe). The right choice for dev / demo / CI.
  - **`okx`** — delegate to OKX Agentic Wallet with TEE-signed policy controls. The right choice for production. Mainnet only — [setup docs](https://web3.okx.com/zh-hans/onchainos/dev-docs/home/install-your-agentic-wallet).

## Quick start

```bash
# 1. Clone
git clone https://github.com/wspn-ai/wagent-skill.git
cd wagent-skill

# 2. Pick a backend (Sepolia testnet is free; pick this to play)
#    Easy path: copy .env.example to .env and edit. Or use `export` as below.
cp .env.example .env       # then edit
# OR:
export PAYMENT_BACKEND=local
export LOCAL_NETWORK=sepolia
export AGENT_WALLET_PRIVATE_KEY=0x...   # any throwaway key

# 3. See what's for sale
python3 scripts/discover.py

# 4. Buy + run a tool (use any agent_url from the discover.py output)
python3 scripts/buy.py \
  --agent-url "<merchant agent_url from discover.py>" \
  --product-id 101 \
  --quantity 1 \
  --token ETH_USDC \
  --call-body '{"address":"0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045"}'
```

Or load it into Claude Code as a skill and let the agent drive — full setup in **[SETUP.md](SETUP.md)**.

## Documentation

| Doc | What it covers |
|---|---|
| **[SETUP.md](SETUP.md)** / [zh-CN](SETUP.zh-CN.md) | Install + first run + connecting to Claude Code |
| **[WALLET-BACKENDS.md](WALLET-BACKENDS.md)** / [zh-CN](WALLET-BACKENDS.zh-CN.md) | Local web3.py vs OKX Agentic Wallet — when to use which |
| **[TROUBLESHOOTING.md](TROUBLESHOOTING.md)** / [zh-CN](TROUBLESHOOTING.zh-CN.md) | Common errors and fixes |
| **[SKILL.md](SKILL.md)** | The skill manifest itself (read by the agent harness) |

## Network / cost expectations

| Backend | Network | Pricing | Setup time | When to use |
|---|---|---|---|---|
| `local` + Sepolia | Testnet | **Free** (faucet ETH + faucet USDC) | 5 min | **Recommended for dev / test** |
| `local` + mainnet | Ethereum | Real money — gas ~$0.50 + tool $0.10–$0.60 | 10 min | Self-custody on mainnet |
| `okx` | Base / Ethereum / BSC / Arbitrum / Polygon | Real money — same scale | 15 min ([OKX setup](https://web3.okx.com/zh-hans/onchainos/dev-docs/home/install-your-agentic-wallet)) | **Recommended for production** |

**OKX has no testnet.** If you want to play around without spending, stay on `local` + Sepolia. When you graduate to production, switch to `okx` for MPC custody + Policy controls instead of running a hot private key on a server.

## How it works (60-second version)

1. **Discover** — `discover.py` calls W Connector's `/merchants/search` then each merchant's `/.well-known/agent.json` to list every SKU.
2. **Purchase intent** — `buy.py` sends an A2A `tasks/send` request to the merchant's shop. Shop creates a W Connector order, returns a payment address.
3. **Pay** — wagent signs an EVM transfer (locally or via OKX) for the exact amount in the chosen token. Hash returns immediately.
4. **Settlement** — Stablelink (the payment rail W Connector uses) watches the chain, fires a webhook to the shop, shop relays "PAID" to W Connector.
5. **Token issuance** — wagent polls for the `delivery` artifact. Gets a bearer token good for 1 call (`calls_per_unit: 1`) within 24h.
6. **Call** — wagent `POST`s the user's input to `/v1/tools/<tool_name>` with the bearer. Result comes back. Token is burned.

The full A2A + payment protocol lives in W Connector; you don't need to learn it — just run the two scripts.

## Safety

- This skill makes real on-chain payments when `PAYMENT_BACKEND=local LOCAL_NETWORK=mainnet` or `PAYMENT_BACKEND=okx`. Validate your `--call-body` matches each product's `input_schema` (the skill does this for you and refuses to pay if it doesn't).
- One `buy.py` invocation = one order. Re-running the same command pays again. The skill warns if it detects a near-identical purchase within 5 minutes — pass `--force-new` to override.
- Tokens are persisted at `~/.wagent/tokens.json`. Use `--use-token <order_no>` to re-call (when `calls_per_unit > 1`, which isn't current default) or `--return-token <order_no>` to dispose.

## License

MIT — see [LICENSE](LICENSE).

## Issues, PRs

GitHub Issues and PRs welcome. For protocol-level questions (anything about how the W Connector network itself works — merchant onboarding, settlement, gateway behavior), file an issue on the **[W Connector integration repo](https://github.com/wspn-ai/wconnector-integration/issues)** instead.
