#!/usr/bin/env python3
# skill/scripts/buy.py
"""End-to-end purchase + tool invocation orchestrator.

Pipeline:
  1. purchase_intent     → task with deposit_address + paying_amount + order_no
  2. pay_on_chain        → tx_hash via OKX Agentic Wallet (mainnet)
  3. poll_settlement     → task transitions to completed
  4. invoke_tool         → POST usage_endpoint with bearer token

Production-only: Ethereum mainnet via OKX. No testnet, no env switch.
"""
from __future__ import annotations
import argparse
import calendar
import hashlib
import json
import os
import pathlib
import re
import socket
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# Sibling-module imports work in two contexts:
#   1. Imported as a package (pytest, `python3 -m`) → relative imports.
#   2. Direct CLI (`python3 scripts/buy.py`) → no parent package, fall back.
try:
    from . import _tokens
    from .okx import _wallet_okx
    from .local import _wallet_local
except ImportError:
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
    import _tokens                              # type: ignore[no-redef]  # noqa: E402
    from okx import _wallet_okx                 # type: ignore[no-redef,import-not-found]  # noqa: E402
    from local import _wallet_local             # type: ignore[no-redef,import-not-found]  # noqa: E402


CONNECTOR_URL = "https://connector.wcheckout.app"
DEFAULT_TOKEN_PRIORITY = "ETH_WUSD,ETH_USDT,ETH_USDC"
DEFAULT_PAYMENT_BACKEND = "okx"

_TX_HASH_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")
_INFLIGHT_TTL_SEC = 30 * 60                    # 30 min — covers Stablelink expiredIn / 2


# ── Exceptions ──────────────────────────────────────────────────────────────

class OrderFailed(RuntimeError): pass
class UnexpectedState(RuntimeError): pass
class WrongAgentUrlError(RuntimeError): pass
class PaymentNotConfirmed(RuntimeError): pass
class SettlementTimeout(RuntimeError): pass
class ToolCallError(RuntimeError): pass
class ConfigError(RuntimeError): pass


# ── HTTP helpers ────────────────────────────────────────────────────────────

_TRANSIENT_EXC: tuple[type[BaseException], ...] = (
    socket.timeout, ssl.SSLError, ConnectionError, TimeoutError,
    urllib.error.URLError,                     # wraps DNS / connect-refused / handshake
)


def _post_json(url: str, payload: dict, timeout: float = 30.0) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as res:
        return json.loads(res.read())


def _get_json(url: str, timeout: float = 30.0) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as res:
        return json.loads(res.read())


def _post_json_polling(url: str, payload: dict, timeout: float = 30.0) -> dict | None:
    """Like _post_json but returns None (with stderr log) on transient errors
    so a polling caller can retry on the next iteration. Hard 4xx still raises."""
    try:
        return _post_json(url, payload, timeout=timeout)
    except urllib.error.HTTPError as e:
        if not (500 <= e.code < 600):
            raise
        print(f"   [transient] HTTP {e.code} — retrying", file=sys.stderr)
    except _TRANSIENT_EXC as e:
        print(f"   [transient] {type(e).__name__}: {e} — retrying", file=sys.stderr)
    return None


def _get_json_polling(url: str, timeout: float = 30.0) -> dict | None:
    try:
        return _get_json(url, timeout=timeout)
    except urllib.error.HTTPError as e:
        if not (500 <= e.code < 600):
            raise
        print(f"   [transient] HTTP {e.code} — retrying", file=sys.stderr)
    except _TRANSIENT_EXC as e:
        print(f"   [transient] {type(e).__name__}: {e} — retrying", file=sys.stderr)
    return None


# ── Local persistence — call tokens + in-flight purchase guard ──────────────

# tokens.json   : bearer + usage_endpoint kept POST-delivery for --use-token / --return-token
# inflight.json : {fingerprint → order_no + tx_hash} from purchase to delivery,
#                 prevents a panicked re-run after a network blip from paying twice.

def _state_dir() -> pathlib.Path:
    return pathlib.Path(
        os.environ.get("_WPAY_STATE_DIR")
        or (pathlib.Path.home() / ".wagent")
    )


def _tokens_db_path() -> pathlib.Path:
    return pathlib.Path(os.environ.get("_WPAY_TOKENS_DB") or _state_dir() / "tokens.json")


def _inflight_db_path() -> pathlib.Path:
    return pathlib.Path(os.environ.get("_WPAY_INFLIGHT_DB") or _state_dir() / "inflight.json")


def _read_db(p: pathlib.Path) -> dict:
    if not p.exists():
        return {}
    try:
        with p.open() as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        print(f"   [WARN] {p} is corrupted; ignoring", file=sys.stderr)
        return {}


def _write_db(p: pathlib.Path, db: dict) -> None:
    """Atomic write: tmp + rename. A mid-write crash can't corrupt the DB."""
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    with tmp.open("w") as f:
        json.dump(db, f, indent=2, sort_keys=True)
    tmp.replace(p)


