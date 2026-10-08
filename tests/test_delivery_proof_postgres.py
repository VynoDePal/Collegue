"""Preuve de livraison sur un VRAI PostgreSQL (vague 3) — persistance, relecture, échappement LIKE, course.

Même fixture (`pg_url`) que `test_budget_ledger_postgres.py` : service fourni (`COLLEGUE_TEST_POSTGRES_URL`) ou cluster
jetable ; sans PostgreSQL, le test ÉCHOUE (jamais de skip). Aucun mock, aucun secret.
"""

from __future__ import annotations

import json
import threading
from dataclasses import replace

import pytest
from test_budget_ledger_postgres import pg_url  # noqa: F401  (fixture de module partagée)

from collegue.executor.delivery_proof import (
    MANDATORY_VERDICTS,
    PHASE_BUILD,
    DeliveryProofError,
    ProofDraft,
    TestedContent,
    _record_without_id,
    compute_proof_id,
    load_delivery_proof,
    persist_delivery_proof,
    persist_or_reuse_delivery_proof,
    seal_proof,
)
from collegue.state import ProjectStateManager

HEAD = "a" * 40


def make_proof(project_id, *, owner="o", repo="r_1", pr=7, head=HEAD, tree="b" * 40):
    content = TestedContent(
        base_sha="c" * 40,
        base_tree_sha="d" * 40,
        tree_sha=tree,
        content_sha256="e" * 64,
        files_count=3,
    )
    draft = ProofDraft(phase=PHASE_BUILD, content=content, delivered_paths=("a.py",))
    for name in MANDATORY_VERDICTS[PHASE_BUILD]:
        draft.add(name, True, "ok")
    return seal_proof(
        draft, owner=owner, repo=repo, project_id=project_id, pr_number=pr, head_sha=head, base_sha="c" * 40
    )


@pytest.fixture
def pg_managers(pg_url):  # noqa: F811
    managers = []

    def open_manager():
        manager = ProjectStateManager.from_url(pg_url, create=True)
        managers.append(manager)
        return manager

    yield open_manager
    for manager in managers:
        engine = getattr(manager, "_engine", None) or getattr(manager, "engine", None)
        if engine is not None:
            engine.dispose()


def test_proof_survives_a_new_manager_instance_on_postgres(pg_managers):
    first = pg_managers()
    project_id = first.create_project(name="preuve-pg")
    proof = make_proof(project_id)
    persist_delivery_proof(first, proof)

    second = pg_managers()  # nouvelle instance, nouvelle connexion
    loaded = load_delivery_proof(second, project_id, owner="o", repo="r_1", pr_number=7, head_sha=HEAD)
    assert loaded == proof and loaded.passed is True and loaded.proof_id == compute_proof_id(proof)


def test_like_metacharacters_never_match_another_repository(pg_managers):
    """`_` et `%` sont des jokers LIKE : la preuve de `r_1` ne doit jamais répondre à une requête sur `rX1` ou `r%`."""
    manager = pg_managers()
    project_id = manager.create_project(name="like-pg")
    persist_delivery_proof(manager, make_proof(project_id, repo="r_1"))
    for other in ("rX1", "r%", "r_%", "%"):
        with pytest.raises(DeliveryProofError, match="aucune preuve"):
            load_delivery_proof(manager, project_id, owner="o", repo=other, pr_number=7, head_sha=HEAD)
    assert load_delivery_proof(manager, project_id, owner="o", repo="r_1", pr_number=7, head_sha=HEAD)


def test_identity_and_tamper_refusals_hold_on_postgres(pg_managers):
    manager = pg_managers()
    project_id = manager.create_project(name="refus-pg")
    other_project = manager.create_project(name="autre-pg")
    proof = make_proof(project_id)
    persist_delivery_proof(manager, proof)
    with pytest.raises(DeliveryProofError):
        load_delivery_proof(manager, other_project, owner="o", repo="r_1", pr_number=7, head_sha=HEAD)
    with pytest.raises(DeliveryProofError):
        load_delivery_proof(manager, project_id, owner="o", repo="r_1", pr_number=8, head_sha=HEAD)

    forged = replace(proof, tree_sha="f" * 40)  # proof_id volontairement conservé : contenu altéré
    forged_head = "9" * 40
    record = {**_record_without_id(forged), "proof_id": proof.proof_id, "head_sha": forged_head}
    manager.record_decision(project_id, f"delivery-proof:v1:o/r_1#7@{forged_head}:{proof.proof_id}", json.dumps(record))
    with pytest.raises(DeliveryProofError, match="altérée|incohérent"):
        load_delivery_proof(manager, project_id, owner="o", repo="r_1", pr_number=7, head_sha=forged_head)


def test_two_workers_persisting_the_same_delivery_leave_one_truth(pg_managers):
    """Course : deux workers terminent la même livraison en même temps. L'identifiant est indépendant de l'horodatage,
    donc même si les deux écrivent, la relecture ne voit qu'une seule preuve (jamais un conflit durable)."""
    setup = pg_managers()
    project_id = setup.create_project(name="course-pg")
    barrier = threading.Barrier(4)
    failures = []

    def worker():
        try:
            manager = pg_managers()
            proof = make_proof(project_id)
            barrier.wait(timeout=30)
            persist_or_reuse_delivery_proof(manager, proof)
        except Exception as exc:  # noqa: BLE001
            failures.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert failures == []
    loaded = load_delivery_proof(pg_managers(), project_id, owner="o", repo="r_1", pr_number=7, head_sha=HEAD)
    assert loaded.proof_id == compute_proof_id(make_proof(project_id))
    entries = pg_managers().get_decision_journal(project_id, "delivery-proof:v1:")
    assert 1 <= len(entries) <= 4  # écritures concurrentes possibles, une seule identité observable


def test_a_different_proof_for_the_same_head_is_refused_not_overwritten(pg_managers):
    manager = pg_managers()
    project_id = manager.create_project(name="conflit-pg")
    persist_or_reuse_delivery_proof(manager, make_proof(project_id))
    with pytest.raises(DeliveryProofError, match="différente"):
        persist_or_reuse_delivery_proof(manager, make_proof(project_id, tree="1" * 40))
    assert (
        load_delivery_proof(manager, project_id, owner="o", repo="r_1", pr_number=7, head_sha=HEAD).tree_sha == "b" * 40
    )
