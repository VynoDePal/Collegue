"""Tests P4 (#355) : synchronisation du plan vers GitHub (clients mockés)."""

from types import SimpleNamespace

import pytest

from collegue.planner import (
    PlanNotApproved,
    Spec,
    SpecSyncError,
    SyncTargetMismatch,
    approve_plan,
    load_plan_snapshot,
    normalize_plan_sync_config,
    persist_spec,
    sync_plan,
)
from collegue.planner.github_sync import SyncClients, SyncError, _default_clients
from collegue.state import ProjectStateManager


@pytest.fixture
def manager(tmp_path):
    return ProjectStateManager.from_url(f"sqlite:///{tmp_path / 'state.db'}", create=True)


class _FakeIssues:
    def __init__(self, start=100):
        self.created = []
        self._next = start  # GitHub numérote de façon monotone (jamais réutilisé)

    def create_issue(self, owner, repo, title, body=None):
        self._next += 1
        self.created.append({"title": title, "body": body, "number": self._next})
        return SimpleNamespace(number=self._next, title=title)


class _FakeLabels:
    def __init__(self):
        self.ensured = []
        self.added = []

    def ensure_label(self, owner, repo, name, color="ededed", description=None):
        self.ensured.append(name)
        return SimpleNamespace(name=name)

    def add_labels_to_issue(self, owner, repo, issue_number, labels):
        self.added.append((issue_number, list(labels)))
        return list(labels)


class _FakeMilestones:
    def __init__(self):
        self.assigned = []

    def ensure_milestone(self, owner, repo, title, description=None, due_on=None):
        return SimpleNamespace(number=7, title=title)

    def assign_milestone(self, owner, repo, issue_number, milestone_number):
        self.assigned.append((issue_number, milestone_number))


class _FakeProjects:
    def __init__(self):
        self.added = []

    def ensure_project(self, owner, title):
        return SimpleNamespace(id="BOARD1", number=1, title=title)

    def issue_node_id(self, owner, repo, issue_number):
        return f"NODE{issue_number}"

    def add_issue_to_project(self, project_id, node_id):
        self.added.append((project_id, node_id))
        return "ITEM"


class _FakeFiles:
    def __init__(self, fail=False, current_content=None, commit_sha="abc"):
        self.calls = []
        self.get_calls = []
        self._fail = fail
        self._current_content = current_content
        self._commit_sha = commit_sha

    def get_file_content(self, owner, repo, path, branch=None):
        self.get_calls.append({"owner": owner, "repo": repo, "path": path, "branch": branch})
        if self._current_content is None:
            raise RuntimeError("not found")
        return {"content": self._current_content, "sha": "blob-sha"}

    def update_file(self, owner, repo, path, message, content, branch=None):
        if self._fail:
            raise RuntimeError("commit refusé")
        self.calls.append(
            {"owner": owner, "repo": repo, "path": path, "message": message, "content": content, "branch": branch}
        )
        return {"commit": {"sha": self._commit_sha} if self._commit_sha else {}}


def _clients():
    return SyncClients(_FakeIssues(), _FakeLabels(), _FakeMilestones(), _FakeProjects())


def _clients_with_files(files=None):
    return SyncClients(_FakeIssues(), _FakeLabels(), _FakeMilestones(), _FakeProjects(), files=files or _FakeFiles())


def _planned(manager, *, approve=False):
    pid = persist_spec(manager, name="demo", spec=Spec(title="Demo", acceptance_criteria=["AC1"]))
    a = manager.add_task(pid, title="A", acceptance="critère A")
    manager.add_task(pid, title="B", acceptance="critère B", depends_on=[a])
    if approve:
        approve_plan(manager, pid)
    return pid


# --- dry-run --------------------------------------------------------------------


def test_dry_run_describes_without_writing(manager):
    pid = _planned(manager)
    clients = _clients()
    result = sync_plan(manager, pid, "o", "r", dry_run=True, clients=clients)
    assert result.dry_run is True
    assert [i["title"] for i in result.issues] == ["A", "B"]
    assert clients.issues.created == []  # AUCUNE écriture


