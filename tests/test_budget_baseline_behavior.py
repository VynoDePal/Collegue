"""Comportements budgétaires PUBLICS de la vague 2, écrits avec les seules API qui existaient avant elle.

Ces tests n'importent rien de nouveau : ils échouent sur la base ``9862b39`` pour une raison
FONCTIONNELLE (le budget laisse repartir une dépense déjà faite), pas par ``ImportError`` — c'est
la preuve rouge → vert de la vague. Ils restent comme tests de non-régression des cinq défauts
établis par l'audit du 28/09 :

1. deux passes BUILD à 0,60 $ séparées par un merge, plafond 1 $, la suite est encore autorisée ;
2. deux tentatives IMPROVE à 0,70 $ non débitées ;
3. un redémarrage (nouvelle instance, même base) remet la dépense à zéro ;
4. un worker qui meurt sans rapporter d'usage laisse la suite repartir ;
5. les frais de planification (SPEC, décomposition) ne figurent dans aucun coût de projet.
"""

from __future__ import annotations

import dataclasses
import subprocess
from types import SimpleNamespace

import pytest
from test_improve_loop import _clients as improve_clients
from test_improve_loop import _metrics, _ScriptedMeasure
from test_pilot_driver import _linear_project, _run

from collegue.executor import FakeCodeAgent
from collegue.improve import run_improvement
from collegue.monitoring.metrics import MetricsCollector
from collegue.pilot.audit import RunAuditLog, run_cost_summary
from collegue.pilot.budget import BudgetTimeController
from collegue.state import ProjectStateManager

MODEL = "gemini-3.5-flash"


@pytest.fixture(autouse=True)
def _isolated_metrics(tmp_path, monkeypatch):
    """Le MetricsCollector persiste sur disque : confiné à tmp_path (jamais le COLLEGUE_HOME du rôle)."""
    monkeypatch.setattr(MetricsCollector, "_PERSIST_DIR", tmp_path / "monitoring")


def _settings():
    return SimpleNamespace(
        MAX_COST_USD=1.0,
        MAX_TOKENS_BUDGET=0,
        BUDGET_EXHAUSTED_ACTION="pause",
        LLM_PROVIDER="gemini",
        LLM_MODEL=MODEL,
    )


def _controller():
    return BudgetTimeController(collector=MetricsCollector(), settings_obj=_settings())


class _Priced:
    budget_enforcement = "test-double"

    def __init__(self, price):
        self.calls = 0
        self.price = price
        self._delegate = FakeCodeAgent()

    def implement_issue(self, workspace, issue):
        self.calls += 1
        return dataclasses.replace(
            self._delegate.implement_issue(workspace, issue),
            cost_usd=self.price,
            prompt_tokens=100,
            completion_tokens=50,
        )


class _CrashesOnce:
    """Plante à la 1ʳᵉ passe sans rapporter d'usage (OOM-kill), puis fonctionnerait normalement."""

    budget_enforcement = "test-double"

    def __init__(self):
        self.calls = 0
        self._delegate = _Priced(0.1)

    def implement_issue(self, workspace, issue):
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("OOM-kill avant tout rapport d'usage")
        return self._delegate.implement_issue(workspace, issue)


@pytest.fixture
def url(tmp_path):
    return f"sqlite:///{tmp_path / 'state.db'}"


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


