"""Tests E5 (#367) : assemblage execute_issue() de bout en bout (fakes, fixture git)."""

import subprocess
from types import SimpleNamespace

import pytest

from collegue.executor import (
    ExecutionOutcome,
    FakeCodeAgent,
    FakeReviewer,
    IssueSpec,
    PrClients,
    execute_issue,
)
from collegue.executor.quality_gate import ReviewFindingLite
from collegue.sandbox import SandboxResult
from collegue.state import ProjectStateManager
from collegue.tools.quotas import BudgetExceeded

ISSUE = IssueSpec(number=11, title="Faire la chose")


# --- fakes ----------------------------------------------------------------------


class _Sandbox:
    def __init__(self, ok=True):
        self._ok = ok

    def run_tests(self, workspace, command="pytest -q"):
        return SandboxResult(exit_code=0 if self._ok else 1, stdout="tests output", stderr="")


class _Branches:
    def __init__(self):
        self.created = []

    def ensure_branch(self, owner, repo, branch, from_branch=None):
        self.created.append(branch)
        return SimpleNamespace(name=branch)


class _Files:
    def __init__(self):
        self.updated = []

    def update_file(self, owner, repo, path, message, content, branch=None):
        self.updated.append(path)
        return {}

    def delete_file(self, owner, repo, path, message, branch=None):
        return {}


class _PRs:
    def __init__(self):
        self.created = []

    def find_pr_by_head(self, owner, repo, head, base=None, state="open"):
        return None

    def create_pr(self, owner, repo, title, head, base, body):
        self.created.append({"head": head, "body": body})
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


def _kwargs(**overrides):
    base = dict(
        agent=FakeCodeAgent(),
        owner="o",
        repo="r",
        sandbox=_Sandbox(ok=True),
        reviewer=FakeReviewer(),
        clients=_clients(),
    )
    base.update(overrides)
    return base


# --- bout en bout ---------------------------------------------------------------


async def test_dry_run_success_previews_without_writes(repo):
    clients = _clients()
    outcome = await execute_issue(ISSUE, repo, ctx=None, dry_run=True, **_kwargs(clients=clients))
    assert isinstance(outcome, ExecutionOutcome)
    assert outcome.success is True
    assert outcome.stage == "pr"
    assert outcome.pr.dry_run is True
    assert "Closes #11" in outcome.pr.body
    # dry_run : aucune écriture GitHub
    assert clients.prs.created == []
    assert clients.branches.created == []
    # dry_run : aucune transition d'état
    assert outcome.final_status is None


async def test_real_run_advances_state_to_in_review(repo, tmp_path):
    manager = ProjectStateManager.from_url(f"sqlite:///{tmp_path / 'state.db'}", create=True)
    pid = manager.create_project(name="demo")
    tid = manager.add_task(pid, title="T")  # statut : todo
    clients = _clients()
    outcome = await execute_issue(
        ISSUE, repo, ctx=None, dry_run=False, manager=manager, task_id=tid, project_id=pid, **_kwargs(clients=clients)
    )
    assert outcome.success is True
    assert outcome.stage == "pr"
    assert outcome.pr.number == 101
    assert clients.prs.created and clients.branches.created  # écriture réelle
    # état avancé jusqu'à in_review (jamais done)
    task = next(t for t in manager.get_tasks(pid) if t.id == tid)
    assert task.status == "in_review"
    assert outcome.final_status == "in_review"


# --- fail-closed ----------------------------------------------------------------


async def test_tests_red_stops_at_gate_no_pr(repo, tmp_path):
    manager = ProjectStateManager.from_url(f"sqlite:///{tmp_path / 'state.db'}", create=True)
    pid = manager.create_project(name="demo")
    tid = manager.add_task(pid, title="T")
    clients = _clients()
    outcome = await execute_issue(
        ISSUE,
        repo,
        ctx=None,
        dry_run=False,
        manager=manager,
        task_id=tid,
        project_id=pid,
        **_kwargs(clients=clients, sandbox=_Sandbox(ok=False)),
    )
    assert outcome.success is False
    assert outcome.stage == "gate"
    assert outcome.reason == "gate_failed"  # raison différenciée (#421)
    assert outcome.pr is None
    assert clients.prs.created == []  # aucune PR
    # l'état ne dépasse pas in_progress
    task = next(t for t in manager.get_tasks(pid) if t.id == tid)
    assert task.status == "in_progress"
    assert outcome.final_status == "in_progress"


