"""Vague 4 — A, retours 2 : le VRAI démarrage de l'application utilise le routage par rôle.

Chaque cas importe ``collegue.app`` dans un processus FRAIS à l'environnement isolé (clés factices), construit la vraie
application FastMCP, exécute la vraie validation de démarrage (``validate_llm_config``, puis le vrai ``lifespan`` via un
``Client`` en mémoire) et fait appeler des rôles par un outil enregistré SUR cette application, donc par le handler
réellement attaché à elle. Le vrai SDK ``openai`` + ``httpx`` tournent sur un ``MockTransport`` ; les sockets sont
interdites ; aucun appel distant, aucune vraie clé.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

CHILD = r"""
import asyncio, importlib, json, os, socket, sys
from unittest.mock import patch

import httpx, openai
from fastmcp import Client, Context
import fastmcp.client.sampling.handlers.openai  # annotations évaluées AVANT le remplacement du constructeur

requests = []


def respond(request):
    auth = request.headers.get("authorization", "").removeprefix("Bearer ")
    body = json.loads(request.content) if request.content else {}
    requests.append({"url": str(request.url), "key": auth, "model": body.get("model"),
                     "max_completion_tokens": body.get("max_completion_tokens"), "method": request.method})
    if "/chat/completions" in str(request.url):
        return httpx.Response(200, json={"id": "c", "object": "chat.completion", "created": 1,
            "model": body.get("model", "m"),
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3}})
    return httpx.Response(200, json={"id": "m", "object": "model", "created": 0, "owned_by": "fixture"})


REAL_ASYNC, REAL_SYNC = openai.AsyncOpenAI, openai.OpenAI


def async_factory(*a, **k):
    k["http_client"] = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    return REAL_ASYNC(*a, **k)


def sync_factory(*a, **k):
    k["http_client"] = httpx.Client(transport=httpx.MockTransport(respond))
    return REAL_SYNC(*a, **k)


def no_network(*a, **k):
    raise AssertionError("W4_NETWORK_FORBIDDEN")


def google_forbidden(*a, **k):
    requests.append({"url": "google.genai.Client", "key": "", "model": None, "method": "CALL"})
    raise AssertionError("W4_GOOGLE_SDK_FORBIDDEN")


report = {"import_error": None, "handler": None, "startup_error": None, "lifespan_error": None, "calls": {}}
roles = [r for r in os.environ.get("W4_ROLES", "").split(",") if r]
with patch.object(openai, "AsyncOpenAI", async_factory), patch.object(openai, "OpenAI", sync_factory), \
        patch.object(socket.socket, "connect", no_network):
    try:
        import google.genai as genai
        genai.Client = google_forbidden
    except Exception:
        pass
    try:
        module = importlib.import_module("collegue.app")
    except BaseException as exc:
        report["import_error"] = {"type": type(exc).__name__, "message": str(exc)[:400]}
        print("W4_REPORT:" + json.dumps(report))
        raise SystemExit(0)
    report["handler"] = module.app.sampling_handler is not None
    from collegue.core.llm.client import accounted_sample, model_preferences_for_role

    @module.app.tool
    async def w4_probe_sample(role: str, ctx: Context) -> str:
        result = await accounted_sample(ctx, role=role, operation="w4-app", settings_obj=module.settings,
                                        messages="q-" + role, max_tokens=64,
                                        model_preferences=model_preferences_for_role(role, module.settings))
        return result.text

    async def main():
        try:
            await module.validate_llm_config()
        except Exception as exc:
            report["startup_error"] = {"type": type(exc).__name__, "message": str(exc)[:900]}
        report["requests_after_startup"] = len(requests)
        if os.environ.get("W4_LIFESPAN") == "1":
            try:
                async with Client(module.app, timeout=30) as client:
                    for role in roles:
                        before = len(requests)
                        res = await client.call_tool("w4_probe_sample", {"role": role}, raise_on_error=False)
                        report["calls"][role] = {"is_error": res.is_error, "emitted": requests[before:],
                                                 "text": str(res.content)[:300]}
            except Exception as exc:
                report["lifespan_error"] = {"type": type(exc).__name__, "message": str(exc)[:600]}

    asyncio.run(main())
