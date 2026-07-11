"""Tests C7 (#341) : checkpoint/reprise + journal de décisions.

- l'état d'un projet survit à un redémarrage simulé (AC#1 de l'epic #334) ;
- le journal de décisions est requêtable (insert + read + recherche, AC#2) ;
- tout tourne sur SQLite en CI ; variantes Postgres marquées `integration`.
"""

import json
import os
from dataclasses import asdict

import pytest

from collegue.state import ProjectStateManager, load_snapshot


@pytest.fixture
def db_url(tmp_path):
    return f"sqlite:///{tmp_path / 'state.db'}"


# --- reprise : l'état survit à un redémarrage simulé (AC#1) ----------------------


def test_state_survives_simulated_restart(db_url):
    # Process 1 : on écrit l'état puis on "ferme" le process.
    mgr1 = ProjectStateManager.from_url(db_url, create=True)
    plan_sync_config = {
        "repository": "owner/target",
        "labels": ["collegue", "build"],
        "options": {"create_project_board": False},
    }
    pid = mgr1.create_project(
        name="run-long",
        spec="construire le moteur",
        phase="2",
        plan_sync_config=plan_sync_config,
    )
    t1 = mgr1.add_task(pid, title="t1", status="done", depends_on=[])
    mgr1.add_task(pid, title="t2", status="todo", depends_on=[1])
    acceptance_source = "def test_contract():\n    assert True\n"
    mgr1.set_acceptance_test_artifact(
        t1,
        acceptance_source,
        {"role": "qa", "model": "qa-model", "schema_version": 1},
    )
    mgr1.record_decision(pid, summary="choisir PostgreSQL", rationale="durabilité")
    mgr1.save_checkpoint(pid, iteration=5, state_json={"cursor": 42, "phase": "2"})
    del mgr1  # simule l'arrêt du process

    # Process 2 : nouveau manager sur la MÊME base → recharge identique.
    mgr2 = ProjectStateManager.from_url(db_url, create=False)
    snap = load_snapshot(mgr2, pid)
    assert snap is not None
    assert snap.project["name"] == "run-long"
    assert snap.project["phase"] == "2"
    assert snap.project["plan_sync_config"] == plan_sync_config
    assert [t["title"] for t in snap.tasks] == ["t1", "t2"]
    assert [t["status"] for t in snap.tasks] == ["done", "todo"]
    assert [d["summary"] for d in snap.decisions] == ["choisir PostgreSQL"]
    assert snap.latest_checkpoint["iteration"] == 5
    assert snap.latest_checkpoint["state_json"] == {"cursor": 42, "phase": "2"}
    # Valeurs identiques jusqu'au détail : depends_on (JSON) + tz des datetimes.
    assert snap.tasks[1]["depends_on"] == [1]
    assert snap.tasks[0]["acceptance_test_source"] == acceptance_source
    assert len(snap.tasks[0]["acceptance_test_sha256"]) == 64
    assert snap.tasks[0]["acceptance_test_provenance"] == {
        "model": "qa-model",
        "role": "qa",
        "schema_version": 1,
    }
    assert snap.tasks[1]["acceptance_test_source"] is None  # projet historique / artefact absent
    assert snap.project["created_at"].endswith("+00:00")  # tz-aware préservée
    assert snap.decisions[0]["ts"].endswith("+00:00")


def test_snapshot_is_json_serializable(db_url):
    # Le snapshot doit pouvoir être json.dumps (datetimes en ISO-8601).
    mgr = ProjectStateManager.from_url(db_url, create=True)
    pid = mgr.create_project(
        name="p",
        spec="s",
        plan_sync_config={"repository": "owner/repo", "project_number": None},
    )
    tid = mgr.add_task(pid, title="t", depends_on=[1])
    mgr.set_acceptance_test_artifact(
        tid,
        "def test_snapshot():\n    assert True\n",
        {"role": "qa", "prompt": {"sha256": "a" * 64}},
    )
    mgr.record_decision(pid, summary="d")
    mgr.add_metric(pid, name="cov", value=63.5)
    mgr.save_checkpoint(pid, iteration=1, state_json={"k": "v"})
    snap = load_snapshot(mgr, pid)
    dumped = json.dumps(asdict(snap))  # ne doit pas lever
    assert "cov" in dumped
    assert "acceptance_test_sha256" in dumped
    assert "plan_sync_config" in dumped


def test_load_snapshot_missing_project(db_url):
    mgr = ProjectStateManager.from_url(db_url, create=True)
    assert load_snapshot(mgr, 99999) is None


def test_snapshot_without_checkpoint(db_url):
    # Projet existant mais sans checkpoint → latest_checkpoint None (pas de crash).
    mgr = ProjectStateManager.from_url(db_url, create=True)
    pid = mgr.create_project(name="neuf")
    snap = load_snapshot(mgr, pid)
    assert snap is not None
    assert snap.latest_checkpoint is None
    assert snap.tasks == []
    assert snap.decisions == []
    assert snap.metrics == []


def test_snapshot_includes_metrics(db_url):
    mgr = ProjectStateManager.from_url(db_url, create=True)
    pid = mgr.create_project(name="p")
    mgr.add_metric(pid, name="coverage", value=64.0)
    snap = load_snapshot(mgr, pid)
    assert [m["name"] for m in snap.metrics] == ["coverage"]
    assert snap.metrics[0]["value"] == 64.0


