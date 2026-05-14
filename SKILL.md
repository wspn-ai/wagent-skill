---
name: wagent
version: 3.0.0
description: 'Discover MCP-tool merchants, buy a tool pack on Ethereum (USDC/USDT/WUSD), and invoke one tool call. Backends — local web3.py (Sepolia or mainnet) or OKX Agentic Wallet (mainnet only).'
---

# wagent (A2A)

Discover MCP-tool merchants and buy a single tool call. Two CLI scripts:

- `scripts/discover.py` — list buyable SKUs
- `scripts/buy.py` — purchase + invoke one tool call end-to-end

> **Path note:** all `scripts/...` paths in this file are relative to `SKILL.md`
> itself. Run them from the directory containing `SKILL.md` (most agent
> harnesses CD there automatically). If your harness invokes scripts from a
> different CWD, prefix the path accordingly.

## Trigger

Invoke when the user says: "list MCP tools", "find a tool to do X",
"buy and run tool Y", "screen this address", "profile this wallet",
"pay with USDC/USDT/WUSD".

## Hard rules (read first)

1. **One `buy.py` invocation = one order.** Failed runs create a brand-new
   order on retry — that's intentional. **Never** call `buy.py` twice for
   the same product to "refresh state": that's two charges.
2. **Approve URL is plain text passthrough.** When `buy.py` prints
   `Approve: https://...&ts=X&sig=Y`, surface it verbatim — no markdown
   link, no angle brackets, no shortening. Lark/Slack/Telegram truncate at
   `&sig=` if wrapped → 403/404.
3. **Tx hash never gets fabricated.** "Payment successful" is only true
   when `buy.py` exits 0 and stdout contains a line matching
   `^0x[0-9a-f]{64}$`. `payment_url` / `order_no` / signature blobs are
   not tx hashes.
