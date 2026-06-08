"""Tests P5 (#356) : gate de validation humaine du plan."""

import pytest

from collegue.planner import (
    PlanNotApproved,
    Spec,
    approve_plan,
    build_plan_preview,
    decompose,
    is_approved,
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
