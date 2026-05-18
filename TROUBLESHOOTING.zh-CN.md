# 故障排查

> **English → [TROUBLESHOOTING.md](TROUBLESHOOTING.md)**

常见错误和具体解决方法。`buy.py` 每个错误都会带一行 `Recovery:` 提示 —— 先读这个。

## `discover.py` 空表 / 报错

| 现象 | 原因 | 解决 |
|---|---|---|
| `ERROR: connector request failed (after retries): ...` | 网络出不去或 DNS 问题 | 确认能访问到 `https://connector.wcheckout.app` —— `curl -I https://connector.wcheckout.app/merchants/search`。 |
| 空表 + exit 0 | 没匹配上的商户 | 试 `--query ''`（空字符串）列出所有。 |
| `[WARN] <商户>: ...` 行 | 某个商户挂了，其他正常 | 非致命 —— 其他行照样列。 |

## `buy.py` —— 付款错误

### `ERROR: --call-body is not valid JSON`

shell 引号坑（Windows / fish 常见）。用 heredoc：

```bash
buy.py ... --call-body "$(cat <<'EOF'
{"address":"0xd8d..."}
EOF
)"
```

### `Schema validation failed — no payment was made`

`--call-body` 不符合 product 的 `input_schema`。Skill **拒绝付款** —— 这是省下你的钱。

报错信息会指出哪个字段错了：

```
missing required field 'address'
   No payment was made. Fix --call-body and retry.
```

查工具的 input schema：

```bash
curl -s https://<agent_url>/.well-known/agent.json \
  | jq '.skills[] | select(.metadata.tool_name=="<tool>") | .metadata.input_schema'
```

### `Tool call failed AFTER successful payment`

链上付款成功了但工具返回 4xx / 5xx。Shop 只在调用成功时才扣 token，**所以你的 token 还有效**。**不要**重跑 `buy.py`（那是再付一次）。

修好 `--call-body`，用保存的 token 重试：

```bash
python3 scripts/buy.py --use-token <order_no> --call-body '<改好的 JSON>'
```

如果不想要这个 token 了，销毁退款：

```bash
python3 scripts/buy.py --return-token <order_no>
```

### `In-flight purchase detected — refusing to create a duplicate`

防重复下单守卫。Skill 检测到 30 分钟内有几乎一样的购买。如果你确定上次根本没付出去（比如付款前就崩了）：

```bash
buy.py ... --force-new
```

如果上次已经发了 tx，等结算就行 —— 不要加 `--force-new`。

### `Wrong --agent-url`

你把 connector 的 URL 当 merchant URL 传了。两者不一样：

- **Connector**（`connector.wcheckout.app`）—— 注册中心 / 订单机器。
- **Merchant agent** —— 每个商户自己的 URL，在 `discover.py` 输出最后一列。

务必从你自己的 `discover.py` 输出里复制 `agent_url`。

### `settlement not confirmed within 180s`

链上付款成功了但 shop 没观察到。报错会列出三种常见 shop 端配置问题。先查 connector 视角：

```bash
curl https://connector.wcheckout.app/orders/<order_no>
```

如果 connector 显示 `status: PAID`，说明 shop 在掉队 —— 等等或联系商户运维。如果显示 `status: PAYING`，说明 Stablelink 或 shop webhook 链路没通。

## OKX 后端错误

### `onchainos` 命令不存在

按 <https://web3.okx.com/zh-hans/onchainos/dev-docs/home/install-your-agentic-wallet> 装好，然后 `onchainos wallet login <邮箱>`。

### `Policy rejected — exceeds daily limit`

撞到 OKX Policy 上限了。要么：

- 等到当日重置（UTC 0 点）
- 去 <https://web3.okx.com/portfolio/agentic-wallet-policy> 调高

### `OTP expired`

```bash
onchainos wallet login your-email@example.com
```

### `insufficient balance` / `wallet underfunded`

Skill 自动尝试 `--token` 优先级列表里的下一个。所有 token 都缺钱时，往 OKX 钱包充值（USDC / USDT / WUSD），别忘了留一点 ETH 当 gas。

## Token / 状态问题

### `--use-token` 提示 "no saved token"

Token 持久化在 `~/.wagent/tokens.json`。如果清过这个文件，或者 `HOME` 变了（Docker / CI），保存的 token 就没了。重新跑 `buy.py`。

### `--return-token` 被拒

已经销毁、过期、或部分用过。看一下：

```bash
cat ~/.wagent/tokens.json | jq '.["<order_no>"]'
```

## 找帮助

上面都不对，提 GitHub issue，附：

- 跑的命令（去掉密钥）
- 完整 stderr
- `python3 --version` 和 `onchainos --version`
