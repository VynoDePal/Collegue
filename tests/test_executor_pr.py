"""Tests E4 (#366) : ouverture de PR (dry_run par défaut), clients GitHub mockés."""

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from collegue.executor import (
    IssueSpec,
    PrClients,
    QualityReport,
    Workspace,
    build_pr_body,
    exec_marker,
    open_pr,
)
from collegue.state import ProjectStateManager
from collegue.tools.base import ToolExecutionError
from collegue.tools.github_commands import FileCommands

ISSUE = IssueSpec(number=5, title="Ajouter le endpoint")
REPORT = QualityReport(
    tests_passed=True,
    test_exit_code=0,
    test_output="2 passed",
    review_summary="RAS",
    review_findings=(),
    review_blocking=False,
    passed=True,
)


class _FakeBranches:
    def __init__(self):
        self.created = []

    def ensure_branch(self, owner, repo, branch, from_branch=None):
        self.created.append((branch, from_branch))
        return SimpleNamespace(name=branch)


class _FakeFiles:
    def __init__(self):
        self.updated = []
        self.deleted = []

    def update_file(self, owner, repo, path, message, content, branch=None):
        self.updated.append((path, content, branch))
        return {}

    def delete_file(self, owner, repo, path, message, branch=None):
        self.deleted.append((path, branch))
        return {}


class _FakePRs:
    def __init__(self, existing=None, number=101):
        self._existing = existing  # PRInfo-like ou None
        self._number = number
        self.created = []

    def find_pr_by_head(self, owner, repo, head, base=None, state="open"):
        if self._existing is not None and getattr(self._existing, "head_branch", None) == head:
            return self._existing
        return None

    def create_pr(self, owner, repo, title, head, base, body):
        self.created.append({"title": title, "head": head, "base": base, "body": body})
        return SimpleNamespace(number=self._number, html_url=f"https://gh/pull/{self._number}", head_branch=head)


def _clients(existing=None):
    return PrClients(branches=_FakeBranches(), files=_FakeFiles(), prs=_FakePRs(existing=existing))


def _workspace(tmp_path, files=None):
    ws_dir = tmp_path / "ws"
    ws_dir.mkdir()
    for rel, content in (files or {"a.py": "print('a')\n"}).items():
        path = ws_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return Workspace(path=str(ws_dir), branch="collegue/issue-5", base_commit="basesha123")


# --- dry-run --------------------------------------------------------------------


def test_dry_run_writes_nothing(tmp_path):
    ws = _workspace(tmp_path)
    clients = _clients()
    result = open_pr(ws, REPORT, ISSUE, "o", "r", files_changed=("a.py",), clients=clients, dry_run=True)
    assert result.dry_run is True
    assert result.head == "collegue/issue-5"
    assert "Closes #5" in result.body
    assert exec_marker(5) in result.body
    assert clients.branches.created == []
    assert clients.files.updated == []
    assert clients.prs.created == []


# --- écriture réelle ------------------------------------------------------------


def test_real_creates_branch_files_and_pr(tmp_path):
    ws = _workspace(tmp_path, {"a.py": "print('a')\n", "sub/b.py": "x = 1\n"})
    clients = _clients()
    result = open_pr(
        ws, REPORT, ISSUE, "o", "r", files_changed=("a.py", "sub/b.py"), base="main", clients=clients, dry_run=False
    )
    assert result.dry_run is False
    assert result.skipped is False
    assert result.number == 101
    assert result.html_url == "https://gh/pull/101"
    # branche dédiée créée depuis base
    assert clients.branches.created == [("collegue/issue-5", "main")]
    # fichiers committés avec leur contenu
    assert {p for p, _c, _b in clients.files.updated} == {"a.py", "sub/b.py"}
    assert all(b == "collegue/issue-5" for _p, _c, b in clients.files.updated)
    # une seule PR, corps complet
    assert len(clients.prs.created) == 1
    body = clients.prs.created[0]["body"]
    assert "Closes #5" in body and "## Gate qualité" in body and exec_marker(5) in body


def test_idempotent_when_pr_already_open(tmp_path):
    ws = _workspace(tmp_path)
    existing = SimpleNamespace(number=77, html_url="https://gh/pull/77", head_branch="collegue/issue-5")
    clients = _clients(existing=existing)
    result = open_pr(ws, REPORT, ISSUE, "o", "r", files_changed=("a.py",), clients=clients, dry_run=False)
    assert result.skipped is True
    assert result.number == 77
    # rien recréé
    assert clients.branches.created == []
    assert clients.files.updated == []
    assert clients.prs.created == []


