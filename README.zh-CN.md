# wagent —— 按次付费的 AI agent skill

> **English → [README.md](README.md)**

`wagent` 是给 Claude / agent 用的 skill,让你的 AI agent 自动**发现**、**付费调用**互联网上的付费 API 工具 —— 在以太坊主网或 Sepolia 测试网上用稳定币付款 —— 你不用插手。

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

## 你拿到的是什么

- 一个 [Anthropic 风格的 skill](https://docs.claude.com/en/docs/agents-and-tools/agent-skills/overview)(`SKILL.md` + `scripts/`)
- 两个 CLI 脚本:
  - `scripts/discover.py` —— 列出 W Connector 上所有可买的付费工具
  - `scripts/buy.py` —— 端到端走一遍购买 + 调用
- 两套钱包后端 —— **推荐搭配:测试用 `local`,生产用 `okx`**:
  - **`local`**(默认)—— 本地私钥在 **Sepolia 测试网**(免费、安全)签名。适合开发 / demo / CI。
  - **`okx`** —— 委托给 OKX Agentic Wallet,带 TEE 签名 + Policy 策略控制。生产环境首选。只支持主网 —— [配置文档](https://web3.okx.com/zh-hans/onchainos/dev-docs/home/install-your-agentic-wallet)。

## 快速开始

```bash
# 1. 克隆
git clone https://github.com/wspn-ai/wagent-skill.git
cd wagent-skill

# 2. 选后端(想玩玩选 Sepolia 测试网,免费)
#    推荐:拷 .env.example 到 .env 然后改。或用 export 也行。
cp .env.example .env       # 然后编辑
# 或者:
export PAYMENT_BACKEND=local
export LOCAL_NETWORK=sepolia
export AGENT_WALLET_PRIVATE_KEY=0x...   # 一次性的钥匙就行

# 3. 看看在卖什么
python3 scripts/discover.py

# 4. 买一次 + 跑一次工具(--agent-url 用 discover.py 输出里任一行的 agent_url)
python3 scripts/buy.py \
  --agent-url "<discover.py 输出里的 merchant agent_url>" \
  --product-id 101 \
  --quantity 1 \
  --token ETH_USDC \
  --call-body '{"address":"0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045"}'
```

或者把它装进 Claude Code 当 skill 用,让 agent 自己开车 —— 完整步骤见 **[SETUP.zh-CN.md](SETUP.zh-CN.md)**。

## 文档目录

| 文档 | 内容 |
|---|---|
| **[SETUP.zh-CN.md](SETUP.zh-CN.md)** / [EN](SETUP.md) | 安装 + 第一次跑通 + 接 Claude Code |
| **[WALLET-BACKENDS.zh-CN.md](WALLET-BACKENDS.zh-CN.md)** / [EN](WALLET-BACKENDS.md) | local web3.py vs OKX Agentic Wallet —— 选哪个 |
| **[TROUBLESHOOTING.zh-CN.md](TROUBLESHOOTING.zh-CN.md)** / [EN](TROUBLESHOOTING.md) | 常见错误 + 解决方法 |
| **[SKILL.md](SKILL.md)** | skill 自身的 manifest(给 agent harness 读) |

## 链 / 成本预期

| 后端 | 链 | 费用 | 接入耗时 | 适用场景 |
|---|---|---|---|---|
| `local` + Sepolia | 测试网 | **免费**(faucet 给 ETH + USDC) | 5 分钟 | **测试 / 开发首选** |
| `local` + mainnet | 以太坊主网 | 真金白银 —— gas ~$0.50 + 工具 $0.10–$0.60 | 10 分钟 | 主网自托管 |
| `okx` | Base / 以太坊 / BSC / Arbitrum / Polygon | 同上 | 15 分钟([OKX 配置](https://web3.okx.com/zh-hans/onchainos/dev-docs/home/install-your-agentic-wallet)) | **生产首选** |

**OKX 没有测试网。** 想白嫖练手 → `local` + Sepolia。上生产 → 切到 `okx`,用 MPC 托管 + Policy 控制,别把热私钥放在服务器上。

## 它是怎么工作的(60 秒版本)

1. **Discover** —— `discover.py` 调 W Connector 的 `/merchants/search`,再去每个商户的 `/.well-known/agent.json` 列出所有 SKU。
2. **下单意图** —— `buy.py` 给商户 shop 发 A2A `tasks/send`。Shop 在 W Connector 上创建订单,返回付款地址。
3. **支付** —— wagent 用本地私钥或 OKX 签一笔精确金额的 EVM 转账。上链 hash 立即返回。
4. **结算** —— Stablelink(W Connector 用的支付通道)监听链上,触发 shop 的 webhook,shop 把 "PAID" 中继给 W Connector。
5. **发 token** —— wagent 轮询 `delivery` artifact,拿到一个 24 小时内有效的 bearer token(`calls_per_unit: 1` —— 一个 token 调一次)。
6. **调用** —— wagent 把用户输入 POST 到 `/v1/tools/<tool_name>`,带 bearer。结果返回,token 销毁。

完整的 A2A + 支付协议在 W Connector 内部跑,**你不用学** —— 跑两个脚本就行。

## 安全注意

- 当 `PAYMENT_BACKEND=local LOCAL_NETWORK=mainnet` 或 `PAYMENT_BACKEND=okx` 时,这个 skill 真的会在链上花真钱。务必保证 `--call-body` 匹配每个 product 的 `input_schema`(skill 内置校验,不匹配直接拒付)。
- 一次 `buy.py` 调用 = 一笔订单。同一条命令重跑就是再付一次。Skill 检测到 5 分钟内有相似购买会警告 —— 加 `--force-new` 强制下单。
- Token 持久化在 `~/.wagent/tokens.json`。用 `--use-token <order_no>` 复用(当 `calls_per_unit > 1` 时,当前默认不开),或 `--return-token <order_no>` 销毁。

## 协议

MIT —— 详见 [LICENSE](LICENSE)。

## Issues / PR

欢迎提 Issues 和 PR。如果问的是**协议层面**的问题(W Connector 网络本身、商户接入、结算、gateway 行为之类),去 **[W Connector 接入仓库](https://github.com/wspn-ai/wconnector-integration/issues)** 提 issue。
