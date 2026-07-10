"""Tests du ctx de sampling offline (A1) — collegue/core/llm/sampling_ctx.py."""

from __future__ import annotations

from collections import deque
from types import SimpleNamespace

import pytest

from collegue.core.llm.sampling_ctx import (
    DEFAULT_MAX_TOKENS,
    LocalSamplingContext,
    PerModelRateLimiter,
    SampleResult,
    _coerce,
    _pick_model,
    to_openai_messages,
)

# --- faux client OpenAI-compatible ---------------------------------------------


def _resp(content, *, model="m", usage=None):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
        model=model,
        usage=usage,
    )


class _FakeClient:
    def __init__(self, resp):
        self._resp = resp
        self.calls = []
        self.closed = False
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def _create(self, **kwargs):
        self.calls.append(kwargs)
        return self._resp

    def close(self):
        self.closed = True


@pytest.fixture(autouse=True)
def _neutralize_budget_usage(monkeypatch):
    """Défaut : budget no-op + usage avalé (les tests qui veulent l'inverse re-patchent)."""
    monkeypatch.setattr("collegue.monitoring.metrics.enforce_budget", lambda: None)
    monkeypatch.setattr("collegue.monitoring.sampling_usage.record_usage", lambda *a, **k: None)


# --- normalisation des messages ------------------------------------------------


def test_to_openai_messages_string_plus_system():
    msgs = to_openai_messages("salut", "tu es X")
    assert msgs == [{"role": "system", "content": "tu es X"}, {"role": "user", "content": "salut"}]


def test_to_openai_messages_list_roles_and_list_content():
    msgs = to_openai_messages(
        [{"role": "system", "content": "S"}, {"role": "user", "content": [{"text": "a"}, "b"]}],
        None,
    )
    assert msgs == [{"role": "system", "content": "S"}, {"role": "user", "content": "a b"}]


def test_to_openai_messages_guarantees_user_turn():
    msgs = to_openai_messages("", "S")  # system seul → un tour user est garanti
    assert msgs[0]["role"] == "system"
    assert any(m["role"] == "user" for m in msgs)


def test_to_openai_messages_none_content_becomes_empty_string():
    # content=None explicite → "" (jamais le littéral "None" envoyé au modèle).
    msgs = to_openai_messages([{"role": "user", "content": None}], None)
    assert msgs == [{"role": "user", "content": ""}]


# --- choix du modèle + coercition ---------------------------------------------


def test_pick_model_prefers_first_preference():
    assert _pick_model(["gpt-x"], "default") == "gpt-x"
    assert _pick_model([], "default") == "default"
    assert _pick_model(None, "default") == "default"


class _Spec:
    def __init__(self, title):
        self.title = title

    @classmethod
    def model_validate(cls, data):
        return cls(data["title"])


def test_coerce_parses_json_into_result_type():
    out = _coerce('{"title": "T"}', _Spec)
    assert isinstance(out, _Spec) and out.title == "T"


def test_coerce_falls_back_to_text_on_unparsable():
    assert _coerce("pas du json", _Spec) == "pas du json"


# --- sample() ------------------------------------------------------------------


async def test_sample_returns_text_and_records_usage(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        "collegue.monitoring.sampling_usage.record_usage",
        lambda p, c, m: seen.update(p=p, c=c, m=m),
    )
    usage = SimpleNamespace(prompt_tokens=12, completion_tokens=3)
    client = _FakeClient(_resp("bonjour", model="gemma-x", usage=usage))
    ctx = LocalSamplingContext(default_model="d", client=client)

    res = await ctx.sample("hi", model_preferences=["gemma-x"], max_tokens=2000)

    assert isinstance(res, SampleResult)
    assert res.text == "bonjour"
    assert res.result is None  # pas de result_type
    assert client.calls[0]["model"] == "gemma-x"
    assert client.calls[0]["max_tokens"] == 2000  # cap explicite de l'appelant RESPECTÉ
    assert seen == {"p": 12, "c": 3, "m": "gemma-x"}


async def test_sample_defaults_max_tokens_when_absent():
    client = _FakeClient(_resp("ok"))
    ctx = LocalSamplingContext(default_model="d", client=client)
    await ctx.sample("hi")  # aucun max_tokens → défaut généreux
    assert client.calls[0]["max_tokens"] == DEFAULT_MAX_TOKENS


