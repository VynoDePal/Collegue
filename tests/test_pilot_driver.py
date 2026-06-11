"""Tests F3 (#376) : Project Driver — boucle execute_issue sous budget + bascule MVP.

Pipeline réel par tâche (prepare_workspace + run_issue sur git fixture) avec
sandbox/reviewer/clients factices. Budget injecté (déterministe).
"""

import os
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


def _sibling_project(manager, n=2):
    """Tâches SŒURS : indépendantes entre elles (toutes prêtes dès la 1re passe)."""
    pid = manager.create_project(name="demo-siblings")
    for i in range(n):
        manager.add_task(pid, title=f"S{i}")
    return pid


async def _run(manager, repo, pid, *, budget=None, dry_run=True, sandbox=None, agent=None, clients=None, **kw):
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
        clients=clients or _clients(),
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


# --- cohérence inter-tâches sur PR non mergée (#411) -------------------------------


async def test_unmerged_dep_start_is_signaled(repo, manager):
    """#411 (mode historique) : démarrer un dépendant alors que sa dépendance est
    `in_review` (PR non mergée → code absent du clone) est SIGNALÉ dans l'audit
    (`task_started.unmerged_deps`) au lieu d'être silencieux."""
    from collegue.pilot.audit import RunAuditLog

    pid = _linear_project(manager, 2)
    audit = RunAuditLog(pid)
    result = await _run(manager, repo, pid, dry_run=False, audit=audit)
    assert result.stop_reason == "completed"
    started = [e for e in audit.events if e.kind == "task_started"]
    t0 = manager.get_tasks(pid)[0]
    assert "unmerged_deps" not in started[0].detail  # T0 (racine) : rien à signaler
    assert started[1].detail["unmerged_deps"] == [t0.id]  # T1 démarrée sur T0 non mergée


async def test_agent_context_flags_unmerged_dependency(repo, manager):
    # Le contexte donné à l'agent ne MENT plus : une dépendance in_review n'est pas
    # présentée comme « déjà construite » (son code peut être absent du clone).
    pid = _linear_project(manager, 2)
    agent = _RecordingAgent()
    await _run(manager, repo, pid, dry_run=False, agent=agent)
    ctx = agent.contexts[1]  # T1, dépend de T0 in_review
    assert "PAS encore" in ctx and "ABSENT" in ctx
    assert "déjà construites" not in ctx


async def test_require_merged_deps_stops_awaiting_merge_then_resumes(repo, manager):
    """#411 (mode strict) : la PR de T0 n'étant pas mergée, T1 ne démarre pas et le
    run s'arrête `awaiting_merge` (≠ `blocked` : seul un merge humain manque).
    Après le merge, un nouveau run reprend naturellement et termine."""
    pid = _linear_project(manager, 2)
    result = await _run(manager, repo, pid, dry_run=False, require_merged_deps=True)
    assert result.iterations == 1  # T0 seulement
    assert result.stop_reason == "awaiting_merge"
    t0, t1 = manager.get_tasks(pid)
    assert t0.status == "in_review" and t1.status == "todo"

    manager.update_task_status(t0.id, "merged")  # merge humain
    result2 = await _run(manager, repo, pid, dry_run=False, require_merged_deps=True)
    assert result2.stop_reason == "completed"
    assert manager.get_tasks(pid)[1].status == "in_review"


async def test_strict_mode_hard_failure_still_blocked(repo, manager):
    # Un échec dur n'est PAS un merge manquant : `blocked`, pas `awaiting_merge`.
    pid = _linear_project(manager, 2)
    result = await _run(manager, repo, pid, dry_run=False, require_merged_deps=True, sandbox=_Sandbox(ok=False))
    assert result.stop_reason == "blocked"


# --- intégration sérielle en mode strict (#434) -------------------------------------


async def test_strict_mode_serializes_sibling_prs(repo, manager):
    """#434 : deux tâches SŒURS (indépendantes) ne sont plus construites depuis la
    même base — dès la 1re PR en vol, le run s'arrête `awaiting_merge` ; la sœur
    n'est construite qu'après le merge (sa base inclut alors la PR précédente →
    plus de PRs sœurs en conflit, merge 405 irrécupérable)."""
    pid = _sibling_project(manager, 2)
    result = await _run(manager, repo, pid, dry_run=False, require_merged_deps=True)
    assert result.stop_reason == "awaiting_merge"
    assert result.iterations == 1  # S0 seulement : S1 attend le merge
    s0, s1 = manager.get_tasks(pid)
    assert s0.status == "in_review" and s1.status == "todo"

    manager.update_task_status(s0.id, "merged")  # merge humain
    result2 = await _run(manager, repo, pid, dry_run=False, require_merged_deps=True)
    assert result2.iterations == 1
    assert result2.stop_reason == "completed"
    assert manager.get_tasks(pid)[1].status == "in_review"


async def test_strict_mode_inflight_cap_is_configurable(repo, manager):
    # max_inflight_reviews=2 : les deux sœurs partent dans la même passe (N PRs en
    # vol, comportement pré-#434) — l'opérateur choisit explicitement son risque.
    pid = _sibling_project(manager, 2)
    result = await _run(manager, repo, pid, dry_run=False, require_merged_deps=True, max_inflight_reviews=2)
    assert result.stop_reason == "completed"
    assert result.iterations == 2


