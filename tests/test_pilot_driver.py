"""Tests F3 (#376) : Project Driver — boucle execute_issue sous budget + bascule MVP.

Pipeline réel par tâche (prepare_workspace + run_issue sur git fixture) avec
sandbox/reviewer/clients factices. Budget injecté (déterministe).
"""

import subprocess
from types import SimpleNamespace

import pytest

from collegue.executor import AgentResult, FakeCodeAgent, FakeReviewer, PrClients
from collegue.pilot import (
    ACTION_CONTINUE,
    ACTION_DEADLINE,
    ACTION_PAUSED_BUDGET,
    ContinueDecision,
    run_project,
)
from collegue.sandbox import SandboxResult
from collegue.state import ProjectStateManager

CONT = ContinueDecision(action=ACTION_CONTINUE, reason="ok")
PAUSE = ContinueDecision(action=ACTION_PAUSED_BUDGET, reason="budget")
DEADLINE = ContinueDecision(action=ACTION_DEADLINE, reason="deadline")


class _Budget:
    """Budget factice : déroule une séquence de décisions (dernière répétée)."""

    def __init__(self, seq):
        self._seq = list(seq)
        self._i = 0

    def should_continue(self):
        d = self._seq[min(self._i, len(self._seq) - 1)]
        self._i += 1
        return d


def _always():
    return _Budget([CONT])


class _Sandbox:
    def __init__(self, ok=True):
        self._ok = ok

    def run_tests(self, workspace, command="pytest -q"):
        return SandboxResult(exit_code=0 if self._ok else 1, stdout="out", stderr="")


class _Branches:
    def ensure_branch(self, owner, repo, branch, from_branch=None):
        return SimpleNamespace(name=branch)


class _Files:
    def update_file(self, owner, repo, path, message, content, branch=None):
        return {}

    def delete_file(self, owner, repo, path, message, branch=None):
        return {}


class _PRs:
    def find_pr_by_head(self, owner, repo, head, base=None, state="open"):
        return None

    def create_pr(self, owner, repo, title, head, base, body):
        return SimpleNamespace(number=101, html_url="https://gh/pull/101", head_branch=head)


def _clients():
    return PrClients(branches=_Branches(), files=_Files(), prs=_PRs())


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path):
    src = tmp_path / "source"
    src.mkdir()
    _git(src, "init", "-q")
    _git(src, "config", "user.email", "t@example.com")
    _git(src, "config", "user.name", "Test")
    (src / "existing.txt").write_text("original\n")
    _git(src, "add", "-A")
    _git(src, "commit", "-q", "-m", "init")
    return str(src)


@pytest.fixture
def manager(tmp_path):
    return ProjectStateManager.from_url(f"sqlite:///{tmp_path / 'state.db'}", create=True)


def _linear_project(manager, n=3):
    pid = manager.create_project(name="demo")
    prev = None
    for i in range(n):
        prev = manager.add_task(pid, title=f"T{i}", depends_on=[prev] if prev else None)
    return pid


async def _run(manager, repo, pid, *, budget=None, dry_run=True, sandbox=None, agent=None, **kw):
    return await run_project(
        pid,
        repo,
        ctx=None,
        agent=agent or FakeCodeAgent(),
        owner="o",
        repo="r",
        manager=manager,
        budget=budget or _always(),
        sandbox=sandbox or _Sandbox(ok=True),
        reviewer=FakeReviewer(),
        clients=_clients(),
        dry_run=dry_run,
        **kw,
    )


# --- contexte inter-tâches (#412) -----------------------------------------------


def test_issue_from_task_injects_dependency_context():
    from collegue.pilot.driver import _issue_from_task

    schema = SimpleNamespace(id=1, issue_number=1, title="Schéma DB", acceptance="", depends_on=[])
    api = SimpleNamespace(id=2, issue_number=12, title="API clients", acceptance="CRUD", depends_on=[1])
    by_id = {1: schema, 2: api}
    # une tâche dépendante reçoit le titre de ses dépendances déjà construites
    ctx = _issue_from_task(api, by_id).context
    assert "« Schéma DB »" in ctx and "réutilise" in ctx.lower()
    # une tâche racine (sans dépendance) n'a pas de contexte
    assert _issue_from_task(schema, by_id).context == ""


# --- bout en bout ---------------------------------------------------------------


async def test_dry_run_builds_whole_chain_without_writes(repo, manager):
    pid = _linear_project(manager, 3)
    result = await _run(manager, repo, pid, dry_run=True)
    assert result.stop_reason == "completed"
    assert result.iterations == 3
    assert all(t.success for t in result.processed)
    assert result.project_status is None  # dry_run n'écrit rien
    # aucune transition persistée
    assert all(t.status == "todo" for t in manager.get_tasks(pid))


async def test_real_run_advances_states_and_switches_to_improving(repo, manager):
    pid = _linear_project(manager, 2)
    result = await _run(manager, repo, pid, dry_run=False)
    assert result.stop_reason == "completed"
    assert result.iterations == 2
    assert result.project_status == "improving"
    assert all(t.status == "in_review" for t in manager.get_tasks(pid))
    assert manager.get_project(pid).status == "improving"
    assert result.opened_prs == [101, 101]


