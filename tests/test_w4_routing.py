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


# ── worker : de la route du codeur au constructeur ``openhands.sdk.LLM`` ──────────────────────────────


def _run_runner(monkeypatch, settings, tmp_path):
    """Exécute ``oh_runner.main`` avec l'env EXACT que le produit donne au conteneur (env non secret + secrets) ;
    renvoie les appels reçus par un faux ``openhands.sdk`` (constructeur ``LLM`` et ``subscription_login``)."""
    import sys
    import types

    import collegue.pilot.runtime as runtime
    from collegue.executor import oh_runner

    seen = {"llm": [], "login": []}

    class FakeLLM:
        def __init__(self, **kwargs):
            seen["llm"].append(kwargs)
            self.model = kwargs["model"]
            self.metrics = SimpleNamespace(
                accumulated_token_usage=SimpleNamespace(prompt_tokens=0, completion_tokens=0), accumulated_cost=0.0
            )

        @classmethod
        def subscription_login(cls, **kwargs):
            seen["login"].append(kwargs)
            return cls(model=kwargs["model"])

    class FakeConversation:
        def __init__(self, **_kwargs):
            pass

        def send_message(self, _task):
            return None

        def run(self):
            return None

    sdk = types.ModuleType("openhands.sdk")
    sdk.LLM = FakeLLM
    sdk.Conversation = FakeConversation
    default = types.ModuleType("openhands.tools.preset.default")
    default.get_default_agent = lambda *, llm, cli_mode: llm
    monkeypatch.setitem(sys.modules, "openhands.sdk", sdk)
    monkeypatch.setitem(sys.modules, "openhands.tools.preset.default", default)
    for name in (
        "LLM_API_KEY",
        "GEMINI_API_KEY",
        "LLM_MODEL",
        "LLM_BASE_URL",
        "OH_FALLBACK_MODELS",
        "LLM_SUBSCRIPTION",
    ):
        monkeypatch.delenv(name, raising=False)
    # Ce que le conteneur reçoit : l'env non secret + les secrets par référence ; RIEN d'autre de l'hôte pour le LLM.
    monkeypatch.setenv("OPENAI_API_KEY", "host-openai-key-must-not-be-used")
    for name, value in {**runtime._coder_sandbox_env(settings), **runtime._coder_sandbox_secrets(settings)}.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(sys, "argv", ["oh_runner", "--task", "t", "--workspace", str(tmp_path)])
    assert oh_runner.main() == 0
    return seen, oh_runner


def test_worker_sdk_constructor_gets_the_coder_route_for_an_openai_gateway(monkeypatch, tmp_path):
    settings = _settings(
        LLM_PROVIDER_CODER="openai",
        LLM_MODEL_CODER="gpt-5.4",
        LLM_API_KEY_CODER="coder-openai",
        LLM_BASE_URL_CODER="https://coder-gateway.example/v1",
    )
    seen, oh_runner = _run_runner(monkeypatch, settings, tmp_path)
    (kwargs,) = seen["llm"]
    assert kwargs["model"] == "openai/gpt-5.4"  # JAMAIS « gemini/gpt-5.4 »
    assert kwargs["api_key"] == "coder-openai"  # la clé du rôle, pas OPENAI_API_KEY de l'hôte ni la clé globale Gemini
    assert kwargs["base_url"] == "https://coder-gateway.example/v1"
    assert set(kwargs) <= set(oh_runner.LLM_CONSTRUCTOR_KWARGS)


def test_worker_sdk_constructor_for_a_gemini_coder_has_gemini_prefix_and_no_foreign_endpoint(monkeypatch, tmp_path):
    settings = _settings(LLM_MODEL_CODER="gemma-4-31b-it")  # clé globale Gemini héritée : même fournisseur
    seen, oh_runner = _run_runner(monkeypatch, settings, tmp_path)
    assert [k["model"] for k in seen["llm"]] == ["gemini/gemma-4-31b-it"]
    assert seen["llm"][0]["api_key"] == "global-gemini" and "base_url" not in seen["llm"][0]
    assert set(seen["llm"][0]) <= set(oh_runner.LLM_CONSTRUCTOR_KWARGS)