def test_dry_run_does_not_require_approval(manager):
    pid = _planned(manager, approve=False)  # non approuvé
    result = sync_plan(manager, pid, "o", "r", dry_run=True, clients=_clients())
    assert result.dry_run is True  # le dry-run (lecture seule) ne nécessite pas l'approbation


# --- gate (P5) ------------------------------------------------------------------


def test_real_sync_requires_approval(manager):
    pid = _planned(manager, approve=False)
    clients = _clients()
    with pytest.raises(PlanNotApproved):
        sync_plan(manager, pid, "o", "r", dry_run=False, clients=clients)
    assert clients.issues.created == []  # rien écrit sans approbation


# --- écriture réelle (approuvée) ------------------------------------------------


def test_real_sync_creates_linked_issues(manager):
    pid = _planned(manager, approve=True)
    clients = _clients()
    result = sync_plan(
        manager,
        pid,
        "o",
        "r",
        dry_run=False,
        labels=["autonome", "feature"],
        milestone_title="Phase 1",
        board_title="Board",
        clients=clients,
    )
    assert result.dry_run is False
    assert len(clients.issues.created) == 2
    # B (créée après A) référence le numéro d'issue de A dans son corps.
    a_number = clients.issues.created[0]["number"]
    b_body = clients.issues.created[1]["body"]
    assert f"#{a_number}" in b_body
    assert "## Critère d'acceptation" in b_body
    assert f";project:{pid};task:" in b_body
    assert "<!-- collegue-plan:" in b_body
    # labels appliqués à chaque issue
    assert len(clients.labels.added) == 2
    # milestone assigné, ajout au board
    assert len(clients.milestones.assigned) == 2
    assert len(clients.projects.added) == 2
    assert result.milestone == "Phase 1" and result.board == "Board"
    # issue_number persisté sur les tâches (mapping task↔issue)
    assert all(t.issue_number for t in manager.get_tasks(pid))


def test_product_client_creates_issue_labels_and_milestone_atomically(manager):
    class _AtomicIssues(_FakeIssues):
        def __init__(self):
            super().__init__()
            self.atomic = []

        def create_issue_with_metadata(
            self,
            owner,
            repo,
            title,
            *,
            body=None,
            labels=(),
            milestone_number=None,
        ):
            issue = super().create_issue(owner, repo, title, body)
            self.atomic.append((issue.number, list(labels), milestone_number))
            return issue

    pid = _planned(manager, approve=True)
    issues = _AtomicIssues()
    labels = _FakeLabels()
    milestones = _FakeMilestones()
    clients = SyncClients(issues, labels, milestones, _FakeProjects())

    sync_plan(
        manager,
        pid,
        "o",
        "r",
        dry_run=False,
        labels=["nightly-run"],
        milestone_title="Nightly",
        clients=clients,
    )

    assert issues.atomic == [(101, ["nightly-run"], 7), (102, ["nightly-run"], 7)]
    assert labels.added == []
    assert milestones.assigned == []


def test_real_sync_journals_decision(manager):
    pid = _planned(manager, approve=True)
    sync_plan(manager, pid, "o", "r", dry_run=False, clients=_clients())
    assert any("synchronis" in d.summary.lower() for d in manager.get_decisions(pid))


def test_real_sync_is_idempotent(manager):
    pid = _planned(manager, approve=True)
    clients1 = _clients()
    sync_plan(manager, pid, "o", "r", dry_run=False, clients=clients1)
    assert len(clients1.issues.created) == 2
    # 2e run : approbation toujours valide (issue_number ne fait pas partie du hash),
    # rien n'est recréé.
    clients2 = _clients()
    result = sync_plan(manager, pid, "o", "r", dry_run=False, clients=clients2)
    assert clients2.issues.created == []  # aucune recréation
    assert all(i.get("skipped") for i in result.issues)


