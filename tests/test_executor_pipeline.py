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


# --- dé-troncature du short summary pytest (#478) ------------------------------------


def test_failure_feedback_detruncates_pytest_short_summary():
    """#478 : pytest borne « FAILED nodeid - message » à COLUMNS (80 en non-tty)
    et tronque avec « ... » au moment exact où il nommait le paquet manquant —
    le message entier est repris des lignes ``E   …`` du traceback."""
    from collegue.executor.pipeline import failure_feedback

    output = (
        "==== ERRORS ====\n"
        "_ ERROR collecting tests/test_auth.py _\n"
        'E   RuntimeError: Form data requires "python-multipart" to be installed. '
        "You can install it with pip install python-multipart\n"
        "==== short test summary info ====\n"
        'ERROR tests/test_auth.py - RuntimeError: Form data requires "python-multipart...\n'
    )
    feedback = failure_feedback(_gate_outcome(output))
    assert "pip install python-multipart" in feedback
    assert not feedback.endswith("...")

    # Cas réel httpx (run v4, 09:21) : « The starlette.testclient module requ… ».
    output_httpx = (
        "E   RuntimeError: The starlette.testclient module requires the httpx package to be installed.\n"
        "==== short test summary info ====\n"
        "FAILED tests/test_app.py::test_root - RuntimeError: The starlette.testclient module requ...\n"
    )
    assert "httpx" in failure_feedback(_gate_outcome(output_httpx))


def test_detruncate_is_best_effort_and_bounded():
    """Sans ligne E correspondante, la ligne tronquée est relayée telle quelle ;
    une ligne E géante est bornée pour ne pas masquer les FAILED suivants."""
    from collegue.executor.pipeline import _detruncate_summary_line

    # Pas de traceback correspondant → inchangée.
    line = "FAILED tests/test_x.py::t - RuntimeError: mystere..."
    assert _detruncate_summary_line(line, "du bruit sans rapport") == line
    # Ligne sans « - » (nodeid nu) → inchangée.
    bare = "ERROR tests/test_auth.py..."
    assert _detruncate_summary_line(bare, "E   peu importe") == bare
    # Ligne non tronquée → inchangée (le filet ne réécrit jamais un diagnostic sain).
    clean = "FAILED tests/test_x.py::t - assert 1 == 2"
    assert _detruncate_summary_line(clean, "E   assert 1 == 2 long contexte") == clean
    # Ligne E géante → reprise bornée à 300 caractères de message.
    long_msg = "RuntimeError: contexte " + "x" * 500
    out = f"E   {long_msg}\n"
    detrunc = _detruncate_summary_line("FAILED tests/test_y.py::t - RuntimeError: contexte...", out)
    assert detrunc.startswith("FAILED tests/test_y.py::t - RuntimeError: contexte")
    assert len(detrunc) <= len("FAILED tests/test_y.py::t - ") + 300


# --- classification infra d'un échec de gate (#477) ----------------------------------


def _gate_outcome(output, *, deps_install_failed=False, reason="gate_failed", tests_passed=False):
    from collegue.executor import AgentResult, QualityReport, Workspace
    from collegue.executor.pipeline import ExecutionOutcome
    from collegue.executor.runner import ExecutionResult

    return ExecutionOutcome(
        success=False,
        stage="gate",
        workspace=Workspace(path="/w", branch="b", base_commit="c"),
        execution=ExecutionResult(
            agent_result=AgentResult(success=True, logs="journal"),
            changed=True,
            diff="",
            files_changed=("a.py",),
            success=True,
        ),
        quality_report=QualityReport(
            tests_passed=tests_passed,
            test_exit_code=0 if tests_passed else 1,
            test_output=output,
            review_summary="",
            review_findings=(),
            review_blocking=tests_passed,  # gate rouge à tests verts = revue bloquante (#437)
            passed=False,
            deps_install_failed=deps_install_failed,
        ),
        reason=reason,
    )


# Sortie réelle du run FacNor v4 (12:17, tâche 6) : pip échoue sur un timeout
# PyPI — la ligne « ERROR: Exception: » (deux-points) précède le traceback réseau.
_PIP_TIMEOUT_OUTPUT = (
    "Collecting fastapi\n"
    "ERROR: Exception:\n"
    "Traceback (most recent call last):\n"
    '  File "/usr/local/lib/python3.12/site-packages/pip/_vendor/urllib3/response.py", line 438, in _error_catcher\n'
    "    yield\n"
    "pip._vendor.urllib3.exceptions.ReadTimeoutError: "
    "HTTPSConnectionPool(host='pypi.org', port=443): Read timed out.\n"
)


