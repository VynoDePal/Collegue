"""Regression tests for ToolsRegistry / lifespan-scoped tool discovery (#211).

Three things we need to guarantee:

1. ``discover_tools`` walks ``collegue.tools`` and surfaces every
   ``BaseTool`` subclass that currently ships with the project (including
   those re-exported from sub-packages such as ``collegue.tools.secret_scan.tool``).
2. ``ToolsRegistry`` only runs ``discover_tools`` once even when dozens of
   coroutines hit ``.get()`` concurrently during cold start — the previous
   ``_TOOLS_CACHE`` global could race.
3. The orchestrator handler reads the registry injected via
   ``ctx.lifespan_context["tools_registry"]`` and no longer depends on any
   module-level global.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

import collegue.core.meta_orchestrator as mo
from collegue.core.tools_registry import (
    ToolsRegistry,
    discover_tools,
)


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ---------------------------------------------------------------------------
# discover_tools
# ---------------------------------------------------------------------------

def test_discover_tools_returns_all_registered_tools():
    """Every shipping tool must be discoverable through the public API.

    The assertion is on a conservative subset — we just want to catch the
    class of bug where a new sub-package stops being picked up. The exact
    count is not frozen because the project keeps adding tools.
    """
    registry = discover_tools()

    expected_monolithic = {
        "github_ops",
        "sentry_monitor",
        "postgres_db",
        "kubernetes_ops",
    }
    expected_modular = {
        "secret_scan",
        "dependency_guard",
        "iac_guardrails_scan",
        "repo_consistency_check",
        "impact_analysis",
        "code_documentation",
        "test_generation",
        "code_refactoring",
    }

    missing_mono = expected_monolithic - registry.keys()
    assert not missing_mono, f"Monolithic tools missing: {missing_mono}"

    # Modular tools may fail to import on Python < 3.12 (PEP 701 fstring with
    # backslashes in repo_consistency_check). Tolerate up to 2 absences so
    # the test is portable across supported Python versions.
    missing_modular = expected_modular - registry.keys()
    assert len(missing_modular) <= 2, (
        f"Too many modular tools missing: {missing_modular}. "
        f"The startswith(module.__name__) fix should surface most of "
        f"{expected_modular}."
    )


def test_discovered_entry_has_expected_shape():
    """Every entry must expose the 4 fields the orchestrator renders."""
    registry = discover_tools()
    assert registry, "discovery returned an empty dict — check the traversal"

    # Pick any entry (secret_scan almost certainly present) and verify shape.
    sample_name = next(iter(registry))
    entry = registry[sample_name]
    assert "class" in entry
    assert "description" in entry
    assert "prompt_desc" in entry
    assert "schema" in entry
    assert sample_name in entry["prompt_desc"]  # name must be in the prompt


def test_discover_does_not_include_smart_orchestrator():
    """The orchestrator must not recommend itself — infinite loop risk."""
    registry = discover_tools()
    assert "smart_orchestrator" not in registry


# ---------------------------------------------------------------------------
# ToolsRegistry — concurrency + caching
# ---------------------------------------------------------------------------

def test_registry_returns_injected_dict_without_calling_discovery(monkeypatch):
    """An initial dict is returned as-is; discover_tools is NOT called."""
    called = {"n": 0}

    def _fail_if_called():
        called["n"] += 1
        return {}

    monkeypatch.setattr(
        "collegue.core.tools_registry.discover_tools", _fail_if_called
    )

    registry = ToolsRegistry(initial={"fake": {"prompt_desc": "fake desc"}})
    result = _run(registry.get())
    assert result == {"fake": {"prompt_desc": "fake desc"}}
    assert called["n"] == 0


def test_registry_cold_start_triggers_discovery_exactly_once(monkeypatch):
    """``asyncio.gather`` on an empty registry: N waiters, 1 discovery."""
    called = {"n": 0}

    def _count_discovery():
        called["n"] += 1
        # Returning a predictable shape so we can assert callers see it.
        return {"probe_tool": {"prompt_desc": "probe"}}

    monkeypatch.setattr(
        "collegue.core.tools_registry.discover_tools", _count_discovery
    )

    registry = ToolsRegistry()

    async def _hammer(n: int):
        return await asyncio.gather(*[registry.get() for _ in range(n)])

    results = _run(_hammer(30))

    assert called["n"] == 1, (
        f"discover_tools was called {called['n']} times for 30 concurrent "
        f"waiters — the lock around cold-start initialisation is broken."
    )
    for r in results:
        assert r == {"probe_tool": {"prompt_desc": "probe"}}


def test_registry_clear_forces_fresh_discovery(monkeypatch):
    """``clear()`` invalidates the cache and lets the next ``get()`` rediscover."""
    calls = []

    def _tick():
        calls.append(None)
        return {"snap": {"prompt_desc": f"tick-{len(calls)}"}}

    monkeypatch.setattr(
        "collegue.core.tools_registry.discover_tools", _tick
    )

    registry = ToolsRegistry()
    _run(registry.get())   # first discovery
    _run(registry.get())   # cached
    registry.clear()
    _run(registry.get())   # second discovery

    assert len(calls) == 2


# ---------------------------------------------------------------------------
# meta_orchestrator uses the lifespan-injected registry
# ---------------------------------------------------------------------------

def test_orchestrator_reads_registry_from_lifespan_context():
    """The handler must NOT fall back to discovery when ``ctx.lifespan_context``
    already carries a ``tools_registry``."""
    # Register the orchestrator on a stub app to capture the handler coroutine.
    app = MagicMock()
    mo.register_meta_orchestrator(app)
    handler = app.tool.return_value.call_args[0][0]

    # A stub tool the plan will reference
    class _StubTool:
        def __init__(self, *_a, **_k):
            pass

        def get_request_model(self):
            from pydantic import BaseModel
            class _R(BaseModel):
                model_config = {"extra": "allow"}
            return _R

        async def execute_async(self, request, **kwargs):
            class _Res:
                def dict(self_inner):
                    return {"ok": True}
            return _Res()

    injected = {
        "fake_tool": {
            "class": _StubTool,
            "description": "fake",
            "prompt_desc": "fake_tool: fake\n  Arguments: none",
            "schema": {},
        }
    }

    ctx = MagicMock()
    ctx.lifespan_context = {"tools_registry": injected}
    ctx.info = AsyncMock()
    ctx.warning = AsyncMock()
    ctx.error = AsyncMock()
    ctx.sample = AsyncMock()

    plan_resp = MagicMock()
    plan_resp.result = mo.OrchestratorPlan(steps=[
        mo.OrchestratorStep(tool="fake_tool", reason="t", params={})
    ])
    synth_resp = MagicMock(); synth_resp.text = "done"
    ctx.sample.side_effect = [plan_resp, synth_resp]

    response = _run(handler(mo.OrchestratorRequest(query="hello"), ctx))
    assert response.tools_used == ["fake_tool"]


def test_orchestrator_no_longer_imports_global_tools_cache():
    """Guard against regressions: the module must not expose ``_TOOLS_CACHE``
    anymore. If someone reintroduces it, this test fails loudly."""
    assert not hasattr(mo, "_TOOLS_CACHE"), (
        "Module-level _TOOLS_CACHE is back — revert to the lifespan-scoped "
        "registry (see #211) instead of reintroducing a global mutable."
    )
    assert not hasattr(mo, "_MAX_TOOLS_CACHE_SIZE")
    assert not hasattr(mo, "_TOOLS_CACHE_TTL")
