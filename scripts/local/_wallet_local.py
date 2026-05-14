# skill/scripts/local/_wallet_local.py
"""Local web3.py ERC-20 transfer backend.

Reads from env at call time (no module-level config) so tests can monkeypatch:
  AGENT_WALLET_PRIVATE_KEY  – signing key (required)
  LOCAL_NETWORK             – sepolia | mainnet (default sepolia)
  SEPOLIA_RPC_URL           – required if LOCAL_NETWORK=sepolia
  MAINNET_RPC_URL           – required if LOCAL_NETWORK=mainnet

Lives in `skill/scripts/local/` — the testnet-friendly self-managed wallet
path. Independent of `skill/scripts/okx/` (the OKX mainnet path); deleting
either subdir doesn't break the other."""
from __future__ import annotations
import os
import sys
from web3 import Web3

# _tokens.py lives one level up (shared lookup table). Relative import works
# when this module is loaded as part of the skill.scripts package (pytest,
# `python3 -m …`); the absolute fallback adds the parent directory to
# sys.path for direct CLI invocation in non-standard harness layouts.
try:
    from .. import _tokens
except ImportError:
    import pathlib
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
    import _tokens  # type: ignore[no-redef]  # noqa: E402


ERC20_ABI = [
    {"name": "decimals", "type": "function", "inputs": [],
     "outputs": [{"name": "", "type": "uint8"}], "stateMutability": "view"},
    {"name": "transfer", "type": "function",
     "inputs": [{"name": "to", "type": "address"}, {"name": "amount", "type": "uint256"}],
     "outputs": [{"name": "", "type": "bool"}], "stateMutability": "nonpayable"},
]


class ConfigError(RuntimeError):
    pass


class TxRevertedError(RuntimeError):
    pass


def _require_env(key: str) -> str:
    val = os.environ.get(key)
    if not val:
        raise ConfigError(f"{key} not set")
    return val


def send_erc20(*, contract: str, recipient: str, amount: float) -> str:
    """Sign and broadcast an ERC-20 transfer. Returns 0x-prefixed 64-hex tx hash.

    Reads RPC URL + private key + chain id from env (see module docstring)."""
    network = os.environ.get("LOCAL_NETWORK", "sepolia")
    chain_id = _tokens.chain_id(network)              # validates network first
    private_key = _require_env("AGENT_WALLET_PRIVATE_KEY")
    if network == "mainnet":
        rpc_url = _require_env("MAINNET_RPC_URL")
    else:
        rpc_url = _require_env("SEPOLIA_RPC_URL")

    w3 = Web3(Web3.HTTPProvider(rpc_url))
    account = w3.eth.account.from_key(private_key)
    erc20 = w3.eth.contract(address=Web3.to_checksum_address(contract), abi=ERC20_ABI)

    decimals = erc20.functions.decimals().call()
    raw_amount = int(round(float(amount) * 10 ** decimals))
    gas_price = w3.eth.gas_price * 6 // 5  # +20% premium, integer-only
    nonce = w3.eth.get_transaction_count(account.address)

    tx = erc20.functions.transfer(
        Web3.to_checksum_address(recipient), raw_amount
    ).build_transaction({
        "chainId": chain_id,
        "gas": 100000,
        "gasPrice": gas_price,
        "nonce": nonce,
    })
    signed = w3.eth.account.sign_transaction(tx, account.key)
    raw_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    receipt = w3.eth.wait_for_transaction_receipt(raw_hash, timeout=120)
    if receipt.get("status", 1) == 0:
        raise TxRevertedError("erc20 transfer reverted")
    return "0x" + raw_hash.hex()
