"""Transports LLM sous le registre durable (vague 2) : on montre les appels RÉELLEMENT émis.

Un faux client OpenAI-compatible enregistre chaque appel émis ET l'état du registre PENDANT l'appel
(la réservation doit déjà exister avant l'émission). Les erreurs sont celles du vrai SDK ``openai``
(``RateLimitError``, ``APIConnectionError`` + cause ``httpx``) pour que la classification soit réelle.
Aucun réseau, aucune clé.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import openai
import pytest

from collegue.core.llm.budget_guard import bind_budget, budget_role, estimate_call
from collegue.core.llm.sampling_ctx import LocalSamplingContext
from collegue.monitoring.metrics import MetricsCollector
from collegue.state import BudgetRefused, ProjectStateManager
from collegue.state.budget_ledger import REFUSED_BLOCKED, REFUSED_CAP_USD, REFUSED_DEADLINE, REFUSED_UNBOUNDED

MODEL = "gemini-3.5-flash"  # (1,50 $ / 9,00 $) par million de tokens dans la grille
SETTINGS = SimpleNamespace(LLM_PROVIDER="gemini", LLM_MODEL=MODEL, LLM_CALL_TIMEOUT=0.0)


@pytest.fixture(autouse=True)
def _isolated_metrics(tmp_path, monkeypatch):
    """Le MetricsCollector persiste sur disque : confiné à tmp_path, jamais le COLLEGUE_HOME du rôle."""
    monkeypatch.setattr(MetricsCollector, "_PERSIST_DIR", tmp_path / "monitoring")


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch):
    async def instant(_delay):
        return None

    monkeypatch.setattr(asyncio, "sleep", instant)


def _response(prompt=60, completion=40, model=MODEL, content="ok", usage=True):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
        usage=SimpleNamespace(prompt_tokens=prompt, completion_tokens=completion) if usage else None,
        model=model,
    )


def _status_error(code):
    request = httpx.Request("POST", "http://llm.invalid/v1/chat/completions")
    response = httpx.Response(code, request=request)
    cls = {429: openai.RateLimitError, 400: openai.BadRequestError, 503: openai.InternalServerError}.get(
        code, openai.APIStatusError
    )
    return cls(f"HTTP {code}", response=response, body=None)


def _connection_error(cause):
    error = openai.APIConnectionError(request=httpx.Request("POST", "http://llm.invalid"))
    error.__cause__ = cause
    return error


class FakeClient:
    """Client OpenAI-compatible scripté. ``probe`` photographie le registre pendant l'appel."""

    def __init__(self, script, probe=None):
        self.script = list(script)
        self.calls = []
        self.during = []
        self.options = []
        self._probe = probe
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def with_options(self, **options):
        """Comme le vrai SDK : une COPIE (non patchée par le handler) qui partage l'émission réelle."""
        self.options.append(options)
        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=self._create)))

    async def _create(self, **kwargs):
        self.calls.append(kwargs)
        if self._probe is not None:
            self.during.append(self._probe())
        step = self.script.pop(0)
        if isinstance(step, BaseException):
            raise step
        if callable(step):
            return await step()
        return step


@pytest.fixture
def env(tmp_path):
    manager = ProjectStateManager.from_url(f"sqlite:///{tmp_path / 'state.db'}", create=True)
    pid = manager.create_project(name="p")
    scope = manager.budget_ledger.scope_for_project(pid, max_cost_usd=1.0, max_tokens=1_000_000)
    return SimpleNamespace(manager=manager, ledger=manager.budget_ledger, key=scope.scope_key, pid=pid)


@pytest.fixture
def usd_env(tmp_path):
    """Scope à plafond USD SEUL : le seul cas où le sampler d'abonnement (0 $ établi) est accepté en strict."""
    manager = ProjectStateManager.from_url(f"sqlite:///{tmp_path / 'usd.db'}", create=True)
    pid = manager.create_project(name="p")
    scope = manager.budget_ledger.scope_for_project(pid, max_cost_usd=1.0)
    return SimpleNamespace(manager=manager, ledger=manager.budget_ledger, key=scope.scope_key, pid=pid)


def _ctx(client, **kwargs):
    return LocalSamplingContext(default_model=MODEL, client=client, max_retries=kwargs.pop("max_retries", 2), **kwargs)


async def _sample(ctx, env, text="bonjour", max_tokens=100, **kw):
    kw.setdefault("settings", SETTINGS)
    with bind_budget(env.ledger, env.key, **kw):
        return await ctx.sample(text, max_tokens=max_tokens)


# --- réservation AVANT émission, règlement APRÈS --------------------------------------------------


async def test_the_reservation_exists_before_the_call_and_actual_usage_settles_it(env):
    client = FakeClient([_response(60, 40)], probe=lambda: env.ledger.snapshot(env.key).reserved_usd)
    result = await _sample(_ctx(client), env)

    assert result.text == "ok" and len(client.calls) == 1
    assert client.during[0] > 0  # la réservation était DÉJÀ prise quand l'appel est parti
    snap = env.ledger.snapshot(env.key)
    expected = 60 * 1.5e-6 + 40 * 9e-6  # usage réel, au tarif autoritaire
    assert snap.consumed_usd == pytest.approx(expected, abs=1e-6)
    assert (snap.reserved_usd, snap.unknown_usd, snap.blocked_reason) == (0.0, 0.0, None)  # le reliquat est libéré


async def test_the_sdk_never_retries_internally_when_a_ledger_is_bound(env):
    client = FakeClient([_response()])
    await _sample(_ctx(client, max_retries=4), env)
    assert client.options == [{"max_retries": 0}]  # chaque retry est NOTRE boucle, donc réservé


async def test_over_budget_is_refused_before_any_call_is_emitted(env):
    env.ledger.scope_for_project(env.pid, max_cost_usd=0.001)  # plafond ridicule
    client = FakeClient([_response()])
    with pytest.raises(BudgetRefused) as refused:
        await _sample(_ctx(client), env, max_tokens=4096)
    assert refused.value.code == REFUSED_CAP_USD
    assert client.calls == []  # AUCUN appel émis
    assert env.ledger.snapshot(env.key).reserved_usd == 0.0


