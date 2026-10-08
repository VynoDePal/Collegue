"""Cycle de planification : identité durable, reprise, création atomique projet + scope (vague 2, passe 10).

Entrées PUBLIQUES (``plan_project_from_settings``, ``BudgetLedger``, ``ProjectStateManager``, CLI). Le fournisseur est
factice (aucun réseau, aucune clé) et photographie les appels RÉELLEMENT émis.
"""

from __future__ import annotations

import asyncio
import json
import threading
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import select, update

from collegue.core.llm.sampling_ctx import LocalSamplingContext
from collegue.monitoring.metrics import MetricsCollector
from collegue.pilot.runtime import plan_project_from_settings, planning_cycle_key
from collegue.state import BudgetRefused, PlanningCycleError, ProjectStateManager
from collegue.state.budget_ledger import REFUSED_CAP_USD, BudgetLedger
from collegue.state.models import BudgetScope, Project

MODEL = "gpt-4o-mini"
SPEC = {"title": "Audit", "objectives": ["Persister un audit"], "acceptance_criteria": ["Un audit est relu"]}
TASKS = {"tasks": [{"title": "Persister un audit", "acceptance": "Un audit est relu", "depends_on": []}]}


@pytest.fixture(autouse=True)
def _isolated_metrics(tmp_path, monkeypatch):
    monkeypatch.setattr(MetricsCollector, "_PERSIST_DIR", tmp_path / "monitoring")


def _settings(**extra):
    base = dict(
        LLM_PROVIDER="openai",
        LLM_MODEL=MODEL,
        MAX_COST_USD=1.0,
        MAX_TOKENS_BUDGET=100_000,
        BUDGET_EXHAUSTED_ACTION="pause",
        BUDGET_MODE="strict",
        LLM_CALL_TIMEOUT=2,
        GATE_ACCEPTANCE_TESTS=False,
        LLM_PRICE_PROMPT_PER_1M=1.0,
        LLM_PRICE_COMPLETION_PER_1M=2.0,
        BUDGET_ATTESTED_BYTE_TOKENIZER_MODELS="",
    )
    base.update(extra)
    return SimpleNamespace(**base)


class Provider:
    """Fournisseur factice scripté par genre d'appel (SPEC / décomposition) ; compte les appels émis."""

    def __init__(self, *, spec=SPEC, tasks=TASKS, completion=10):
        self.spec, self.tasks, self.completion = spec, tasks, completion
        self.calls = []
        self.gate = None  # asyncio.Event : bloque l'appel en vol (concurrence)
        self.entered = None
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def with_options(self, **_):
        return self

    async def _create(self, *, model, messages, max_tokens=None, **_):
        system = messages[0]["content"] if messages and messages[0]["role"] == "system" else ""
        kind = "decompose" if '"tasks"' in system else "spec"
        self.calls.append(kind)
        if self.entered is not None:
            self.entered.set()
        if self.gate is not None:
            await self.gate.wait()
        data = self.tasks if kind == "decompose" else self.spec
        content = data if isinstance(data, str) else json.dumps(data)
        completion = min(self.completion, int(max_tokens or self.completion))
        return SimpleNamespace(
            model=MODEL,
            usage=SimpleNamespace(prompt_tokens=100, completion_tokens=completion),
            choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
        )

    def ctx(self):
        return LocalSamplingContext(default_model=MODEL, client=self, max_retries=0)


@pytest.fixture
def url(tmp_path):
    return f"sqlite:///{tmp_path / 'state.db'}"


@pytest.fixture
def manager(url):
    return ProjectStateManager.from_url(url, create=True)


async def _plan(manager, provider, *, problem="Persister et relire", cycle_id=None, settings=None, **kw):
    return await plan_project_from_settings(
        "Audit",
        problem,
        owner="o",
        repo="r",
        ctx=provider.ctx(),
        settings_obj=settings or _settings(),
        manager=manager,
        decompose_attempts=1,
        retry_sleep_seconds=0,
        cycle_id=cycle_id,
        **kw,
    )


def _scopes(manager):
    with manager.session() as session:
        return [row.scope_key for row in session.scalars(select(BudgetScope).order_by(BudgetScope.id))]


def _consumed_tokens(manager):
    return sum(manager.budget_ledger.snapshot(k).consumed_tokens for k in _scopes(manager))


