"""Vague 4 — A, retours de validation indépendante : limite de sortie unique, endpoint global respecté, montage d'abonnement.

Aucun réseau, aucune vraie clé : le vrai SDK ``openai`` et ``httpx`` tournent sur un ``MockTransport`` ; les appels passent
par la vraie entrée FastMCP (``Context.sample``) et par le vrai registre budgétaire durable.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import openai
import pytest
from fastmcp import Client, Context, FastMCP

import collegue.pilot.runtime as runtime
from collegue.core.llm import LLMRole, LLMRoutingError, resolve_route
from collegue.core.llm.budget_guard import bind_budget
from collegue.core.llm.client import accounted_sample, model_preferences_for_role
from collegue.core.llm.sampling_ctx import LocalSamplingContext
from collegue.core.llm.sampling_handler import (
    DEFAULT_BOUNDED_MAX_TOKENS,
    OutputLimitError,
    build_routing_sampling_handler,
    normalize_output_limit,
)
from collegue.state import ProjectStateManager

MODEL = "gpt-5.4-mini"
REAL_CLIENT = openai.AsyncOpenAI


# ── 1. limite de sortie : UNE seule borne, la même pour la réservation et pour la requête ────────────────────────


def test_normalize_output_limit_keeps_one_limit_and_never_lowers_the_callers():
    kw = {"max_completion_tokens": 8192}
    assert normalize_output_limit(kw, default=DEFAULT_BOUNDED_MAX_TOKENS) == 8192 and kw == {
        "max_completion_tokens": 8192
    }
    kw = {"max_tokens": 100}
    assert normalize_output_limit(kw, default=DEFAULT_BOUNDED_MAX_TOKENS) == 100 and kw == {"max_tokens": 100}
    kw = {"max_tokens": 700, "max_completion_tokens": 700}  # redondance cohérente : une seule clé émise
    assert normalize_output_limit(kw) == 700 and kw == {"max_completion_tokens": 700}
    kw = {}
    assert normalize_output_limit(kw, default=DEFAULT_BOUNDED_MAX_TOKENS) == DEFAULT_BOUNDED_MAX_TOKENS
    assert kw == {"max_tokens": DEFAULT_BOUNDED_MAX_TOKENS}
    kw = {"max_tokens": None}
    assert normalize_output_limit(kw) is None and kw == {}  # sans registre, aucune borne inventée


@pytest.mark.parametrize(
    "kw",
    [
        {"max_completion_tokens": 8192, "max_tokens": 4096},
        {"max_tokens": 0},
        {"max_tokens": -5},
        {"max_completion_tokens": True},
        {"max_completion_tokens": "100"},
        {"max_tokens": 12.5},
    ],
)
def test_normalize_output_limit_refuses_invalid_or_contradictory_limits(kw):
    with pytest.raises(OutputLimitError):
        normalize_output_limit(kw, default=DEFAULT_BOUNDED_MAX_TOKENS)


class _Wire:
    """Vrai SDK ``openai`` + ``httpx.MockTransport`` ; photographie le registre PENDANT chaque requête émise."""

    def __init__(self, ledger, scope_key):
        self.ledger, self.scope_key = ledger, scope_key
        self.bodies = []

    def respond(self, request):
        body = json.loads(request.content)
        snap = self.ledger.snapshot(self.scope_key)
        self.bodies.append({"body": body, "reserved_tokens": snap.reserved_tokens})
        return httpx.Response(
            200,
            json={
                "id": "c1",
                "object": "chat.completion",
                "created": 1,
                "model": MODEL,
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30},
            },
        )

    def factory(self, *args, **kwargs):
        kwargs["http_client"] = httpx.AsyncClient(transport=httpx.MockTransport(self.respond))
        return REAL_CLIENT(*args, **kwargs)


def _openai_settings():
    return SimpleNamespace(
        LLM_PROVIDER="openai",
        LLM_MODEL=MODEL,
        LLM_API_KEY="FAKE_GLOBAL",
        LLM_CALL_TIMEOUT=10,
        MAX_COST_USD=2,
        MAX_TOKENS_BUDGET=250000,
        LLM_PRICE_PROMPT_PER_1M=0,
        LLM_PRICE_COMPLETION_PER_1M=0,
    )


@pytest.fixture
def wire(tmp_path, monkeypatch):
    import fastmcp.client.sampling.handlers.openai  # noqa: F401  (annotations évaluées avant le remplacement)

    from collegue.monitoring.metrics import MetricsCollector

    monkeypatch.setattr(MetricsCollector, "_PERSIST_DIR", tmp_path / "monitoring")
    ledger = ProjectStateManager.from_url(f"sqlite:///{tmp_path / 'state.db'}", create=True).budget_ledger
    scope = ledger.create_planning_scope(max_cost_usd=2, max_tokens=250000, strict=True)
    recorder = _Wire(ledger, scope.scope_key)
    monkeypatch.setattr(openai, "AsyncOpenAI", recorder.factory)
    return recorder


async def _sample_via_fastmcp(wire, settings, cap):
    """``ctx.sample`` DEPUIS un outil FastMCP (handler serveur en repli), sous le registre strict, via JSON MCP."""
    handler = build_routing_sampling_handler(settings)

    async def over_the_wire(messages, params, context):
        restored = type(params).model_validate_json(params.model_dump_json())
        return await handler(messages, restored, context)

    server = FastMCP("w4-review1", sampling_handler=over_the_wire, sampling_handler_behavior="fallback", tasks=False)

    @server.tool
    async def ask(ctx: Context) -> str:
        with bind_budget(
            wire.ledger,
            wire.scope_key,
            settings=settings,
            deadline=datetime.now(timezone.utc) + timedelta(seconds=15),
        ):
            result = await accounted_sample(
                ctx,
                role="default",
                operation="review1",
                settings_obj=settings,
                messages="Bonjour " * 200,
                max_tokens=cap,
                model_preferences=model_preferences_for_role("default", settings),
            )
            return result.text

    async with Client(server, timeout=12) as client:
        return await client.call_tool("ask", {}, raise_on_error=False)


@pytest.mark.parametrize("cap", [8192, 4096, 100])
async def test_server_handler_emits_exactly_the_requested_output_limit_and_reserves_for_it(wire, cap):
    result = await _sample_via_fastmcp(wire, _openai_settings(), cap)

    assert not result.is_error, result.content
    (sent,) = wire.bodies
    body = sent["body"]
    # Une seule limite dans le corps HTTP, celle de l'appelant (jamais réduite, jamais complétée par 4096).
    assert body.get("max_completion_tokens") == cap and "max_tokens" not in body
    assert sent["reserved_tokens"] >= cap  # la réservation pré-émission couvre la sortie autorisée


async def test_the_reservation_tracks_the_emitted_limit_one_for_one(wire):
    await _sample_via_fastmcp(wire, _openai_settings(), 100)
    await _sample_via_fastmcp(wire, _openai_settings(), 8192)
    small, large = wire.bodies
    assert large["reserved_tokens"] - small["reserved_tokens"] == 8192 - 100  # même message : seule la borne varie


async def test_an_invalid_output_limit_is_refused_before_reservation_and_emission(wire):
    result = await _sample_via_fastmcp(wire, _openai_settings(), 0)
    assert result.is_error and wire.bodies == []
    snap = wire.ledger.snapshot(wire.scope_key)
    assert not snap.reserved_tokens and not snap.consumed_tokens and not snap.blocked


async def test_direct_calls_without_a_limit_get_the_default_bound_and_contradictions_are_refused(wire):
    """Entrée directe du handler interne (hors FastMCP) : sans limite → borne par défaut unique ; deux limites → refus."""
    settings = _openai_settings()
    handler = build_routing_sampling_handler(settings)
    route = resolve_route(LLMRole.DEFAULT, settings)
    inner = handler._handler_for(route)
    messages = [{"role": "user", "content": "x " * 50}]
    with bind_budget(wire.ledger, wire.scope_key, settings=settings):
        await inner.client.chat.completions.create(model=MODEL, messages=messages)
        with pytest.raises(OutputLimitError):
            await inner.client.chat.completions.create(
                model=MODEL, messages=messages, max_tokens=4096, max_completion_tokens=8192
            )
    (sent,) = wire.bodies
    assert sent["body"].get("max_tokens") == DEFAULT_BOUNDED_MAX_TOKENS and "max_completion_tokens" not in sent["body"]
    assert sent["reserved_tokens"] >= DEFAULT_BOUNDED_MAX_TOKENS


# ── 2. endpoint global : respecté ou refusé, jamais ignoré en silence ─────────────────────────────────────────────


def _gemini(**extra):
    base = dict(LLM_PROVIDER="gemini", LLM_MODEL="gemini-2.5-flash", LLM_API_KEY="FAKE_GLOBAL", LLM_CALL_TIMEOUT=0)
    base.update(extra)
    return SimpleNamespace(**base)


def test_a_global_openai_endpoint_under_gemini_is_a_contradiction_refused_before_emission():
    for url in ("https://api.openai.com/v1", "https://API.OPENAI.COM/v1"):
        settings = _gemini(LLM_BASE_URL=url, llm_base_url=url)
        with pytest.raises(LLMRoutingError, match="contredit"):
            resolve_route(LLMRole.DEFAULT, settings)
        with pytest.raises(LLMRoutingError, match="contredit"):
            resolve_route(LLMRole.QA, settings)  # un rôle qui hérite du fournisseur global hérite aussi du refus


def test_a_global_gateway_endpoint_is_respected_by_gemini_roles_that_inherit_the_provider():
    gateway = "https://manager-gateway.example/v1"
    settings = _gemini(LLM_BASE_URL=gateway, llm_base_url=gateway, LLM_PROVIDER_QA="openai", LLM_MODEL_QA="gpt-5.4")
    inheriting = resolve_route(LLMRole.REVIEWER, settings)
    assert inheriting.provider == "gemini" and inheriting.endpoint == gateway and inheriting.explicit_endpoint
    other = resolve_route(LLMRole.QA, SimpleNamespace(**{**vars(settings), "LLM_API_KEY_QA": "FAKE_QA"}))
    assert (
        other.provider == "openai" and other.endpoint == "https://api.openai.com/v1"
    )  # l'endpoint global ne la suit pas


async def test_the_offline_ctx_emits_to_the_global_gateway_for_gemini(monkeypatch):
    seen = []

    class Client:
        def __init__(self, *, api_key=None, base_url=None, **_kw):
            self.api_key, self.base_url = api_key, base_url
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

        async def create(self, **kw):
            seen.append((self.api_key, self.base_url, kw["model"]))
            return SimpleNamespace(
                model=kw["model"],
                usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
                choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))],
            )

    monkeypatch.setattr(openai, "AsyncOpenAI", Client)
    monkeypatch.setattr("collegue.monitoring.metrics.enforce_budget", lambda *a, **k: None)
    gateway = "https://manager-gateway.example/v1"
    settings = _gemini(LLM_BASE_URL=gateway, llm_base_url=gateway)
    ctx = LocalSamplingContext.from_settings(settings)
    await accounted_sample(ctx, role="default", operation="t", settings_obj=settings, messages="x", max_tokens=8)
    assert seen == [("FAKE_GLOBAL", gateway, "gemini-2.5-flash")]


def test_the_gemini_worker_still_refuses_a_custom_endpoint_explicitly_even_when_inherited():
    gateway = "https://manager-gateway.example/v1"
    settings = _gemini(LLM_BASE_URL=gateway, llm_base_url=gateway, LLM_MODEL_CODER="gemma-4-31b-it")
    with pytest.raises(LLMRoutingError, match="worker"):
        runtime._coder_sandbox_env(settings)


def test_an_openai_global_gateway_keeps_working_for_the_openai_worker():
    gateway = "https://manager-gateway.example/v1"
    settings = SimpleNamespace(
        LLM_PROVIDER="openai",
        LLM_MODEL="gpt-5.4",
        LLM_API_KEY="FAKE_GLOBAL",
        LLM_BASE_URL=gateway,
        llm_base_url=gateway,
    )
    assert runtime._coder_sandbox_env(settings)["LLM_BASE_URL"] == gateway


# ── 3. montage d'abonnement : seulement quand LA ROUTE DU CODEUR l'exige ───────────────────────────────────────────


def _mount_settings(auth_dir, *, subscription_coder):
    config = dict(
        LLM_PROVIDER="gemini",
        LLM_MODEL="gemini-2.5-flash",
        LLM_API_KEY="FAKE_CODER_KEY",
        SANDBOX_SUBSCRIPTION_AUTH_DIR=str(auth_dir) if auth_dir else "",
        LLM_PROVIDER_REVIEWER="openai",
        LLM_MODEL_REVIEWER="gpt-5.4",
        LLM_AUTH_REVIEWER="subscription",
    )
    if subscription_coder:
        config.update(LLM_PROVIDER_CODER="openai", LLM_MODEL_CODER="gpt-5.4", LLM_AUTH_CODER="subscription")
    return SimpleNamespace(**config)


def test_an_api_coder_next_to_a_subscription_reviewer_gets_no_subscription_mount(tmp_path):
    auth = tmp_path / "auth"
    auth.mkdir()
    workspace = tmp_path / "ws"
    workspace.mkdir()
    settings = _mount_settings(auth, subscription_coder=False)

    kwargs = runtime._coder_sandbox_kwargs(settings)
    assert kwargs["subscription_auth_dir"] is None
    assert kwargs["env_secrets"] == {"LLM_API_KEY": "FAKE_CODER_KEY"}  # la clé du codeur, par référence
    assert "HOME" not in kwargs["env"] and "LLM_SUBSCRIPTION" not in kwargs["env"]

    argv = runtime._build_sandbox(settings)._build_run_argv(["python", "-c", "pass"], str(workspace), name="review1")
    assert not any(str(auth) in part for part in argv) and "FAKE_CODER_KEY" not in " ".join(argv)


def test_a_subscription_coder_gets_the_mount_and_no_api_key(tmp_path):
    auth = tmp_path / "auth"
    auth.mkdir()
    workspace = tmp_path / "ws"
    workspace.mkdir()
    settings = _mount_settings(auth, subscription_coder=True)

    kwargs = runtime._coder_sandbox_kwargs(settings)
    assert kwargs["subscription_auth_dir"] == str(auth)
    assert kwargs["env_secrets"] == {} and kwargs["env"]["HOME"] == "/home/sandbox"
    assert kwargs["env"]["LLM_SUBSCRIPTION"] == "1" and kwargs["env"]["LLM_MODEL"] == "gpt-5.4"

    argv = runtime._build_sandbox(settings)._build_run_argv(["python", "-c", "pass"], str(workspace), name="review1")
    assert any(str(auth) in part for part in argv)


def test_a_subscription_coder_without_the_credentials_dir_is_refused_before_launch():
    with pytest.raises(LLMRoutingError, match="SANDBOX_SUBSCRIPTION_AUTH_DIR"):
        runtime._coder_sandbox_kwargs(_mount_settings(None, subscription_coder=True))


def test_the_w1_home_guard_is_untouched():
    """W1 : un montage d'abonnement avec ``HOME=/tmp`` reste refusé par le sandbox lui-même."""
    from collegue.sandbox import executor as ex

    with pytest.raises(ex.SandboxRefused):
        ex.DockerSandbox(image="img", allow_root=True, subscription_auth_dir="/tmp/x")._build_run_argv(
            ["true"], "/tmp/ws-w1", name="n"
        )


