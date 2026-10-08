"""``merge_cycle.verify_local_sync`` sur un VRAI dépôt git temporaire (aucun double de la commande git)."""

from __future__ import annotations

import subprocess

import pytest

from collegue.pilot.merge_cycle import LocalSyncError, verify_local_sync


def _git(path, *args):
    env = {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null", "PATH": "/usr/bin:/bin"}
    out = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", *args],
        cwd=path,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    return out.stdout.strip()


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / "clone"
    path.mkdir()
    _git(path, "init", "-q", "-b", "main")
    (path / "a.txt").write_text("1")
    _git(path, "add", ".")
    _git(path, "commit", "-q", "-m", "one")
    return path


def _commit(path, name):
    (path / name).write_text(name)
    _git(path, "add", ".")
    _git(path, "commit", "-q", "-m", name)
    return _git(path, "rev-parse", "HEAD"), _git(path, "rev-parse", "HEAD^{tree}")


def test_head_on_the_merge_commit_with_the_proven_tree_is_synced(repo):
    merge_sha, tree = _commit(repo, "b.txt")
    verify_local_sync(str(repo), merge_sha, tree)


def test_head_on_the_merge_commit_with_another_tree_is_refused(repo):
    merge_sha, _ = _commit(repo, "b.txt")
    with pytest.raises(LocalSyncError, match="tree"):
        verify_local_sync(str(repo), merge_sha, "0" * 40)


def test_base_that_legitimately_advanced_still_contains_the_merge(repo):
    merge_sha, tree = _commit(repo, "b.txt")
    _commit(repo, "c.txt")
    verify_local_sync(str(repo), merge_sha, tree)


def test_clone_that_does_not_contain_the_merge_is_refused(repo):
    stale = _git(repo, "rev-parse", "HEAD")
    merge_sha, tree = _commit(repo, "b.txt")
    _git(repo, "reset", "-q", "--hard", stale)
    with pytest.raises(LocalSyncError, match="ne contient pas"):
        verify_local_sync(str(repo), merge_sha, tree)


def test_unreadable_head_is_refused(tmp_path):
    with pytest.raises(LocalSyncError):
        verify_local_sync(str(tmp_path / "absent"), "a" * 40, "b" * 40)


def test_a_managed_workspace_is_never_inspected_as_the_operator_checkout(repo, monkeypatch):
    """Le contrôle de resynchronisation ne s'exécute que sur le checkout de confiance de l'opérateur."""
    from collegue.executor import git_boundary

    merge_sha, tree = _commit(repo, "b.txt")

    def untrusted(path, *, role="source"):
        raise git_boundary.WorkspaceError(f"{role} git refusée ({path}) : workspace géré")

    monkeypatch.setattr(git_boundary, "require_trusted_checkout", untrusted)
    with pytest.raises(LocalSyncError, match="refusée"):
        verify_local_sync(str(repo), merge_sha, tree)
