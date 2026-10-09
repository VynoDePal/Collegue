"""Acceptation vague 2 — budget durable commun à la planification, BUILD et IMPROVE.

Chaque test passe par les ENTRÉES PUBLIQUES (``run_project``, ``run_improvement``,
``plan_project_from_settings``) et montre les appels réellement émis et les soldes du registre. Les agents
sont des doubles déterministes qui déclarent un coût ; aucune dépense réelle, aucun réseau.
"""

from __future__ import annotations

import dataclasses
import json
import subprocess
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from test_improve_loop import _clients as improve_clients
from test_improve_loop import _metrics, _ScriptedMeasure
from test_pilot_driver import _linear_project, _run

from collegue.core.llm.budget_guard import bind_budget
from collegue.core.llm.sampling_ctx import LocalSamplingContext
from collegue.executor import FakeCodeAgent, IssueSpec, OHSdkAgent, OpenHandsAgent
from collegue.executor.worker_budget import current_allocation
from collegue.improve import run_improvement
from collegue.monitoring.metrics import MetricsCollector
from collegue.pilot.audit import RunAuditLog
from collegue.pilot.budget import BudgetTimeController
from collegue.pilot.driver import run_project
from collegue.sandbox import SandboxResult
from collegue.state import ProjectStateManager

MODEL = "gemini-3.5-flash"


def _settings(cap=1.0, **extra):
    base = dict(
        MAX_COST_USD=cap,
        MAX_TOKENS_BUDGET=0,
        BUDGET_EXHAUSTED_ACTION="pause",
        LLM_PROVIDER="gemini",
        LLM_MODEL=MODEL,
        COLLEGUE_RUN_DEADLINE_SECONDS=0.0,
    )
    base.update(extra)
    return SimpleNamespace(**base)


def _controller(cap=1.0, **extra):
    """Contrôleur SANS registre au départ, comme celui qu'un appelant construit (la sonde du manager)."""
    return BudgetTimeController(collector=MetricsCollector(), settings_obj=_settings(cap, **extra))


class PricedAgent:
    """Agent qui déclare un coût fixe par passe ; enregistre l'allocation qu'il a reçue."""

    budget_enforcement = "test-double"  # double déterministe : ne dépense rien hors process

    def __init__(self, price, *, clamp=False):
        self.calls = 0
        self.price = price
        self.clamp = clamp
        self.allocations = []
        self._delegate = FakeCodeAgent()

    def implement_issue(self, workspace, issue):
        self.calls += 1
        alloc = current_allocation()
        self.allocations.append(None if alloc is None else alloc.max_usd)
        spend = self.price
        if self.clamp and alloc is not None:
            spend = min(spend, alloc.max_usd)  # un worker honnête ne dépasse pas son allocation
        return dataclasses.replace(
            self._delegate.implement_issue(workspace, issue), cost_usd=spend, prompt_tokens=100, completion_tokens=50
        )


@pytest.fixture(autouse=True)
def _isolated_metrics(tmp_path, monkeypatch):
    """Le MetricsCollector persiste sur disque : confiné à tmp_path, jamais le COLLEGUE_HOME du rôle."""
    monkeypatch.setattr(MetricsCollector, "_PERSIST_DIR", tmp_path / "monitoring")


@pytest.fixture
def url(tmp_path):
    return f"sqlite:///{tmp_path / 'state.db'}"


@pytest.fixture
def manager(url):
    return ProjectStateManager.from_url(url, create=True)


@pytest.fixture
def repo(tmp_path):
    src = tmp_path / "source"
    src.mkdir()
    for args in (["init", "-q"], ["config", "user.email", "t@e.x"], ["config", "user.name", "t"]):
        subprocess.run(["git", *args], cwd=src, check=True, capture_output=True)
    (src / "existing.txt").write_text("original\n")
    subprocess.run(["git", "add", "-A"], cwd=src, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=src, check=True, capture_output=True)
    return str(src)


def _merge_all(manager, pid):
    for task in manager.get_tasks(pid):
        if task.status == "in_review":
            manager.update_task_status(task.id, "merged")


def _ledger(manager, pid):
    return manager.budget_ledger.snapshot_for_project(pid)


# --- BUILD : deux passes séparées par un merge ---------------------------------------------------------


