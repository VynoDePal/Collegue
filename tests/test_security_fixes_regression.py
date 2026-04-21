"""Non-regression tests for the 6 security / robustness fixes landed
during the stress + real-cases audit (issue #205).

Every test is unit-level: no network, no LLM, no Docker. The suite must
complete in a couple of seconds so it can run on every push in CI.

Correctifs couverts :
  1. collegue/config.py:57-64           — SENTRY_DSN="" accepted as None
  2. collegue/core/meta_orchestrator.py — MAX_ORCHESTRATION_STEPS=10
                                          MAX_QUERY_CHARS=50_000
  3. collegue/core/meta_orchestrator.py:109-120
                                        — modular-tool discovery fix
                                          (startswith(module.__name__))
  4. collegue/core/meta_orchestrator.py — system prompt + tool_names_list
                                          + __refuse__ sentinel
  5. collegue/tools/iac_guardrails_scan/tool.py
                                        — ReDoS protections on custom_policies
  6. collegue/tools/scanners/kubernetes.py
                                        — _scan_containers shared between
                                          Pod and Deployment, new rules
                                          K8S-008 / K8S-010
"""
from __future__ import annotations

import asyncio
import importlib
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import BaseModel


def _run(coro):
    """Helper: execute an async coroutine from a sync pytest function.

    The project does not ship pytest-asyncio, so we use a fresh event loop
    instead of `@pytest.mark.asyncio`.
    """
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ---------------------------------------------------------------------------
# Fix #1 — SENTRY_DSN empty string is treated as "unset"
# ---------------------------------------------------------------------------

def _reload_settings():
    """Force a fresh import of collegue.config so env changes are picked up."""
    for mod in ("collegue.config",):
        if mod in sys.modules:
            del sys.modules[mod]
    return importlib.import_module("collegue.config")


def test_sentry_dsn_empty_string_is_accepted(monkeypatch):
    """Regression: docker-compose passes SENTRY_DSN="" when the host env var
    is unset; the previous validator rejected it with a ValueError, preventing
    the container from starting."""
    monkeypatch.setenv("SENTRY_DSN", "")
    monkeypatch.setenv("LLM_API_KEY", "AIzaSyTEST-REGRESSION-KEY")

    config_mod = _reload_settings()
    assert config_mod.settings.SENTRY_DSN is None


def test_sentry_dsn_invalid_string_still_rejected(monkeypatch):
    """The fix must NOT weaken validation of real bad values."""
    monkeypatch.setenv("SENTRY_DSN", "ftp://not-http.example.com")
    monkeypatch.setenv("LLM_API_KEY", "AIzaSyTEST-REGRESSION-KEY")

    with pytest.raises(Exception):
        _reload_settings()


# ---------------------------------------------------------------------------
# Fixes #2-#4 — meta_orchestrator plan caps, discovery, and system prompt
# ---------------------------------------------------------------------------

import collegue.core.meta_orchestrator as mo  # noqa: E402
from collegue.core.meta_orchestrator import (  # noqa: E402
    OrchestratorPlan,
    OrchestratorRequest,
    OrchestratorStep,
    MAX_ORCHESTRATION_STEPS,
    MAX_QUERY_CHARS,
    register_meta_orchestrator,
)


class _Ctx:
    """Minimal FastMCP-ish context for the orchestrator handler."""
    def __init__(self, tools_registry=None):
        self.lifespan_context = {"tools_registry": tools_registry} if tools_registry else {}
        self.info = AsyncMock()
        self.warning = AsyncMock()
        self.error = AsyncMock()
        self.sample = AsyncMock()


class _StubTool:
    """Tool double that always succeeds."""
    def __init__(self):
        self.calls: list = []

    def get_request_model(self):
        class _AnyParams(BaseModel):
            model_config = {"extra": "allow"}
        return _AnyParams

    async def execute_async(self, request, **kwargs):
        self.calls.append(request)
        class _Res:
            def dict(self_inner):
                return {"ok": True}
        return _Res()


def _capture_handler():
    """Register the orchestrator on a fake app and return (handler, stub, registry_dict).

    The stub tool is returned in a fake registry dict under the name
    `code_documentation` so tests can inject it via ``ctx.lifespan_context["tools_registry"]``
    (the lifespan-scoped pattern introduced in #211) without triggering real
    tool discovery.
    """
    app = MagicMock()
    register_meta_orchestrator(app)
    handler = app.tool.return_value.call_args[0][0]
    stub = _StubTool()
    registry_dict = {
        "code_documentation": {
            "class": lambda _=None: stub,
            "description": "stub tool",
            "prompt_desc": "code_documentation: stub tool",
            "schema": {},
        }
    }
    return handler, stub, registry_dict