def test_deleted_file_is_removed_on_branch(tmp_path):
    ws = _workspace(tmp_path, {"keep.py": "k\n"})  # "gone.py" n'existe pas sur disque
    clients = _clients()
    open_pr(ws, REPORT, ISSUE, "o", "r", files_changed=("keep.py", "gone.py"), clients=clients, dry_run=False)
    assert {p for p, _c, _b in clients.files.updated} == {"keep.py"}
    assert [p for p, _b in clients.files.deleted] == ["gone.py"]


def test_persists_pr_number_to_decision_journal(tmp_path):
    manager = ProjectStateManager.from_url(f"sqlite:///{tmp_path / 'state.db'}", create=True)
    pid = manager.create_project(name="demo")
    ws = _workspace(tmp_path)
    open_pr(
        ws,
        REPORT,
        ISSUE,
        "o",
        "r",
        files_changed=("a.py",),
        clients=_clients(),
        dry_run=False,
        manager=manager,
        project_id=pid,
    )
    decisions = manager.get_decisions(pid)
    assert any("PR #101" in d.summary for d in decisions)


def test_path_traversal_rejected(tmp_path):
    ws = _workspace(tmp_path)
    with pytest.raises(ValueError):
        open_pr(ws, REPORT, ISSUE, "o", "r", files_changed=("../evil.py",), clients=_clients(), dry_run=False)


def test_dot_and_empty_segments_rejected(tmp_path):
    ws = _workspace(tmp_path)
    for bad in ("a/./b.py", "a//b.py"):
        with pytest.raises(ValueError):
            open_pr(ws, REPORT, ISSUE, "o", "r", files_changed=(bad,), clients=_clients(), dry_run=False)


def test_symlink_escape_is_rejected_not_exfiltrated(tmp_path):
    # Un agent non fiable crée un symlink pointant un secret hôte hors workspace.
    # open_pr doit REFUSER (ValueError) et ne jamais lire/pousser le secret.
    secret = tmp_path / "host_secret.txt"
    secret.write_text("HOST PRIVATE KEY", encoding="utf-8")
    ws = _workspace(tmp_path, {"real.py": "x = 1\n"})
    os.symlink(secret, Path(ws.path) / "evil.py")
    clients = _clients()
    with pytest.raises(ValueError):
        open_pr(ws, REPORT, ISSUE, "o", "r", files_changed=("evil.py",), clients=clients, dry_run=False)
    # le contenu du secret n'a JAMAIS été poussé
    assert all("HOST PRIVATE KEY" not in content for _p, content, _b in clients.files.updated)


def test_title_is_inlined(tmp_path):
    ws = _workspace(tmp_path)
    issue = IssueSpec(number=5, title="Multi\nligne\ttitre")
    result = open_pr(ws, REPORT, issue, "o", "r", clients=_clients(), dry_run=True)
    assert "\n" not in result.title
    assert result.title == "Multi ligne titre (issue #5)"


# --- build_pr_body --------------------------------------------------------------


def test_build_pr_body_structure():
    body = build_pr_body(REPORT, ISSUE)
    assert body.startswith("## Exécution automatique de l'issue #5")
    assert "Closes #5" in body
    assert exec_marker(5) in body


def test_build_pr_body_closes_issue_flag():
    # Défaut : ferme l'issue (comportement E5 inchangé).
    assert "Closes #5" in build_pr_body(REPORT, ISSUE, closes_issue=True)
    # closes_issue=False : pas de « Closes » (numéro ≠ vraie issue, ex. round G4),
    # mais le marqueur + le rapport restent présents.
    body = build_pr_body(REPORT, ISSUE, closes_issue=False)
    assert "Closes #" not in body
    assert exec_marker(5) in body
    assert "## Gate qualité" in body


# --- FileCommands.delete_file (ajout E4) ----------------------------------------


def test_file_commands_delete_skips_when_absent():
    fc = FileCommands(token="x")
    fc.get_file_content = lambda *a, **k: (_ for _ in ()).throw(ToolExecutionError("404"))
    assert fc.delete_file("o", "r", "p", "msg", branch="b") is None


def test_file_commands_delete_calls_api_with_sha():
    fc = FileCommands(token="x")
    fc.get_file_content = lambda *a, **k: {"sha": "BLOBSHA"}
    captured = {}

    def _fake_request(method, endpoint, json_data=None):
        captured.update(method=method, endpoint=endpoint, data=json_data)
        return {"commit": {"sha": "c1"}}

    fc._request_json = _fake_request
    res = fc.delete_file("o", "r", "path/x.py", "msg", branch="b")
    assert captured["method"] == "DELETE"
    assert captured["data"]["sha"] == "BLOBSHA"
    assert captured["data"]["branch"] == "b"
    assert res["commit"] == {"sha": "c1"}
