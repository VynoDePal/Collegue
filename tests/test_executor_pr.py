"""Tests E4 (#366) : ouverture de PR (dry_run par défaut), clients GitHub mockés."""

import hashlib
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
from collegue.executor.pr import (
    DELIVERY_DELETE,
    DELIVERY_SKIP_BINARY,
    DELIVERY_SKIP_SYMLINK,
    DELIVERY_UPDATE,
    DeliveryDriftError,
    DeliveryFile,
    DeliveryRefusedError,
    DeliverySnapshot,
    capture_delivery_snapshot,
    diff_sha256_marker,
    verify_delivery_snapshot,
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


BENCH_ISSUE = IssueSpec(number=5, title="Ajouter le endpoint")


@pytest.fixture
def bench(tmp_path):
    """Banc de publication RÉEL : dépôt source Git, workspace géré, distant Git fidèle, état SQLite."""
    from github_fakes import FakeRemote, make_source_repo

    from collegue.executor.workspace import prepare_workspace

    source = make_source_repo(
        tmp_path / "source", {"keep.py": "kept\n", "gone.py": "to delete\n", "README.md": "# base\n"}
    )
    workspace = prepare_workspace(source, BENCH_ISSUE, dest_root=str(tmp_path / "ws"))
    manager = ProjectStateManager.from_url(f"sqlite:///{tmp_path / 'state.db'}", create=True)
    return SimpleNamespace(
        workspace=workspace,
        remote=FakeRemote(tmp_path, source),
        manager=manager,
        project_id=manager.create_project(name="demo"),
        root=Path(workspace.path),
    )


def sealed_draft(bench):
    """Fige le contenu testé tel qu'il est maintenant (comme le pipeline avant ses contrôles) + verdicts verts."""
    from collegue.executor.delivery_proof import MANDATORY_VERDICTS, PHASE_BUILD, ProofDraft, seal_tested_content

    content = seal_tested_content(bench.workspace.path)
    draft = ProofDraft(phase=PHASE_BUILD, content=content)
    for name in MANDATORY_VERDICTS[PHASE_BUILD]:
        draft.add(name, True, "ok")
    return draft


def publish(bench, snapshot=None, *, files_changed=(), clients=None, draft=None):
    return open_pr(
        bench.workspace,
        REPORT,
        BENCH_ISSUE,
        "o",
        "r",
        files_changed=files_changed,
        snapshot=snapshot,
        clients=clients or bench.remote.clients(),
        dry_run=False,
        manager=bench.manager,
        project_id=bench.project_id,
        draft=draft or sealed_draft(bench),
    )


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


# --- snapshot immuable / anti-TOCTOU -------------------------------------------


def test_delivery_snapshot_captures_all_operations_and_diff_hash(tmp_path):
    ws_dir = tmp_path / "ws"
    ws_dir.mkdir()
    (ws_dir / "code.py").write_text("print('validé')\n", encoding="utf-8")
    binary = b"\x89PNG\r\n\x1a\n\xff"
    (ws_dir / "logo.png").write_bytes(binary)
    os.symlink("code.py", ws_dir / "alias.py")
    ws = Workspace(path=str(ws_dir), branch="collegue/issue-5", base_commit="base")
    diff = "diff --git a/code.py b/code.py\n+print('validé')\n"

    snapshot = capture_delivery_snapshot(
        ws,
        ("code.py", "gone.py", "logo.png", "alias.py"),
        diff=diff,
    )

    assert isinstance(snapshot, DeliverySnapshot)
    assert all(isinstance(item, DeliveryFile) for item in snapshot.files)
    assert [item.operation for item in snapshot.files] == [
        DELIVERY_UPDATE,
        DELIVERY_DELETE,
        DELIVERY_SKIP_BINARY,
        DELIVERY_SKIP_SYMLINK,
    ]
    assert snapshot.files[0].content == "print('validé')\n"
    assert snapshot.files[2].source_sha256 == hashlib.sha256(binary).hexdigest()
    assert snapshot.diff_sha256 == hashlib.sha256(diff.encode("utf-8")).hexdigest()
    assert snapshot.skipped_binaries == ("logo.png",)
    assert snapshot.skipped_symlinks == ("alias.py",)


def test_verify_delivery_snapshot_detects_live_drift(tmp_path):
    ws = _workspace(tmp_path, {"a.py": "validated\n"})
    snapshot = capture_delivery_snapshot(ws, ("a.py",), diff="validated diff")

    assert verify_delivery_snapshot(ws, snapshot) is None
    Path(ws.path, "a.py").write_text("mutated after gate\n", encoding="utf-8")

    with pytest.raises(DeliveryDriftError, match="a.py"):
        verify_delivery_snapshot(ws, snapshot)


def test_verify_delivery_snapshot_allows_only_explicitly_ignored_drift(tmp_path):
    ws = _workspace(tmp_path, {"app.py": "stable\n", "requirements.txt": "fastapi\n"})
    snapshot = capture_delivery_snapshot(ws, ("app.py", "requirements.txt"))
    Path(ws.path, "requirements.txt").write_text("fastapi\nhttpx\n", encoding="utf-8")

    assert verify_delivery_snapshot(ws, snapshot, ignored_paths=("requirements.txt",)) is None
    with pytest.raises(DeliveryDriftError, match="requirements.txt"):
        verify_delivery_snapshot(ws, snapshot)

    Path(ws.path, "app.py").write_text("unexpected drift\n", encoding="utf-8")
    with pytest.raises(DeliveryDriftError, match="app.py"):
        verify_delivery_snapshot(ws, snapshot, ignored_paths=("requirements.txt",))


def test_verify_delivery_snapshot_fingerprints_skipped_binary_and_symlink(tmp_path):
    ws_dir = tmp_path / "ws"
    ws_dir.mkdir()
    (ws_dir / "asset.bin").write_bytes(b"\xffold")
    os.symlink("first-target", ws_dir / "alias")
    ws = Workspace(path=str(ws_dir), branch="collegue/issue-5", base_commit="base")
    snapshot = capture_delivery_snapshot(ws, ("asset.bin", "alias"))

    (ws_dir / "asset.bin").write_bytes(b"\xffnew")
    with pytest.raises(DeliveryDriftError, match="asset.bin"):
        verify_delivery_snapshot(ws, snapshot)

    (ws_dir / "asset.bin").write_bytes(b"\xffold")
    (ws_dir / "alias").unlink()
    os.symlink("second-target", ws_dir / "alias")
    with pytest.raises(DeliveryDriftError, match="alias"):
        verify_delivery_snapshot(ws, snapshot)


def test_open_pr_with_snapshot_pushes_frozen_payload_without_reading_live_file(bench):
    (bench.root / "a.py").write_text("validated bytes\n")
    snapshot = capture_delivery_snapshot(bench.workspace, ("a.py",), diff="the reviewed diff")
    draft = sealed_draft(bench)  # contenu testé figé AVANT la mutation
    (bench.root / "a.py").write_text("unvalidated mutation\n")

    result = publish(bench, snapshot, draft=draft)

    assert result.number == 101 and result.proof.passed is True
    assert (
        bench.remote.files_at("collegue/issue-5")["a.py"] == "validated bytes\n"
    )  # octets figés, pas le fichier vivant
    assert bench.remote.tree_of(result.head_sha) == draft.content.tree_sha == result.proof.tree_sha
    body = bench.remote.prs[101].body
    assert diff_sha256_marker(snapshot.diff_sha256) in body


def test_open_pr_snapshot_preserves_frozen_deletion_when_file_reappears(bench):
    (bench.root / "gone.py").unlink()
    snapshot = capture_delivery_snapshot(bench.workspace, ("gone.py",), diff="delete gone.py")
    draft = sealed_draft(bench)
    (bench.root / "gone.py").write_text("late content\n")

    result = publish(bench, snapshot, draft=draft)

    published = bench.remote.files_at(result.head)
    assert "gone.py" not in published and published["keep.py"] == "kept\n"
    assert bench.remote.tree_of(result.head_sha) == draft.content.tree_sha


def test_historical_open_pr_captures_before_network_side_effects(bench):
    (bench.root / "a.py").write_text("captured first\n")
    draft = sealed_draft(bench)
    clients = bench.remote.clients()
    ensure = clients.branches.ensure_branch

    def mutating_ensure(owner, repo, branch, from_branch=None):
        (bench.root / "a.py").write_text("changed during network call\n")
        return ensure(owner, repo, branch, from_branch=from_branch)

    clients.branches.ensure_branch = mutating_ensure
    result = publish(bench, files_changed=("a.py",), clients=clients, draft=draft)

    assert bench.remote.files_at(result.head)["a.py"] == "captured first\n"
    assert bench.remote.tree_of(result.head_sha) == draft.content.tree_sha


def test_open_pr_rejects_files_changed_that_disagree_with_snapshot(tmp_path):
    ws = _workspace(tmp_path, {"a.py": "a\n", "b.py": "b\n"})
    snapshot = capture_delivery_snapshot(ws, ("a.py",))

    with pytest.raises(ValueError, match="ne correspond pas"):
        open_pr(
            ws,
            REPORT,
            ISSUE,
            "o",
            "r",
            files_changed=("b.py",),
            snapshot=snapshot,
            clients=_clients(),
            dry_run=False,
        )


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


def test_real_creates_branch_files_and_pr(bench):
    (bench.root / "a.py").write_text("print('a')\n")
    (bench.root / "sub").mkdir()
    (bench.root / "sub" / "b.py").write_text("x = 1\n")
    clients = bench.remote.clients()
    result = open_pr(
        bench.workspace,
        REPORT,
        BENCH_ISSUE,
        "o",
        "r",
        files_changed=("a.py", "sub/b.py"),
        base="main",
        clients=clients,
        dry_run=False,
        manager=bench.manager,
        project_id=bench.project_id,
        draft=sealed_draft(bench),
    )
    assert result.dry_run is False and result.skipped is False and result.number == 101
    assert result.html_url == "https://example.invalid/pull/101"
    assert clients.branches.created == ["collegue/issue-5"]  # branche dédiée créée depuis la base
    assert set(clients.files.updated) == {"a.py", "sub/b.py"}
    assert len(clients.prs.created) == 1
    body = clients.prs.created[0]["body"]
    assert "Closes #5" in body and "## Gate qualité" in body and exec_marker(5) in body
    assert bench.remote.files_at(result.head)["sub/b.py"] == "x = 1\n"


def test_binary_file_is_refused_not_silently_omitted(tmp_path):
    """Vague 3 (était #410 : binaire SAUTÉ) : la Contents API ne pousse que du texte UTF-8. Omettre le binaire faisait
    annoncer un candidat COMPLET alors que la livraison ne le contient pas → refus explicite AVANT toute écriture."""
    ws_dir = tmp_path / "ws"
    ws_dir.mkdir()
    (ws_dir / "code.py").write_text("print('ok')\n", encoding="utf-8")
    (ws_dir / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\xcbbinaire")  # octets non-UTF-8
    ws = Workspace(path=str(ws_dir), branch="collegue/issue-5", base_commit="basesha123")
    clients = _clients()
    with pytest.raises(DeliveryRefusedError, match="logo.png"):
        open_pr(ws, REPORT, ISSUE, "o", "r", files_changed=("code.py", "logo.png"), clients=clients, dry_run=False)
    assert clients.branches.created == [] and clients.files.updated == [] and clients.prs.created == []
    # l'aperçu (dry-run) reste informatif : il nomme ce que la livraison refuserait
    preview = open_pr(ws, REPORT, ISSUE, "o", "r", files_changed=("code.py", "logo.png"), clients=clients, dry_run=True)
    assert preview.skipped_binaries == ("logo.png",) and "logo.png" in preview.body


def test_idempotent_when_pr_already_open_with_the_same_content(bench):
    (bench.root / "a.py").write_text("print('a')\n")
    draft = sealed_draft(bench)
    first = publish(bench, files_changed=("a.py",), draft=draft)
    clients = bench.remote.clients()
    again = publish(bench, files_changed=("a.py",), clients=clients, draft=draft)
    assert again.skipped is True and again.number == first.number
    assert clients.files.updated == [] and clients.prs.created == []  # rien recréé
    assert again.proof.proof_id == first.proof.proof_id  # la preuve immuable de la première publication fait foi


def test_an_open_pr_of_another_revision_is_never_reported_as_the_delivery(bench):
    clients = bench.remote.clients()
    clients.branches.ensure_branch("o", "r", "collegue/issue-5", from_branch="main")
    clients.files.update_file("o", "r", "other.py", "m", "pas la livraison\n", branch="collegue/issue-5")
    clients.prs.create_pr("o", "r", "ancienne", "collegue/issue-5", "main", "ancienne")
    (bench.root / "a.py").write_text("print('a')\n")
    with pytest.raises(DeliveryDriftError, match="arbre publié"):
        publish(bench, files_changed=("a.py",))
    assert bench.manager.get_decision_journal(bench.project_id, "delivery-proof") == []


def test_deleted_file_is_removed_on_branch(bench):
    (bench.root / "gone.py").unlink()
    (bench.root / "new.py").write_text("n\n")
    clients = bench.remote.clients()
    result = publish(bench, files_changed=("new.py", "gone.py"), clients=clients)
    assert clients.files.updated == ["new.py"] and clients.files.deleted == ["gone.py"]
    assert "gone.py" not in bench.remote.files_at(result.head)


def test_persists_pr_number_and_the_proof_to_the_decision_journal(bench):
    (bench.root / "a.py").write_text("print('a')\n")
    result = publish(bench, files_changed=("a.py",))
    decisions = bench.manager.get_decisions(bench.project_id)
    assert any("PR #101" in d.summary for d in decisions)
    assert any(d.summary.startswith("delivery-proof:v1:") for d in decisions)
    assert result.proof.pr_number == 101


def test_real_publication_without_a_proof_draft_a_manager_or_a_passing_draft_is_refused(bench):
    (bench.root / "a.py").write_text("print('a')\n")
    clients = bench.remote.clients()
    draft = sealed_draft(bench)
    common = dict(files_changed=("a.py",), clients=clients, dry_run=False)
    with pytest.raises(DeliveryRefusedError, match="aucun contenu testé"):
        open_pr(
            bench.workspace, REPORT, BENCH_ISSUE, "o", "r", manager=bench.manager, project_id=bench.project_id, **common
        )
    with pytest.raises(DeliveryRefusedError, match="manager et project_id requis"):
        open_pr(bench.workspace, REPORT, BENCH_ISSUE, "o", "r", draft=draft, **common)
    draft.add("tests", False, "rouge")
    with pytest.raises(DeliveryRefusedError, match="tests"):
        open_pr(
            bench.workspace,
            REPORT,
            BENCH_ISSUE,
            "o",
            "r",
            manager=bench.manager,
            project_id=bench.project_id,
            draft=draft,
            **common,
        )
    assert bench.remote.calls == []


def test_path_traversal_rejected(tmp_path):
    ws = _workspace(tmp_path)
    with pytest.raises(ValueError):
        open_pr(ws, REPORT, ISSUE, "o", "r", files_changed=("../evil.py",), clients=_clients(), dry_run=False)


def test_dot_and_empty_segments_rejected(tmp_path):
    ws = _workspace(tmp_path)
    for bad in ("a/./b.py", "a//b.py"):
        with pytest.raises(ValueError):
            open_pr(ws, REPORT, ISSUE, "o", "r", files_changed=(bad,), clients=_clients(), dry_run=False)


def test_symlink_is_refused_not_silently_omitted(tmp_path):
    """Vague 3 (était #423 : lien SAUTÉ) : un lien symbolique du diff n'est pas représentable par la Contents API ;
    l'omettre ferait livrer un contenu différent de celui testé → refus explicite avant toute écriture."""
    ws = _workspace(tmp_path, {"code.py": "x = 1\n", "node_modules/acorn/bin/acorn": "#!/usr/bin/env node\n"})
    bin_dir = Path(ws.path) / "node_modules" / ".bin"
    bin_dir.mkdir(parents=True)
    os.symlink(Path(ws.path) / "node_modules" / "acorn" / "bin" / "acorn", bin_dir / "acorn")
    clients = _clients()
    files = ("code.py", "node_modules/.bin/acorn")
    with pytest.raises(DeliveryRefusedError, match="node_modules/.bin/acorn"):
        open_pr(ws, REPORT, ISSUE, "o", "r", files_changed=files, clients=clients, dry_run=False)
    assert clients.branches.created == [] and clients.files.updated == [] and clients.prs.created == []
    preview = open_pr(ws, REPORT, ISSUE, "o", "r", files_changed=files, clients=clients, dry_run=True)
    assert preview.skipped_symlinks == ("node_modules/.bin/acorn",)
    assert "node_modules/.bin/acorn" in preview.body


def test_symlink_escape_is_refused_and_never_read(tmp_path):
    # Un agent non fiable crée un symlink pointant un secret hôte hors workspace : la livraison est REFUSÉE (lien non
    # représentable) et le contenu du secret n'est JAMAIS lu ni poussé (seule la cible du lien est empreintée).
    secret = tmp_path / "host_secret.txt"
    secret.write_text("HOST PRIVATE KEY", encoding="utf-8")
    ws = _workspace(tmp_path, {"real.py": "x = 1\n"})
    os.symlink(secret, Path(ws.path) / "evil.py")
    clients = _clients()
    with pytest.raises(DeliveryRefusedError, match="evil.py"):
        open_pr(ws, REPORT, ISSUE, "o", "r", files_changed=("real.py", "evil.py"), clients=clients, dry_run=False)
    assert clients.files.updated == [] and clients.prs.created == []
    snapshot = capture_delivery_snapshot(ws, ("real.py", "evil.py"))
    assert all("HOST PRIVATE KEY" not in (item.content or "") for item in snapshot.files)


def test_symlinked_dir_escape_still_rejected(tmp_path):
    # Garde de confinement conservée : un répertoire INTERMÉDIAIRE symlinké vers
    # l'hôte (le fichier terminal n'est pas un lien) reste une évasion → refus.
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("HOST PRIVATE KEY", encoding="utf-8")
    ws = _workspace(tmp_path, {"real.py": "x = 1\n"})
    os.symlink(outside, Path(ws.path) / "leak")
    clients = _clients()
    with pytest.raises(ValueError):
        open_pr(ws, REPORT, ISSUE, "o", "r", files_changed=("leak/secret.txt",), clients=clients, dry_run=False)
    assert clients.files.updated == []


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