def test_worker_local_coder_uses_the_local_endpoint_without_any_cloud_key(monkeypatch, tmp_path):
    settings = _settings(
        LLM_PROVIDER_CODER="lmstudio", LLM_MODEL_CODER="qwen3", LLM_BASE_URL_CODER="http://127.0.0.1:1234/v1"
    )
    seen, _ = _run_runner(monkeypatch, settings, tmp_path)
    (kwargs,) = seen["llm"]
    assert kwargs["model"] == "openai/qwen3" and kwargs["base_url"] == "http://127.0.0.1:1234/v1"
    assert kwargs["api_key"] == "local"  # valeur fictive explicite, ni global-gemini ni la clé hôte


def test_worker_subscription_is_a_bare_model_login_selected_explicitly(monkeypatch, tmp_path):
    settings = _settings(
        CODER_SUBSCRIPTION=True, CODER_SUBSCRIPTION_MODEL="gpt-5.5", CODER_SUBSCRIPTION_FALLBACK="gpt-5.4"
    )
    seen, oh_runner = _run_runner(monkeypatch, settings, tmp_path)
    assert [k["model"] for k in seen["login"]] == ["gpt-5.5"] and seen["llm"][0]["model"] == "gpt-5.5"
    assert "api_key" not in seen["login"][0] and "base_url" not in seen["login"][0]
    assert set(seen["login"][0]) - {"vendor", "open_browser", "model"} <= set(oh_runner.LLM_CONSTRUCTOR_KWARGS)


def test_worker_refuses_before_launch_when_the_coder_has_no_key_of_its_provider(tmp_path):
    import collegue.pilot.runtime as runtime

    settings = _settings(LLM_PROVIDER_CODER="openai", LLM_MODEL_CODER="gpt-5.4")  # seule la clé globale Gemini existe
    with pytest.raises(LLMRoutingError):
        runtime._coder_sandbox_kwargs(settings)
    with pytest.raises(LLMRoutingError):
        runtime._coder_sandbox_secrets(settings)


def test_worker_fallbacks_never_change_provider_or_identity():
    from collegue.executor.openhands_sdk_agent import OHSdkAgent

    openai_coder = _settings(
        LLM_PROVIDER_CODER="openai",
        LLM_MODEL_CODER="gpt-5.4",
        LLM_API_KEY_CODER="k",
        CODER_FALLBACK_MODELS="gpt-5.4-mini",
    )
    assert OHSdkAgent(object(), settings_obj=openai_coder).model_chain() == ["openai/gpt-5.4", "openai/gpt-5.4-mini"]
    no_fallback = _settings(LLM_PROVIDER_CODER="openai", LLM_MODEL_CODER="gpt-5.4", LLM_API_KEY_CODER="k")
    assert OHSdkAgent(object(), settings_obj=no_fallback).model_chain() == ["openai/gpt-5.4"]  # pas de repli Gemini
    foreign = _settings(
        LLM_PROVIDER_CODER="openai",
        LLM_MODEL_CODER="gpt-5.4",
        LLM_API_KEY_CODER="k",
        CODER_FALLBACK_MODELS="gemma-4-26b-a4b-it",
    )
    with pytest.raises(LLMRoutingError):
        OHSdkAgent(object(), settings_obj=foreign).model_chain()


def test_oh_runner_constructor_kwargs_are_fields_of_the_pinned_sdk_llm_when_the_sdk_is_installed():
    """Contrôle d'image (C) : ``LLM_CONSTRUCTOR_KWARGS`` ⊂ ``LLM.model_fields`` du SDK verrouillé (1.19.1)."""
    sdk = pytest.importorskip("openhands.sdk")
    from collegue.executor import oh_runner

    assert set(oh_runner.LLM_CONSTRUCTOR_KWARGS) <= set(sdk.LLM.model_fields)


