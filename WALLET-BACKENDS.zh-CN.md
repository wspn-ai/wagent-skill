# 钱包后端

> **English → [WALLET-BACKENDS.md](WALLET-BACKENDS.md)**

`wagent` 支持两套可切换的钱包后端来给链上付款签名。**推荐搭配:测试 / 开发用 `local`,生产用 `okx`。**

## 速查

| | `PAYMENT_BACKEND=local`(默认) | `PAYMENT_BACKEND=okx` |
|---|---|---|
| **什么时候用** | 开发 / 快速迭代 / CI | 真金白银的生产演示 |
| **链** | Sepolia(默认)或以太坊主网(`LOCAL_NETWORK=mainnet`) | Base(默认)/ Ethereum / BSC / Arbitrum / Polygon(`OKX_NETWORK`)。全主网,OKX 没测试网 |
| **真钱?** | 否(Sepolia)/ 是(主网) | **是** |
| **托管方式** | 明文私钥放 env | MPC(分片密钥)+ Policy 控制 |
| **认证** | env 里 `AGENT_WALLET_PRIVATE_KEY` | `onchainos wallet login`(OTP)或 `OKX_API_KEY` env |
| **接入耗时** | 30 秒 | ~5 分钟(网页 + 手机 App 验证) |

## 怎么切换

```bash
export PAYMENT_BACKEND=local      # 默认,Sepolia
# 或者
export PAYMENT_BACKEND=okx        # Base 主网,真钱
```

签名时自动检测,不用重启不用重新 build。来回切不会丢另一边的状态。

---

## 后端 1 —— `local`(默认)

基于 web3.py 的本地签名。**默认 Sepolia 测试网**(免费、安全)。`LOCAL_NETWORK=mainnet` 切到以太坊主网(真钱)。

### 必填 env

```bash
PAYMENT_BACKEND=local                      # 不填也是这个
AGENT_WALLET_PRIVATE_KEY=0xabcdef...       # 你的钱包私钥
LOCAL_NETWORK=sepolia                      # sepolia(默认)| mainnet

# Sepolia 路径:
SEPOLIA_RPC_URL=https://eth-sepolia.g.alchemy.com/v2/<your_key>

# 主网路径:
MAINNET_RPC_URL=https://eth-mainnet.g.alchemy.com/v2/<your_key>
# token 合约地址默认用规范的 USDC/USDT/WUSD 主网地址。需要换非规范的才覆盖 ——
# 完整地址见下方 "Token 合约" 表。
```

### Token 合约(内置默认值)

`local` 后端会把 `--token` 解析成下面这些链上 ERC-20。**正常情况下不用设 env 覆盖**,只有在测自定义部署时才用。

| `--token`    | Sepolia(测试)                                | 以太坊主网                                    | env 覆盖变量                |
|---           |---                                            |---                                           |---                          |
| `ETH_USDC`   | `0x1c7D4B196Cb0C7B01d743Fbc6116a902379C7238` | `0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48` | `ETH_USDC_CONTRACT`         |
| `ETH_WUSD`   | `0x371607f7463d27ae9deaf64ae00da9cbd4cf0065` | `0x7Cd017ca5ddb86861FA983a34b5F495C6F898c41` | `ETH_WUSD_CONTRACT`         |
| `ETH_USDT`   | *(Sepolia 没有官方 USDT,有自部署用 `ETH_USDT_CONTRACT_SEPOLIA` 指过去)* | `0xdAC17F958D2ee523a2206206994597C13D831ec7` | `ETH_USDT_CONTRACT`         |

**WUSD 特别说明** —— Sepolia 和主网的 WUSD 合约地址不一样(上表)。skill 会根据 `LOCAL_NETWORK` 自动选对的那个。发真钱前**务必去区块浏览器(Etherscan / Sepolia Etherscan)再核对一遍** —— WUSD 合约可能重新部署。

### 验证连通

```bash
python3 -c "from web3 import Web3; print(Web3(Web3.HTTPProvider('$SEPOLIA_RPC_URL')).eth.block_number)"
```

### 风险

- **明文私钥放 env** —— 任何进程 / 日志泄露都会暴露。**用一次性钱包**。
- **没有花费上限** —— agent 能花到 gas 上限。只放你愿意丢的钱。

### Sepolia faucet

| 来源 | 说明 |
|---|---|
| <https://sepoliafaucet.com> | Alchemy faucet,每天 0.5 ETH |
| <https://www.infura.io/faucet/sepolia> | 需要 Infura 账号 |
| <https://faucet.quicknode.com/ethereum/sepolia> | QuickNode faucet |
| USDC faucet → <https://faucet.circle.com/> | Circle 的 Sepolia USDC |

---

## 后端 2 —— `okx`(生产)

OKX Agentic Wallet —— 多链主网,MPC 托管 + 网页端 Policy 控制。**每条链都是真钱,OKX 没测试网。**

### 必填 env

```bash
PAYMENT_BACKEND=okx
OKX_NETWORK=base                          # base(默认)| ethereum | bsc | arbitrum | polygon
OKX_TOKEN_CONTRACT=                       # 可选,覆盖该链默认 USDC

# 静默登录:
OKX_API_KEY=oak_xxxxxxxxxxxxxxxx
# 或者交互登录(env 都不用填):
#   onchainos wallet login your-email@example.com
#   onchainos wallet verify <otp>
```

### 必做配置

1. **OKX 注册** —— 去 <https://web3.okx.com> 注册,开通 Agentic Wallet,过手机 App 验证。**完整教程:** <https://web3.okx.com/zh-hans/onchainos/dev-docs/home/install-your-agentic-wallet>
2. **Spend Policies** —— 在 <https://web3.okx.com/portfolio/agentic-wallet-policy> 设单笔和每日上限。推荐入门:单笔 $5、每日 $20、只允许 USDC。
3. **充值** —— 充你愿意丢的最小金额(比如 $5 USDC 到 Base 上)。

### 验证

```bash
onchainos wallet status
# → 显示余额、默认链、policy 摘要
```

### 风险

- **真金白银**。wagent 或 W Connector 有 bug 会丢 USDC。第一次跑前先把 Policy 上限收紧。
- **主网 gas**。留一点 ETH/原生币给 gas。skill 会报"gas 不够",不会静默失败。

---

## 后端选择速查

```
测试 / 开发(推荐)               → local(默认 Sepolia)
以太坊主网自托管                  → local + LOCAL_NETWORK=mainnet(真钱)
生产(推荐)                      → okx(默认 Base) —— 见下方 OKX onchainos 配置链接
以太坊主网上付 WUSD                → okx + OKX_NETWORK=ethereum + OKX_TOKEN_CONTRACT=0x7Cd017ca...
```

**默认演进路径:** 测试期跑 `local` + Sepolia。**上生产前切到 `okx`** —— MPC 托管 + 网页端 Policy 控制(单笔 / 每日上限)比把热私钥扔服务器 env 里安全得多。

OKX 配置参考(官方中文):<https://web3.okx.com/zh-hans/onchainos/dev-docs/home/install-your-agentic-wallet>
