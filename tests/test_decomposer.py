"""Tests P2 (#353) : décomposition SPEC → graphe de tâches (state store)."""

import json

import pytest

import collegue.planner.decomposer as dec
from collegue.planner import Spec, decompose
from collegue.state import ProjectStateManager


class _Result:
    def __init__(self, text="", result=None):
        self.text = text
        self.result = result


class _Ctx:
    def __init__(self, result):
        self._result = result
        self.kwargs = None

    async def sample(self, **kwargs):
        self.kwargs = kwargs
        return self._result


@pytest.fixture
def manager(tmp_path):
    return ProjectStateManager.from_url(f"sqlite:///{tmp_path / 'state.db'}", create=True)


def _project(manager):
    return manager.create_project(name="p", spec="# spec")


def _decomp(tasks):
    return _Result(result={"tasks": tasks})


# --- découpage + persistance ----------------------------------------------------


@pytest.mark.asyncio
async def test_decompose_persists_tasks(manager):
    pid = _project(manager)
    ctx = _Ctx(
        _decomp(
            [
                {"title": "setup", "acceptance": "repo prêt", "depends_on": []},
                {"title": "build", "acceptance": "tests verts", "depends_on": [0]},
            ]
        )
    )
    tasks = await decompose(Spec(title="T", acceptance_criteria=["ac"]), ctx, manager=manager, project_id=pid)
    assert [t.title for t in tasks] == ["setup", "build"]
    assert tasks[0].acceptance == "repo prêt"


@pytest.mark.asyncio
async def test_decompose_resolves_dependencies_to_ids(manager):
    pid = _project(manager)
    ctx = _Ctx(_decomp([{"title": "A", "depends_on": []}, {"title": "B", "depends_on": [0]}]))
    tasks = await decompose("# SPEC texte", ctx, manager=manager, project_id=pid)
    by_title = {t.title: t for t in tasks}
    assert by_title["A"].depends_on == []  # toujours une liste (jamais None)
    assert by_title["B"].depends_on == [by_title["A"].id]  # index 0 → vrai ID DB


@pytest.mark.asyncio
async def test_decompose_resolves_dependency_listed_later(manager):
    # A (index 0) dépend de B (index 1) qui apparaît APRÈS dans la liste : l'ordre
    # topo doit persister B avant A et résoudre la dépendance vers le vrai ID de B.
    pid = _project(manager)
    ctx = _Ctx(_decomp([{"title": "A", "depends_on": [1]}, {"title": "B", "depends_on": []}]))
    tasks = await decompose("spec", ctx, manager=manager, project_id=pid)
    by_title = {t.title: t for t in tasks}
    assert by_title["B"].id < by_title["A"].id
    assert by_title["A"].depends_on == [by_title["B"].id]


@pytest.mark.asyncio
async def test_decompose_dedups_duplicate_dependency(manager):
    pid = _project(manager)
    ctx = _Ctx(_decomp([{"title": "A", "depends_on": []}, {"title": "B", "depends_on": [0, 0]}]))
    tasks = await decompose("spec", ctx, manager=manager, project_id=pid)
    by_title = {t.title: t for t in tasks}
    assert by_title["B"].depends_on == [by_title["A"].id]  # déduit → une seule entrée


@pytest.mark.asyncio
async def test_decompose_structured_result(manager):
    pid = _project(manager)
    decomp = dec._Decomposition(tasks=[dec._PlannedTask(title="x", depends_on=[])])
    tasks = await decompose("spec", _Ctx(_Result(result=decomp)), manager=manager, project_id=pid)
    assert [t.title for t in tasks] == ["x"]


@pytest.mark.asyncio
async def test_decompose_journals_decision(manager):
    pid = _project(manager)
    ctx = _Ctx(_decomp([{"title": "x", "depends_on": []}]))
    await decompose("spec", ctx, manager=manager, project_id=pid)
    decisions = manager.get_decisions(pid)
    assert len(decisions) == 1
    assert "Décomposition" in decisions[0].summary


@pytest.mark.asyncio
async def test_decompose_json_text_fallback(manager):
    pid = _project(manager)
    payload = json.dumps({"tasks": [{"title": "t1", "depends_on": []}]})
    ctx = _Ctx(_Result(text="Voici:\n" + payload))
    tasks = await decompose("spec", ctx, manager=manager, project_id=pid)
    assert [t.title for t in tasks] == ["t1"]


@pytest.mark.asyncio
async def test_decompose_routes_planner_role(monkeypatch, manager):
    monkeypatch.setattr(dec, "model_preferences_for_role", lambda role, settings_obj=None: ["planner-model"])
    pid = _project(manager)
    ctx = _Ctx(_decomp([{"title": "x", "depends_on": []}]))
    await decompose("spec", ctx, manager=manager, project_id=pid)
    assert ctx.kwargs["model_preferences"] == ["planner-model"]


