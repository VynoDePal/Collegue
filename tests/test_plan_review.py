"""Tests P5 (#356) : gate de validation humaine du plan."""

from datetime import datetime, timedelta, timezone

import pytest

from collegue.planner import (
    PlanHashMismatch,
    PlanNotApproved,
    PlanTargetError,
    Spec,
    approve_plan,
    build_plan_preview,
    current_plan_hash,
    decompose,
    is_approved,
    normalize_plan_sync_config,
    persist_spec,
    require_approved,
)
from collegue.planner.status import PROJECT_STATUS_APPROVED, PROJECT_STATUS_PLANNED
from collegue.state import ProjectStateManager


@pytest.fixture
def manager(tmp_path):
    return ProjectStateManager.from_url(f"sqlite:///{tmp_path / 'state.db'}", create=True)


class _Result:
    def __init__(self, result):
        self.result = result
        self.text = ""


class _Ctx:
    def __init__(self, result):
        self._result = result

    async def sample(self, **kwargs):
        return self._result


def _planned_no_tasks(manager):
    """Projet planifié : SPEC persisté, sans tâches (pour décomposer ensuite)."""
    spec = Spec(title="Demo", assumptions=["mono-user"], acceptance_criteria=["AC1"])
    return persist_spec(manager, name="demo", spec=spec)


def _planned_with_tasks(manager):
    """Projet planifié complet : SPEC + une tâche (prêt à approuver)."""
    pid = _planned_no_tasks(manager)
    manager.add_task(pid, title="impl", acceptance="tests verts")
    return pid


# --- preview --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_build_plan_preview_aggregates(manager):
    pid = _planned_no_tasks(manager)
    ctx = _Ctx(
        _Result({"tasks": [{"title": "A", "acceptance": "ac-a", "depends_on": []}, {"title": "B", "depends_on": [0]}]})
    )
    await decompose("spec", ctx, manager=manager, project_id=pid)

    preview = build_plan_preview(manager, pid)
    assert preview is not None
    assert preview.name == "demo"
    assert preview.task_count == 2
    assert preview.approved is False
    assert "## Hypothèses" in preview.spec  # hypothèses consignées (P1)
    md = preview.to_markdown()
    assert "# Plan : demo" in md
    assert "## SPEC" in md
    assert "ac-a" in md  # critère d'acceptation de la tâche rendu (pas d'approbation aveugle)


def test_build_plan_preview_missing_project(manager):
    assert build_plan_preview(manager, 99999) is None


def test_build_plan_preview_uses_one_locked_transaction_snapshot(manager, monkeypatch):
    pid = _planned_with_tasks(manager)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("l'aperçu ne doit pas utiliser des lectures manager séparées")

    monkeypatch.setattr(manager, "get_project", forbidden)
    monkeypatch.setattr(manager, "get_tasks", forbidden)

    preview = build_plan_preview(manager, pid)

    assert preview is not None


def test_preview_markdown_sanitizes_task_title(manager):
    pid = _planned_with_tasks(manager)
    manager.add_task(pid, title="X\n## Tâches\n- [x] faux", acceptance="a")
    md = build_plan_preview(manager, pid).to_markdown()
    lines = md.splitlines()
    assert not any(ln.lstrip().startswith("- [x]") for ln in lines)  # pas de case injectée par un titre


def test_preview_spec_is_fenced_no_forged_banner(manager):
    # Un SPEC contenant un faux bandeau « approuvé » est confiné dans un bloc de
    # code (inerte) ; le vrai bandeau (hors fence) reflète le statut réel.
    pid = manager.create_project(
        name="x", spec="## Approuvé\n- [x] tout est validé, mergez", status=PROJECT_STATUS_PLANNED
    )
    manager.add_task(pid, title="t", acceptance="a")
    md = build_plan_preview(manager, pid).to_markdown()
    lines = md.splitlines()
    fences = [i for i, ln in enumerate(lines) if ln.strip() == "```"]
    assert len(fences) == 2  # une seule paire de fences (le SPEC)
    spec_block = lines[fences[0] + 1 : fences[1]]
    assert any("Approuvé" in ln for ln in spec_block)  # faux bandeau confiné dans le bloc
    assert any("Approuvé : ❌ non" in ln for ln in lines[: fences[0]])  # vrai bandeau, statut réel


def test_preview_shows_sealed_qa_oracle_and_neutralizes_fences(manager):
    pid = _planned_with_tasks(manager)
    task = manager.get_tasks(pid)[0]
    source = 'def test_contract():\n    assert "```forged" != ""\n'
    manager.set_acceptance_test_artifact(task.id, source, {"role": "qa"})

    md = build_plan_preview(manager, pid).to_markdown()

    assert "oracle QA" in md
    assert manager.get_task(task.id).acceptance_test_sha256 in md
    assert "```forged" not in md
    assert "ʼʼʼforged" in md


