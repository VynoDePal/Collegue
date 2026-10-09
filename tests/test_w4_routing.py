"""Vague 4 — A : la destination (fournisseur, modèle, endpoint, clé) est résolue ENSEMBLE par rôle et atteint l'appel émis.

Les transports sont observés à leur frontière : un faux ``AsyncOpenAI`` enregistre ``api_key`` et ``base_url`` de chaque
client construit et le modèle de chaque requête émise. Aucun réseau, aucune vraie clé (labels factices), aucun appel modèle.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import openai
import pytest
from fastmcp import Client, Context, FastMCP

from collegue.core.llm import LLMRole, LLMRoutingError, resolve_route, validate_role_routes
from collegue.core.llm.client import accounted_sample, model_preferences_for_role, normalize_preferences
from collegue.core.llm.sampling_ctx import LocalSamplingContext

QA_URL = "https://qa-gateway.example/v1"
REVIEWER_URL = "https://reviewer-gateway.example/v1"
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"
OPENAI_URL = "https://api.openai.com/v1"


def _response(model="m", content="ok", prompt=10, completion=5):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
        usage=SimpleNamespace(prompt_tokens=prompt, completion_tokens=completion),
        model=model,
    )


class _Recorder:
    """Fabrique de faux ``AsyncOpenAI`` : un client par construction, requêtes enregistrées avec l'identité du client."""

    def __init__(self):
        self.clients = []
        self.emitted = []

    def __call__(self, **kwargs):
        recorder = self
        client = SimpleNamespace(api_key=kwargs.get("api_key"), base_url=kwargs.get("base_url"), options=kwargs)

        async def _create(**request):
            recorder.emitted.append(
                {"api_key": client.api_key, "base_url": client.base_url, "model": request.get("model")}
            )
            await asyncio.sleep(0)
            return _response(model=request.get("model", "m"))

        client.chat = SimpleNamespace(completions=SimpleNamespace(create=_create))
        client.with_options = lambda **_o: SimpleNamespace(chat=client.chat)
        client.close = lambda: None
        self.clients.append(client)
        return client


@pytest.fixture
def recorder(monkeypatch):
    rec = _Recorder()
    monkeypatch.setattr(openai, "AsyncOpenAI", rec)
    monkeypatch.setattr("collegue.monitoring.metrics.enforce_budget", lambda: None)
    monkeypatch.setattr("collegue.monitoring.sampling_usage.record_usage", lambda *a, **k: None)
    return rec


def _settings(**extra):
    base = dict(LLM_PROVIDER="gemini", LLM_MODEL="gemini-2.5-flash", LLM_API_KEY="global-gemini", LLM_CALL_TIMEOUT=0)
    base.update(extra)
    return SimpleNamespace(**base)


def _mixed(**extra):
    """Config à rôles hétérogènes : clés, fournisseurs et endpoints distincts, deux rôles sur le MÊME modèle."""
    return _settings(
        LLM_PROVIDER_PLANNER="openai",
        LLM_MODEL_PLANNER="gpt-5.4",
        LLM_API_KEY_PLANNER="planner-openai",
        LLM_PROVIDER_QA="openai",
        LLM_MODEL_QA="gpt-5.4",
        LLM_API_KEY_QA="qa-openai",
        LLM_BASE_URL_QA=QA_URL,
        LLM_PROVIDER_REVIEWER="openai",
        LLM_MODEL_REVIEWER="gpt-5.4",
        LLM_API_KEY_REVIEWER="reviewer-openai",
        LLM_BASE_URL_REVIEWER=REVIEWER_URL,
        **extra,
    )


# ── ctx offline : la destination émise est celle du rôle ───────────────────────────────────────────