report["requests"] = requests
print("W4_REPORT:" + json.dumps(report))
"""


def run_app(tmp_path: Path, env_extra: dict, *, roles=(), lifespan=False) -> dict:
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path),
        "PYTHONPATH": str(REPO),
        "PYTHONDONTWRITEBYTECODE": "1",
        "COLLEGUE_HOME": str(tmp_path / "home"),
        "STATE_DATABASE_URL": f"sqlite:///{tmp_path / 'state.sqlite3'}",
        "OAUTH_ENABLED": "false",
        "WATCHDOG_ENABLED": "false",
        "FASTMCP_CHECK_FOR_UPDATES": "off",
        "W4_ROLES": ",".join(roles),
        "W4_LIFESPAN": "1" if lifespan else "0",
        **env_extra,
    }
    proc = subprocess.run(
        [sys.executable, "-c", CHILD], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=120
    )
    lines = [ln for ln in proc.stdout.splitlines() if ln.startswith("W4_REPORT:")]
    assert proc.returncode == 0 and len(lines) == 1, (proc.returncode, proc.stdout[-1500:], proc.stderr[-2500:])
    return json.loads(lines[0].removeprefix("W4_REPORT:"))


GATEWAY = "https://manager-gateway.example/v1"
NAMED = {
    "planner": ("KEY_PLANNER", "https://planner-gw.example/v1"),
    "qa": ("KEY_QA", "https://qa-gw.example/v1"),
    "reviewer": ("KEY_REVIEWER", "https://reviewer-gw.example/v1"),
    "coder": ("KEY_CODER", "https://coder-gw.example/v1"),
}


def named_roles_env() -> dict:
    env = {"LLM_PROVIDER": "openai", "LLM_MODEL": "gpt-5.4-mini"}
    for role, (key, url) in NAMED.items():
        env[f"LLM_API_KEY_{role.upper()}"] = key
        env[f"LLM_BASE_URL_{role.upper()}"] = url
    return env


# ── 1. validation de démarrage : locale, sans émission, avec la route effective ───────────────────────────────────


@pytest.mark.parametrize(
    "env",
    [
        {"LLM_PROVIDER": "openai", "LLM_MODEL": "gpt-5.4-mini", "LLM_API_KEY": "KEY_GLOBAL", "LLM_BASE_URL": GATEWAY},
        {"LLM_PROVIDER": "openai", "LLM_MODEL": "gpt-5.4-mini", "LLM_API_KEY": "KEY_GLOBAL"},
        {"LLM_PROVIDER": "gemini", "LLM_MODEL": "gemini-2.5-flash", "LLM_API_KEY": "KEY_GLOBAL"},
        {
            "LLM_PROVIDER": "gemini",
            "LLM_MODEL": "gemini-2.5-flash",
            "LLM_API_KEY": "KEY_GLOBAL",
            "LLM_BASE_URL": GATEWAY,
        },
        {"LLM_PROVIDER": "lmstudio", "LLM_MODEL": "qwen3"},
        # Profil de remplacement du smoke CI (`--network none`) : fournisseur du catalogue, clé et modèle factices.
        {"LLM_PROVIDER": "gemini", "LLM_MODEL": "test-model", "LLM_API_KEY": "test-key"},
    ],
    ids=["openai-gateway", "openai-native", "gemini-native", "gemini-gateway", "local-no-key", "ci-smoke-profile"],
)
def test_startup_validation_emits_nothing_and_never_sends_a_key_elsewhere(tmp_path, env):
    report = run_app(tmp_path, env)
    assert report["import_error"] is None and report["startup_error"] is None
    assert report["handler"] is True
    assert (
        report["requests"] == []
    )  # ni models.retrieve, ni models.list, ni google.genai : aucune émission au démarrage


def test_startup_validation_through_the_real_lifespan_with_a_gateway(tmp_path):
    env = {"LLM_PROVIDER": "openai", "LLM_MODEL": "gpt-5.4-mini", "LLM_API_KEY": "KEY_GLOBAL", "LLM_BASE_URL": GATEWAY}
    report = run_app(tmp_path, env, roles=["default"], lifespan=True)
    assert report["lifespan_error"] is None and report["startup_error"] is None
    (call,) = report["calls"]["default"]["emitted"]
    # Le seul trafic est l'appel routé : même endpoint, même clé que la passerelle configurée.
    assert call["url"] == f"{GATEWAY}/chat/completions" and call["key"] == "KEY_GLOBAL"
    assert report["calls"]["default"]["is_error"] is False
    # TOUT le trafic de la session (démarrage + lifespan + appel) : un seul appel, jamais de models.retrieve vers api.openai.com.
    assert report["requests"] == [call]


# ── 2. quatre rôles nommés sans clé globale : le serveur démarre et sert chaque rôle ───────────────────────────────


def test_four_named_roles_without_a_global_key_start_the_server_and_each_role_uses_its_own_route(tmp_path):
    report = run_app(tmp_path, named_roles_env(), roles=["planner", "qa", "reviewer", "coder"], lifespan=True)

    assert report["import_error"] is None and report["handler"] is True
    assert report["startup_error"] is None and report["lifespan_error"] is None
    assert report["requests_after_startup"] == 0
    for role, (key, url) in NAMED.items():
        call = report["calls"][role]
        assert call["is_error"] is False, call
        (sent,) = call["emitted"]
        assert (sent["key"], sent["url"], sent["model"]) == (key, f"{url}/chat/completions", "gpt-5.4-mini")


def test_the_default_role_without_a_credential_is_refused_at_its_call_without_disabling_the_named_roles(tmp_path):
    report = run_app(tmp_path, named_roles_env(), roles=["default", "qa"], lifespan=True)

    assert report["handler"] is True and report["startup_error"] is None
    refused = report["calls"]["default"]
    assert refused["is_error"] is True and refused["emitted"] == []  # refusé AVANT émission
    assert "aucune clé" in refused["text"]
    assert all(secret not in refused["text"] for secret in ("KEY_PLANNER", "KEY_QA", "KEY_REVIEWER", "KEY_CODER"))
    (sent,) = report["calls"]["qa"]["emitted"]
    assert sent["key"] == "KEY_QA"  # la route nommée indépendante reste servie


def test_a_role_without_a_credential_next_to_a_valid_global_route_does_not_borrow_another_providers_key(tmp_path):
    env = {
        "LLM_PROVIDER": "gemini",
        "LLM_MODEL": "gemini-2.5-flash",
        "LLM_API_KEY": "KEY_GLOBAL_GEMINI",
        "LLM_PROVIDER_QA": "openai",
        "LLM_MODEL_QA": "gpt-5.4",
    }
    report = run_app(tmp_path, env, roles=["default", "qa"], lifespan=True)
    assert report["startup_error"] is None
    assert report["calls"]["qa"]["is_error"] is True and report["calls"]["qa"]["emitted"] == []
    (sent,) = report["calls"]["default"]["emitted"]
    assert sent["key"] == "KEY_GLOBAL_GEMINI" and "generativelanguage.googleapis.com" in sent["url"]
    assert "KEY_GLOBAL_GEMINI" not in report["calls"]["qa"]["text"]


# ── 3. refus explicite des configurations contradictoires ou sans aucune route utilisable ─────────────────────────


@pytest.mark.parametrize(
    "env, fragment",
    [
        ({"LLM_PROVIDER": "gemini", "LLM_MODEL": "gpt-5.4", "LLM_API_KEY": "KEY_GLOBAL"}, "OpenAI"),
        ({"LLM_PROVIDER": "openai", "LLM_MODEL": "gemini-2.5-flash", "LLM_API_KEY": "KEY_GLOBAL"}, "Gemini"),
        (
            {
                "LLM_PROVIDER": "gemini",
                "LLM_MODEL": "gemini-2.5-flash",
                "LLM_API_KEY": "KEY_GLOBAL",
                "LLM_BASE_URL": "https://api.openai.com/v1",
            },
            "contredit",
        ),
        (
            {
                "LLM_PROVIDER": "gemini",
                "LLM_MODEL": "gemini-2.5-flash",
                "LLM_API_KEY": "KEY_GLOBAL",
                "LLM_PROVIDER_QA": "openai",
                "LLM_MODEL_QA": "gemini-2.5-flash",
                "LLM_API_KEY_QA": "KEY_QA",
            },
            "qa",
        ),
        ({"LLM_PROVIDER": "anthropic", "LLM_MODEL": "claude-x", "LLM_API_KEY": "KEY_GLOBAL"}, "catalogue fermé"),
        (
            {
                "LLM_PROVIDER": "openai",
                "LLM_MODEL": "gpt-5.4",
                "LLM_API_KEY": "KEY_GLOBAL",
                "LLM_AUTH_QA": "subscription",
            },
            "abonnement",
        ),
    ],
    ids=[
        "gemini-gpt-model",
        "openai-gemini-model",
        "gemini-openai-endpoint",
        "role-contradiction",
        "anthropic",
        "subscription-conflict",
    ],
)
def test_contradictory_configurations_are_refused_at_startup_and_get_no_handler(tmp_path, env, fragment):
    report = run_app(tmp_path, env)

    assert report["handler"] is False  # pas de handler : aucune route d'une configuration contradictoire ne sert
    error = report["startup_error"]
    assert error and error["type"] == "ValueError" and "démarrage refusé" in error["message"]
    assert fragment in error["message"]
    assert "KEY_GLOBAL" not in error["message"] and "KEY_QA" not in error["message"]
    assert report["requests"] == []


def test_the_supported_ci_smoke_profile_serves_through_the_real_lifespan_with_no_network(tmp_path):
    env = {"LLM_PROVIDER": "gemini", "LLM_MODEL": "test-model", "LLM_API_KEY": "test-key"}
    report = run_app(tmp_path, env, lifespan=True)
    assert report["lifespan_error"] is None and report["startup_error"] is None and report["requests"] == []


def test_the_historic_anthropic_smoke_profile_is_refused_by_the_real_lifespan(tmp_path):
    """`LLM_PROVIDER=anthropic` (smoke CI, vérification de la roue) sort du catalogue fermé : plus aucune exception silencieuse."""
    env = {"LLM_PROVIDER": "anthropic", "LLM_MODEL": "test-model", "LLM_API_KEY": "test-key"}
    report = run_app(tmp_path, env, lifespan=True)
    assert report["handler"] is False and report["startup_error"]["type"] == "ValueError"
    assert report["lifespan_error"] is not None and report["requests"] == []


def test_a_contradiction_also_stops_the_real_lifespan(tmp_path):
    env = {"LLM_PROVIDER": "gemini", "LLM_MODEL": "gpt-5.4", "LLM_API_KEY": "KEY_GLOBAL"}
    report = run_app(tmp_path, env, roles=["default"], lifespan=True)
    assert report["lifespan_error"] is not None and report["calls"] == {}
    assert report["requests"] == []


def test_no_credential_anywhere_refuses_startup(tmp_path):
    report = run_app(tmp_path, {"LLM_PROVIDER": "openai", "LLM_MODEL": "gpt-5.4-mini"})
    assert report["handler"] is False
    assert (
        report["startup_error"]["type"] == "ValueError"
        and "aucun rôle n'a de route utilisable" in report["startup_error"]["message"]
    )
    assert report["requests"] == []


def test_a_subscription_only_configuration_has_no_server_route(tmp_path):
    """Le handler serveur ne sert pas l'abonnement : sans autre route, le démarrage est refusé (pas de repli sur une clé)."""
    env = {
        "LLM_PROVIDER": "openai",
        "LLM_MODEL": "gpt-5.4",
        "LLM_AUTH_QA": "subscription",
        "LLM_AUTH_REVIEWER": "subscription",
        "LLM_AUTH_PLANNER": "subscription",
        "LLM_AUTH_CODER": "subscription",
    }
    report = run_app(tmp_path, env)
    assert report["handler"] is False and report["startup_error"] is not None