async def test_blocking_review_stops_at_gate(repo):
    clients = _clients()
    reviewer = FakeReviewer(blocking=True, findings=[ReviewFindingLite("security", "critical", "RCE")])
    outcome = await execute_issue(ISSUE, repo, ctx=None, dry_run=False, **_kwargs(clients=clients, reviewer=reviewer))
    assert outcome.success is False
    assert outcome.stage == "gate"
    assert outcome.pr is None
    assert clients.prs.created == []


async def test_agent_noop_stops_at_run(repo):
    clients = _clients()
    outcome = await execute_issue(
        ISSUE, repo, ctx=None, dry_run=False, **_kwargs(clients=clients, agent=FakeCodeAgent(files={}))
    )
    assert outcome.success is False
    assert outcome.stage == "run"
    assert outcome.reason == "no_op"  # agent OK mais zéro diff (#421)
    assert outcome.quality_report is None  # gate jamais atteint
    assert outcome.pr is None
    assert clients.prs.created == []


async def test_agent_process_error_has_distinct_reason(repo):
    """#421 : un agent dont le PROCESS échoue (exit ≠ 0) n'est pas un no-op —
    même stage `run`, mais reason `agent_error` (la couche retry en dépend)."""
    outcome = await execute_issue(ISSUE, repo, ctx=None, dry_run=False, **_kwargs(agent=FakeCodeAgent(succeed=False)))
    assert outcome.success is False
    assert outcome.stage == "run"
    assert outcome.reason == "agent_error"
    # Le diagnostic (logs agent) survit dans l'outcome.
    assert "échec simulé" in outcome.execution.agent_result.logs


async def test_success_outcome_has_no_reason(repo):
    outcome = await execute_issue(ISSUE, repo, ctx=None, dry_run=True, **_kwargs())
    assert outcome.success is True
    assert outcome.reason is None


def test_log_tail_bounds_long_text():
    from collegue.executor.pipeline import log_tail

    assert log_tail("") == ""
    assert log_tail("court") == "court"
    long = "x" * 5000
    tail = log_tail(long, 2000)
    assert len(tail) == 2001  # « … » + 2000 derniers caractères
    assert tail.startswith("…") and tail.endswith("x")


def test_failure_feedback_is_crisp_and_actionable():
    """#424 : la synthèse d'échec privilégie les lignes FAILED/ERROR de pytest
    (courtes, actionnables) — la sortie brute noierait l'agent au retry."""
    from collegue.executor import AgentResult, QualityReport, Workspace
    from collegue.executor.pipeline import ExecutionOutcome, failure_feedback
    from collegue.executor.runner import ExecutionResult

    ws = Workspace(path="/w", branch="b", base_commit="c")
    execution = ExecutionResult(
        agent_result=AgentResult(success=True, logs="journal de l'agent"),
        changed=True,
        diff="",
        files_changed=("a.py",),
        success=True,
    )

    def _report(output):
        return QualityReport(
            tests_passed=False,
            test_exit_code=1,
            test_output=output,
            review_summary="",
            review_findings=(),
            review_blocking=False,
            passed=False,
        )

    # Lignes FAILED/ERROR présentes → seules elles sont retenues (jointes, bornées).
    gate = ExecutionOutcome(
        success=False,
        stage="gate",
        workspace=ws,
        execution=execution,
        quality_report=_report("bruit\nFAILED tests/a.py::t1 - boom\nencore du bruit\nERROR tests/b.py - setup\n"),
        reason="gate_failed",
    )
    assert failure_feedback(gate) == "FAILED tests/a.py::t1 - boom ; ERROR tests/b.py - setup"

    # Pas de ligne FAILED → queue bornée de la sortie de tests.
    fuzzy = ExecutionOutcome(
        success=False,
        stage="gate",
        workspace=ws,
        execution=execution,
        quality_report=_report("z" * 1000),
        reason="gate_failed",
    )
    assert len(failure_feedback(fuzzy)) <= 401 and failure_feedback(fuzzy).endswith("z")

    # Échec au stage run (aucun rapport) → logs agent.
    run = ExecutionOutcome(success=False, stage="run", workspace=ws, execution=execution, reason="agent_error")
    assert failure_feedback(run) == "journal de l'agent"