def test_real_sync_minimal_without_milestone_or_board(manager):
    pid = _planned(manager, approve=True)
    clients = _clients()
    sync_plan(manager, pid, "o", "r", dry_run=False, clients=clients)
    assert len(clients.issues.created) == 2
    assert clients.milestones.assigned == []  # pas de milestone demandé
    assert clients.projects.added == []  # pas de board demandé


def test_explicit_empty_labels_are_not_replaced_by_defaults(manager):
    pid = _planned(manager, approve=True)
    clients = _clients()

    result = sync_plan(manager, pid, "o", "r", dry_run=False, labels=[], clients=clients)

    assert clients.labels.ensured == []
    assert clients.labels.added == []
    assert all(issue["labels"] == [] for issue in result.issues)


def test_dry_run_echoes_milestone_and_board(manager):
    pid = _planned(manager)
    result = sync_plan(
        manager, pid, "o", "r", dry_run=True, milestone_title="Phase 1", board_title="Board", clients=_clients()
    )
    assert result.milestone == "Phase 1" and result.board == "Board"


def test_target_aware_sync_refuses_any_override_before_writing(manager):
    target = normalize_plan_sync_config({"owner": "acme", "repo": "app", "labels": []})
    pid = persist_spec(
        manager, name="sealed", spec=Spec(title="S", acceptance_criteria=["AC"]), plan_sync_config=target
    )
    manager.add_task(pid, title="A")
    approve_plan(manager, pid)
    clients = _clients_with_files()

    with pytest.raises(SyncTargetMismatch):
        sync_plan(manager, pid, "acme", "other", dry_run=False, labels=[], clients=clients)

    assert clients.files.calls == []
    assert clients.issues.created == []


def test_real_sync_consumes_one_immutable_approved_snapshot(manager):
    target = normalize_plan_sync_config({"owner": "acme", "repo": "one", "labels": []})
    pid = persist_spec(
        manager, name="sealed", spec=Spec(title="Original", acceptance_criteria=["AC"]), plan_sync_config=target
    )
    task_id = manager.add_task(pid, title="Tâche originale")
    approve_plan(manager, pid)
    snapshot = load_plan_snapshot(manager, pid, require_approval=True, require_target=True)

    # Simule une révision concurrente après le snapshot. La livraison peut encore
    # envoyer l'ancien contrat approuvé, mais jamais un mélange cible A / contenu B.
    manager.update_project(
        pid,
        spec="# SPEC muté\n",
        plan_sync_config=normalize_plan_sync_config({"owner": "acme", "repo": "two", "labels": []}),
    )
    manager.update_task(task_id, title="Tâche mutée")
    clients = _clients_with_files()

    sync_plan(manager, pid, "acme", "one", dry_run=False, labels=[], clients=clients, snapshot=snapshot)

    assert clients.files.calls[0]["repo"] == "one"
    assert "Original" in clients.files.calls[0]["content"]
    assert clients.issues.created[0]["title"] == "Tâche originale"


# --- graphe invalide : aucune écriture (anti drop silencieux d'arête) ----------


def test_real_sync_raises_on_dangling_dependency(manager):
    pid = persist_spec(manager, name="d", spec=Spec(title="D", acceptance_criteria=["AC"]))
    manager.add_task(pid, title="A", depends_on=[99999])  # dépendance vers un id absent
    approve_plan(manager, pid)
    clients = _clients()
    with pytest.raises(SyncError):
        sync_plan(manager, pid, "o", "r", dry_run=False, clients=clients)
    assert clients.issues.created == []  # rien écrit


def test_real_sync_raises_on_cycle(manager):
    pid = persist_spec(manager, name="d", spec=Spec(title="D", acceptance_criteria=["AC"]))
    a = manager.add_task(pid, title="A")
    b = manager.add_task(pid, title="B", depends_on=[a])
    manager.update_task(a, depends_on=[b])  # A↔B cycle
    approve_plan(manager, pid)
    with pytest.raises(SyncError):
        sync_plan(manager, pid, "o", "r", dry_run=False, clients=_clients())