def test_failure_feedback_ignores_pip_error_colon_lines():
    """#477 : « ERROR: Exception: » (pip) n'est PAS un diagnostic pytest — le
    feedback doit retomber sur la queue de sortie, qui PORTE la signature réseau,
    pour que la grâce #461 puisse classer l'échec en aléa infra."""
    from collegue.executor.pipeline import failure_feedback, is_infra_noise

    outcome = _gate_outcome(_PIP_TIMEOUT_OUTPUT)
    feedback = failure_feedback(outcome)
    assert feedback != "ERROR: Exception:"
    assert "ReadTimeoutError" in feedback
    assert is_infra_noise(feedback)


def test_is_infra_gate_failure_on_pip_timeout():
    from collegue.executor.pipeline import is_infra_gate_failure

    assert is_infra_gate_failure(_gate_outcome(_PIP_TIMEOUT_OUTPUT))
    # Même sortie mais échec hors gate (run/no_op) : jamais gracié.
    assert not is_infra_gate_failure(_gate_outcome(_PIP_TIMEOUT_OUTPUT, reason="no_op"))


def test_is_infra_gate_failure_on_install_cascade():
    """#477 : install en échec réseau (prelude #414 fail-open) → la collecte
    cascade en « ERROR tests/… » d'apparence fonctionnelle. deps_install_failed
    + signature réseau dans la sortie complète ⇒ aléa infra quand même."""
    from collegue.executor.pipeline import failure_feedback, is_infra_gate_failure, is_infra_noise

    cascade = (
        "Temporary failure in name resolution\n"
        "[gate] installation des dépendances en échec — tests lancés quand même (#414)\n"
        "ERROR tests/test_auth.py - ModuleNotFoundError: No module named 'httpx'\n"
    )
    outcome = _gate_outcome(cascade, deps_install_failed=True)
    # Le diagnostic court reste fonctionnel (lignes ERROR pytest)…
    assert not is_infra_noise(failure_feedback(outcome))
    # … mais la classification du gate, elle, voit la cause première réseau.
    assert is_infra_gate_failure(outcome)


def test_is_infra_gate_failure_keeps_functional_install_failures():
    """Un échec d'install SANS signature réseau (requirements invalide) reste
    fonctionnel : c'est le contrat que la passe #439 doit sanctionner."""
    from collegue.executor.pipeline import is_infra_gate_failure

    functional = (
        "ERROR: No matching distribution found for paquet-inexistant==1.0\n"
        "[gate] installation des dépendances en échec — tests lancés quand même (#414)\n"
        "ERROR tests/test_auth.py - ModuleNotFoundError: No module named 'paquet_inexistant'\n"
    )
    assert not is_infra_gate_failure(_gate_outcome(functional, deps_install_failed=True))


def test_is_infra_gate_failure_never_graces_green_tests():
    """Garde (revue #477) : gate rouge pour revue bloquante / adéquation (#437)
    avec tests VERTS — même si l'install a connu un aléa réseau en chemin (note
    #414 + signature dans la sortie), le verdict est fonctionnel : pas de grâce."""
    from collegue.executor.pipeline import is_infra_gate_failure

    green_but_blocked = (
        "Temporary failure in name resolution\n"
        "[gate] installation des dépendances en échec — tests lancés quand même (#414)\n"
        "12 passed in 1.02s\n"
    )
    outcome = _gate_outcome(green_but_blocked, deps_install_failed=True, tests_passed=True)
    assert not is_infra_gate_failure(outcome)


# --- crash d'import agent pré-LLM (#498) ---------------------------------------------


def _agent_error_outcome(logs, *, prompt_tokens=0, completion_tokens=0, reason="agent_error"):
    from collegue.executor import AgentResult, Workspace
    from collegue.executor.pipeline import ExecutionOutcome
    from collegue.executor.runner import ExecutionResult

    return ExecutionOutcome(
        success=False,
        stage="run",
        workspace=Workspace(path="/w", branch="b", base_commit="c"),
        execution=ExecutionResult(
            agent_result=AgentResult(
                success=False, logs=logs, prompt_tokens=prompt_tokens, completion_tokens=completion_tokens
            ),
            changed=False,
            diff="",
            files_changed=(),
            success=False,
        ),
        reason=reason,
    )


_IMPORT_CRASH_LOGS = (
    "Traceback (most recent call last):\n"
    '  File "/opt/oh_runner.py", line 31, in main\n'
    "    from openhands.sdk import LLM, Conversation\n"
    "ModuleNotFoundError: No module named 'lmnr'\n"
)