async def test_strict_mode_resume_with_inflight_pr_starts_nothing(repo, manager):
    # Reprise : une PR d'un run précédent est déjà en vol → on ne démarre RIEN
    # (la base clonée ne l'inclut pas) ; `awaiting_merge` immédiat.
    pid = _sibling_project(manager, 2)
    manager.update_task_status(manager.get_tasks(pid)[0].id, "in_review")
    result = await _run(manager, repo, pid, dry_run=False, require_merged_deps=True)
    assert result.stop_reason == "awaiting_merge"
    assert result.iterations == 0


async def test_historical_mode_keeps_chaining_siblings(repo, manager):
    # Hors mode strict : comportement inchangé, les sœurs s'enchaînent dans la passe
    # (`in_review` débloque déjà — le plafond #434 ne s'applique qu'au mode strict).
    pid = _sibling_project(manager, 2)
    result = await _run(manager, repo, pid, dry_run=False)
    assert result.stop_reason == "completed"
    assert result.iterations == 2


# --- gouvernance de coût : canal coder → ledger (#441) -------------------------------


async def test_coder_usage_feeds_run_cost_ledger(repo, manager):
    """#441 : l'usage auto-déclaré par l'agent (tokens/coût du canal coder,
    majoritaire en dépense et invisible du MetricsCollector serveur) alimente le
    ledger du run — la gouvernance de coût n'est plus structurellement morte."""
    import dataclasses

    from collegue.pilot.audit import RunAuditLog

    class _UsageAgent:
        def __init__(self):
            self._ok = FakeCodeAgent()

        def implement_issue(self, workspace, issue):
            result = self._ok.implement_issue(workspace, issue)
            return dataclasses.replace(result, prompt_tokens=1200, completion_tokens=300, cost_usd=0.002)

    pid = _linear_project(manager, 2)
    audit = RunAuditLog(pid)
    result = await _run(manager, repo, pid, dry_run=False, agent=_UsageAgent(), audit=audit)
    assert result.stop_reason == "completed"
    assert audit.cost.tokens == 2 * 1500
    assert audit.cost.usd == pytest.approx(2 * 0.002)
    assert len([e for e in audit.events if e.kind == "cost_observed"]) == 2


# --- hygiène des workspaces /tmp (#443) ----------------------------------------------


class _WorkspaceTrackingAgent:
    """FakeCodeAgent qui journalise le chemin de chaque workspace reçu."""

    def __init__(self):
        self.paths = []
        self._inner = FakeCodeAgent()

    def implement_issue(self, workspace, issue):
        self.paths.append(workspace)
        return self._inner.implement_issue(workspace, issue)


async def test_successful_task_workspace_is_cleaned(repo, manager):
    """#443 : plus de fuite /tmp — le clone d'une tâche réussie est détruit dès la
    PR ouverte (22 clones / 233 Mo laissés par le run v2, fuite linéaire)."""
    pid = _linear_project(manager, 2)
    agent = _WorkspaceTrackingAgent()
    result = await _run(manager, repo, pid, dry_run=False, agent=agent)
    assert result.stop_reason == "completed"
    assert len(agent.paths) == 2
    assert all(not os.path.exists(p) for p in agent.paths)


async def test_failed_task_keeps_only_last_workspace(repo, manager):
    # Échec terminal après retries : seul le DERNIER clone survit (post-mortem).
    from collegue.executor import cleanup_workspace

    async def _sleep(d):
        pass

    pid = _linear_project(manager, 1)
    agent = _WorkspaceTrackingAgent()
    result = await _run(
        manager,
        repo,
        pid,
        dry_run=False,
        agent=agent,
        sandbox=_Sandbox(ok=False),
        max_task_attempts=2,
        sleep_fn=_sleep,
    )
    assert result.stop_reason == "blocked"
    first, last = agent.paths
    assert not os.path.exists(first)  # tentative 1 purgée
    assert os.path.exists(last)  # dernier état conservé pour le debug
    cleanup_workspace(last)  # hygiène du test


async def test_retry_then_success_purges_kept_failure_workspace(repo, manager):
    # Une tâche qui finit par réussir ne laisse RIEN derrière elle.
    async def _sleep(d):
        pass

    pid = _linear_project(manager, 1)
    agent = _WorkspaceTrackingAgent()
    result = await _run(
        manager,
        repo,
        pid,
        dry_run=False,
        agent=agent,
        sandbox=_FlakySandbox(fail_times=1),
        max_task_attempts=3,
        sleep_fn=_sleep,
    )
    assert result.stop_reason == "completed"
    assert all(not os.path.exists(p) for p in agent.paths)


async def test_cleanup_workspaces_opt_out_keeps_everything(repo, manager):
    from collegue.executor import cleanup_workspace

    pid = _linear_project(manager, 1)
    agent = _WorkspaceTrackingAgent()
    await _run(manager, repo, pid, dry_run=False, agent=agent, cleanup_workspaces=False)
    assert all(os.path.exists(p) for p in agent.paths)  # debug : tout conservé
    for p in agent.paths:
        cleanup_workspace(p)  # hygiène du test


