"""Tests F1 (#374) : ordonnanceur de graphe — sélection des tâches prêtes (DAG)."""

import pytest

from collegue.pilot import SchedulerError, is_blocked, next_task, ready_tasks, remaining_tasks
from collegue.state import ProjectStateManager


@pytest.fixture
def manager(tmp_path):
    return ProjectStateManager.from_url(f"sqlite:///{tmp_path / 'state.db'}", create=True)


def _ids(tasks):
    return [t.id for t in tasks]


# --- graphe linéaire A→B→C ------------------------------------------------------


def test_linear_chain_unblocks_step_by_step(manager):
    pid = manager.create_project(name="demo")
    a = manager.add_task(pid, title="A")
    b = manager.add_task(pid, title="B", depends_on=[a])
    c = manager.add_task(pid, title="C", depends_on=[b])

    assert _ids(ready_tasks(manager.get_tasks(pid))) == [a]  # seul A
    manager.update_task_status(a, "in_review")
    assert _ids(ready_tasks(manager.get_tasks(pid))) == [b]  # A faite → B
    manager.update_task_status(b, "done")
    assert _ids(ready_tasks(manager.get_tasks(pid))) == [c]  # B faite → C
    manager.update_task_status(c, "in_review")
    assert ready_tasks(manager.get_tasks(pid)) == []
    assert remaining_tasks(manager.get_tasks(pid)) == []


# --- branches parallèles --------------------------------------------------------


def test_parallel_branches_ready_in_deterministic_order(manager):
    pid = manager.create_project(name="demo")
    a = manager.add_task(pid, title="A")
    b = manager.add_task(pid, title="B")
    c = manager.add_task(pid, title="C", depends_on=[a, b])

    assert _ids(ready_tasks(manager.get_tasks(pid))) == [a, b]  # trié par id
    assert next_task(manager.get_tasks(pid)).id == a
    # C reste bloquée tant que A ET B ne sont pas satisfaites
    manager.update_task_status(a, "in_review")
    assert _ids(ready_tasks(manager.get_tasks(pid))) == [b]
    manager.update_task_status(b, "in_review")
    assert _ids(ready_tasks(manager.get_tasks(pid))) == [c]


def test_dependency_on_higher_id_no_false_cycle(manager):
    # A (id plus petit) dépend de B (id plus grand) : DAG valide. La validation
    # visite B comme dépendance de A, puis l'atteint à nouveau comme racine (déjà
    # noire) → pas de faux cycle. La « prête » suit les dépendances, pas l'id.
    pid = manager.create_project(name="demo")
    a = manager.add_task(pid, title="A")
    b = manager.add_task(pid, title="B")
    manager.update_task(a, depends_on=[b])  # A → B (id_a < id_b)
    assert _ids(ready_tasks(manager.get_tasks(pid))) == [b]  # B d'abord, malgré l'id
    manager.update_task_status(b, "done")
    assert _ids(ready_tasks(manager.get_tasks(pid))) == [a]


def test_diamond_dependency_no_false_cycle(manager):
    # Diamant D→{A,B}→C : aucune fausse détection de cycle ; seule D est prête.
    pid = manager.create_project(name="demo")
    d = manager.add_task(pid, title="D")
    a = manager.add_task(pid, title="A", depends_on=[d])
    b = manager.add_task(pid, title="B", depends_on=[d])
    manager.add_task(pid, title="C", depends_on=[a, b])
    assert _ids(ready_tasks(manager.get_tasks(pid))) == [d]


def test_in_review_satisfies_dependency(manager):
    # Une PR ouverte (in_review) suffit à débloquer la suivante (pas besoin du merge).
    pid = manager.create_project(name="demo")
    a = manager.add_task(pid, title="A")
    b = manager.add_task(pid, title="B", depends_on=[a])
    manager.update_task_status(a, "in_review")
    assert _ids(ready_tasks(manager.get_tasks(pid))) == [b]


# --- next_task / vide -----------------------------------------------------------


def test_next_task_none_when_nothing_ready(manager):
    pid = manager.create_project(name="demo")
    a = manager.add_task(pid, title="A")
    manager.update_task_status(a, "in_progress")  # en cours, pas prête
    assert next_task(manager.get_tasks(pid)) is None


# --- graphes invalides ----------------------------------------------------------


def test_cycle_raises(manager):
    pid = manager.create_project(name="demo")
    a = manager.add_task(pid, title="A")
    b = manager.add_task(pid, title="B", depends_on=[a])
    manager.update_task(a, depends_on=[b])  # A↔B
    with pytest.raises(SchedulerError):
        ready_tasks(manager.get_tasks(pid))


def test_dangling_dependency_raises(manager):
    pid = manager.create_project(name="demo")
    manager.add_task(pid, title="A", depends_on=[99999])
    with pytest.raises(SchedulerError):
        ready_tasks(manager.get_tasks(pid))


def test_self_dependency_is_a_cycle(manager):
    pid = manager.create_project(name="demo")
    a = manager.add_task(pid, title="A")
    manager.update_task(a, depends_on=[a])
    with pytest.raises(SchedulerError):
        ready_tasks(manager.get_tasks(pid))


# --- blocage --------------------------------------------------------------------


def test_is_blocked_when_dependency_failed(manager):
    pid = manager.create_project(name="demo")
    a = manager.add_task(pid, title="A")
    manager.add_task(pid, title="B", depends_on=[a])
    manager.update_task_status(a, "failed")  # ni satisfaite ni active
    tasks = manager.get_tasks(pid)
    assert ready_tasks(tasks) == []  # B non prête (dep failed)
    assert is_blocked(tasks) is True


def test_not_blocked_while_a_task_is_in_progress(manager):
    pid = manager.create_project(name="demo")
    a = manager.add_task(pid, title="A")
    manager.add_task(pid, title="B", depends_on=[a])
    manager.update_task_status(a, "in_progress")  # progresse → pas un blocage
    tasks = manager.get_tasks(pid)
    assert ready_tasks(tasks) == []
    assert is_blocked(tasks) is False


def test_not_blocked_when_work_is_ready(manager):
    pid = manager.create_project(name="demo")
    manager.add_task(pid, title="A")
    tasks = manager.get_tasks(pid)
    assert is_blocked(tasks) is False  # A est prête


def test_not_blocked_when_all_done(manager):
    pid = manager.create_project(name="demo")
    a = manager.add_task(pid, title="A")
    manager.update_task_status(a, "done")
    assert is_blocked(manager.get_tasks(pid)) is False