async def test_two_build_passes_separated_by_a_merge_share_one_durable_budget(manager, repo):
    pid = _linear_project(manager, 4)
    agent = PricedAgent(0.6)
    audit = RunAuditLog(pid, manager=manager, persist=True)

    first = await _run(
        manager,
        repo,
        pid,
        budget=_controller(),
        agent=agent,
        dry_run=False,
        max_iterations=1,
        audit=audit,
        reconcile_reviews=False,
    )
    _merge_all(manager, pid)
    second = await _run(
        manager,
        repo,
        pid,
        budget=_controller(),
        agent=agent,
        dry_run=False,
        max_iterations=1,
        audit=audit,
        reconcile_reviews=False,
    )  # NOUVEAU contrôleur : aucun état en mémoire entre les deux passes
    _merge_all(manager, pid)
    third = await _run(
        manager,
        repo,
        pid,
        budget=_controller(),
        agent=agent,
        dry_run=False,
        max_iterations=1,
        audit=audit,
        reconcile_reviews=False,
    )

    snap = _ledger(manager, pid)
    assert first.stop_reason == "safety_cap" and second.stop_reason == "safety_cap"
    assert snap.consumed_usd == pytest.approx(1.2)  # 2 × 0,60 : la 2ᵉ passe a SU que la 1ʳᵉ avait dépensé
    assert third.stop_reason == "paused_budget"  # plafond 1 $ atteint : plus rien ne part
    assert agent.calls == 2  # appels réellement émis : le 3ᵉ a été refusé avant lancement
    assert audit.cost.usd == pytest.approx(snap.spent_usd)  # l'audit AFFICHE le registre (pas de double comptage)
    assert _controller().__class__ is BudgetTimeController  # (contrôleur neuf, sans ledger propre)


async def test_the_baseline_probe_scenario_is_now_safe(manager, repo):
    """Reproduction du manager (2 × 0,60 $ sous plafond 1 $ : décision « continue » à tort) → pause."""
    pid = _linear_project(manager, 3)
    ctrl = _controller()
    agent = PricedAgent(0.6)
    audit = RunAuditLog(pid, manager=manager, persist=True)
    await _run(
        manager,
        repo,
        pid,
        budget=ctrl,
        agent=agent,
        dry_run=False,
        max_iterations=1,
        audit=audit,
        reconcile_reviews=False,
    )
    _merge_all(manager, pid)
    await _run(
        manager,
        repo,
        pid,
        budget=ctrl,
        agent=agent,
        dry_run=False,
        max_iterations=1,
        audit=audit,
        reconcile_reviews=False,
    )
    decision = ctrl.should_continue()
    assert agent.calls * 0.6 > 1.0 and decision.ok is False and decision.action == "paused_budget"


async def test_allocations_shrink_with_the_balance_and_an_honest_worker_stays_inside_them(manager, repo):
    pid = _linear_project(manager, 6)
    agent = PricedAgent(0.9, clamp=True)
    for _ in range(4):
        await _run(
            manager,
            repo,
            pid,
            budget=_controller(),
            agent=agent,
            dry_run=False,
            max_iterations=1,
            reconcile_reviews=False,
        )
        _merge_all(manager, pid)
    assert agent.allocations[0] == pytest.approx(0.8)  # 80 % du solde de 1 $
    assert agent.allocations[1] == pytest.approx(0.16, abs=0.01)  # 80 % des 0,20 $ restants
    assert _ledger(manager, pid).consumed_usd <= 1.0 + 1e-9  # jamais au-delà du plafond
    assert agent.calls <= 3  # la passe suivante, solde nul, n'est plus lancée


# --- redémarrage : nouvelle instance, même base -----------------------------------------------------------


async def test_a_restart_on_a_new_instance_keeps_the_same_balance(url, repo):
    first = ProjectStateManager.from_url(url, create=True)
    pid = _linear_project(first, 3)
    await _run(
        first,
        repo,
        pid,
        budget=_controller(),
        agent=PricedAgent(0.7),
        dry_run=False,
        max_iterations=1,
        reconcile_reviews=False,
    )
    before = _ledger(first, pid)

    restarted = ProjectStateManager.from_url(url)  # « nouveau process » : aucune donnée héritée
    _merge_all(restarted, pid)
    ctrl = _controller()
    agent = PricedAgent(0.7)
    await _run(restarted, repo, pid, budget=ctrl, agent=agent, dry_run=False, max_iterations=1, reconcile_reviews=False)

    after = _ledger(restarted, pid)
    assert before.consumed_usd == pytest.approx(0.7)
    assert after.consumed_usd == pytest.approx(1.4)  # le solde n'a PAS été remis à zéro par le redémarrage
    assert ctrl.should_continue().ok is False


# --- BUILD → IMPROVE ---------------------------------------------------------------------------------------


