#!/usr/bin/env python3
# skill/scripts/buy.py
"""End-to-end purchase + tool invocation orchestrator.

Pipeline:
  1. purchase_intent      → task with state completed/failed
                            (input-required branch retired — spend control
                             now lives in the buyer's wallet policy)
  2. extract_deposit      → (deposit_address, paying_amount, order_no)
  3. wallet.send_erc20    → tx_hash (dispatched on PAYMENT_BACKEND)
  4. poll_settlement      → task transitions to completed
  5. invoke_tool          → POST usage_endpoint with bearer token
"""
from __future__ import annotations
import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


def _load_dotenv_from_skill_root() -> None:
    """Load `.env` from the skill root (one level above scripts/) into
    os.environ — but never overwrite an already-set env var.

    Order of precedence (highest first):
      1. shell env (the operator already set it)
      2. `.env` file in the skill root (committed in `.env.example`,
         user copies to `.env` and fills in)
      3. baked-in defaults inside the scripts

    No third-party `python-dotenv` dep — stdlib only, ~20 lines of parser.
    Supports `KEY=value`, `KEY="value with spaces"`, comments after `#`,
    and blank lines. Does NOT support multi-line values or interpolation
    (not needed for our config).
    """
    import pathlib
    skill_root = pathlib.Path(__file__).resolve().parent.parent
    env_path = skill_root / ".env"
    if not env_path.is_file():
        return
    try:
        for raw in env_path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            # Strip a single pair of surrounding quotes (".." or '..')
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
                value = value[1:-1]
            # Don't overwrite — shell env always wins
            if key and key not in os.environ:
                os.environ[key] = value
    except OSError:
        # `.env` exists but is unreadable; not worth crashing on it.
        pass


_load_dotenv_from_skill_root()

# Sibling-module imports work in two contexts:
#   1. Imported as a package (pytest, `python3 -m skill.scripts.buy`) →
#      relative `from . … import` resolves cleanly.
#   2. Direct CLI (`python3 skill/scripts/buy.py`, or any harness that
#      invokes the script directly with an arbitrary parent layout) → the
#      relative import fails because there's no parent package, so add the
#      script's own dir to sys.path and fall back to absolute imports.
#
# Backends live in two independent subdirs (`okx/` for mainnet,
# `local/` for testnet-friendly self-managed wallet). Each backend's code
# is self-contained — no cross-imports between them.
try:
    from . import _tokens
    from .okx import _wallet_okx
    from .local import _wallet_local
except ImportError:
    import pathlib
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
    import _tokens  # type: ignore[no-redef]  # noqa: E402
    from okx import _wallet_okx  # type: ignore[no-redef,import-not-found]  # noqa: E402
    from local import _wallet_local  # type: ignore[no-redef,import-not-found]  # noqa: E402


class OrderFailed(RuntimeError):
    pass


# ── Local call-token persistence ────────────────────────────────────────────
# After a successful purchase, we persist the bearer + usage_endpoint locally
# so the buyer can:
#   (a) retry the tool call after a transient failure WITHOUT paying again
#       (`buy.py --use-token <order_no> --call-body '...'`)
#   (b) explicitly return an unused token for refund
#       (`buy.py --return-token <order_no>`)
# Without this, a failed tool call sends the user back to the start of the
# purchase flow — the exact UX that caused the duplicate-payment incident.

_TOKENS_DB_PATH_OVERRIDE: str | None = None  # set by tests via env _WPAY_TOKENS_DB


def _tokens_db_path():
    """Return Path to the local token store. Override via _WPAY_TOKENS_DB
    env so tests can use a tmp dir without polluting the user's HOME."""
    import pathlib
    override = os.environ.get("_WPAY_TOKENS_DB")
    if override:
        return pathlib.Path(override)
    return pathlib.Path.home() / ".wagent" / "tokens.json"


def _load_tokens_db() -> dict:
    """Read the local token DB. Empty/missing → return {}. Corrupted JSON →
    return {} and warn (don't crash on bad state)."""
    p = _tokens_db_path()
    if not p.exists():
        return {}
    try:
        with p.open() as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        print(f"   [WARN] {p} is corrupted; ignoring", file=sys.stderr)
        return {}


def _save_token(order_no: str, info: dict) -> None:
    """Atomic write of one token record into the local DB.

    Uses write-temp-then-rename so a crash mid-write can't corrupt the DB.
    The DB is a single JSON object keyed by order_no — small (typically
    < 100 entries), so rewriting the whole file each save is fine."""
    p = _tokens_db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    db = _load_tokens_db()
    db[order_no] = info
    tmp = p.with_suffix(p.suffix + ".tmp")
    with tmp.open("w") as f:
        json.dump(db, f, indent=2, sort_keys=True)
    tmp.replace(p)


def _get_saved_token(order_no: str) -> dict | None:
    return _load_tokens_db().get(order_no)


def _delete_saved_token(order_no: str) -> None:
    """Used after a successful return — the token is no longer usable."""
    db = _load_tokens_db()
    if order_no in db:
        del db[order_no]
        p = _tokens_db_path()
        tmp = p.with_suffix(p.suffix + ".tmp")
        with tmp.open("w") as f:
            json.dump(db, f, indent=2, sort_keys=True)
        tmp.replace(p)


# ── Inflight purchase tracker (cross-process duplicate-payment guard) ─────
# Separate from tokens.json (which holds POST-delivery state). inflight.json
# records {fingerprint → {order_no, deposit_address, paying_amount, token,
# tx_hash, started_at}} from the moment purchase_intent completes until
# delivery succeeds. A re-run with the same purchase intent within
# INFLIGHT_TTL_SEC trips a refuse-or-resume warning instead of creating a
# duplicate order + risking a second on-chain payment.
#
# This is the SKILL-SIDE half of the dup-payment defense; the GATEWAY also
# does server-side semantic dedup on POST /orders. Either half alone
# catches the common cases; both together close the remaining races.

_INFLIGHT_TTL_SEC = 30 * 60  # 30 min — covers Stablelink expiredIn (3600s/2)


def _inflight_db_path():
    import pathlib
    override = os.environ.get("_WPAY_INFLIGHT_DB")
    if override:
        return pathlib.Path(override)
    return pathlib.Path.home() / ".wagent" / "inflight.json"


def _purchase_fingerprint(
    agent_url: str, product_id: int, quantity: int,
    token: str, customer_id: str,
) -> str:
    """Stable hash of the buyer's purchase intent. Two skill invocations
    with the same fingerprint are presumed to be retries of the same
    purchase. customer_id is included so two concurrent buyers don't
    collide; agent_url so multi-shop runs stay independent."""
    import hashlib
    key = f"{agent_url.rstrip('/')}|{product_id}|{quantity}|{token}|{customer_id}"
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def _load_inflight() -> dict:
    p = _inflight_db_path()
    if not p.exists():
        return {}
    try:
        with p.open() as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        print(f"   [WARN] {p} is corrupted; ignoring", file=sys.stderr)
        return {}


