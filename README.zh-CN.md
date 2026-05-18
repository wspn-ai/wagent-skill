# wagent —— 按次付费的 AI agent skill

> **English → [README.md](README.md)**

`wagent` 是给 Claude / agent 用的 skill，让你的 AI agent 自动**发现**、**付费调用**互联网上的付费 API 工具 —— 在以太坊主网用稳定币付款 —— 你不用插手。

agent 思考 → 找到能解决用户任务的工具 → 付 USDC/USDT/WUSD → 拿到一次性 bearer token → 调工具 → 把结果返回给用户。

```
"查 0x...abcd 是不是被制裁地址" ─┐
                                ▼
                      agent (Claude + wagent skill)
                                │
                       1. 在 marketplace 里找
                       2. 选一个 sanctions 工具
                       3. 付 $0.30 USDC
                       4. 调工具
                       5. 返回:"无 OFAC 命中"
```

> **生产环境专用。** 本 skill 在以太坊主网上花真钱。Connector 地址（`connector.wcheckout.app`）已硬编码，没有测试网。

## 你拿到的是什么

- 一个 [Anthropic 风格的 skill](https://docs.claude.com/en/docs/agents-and-tools/agent-skills/overview)（`SKILL.md` + `scripts/`）
- 两个 CLI 脚本：
  - `scripts/discover.py` —— 列出 W Connector 上所有可买的付费工具
  - `scripts/buy.py` —— 端到端走一遍购买 + 调用
- 两套钱包后端，用 `PAYMENT_BACKEND` 切换：
  - **`okx`**（默认，生产推荐）—— OKX Agentic Wallet：MPC 托管 + TEE 签名 + Policy 策略控制。[配置文档](https://web3.okx.com/zh-hans/onchainos/dev-docs/home/install-your-agentic-wallet)
  - **`local`** —— 自管理私钥（web3.py）。建议用一次性钱包 + $1–$5 余额，对接真实 connector 和真实商户做小额测试

## 快速开始

```bash
# 1. 克隆
git clone https://github.com/wspn-ai/wagent-skill.git
cd wagent-skill

# 2a. 默认 —— OKX Agentic Wallet(生产)
onchainos wallet login <你的邮箱>
# 配置每笔 / 每日上限:
#   https://web3.okx.com/portfolio/agentic-wallet-policy

# 2b. 或者 —— 本地测试钱包(小额体验)
#     pip install web3
#     export PAYMENT_BACKEND=local
#     export AGENT_WALLET_PRIVATE_KEY=0x<一次性 64-hex 私钥>
#     export MAINNET_RPC_URL=https://eth-mainnet.g.alchemy.com/v2/<KEY>

# 3. 看看在卖什么
python3 scripts/discover.py

# 4. 买一次 + 跑一次工具(--agent-url 用 discover.py 输出里任一行的 agent_url)
python3 scripts/buy.py \
  --agent-url "<discover.py 输出里的 merchant agent_url>" \
  --product-id 101 \
  --quantity 1 \
  --call-body '{"address":"0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045"}'
```

或者把它装进 Claude Code 当 skill 用，让 agent 自己开车 —— 完整步骤见 **[SETUP.zh-CN.md](SETUP.zh-CN.md)**。

## 文档目录

| 文档 | 内容 |
|---|---|
| **[SETUP.zh-CN.md](SETUP.zh-CN.md)** / [EN](SETUP.md) | 安装 + 第一次跑通 + 接 Claude Code |
| **[TROUBLESHOOTING.zh-CN.md](TROUBLESHOOTING.zh-CN.md)** / [EN](TROUBLESHOOTING.md) | 常见错误 + 解决方法 |
| **[SKILL.md](SKILL.md)** | skill 自身的 manifest（给 agent harness 读） |

## 成本预期

| 后端 | 链 | 费用 | 适用场景 |
|---|---|---|---|
| `okx`（默认） | 以太坊主网 | 真金白银 —— gas ~$0.50 + 工具 $0.10–$0.60 每次 | 生产 |
| `local` | 以太坊主网 | 同上，但私钥自管 | 小额测试钱包 |

## 它是怎么工作的（60 秒版本）

1. **Discover** —— `discover.py` 调 `connector.wcheckout.app/merchants/search`，再去每个商户的 `/.well-known/agent.json` 列出所有 SKU。
2. **下单意图** —— `buy.py` 给商户发 A2A `tasks/send`。Shop 在 W Connector 上创建订单，返回付款地址。
3. **支付** —— wagent 通过 OKX 签一笔精确金额的 EVM 转账，链上 hash 立即返回。
4. **结算** —— Stablelink（W Connector 用的支付通道）监听链上，触发 shop 的 webhook，shop 把 "PAID" 中继给 W Connector。
5. **发 token** —— wagent 轮询 `delivery` artifact，拿到一个 24 小时内有效的 bearer token。
6. **调用** —— wagent 把用户输入 POST 到 `/v1/tools/<tool_name>`，带 bearer。结果返回，token 销毁。

完整的 A2A + 支付协议在 W Connector 内部跑，**你不用学** —— 跑两个脚本就行。

## 安全注意

- 每次 `buy.py` 都会在链上花真钱。Skill 会用每个 product 的 `input_schema` 校验 `--call-body`，不匹配直接拒付。
- 一次 `buy.py` 调用 = 一笔订单。同一条命令重跑就是再付一次。Skill 检测到 30 分钟内有相似购买会拒绝下单 —— 加 `--force-new` 强制下单。
- Token 持久化在 `~/.wagent/tokens.json`。工具调用失败时用 `--use-token <order_no>` 重试（不再付钱），或 `--return-token <order_no>` 销毁退款。

## 协议

MIT —— 详见 [LICENSE](LICENSE)。

## Issues / PR

欢迎提 Issues 和 PR。如果问的是**协议层面**的问题（W Connector 网络本身、商户接入、结算、gateway 行为之类），去 **[W Connector 接入仓库](https://github.com/wspn-ai/wconnector-integration/issues)** 提 issue。
