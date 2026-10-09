"""Vague 4 — A, retours 3 : une authentification EXPLICITE n'est jamais dégradée implicitement en accès anonyme.

Fournisseurs locaux (lmstudio, ollama, unsloth) : sans choix d'authentification ou avec ``none``, aucune clé n'est exigée ;
avec ``LLM_AUTH_<ROLE>=api_key`` une clé effective (rôle, ou globale du MÊME fournisseur) est obligatoire, sinon refus
AVANT transport. Vrai SDK ``openai`` + ``httpx.MockTransport``, vrais consommateurs routés (ctx offline, handler serveur
via FastMCP), sockets interdites, clés factices.
"""

from __future__ import annotations

import asyncio
import json
import socket
from types import SimpleNamespace

import httpx
import openai
import pytest
from fastmcp import Client, Context, FastMCP

from collegue.core.llm import (
    LLMMissingCredentialError,
    LLMRole,
    LLMRoutingError,
    check_role_routes,
    resolve_route,
    validate_role_routes,
)
from collegue.core.llm.client import accounted_sample, model_preferences_for_role
from collegue.core.llm.sampling_ctx import LocalSamplingContext
from collegue.core.llm.sampling_handler import build_routing_sampling_handler

PROVIDERS = {
    "lmstudio": "http://127.0.0.1:1234/v1",
    "ollama": "http://127.0.0.1:11434/v1",
    "unsloth": "http://127.0.0.1:8888/v1",
}
ROLE_KEY = "w4-fake-role-key"
GLOBAL_KEY = "w4-fake-global-key"
REAL_ASYNC = openai.AsyncOpenAI


def settings(provider, *, auth="", role_key="", global_key="", global_provider=None, **extra):
    """QA sur un fournisseur local ; le fournisseur global est ``global_provider`` (défaut : le même)."""
    base = dict(
        LLM_PROVIDER=global_provider or provider,
        LLM_MODEL="local-fixture",
        LLM_API_KEY=global_key,
        LLM_PROVIDER_QA=provider,
        LLM_MODEL_QA="local-fixture",
        LLM_API_KEY_QA=role_key,
        LLM_BASE_URL_QA=PROVIDERS[provider],
        LLM_CALL_TIMEOUT=2,
        MAX_COST_USD=0,
        MAX_TOKENS_BUDGET=0,
    )
    if auth:
        base["LLM_AUTH_QA"] = auth
    base.update(extra)
    return SimpleNamespace(**base)


class Wire:
    def __init__(self):
        self.calls = []

    def respond(self, request):
        self.calls.append({"url": str(request.url), "authorization": request.headers.get("authorization")})
        return httpx.Response(
            200,
            json={
                "id": "c",
                "object": "chat.completion",
                "created": 1,
                "model": "local-fixture",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3},
            },
        )

    def factory(self, **kwargs):
        kwargs["http_client"] = httpx.AsyncClient(transport=httpx.MockTransport(self.respond))
        return REAL_ASYNC(**kwargs)


@pytest.fixture
def wire(monkeypatch):
    import fastmcp.client.sampling.handlers.openai  # noqa: F401  (annotations évaluées avant le remplacement)

    recorder = Wire()
    monkeypatch.setattr(openai, "AsyncOpenAI", recorder.factory)
    monkeypatch.setattr("collegue.monitoring.metrics.enforce_budget", lambda *a, **k: None)
    monkeypatch.setattr("collegue.monitoring.sampling_usage.record_usage", lambda *a, **k: None)

    def no_network(*a, **k):
        raise AssertionError("W4_NETWORK_FORBIDDEN")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    return recorder


async def sample_qa(config):
    ctx = LocalSamplingContext.from_settings(config)
    try:
        return await accounted_sample(
            ctx,
            role=LLMRole.QA,
            operation="w4-auth",
            settings_obj=config,
            messages="Bonjour",
            model_preferences=model_preferences_for_role(LLMRole.QA, config),
            max_tokens=8,
        )
    finally:
        await ctx.aclose()


# ── témoins verts : le local sans clé reste utilisable quand il n'a pas été exclu ────────────────────────────────


@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize("auth", ["", "none"], ids=["default", "explicit-none"])
async def test_a_local_provider_without_key_is_usable_by_default_or_by_explicit_none(wire, provider, auth):
    config = settings(provider, auth=auth)
    route = resolve_route(LLMRole.QA, config)
    assert route.auth == "none" and route.credential() is None and route.credential_source == "none"
    assert check_role_routes(config, roles=[LLMRole.QA])["qa"]["status"] == "ok"

    await sample_qa(config)

    assert wire.calls == [{"url": f"{PROVIDERS[provider]}/chat/completions", "authorization": "Bearer local"}]