# --- réconciliation GitHub→état au démarrage (#442) ----------------------------------


class _ReconcilePRs(_PRs):
    """``find_pr_by_head`` scripté par branche (et journal des requêtes)."""

    def __init__(self, mapping):
        self._mapping = mapping
        self.queries = []

    def find_pr_by_head(self, owner, repo, head, base=None, state="open"):
        self.queries.append((head, state))
        value = self._mapping.get(head)
        if isinstance(value, Exception):
            raise value
        return value


def _reconcile_clients(mapping):
    return PrClients(branches=_Branches(), files=_Files(), prs=_ReconcilePRs(mapping))


async def test_reconcile_merged_pr_updates_state_and_unblocks_strict(repo, manager):
    """#442 : une tâche in_review dont la PR a été MERGÉE hors-run (merge manuel
    post-deadline) repart `merged` au démarrage — la reprise est idempotente et le
    mode strict ne bloque plus `awaiting_merge` sur du travail déjà dans main."""
    pid = _sibling_project(manager, 2)
    s0 = manager.get_tasks(pid)[0]
    manager.update_task_status(s0.id, "in_review")
    branch = f"collegue/issue-{s0.id}"
    clients = _reconcile_clients({branch: SimpleNamespace(number=72, state="closed", merged=True)})
    result = await _run(manager, repo, pid, dry_run=False, clients=clients, require_merged_deps=True)
    statuses = {t.title: t.status for t in manager.get_tasks(pid)}
    assert statuses["S0"] == "merged"  # vérité GitHub réalignée
    assert statuses["S1"] == "in_review"  # la sœur a pu se construire
    assert result.iterations == 1
    assert clients.prs.queries[0] == (branch, "all")


async def test_reconcile_closed_pr_requeues_with_feedback(repo, manager):
    # PR fermée SANS merge hors-run → redo : la tâche repart `todo` avec le
    # feedback (#424) et est relivrée dans CE run.
    pid = _linear_project(manager, 1)
    t0 = manager.get_tasks(pid)[0]
    manager.update_task_status(t0.id, "in_review")
    branch = f"collegue/issue-{t0.id}"
    clients = _reconcile_clients({branch: SimpleNamespace(number=64, state="closed", merged=False)})
    agent = _RecordingAgent()
    result = await _run(manager, repo, pid, dry_run=False, clients=clients, agent=agent)
    assert result.iterations == 1  # relivrée dans ce run
    assert "FERMÉE sans merge" in agent.contexts[0]  # feedback ré-injecté
    assert manager.get_tasks(pid)[0].status == "in_review"


async def test_reconcile_is_best_effort_and_conservative(repo, manager):
    """PR encore ouverte → in_review conservé ; PR introuvable → état intact ;
    GitHub injoignable → le run continue (best-effort, pas de mort du run)."""
    from collegue.pilot.audit import RunAuditLog

    pid = _sibling_project(manager, 3)
    s0, s1, s2 = manager.get_tasks(pid)
    for t in (s0, s1, s2):
        manager.update_task_status(t.id, "in_review")
    mapping = {
        f"collegue/issue-{s0.id}": SimpleNamespace(number=70, state="open", merged=False),
        f"collegue/issue-{s1.id}": None,
        f"collegue/issue-{s2.id}": RuntimeError("GitHub 503"),
    }
    audit = RunAuditLog(pid)
    result = await _run(manager, repo, pid, dry_run=False, clients=_reconcile_clients(mapping), audit=audit)
    assert result.stop_reason == "completed"  # rien à construire, rien n'a explosé
    assert all(t.status == "in_review" for t in manager.get_tasks(pid))
    assert [e for e in audit.events if e.kind == "task_reconciled"] == []


async def test_reconcile_skipped_in_dry_run(repo, manager):
    # dry_run n'écrit rien : pas de réconciliation (aperçu sans effet de bord).
    pid = _linear_project(manager, 1)
    t0 = manager.get_tasks(pid)[0]
    manager.update_task_status(t0.id, "in_review")
    branch = f"collegue/issue-{t0.id}"
    clients = _reconcile_clients({branch: SimpleNamespace(number=72, state="closed", merged=True)})
    await _run(manager, repo, pid, dry_run=True, clients=clients)
    assert clients.prs.queries == []
    assert manager.get_tasks(pid)[0].status == "in_review"


async def test_reconcile_audits_outcomes(repo, manager):
    from collegue.pilot.audit import RunAuditLog

    pid = _sibling_project(manager, 2)
    s0, s1 = manager.get_tasks(pid)
    manager.update_task_status(s0.id, "in_review")
    manager.update_task_status(s1.id, "in_review")
    mapping = {
        f"collegue/issue-{s0.id}": SimpleNamespace(number=72, state="closed", merged=True),
        f"collegue/issue-{s1.id}": SimpleNamespace(number=64, state="closed", merged=False),
    }
    audit = RunAuditLog(pid)
    await _run(manager, repo, pid, dry_run=False, clients=_reconcile_clients(mapping), audit=audit)
    outcomes = {e.detail["pr_number"]: e.detail["outcome"] for e in audit.events if e.kind == "task_reconciled"}
    assert outcomes == {72: "merged", 64: "closed_requeued"}


