"""Contrat du registre durable ``task_merges`` (write-ahead de fusion, CAS, réouverture).

Les MÊMES cas tournent sur SQLite (``test_task_merge_state.py``) et sur un VRAI PostgreSQL
(``test_task_merge_postgres.py``) : l'atomicité d'un compare-and-set ne se prouve pas sur un double.
Chaque cas reçoit ``(url, manager)`` ; ``url`` permet d'ouvrir d'autres gestionnaires (processus concurrents).
"""

from __future__ import annotations

import threading

import pytest

from collegue.state import ProjectStateManager
from collegue.state.manager import TaskMergeConflictError

PROOF = "a" * 64
H1, H2, BASE, TREE, MERGE = ("1" * 40, "2" * 40, "b" * 40, "c" * 40, "d" * 40)


def _begin(manager, task_id, **overrides):
    kwargs = dict(
        owner="o",
        repo="r",
        base_branch="main",
        pr_number=11,
        head_sha=H1,
        base_sha=BASE,
        tree_sha=TREE,
        proof_id=PROOF,
        merge_method="squash",
    )
    kwargs.update(overrides)
    return manager.begin_task_merge(task_id, **kwargs)


def _task(manager, title="T1", status="in_review"):
    project_id = manager.create_project(name="p" + title)
    return project_id, manager.add_task(project_id, title, status=status)


def case_write_ahead_is_idempotent_for_the_same_identity_and_refuses_another(url, manager):
    _, task_id = _task(manager)
    first = _begin(manager, task_id)
    assert (first.state, first.revision, first.merge_sha) == ("merge_pending", 0, None)
    assert _begin(manager, task_id).revision == 0
    with pytest.raises(TaskMergeConflictError):
        _begin(manager, task_id, head_sha=H2)
    assert manager.get_task_merge(task_id).head_sha == H1


def case_nominal_path_completes_the_task_in_the_same_transaction(url, manager):
    _, task_id = _task(manager)
    row = _begin(manager, task_id)
    row = manager.transition_task_merge(
        task_id, expected_state=row.state, expected_revision=row.revision, new_state="merged_unsynced", merge_sha=MERGE
    )
    assert (row.state, row.revision, row.merge_sha) == ("merged_unsynced", 1, MERGE)
    assert manager.get_task(task_id).status == "in_review", "livraison non comptée prête tant que non synchronisée"
    row = manager.transition_task_merge(
        task_id,
        expected_state="merged_unsynced",
        expected_revision=1,
        new_state="merged_unsynced",
        last_error="resynchronisation échouée",
    )
    assert row.revision == 2 and row.merge_sha == MERGE and row.last_error == "resynchronisation échouée"
    row = manager.transition_task_merge(
        task_id,
        expected_state="merged_unsynced",
        expected_revision=2,
        new_state="synced",
        last_error=None,
        complete_task=True,
    )
    assert (row.state, row.revision) == ("synced", 3)
    assert manager.get_task(task_id).status == "merged"


def case_unsynced_requires_the_remote_merge_sha_and_leaves_the_row_untouched(url, manager):
    _, task_id = _task(manager)
    _begin(manager, task_id)
    with pytest.raises(ValueError, match="SHA de fusion"):
        manager.transition_task_merge(
            task_id, expected_state="merge_pending", expected_revision=0, new_state="merged_unsynced"
        )
    row = manager.get_task_merge(task_id)
    assert (row.state, row.revision) == ("merge_pending", 0)


def case_stale_revision_or_state_is_a_conflict_and_changes_nothing(url, manager):
    _, task_id = _task(manager)
    _begin(manager, task_id)
    for state, revision in (("merge_pending", 5), ("merged_unsynced", 0)):
        with pytest.raises(TaskMergeConflictError):
            manager.transition_task_merge(
                task_id, expected_state=state, expected_revision=revision, new_state="attention"
            )
    assert manager.get_task_merge(task_id).state == "merge_pending"


def case_illegal_transitions_are_rejected(url, manager):
    _, task_id = _task(manager)
    _begin(manager, task_id)
    with pytest.raises(ValueError):  # pending -> synced saute la fusion confirmée
        manager.transition_task_merge(
            task_id, expected_state="merge_pending", expected_revision=0, new_state="synced", merge_sha=MERGE
        )
    with pytest.raises(ValueError):  # complete_task seulement vers synced
        manager.transition_task_merge(
            task_id, expected_state="merge_pending", expected_revision=0, new_state="abandoned", complete_task=True
        )
    with pytest.raises(ValueError):
        manager.transition_task_merge(task_id, expected_state="merge_pending", expected_revision=0, new_state="inconnu")
    manager.transition_task_merge(task_id, expected_state="merge_pending", expected_revision=0, new_state="abandoned")
    with pytest.raises(ValueError):  # un état terminal ne repart pas par transition
        manager.transition_task_merge(
            task_id, expected_state="abandoned", expected_revision=1, new_state="merge_pending"
        )