async def test_build_then_improve_draw_on_the_same_durable_scope(manager, repo):
    pid = _linear_project(manager, 1)
    await _run(
        manager,
        repo,
        pid,
        budget=_controller(),
        agent=PricedAgent(0.6),
        dry_run=False,
        max_iterations=1,
        reconcile_reviews=False,
    )
    assert _ledger(manager, pid).consumed_usd == pytest.approx(0.6)

    improver = PricedAgent(0.7)
    result = await run_improvement(
        pid,
        repo,
        None,
        agent=improver,
        owner="o",
        repo="r",
        manager=manager,
        budget=_controller(),  # nouveau contrôleur : le cumul BUILD vient du registre
        clients=improve_clients(),
        dry_run=False,
        plateau_rounds=3,
        measure_fn=_ScriptedMeasure([_metrics(0.5)] * 6),
    )

    snap = _ledger(manager, pid)
    assert snap.consumed_usd == pytest.approx(1.3)  # 0,6 (BUILD) + 0,7 (IMPROVE), UN SEUL registre
    assert improver.calls == 1  # le 2ᵉ round est refusé : plafond atteint
    assert result.stop_reason == "paused_budget"


async def test_failed_improve_attempts_are_debited(manager, repo):
    """Deux tentatives IMPROVE de 0,70 $ sans promotion : la dépense est débitée quand même (l'audit du
    28/09 montrait 1,40 $ dépensés pour 0 $ comptés)."""
    pid = _linear_project(manager, 1)
    manager.update_task_status(manager.get_tasks(pid)[0].id, "merged")
    improver = PricedAgent(0.7)
    ctrl = _controller()
    result = await run_improvement(
        pid,
        repo,
        None,
        agent=improver,
        owner="o",
        repo="r",
        manager=manager,
        budget=ctrl,
        clients=improve_clients(),
        dry_run=False,
        plateau_rounds=5,
        measure_fn=_ScriptedMeasure([_metrics(0.5)] * 12),  # aucun gain : rien promu
    )
    assert result.promoted == [] and improver.calls == 2
    assert _ledger(manager, pid).consumed_usd == pytest.approx(1.4)  # les deux tentatives échouées sont débitées
    assert ctrl.should_continue().ok is False and result.stop_reason == "paused_budget"


# --- crashs et usage inconnu --------------------------------------------------------------------------------


class _CrashingAgent:
    budget_enforcement = "test-double"  # meurt AVANT de rapporter son usage (la capacité déclarée n'y change rien)

    def __init__(self):
        self.calls = 0

    def implement_issue(self, workspace, issue):
        self.calls += 1
        raise RuntimeError("OOM-kill : le worker est mort avant de rapporter son usage")


async def test_a_worker_that_dies_before_reporting_blocks_the_next_pass_with_a_durable_reason(manager, repo, url):
    pid = _linear_project(manager, 2)
    crashing = _CrashingAgent()
    await _run(
        manager,
        repo,
        pid,
        budget=_controller(),
        agent=crashing,
        dry_run=False,
        max_iterations=1,
        reconcile_reviews=False,
        max_task_attempts=1,
    )

    snap = _ledger(manager, pid)
    assert snap.unknown_usd > 0 and snap.blocked  # réservation CONSERVÉE (borne haute), suite stricte bloquée
    assert "worker interrompu" in snap.blocked_reason

    healthy = PricedAgent(0.1)
    restarted = ProjectStateManager.from_url(url)  # nouveau process, même base : le motif est durable
    result = await _run(
        restarted, repo, pid, budget=_controller(), agent=healthy, dry_run=False, reconcile_reviews=False
    )
    assert result.stop_reason == "paused_budget" and healthy.calls == 0  # rien n'a été émis
    assert crashing.calls == 1


async def test_a_reservation_orphaned_before_emission_is_recovered_as_unknown_at_the_next_run(manager, repo):
    pid = _linear_project(manager, 1)
    ledger = manager.budget_ledger
    key = ledger.scope_for_project(pid, max_cost_usd=1.0).scope_key
    ledger.reserve(key, usd=0.4, tokens=0, kind="worker", expires_at=datetime.now(timezone.utc) - timedelta(seconds=5))

    agent = PricedAgent(0.1)
    result = await _run(manager, repo, pid, budget=_controller(), agent=agent, dry_run=False, reconcile_reviews=False)

    snap = _ledger(manager, pid)
    assert result.stop_reason == "paused_budget" and agent.calls == 0
    assert snap.unknown_usd == pytest.approx(0.4) and "non réglée à l'échéance" in snap.blocked_reason


# --- worker réel (OHSdkAgent) : allocation, échéance, marqueurs ----------------------------------------------


class _Sandbox:
    """Faux sandbox qui accepte ``timeout`` (comme DockerSandbox) et rend un script de logs."""

    def __init__(self, stdout="", *, exit_code=0, timed_out=False):
        self.calls = []
        self._result = SandboxResult(exit_code=exit_code, stdout=stdout, stderr="", timed_out=timed_out)

    def run_command(self, argv, workspace, *, timeout=None):
        self.calls.append({"argv": argv, "timeout": timeout})
        return self._result


def _usage_line(prompt=100, completion=50, cost=0.0, billable=False):
    flag = "true" if billable else "false"
    return f'[collegue-usage] {{"prompt_tokens": {prompt}, "completion_tokens": {completion}, "cost_usd": {cost}, "billable": {flag}}}'