# --- drain des reviews au stop (#440) ------------------------------------------------


async def test_deadline_stop_exposes_pending_reviews(repo, manager):
    """#440 : au STOP_DEADLINE, les tâches `in_review` (gate vert, PR ouverte) sont
    EXPOSÉES dans le résultat — l'appelant sait qu'un drain (merge) est requis au
    lieu de découvrir la PR abandonnée en post-mortem (PR FacNor #72)."""
    pid = _sibling_project(manager, 2)
    result = await _run(manager, repo, pid, dry_run=False, budget=_Budget([CONT, DEADLINE]))
    assert result.stop_reason == "deadline_reached"
    s0 = manager.get_tasks(pid)[0]
    assert result.pending_reviews == [s0.id]  # S0 validée, en attente de merge


async def test_pending_reviews_reflect_all_unmerged_work(repo, manager):
    # Vrai pour TOUT motif d'arrêt : un run `completed` en mode historique laisse
    # toutes ses PRs en attente de merge — le drain les voit aussi.
    pid = _sibling_project(manager, 2)
    result = await _run(manager, repo, pid, dry_run=False)
    assert result.stop_reason == "completed"
    assert result.pending_reviews == [t.id for t in manager.get_tasks(pid)]

    # …et vide quand tout est mergé (reprise d'un projet terminé).
    for t in manager.get_tasks(pid):
        manager.update_task_status(t.id, "merged")
    result2 = await _run(manager, repo, pid, dry_run=False)
    assert result2.pending_reviews == []


def test_run_report_flags_pending_reviews():
    # Le rapport lisible signale le drain requis (#440).
    from collegue.pilot import format_run_report
    from collegue.pilot.driver import ProjectRunResult

    result = ProjectRunResult(stop_reason="deadline_reached", iterations=1, processed=[], pending_reviews=[11])
    report = format_run_report(result, project_id=1)
    assert "drain requis" in report and "[11]" in report
    silent = ProjectRunResult(stop_reason="completed", iterations=0, processed=[])
    assert "drain requis" not in format_run_report(silent, project_id=1)


# --- options du gate par projet (#438) ----------------------------------------------


async def test_gate_options_threaded_from_run_project(repo, manager):
    # #438 : la config du gate traverse run_project → execute_issue → run_quality_gate.
    class _RecordingSandbox(_Sandbox):
        def __init__(self):
            super().__init__(ok=True)
            self.commands = []

        def run_tests(self, workspace, command="pytest -q"):
            self.commands.append(command)
            return super().run_tests(workspace, command)

    pid = _linear_project(manager, 1)
    sandbox = _RecordingSandbox()
    result = await _run(manager, repo, pid, dry_run=True, sandbox=sandbox, gate_options={"test_command": "make check"})
    assert result.stop_reason == "completed"
    assert sandbox.commands == ["make check"]


# --- mémoire de la meilleure tentative entre retries (#436) -------------------------


class _SeqSandbox:
    """Sorties de tests scriptées, une par appel (la dernière se répète)."""

    def __init__(self, outputs):
        self._outputs = list(outputs)
        self.calls = 0

    def run_tests(self, workspace, command="pytest -q"):
        out = self._outputs[min(self.calls, len(self._outputs) - 1)]
        self.calls += 1
        exit_code = 1 if ("failed" in out or "Error" in out) else 0
        return SandboxResult(exit_code=exit_code, stdout=out, stderr="")


class _SeedProbeAgent:
    """Écrit feature.py et mémorise l'état du workspace AVANT son passage (#436)."""

    def __init__(self):
        self.calls = 0
        self.contexts = []
        self.seeded = []  # le travail précédent était-il déjà dans le clone ?
        self.seen_content = []  # contenu trouvé (None si absent)

    def implement_issue(self, workspace, issue):
        self.calls += 1
        self.contexts.append(issue.context)
        marker = os.path.join(workspace, "feature.py")
        if os.path.exists(marker):
            self.seeded.append(True)
            with open(marker, encoding="utf-8") as fh:
                self.seen_content.append(fh.read())
        else:
            self.seeded.append(False)
            self.seen_content.append(None)
        with open(marker, "w", encoding="utf-8") as fh:
            fh.write(f"VERSION = {self.calls}\n")
        return AgentResult(success=True, logs="ok", files_changed=("feature.py",))


