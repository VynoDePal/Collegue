"""Vague 4 — A : contrat EXACT du constructeur ``openhands.sdk.LLM`` 1.19.1 tel que l'utilisent le runner et le sampler.

Le vrai ``LLM`` (pydantic, ``extra="ignore"``) IGNORE sans erreur un argument inconnu : l'ancien ``service_id`` n'a jamais
fixé l'identité du coder (``llm.usage_id`` restait ``"default"``). Ce fichier ne s'appuie donc pas sur un double qui accepte
n'importe quels arguments : ``StrictLLM`` REFUSE tout argument hors des 52 champs de ``LLM`` du wheel verrouillé et reproduit
``create_llm`` de ``subscription_login`` (``max_output_tokens=None`` posé par le SDK, donc un doublon est un ``TypeError``).
La liste des champs est un instantané du wheel ``openhands-sdk==1.19.1`` (sha256 du verrou) ; le test de conformité à la
source réelle s'exécute si ``W4_SDK_WHEEL`` désigne ce wheel (ignoré sinon), sans l'importer ni installer sa fermeture.
"""

from __future__ import annotations

import ast
import hashlib
import io
import json
import os
import sys
import types
import zipfile
from pathlib import Path

import pytest

from collegue.executor import oh_runner, oh_sampler

# Champs de ``openhands.sdk.LLM`` (``LLM.model_fields``) dans openhands-sdk 1.19.1.
SDK_1_19_1_LLM_FIELDS = frozenset(
    """api_key api_version aws_access_key_id aws_bedrock_runtime_endpoint aws_profile_name aws_region_name aws_role_name
    aws_secret_access_key aws_session_name aws_session_token base_url caching_prompt custom_tokenizer disable_stop_word
    disable_vision drop_params enable_encrypted_reasoning extended_thinking_budget extra_headers fallback_strategy
    force_string_serializer input_cost_per_token litellm_extra_body log_completions log_completions_folder max_input_tokens
    max_message_chars max_output_tokens model model_canonical_name modify_params native_tool_calling num_retries
    ollama_base_url openrouter_app_name openrouter_site_url output_cost_per_token prompt_cache_retention reasoning_effort
    reasoning_summary retry_listener retry_max_wait retry_min_wait retry_multiplier safety_settings seed stream temperature
    timeout top_k top_p usage_id""".split()
)
SDK_WHEEL_SHA256 = "fd2c7ed11663595f6131b5e1879f636ad1dd4d897490b582509c924ca238a150"  # locks/sandbox-openhands.txt


class StrictLLM:
    """``openhands.sdk.LLM`` sans la tolérance ``extra="ignore"`` : un argument inconnu est une erreur."""

    instances: list = []
    login_calls: list = []

    def __init__(self, **kwargs):
        unknown = sorted(set(kwargs) - SDK_1_19_1_LLM_FIELDS)
        if unknown:
            raise TypeError(
                f"champs inconnus de openhands.sdk.LLM 1.19.1 (le SDK réel les ignorerait en silence) : {unknown}"
            )
        self.kwargs = dict(kwargs)
        self.model = kwargs["model"]
        self.usage_id = kwargs.get("usage_id", "default")  # défaut du champ
        self.api_key = kwargs.get("api_key")
        self.base_url = kwargs.get("base_url")
        self.max_output_tokens = kwargs.get("max_output_tokens")
        self._is_subscription = False
        self.metrics = types.SimpleNamespace(
            accumulated_token_usage=types.SimpleNamespace(prompt_tokens=0, completion_tokens=0), accumulated_cost=0.0
        )
        StrictLLM.instances.append(self)

    @classmethod
    def subscription_login(
        cls,
        *,
        vendor="openai",
        model="gpt-5.2-codex",
        force_login=False,
        open_browser=True,
        auth_method="browser",
        **llm_kwargs,
    ):
        """Reproduit ``subscription_login`` → ``create_llm`` (openhands/sdk/llm/auth/openai.py, 1.19.1)."""
        StrictLLM.login_calls.append({"vendor": vendor, "model": model, "open_browser": open_browser, **llm_kwargs})
        llm = cls(
            model=f"openai/{model}",
            base_url="https://chatgpt.com/backend-api/codex",
            api_key="oauth-token",
            extra_headers={},
            litellm_extra_body={"store": False},
            temperature=None,
            max_output_tokens=None,  # posé par le SDK : le repasser dans llm_kwargs = « multiple values »
            stream=True,
            **llm_kwargs,
        )
        llm._is_subscription = True
        llm.max_output_tokens = None
        return llm

    def uses_responses_api(self):
        return False

    def completion(self, **_kw):
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=types.SimpleNamespace(content="ok"))])


