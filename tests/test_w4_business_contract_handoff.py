"""Handoff BUILD → IMPROVE avec contrats scellés : approbation du CONTENU ≠ statut de cycle (``improving``).

Le pilote pose le statut ``improving`` à la fin du BUILD, avant d'appeler la boucle d'amélioration. La relecture des
contrats livrés exigeait pourtant le statut ``approved`` : toute amélioration d'un projet à oracles scellés était refusée
alors que l'empreinte approuvée n'avait pas changé. Ces tests passent par le vrai générateur d'oracles, le vrai scellement,
la vraie approbation, un SQLite relu par une nouvelle instance et l'entrée publique ``run_project_from_settings``.

Seuls l'échantillonnage QA (oracles déterministes), l'agent et la mesure de qualité sont des doublons.
"""

from __future__ import annotations

import functools
import socket

import pytest
from test_improve_promotion import Scripted, metrics
from test_w3_integration_build import (
    CONTRACT_SETTINGS,
    DeliveringAgent,
    _file_oracle,
    bridge,  # noqa: F401 - fixture
    open_manager,
    oracle_sandbox,
    plan_with_oracles,
    run_pass,
    source,  # noqa: F401 - fixture
    state_url,  # noqa: F401 - fixture
    statuses,
)
from test_w3_integration_improve import FilesFeature

from collegue.executor.contracts import ContractError, load_delivered_contracts
from collegue.improve import run_improvement
from collegue.planner import require_approved
from collegue.planner.plan_review import PlanNotApproved, current_plan_hash, require_approved_content


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("réseau interdit")

    monkeypatch.setattr(socket.socket, "connect", refuse)


SOURCES = {"A": _file_oracle("delivered-0.txt"), "B": _file_oracle("delivered-1.txt")}


def improvement_kwargs(sequence):
    run_imp = functools.partial(run_improvement, measure_fn=Scripted(sequence), plateau_rounds=1, max_iterations=1)
    return dict(improve=True, run_improvement_fn=run_imp, settings=CONTRACT_SETTINGS)


async def delivered_project(monkeypatch, state_url, source, bridge):
    pid, _qa = await plan_with_oracles(monkeypatch, state_url, SOURCES)
    return pid


# ── l'API : contenu approuvé ≠ statut de cycle ───────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "status, changed, accepted",
    [
        ("approved", False, True),
        ("improving", False, True),  # le cas du défaut : même empreinte approuvée, statut de cycle
        ("improving", True, False),  # plan modifié depuis l'approbation
        ("planned", False, False),  # jamais approuvé / en brouillon
    ],
)
async def test_delivered_contracts_follow_the_sealed_content_approval_not_the_cycle_status(
    monkeypatch, state_url, status, changed, accepted
):
    pid, _ = await plan_with_oracles(monkeypatch, state_url, SOURCES)
    manager = open_manager(state_url)
    for task in manager.get_tasks(pid):
        manager.update_task_status(task.id, "merged")
    manager.update_project(pid, status=status)
    if changed:
        manager.update_project(pid, spec=(manager.get_project(pid).spec or "") + "\nExigence modifiée.\n")

    if accepted:
        contracts = load_delivered_contracts(open_manager(state_url), pid)
        assert [c.title for c in contracts] == ["A", "B"]
    else:
        with pytest.raises(ContractError, match="non approuvé ou modifié"):
            load_delivered_contracts(open_manager(state_url), pid)


async def test_a_revoked_approval_still_blocks_even_in_the_improving_cycle(monkeypatch, state_url):
    pid, _ = await plan_with_oracles(monkeypatch, state_url, SOURCES)
    manager = open_manager(state_url)
    manager.update_project(pid, status="improving", approved_plan_hash=None)

    with pytest.raises(ContractError):
        load_delivered_contracts(open_manager(state_url), pid)
    with pytest.raises(PlanNotApproved):
        require_approved_content(open_manager(state_url), pid)


