# Setup — wagent skill

> **中文 → [SETUP.zh-CN.md](SETUP.zh-CN.md)**

End-to-end first run, from `git clone` to a successful tool call. ~10 minutes.

This skill is production-only — Ethereum mainnet, real money. Two wallet
backends; pick one.

## Prerequisites

- Python 3.10+
- Either:
  - **OKX** (recommended for production): the [`onchainos`](https://web3.okx.com/onchainos/dev-docs/home/install-your-agentic-wallet) CLI installed
  - **Local** (small-balance test wallet): `pip install web3` + a mainnet RPC URL (Alchemy / Infura / QuickNode free tier all work)
- A wallet with:
  - A small ETH balance for gas (≈ $5 is plenty)
  - At least $0.10 of USDC / USDT / WUSD for a test call

## Step 1 — Clone

```bash
git clone https://github.com/wspn-ai/wagent-skill.git
cd wagent-skill
```

## Step 2 — Configure a wallet backend

### Option A — OKX Agentic Wallet (recommended for production)

```bash
onchainos wallet login <your-email>
# follow the OTP prompt
```

Set per-tx and daily USD caps so a buggy agent can't drain your wallet:

> <https://web3.okx.com/portfolio/agentic-wallet-policy>

Recommended starting caps for testing:
- Per-tx: **$5**
- Daily: **$20**

### Option B — local test wallet (small-balance experimentation)

```bash
pip install web3
export PAYMENT_BACKEND=local

# Generate a throwaway key. NEVER reuse a key that holds real funds.
python3 -c "import secrets; print('0x' + secrets.token_hex(32))"
export AGENT_WALLET_PRIVATE_KEY=0x<the_64_hex_above>

# Any Ethereum mainnet RPC works
export MAINNET_RPC_URL=https://eth-mainnet.g.alchemy.com/v2/<YOUR_KEY>
```

Fund the wallet with $1–$5 of stablecoin and a tiny ETH balance for gas.
The connector and merchants are the same as production — this is just a
different signer.

## Step 3 — Discover available tools

```bash
python3 scripts/discover.py
```

Expected output — exact merchants and prices depend on who is live at the
moment; **don't copy these URLs verbatim, use what your own output shows**:

```
product_id  merchant        tool_name               $/pack    pack   $/call   agent_url
─────────────────────────────────────────────────────────────────────────────────────────
101         AgentPay Tools  onchain_profile         $0.10     x1     $0.10    https://<merchant-agent>.wcheckout.app
201         AgentPay Tools  sanctions_screen        $0.30     x1     $0.30    https://<merchant-agent>.wcheckout.app
202         AgentPay Tools  address_risk_profile    $0.60     x1     $0.60    https://<merchant-agent>.wcheckout.app
```

If empty / errors → see [TROUBLESHOOTING.md](TROUBLESHOOTING.md#discoverpy-empty--errors).

## Step 4 — Buy + invoke

Pick a `product_id` from the table and use the **`agent_url` from that same row**
(each merchant has its own URL — `connector.wcheckout.app` is the registry, not
a merchant):

```bash
python3 scripts/buy.py \
  --agent-url "<paste agent_url from discover.py>" \
  --product-id 201 \
  --quantity 1 \
  --call-body '{"address":"0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045"}'
```

On success you'll see:

```
Tx: 0x<64 hex>
{
  "address": "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045",
  "ofac_match": false,
  ...
}
```

Total run time ≈ 30 sec end-to-end.

## Step 5 — Wire into your agent harness

The skill follows the Anthropic skill format — `SKILL.md` at the top, scripts
under `scripts/`. Drop the directory into the harness's skills folder, and
the harness picks it up on next start.

### Claude Code

```bash
# User-level (every Claude Code session)
ln -s "$(pwd)" ~/.claude/skills/wagent

# OR project-level
mkdir -p .claude/skills && ln -s "$(pwd)" .claude/skills/wagent
```

Then in any session: *"use wagent to screen 0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045 for sanctions"*. Claude reads `SKILL.md`, runs the scripts, surfaces a spend confirmation before any on-chain transaction, and reports the result.

### OpenClaw

```bash
ln -s "$(pwd)" ~/.openclaw/workspace/skills/wagent
```

Restart OpenClaw (or `:reload skills`). Invoke the same way as in Claude Code.

### Hermes (and other Anthropic-style harnesses)

```bash
ln -s "$(pwd)" "$(hermes config get skills.path)/wagent"
hermes reload     # or restart Hermes
```

For other harnesses, symlink the repo into your harness's skills directory.
The agent invokes the skill by description — "buy tool X via wagent",
"use wagent to discover MCP merchants", etc.

## What's next

- Read [TROUBLESHOOTING.md](TROUBLESHOOTING.md) when something fails.
- Build your own merchant + tool on the seller side → [W Connector integration guide](https://github.com/wspn-ai/wconnector-integration).