# --- load_checkpoint par itération ----------------------------------------------


def test_load_checkpoint_specific_and_latest(db_url):
    mgr = ProjectStateManager.from_url(db_url, create=True)
    pid = mgr.create_project(name="p")
    mgr.save_checkpoint(pid, iteration=1, state_json={"i": 1})
    mgr.save_checkpoint(pid, iteration=2, state_json={"i": 2})
    mgr.save_checkpoint(pid, iteration=3, state_json={"i": 3})

    assert mgr.load_checkpoint(pid, iteration=2).state_json == {"i": 2}
    # Sans itération → le dernier.
    assert mgr.load_checkpoint(pid).state_json == {"i": 3}
    # Itération inexistante → None.
    assert mgr.load_checkpoint(pid, iteration=99) is None


def test_load_checkpoint_iteration_zero(db_url):
    # iteration=0 doit cibler l'itération 0 (et non être confondu avec None).
    mgr = ProjectStateManager.from_url(db_url, create=True)
    pid = mgr.create_project(name="p")
    mgr.save_checkpoint(pid, iteration=0, state_json={"i": 0})
    assert mgr.load_checkpoint(pid, iteration=0).state_json == {"i": 0}


def test_save_checkpoint_upsert_no_duplicate(db_url):
    # Ré-enregistrer la même itération met à jour (pas de doublon qui masquerait).
    mgr = ProjectStateManager.from_url(db_url, create=True)
    pid = mgr.create_project(name="p")
    id1 = mgr.save_checkpoint(pid, iteration=5, state_json={"v": 1})
    id2 = mgr.save_checkpoint(pid, iteration=5, state_json={"v": 2})
    assert id1 == id2  # même ligne mise à jour
    latest = mgr.load_checkpoint(pid, iteration=5)
    assert latest.state_json == {"v": 2}
    snap = load_snapshot(mgr, pid)
    assert len([c for c in [snap.latest_checkpoint] if c]) == 1


# --- journal de décisions requêtable (AC#2) -------------------------------------


def test_decision_journal_insert_read_search(db_url):
    mgr = ProjectStateManager.from_url(db_url, create=True)
    pid = mgr.create_project(name="p")
    mgr.record_decision(pid, summary="adopter PostgreSQL pour l'état", rationale="durabilité")
    mgr.record_decision(pid, summary="ajouter un budget dur", rationale="éviter la surchauffe LLM")
    mgr.record_decision(pid, summary="timeout par appel", rationale="anti-hang")

    # read complet
    assert len(mgr.get_decision_journal(pid)) == 3
    # recherche par sous-chaîne (insensible à la casse) sur summary
    res = mgr.get_decision_journal(pid, query="postgres")
    assert [d.summary for d in res] == ["adopter PostgreSQL pour l'état"]
    # recherche sur rationale
    res2 = mgr.get_decision_journal(pid, query="HANG")
    assert [d.summary for d in res2] == ["timeout par appel"]
    # aucune correspondance
    assert mgr.get_decision_journal(pid, query="kubernetes") == []


def test_decision_search_escapes_like_wildcards(db_url):
    # Les métacaractères LIKE de la requête sont échappés : "%" est traité comme
    # un littéral, pas un wildcard match-all.
    mgr = ProjectStateManager.from_url(db_url, create=True)
    pid = mgr.create_project(name="p")
    mgr.record_decision(pid, summary="viser 100% de couverture")
    mgr.record_decision(pid, summary="autre décision sans pourcentage")
    # query "%" → matche UNIQUEMENT les textes contenant un vrai "%" (1 sur 2),
    # pas tout : preuve que "%" n'est pas interprété comme wildcard.
    res = mgr.get_decision_journal(pid, query="%")
    assert [d.summary for d in res] == ["viser 100% de couverture"]
    # recherche du littéral "100%" → trouve la bonne décision (pas de faux négatif).
    res2 = mgr.get_decision_journal(pid, query="100%")
    assert [d.summary for d in res2] == ["viser 100% de couverture"]


def test_search_tasks(db_url):
    mgr = ProjectStateManager.from_url(db_url, create=True)
    pid = mgr.create_project(name="p")
    mgr.add_task(pid, title="implémenter le parser Python", acceptance="AST complet")
    mgr.add_task(pid, title="écrire la doc", acceptance="couverture 80%")
    res = mgr.search_tasks(pid, query="PARSER")
    assert [t.title for t in res] == ["implémenter le parser Python"]
    assert mgr.search_tasks(pid, query="couverture")  # match sur acceptance


# --- intégration Postgres réelle (skippée en CI) --------------------------------


@pytest.mark.integration
def test_checkpoint_survives_restart_postgres():
    url = os.getenv("STATE_DATABASE_URL")
    if not url or not url.startswith("postgresql"):
        pytest.skip("STATE_DATABASE_URL PostgreSQL non configuré")
    mgr1 = ProjectStateManager.from_url(url, create=True)
    pid = mgr1.create_project(name="pg-run")
    mgr1.save_checkpoint(pid, iteration=7, state_json={"cursor": 7})
    mgr1.record_decision(pid, summary="décision pg")

    mgr2 = ProjectStateManager.from_url(url, create=False)
    snap = load_snapshot(mgr2, pid)
    assert snap.latest_checkpoint["iteration"] == 7
    assert snap.latest_checkpoint["state_json"] == {"cursor": 7}
    assert mgr2.get_decision_journal(pid, query="pg")