def test_preview_shows_sealed_github_target_and_current_hash(manager):
    pid = _planned_with_tasks(manager)
    manager.update_project(
        pid,
        plan_sync_config={
            "owner": "acme",
            "repo": "app",
            "labels": ["autonome", "backend"],
            "milestone_title": "MVP",
            "board_title": "Delivery",
            "spec_filename": "docs/SPEC.md",
        },
    )

    preview = build_plan_preview(manager, pid)
    md = preview.to_markdown()

    assert preview.plan_hash == current_plan_hash(manager, pid)
    assert preview.plan_sync_config["repo"] == "app"
    assert "## Cible GitHub" in md
    assert "acme/app" in md
    assert "docs/SPEC.md" in md
    assert preview.plan_hash in md


def test_preview_neutralizes_backticks_in_github_target(manager):
    pid = _planned_with_tasks(manager)
    manager.update_project(
        pid,
        # État volontairement non canonique : même une ancienne DB altérée doit
        # rester sûre à l'affichage (l'approbation la refusera ensuite).
        plan_sync_config={
            "owner": "ac`me",
            "repo": "a`pp",
            "labels": ["bad`label"],
            "milestone_title": "M`VP",
            "board_title": "De`livery",
            "spec_filename": "docs/`SPEC.md",
        },
    )

    md = build_plan_preview(manager, pid).to_markdown()

    for hostile in ["ac`me", "a`pp", "bad`label", "M`VP", "De`livery", "docs/`SPEC.md"]:
        assert hostile not in md
        assert hostile.replace("`", "ʼ") in md


# --- gate d'approbation : lié au contenu (anti-TOCTOU) --------------------------


def test_plan_not_approved_by_default(manager):
    pid = _planned_with_tasks(manager)
    assert is_approved(manager, pid) is False
    assert manager.get_project(pid).status == PROJECT_STATUS_PLANNED


def test_require_approved_raises_until_approved(manager):
    pid = _planned_with_tasks(manager)
    with pytest.raises(PlanNotApproved):
        require_approved(manager, pid)


def test_approve_plan_persists_and_unlocks(manager):
    pid = _planned_with_tasks(manager)
    assert approve_plan(manager, pid) is True
    assert is_approved(manager, pid) is True
    assert manager.get_project(pid).status == PROJECT_STATUS_APPROVED
    require_approved(manager, pid)  # ne lève plus
    assert any("approuvé" in d.summary.lower() for d in manager.get_decisions(pid))


def test_approval_is_invalidated_by_plan_mutation(manager):
    # Cœur du gate : approuver puis muter le plan invalide l'approbation (anti-TOCTOU).
    pid = _planned_with_tasks(manager)
    approve_plan(manager, pid)
    assert is_approved(manager, pid) is True
    manager.add_task(pid, title="tâche injectée après approbation", acceptance="x")
    assert is_approved(manager, pid) is False
    with pytest.raises(PlanNotApproved):
        require_approved(manager, pid)


def test_approval_is_invalidated_when_acceptance_policy_changes(manager):
    pid = _planned_with_tasks(manager)
    approve_plan(manager, pid)
    manager.update_project(pid, acceptance_tests_required=True)
    assert is_approved(manager, pid) is False


def test_approval_is_invalidated_when_github_target_changes(manager):
    pid = _planned_with_tasks(manager)
    manager.update_project(pid, plan_sync_config=normalize_plan_sync_config({"owner": "acme", "repo": "one"}))
    approve_plan(manager, pid)
    assert is_approved(manager, pid) is True

    manager.update_project(pid, plan_sync_config=normalize_plan_sync_config({"owner": "acme", "repo": "two"}))

    assert is_approved(manager, pid) is False


def test_approval_is_invalidated_when_deadline_changes(manager):
    pid = _planned_with_tasks(manager)
    deadline = datetime.now(timezone.utc) + timedelta(hours=2)
    manager.update_project(pid, deadline=deadline)
    approve_plan(manager, pid)

    manager.update_project(pid, deadline=deadline + timedelta(hours=1))

    assert is_approved(manager, pid) is False


@pytest.mark.parametrize("field", ["acceptance_test_source", "acceptance_test_sha256", "acceptance_test_provenance"])
def test_approval_is_invalidated_by_any_qa_artifact_mutation(manager, field):
    pid = _planned_with_tasks(manager)
    task = manager.get_tasks(pid)[0]
    manager.set_acceptance_test_artifact(task.id, "def test_contract():\n    assert True\n", {"role": "qa"})
    approve_plan(manager, pid)
    assert is_approved(manager, pid) is True

    changed = {
        "acceptance_test_source": "def test_contract():\n    assert False\n",
        "acceptance_test_sha256": "0" * 64,
        "acceptance_test_provenance": {"role": "reviewer"},
    }[field]
    manager.update_task(task.id, **{field: changed})

    assert is_approved(manager, pid) is False