async def test_retry_reseeds_workspace_with_best_attempt(repo, manager):
    """#436 : la tentative suivante repart de l'ÉTAT de la meilleure tentative
    (workspace réensemencé, consigne de réparation ciblée) au lieu d'une
    régénération-loterie qui jette un travail quasi-vert."""

    async def _sleep(d):
        pass

    pid = _linear_project(manager, 1)
    agent = _SeedProbeAgent()
    sandbox = _SeqSandbox(["FAILED tests/test_x.py::t - boom\n1 failed, 26 passed", "27 passed"])
    result = await _run(
        manager, repo, pid, dry_run=False, agent=agent, sandbox=sandbox, max_task_attempts=3, sleep_fn=_sleep
    )
    assert result.stop_reason == "completed"
    assert agent.seeded == [False, True]  # tentative 2 : le travail précédent est LÀ
    ctx = agent.contexts[1]
    assert "RÉPARATION CIBLÉE" in ctx
    assert "FAILED tests/test_x.py::t" in ctx  # les échecs de la MEILLEURE tentative
    t = manager.get_tasks(pid)[0]
    assert t.best_passed == 26 and t.best_failed == 1  # mémoire persistée (DB)


async def test_worse_attempt_never_replaces_best(repo, manager):
    """#436 anti-oscillation : une tentative PIRE ne remplace pas la meilleure —
    l'essai suivant repart de la MEILLEURE tentative, pas de la dernière
    (la dégradation monotone de la task 7 FacNor devient impossible)."""

    async def _sleep(d):
        pass

    pid = _linear_project(manager, 1)
    agent = _SeedProbeAgent()
    sandbox = _SeqSandbox(
        [
            "FAILED tests/test_x.py::t - a\n1 failed, 26 passed",
            "FAILED tests/test_x.py::t - a\nFAILED tests/test_y.py::u - b\n3 failed, 24 passed",
            "27 passed",
        ]
    )
    result = await _run(
        manager, repo, pid, dry_run=False, agent=agent, sandbox=sandbox, max_task_attempts=3, sleep_fn=_sleep
    )
    assert result.stop_reason == "completed"
    # Tentative 2 voit l'état de la tentative 1 ; tentative 3 voit ENCORE l'état 1
    # (le 24/27 de la tentative 2 n'a pas remplacé le 26/27 de la 1).
    assert agent.seen_content == [None, "VERSION = 1\n", "VERSION = 1\n"]
    t = manager.get_tasks(pid)[0]
    assert t.best_passed == 26 and t.best_failed == 1


async def test_collection_error_attempt_is_not_memorized(repo, manager):
    # L'anti-exemple de la task 7 FacNor : un état qui ne COLLECTE même pas
    # (ImportError de conftest, 0 test) ne devient JAMAIS « meilleure tentative ».
    async def _sleep(d):
        pass

    pid = _linear_project(manager, 1)
    agent = _SeedProbeAgent()
    sandbox = _SeqSandbox(
        [
            "FAILED tests/test_x.py::t - a\n1 failed, 26 passed",
            "ImportError while loading conftest: app/crud.py:114 AttributeError",
            "27 passed",
        ]
    )
    result = await _run(
        manager, repo, pid, dry_run=False, agent=agent, sandbox=sandbox, max_task_attempts=3, sleep_fn=_sleep
    )
    assert result.stop_reason == "completed"
    t = manager.get_tasks(pid)[0]
    assert t.best_passed == 26  # l'ImportError n'a pas remplacé le 26/27


def test_issue_from_task_repair_mode_for_green_state():
    # État mémorisé VERT (la tentative a échoué APRÈS le gate, ex. panne à la PR) :
    # la consigne dit de relivrer l'état tel quel en corrigeant la cause externe.
    from collegue.pilot.driver import _issue_from_task

    t = SimpleNamespace(
        id=1,
        issue_number=5,
        title="T",
        acceptance="",
        depends_on=[],
        attempt_count=1,
        last_error="[pr/engine_error] tentative 1/3 — ConnectionError: GitHub 502",
        best_diff="diff --git a/x b/x",
        best_passed=27,
        best_failed=0,
        best_feedback=None,
    )
    ctx = _issue_from_task(t, {1: t}).context
    assert "RÉPARATION CIBLÉE" in ctx
    assert "passait les tests" in ctx
    assert "GitHub 502" in ctx


# --- barrière d'exception par tâche (#435) ------------------------------------------


class _ExplodingThenOkAgent:
    """Lève une exception CRUE N fois (panne d'infrastructure) puis réussit."""

    def __init__(self, boom_times=1):
        self.calls = 0
        self.contexts = []
        self._boom_times = boom_times
        self._ok = FakeCodeAgent()

    def implement_issue(self, workspace, issue):
        self.calls += 1
        self.contexts.append(issue.context)
        if self.calls <= self._boom_times:
            raise RuntimeError("connexion réinitialisée (simulé)")
        return self._ok.implement_issue(workspace, issue)


async def test_engine_exception_enters_retry_path_not_run_crash(repo, manager):
    """#435 : une exception d'infrastructure d'UNE tâche ne tue plus le run — elle
    devient un échec retentable (engine_error) qui entre dans le canal #420/#424
    (attempt_count, backoff, feedback ré-injecté) et le run aboutit."""
    from collegue.pilot.audit import RunAuditLog

    async def _sleep(d):
        pass

    pid = _linear_project(manager, 1)
    agent = _ExplodingThenOkAgent(boom_times=1)
    audit = RunAuditLog(pid)
    result = await _run(
        manager, repo, pid, dry_run=False, agent=agent, audit=audit, max_task_attempts=3, sleep_fn=_sleep
    )
    assert result.stop_reason == "completed"
    retries = [e for e in audit.events if e.kind == "task_retry"]
    assert retries and retries[0].detail["reason"] == "engine_error"
    assert "connexion réinitialisée" in retries[0].detail["error"]
    assert "connexion réinitialisée" in agent.contexts[1]  # feedback #424 ré-injecté