def test_is_infra_agent_crash_on_import_traceback():
    """#498 : crash d'import pré-LLM (agent_error, 0 token, ModuleNotFoundError)
    = aléa d'infra global (image cassée), pas un échec fonctionnel."""
    from collegue.executor.pipeline import is_infra_agent_crash

    assert is_infra_agent_crash(_agent_error_outcome(_IMPORT_CRASH_LOGS))


def test_is_infra_agent_crash_requires_zero_tokens():
    """L'agent a appelé le LLM (tokens > 0) puis échoué : échec FONCTIONNEL,
    jamais classé crash d'infra — même si les logs mentionnent un import."""
    from collegue.executor.pipeline import is_infra_agent_crash

    assert not is_infra_agent_crash(_agent_error_outcome(_IMPORT_CRASH_LOGS, completion_tokens=50))


def test_is_infra_agent_crash_requires_import_signature():
    """0 token mais pas de traceback d'import (assertion, autre crash) → non classé."""
    from collegue.executor.pipeline import is_infra_agent_crash

    assert not is_infra_agent_crash(_agent_error_outcome("AssertionError: x != y\n"))


def test_is_infra_agent_crash_handles_importerror():
    from collegue.executor.pipeline import is_infra_agent_crash

    assert is_infra_agent_crash(_agent_error_outcome("ImportError: cannot import name 'sdk' from 'openhands'\n"))


def test_is_infra_agent_crash_only_on_agent_error():
    """Un no_op ou un gate_failed avec les mêmes logs n'est pas un crash agent."""
    from collegue.executor.pipeline import is_infra_agent_crash

    assert not is_infra_agent_crash(_agent_error_outcome(_IMPORT_CRASH_LOGS, reason="no_op"))
    assert not is_infra_agent_crash(_agent_error_outcome(_IMPORT_CRASH_LOGS, reason="gate_failed"))


# --- remédiation requirements : recapture du diff (#481) -----------------------------


class _SeqGateSandbox:
    """Rouge (ModuleNotFoundError) puis vert — la remédiation du gate relance.

    Écrit aussi un ARTEFACT dans le workspace monté (comme le ferait le
    conteneur réel : __pycache__, node_modules, DB du smoke) — la recapture
    post-remédiation ne doit PAS l'embarquer dans la PR (revue #481)."""

    def __init__(self, results):
        self._results = list(results)
        self.calls = 0

    def run_tests(self, workspace, command="pytest -q"):
        self.calls += 1
        import pathlib

        artefacts = pathlib.Path(workspace, "node_modules")
        artefacts.mkdir(exist_ok=True)
        (artefacts / "artefact.js").write_text("// écrit par le conteneur du gate\n")
        return self._results.pop(0) if len(self._results) > 1 else self._results[0]


async def test_requirements_remediation_recaptures_diff_for_pr(repo):
    """#481 : le gate a amendé requirements.txt — le diff autoritatif est
    RECAPTURÉ, sinon la PR et la mémoire de retry (#436) partiraient sans le
    correctif (récidive du bug livré)."""
    red = SandboxResult(
        exit_code=2,
        stdout="E   ModuleNotFoundError: No module named 'httpx'\n",
        stderr="",
    )
    green = SandboxResult(exit_code=0, stdout="2 passed", stderr="")
    clients = _clients()
    outcome = await execute_issue(
        ISSUE,
        repo,
        ctx=None,
        dry_run=False,
        **_kwargs(
            agent=FakeCodeAgent(files={"requirements.txt": "fastapi\n", "app.py": "import httpx\n"}),
            sandbox=_SeqGateSandbox([red, green]),
            clients=clients,
        ),
    )
    assert outcome.success is True
    assert outcome.quality_report.requirements_added == ("httpx",)
    assert "+httpx" in outcome.execution.diff
    assert "requirements.txt" in outcome.execution.files_changed
    assert clients.prs.created  # la PR part AVEC le correctif
    # Revue #481 : les artefacts écrits par le conteneur du gate restent hors PR.
    assert not any("node_modules" in path for path in outcome.execution.files_changed)
    assert "artefact.js" not in outcome.execution.diff