async def test_reviewer_error_is_contained_not_propagated(repo):
    # Une exception « ordinaire » du reviewer est CONTENUE par le gate (fail-closed) :
    # le pipeline s'arrête proprement, sans laisser l'exception remonter.
    clients = _clients()
    reviewer = FakeReviewer(raises=RuntimeError("LLM indisponible"))
    outcome = await execute_issue(ISSUE, repo, ctx=None, dry_run=False, **_kwargs(clients=clients, reviewer=reviewer))
    assert outcome.success is False
    assert outcome.stage == "gate"
    assert outcome.pr is None
    assert clients.prs.created == []


async def test_budget_exception_propagates_through_pipeline(repo):
    # BudgetExceeded (BaseException) NE doit PAS être contenue : elle traverse le
    # gate ET execute_issue pour stopper la boucle (auto-pause budget, C4).
    reviewer = FakeReviewer(raises=BudgetExceeded("cost", 10.0, 5.0))
    with pytest.raises(BudgetExceeded):
        await execute_issue(ISSUE, repo, ctx=None, dry_run=False, **_kwargs(reviewer=reviewer))


# --- options du gate par projet (#438) ---------------------------------------------


async def test_gate_options_are_threaded_to_quality_gate(repo):
    # #438 : la configuration du gate (commande de tests, passe frontend…)
    # traverse execute_issue sans coupler l'exécuteur à la config.
    class _RecordingSandbox(_Sandbox):
        def __init__(self):
            super().__init__(ok=True)
            self.commands = []

        def run_tests(self, workspace, command="pytest -q"):
            self.commands.append(command)
            return super().run_tests(workspace, command)

    sandbox = _RecordingSandbox()
    outcome = await execute_issue(
        ISSUE,
        repo,
        ctx=None,
        dry_run=True,
        gate_options={"test_command": "make check"},
        **_kwargs(sandbox=sandbox),
    )
    assert outcome.success is True
    assert sandbox.commands == ["make check"]


# --- réensemencement du workspace au retry (#436) ----------------------------------


async def test_seed_diff_is_applied_before_agent(repo):
    """#436 : le diff d'une tentative précédente est ré-appliqué sur le clone neuf
    AVANT l'agent et fait partie du diff autoritatif final — la PR porte l'état
    complet (seed + réparation), pas seulement ce que l'agent vient d'écrire."""
    import os
    import subprocess as sp

    from collegue.executor.workspace import prepare_workspace

    scratch = prepare_workspace(repo, ISSUE)
    with open(os.path.join(scratch.path, "existing.txt"), "w", encoding="utf-8") as fh:
        fh.write("état de la meilleure tentative\n")
    _git(scratch.path, "add", "-A")
    seed = sp.run(["git", "diff", "--staged"], cwd=scratch.path, capture_output=True, text=True).stdout
    assert "existing.txt" in seed

    outcome = await execute_issue(
        ISSUE, repo, ctx=None, dry_run=True, seed_diff=seed, **_kwargs(agent=FakeCodeAgent(files={"new.txt": "x\n"}))
    )
    assert outcome.success is True
    assert "existing.txt" in outcome.execution.files_changed  # le seed est DANS le diff
    assert "new.txt" in outcome.execution.files_changed


async def test_invalid_seed_diff_falls_back_to_clean_clone(repo):
    # Best-effort : un seed inapplicable (corrompu / base déplacée) est ignoré —
    # la tentative continue sur le clone vierge au lieu d'échouer.
    outcome = await execute_issue(ISSUE, repo, ctx=None, dry_run=True, seed_diff="pas un diff git valide", **_kwargs())
    assert outcome.success is True
    assert "existing.txt" not in outcome.execution.files_changed


