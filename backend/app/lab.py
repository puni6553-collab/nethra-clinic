"""Iteration 6, R3 — the CityCare Lab partner MCP connection.

CityCare Hospital Lab runs its own MCP server (data pack 6,
`partner-lab/citycare_lab_mcp.py`) that NetraCare does not own. At startup
this module tries to load its one tool, `get_lab_test_info`, with
`langchain-mcp-adapters` and keeps the result for the patient assistant.

When the partner's server is not running the chat still works: the tool
answers with the fixed `LAB_UNAVAILABLE` message, and `connected()` stays
False so the system prompt can say so. Nothing here ever raises into a
request — a partner outage is an ordinary result.
"""
from __future__ import annotations

import asyncio
import threading
import time
from typing import Any

from . import config

# Populated by connect(); read by the get_lab_test_info tool wrapper.
_lock = threading.Lock()
_tools: list[Any] = []
_connected = False
_error = ""
# Reconnect throttle: a lab server started after the API must not be
# re-probed on every message, and a lab that is down must not stall chat.
_last_attempt = 0.0
_cooldown = 0.0  # seconds to wait before the next reconnect attempt
_RECONNECT_COOLDOWN_OK = 2.0     # lab is up and healthy: brief pause
_RECONNECT_COOLDOWN_FAIL = 15.0  # lab down: back off so chat stays snappy


def connected() -> bool:
    with _lock:
        return _connected


def tools() -> list[Any]:
    """LangChain tools loaded from the lab server (empty when it is down)."""
    with _lock:
        return list(_tools)


def status() -> dict[str, Any]:
    with _lock:
        return {"connected": _connected, "error": _error, "url": config.LAB_MCP_URL}


async def connect(timeout: float | None = None) -> bool:
    """Load the lab's tools once, at startup. Never raises.

    Returns True when the partner server answered. Safe to call again: a
    successful connection is kept, a failed one is retried.
    """
    global _connected, _error, _tools
    with _lock:
        if _connected and _tools:
            return True

    limit = config.LAB_TIMEOUT_SECONDS if timeout is None else timeout
    try:
        from langchain_mcp_adapters.client import MultiServerMCPClient

        client = MultiServerMCPClient({
            "citycare": {
                "url": config.LAB_MCP_URL,
                "transport": "streamable_http",
            }
        })
        loaded = await asyncio.wait_for(client.get_tools(), timeout=limit)
    except Exception as exc:  # noqa: BLE001 — partner down is ordinary
        with _lock:
            _connected = False
            _error = str(exc)
            _tools = []
        print(f"[lab] CityCare Lab not reachable: {exc}", flush=True)
        return False

    with _lock:
        _tools = list(loaded or [])
        _connected = bool(_tools)
        _error = "" if _connected else "lab server returned no tools"
    if _connected:
        names = ", ".join(getattr(t, "name", "?") for t in _tools)
        print(f"[lab] CityCare Lab connected ({names})", flush=True)
    return _connected


def connect_blocking(timeout: float | None = None) -> bool:
    """For scripts and tests that are not already inside an event loop."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(connect(timeout))
    # Already on a loop (e.g. FastAPI lifespan) — caller should await connect().
    raise RuntimeError("connect_blocking() called from a running event loop; await connect() instead")


def _maybe_reconnect() -> None:
    """Throttled background reconnect when the partner server is down.

    The API may start before the lab (or the lab may restart). Without
    this, the first lab question would stay unavailable until the API
    restarts. Safe to call from any thread: never raises, never blocks
    the caller longer than the connect timeout, and backs off while the
    partner stays offline.
    """
    global _last_attempt, _cooldown
    now = time.monotonic()
    with _lock:
        if _connected and _tools:
            return
        if now - _last_attempt < _cooldown:
            return
        _last_attempt = now

    def _run() -> None:
        global _cooldown
        try:
            ok = asyncio.run(connect(timeout=config.LAB_TIMEOUT_SECONDS))
        except Exception:  # noqa: BLE001 — partner down is ordinary
            ok = False
        with _lock:
            _cooldown = _RECONNECT_COOLDOWN_OK if ok else _RECONNECT_COOLDOWN_FAIL

    threading.Thread(target=_run, daemon=True, name="lab-reconnect").start()


async def aquery(test: str = "") -> dict[str, Any]:
    """Call `get_lab_test_info` on the partner server (async — MCP tools
    do not support sync invoke).

    Returns the lab's payload under `info`, or the fixed unavailable
    message when the server is down / not yet connected. The tool itself
    never invents test data — that is the lab's job (it returns
    found: false for unknown tests). When the partner is down a throttled
    reconnect runs in the background, so starting the lab server after
    the API heals the chat without a restart.
    """
    global _connected, _error, _tools
    with _lock:
        connected_now = _connected
        tool_list = list(_tools)

    if not connected_now or not tool_list:
        _maybe_reconnect()
        return {
            "result": "unavailable",
            "message": config.LAB_UNAVAILABLE,
            "source": config.LAB_SOURCE,
        }

    tool = next(
        (t for t in tool_list if getattr(t, "name", "") == "get_lab_test_info"),
        None,
    )
    if tool is None:
        return {
            "result": "unavailable",
            "message": config.LAB_UNAVAILABLE,
            "source": config.LAB_SOURCE,
        }

    try:
        payload = await tool.ainvoke({"test": test or ""})
    except Exception as exc:  # noqa: BLE001 — treat a mid-run outage as down
        with _lock:
            _connected = False
            _error = str(exc)
            _tools = []
        print(f"[lab] get_lab_test_info failed: {exc}", flush=True)
        _maybe_reconnect()
        return {
            "result": "unavailable",
            "message": config.LAB_UNAVAILABLE,
            "source": config.LAB_SOURCE,
        }

    return {"result": "ok", "info": payload, "source": config.LAB_SOURCE}


def query(test: str = "") -> dict[str, Any]:
    """Sync wrapper around `aquery` for scripts that are not on a loop.

    The patient assistant's tool is async and awaits `aquery` directly;
    this path exists for tests and CLI probes.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(aquery(test))
    raise RuntimeError("query() called from a running event loop; await aquery() instead")