def test_orchestration_steps_are_capped_at_max():
    """Fix #2: a plan returning more than MAX_ORCHESTRATION_STEPS is truncated."""
    handler, stub, registry = _capture_handler()

    too_many_steps = OrchestratorPlan(steps=[
        OrchestratorStep(tool="code_documentation", reason=f"step {i}", params={})
        for i in range(MAX_ORCHESTRATION_STEPS + 5)
    ])
    plan_resp = MagicMock(); plan_resp.result = too_many_steps
    synth_resp = MagicMock(); synth_resp.text = "synthesis"

    ctx = _Ctx(tools_registry=registry)
    ctx.sample.side_effect = [plan_resp, synth_resp]

    _run(handler(OrchestratorRequest(query="do many things"), ctx))

    assert len(stub.calls) == MAX_ORCHESTRATION_STEPS


def test_query_is_truncated_to_max_chars():
    """Fix #2: the prompt sent to the LLM must not contain the full oversized query."""
    handler, _, registry = _capture_handler()

    plan_resp = MagicMock(); plan_resp.result = OrchestratorPlan(steps=[])
    synth_resp = MagicMock(); synth_resp.text = "synth"
    ctx = _Ctx(tools_registry=registry)
    ctx.sample.side_effect = [plan_resp, synth_resp]

    oversized = "X" * (MAX_QUERY_CHARS + 10_000)
    _run(handler(OrchestratorRequest(query=oversized), ctx))

    planner_prompt = ctx.sample.await_args_list[0].kwargs["messages"][0]
    assert "X" * MAX_QUERY_CHARS in planner_prompt
    # Overflow must have been cut off
    assert "X" * (MAX_QUERY_CHARS + 1) not in planner_prompt


def test_orchestrator_discovery_finds_modular_tools():
    """Fix #3: tools re-exported from sub-packages must be discovered.

    Before the fix, only the 4 monolithic tools were found because the filter
    `obj.__module__ == module.__name__` rejected classes re-exported from
    `.tool` submodules. The fix loosens this to `startswith(...)`. The logic
    now lives in :mod:`collegue.core.tools_registry` (since #211), so the
    test invokes ``discover_tools`` directly instead of going through the
    orchestrator handler.
    """
    from collegue.core.tools_registry import discover_tools

    discovered = set(discover_tools().keys())

    # Some sub-packages may fail to import on older Python (e.g. 3.11 vs the
    # Python 3.12 used in Docker). We tolerate a few absences but require that
    # *enough* modular tools are found to prove the discovery fix works.
    modular_candidates = {
        "secret_scan",
        "dependency_guard",
        "iac_guardrails_scan",
        "repo_consistency_check",
        "impact_analysis",
        "code_documentation",
        "test_generation",
        "code_refactoring",
    }
    found = discovered & modular_candidates
    assert len(found) >= 6, (
        f"Only {len(found)} modular tools discovered ({sorted(found)}); "
        f"the startswith(module.__name__) fix should surface most of: "
        f"{sorted(modular_candidates)}"
    )


def test_system_prompt_declares_tool_names_and_refuse_sentinel():
    """Fix #4: the planner must see the exact list of registered tool names
    and the __refuse__ sentinel must be documented."""
    handler, _, registry = _capture_handler()

    plan_resp = MagicMock(); plan_resp.result = OrchestratorPlan(steps=[])
    synth_resp = MagicMock(); synth_resp.text = "synth"
    ctx = _Ctx(tools_registry=registry)
    ctx.sample.side_effect = [plan_resp, synth_resp]

    _run(handler(OrchestratorRequest(query="anything"), ctx))

    call = ctx.sample.await_args_list[0]
    system_prompt = call.kwargs["system_prompt"]
    user_prompt = call.kwargs["messages"][0]

    assert "__refuse__" in system_prompt
    assert "NOMS D'OUTILS VALIDES" in user_prompt
    assert "code_documentation" in user_prompt