@pytest.fixture(autouse=True)
def _reset():
    StrictLLM.instances, StrictLLM.login_calls = [], []


def _install_sdk(monkeypatch):
    sdk = types.ModuleType("openhands.sdk")
    sdk.LLM = StrictLLM

    class Conversation:
        def __init__(self, **_kw):
            pass

        def send_message(self, _task):
            return None

        def run(self):
            return None

    sdk.Conversation = Conversation
    llm_pkg = types.ModuleType("openhands.sdk.llm")
    llm_pkg.Message = lambda **kw: types.SimpleNamespace(**kw)
    llm_pkg.TextContent = lambda **kw: types.SimpleNamespace(**kw)
    default = types.ModuleType("openhands.tools.preset.default")
    default.get_default_agent = lambda *, llm, cli_mode: llm
    monkeypatch.setitem(sys.modules, "openhands.sdk", sdk)
    monkeypatch.setitem(sys.modules, "openhands.sdk.llm", llm_pkg)
    monkeypatch.setitem(sys.modules, "openhands.tools.preset.default", default)


def _runner_env(monkeypatch, *, subscription):
    for name in ("LLM_API_KEY", "GEMINI_API_KEY", "LLM_BASE_URL", "OH_FALLBACK_MODELS", "LLM_SUBSCRIPTION"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LLM_MODEL", "gpt-5.5" if subscription else "openai/gpt-5.4")
    monkeypatch.setenv("OH_FALLBACK_MODELS", "")
    if subscription:
        monkeypatch.setenv("LLM_SUBSCRIPTION", "1")
    else:
        monkeypatch.setenv("LLM_API_KEY", "w4-fake-key")
        monkeypatch.setenv("LLM_BASE_URL", "https://gateway.example/v1")
    monkeypatch.setattr(oh_runner.BudgetGuard, "start_watchdog", lambda self, period=0.5: None)
    monkeypatch.setattr(oh_runner.signal, "signal", lambda *a, **k: None)


def _run_runner(monkeypatch, *args):
    monkeypatch.setattr(sys, "argv", ["oh_runner", "--task", "t", *args])
    return oh_runner.main()


# ── contrat des arguments ────────────────────────────────────────────────────────────────────────────────────────


def test_every_declared_runner_argument_is_a_real_field_of_the_pinned_sdk():
    assert set(oh_runner.LLM_CONSTRUCTOR_KWARGS) <= SDK_1_19_1_LLM_FIELDS
    assert set(oh_runner.LLM_SUBSCRIPTION_KWARGS) <= SDK_1_19_1_LLM_FIELDS
    assert "service_id" not in SDK_1_19_1_LLM_FIELDS and "usage_id" in SDK_1_19_1_LLM_FIELDS
    assert "service_id" not in oh_runner.LLM_CONSTRUCTOR_KWARGS


def test_the_strict_double_rejects_what_the_real_sdk_would_silently_ignore():
    with pytest.raises(TypeError, match="service_id"):
        StrictLLM(model="m", service_id="coder")
    with pytest.raises(TypeError, match="multiple values"):  # le doublon de max_output_tokens du login d'abonnement
        StrictLLM.subscription_login(model="gpt-5.5", max_output_tokens=4096)


# ── mode clé API ─────────────────────────────────────────────────────────────────────────────────────────────────


def test_api_key_mode_builds_the_llm_with_known_fields_and_the_coder_identity(monkeypatch):
    _install_sdk(monkeypatch)
    _runner_env(monkeypatch, subscription=False)

    assert _run_runner(monkeypatch) == 0

    (llm,) = StrictLLM.instances
    assert llm.usage_id == "coder"  # identité EFFECTIVEMENT portée par l'objet (pas le défaut « default »)
    assert (llm.model, llm.api_key, llm.base_url) == ("openai/gpt-5.4", "w4-fake-key", "https://gateway.example/v1")
    assert set(llm.kwargs) <= set(oh_runner.LLM_CONSTRUCTOR_KWARGS)


def test_api_key_mode_under_an_allocation_keeps_the_output_cap_and_the_identity(monkeypatch):
    _install_sdk(monkeypatch)
    _runner_env(monkeypatch, subscription=False)
    monkeypatch.setenv("OH_MAX_OUTPUT_TOKENS", "1000")

    assert _run_runner(monkeypatch, "--budget-tokens", "100000") == 0

    (llm,) = StrictLLM.instances
    assert llm.usage_id == "coder" and llm.max_output_tokens == 1000 and llm.kwargs["num_retries"] == 0
    assert set(llm.kwargs) <= set(oh_runner.LLM_CONSTRUCTOR_KWARGS)


# ── mode abonnement ──────────────────────────────────────────────────────────────────────────────────────────────


def test_subscription_login_receives_only_accepted_arguments_and_keeps_the_coder_identity(monkeypatch):
    _install_sdk(monkeypatch)
    _runner_env(monkeypatch, subscription=True)

    assert _run_runner(monkeypatch) == 0

    (call,) = StrictLLM.login_calls
    assert call["model"] == "gpt-5.5" and call["vendor"] == "openai" and call["open_browser"] is False
    assert set(call) - {"vendor", "model", "open_browser"} <= set(oh_runner.LLM_SUBSCRIPTION_KWARGS)
    (llm,) = StrictLLM.instances
    assert llm.usage_id == "coder" and llm._is_subscription and llm.api_key == "oauth-token"


def test_subscription_login_under_an_allocation_never_passes_max_output_tokens(monkeypatch):
    """Sans ce filtre le vrai SDK lève TypeError (``max_output_tokens`` déjà fixé à None par ``create_llm``)."""
    _install_sdk(monkeypatch)
    _runner_env(monkeypatch, subscription=True)

    code = _run_runner(monkeypatch, "--budget-usd", "2.0", "--strict", "--no-billing")

    assert code == 0
    (call,) = StrictLLM.login_calls
    assert "max_output_tokens" not in call and call["usage_id"] == "coder" and call["num_retries"] == 0
    (llm,) = StrictLLM.instances
    assert llm.max_output_tokens is None  # le SDK ne l'envoie pas en abonnement : la garde le constate (voir rapport)


# ── sampler d'abonnement (reviewer / juge) ───────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("strict", [False, True])
def test_the_subscription_sampler_logs_in_with_accepted_arguments_only(monkeypatch, capsys, strict):
    _install_sdk(monkeypatch)
    monkeypatch.setenv("LLM_MODEL", "gpt-5.4")
    payload = {"system": "s", "prompt": "p"}
    if strict:
        payload.update(strict=True, max_output_tokens=2000)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))

    assert oh_sampler.main() == 0

    (call,) = StrictLLM.login_calls
    assert call["usage_id"] == "sampler" and "max_output_tokens" not in call and "service_id" not in call
    assert set(call) - {"vendor", "model", "open_browser"} <= SDK_1_19_1_LLM_FIELDS
    assert StrictLLM.instances[0].usage_id == "sampler"
    assert "<<<SAMPLE_BEGIN>>>ok<<<SAMPLE_END>>>" in capsys.readouterr().out


