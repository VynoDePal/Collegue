"""A4 — validation end-to-end **par le produit** : handoff plan → run (sans harnais).

Prouve que les entrypoints committés s'enchaînent via l'état durable partagé
(``project_id``) : ``plan_project_from_settings`` crée projet + SPEC + tâches en DB,
puis ``run_project_from_settings`` les reprend et les amène en ``in_review`` — le
tout sans aucun script de ``scripts/facnor_run/``. Les appels LLM (planner) et
l'infra (sandbox/agent/clients GitHub) sont des doubles ; le run RÉEL de bout en
bout (Docker + OpenHands + LLM) est derrière le marqueur ``integration`` et dépend
de l'image sandbox OpenHands (#404, Phase D).
"""

from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest

from collegue.executor import FakeCodeAgent, FakeReviewer, PrClients
from collegue.pilot import plan_project_from_settings, run_project_from_settings
from collegue.planner.spec_generator import Spec
from collegue.sandbox import SandboxResult
from collegue.state import ProjectStateManager

# --- doubles infra (alignés sur test_pilot_runtime) ----------------------------


class _Sandbox:
    def __init__(self):
        self.commands = []

    def run_tests(self, workspace, command="pytest -q"):
        self.commands.append(command)
        return SandboxResult(exit_code=0, stdout="ok", stderr="")


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


class _Budget:
    def should_continue(self):
        return SimpleNamespace(action="continue", ok=True)

    def time_remaining_seconds(self):
        return None


class _Ctx:
    """ctx de sampling injecté (les doubles planner/reviewer ne l'appellent pas)."""

    async def aclose(self):
        return None


class _QACtx(_Ctx):
    """Sampling QA déterministe ; ne voit que le SPEC/DAG transmis au plan-time."""

    def __init__(self):
        self.calls = []

    async def sample(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            text=(
                "from pathlib import Path\n\n"
                "def test_contract():\n"
                "    workspace = Path('/workspace')\n"
                "    assert workspace.is_dir()\n"
            )
        )


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


@pytest.fixture
def git_repo(tmp_path):
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


# --- doubles planner (le LLM est hors-scope ici ; la persistance DB est RÉELLE) ---


def _patch_planner_llm(monkeypatch):
    async def _generate(problem, ctx, **kw):
        return Spec(title="E2E", summary=problem, acceptance_criteria=["AC1"])

    async def _decompose(spec, ctx, *, manager, project_id, **kw):
        # Écrit de VRAIES tâches en DB (comme le ferait le décomposeur) → testent le handoff.
        a = manager.add_task(project_id, title="A", acceptance="critère A")
        manager.add_task(project_id, title="B", acceptance="critère B", depends_on=[a])
        return manager.get_tasks(project_id)

    monkeypatch.setattr("collegue.planner.spec_generator.generate_spec", _generate)
    monkeypatch.setattr("collegue.planner.decomposer.decompose", _decompose)


# --- handoff plan → run --------------------------------------------------------


async def test_plan_then_run_handoff_via_product(monkeypatch, manager, git_repo):
    _patch_planner_llm(monkeypatch)

    # 1) PLAN (sync en dry-run → pas de GitHub) : crée projet + SPEC + tâches en DB.
    plan = await plan_project_from_settings(
        "E2E",
        "construire une app X",
        owner="o",
        repo="r",
        settings_obj=SimpleNamespace(),
        manager=manager,
        ctx=_Ctx(),
        approve=True,
        execute_sync=False,
    )
    assert plan.task_count == 2
    project = manager.get_project(plan.project_id)
    assert project.spec and "E2E" in project.spec  # SPEC persisté (markdown)
    assert all(t.status == "todo" for t in manager.get_tasks(plan.project_id))

    # 2) RUN (doubles infra) sur le MÊME project_id : les tâches passent in_review.
    # On ISOLE le handoff du merge-bot (BUILD_AUTO_MERGE=False) — le merge-bot a ses
    # tests dédiés (test_pilot_runtime) et les doubles ici ne gèrent pas le merge.
    result = await run_project_from_settings(
        plan.project_id,
        git_repo,
        owner="o",
        repo="r",
        dry_run=False,
        settings_obj=SimpleNamespace(BUILD_AUTO_MERGE=False),
        manager=manager,
        sandbox=_Sandbox(),
        agent=FakeCodeAgent(),
        reviewer=FakeReviewer(),
        clients=_clients(),
        budget=_Budget(),
        ctx=_Ctx(),
    )
    # #580 : sans merge-bot, des PR ouvertes ne constituent pas encore un MVP
    # intégré. Le runtime produit expose donc explicitement l'attente de merge.
    assert result.stop_reason == "awaiting_merge"
    assert result.iterations == 2
    # Le handoff fonctionne : le run a repris les tâches planifiées et les a construites.
    assert all(t.status == "in_review" for t in manager.get_tasks(plan.project_id))
    assert any("Run pilote" in d.summary for d in manager.get_decisions(plan.project_id))