# --- enveloppe : une reprise du même cycle ne repart pas d'un budget neuf ------------------------------------------


async def test_repeated_failed_spec_planning_shares_one_envelope_and_the_second_emission_is_refused(manager, url):
    """Reproduction du manager : SPEC invalide, 4096 tokens de sortie à 200 $/M ⇒ 0,82 $ par tentative sous 1 $."""
    settings = _settings(LLM_PRICE_COMPLETION_PER_1M=200.0, MAX_TOKENS_BUDGET=2_000_000)
    provider = Provider(spec="x " * 2047, completion=4096)

    with pytest.raises(Exception) as first:
        await _plan(manager, provider, settings=settings)
    assert not isinstance(first.value, BudgetRefused)
    with pytest.raises(BudgetRefused) as second:
        await _plan(
            ProjectStateManager.from_url(url),
            provider,
            settings=settings,
        )

    assert second.value.code == REFUSED_CAP_USD
    assert provider.calls == ["spec"]  # la seconde tentative n'a RIEN émis
    assert len(_scopes(manager)) == 1 and manager.list_projects() == []
    total = sum(manager.budget_ledger.snapshot(k).consumed_usd for k in _scopes(manager))
    assert 0 < total <= 1.0  # jamais 1,64 $ sous un plafond de 1 $


async def test_a_failed_spec_resumes_on_a_new_manager_instance_with_the_same_scope_and_no_project(url):
    first_manager = ProjectStateManager.from_url(url, create=True)
    with pytest.raises(Exception):
        await _plan(first_manager, Provider(spec="pas du json"))
    key = _scopes(first_manager)[0]
    assert first_manager.budget_ledger.snapshot(key).project_id is None  # encore sans project_id
    spent = _consumed_tokens(first_manager)

    restarted = ProjectStateManager.from_url(url)  # « nouveau process », même base
    provider = Provider()
    result = await _plan(restarted, provider)

    assert _scopes(restarted) == [key]  # le MÊME scope : aucune enveloppe neuve
    snap = restarted.budget_ledger.snapshot(key)
    assert snap.project_id == result.project_id and snap.consumed_tokens > spent  # l'échec payé reste dans le solde
    assert [p.id for p in restarted.list_projects()] == [result.project_id]
    assert result.cycle_id == key


async def test_an_explicit_cycle_id_survives_a_changed_problem_and_a_new_id_opens_a_new_envelope(manager):
    with pytest.raises(Exception):
        await _plan(manager, Provider(spec="invalide"), cycle_id="audit-q3", problem="Première formulation")
    key = _scopes(manager)[0]
    assert key == "planning:cycle:audit-q3"

    result = await _plan(manager, Provider(), cycle_id="audit-q3", problem="Consigne reformulée")  # consigne modifiée
    assert _scopes(manager) == [key] and result.cycle_id == key

    other = await _plan(
        manager, Provider(), cycle_id="audit-q4", problem="Consigne reformulée"
    )  # NOUVEAU cycle explicite
    assert sorted(_scopes(manager)) == ["planning:cycle:audit-q3", "planning:cycle:audit-q4"]
    assert other.project_id != result.project_id


async def test_distinct_projects_get_distinct_implicit_envelopes(manager):
    first = await _plan(manager, Provider(), problem="Premier problème")
    second = await _plan(manager, Provider(), problem="Second problème")
    assert first.cycle_id != second.cycle_id and len(_scopes(manager)) == 2
    assert planning_cycle_key("a", "p", "o", "r") != planning_cycle_key("a", "p", "o", "autre-repo")


@pytest.mark.parametrize("bad", ["", "../etc", "a b", "é", "x" * 65, ".hidden", 7])
def test_an_invalid_cycle_id_is_refused(bad):
    with pytest.raises(ValueError):
        planning_cycle_key("n", "p", "o", "r", bad)


async def test_a_completed_cycle_is_refused_and_emits_nothing(manager):
    provider = Provider()
    done = await _plan(manager, provider)
    before = list(provider.calls)

    with pytest.raises(PlanningCycleError) as refused:
        await _plan(manager, provider)  # invocation identique : pas de budget neuf, pas de double projet

    assert refused.value.project_id == done.project_id and provider.calls == before
    assert len(manager.list_projects()) == 1 and _consumed_tokens(manager) > 0
    assert manager.budget_ledger.snapshot(_scopes(manager)[0]).last_error is None  # un refus n'est pas un échec payé