def _subscription_settings(cap=1.0, **extra):
    """Coder par abonnement (0 $/token) : seule configuration réelle où le mode strict accepte l'agent SDK."""
    return _settings(
        cap,
        CODER_SUBSCRIPTION=True,
        LLM_MODEL_CODER="gpt-5.5",
        CODER_SUBSCRIPTION_MODEL="gpt-5.5",
        CODER_SUBSCRIPTION_FALLBACK="gpt-5.4",
        **extra,
    )


def _run_oh(manager, pid, sandbox, *, cap=1.0, deadline_seconds=None, settings=None):
    """Un round BUILD avec le VRAI OHSdkAgent sur un faux sandbox."""
    settings = settings if settings is not None else _subscription_settings(cap)
    ctrl = BudgetTimeController(settings_obj=settings, deadline_seconds=deadline_seconds)
    agent = OHSdkAgent(sandbox, settings_obj=settings)
    return agent, ctrl


async def _oh_pass(manager, repo, pid, sandbox, **kw):
    agent, ctrl = _run_oh(
        manager, pid, sandbox, **{k: v for k, v in kw.items() if k in ("cap", "deadline_seconds", "settings")}
    )
    return agent, await _run(
        manager, repo, pid, budget=ctrl, agent=agent, dry_run=False, max_iterations=1, reconcile_reviews=False
    )


async def test_the_worker_receives_a_bounded_allocation_and_a_container_deadline(manager, repo):
    pid = _linear_project(manager, 1)
    sandbox = _Sandbox(f"{_usage_line()}\n[collegue-budget] armed {{}}\n[collegue-budget] final\nOH_RUNNER_DONE\n")
    await _oh_pass(manager, repo, pid, sandbox, deadline_seconds=900)

    (call,) = sandbox.calls
    argv = call["argv"]
    assert "--budget-usd" in argv and float(argv[argv.index("--budget-usd") + 1]) == pytest.approx(0.8)
    assert "--deadline-epoch" in argv and float(argv[argv.index("--deadline-epoch") + 1]) > time.time()
    table = json.loads(argv[argv.index("--prices") + 1])  # tarif de CHAQUE modèle de la chaîne (repli compris)
    assert set(table) == {"gpt-5.5", "gpt-5.4"} and "--strict" in argv and "--no-billing" in argv
    assert call["timeout"] is not None and 0 < call["timeout"] <= 900  # échéance transmise au conteneur
    snap = _ledger(manager, pid)
    assert (snap.consumed_usd, snap.consumed_tokens) == (0.0, 150) and snap.reserved_usd == 0.0  # abonnement : 0 $
    assert snap.unknown_usd == 0.0


async def test_a_worker_killed_after_arming_is_unknown_and_blocks_the_rest(manager, repo):
    pid = _linear_project(manager, 2)
    sandbox = _Sandbox(f"[collegue-budget] armed {{}}\n{_usage_line()}\n", exit_code=124, timed_out=True)
    await _oh_pass(manager, repo, pid, sandbox)

    snap = _ledger(manager, pid)
    assert snap.unknown_usd == pytest.approx(0.8) and snap.blocked  # allocation CONSERVÉE : le reste est inconnu
    assert "incomplet" in snap.blocked_reason or "interrompu" in snap.blocked_reason


async def test_a_runner_that_died_before_arming_is_a_proven_zero_not_a_block(manager, repo):
    """Crash d'import (#498) : le runner n'a jamais pu dépenser — zéro ÉTABLI, aucun blocage."""
    pid = _linear_project(manager, 1)
    sandbox = _Sandbox("Traceback...\nModuleNotFoundError: No module named 'lmnr'\n", exit_code=1)
    await _oh_pass(manager, repo, pid, sandbox)
    snap = _ledger(manager, pid)
    assert (snap.consumed_usd, snap.unknown_usd, snap.reserved_usd) == (0.0, 0.0, 0.0) and not snap.blocked


async def test_the_deadline_of_the_run_bounds_the_allocation_and_an_expired_run_launches_nothing(manager, repo):
    pid = _linear_project(manager, 1)
    sandbox = _Sandbox()
    agent, ctrl = _run_oh(manager, pid, sandbox, deadline_seconds=0.01)
    time.sleep(0.05)  # l'échéance du run est dépassée
    result = await _run(manager, repo, pid, budget=ctrl, agent=agent, dry_run=False, reconcile_reviews=False)
    assert result.stop_reason == "deadline_reached" and sandbox.calls == []