# --- retries et replis -----------------------------------------------------------------------------


async def test_each_retry_is_reserved_and_failed_attempts_cost_nothing(env):
    client = FakeClient(
        [_status_error(429), _status_error(429), _response(50, 30)],
        probe=lambda: env.ledger.snapshot(env.key).reserved_usd,
    )
    result = await _sample(_ctx(client, max_retries=2), env)

    assert result.text == "ok" and len(client.calls) == 3
    assert all(during > 0 for during in client.during)  # une réservation à CHAQUE tentative
    snap = env.ledger.snapshot(env.key)
    assert snap.consumed_usd == pytest.approx(50 * 1.5e-6 + 30 * 9e-6, abs=1e-6)  # seule la réussie est facturée
    assert (snap.reserved_usd, snap.unknown_usd) == (0.0, 0.0)
    reservations = env.ledger.reservations(env.key)
    assert [r.state for r in reservations] == ["released", "released", "committed"]


async def test_exhausted_retries_raise_the_last_error_with_everything_released(env):
    client = FakeClient([_status_error(429)] * 3)
    with pytest.raises(openai.RateLimitError):
        await _sample(_ctx(client, max_retries=2), env)
    assert len(client.calls) == 3
    snap = env.ledger.snapshot(env.key)
    assert (snap.consumed_usd, snap.reserved_usd, snap.unknown_usd) == (0.0, 0.0, 0.0)


async def test_a_non_retryable_status_is_released_and_not_retried(env):
    client = FakeClient([_status_error(400), _response()])
    with pytest.raises(openai.BadRequestError):
        await _sample(_ctx(client, max_retries=3), env)
    assert len(client.calls) == 1
    assert env.ledger.snapshot(env.key).reserved_usd == 0.0


async def test_a_connect_failure_is_released_but_a_read_failure_is_unknown(env):
    client = FakeClient([_connection_error(httpx.ConnectError("refused")), _response()])
    assert (await _sample(_ctx(client, max_retries=2), env)).text == "ok"
    assert len(client.calls) == 2  # la tentative sans connexion est libérée puis retentée
    assert not env.ledger.snapshot(env.key).blocked

    client2 = FakeClient([_connection_error(httpx.ReadError("reset after send")), _response()])
    with pytest.raises(openai.APIConnectionError):
        await _sample(_ctx(client2, max_retries=2), env)
    assert len(client2.calls) == 1  # indéterminé : on ne retente PAS un appel peut-être déjà facturé
    snap = env.ledger.snapshot(env.key)
    assert snap.unknown_usd > 0 and snap.blocked


async def test_a_model_change_is_reserved_separately_per_model(env):
    """Repli de modèle : chaque modèle est réservé et réglé à SON tarif, dans le même registre."""
    for model in ("gemini-3.5-flash", "gemini-2.5-flash"):
        client = FakeClient([_response(60, 40, model=model)])
        ctx = LocalSamplingContext(default_model=model, client=client)
        with bind_budget(env.ledger, env.key, settings=SETTINGS):
            await ctx.sample("x", max_tokens=100)
    rows = env.ledger.reservations(env.key)
    assert [r.state for r in rows] == ["committed", "committed"]
    expected = (60 * 1.5e-6 + 40 * 9e-6) + (60 * 0.3e-6 + 40 * 2.5e-6)
    assert env.ledger.snapshot(env.key).consumed_usd == pytest.approx(expected, abs=2e-6)


# --- interruption, usage inconnu, échéance -----------------------------------------------------------


async def test_a_call_cancelled_by_a_deadline_is_unknown_and_blocks_the_next_one(env):
    async def never():
        await asyncio.sleep(3600)

    # asyncio.sleep est neutralisé par la fixture : on attend sur un Event pour ne jamais rendre la main.
    gate = asyncio.Event()

    async def hang():
        await gate.wait()

    client = FakeClient([hang, _response()])
    with bind_budget(env.ledger, env.key, settings=SETTINGS):
        ctx = _ctx(client)
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(ctx.sample("x", max_tokens=100), timeout=0.05)
        snap = env.ledger.snapshot(env.key)
        assert snap.unknown_usd > 0 and snap.blocked  # émis puis interrompu : INCONNU, jamais zéro
        with pytest.raises(BudgetRefused) as refused:
            await ctx.sample("y", max_tokens=100)
    assert refused.value.code == REFUSED_BLOCKED
    assert len(client.calls) == 1  # le second appel n'a PAS été émis


async def test_a_response_without_usage_is_unknown_not_free(env):
    client = FakeClient([_response(usage=False)])
    result = await _sample(_ctx(client), env)
    assert result.text == "ok"  # le résultat obtenu est rendu…
    snap = env.ledger.snapshot(env.key)
    assert snap.unknown_usd > 0 and snap.consumed_usd == 0.0 and snap.blocked  # …mais la dépense n'est pas « 0 »


async def test_an_expired_run_deadline_emits_nothing(env):
    client = FakeClient([_response()])
    past = datetime.now(timezone.utc) - timedelta(seconds=1)
    with pytest.raises(BudgetRefused) as refused:
        await _sample(_ctx(client), env, deadline=past)
    assert refused.value.code == REFUSED_DEADLINE and client.calls == []


# --- fournisseur non bornable ---------------------------------------------------------------------------


async def test_a_model_without_authoritative_price_is_refused_under_a_usd_cap(env):
    client = FakeClient([_response(model="mystery-9")])
    ctx = LocalSamplingContext(default_model="mystery-9", client=client)
    with pytest.raises(BudgetRefused) as refused:
        await _sample(ctx, env)
    assert refused.value.code == REFUSED_UNBOUNDED and client.calls == []


