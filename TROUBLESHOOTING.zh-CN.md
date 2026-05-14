# 故障排查

> **English → [TROUBLESHOOTING.md](TROUBLESHOOTING.md)**

常见错误 + 精确解决方法。`buy.py` 每一条错误信息都会带 `Recovery:` 提示,先读它。

## discover 报错

| 现象 | 原因 | 解决 |
|---|---|---|
| `ERROR: gateway request failed (after retries): ...` | `CONNECTOR_URL` 写错或不通 | 检查 env,或显式传 `--connector-url https://connector-dev.wcheckout.app`(dev) / `https://connector.wcheckout.app`(prod)。skill 默认指向 dev gateway。 |
| 空表,exit 0 | 平台上没有活跃商户 | 试 `--query ''` 列全部。还空就是 demo gateway 上没商户在线了,联系我们。 |
| 部分商户 `404` | 某个商户挂了,其他正常 | 会打 `[WARN]`。不致命 —— 其他行照样加载。 |

## buy 支付错误

### `ERROR: --call-body is not valid JSON`

Shell 引号坑(Windows / fish 常见)。用 here-doc:

```bash
buy.py ... --call-body "$(cat <<'EOF'
{"address":"0xd8d..."}
EOF
)"
```

### `Schema validation failed (no payment was made)`

`--call-body` 不符合 product 的 `input_schema`。skill **拒绝付款**(免得付了钱跑不了)。

错误里会精确告诉你哪个字段错:

```
'address' is required
✗ no payment was made. Fix --call-body and retry.
```

查 input schema:

```bash
curl -s https://<agent_url>/.well-known/agent.json | jq '.skills[] | select(.metadata.tool_name=="<tool>") | .metadata.input_schema'
```

### 付完款 `Order ORD_xxx FAILED`

链上转账走了但商户把订单标 failed。最常见的原因(看 recovery 提示):

1. **Stablelink 没看到付款** → 等 60s 再去 `${CONNECTOR_URL}/orders/<order_no>` 看。区块重组有时会延迟。
2. **token / 金额错** → 你付了 USDT 但订单要 USDC,或在 Sepolia 上付了主网才有的商品。skill 本该在付款前拦下,看到这个错请提 issue。
3. **FAILED 订单是终态**,**不能重试**。重新跑 `buy.py` 会自动开新单。

### `insufficient funds for gas`

钱包里 ETH(或原生币)不够付 gas。充钱。Sepolia faucet 每天 0.5 ETH;主网留 $5 ETH 缓冲。

### `recent matching purchase detected (5-min window)`

防重复购买保护。5 分钟内检测到几乎一样的 `buy.py`,要你确认。强制下单:

```bash
buy.py ... --force-new
```

### `Wrong --agent-url`

你把 gateway URL 当 merchant URL 传了。两者不是一回事:

- **Gateway**(W Connector):`connector-dev.wcheckout.app`(dev)或 `connector.wcheckout.app`(prod) —— 商户注册表 / 订单状态机的位置。
- **Merchant agent**:每个商户有自己的 URL —— `discover.py` 输出表里最后一列。**不是固定的几个**,商户来来去去。

永远从你自己跑 `discover.py` 的输出里拷 `agent_url`,**不要猜**。

## OKX 后端报错

### `onchainos wallet status` 失败 / 没装

得先装 `okx-agentic-wallet` skill。没它 OKX 后端就签不了名。临时方案:切回 `PAYMENT_BACKEND=local`。

### `Policy rejected — exceeds daily limit`

撞到 OKX Policy 上限了。要么:

- 等 UTC 0 点重置
- 去 <https://web3.okx.com/portfolio/agentic-wallet-policy> 调高

### `OTP expired`

重新登录:

```bash
onchainos wallet login your-email@example.com
onchainos wallet verify <新-otp>
```

## Local 后端报错

### `private key has wrong length`

忘了加 `0x` 前缀?应该是 `0x` + 64 个十六进制字符。

### `Transaction underpriced`

提高 priority fee。skill 给的默认值在主网拥堵时不够。设:

```bash
export WAGENT_PRIORITY_FEE_GWEI=3
```

(只在主网生效,Sepolia 默认就够。)

## Token / 状态问题

### `--use-token` 报 "token not found"

Token 持久化在 `~/.wagent/tokens.json`。你清空了文件、或者 `HOME` 变了(Docker / CI 里很常见),保存的 token 就丢了。重新跑 `buy.py`。

### `--return-token` 被拒绝

已经 return 过了或过期了。检查 `cat ~/.wagent/tokens.json | jq '.[<order_no>]'`。

## 还是不行?

如果以上都对不上:

1. 加 `--verbose` 重跑(版本支持的话)
2. 完整输出 + `Recovery:` 提示
3. 提 GitHub issue,带:
   - 完整命令(私钥脱敏)
   - 完整 stderr
   - `python3 --version`、`pip show web3` 版本
   - 你打的 Gateway URL