async def test_a_non_boundable_legacy_agent_is_refused_in_strict_but_allowed_in_advisory(manager, repo):
    pid = _linear_project(manager, 1)
    sandbox = _Sandbox(_usage_line())
    legacy = OpenHandsAgent(sandbox, settings_obj=_settings())
    result = await _run(manager, repo, pid, budget=_controller(), agent=legacy, dry_run=False, reconcile_reviews=False)
    assert result.stop_reason == "paused_budget" and sandbox.calls == []  # AUCUN conteneur lancé

    pid2 = _linear_project(manager, 1)
    advisory = _controller(BUDGET_MODE="advisory", BUDGET_EXHAUSTED_ACTION="warn")
    sandbox2 = _Sandbox(_usage_line())
    await _run(
        manager,
        repo,
        pid2,
        budget=advisory,
        agent=OpenHandsAgent(sandbox2, settings_obj=_settings()),
        dry_run=False,
        reconcile_reviews=False,
    )
    assert len(sandbox2.calls) == 1  # mode non strict explicitement nommé : enregistré, sans garantie
    assert _ledger(manager, pid2).strict is False


async def test_an_unpriced_coder_is_refused_under_a_usd_cap_before_any_launch(manager, repo):
    pid = _linear_project(manager, 1)
    sandbox = _Sandbox(_usage_line())
    settings = _settings(LLM_MODEL="mystery-9")
    ctrl = BudgetTimeController(settings_obj=settings)
    result = await _run(
        manager,
        repo,
        pid,
        budget=ctrl,
        agent=OHSdkAgent(sandbox, settings_obj=settings),
        dry_run=False,
        reconcile_reviews=False,
    )
    assert result.stop_reason == "paused_budget" and sandbox.calls == []


# --- arbitrage des workers : « in-runner » n'est pas une barrière contre une clé facturable accessible ----------


class _Mute:
    """Agent réel potentiel qui ne dit RIEN de sa dépense : aucune garantie par défaut."""

    def __init__(self):
        self.calls = 0

    def implement_issue(self, workspace, issue):
        self.calls += 1
        return FakeCodeAgent().implement_issue(workspace, issue)


async def test_an_agent_without_declared_enforcement_gets_no_guarantee_by_default(manager, repo):
    pid = _linear_project(manager, 1)
    mute = _Mute()
    result = await _run(manager, repo, pid, budget=_controller(), agent=mute, dry_run=False, reconcile_reviews=False)
    assert result.stop_reason == "paused_budget" and mute.calls == 0  # refusé AVANT tout lancement


async def test_in_runner_with_a_billable_key_is_not_a_strict_guarantee_and_is_refused(manager, repo):
    """Une commande du workspace dispose de la même clé facturable et d'un réseau libre : pas de barrière."""
    pid = _linear_project(manager, 1)
    sandbox = _Sandbox(_usage_line())
    result = await _run(
        manager,
        repo,
        pid,
        budget=_controller(),
        agent=OHSdkAgent(sandbox, settings_obj=_settings()),  # clé API facturable (gemini), pas d'abonnement
        dry_run=False,
        reconcile_reviews=False,
    )
    assert result.stop_reason == "paused_budget" and sandbox.calls == []  # AUCUN conteneur lancé


async def test_in_runner_with_a_billable_key_stays_available_in_advisory_mode(manager, repo):
    pid = _linear_project(manager, 1)
    sandbox = _Sandbox(_usage_line(cost=0.05, billable=True))
    advisory = _controller(BUDGET_MODE="advisory", BUDGET_EXHAUSTED_ACTION="warn")
    await _run(
        manager,
        repo,
        pid,
        budget=advisory,
        agent=OHSdkAgent(sandbox, settings_obj=_settings()),
        dry_run=False,
        reconcile_reviews=False,
    )
    assert len(sandbox.calls) == 1 and _ledger(manager, pid).strict is False  # enregistré, sans garantie annoncée


async def test_a_subscription_coder_is_accepted_in_strict_mode(manager, repo):
    pid = _linear_project(manager, 1)
    sandbox = _Sandbox(f"[collegue-budget] armed {{}}\n{_usage_line()}\n[collegue-budget] final\nOH_RUNNER_DONE\n")
    await _oh_pass(manager, repo, pid, sandbox)
    assert len(sandbox.calls) == 1 and _ledger(manager, pid).consumed_tokens == 150


async def test_an_unknown_marker_wins_over_the_final_marker(manager, repo):
    """``final`` prouve que les deltas ont été vidés, pas que les compteurs du SDK ont tout vu."""
    pid = _linear_project(manager, 2)
    out = (
        '[collegue-budget] armed {}\n[collegue-budget] unknown {"reason": "appel indéterminé (HTTP 503)"}\n'
        f"{_usage_line()}\n[collegue-budget] final\n"
    )
    await _oh_pass(manager, repo, pid, _Sandbox(out, exit_code=4))
    snap = _ledger(manager, pid)
    assert snap.unknown_usd == pytest.approx(0.8) and snap.blocked and "indéterminé" in snap.blocked_reason