async def test_the_write_guard_p4_still_demands_the_approved_status_and_nothing_re_approves_the_plan(
    monkeypatch, state_url
):
    pid, _ = await plan_with_oracles(monkeypatch, state_url, SOURCES)
    manager = open_manager(state_url)
    manager.update_project(pid, status="improving")
    approved_hash = open_manager(state_url).get_project(pid).approved_plan_hash

    require_approved_content(open_manager(state_url), pid)  # relecture de contrats : permise
    with pytest.raises(PlanNotApproved):  # écriture GitHub (P4) : toujours liée au statut approuvé
        require_approved(open_manager(state_url), pid)

    after = open_manager(state_url).get_project(pid)
    assert after.status == "improving" and after.approved_plan_hash == approved_hash == current_plan_hash(manager, pid)
    decisions = [d for d in open_manager(state_url).get_decision_journal(pid, "Plan approuvé")]
    assert len(decisions) == 1, "aucune ré-approbation : la décision humaine est la seule"


# ── l'entrée publique : BUILD → handoff → IMPROVE, puis reprise avec la même empreinte ───────────────────────────


async def test_public_handoff_replays_the_delivered_contracts_in_the_improving_cycle_and_promotes(
    monkeypatch, bridge, source, state_url
):
    pid = await delivered_project(monkeypatch, state_url, source, bridge)
    fingerprint = current_plan_hash(open_manager(state_url), pid)

    result = await run_pass(
        state_url,
        source,
        bridge,
        pid,
        sandbox=oracle_sandbox(),
        **improvement_kwargs([metrics(80), metrics(90)]),
    )

    assert result.project_status == "improving"
    assert statuses(state_url, pid) == {"A": "merged", "B": "merged"}
    improvement = result.improvement
    assert improvement is not None and improvement.rejected == [], improvement.rejected
    assert len(improvement.promoted) == 1, "les contrats livrés ont été relus au statut improving et la PR promue"
    heads = [pr["head"]["ref"] for pr in bridge.prs.values()]
    assert sum(head.startswith("collegue/improve-") for head in heads) == 1
    assert current_plan_hash(open_manager(state_url), pid) == fingerprint, "l'empreinte approuvée n'a pas bougé"
    assert open_manager(state_url).get_project(pid).status == "improving"


async def test_public_resume_while_improving_keeps_the_same_fingerprint_and_reuses_the_delivery(
    monkeypatch, bridge, source, state_url
):
    pid = await delivered_project(monkeypatch, state_url, source, bridge)
    first = await run_pass(
        state_url, source, bridge, pid, sandbox=oracle_sandbox(), **improvement_kwargs([metrics(80), metrics(90)])
    )
    fingerprint = current_plan_hash(open_manager(state_url), pid)
    prs_before = dict(bridge.prs)

    again = await run_pass(
        state_url,
        source,
        bridge,
        pid,
        sandbox=oracle_sandbox(),
        agent=FilesFeature({"docs/resume.md": "# reprise\n"}),
        **improvement_kwargs([metrics(80), metrics(90)]),
    )

    assert again.project_status == "improving"
    assert current_plan_hash(open_manager(state_url), pid) == fingerprint
    assert statuses(state_url, pid) == {"A": "merged", "B": "merged"}
    assert again.improvement is not None and again.improvement.rejected == [], again.improvement.rejected
    assert len(first.improvement.promoted) == len(again.improvement.promoted) == 1
    assert set(prs_before) < set(bridge.prs), "la reprise publie une NOUVELLE amélioration ; les PR précédentes restent"
    assert all(bridge.prs[n]["head"]["sha"] == pr["head"]["sha"] for n, pr in prs_before.items())


async def test_public_handoff_is_blocked_when_the_plan_changed_after_the_build(monkeypatch, bridge, source, state_url):
    pid = await delivered_project(monkeypatch, state_url, source, bridge)
    await run_pass(state_url, source, bridge, pid, sandbox=oracle_sandbox(), settings=CONTRACT_SETTINGS)
    manager = open_manager(state_url)
    manager.update_project(pid, status="improving", spec=(manager.get_project(pid).spec or "") + "\nModifié.\n")

    with pytest.raises(PlanNotApproved):
        await run_pass(
            state_url, source, bridge, pid, sandbox=oracle_sandbox(), **improvement_kwargs([metrics(80), metrics(90)])
        )
