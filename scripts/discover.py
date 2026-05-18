#!/usr/bin/env python3
# skill/scripts/discover.py
"""List purchasable MCP-tool SKUs across all registered merchants.

Production connector is hardcoded — this skill has no test/dev mode.

Usage:
  python3 scripts/discover.py [--query <keyword>]
"""
from __future__ import annotations
import argparse
import json
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

CONNECTOR_URL = "https://connector.wcheckout.app"

_HTTP_TIMEOUT = 5.0
_RETRIES = 2                      # 1 initial + 2 retries = 3 attempts
_RETRY_BACKOFF = (0.3, 0.8)       # seconds per retry

_TRANSIENT_EXC: tuple[type[BaseException], ...] = (
    socket.timeout, TimeoutError, ConnectionError, urllib.error.URLError,
)


def _http_get(url: str, *, timeout: float = _HTTP_TIMEOUT) -> dict | list:
    """GET + JSON-decode with retry on transient transport errors and HTTP 5xx.
    Hard-fails on 4xx (e.g. 404 = wrong URL — retrying won't help)."""
    last_exc: BaseException | None = None
    for attempt in range(_RETRIES + 1):
        try:
            req = urllib.request.Request(url, headers={"Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as res:
                return json.loads(res.read())
        except urllib.error.HTTPError as e:
            if not (500 <= e.code < 600):
                raise
            last_exc = e
        except _TRANSIENT_EXC as e:
            last_exc = e
        if attempt < _RETRIES:
            time.sleep(_RETRY_BACKOFF[min(attempt, len(_RETRY_BACKOFF) - 1)])
    assert last_exc is not None
    raise last_exc


def _agent_skus(merchant: dict, *, timeout: float) -> list[dict]:
    """Pull a merchant's agent card; emit one row per MCP-tool product."""
    card_url = merchant["agent_url"].rstrip("/") + "/.well-known/agent.json"
    card = _http_get(card_url, timeout=timeout)

    catalog = {
        (p.get("delivery") or {}).get("tool_name"): p
        for p in card.get("catalog", {}).get("products", [])
        if (p.get("delivery") or {}).get("type") == "mcp_call_token"
        and (p.get("delivery") or {}).get("tool_name")
    }

    rows = []
    for s in card.get("skills", []):
        if not s.get("id", "").startswith("buy_"):
            continue
        md = s.get("metadata") or {}
        prod = catalog.get(md.get("tool_name"))
        if not prod:
            continue
        rows.append({
            "product_id": prod["id"],
            "merchant": card.get("name") or merchant.get("name", "?"),
            "tool_name": md["tool_name"],
            "price_usd_per_unit": md.get("price_usd_per_unit"),
            "calls_per_unit": md.get("calls_per_unit"),
            "agent_url": card.get("url") or merchant.get("agent_url"),
        })
    return rows


def _fmt_price(v: float | None) -> str:
    """Format a USD price with enough precision for sub-cent test products."""
    if v is None:
        return "?"
    if v == 0:
        return "$0"
    if v >= 0.01:
        return f"${v:.2f}"
    return "$" + f"{v:.8f}".rstrip("0").rstrip(".")


def _print_table(skus: list[dict]) -> None:
    if not skus:
        print("No purchasable MCP-tool SKUs discovered.")
        return
    header = (
        f"{'product_id':<10}  {'merchant':<14}  {'tool_name':<22}  "
        f"{'$/pack':<10}  {'pack':<6}  {'$/call':<12}  agent_url"
    )
    print(header)
    for s in skus:
        pu, cpu = s["price_usd_per_unit"], s["calls_per_unit"]
        per_call = _fmt_price(pu / cpu) if (pu and cpu) else "?"
        print(
            f"{str(s['product_id']):<10}  {s['merchant'][:14]:<14}  "
            f"{s['tool_name'][:22]:<22}  {_fmt_price(pu):<10}  "
            f"{f'x{cpu}' if cpu is not None else '?':<6}  "
            f"{per_call:<12}  {s['agent_url']}"
        )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--query", default="", help="merchant search keyword (empty = all)")
    ap.add_argument(
        "--http-timeout-sec", type=float, default=_HTTP_TIMEOUT,
        help=f"per-request timeout (default {_HTTP_TIMEOUT}s, up to {_RETRIES} retries on transient errors)",
    )
    args = ap.parse_args()

    search_url = (
        CONNECTOR_URL + "/merchants/search?"
        + urllib.parse.urlencode({"q": args.query})
    )
    try:
        merchants = _http_get(search_url, timeout=args.http_timeout_sec)
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        print(f"ERROR: connector responded {e.code}: {body}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"ERROR: connector request failed (after retries): {e}", file=sys.stderr)
        return 1

    skus: list[dict] = []
    if merchants:
        with ThreadPoolExecutor(max_workers=8) as ex:
            futures = {
                ex.submit(_agent_skus, m, timeout=args.http_timeout_sec): m
                for m in merchants
            }
            for fut, m in futures.items():
                try:
                    skus.extend(fut.result())
                except Exception as e:
                    name = m.get("name") or m.get("id") or "?"
                    print(f"  [WARN] {name}: {e}", file=sys.stderr)

    _print_table(skus)
    return 0


if __name__ == "__main__":
    sys.exit(main())
