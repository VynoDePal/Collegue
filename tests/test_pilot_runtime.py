"""Tests F4 (#377) : câblage runtime opt-in (entrypoint + assemblage) + reporting."""

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from collegue.executor import FakeCodeAgent, FakeReviewer, PrClients
from collegue.pilot import ProjectRunResult, TaskOutcome, format_run_report, run_project_from_settings
from collegue.pilot.__main__ import build_parser
from collegue.sandbox import SandboxResult
from collegue.state import ProjectStateManager

# --- fakes (mêmes doubles que F3) -----------------------------------------------


class _Sandbox:
    def run_tests(self, workspace, command="pytest -q"):
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
    """Budget toujours OK (déterministe, sans collecteur global)."""

    def should_continue(self):
        return SimpleNamespace(action="continue", ok=True)

    def time_remaining_seconds(self):
        return None


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


def _linear(manager, n):
    pid = manager.create_project(name="demo")
    prev = None
    for i in range(n):
        prev = manager.add_task(pid, title=f"T{i}", depends_on=[prev] if prev else None)
    return pid


async def _run(manager, git_repo, pid, *, dry_run):
    return await run_project_from_settings(
        pid,
        git_repo,
        owner="o",
        repo="r",
        dry_run=dry_run,
        manager=manager,
        sandbox=_Sandbox(),
        agent=FakeCodeAgent(),
        reviewer=FakeReviewer(),
        clients=_clients(),
        budget=_Budget(),
    )


# --- assemblage + run -----------------------------------------------------------


async def test_dry_run_builds_chain_without_writes(git_repo, manager):
    pid = _linear(manager, 2)
    result = await _run(manager, git_repo, pid, dry_run=True)
    assert result.stop_reason == "completed"
    assert result.iterations == 2
    assert all(t.status == "todo" for t in manager.get_tasks(pid))  # aucune écriture
    assert manager.get_decisions(pid) == []  # pas de résumé en dry_run


async def test_real_run_records_summary_decision(git_repo, manager):
    pid = _linear(manager, 1)
    result = await _run(manager, git_repo, pid, dry_run=False)
    assert result.stop_reason == "completed"
    assert any("Run pilote" in d.summary for d in manager.get_decisions(pid))
    assert manager.get_tasks(pid)[0].status == "in_review"


# --- reporting ------------------------------------------------------------------


def test_format_run_report_contents():
    result = ProjectRunResult(
        stop_reason="completed",
        iterations=1,
        processed=[TaskOutcome(task_id=5, title="Faire X", success=True, stage="pr", pr_number=42)],
        project_status="improving",
    )
    report = format_run_report(result, project_id=7, budget=_Budget())
    assert "Arrêt : completed" in report
    assert "#5 Faire X (pr) → PR #42" in report
    assert "improving" in report
    assert "illimité" in report  # budget sans deadline


def test_format_run_report_no_prs_and_no_budget():
    result = ProjectRunResult(stop_reason="blocked", iterations=0, processed=[], project_status=None)
    report = format_run_report(result)
    assert "PRs ouvertes : (aucune)" in report
    assert "Statut projet : (inchangé)" in report
    assert "Budget-temps restant : n/a" in report


# --- CLI parser -----------------------------------------------------------------


def test_parser_defaults_dry_run():
    ns = build_parser().parse_args(["--project-id", "3", "--repo-source", "/r", "--owner", "o", "--repo", "app"])
    assert ns.project_id == 3
    assert ns.repo_source == "/r"
    assert ns.owner == "o" and ns.repo == "app"
    assert ns.base == "main"
    assert ns.execute is False  # dry_run par défaut
    assert ns.max_iterations is None


def test_parser_execute_and_overrides():
    ns = build_parser().parse_args(
        [
            "--project-id",
            "1",
            "--repo-source",
            "/r",
            "--owner",
            "o",
            "--repo",
            "a",
            "--execute",
            "--base",
            "dev",
            "--max-iterations",
            "5",
        ]
    )
    assert ns.execute is True
    assert ns.base == "dev"
    assert ns.max_iterations == 5


def test_parser_requires_mandatory_args():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--owner", "o"])  # manque project-id/repo-source/repo


# --- CLI main (glue + codes de sortie) ------------------------------------------

_CLI_ARGS = ["--project-id", "1", "--repo-source", "/r", "--owner", "o", "--repo", "a"]


def _patch_run(monkeypatch, result):
    import collegue.pilot.__main__ as cli

    async def _fake(*args, **kwargs):
        return result

    monkeypatch.setattr(cli, "run_project_from_settings", _fake)
    return cli


def test_main_returns_0_on_completed(monkeypatch, capsys):
    cli = _patch_run(
        monkeypatch,
        ProjectRunResult(stop_reason="completed", iterations=1, processed=[], project_status="improving"),
    )
    assert cli.main(_CLI_ARGS) == 0
    assert "Rapport du pilote" in capsys.readouterr().out


def test_main_returns_1_on_blocked(monkeypatch):
    cli = _patch_run(monkeypatch, ProjectRunResult(stop_reason="blocked", iterations=0, processed=[]))
    assert cli.main(_CLI_ARGS) == 1


# --- isolation ------------------------------------------------------------------


def test_app_does_not_wire_pilot():
    # Le serveur ne câble pas le pilote : il s'invoque via `python -m collegue.pilot`.
    app_src = (Path(__file__).resolve().parent.parent / "collegue" / "app.py").read_text(encoding="utf-8")
    assert "collegue.pilot" not in app_src
    assert "from collegue.pilot" not in app_src


def test_importing_pilot_does_not_pull_openhands_runtime():
    import collegue.pilot  # noqa: F401

    assert not any(name == "openhands" or name.startswith("openhands.") for name in sys.modules)
