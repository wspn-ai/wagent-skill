#!/usr/bin/env python3
# skill/scripts/discover.py
"""List purchasable MCP tool SKUs across all registered merchants.

Usage:
  python3 scripts/discover.py [--query <keyword>] [--connector-url <url>]

Reads CONNECTOR_URL from env if --connector-url is omitted."""
from __future__ import annotations
import argparse
import json
import os
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor


def _load_dotenv_from_skill_root() -> None:
    """Load `.env` from the skill root into os.environ (shell env wins).
    Mirror of the loader in buy.py — see buy.py for full docstring."""
    import pathlib
    env_path = pathlib.Path(__file__).resolve().parent.parent / ".env"
    if not env_path.is_file():
        return
    try:
        for raw in env_path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
                value = value[1:-1]
            if key and key not in os.environ:
                os.environ[key] = value
    except OSError:
        pass


_load_dotenv_from_skill_root()


# Default per-request timeout. Tight on purpose: discover is read-only, so
# slow merchants shouldn't make the whole listing hang. Failures retry.
_DEFAULT_HTTP_TIMEOUT = 5.0
_DEFAULT_RETRIES = 2          # 1 initial + 2 retries = 3 total attempts
_RETRY_BACKOFF = (0.3, 0.8)   # backoff seconds: attempt 1 → 0.3s, attempt 2 → 0.8s

_TRANSIENT_TRANSPORT_EXC: tuple[type[BaseException], ...] = (
    socket.timeout, TimeoutError, ConnectionError, urllib.error.URLError,
)


def _http_get(
    url: str,
    timeout: float = _DEFAULT_HTTP_TIMEOUT,
    retries: int = _DEFAULT_RETRIES,
) -> dict | list:
    """Fetch JSON with timeout + retry-on-transient.

    Retries on socket timeout, connection refused, DNS, SSL handshake hang,
    and HTTP 5xx. Hard-fails on HTTP 4xx (e.g., 404 = wrong URL — retrying
    won't help)."""
    last_exc: BaseException | None = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={"Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as res:
                return json.loads(res.read())
        except urllib.error.HTTPError as e:
            if 500 <= e.code < 600:
                last_exc = e
            else:
                raise  # 4xx — don't retry
        except _TRANSIENT_TRANSPORT_EXC as e:
            last_exc = e
        if attempt < retries:
            time.sleep(_RETRY_BACKOFF[min(attempt, len(_RETRY_BACKOFF) - 1)])
    assert last_exc is not None
    raise last_exc


def _agent_skus(
    merchant: dict, *, timeout: float = _DEFAULT_HTTP_TIMEOUT,
) -> list[dict]:
    """Pull agent card; emit one row per MCP-tool product."""
    card_url = merchant["agent_url"].rstrip("/") + "/.well-known/agent.json"
    card = _http_get(card_url, timeout=timeout)

    catalog_by_tool: dict[str, dict] = {}
    for p in card.get("catalog", {}).get("products", []):
        delivery = p.get("delivery") or {}
        if delivery.get("type") != "mcp_call_token":
            continue
        tn = delivery.get("tool_name")
        if not tn:
            continue
        catalog_by_tool[tn] = p
    out = []
    for s in card.get("skills", []):
        if not s.get("id", "").startswith("buy_"):
            continue
        md = s.get("metadata", {}) or {}
        tn = md.get("tool_name")
        if not tn:
            continue
        prod = catalog_by_tool.get(tn)
        if not prod:
            continue
        out.append({
            "product_id": prod["id"],
            "merchant": card.get("name") or merchant.get("name", "?"),
            "tool_name": tn,
            "price_usd_per_unit": md.get("price_usd_per_unit"),
            "calls_per_unit": md.get("calls_per_unit"),
            "ttl_hours": md.get("ttl_hours"),
            "tool_endpoint": md.get("tool_endpoint"),
            "agent_url": card.get("url") or merchant.get("agent_url"),
        })
    return out


def _fmt_price(v: float | None) -> str:
    """Format a USD price with enough precision to show sub-cent values.

    Mainnet test products are priced at 0.00001-0.00006 USD; ':.2f' rounds
    these to '$0.00' and misleads the user into thinking they're free."""
    if v is None:
        return "?"
    if v == 0:
        return "$0"
    if v >= 0.01:
        return f"${v:.2f}"
    # Sub-cent: show up to 8 decimals, strip trailing zeros so output is tight.
    return "$" + f"{v:.8f}".rstrip("0").rstrip(".")


def _print_table(skus: list[dict]) -> None:
    if not skus:
        print("No purchasable MCP tool SKUs discovered.")
        return
    header = f"{'product_id':<10}  {'merchant':<14}  {'tool_name':<22}  {'$/pack':<10}  {'pack':<6}  {'$/call':<12}  agent_url"
    print(header)
    for s in skus:
        pu, cpu = s["price_usd_per_unit"], s["calls_per_unit"]
        per_call = _fmt_price(pu / cpu) if (pu and cpu) else "?"
        pack_str = f"x{cpu}" if cpu is not None else "?"
        print(
            f"{str(s['product_id']):<10}  {s['merchant'][:14]:<14}  {s['tool_name'][:22]:<22}  "
            f"{_fmt_price(pu):<10}  {pack_str:<6}  {per_call:<12}  {s['agent_url']}"
        )


DEMO_CONNECTOR_URL = "https://connector-dev.wcheckout.app"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--query", default="", help="merchant search keyword (empty = all)")
    ap.add_argument("--connector-url",
                    default=os.environ.get("CONNECTOR_URL") or DEMO_CONNECTOR_URL,
                    help=f"default: ${{CONNECTOR_URL}} env or {DEMO_CONNECTOR_URL}")
    ap.add_argument(
        "--http-timeout-sec", type=float, default=_DEFAULT_HTTP_TIMEOUT,
        help=f"per-request timeout (default {_DEFAULT_HTTP_TIMEOUT}s); each "
             f"request retries up to {_DEFAULT_RETRIES} times on transient "
             "transport errors and HTTP 5xx",
    )
    args = ap.parse_args()

    if not args.connector_url:  # only triggers if both env and default were ""
        print("ERROR: pass --connector-url or set CONNECTOR_URL env", file=sys.stderr)
        return 1

    search_url = args.connector_url.rstrip("/") + "/merchants/search?" + urllib.parse.urlencode({"q": args.query})
    try:
        merchants = _http_get(search_url, timeout=args.http_timeout_sec)
    except urllib.error.HTTPError as e:
        print(f"ERROR: gateway responded {e.code}: {e.read().decode(errors='replace')}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"ERROR: gateway request failed (after retries): {e}", file=sys.stderr)
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
                    print(f"  [WARN] {m.get('name', m.get('id', '?'))}: {e}", file=sys.stderr)

    _print_table(skus)
    return 0


if __name__ == "__main__":
    sys.exit(main())