def _save_token(order_no: str, info: dict) -> None:
    p = _tokens_db_path()
    db = _read_db(p)
    db[order_no] = info
    _write_db(p, db)


def _get_saved_token(order_no: str) -> dict | None:
    return _read_db(_tokens_db_path()).get(order_no)


def _delete_saved_token(order_no: str) -> None:
    p = _tokens_db_path()
    db = _read_db(p)
    if order_no in db:
        del db[order_no]
        _write_db(p, db)


def _save_inflight(fp: str, info: dict) -> None:
    p = _inflight_db_path()
    db = _read_db(p)
    db[fp] = info
    _write_db(p, db)


def _delete_inflight(fp: str) -> None:
    p = _inflight_db_path()
    db = _read_db(p)
    if fp in db:
        del db[fp]
        _write_db(p, db)


def _get_recent_inflight(fp: str, max_age_sec: int = _INFLIGHT_TTL_SEC) -> dict | None:
    entry = _read_db(_inflight_db_path()).get(fp)
    if not entry or not entry.get("started_at"):
        return None
    try:
        # started_at is UTC; timegm interprets struct_time as UTC (mktime would
        # treat it as local → off by TZ offset on non-UTC machines).
        started_epoch = calendar.timegm(
            time.strptime(entry["started_at"], "%Y-%m-%dT%H:%M:%SZ")
        )
    except (ValueError, TypeError):
        return None
    return entry if time.time() - started_epoch <= max_age_sec else None


def _purchase_fingerprint(
    *, agent_url: str, product_id: int, quantity: int, token: str,
) -> str:
    """Stable hash of the buyer's purchase intent. Two skill invocations with
    the same fingerprint are presumed to be retries of the same purchase."""
    key = f"{agent_url.rstrip('/')}|{product_id}|{quantity}|{token}|agent"
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def _now_utc() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ── Artifact extraction ─────────────────────────────────────────────────────

def extract_deposit(task: dict) -> tuple[str, float, str]:
    """Pull (deposit_address, paying_amount, order_no) from task artifacts."""
    for a in task.get("artifacts") or []:
        for p in a.get("parts") or []:
            d = p.get("data") or {}
            if {"deposit_address", "paying_amount", "order_no"} <= d.keys():
                return d["deposit_address"], float(d["paying_amount"]), d["order_no"]
    raise KeyError("no deposit_address / paying_amount / order_no in task artifacts")


def extract_delivery(task: dict) -> tuple[str, str]:
    """Pull (bearer_token, usage_endpoint) from a completed-state task.

    Canonical shape (parts[*].data.{usage_endpoint, payload.token}). Two
    legacy fallback shapes are accepted for robustness against future shop
    changes. Raises KeyError when no artifact carries delivery info — payment
    happened but the shop never emitted the token (typically: shop restart
    lost the in-memory task store between purchase and check_payment)."""
    for a in task.get("artifacts") or []:
        # Canonical: parts[*].data.{usage_endpoint, payload.token}
        for p in a.get("parts") or []:
            d = p.get("data") or {}
            ue = d.get("usage_endpoint")
            tok = (d.get("payload") or {}).get("token")
            if ue and tok:
                return tok, ue
        # Legacy artifact-level shape
        if a.get("usage_endpoint") and (a.get("payload") or {}).get("token"):
            return a["payload"]["token"], a["usage_endpoint"]
        # Defensive flat / nested-delivery shape
        for p in a.get("parts") or []:
            d = p.get("data") or {}
            for blob in (d.get("delivery") or {}, d):
                if blob.get("token") and blob.get("usage_endpoint"):
                    return blob["token"], blob["usage_endpoint"]
    raise KeyError(
        "no delivery payload in completed task artifacts. Payment went "
        "through (Tx hash above) but the shop did not emit a delivery "
        "artifact. Recover by querying "
        f"{CONNECTOR_URL}/orders/<order_no> or contact merchant ops."
    )


def _extract_delivery_for_save(task: dict, order_no: str) -> dict | None:
    """Pull a savable record of a fresh delivery — for --use-token recovery."""
    for a in task.get("artifacts") or []:
        for p in a.get("parts") or []:
            d = p.get("data") or {}
            ue = d.get("usage_endpoint")
            payload = d.get("payload") or {}
            tok = payload.get("token")
            if ue and tok:
                return {
                    "order_no": order_no,
                    "agent_url": ue.split("/v1/tools/", 1)[0] if "/v1/tools/" in ue else "",
                    "bearer_token": tok,
                    "usage_endpoint": ue,
                    "tool_name": payload.get("tool_name", ""),
                    "calls_remaining": payload.get("calls_remaining"),
                    "calls_total": payload.get("calls_total"),
                    "expires_at": payload.get("expires_at"),
                    "saved_at": _now_utc(),
                }
    return None