async def test_engine_exception_is_isolated_to_its_task(repo, manager):
    # Panne DURABLE sur S0 (exception à chaque tentative) → failed terminal ; le
    # run survit et construit S1 (avant : l'exception tuait run_project entier).
    class _SelectiveBoom:
        def __init__(self):
            self._ok = FakeCodeAgent()

        def implement_issue(self, workspace, issue):
            if issue.title == "S0":
                raise OSError("disque plein simulé")
            return self._ok.implement_issue(workspace, issue)

    pid = _sibling_project(manager, 2)
    result = await _run(manager, repo, pid, dry_run=False, agent=_SelectiveBoom())
    statuses = {t.title: t.status for t in manager.get_tasks(pid)}
    assert statuses == {"S0": "failed", "S1": "in_review"}
    assert result.stop_reason == "blocked"  # S0 échouée : le graphe reste incomplet
    s0 = next(t for t in manager.get_tasks(pid) if t.title == "S0")
    assert "engine_error" in s0.last_error and "disque plein" in s0.last_error


def test_requeue_task_for_redo_resets_for_feedback(manager):
    """#434 : contrat du close+redo — la tâche repart `todo` avec un feedback (#424)
    et un attempt_count MINIMAL (un conflit d'infrastructure ne consomme pas le
    budget de retries fonctionnels)."""
    from collegue.pilot import requeue_task_for_redo

    pid = _linear_project(manager, 1)
    tid = manager.get_tasks(pid)[0].id
    manager.update_task(tid, status="in_review", attempt_count=2)
    requeue_task_for_redo(manager, tid, message="[merge/conflit] ta PR était en conflit avec main — repars à jour")
    t = manager.get_tasks(pid)[0]
    assert t.status == "todo"
    assert t.attempt_count == 1
    assert "conflit" in t.last_error
    assert "conflit" in t.best_feedback  # #460 : le canal réparation aussi


def test_requeue_message_reaches_repair_prompt(manager):
    """#460 (cas réel FacNor v3) : une tâche en mode réparation (#436 — best_diff
    présent, échecs connus) re-filée via requeue_task_for_redo doit voir le
    message du redo dans son PROMPT — avant, seul best_feedback y était injecté
    et l'opérateur devait l'écrire à la main en base."""
    from collegue.pilot import requeue_task_for_redo
    from collegue.pilot.driver import _issue_from_task

    pid = _linear_project(manager, 1)
    tid = manager.get_tasks(pid)[0].id
    manager.update_task(
        tid,
        status="failed",
        attempt_count=3,
        best_diff="diff --git a/x.py b/x.py\n+x",
        best_passed=20,
        best_failed=2,
        best_feedback="FAILED tests/test_auth.py::test_login - no such table",
    )
    requeue_task_for_redo(manager, tid, message="[opérateur] crée le schéma dans les fixtures de test")
    task = manager.get_tasks(pid)[0]
    context = _issue_from_task(task).context
    assert "RÉPARATION CIBLÉE" in context  # le mode réparation reste actif
    assert "crée le schéma dans les fixtures" in context  # le message ATTEINT l'agent
    assert "test_login" in context  # ... sans perdre les échecs connus


def test_requeue_without_prior_feedback_sets_message(manager):
    from collegue.pilot import requeue_task_for_redo

    pid = _linear_project(manager, 1)
    tid = manager.get_tasks(pid)[0].id
    manager.update_task(tid, status="in_review")
    fields = requeue_task_for_redo(manager, tid, message="[reconcile] PR fermée hors-run")
    assert fields["best_feedback"] == "[reconcile] PR fermée hors-run"
    t = manager.get_tasks(pid)[0]
    assert t.best_feedback == "[reconcile] PR fermée hors-run"


def test_requeue_twice_replaces_redo_motive_not_stacks(manager):
    """#460 : deux requeues successifs ne nidifient pas les motifs — le motif
    précédent est consommé, seul le diagnostic d'ORIGINE est conservé."""
    from collegue.pilot import requeue_task_for_redo

    pid = _linear_project(manager, 1)
    tid = manager.get_tasks(pid)[0].id
    manager.update_task(tid, status="in_review", best_feedback="FAILED tests/test_x.py::t - boom")
    requeue_task_for_redo(manager, tid, message="[reconcile] PR fermée hors-run")
    requeue_task_for_redo(manager, tid, message="[merge/conflit] PR en conflit avec main")
    t = manager.get_tasks(pid)[0]
    assert "conflit" in t.best_feedback
    assert "FAILED tests/test_x.py::t - boom" in t.best_feedback  # l'origine survit
    assert "PR fermée hors-run" not in t.best_feedback  # le motif consommé disparaît
    assert t.best_feedback.count("échecs connus") == 1  # jamais nidifié


