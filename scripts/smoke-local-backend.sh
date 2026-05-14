#!/usr/bin/env bash
# Smoke test for PAYMENT_BACKEND=local — ensures buy.py produces a tx hash
# end-to-end against a real merchant on Sepolia. Requires a funded wallet.
set -euo pipefail

: "${CONNECTOR_URL:?Set CONNECTOR_URL}"
: "${AGENT_WALLET_PRIVATE_KEY:?Set AGENT_WALLET_PRIVATE_KEY}"
: "${SEPOLIA_RPC_URL:?Set SEPOLIA_RPC_URL}"
: "${SMOKE_AGENT_URL:?Set SMOKE_AGENT_URL — a real merchant from /merchants/search}"
: "${SMOKE_PRODUCT_ID:?Set SMOKE_PRODUCT_ID}"
: "${SMOKE_CALL_BODY:?Set SMOKE_CALL_BODY — JSON object for the tool call}"

export PAYMENT_BACKEND=local
export LOCAL_NETWORK=sepolia

# Capture stdout to grep for tx hash; let stderr stream to the user.
output=$(python3 scripts/buy.py \
  --agent-url "$SMOKE_AGENT_URL" \
  --product-id "$SMOKE_PRODUCT_ID" \
  --quantity 1 \
  --token ETH_USDC \
  --call-body "$SMOKE_CALL_BODY" 2>&1 | tee /dev/stderr)

if echo "$output" | grep -qE 'Tx: 0x[0-9a-fA-F]{64}'; then
  echo "✅ smoke local backend: PASS"
  exit 0
fi
echo "❌ smoke local backend: FAIL (no tx hash)"
exit 1
