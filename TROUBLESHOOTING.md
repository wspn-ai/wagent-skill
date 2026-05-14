# Troubleshooting

> **中文 → [TROUBLESHOOTING.zh-CN.md](TROUBLESHOOTING.zh-CN.md)**

Common failures and exact fixes. Every error message produced by `buy.py` includes a `Recovery:` hint — read it first.

## `discover.py` empty / errors

| Symptom | Cause | Fix |
|---|---|---|
| `ERROR: gateway request failed (after retries): ...` | `CONNECTOR_URL` wrong or unreachable | Check the URL in env or pass `--connector-url https://connector-dev.wcheckout.app` explicitly (dev) or `https://connector.wcheckout.app` (prod). The default in the skill points at our dev gateway. |
| Empty table, exit 0 | No active merchants on the gateway | Try `--query ''` (empty) to list all. If still empty, the demo gateway has no live merchants — contact us. |
| `404` from a merchant during fanout | One merchant down, others fine | Logged as `[WARN]`. Non-fatal — other rows still load. |

## `buy.py` — payment errors

### `ERROR: --call-body is not valid JSON`

Single-quote shell pitfall on Windows / fish shell. Use:

```bash
buy.py ... --call-body "$(cat <<'EOF'
{"address":"0xd8d..."}
EOF
)"
```

### `Schema validation failed (no payment was made)`

Your `--call-body` doesn't match the product's `input_schema`. The skill **refuses to pay** if input is invalid (saving you from paying for a failed call).

Look at the error — it tells you exactly which field is wrong:

```
'address' is required
✗ no payment was made. Fix --call-body and retry.
```

Inspect the tool's input schema:

```bash
curl -s https://<agent_url>/.well-known/agent.json | jq '.skills[] | select(.metadata.tool_name=="<tool>") | .metadata.input_schema'
```

### `Order ORD_xxx FAILED` after payment was sent

The on-chain tx went through but the merchant marked the order failed. Most likely causes (from the recovery hint):

1. **Stablelink didn't see the payment** → wait 60s and re-check `${CONNECTOR_URL}/orders/<order_no>`. Sometimes block reorgs delay observation.
2. **Wrong token / amount** → you sent USDT but the order expected USDC, or sent on Sepolia when product is mainnet-only. The skill is supposed to catch this before paying — if you see this, file an issue.
3. **Failed orders are terminal.** You cannot retry. The skill auto-creates a new order if you re-run `buy.py`.

### `insufficient funds for gas`

Your wallet doesn't have enough ETH (or chain-native token) to pay gas. Top up. On Sepolia, faucets give you 0.5 ETH/day. On mainnet, keep a $5 ETH buffer.

### `recent matching purchase detected (5-min window)`

Anti-duplicate guard. The skill saw you ran an almost-identical `buy.py` within 5 minutes and is asking you to confirm. To override:

```bash
buy.py ... --force-new
```

### `Wrong --agent-url`

You passed a gateway URL where a merchant URL was expected. The two are different:

- **Gateway** (the W Connector): `connector-dev.wcheckout.app` (dev) or `connector.wcheckout.app` (prod) — this is where the registry / order machine lives.
- **Merchant agent**: every merchant has their own URL — these are the rows in the last column of `discover.py` output. **They are NOT a fixed list** — merchants come and go.

Always copy the `agent_url` from your own `discover.py` output instead of guessing.

## OKX backend errors

### `onchainos wallet status` fails / not installed

The `okx-agentic-wallet` skill must be installed first. Without it, the OKX backend can't sign. Falls back: switch to `PAYMENT_BACKEND=local` for now.

### `Policy rejected — exceeds daily limit`

You hit your OKX Policy cap. Either:

- Wait until daily reset (UTC midnight)
- Bump the limit at <https://web3.okx.com/portfolio/agentic-wallet-policy>

### `OTP expired`

Re-login:

```bash
onchainos wallet login your-email@example.com
onchainos wallet verify <new-otp>
```

## Local backend errors

### `private key has wrong length`

Did you forget the `0x` prefix? Should be `0x` + 64 hex chars.

### `Transaction underpriced`

Bump the priority fee. The skill picks a sensible default but congested chains need more. Set:

```bash
export WAGENT_PRIORITY_FEE_GWEI=3
```

(Only respected on mainnet — Sepolia is fine with defaults.)

## Token / state issues

### `--use-token` says "token not found"

Tokens are persisted at `~/.wagent/tokens.json`. If you cleared the file or your `HOME` changed (Docker / CI), the saved tokens are gone. Run a fresh `buy.py`.

### `--return-token` rejected

Already returned or expired. Check `cat ~/.wagent/tokens.json | jq '.[<order_no>]'`.

## Getting help

If none of the above match:

1. Re-run with `--verbose` (if your version supports it)
2. Capture the full output including the `Recovery:` hint
3. File a GitHub issue with:
   - The exact command (redact private key)
   - Full stderr
   - `python3 --version`, `pip show web3` versions
   - Gateway URL you're hitting