async def test_sample_coerces_result_type():
    client = _FakeClient(_resp('{"title": "T"}'))
    ctx = LocalSamplingContext(default_model="d", client=client)
    res = await ctx.sample("x", result_type=_Spec)
    assert isinstance(res.result, _Spec) and res.result.title == "T"


async def test_sample_enforces_budget_before_call(monkeypatch):
    class _Stop(BaseException):
        pass

    def _boom():
        raise _Stop()

    monkeypatch.setattr("collegue.monitoring.metrics.enforce_budget", _boom)
    client = _FakeClient(_resp("never"))
    ctx = LocalSamplingContext(default_model="d", client=client)
    with pytest.raises(_Stop):
        await ctx.sample("hi")
    assert client.calls == []  # le budget coupe AVANT l'appel LLM


async def test_noop_stubs_are_awaitable():
    ctx = LocalSamplingContext(default_model="d", client=_FakeClient(_resp("x")))
    assert await ctx.info("m") is None
    assert await ctx.debug("m") is None
    assert await ctx.warning("m") is None
    assert await ctx.error("m") is None
    assert await ctx.report_progress(1, 2) is None


async def test_aclose_closes_client():
    client = _FakeClient(_resp("x"))
    ctx = LocalSamplingContext(default_model="d", client=client)
    await ctx.aclose()
    assert client.closed is True


# --- from_settings -------------------------------------------------------------


def test_from_settings_resolves_endpoint_without_limiter(monkeypatch):
    # Les LLM_RATE_LIMIT_* (middleware serveur par-client) NE doivent PAS throttler
    # le ctx du moteur : aucun limiter assemblé depuis la config, même si définis.
    monkeypatch.setattr(
        "collegue.core.llm.sampling_handler.resolve_openai_endpoint",
        lambda s: ("modX", "k", "http://bu/"),
    )
    settings = SimpleNamespace(LLM_RATE_LIMIT_PER_MINUTE=5, LLM_RATE_LIMIT_PER_DAY=500)
    ctx = LocalSamplingContext.from_settings(settings)
    assert ctx._default_model == "modX"
    assert ctx._api_key == "k"
    assert ctx._base_url == "http://bu/"
    assert ctx._limiter is None  # pas de throttle moteur par défaut


def test_rate_limiter_injectable_via_constructor():
    rl = PerModelRateLimiter(per_minute=15, per_day=1500)
    ctx = LocalSamplingContext(default_model="m", rate_limiter=rl)
    assert ctx._limiter is rl  # cadençage opt-in pour qui le veut


# --- rate limiter (maths, sans dormir) -----------------------------------------


def test_rate_limiter_wait_needed_when_minute_full():
    rl = PerModelRateLimiter(per_minute=1, per_day=0)
    now = 1000.0
    rl._minute["m"] = deque([now])
    rl._day["m"] = deque([now])
    assert rl._wait_needed("m", now + 1) > 0  # créneau pris → attente
    assert rl._wait_needed("m", now + 61) == 0  # après 60 s → libre


# --- routage abonnement (modèle non-Gemini → sampler sandbox) -------------------


def _sub_ctx(runner):
    return LocalSamplingContext(
        default_model="d",
        subscription_enabled=True,
        subscription_auth_dir="/home/u/.openhands",
        sampler_script="/repo/collegue/executor/oh_sampler.py",
        runner=runner,
    )


def test_is_subscription_model_routes_non_gemini_only():
    ctx = _sub_ctx(runner=lambda a, p: (0, "", ""))
    assert ctx._is_subscription_model("gpt-5.4") is True
    assert ctx._is_subscription_model("gemma-4-26b-a4b-it") is False
    assert ctx._is_subscription_model("gemini-2.5-pro") is False


def test_subscription_disabled_never_routes():
    ctx = LocalSamplingContext(default_model="d")  # subscription_enabled=False
    assert ctx._is_subscription_model("gpt-5.4") is False