def test_the_strict_sampler_still_refuses_an_unbounded_output_request(monkeypatch):
    _install_sdk(monkeypatch)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"system": "s", "prompt": "p", "strict": True})))
    assert oh_sampler.main() == 3 and StrictLLM.login_calls == []


# ── conformité de l'instantané à la source réelle (wheel verrouillé, jamais importé) ─────────────────────────────

WHEEL = os.environ.get("W4_SDK_WHEEL", "")


@pytest.mark.skipif(not WHEEL, reason="W4_SDK_WHEEL non défini : wheel openhands-sdk 1.19.1 requis (non importé)")
def test_the_snapshot_matches_the_real_pinned_sdk_source():
    raw = Path(WHEEL).read_bytes()
    assert hashlib.sha256(raw).hexdigest() == SDK_WHEEL_SHA256  # c'est bien le wheel du verrou
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        llm_src = archive.read("openhands/sdk/llm/llm.py").decode()
        auth_src = archive.read("openhands/sdk/llm/auth/openai.py").decode()
    cls = next(n for n in ast.parse(llm_src).body if isinstance(n, ast.ClassDef) and n.name == "LLM")
    fields = {
        n.target.id
        for n in cls.body
        if isinstance(n, ast.AnnAssign)
        and isinstance(n.target, ast.Name)
        and not n.target.id.startswith("_")
        and "ClassVar" not in ast.unparse(n.annotation)
    }
    assert fields == SDK_1_19_1_LLM_FIELDS
    assert 'extra="ignore"' in llm_src  # raison pour laquelle un champ inconnu passe silencieusement
    assert "service_id" not in llm_src + auth_src
    # ``create_llm`` fixe max_output_tokens=None AVANT ``**llm_kwargs`` (doublon = TypeError) puis le remet à None.
    assert "max_output_tokens=None,\n            stream=True,\n            **llm_kwargs," in auth_src
    assert "llm.max_output_tokens = None" in auth_src