async def test_a_container_timeout_is_unknown_even_when_the_final_marker_was_printed(manager, repo):
    pid = _linear_project(manager, 2)
    out = f"[collegue-budget] armed {{}}\n{_usage_line()}\n[collegue-budget] final\n"
    await _oh_pass(manager, repo, pid, _Sandbox(out, exit_code=124, timed_out=True))
    assert _ledger(manager, pid).blocked


class _DeadlineAgent:
    budget_enforcement = "test-double"

    def implement_issue(self, workspace, issue):
        from collegue.state import BudgetRefused
        from collegue.state.budget_ledger import REFUSED_DEADLINE

        raise BudgetRefused(REFUSED_DEADLINE, "échéance atteinte pendant l'appel : annulé, usage inconnu")


async def test_a_deadline_refusal_during_a_call_stops_the_run_as_deadline_reached(manager, repo):
    pid = _linear_project(manager, 2)
    result = await _run(
        manager, repo, pid, budget=_controller(), agent=_DeadlineAgent(), dry_run=False, reconcile_reviews=False
    )
    assert result.stop_reason == "deadline_reached"


@pytest.mark.parametrize(
    "extra",
    [
        {"BUDGET_WORKER_SHARE": "oops"},
        {"BUDGET_WORKER_SHARE": float("nan")},
        {"BUDGET_WORKER_SHARE": 0},
        {"BUDGET_WORKER_SHARE": 1.5},
        {"BUDGET_WORKER_SHARE": True},
        {"BUDGET_WORKER_MAX_USD": -1},
        {"BUDGET_WORKER_MAX_USD": float("inf")},
        {"BUDGET_WORKER_MAX_TOKENS": 12.5},
        {"BUDGET_WORKER_MIN_USD": float("nan")},
        {"BUDGET_WORKER_MIN_TOKENS": "x"},
    ],
)
def test_invalid_worker_settings_are_refused_not_corrected(manager, extra):
    from collegue.core.llm.budget_guard import BudgetBinding
    from collegue.executor.worker_budget import allocate_worker
    from collegue.state import BudgetRefused

    pid = manager.create_project(name="p")
    key = manager.budget_ledger.scope_for_project(pid, max_cost_usd=1.0).scope_key
    binding = BudgetBinding(manager.budget_ledger, key, settings=_settings(**extra))
    with pytest.raises(BudgetRefused) as refused:
        allocate_worker(binding, agent=PricedAgent(0.1))
    assert refused.value.code == "unbounded_transport"
    snap = manager.budget_ledger.snapshot(key)
    assert snap.reserved_usd == 0.0  # rien n'a été réservé


@pytest.mark.parametrize("runtime", [float("nan"), float("inf"), 0, -5, True, "60"])
def test_an_invalid_worker_runtime_is_refused(manager, runtime):
    from collegue.core.llm.budget_guard import BudgetBinding
    from collegue.executor.worker_budget import allocate_worker
    from collegue.state import BudgetRefused

    pid = manager.create_project(name="p")
    key = manager.budget_ledger.scope_for_project(pid, max_cost_usd=1.0).scope_key
    with pytest.raises(BudgetRefused):
        allocate_worker(
            BudgetBinding(manager.budget_ledger, key, settings=_settings()),
            agent=PricedAgent(0.1),
            timeout_seconds=runtime,
        )
    assert manager.budget_ledger.snapshot(key).reserved_usd == 0.0


async def test_a_subscription_worker_under_a_strict_token_cap_is_refused_before_any_launch(manager, repo):
    """0 $ établi ≠ garantie de tokens : le backend abonnement n'offre pas de plafond de sortie effectif."""
    pid = _linear_project(manager, 1)
    sandbox = _Sandbox(_usage_line())
    settings = _subscription_settings(1.0, MAX_TOKENS_BUDGET=500_000)
    result = await _oh_pass(manager, repo, pid, sandbox, settings=settings)
    assert result[1].stop_reason == "paused_budget" and sandbox.calls == []


async def test_a_subscription_worker_stays_available_under_a_token_cap_in_advisory_mode(manager, repo):
    pid = _linear_project(manager, 1)
    sandbox = _Sandbox(f"[collegue-budget] armed {{}}\n{_usage_line()}\n[collegue-budget] final\nOH_RUNNER_DONE\n")
    settings = _subscription_settings(
        1.0, MAX_TOKENS_BUDGET=500_000, BUDGET_MODE="advisory", BUDGET_EXHAUSTED_ACTION="warn"
    )
    await _oh_pass(manager, repo, pid, sandbox, settings=settings)
    assert len(sandbox.calls) == 1 and _ledger(manager, pid).strict is False


# --- opérateur : causes de blocage indépendantes ---------------------------------------------------------------


