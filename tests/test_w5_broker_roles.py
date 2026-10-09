"""Mode ``budget_broker`` pour les producteurs hors worker (ctx offline, handler FastMCP), le démarrage serveur et le runner.

Tous les rôles passent par le MÊME courtier et le MÊME scope global que les workers : aucun client réseau, aucune clé dans le
contexte appelant, un contexte sans registre lié n'est jamais une exemption.
"""

from __future__ import annotations

import sys
import types
from types import SimpleNamespace

import openai
import pytest
from fastmcp import Client, Context, FastMCP
from w5_broker_support import FakeUpstream, google_response, http_error

from collegue.broker import BrokerConfig
from collegue.broker.errors import BrokerForbidden
from collegue.broker.runtime import (
    BrokerConfigurationError,
    BrokerRuntime,
    install_runtime_for_tests,
    validate_broker_settings,
)
from collegue.core.llm.budget_guard import bind_budget
from collegue.core.llm.client import accounted_sample, model_preferences_for_role
from collegue.core.llm.sampling_ctx import LocalSamplingContext
from collegue.core.llm.sampling_handler import build_routing_sampling_handler
from collegue.state import ProjectStateManager

GOOGLE_KEY = "AIzaFAKE-w5-roles-key-0002"


def settings(**extra):
    base = dict(
        LLM_PROVIDER="gemini",
        LLM_MODEL="gemma-4-31b-it",
        LLM_API_KEY=GOOGLE_KEY,
        LLM_TRANSPORT="budget_broker",
        LLM_CALL_TIMEOUT=0,
        MAX_COST_USD=2.0,
        MAX_TOKENS_BUDGET=250000,
    )
    base.update(extra)
    return SimpleNamespace(**base)


@pytest.fixture
def stack(tmp_path, monkeypatch):
    import fastmcp.client.sampling.handlers.openai  # noqa: F401  (annotations évaluées avant tout remplacement)

    manager = ProjectStateManager.from_url(f"sqlite:///{tmp_path / 'w5.db'}", create=True)
    upstream = FakeUpstream()
    runtime = BrokerRuntime(upstream=upstream, config=BrokerConfig(), run_root=str(tmp_path / "run"))
    install_runtime_for_tests(runtime)
    ledger = manager.budget_ledger
    scope = ledger.scope_for_project(
        manager.create_project(name="roles"), max_cost_usd=2.0, max_tokens=250_000
    ).scope_key

    def no_network_client(*a, **k):
        raise AssertionError("aucun client réseau ne doit être construit en mode courtier")

    monkeypatch.setattr(openai, "AsyncOpenAI", no_network_client)
    monkeypatch.setattr("collegue.monitoring.metrics.enforce_budget", lambda *a, **k: None)
    yield SimpleNamespace(manager=manager, upstream=upstream, runtime=runtime, ledger=ledger, scope=scope)
    install_runtime_for_tests(None)


# ── contexte offline ─────────────────────────────────────────────────────────────────────────────────────────


async def test_every_role_spends_in_the_global_scope_through_the_broker_without_any_network_client(stack):
    cfg = settings()
    ctx = LocalSamplingContext.from_settings(cfg)
    with bind_budget(stack.ledger, stack.scope, settings=cfg):
        for role in ("planner", "qa", "reviewer", "default"):
            result = await accounted_sample(
                ctx,
                role=role,
                operation="w5",
                settings_obj=cfg,
                messages=f"bonjour {role}",
                max_tokens=64,
                model_preferences=model_preferences_for_role(role, cfg),
            )
            assert result.text == "ok"
    project = stack.ledger.snapshot(stack.scope)
    assert project.consumed_tokens == 4 * 15 and project.reserved_tokens == 0
    assert len(stack.upstream.generate_calls) == 4
    assert {c["body"]["model"] for c in stack.upstream.generate_calls} == {"models/gemma-4-31b-it"}
    assert all(c["body"]["generationConfig"]["maxOutputTokens"] == 64 for c in stack.upstream.generate_calls)


async def test_a_context_without_a_bound_registry_is_never_an_exemption(stack):
    cfg = settings()
    ctx = LocalSamplingContext.from_settings(cfg)
    with pytest.raises(BrokerForbidden) as caught:
        await ctx.sample("x", model_preferences=model_preferences_for_role("qa", cfg), max_tokens=32)
    assert caught.value.code == "no_budget_context" and stack.upstream.count_calls == []


async def test_an_explicit_output_limit_above_the_ceiling_is_refused_not_clamped_but_the_contexts_own_default_is_bounded(
    stack,
):
    cfg = settings()
    ctx = LocalSamplingContext.from_settings(cfg)
    with bind_budget(stack.ledger, stack.scope, settings=cfg):
        with pytest.raises(Exception) as caught:
            await ctx.sample("x", model_preferences=model_preferences_for_role("qa", cfg), max_tokens=20000)
        assert "output_limit_exceeds_ceiling" in str(getattr(caught.value, "code", "")) or "plafond" in str(
            caught.value
        )
        assert stack.upstream.count_calls == []
        await ctx.sample(
            "x", model_preferences=model_preferences_for_role("qa", cfg)
        )  # défaut du contexte, borné au plafond
    assert stack.upstream.generate_calls[0]["body"]["generationConfig"]["maxOutputTokens"] == 8192


