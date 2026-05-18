# wagent — pay-per-call AI agent skill

> **中文 → [README.zh-CN.md](README.zh-CN.md)**

`wagent` is a Claude / agent skill that lets your AI agent **discover**, **pay for**, and **invoke** paid API tools — using stablecoin on Ethereum mainnet — without you stepping in.

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

> **Production-only.** This skill pays real money on Ethereum mainnet. The connector URL (`connector.wcheckout.app`) is hardcoded — there is no testnet.

## What you get

- One [Anthropic-style skill](https://docs.claude.com/en/docs/agents-and-tools/agent-skills/overview) (`SKILL.md` + `scripts/`)
- Two CLI scripts:
  - `scripts/discover.py` — list every paid tool on the W Connector network
  - `scripts/buy.py` — purchase + invoke one tool call, end-to-end
- Two wallet backends — choose with `PAYMENT_BACKEND`:
  - **`okx`** (default, recommended for production) — OKX Agentic Wallet: MPC custody + TEE-signed Policy controls. Setup: <https://web3.okx.com/onchainos/dev-docs/home/install-your-agentic-wallet>
  - **`local`** — self-managed private key (web3.py). Use a throwaway wallet with $1–$5 for small-balance testing against real merchants.

## Quick start

```bash
# 1. Clone
git clone https://github.com/wspn-ai/wagent-skill.git
cd wagent-skill

# 2a. Default — OKX Agentic Wallet (production)
onchainos wallet login <your-email>
# Set per-tx + daily caps at:
#   https://web3.okx.com/portfolio/agentic-wallet-policy

# 2b. OR — local test wallet (small-balance experimentation)
#     pip install web3
#     export PAYMENT_BACKEND=local
#     export AGENT_WALLET_PRIVATE_KEY=0x<throwaway 64-hex>
#     export MAINNET_RPC_URL=https://eth-mainnet.g.alchemy.com/v2/<KEY>

# 3. See what's for sale
python3 scripts/discover.py

# 4. Buy + invoke a tool (use any agent_url from the discover.py output)
python3 scripts/buy.py \
  --agent-url "<merchant agent_url from discover.py>" \
  --product-id 101 \
  --quantity 1 \
  --call-body '{"address":"0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045"}'
```

Or load it into Claude Code as a skill and let the agent drive — full setup in **[SETUP.md](SETUP.md)**.

## Documentation

| Doc | What it covers |
|---|---|
| **[SETUP.md](SETUP.md)** / [zh-CN](SETUP.zh-CN.md) | Install + first run + connecting to Claude Code |
| **[TROUBLESHOOTING.md](TROUBLESHOOTING.md)** / [zh-CN](TROUBLESHOOTING.zh-CN.md) | Common errors and fixes |
| **[SKILL.md](SKILL.md)** | The skill manifest itself (read by the agent harness) |

## Cost expectations

| Backend | Network | Pricing | When to use |
|---|---|---|---|
| `okx` (default) | Ethereum mainnet | Real money — gas ~$0.50 + tool $0.10–$0.60 per call | Production |
| `local` | Ethereum mainnet | Same — but you control the key | Small-balance test wallet |

## How it works (60-second version)

1. **Discover** — `discover.py` calls `connector.wcheckout.app/merchants/search` then each merchant's `/.well-known/agent.json` to list every SKU.
2. **Purchase intent** — `buy.py` sends an A2A `tasks/send` request to the merchant. Shop creates a W Connector order and returns a payment address.
3. **Pay** — wagent signs an EVM transfer via OKX for the exact amount in the chosen token. Tx hash returns immediately.
4. **Settlement** — Stablelink (the payment rail W Connector uses) watches the chain, fires a webhook to the shop, shop relays "PAID" to W Connector.
5. **Token issuance** — wagent polls for the `delivery` artifact. Gets a bearer token good for 1 call within 24h.
6. **Call** — wagent `POST`s the user's input to `/v1/tools/<tool_name>` with the bearer. Result comes back. Token is burned.

The full A2A + payment protocol lives in W Connector; you don't need to learn it — just run the two scripts.

## Safety

- This skill makes real on-chain payments on every `buy.py` invocation. The skill prevalidates `--call-body` against each product's `input_schema` and refuses to pay if it doesn't match.
- One `buy.py` invocation = one order. Re-running the same command pays again. The skill warns if it detects a near-identical purchase within 30 minutes — pass `--force-new` to override.
- Tokens are persisted at `~/.wagent/tokens.json`. Use `--use-token <order_no>` to retry after a failed tool call (without paying again) or `--return-token <order_no>` to void.

## License

MIT — see [LICENSE](LICENSE).

## Issues, PRs

GitHub Issues and PRs welcome. For protocol-level questions about the W Connector network itself (merchant onboarding, settlement, gateway behavior), file an issue on the **[W Connector integration repo](https://github.com/wspn-ai/wconnector-integration/issues)** instead.