async def test_the_same_model_is_bounded_by_tokens_when_there_is_no_usd_cap(tmp_path):
    manager = ProjectStateManager.from_url(f"sqlite:///{tmp_path / 's.db'}", create=True)
    pid = manager.create_project(name="p")
    key = manager.budget_ledger.scope_for_project(pid, max_tokens=5000).scope_key
    client = FakeClient([_response(60, 50, model="mystery-9"), _response(60, 50, model="mystery-9")])
    ctx = LocalSamplingContext(default_model="mystery-9", client=client)
    env = SimpleNamespace(ledger=manager.budget_ledger, key=key)
    attested = SimpleNamespace(**vars(SETTINGS), BUDGET_ATTESTED_BYTE_TOKENIZER_MODELS="mystery-9")
    await _sample(ctx, env, max_tokens=500, settings=attested)
    assert manager.budget_ledger.snapshot(key).consumed_tokens == 110  # borné et compté en tokens
    with pytest.raises(BudgetRefused):
        await _sample(ctx, env, max_tokens=5000, settings=attested)  # ne tient plus dans les tokens restants
    assert len(client.calls) == 1


def test_local_providers_are_free_and_remain_bounded_by_tokens():
    settings = SimpleNamespace(LLM_PROVIDER="lmstudio", LLM_MODEL="local-model")
    est = estimate_call(model="local-model", messages="x" * 100, max_tokens=50, settings=settings, capped_usd=True)
    assert est.micro_usd == 0 and est.tokens == 102 + 16 + 64 + 50  # octets sérialisés (guillemets) + cadrage + sortie


# --- persistance : jamais un zéro ------------------------------------------------------------------------