async def test_sample_gpt_model_goes_through_subscription_sampler(monkeypatch):
    captured = {}

    def _runner(argv, payload):
        captured["argv"] = argv
        captured["payload"] = payload
        return (0, "bannière...\n<<<SAMPLE_BEGIN>>>verdict du juge<<<SAMPLE_END>>>", "")

    ctx = _sub_ctx(_runner)
    res = await ctx.sample("revois ce diff", system_prompt="Tu es QA", model_preferences=["gpt-5.4"])
    assert res.text == "verdict du juge"  # texte extrait des marqueurs
    # docker run avec abonnement + creds + sampler montés
    argv = captured["argv"]
    assert argv[:3] == ["docker", "run", "--rm"] or argv[0] == "docker"
    assert "LLM_MODEL=gpt-5.4" in argv and "LLM_SUBSCRIPTION=1" in argv
    assert any("/home/u/.openhands:" in a for a in argv)
    assert any("oh_sampler.py:ro" in a for a in argv)
    # Durcissement du conteneur sampler (M1 revue) : caps droppées + no-new-privileges.
    assert "ALL" in argv and "no-new-privileges" in argv
    payload = __import__("json").loads(captured["payload"])
    assert payload["system"] == "Tu es QA" and "revois ce diff" in payload["prompt"]


async def test_subscription_sampler_records_trusted_usage_envelope(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        "collegue.monitoring.sampling_usage.record_usage",
        lambda prompt, completion, model: seen.update(prompt=prompt, completion=completion, model=model),
    )
    output = (
        "<<<SAMPLE_BEGIN>>>ok<<<SAMPLE_END>>>"
        '<<<SAMPLE_USAGE>>>{"prompt_tokens":12,"completion_tokens":4,'
        '"model":"gpt-5.4","billable":false}<<<SAMPLE_USAGE_END>>>'
    )
    ctx = _sub_ctx(runner=lambda _argv, _payload: (0, output, ""))

    result = await ctx.sample("x", model_preferences=["gpt-5.4"])

    assert result.text == "ok"
    assert seen == {"prompt": 12, "completion": 4, "model": "gpt-5.4"}


async def test_subscription_sampler_rejects_untrusted_usage_envelope():
    output = (
        "<<<SAMPLE_BEGIN>>>ok<<<SAMPLE_END>>>"
        '<<<SAMPLE_USAGE>>>{"prompt_tokens":12,"completion_tokens":4,'
        '"model":"gpt-5.4","billable":true}<<<SAMPLE_USAGE_END>>>'
    )
    ctx = _sub_ctx(runner=lambda _argv, _payload: (0, output, ""))

    with pytest.raises(RuntimeError, match="enveloppe usage invalide"):
        await ctx.sample("x", model_preferences=["gpt-5.4"])


async def test_sample_subscription_failure_raises(monkeypatch):
    ctx = _sub_ctx(runner=lambda a, p: (1, "", "auth expirée"))
    import pytest as _pytest

    with _pytest.raises(RuntimeError) as exc:
        await ctx.sample("x", model_preferences=["gpt-5.4"])
    assert "auth expirée" in str(exc.value)


async def test_sample_subscription_empty_verdict_raises():
    # rc=0 mais verdict VIDE entre marqueurs → fail-closed (ne pas rendre "" au reviewer).
    import pytest as _pytest

    ctx = _sub_ctx(runner=lambda a, p: (0, "<<<SAMPLE_BEGIN>>>   <<<SAMPLE_END>>>", ""))
    with _pytest.raises(RuntimeError) as exc:
        await ctx.sample("x", model_preferences=["gpt-5.4"])
    assert "vide" in str(exc.value)


async def test_sample_gemini_model_does_not_use_subscription():
    # Même avec l'abonnement activé, un modèle Gemini reste sur l'endpoint OpenAI-compat.
    client = _FakeClient(_resp("réponse gemma"))
    ctx = LocalSamplingContext(
        default_model="d",
        subscription_enabled=True,
        subscription_auth_dir="/a",
        sampler_script="/s",
        client=client,
        runner=lambda a, p: (_ for _ in ()).throw(AssertionError("ne doit pas sampler")),
    )
    res = await ctx.sample("hi", model_preferences=["gemma-4-26b-a4b-it"])
    assert res.text == "réponse gemma"  # passé par le client OpenAI-compat, pas le sampler
