# skill/scripts/okx/_wallet_okx.py
"""OKX Agentic Wallet ERC-20 transfer backend (Ethereum mainnet only).

Wraps `onchainos wallet send` via subprocess. Returns the txHash from
the CLI's stdout JSON. OKX has no testnets — every call is real money.

Lives in `skill/scripts/okx/` — the production / mainnet backend.
Independent of `skill/scripts/local/` (the testnet-friendly web3.py
backend); deleting either subdir doesn't break the other.

Env:
  OKX_TOKEN_CONTRACT  – optional override for the ERC-20 contract address
                        (e.g. WUSD on Ethereum). Without it, the `contract`
                        argument is passed straight through.
"""
from __future__ import annotations
import json
import os
import re
import subprocess
from decimal import Decimal


TX_HASH_RE = re.compile(r"0x[0-9a-fA-F]{64}")


def _format_amount(amount: float) -> str:
    """Format a token amount for the CLI without scientific notation.

    Uses Decimal(str(amount)) to recover the user-intended decimal precision
    (avoiding IEEE 754 artefacts from f"{amount:.18f}"), then strips trailing
    zeros.  Decimal also normalises scientific notation: str(0.00005) is
    '5e-05' but Decimal('5e-05') renders as '0.00005', which onchainos
    accepts."""
    return str(Decimal(str(amount))).rstrip("0").rstrip(".")


class CliError(RuntimeError):
    pass


class HighRiskError(RuntimeError):
    pass


class InsufficientBalanceError(CliError):
    """The OKX wallet doesn't have enough of the requested token to send.
    Caught at simulate-tx / estimateGas time; no on-chain side effect."""
    pass


# Heuristics that classify an onchainos error JSON as 'wallet underfunded'.
# 'transfer amount exceeds balance' is the OZ ERC-20 standard revert. Other
# tokens (USDT non-OZ) emit 'insufficient balance', 'insufficient funds',
# or omit a clean reason — match defensively.
_INSUFFICIENT_BALANCE_PATTERNS = (
    "transfer amount exceeds balance",
    "insufficient balance",
    "insufficient funds",
    "amount exceeds balance",
)


def _looks_like_insufficient_balance(blob: str) -> bool:
    low = blob.lower()
    return any(p in low for p in _INSUFFICIENT_BALANCE_PATTERNS)


def send_erc20(*, contract: str, recipient: str, amount: float) -> str:
    """Run `onchainos wallet send` for an ERC-20 transfer on Ethereum mainnet.

    The `contract` arg can be overridden by the OKX_TOKEN_CONTRACT env."""
    final_contract = os.environ.get("OKX_TOKEN_CONTRACT") or contract

    cmd = [
        "onchainos", "wallet", "send",
        "--readable-amount", _format_amount(amount),
        "--recipient", recipient,
        "--chain", "ethereum",
        "--contract-token", final_contract,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)

    # Parse JSON if possible — onchainos always emits JSON on stdout.
    stdout = result.stdout or ""
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        payload = {}

    if payload.get("confirming") is True:
        raise HighRiskError(
            "OKX flagged this transaction as high-risk; confirm in the OKX "
            "phone app, then re-run with --force. Auto-retry is disabled."
        )

    if result.returncode != 0:
        # Detect insufficient-balance early so the buyer skill / agent can
        # surface a clean message ('try a different token / top up') instead
        # of dumping the raw JSON revert reason.
        error_text = payload.get("error") or stdout
        combined = f"{error_text} {result.stderr or ''}"
        if _looks_like_insufficient_balance(combined):
            raise InsufficientBalanceError(
                f"insufficient balance for {_format_amount(amount)} "
                f"of contract {final_contract} on Ethereum mainnet. "
                f"Top up the OKX wallet or pick a token you actually hold."
            )
        raise CliError(
            f"onchainos wallet send exited {result.returncode}: "
            f"{result.stderr.strip() or stdout.strip()}"
        )

    tx_hash = payload.get("txHash") or ""
    if not TX_HASH_RE.fullmatch(tx_hash):
        # Fallback: regex-scrape stdout in case the field name shifts.
        m = TX_HASH_RE.search(stdout)
        if not m:
            raise CliError(f"onchainos wallet send returned no tx hash: {stdout!r}")
        tx_hash = m.group(0)
    return tx_hash