async def test_the_fallback_model_is_not_reachable_by_non_coder_roles(stack):
    cfg = settings(LLM_MODEL_QA="gemma-4-26b-a4b-it")
    with pytest.raises(BrokerConfigurationError, match="réservé au codeur"):
        validate_broker_settings(cfg)


async def test_a_blocked_project_stops_every_producer_before_any_provider_call(stack):
    cfg = settings()
    stack.upstream.generate_error = http_error(503)
    ctx = LocalSamplingContext.from_settings(cfg)
    with bind_budget(stack.ledger, stack.scope, settings=cfg):
        with pytest.raises(Exception):
            await ctx.sample("x", model_preferences=model_preferences_for_role("planner", cfg), max_tokens=32)
        stack.upstream.generate_error = None
        with pytest.raises(Exception):
            await ctx.sample("x", model_preferences=model_preferences_for_role("reviewer", cfg), max_tokens=32)
    assert stack.ledger.snapshot(stack.scope).blocked and len(stack.upstream.generate_calls) == 1


# ── handler serveur FastMCP (vraie entrée Context.sample) ────────────────────────────────────────────────────


async def test_the_fastmcp_server_handler_uses_the_broker_for_every_role_and_counts_once(stack):
    cfg = settings()
    handler = build_routing_sampling_handler(cfg)
    app = FastMCP("w5-roles", sampling_handler=handler, sampling_handler_behavior="fallback", tasks=False)

    @app.tool
    async def ask(role: str, ctx: Context) -> str:
        with bind_budget(stack.ledger, stack.scope, settings=cfg):
            result = await ctx.sample(
                messages=f"q-{role}", max_tokens=32, model_preferences=model_preferences_for_role(role, cfg)
            )
        return result.text or ""

    outcomes = []
    async with Client(app, timeout=20) as client:
        for role in ("planner", "qa", "default"):
            result = await client.call_tool("ask", {"role": role}, raise_on_error=False)
            outcomes.append(None if not result.is_error else result.content)
    assert outcomes == [None, None, None]
    assert stack.ledger.snapshot(stack.scope).consumed_tokens == 3 * 15  # UNE réservation par appel, pas deux
    assert len(stack.upstream.generate_calls) == 3


async def test_the_fastmcp_handler_refuses_without_a_bound_registry(stack):
    cfg = settings()
    app = FastMCP(
        "w5-roles",
        sampling_handler=build_routing_sampling_handler(cfg),
        sampling_handler_behavior="fallback",
        tasks=False,
    )

    @app.tool
    async def ask(ctx: Context) -> str:
        return (
            await ctx.sample(messages="q", max_tokens=16, model_preferences=model_preferences_for_role("qa", cfg))
        ).text or ""

    async with Client(app, timeout=20) as client:
        result = await client.call_tool("ask", {}, raise_on_error=False)
    assert result.is_error
    assert "no_budget_context" in repr(result.content) or "registre budgétaire" in repr(result.content)
    assert stack.upstream.count_calls == []


# ── contrat de configuration ─────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "extra, fragment",
    [
        (dict(LLM_PROVIDER="openai", LLM_MODEL="gpt-5.4"), "Google"),
        (dict(LLM_MODEL="gemini-2.5-flash"), "non autorisé"),
        (dict(LLM_MODEL="gemma-4-31b-it", LLM_MODEL_PLANNER="gemma-3-27b-it"), "non autorisé"),
        (dict(LLM_PROVIDER_QA="openai"), "seul Google"),
        (dict(CODER_FALLBACK_MODELS="gemma-4-31b-it"), "seul gemma-4-26b-a4b-it"),
        (dict(CODER_FALLBACK_MODELS="gemini-2.5-flash"), "seul gemma-4-26b-a4b-it"),
        (dict(CODER_SUBSCRIPTION=True), "substitution"),
        (dict(LLM_AUTH_QA="subscription"), "clé API"),
        (dict(LLM_BASE_URL="https://proxy.example/v1"), "fixe"),
        (dict(LLM_BASE_URL_CODER="https://proxy.example/v1"), "fixe"),
        (dict(LLM_TRANSPORT="direct"), "n'est pas"),
    ],
)
def test_the_broker_contract_refuses_anything_but_google_and_the_two_official_gemma(extra, fragment):
    with pytest.raises(BrokerConfigurationError, match=fragment):
        validate_broker_settings(settings(**extra))


def test_the_valid_contract_is_accepted_with_the_coder_fallback():
    validate_broker_settings(settings(CODER_FALLBACK_MODELS="gemma-4-26b-a4b-it", LLM_MODEL_CODER="gemma-4-31b-it"))