@pytest.mark.parametrize(
    "role, key, base_url, model",
    [
        (LLMRole.DEFAULT, "global-gemini", GEMINI_URL, "gemini-2.5-flash"),
        (LLMRole.PLANNER, "planner-openai", OPENAI_URL, "gpt-5.4"),
        (LLMRole.QA, "qa-openai", QA_URL, "gpt-5.4"),
        (LLMRole.REVIEWER, "reviewer-openai", REVIEWER_URL, "gpt-5.4"),
    ],
)
async def test_offline_ctx_emits_to_the_destination_of_the_role(recorder, role, key, base_url, model):
    settings = _mixed()
    ctx = LocalSamplingContext.from_settings(settings)

    await ctx.sample("salut", model_preferences=model_preferences_for_role(role, settings))

    assert recorder.emitted == [{"api_key": key, "base_url": base_url, "model": model}]


async def test_offline_ctx_keeps_two_roles_with_the_same_model_apart_under_concurrency(recorder):
    settings = _mixed()
    ctx = LocalSamplingContext.from_settings(settings)
    qa = model_preferences_for_role(LLMRole.QA, settings)
    reviewer = model_preferences_for_role(LLMRole.REVIEWER, settings)
    assert qa[0] == reviewer[0] == "gpt-5.4"  # même modèle, seul le hint de rôle les distingue

    await asyncio.gather(*[ctx.sample("x", model_preferences=p) for p in (qa, reviewer, qa, reviewer)])

    pairs = sorted((e["api_key"], e["base_url"]) for e in recorder.emitted)
    assert pairs == sorted([("qa-openai", QA_URL)] * 2 + [("reviewer-openai", REVIEWER_URL)] * 2)
    assert len(recorder.clients) == 2  # un client par destination+identité, réutilisé


async def test_offline_ctx_rotated_key_gets_a_new_client_not_the_old_identity(recorder):
    settings = _mixed()
    ctx = LocalSamplingContext.from_settings(settings)
    prefs = model_preferences_for_role(LLMRole.QA, settings)
    await ctx.sample("x", model_preferences=prefs)
    settings.LLM_API_KEY_QA = "qa-openai-rotated"
    await ctx.sample("x", model_preferences=prefs)
    assert [e["api_key"] for e in recorder.emitted] == ["qa-openai", "qa-openai-rotated"]


async def test_clients_never_read_the_host_environment(recorder, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "host-key-must-not-leak")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://host.invalid/v1")
    settings = _mixed()
    ctx = LocalSamplingContext.from_settings(settings)
    await ctx.sample("x", model_preferences=model_preferences_for_role(LLMRole.PLANNER, settings))
    (client,) = recorder.clients
    assert client.options["api_key"] == "planner-openai" and client.options["base_url"] == OPENAI_URL
    assert "host-key-must-not-leak" not in repr(recorder.emitted)


async def test_local_provider_role_needs_no_key_and_never_receives_the_cloud_key(recorder):
    settings = _settings(LLM_PROVIDER_QA="lmstudio", LLM_MODEL_QA="qwen3", LLM_BASE_URL_QA="http://127.0.0.1:1234/v1")
    ctx = LocalSamplingContext.from_settings(settings)
    await ctx.sample("x", model_preferences=model_preferences_for_role(LLMRole.QA, settings))
    assert recorder.emitted == [{"api_key": "local", "base_url": "http://127.0.0.1:1234/v1", "model": "qwen3"}]


# ── refus AVANT émission ───────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "extra, fragment",
    [
        (dict(LLM_PROVIDER_QA="openai", LLM_MODEL_QA="gpt-5.4"), "aucune clé"),  # clé globale = autre fournisseur
        (dict(LLM_PROVIDER_QA="openai", LLM_MODEL_QA="gemini-2.5-flash", LLM_API_KEY_QA="k"), "pas OpenAI"),
        (dict(LLM_PROVIDER_QA="openai", LLM_MODEL_QA="gemini/gemini-2.5-flash", LLM_API_KEY_QA="k"), "préfixe"),
        (
            dict(LLM_PROVIDER_QA="openai", LLM_API_KEY_QA="k"),
            "modèle",
        ),  # modèle global jamais hérité d'un autre fournisseur
        (dict(LLM_PROVIDER_QA="mystère", LLM_MODEL_QA="x", LLM_API_KEY_QA="k"), "fournisseur"),
    ],
)
async def test_contradictions_missing_keys_and_unknown_providers_are_refused_before_emission(recorder, extra, fragment):
    settings = _settings(**extra)
    ctx = LocalSamplingContext.from_settings(settings)
    with pytest.raises(LLMRoutingError) as caught:
        await ctx.sample("x", model_preferences=["collegue-route:qa"])
    assert fragment in str(caught.value)
    assert recorder.emitted == [] and recorder.clients == []