# --- arrêts ---------------------------------------------------------------------


async def test_budget_pause_stops_mid_run(repo, manager):
    pid = _linear_project(manager, 3)
    result = await _run(manager, repo, pid, dry_run=False, budget=_Budget([CONT, PAUSE]))
    assert result.stop_reason == "paused_budget"
    assert result.iterations == 1  # une tâche traitée avant la pause
    assert result.project_status is None  # pas de bascule MVP


async def test_budget_stops_before_first_task(repo, manager):
    # Budget déjà épuisé à la 1re itération : 0 tâche traitée, pas de bascule.
    pid = _linear_project(manager, 2)
    result = await _run(manager, repo, pid, dry_run=False, budget=_Budget([PAUSE]))
    assert result.stop_reason == "paused_budget"
    assert result.iterations == 0
    assert result.project_status is None
    assert all(t.status == "todo" for t in manager.get_tasks(pid))


async def test_deadline_stops_run(repo, manager):
    pid = _linear_project(manager, 2)
    result = await _run(manager, repo, pid, dry_run=False, budget=_Budget([CONT, DEADLINE]))
    assert result.stop_reason == "deadline_reached"
    assert result.iterations == 1


async def test_interrupted_in_progress_task_is_retried(repo, manager):
    # Reliquat `in_progress` (run précédent interrompu) → repassé `todo` et re-tenté,
    # PAS pris pour un MVP terminé.
    pid = _linear_project(manager, 1)
    tasks = manager.get_tasks(pid)
    manager.update_task_status(tasks[0].id, "in_progress")
    result = await _run(manager, repo, pid, dry_run=False)
    assert result.stop_reason == "completed"
    assert result.iterations == 1  # re-tentée
    assert result.project_status == "improving"
    assert manager.get_tasks(pid)[0].status == "in_review"


async def test_failed_task_blocks_dependents(repo, manager):
    # Tests rouges sur T0 → fail-closed ; T1 (dépend de T0) devient bloquée.
    pid = _linear_project(manager, 2)
    result = await _run(manager, repo, pid, dry_run=False, sandbox=_Sandbox(ok=False))
    assert result.iterations == 1
    assert result.processed[0].success is False
    assert result.processed[0].stage == "gate"
    assert result.stop_reason == "blocked"
    assert result.project_status is None
    statuses = {t.title: t.status for t in manager.get_tasks(pid)}
    assert statuses["T0"] == "failed"
    assert statuses["T1"] == "todo"  # jamais lancée


# --- retry au niveau tâche (#420) -------------------------------------------------


class _FlakyAgent:
    """Agent qui échoue N fois (process en erreur) puis réussit — simule un 503."""

    def __init__(self, fail_times=1):
        self.calls = 0
        self._fail_times = fail_times
        self._ok = FakeCodeAgent()

    def implement_issue(self, workspace, issue):
        self.calls += 1
        if self.calls <= self._fail_times:
            return AgentResult(success=False, logs="503 transitoire simulé")
        return self._ok.implement_issue(workspace, issue)


async def test_transient_failure_retried_then_recovers(repo, manager):
    """#420 : un échec transitoire ne fige plus le DAG — la tâche est re-filée
    `todo` avec backoff, réussit à la tentative suivante, et les dépendants
    s'enchaînent jusqu'au MVP (avant : `failed` terminal → run `blocked` à 0%)."""
    from collegue.pilot.audit import RunAuditLog

    pid = _linear_project(manager, 2)
    agent = _FlakyAgent(fail_times=1)
    sleeps = []

    async def _sleep(d):
        sleeps.append(d)

    audit = RunAuditLog(pid)
    result = await _run(
        manager, repo, pid, dry_run=False, agent=agent, audit=audit, max_task_attempts=3, sleep_fn=_sleep
    )
    assert result.stop_reason == "completed"
    statuses = {t.title: t.status for t in manager.get_tasks(pid)}
    assert statuses == {"T0": "in_review", "T1": "in_review"}
    assert sleeps == [15.0]  # backoff linéaire : 15 × tentative 1
    retries = [e for e in audit.events if e.kind == "task_retry"]
    assert len(retries) == 1
    assert retries[0].detail["reason"] == "agent_error"
    assert retries[0].detail["attempt"] == 1
    # Compteur + motif persistés (le plafond survit aux redémarrages).
    t0 = manager.get_tasks(pid)[0]
    assert t0.attempt_count == 1
    assert "agent_error" in t0.last_error and "503 transitoire" in t0.last_error


