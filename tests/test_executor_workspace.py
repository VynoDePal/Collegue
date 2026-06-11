"""Tests E2 (#364) : préparation du workspace git + exécution agent → diff.

Utilise git en local sur un dépôt-fixture (pas de Docker) ; la variante sandbox
réelle est couverte en ``integration``.
"""

import os
import subprocess

import pytest

from collegue.executor import (
    CommandRunner,
    FakeCodeAgent,
    IssueSpec,
    LocalCommandRunner,
    Workspace,
    WorkspaceError,
    branch_for_issue,
    prepare_workspace,
    run_issue,
)
from collegue.sandbox import DockerSandbox
from collegue.state import ProjectStateManager

ISSUE = IssueSpec(number=11, title="Faire la chose")


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _git_out(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def _make_repo(path, files=None):
    files = files or {"existing.txt": "original\n"}
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "t@example.com")
    _git(path, "config", "user.name", "Test")
    for rel, content in files.items():
        (path / rel).write_text(content)
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "init")
    return str(path)


@pytest.fixture
def repo(tmp_path):
    return _make_repo(tmp_path / "source")


# --- branch_for_issue -----------------------------------------------------------


def test_branch_for_issue():
    assert branch_for_issue(42) == "collegue/issue-42"


# --- prepare_workspace ----------------------------------------------------------


def test_prepare_workspace_clones_on_dedicated_branch(repo, tmp_path):
    base = _git_out(repo, "rev-parse", "HEAD")
    ws = prepare_workspace(repo, ISSUE, dest_root=str(tmp_path / "out"))
    assert isinstance(ws, Workspace)
    assert ws.path != repo  # clone, pas la source
    assert (tmp_path / "out" / "workspace" / ".git").exists()
    assert ws.branch == "collegue/issue-11"
    assert _git_out(ws.path, "rev-parse", "--abbrev-ref", "HEAD") == "collegue/issue-11"
    assert ws.base_commit == base
    assert (tmp_path / "out" / "workspace" / "existing.txt").read_text() == "original\n"