def test_the_operator_reads_and_resolves_independent_block_causes(manager):
    from collegue.pilot.budget import budget_status, resolve_budget_block

    pid = _linear_project(manager, 1)
    ledger = manager.budget_ledger
    key = ledger.scope_for_project(pid, max_cost_usd=1.0, max_tokens=100_000).scope_key
    ledger.block(key, reason="borne du fournisseur démentie", event_key="bound-1")

    status = budget_status(manager, pid)
    assert [b["block_key"] for b in status["blocks"]] == ["bound-1"] and status["scope"]["blocked_reason"]
    with pytest.raises(ValueError):
        resolve_budget_block(manager, pid, "bound-1", note="  ")  # une justification est exigée
    after = resolve_budget_block(manager, pid, "bound-1", note="hypothèse corrigée après revue")
    assert after["blocks"] == [] and after["scope"]["blocked_reason"] is None
    assert resolve_budget_block(manager, pid, "bound-1", note="hypothèse corrigée après revue")["blocks"] == []


def _worker_price_table(manager, **overrides):
    from collegue.core.llm.budget_guard import BudgetBinding
    from collegue.executor.worker_budget import allocate_worker

    pid = manager.create_project(name="p")
    key = manager.budget_ledger.scope_for_project(pid, max_cost_usd=5.0, strict=False).scope_key  # advisory
    settings = _settings(5.0, LLM_API_KEY="fake-key", **overrides)
    agent = OHSdkAgent(_Sandbox(), settings_obj=settings)
    alloc = allocate_worker(BudgetBinding(manager.budget_ledger, key, settings=settings), agent=agent)
    return {name: (price_in, price_out) for name, price_in, price_out in alloc.prices}


def test_the_worker_price_table_follows_the_coder_route_destination_gemini(manager):
    # Route Gemini : principal ET repli par défaut gemma, chacun tarifé à son propre prix dans SA famille (gratuit).
    table = _worker_price_table(manager, LLM_PROVIDER="gemini", LLM_MODEL_CODER="gemma-4-31b-it")
    assert table == {"gemini/gemma-4-31b-it": (0.0, 0.0), "gemini/gemma-4-26b-a4b-it": (0.0, 0.0)}


def test_the_worker_price_table_follows_the_coder_route_destination_openai(manager):
    # Route OpenAI : tarif cloud de SA famille ; aucun repli (jamais gemma envoyé à l'endpoint OpenAI).
    table = _worker_price_table(manager, LLM_PROVIDER="openai", LLM_MODEL_CODER="gpt-5.4")
    assert set(table) == {"openai/gpt-5.4"}
    assert table["openai/gpt-5.4"][0] > 0


# --- dry-run : aucune écriture au registre ----------------------------------------------------------------------


async def test_a_dry_run_never_touches_the_ledger(manager, repo):
    pid = _linear_project(manager, 1)
    agent = PricedAgent(0.6)
    await _run(manager, repo, pid, budget=_controller(), agent=agent, dry_run=True)
    assert agent.calls == 1 and _ledger(manager, pid) is None


# --- planification : le scope existe avant le projet ----------------------------------------------------------


class _FakePlannerClient:
    """Client OpenAI-compatible qui répond SPEC puis décomposition ; photographie le registre en cours d'appel."""

    def __init__(self, manager, *, fail_decompose=False):
        self.manager = manager
        self.fail_decompose = fail_decompose
        self.calls = []
        self.snapshots = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def with_options(self, **_):
        return self

    async def _create(self, *, model, messages, **_):
        system = messages[0]["content"] if messages and messages[0]["role"] == "system" else ""
        decompose = '"tasks"' in system
        self.calls.append("decompose" if decompose else "spec")
        ledger = self.manager.budget_ledger
        scopes = [ledger.snapshot(s.scope_key).to_dict() for s in self._scopes()]
        self.snapshots.append(scopes)
        if decompose:
            content = (
                "pas du json"
                if self.fail_decompose
                else (
                    '{"tasks": [{"title": "A", "acceptance": "a"}, {"title": "B", "acceptance": "b", "depends_on": [0]}]}'
                )
            )
        else:
            content = '{"title": "Demo", "summary": "s", "objectives": ["o"], "acceptance_criteria": ["le test passe"]}'
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
            usage=SimpleNamespace(prompt_tokens=300, completion_tokens=200),  # sous la borne du payload transmis
            model=model,
        )

    def _scopes(self):
        from sqlalchemy import select

        from collegue.state.models import BudgetScope

        with self.manager.session() as session:
            return list(session.scalars(select(BudgetScope)))