async def test_remediation_failure_keeps_gate_red_and_diff_updated(repo):
    """#481 : remédiation insuffisante (toujours rouge) → gate rouge fail-closed,
    mais le diff recapturé porte l'ajout — cohérence de best_diff (#436)."""
    red = SandboxResult(
        exit_code=2,
        stdout="E   ModuleNotFoundError: No module named 'httpx'\n",
        stderr="",
    )
    outcome = await execute_issue(
        ISSUE,
        repo,
        ctx=None,
        dry_run=False,
        **_kwargs(
            agent=FakeCodeAgent(files={"requirements.txt": "fastapi\n", "app.py": "import httpx\n"}),
            sandbox=_SeqGateSandbox([red]),
        ),
    )
    assert outcome.success is False
    assert outcome.reason == "gate_failed"
    assert outcome.quality_report.requirements_added == ("httpx",)
    assert "+httpx" in outcome.execution.diff


# --- garde append-only requirements : feedback nominatif (#482) ----------------------


def test_failure_feedback_names_removed_requirements():
    """#482 : tests VERTS mais lignes de requirements perdues — le feedback est
    la liste NOMINATIVE des lignes (la sortie verte serait un feedback trompeur)."""
    from collegue.executor import AgentResult, QualityReport, Workspace
    from collegue.executor.pipeline import ExecutionOutcome, failure_feedback
    from collegue.executor.runner import ExecutionResult

    outcome = ExecutionOutcome(
        success=False,
        stage="gate",
        workspace=Workspace(path="/w", branch="b", base_commit="c"),
        execution=ExecutionResult(
            agent_result=AgentResult(success=True, logs="journal"),
            changed=True,
            diff="",
            files_changed=("requirements.txt",),
            success=True,
        ),
        quality_report=QualityReport(
            tests_passed=True,
            test_exit_code=0,
            test_output="12 passed in 1.02s",
            review_summary="",
            review_findings=(),
            review_blocking=False,
            passed=False,
            requirements_removed=("python-jose[cryptography]", "passlib[bcrypt]"),
        ),
        reason="gate_failed",
    )
    feedback = failure_feedback(outcome)
    assert "APPEND-ONLY" in feedback
    assert "python-jose[cryptography]" in feedback and "passlib[bcrypt]" in feedback
    assert "12 passed" not in feedback
    assert len(feedback) <= 700


async def test_requirements_regeneration_stops_at_gate(repo):
    """#482 bout en bout : l'agent régénère requirements.txt en perdant une ligne
    de la base → gate rouge, aucune PR."""
    import pathlib

    src = pathlib.Path(repo)
    (src / "requirements.txt").write_text("fastapi\npython-jose[cryptography]\n")
    _git(src, "add", "-A")
    _git(src, "commit", "-q", "-m", "deps de base")

    clients = _clients()
    outcome = await execute_issue(
        ISSUE,
        repo,
        ctx=None,
        dry_run=False,
        **_kwargs(
            agent=FakeCodeAgent(files={"requirements.txt": "fastapi\nhttpx\n"}),
            clients=clients,
        ),
    )
    assert outcome.success is False
    assert outcome.stage == "gate"
    assert outcome.reason == "gate_failed"
    assert outcome.quality_report.requirements_removed == ("python-jose[cryptography]",)
    assert clients.prs.created == []


def test_agent_crash_signature_is_stable_across_variable_preamble():
    """#498 (revue) : deux crashs de la MÊME cause avec préambule variable (ANSI,
    timestamps, chemin workspace randomisé) produisent la MÊME signature — sinon
    le fail-fast crash-loop ne se déclencherait jamais en conditions réelles."""
    from collegue.executor.pipeline import agent_crash_signature

    crash_a = (
        "\x1b[36m2026-06-12 01:00:01 WARNING litellm\x1b[0m\n"
        "running in /tmp/collegue-exec-aAaAaA/workspace\n"
        "Traceback (most recent call last):\n"
        '  File "/opt/oh_runner.py", line 31, in main\n'
        "ModuleNotFoundError: No module named 'lmnr'\n"
    )
    crash_b = (
        "\x1b[36m2026-06-12 02:33:47 WARNING litellm\x1b[0m\n"
        "running in /tmp/collegue-exec-zZzZzZ/workspace\n"
        "Traceback (most recent call last):\n"
        '  File "/opt/oh_runner.py", line 31, in main\n'
        "ModuleNotFoundError: No module named 'lmnr'\n"
    )
    assert agent_crash_signature(crash_a) == agent_crash_signature(crash_b)
    # Un paquet manquant DIFFÉRENT donne une signature différente.
    crash_c = crash_b.replace("'lmnr'", "'httpx'")
    assert agent_crash_signature(crash_c) != agent_crash_signature(crash_b)