4. **Confirm spend with the user before running `buy.py`.** Show
   merchant + amount + token in chat. **The default backend is OKX on
   Ethereum mainnet — REAL MONEY.** There is no `MAX_SPEND_USD` guard;
   per-tx caps live in OKX Policy Settings (configure once at
   <https://web3.okx.com/portfolio/agentic-wallet-policy>). Set
   `PAYMENT_BACKEND=local LOCAL_NETWORK=sepolia` for free testnet runs.

## Quick start

`CONNECTOR_URL` defaults to `https://connector-dev.wcheckout.app`,
so `discover.py` works zero-config:

```bash
python3 scripts/discover.py
```

`buy.py` defaults to **OKX Agentic Wallet on Ethereum mainnet** —
🚨 **REAL MONEY**. Only setup needed (one-time, then never again):

```bash
onchainos wallet login <your-email>     # one-time login
# Configure per-tx + daily USD caps at:
# https://web3.okx.com/portfolio/agentic-wallet-policy
```

Then run zero-config:
```bash
python3 scripts/buy.py \
  --agent-url <merchant-url> --product-id <id> --quantity 1 \
  --call-body '{"address": "0xabc..."}'
```

`--token` defaults to the priority list `ETH_WUSD,ETH_USDT,ETH_USDC` —
on wallet underfunded for one token, falls back to the next without
re-charging.

**Alternative: Sepolia testnet (free, safe to play with) — `local` backend:**
```bash
export PAYMENT_BACKEND=local
export LOCAL_NETWORK=sepolia
# Generate a throwaway key — NEVER reuse a key holding real funds.
# Quick generator: `python3 -c "import secrets; print('0x' + secrets.token_hex(32))"`
export AGENT_WALLET_PRIVATE_KEY=0x<your_64_hex_chars_here>
# Sign up at https://www.alchemy.com (free tier is enough for Sepolia).
export SEPOLIA_RPC_URL=https://eth-sepolia.g.alchemy.com/v2/<YOUR_ALCHEMY_KEY>
```

Override `CONNECTOR_URL` only if you're not running against the demo gateway.

## Configuration (full reference)

```bash
# All optional — sensible defaults exist
CONNECTOR_URL=https://connector-dev.wcheckout.app   # default
PAYMENT_BACKEND=okx          # default. Set 'local' for testnet (see below)

# OKX backend (the default — Ethereum mainnet, REAL MONEY)
# One-time setup: `onchainos wallet login <email>` (or set OKX_API_KEY for silent mode).
# Configure caps server-side at https://web3.okx.com/portfolio/agentic-wallet-policy
# Optional: override the ERC-20 contract address (only useful for non-canonical tokens)
OKX_TOKEN_CONTRACT=

# Local backend (only when PAYMENT_BACKEND=local)
LOCAL_NETWORK=sepolia        # sepolia | mainnet
SEPOLIA_RPC_URL=https://eth-sepolia.g.alchemy.com/v2/...   # required if LOCAL_NETWORK=sepolia
MAINNET_RPC_URL=https://eth-mainnet.g.alchemy.com/v2/...   # required if LOCAL_NETWORK=mainnet
AGENT_WALLET_PRIVATE_KEY=0x...

# Sepolia-only escape hatch (sepolia has no canonical USDT)
ETH_USDT_CONTRACT_SEPOLIA=
```

## Step 1 — Discover

```bash
python3 scripts/discover.py --query "screen address"
```

Output (one row per buyable MCP-tool SKU — exact merchants, URLs, and prices vary
with who's live on the network at the moment; **do not memorize or hardcode them**):

```
product_id  merchant        tool_name              $/pack   pack    $/call      agent_url
101         AgentPay Tools  onchain_profile        $0.10    x1      $0.10       https://<merchant-agent>.wcheckout.app
201         AgentPay Tools  sanctions_screen       $0.30    x1      $0.30       https://<merchant-agent>.wcheckout.app
202         AgentPay Tools  address_risk_profile   $0.60    x1      $0.60       https://<merchant-agent>.wcheckout.app
```

Empty `--query` lists all merchants. Failures on individual merchants
print `[WARN] <name>: <err>` to stderr and the run continues.

## Step 2 — Buy + run one tool call

Pick a row from `discover` output. Confirm spend with the user. Then use the
**`agent_url` from that row** (NOT `CONNECTOR_URL` — each merchant has its own URL):

```bash
python3 scripts/buy.py \
  --agent-url <paste agent_url from the row you picked> \
  --product-id 201 \
  --quantity 1 \
  --token ETH_WUSD,ETH_USDT,ETH_USDC \
  --call-body '{"address": "0xabc1234567890abcdef1234567890abcdef12ab"}'
```

`--token` accepts a single token (e.g. `ETH_USDC`) OR a **comma-separated
priority list**. The skill tries each in order and falls back to the next on
*pre-payment* errors only — wallet underfunded for that token, OKX
high-risk policy block, or merchant rejecting that token. Once a tx is
broadcast on-chain, no fallback (would double-charge). Default if `--token`
is omitted: `ETH_WUSD,ETH_USDT,ETH_USDC`.

Behavior:

1. Sends a `purchase` task to the merchant for the first token.
2. If `state=input-required` → prints `Approve: <url>` + `Reject: <url>`
   to stdout (verbatim per Hard rule 2), then polls every 5s up to 10
   minutes for the user's click.
3. On approval → signs the ERC-20 transfer via `PAYMENT_BACKEND` and
   prints `Tx: 0x<64 hex>`.
4. Polls every 5s up to 3 minutes for settlement (`check_payment` intent).
   If settlement doesn't land within 3 min, the timeout message lists the
   3 common backend misconfigs that cause permanent stuck-in-PAYING. Bump
   via `--settle-timeout-sec` if you genuinely want to wait longer.
5. Calls `usage_endpoint` once with the `--call-body` JSON and the
   bearer token from delivery; prints the tool's JSON response.

Exit codes:

- `0` → tool result printed; payment confirmed.
- `0` → user clicked Reject (`Order cancelled`).
- `1` → any failure (purchase failed, approval timeout, payment not
  confirmed, settlement timeout, tool 4xx/5xx). stderr has the reason.

## Token routing

| `--token`  | local backend (sepolia) | local backend (mainnet) | okx backend (mainnet) |
|---|---|---|---|
| `ETH_USDC` | 0x1c7D…7238 | 0xA0b8…eB48 | 0xA0b8…eB48 (or `OKX_TOKEN_CONTRACT`) |
| `ETH_WUSD` | 0x3716…0065 | 0x7Cd0…8c41 | 0x7Cd0…8c41 (or `OKX_TOKEN_CONTRACT`) |
| `ETH_USDT` | env `ETH_USDT_CONTRACT_SEPOLIA` (no canonical) | 0xdAC1…1ec7 | 0xdAC1…1ec7 (or `OKX_TOKEN_CONTRACT`) |

Solana / TRON tokens (`SOL_*`, `TRX_*`) are not supported by this skill —
even though W Checkout (the merchant side) accepts them.

## Response templates

**Pre-purchase confirmation (per Hard rule 4):**
```
About to buy: <tool_name> @ <merchant>
  Amount: $<paying_amount> <token>
  Quantity: <n> packs (<calls_per_unit>×n calls, TTL <ttl_hours>h)
  Backend: <PAYMENT_BACKEND> (<chain>)
Confirm? [y/N]
```

**Approval needed:**
```
Order requires your authorization:
  Approve: <approve_url verbatim>
  Reject:  <reject_url verbatim>
(Polling every 5s — paying on-chain automatically on your click. No reply needed.)
```

**Success:**
```
Tx: 0x<64 hex>  (Sepolia / Ethereum mainnet — depends on PAYMENT_BACKEND × LOCAL_NETWORK)
Tool result:
{
  ...
}
```

**Cancelled:**
```
Order cancelled (you rejected the payment).
```

## Failure surfacing

`buy.py` prints the failure reason from the merchant artifact, then enriches
it with `${CONNECTOR_URL}/orders/<order_no>` (`error_message` /
`wcheckout_response` / `last_error`). Common shapes:

- "Stablelink vault doesn't have <chain> enabled" → try a different `--token`
- "amount below minimum" → bump `--quantity`
- "approval timeout" → user didn't click within 10 min; rerun with a fresh
  order if you still want to buy

## Files shipped

```
skill/
├── SKILL.md                       this file — agent contract
└── scripts/
    ├── discover.py                CLI: list MCP-tool SKUs (chain-agnostic)
    ├── buy.py                     CLI: orchestrator (purchase → pay → tool call)
    ├── _tokens.py                 token+network → contract address lookup
    ├── okx/                       MAINNET backend (OKX Agentic Wallet)
    │   └── _wallet_okx.py           shells out to onchainos CLI; Ethereum mainnet only
    └── local/                     TESTNET-friendly backend (self-managed wallet)
        └── _wallet_local.py         web3.py signer; defaults Sepolia, can do mainnet via env
```

`okx/` and `local/` are independent — each backend's code is self-contained
and lives under its own subdir. If you ship the skill to a context that
only ever uses one backend, the other subdir is dead weight (delete it
plus the matching import line at the top of `buy.py`).

## Runtime requirements

- Python 3.11+
- `web3` (only when `PAYMENT_BACKEND=local`): `pip install web3`
- `onchainos` CLI (only when `PAYMENT_BACKEND=okx`)

## A2A protocol summary

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/.well-known/agent.json` | GET | Merchant capabilities + catalog |
| `/tasks/send` | POST | Send task (purchase / check_approval / check_payment) |

Task states: `submitted` → `working` → `completed` / `input-required` / `canceled` / `failed`
