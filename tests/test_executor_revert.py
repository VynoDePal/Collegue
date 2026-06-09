"""Tests H1 (#392) : primitive de revert git (revert_commit + prepare_revert).

Vrai ``git`` sur un dépôt fixture (pas de Docker, pas de réseau) — comme
``test_executor_workspace``. Vérifie l'annulation effective, le fail-closed sur SHA
inconnu (abort → workspace propre), et la pureté de l'aperçu de PR.
"""

import subprocess
from pathlib import Path

import pytest

from collegue.executor.revert import RevertError, prepare_revert, revert_commit, revert_pr_preview


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _head(cwd):
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=cwd, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def source_repo(tmp_path):
    """Dépôt source : 2 commits (v1 puis v2) ; renvoie (chemin, sha_du_commit_v2)."""
    src = tmp_path / "source"
    src.mkdir()
    _git(src, "init", "-q")
    _git(src, "config", "user.email", "t@example.com")
    _git(src, "config", "user.name", "Test")
    (src / "file.txt").write_text("v1\n")
    _git(src, "add", "-A")
    _git(src, "commit", "-q", "-m", "v1")
    (src / "file.txt").write_text("v2\n")
    _git(src, "add", "-A")
    _git(src, "commit", "-q", "-m", "v2")
    return str(src), _head(src)


def _clone_with_identity(src, dest):
    _git(dest.parent, "clone", "--quiet", src, str(dest))
    _git(dest, "config", "user.email", "bot@example.com")
    _git(dest, "config", "user.name", "Bot")


def test_revert_commit_undoes_change(tmp_path, source_repo):
    src, sha2 = source_repo
    dest = tmp_path / "clone"
    _clone_with_identity(src, dest)

    res = revert_commit(str(dest), sha2)
    assert res.reverted and res.revert_sha
    assert (dest / "file.txt").read_text() == "v1\n"  # contenu annulé
    head = _head(dest)
    assert head == res.revert_sha and head != sha2  # nouveau commit (le revert)


def test_revert_commit_works_without_preset_identity(tmp_path, source_repo):
    # Clone SANS config user.* : l'identité ``-c`` du bot doit permettre le commit de revert.
    src, sha2 = source_repo
    dest = tmp_path / "clone"
    _git(dest.parent, "clone", "--quiet", src, str(dest))
    res = revert_commit(str(dest), sha2)
    assert res.reverted and res.revert_sha
    assert (dest / "file.txt").read_text() == "v1\n"


def test_revert_commit_invalid_sha():
    with pytest.raises(RevertError):
        revert_commit("/tmp", "not-a-sha")


def test_revert_commit_unknown_sha_fails_closed(tmp_path, source_repo):
    src, _ = source_repo
    dest = tmp_path / "clone"
    _clone_with_identity(src, dest)
    # SHA bien formé mais inexistant → git revert échoue → reverted False, workspace propre.
    res = revert_commit(str(dest), "0123456789abcdef0123456789abcdef01234567")
    assert not res.reverted
    status = subprocess.run(["git", "status", "--porcelain"], cwd=dest, capture_output=True, text=True).stdout
    assert status.strip() == ""  # l'abort a nettoyé


def test_prepare_revert_clones_branches_and_reverts(source_repo):
    src, sha2 = source_repo
    res = prepare_revert(src, sha2)
    assert res.reverted
    assert res.branch and res.branch.startswith("collegue/revert-")
    assert (Path(res.workspace) / "file.txt").read_text() == "v1\n"


def test_prepare_revert_rejects_non_repo(tmp_path):
    with pytest.raises(RevertError):
        prepare_revert(str(tmp_path / "nope"), "0123456789abcdef")


def test_revert_pr_preview_is_pure():
    out = revert_pr_preview("0123456789abcdef", base="main", reason="tests rouges")
    assert "0123456789ab" in out["title"]
    assert "tests rouges" in out["body"]
    assert "§6" in out["body"]
    with pytest.raises(RevertError):
        revert_pr_preview("nope")