async def test_defect_1_two_build_passes_cannot_exceed_the_cap_across_a_merge(url, repo):
    manager = ProjectStateManager.from_url(url, create=True)
    pid = _linear_project(manager, 4)
    agent = _Priced(0.6)
    audit = RunAuditLog(pid, manager=manager, persist=True)
    for _ in range(3):  # une nouvelle passe (donc un nouveau contrôleur) après chaque merge
        await _run(
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
    assert agent.calls == 2  # 2 × 0,60 = 1,20 $ ≥ 1 $ : la 3ᵉ passe ne doit PAS être lancée
    assert run_cost_summary(manager, pid)["usd"] == pytest.approx(1.2)


async def test_defect_2_failed_improve_attempts_are_debited_and_stop_the_loop(url, repo):
    manager = ProjectStateManager.from_url(url, create=True)
    pid = _linear_project(manager, 1)
    manager.update_task_status(manager.get_tasks(pid)[0].id, "merged")
    agent = _Priced(0.7)
    await run_improvement(
        pid,
        repo,
        None,
        agent=agent,
        owner="o",
        repo="r",
        manager=manager,
        budget=_controller(),
        clients=improve_clients(),
        dry_run=False,
        plateau_rounds=6,
        measure_fn=_ScriptedMeasure([_metrics(0.5)] * 20),
        # le round est mesuré, jamais promu : la dépense du coder doit pourtant compter
    )
    assert agent.calls == 2  # 2 × 0,70 = 1,40 $ ≥ 1 $ : pas de 3ᵉ tentative
    assert run_cost_summary(manager, pid)["usd"] == pytest.approx(1.4)


async def test_defect_3_a_restart_does_not_reset_the_spend(url, repo):
    first = ProjectStateManager.from_url(url, create=True)
    pid = _linear_project(first, 3)
    agent = _Priced(0.7)
    await _run(
        first, repo, pid, budget=_controller(), agent=agent, dry_run=False, max_iterations=1, reconcile_reviews=False
    )
    restarted = ProjectStateManager.from_url(url)  # nouvelle instance, aucune donnée héritée
    _merge_all(restarted, pid)
    await _run(
        restarted,
        repo,
        pid,
        budget=_controller(),
        agent=agent,
        dry_run=False,
        max_iterations=1,
        reconcile_reviews=False,
    )
    _merge_all(restarted, pid)
    third = await _run(
        restarted,
        repo,
        pid,
        budget=_controller(),
        agent=agent,
        dry_run=False,
        max_iterations=1,
        reconcile_reviews=False,
    )
    assert agent.calls == 2 and third.stop_reason == "paused_budget"


async def test_defect_4_a_worker_that_dies_without_usage_report_stops_the_retry(url, repo):
    manager = ProjectStateManager.from_url(url, create=True)
    pid = _linear_project(manager, 1)
    agent = _CrashesOnce()

    async def instant(_delay):
        return None

    result = await _run(
        manager,
        repo,
        pid,
        budget=_controller(),
        agent=agent,
        dry_run=False,
        reconcile_reviews=False,
        max_task_attempts=3,
        sleep_fn=instant,
    )
    # Usage inconnu d'un worker mort : le retry ne repart pas sur une dépense non établie (la suite stricte
    # est bloquée avec un motif durable) — avant la vague 2, la tentative suivante repartait aussitôt.
    assert agent.calls == 1 and result.stop_reason == "paused_budget"


async def test_defect_5_planning_spend_reaches_the_project_cost(url):
    from collegue.core.llm.sampling_ctx import LocalSamplingContext
    from collegue.pilot import plan_project_from_settings

    manager = ProjectStateManager.from_url(url, create=True)

    class _Client:
        def __init__(self):
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

        def with_options(self, **_):
            return self

        async def _create(self, *, model, messages, **_):
            system = messages[0]["content"] if messages and messages[0]["role"] == "system" else ""
            content = (
                '{"tasks": [{"title": "A", "acceptance": "a"}]}'
                if '"tasks"' in system
                else '{"title": "Demo", "summary": "s", "objectives": ["o"], "acceptance_criteria": ["ok"]}'
            )
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
                usage=SimpleNamespace(prompt_tokens=300, completion_tokens=200),
                model=model,
            )

    settings = SimpleNamespace(**{**vars(_settings()), "GATE_ACCEPTANCE_TESTS": False})
    plan = await plan_project_from_settings(
        "Demo",
        "x",
        owner="o",
        repo="r",
        settings_obj=settings,
        manager=manager,
        ctx=LocalSamplingContext(default_model=MODEL, client=_Client()),
    )
    expected = 2 * (300 * 1.5e-6 + 200 * 9e-6)  # SPEC + décomposition, tarif autoritaire
    assert run_cost_summary(manager, plan.project_id)["usd"] == pytest.approx(expected, abs=1e-5)