async def test_a_commit_that_cannot_be_persisted_refuses_in_strict_and_keeps_the_reservation(env, monkeypatch):
    client = FakeClient([_response(60, 40)])
    real_commit = env.ledger.commit

    def broken_commit(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(env.ledger, "commit", broken_commit)
    with pytest.raises(BudgetRefused) as refused:
        await _sample(_ctx(client), env)
    assert refused.value.code == "ledger_unavailable"
    monkeypatch.setattr(env.ledger, "commit", real_commit)
    snap = env.ledger.snapshot(env.key)
    assert snap.reserved_usd > 0 and snap.consumed_usd == 0.0  # l'estimation reste comptée : jamais un zéro


# --- rôle, MetricsCollector -------------------------------------------------------------------------------


async def test_reservations_carry_the_role_and_model(env):
    client = FakeClient([_response()])
    with budget_role("reviewer"):
        await _sample(_ctx(client), env)
    (row,) = env.ledger.reservations(env.key)
    with env.manager.session() as session:
        from collegue.state.models import BudgetReservation

        stored = session.query(BudgetReservation).one()
        assert (stored.role, stored.model, stored.transport) == ("reviewer", MODEL, "openai-compatible-http")
    assert row.state == "committed"


async def test_the_metrics_collector_no_longer_decides_when_a_ledger_is_bound(env):
    """Pas de double comptage : un collector « plein » ne bloque pas, le registre est seul juge."""
    from collegue.core.llm.client import accounted_sample
    from collegue.monitoring.metrics import MetricsCollector

    collector = MetricsCollector(input_cost_per_token=0.0, output_cost_per_token=0.0)
    collector.record_execution("expert", 1.0, True, cost_usd=999.0)  # le collector dirait « dépassé »
    client = FakeClient([_response(100, 50)])
    ctx = _ctx(client)
    settings = SimpleNamespace(
        LLM_PROVIDER="gemini", LLM_MODEL=MODEL, MAX_COST_USD=1.0, MAX_TOKENS_BUDGET=0, BUDGET_EXHAUSTED_ACTION="pause"
    )
    with bind_budget(env.ledger, env.key, settings=settings):
        result = await accounted_sample(
            ctx, role="planner", operation="planner.spec", settings_obj=settings, collector=collector, messages="x"
        )
    assert result.text == "ok" and len(client.calls) == 1
    (row,) = env.ledger.reservations(env.key)
    assert row.state == "committed"


# --- sampler d'abonnement ---------------------------------------------------------------------------------


def _sub_ctx(tmp_path, runner):
    script = tmp_path / "oh_sampler.py"
    script.write_text("print('x')\n")
    auth = tmp_path / "auth"
    auth.mkdir()
    return LocalSamplingContext(
        default_model="d",
        subscription_enabled=True,
        subscription_auth_dir=str(auth),
        sampler_script=str(script),
        runner=runner,
    )


def _envelope(prompt=60, completion=40):
    usage = {"prompt_tokens": prompt, "completion_tokens": completion, "model": "gpt-5.4", "billable": False}
    return f"<<<SAMPLE_BEGIN>>>verdict<<<SAMPLE_END>>>\n<<<SAMPLE_USAGE>>>{json.dumps(usage)}<<<SAMPLE_USAGE_END>>>"


async def test_the_subscription_sampler_reserves_tokens_before_launch_and_settles_with_the_envelope(usd_env, tmp_path):
    seen = {}

    def runner(argv, payload):
        seen["during"] = usd_env.ledger.snapshot(usd_env.key).reserved_tokens
        seen["argv"] = argv
        return 0, _envelope(), ""

    ctx = _sub_ctx(tmp_path, runner)
    with bind_budget(usd_env.ledger, usd_env.key, settings=SETTINGS):
        res = await ctx.sample("revois", model_preferences=["gpt-5.4"], max_tokens=100)
    assert res.text == "verdict" and seen["during"] > 0
    snap = usd_env.ledger.snapshot(usd_env.key)
    assert (snap.consumed_tokens, snap.consumed_usd, snap.reserved_tokens) == (100, 0.0, 0)  # abonnement : 0 $
    # conteneur nommé et auto-limité : tuer le client docker ne laisse pas un conteneur dépenser
    assert "--name" in seen["argv"] and "timeout" in seen["argv"]


async def test_a_failing_subscription_sampler_is_unknown_and_blocks_under_a_cap(usd_env, tmp_path):
    ctx = _sub_ctx(tmp_path, lambda argv, payload: (1, "", "boom"))
    with bind_budget(usd_env.ledger, usd_env.key, settings=SETTINGS):
        with pytest.raises(RuntimeError):
            await ctx.sample("revois", model_preferences=["gpt-5.4"], max_tokens=100)
    snap = usd_env.ledger.snapshot(usd_env.key)
    assert snap.unknown_tokens > 0 and snap.blocked


async def test_a_token_cap_in_strict_refuses_the_subscription_sampler_before_any_launch(env, tmp_path):
    """Pas de plafond de sortie prouvé côté backend abonnement ⇒ aucune garantie de tokens : refus, rien lancé."""
    launched = []
    ctx = _sub_ctx(tmp_path, lambda argv, payload: launched.append(argv) or (0, _envelope(), ""))
    with bind_budget(env.ledger, env.key, settings=SETTINGS):  # env : plafond de tokens 1 000 000, strict
        with pytest.raises(BudgetRefused) as refused:
            await ctx.sample("revois", model_preferences=["gpt-5.4"], max_tokens=100)
    assert refused.value.code == REFUSED_UNBOUNDED and launched == []


async def test_the_subscription_sampler_stays_available_in_advisory_mode(tmp_path):
    manager = ProjectStateManager.from_url(f"sqlite:///{tmp_path / 'adv.db'}", create=True)
    pid = manager.create_project(name="p")
    key = manager.budget_ledger.scope_for_project(pid, max_tokens=1000, strict=False).scope_key
    ctx = _sub_ctx(tmp_path, lambda argv, payload: (0, _envelope(), ""))
    with bind_budget(manager.budget_ledger, key, settings=SETTINGS):
        assert (await ctx.sample("revois", model_preferences=["gpt-5.4"], max_tokens=100)).text == "verdict"


async def test_a_free_subscription_call_is_accepted_when_the_usd_cap_is_reached_exactly(usd_env, tmp_path):
    """0 $ de plus ne dépasse pas un plafond USD atteint exactement : un appel autoritairement gratuit est accepté."""
    ctx = _sub_ctx(tmp_path, lambda argv, payload: (0, _envelope(), ""))
    usd_env.ledger.scope_for_project(usd_env.pid, max_cost_usd=0.000001)
    usd_env.ledger.reserve(usd_env.key, usd=0.000001, tokens=0)  # plafond USD atteint exactement
    with bind_budget(usd_env.ledger, usd_env.key, settings=SETTINGS):
        assert (await ctx.sample("revois", model_preferences=["gpt-5.4"], max_tokens=100)).text == "verdict"


async def test_a_blocked_scope_launches_no_subscription_container(usd_env, tmp_path):
    """Usage inconnu déjà établi ⇒ blocage strict : aucun conteneur, même pour un transport gratuit."""
    launched = []
    ctx = _sub_ctx(tmp_path, lambda argv, payload: launched.append(argv) or (0, _envelope(), ""))
    unknown = usd_env.ledger.reserve(usd_env.key, usd=0.01, tokens=0)
    usd_env.ledger.mark_unknown(unknown.reservation_id, reason="appel précédent interrompu")
    with bind_budget(usd_env.ledger, usd_env.key, settings=SETTINGS):
        with pytest.raises(BudgetRefused) as refused:
            await ctx.sample("revois", model_preferences=["gpt-5.4"], max_tokens=100)
    assert refused.value.code == REFUSED_BLOCKED and launched == []


async def test_an_overspent_usd_scope_launches_no_subscription_container(usd_env, tmp_path):
    """Dépassement déjà ÉTABLI (consommation réelle > plafond) : le strict refuse la suite, même gratuite."""
    launched = []
    ctx = _sub_ctx(tmp_path, lambda argv, payload: launched.append(argv) or (0, _envelope(), ""))
    held = usd_env.ledger.reserve(usd_env.key, usd=0.5, tokens=0)
    usd_env.ledger.commit(held.reservation_id, usd=1.5, tokens=0)  # le fournisseur a facturé plus que le plafond (1 $)
    with bind_budget(usd_env.ledger, usd_env.key, settings=SETTINGS):
        with pytest.raises(BudgetRefused) as refused:
            await ctx.sample("revois", model_preferences=["gpt-5.4"], max_tokens=100)
    assert refused.value.code == REFUSED_CAP_USD and launched == []


async def test_the_sampler_container_is_killed_by_name_on_a_host_timeout(usd_env, tmp_path, monkeypatch):
    import collegue.core.llm.sampling_ctx as sc

    killed = []
    monkeypatch.setattr(sc, "_kill_container", lambda name: killed.append(name))

    def fake_run(argv, **kwargs):
        raise sc.subprocess.TimeoutExpired(argv, kwargs["timeout"])

    monkeypatch.setattr(sc.subprocess, "run", fake_run)
    ctx = _sub_ctx(tmp_path, None)
    with bind_budget(usd_env.ledger, usd_env.key, settings=SETTINGS):
        with pytest.raises(Exception):
            await ctx.sample("revois", model_preferences=["gpt-5.4"], max_tokens=100)
    assert len(killed) == 1 and killed[0].startswith("collegue-smp-")
    assert usd_env.ledger.snapshot(usd_env.key).unknown_tokens > 0  # interrompu après lancement : usage inconnu


# --- handler serveur ----------------------------------------------------------------------------------------


async def test_the_server_sampling_handler_goes_through_the_same_guard(env):
    from collegue.core.llm.sampling_handler import _make_handler_class

    client = FakeClient([_status_error(429), _response(50, 20)])
    handler = _make_handler_class()(default_model=MODEL, client=client)
    with bind_budget(env.ledger, env.key, settings=SETTINGS):
        response = await handler.client.chat.completions.create(
            model=MODEL, messages=[{"role": "user", "content": "x"}], max_tokens=64
        )
    assert response.model == MODEL and len(client.calls) == 2  # la 429 est retentée par NOTRE boucle
    assert [r.state for r in env.ledger.reservations(env.key)] == ["released", "committed"]


# --- borne HAUTE du payload complet (contre-test du manager : « chars/2 + 32 » n'en est pas une) -------------


def test_the_prompt_bound_counts_utf8_bytes_not_characters():
    text = "漢" * 100  # 100 caractères, 300 octets UTF-8 : au moins 100 tokens d'octet dans le pire cas
    est = estimate_call(model=MODEL, messages=text, max_tokens=10, settings=SETTINGS, require_bound=True)
    assert est.prompt_tokens >= 300 > 100 // 2 + 32


def test_the_whole_transmitted_payload_is_counted_system_tools_and_schemas():
    base = [{"role": "user", "content": "x"}]
    with_system = [{"role": "system", "content": "s" * 400}] + base
    tools = [{"type": "function", "function": {"name": "t", "parameters": {"type": "object", "d": "p" * 500}}}]
    plain = estimate_call(model=MODEL, messages=base, max_tokens=10, settings=SETTINGS).prompt_tokens
    assert (
        estimate_call(model=MODEL, messages=with_system, max_tokens=10, settings=SETTINGS).prompt_tokens >= plain + 400
    )
    assert (
        estimate_call(model=MODEL, messages=base, tools=tools, max_tokens=10, settings=SETTINGS).prompt_tokens
        >= plain + 500
    )


@pytest.mark.parametrize("part", [{"type": "image_url", "image_url": {"url": "data:..."}}, {"type": "input_audio"}])
def test_a_non_text_modality_is_refused_in_strict(part):
    messages = [{"role": "user", "content": [{"type": "text", "text": "x"}, part]}]
    with pytest.raises(BudgetRefused) as refused:
        estimate_call(model=MODEL, messages=messages, max_tokens=10, settings=SETTINGS, require_bound=True)
    assert refused.value.code == REFUSED_UNBOUNDED


def test_an_unknown_tokenizer_family_or_unbounded_output_is_refused_in_strict():
    with pytest.raises(BudgetRefused) as unknown:
        estimate_call(model="mystery-9", messages="x", max_tokens=10, settings=SETTINGS, require_bound=True)
    assert unknown.value.code == REFUSED_UNBOUNDED
    for missing in (0, None, "oops"):
        with pytest.raises(BudgetRefused) as unbounded:
            estimate_call(model=MODEL, messages="x", max_tokens=missing, settings=SETTINGS, require_bound=True)
        assert unbounded.value.code == REFUSED_UNBOUNDED


async def test_a_non_text_request_emits_nothing_through_the_public_handler(env):
    from collegue.core.llm.sampling_handler import _make_handler_class

    client = FakeClient([_response()])
    handler = _make_handler_class()(default_model=MODEL, client=client)
    messages = [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "data:image/png;base64,AA"}}]}]
    with bind_budget(env.ledger, env.key, settings=SETTINGS):
        with pytest.raises(BudgetRefused) as refused:
            await handler.client.chat.completions.create(model=MODEL, messages=messages, max_tokens=64)
    assert refused.value.code == REFUSED_UNBOUNDED and client.calls == []


