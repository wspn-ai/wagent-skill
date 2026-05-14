# 接入指南 —— wagent skill

> **English → [SETUP.md](SETUP.md)**

端到端的首次跑通,从 `git clone` 到工具调用成功,大约 10 分钟。

## 前置条件

- Python 3.10+
- `pip install web3`(用 `local` 后端)或 OKX 账号开通(用 `okx` 后端,见 [WALLET-BACKENDS.zh-CN.md](WALLET-BACKENDS.zh-CN.md))
- 一个以太坊 RPC endpoint(Alchemy / Infura / QuickNode 都行,Sepolia 免费够用)
- 一个测试钱包 —— **绝不要用持有真金的私钥**

## 第 1 步 —— 克隆

```bash
git clone https://github.com/wspn-ai/wagent-skill.git
cd wagent-skill
```

## 第 2 步 —— 选钱包后端

**测试 / 开发 —— 用 `local` + Sepolia 测试网**(免费,丢不了钱)。下面的步骤都假设你用这个。

**生产 —— 用 `okx`(OKX Agentic Wallet)** —— MPC 托管 + Policy 控制,agent 有 bug 也不会把钱包掏空。配置教程:<https://web3.okx.com/zh-hans/onchainos/dev-docs/home/install-your-agentic-wallet>。OKX 配好之后直接跳到 [WALLET-BACKENDS.zh-CN.md §后端 2](WALLET-BACKENDS.zh-CN.md#后端-2--okx生产)。

两种配置方式 —— **推荐用 `.env` 文件**:

```bash
# 简单路径:拷模板改值
cp .env.example .env
# 在编辑器里打开 .env,取消注释并填好 PAYMENT_BACKEND / LOCAL_NETWORK /
# AGENT_WALLET_PRIVATE_KEY / SEPOLIA_RPC_URL。脚本每次运行都会自动读 `.env`。

# 或者:shell export(不想写文件可以用这个)
export PAYMENT_BACKEND=local
export LOCAL_NETWORK=sepolia
export AGENT_WALLET_PRIVATE_KEY=0x<你的一次性私钥>
export SEPOLIA_RPC_URL=https://eth-sepolia.g.alchemy.com/v2/<your_alchemy_key>
```

两种都行 —— shell env 永远赢 `.env`,所以 `.env` 当默认配置留着、临时用 `export` 覆盖也没问题。`.env` 在 `.gitignore` 里,不会被误提交。

用 `python3 -c "import secrets; print('0x' + secrets.token_hex(32))"` 生成一个一次性私钥。**绝不要复用持有真金的私钥。** 然后去 <https://sepoliafaucet.com> 领 Sepolia ETH,去 <https://faucet.circle.com> 领 Sepolia USDC。

想用真金 / 主网 / OKX → 看 [WALLET-BACKENDS.zh-CN.md](WALLET-BACKENDS.zh-CN.md)。

## 第 3 步 —— 冒烟测连通性

```bash
python3 -c "from web3 import Web3; print('latest block:', Web3(Web3.HTTPProvider('$SEPOLIA_RPC_URL')).eth.block_number)"
# → latest block: <一个数字>
```

如果报错,先修 `SEPOLIA_RPC_URL`。不通的话后面全做不了。

## 第 4 步 —— 看市场上有什么工具

```bash
python3 scripts/discover.py
```

输出形如(具体的商户、价格、URL 取决于当时活跃的商户 —— **别照搬下面这些 URL,用你自己输出里的**):

```
product_id  merchant        tool_name               $/pack    pack   $/call   agent_url
─────────────────────────────────────────────────────────────────────────────────────────
101         AgentPay Tools  onchain_profile         $0.10     x1     $0.10    https://<merchant-agent>.wcheckout.app
201         AgentPay Tools  sanctions_screen        $0.30     x1     $0.30    https://<merchant-agent>.wcheckout.app
202         AgentPay Tools  address_risk_profile    $0.60     x1     $0.60    https://<merchant-agent>.wcheckout.app
```