# ── sandbox : le secret passe par référence, jamais par l'argv ni par os.environ ──────────────────────


def test_sandbox_env_secret_is_a_reference_in_argv_and_a_value_only_in_the_docker_child_env(monkeypatch, tmp_path):
    import os

    from pydantic import SecretStr

    from collegue.sandbox import executor as ex

    captured = {}

    def fake_run(argv, **kw):
        captured["argv"], captured["env"] = argv, kw.get("env")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(ex.subprocess, "run", fake_run)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    sandbox = ex.DockerSandbox(
        image="img",
        allow_root=True,
        env={"LLM_MODEL": "openai/gpt-5.4"},
        env_secrets={"LLM_API_KEY": SecretStr("qa-openai")},
    )
    assert "qa-openai" not in repr(sandbox) and "qa-openai" not in repr(vars(sandbox)).replace(
        "SecretStr('**********')", ""
    )
    sandbox.run_command(["true"], str(tmp_path))

    assert "LLM_API_KEY" in captured["argv"] and "qa-openai" not in " ".join(captured["argv"])
    assert captured["argv"][captured["argv"].index("LLM_API_KEY") - 1] == "-e"
    assert captured["env"]["LLM_API_KEY"] == "qa-openai"
    assert "LLM_API_KEY" not in os.environ  # l'hôte n'est jamais muté


def test_sandbox_env_secret_replaces_a_host_key_of_the_same_name_and_rejects_bad_names(monkeypatch, tmp_path):
    from collegue.sandbox import executor as ex

    seen = {}
    monkeypatch.setattr(
        ex.subprocess, "run", lambda argv, **kw: seen.update(env=kw.get("env")) or SimpleNamespace(returncode=0)
    )
    monkeypatch.setenv("LLM_API_KEY", "host-key-of-another-provider")
    ex.DockerSandbox(image="img", allow_root=True, env_secrets={"LLM_API_KEY": "role-key"}).run_command(
        ["true"], str(tmp_path)
    )
    assert seen["env"]["LLM_API_KEY"] == "role-key"
    with pytest.raises(ex.SandboxRefused):
        ex.DockerSandbox(image="img", allow_root=True, env_secrets={"bad name": "x"})
    with pytest.raises(ex.SandboxRefused):
        ex.DockerSandbox(image="img", allow_root=True, env={"LLM_API_KEY": "x"}, env_secrets={"LLM_API_KEY": "y"})


def test_pilot_coder_sandbox_kwargs_carry_the_key_only_as_a_secret_reference():
    import collegue.pilot.runtime as runtime

    settings = _settings(
        LLM_PROVIDER_CODER="openai",
        LLM_MODEL_CODER="gpt-5.4",
        LLM_API_KEY_CODER="coder-openai",
        LLM_BASE_URL_CODER=QA_URL,
    )
    kwargs = runtime._coder_sandbox_kwargs(settings)
    assert kwargs["env_secrets"] == {"LLM_API_KEY": "coder-openai"}
    assert "coder-openai" not in repr(kwargs["env"]) and kwargs["env"]["LLM_BASE_URL"] == QA_URL
    assert kwargs["env"]["LLM_MODEL"] == "openai/gpt-5.4" and kwargs["env"]["OH_FALLBACK_MODELS"] == ""


# ── budget W2 : la réservation juge la destination RÉELLEMENT émise (retries compris) ─────────────────


@pytest.fixture
def ledger_env(tmp_path, monkeypatch):
    from collegue.monitoring.metrics import MetricsCollector
    from collegue.state import ProjectStateManager

    monkeypatch.setattr(MetricsCollector, "_PERSIST_DIR", tmp_path / "monitoring")

    async def instant(_delay):
        return None

    monkeypatch.setattr(asyncio, "sleep", instant)
    manager = ProjectStateManager.from_url(f"sqlite:///{tmp_path / 'state.db'}", create=True)
    pid = manager.create_project(name="p")
    scope = manager.budget_ledger.scope_for_project(pid, max_cost_usd=5.0)  # plafond USD seul, strict
    return SimpleNamespace(ledger=manager.budget_ledger, key=scope.scope_key)