def _failure_reason(task: dict) -> str:
    """Human-readable error from task artifacts + messages + connector detail.

    Shop-node A2A handlers return failures as ``{state: 'failed', message,
    artifacts: []}``; the router stores that ``message`` into
    ``task.messages`` (not artifacts). Scanning only artifacts therefore
    surfaces 'unknown' for the most common failure modes (handler crash,
    gateway 502, validation error). Read ``task.messages`` first, then fall
    back to artifacts and the connector's order detail."""
    reason = ""
    text_reason = ""
    order_no = ""

    # Agent-role messages from newest to oldest; the most recent text part is
    # the failure cause (handlers push exactly one on transition to 'failed').
    for m in reversed(task.get("messages") or []):
        if m.get("role") != "agent":
            continue
        for p in m.get("parts") or []:
            d = p.get("data") or {}
            for key in ("error", "message", "reason"):
                if not reason and d.get(key):
                    reason = d[key]
            if not text_reason and p.get("type") == "text" and p.get("text"):
                text_reason = p["text"]
        if reason or text_reason:
            break

    for a in task.get("artifacts") or []:
        for p in a.get("parts") or []:
            d = p.get("data") or {}
            order_no = order_no or d.get("order_no", "")
            for key in ("error", "message", "reason"):
                if not reason and d.get(key):
                    reason = d[key]
            if not text_reason and p.get("type") == "text" and p.get("text"):
                text_reason = p["text"]
    reason = reason or text_reason

    if order_no:
        try:
            detail = _get_json(f"{CONNECTOR_URL}/orders/{order_no}", timeout=10)
            extras = [
                f"{k}: {detail[k]}"
                for k in ("error", "error_message", "status_message",
                          "last_error", "wcheckout_response")
                if detail.get(k)
            ]
            if extras:
                reason = (reason + "\n" + "\n".join(extras)).strip()
        except Exception:
            pass
    return reason or "unknown"


def _is_actionable(task: dict) -> bool:
    """A task is actionable once artifacts carry deposit info — even if state
    is still working / submitted."""
    try:
        extract_deposit(task)
        return True
    except KeyError:
        return False


# ── Purchase intent + polling ───────────────────────────────────────────────

def _wait_for_initial_terminal(
    *, agent_url: str, task_id: str,
    timeout_sec: int = 30, interval_sec: int = 2,
) -> dict:
    """Poll GET /tasks/<id> while the task is non-terminal and not yet
    actionable. Exits early on completed/failed OR on artifacts going
    actionable. Never re-sends purchase intent (Hard rule 1)."""
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        resp = _get_json_polling(agent_url.rstrip("/") + f"/tasks/{task_id}")
        if resp is None:
            time.sleep(interval_sec)
            continue
        task = resp.get("result") or resp
        if not isinstance(task, dict):
            time.sleep(interval_sec)
            continue
        if task.get("state") in ("completed", "failed") or _is_actionable(task):
            return task
        time.sleep(interval_sec)
    raise UnexpectedState(
        f"task {task_id} stuck in non-terminal state for {timeout_sec}s"
    )


def purchase_intent(
    *, agent_url: str, product_id: int, quantity: int, token: str,
    initial_state_timeout_sec: int = 30,
) -> dict:
    """Send purchase intent, return an actionable or terminal task.

    Tolerates three merchant patterns:
      A. Sync: state=completed immediately.
      B. Async transition: state=working/submitted → poll until terminal.
      C. Soft-working: state stays working but artifacts immediately carry
         deposit info. Trust artifacts as authoritative."""
    payload = {"message": {"role": "user", "parts": [{
        "type": "data",
        "data": {
            "intent": "purchase",
            "product_id": product_id,
            "quantity": quantity,
            "token": token,
            "customer_id": "agent",
        },
    }]}}
    target = agent_url.rstrip("/") + "/tasks/send"
    try:
        response = _post_json(target, payload)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            host = urllib.parse.urlsplit(target).hostname or target
            raise WrongAgentUrlError(
                f"POST {target} returned 404 Not Found.\n"
                f"   '{host}' does not expose /tasks/send. Most likely you\n"
                f"   passed the connector URL instead of a merchant agent\n"
                f"   URL. Run `python3 scripts/discover.py` and use the\n"
                f"   agent_url column for the SKU you want."
            ) from e
        raise

    task = response["result"]
    state = task.get("state")

    if state == "completed":
        return task
    if state == "failed":
        raise OrderFailed(_failure_reason(task))
    if state in ("working", "submitted") and _is_actionable(task):
        return task
    if state in ("working", "submitted"):
        task = _wait_for_initial_terminal(
            agent_url=agent_url, task_id=task["id"],
            timeout_sec=initial_state_timeout_sec,
        )
        state = task.get("state")
        if state == "completed" or _is_actionable(task):
            return task
        if state == "failed":
            raise OrderFailed(_failure_reason(task))

    raise UnexpectedState(
        f"unexpected initial state: {state}. Task: {json.dumps(task)[:500]}"
    )


# ── On-chain payment ────────────────────────────────────────────────────────