# ── démarrage du vrai serveur ────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "env, ok, fragment",
    [
        (
            {
                "LLM_TRANSPORT": "budget_broker",
                "LLM_PROVIDER": "gemini",
                "LLM_MODEL": "gemma-4-31b-it",
                "LLM_API_KEY": "k",
            },
            True,
            "",
        ),
        (
            {
                "LLM_TRANSPORT": "budget_broker",
                "LLM_PROVIDER": "gemini",
                "LLM_MODEL": "gemini-2.5-flash",
                "LLM_API_KEY": "k",
            },
            False,
            "courtier",
        ),
        (
            {"LLM_TRANSPORT": "budget_broker", "LLM_PROVIDER": "openai", "LLM_MODEL": "gpt-5.4", "LLM_API_KEY": "k"},
            False,
            "courtier",
        ),
    ],
    ids=["valid", "non-official-model", "non-google"],
)
def test_the_real_application_startup_validates_the_broker_contract_locally(tmp_path, env, ok, fragment):
    from test_w4_routing_app import run_app

    report = run_app(tmp_path, env)
    assert report["requests"] == []  # aucune émission au démarrage, jamais
    if ok:
        assert report["startup_error"] is None and report["handler"] is True
    else:
        assert report["startup_error"]["type"] == "ValueError" and fragment in report["startup_error"]["message"]
        assert report["handler"] is False


# ── runner : relais embarqué lancé par oh_runner ─────────────────────────────────────────────────────────────


def test_oh_runner_starts_the_embedded_relay_and_reaches_the_broker_with_the_session_token(stack, monkeypatch):
    from test_w4_routing_sdk import SDK_1_19_1_LLM_FIELDS, StrictLLM

    from collegue.broker.server import BrokerSocketServer
    from collegue.executor import oh_runner

    parent = stack.ledger.reserve(
        stack.scope, micro_usd=1000, tokens=100_000, kind="worker", role="coder", transport="worker"
    )
    service = stack.runtime.service_for(stack.ledger)
    session = service.open_session(
        parent_scope_key=stack.scope, parent_reservation_id=parent.reservation_id, role="coder"
    )
    server = BrokerSocketServer(service, session.session_id, run_root=str(stack.runtime.run_root)).start()
    answers = []
    StrictLLM.instances, StrictLLM.login_calls = [], []

    class Conversation:
        def __init__(self, **_kw):
            pass

        def send_message(self, _task):
            return None

        def run(self):
            llm = StrictLLM.instances[-1]
            client = openai.OpenAI(base_url=llm.base_url, api_key=llm.api_key, max_retries=0, timeout=20)
            reply = client.chat.completions.create(
                model=llm.model.split("/", 1)[1],
                messages=[{"role": "user", "content": "x"}],
                max_tokens=llm.max_output_tokens,
            )
            answers.append(reply.choices[0].message.content)

    sdk = types.ModuleType("openhands.sdk")
    sdk.LLM, sdk.Conversation = StrictLLM, Conversation
    default = types.ModuleType("openhands.tools.preset.default")
    default.get_default_agent = lambda *, llm, cli_mode: llm
    monkeypatch.setitem(sys.modules, "openhands.sdk", sdk)
    monkeypatch.setitem(sys.modules, "openhands.tools.preset.default", default)
    for name in ("LLM_API_KEY", "GEMINI_API_KEY", "LLM_BASE_URL", "LLM_SUBSCRIPTION"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("COLLEGUE_BROKER_SOCKET", server.socket_path)
    monkeypatch.setenv("LLM_API_KEY", session.token)  # jeton de SESSION, pas une clé fournisseur
    monkeypatch.setenv("LLM_MODEL", "openai/gemma-4-31b-it")
    monkeypatch.setenv("OH_FALLBACK_MODELS", "openai/gemma-4-26b-a4b-it")
    monkeypatch.setenv("OH_MAX_OUTPUT_TOKENS", "128")
    monkeypatch.setattr(
        sys, "argv", ["oh_runner", "--task", "t", "--budget-usd", "1", "--budget-tokens", "5000", "--strict"]
    )
    try:
        assert oh_runner.main() == 0
    finally:
        server.stop()

    (llm,) = StrictLLM.instances
    assert answers == ["ok"]
    assert llm.model == "openai/gemma-4-31b-it" and llm.api_key == session.token
    assert llm.base_url.startswith("http://127.0.0.1:") and llm.base_url.endswith("/v1")
    assert llm.max_output_tokens == 128 and llm.kwargs["reasoning_effort"] is None and llm.usage_id == "coder"
    assert set(llm.kwargs) <= set(oh_runner.LLM_CONSTRUCTOR_KWARGS) and set(llm.kwargs) <= SDK_1_19_1_LLM_FIELDS
    assert stack.upstream.generate_calls[0]["body"]["generationConfig"]["maxOutputTokens"] == 128
    assert stack.ledger.snapshot(session.scope_key).consumed_tokens == 15


def test_the_google_key_is_not_a_setting_of_the_session_or_the_ctx(stack):
    cfg = settings()
    ctx = LocalSamplingContext.from_settings(cfg)
    assert GOOGLE_KEY not in repr(vars(ctx)).replace(repr(cfg), "")  # le ctx n'a pas de client ni de clé propre
    assert GOOGLE_KEY not in repr(stack.runtime)
