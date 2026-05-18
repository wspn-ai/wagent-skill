---
name: wagent
version: 4.0.0
description: 'Discover MCP-tool merchants on the W Connector network and buy a single tool call, paid in USDC / USDT / WUSD on Ethereum mainnet. Default backend is OKX Agentic Wallet; a self-managed test wallet (web3.py) is available for small-balance experimentation.'
---

# wagent

Discover MCP-tool merchants on the W Connector network and buy a single tool
call. Two CLI scripts, real money.

- `scripts/discover.py` — list buyable SKUs
- `scripts/buy.py` — purchase + invoke one tool call end-to-end

Connector: `https://connector.wcheckout.app` (production, hardcoded — this is
a production-only skill, no test connector exists).

Backends:

| `PAYMENT_BACKEND` | Wallet | When to use |
|---|---|---|
| `okx` (default) | OKX Agentic Wallet — MPC custody + Policy caps | Production |
| `local` | Self-managed private key (web3.py) | Small-balance test wallet ($1–$5) |

Both backends transact on **Ethereum mainnet** — there is no testnet. The
`local` backend is for users who want to try things end-to-end against the
real connector + real merchants with their own throwaway wallet.

> **Path note:** all `scripts/...` paths in this file are relative to
> `SKILL.md` itself. Run them from the directory containing `SKILL.md`.

## Trigger

Invoke when the user says: "list MCP tools", "find a tool to do X",
"buy and run tool Y", "screen this address", "profile this wallet",
"pay with USDC/USDT/WUSD".

## Hard rules (read first)

1. **One `buy.py` invocation = one order.** Failed runs create a brand-new
   order on retry — that's intentional. **Never** call `buy.py` twice for
   the same product to "refresh state": that's two charges.
2. **Tx hash never gets fabricated.** "Payment successful" is only true
   when `buy.py` exits 0 and stdout contains a line matching
   `^0x[0-9a-f]{64}$`. `order_no` / signature blobs are not tx hashes.
3. **Confirm spend with the user before running `buy.py`.** Show
   merchant + amount + token in chat. **This skill pays real money on
   Ethereum mainnet.** Per-tx and daily caps live in OKX Policy Settings —
   configure once at <https://web3.okx.com/portfolio/agentic-wallet-policy>.

## One-time setup

### Default — OKX Agentic Wallet (recommended for production)

```bash
onchainos wallet login <your-email>
# Configure per-tx + daily caps at:
#   https://web3.okx.com/portfolio/agentic-wallet-policy
```

### Alternative — local test wallet (small-balance experimentation)

```bash
export PAYMENT_BACKEND=local
# Generate a throwaway key (NEVER reuse a key holding real funds):
#   python3 -c "import secrets; print('0x' + secrets.token_hex(32))"
export AGENT_WALLET_PRIVATE_KEY=0x<your_64_hex>
# Any Ethereum mainnet RPC (Alchemy/Infura/QuickNode free tier works):
export MAINNET_RPC_URL=https://eth-mainnet.g.alchemy.com/v2/<YOUR_KEY>
```

Fund the wallet with $1–$5 of USDC/USDT/WUSD and a few cents of ETH for gas.
Same connector, same merchants — just a different signer.

## Step 1 — Discover

```bash
python3 scripts/discover.py                  # list everything
python3 scripts/discover.py --query "screen" # filter by keyword
```

Output — one row per buyable SKU. Exact merchants, URLs, and prices change
with who is live on the network; **do not memorize or hardcode them**:

```
product_id  merchant        tool_name              $/pack   pack    $/call      agent_url
101         AgentPay Tools  onchain_profile        $0.10    x1      $0.10       https://<merchant-agent>.wcheckout.app
201         AgentPay Tools  sanctions_screen       $0.30    x1      $0.30       https://<merchant-agent>.wcheckout.app
202         AgentPay Tools  address_risk_profile   $0.60    x1      $0.60       https://<merchant-agent>.wcheckout.app
```

Failures on individual merchants print `[WARN] <name>: <err>` to stderr;
the rest of the listing continues.

## Step 2 — Buy + invoke

Pick a row from `discover` output. Confirm spend with the user. Use the
**`agent_url` from that row** (each merchant has its own — `connector.wcheckout.app`
is the registry, not a merchant):