# --- ré-injection du feedback d'échec au retry (#424) ------------------------------


class _RecordingAgent:
    """Enregistre le contexte de chaque issue reçue (délègue au FakeCodeAgent)."""

    def __init__(self):
        self.contexts = []
        self._ok = FakeCodeAgent()

    def implement_issue(self, workspace, issue):
        self.contexts.append(issue.context)
        return self._ok.implement_issue(workspace, issue)


class _FlakySandbox:
    """Tests rouges N fois (sortie pytest réaliste) puis verts."""

    def __init__(self, fail_times=1):
        self.calls = 0
        self._fail_times = fail_times

    def run_tests(self, workspace, command="pytest -q"):
        self.calls += 1
        if self.calls <= self._fail_times:
            return SandboxResult(
                exit_code=1,
                stdout=(
                    "collected 4 items\n"
                    "bruit de collection\n"
                    "FAILED tests/test_auth.py::test_login - sqlalchemy.exc.OperationalError: no such table\n"
                    "1 failed, 3 passed"
                ),
                stderr="",
            )
        return SandboxResult(exit_code=0, stdout="4 passed", stderr="")


async def test_retry_reinjects_crisp_failure_feedback(repo, manager):
    """#424/#436 : la tentative suivante reçoit le MOTIF de l'échec précédent (lignes
    FAILED de pytest, pas la sortie brute) ET bascule en mode réparation ciblée
    (l'état de la meilleure tentative est réensemencé) — sans ça, le retry rejoue
    la même consigne à l'identique et reproduit le même bug."""

    async def _sleep(d):
        pass

    pid = _linear_project(manager, 1)
    agent = _RecordingAgent()
    result = await _run(
        manager,
        repo,
        pid,
        dry_run=False,
        agent=agent,
        sandbox=_FlakySandbox(fail_times=1),
        max_task_attempts=3,
        sleep_fn=_sleep,
    )
    assert result.stop_reason == "completed"
    assert len(agent.contexts) == 2
    assert "RÉPARATION" not in agent.contexts[0]  # 1re tentative : pas de feedback
    ctx = agent.contexts[1]
    assert "RÉPARATION CIBLÉE" in ctx  # mode réparation (#436), pas régénération
    assert "FAILED tests/test_auth.py::test_login" in ctx  # crisp et actionnable
    assert "collected 4 items" not in ctx  # le bruit n'est PAS ré-injecté


class _InfraThenGreenSandbox:
    """Échec FONCTIONNEL (ligne FAILED), puis aléa INFRA (traceback réseau sans
    ligne FAILED), puis vert — la séquence exacte du run FacNor v3 (#459)."""

    def __init__(self):
        self.calls = 0

    def run_tests(self, workspace, command="pytest -q"):
        self.calls += 1
        if self.calls == 1:
            # Le FAILED mentionne une exception réseau : il doit RESTER classé
            # actionnable une fois stocké dans last_error (préfixe compris).
            return SandboxResult(
                exit_code=1,
                stdout="FAILED tests/test_x.py::test_a - requests.exceptions.ConnectionError: email_validator absent",
                stderr="",
            )
        if self.calls == 2:
            return SandboxResult(
                exit_code=1,
                stdout=(
                    "Collecting fastapi\n"
                    "urllib3.exceptions.ReadTimeoutError: HTTPSConnectionPool(host='files.pythonhosted.org')\n"
                    "pip install a échoué"
                ),
                stderr="",
            )
        return SandboxResult(exit_code=0, stdout="4 passed", stderr="")


class _InfraGateForeverSandbox:
    """Gate rouge sur bruit RÉSEAU pur (timeout PyPI, aucune ligne FAILED)."""

    def __init__(self):
        self.calls = 0

    def run_tests(self, workspace, command="pytest -q"):
        self.calls += 1
        return SandboxResult(
            exit_code=1,
            stdout="urllib3.exceptions.ReadTimeoutError: HTTPSConnectionPool(host='files.pythonhosted.org')",
            stderr="",
        )


async def test_infra_gate_failure_does_not_consume_attempt_budget(repo, manager):
    """#461 : un gate rouge au diagnostic purement infra ne décompte pas le
    budget fonctionnel (grâce bornée) — puis recommence à décompter au-delà de
    MAX_INFRA_GATE_GRACE (pas de boucle infinie sur PyPI down)."""
    from collegue.pilot.driver import MAX_INFRA_GATE_GRACE

    async def _sleep(d):
        pass

    pid = _linear_project(manager, 1)
    result = await _run(
        manager,
        repo,
        pid,
        dry_run=False,
        agent=FakeCodeAgent(),
        sandbox=_InfraGateForeverSandbox(),
        max_task_attempts=2,
        sleep_fn=_sleep,
    )
    # MAX_INFRA_GATE_GRACE aléas graciés + 2 tentatives décomptées → terminal 2/2.
    assert result.stop_reason == "blocked"
    t = manager.get_tasks(pid)[0]
    assert t.status == "failed"
    assert t.attempt_count == 2
    assert MAX_INFRA_GATE_GRACE == 2  # contrat de borne (pas de boucle infinie)