def test_product_preflight_rejects_invalid_dag_before_spec_write(manager):
    pid = persist_spec(manager, name="d", spec=Spec(title="D", acceptance_criteria=["AC"]))
    task = manager.add_task(pid, title="A")
    manager.update_task(task, depends_on=[task])
    approve_plan(manager, pid)
    files = _FakeFiles()

    with pytest.raises(SyncError, match="cycle"):
        sync_plan(
            manager,
            pid,
            "o",
            "r",
            dry_run=False,
            clients=_clients_with_files(files),
            require_spec_commit=True,
        )

    assert files.get_calls == []
    assert files.calls == []


# --- idempotence : pas de gaspillage d'API quand tout est déjà synchronisé ------


def test_idempotent_rerun_skips_ensure_calls(manager):
    pid = _planned(manager, approve=True)
    sync_plan(manager, pid, "o", "r", dry_run=False, labels=["autonome"], clients=_clients())
    # 2e run : tout est déjà synchronisé → aucun ensure_label (pas de brûlage d'API).
    clients2 = _clients()
    result = sync_plan(manager, pid, "o", "r", dry_run=False, labels=["autonome"], clients=clients2)
    assert clients2.labels.ensured == []
    assert clients2.issues.created == []
    assert all(i.get("skipped") for i in result.issues)


# --- échec partiel : pas de double-création au retry (idempotence) --------------


def test_partial_failure_then_retry_no_duplicate(manager):
    pid = _planned(manager, approve=True)

    class _FailSecond(_FakeIssues):
        def create_issue(self, owner, repo, title, body=None):
            if title == "B":
                raise RuntimeError("réseau coupé sur la 2e issue")
            return super().create_issue(owner, repo, title, body)

    clients = SyncClients(_FailSecond(), _FakeLabels(), _FakeMilestones(), _FakeProjects())
    with pytest.raises(RuntimeError):
        sync_plan(manager, pid, "o", "r", dry_run=False, clients=clients)
    # A a été créée et son issue_number persisté ; B non.
    tasks = {t.title: t for t in manager.get_tasks(pid)}
    assert tasks["A"].issue_number is not None
    assert tasks["B"].issue_number is None

    # Retry : A est skippée (pas de doublon), seule B est créée. Numérotation
    # monotone (GitHub ne réutilise jamais un numéro) → pas de collision avec A.
    clients2 = SyncClients(_FakeIssues(start=200), _FakeLabels(), _FakeMilestones(), _FakeProjects())
    sync_plan(manager, pid, "o", "r", dry_run=False, clients=clients2)
    assert [c["title"] for c in clients2.issues.created] == ["B"]


def test_default_clients_smoke():
    clients = _default_clients(token="x")
    assert clients.issues and clients.labels and clients.milestones and clients.projects


# --- A3 : commit du SPEC.md dans le repo cible ----------------------------------


def test_real_sync_commits_spec_md(manager):
    pid = _planned(manager, approve=True)
    files = _FakeFiles()
    result = sync_plan(manager, pid, "o", "r", dry_run=False, clients=_clients_with_files(files))
    assert result.spec_committed == "SPEC.md"
    assert result.spec_commit_sha == "abc"
    assert len(files.calls) == 1
    call = files.calls[0]
    assert call["path"] == "SPEC.md" and call["owner"] == "o" and call["repo"] == "r"
    assert "Demo" in call["content"]  # le SPEC markdown persisté en DB est committé


def test_spec_filename_configurable(manager):
    pid = _planned(manager, approve=True)
    files = _FakeFiles()
    result = sync_plan(
        manager, pid, "o", "r", dry_run=False, clients=_clients_with_files(files), spec_filename="docs/SPEC.md"
    )
    assert result.spec_committed == "docs/SPEC.md"
    assert files.calls[0]["path"] == "docs/SPEC.md"


