"""Tests H6 (#397) : outil MCP du pilote, garde stricte (off, OAuth requis, allowlist)."""

from types import SimpleNamespace

import pytest

from collegue.pilot.mcp_tool import (
    PILOT_TOOL_NAME,
    PilotToolError,
    PilotToolRequest,
    PilotToolResult,
    caller_allowed,
    evaluate_pilot_gate,
    register_pilot_tool,
    run_pilot_tool,
)


def _settings(**kw):
    base = dict(PILOT_TOOL_ENABLED=False, OAUTH_ENABLED=False, PILOT_TOOL_ALLOWED_SUBJECTS="")
    base.update(kw)
    return SimpleNamespace(**base)


def _req(**kw):
    base = dict(project_id=1, repo_source="/repo", owner="o", repo="r")
    base.update(kw)
    return PilotToolRequest(**base)


# --- gate -----------------------------------------------------------------------


def test_gate_off_by_default():
    g = evaluate_pilot_gate(_settings())
    assert g.allowed is False and g.misconfigured is False


def test_gate_enabled_without_oauth_is_misconfigured():
    g = evaluate_pilot_gate(_settings(PILOT_TOOL_ENABLED=True, OAUTH_ENABLED=False))
    assert g.allowed is False and g.misconfigured is True


def test_gate_enabled_with_oauth_allowed():
    g = evaluate_pilot_gate(_settings(PILOT_TOOL_ENABLED=True, OAUTH_ENABLED=True))
    assert g.allowed is True


def test_gate_string_false_is_not_enabled():
    # bool("false") vaut True en Python : une chaîne « false » ne doit PAS activer l'outil.
    g = evaluate_pilot_gate(_settings(PILOT_TOOL_ENABLED="false", OAUTH_ENABLED="false"))
    assert g.allowed is False and g.misconfigured is False
    # Et « true » en chaîne + OAuth absent → misconfiguré (refus dur), pas activé.
    g2 = evaluate_pilot_gate(_settings(PILOT_TOOL_ENABLED="true", OAUTH_ENABLED="false"))
    assert g2.allowed is False and g2.misconfigured is True


# --- caller allowlist (fail-closed) ---------------------------------------------


def test_caller_empty_allowlist_rejects_everyone():
    assert caller_allowed("alice", _settings()) is False  # allowlist vide → personne


def test_caller_must_be_in_allowlist():
    s = _settings(PILOT_TOOL_ALLOWED_SUBJECTS="alice, bob")
    assert caller_allowed("alice", s) is True
    assert caller_allowed("carol", s) is False
    assert caller_allowed(None, s) is False
    assert caller_allowed("", s) is False


# --- run_pilot_tool -------------------------------------------------------------


async def _fake_run(request, *, ctx, settings):
    return SimpleNamespace(stop_reason="completed", iterations=2, opened_prs=[101], project_status="improving")


async def test_run_refused_when_gate_off():
    with pytest.raises(PilotToolError):
        await run_pilot_tool(_req(), subject="alice", settings=_settings(), run_fn=_fake_run)


async def test_run_refused_when_caller_not_allowed():
    s = _settings(PILOT_TOOL_ENABLED=True, OAUTH_ENABLED=True, PILOT_TOOL_ALLOWED_SUBJECTS="bob")
    with pytest.raises(PilotToolError):
        await run_pilot_tool(_req(), subject="alice", settings=s, run_fn=_fake_run)


async def test_run_allowed_caller_executes_and_defaults_dry_run():
    s = _settings(PILOT_TOOL_ENABLED=True, OAUTH_ENABLED=True, PILOT_TOOL_ALLOWED_SUBJECTS="alice")
    audit = SimpleNamespace(events=[], record=lambda kind, **d: audit.events.append((kind, d)))
    res = await run_pilot_tool(_req(), subject="alice", settings=s, run_fn=_fake_run, audit=audit)
    assert isinstance(res, PilotToolResult)
    assert res.stop_reason == "completed" and res.opened_prs == [101] and res.dry_run is True
    assert audit.events and audit.events[0][0] == "pilot_tool_invoked"


async def test_run_passes_explicit_no_dry_run():
    s = _settings(PILOT_TOOL_ENABLED=True, OAUTH_ENABLED=True, PILOT_TOOL_ALLOWED_SUBJECTS="alice")
    seen = {}

    async def capture(request, *, ctx, settings):
        seen["dry_run"] = request.dry_run
        return SimpleNamespace(stop_reason="paused_budget", iterations=1, opened_prs=[], project_status=None)

    res = await run_pilot_tool(_req(dry_run=False), subject="alice", settings=s, run_fn=capture)
    assert seen["dry_run"] is False and res.dry_run is False


def test_request_defaults_dry_run_true():
    assert _req().dry_run is True


# --- register_pilot_tool --------------------------------------------------------


class _App:
    def __init__(self):
        self.registered = []

    def tool(self, *, name, description):
        self.registered.append(name)

        def deco(fn):
            return fn

        return deco


def test_register_skips_when_disabled():
    app = _App()
    assert register_pilot_tool(app, _settings()) is False
    assert app.registered == []  # outil absent par défaut


def test_register_raises_when_enabled_without_oauth():
    # PILOT_TOOL_ENABLED=true + OAUTH_ENABLED=false → refus DUR (ne démarre pas).
    app = _App()
    with pytest.raises(RuntimeError):
        register_pilot_tool(app, _settings(PILOT_TOOL_ENABLED=True, OAUTH_ENABLED=False))
    assert app.registered == []


def test_register_when_enabled_with_oauth():
    app = _App()
    assert register_pilot_tool(app, _settings(PILOT_TOOL_ENABLED=True, OAUTH_ENABLED=True)) is True
    assert app.registered == [PILOT_TOOL_NAME]