async def test_plan_dry_run_sync_writes_nothing_to_github(monkeypatch, manager):
    # En dry-run de sync, la planification persiste en DB mais ne touche pas GitHub.
    _patch_planner_llm(monkeypatch)
    plan = await plan_project_from_settings(
        "E2E", "x", owner="o", repo="r", settings_obj=SimpleNamespace(), manager=manager, ctx=_Ctx()
    )
    assert plan.dry_run is True
    assert plan.task_count == 2
    # issues en aperçu (pas de numéro réel attribué).
    assert all(i.get("issue_number") is None for i in plan.issues)


async def test_plan_time_oracle_is_persisted_approved_and_replayed_without_llm(monkeypatch, manager, git_repo):
    """Tranche §4.7 complète : QA au plan → hash → gate stocké au run."""
    _patch_planner_llm(monkeypatch)
    qa_ctx = _QACtx()

    plan = await plan_project_from_settings(
        "E2E-QA",
        "construire une app testable",
        owner="o",
        repo="r",
        settings_obj=SimpleNamespace(
            GATE_ACCEPTANCE_TESTS=True,
            LLM_PROVIDER="test",
            LLM_MODEL="qa-fixture",
        ),
        manager=manager,
        ctx=qa_ctx,
        approve=True,
    )

    project = manager.get_project(plan.project_id)
    tasks = manager.get_tasks(plan.project_id)
    assert project.acceptance_tests_required is True
    assert len(qa_ctx.calls) == len(tasks) == 2
    assert all(task.acceptance_test_source for task in tasks)
    assert all(task.acceptance_test_sha256 for task in tasks)
    assert all(task.acceptance_test_provenance["role"] == "qa" for task in tasks)
    assert "oracle QA" in plan.preview_markdown

    from collegue.executor.quality_gate import StoredAcceptanceChecker
    from collegue.pilot.driver import _issue_from_task

    preflight = await StoredAcceptanceChecker(manager=manager, project_id=plan.project_id).check(
        git_repo,
        "diff ignoré",
        _issue_from_task(tasks[0], {task.id: task for task in tasks}),
        _Ctx(),
        sandbox=_Sandbox(),
    )
    assert preflight.passed is True, preflight.error

    # Le flag env est volontairement OFF au run : la politique persistée du plan
    # doit tout de même imposer le checker stocké, sans aucun nouveau sampling.
    sandbox = _Sandbox()
    result = await run_project_from_settings(
        plan.project_id,
        git_repo,
        owner="o",
        repo="r",
        dry_run=False,
        settings_obj=SimpleNamespace(
            BUILD_AUTO_MERGE=False,
            GATE_ACCEPTANCE_TESTS=False,
            TASK_MAX_ATTEMPTS=1,
            TASK_RETRY_BACKOFF_SECONDS=0,
        ),
        manager=manager,
        sandbox=sandbox,
        agent=FakeCodeAgent(),
        reviewer=FakeReviewer(),
        clients=_clients(),
        budget=_Budget(),
        ctx=_Ctx(),
    )

    # Sans merge-bot, les oracles sont bien rejoués mais les PR BUILD restent
    # ouvertes : #580 interdit donc de déclarer le MVP intégré.
    assert result.stop_reason == "awaiting_merge", [task.last_error for task in manager.get_tasks(plan.project_id)]
    acceptance_commands = [command for command in sandbox.commands if "python -I -c" in command]
    assert len(acceptance_commands) == 2