def _maybe_test_pay() -> str | None:
    """Honor BUY_TEST_FAKE_TX env so integration tests can bypass real signing."""
    fake = os.environ.get("BUY_TEST_FAKE_TX")
    return fake if fake and _TX_HASH_RE.match(fake) else None


def pay_on_chain(*, token: str, recipient: str, amount: float) -> str:
    """Dispatch ERC-20 transfer to the configured backend; return tx hash.

    Default backend is `okx` (OKX Agentic Wallet, recommended for production).
    Set `PAYMENT_BACKEND=local` to sign with a self-managed private key —
    useful as a small-balance test wallet ($1–$5) on Ethereum mainnet.
    Both backends transact on mainnet; the connector is production-only.
    """
    backend = os.environ.get("PAYMENT_BACKEND") or DEFAULT_PAYMENT_BACKEND
    if backend not in ("okx", "local"):
        raise ConfigError(
            f"PAYMENT_BACKEND must be 'okx' or 'local' (got {backend!r})"
        )

    contract = _tokens.resolve_contract(token)
    mod = _wallet_okx if backend == "okx" else _wallet_local
    tx_hash = mod.send_erc20(contract=contract, recipient=recipient, amount=amount)

    if not _TX_HASH_RE.match(tx_hash):
        raise PaymentNotConfirmed(
            f"payment did NOT happen: backend returned {tx_hash!r}, "
            "not a 0x+64-hex tx hash"
        )
    return tx_hash


# ── Settlement polling ──────────────────────────────────────────────────────

def _connector_shows_paid(order_no: str) -> bool:
    """Fast (3s) probe of the connector DB. Never raises."""
    try:
        detail = _get_json(f"{CONNECTOR_URL}/orders/{order_no}", timeout=3)
    except Exception:
        return False
    status = detail.get("status") or detail.get("state") or ""
    return str(status).upper() == "PAID"


def _connector_order_summary(order_no: str) -> str:
    """Connector's view of an order, for enriching a timeout message. Never raises."""
    try:
        detail = _get_json(f"{CONNECTOR_URL}/orders/{order_no}", timeout=10)
    except Exception as e:
        return f"   connector lookup failed: {e}"
    body = f"   connector status: {detail.get('status') or detail.get('state') or 'unknown'}"
    extras = [
        f"   {k}: {detail[k]}"
        for k in ("error", "error_message", "status_message",
                  "last_error", "wcheckout_response", "tx_hash")
        if detail.get(k)
    ]
    return body + ("\n" + "\n".join(extras) if extras else "")


def _settlement_hint(order_no: str) -> str:
    return (
        "\n   The tx is on chain — the transfer succeeded. The shop never\n"
        "   observed it. Three usual causes (in order of frequency):\n"
        "   1. shop's MERCHANT_AGENT_URL is localhost / private — webhook\n"
        "      can't reach it. Set a publicly reachable URL.\n"
        "   2. WCHECKOUT_SANDBOX=true on the shop while paying mainnet.\n"
        "   3. CONNECTOR_API_KEY mismatch — shop /notify silently 401s.\n"
        f"   Recovery: {CONNECTOR_URL}/orders/{order_no}"
    )


def poll_settlement(
    *, agent_url: str, task_id: str, order_no: str,
    timeout_sec: int = 180, interval_sec: int = 5,
) -> dict:
    """Poll check_payment until the task transitions to completed.

    Default 180s (3 min). If Stablelink + webhook + shop can't confirm in
    3 min, something is misconfigured (see _settlement_hint)."""
    deadline = time.monotonic() + timeout_sec
    connector_paid_announced = False
    while time.monotonic() < deadline:
        payload = {"id": task_id, "message": {"role": "user", "parts": [{
            "type": "data",
            "data": {"intent": "check_payment", "order_no": order_no},
        }]}}
        resp = _post_json_polling(agent_url.rstrip("/") + "/tasks/send", payload)

        # If shop is flaky, fall back to connector DB — that's where settlement
        # actually lives. Users panic-retry under network jitter and double-pay
        # if we just abort here.
        if resp is None:
            if not connector_paid_announced and _connector_shows_paid(order_no):
                print(
                    f"   ✓ Connector confirms order {order_no} is PAID. "
                    "Waiting for shop to emit delivery artifact...",
                    file=sys.stderr,
                )
                connector_paid_announced = True
            time.sleep(interval_sec)
            continue

        task = resp["result"]
        state = task.get("state")
        if state == "completed":
            return task
        if state == "failed":
            raise OrderFailed(_failure_reason(task))

        if not connector_paid_announced and _connector_shows_paid(order_no):
            print(
                f"   ✓ Connector confirms order {order_no} is PAID. "
                "Shop lagging; will keep polling for delivery...",
                file=sys.stderr,
            )
            connector_paid_announced = True
        time.sleep(interval_sec)

    raise SettlementTimeout(
        f"settlement not confirmed within {timeout_sec}s.\n"
        + _connector_order_summary(order_no) + "\n"
        + _settlement_hint(order_no)
    )


# ── Tool invocation ─────────────────────────────────────────────────────────