def case_abandoned_and_other_head_synced_rows_reopen_but_same_head_synced_does_not(url, manager):
    _, task_id = _task(manager)
    row = _begin(manager, task_id)
    manager.transition_task_merge(task_id, expected_state=row.state, expected_revision=0, new_state="abandoned")
    reopened = _begin(manager, task_id, head_sha=H2)
    assert (reopened.state, reopened.revision, reopened.head_sha, reopened.merge_sha) == ("merge_pending", 2, H2, None)

    manager.transition_task_merge(
        task_id, expected_state="merge_pending", expected_revision=2, new_state="merged_unsynced", merge_sha=MERGE
    )
    manager.transition_task_merge(
        task_id, expected_state="merged_unsynced", expected_revision=3, new_state="synced", complete_task=True
    )
    with pytest.raises(TaskMergeConflictError):
        _begin(manager, task_id, head_sha=H2)  # même tête déjà livrée : jamais de seconde fusion
    again = _begin(manager, task_id, head_sha=H1, pr_number=12)  # nouvelle PR après un revert
    assert (again.state, again.pr_number, again.merge_sha) == ("merge_pending", 12, None)


def case_unfinished_cycles_block_a_new_intention(url, manager):
    _, task_id = _task(manager)
    row = _begin(manager, task_id)
    manager.transition_task_merge(
        task_id, expected_state=row.state, expected_revision=0, new_state="merged_unsynced", merge_sha=MERGE
    )
    with pytest.raises(TaskMergeConflictError):
        _begin(manager, task_id, head_sha=H2)
    assert manager.get_task_merge(task_id).state == "merged_unsynced"


def case_list_filters_by_project_and_state(url, manager):
    project_id, first = _task(manager, "A")
    second = manager.add_task(project_id, "B", status="in_review")
    other_project, other_task = _task(manager, "C")
    _begin(manager, first)
    _begin(manager, second, pr_number=12)
    _begin(manager, other_task, pr_number=13)
    manager.transition_task_merge(second, expected_state="merge_pending", expected_revision=0, new_state="abandoned")
    assert [r.task_id for r in manager.list_task_merges(project_id)] == [first, second]
    assert [r.task_id for r in manager.list_task_merges(project_id, states={"merge_pending"})] == [first]
    assert [r.task_id for r in manager.list_task_merges(other_project)] == [other_task]
    with pytest.raises(ValueError):
        manager.list_task_merges(project_id, states={"nope"})


def case_acknowledge_only_clears_an_attention_row_at_the_expected_revision(url, manager):
    _, task_id = _task(manager)
    _begin(manager, task_id)
    with pytest.raises(TaskMergeConflictError):
        manager.acknowledge_task_merge(task_id, expected_revision=0)
    manager.transition_task_merge(
        task_id, expected_state="merge_pending", expected_revision=0, new_state="attention", last_error="tree"
    )
    with pytest.raises(TaskMergeConflictError):
        manager.acknowledge_task_merge(task_id, expected_revision=0)
    assert manager.acknowledge_task_merge(task_id, expected_revision=1) is True
    assert manager.get_task_merge(task_id) is None
    assert manager.acknowledge_task_merge(task_id, expected_revision=1) is False


