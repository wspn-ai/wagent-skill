#!/usr/bin/env bash
# Smoke test for PAYMENT_BACKEND=okx — Ethereum mainnet, REAL MONEY.
# Cannot run unattended because OKX may require phone confirmation.
set -euo pipefail

cat <<'EOM'
================================================================================
⚠️  REAL MONEY — Ethereum mainnet via OKX Agentic Wallet.
================================================================================

Pre-flight:
  ☐ Policy Settings configured at
    https://web3.okx.com/portfolio/agentic-wallet-policy
       - Per-tx cap:  $5
       - Daily cap:   $20
  ☐ Wallet has ≥ $0.10 USDC + a tiny bit of ETH for gas
  ☐ Logged in: OKX_API_KEY env set, OR `onchainos wallet login` ran
  ☐ Picked a real merchant + product_id from `python3 scripts/discover.py`

Then run, replacing the placeholders with real values:

  PAYMENT_BACKEND=okx \
  CONNECTOR_URL=https://connector-dev.wcheckout.app \
  python3 scripts/buy.py \
    --agent-url "<merchant agent_url — get from scripts/discover.py>" \
    --product-id 201 \
    --quantity 1 \
    --token ETH_USDC \
    --call-body '{"address": "0xabc..."}'

What to watch for:
  • OKX phone push if the order exceeds your per-tx cap → confirm in app
  • A line `Tx: 0x<64 hex>` on stdout
  • A JSON tool response after that line
================================================================================
EOM
