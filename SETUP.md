# Setup — wagent skill

> **中文 → [SETUP.zh-CN.md](SETUP.zh-CN.md)**

End-to-end first run, from `git clone` to a successful tool call. ~10 minutes.

## Prerequisites

- Python 3.10+
- `pip install web3` (for `local` backend) or OKX onboarding (for `okx` backend, see [WALLET-BACKENDS.md](WALLET-BACKENDS.md))
- An Alchemy / Infura / QuickNode key for an Ethereum RPC endpoint (Sepolia is fine and free)
- A test wallet — **never use a key holding funds you care about**

## Step 1 — Clone

```bash
git clone https://github.com/<owner>/wagent-skill.git
cd wagent-skill
```

## Step 2 — Pick a wallet backend

**Test / dev — use `local` + Sepolia testnet** (free, can't lose anything). All steps below assume this.

**Production — use `okx` (OKX Agentic Wallet)** — MPC custody + Policy controls so a buggy agent can't drain your wallet. Setup: <https://web3.okx.com/zh-hans/onchainos/dev-docs/home/install-your-agentic-wallet>. After OKX is set up, jump to [WALLET-BACKENDS.md §Backend 2](WALLET-BACKENDS.md#backend-2--okx-production).

You can configure two ways — **`.env` file is the easy path**:

```bash
# Easy path: copy the template and edit values
cp .env.example .env
# Open .env in your editor and uncomment PAYMENT_BACKEND, LOCAL_NETWORK,
# AGENT_WALLET_PRIVATE_KEY, SEPOLIA_RPC_URL. The scripts auto-load `.env`
# on every run.

# OR: shell exports (use this if you prefer not to write a file)
export PAYMENT_BACKEND=local
export LOCAL_NETWORK=sepolia
export AGENT_WALLET_PRIVATE_KEY=0x<your-throwaway-key>
export SEPOLIA_RPC_URL=https://eth-sepolia.g.alchemy.com/v2/<your_alchemy_key>
```

Both work — shell env always wins over `.env`, so `.env` is safe to leave around as a default while you override with `export` for one-off tests. `.env` is in `.gitignore`, so you won't accidentally commit secrets.

Generate a throwaway private key — `python3 -c "import secrets; print('0x' + secrets.token_hex(32))"`. **Never reuse a key holding real funds.** Fund the address with Sepolia ETH from <https://sepoliafaucet.com> and Sepolia USDC from <https://faucet.circle.com>.

For real money / mainnet / OKX → see [WALLET-BACKENDS.md](WALLET-BACKENDS.md).

## Step 3 — Smoke test connectivity

```bash
python3 -c "from web3 import Web3; print('latest block:', Web3(Web3.HTTPProvider('$SEPOLIA_RPC_URL')).eth.block_number)"
# → latest block: <some number>
```

If that errors, fix your `SEPOLIA_RPC_URL` first. Nothing else works without it.

## Step 4 — Discover available tools

```bash
python3 scripts/discover.py
```

You should see a table similar to this (exact merchants, prices, and URLs depend on who's live at the moment — **don't copy these URLs verbatim; use whatever your own output shows**):

```
product_id  merchant        tool_name               $/pack    pack   $/call   agent_url
─────────────────────────────────────────────────────────────────────────────────────────
101         AgentPay Tools  onchain_profile         $0.10     x1     $0.10    https://<merchant-agent>.wcheckout.app
201         AgentPay Tools  sanctions_screen        $0.30     x1     $0.30    https://<merchant-agent>.wcheckout.app
202         AgentPay Tools  address_risk_profile    $0.60     x1     $0.60    https://<merchant-agent>.wcheckout.app
```

If empty / errors → see [TROUBLESHOOTING.md](TROUBLESHOOTING.md#discover).

## Step 5 — Buy + invoke

Pick a `product_id` from the table and use the **`agent_url` from that same row** (this is the merchant's own URL — different per merchant, and different from `CONNECTOR_URL`):

```bash
python3 scripts/buy.py \
  --agent-url "<paste agent_url from discover.py output>" \
  --product-id 201 \
  --quantity 1 \
  --token ETH_USDC \
  --call-body '{"address":"0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045"}'
```

On success you'll see something like:

```
✅ Order ORD_xxx PAID  ($0.30 ETH_USDC)
✅ Tool call → sanctions_screen
{
  "address": "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045",
  "ofac_match": false,
  ...
}
```

Total run time ≈ 30 sec end-to-end (most of it waiting for Sepolia block confirmation).

## Step 6 — Wire into your agent harness

The skill follows the Anthropic skill format — `SKILL.md` at the top, scripts under `scripts/`. Any harness that supports Anthropic-style skills can load it. Three concrete examples below.

> **The pattern is the same everywhere**: drop the skill directory into the harness's skills folder (symlink or copy), the harness picks it up on next start, then the agent invokes it by name (`wagent`).

### Claude Code (Anthropic CLI)

```bash
# User-level (available in every Claude Code session)
ln -s "$(pwd)" ~/.claude/skills/wagent

# OR project-level (only in projects with .claude/skills/)
mkdir -p .claude/skills && ln -s "$(pwd)" .claude/skills/wagent
```

In a Claude Code session, just describe the intent and Claude finds the skill:

> "use wagent to screen 0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045 for sanctions"

Claude reads `SKILL.md`, runs `scripts/discover.py` and `scripts/buy.py`, surfaces a spend confirmation before any on-chain transaction, and reports the result.

### OpenClaw

OpenClaw uses a per-workspace skills directory:

```bash
ln -s "$(pwd)" ~/.openclaw/workspace/skills/wagent
```

Restart OpenClaw (or `:reload skills` in-session if your build supports it). The skill is then invocable the same way as in Claude Code.

### Hermes

Hermes loads skills from a configurable path. Check yours with:

```bash
hermes config get skills.path     # or: hermes config --show | grep skills
```

Then link the skill there:

```bash
ln -s "$(pwd)" "$(hermes config get skills.path)/wagent"
hermes reload     # or restart Hermes
```

If your Hermes build doesn't have a `config get skills.path` command, consult its docs — common defaults are `~/.hermes/skills/` and `~/.config/hermes/skills/`.

### Generic Anthropic-style harness (any other)

If your harness loads skills from a directory (e.g., `~/.agents/skills/`, `./skills/`, etc.), symlink the wagent repo there with the name `wagent`. The harness will read `SKILL.md` on next start.

```bash
ln -s "$(pwd)" <your-harness-skills-dir>/wagent
```

The agent invokes the skill by saying something like "buy tool X via wagent" or "use wagent to discover MCP merchants" — the same description-matching pattern Anthropic skills are designed for.

### Environment variables — set once, every harness sees them

All harnesses inherit your shell environment. Put your `PAYMENT_BACKEND`, `AGENT_WALLET_PRIVATE_KEY`, `SEPOLIA_RPC_URL` (etc.) in `~/.zshrc` / `~/.bashrc` / `~/.config/fish/config.fish`, and every harness picks them up automatically. Don't put secrets in the skill repo itself — `.gitignore` already excludes `.env*` for safety, but the most secure path is the shell rc file or a secret manager.

## What's next

- Read [WALLET-BACKENDS.md](WALLET-BACKENDS.md) before switching to mainnet / OKX.
- Read [TROUBLESHOOTING.md](TROUBLESHOOTING.md) when something fails.
- Build your own merchant + tool on the seller side → [W Connector integration guide](https://github.com/<owner>/wconnector-integration).