async def test_the_handler_transmits_a_bounding_max_tokens_when_the_caller_gave_none(env):
    from collegue.core.llm.sampling_handler import DEFAULT_BOUNDED_MAX_TOKENS, _make_handler_class

    client = FakeClient([_response()])
    handler = _make_handler_class()(default_model=MODEL, client=client)
    with bind_budget(env.ledger, env.key, settings=SETTINGS):
        await handler.client.chat.completions.create(model=MODEL, messages=[{"role": "user", "content": "x"}])
    assert client.calls[0]["max_tokens"] == DEFAULT_BOUNDED_MAX_TOKENS  # la sortie est réellement bornée


async def test_a_provider_exceeding_the_bound_is_recorded_in_full_and_blocks_the_scope(env):
    client = FakeClient([_response(prompt=5000, completion=40)])  # bien au-delà des octets transmis
    await _sample(_ctx(client), env)
    snap = env.ledger.snapshot(env.key)
    assert snap.consumed_tokens == 5040 and snap.blocked  # consommation réelle ENTIÈRE, suite bloquée


# --- une erreur HTTP ne prouve pas l'absence de facturation ---------------------------------------------------


async def test_a_5xx_after_emission_is_unknown_never_retried_and_blocks(env):
    client = FakeClient([_status_error(503), _response()])
    with pytest.raises(openai.InternalServerError):
        await _sample(_ctx(client, max_retries=3), env)
    assert len(client.calls) == 1  # aucun retry : l'appel a pu être facturé
    snap = env.ledger.snapshot(env.key)
    assert snap.unknown_usd > 0 and snap.blocked
    assert [r.state for r in env.ledger.reservations(env.key)] == ["unknown"]


async def test_a_5xx_is_unknown_through_the_server_handler_too(env):
    from collegue.core.llm.sampling_handler import _make_handler_class

    client = FakeClient([_status_error(503), _response()])
    handler = _make_handler_class()(default_model=MODEL, client=client)
    with bind_budget(env.ledger, env.key, settings=SETTINGS):
        with pytest.raises(openai.InternalServerError):
            await handler.client.chat.completions.create(
                model=MODEL, messages=[{"role": "user", "content": "x"}], max_tokens=64
            )
    assert len(client.calls) == 1 and env.ledger.snapshot(env.key).blocked


async def test_a_client_timeout_is_unknown_and_not_retried(env):
    timeout = openai.APITimeoutError(request=httpx.Request("POST", "http://llm.invalid"))
    client = FakeClient([timeout, _response()])
    with pytest.raises(openai.APITimeoutError):
        await _sample(_ctx(client, max_retries=3), env)
    assert len(client.calls) == 1 and env.ledger.snapshot(env.key).unknown_usd > 0


# --- l'échéance s'applique PENDANT l'appel et les backoffs ------------------------------------------------------


async def test_the_deadline_cancels_an_in_flight_call_and_leaves_its_usage_unknown(env):
    gate = asyncio.Event()

    async def hang():
        await gate.wait()

    client = FakeClient([hang, _response()])
    deadline = datetime.now(timezone.utc) + timedelta(milliseconds=150)
    with pytest.raises(BudgetRefused) as refused:
        await _sample(_ctx(client), env, deadline=deadline)
    assert refused.value.code == REFUSED_DEADLINE
    snap = env.ledger.snapshot(env.key)
    assert snap.unknown_usd > 0 and snap.blocked and len(client.calls) == 1