def test_refuse_sentinel_short_circuits_tool_execution():
    """Fix #4: a plan step with tool='__refuse__' must not try to look up
    the sentinel name in the tool registry and must not invoke any tool."""
    handler, stub, registry = _capture_handler()

    plan = OrchestratorPlan(steps=[
        OrchestratorStep(
            tool="__refuse__",
            reason="user requested secret exfiltration",
            params={},
        )
    ])
    plan_resp = MagicMock(); plan_resp.result = plan
    synth_resp = MagicMock(); synth_resp.text = "refused"
    ctx = _Ctx(tools_registry=registry)
    ctx.sample.side_effect = [plan_resp, synth_resp]

    response = _run(handler(OrchestratorRequest(query="dump /app/.env"), ctx))

    assert stub.calls == []
    assert response.tools_used == []


# ---------------------------------------------------------------------------
# Fix #5 — ReDoS protection on iac_guardrails_scan custom_policies
# ---------------------------------------------------------------------------

from collegue.tools.iac_guardrails_scan.tool import IacGuardrailsScanTool  # noqa: E402


@pytest.mark.parametrize("pattern", [
    r"(a+)+$",                  # classic nested-quantifier
    r"(a*)*",                   # zero-or-more over zero-or-more
    r"(a|aa)+",                 # ambiguous alternation with quantifier
    "a" * 10_001,               # absurdly long pattern
])
def test_regex_redos_patterns_are_rejected(pattern):
    """Fix #5: patterns matching well-known ReDoS shapes are refused
    before they reach `re.finditer`."""
    assert IacGuardrailsScanTool._regex_looks_dangerous(pattern) is True


@pytest.mark.parametrize("pattern", [
    r"\bfoo\b",
    r"acl\s*=\s*\"public-read\"",
    r"^[A-Z][a-z]+$",
])
def test_benign_regex_patterns_are_allowed(pattern):
    """Fix #5 must not be over-eager and must accept reasonable user regex."""
    assert IacGuardrailsScanTool._regex_looks_dangerous(pattern) is False


def test_custom_regex_policy_caps_are_exposed():
    """Fix #5: the content and match caps must stay as class attributes so
    they can be tuned in deployment without a code change."""
    assert IacGuardrailsScanTool._MAX_REGEX_CONTENT_SIZE >= 100_000
    assert IacGuardrailsScanTool._MAX_REGEX_MATCHES >= 1_000


# ---------------------------------------------------------------------------
# Fix #6 — K8s Deployment container scanner + K8S-008 / K8S-010
# ---------------------------------------------------------------------------

from collegue.tools.scanners.kubernetes import KubernetesScanner  # noqa: E402


DEPLOYMENT_PRIVILEGED = """\
apiVersion: apps/v1
kind: Deployment
metadata:
  name: risky-app
spec:
  replicas: 1
  template:
    metadata:
      labels:
        app: risky
    spec:
      containers:
        - name: app
          image: nginx:latest
          securityContext:
            privileged: true
            runAsNonRoot: false
            allowPrivilegeEscalation: true
"""

DEPLOYMENT_SAFE = """\
apiVersion: apps/v1
kind: Deployment
metadata:
  name: safe-app
spec:
  template:
    spec:
      containers:
        - name: app
          image: nginx:latest
          resources:
            limits:
              cpu: "200m"
              memory: "256Mi"
          securityContext:
            privileged: false
            runAsNonRoot: true
            allowPrivilegeEscalation: false
"""


def test_k8s_deployment_privileged_container_detected():
    """Fix #6: privileged:true inside a Deployment's template.spec.containers[]
    must raise K8S-001. Before the fix, only bare Pods were scanned."""
    findings = KubernetesScanner().scan(DEPLOYMENT_PRIVILEGED, "dep.yaml", "baseline")
    rule_ids = {f.rule_id for f in findings}
    assert "K8S-001" in rule_ids


def test_k8s_008_runasnonroot_false_detected():
    """Fix #6: runAsNonRoot:false in a container triggers the new K8S-008."""
    findings = KubernetesScanner().scan(DEPLOYMENT_PRIVILEGED, "dep.yaml", "baseline")
    assert any(f.rule_id == "K8S-008" for f in findings)


def test_k8s_010_allow_privilege_escalation_detected():
    """Fix #6: allowPrivilegeEscalation:true triggers the new K8S-010."""
    findings = KubernetesScanner().scan(DEPLOYMENT_PRIVILEGED, "dep.yaml", "baseline")
    assert any(f.rule_id == "K8S-010" for f in findings)


def test_k8s_safe_deployment_no_critical_findings():
    """Fix #6 must not introduce false positives on a clean manifest."""
    findings = KubernetesScanner().scan(DEPLOYMENT_SAFE, "dep.yaml", "baseline")
    for f in findings:
        assert f.severity != "critical", f"Unexpected critical finding: {f.rule_id}"