async def test_binary_diff_is_reseedable(repo):
    """#455 : le diff autoritatif est capturé avec ``--binary`` — un fichier
    binaire (png…) produit par une tentative est RÉ-APPLICABLE au retry (#436).
    Sans le payload, ``git apply`` échouait (« sans la ligne complète d'index »)
    et la mémoire de retry était neutralisée sur les tâches frontend."""
    import os

    from collegue.executor import AgentResult
    from collegue.executor.workspace import apply_seed_diff, prepare_workspace

    class _BinaryAgent:
        def implement_issue(self, workspace, issue):
            os.makedirs(os.path.join(workspace, "assets"), exist_ok=True)
            with open(os.path.join(workspace, "assets", "hero.png"), "wb") as fh:
                fh.write(b"\x89PNG\r\n\x1a\n" + bytes(range(64)))
            return AgentResult(success=True, logs="ok", files_changed=("assets/hero.png",))

    outcome = await execute_issue(ISSUE, repo, ctx=None, dry_run=True, **_kwargs(agent=_BinaryAgent()))
    assert outcome.success is True
    assert "GIT binary patch" in outcome.execution.diff  # payload embarqué

    fresh = prepare_workspace(repo, ISSUE)
    assert apply_seed_diff(fresh, outcome.execution.diff) is True
    assert os.path.exists(os.path.join(fresh.path, "assets", "hero.png"))


# --- barrière d'exception par tâche (#435) ----------------------------------------


class _BoomAgent:
    def implement_issue(self, workspace, issue):
        raise RuntimeError("panne réseau simulée")


async def test_infra_exception_becomes_engine_error_outcome(repo):
    """#435 : une exception d'infrastructure ne remonte PLUS crue — outcome failed
    (reason=engine_error, stage atteint), exception dans ``error`` ET en feedback :
    elle entre dans le chemin retry du pilote au lieu de tuer le run entier."""
    from collegue.executor.pipeline import failure_feedback

    outcome = await execute_issue(ISSUE, repo, ctx=None, dry_run=True, **_kwargs(agent=_BoomAgent()))
    assert outcome.success is False
    assert outcome.reason == "engine_error"
    assert outcome.stage == "run"
    assert "panne réseau simulée" in outcome.error
    assert "panne réseau simulée" in failure_feedback(outcome)


async def test_workspace_error_caught_with_synthetic_execution(tmp_path):
    # Panne AVANT l'agent (clone impossible) : l'outcome reste exploitable
    # (workspace None, exécution synthétique) au lieu d'une WorkspaceError crue.
    outcome = await execute_issue(ISSUE, str(tmp_path / "absent"), ctx=None, dry_run=True, **_kwargs())
    assert outcome.success is False and outcome.reason == "engine_error"
    assert outcome.stage == "run"
    assert outcome.workspace is None
    assert "[engine] exception avant l'agent" in outcome.execution.agent_result.logs


async def test_pr_stage_exception_keeps_stage_and_real_feedback(repo):
    # Panne réseau à l'OUVERTURE de PR : stage=pr, et le feedback est l'exception —
    # PAS les logs (verts) de l'agent, qui seraient un motif de retry trompeur.
    from collegue.executor.pipeline import failure_feedback

    class _DownPRs(_PRs):
        def create_pr(self, owner, repo, title, head, base, body):
            raise ConnectionError("GitHub 502 simulé")

    clients = PrClients(branches=_Branches(), files=_Files(), prs=_DownPRs())
    outcome = await execute_issue(ISSUE, repo, ctx=None, dry_run=False, **_kwargs(clients=clients))
    assert outcome.success is False and outcome.reason == "engine_error"
    assert outcome.stage == "pr"
    assert "GitHub 502" in failure_feedback(outcome)


