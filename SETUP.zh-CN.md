# 接入指南 —— wagent skill

> **English → [SETUP.md](SETUP.md)**

端到端的首次跑通，从 `git clone` 到工具调用成功，大约 10 分钟。

本 skill 生产环境专用 —— 以太坊主网，每笔都是真金。两套钱包后端，挑一个。

## 前置条件

- Python 3.10+
- 二选一：
  - **OKX**（生产推荐）：装好 [`onchainos`](https://web3.okx.com/zh-hans/onchainos/dev-docs/home/install-your-agentic-wallet) CLI
  - **Local**（小额测试钱包）：`pip install web3` + 一个以太坊主网 RPC URL（Alchemy / Infura / QuickNode 免费档够用）
- 钱包里：
  - 留一点 ETH 作为 gas（≈ $5 就够）
  - 至少 $0.10 的 USDC / USDT / WUSD 用于测试调用

## 第 1 步 —— 克隆

```bash
git clone https://github.com/wspn-ai/wagent-skill.git
cd wagent-skill
```

## 第 2 步 —— 配钱包后端

### 方案 A —— OKX Agentic Wallet（生产推荐）

```bash
onchainos wallet login <你的邮箱>
# 按提示输入 OTP
```

去设置每笔 / 每日的 USD 上限，agent 有 bug 也掏不空钱包：

> <https://web3.okx.com/portfolio/agentic-wallet-policy>

测试期建议从严：
- 每笔：**$5**
- 每日：**$20**

### 方案 B —— 本地测试钱包（小额体验）

```bash
pip install web3
export PAYMENT_BACKEND=local

# 生成一个一次性私钥。绝不要复用持有真金的私钥。
python3 -c "import secrets; print('0x' + secrets.token_hex(32))"
export AGENT_WALLET_PRIVATE_KEY=0x<上一行输出的 64-hex>

# 任意以太坊主网 RPC
export MAINNET_RPC_URL=https://eth-mainnet.g.alchemy.com/v2/<YOUR_KEY>
```

往钱包打 $1–$5 稳定币 + 一点 ETH 当 gas。Connector 和商户跟生产是同一套 —— 只是换了签名方。

## 第 3 步 —— 看市场上有什么工具

```bash
python3 scripts/discover.py
```

输出形如（具体的商户、价格、URL 取决于当时活跃的商户 —— **别照搬下面这些 URL，用你自己输出里的**）：

```
product_id  merchant        tool_name               $/pack    pack   $/call   agent_url
─────────────────────────────────────────────────────────────────────────────────────────
101         AgentPay Tools  onchain_profile         $0.10     x1     $0.10    https://<merchant-agent>.wcheckout.app
201         AgentPay Tools  sanctions_screen        $0.30     x1     $0.30    https://<merchant-agent>.wcheckout.app
202         AgentPay Tools  address_risk_profile    $0.60     x1     $0.60    https://<merchant-agent>.wcheckout.app
```

空表 / 报错 → 看 [TROUBLESHOOTING.zh-CN.md](TROUBLESHOOTING.zh-CN.md#discoverpy-空表--报错)。

## 第 4 步 —— 买 + 调用

从表里挑一个 `product_id`，**用同一行的 `agent_url`**（每个商户的 URL 不一样，跟 connector URL 也不是同一个）：

```bash
python3 scripts/buy.py \
  --agent-url "<把 discover.py 输出里这一行的 agent_url 粘进来>" \
  --product-id 201 \
  --quantity 1 \
  --call-body '{"address":"0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045"}'
```

成功的话会看到：

```
Tx: 0x<64 hex>
{
  "address": "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045",
  "ofac_match": false,
  ...
}
```

端到端总耗时 ~30 秒。

## 第 5 步 —— 挂进你的 agent harness

skill 遵循 Anthropic skill 格式 —— 顶层一个 `SKILL.md`，脚本在 `scripts/` 下。
把目录放进 harness 的 skills 文件夹，下次启动自动识别。

### Claude Code

```bash
# 用户级(所有 Claude Code 会话都能用)
ln -s "$(pwd)" ~/.claude/skills/wagent

# 或项目级
mkdir -p .claude/skills && ln -s "$(pwd)" .claude/skills/wagent
```

会话里直接描述意图：*"用 wagent 帮我筛查 0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045 是不是 OFAC 制裁地址"*。Claude 会自己读 `SKILL.md`，跑脚本，链上付款前让你确认，然后把结果展示给你。

### OpenClaw

```bash
ln -s "$(pwd)" ~/.openclaw/workspace/skills/wagent
```

重启 OpenClaw（或 `:reload skills`），跟 Claude Code 一样调用。

### Hermes（及其他 Anthropic 风格 harness）

```bash
ln -s "$(pwd)" "$(hermes config get skills.path)/wagent"
hermes reload
```

其他 harness 同理：把 repo 软链进 harness 的 skills 目录就行。agent 通过描述触发，比如 *"买 X 工具 用 wagent"*、*"用 wagent 找一下 MCP 商户"*。

## 接下来

- 出错了看 [TROUBLESHOOTING.zh-CN.md](TROUBLESHOOTING.zh-CN.md)。
- 想反过来当卖家做商户和工具 → 看 [W Connector 接入手册](https://github.com/wspn-ai/wconnector-integration)。
