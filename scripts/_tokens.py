# skill/scripts/_tokens.py
"""Token + network → ERC-20 contract address lookup.

Only supports the EVM/Ethereum family. Sepolia and mainnet only. Adding
chains here also requires editing _wallet_local.py's RPC selection."""
from __future__ import annotations
import os


class UnknownTokenError(ValueError):
    pass


class UnknownNetworkError(ValueError):
    pass


_CONTRACTS = {
    ("ETH_USDC", "sepolia"): "0x1c7D4B196Cb0C7B01d743Fbc6116a902379C7238",
    ("ETH_USDC", "mainnet"): "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
    ("ETH_WUSD", "sepolia"): "0x371607f7463d27ae9deaf64ae00da9cbd4cf0065",
    ("ETH_WUSD", "mainnet"): "0x7Cd017ca5ddb86861FA983a34b5F495C6F898c41",
    ("ETH_USDT", "mainnet"): "0xdAC17F958D2ee523a2206206994597C13D831ec7",
    # ETH_USDT on sepolia: no canonical deployment — see resolve_contract.
}

_CHAIN_IDS = {"sepolia": 11155111, "mainnet": 1}

VALID_NETWORKS = frozenset(_CHAIN_IDS)
VALID_TOKENS = frozenset({"ETH_USDC", "ETH_WUSD", "ETH_USDT"})


def resolve_contract(token: str, network: str) -> str:
    """Resolve (token, network) to an ERC-20 contract address.

    Only special case: ETH_USDT on sepolia must come from
    ETH_USDT_CONTRACT_SEPOLIA env (no canonical deployment exists)."""
    if token not in VALID_TOKENS:
        raise UnknownTokenError(
            f"unknown token {token!r}; expected one of {sorted(VALID_TOKENS)}"
        )
    if network not in VALID_NETWORKS:
        raise UnknownNetworkError(
            f"unknown network {network!r}; expected one of {sorted(VALID_NETWORKS)}"
        )
    if token == "ETH_USDT" and network == "sepolia":
        env_addr = os.environ.get("ETH_USDT_CONTRACT_SEPOLIA")
        if not env_addr:
            raise UnknownTokenError(
                "ETH_USDT on sepolia has no canonical contract; "
                "set ETH_USDT_CONTRACT_SEPOLIA env to override"
            )
        return env_addr
    return _CONTRACTS[(token, network)]


def chain_id(network: str) -> int:
    if network not in _CHAIN_IDS:
        raise UnknownNetworkError(
            f"unknown network {network!r}; expected one of {sorted(_CHAIN_IDS)}"
        )
    return _CHAIN_IDS[network]