async def test_base_exception_still_propagates_from_agent(repo):
    # Les BaseException (annulation asyncio, arrêt process) traversent la barrière —
    # même contrat que BudgetExceeded ci-dessus.
    import asyncio

    class _Cancel:
        def implement_issue(self, workspace, issue):
            raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await execute_issue(ISSUE, repo, ctx=None, dry_run=True, **_kwargs(agent=_Cancel()))


# --- garanties d'état -----------------------------------------------------------


async def test_dry_run_does_not_transition_state(repo, tmp_path):
    manager = ProjectStateManager.from_url(f"sqlite:///{tmp_path / 'state.db'}", create=True)
    pid = manager.create_project(name="demo")
    tid = manager.add_task(pid, title="T")
    await execute_issue(ISSUE, repo, ctx=None, dry_run=True, manager=manager, task_id=tid, project_id=pid, **_kwargs())
    task = next(t for t in manager.get_tasks(pid) if t.id == tid)
    assert task.status == "todo"  # aucune transition en dry_run


async def test_never_auto_done(repo, tmp_path):
    manager = ProjectStateManager.from_url(f"sqlite:///{tmp_path / 'state.db'}", create=True)
    pid = manager.create_project(name="demo")
    tid = manager.add_task(pid, title="T")
    outcome = await execute_issue(
        ISSUE, repo, ctx=None, dry_run=False, manager=manager, task_id=tid, project_id=pid, **_kwargs()
    )
    task = next(t for t in manager.get_tasks(pid) if t.id == tid)
    # Un succès s'arrête EXACTEMENT à in_review (jamais done) : assertion forte qui
    # détecterait une transition manquante (in_progress) autant qu'un done illicite.
    assert outcome.final_status == "in_review"
    assert task.status == "in_review"
    assert task.status != "done"  # le merge (humain) fera done, pas l'exécuteur


async def test_manager_without_task_id_is_safe(repo, tmp_path):
    # manager fourni mais task_id absent : aucune transition tentée, pas de crash.
    manager = ProjectStateManager.from_url(f"sqlite:///{tmp_path / 'state.db'}", create=True)
    pid = manager.create_project(name="demo")
    outcome = await execute_issue(
        ISSUE, repo, ctx=None, dry_run=False, manager=manager, task_id=None, project_id=pid, **_kwargs()
    )
    assert outcome.success is True
    assert outcome.final_status is None  # rien à transitionner sans task_id


# --- bruit infra vs diagnostic fonctionnel (#459) -----------------------------------


def test_is_infra_noise_detects_network_tracebacks():
    from collegue.executor.pipeline import is_infra_noise

    assert is_infra_noise("urllib3.exceptions.ReadTimeoutError: HTTPSConnectionPool(host='pypi.org')")
    assert is_infra_noise("requests.exceptions.ConnectionError: Connection refused")
    assert is_infra_noise("503 Server Error: Service Unavailable")


def test_is_infra_noise_never_flags_functional_diagnostics():
    from collegue.executor.pipeline import is_infra_noise

    assert not is_infra_noise("")
    assert not is_infra_noise("FAILED tests/test_x.py::test_a - assert 1 == 2")
    # Une ligne FAILED présente = fonctionnel, même si du bruit réseau l'entoure.
    assert not is_infra_noise(
        "urllib3.exceptions.ReadTimeoutError: ...\nFAILED tests/test_x.py::test_a - ConnectionError"
    )
    assert not is_infra_noise("ADÉQUATION REFUSÉE — le diff ne réalise pas l'issue : schéma sans logique")
    # Un échec FONCTIONNEL qui mentionne une exception réseau reste actionnable
    # (projets web : TestClient/mocks lèvent ConnectionError dans les FAILED).
    assert not is_infra_noise("FAILED tests/test_api.py::test_remote - requests.exceptions.ConnectionError: refusé")
    # Le « ERROR: » de pip (deux-points) est du bruit d'install, PAS du pytest.
    assert is_infra_noise("ERROR: Could not install packages due to an OSError: ReadTimeoutError")
    # Kill du conteneur au timeout (#461) : la note sandbox est le seul indice
    # quand pip pend sans avoir imprimé de traceback réseau.
    assert is_infra_noise("Collecting fastapi\n[sandbox] délai dépassé après 120s")
