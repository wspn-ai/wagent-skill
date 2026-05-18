# Troubleshooting

> **中文 → [TROUBLESHOOTING.zh-CN.md](TROUBLESHOOTING.zh-CN.md)**

Common failures and exact fixes. Every error message produced by `buy.py`
includes a `Recovery:` hint — read it first.

## `discover.py` empty / errors

| Symptom | Cause | Fix |
|---|---|---|
| `ERROR: connector request failed (after retries): ...` | Network egress blocked or DNS issue | Verify you can reach `https://connector.wcheckout.app` — `curl -I https://connector.wcheckout.app/merchants/search`. |
| Empty table, exit 0 | No matching merchants for your query | Try `--query ''` (empty) to list everything. |
| `[WARN] <merchant>: ...` lines | One merchant down, others fine | Non-fatal — other rows still load. |

## `buy.py` — payment errors

### `ERROR: --call-body is not valid JSON`

Shell-quoting pitfall (most common on Windows / fish). Use a heredoc:

```bash
buy.py ... --call-body "$(cat <<'EOF'
{"address":"0xd8d..."}
EOF
)"
```

### `Schema validation failed — no payment was made`

`--call-body` doesn't match the product's `input_schema`. The skill **refuses
to pay** if input is invalid — this saves you from paying for a failed call.

Look at the error — it names the bad field:

```
missing required field 'address'
   No payment was made. Fix --call-body and retry.
```

Inspect the tool's input schema:

```bash
curl -s https://<agent_url>/.well-known/agent.json \
  | jq '.skills[] | select(.metadata.tool_name=="<tool>") | .metadata.input_schema'
```

### `Tool call failed AFTER successful payment`

The on-chain payment went through but the tool returned 4xx/5xx. The shop
only decrements the call_token on a successful invocation, so **your token
is still valid**. Do NOT re-run `buy.py` (that pays again).

Fix `--call-body` and retry with the saved token:

```bash
python3 scripts/buy.py --use-token <order_no> --call-body '<corrected JSON>'
```

If you no longer want the token, void it for refund:

```bash
python3 scripts/buy.py --return-token <order_no>
```

### `In-flight purchase detected — refusing to create a duplicate`

Anti-duplicate guard. The skill saw a near-identical purchase started within
the last 30 minutes. If you're sure the previous run never broadcast a tx
(e.g. crashed before payment):

```bash
buy.py ... --force-new
```

If a tx WAS sent, wait for settlement — do not pass `--force-new`.

### `Wrong --agent-url`

You passed the connector URL where a merchant URL was expected. The two are
different:

- **Connector** (`connector.wcheckout.app`) — the registry / order machine.
- **Merchant agent** — every merchant has its own URL, shown in the last
  column of `discover.py` output.

Always copy the `agent_url` from your own `discover.py` output.

### `settlement not confirmed within 180s`

The on-chain transfer succeeded but the shop never observed it. The error
prints three likely shop misconfigs — verify the order's connector view:

```bash
curl https://connector.wcheckout.app/orders/<order_no>
```

If the connector shows `status: PAID`, the shop is lagging — wait and retry
the merchant's side. If the connector shows `status: PAYING`, Stablelink
or the shop webhook isn't relaying.

## OKX backend errors

### `onchainos` command not found

Install per <https://web3.okx.com/onchainos/dev-docs/home/install-your-agentic-wallet>, then `onchainos wallet login <email>`.

### `Policy rejected — exceeds daily limit`

You hit your OKX Policy cap. Either:

- Wait until daily reset (UTC midnight)
- Bump the limit at <https://web3.okx.com/portfolio/agentic-wallet-policy>

### `OTP expired`

```bash
onchainos wallet login your-email@example.com
```

### `insufficient balance` / `wallet underfunded`

The skill tries the next token in your `--token` priority list automatically.
If all tokens are underfunded, top up the OKX wallet (USDC / USDT / WUSD)
and a small ETH buffer for gas.

## Token / state issues

### `--use-token` says "no saved token"

Tokens are persisted at `~/.wagent/tokens.json`. If you cleared the file or
your `HOME` changed (Docker / CI), saved tokens are gone. Run a fresh `buy.py`.

### `--return-token` rejected

Already returned, expired, or partially used. Inspect:

```bash
cat ~/.wagent/tokens.json | jq '.["<order_no>"]'
```

## Getting help

If none of the above match, file a GitHub issue with:

- The exact command (no secrets)
- Full stderr
- `python3 --version` and `onchainos --version`