# --- reprise après SPEC persistée --------------------------------------------------------------------------------


@pytest.mark.parametrize("failure", ["value-error", "crash"])
async def test_a_failed_decomposition_resumes_the_same_project_without_a_second_spec(url, failure, monkeypatch):
    manager = ProjectStateManager.from_url(url, create=True)
    provider = Provider(tasks="pas du json")
    if failure == "crash":

        class Crash(BaseException):
            pass

        async def crash(*args, **kwargs):
            raise Crash("arrêt pendant la décomposition")

        monkeypatch.setattr("collegue.planner.decomposer.decompose", crash)
    with pytest.raises(BaseException):
        await _plan(manager, provider)
    monkeypatch.undo()
    assert [p.id for p in manager.list_projects()] == [1] and manager.get_tasks(1) == []  # projet sans tâches
    spec_calls = provider.calls.count("spec")
    spent = _consumed_tokens(manager)

    restarted = ProjectStateManager.from_url(url)
    provider.tasks = TASKS
    result = await _plan(restarted, provider)

    assert provider.calls.count("spec") == spec_calls  # la SPEC payée est RÉUTILISÉE, pas régénérée
    assert result.project_id == 1 and result.task_count == 1 and result.plan_hash
    assert [p.id for p in restarted.list_projects()] == [1] and len(restarted.get_tasks(1)) == 1
    assert _consumed_tokens(restarted) >= spent  # même enveloppe, la dépense précédente est conservée
    assert len(_scopes(restarted)) == 1
    with pytest.raises(PlanningCycleError):  # puis le cycle est abouti
        await _plan(restarted, provider)


async def test_a_failed_acceptance_generation_resumes_at_that_step_only(manager, monkeypatch):
    settings = _settings(GATE_ACCEPTANCE_TESTS=True)
    provider = Provider()
    attempts = []

    async def failing(*args, **kwargs):
        attempts.append("fail")
        raise RuntimeError("génération des tests d'acceptation impossible")

    monkeypatch.setattr("collegue.planner.acceptance_tests.generate_acceptance_tests", failing)
    with pytest.raises(RuntimeError):
        await _plan(manager, provider, settings=settings)
    assert len(manager.get_tasks(1)) == 1 and manager.get_project(1).acceptance_tests_required is False
    calls = list(provider.calls)

    resumed = []

    async def succeeding(spec, tasks, ctx, *, manager, project_id, settings_obj=None, **kw):
        resumed.append((project_id, [t.id for t in tasks], isinstance(spec, str)))
        manager.update_project(project_id, acceptance_tests_required=True)
        return {}

    monkeypatch.setattr("collegue.planner.acceptance_tests.generate_acceptance_tests", succeeding)
    result = await _plan(manager, provider, settings=settings)

    assert provider.calls == calls  # ni nouvelle SPEC ni nouvelle décomposition
    assert resumed == [(1, [manager.get_tasks(1)[0].id], True)] and result.project_id == 1
    assert len(manager.list_projects()) == 1 and len(manager.get_tasks(1)) == 1


# --- concurrence : un seul appel détient le cycle -------------------------------------------------------------


async def test_concurrent_invocations_of_the_same_cycle_create_one_project_and_one_refuses_without_emitting(
    manager, url
):
    provider = Provider()
    provider.gate, provider.entered = asyncio.Event(), asyncio.Event()
    first = asyncio.create_task(_plan(manager, provider))
    await asyncio.wait_for(provider.entered.wait(), 10)  # le premier appel est EN VOL, il détient le cycle

    with pytest.raises(PlanningCycleError) as busy:
        await _plan(ProjectStateManager.from_url(url), provider)
    assert busy.value.busy is True and provider.calls == ["spec"]  # le second n'a rien émis

    provider.gate.set()
    result = await first
    assert [p.id for p in manager.list_projects()] == [result.project_id] and len(_scopes(manager)) == 1