def _save_inflight(fp: str, info: dict) -> None:
    """Atomic upsert into inflight.json under fingerprint key.
    Tolerates dirs that don't exist yet."""
    p = _inflight_db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    db = _load_inflight()
    db[fp] = info
    tmp = p.with_suffix(p.suffix + ".tmp")
    with tmp.open("w") as f:
        json.dump(db, f, indent=2, sort_keys=True)
    tmp.replace(p)


def _delete_inflight(fp: str) -> None:
    db = _load_inflight()
    if fp in db:
        del db[fp]
        p = _inflight_db_path()
        tmp = p.with_suffix(p.suffix + ".tmp")
        with tmp.open("w") as f:
            json.dump(db, f, indent=2, sort_keys=True)
        tmp.replace(p)


def _get_recent_inflight(fp: str, max_age_sec: int = _INFLIGHT_TTL_SEC) -> dict | None:
    """Return the inflight record for `fp` if it's fresher than max_age_sec.
    Stale entries are returned as None (caller proceeds with a fresh
    purchase + overwrites the entry)."""
    entry = _load_inflight().get(fp)
    if not entry:
        return None
    started = entry.get("started_at")
    if not started:
        return None
    try:
        # started_at is written via time.gmtime() (UTC). calendar.timegm
        # interprets the parsed struct_time as UTC — using time.mktime
        # here would treat it as LOCAL time, shifting the comparison by
        # the local TZ offset and breaking the "30 min" semantics for
        # anyone not on UTC.
        import calendar
        started_epoch = calendar.timegm(time.strptime(started, "%Y-%m-%dT%H:%M:%SZ"))
    except (ValueError, TypeError):
        return None
    if time.time() - started_epoch > max_age_sec:
        return None
    return entry


