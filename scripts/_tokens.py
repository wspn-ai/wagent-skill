# skill/scripts/_tokens.py
"""Token → ERC-20 contract address lookup. Ethereum mainnet only.

This skill is production-only — there is no testnet or environment switch.
Override a single contract via OKX_TOKEN_CONTRACT env if you need a non-
canonical deployment.
"""
from __future__ import annotations


class UnknownTokenError(ValueError):
    pass


_CONTRACTS = {
    "ETH_USDC": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
    "ETH_WUSD": "0x7Cd017ca5ddb86861FA983a34b5F495C6F898c41",
    "ETH_USDT": "0xdAC17F958D2ee523a2206206994597C13D831ec7",
}

VALID_TOKENS = frozenset(_CONTRACTS)


def resolve_contract(token: str) -> str:
    """Resolve a token symbol to its ERC-20 contract on Ethereum mainnet."""
    try:
        return _CONTRACTS[token]
    except KeyError:
        raise UnknownTokenError(
            f"unknown token {token!r}; expected one of {sorted(VALID_TOKENS)}"
        ) from None