# ── 4. campagne 2 USD / 250 000 tokens : l'abonnement est REFUSÉ sous plafond strict de tokens ─────────────────────


def _worker_allocation(tmp_path, **scope):
    from collegue.core.llm.budget_guard import BudgetBinding
    from collegue.executor.openhands_sdk_agent import OHSdkAgent
    from collegue.executor.worker_budget import allocate_worker

    ledger = ProjectStateManager.from_url(f"sqlite:///{tmp_path / 'camp.db'}", create=True).budget_ledger
    key = ledger.create_planning_scope(strict=True, **scope).scope_key
    settings = SimpleNamespace(
        LLM_PROVIDER="gemini",
        LLM_MODEL="gemini-2.5-flash",
        CODER_SUBSCRIPTION=True,
        CODER_SUBSCRIPTION_MODEL="gpt-5.5",
        COLLEGUE_RUN_DEADLINE_SECONDS=0.0,
    )
    agent = OHSdkAgent(SimpleNamespace(run_command=lambda *a, **k: None), settings_obj=settings)
    return allocate_worker(BudgetBinding(ledger, key, settings=settings), agent=agent)


def test_a_subscription_coder_is_refused_under_the_campaign_strict_token_cap(tmp_path):
    from collegue.state import BudgetRefused

    with pytest.raises(BudgetRefused, match="TOKENS"):
        _worker_allocation(tmp_path, max_cost_usd=2.0, max_tokens=250000)


def test_only_a_usd_cap_without_a_token_cap_is_accepted_for_a_subscription_coder(tmp_path):
    allocation = _worker_allocation(tmp_path, max_cost_usd=2.0)
    assert not allocation.billable