async def test_a_released_cycle_after_a_failure_can_be_taken_again(manager):
    with pytest.raises(Exception):
        await _plan(manager, Provider(spec="invalide"))
    key = _scopes(manager)[0]
    with manager.session() as session:
        assert session.scalar(select(BudgetScope.claim_token).where(BudgetScope.scope_key == key)) is None


async def test_a_stale_claim_from_a_dead_process_expires_and_can_be_taken_over(manager):
    ledger = manager.budget_ledger
    key = planning_cycle_key("Audit", "Persister et relire", "o", "r")
    ledger.open_planning_cycle(key, max_cost_usd=1.0, max_tokens=100_000)  # « process mort » : jamais libéré
    with pytest.raises(PlanningCycleError):
        await _plan(manager, Provider())  # tant que le droit n'est pas échu, le cycle est occupé
    with manager.session() as session:
        session.execute(
            update(BudgetScope)
            .where(BudgetScope.scope_key == key)
            .values(claim_expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))
        )
    result = await _plan(manager, Provider())
    assert result.cycle_id == key


# --- création atomique du projet et liaison du scope --------------------------------------------------------------


class InjectedCrash(BaseException):
    pass


async def test_a_crash_inside_project_creation_leaves_no_project_and_keeps_the_spec_spend(url):
    manager = ProjectStateManager.from_url(url, create=True)
    provider = Provider()

    def crash(*args, **kwargs):  # après l'insertion du projet, AVANT le commit de la transaction
        raise InjectedCrash("arrêt entre l'insertion du projet et la liaison du scope")

    manager.budget_ledger.bind_new_project_in_session = crash
    with pytest.raises(InjectedCrash):
        await _plan(manager, provider)

    restarted = ProjectStateManager.from_url(url)
    assert restarted.list_projects() == []  # transaction annulée : aucun projet orphelin
    key = _scopes(restarted)[0]
    assert restarted.budget_ledger.snapshot(key).project_id is None
    spent = restarted.budget_ledger.snapshot(key).consumed_tokens
    assert spent > 0  # la dépense de la SPEC reste durable

    result = await _plan(restarted, provider)  # la reprise ne renouvelle pas l'enveloppe
    assert _scopes(restarted) == [key] and [p.id for p in restarted.list_projects()] == [result.project_id]
    assert restarted.budget_ledger.snapshot(key).consumed_tokens > spent


def test_creating_the_project_and_binding_the_scope_is_one_transaction(manager):
    ledger = manager.budget_ledger
    _snap, token = ledger.open_planning_cycle("planning:cycle:t", max_cost_usd=1.0)
    pid = manager.create_project_in_cycle("planning:cycle:t", token, name="p", spec="# s")
    assert ledger.snapshot("planning:cycle:t").project_id == pid
    assert manager.get_project(pid).spec == "# s"


def test_a_lost_or_foreign_claim_creates_no_project(manager):
    ledger = manager.budget_ledger
    ledger.open_planning_cycle("planning:cycle:t", max_cost_usd=1.0)
    with pytest.raises(PlanningCycleError):
        manager.create_project_in_cycle("planning:cycle:t", "jeton-etranger", name="p", spec="# s")
    assert manager.list_projects() == [] and ledger.snapshot("planning:cycle:t").project_id is None


def test_an_expired_claim_taken_over_by_another_caller_cannot_create_the_project(url):
    ticks = {"now": datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)}
    base = ProjectStateManager.from_url(url, create=True)
    ledger = BudgetLedger(base._session_factory, clock=lambda: ticks["now"])
    _snap, stale = ledger.open_planning_cycle("planning:cycle:t", max_cost_usd=1.0, claim_ttl_seconds=60)
    ticks["now"] += timedelta(seconds=120)  # le droit du premier appelant est échu
    _snap, fresh = ledger.open_planning_cycle("planning:cycle:t", max_cost_usd=1.0, claim_ttl_seconds=60)
    assert fresh != stale

    base._budget_ledger = ledger  # le manager utilise cette horloge pour la vérification d'échéance
    with pytest.raises(PlanningCycleError):
        base.create_project_in_cycle("planning:cycle:t", stale, name="p", spec="# s")
    assert base.list_projects() == []
    pid = base.create_project_in_cycle("planning:cycle:t", fresh, name="p", spec="# s")
    assert [p.id for p in base.list_projects()] == [pid]