@pytest.mark.parametrize("provider", PROVIDERS)
async def test_an_explicit_api_key_with_a_role_key_emits_with_that_key(wire, provider):
    config = settings(provider, auth="api_key", role_key=ROLE_KEY)
    route = resolve_route(LLMRole.QA, config)
    assert route.auth == "api_key" and route.credential_source == "role"

    await sample_qa(config)

    assert [c["authorization"] for c in wire.calls] == [f"Bearer {ROLE_KEY}"]


@pytest.mark.parametrize("provider", PROVIDERS)
async def test_an_explicit_api_key_inherits_the_global_key_of_the_same_provider(wire, provider):
    config = settings(provider, auth="api_key", global_key=GLOBAL_KEY)
    route = resolve_route(LLMRole.QA, config)
    assert route.auth == "api_key" and route.credential_source == "global"

    await sample_qa(config)

    assert [c["authorization"] for c in wire.calls] == [f"Bearer {GLOBAL_KEY}"]


# ── cas rouge : api_key explicite sans credential effectif → refus AVANT transport ──────────────────────────────


@pytest.mark.parametrize("provider", PROVIDERS)
async def test_an_explicit_api_key_without_a_credential_is_refused_before_any_transport(wire, provider):
    config = settings(provider, auth="api_key")

    with pytest.raises(LLMMissingCredentialError, match="api_key est explicite"):
        resolve_route(LLMRole.QA, config)
    with pytest.raises(LLMRoutingError):
        await sample_qa(config)

    assert wire.calls == []  # aucune requête, aucun Bearer local


@pytest.mark.parametrize("provider", PROVIDERS)
async def test_the_global_key_of_another_provider_never_satisfies_an_explicit_api_key(wire, provider):
    config = settings(provider, auth="api_key", global_key=GLOBAL_KEY, global_provider="openai", LLM_MODEL="gpt-5.4")
    with pytest.raises(LLMMissingCredentialError, match="n'est héritée que pour le fournisseur global"):
        resolve_route(LLMRole.QA, config)
    with pytest.raises(LLMRoutingError):
        await sample_qa(config)
    assert wire.calls == []


@pytest.mark.parametrize("provider", PROVIDERS)
def test_none_with_a_key_is_a_contradiction_not_a_silent_choice(provider):
    for config in (
        settings(provider, auth="none", role_key=ROLE_KEY),
        settings(
            provider, auth="none", global_key=GLOBAL_KEY
        ),  # clé globale du MÊME fournisseur : héritée, donc contradictoire
    ):
        with pytest.raises(LLMRoutingError, match="contredit une clé définie") as caught:
            resolve_route(LLMRole.QA, config)
        assert type(caught.value) is LLMRoutingError  # contradiction, pas « clé manquante »
        assert ROLE_KEY not in str(caught.value) and GLOBAL_KEY not in str(caught.value)
        assert check_role_routes(config, roles=[LLMRole.QA])["qa"]["status"] == "invalid"


def test_a_hosted_provider_still_requires_a_key_with_or_without_a_choice():
    hosted = SimpleNamespace(LLM_PROVIDER="openai", LLM_MODEL="gpt-5.4", LLM_API_KEY="")
    with pytest.raises(LLMMissingCredentialError):
        resolve_route(LLMRole.DEFAULT, hosted)
    hosted_none = SimpleNamespace(
        LLM_PROVIDER="gemini",
        LLM_MODEL="gemini-2.5-flash",
        LLM_API_KEY=GLOBAL_KEY,
        LLM_PROVIDER_QA="openai",
        LLM_MODEL_QA="gpt-5.4",
        LLM_AUTH_QA="none",
    )
    with pytest.raises(LLMRoutingError, match="exige une clé"):
        resolve_route(LLMRole.QA, hosted_none)


# ── require_credential=False : cohérent, documenté, jamais émetteur ──────────────────────────────────────────────


@pytest.mark.parametrize("provider", PROVIDERS)
def test_require_credential_false_keeps_the_explicit_choice_and_no_transport_accepts_it(provider):
    config = settings(provider, auth="api_key")
    route = resolve_route(LLMRole.QA, config, require_credential=False)
    assert route.auth == "api_key" and route.credential() is None  # le choix n'est pas réécrit en « none »
    with pytest.raises(LLMMissingCredentialError, match="émission refusée"):
        route.transport_key()
    anonymous = resolve_route(LLMRole.QA, settings(provider), require_credential=False)
    assert anonymous.auth == "none" and anonymous.transport_key() == "local"
    # La forme stricte (celle des transports) refuse toujours, la forme laxiste ne la contourne pas.
    with pytest.raises(LLMMissingCredentialError):
        resolve_route(LLMRole.QA, config, require_credential=True)