def test_prepare_workspace_rejects_non_git_source(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(WorkspaceError):
        prepare_workspace(str(plain), ISSUE, dest_root=str(tmp_path / "out"))


def test_prepare_workspace_rejects_missing_source(tmp_path):
    with pytest.raises(WorkspaceError):
        prepare_workspace(str(tmp_path / "nope"), ISSUE, dest_root=str(tmp_path / "out"))


# --- run_issue : capture du diff ------------------------------------------------


def test_run_issue_captures_new_file(repo, tmp_path):
    ws = prepare_workspace(repo, ISSUE, dest_root=str(tmp_path / "out"))
    result = run_issue(FakeCodeAgent(), ws, ISSUE)
    assert result.changed is True
    assert result.success is True
    assert "COLLEGUE_FAKE.txt" in result.files_changed
    assert "COLLEGUE_FAKE.txt" in result.diff
    assert "changement simulé" in result.diff


def test_run_issue_captures_modified_and_new(repo, tmp_path):
    ws = prepare_workspace(repo, ISSUE, dest_root=str(tmp_path / "out"))
    agent = FakeCodeAgent({"existing.txt": "modifié\n", "src/new.py": "x = 1\n"})
    result = run_issue(agent, ws, ISSUE)
    assert set(result.files_changed) == {"existing.txt", "src/new.py"}
    assert result.changed is True


def test_run_issue_noop_is_not_error(repo, tmp_path):
    ws = prepare_workspace(repo, ISSUE, dest_root=str(tmp_path / "out"))
    result = run_issue(FakeCodeAgent(files={}), ws, ISSUE)  # n'écrit rien
    assert result.changed is False
    assert result.success is False  # rien produit
    assert result.agent_result.success is True  # mais pas une erreur de l'agent
    assert result.files_changed == ()


def test_run_issue_agent_failure(repo, tmp_path):
    ws = prepare_workspace(repo, ISSUE, dest_root=str(tmp_path / "out"))
    result = run_issue(FakeCodeAgent(succeed=False), ws, ISSUE)
    assert result.agent_result.success is False
    assert result.changed is False
    assert result.success is False


def test_run_issue_raises_on_non_git_workspace(tmp_path):
    # Workspace pointant un dossier qui n'est pas un dépôt git : `git add` échoue
    # → WorkspaceError (plomberie cassée), pas un faux « aucun changement ».
    plain = tmp_path / "plain"
    plain.mkdir()
    ws = Workspace(path=str(plain), branch="x", base_commit="0" * 40)
    with pytest.raises(WorkspaceError):
        run_issue(FakeCodeAgent(), ws, ISSUE)


def test_run_issue_transitions_task_to_in_progress(repo, tmp_path):
    manager = ProjectStateManager.from_url(f"sqlite:///{tmp_path / 'state.db'}", create=True)
    pid = manager.create_project(name="demo")
    tid = manager.add_task(pid, title="T")  # statut par défaut : todo
    ws = prepare_workspace(repo, ISSUE, dest_root=str(tmp_path / "out"))
    run_issue(FakeCodeAgent(), ws, ISSUE, manager=manager, task_id=tid)
    task = next(t for t in manager.get_tasks(pid) if t.id == tid)
    assert task.status == "in_progress"


# --- LocalCommandRunner ---------------------------------------------------------


def test_runners_satisfy_command_runner_protocol():
    # LocalCommandRunner (CI) et DockerSandbox (integration) sont interchangeables.
    assert isinstance(LocalCommandRunner(), CommandRunner)
    assert isinstance(DockerSandbox(), CommandRunner)


def test_local_runner_success(tmp_path):
    res = LocalCommandRunner().run_command(["sh", "-c", "printf hi"], str(tmp_path))
    assert res.ok is True
    assert res.stdout == "hi"


def test_local_runner_nonzero_exit(tmp_path):
    res = LocalCommandRunner().run_command(["sh", "-c", "exit 3"], str(tmp_path))
    assert res.exit_code == 3
    assert res.ok is False


def test_local_runner_missing_binary(tmp_path):
    res = LocalCommandRunner().run_command(["collegue-no-such-bin"], str(tmp_path))
    assert res.exit_code == 127
    assert res.ok is False
    assert "introuvable" in res.stderr


def test_local_runner_caps_output(tmp_path):
    runner = LocalCommandRunner(max_output_bytes=10)
    res = runner.run_command(["sh", "-c", "printf 'a%.0s' $(seq 1 50)"], str(tmp_path))
    assert "tronquée" in res.stdout
    assert len(res.stdout.encode("utf-8")) < 60  # bien borné


# --- nettoyage des workspaces (#443) ----------------------------------------------


def test_cleanup_workspace_removes_temp_root(repo):
    from collegue.executor import IssueSpec, cleanup_workspace, prepare_workspace

    ws = prepare_workspace(repo, IssueSpec(number=7, title="T"))
    parent = os.path.dirname(ws.path)
    assert os.path.basename(parent).startswith("collegue-exec-")
    cleanup_workspace(ws)
    assert not os.path.exists(parent)  # le répertoire racine /tmp disparaît aussi


def test_cleanup_workspace_confined_when_dest_root_is_custom(repo, tmp_path):
    # ``dest_root`` fourni par l'appelant : on ne supprime QUE le clone, jamais le
    # répertoire parent qui ne nous appartient pas.
    from collegue.executor import IssueSpec, cleanup_workspace, prepare_workspace

    root = tmp_path / "mine"
    root.mkdir()
    (root / "garde.txt").write_text("à conserver", encoding="utf-8")
    ws = prepare_workspace(repo, IssueSpec(number=8, title="T"), dest_root=str(root))
    cleanup_workspace(ws)
    assert not os.path.exists(ws.path)
    assert (root / "garde.txt").exists()  # le parent n'a pas été touché


def test_cleanup_workspace_tolerates_missing_and_none():
    from collegue.executor import cleanup_workspace

    cleanup_workspace("/tmp/collegue-exec-inexistant/workspace")  # ne lève pas
    cleanup_workspace(None)
    cleanup_workspace("")


# --- GC des clones temporaires (#466) -----------------------------------------------


def test_cleanup_workspace_strips_revert_parent(tmp_path):
    from collegue.executor.workspace import cleanup_workspace

    parent = tmp_path / "collegue-revert-abc123"
    ws = parent / "workspace"
    ws.mkdir(parents=True)
    cleanup_workspace(str(ws))
    assert not parent.exists()  # le répertoire racine part avec


def test_sweep_stale_temp_clones_age_based(tmp_path):
    """#466 : seuls les répertoires PÉRIMÉS du préfixe sont supprimés — un revert
    frais (sa branche locale est le livrable) et les autres préfixes survivent."""
    import os
    import time

    from collegue.executor.workspace import sweep_stale_temp_clones

    old_dir = tmp_path / "collegue-revert-old"
    fresh_dir = tmp_path / "collegue-revert-fresh"
    other = tmp_path / "collegue-exec-old"
    for d in (old_dir, fresh_dir, other):
        d.mkdir()
    stale = time.time() - 8 * 24 * 3600
    os.utime(old_dir, (stale, stale))
    os.utime(other, (stale, stale))

    removed = sweep_stale_temp_clones(tmp_dir=str(tmp_path))
    assert removed == 1
    assert not old_dir.exists()
    assert fresh_dir.exists() and other.exists()


def test_sweep_tolerates_missing_tmp_dir(tmp_path):
    from collegue.executor.workspace import sweep_stale_temp_clones

    assert sweep_stale_temp_clones(tmp_dir=str(tmp_path / "absent")) == 0


def test_cleanup_workspace_refuses_paths_outside_perimeter(monkeypatch, tmp_path):
    """#466 (revue) : un kept_workspace corrompu en base (« / », « /home »…) ne
    doit JAMAIS devenir un rmtree récursif — hors préfixes collegue-*, seule une
    cible strictement sous le tempdir est supprimée."""
    import collegue.executor.workspace as ws_mod
    from collegue.executor.workspace import cleanup_workspace

    deleted = []
    monkeypatch.setattr(ws_mod.shutil, "rmtree", lambda p, **k: deleted.append(p))

    cleanup_workspace("/")
    cleanup_workspace("/home/quelquun")
    cleanup_workspace(str(tmp_path.parent.parent / "hors-tmp") if "/tmp" not in str(tmp_path) else "/opt/x")
    assert deleted == []  # tout refusé

    # Sous le tempdir sans préfixe connu : supprimé (comportement historique).
    import tempfile

    inside = tempfile.mkdtemp(prefix="autre-")
    cleanup_workspace(inside)
    assert deleted == [inside]


def test_sweep_skips_symlinks_and_counts_honestly(tmp_path):
    import os
    import time

    from collegue.executor.workspace import sweep_stale_temp_clones

    target = tmp_path / "cible"
    target.mkdir()
    link = tmp_path / "collegue-revert-lien"
    link.symlink_to(target)
    stale = time.time() - 8 * 24 * 3600
    os.utime(target, (stale, stale))

    assert sweep_stale_temp_clones(tmp_dir=str(tmp_path)) == 0  # lien ignoré
    assert link.exists() and target.exists()