空表 / 报错 → 看 [TROUBLESHOOTING.zh-CN.md](TROUBLESHOOTING.zh-CN.md#discover-报错)。

## 第 5 步 —— 买 + 调用

从表里挑一个 `product_id`,**用同一行的 `agent_url`**(每个商户的 URL 不一样,跟 `CONNECTOR_URL` 也不是同一个):

```bash
python3 scripts/buy.py \
  --agent-url "<把 discover.py 输出里这一行的 agent_url 粘进来>" \
  --product-id 201 \
  --quantity 1 \
  --token ETH_USDC \
  --call-body '{"address":"0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045"}'
```

成功的话会看到类似:

```
✅ Order ORD_xxx PAID  ($0.30 ETH_USDC)
✅ Tool call → sanctions_screen
{
  "address": "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045",
  "ofac_match": false,
  ...
}
```

端到端总耗时 ~30 秒(主要等 Sepolia 出块确认)。

## 第 6 步 —— 挂进你的 agent harness

skill 遵循 Anthropic skill 格式 —— 顶层一个 `SKILL.md`,脚本在 `scripts/` 下。任何支持 Anthropic 风格 skill 的 harness 都能加载它。下面三个具体例子。

> **模式都一样**:把 skill 目录(软链或拷贝)放到 harness 的 skills 文件夹下,harness 下次启动时识别它,agent 用 skill 名字(`wagent`)调用。

### Claude Code(Anthropic 官方 CLI)

```bash
# 用户级(所有 Claude Code 会话都能用)
ln -s "$(pwd)" ~/.claude/skills/wagent

# 或项目级(只在含 .claude/skills/ 的项目里生效)
mkdir -p .claude/skills && ln -s "$(pwd)" .claude/skills/wagent
```

在 Claude Code 会话里直接描述意图,Claude 会自己找 skill:

> "用 wagent 帮我筛查 0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045 是不是 OFAC 制裁地址"

Claude 自己读 `SKILL.md`、跑 `scripts/discover.py` 和 `scripts/buy.py`、链上付款前会让你确认、把结果展示给你。

### OpenClaw

OpenClaw 用 per-workspace 的 skills 目录:

```bash
ln -s "$(pwd)" ~/.openclaw/workspace/skills/wagent
```

重启 OpenClaw(或者会话里 `:reload skills`,如果你的版本支持)。然后跟 Claude Code 一样调用。

### Hermes

Hermes 的 skill 加载路径是可配置的。看看你的是什么:

```bash
hermes config get skills.path     # 或: hermes config --show | grep skills
```

然后把 skill 软链到那里:

```bash
ln -s "$(pwd)" "$(hermes config get skills.path)/wagent"
hermes reload     # 或重启 Hermes
```

如果你的 Hermes 没有 `config get skills.path` 这个命令,查你这个版本的文档 —— 常见默认是 `~/.hermes/skills/` 或 `~/.config/hermes/skills/`。

### 其他 Anthropic 风格 harness

只要 harness 从某个目录里加载 skill(比如 `~/.agents/skills/`、`./skills/` 之类),把 wagent repo 软链到那个位置,名字叫 `wagent`。harness 下次启动会读 `SKILL.md`。

```bash
ln -s "$(pwd)" <你的-harness-skills-目录>/wagent
```

agent 通过描述触发,比如 "买 X 工具 用 wagent" 或 "用 wagent 找一下 MCP 商户" —— 这是 Anthropic skill 标准的 description-matching 触发方式。

### 环境变量 —— 配一次,所有 harness 都识别

所有 harness 都继承你的 shell 环境。把 `PAYMENT_BACKEND`、`AGENT_WALLET_PRIVATE_KEY`、`SEPOLIA_RPC_URL` 这些放进 `~/.zshrc` / `~/.bashrc` / `~/.config/fish/config.fish`,每个 harness 都自动看到。**不要把秘密放进 skill repo 本身** —— `.gitignore` 已经排除了 `.env*` 以防万一,但最安全的还是放在 shell rc 或 secret manager 里。

## 接下来

- 切到主网 / OKX 前先看 [WALLET-BACKENDS.zh-CN.md](WALLET-BACKENDS.zh-CN.md)。
- 出错了看 [TROUBLESHOOTING.zh-CN.md](TROUBLESHOOTING.zh-CN.md)。
- 想反过来,自己当卖家做商户和工具 → 看 [W Connector 接入手册](https://github.com/wspn-ai/wconnector-integration)。