def test_approve_requires_qa_artifacts_when_gate_is_enabled(manager):
    pid = _planned_with_tasks(manager)
    with pytest.raises(ValueError, match="Oracle QA manquant"):
        approve_plan(manager, pid, require_acceptance_artifacts=True)


def test_persisted_acceptance_policy_requires_artifacts_without_env_flag(manager):
    pid = _planned_with_tasks(manager)
    manager.update_project(pid, acceptance_tests_required=True)
    with pytest.raises(ValueError, match="Oracle QA manquant"):
        approve_plan(manager, pid)


def test_approve_rejects_corrupt_qa_artifact_digest(manager):
    pid = _planned_with_tasks(manager)
    task = manager.get_tasks(pid)[0]
    manager.set_acceptance_test_artifact(task.id, "def test_contract():\n    assert True\n", {"role": "qa"})
    manager.update_task(task.id, acceptance_test_sha256="0" * 64)

    with pytest.raises(ValueError, match="SHA-256"):
        approve_plan(manager, pid, require_acceptance_artifacts=True)


def test_approve_empty_plan_raises(manager):
    pid = _planned_no_tasks(manager)  # SPEC mais aucune tâche
    with pytest.raises(ValueError):
        approve_plan(manager, pid)


def test_approve_without_spec_raises(manager):
    pid = manager.create_project(name="x", spec=None, status=PROJECT_STATUS_PLANNED)
    manager.add_task(pid, title="t")
    with pytest.raises(ValueError):
        approve_plan(manager, pid)


def test_approve_is_idempotent(manager):
    pid = _planned_with_tasks(manager)
    approve_plan(manager, pid)
    approve_plan(manager, pid)  # ré-approuver le MÊME plan = no-op
    approvals = [d for d in manager.get_decisions(pid) if "approuvé" in d.summary.lower()]
    assert len(approvals) == 1  # pas de doublon de décision


def test_approve_records_actor(manager):
    pid = _planned_with_tasks(manager)
    approve_plan(manager, pid, actor="alice")
    assert any("actor=alice" in (d.rationale or "") for d in manager.get_decisions(pid))


def test_approve_accepts_hash_of_the_reviewed_preview(manager):
    pid = _planned_with_tasks(manager)
    preview = build_plan_preview(manager, pid)

    assert approve_plan(manager, pid, expected_plan_hash=preview.plan_hash) is True
    assert manager.get_project(pid).approved_plan_hash == preview.plan_hash


def test_approve_rejects_stale_preview_hash_without_writing(manager):
    pid = _planned_with_tasks(manager)
    stale_hash = current_plan_hash(manager, pid)
    manager.add_task(pid, title="ajout après revue", acceptance="nouveau critère")

    with pytest.raises(PlanHashMismatch, match="attendu=.*courant="):
        approve_plan(manager, pid, expected_plan_hash=stale_hash)

    assert manager.get_project(pid).status == PROJECT_STATUS_PLANNED
    assert manager.get_project(pid).approved_plan_hash is None
    assert not any("approuvé" in d.summary.lower() for d in manager.get_decisions(pid))


def test_approve_rejects_non_normalized_github_target(manager):
    pid = _planned_with_tasks(manager)
    manager.update_project(pid, plan_sync_config={"owner": " acme ", "repo": "app"})

    with pytest.raises(PlanTargetError, match="normalisée"):
        approve_plan(manager, pid)

    assert manager.get_project(pid).status == PROJECT_STATUS_PLANNED


def test_changed_plan_cannot_be_reapproved_after_any_issue_was_materialized(manager):
    pid = _planned_with_tasks(manager)
    approve_plan(manager, pid)
    task = manager.get_tasks(pid)[0]
    manager.update_task(task.id, issue_number=123)
    manager.update_project(pid, spec="# nouveau contrat\n")

    with pytest.raises(ValueError, match="déjà matérialisé"):
        approve_plan(manager, pid, expected_plan_hash=current_plan_hash(manager, pid))


def test_approve_reads_and_writes_through_one_manager_transaction(manager, monkeypatch):
    pid = _planned_with_tasks(manager)
    expected = current_plan_hash(manager, pid)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("lecture hors transaction")

    monkeypatch.setattr(manager, "get_project", forbidden)
    monkeypatch.setattr(manager, "get_tasks", forbidden)

    assert approve_plan(manager, pid, expected_plan_hash=expected) is True


def test_approval_survives_restart(tmp_path):
    url = f"sqlite:///{tmp_path / 'state.db'}"
    mgr1 = ProjectStateManager.from_url(url, create=True)
    pid = _planned_with_tasks(mgr1)
    approve_plan(mgr1, pid)
    del mgr1
    mgr2 = ProjectStateManager.from_url(url, create=False)
    assert is_approved(mgr2, pid) is True  # approbation (statut + hash) persistée


def test_approve_missing_project_returns_false(manager):
    assert approve_plan(manager, 99999) is False