@pytest.mark.asyncio
async def test_exact_single_task_keeps_route_and_test_in_one_task(manager):
    pid = _project(manager)
    ctx = _Ctx(
        _decomp(
            [
                {
                    "title": "Ajouter la route nightly et son test",
                    "acceptance": "GET /nightly renvoie 200 et le JSON exact",
                    "depends_on": [],
                }
            ]
        )
    )

    tasks = await decompose(
        "Ajouter GET /nightly et son test",
        ctx,
        manager=manager,
        project_id=pid,
        exact_task_count=1,
    )

    assert [task.title for task in tasks] == ["Ajouter la route nightly et son test"]
    assert "exactement 1 tâche" in ctx.kwargs["messages"]
    assert "ne les scinde pas" in ctx.kwargs["messages"]
    assert "EXACTEMENT 1 tâche" in ctx.kwargs["system_prompt"]
    assert "implémentation et ses tests" in ctx.kwargs["system_prompt"]


@pytest.mark.asyncio
async def test_exact_single_task_rejects_multi_task_before_any_persistence(manager):
    pid = _project(manager)
    ctx = _Ctx(
        _decomp(
            [
                {"title": "Ajouter la route", "depends_on": []},
                {"title": "Ajouter le test", "depends_on": [0]},
            ]
        )
    )

    with pytest.raises(dec.DecompositionCardinalityError, match="exactement 1"):
        await decompose("spec", ctx, manager=manager, project_id=pid, exact_task_count=1)

    assert manager.get_tasks(pid) == []
    assert manager.get_decisions(pid) == []


# --- validation du graphe (rien persisté si invalide) ---------------------------


@pytest.mark.asyncio
async def test_decompose_rejects_empty(manager):
    pid = _project(manager)
    ctx = _Ctx(_decomp([]))
    with pytest.raises(ValueError):
        await decompose("spec", ctx, manager=manager, project_id=pid)
    assert manager.get_tasks(pid) == []


@pytest.mark.asyncio
async def test_decompose_rejects_cycle(manager):
    pid = _project(manager)
    # A dépend de B, B dépend de A → cycle.
    ctx = _Ctx(_decomp([{"title": "A", "depends_on": [1]}, {"title": "B", "depends_on": [0]}]))
    with pytest.raises(ValueError):
        await decompose("spec", ctx, manager=manager, project_id=pid)
    assert manager.get_tasks(pid) == []  # rien persisté


@pytest.mark.asyncio
async def test_decompose_rejects_out_of_range_dependency(manager):
    pid = _project(manager)
    ctx = _Ctx(_decomp([{"title": "A", "depends_on": [5]}]))
    with pytest.raises(ValueError):
        await decompose("spec", ctx, manager=manager, project_id=pid)
    assert manager.get_tasks(pid) == []


@pytest.mark.asyncio
async def test_decompose_rejects_self_dependency(manager):
    pid = _project(manager)
    ctx = _Ctx(_decomp([{"title": "A", "depends_on": [0]}]))
    with pytest.raises(ValueError):
        await decompose("spec", ctx, manager=manager, project_id=pid)
    assert manager.get_tasks(pid) == []


@pytest.mark.asyncio
async def test_decompose_raises_on_garbage(manager):
    pid = _project(manager)
    ctx = _Ctx(_Result(text="pas de json"))
    with pytest.raises(ValueError):
        await decompose("spec", ctx, manager=manager, project_id=pid)


@pytest.mark.asyncio
async def test_decompose_rejects_malformed_tasks(manager):
    # {"tasks": null} → ValidationError pydantic convertie en ValueError (contrat module).
    pid = _project(manager)
    ctx = _Ctx(_Result(result={"tasks": None}))
    with pytest.raises(ValueError):
        await decompose("spec", ctx, manager=manager, project_id=pid)


@pytest.mark.asyncio
async def test_decompose_rejects_too_many_tasks(manager):
    pid = _project(manager)
    big = [{"title": f"t{i}", "depends_on": []} for i in range(dec.MAX_TASKS + 1)]
    ctx = _Ctx(_decomp(big))
    with pytest.raises(ValueError):
        await decompose("spec", ctx, manager=manager, project_id=pid)
    assert manager.get_tasks(pid) == []  # rien persisté


@pytest.mark.asyncio
async def test_decompose_idempotency_guard(manager):
    # Un 2e décompose sur un projet déjà décomposé est refusé (pas de duplication).
    pid = _project(manager)
    await decompose("spec", _Ctx(_decomp([{"title": "x", "depends_on": []}])), manager=manager, project_id=pid)
    with pytest.raises(ValueError):
        await decompose("spec", _Ctx(_decomp([{"title": "y", "depends_on": []}])), manager=manager, project_id=pid)
    assert [t.title for t in manager.get_tasks(pid)] == ["x"]


@pytest.mark.asyncio
async def test_decompose_is_atomic_on_midloop_failure(monkeypatch, manager):
    # Un échec d'insertion en cours de boucle → rollback total (aucune tâche, aucune décision).
    pid = _project(manager)
    real_task = dec.Task

    def _boom_task(**kwargs):
        if kwargs.get("title") == "BOOM":
            raise RuntimeError("insertion échouée")
        return real_task(**kwargs)

    monkeypatch.setattr(dec, "Task", _boom_task)
    ctx = _Ctx(_decomp([{"title": "ok", "depends_on": []}, {"title": "BOOM", "depends_on": [0]}]))
    with pytest.raises(RuntimeError):
        await decompose("spec", ctx, manager=manager, project_id=pid)
    assert manager.get_tasks(pid) == []  # la 1re tâche a été rollback
    assert manager.get_decisions(pid) == []  # pas de décision journalisée