async def test_a_backoff_that_would_cross_the_deadline_emits_no_further_call(env):
    client = FakeClient([_status_error(429), _response()])
    deadline = datetime.now(timezone.utc) + timedelta(milliseconds=50)
    with pytest.raises(BudgetRefused) as refused:
        await _sample(_ctx(client, max_retries=3), env, deadline=deadline)
    assert refused.value.code == REFUSED_DEADLINE and len(client.calls) == 1
    assert env.ledger.snapshot(env.key).reserved_usd == 0.0  # la 429 rejetée est libérée


async def test_the_strict_subscription_sampler_gets_a_bounded_output_and_no_internal_retry(usd_env, tmp_path):
    seen = {}

    def runner(argv, payload):
        seen["payload"] = json.loads(payload)
        return 0, _envelope(), ""

    with bind_budget(usd_env.ledger, usd_env.key, settings=SETTINGS):
        await _sub_ctx(tmp_path, runner).sample("revois", model_preferences=["gpt-5.4"], max_tokens=100)
    assert seen["payload"]["strict"] is True and seen["payload"]["max_output_tokens"] > 0


# --- bornes : structure sérialisée, grille tarifaire, endpoint ---------------------------------------------------


def test_the_bound_counts_the_serialized_structure_not_only_keys_and_values():
    ints = list(range(1000, 3000))
    tools = [{"type": "function", "function": {"name": "t", "parameters": {"enum": ints}}}]
    base = estimate_call(model=MODEL, messages="x", max_tokens=10, settings=SETTINGS).prompt_tokens
    with_tools = estimate_call(model=MODEL, messages="x", tools=tools, max_tokens=10, settings=SETTINGS).prompt_tokens
    assert with_tools - base >= 2000 * 6  # 4 chiffres + « , » par entier : les délimiteurs comptent


def test_a_prompt_beyond_the_tariff_grid_domain_is_refused_under_a_usd_cap():
    huge = "a" * 250_000  # > 200k tokens dans le pire cas : la grille standard ne borne pas ce tarif
    with pytest.raises(BudgetRefused) as refused:
        estimate_call(model=MODEL, messages=huge, max_tokens=10, settings=SETTINGS, capped_usd=True, require_bound=True)
    assert refused.value.code == REFUSED_UNBOUNDED and "200000" in str(refused.value)


def test_an_explicit_operator_price_is_not_limited_to_the_grid_domain():
    settings = SimpleNamespace(
        LLM_PROVIDER="gemini",
        LLM_MODEL="gemini-x",
        LLM_PRICE_PROMPT_PER_1M=5.0,
        LLM_PRICE_COMPLETION_PER_1M=15.0,
        BUDGET_ATTESTED_BYTE_TOKENIZER_MODELS="gemini-x",  # un tarif explicite n'atteste PAS le tokenizer
    )
    est = estimate_call(
        model="gemini-x", messages="a" * 250_000, max_tokens=10, settings=settings, capped_usd=True, require_bound=True
    )
    assert est.prompt_tokens > 250_000 and est.micro_usd > 0


@pytest.mark.parametrize(
    ("provider", "base_url", "model", "justified"),
    [
        ("gemini", None, "gemini-3.5-flash", True),
        ("gemini", None, "gemma-4-31b-it", True),
        ("openai", None, "gpt-4o-mini", True),
        ("openai", "http://gateway.invalid/v1", "gpt-4o-mini", False),  # base_url custom : tokenizer inconnu
        ("lmstudio", "http://localhost:1234/v1", "gemma-local", False),  # local : un nom n'est pas une preuve
        ("gemini", None, "gpt-4o-mini", False),  # famille incompatible avec l'endpoint hébergé
        ("openai", None, "claude-sonnet-4", False),
    ],
)
def test_the_tokenizer_bound_is_scoped_to_the_endpoint_and_family(provider, base_url, model, justified):
    from collegue.core.llm.budget_guard import tokenizer_is_byte_bounded

    settings = SimpleNamespace(LLM_PROVIDER=provider, llm_base_url=base_url)
    assert tokenizer_is_byte_bounded(model, settings) is justified
    attested = SimpleNamespace(
        LLM_PROVIDER=provider, llm_base_url=base_url, BUDGET_ATTESTED_BYTE_TOKENIZER_MODELS=model
    )
    assert tokenizer_is_byte_bounded(model, attested) is True  # l'opérateur peut attester explicitement


# --- identité exacte et destination réelle du transport ----------------------------------------------------------


def _client_at(base_url, script):
    client = FakeClient(script)
    client.base_url = base_url
    return client


async def test_an_unverified_model_name_is_refused_even_with_an_explicit_price(env):
    """Un tarif explicite n'atteste pas un tokenizer : ``gpt-unverified-derivative`` n'est PAS une identité connue."""
    settings = SimpleNamespace(
        LLM_PROVIDER="openai", LLM_PRICE_PROMPT_PER_1M=1.0, LLM_PRICE_COMPLETION_PER_1M=2.0, LLM_CALL_TIMEOUT=2
    )
    client = FakeClient([_response(model="gpt-unverified-derivative")])
    ctx = LocalSamplingContext(default_model="gpt-unverified-derivative", client=client)
    with pytest.raises(BudgetRefused) as refused:
        await _sample(ctx, env, settings=settings)
    assert refused.value.code == REFUSED_UNBOUNDED and client.calls == []
    assert "n'atteste pas un tokenizer" in str(refused.value)