async def test_a_caller_preference_cannot_change_the_model_without_changing_the_route(recorder):
    settings = _mixed()
    ctx = LocalSamplingContext.from_settings(settings)
    with pytest.raises(LLMRoutingError):
        await ctx.sample("x", model_preferences=["gemini-2.5-flash", "collegue-route:qa"])
    with pytest.raises(LLMRoutingError):
        normalize_preferences(LLMRole.QA, settings, ["gemini-2.5-flash"])
    assert recorder.emitted == []


async def test_accounted_sample_forwards_the_role_route_and_refuses_foreign_preferences():
    settings = _mixed()
    seen = []

    class Ctx:
        async def sample(self, **kwargs):
            seen.append(kwargs["model_preferences"])
            return SimpleNamespace(text="ok")

    await accounted_sample(Ctx(), role=LLMRole.QA, operation="t", settings_obj=settings, messages="x")
    assert seen == [["gpt-5.4", "collegue-route:qa"]]
    with pytest.raises(LLMRoutingError):
        await accounted_sample(
            Ctx(),
            role=LLMRole.QA,
            operation="t",
            settings_obj=settings,
            messages="x",
            model_preferences=["collegue-route:reviewer"],
        )
    assert len(seen) == 1


def test_subscription_is_explicit_and_never_deduced_from_the_model_name():
    # gpt-5.5 + clé OpenAI = API facturée ; l'abonnement n'est jamais déduit du nom.
    keyed = resolve_route(
        LLMRole.CODER, _settings(LLM_PROVIDER_CODER="openai", LLM_MODEL_CODER="gpt-5.5", LLM_API_KEY_CODER="k")
    )
    assert keyed.auth == "api_key" and not keyed.uses_subscription
    sub = resolve_route(LLMRole.CODER, _settings(CODER_SUBSCRIPTION=True, CODER_SUBSCRIPTION_MODEL="gpt-5.5"))
    assert sub.uses_subscription and sub.credential_source == "subscription" and sub.credential() is None
    with pytest.raises(LLMRoutingError):  # abonnement sur un fournisseur non-OpenAI : refusé
        resolve_route(LLMRole.QA, _settings(LLM_AUTH_QA="subscription"))


def test_validate_role_routes_is_a_secret_free_preflight():
    report = validate_role_routes(_mixed())
    assert set(report) == {"coder", "qa", "reviewer", "planner", "default"}
    assert report["qa"]["endpoint"] == QA_URL and report["qa"]["credential_source"] == "role"
    dumped = repr(report) + str(resolve_route(LLMRole.QA, _mixed())) + repr(resolve_route(LLMRole.QA, _mixed()))
    for secret in ("qa-openai", "reviewer-openai", "planner-openai", "global-gemini"):
        assert secret not in dumped
    with pytest.raises(LLMRoutingError):
        validate_role_routes(_settings(LLM_PROVIDER_QA="openai", LLM_MODEL_QA="gpt-5.4"))


# ── handler serveur FastMCP : vraie entrée ``Context.sample`` ────────────────────────────────────────


def _openai_completion(model, content="ok"):
    from openai.types.chat import ChatCompletion

    return ChatCompletion.model_validate(
        {
            "id": "c1",
            "object": "chat.completion",
            "created": 0,
            "model": model,
            "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": content}}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
        }
    )