def invoke_tool(*, usage_endpoint: str, bearer_token: str, body: dict) -> dict:
    """POST one bearer-token call to the merchant's tool endpoint."""
    req = urllib.request.Request(
        usage_endpoint, data=json.dumps(body).encode(),
        headers={
            "Authorization": f"Bearer {bearer_token}",
            "Content-Type": "application/json",
        }, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as res:
            return json.loads(res.read())
    except urllib.error.HTTPError as e:
        raise ToolCallError(
            f"tool call returned HTTP {e.code}: {e.read().decode(errors='replace')}"
        ) from e


# ── Input-schema prevalidation (prevent pay-then-fail) ──────────────────────

def _find_input_schema(agent_url: str, product_id: int) -> dict | None:
    """product_id → tool_name → skill.metadata.input_schema. None when card
    is unreachable OR schema absent (older shops); caller defers to server."""
    try:
        with urllib.request.urlopen(
            agent_url.rstrip("/") + "/.well-known/agent.json", timeout=5,
        ) as r:
            card = json.load(r)
    except Exception:
        return None

    tool_name = None
    for p in card.get("catalog", {}).get("products") or []:
        if p.get("id") == product_id:
            tool_name = (p.get("delivery") or {}).get("tool_name")
            break
    if not tool_name:
        return None

    for s in card.get("skills") or []:
        md = s.get("metadata") or {}
        if md.get("tool_name") == tool_name:
            return md.get("input_schema") or None
    return None


def _validate_against_schema(body, schema: dict, path: str = "$") -> str | None:
    """Minimal JSON Schema validator — supports the subset shop-node uses
    (type, properties, required, pattern, additionalProperties, enum, items)."""
    if not isinstance(schema, dict):
        return None
    t = schema.get("type")
    if t == "object":
        if not isinstance(body, dict):
            return f"{path}: expected object, got {type(body).__name__}"
        props = schema.get("properties") or {}
        for req in schema.get("required") or []:
            if req not in body:
                return f"missing required field '{req}'. Required: {sorted(schema.get('required') or [])}"
        if schema.get("additionalProperties") is False:
            extras = [k for k in body if k not in props]
            if extras:
                return f"unknown field(s) {extras}. Schema allows: {sorted(props.keys())}"
        for k, v in body.items():
            if k in props:
                sub = _validate_against_schema(v, props[k], path=f"{path}.{k}")
                if sub:
                    return sub
        return None
    if t == "string":
        if not isinstance(body, str):
            return f"{path}: expected string, got {type(body).__name__}"
        pat = schema.get("pattern")
        if pat:
            try:
                if not re.search(pat, body):
                    desc = schema.get("description") or ""
                    msg = f"{path}: value {body!r} does not match pattern {pat!r}"
                    return msg + (f". Field expects: {desc}" if desc else "")
            except re.error:
                return None
        enum = schema.get("enum")
        if enum and body not in enum:
            return f"{path}: value {body!r} not in allowed {enum}"
        return None
    if t == "integer":
        if not isinstance(body, int) or isinstance(body, bool):
            return f"{path}: expected integer, got {type(body).__name__}"
        return None
    if t == "number":
        if not isinstance(body, (int, float)) or isinstance(body, bool):
            return f"{path}: expected number, got {type(body).__name__}"
        return None
    if t == "boolean":
        if not isinstance(body, bool):
            return f"{path}: expected boolean, got {type(body).__name__}"
        return None
    if t == "array":
        if not isinstance(body, list):
            return f"{path}: expected array, got {type(body).__name__}"
        item_schema = schema.get("items")
        if item_schema:
            for i, v in enumerate(body):
                sub = _validate_against_schema(v, item_schema, path=f"{path}[{i}]")
                if sub:
                    return sub
    return None


def _prevalidate_call_body(agent_url: str, product_id: int, body: dict) -> str | None:
    """Client-side schema check BEFORE any payment. Prevents the
    pay-then-fail-then-panic-retry double-payment pattern."""
    schema = _find_input_schema(agent_url, product_id)
    return _validate_against_schema(body, schema) if schema else None


# ── Buy pipeline ────────────────────────────────────────────────────────────

# Pre-payment errors that are safe to recover from by trying the next token —
# no on-chain broadcast happened. Same shape across both backends so callers
# don't need to know which one is active.
_FALLBACK_TRIGGERS: tuple[type[BaseException], ...] = (
    _wallet_okx.InsufficientBalanceError,
    _wallet_okx.HighRiskError,
    _wallet_local.InsufficientBalanceError,
)


def _try_one_token(token: str, args, call_body: dict) -> int | None:
    """Full purchase pipeline for one token.

    Returns 0 on success, 1 on terminal failure, None if a recoverable
    pre-payment error fired (caller tries the next token in priority)."""
    # Stage A: purchase intent
    try:
        task = purchase_intent(
            agent_url=args.agent_url, product_id=args.product_id,
            quantity=args.quantity, token=token,
        )
    except OrderFailed as e:
        print(f"   {token}: merchant rejected — {e}", file=sys.stderr)
        return None

    deposit, amount, order_no = extract_deposit(task)

    inflight_fp = _purchase_fingerprint(
        agent_url=args.agent_url, product_id=args.product_id,
        quantity=args.quantity, token=token,
    )
    try:
        _save_inflight(inflight_fp, {
            "order_no": order_no, "task_id": task.get("id"),
            "deposit_address": deposit, "paying_amount": amount,
            "token": token, "agent_url": args.agent_url,
            "tx_hash": None, "started_at": _now_utc(),
        })
    except Exception as e:                                                  # noqa: BLE001
        print(f"   [WARN] could not persist inflight record: {e}", file=sys.stderr)

    # Stage B: on-chain broadcast
    fake_tx = _maybe_test_pay()
    if fake_tx:
        tx_hash = fake_tx
    else:
        try:
            tx_hash = pay_on_chain(token=token, recipient=deposit, amount=amount)
        except (_wallet_okx.InsufficientBalanceError,
                _wallet_local.InsufficientBalanceError) as e:
            print(f"   {token}: wallet underfunded — {e}", file=sys.stderr)
            return None
        except _wallet_okx.HighRiskError as e:
            print(f"   {token}: OKX flagged high-risk — {e}", file=sys.stderr)
            return None

    try:
        _save_inflight(inflight_fp, {
            "order_no": order_no, "task_id": task.get("id"),
            "deposit_address": deposit, "paying_amount": amount,
            "token": token, "agent_url": args.agent_url,
            "tx_hash": tx_hash, "started_at": _now_utc(),
        })
    except Exception:                                                       # noqa: BLE001
        pass

    print(f"Tx: {tx_hash}")
    sys.stdout.flush()

    # Stage C: settle + deliver + invoke (past the point of no return)
    task = poll_settlement(
        agent_url=args.agent_url, task_id=task["id"], order_no=order_no,
        timeout_sec=args.settle_timeout_sec, interval_sec=args.poll_interval_sec,
    )

    # Persist delivery BEFORE invoking — if the tool call below errors, the
    # user can recover with --use-token instead of paying again.
    try:
        saved = _extract_delivery_for_save(task, order_no)
        if saved:
            _save_token(order_no, saved)
            print(
                f"   Token saved locally — recover with: "
                f"buy.py --use-token {order_no} --call-body '...'",
                file=sys.stderr,
            )
    except Exception as e:                                                  # noqa: BLE001
        print(f"   [WARN] could not persist token locally: {e}", file=sys.stderr)

    try:
        bearer, usage_endpoint = extract_delivery(task)
    except KeyError as e:
        print("\n⚠️  Payment succeeded but tool not delivered.", file=sys.stderr)
        print(f"   Tx:        {tx_hash}", file=sys.stderr)
        print(f"   Order:     {order_no}", file=sys.stderr)
        print(f"   Token:     {token}", file=sys.stderr)
        print(f"   Amount:    {amount}", file=sys.stderr)
        print(f"   Recovery:  curl {CONNECTOR_URL}/orders/{order_no}", file=sys.stderr)
        print(f"   Reason:    {e}", file=sys.stderr)
        print("\n   Full settlement task JSON (for shop ops):", file=sys.stderr)
        print(json.dumps(task, indent=2), file=sys.stderr)
        return 1

    try:
        result = invoke_tool(
            usage_endpoint=usage_endpoint, bearer_token=bearer, body=call_body,
        )
    except ToolCallError as e:
        # Shop only decrements the token on successful invocation. The bearer
        # is still good. Print enough that the user can retry by hand WITHOUT
        # paying again.
        print(f"\n⚠️  Tool call failed AFTER successful payment.", file=sys.stderr)
        print(f"   Error:   {e}", file=sys.stderr)
        print(f"   Tx:      {tx_hash}", file=sys.stderr)
        print(f"   Order:   {order_no}", file=sys.stderr)
        print(
            f"\n   YOUR CALL TOKEN IS STILL VALID — do NOT re-run buy.py.\n"
            f"   Fix --call-body and retry with --use-token:",
            file=sys.stderr,
        )
        print(
            f"\n   python3 scripts/buy.py --use-token {order_no} "
            f"--call-body '<corrected JSON>'\n",
            file=sys.stderr,
        )
        raise

    try:
        _delete_inflight(inflight_fp)
    except Exception:                                                       # noqa: BLE001
        pass

    print(json.dumps(result, indent=2))
    return 0


# ── Recovery flows: --use-token / --return-token ────────────────────────────

def _run_use_token(order_no: str, call_body: dict) -> int:
    """Invoke a previously bought token without paying again. Successful call
    decrements the saved calls_remaining so future runs honor the budget."""
    saved = _get_saved_token(order_no)
    if not saved:
        print(
            f"ERROR: no saved token for order {order_no}.\n"
            f"   Local DB: {_tokens_db_path()}\n"
            f"   Possible causes:\n"
            f"   - the order was bought in a different HOME / agent harness\n"
            f"   - the token was already returned via --return-token\n"
            f"   - the order_no is a typo",
            file=sys.stderr,
        )
        return 1
    remaining = saved.get("calls_remaining")
    if isinstance(remaining, int) and remaining <= 0:
        print(
            f"ERROR: saved token for {order_no} has 0 calls remaining.\n"
            f"   If this is stale, delete the entry from {_tokens_db_path()} "
            f"and re-purchase.",
            file=sys.stderr,
        )
        return 1
    try:
        result = invoke_tool(
            usage_endpoint=saved["usage_endpoint"],
            bearer_token=saved["bearer_token"], body=call_body,
        )
    except ToolCallError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        print(
            f"   Token still valid (call NOT decremented). Fix --call-body and retry.\n"
            f"   To void this token and request refund: "
            f"buy.py --return-token {order_no}",
            file=sys.stderr,
        )
        return 1
    if isinstance(result, dict) and "calls_remaining" in result:
        saved["calls_remaining"] = result["calls_remaining"]
        _save_token(order_no, saved)
    print(json.dumps(result, indent=2))
    return 0


def _run_return_token(order_no: str) -> int:
    """Tell the shop the buyer is done with this token. Shop voids + reports
    failure to the connector → admin sees the order as refund-eligible."""
    saved = _get_saved_token(order_no)
    if not saved:
        print(f"ERROR: no saved token for order {order_no}", file=sys.stderr)
        return 1
    bearer = saved.get("bearer_token")
    agent_url = saved.get("agent_url")
    if not bearer or not agent_url:
        print(
            f"ERROR: saved record for {order_no} missing bearer / agent_url.\n"
            f"   {_tokens_db_path()}: {saved}",
            file=sys.stderr,
        )
        return 1
    req = urllib.request.Request(
        agent_url.rstrip("/") + "/v1/call-tokens/return",
        data=json.dumps({"reason": "buyer voluntary return via --return-token"}).encode(),
        headers={
            "Authorization": f"Bearer {bearer}",
            "Content-Type": "application/json",
        }, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as res:
            body = json.loads(res.read())
    except urllib.error.HTTPError as e:
        try:
            err = json.loads(e.read())
        except Exception:                                                   # noqa: BLE001
            err = {"reason": f"HTTP {e.code}"}
        print(
            f"ERROR: shop refused return: {err}\n"
            f"   Common cases:\n"
            f"   - 'partially_used': you already made a tool call with this token.\n"
            f"   - 'expired': the call-token TTL has passed.\n"
            f"   - 'already_returned': you already returned this token.",
            file=sys.stderr,
        )
        return 1
    if not body.get("ok"):
        print(f"ERROR: shop refused return: {body}", file=sys.stderr)
        return 1
    print(json.dumps(body, indent=2))
    _delete_saved_token(order_no)
    print(
        f"   Order {body.get('order_no')} marked refund-eligible on connector.\n"
        f"   Admin can complete the refund from the Orders dashboard.",
        file=sys.stderr,
    )
    return 0


# ── CLI ─────────────────────────────────────────────────────────────────────

def _parse_token_priority(token_arg: str) -> list[str]:
    """Parse comma-separated --token list. Validates + dedupes preserving order."""
    tokens = [t.strip() for t in token_arg.split(",") if t.strip()]
    if not tokens:
        raise ValueError("--token must list at least one token")
    invalid = [t for t in tokens if t not in _tokens.VALID_TOKENS]
    if invalid:
        raise ValueError(
            f"unknown token(s) {invalid}; valid: {sorted(_tokens.VALID_TOKENS)}"
        )
    seen, out = set(), []
    for t in tokens:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


def _check_inflight_guard(args, tokens: list[str]) -> int | None:
    """Refuse to create a duplicate purchase if an identical intent is
    inflight (and <30 min old). Returns 1 to abort, or None to proceed."""
    if args.force_new:
        return None
    for token in tokens:
        fp = _purchase_fingerprint(
            agent_url=args.agent_url, product_id=args.product_id,
            quantity=args.quantity, token=token,
        )
        inflight = _get_recent_inflight(fp)
        if not inflight:
            continue
        tx = inflight.get("tx_hash")
        print(
            "\n⚠️  In-flight purchase detected — refusing to create a duplicate.\n",
            file=sys.stderr,
        )
        for k in ("order_no", "token", "paying_amount", "deposit_address", "started_at"):
            print(f"   {k}:    {inflight.get(k)}", file=sys.stderr)
        if tx:
            print(f"   tx_hash:    {tx}", file=sys.stderr)
            print(
                f"\n   Your previous transaction IS on-chain. Wait for settlement.\n"
                f"   Check: curl {CONNECTOR_URL}/orders/{inflight.get('order_no')}",
                file=sys.stderr,
            )
        else:
            print("   tx_hash:    not yet broadcast", file=sys.stderr)
            print(
                f"\n   Previous run may have crashed pre-broadcast.\n"
                f"   - If NO tx was sent: pass --force-new to retry.\n"
                f"   - If a tx WAS sent: wait for it to settle, do NOT use --force-new.",
                file=sys.stderr,
            )
        print(
            f"\n   Inflight entry expires after 30 min. Override now with --force-new.",
            file=sys.stderr,
        )
        return 1
    return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Buy a tool pack and invoke one tool call. "
                    "Production: OKX Agentic Wallet on Ethereum mainnet.",
    )
    ap.add_argument("--agent-url",
                    help="(buy mode) merchant agent URL from discover.py")
    ap.add_argument("--product-id", type=int,
                    help="(buy mode) product id from agent card / discover.py")
    ap.add_argument("--quantity", type=int,
                    help="(buy mode) how many delivery units to buy")
    ap.add_argument(
        "--token", default=DEFAULT_TOKEN_PRIORITY,
        help=(
            "(buy mode) single token (e.g. ETH_USDC) OR comma-separated priority "
            f"list. Default: {DEFAULT_TOKEN_PRIORITY}. On wallet-underfunded / "
            "merchant-rejection / OKX-policy-block, falls back to next token."
        ),
    )
    ap.add_argument("--call-body",
                    help="JSON object passed verbatim as the tool call body "
                         "(required in buy and --use-token modes)")
    ap.add_argument("--use-token", metavar="ORDER_NO",
                    help="Invoke a saved call_token from a previous purchase "
                         "without paying again. Reads ~/.wagent/tokens.json.")
    ap.add_argument("--return-token", metavar="ORDER_NO",
                    help="Return an unused call_token to flag the order as "
                         "refund-eligible. Shop voids the token.")
    ap.add_argument("--force-new", action="store_true",
                    help="Override the in-flight guard. Risks duplicate "
                         "payment — use only when SURE the previous run never "
                         "broadcast a tx.")
    ap.add_argument("--settle-timeout-sec", type=int, default=180,
                    help="default 180s; fail fast on stuck settlement")
    ap.add_argument("--poll-interval-sec", type=int, default=5)
    args = ap.parse_args(argv)

    # ── Mode dispatch ───────────────────────────────────────────────────
    if args.use_token and args.return_token:
        print("ERROR: --use-token and --return-token are mutually exclusive",
              file=sys.stderr)
        return 1

    if args.return_token:
        return _run_return_token(args.return_token)

    if args.use_token:
        if not args.call_body:
            print("ERROR: --call-body is required with --use-token", file=sys.stderr)
            return 1
        try:
            call_body = json.loads(args.call_body)
        except json.JSONDecodeError as e:
            print(f"ERROR: --call-body is not valid JSON: {e}", file=sys.stderr)
            return 1
        return _run_use_token(args.use_token, call_body)

    # ── Buy mode ────────────────────────────────────────────────────────
    missing = [name for name, val in [
        ("--agent-url", args.agent_url),
        ("--product-id", args.product_id),
        ("--quantity", args.quantity),
        ("--call-body", args.call_body),
    ] if val is None]
    if missing:
        print(
            f"ERROR: buy mode requires {missing}.\n"
            f"   Or use --use-token / --return-token for recovery flows.",
            file=sys.stderr,
        )
        return 1

    try:
        call_body = json.loads(args.call_body)
    except json.JSONDecodeError as e:
        print(f"ERROR: --call-body is not valid JSON: {e}", file=sys.stderr)
        return 1

    try:
        tokens = _parse_token_priority(args.token)
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    # Prevalidate against tool's input_schema BEFORE any on-chain payment.
    schema_err = _prevalidate_call_body(args.agent_url, args.product_id, call_body)
    if schema_err:
        print(f"ERROR: {schema_err}", file=sys.stderr)
        print("   No payment was made. Fix --call-body and retry.", file=sys.stderr)
        return 1

    blocked = _check_inflight_guard(args, tokens)
    if blocked is not None:
        return blocked

    try:
        for i, token in enumerate(tokens):
            if i == 0 and len(tokens) > 1:
                print(f"→ trying tokens in priority order: {' → '.join(tokens)}",
                      file=sys.stderr)
            elif i > 0:
                print(f"\n→ retrying with next token: {token} ({i + 1}/{len(tokens)})",
                      file=sys.stderr)
            result = _try_one_token(token, args, call_body)
            if result is not None:
                return result
        print(
            f"\n⚠️  All tokens failed at pre-payment stage. Tried {len(tokens)}: {tokens}\n"
            f"   No money was sent on-chain. Likely causes: wallet underfunded "
            f"across all tokens, OR the merchant doesn't accept any of them.",
            file=sys.stderr,
        )
        return 1
    except WrongAgentUrlError as e:
        print(f"\n⚠️  Wrong --agent-url. {e}", file=sys.stderr)
        return 1
    except (OrderFailed, SettlementTimeout, PaymentNotConfirmed,
            UnexpectedState, ToolCallError, ConfigError, KeyError,
            _wallet_okx.CliError,
            _wallet_local.ConfigError, _wallet_local.TxRevertedError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