def _extract_delivery_for_save(task: dict, order_no: str) -> dict | None:
    """Walk the completed-state task artifacts and pull everything we'd
    need to invoke or return the token later. Returns None when no
    delivery artifact is present (legitimate for products without
    mcp_call_token delivery)."""
    for a in task.get("artifacts", []) or []:
        for p in a.get("parts", []) or []:
            d = p.get("data") or {}
            ue = d.get("usage_endpoint")
            payload = d.get("payload") or {}
            tok = payload.get("token")
            if ue and tok:
                return {
                    "order_no": order_no,
                    "agent_url": ue.rsplit("/v1/tools/", 1)[0] if "/v1/tools/" in ue else "",
                    "bearer_token": tok,
                    "usage_endpoint": ue,
                    "tool_name": payload.get("tool_name", ""),
                    "calls_remaining": payload.get("calls_remaining"),
                    "calls_total": payload.get("calls_total"),
                    "expires_at": payload.get("expires_at"),
                    "saved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                }
    return None


class UnexpectedState(RuntimeError):
    pass


class WrongAgentUrlError(RuntimeError):
    """Raised when --agent-url doesn't expose the A2A `/tasks/send` route.

    Most common cause: caller passed the wconnector URL (the registry /
    discovery service at connector-dev.wcheckout.app / connector.wcheckout.app)
    instead of a merchant agent URL (each merchant has its own, visible in
    the discover.py output's `agent_url` column). wconnector only exposes
    /merchants/search and /orders/<id>; the A2A protocol endpoints live on
    the merchant agent."""
    pass


def _post_json(url: str, payload: dict, timeout: float = 30.0) -> dict:
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as res:
        return json.loads(res.read())


def _get_json(url: str, timeout: float = 30.0) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as res:
        return json.loads(res.read())


# Transport-layer exceptions that are likely transient inside a polling loop:
# SSL handshake timeouts, connection resets, DNS hiccups, gateway 5xx, etc.
# Catching these inside poll loops lets us swallow brief network blips and
# retry on the next interval instead of failing the whole purchase. Hard
# protocol errors (HTTPError 4xx, JSON decode) still bubble up.
import socket  # noqa: E402
import ssl  # noqa: E402

_TRANSIENT_TRANSPORT_EXC: tuple[type[BaseException], ...] = (
    socket.timeout,
    ssl.SSLError,
    ConnectionError,
    TimeoutError,
    urllib.error.URLError,  # wraps DNS / connect-refused / handshake fails
)


def _is_transient_http_error(exc: BaseException) -> bool:
    """5xx is transient (retry); 4xx is a real error (fail fast)."""
    if isinstance(exc, urllib.error.HTTPError):
        return 500 <= exc.code < 600
    return isinstance(exc, _TRANSIENT_TRANSPORT_EXC)


def _post_json_polling(url: str, payload: dict, timeout: float = 30.0) -> dict | None:
    """Like _post_json, but returns None on transient transport errors and
    logs a one-line stderr notice. Used inside poll loops where the next
    iteration retries naturally. Hard 4xx still raises."""
    try:
        return _post_json(url, payload, timeout=timeout)
    except urllib.error.HTTPError as e:
        if 500 <= e.code < 600:
            print(f"   [transient] HTTP {e.code} — retrying", file=sys.stderr)
            return None
        raise
    except _TRANSIENT_TRANSPORT_EXC as e:
        print(f"   [transient] {type(e).__name__}: {e} — retrying", file=sys.stderr)
        return None


def _get_json_polling(url: str, timeout: float = 30.0) -> dict | None:
    """Same retry tolerance as _post_json_polling, for GET-based polls."""
    try:
        return _get_json(url, timeout=timeout)
    except urllib.error.HTTPError as e:
        if 500 <= e.code < 600:
            print(f"   [transient] HTTP {e.code} — retrying", file=sys.stderr)
            return None
        raise
    except _TRANSIENT_TRANSPORT_EXC as e:
        print(f"   [transient] {type(e).__name__}: {e} — retrying", file=sys.stderr)
        return None


def _failure_reason(task: dict, connector_url: str | None = None) -> str:
    """Pull human-readable error from artifact + (optionally) gateway order detail."""
    reason = ""
    order_no = ""
    text_reason = ""
    for a in task.get("artifacts", []) or []:
        for p in a.get("parts", []) or []:
            d = p.get("data") or {}
            order_no = order_no or d.get("order_no", "")
            # Prefer structured data fields; they're more precise than a
            # generic text label like "Payment failed".
            for key in ("error", "message", "reason"):
                if not reason and d.get(key):
                    reason = d[key]
            # Collect text part as fallback only if data didn't yield anything.
            if not text_reason and p.get("type") == "text" and p.get("text"):
                text_reason = p["text"]
    # Use text only when no structured field was found across all parts.
    reason = reason or text_reason
    if order_no and connector_url:
        try:
            detail = _get_json(f"{connector_url.rstrip('/')}/orders/{order_no}", timeout=10)
            extras = []
            for k in ("error", "error_message", "status_message", "last_error", "wcheckout_response"):
                if detail.get(k):
                    extras.append(f"{k}: {detail[k]}")
            if extras:
                reason = (reason + "\n" + "\n".join(extras)).strip()
        except Exception:
            pass
    return reason or "unknown"


def _is_actionable(task: dict) -> bool:
    """A task is 'actionable' once the buyer can take a next step: pay
    (deposit info present in artifacts).

    State is informational; artifacts are authoritative. Some merchants
    return state=working with deposit_address+paying_amount already
    populated — there's nothing left to wait for."""
    try:
        extract_deposit(task)
        return True
    except KeyError:
        return False


def _wait_for_initial_terminal(
    *, agent_url: str, task_id: str,
    timeout_sec: int = 30, interval_sec: int = 2,
) -> dict:
    """Poll GET /tasks/<id> while task is in a non-terminal, non-actionable
    initial state.

    Some merchants accept the purchase intent synchronously and return
    state=working/submitted while their backend talks to wcheckout. Polling
    via GET /tasks/<id> drives the task to a usable state without re-sending
    the purchase intent (which would create duplicate orders — Hard rule 1).

    Exits early when state turns terminal OR when artifacts become actionable
    (deposit info / approve URL populated) — some merchants stay 'working'
    forever but produce all the data the buyer needs in artifacts."""
    deadline = time.monotonic() + timeout_sec
    last_task: dict = {}
    while time.monotonic() < deadline:
        response = _get_json_polling(agent_url.rstrip("/") + f"/tasks/{task_id}")
        if response is None:
            # transient transport error — retry next iteration
            time.sleep(interval_sec)
            continue
        try:
            task = response.get("result") or response
            last_task = task
        except AttributeError:
            # Non-dict response, treat as transient
            time.sleep(interval_sec)
            continue
        state = task.get("state")
        # input-required is dead state for us now (approval flow retired),
        # but keep accepting it in case a stale shop deploy still emits it
        # — we'll bail out with the same "stuck" UnexpectedState below.
        if state in ("completed", "failed"):
            return task
        if _is_actionable(task):
            return task
        time.sleep(interval_sec)
    raise UnexpectedState(
        f"task {task_id} stuck in non-terminal state for {timeout_sec}s"
    )


def purchase_intent(
    *, agent_url: str, product_id: int, quantity: int, token: str,
    connector_url: str | None = None,
    initial_state_timeout_sec: int = 30,
) -> dict:
    """Send purchase intent. Return a task the buyer can act on.

    Tolerates three merchant patterns:
      A. Synchronous: state=completed/input-required immediately.
      B. Async with eventual transition: state=working/submitted, GET polling
         drives it to terminal. Never re-sends purchase intent (Hard rule 1).
      C. Soft-working: state stays working/submitted permanently but
         artifacts immediately carry deposit_address + paying_amount (or
         approve_url). Trust artifacts as authoritative; state is a hint.

    Raises OrderFailed on failed; UnexpectedState only when the task is
    neither in a terminal state nor carries actionable artifacts after the
    initial-state timeout."""
    payload = {
        "message": {"role": "user", "parts": [{"type": "data", "data": {
            "intent": "purchase",
            "product_id": product_id,
            "quantity": quantity,
            "token": token,
            "customer_id": "agent",
        }}]}
    }
    target_url = agent_url.rstrip("/") + "/tasks/send"
    try:
        response = _post_json(target_url, payload)
    except urllib.error.HTTPError as e:
        # 404 here almost always means: the user passed a wconnector URL
        # (registry/discovery) instead of a merchant agent URL. wconnector
        # exposes /merchants/search + /orders/<id>, not /tasks/send.
        # Translate to a structured error with recovery hint instead of
        # bubbling raw HTTPError to the catch-all.
        if e.code == 404:
            host = urllib.parse.urlsplit(target_url).hostname or target_url
            # Match either the legacy "gateway" branding or the new "wconnector"
            # branding — same root cause, same fix.
            looks_like_wconnector = host is not None and (
                "connector" in host.lower() or "whub" in host.lower() or "gateway" in host.lower()
            )
            hint = (
                "\n   This URL doesn't expose POST /tasks/send. Most likely:\n"
                "   you passed a wconnector URL where a merchant Agent URL was\n"
                "   expected. wconnector only hosts /merchants/search and\n"
                "   /orders/<id>; the A2A endpoints live on the merchant.\n"
                "   Run `python3 scripts/discover.py` to list merchants and\n"
                "   pick the agent_url for the SKU you want."
            )
            if looks_like_wconnector:
                hint = (
                    f"\n   The hostname '{host}' looks like a connector / gateway "
                    f"URL — did you mean to pass the merchant agent URL instead?"
                    + hint
                )
            raise WrongAgentUrlError(
                f"POST {target_url} returned 404 Not Found.{hint}"
            ) from e
        raise
    task = response["result"]
    state = task.get("state")

    # Pattern A: terminal initial state — return immediately.
    if state == "completed":
        return task
    if state == "failed":
        raise OrderFailed(_failure_reason(task, connector_url))

    # Pattern C: working/submitted but artifacts already actionable — short-
    # circuit, no polling needed.
    if state in ("working", "submitted") and _is_actionable(task):
        return task

    # Pattern B: working/submitted without actionable artifacts yet — poll.
    if state in ("working", "submitted"):
        task = _wait_for_initial_terminal(
            agent_url=agent_url, task_id=task["id"],
            timeout_sec=initial_state_timeout_sec,
        )
        state = task.get("state")

    if state == "completed":
        return task
    if state == "failed":
        raise OrderFailed(_failure_reason(task, connector_url))
    # Final fallback: state may still be working/submitted but if artifacts
    # are populated by now, the task is usable.
    if _is_actionable(task):
        return task
    raise UnexpectedState(
        f"unexpected initial state: {state}. Full task: {json.dumps(task)[:500]}"
    )


class OrderCanceled(RuntimeError):
    pass


_TX_HASH_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")

# Demo gateway URL — used as a final fallback when neither --connector-url nor
# CONNECTOR_URL env is set. NOT a secret — already published in the docs. Lets
# the agent run discover/buy zero-config against the demo without an
# explicit env step. Override anytime via --connector-url or CONNECTOR_URL env.
# wconnector demo / dev environment. Production: https://connector.wcheckout.app —
# set via CONNECTOR_URL env or --connector-url. NOT a secret — published in docs
# so wagent can run discover/buy zero-config against the demo.
DEMO_CONNECTOR_URL = "https://connector-dev.wcheckout.app"


class PaymentNotConfirmed(RuntimeError):
    pass


class ConfigError(RuntimeError):
    pass


def extract_deposit(task: dict) -> tuple[str, float, str]:
    """Pull (deposit_address, paying_amount, order_no) from task artifacts.

    Raises KeyError if the artifact is missing the expected shape."""
    for a in task.get("artifacts", []) or []:
        for p in a.get("parts", []) or []:
            d = p.get("data") or {}
            if "deposit_address" in d and "paying_amount" in d and "order_no" in d:
                return d["deposit_address"], float(d["paying_amount"]), d["order_no"]
    raise KeyError("no deposit_address/paying_amount/order_no in task artifacts")


DEFAULT_PAYMENT_BACKEND = "okx"


def pay_on_chain(*, token: str, recipient: str, amount: float) -> str:
    """Dispatch ERC-20 transfer to the configured backend; return tx hash.

    Default backend is `okx` (Ethereum mainnet, REAL MONEY). Set
    PAYMENT_BACKEND=local to use the web3.py backend (Sepolia testnet by
    default; mainnet via LOCAL_NETWORK=mainnet)."""
    backend = os.environ.get("PAYMENT_BACKEND") or DEFAULT_PAYMENT_BACKEND
    if backend not in ("local", "okx"):
        raise ConfigError(
            f"PAYMENT_BACKEND must be 'local' or 'okx' (got {backend!r})."
        )

    if backend == "local":
        network = os.environ.get("LOCAL_NETWORK", "sepolia")
        contract = _tokens.resolve_contract(token, network)
        tx_hash = _wallet_local.send_erc20(
            contract=contract, recipient=recipient, amount=amount,
        )
    else:  # okx
        contract = _tokens.resolve_contract(token, "mainnet")
        tx_hash = _wallet_okx.send_erc20(
            contract=contract, recipient=recipient, amount=amount,
        )

    if not _TX_HASH_RE.match(tx_hash):
        raise PaymentNotConfirmed(
            f"payment did NOT happen: backend returned {tx_hash!r}, not a 0x+64-hex tx hash"
        )
    return tx_hash


class SettlementTimeout(RuntimeError):
    pass


def poll_settlement(
    *, agent_url: str, task_id: str, order_no: str,
    timeout_sec: int = 180, interval_sec: int = 5,
    connector_url: str | None = None,
) -> dict:
    """Poll the task with intent=check_payment until terminal.

    Returns the task on completed (artifact contains delivery payload).
    Raises OrderFailed on failed; SettlementTimeout on deadline.

    Default timeout 180s (3 min): if Stablelink + webhook + gateway can't
    confirm settlement in 3 min, something is misconfigured (see the
    timeout message for the 3 most common causes). Don't busy-wait for
    minutes hoping a broken pipeline will heal — fail fast and surface
    the receipt + recovery URL so the user can investigate."""
    deadline = time.monotonic() + timeout_sec
    gateway_paid_announced = False
    while time.monotonic() < deadline:
        payload = {"id": task_id, "message": {"role": "user", "parts": [
            {"type": "data", "data": {"intent": "check_payment", "order_no": order_no}}
        ]}}
        response = _post_json_polling(agent_url.rstrip("/") + "/tasks/send", payload)
        if response is None:
            # Shop's check_payment is flaky right now (SSL handshake timeout,
            # connection reset, 502, etc). Don't abort — under network jitter
            # users panic-retry and end up paying twice. Instead, fall back
            # to querying the gateway DB directly: it's where settlement
            # state actually lives, and shop's check_payment just reads from
            # it. If gateway already shows PAID, the user's money is safe;
            # we just need shop to eventually emit the delivery artifact.
            if not gateway_paid_announced and _gateway_shows_paid(connector_url, order_no):
                print(
                    f"   ✓ Gateway confirms order {order_no} is PAID. "
                    "Waiting for shop to emit delivery artifact...",
                    file=sys.stderr,
                )
                gateway_paid_announced = True
            time.sleep(interval_sec)
            continue
        task = response["result"]
        state = task.get("state")
        if state == "completed":
            return task
        if state == "failed":
            raise OrderFailed(_failure_reason(task, connector_url))
        # Shop says working / submitted. Cross-check gateway one time so the
        # user knows whether the slowdown is wcheckout-side (waiting on
        # webhook) or shop-side (already paid, slow to react).
        if not gateway_paid_announced and _gateway_shows_paid(connector_url, order_no):
            print(
                f"   ✓ Gateway confirms order {order_no} is PAID. "
                "Shop is lagging; will keep polling for delivery...",
                file=sys.stderr,
            )
            gateway_paid_announced = True
        time.sleep(interval_sec)

    # Timed out. Surface the gateway's view + recovery hint so the user can
    # tell whether their money is stuck pre-PAID (webhook misconfig) or
    # post-PAID (shop just slow / restarted between purchase and settle).
    backend_status = _query_gateway_order_status(connector_url, order_no)
    raise SettlementTimeout(
        f"settlement not confirmed within {timeout_sec}s.\n"
        + (backend_status + "\n" if backend_status else "")
        + _settlement_troubleshoot_hint(order_no, connector_url)
    )


def _gateway_shows_paid(connector_url: str | None, order_no: str) -> bool:
    """Best-effort, fast (3s timeout) probe of gateway DB.

    Returns True iff the order's status is exactly PAID. Never raises —
    a flaky gateway during a flaky-shop-node window is the worst case
    and just means we don't get the early-confirmation message; the
    main poll loop continues regardless."""
    if not connector_url:
        return False
    try:
        detail = _get_json(
            f"{connector_url.rstrip('/')}/orders/{order_no}", timeout=3,
        )
    except Exception:
        return False
    status = detail.get("status") or detail.get("state") or ""
    return str(status).upper() == "PAID"


def _query_gateway_order_status(connector_url: str | None, order_no: str) -> str:
    """Best-effort fetch of the gateway's view of the order, used only to
    enrich a settlement-timeout message. Never raises."""
    if not connector_url:
        return ""
    try:
        detail = _get_json(f"{connector_url.rstrip('/')}/orders/{order_no}", timeout=10)
    except Exception as e:
        return f"   gateway lookup failed: {e}"
    status = detail.get("status") or detail.get("state") or "unknown"
    extras = []
    for k in ("error", "error_message", "status_message", "last_error",
              "wcheckout_response", "tx_hash"):
        if detail.get(k):
            extras.append(f"   {k}: {detail[k]}")
    body = f"   gateway status: {status}"
    if extras:
        body += "\n" + "\n".join(extras)
    return body


def _settlement_troubleshoot_hint(order_no: str, connector_url: str | None) -> str:
    """3 most-common backend misconfigurations that cause permanent
    'stuck in PAYING'. Surfaced on every settlement timeout because the
    skill cannot tell which one is at fault — but the user / shop ops can."""
    return (
        "\n   Tx hash is on chain — the on-chain transfer succeeded. The shop\n"
        "   never observed it. Three usual causes (in order of frequency):\n"
        "   1. shop's MERCHANT_AGENT_URL is localhost / private — Stablelink's\n"
        "      webhook can't reach it. Set it to a publicly reachable URL\n"
        "      (ngrok, public ingress) and retry a fresh order.\n"
        "   2. WCHECKOUT_SANDBOX=true on shop or gateway while paying mainnet —\n"
        "      sandbox Stablelink doesn't see your mainnet tx. Set to false.\n"
        "   3. CONNECTOR_API_KEY mismatch — shop /notify silently 401s when\n"
        "      relaying the webhook to the gateway. Verify the key matches.\n"
        f"   Recovery: {connector_url or '<gateway>'}/orders/{order_no}"
    )


class ToolCallError(RuntimeError):
    pass


def extract_delivery(task: dict) -> tuple[str, str]:
    """Pull (bearer_token, usage_endpoint) from a completed-state task.

    Canonical W Connector shop-node shape (verified against
    shop-node/src/delivery/mcp-token-handler.ts:27 and a2a/handlers.ts:306):

        {"artifacts": [
          {"name": "payment_confirmed", "parts": [...]},
          {"name": "delivery", "parts": [{"type": "data", "data": {
              "kind": "call_token",
              "usage_endpoint": "http://shop/v1/tools/...",
              "payload": {
                "token": "<bearer>",
                "tool_name": "...",
                "calls_remaining": N,
                "tx_hash": "0x...",
                ...
              }
          }}]}
        ]}

    The delivery artifact is the one named "delivery" (or any artifact whose
    parts[0].data carries usage_endpoint + payload.token). The payment_confirmed
    artifact has no token — it just acknowledges receipt. Walk all artifacts
    and find the one with the right shape; don't assume index.

    Tolerates three legacy alternative shapes for robustness against future
    shop changes — see Pattern Y/Z fallbacks below.

    Raises KeyError when no artifact carries delivery info — typically means
    the shop emitted state=completed without delivery (in-memory task store
    on shop-node lost metadata between purchase and check_payment intents,
    silently skipping onPaid()). The KeyError message points at the recovery
    path."""
    for a in task.get("artifacts", []) or []:
        # Pattern X (canonical): parts[*].data.{usage_endpoint, payload.token}
        for p in a.get("parts", []) or []:
            d = p.get("data") or {}
            ue = d.get("usage_endpoint")
            payload = d.get("payload") or {}
            tok = payload.get("token")
            if ue and tok:
                return tok, ue

        # Pattern Y (legacy SKILL.md Python example): artifact-level
        # usage_endpoint + payload.token (no parts/data wrapper).
        ue = a.get("usage_endpoint")
        ap = a.get("payload") or {}
        tok = ap.get("token")
        if ue and tok:
            return tok, ue

        # Pattern Z (defensive): parts[*].data.delivery.{token, usage_endpoint}
        # OR parts[*].data.{token, usage_endpoint} flat. Future-proofing only.
        for p in a.get("parts", []) or []:
            d = p.get("data") or {}
            delivery = d.get("delivery") or {}
            if delivery.get("token") and delivery.get("usage_endpoint"):
                return delivery["token"], delivery["usage_endpoint"]
            if d.get("token") and d.get("usage_endpoint"):
                return d["token"], d["usage_endpoint"]

    raise KeyError(
        "no delivery payload in completed task artifacts. Payment did go "
        "through (see Tx hash above) but the shop did not emit a delivery "
        "artifact. Likely cause: shop-node's in-memory TaskStore lost the "
        "purchase metadata between intents (e.g., shop restart, or a fresh "
        "task created with check_payment as first intent). Recover via the "
        "order_no — query ${CONNECTOR_URL}/orders/<order_no> for the order "
        "record, or contact the merchant ops to re-emit the delivery."
    )


def invoke_tool(*, usage_endpoint: str, bearer_token: str, body: dict) -> dict:
    """POST one bearer-token call to the merchant's tool endpoint."""
    req = urllib.request.Request(
        usage_endpoint,
        data=json.dumps(body).encode(),
        headers={
            "Authorization": f"Bearer {bearer_token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as res:
            return json.loads(res.read())
    except urllib.error.HTTPError as e:
        raise ToolCallError(
            f"tool call returned HTTP {e.code}: {e.read().decode(errors='replace')}"
        ) from e


def _maybe_test_pay(token: str, recipient: str, amount: float) -> str | None:
    """Honor BUY_TEST_FAKE_TX env so integration tests can bypass real signing.

    Production paths never set this env; safe to leave in."""
    fake = os.environ.get("BUY_TEST_FAKE_TX")
    if fake and _TX_HASH_RE.match(fake):
        return fake
    return None


_DEFAULT_TOKEN_PRIORITY = "ETH_WUSD,ETH_USDT,ETH_USDC"


# Errors that are safe to recover from by retrying with a different token.
# All happen BEFORE any on-chain broadcast, so no money has moved yet:
#   - OrderFailed at purchase_intent: merchant doesn't accept this token
#     (e.g. Stablelink vault for that chain not enabled).
#   - InsufficientBalanceError at pay_on_chain: caught at OKX simulation
#     stage, no tx broadcast.
#   - HighRiskError at pay_on_chain: OKX refuses to even submit; no tx.
#   - _wallet_local.ConfigError: e.g. ETH_USDT_CONTRACT_SEPOLIA missing
#     for one specific token but not others.
_FALLBACK_TRIGGERS: tuple[type[BaseException], ...] = (
    _wallet_okx.InsufficientBalanceError,
    _wallet_okx.HighRiskError,
    _wallet_local.ConfigError,
)


def _try_one_token(
    token: str, args, call_body: dict,
) -> int | None:
    """Run the full purchase pipeline for one token.

    Returns:
      0  on success (tool result printed to stdout)
      1  on a non-recoverable failure that already printed its own diagnostic
      None if a recoverable pre-payment error fired and the caller should try
           the next token in the priority list

    Raises only non-recoverable exceptions (e.g. OrderCanceled, ToolCallError,
    SettlementTimeout) that should not trigger fallback."""

    # Stage A: purchase intent + (optional) approval polling + deposit
    # extraction. Recoverable on OrderFailed (merchant rejected this token).
    try:
        task = purchase_intent(
            agent_url=args.agent_url, product_id=args.product_id,
            quantity=args.quantity, token=token,
            connector_url=args.connector_url or None,
        )
    except OrderFailed as e:
        print(f"   {token}: merchant rejected — {e}", file=sys.stderr)
        return None

    # Approval flow retired — buyer's wallet (OKX Agentic Wallet Policy
    # Settings) is the source of spend control. If a stale shop still
    # returns state=input-required, fail loudly rather than hang on an
    # endpoint that doesn't exist anymore.
    if task["state"] == "input-required":
        raise UnexpectedState(
            "shop returned input-required (approval flow), which has been "
            "retired. Buyer-side spend caps now live in the wallet Policy "
            "Settings (e.g. OKX Agentic Wallet). The shop deploy is stale; "
            "ask shop ops to redeploy."
        )

    deposit, amount, order_no = extract_deposit(task)

    # Inflight tracker: persist the purchase fingerprint → order_no mapping
    # BEFORE any on-chain step so a re-run after a network blip in
    # pay_on_chain sees this entry and refuses to create another order.
    # Best-effort; never block the buyer on save failures.
    inflight_fp = _purchase_fingerprint(
        agent_url=args.agent_url, product_id=args.product_id,
        quantity=args.quantity, token=token, customer_id="agent",
    )
    try:
        _save_inflight(inflight_fp, {
            "order_no": order_no,
            "task_id": task.get("id"),
            "deposit_address": deposit,
            "paying_amount": amount,
            "token": token,
            "agent_url": args.agent_url,
            "tx_hash": None,
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        })
    except Exception as e:  # noqa: BLE001
        print(f"   [WARN] could not persist inflight record: {e}", file=sys.stderr)

    # Stage B: on-chain broadcast. Recoverable on InsufficientBalanceError /
    # HighRiskError (no tx hits chain in either case). After this stage,
    # money is in flight — any further error is terminal.
    fake_tx = _maybe_test_pay(token, deposit, amount)
    if fake_tx is not None:
        tx_hash = fake_tx
    else:
        try:
            tx_hash = pay_on_chain(token=token, recipient=deposit, amount=amount)
        except _wallet_okx.InsufficientBalanceError as e:
            print(f"   {token}: wallet underfunded — {e}", file=sys.stderr)
            return None
        except _wallet_okx.HighRiskError as e:
            print(f"   {token}: OKX flagged high-risk — {e}", file=sys.stderr)
            return None
        except _wallet_local.ConfigError as e:
            print(f"   {token}: backend config missing — {e}", file=sys.stderr)
            return None

    # Update inflight with tx_hash so a re-run can tell "tx is on chain,
    # just slow" apart from "tx was never broadcast".
    try:
        _save_inflight(inflight_fp, {
            "order_no": order_no,
            "task_id": task.get("id"),
            "deposit_address": deposit,
            "paying_amount": amount,
            "token": token,
            "agent_url": args.agent_url,
            "tx_hash": tx_hash,
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        })
    except Exception:  # noqa: BLE001
        pass

    print(f"Tx: {tx_hash}")
    sys.stdout.flush()

    # Stage C: settle + deliver + invoke. Past the point of no return.
    # Errors here are surfaced as failures, never trigger fallback.
    task = poll_settlement(
        agent_url=args.agent_url, task_id=task["id"], order_no=order_no,
        timeout_sec=args.settle_timeout_sec,
        interval_sec=args.poll_interval_sec,
        connector_url=args.connector_url or None,
    )

    # Persist the delivery artifact before doing anything that could fail.
    # If the tool call below errors, the user can recover with
    # `buy.py --use-token <order_no> --call-body '...'` instead of paying
    # again. Save failures are non-fatal (best-effort persistence).
    try:
        saved = _extract_delivery_for_save(task, order_no)
        if saved:
            _save_token(order_no, saved)
            print(
                f"   Token saved locally — recover with: "
                f"buy.py --use-token {order_no} --call-body '...'",
                file=sys.stderr,
            )
    except Exception as e:  # noqa: BLE001 — never let save errors break purchase
        print(f"   [WARN] could not persist token locally: {e}", file=sys.stderr)

    try:
        bearer, usage_endpoint = extract_delivery(task)
    except KeyError as e:
        print(
            "\n⚠️  Payment succeeded but tool not delivered.",
            file=sys.stderr,
        )
        print(f"   Tx:        {tx_hash}", file=sys.stderr)
        print(f"   Order:     {order_no}", file=sys.stderr)
        print(f"   Token:     {token}", file=sys.stderr)
        print(f"   Amount:    {amount}", file=sys.stderr)
        print(
            f"   Recovery:  curl {args.connector_url.rstrip('/')}/orders/{order_no}",
            file=sys.stderr,
        )
        print(f"   Reason:    {e}", file=sys.stderr)
        print(
            "\n   Full settlement task JSON (for shop ops to debug):",
            file=sys.stderr,
        )
        print(json.dumps(task, indent=2), file=sys.stderr)
        return 1

    try:
        result = invoke_tool(
            usage_endpoint=usage_endpoint, bearer_token=bearer, body=call_body,
        )
    except ToolCallError as e:
        # Shop's tool endpoint decrements the call_token ONLY on successful
        # invocation (see shop-node/src/routes/tools.ts: `store.decrement()`
        # runs AFTER `tool.run()`). On 4xx/5xx the token is still good.
        # Print enough that the user can retry by hand (with corrected
        # input) WITHOUT paying again. This is the recovery hint the
        # original failure path was missing — the user paid, the tool
        # errored, the skill exited, and the user (not knowing the token
        # was still valid) ran the whole skill again → double payment.
        print(f"\n⚠️  Tool call failed AFTER successful payment.", file=sys.stderr)
        print(f"   Error: {e}", file=sys.stderr)
        print(f"   Tx hash:        {tx_hash}", file=sys.stderr)
        print(f"   Order:          {order_no}", file=sys.stderr)
        print(
            "\n   YOUR CALL TOKEN IS STILL VALID — do NOT re-run buy.py "
            "(that pays again). Fix --call-body and retry:",
            file=sys.stderr,
        )
        retry_curl = (
            f"curl -X POST {usage_endpoint} \\\n"
            f"     -H 'Authorization: Bearer {bearer}' \\\n"
            f"     -H 'Content-Type: application/json' \\\n"
            f"     -d '<corrected JSON body>'"
        )
        print(f"\n   {retry_curl}\n", file=sys.stderr)
        raise
    # Settled + delivered + invoked → clear the inflight entry. The
    # call_token survives in ~/.wagent/tokens.json for future --use-token.
    try:
        _delete_inflight(inflight_fp)
    except Exception:  # noqa: BLE001
        pass
    print(json.dumps(result, indent=2))
    return 0


def _run_use_token(order_no: str, call_body: dict) -> int:
    """`buy.py --use-token <order_no> --call-body '...'`: invoke a previously
    purchased token without paying again. Idempotent in the happy path —
    a successful call decrements the saved `calls_remaining` so subsequent
    runs honor the remaining budget."""
    saved = _get_saved_token(order_no)
    if not saved:
        print(
            f"ERROR: no saved token for order {order_no}.\n"
            f"   Local token DB: {_tokens_db_path()}\n"
            f"   Possible causes:\n"
            f"   - the order was bought in a different agent harness (different HOME)\n"
            f"   - the token was already returned via --return-token\n"
            f"   - the order_no is a typo",
            file=sys.stderr,
        )
        return 1
    remaining = saved.get("calls_remaining")
    if isinstance(remaining, int) and remaining <= 0:
        print(
            f"ERROR: saved token for {order_no} has 0 calls remaining.\n"
            f"   If you believe this is a stale local record, delete the entry from\n"
            f"   {_tokens_db_path()} and re-purchase.",
            file=sys.stderr,
        )
        return 1
    try:
        result = invoke_tool(
            usage_endpoint=saved["usage_endpoint"],
            bearer_token=saved["bearer_token"],
            body=call_body,
        )
    except ToolCallError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        print(
            f"   Token still valid (call NOT decremented). Fix --call-body and retry.\n"
            f"   To void this token and request a refund: "
            f"buy.py --return-token {order_no}",
            file=sys.stderr,
        )
        return 1
    # Server returns updated calls_remaining — keep our local copy in sync.
    if isinstance(result, dict) and "calls_remaining" in result:
        saved["calls_remaining"] = result["calls_remaining"]
        _save_token(order_no, saved)
    print(json.dumps(result, indent=2))
    return 0


def _run_return_token(order_no: str) -> int:
    """`buy.py --return-token <order_no>`: tell the shop the buyer is done
    with this token. Shop voids the token + reports failure to gateway →
    admin sees order as refund-eligible in Orders dashboard."""
    saved = _get_saved_token(order_no)
    if not saved:
        print(f"ERROR: no saved token for order {order_no}", file=sys.stderr)
        return 1
    bearer = saved.get("bearer_token")
    agent_url = saved.get("agent_url")
    if not bearer or not agent_url:
        print(
            f"ERROR: saved record for {order_no} is missing bearer / agent_url.\n"
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
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as res:
            body = json.loads(res.read())
    except urllib.error.HTTPError as e:
        # Decode the server's JSON error so the user knows WHICH precondition
        # failed (partially_used / expired / already_returned / unknown).
        try:
            err_body = json.loads(e.read())
        except Exception:  # noqa: BLE001
            err_body = {"reason": f"HTTP {e.code}"}
        print(
            f"ERROR: shop refused return: {err_body}\n"
            f"   Common cases:\n"
            f"   - 'partially_used': you already made a tool call with this token. "
            f"Shop won't refund partial use.\n"
            f"   - 'expired': the call-token TTL has passed. Refund window over.\n"
            f"   - 'already_returned': you already returned this token.",
            file=sys.stderr,
        )
        return 1
    if not body.get("ok"):
        print(f"ERROR: shop refused return: {body}", file=sys.stderr)
        return 1
    print(json.dumps(body, indent=2))
    # Remove from local DB so subsequent --use-token gives a clean "not found"
    _delete_saved_token(order_no)
    print(
        f"   Order {body.get('order_no')} marked refund-eligible on gateway.\n"
        f"   Admin can complete the refund from the Orders dashboard.",
        file=sys.stderr,
    )
    return 0


def _parse_token_priority(token_arg: str) -> list[str]:
    """Parse comma-separated --token list. Validates each is supported."""
    tokens = [t.strip() for t in token_arg.split(",") if t.strip()]
    if not tokens:
        raise ValueError("--token must list at least one token")
    invalid = [t for t in tokens if t not in _tokens.VALID_TOKENS]
    if invalid:
        raise ValueError(
            f"unknown token(s) {invalid}; valid: {sorted(_tokens.VALID_TOKENS)}"
        )
    # Drop dups while preserving order (priority).
    seen: set[str] = set()
    deduped = []
    for t in tokens:
        if t not in seen:
            seen.add(t)
            deduped.append(t)
    return deduped


def _find_input_schema(agent_url: str, product_id: int) -> dict | None:
    """Resolve product_id → tool_name → skill.metadata.input_schema.

    Returns None when the agent card is unreachable OR the schema is absent
    (older shop deployments). In that case we cannot prevalidate and have
    to trust the shop's server-side check — which means the buyer might
    still pay-then-fail. Tradeoff: don't block legitimate purchases just
    because a shop chose to omit the schema."""
    try:
        with urllib.request.urlopen(
            agent_url.rstrip("/") + "/.well-known/agent.json", timeout=5
        ) as r:
            card = json.load(r)
    except Exception:
        return None

    # Find the product → its delivery.tool_name
    tool_name = None
    for p in (card.get("catalog", {}).get("products") or []):
        if p.get("id") == product_id:
            tool_name = (p.get("delivery") or {}).get("tool_name")
            break
    if not tool_name:
        return None

    # Find the skill whose metadata.tool_name matches → input_schema
    for s in (card.get("skills") or []):
        md = s.get("metadata") or {}
        if md.get("tool_name") == tool_name:
            return md.get("input_schema") or None
    return None


def _validate_against_schema(body: dict, schema: dict, path: str = "$") -> str | None:
    """Minimal JSON Schema validator — only the subset shop-node's
    input_schemas actually use: type, properties, required, pattern,
    additionalProperties, enum. Avoids a hard dep on jsonschema.

    Returns None on success, an explanatory string on failure (suitable for
    direct stderr printing). The error is anchored at the field path so the
    user can fix it without reading the whole schema."""
    if not isinstance(schema, dict):
        return None  # nothing to validate against
    expected = schema.get("type")
    if expected == "object":
        if not isinstance(body, dict):
            return f"{path}: expected object, got {type(body).__name__}"
        props = schema.get("properties") or {}
        for req in (schema.get("required") or []):
            if req not in body:
                return (
                    f"missing required field '{req}'. Tool's input_schema "
                    f"requires: {sorted((schema.get('required') or []))}"
                )
        if schema.get("additionalProperties") is False:
            extras = [k for k in body if k not in props]
            if extras:
                return (
                    f"unknown field(s) {extras}. Schema allows only: "
                    f"{sorted(props.keys())}"
                )
        for k, v in body.items():
            if k in props:
                sub = _validate_against_schema(v, props[k], path=f"{path}.{k}")
                if sub:
                    return sub
        return None
    if expected == "string":
        if not isinstance(body, str):
            return f"{path}: expected string, got {type(body).__name__}"
        pat = schema.get("pattern")
        if pat:
            try:
                if not re.search(pat, body):
                    desc = schema.get("description") or ""
                    return (
                        f"{path}: value {body!r} does not match required "
                        f"pattern {pat!r}"
                        + (f". Field expects: {desc}" if desc else "")
                    )
            except re.error:
                return None  # malformed schema; don't block
        enum = schema.get("enum")
        if enum and body not in enum:
            return f"{path}: value {body!r} not in allowed {enum}"
        return None
    if expected == "integer":
        if not isinstance(body, int) or isinstance(body, bool):
            return f"{path}: expected integer, got {type(body).__name__}"
        return None
    if expected == "number":
        if not isinstance(body, (int, float)) or isinstance(body, bool):
            return f"{path}: expected number, got {type(body).__name__}"
        return None
    if expected == "boolean":
        if not isinstance(body, bool):
            return f"{path}: expected boolean, got {type(body).__name__}"
        return None
    if expected == "array":
        if not isinstance(body, list):
            return f"{path}: expected array, got {type(body).__name__}"
        item_schema = schema.get("items")
        if item_schema:
            for i, v in enumerate(body):
                sub = _validate_against_schema(v, item_schema, path=f"{path}[{i}]")
                if sub:
                    return sub
        return None
    return None


def _prevalidate_call_body(agent_url: str, product_id: int, body: dict) -> str | None:
    """Client-side schema check that runs BEFORE any payment.

    Returns None when valid OR when no schema is available (defer to shop
    server-side). Returns an actionable error string otherwise. The whole
    point of this guard is to prevent the duplicate-payment pattern:
        1. user supplies bad input (e.g. ENS name, missing field)
        2. skill pays on chain
        3. tool call returns 400 due to invalid input
        4. user panic-retries with fixed input → pays again
    With this check, step 1 stops the pipeline before step 2."""
    schema = _find_input_schema(agent_url, product_id)
    if not schema:
        return None  # nothing to validate against; trust shop server
    return _validate_against_schema(body, schema)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Buy a tool pack and invoke one tool call.")
    # Three CLI modes are mutually exclusive at the top of main(); for each
    # mode only a subset of args is meaningful. Argparse can't model this
    # cleanly without subcommands, so flags are non-required and we validate
    # the combo by hand after parse.
    ap.add_argument("--agent-url",
                    help="(buy mode) merchant agent URL; required for new purchase")
    ap.add_argument("--product-id", type=int,
                    help="(buy mode) product id from agent card / discover.py")
    ap.add_argument("--quantity", type=int,
                    help="(buy mode) how many delivery units to buy")
    ap.add_argument(
        "--token",
        default=_DEFAULT_TOKEN_PRIORITY,
        help=(
            "(buy mode) single token (e.g. ETH_USDC) OR comma-separated priority list "
            f"(e.g. ETH_WUSD,ETH_USDT,ETH_USDC). Default: {_DEFAULT_TOKEN_PRIORITY}. "
            "On wallet underfunded / merchant rejection / config-missing, the "
            "skill falls back to the next token in the list."
        ),
    )
    ap.add_argument("--call-body",
                    help="JSON object passed verbatim as the tool call body "
                         "(required in --use-token and buy modes)")
    ap.add_argument("--use-token", metavar="ORDER_NO",
                    help="Invoke the saved call_token from a previous purchase "
                         "without paying again. Reads from ~/.wagent/tokens.json. "
                         "Useful when the first tool call failed due to bad input.")
    ap.add_argument("--return-token", metavar="ORDER_NO",
                    help="Return an unused call_token to the shop to flag the "
                         "order as refund-eligible. Shop voids the token and "
                         "reports failure to the gateway.")
    ap.add_argument("--force-new", action="store_true",
                    help="Override the in-flight purchase guard: create a new "
                         "order even if an identical purchase intent was started "
                         "within the last 30 min. Risks duplicate on-chain "
                         "payment — use only when you're SURE the previous run "
                         "never broadcast a tx.")
    ap.add_argument("--connector-url",
                    default=os.environ.get("CONNECTOR_URL") or DEMO_CONNECTOR_URL,
                    help=f"for richer error detail on failed orders "
                         f"(default: ${{CONNECTOR_URL}} env or {DEMO_CONNECTOR_URL})")
    # NOTE: --approve-timeout-sec retired with the approval flow itself.
    # Kept silently accepted for backward-compat in case agent harnesses
    # still pass it (argparse just ignores unknown args isn't true, so
    # we accept-and-discard explicitly).
    ap.add_argument("--approve-timeout-sec", type=int, default=600,
                    help=argparse.SUPPRESS)
    ap.add_argument("--settle-timeout-sec", type=int, default=180,
                    help="default 180s (3 min); fail fast on stuck settlement "
                         "rather than busy-waiting")
    ap.add_argument("--poll-interval-sec", type=int, default=5)
    args = ap.parse_args(argv)

    # ── Mode dispatch ───────────────────────────────────────────────────
    # Three modes — mutually exclusive. Buy mode is the default; the other
    # two are recovery flows after a buy.
    if args.use_token and args.return_token:
        print("ERROR: --use-token and --return-token are mutually exclusive", file=sys.stderr)
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

    # ── Buy mode (default) — original purchase pipeline ─────────────────
    missing = [
        name for name, val in [
            ("--agent-url", args.agent_url),
            ("--product-id", args.product_id),
            ("--quantity", args.quantity),
            ("--call-body", args.call_body),
        ] if val is None
    ]
    if missing:
        print(
            f"ERROR: buy mode requires {missing}.\n"
            f"   Or use --use-token <order_no> / --return-token <order_no> "
            f"for recovery flows.",
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

    # CRITICAL: validate --call-body against the tool's input_schema BEFORE
    # broadcasting any payment. Without this, a buyer agent that passes a
    # bad input (ENS name where 0x-hex is required, missing field, wrong
    # type) pays first, then gets HTTP 500 from the tool, then panic-retries
    # — paying twice for one valid result. See SKILL.md hard rule #1.
    schema_err = _prevalidate_call_body(args.agent_url, args.product_id, call_body)
    if schema_err:
        print(f"ERROR: {schema_err}", file=sys.stderr)
        print(
            "   No payment was made. Fix --call-body and retry.",
            file=sys.stderr,
        )
        return 1

    # Inflight guard: check for a recent purchase with the same fingerprint.
    # Trips when a buyer reruns buy.py after a network blip in pay_on_chain
    # — without this, each rerun creates a new order + new deposit address +
    # potentially a second on-chain tx. The fingerprint matches against the
    # FIRST token in the priority list, which is what the previous run
    # would have tried first (90%+ of cases). For multi-token fallback
    # scenarios, server-side semantic dedup is the safety net.
    if not args.force_new:
        for token_candidate in tokens:
            fp = _purchase_fingerprint(
                agent_url=args.agent_url, product_id=args.product_id,
                quantity=args.quantity, token=token_candidate, customer_id="agent",
            )
            inflight = _get_recent_inflight(fp)
            if not inflight:
                continue
            # Refuse + show recovery instructions. Keep the message dense
            # because the user is probably already panicking after a
            # failed pay.
            tx = inflight.get("tx_hash")
            print(
                "\n⚠️  In-flight purchase detected — refusing to create a "
                "duplicate.\n",
                file=sys.stderr,
            )
            print(f"   Order:        {inflight.get('order_no')}", file=sys.stderr)
            print(f"   Token:        {inflight.get('token')}", file=sys.stderr)
            print(f"   Amount:       {inflight.get('paying_amount')}", file=sys.stderr)
            print(f"   Deposit:      {inflight.get('deposit_address')}", file=sys.stderr)
            print(f"   Started:      {inflight.get('started_at')}", file=sys.stderr)
            if tx:
                print(f"   Tx hash:      {tx}", file=sys.stderr)
                print(
                    f"\n   Your previous transaction IS on-chain. "
                    f"Wait for settlement.\n"
                    f"   Check status: curl {args.connector_url.rstrip('/')}/orders/{inflight.get('order_no')}",
                    file=sys.stderr,
                )
            else:
                print("   Tx hash:      not yet broadcast", file=sys.stderr)
                print(
                    f"\n   Previous run may have crashed before broadcast. "
                    f"Verify in your wallet history.\n"
                    f"   - If NO tx was sent: pass --force-new to retry "
                    f"(allocates a fresh order_no).\n"
                    f"   - If a tx WAS sent: wait for it to settle, do NOT "
                    f"pass --force-new.",
                    file=sys.stderr,
                )
            print(
                f"\n   Inflight entry expires automatically after 30 min. "
                f"Override now with --force-new.",
                file=sys.stderr,
            )
            return 1

    try:
        for i, token in enumerate(tokens):
            if i > 0:
                print(
                    f"\n→ retrying with next token: {token} "
                    f"({i + 1}/{len(tokens)})",
                    file=sys.stderr,
                )
            elif len(tokens) > 1:
                print(
                    f"→ trying tokens in priority order: {' → '.join(tokens)}",
                    file=sys.stderr,
                )
            result = _try_one_token(token, args, call_body)
            if result is not None:
                return result  # success or non-recoverable failure
            # else: pre-payment failure for this token, fall through to next
        # All tokens exhausted at pre-payment stage.
        print(
            "\n⚠️  All tokens failed at pre-payment stage. "
            f"Tried {len(tokens)}: {tokens}",
            file=sys.stderr,
        )
        print(
            "   No money was sent on-chain. Likely causes: wallet underfunded "
            "across all tokens, OR the merchant doesn't accept any of them.",
            file=sys.stderr,
        )
        return 1

    except OrderCanceled as e:
        # OrderCanceled was historically raised by poll_approval (now
        # retired). Kept as a public exception type for backward compat
        # with anything importing the skill as a module — never raised
        # by current code paths.
        print(f"Order cancelled: {e}")
        return 0
    except WrongAgentUrlError as e:
        print(f"\n⚠️  Wrong --agent-url. {e}", file=sys.stderr)
        return 1
    except (OrderFailed, SettlementTimeout,
            PaymentNotConfirmed, ConfigError, UnexpectedState,
            ToolCallError, KeyError,
            _wallet_okx.CliError, _wallet_local.TxRevertedError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