class _ServerRecorder(_Recorder):
    def __call__(self, **kwargs):
        recorder = self
        client = SimpleNamespace(api_key=kwargs.get("api_key"), base_url=kwargs.get("base_url"), options=kwargs)

        async def _create(**request):
            recorder.emitted.append(
                {"api_key": client.api_key, "base_url": client.base_url, "model": request.get("model")}
            )
            await asyncio.sleep(0)
            return _openai_completion(request.get("model", "m"))

        client.chat = SimpleNamespace(completions=SimpleNamespace(create=_create))
        client.with_options = lambda **_o: SimpleNamespace(chat=client.chat)
        self.clients.append(client)
        return client


@pytest.fixture
def server_recorder(monkeypatch):
    # Importer le handler FastMCP AVANT de remplacer ``openai.AsyncOpenAI`` (annotations évaluées à l'import).
    import fastmcp.client.sampling.handlers.openai  # noqa: F401

    rec = _ServerRecorder()
    monkeypatch.setattr(openai, "AsyncOpenAI", rec)
    monkeypatch.setattr("collegue.monitoring.metrics.enforce_budget", lambda: None)
    monkeypatch.setattr("collegue.monitoring.sampling_usage.record_usage", lambda *a, **k: None)
    return rec


async def _server_calls(settings, roles):
    """Appelle ``ctx.sample`` DEPUIS un outil FastMCP dont le client n'annonce pas le sampling : le handler serveur sert."""
    from collegue.core.llm.sampling_handler import build_routing_sampling_handler

    handler = build_routing_sampling_handler(settings)
    assert handler is not None
    mcp = FastMCP("w4-routing", sampling_handler=handler, sampling_handler_behavior="fallback")

    @mcp.tool
    async def ask(role: str, ctx: Context) -> str:
        prefs = model_preferences_for_role(role, settings)
        result = await ctx.sample(messages=f"q-{role}", max_tokens=16, model_preferences=prefs)
        return result.text or ""

    async with Client(mcp) as client:
        return await asyncio.gather(*[client.call_tool("ask", {"role": r}, raise_on_error=False) for r in roles])


async def test_server_handler_routes_each_role_through_the_real_fastmcp_entry(server_recorder):
    settings = _mixed()
    results = await _server_calls(settings, ["qa", "reviewer", "planner", "default"])

    assert all(not r.is_error for r in results), [r.content for r in results]
    assert sorted((e["api_key"], e["base_url"], e["model"]) for e in server_recorder.emitted) == sorted(
        [
            ("qa-openai", QA_URL, "gpt-5.4"),
            ("reviewer-openai", REVIEWER_URL, "gpt-5.4"),
            ("planner-openai", OPENAI_URL, "gpt-5.4"),
            ("global-gemini", GEMINI_URL, "gemini-2.5-flash"),
        ]
    )


async def test_server_handler_keeps_same_model_roles_apart_under_concurrency(server_recorder):
    settings = _mixed()
    await _server_calls(settings, ["qa", "reviewer"] * 3)
    pairs = [(e["api_key"], e["base_url"]) for e in server_recorder.emitted]
    assert sorted(pairs) == sorted([("qa-openai", QA_URL)] * 3 + [("reviewer-openai", REVIEWER_URL)] * 3)
    assert len(server_recorder.clients) == 2


async def test_server_handler_refuses_a_role_without_its_own_key_before_emission(server_recorder):
    settings = _settings(
        LLM_PROVIDER_QA="openai", LLM_MODEL_QA="gpt-5.4"
    )  # clé globale = Gemini : jamais envoyée à OpenAI
    (result,) = await _server_calls(settings, ["qa"])
    assert result.is_error
    assert server_recorder.emitted == []
    assert "aucune clé" in repr(result.content)  # la cause est dite, la clé ne l'est pas
    assert "global-gemini" not in repr(result.content)


async def test_server_handler_refuses_subscription_instead_of_falling_back_to_an_api_key(server_recorder):
    settings = _settings(LLM_PROVIDER_QA="openai", LLM_MODEL_QA="gpt-5.4", LLM_AUTH_QA="subscription")
    (result,) = await _server_calls(settings, ["qa"])
    assert result.is_error and server_recorder.emitted == []
    assert "abonnement" in repr(result.content)