def test_validate_role_routes_stays_strict_by_default():
    with pytest.raises(LLMMissingCredentialError):
        validate_role_routes(settings("lmstudio", auth="api_key"), roles=[LLMRole.QA])
    report = validate_role_routes(settings("lmstudio", auth="api_key"), roles=[LLMRole.QA], require_credential=False)
    assert report["qa"]["auth"] == "api_key" and report["qa"]["credential_present"] is False


# ── vrai consommateur serveur (handler FastMCP) et démarrage : refus sans désactiver les autres rôles ─────────────


async def _server_calls(config, roles):
    handler = build_routing_sampling_handler(config)
    server = FastMCP("w4-auth", sampling_handler=handler, sampling_handler_behavior="fallback", tasks=False)

    @server.tool
    async def ask(role: str, ctx: Context) -> str:
        result = await ctx.sample(
            messages=f"q-{role}", max_tokens=16, model_preferences=model_preferences_for_role(role, config)
        )
        return result.text or ""

    async with Client(server, timeout=12) as client:
        return await asyncio.gather(*[client.call_tool("ask", {"role": r}, raise_on_error=False) for r in roles])


async def test_the_server_handler_refuses_an_explicit_api_key_without_credential_before_emission(wire):
    config = settings("lmstudio", auth="api_key")
    (result,) = await _server_calls(config, ["qa"])
    assert result.is_error and "aucune clé" in repr(result.content) and wire.calls == []


async def test_the_server_handler_serves_the_anonymous_local_role_next_to_the_refused_one(wire):
    config = settings(
        "lmstudio",
        LLM_PROVIDER_REVIEWER="ollama",
        LLM_MODEL_REVIEWER="local-fixture",
        LLM_BASE_URL_REVIEWER=PROVIDERS["ollama"],
        LLM_AUTH_QA="api_key",
    )
    qa, reviewer = await _server_calls(config, ["qa", "reviewer"])
    assert qa.is_error and not reviewer.is_error
    assert wire.calls == [{"url": f"{PROVIDERS['ollama']}/chat/completions", "authorization": "Bearer local"}]


def test_startup_distinguishes_a_missing_credential_from_a_contradiction_without_disabling_other_roles():
    config = settings(
        "lmstudio",
        auth="api_key",  # QA : api_key explicite sans clé
        LLM_PROVIDER_REVIEWER="unsloth",
        LLM_MODEL_REVIEWER="local-fixture",
        LLM_BASE_URL_REVIEWER=PROVIDERS["unsloth"],
        LLM_AUTH_REVIEWER="none",
        LLM_API_KEY_REVIEWER=ROLE_KEY,  # REVIEWER : none + clé = contradiction
    )
    report = check_role_routes(config)
    assert report["qa"]["status"] == "missing_credential"
    assert report["reviewer"]["status"] == "invalid"
    assert report["default"]["status"] == "ok" and report["planner"]["status"] == "ok"

    only_missing = settings("lmstudio", auth="api_key")
    states = {role: item["status"] for role, item in check_role_routes(only_missing).items()}
    assert states["qa"] == "missing_credential" and states["default"] == "ok" and states["coder"] == "ok"


def test_the_real_application_starts_with_the_other_roles_and_refuses_the_explicit_api_key_role(tmp_path):
    from test_w4_routing_app import run_app

    env = {
        "LLM_PROVIDER": "lmstudio",
        "LLM_MODEL": "local-fixture",
        "LLM_PROVIDER_QA": "lmstudio",
        "LLM_MODEL_QA": "local-fixture",
        "LLM_AUTH_QA": "api_key",
    }
    report = run_app(tmp_path, env, roles=["qa", "default"], lifespan=True)

    assert report["startup_error"] is None and report["lifespan_error"] is None and report["handler"] is True
    assert report["calls"]["qa"]["is_error"] is True and report["calls"]["qa"]["emitted"] == []
    assert "api_key est explicite" in report["calls"]["qa"]["text"]
    (sent,) = report["calls"]["default"]["emitted"]
    assert sent["key"] == "local"  # le rôle par défaut local sans clé continue de servir


def test_a_dumped_failure_never_contains_a_credential():
    config = settings("lmstudio", auth="none", role_key=ROLE_KEY)
    report = check_role_routes(config, roles=[LLMRole.QA])
    assert ROLE_KEY not in json.dumps(report)