async def test_attempts_exhausted_marks_failed_and_blocks(repo, manager):
    pid = _linear_project(manager, 2)
    sleeps = []

    async def _sleep(d):
        sleeps.append(d)

    result = await _run(
        manager, repo, pid, dry_run=False, agent=FakeCodeAgent(files={}), max_task_attempts=3, sleep_fn=_sleep
    )
    assert result.stop_reason == "blocked"
    t0, t1 = manager.get_tasks(pid)
    assert t0.status == "failed"  # terminal après épuisement
    assert t0.attempt_count == 3
    assert "no_op" in t0.last_error
    assert sleeps == [15.0, 30.0]  # 15×1 puis 15×2 (plafonné à 90)
    assert t1.status == "todo"  # dépendant jamais lancé


async def test_default_module_behavior_is_no_retry(repo, manager):
    # Défaut du MODULE isolé (max_task_attempts=1) : comportement historique —
    # tout échec est terminal, aucun retry (le runtime, lui, passe la config).
    pid = _linear_project(manager, 1)
    result = await _run(manager, repo, pid, dry_run=False, agent=FakeCodeAgent(files={}))
    assert result.iterations == 1
    assert manager.get_tasks(pid)[0].status == "failed"


async def test_dry_run_retry_simulates_without_persisting_or_sleeping(repo, manager):
    pid = _linear_project(manager, 1)
    sleeps = []

    async def _sleep(d):
        sleeps.append(d)

    result = await _run(
        manager, repo, pid, dry_run=True, agent=_FlakyAgent(fail_times=1), max_task_attempts=3, sleep_fn=_sleep
    )
    assert result.stop_reason == "completed"  # la simulation retente et aboutit
    assert sleeps == []  # l'aperçu n'attend jamais
    t = manager.get_tasks(pid)[0]
    assert t.status == "todo" and t.attempt_count == 0 and t.last_error is None  # rien persisté


async def test_attempt_budget_survives_restart(repo, manager):
    # attempt_count est lu depuis la DB : 2 tentatives déjà consommées avant un
    # redémarrage → la 3e (dernière) échoue TERMINAL sans re-queue infinie.
    pid = _linear_project(manager, 1)
    tid = manager.get_tasks(pid)[0].id
    manager.update_task(tid, attempt_count=2)
    result = await _run(manager, repo, pid, dry_run=False, agent=FakeCodeAgent(files={}), max_task_attempts=3)
    t = manager.get_tasks(pid)[0]
    assert result.iterations == 1
    assert t.status == "failed" and t.attempt_count == 3


async def test_failure_is_audited_with_reason_and_log_tails(repo, manager):
    """#421 : la cause d'un échec SURVIT — l'audit porte la raison différenciée
    (gate_failed / no_op / agent_error) + extraits bornés des logs agent et de la
    sortie des tests (avant : seul success/stage, post-mortem impossible)."""
    from collegue.pilot.audit import RunAuditLog

    # Échec au gate (tests rouges) : raison + tails agent/tests.
    pid = _linear_project(manager, 1)
    audit = RunAuditLog(pid)
    await _run(manager, repo, pid, dry_run=False, sandbox=_Sandbox(ok=False), audit=audit)
    failed = [e for e in audit.events if e.kind == "task_failed"]
    assert len(failed) == 1
    detail = failed[0].detail
    assert detail["stage"] == "gate" and detail["reason"] == "gate_failed"
    assert "fake agent" in detail["agent_log_tail"]  # logs agent enfin journalisés
    assert detail["test_output_tail"] == "out"  # sortie des tests rouge capturée
    # gate_decision porte aussi la raison (None si succès).
    gates = [e for e in audit.events if e.kind == "gate_decision"]
    assert gates and gates[0].detail["reason"] == "gate_failed"

    # No-op de l'agent (zéro diff) : raison distincte, pas de sortie de tests.
    pid2 = _linear_project(manager, 1)
    audit2 = RunAuditLog(pid2)
    await _run(manager, repo, pid2, dry_run=False, agent=FakeCodeAgent(files={}), audit=audit2)
    failed2 = [e for e in audit2.events if e.kind == "task_failed"]
    assert failed2 and failed2[0].detail["reason"] == "no_op"
    assert "test_output_tail" not in failed2[0].detail  # gate jamais atteint


async def test_resume_skips_already_done(repo, manager):
    pid = _linear_project(manager, 2)
    tasks = manager.get_tasks(pid)
    manager.update_task_status(tasks[0].id, "in_review")  # T0 déjà faite (run précédent)
    result = await _run(manager, repo, pid, dry_run=False)
    assert result.iterations == 1  # seule T1 traitée
    assert result.processed[0].task_id == tasks[1].id


async def test_safety_cap_stops_runaway(repo, manager):
    pid = _linear_project(manager, 3)
    result = await _run(manager, repo, pid, dry_run=True, max_iterations=1)
    assert result.stop_reason == "safety_cap"
    assert result.iterations == 1


async def test_empty_project_completes_without_switching(repo, manager):
    pid = manager.create_project(name="empty")
    result = await _run(manager, repo, pid, dry_run=False)
    assert result.stop_reason == "completed"
    assert result.iterations == 0
    # Projet vide : pas de MVP construit → pas de bascule improving (anti-vacuité).
    assert result.project_status is None
    assert manager.get_project(pid).status != "improving"