def case_exactly_one_of_many_concurrent_processes_wins_the_compare_and_set(url, manager):
    _, task_id = _task(manager)
    _begin(manager, task_id)
    workers = 8
    barrier = threading.Barrier(workers)
    outcomes = []

    def contender(index):
        mine = ProjectStateManager.from_url(url)
        try:
            barrier.wait(timeout=30)
            mine.transition_task_merge(
                task_id,
                expected_state="merge_pending",
                expected_revision=0,
                new_state="merged_unsynced",
                merge_sha=f"{index:x}" * 40,
            )
            outcomes.append("won")
        except TaskMergeConflictError:
            outcomes.append("lost")
        except Exception as exc:  # noqa: BLE001 - toute autre erreur est un échec du test
            outcomes.append(f"error:{exc!r}")
        finally:
            engine = getattr(mine, "engine", None) or getattr(mine, "_engine", None)
            if engine is not None:
                engine.dispose()

    threads = [threading.Thread(target=contender, args=(i,)) for i in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert sorted(outcomes) == ["lost"] * (workers - 1) + ["won"], outcomes
    row = manager.get_task_merge(task_id)
    assert (row.state, row.revision) == ("merged_unsynced", 1)


def case_deleting_the_task_deletes_its_cycle_row(url, manager):
    from sqlalchemy import delete

    from collegue.state.models import Task

    _, task_id = _task(manager)
    _begin(manager, task_id)
    with manager.session() as session:
        session.execute(delete(Task).where(Task.id == task_id))
    assert manager.get_task_merge(task_id) is None


def run_alembic_upgrade_0011_to_0012(url):
    """VRAIE migration Alembic 0011 → 0012 → 0011 → head sur une base VIERGE (jamais ``create_all``).

    Données préexistantes (projet, tâche ``in_review``, décision) créées à 0011 et conservées à chaque étape ;
    version Alembic relue en base ; contraintes et cascade de ``task_merges`` éprouvées sur le schéma migré."""
    from alembic import command
    from sqlalchemy import create_engine, inspect, text
    from sqlalchemy.exc import IntegrityError

    from collegue.migrations import alembic_config, head_revisions

    cfg = alembic_config(url)
    engine = create_engine(url)

    def version():
        with engine.connect() as conn:
            return conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()

    def snapshot():
        with engine.connect() as conn:
            projects = conn.execute(text("SELECT id, name FROM projects ORDER BY id")).all()
            tasks = conn.execute(text("SELECT id, project_id, title, status FROM tasks ORDER BY id")).all()
        return [tuple(r) for r in projects], [tuple(r) for r in tasks]

    try:
        assert "projects" not in inspect(engine).get_table_names(), "base vierge exigée : aucune table pré-créée"
        command.upgrade(cfg, "0011")
        assert version() == "0011" and "task_merges" not in inspect(engine).get_table_names()

        legacy = ProjectStateManager.from_url(url)  # create=False : le schéma vient d'Alembic seul
        project_id = legacy.create_project(name="préexistant", spec="spec")
        task_id = legacy.add_task(project_id, "T1", status="in_review")
        legacy.record_decision(project_id, "décision antérieure à 0012")
        before = snapshot()
        assert before[1] == [(task_id, project_id, "T1", "in_review")]

        command.upgrade(cfg, "0012")
        assert version() == "0012" and head_revisions() == ["0012"]
        schema = inspect(engine)
        assert "task_merges" in schema.get_table_names()
        assert {"task_id", "state", "revision", "head_sha", "base_sha", "tree_sha", "proof_id", "merge_sha"} <= {
            c["name"] for c in schema.get_columns("task_merges")
        }
        assert "ix_task_merges_project_id" in {i["name"] for i in schema.get_indexes("task_merges")}
        assert {
            "ck_task_merges_state",
            "ck_task_merges_merge_method",
            "ck_task_merges_pr_positive",
            "ck_task_merges_revision_nonnegative",
            "ck_task_merges_sha_lengths",
            "ck_task_merges_required_text",
            "ck_task_merges_state_merge_sha",
        } <= {c["name"] for c in schema.get_check_constraints("task_merges")}
        checks = {c["name"]: c["sqltext"] for c in schema.get_check_constraints("task_merges")}
        assert (
            "'merged_unsynced'" in checks["ck_task_merges_state"]
            and "IS NOT NULL OR" not in checks["ck_task_merges_state"]
        )
        assert snapshot() == before, "les données préexistantes sont conservées"

        migrated = ProjectStateManager.from_url(url)
        row = _begin(migrated, task_id)  # la tâche préexistante peut porter un cycle de fusion
        assert (row.state, row.revision) == ("merge_pending", 0)
        with pytest.raises(IntegrityError):
            with engine.begin() as conn:
                conn.execute(text("UPDATE task_merges SET state = 'nope' WHERE task_id = :t"), {"t": task_id})
        extra = migrated.add_task(project_id, "éphémère", status="in_review")  # cascade : tâche supprimée -> cycle
        _begin(migrated, extra, pr_number=12)
        with migrated.session() as session:  # le gestionnaire active les FK (PRAGMA) sur SQLite
            from sqlalchemy import delete

            from collegue.state.models import Task

            session.execute(delete(Task).where(Task.id == extra))
        assert migrated.get_task_merge(extra) is None
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM task_merges WHERE task_id = :t"), {"t": task_id})

        command.downgrade(cfg, "0011")
        assert version() == "0011" and "task_merges" not in inspect(engine).get_table_names()
        assert snapshot() == before, "le downgrade ne touche que task_merges"

        command.upgrade(cfg, "head")
        assert version() == head_revisions()[0] == "0012" and "task_merges" in inspect(engine).get_table_names()
        assert snapshot() == before
    finally:
        engine.dispose()


CONTRACT = [value for name, value in sorted(globals().items()) if name.startswith("case_") and callable(value)]
IDS = [fn.__name__.removeprefix("case_") for fn in CONTRACT]