async def test_planning_spend_is_in_the_ledger_before_the_project_exists_and_survives_restart(url):
    from collegue.pilot import plan_project_from_settings

    manager = ProjectStateManager.from_url(url, create=True)
    client = _FakePlannerClient(manager)
    ctx = LocalSamplingContext(default_model=MODEL, client=client)
    settings = _settings(GATE_ACCEPTANCE_TESTS=False)

    plan = await plan_project_from_settings(
        "Demo", "construire une app", owner="o", repo="r", settings_obj=settings, manager=manager, ctx=ctx
    )

    assert client.calls == ["spec", "decompose"]
    # PENDANT le 1er appel (generate_spec), le projet n'existait pas : le scope de planification, lui, si,
    # et la réservation y était déjà prise.
    (during_spec,) = client.snapshots[0]
    assert during_spec["project_id"] is None and during_spec["reserved_usd"] > 0
    expected = 2 * (300 * 1.5e-6 + 200 * 9e-6)

    scope = manager.budget_ledger.snapshot_for_project(plan.project_id)  # lié au projet ensuite
    assert scope is not None and scope.scope_key.startswith("planning:")
    assert scope.consumed_usd == pytest.approx(expected, abs=1e-5) and scope.reserved_usd == 0.0

    # redémarrage : même solde, et BUILD retrouve le MÊME scope (pas un second registre)
    restarted = ProjectStateManager.from_url(url)
    again = restarted.budget_ledger.scope_for_project(plan.project_id, max_cost_usd=1.0)
    assert again.scope_key == scope.scope_key and again.consumed_usd == pytest.approx(expected, abs=1e-5)


async def test_a_failed_planning_keeps_its_spend_and_the_error(url):
    from collegue.pilot import plan_project_from_settings

    manager = ProjectStateManager.from_url(url, create=True)
    client = _FakePlannerClient(manager, fail_decompose=True)
    ctx = LocalSamplingContext(default_model=MODEL, client=client)
    with pytest.raises(Exception):
        await plan_project_from_settings(
            "Demo",
            "x",
            owner="o",
            repo="r",
            settings_obj=_settings(GATE_ACCEPTANCE_TESTS=False),
            manager=manager,
            ctx=ctx,
            retry_sleep_seconds=0,
            decompose_attempts=1,
        )
    from sqlalchemy import select

    from collegue.state.models import BudgetScope

    with manager.session() as session:
        scopes = list(session.scalars(select(BudgetScope)))
    assert len(scopes) == 1
    snap = manager.budget_ledger.snapshot(scopes[0].scope_key)
    assert snap.last_error and snap.consumed_usd > 0  # la planification ratée n'est pas « gratuite »
    assert snap.project_id is not None  # le projet (SPEC persistée) existe et reste lié


async def test_the_planning_entry_refuses_when_the_cap_is_already_spent(url):
    from collegue.pilot import plan_project_from_settings
    from collegue.state import BudgetRefused

    manager = ProjectStateManager.from_url(url, create=True)
    client = _FakePlannerClient(manager)
    ctx = LocalSamplingContext(default_model=MODEL, client=client)
    with pytest.raises(BudgetRefused):
        await plan_project_from_settings(
            "Demo", "x", owner="o", repo="r", settings_obj=_settings(cap=0.0001), manager=manager, ctx=ctx
        )
    assert client.calls == []  # plafond ridicule : AUCUN appel de planification n'a été émis


# --- voie opérateur : lire le motif durable, résoudre l'usage inconnu ---------------------------------------------


async def test_the_operator_can_read_the_durable_reason_and_resolve_the_unknown_usage(manager, repo, url):
    from collegue.pilot.budget import budget_status, resolve_unknown_usage

    pid = _linear_project(manager, 2)
    await _run(
        manager,
        repo,
        pid,
        budget=_controller(),
        agent=_CrashingAgent(),
        dry_run=False,
        max_iterations=1,
        reconcile_reviews=False,
        max_task_attempts=1,
    )

    status = budget_status(ProjectStateManager.from_url(url), pid)  # lecture depuis une autre instance
    assert status["scope"]["blocked_reason"] and len(status["unknown_reservations"]) == 1
    rid = status["unknown_reservations"][0]["reservation_id"]

    with pytest.raises(ValueError):
        resolve_unknown_usage(manager, pid, "call:inexistant", usd=0.0, tokens=0)
    after = resolve_unknown_usage(manager, pid, rid, usd=0.0, tokens=0, note="relevé: rien facturé")
    assert after["scope"]["blocked_reason"] is None and after["unknown_reservations"] == []

    for task in manager.get_tasks(pid):  # l'opérateur re-file aussi la tâche échouée (hors budget)
        if task.status == "failed":
            manager.update_task_status(task.id, "todo")
    healthy = PricedAgent(0.1)
    result = await _run(
        manager,
        repo,
        pid,
        budget=_controller(),
        agent=healthy,
        dry_run=False,
        reconcile_reviews=False,
        max_task_attempts=1,
    )
    assert healthy.calls >= 1 and result.stop_reason != "paused_budget"  # la suite repart après résolution