async def test_infra_gate_grace_recorded_in_audit(repo, manager):
    from collegue.pilot.audit import RunAuditLog

    async def _sleep(d):
        pass

    pid = _linear_project(manager, 1)
    audit = RunAuditLog(pid)
    await _run(
        manager,
        repo,
        pid,
        dry_run=False,
        agent=FakeCodeAgent(),
        sandbox=_InfraGateForeverSandbox(),
        max_task_attempts=1,
        audit=audit,
        sleep_fn=_sleep,
    )
    graced = [e for e in audit.events if e.kind in ("task_retry", "task_failed") and e.detail.get("infra_grace")]
    assert len(graced) == 2  # MAX_INFRA_GATE_GRACE
    assert [e.detail["infra_grace"] for e in graced] == [1, 2]


async def test_functional_gate_failure_still_consumes_budget(repo, manager):
    """Contre-épreuve #461 : un échec FONCTIONNEL (lignes FAILED) décompte
    normalement — la grâce ne s'applique qu'au bruit infra."""

    async def _sleep(d):
        pass

    pid = _linear_project(manager, 1)
    result = await _run(
        manager,
        repo,
        pid,
        dry_run=False,
        agent=FakeCodeAgent(),
        sandbox=_FlakySandbox(fail_times=99),
        max_task_attempts=2,
        sleep_fn=_sleep,
    )
    assert result.stop_reason == "blocked"
    t = manager.get_tasks(pid)[0]
    assert t.attempt_count == 2  # aucun gracié


async def test_infra_noise_does_not_clobber_actionable_feedback(repo, manager):
    """#459 : un timeout PyPI à la tentative 2 n'écrase pas le diagnostic
    actionnable de la tentative 1 — l'agent (tentative 3) et l'opérateur voient
    toujours « email_validator », plus le marqueur d'aléa, borné."""

    async def _sleep(d):
        pass

    pid = _linear_project(manager, 1)
    agent = _RecordingAgent()
    result = await _run(
        manager,
        repo,
        pid,
        dry_run=False,
        agent=agent,
        sandbox=_InfraThenGreenSandbox(),
        max_task_attempts=3,
        sleep_fn=_sleep,
    )
    assert result.stop_reason == "completed"
    t = manager.get_tasks(pid)[0]
    assert t.status == "in_review"
    # Pendant la 3e tentative, last_error portait toujours le diagnostic utile.
    ctx = agent.contexts[2]
    assert "email_validator" in ctx
    assert "ReadTimeoutError" not in ctx  # le bruit réseau n'est PAS ré-injecté


async def test_infra_noise_suffix_is_replaced_not_stacked(repo, manager):
    """#459 : une SÉRIE d'aléas infra ne fait pas gonfler last_error — le suffixe
    est remplacé ; à l'échec terminal, le feedback persisté reste le diagnostic
    fonctionnel, pas le dernier aléa."""

    class _FunctionalThenInfraForever(_InfraThenGreenSandbox):
        def run_tests(self, workspace, command="pytest -q"):
            self.calls += 1
            if self.calls == 1:
                return SandboxResult(exit_code=1, stdout="FAILED tests/test_x.py::test_a - assert 1 == 2", stderr="")
            return SandboxResult(
                exit_code=1,
                stdout="urllib3.exceptions.ReadTimeoutError: HTTPSConnectionPool(host='pypi.org')",
                stderr="",
            )

    async def _sleep(d):
        pass

    pid = _linear_project(manager, 1)
    result = await _run(
        manager,
        repo,
        pid,
        dry_run=False,
        agent=_RecordingAgent(),
        sandbox=_FunctionalThenInfraForever(),
        max_task_attempts=3,
        sleep_fn=_sleep,
    )
    assert result.stop_reason == "blocked"
    t = manager.get_tasks(pid)[0]
    assert t.status == "failed"
    assert "FAILED tests/test_x.py::test_a" in t.last_error  # le diagnostic survit
    assert t.last_error.count("aléa infra") == 1  # suffixe remplacé, pas empilé
    assert "tentative 3" in t.last_error  # ... et à jour


async def test_run_stage_retry_feeds_agent_logs(repo, manager):
    # Échec au stage `run` (process agent) : le feedback vient des logs agent.
    async def _sleep(d):
        pass

    pid = _linear_project(manager, 1)
    agent = _FlakyAgent(fail_times=1)
    result = await _run(manager, repo, pid, dry_run=False, agent=agent, max_task_attempts=3, sleep_fn=_sleep)
    assert result.stop_reason == "completed"
    assert "503 transitoire simulé" in agent.contexts[1]


# --- retry au niveau tâche (#420) -------------------------------------------------


class _FlakyAgent:
    """Agent qui échoue N fois (process en erreur) puis réussit — simule un 503.

    Enregistre le ``context`` de chaque ``IssueSpec`` reçu (vérification de la
    ré-injection de feedback, #424).
    """

    def __init__(self, fail_times=1):
        self.calls = 0
        self.contexts = []
        self._fail_times = fail_times
        self._ok = FakeCodeAgent()

    def implement_issue(self, workspace, issue):
        self.calls += 1
        self.contexts.append(issue.context)
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