```bash
python3 scripts/buy.py \
  --agent-url <agent_url from the row you picked> \
  --product-id 201 \
  --quantity 1 \
  --call-body '{"address": "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045"}'
```

`--token` is optional. Default priority list: `ETH_WUSD,ETH_USDT,ETH_USDC`.
On pre-payment errors (wallet underfunded for that token, OKX policy block,
merchant rejecting that token) the skill falls back to the next token without
double-charging. Once a tx is broadcast, no fallback.

Pipeline:

1. Send `purchase` intent to the merchant for the first token.
2. Extract `deposit_address` + `paying_amount` + `order_no` from the
   merchant's response artifacts.
3. Sign the ERC-20 transfer via OKX; print `Tx: 0x<64 hex>`.
4. Poll every 5s up to 3 min for settlement (`check_payment` intent).
   On timeout, the message lists the three common shop misconfigs that
   cause permanent stuck-in-PAYING. Bump via `--settle-timeout-sec` only
   if you genuinely want to wait longer.
5. Call `usage_endpoint` once with the `--call-body` JSON and the bearer
   token from delivery; print the tool's JSON response.

Exit codes:

- `0` → tool result printed; payment confirmed.
- `1` → any failure (purchase failed, payment not confirmed, settlement
  timeout, tool 4xx/5xx). stderr has the reason.

## Recovery flows

Both flows operate on a previously bought order saved in
`~/.wagent/tokens.json` — no second payment.

**Tool call failed after payment** (bad `--call-body`, transient 5xx, etc.):

```bash
python3 scripts/buy.py --use-token <order_no> --call-body '{...corrected...}'
```

**Want to void an unused token and request refund:**

```bash
python3 scripts/buy.py --return-token <order_no>
```

## Token routing

| `--token`  | ERC-20 contract on Ethereum mainnet |
|---|---|
| `ETH_USDC` | `0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48` |
| `ETH_WUSD` | `0x7Cd017ca5ddb86861FA983a34b5F495C6F898c41` |
| `ETH_USDT` | `0xdAC17F958D2ee523a2206206994597C13D831ec7` |

Override a single contract via `OKX_TOKEN_CONTRACT` env if you really need
to (rare — only for non-canonical deployments).

## Response templates

**Pre-purchase confirmation (per Hard rule 3):**
```
About to buy: <tool_name> @ <merchant>
  Amount:   $<paying_amount> <token>
  Quantity: <n> packs (<calls_per_unit>×n calls, TTL <ttl_hours>h)
  Backend:  <PAYMENT_BACKEND> — Ethereum mainnet (REAL MONEY)
Confirm? [y/N]
```

**Success:**
```
Tx: 0x<64 hex>
Tool result:
{ ... }
```

## Failure surfacing

On failure `buy.py` prints the merchant's failure reason, then enriches it
with `https://connector.wcheckout.app/orders/<order_no>` detail
(`error_message` / `wcheckout_response` / `last_error`). Common shapes:

- *"Stablelink vault doesn't have <chain> enabled"* → try a different `--token`
- *"amount below minimum"* → bump `--quantity`
- *"settlement not confirmed within 180s"* → see the three-cause hint
  the script prints; verify via the order URL above

## Files shipped

```
wagent-skill/
├── SKILL.md            this file — agent contract
└── scripts/
    ├── discover.py     CLI: list MCP-tool SKUs
    ├── buy.py          CLI: purchase → pay → tool call
    ├── _tokens.py      token → mainnet ERC-20 contract lookup
    ├── okx/
    │   └── _wallet_okx.py     default backend — onchainos CLI
    └── local/
        └── _wallet_local.py   alt backend — self-managed web3.py signer
```

## Runtime requirements

- Python 3.10+
- For `okx` backend (default): `onchainos` CLI installed and logged in
  (<https://web3.okx.com/onchainos/dev-docs/home/install-your-agentic-wallet>)
- For `local` backend: `pip install web3`, plus a mainnet RPC URL and a
  throwaway private key

## A2A protocol summary

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/.well-known/agent.json` | GET | Merchant capabilities + catalog |
| `/tasks/send` | POST | Send task (`purchase` / `check_payment`) |
| `/tasks/<id>` | GET | Poll task state |

Task states: `submitted` → `working` → `completed` / `failed`.