# ── 4. garanties OAuth W1 conservées ──────────────────────────────────────────────────────────────────────────────


def test_oauth_stays_fail_closed_whatever_the_llm_routing(tmp_path):
    env = {"OAUTH_ENABLED": "true", "LLM_PROVIDER": "openai", "LLM_MODEL": "gpt-5.4", "LLM_API_KEY": "KEY_GLOBAL"}
    report = run_app(tmp_path, env)
    assert (
        report["import_error"] is not None
    )  # aucune application sans authentification effective (pas de repli anonyme)
    assert "OAuth" in report["import_error"]["type"] or "oauth" in report["import_error"]["message"].lower()


# ── 5. unités : classification sans émission ─────────────────────────────────────────────────────────────────────


def test_check_role_routes_classifies_without_raising_or_leaking():
    from types import SimpleNamespace

    from collegue.core.llm import check_role_routes

    settings = SimpleNamespace(
        LLM_PROVIDER="openai",
        LLM_MODEL="gpt-5.4",
        LLM_PROVIDER_QA="gemini",
        LLM_MODEL_QA="gpt-5.4",  # contradiction
        LLM_API_KEY_QA="SECRET_QA",
        LLM_API_KEY_PLANNER="SECRET_PLANNER",  # ok
    )
    report = check_role_routes(settings)
    assert report["qa"]["status"] == "invalid" and report["qa"]["route"] is None
    assert report["planner"]["status"] == "ok" and report["planner"]["route"]["credential_source"] == "role"
    assert report["default"]["status"] == "missing_credential"
    assert report["reviewer"]["status"] == "missing_credential"
    assert "SECRET" not in json.dumps(report)