def test_concurrent_cycle_claims_and_project_creations_have_one_winner(url):
    base = ProjectStateManager.from_url(url, create=True)
    barrier = threading.Barrier(8)
    outcomes = []

    def worker():
        mgr = ProjectStateManager.from_url(url)
        barrier.wait()
        try:
            _snap, token = mgr.budget_ledger.open_planning_cycle("planning:cycle:race", max_cost_usd=1.0)
            outcomes.append(("claimed", mgr.create_project_in_cycle("planning:cycle:race", token, name="p", spec="#")))
        except PlanningCycleError as exc:
            outcomes.append(("busy", exc.busy))
        except BaseException as exc:  # noqa: BLE001
            outcomes.append(("autre", repr(exc)))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    kinds = [kind for kind, _ in outcomes]
    assert kinds.count("claimed") == 1 and kinds.count("busy") == 7, outcomes
    assert len(base.list_projects()) == 1  # aucune double création
    with base.session() as session:
        assert session.scalar(select(Project.id)) == base.budget_ledger.snapshot("planning:cycle:race").project_id


def test_the_cycle_claim_is_released_explicitly_and_only_by_its_holder(manager):
    ledger = manager.budget_ledger
    _snap, token = ledger.open_planning_cycle("planning:cycle:t", max_cost_usd=1.0)
    with pytest.raises(PlanningCycleError):
        ledger.open_planning_cycle("planning:cycle:t", max_cost_usd=1.0)
    ledger.release_planning_claim("planning:cycle:t", "pas-le-detenteur")  # sans effet
    with pytest.raises(PlanningCycleError):
        ledger.open_planning_cycle("planning:cycle:t", max_cost_usd=1.0)
    ledger.release_planning_claim("planning:cycle:t", token)
    ledger.open_planning_cycle("planning:cycle:t", max_cost_usd=1.0)  # de nouveau disponible


# --- CLI : l'identité du cycle est exposée -------------------------------------------------------------------------


def test_the_cli_exposes_an_explicit_cycle_id_for_draft_only():
    from collegue.pilot.__main__ import _validate_plan_args, build_parser

    parser = build_parser()
    args = parser.parse_args(
        ["plan", "draft", "--problem", "p", "--owner", "o", "--repo", "r", "--cycle-id", "audit-q3"]
    )
    assert args.cycle_id == "audit-q3"
    _validate_plan_args(parser, args)  # accepté pour draft

    forbidden = parser.parse_args(
        ["plan", "approve", "--project-id", "1", "--expected-plan-hash", "a" * 64, "--cycle-id", "x"]
    )
    with pytest.raises(SystemExit):
        _validate_plan_args(parser, forbidden)  # approve/sync : cible scellée, pas de nouvelle enveloppe


# --- F5 : un run réel strict sans registre est refusé ---------------------------------------------------------


class _NoLedgerManager:
    """Manager sans registre budgétaire, SANS capacité déclarée."""


async def test_planning_without_a_ledger_is_refused_in_strict_mode():
    with pytest.raises(BudgetRefused) as refused:
        await _plan(_NoLedgerManager(), Provider())
    assert refused.value.code == "ledger_unavailable"


def test_require_budget_ledger_matrix():
    from collegue.pilot.budget import require_budget_ledger

    class Declared:
        budget_enforcement = "test-double"

    with pytest.raises(BudgetRefused):
        require_budget_ledger(_NoLedgerManager(), _settings(), what="le run")
    assert require_budget_ledger(Declared(), _settings()) is None  # double déclaré explicitement
    assert require_budget_ledger(_NoLedgerManager(), _settings(BUDGET_MODE="advisory")) is None  # advisory nommé
    assert require_budget_ledger(_NoLedgerManager(), _settings(BUDGET_EXHAUSTED_ACTION="warn")) is None
    real = SimpleNamespace(budget_ledger=object())
    assert require_budget_ledger(real, _settings()) is real.budget_ledger


def test_attach_project_budget_refuses_a_real_run_without_a_ledger():
    from collegue.pilot.budget import attach_project_budget

    budget = SimpleNamespace(attach_ledger=lambda *a: None, settings=_settings())
    with pytest.raises(BudgetRefused):
        attach_project_budget(budget, _NoLedgerManager(), 1)