def test_spec_commit_uses_the_approved_base_branch(manager):
    pid = _planned(manager, approve=True)
    files = _FakeFiles()

    sync_plan(
        manager,
        pid,
        "o",
        "r",
        dry_run=False,
        clients=_clients_with_files(files),
        base_branch="develop",
    )

    assert files.calls[0]["branch"] == "develop"


def test_spec_commit_is_best_effort(manager):
    # Un échec de commit du SPEC ne casse PAS la synchro des issues (best-effort).
    pid = _planned(manager, approve=True)
    result = sync_plan(manager, pid, "o", "r", dry_run=False, clients=_clients_with_files(_FakeFiles(fail=True)))
    assert result.spec_committed is None  # échec signalé
    assert all(t.issue_number for t in manager.get_tasks(pid))  # issues créées quand même


def test_product_sync_fails_closed_when_spec_commit_fails(manager):
    pid = _planned(manager, approve=True)
    clients = _clients_with_files(_FakeFiles(fail=True))

    with pytest.raises(SpecSyncError, match="Commit obligatoire"):
        sync_plan(manager, pid, "o", "r", dry_run=False, clients=clients, require_spec_commit=True)

    assert clients.issues.created == []
    assert clients.labels.ensured == []
    assert clients.milestones.assigned == []
    assert clients.projects.added == []
    assert all(t.issue_number is None for t in manager.get_tasks(pid))


def test_product_sync_requires_files_client_and_confirmed_commit_sha(manager):
    pid = _planned(manager, approve=True)
    with pytest.raises(SpecSyncError, match="client GitHub files absent"):
        sync_plan(manager, pid, "o", "r", dry_run=False, clients=_clients(), require_spec_commit=True)

    clients = _clients_with_files(_FakeFiles(commit_sha=None))
    with pytest.raises(SpecSyncError, match="commit.sha absent"):
        sync_plan(manager, pid, "o", "r", dry_run=False, clients=clients, require_spec_commit=True)
    assert clients.issues.created == []


def test_product_sync_reuses_identical_spec_without_empty_commit(manager):
    pid = _planned(manager, approve=True)
    spec = manager.get_project(pid).spec
    files = _FakeFiles(current_content=spec)
    clients = _clients_with_files(files)

    result = sync_plan(manager, pid, "o", "r", dry_run=False, clients=clients, require_spec_commit=True)

    assert result.spec_committed == "SPEC.md"
    assert result.spec_unchanged is True
    assert result.spec_commit_sha is None
    assert files.calls == []
    assert len(clients.issues.created) == 2


def test_product_sync_refuses_to_overwrite_divergent_spec(manager):
    pid = _planned(manager, approve=True)
    files = _FakeFiles(current_content="# SPEC humain\n")
    clients = _clients_with_files(files)

    with pytest.raises(SpecSyncError, match="contenu divergent"):
        sync_plan(manager, pid, "o", "r", dry_run=False, clients=clients, require_spec_commit=True)

    assert files.calls == []
    assert clients.issues.created == []


def test_no_files_client_skips_spec_commit(manager):
    # SyncClients 4-clients (rétro-compat) → pas de client files → no-op, pas d'erreur.
    pid = _planned(manager, approve=True)
    result = sync_plan(manager, pid, "o", "r", dry_run=False, clients=_clients())
    assert result.spec_committed is None
    assert all(t.issue_number for t in manager.get_tasks(pid))


def test_dry_run_does_not_commit_spec(manager):
    pid = _planned(manager)  # non approuvé, dry-run
    files = _FakeFiles()
    result = sync_plan(manager, pid, "o", "r", dry_run=True, clients=_clients_with_files(files))
    assert result.spec_committed is None
    assert files.calls == []  # aucune écriture en dry-run


def test_default_clients_includes_files():
    from collegue.tools.github_commands import FileCommands

    assert isinstance(_default_clients(token=None).files, FileCommands)
