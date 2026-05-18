# skill/scripts/local/_wallet_local.py
"""Local web3.py ERC-20 transfer backend (Ethereum mainnet only).

The "test wallet" backend — you control the private key directly. Use a
throwaway wallet with a small balance ($1–$5) for testing. Production
traffic should use OKX (MPC custody + Policy controls).

Reads at call time so tests can monkeypatch:
  AGENT_WALLET_PRIVATE_KEY  – signing key (required)
  MAINNET_RPC_URL           – Ethereum mainnet RPC (required)

Independent of `scripts/okx/`; deleting either subdir doesn't break the other.
"""
from __future__ import annotations
import os
import sys
import pathlib

from web3 import Web3

try:
    from .. import _tokens  # noqa: F401 — kept for import parity with okx
except ImportError:
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
    import _tokens  # type: ignore[no-redef]  # noqa: E402,F401


_ETHEREUM_CHAIN_ID = 1

ERC20_ABI = [
    {"name": "decimals", "type": "function", "inputs": [],
     "outputs": [{"name": "", "type": "uint8"}], "stateMutability": "view"},
    {"name": "transfer", "type": "function",
     "inputs": [{"name": "to", "type": "address"},
                {"name": "amount", "type": "uint256"}],
     "outputs": [{"name": "", "type": "bool"}], "stateMutability": "nonpayable"},
]


class ConfigError(RuntimeError):
    pass


class TxRevertedError(RuntimeError):
    pass


class InsufficientBalanceError(ConfigError):
    """Wallet doesn't hold enough of the requested token. Surfaced before
    broadcast so the buy pipeline can fall back to the next token."""
    pass


def _require_env(key: str) -> str:
    val = os.environ.get(key)
    if not val:
        raise ConfigError(f"{key} not set")
    return val


def send_erc20(*, contract: str, recipient: str, amount: float) -> str:
    """Sign and broadcast an ERC-20 transfer on Ethereum mainnet.
    Returns 0x-prefixed 64-hex tx hash."""
    private_key = _require_env("AGENT_WALLET_PRIVATE_KEY")
    rpc_url = _require_env("MAINNET_RPC_URL")

    w3 = Web3(Web3.HTTPProvider(rpc_url))
    account = w3.eth.account.from_key(private_key)
    erc20 = w3.eth.contract(
        address=Web3.to_checksum_address(contract), abi=ERC20_ABI,
    )

    decimals = erc20.functions.decimals().call()
    raw_amount = int(round(float(amount) * 10 ** decimals))

    # Cheap pre-flight balance check — catches "wallet underfunded" before
    # broadcast so the buy pipeline can fall back to the next token.
    try:
        balance_raw = w3.eth.contract(
            address=Web3.to_checksum_address(contract),
            abi=ERC20_ABI + [{
                "name": "balanceOf", "type": "function",
                "inputs": [{"name": "owner", "type": "address"}],
                "outputs": [{"name": "", "type": "uint256"}],
                "stateMutability": "view",
            }],
        ).functions.balanceOf(account.address).call()
        if balance_raw < raw_amount:
            raise InsufficientBalanceError(
                f"wallet {account.address} holds "
                f"{balance_raw / 10 ** decimals} of contract {contract} "
                f"but needs {amount}"
            )
    except InsufficientBalanceError:
        raise
    except Exception:
        # balanceOf failed for some unrelated reason — proceed and let the
        # broadcast surface the error naturally.
        pass

    gas_price = w3.eth.gas_price * 6 // 5             # +20% premium
    nonce = w3.eth.get_transaction_count(account.address)

    tx = erc20.functions.transfer(
        Web3.to_checksum_address(recipient), raw_amount,
    ).build_transaction({
        "chainId": _ETHEREUM_CHAIN_ID,
        "gas": 100000,
        "gasPrice": gas_price,
        "nonce": nonce,
    })
    signed = w3.eth.account.sign_transaction(tx, account.key)
    raw_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    receipt = w3.eth.wait_for_transaction_receipt(raw_hash, timeout=120)
    if receipt.get("status", 1) == 0:
        raise TxRevertedError("ERC-20 transfer reverted")
    return "0x" + raw_hash.hex()