async def test_the_real_destination_of_the_client_decides_not_an_unrelated_provider_setting(env):
    """``LLM_PROVIDER=gemini`` mais le client parle à une passerelle : la borne n'est pas justifiée."""
    settings = SimpleNamespace(LLM_PROVIDER="gemini", LLM_CALL_TIMEOUT=2)
    gateway = _client_at("http://gateway.invalid/v1/", [_response(model=MODEL)])
    with pytest.raises(BudgetRefused) as refused:
        await _sample(LocalSamplingContext(default_model=MODEL, client=gateway), env, settings=settings)
    assert refused.value.code == REFUSED_UNBOUNDED and gateway.calls == []

    hosted = _client_at("https://generativelanguage.googleapis.com/v1beta/openai/", [_response(model=MODEL)])
    result = await _sample(LocalSamplingContext(default_model=MODEL, client=hosted), env, settings=settings)
    assert result.text == "ok" and len(hosted.calls) == 1  # destination hébergée + identité connue : acceptée


async def test_an_exact_operator_attestation_admits_a_gateway_destination(env):
    gateway = _client_at("http://gateway.invalid/v1/", [_response(model="llama3-8b-instruct")])
    ctx = LocalSamplingContext(default_model="llama3-8b-instruct", client=gateway)
    usd_free = SimpleNamespace(
        LLM_PROVIDER="lmstudio",
        LLM_CALL_TIMEOUT=2,
        BUDGET_ATTESTED_BYTE_TOKENIZER_MODELS="llama3-8b-instruct",
    )
    result = await _sample(ctx, env, settings=usd_free)
    assert result.text == "ok" and len(gateway.calls) == 1
    other = _client_at("http://gateway.invalid/v1/", [_response(model="llama3-8b-instruct-derived")])
    with pytest.raises(BudgetRefused):  # l'attestation est une identité exacte, pas un préfixe
        await _sample(
            LocalSamplingContext(default_model="llama3-8b-instruct-derived", client=other), env, settings=usd_free
        )
    assert other.calls == []


async def test_a_long_prompt_on_a_grid_model_is_refused_with_an_honest_diagnostic(env):
    """Au-delà de 200k tokens la grille ne borne rien ; ``LLM_PRICE_*`` ne remplace pas la grille d'un modèle connu."""
    settings = SimpleNamespace(
        LLM_PROVIDER="gemini", LLM_PRICE_PROMPT_PER_1M=9.0, LLM_PRICE_COMPLETION_PER_1M=9.0, LLM_CALL_TIMEOUT=2
    )
    client = FakeClient([_response(model="gemini-2.5-pro")])
    ctx = LocalSamplingContext(default_model="gemini-2.5-pro", client=client)
    with pytest.raises(BudgetRefused) as refused:
        await _sample(ctx, env, text="哈" * 210_000, settings=settings)
    message = str(refused.value)
    assert refused.value.code == REFUSED_UNBOUNDED and client.calls == []
    assert "ne remplacent PAS la grille" in message and "configurer LLM_PRICE" not in message


def test_prices_resolve_from_the_grid_first_and_from_the_operator_only_for_unknown_models():
    from collegue.core.llm.budget_guard import SOURCE_CONFIGURED, SOURCE_GRID, resolve_prices_with_source

    settings = SimpleNamespace(LLM_PROVIDER="gemini", LLM_PRICE_PROMPT_PER_1M=9.0, LLM_PRICE_COMPLETION_PER_1M=9.0)
    assert resolve_prices_with_source("gemini-2.5-pro", settings)[1] == SOURCE_GRID  # la grille l'emporte
    assert resolve_prices_with_source("modele-inconnu", settings)[1] == SOURCE_CONFIGURED


# --- autorité tarifaire = destination réelle du transport ; attestation de tokenizer ≠ tarif -----------------------


def _scope(tmp_path, name, *, usd, tokens=None):
    manager = ProjectStateManager.from_url(f"sqlite:///{tmp_path / name}", create=True)
    pid = manager.create_project(name="p")
    key = manager.budget_ledger.scope_for_project(pid, max_cost_usd=usd, max_tokens=tokens).scope_key
    return SimpleNamespace(ledger=manager.budget_ledger, key=key, manager=manager)


def _openai_cloud(model, script):
    return _client_at("https://api.openai.com/v1/", script)


async def test_a_remote_billed_endpoint_is_priced_as_cloud_even_when_the_provider_is_declared_local(tmp_path):
    """``LLM_PROVIDER=lmstudio`` mais le client parle à l'API OpenAI : tarif cloud, jamais 0, avant ET après l'appel."""
    settings = SimpleNamespace(LLM_PROVIDER="lmstudio", LLM_CALL_TIMEOUT=2)
    tiny = _scope(tmp_path, "tiny.db", usd=0.000001)
    refused_client = _openai_cloud("gpt-5.4", [_response(2, 1, model="gpt-5.4")])
    with pytest.raises(BudgetRefused) as refused:
        await _sample(
            LocalSamplingContext(default_model="gpt-5.4", client=refused_client), tiny, settings=settings, max_tokens=8
        )
    assert refused.value.code == REFUSED_CAP_USD and refused_client.calls == []  # refusé AVANT émission

    roomy = _scope(tmp_path, "roomy.db", usd=100.0)
    client = _openai_cloud("gpt-5.4", [_response(2, 1, model="gpt-5.4")])
    await _sample(LocalSamplingContext(default_model="gpt-5.4", client=client), roomy, settings=settings, max_tokens=8)
    snap = roomy.ledger.snapshot(roomy.key)
    assert snap.consumed_usd == pytest.approx(2 * 2.5e-6 + 1 * 15e-6, abs=1e-9)  # règlement au MÊME tarif cloud
    assert snap.consumed_usd > 0 and (snap.reserved_usd, snap.unknown_usd) == (0.0, 0.0)