async def test_reservation_prices_the_cloud_role_and_not_the_local_role_of_the_same_binding(recorder, ledger_env):
    from collegue.core.llm.budget_guard import bind_budget

    settings = _settings(
        LLM_PROVIDER_PLANNER="openai",
        LLM_MODEL_PLANNER="gpt-5.4",
        LLM_API_KEY_PLANNER="planner-openai",
        LLM_PROVIDER_QA="lmstudio",
        LLM_MODEL_QA="qwen3",
        LLM_BASE_URL_QA="http://127.0.0.1:1234/v1",
    )
    ctx = LocalSamplingContext.from_settings(settings)
    with bind_budget(ledger_env.ledger, ledger_env.key, settings=settings):
        await ctx.sample("x", model_preferences=model_preferences_for_role(LLMRole.QA, settings), max_tokens=64)
        local_spent = ledger_env.ledger.snapshot(ledger_env.key).spent_usd
        await ctx.sample("x", model_preferences=model_preferences_for_role(LLMRole.PLANNER, settings), max_tokens=64)
        total_spent = ledger_env.ledger.snapshot(ledger_env.key).spent_usd
    assert local_spent == 0  # local : gratuit, jugé sur SON endpoint (le fournisseur global est Gemini)
    assert total_spent > 0  # cloud : tarif OpenAI de SA famille
    assert [e["base_url"] for e in recorder.emitted] == ["http://127.0.0.1:1234/v1", OPENAI_URL]


async def test_each_retry_is_reserved_and_goes_to_the_same_role_destination(ledger_env, monkeypatch):
    from collegue.core.llm.budget_guard import bind_budget

    attempts = []

    class Flaky(_Recorder):
        def __call__(self, **kwargs):
            client = super().__call__(**kwargs)
            inner = client.chat.completions.create
            calls = {"n": 0}

            async def create(**request):
                calls["n"] += 1
                attempts.append(
                    (client.api_key, client.base_url, ledger_env.ledger.snapshot(ledger_env.key).reserved_micro_usd)
                )
                if calls["n"] == 1:
                    import httpx

                    raise openai.RateLimitError(
                        "HTTP 429",
                        response=httpx.Response(429, request=httpx.Request("POST", "http://x.invalid")),
                        body=None,
                    )
                return await inner(**request)

            client.chat = SimpleNamespace(completions=SimpleNamespace(create=create))
            client.with_options = lambda **_o: SimpleNamespace(chat=client.chat)
            return client

    rec = Flaky()
    monkeypatch.setattr(openai, "AsyncOpenAI", rec)
    monkeypatch.setattr("collegue.monitoring.sampling_usage.record_usage", lambda *a, **k: None)
    settings = _mixed()
    ctx = LocalSamplingContext.from_settings(settings)
    with bind_budget(ledger_env.ledger, ledger_env.key, settings=settings):
        await ctx.sample("x", model_preferences=model_preferences_for_role(LLMRole.PLANNER, settings), max_tokens=64)

    assert [(k, u) for k, u, _ in attempts] == [("planner-openai", OPENAI_URL)] * 2  # la 2e tentative reste sur le rôle
    assert all(reserved > 0 for _, _, reserved in attempts)  # une réservation existait AVANT chaque émission


async def test_strict_budget_refuses_an_unattested_gateway_destination_before_emission(recorder, ledger_env):
    from collegue.core.llm.budget_guard import bind_budget
    from collegue.state import BudgetRefused

    settings = _mixed()  # le rôle QA vise une passerelle dont l'identité de modèle n'est pas attestée
    ctx = LocalSamplingContext.from_settings(settings)
    with bind_budget(ledger_env.ledger, ledger_env.key, settings=settings):
        with pytest.raises(BudgetRefused):
            await ctx.sample("x", model_preferences=model_preferences_for_role(LLMRole.QA, settings), max_tokens=64)
    assert recorder.emitted == []