async def test_a_local_provider_on_a_local_destination_is_free_and_stays_free_at_settlement(tmp_path):
    settings = SimpleNamespace(LLM_PROVIDER="lmstudio", LLM_CALL_TIMEOUT=2)
    scope = _scope(tmp_path, "local.db", usd=0.000001)  # plafond USD ridicule : un modèle gratuit n'y touche pas
    client = _client_at("http://localhost:1234/v1/", [_response(2, 1, model="llama-local")])
    result = await _sample(
        LocalSamplingContext(default_model="llama-local", client=client), scope, settings=settings, max_tokens=8
    )
    assert result.text == "ok" and len(client.calls) == 1
    snap = scope.ledger.snapshot(scope.key)
    assert snap.consumed_usd == 0.0 and snap.consumed_tokens == 3  # gratuit ET compté en tokens
    # sous un plafond de tokens, l'identité locale doit en revanche être attestée (aucune borne sinon)
    capped = _scope(tmp_path, "local-tokens.db", usd=1.0, tokens=100_000)
    other = _client_at("http://localhost:1234/v1/", [_response(2, 1, model="llama-local")])
    with pytest.raises(BudgetRefused) as refused:
        await _sample(
            LocalSamplingContext(default_model="llama-local", client=other), capped, settings=settings, max_tokens=8
        )
    assert refused.value.code == REFUSED_UNBOUNDED and other.calls == []
    attested = SimpleNamespace(**vars(settings), BUDGET_ATTESTED_BYTE_TOKENIZER_MODELS="llama-local")
    again = _client_at("http://localhost:1234/v1/", [_response(2, 1, model="llama-local")])
    await _sample(
        LocalSamplingContext(default_model="llama-local", client=again), capped, settings=attested, max_tokens=8
    )
    assert len(again.calls) == 1


async def test_a_tokenizer_attestation_is_not_a_price_attestation(tmp_path):
    """``gpt-5.4-expensive-variant`` attesté pour son tokenizer n'hérite PAS du tarif de ``gpt-5.4`` (préfixe)."""
    variant = "gpt-5.4-expensive-variant"
    base = dict(LLM_PROVIDER="openai", LLM_CALL_TIMEOUT=2, BUDGET_ATTESTED_BYTE_TOKENIZER_MODELS=variant)
    scope = _scope(tmp_path, "variant.db", usd=100.0)
    client = _openai_cloud(variant, [_response(2, 1, model=variant)])
    with pytest.raises(BudgetRefused) as refused:
        await _sample(
            LocalSamplingContext(default_model=variant, client=client),
            scope,
            settings=SimpleNamespace(**base),
            max_tokens=8,
        )
    assert refused.value.code == REFUSED_UNBOUNDED and client.calls == []  # aucun tarif établi ⇒ refus avant émission

    priced = SimpleNamespace(**base, LLM_PRICE_PROMPT_PER_1M=50.0, LLM_PRICE_COMPLETION_PER_1M=100.0)
    accepted = _openai_cloud(variant, [_response(2, 1, model=variant)])
    await _sample(LocalSamplingContext(default_model=variant, client=accepted), scope, settings=priced, max_tokens=8)
    assert len(accepted.calls) == 1
    assert scope.ledger.snapshot(scope.key).consumed_usd == pytest.approx(2 * 50e-6 + 1 * 100e-6, abs=1e-9)


async def test_the_server_handler_applies_the_same_pricing_authority(tmp_path):
    from collegue.core.llm.sampling_handler import _make_handler_class

    variant = "gpt-5.4-expensive-variant"
    scope = _scope(tmp_path, "handler.db", usd=100.0)
    client = _openai_cloud(variant, [_response(2, 1, model=variant)])
    handler = _make_handler_class()(default_model=variant, client=client)
    settings = SimpleNamespace(
        LLM_PROVIDER="lmstudio", LLM_CALL_TIMEOUT=2, BUDGET_ATTESTED_BYTE_TOKENIZER_MODELS=variant
    )
    with bind_budget(scope.ledger, scope.key, settings=settings):
        with pytest.raises(BudgetRefused) as refused:
            await handler.client.chat.completions.create(
                model=variant, messages=[{"role": "user", "content": "x"}], max_tokens=8
            )
    assert refused.value.code == REFUSED_UNBOUNDED and client.calls == []


def test_strict_grid_prices_need_an_exact_identity_and_the_right_family():
    from collegue.monitoring.pricing import cost_per_token, strict_grid_price

    assert strict_grid_price("gpt-5.4", "openai") == pytest.approx((2.5e-6, 15e-6))
    assert strict_grid_price("gpt-5.4-2026-03-05", "openai") == pytest.approx((2.5e-6, 15e-6))  # instantané daté
    assert strict_grid_price("gpt-5.4-mini", "openai") == pytest.approx((0.75e-6, 4.5e-6))  # clé plus spécifique
    assert strict_grid_price("gpt-5.4-expensive-variant", "openai") is None  # préfixe ≠ identité
    assert strict_grid_price("gemma-4-31b-it", "gemini") == (0.0, 0.0)  # gratuit lié à SA famille
    assert strict_grid_price("gemma-4-31b-it", "openai") is None  # même nom sur un autre endpoint : pas de zéro
    # l'affichage historique du dashboard garde ses préfixes (estimation nommée, hors garantie)
    assert cost_per_token("gpt-5.4-expensive-variant", provider="openai") == pytest.approx((2.5e-6, 15e-6))


def test_prices_follow_the_destination_not_the_declared_provider():
    from collegue.core.llm.budget_guard import SOURCE_GRID, pricing_family, resolve_prices_with_source

    local = SimpleNamespace(LLM_PROVIDER="lmstudio")
    assert pricing_family(local, "https://api.openai.com/v1/") == ("openai", False)
    assert pricing_family(local, "http://localhost:1234/v1") == (None, True)
    assert pricing_family(local, None) == (None, True)
    assert pricing_family(SimpleNamespace(LLM_PROVIDER="gemini"), "https://api.openai.com/v1/") == ("openai", False)
    assert resolve_prices_with_source("gpt-5.4", local, endpoint="https://api.openai.com/v1/")[1] == SOURCE_GRID
    assert resolve_prices_with_source("gpt-5.4", local, endpoint="https://api.openai.com/v1/")[0][0] > 0
    assert resolve_prices_with_source("llama", local, endpoint="http://localhost:1234/v1")[0] == (0.0, 0.0)
    assert (
        resolve_prices_with_source("gpt-5.4", SimpleNamespace(LLM_PROVIDER="openai"), endpoint="http://gw.invalid/v1")
        is None
    )
