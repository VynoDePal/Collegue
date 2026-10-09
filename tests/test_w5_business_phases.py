"""Phases R04 (amélioration livrée) et R05 (incident contrôlé, rollback, acquittement CAS, reprise) de la campagne réelle W5.

UN scénario déterministe joue, sur le monde de ``w5_business_world`` (trois tâches BUILD réellement livrées, socle d'exemples), les
refus puis les réussites DANS L'ORDRE où ils s'enchaînent sur un même projet ; les tests lisent ses résultats. Les phases sont
celles de production, appelées par les vraies entrées publiques du produit. Aucun verdict de revue, de mesure ou de fusion n'est
injecté : les refus ci-dessous viennent des gardes réelles (gate de gain, couverture, revue, contrats, politique Phase 5).
"""

from __future__ import annotations

import dataclasses
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict

import pytest
import w4_business_campaign as harness
import w4_business_fixture as fixture
import w5_business_world as world_support

from collegue.executor import FakeReviewer
from collegue.improve.metrics import measure
from collegue.pilot import w4_business as business
from collegue.pilot import w5_business as w5

pytestmark = pytest.mark.slow


class ScriptedMeasure:
    """Mesure RÉELLE avec une couverture dégradée APRÈS le diff (90 → 80) : le gate doit refuser, quel que soit le gain par ailleurs."""

    def __init__(self, real):
        self.real, self.calls = real, 0

    async def __call__(self, workspace, ctx, **kwargs):
        result = await self.real(workspace, ctx, **kwargs)
        self.calls += 1
        if self.calls >= 2:
            result = dataclasses.replace(result, coverage_pct=80.0)
        return result


def phase_outcome(step: Any) -> Dict[str, Any]:
    return {"state": step.state, "detail": step.detail, "evidence": dict(step.evidence)}


def run(world, services, phase, step_id, context=None):
    report = world_support.campaign_report()
    outcome = world_support.run_phase(phase, report, context or world_support.context_for(world), services, step_id)
    return phase_outcome(outcome)


@pytest.fixture(scope="module")
def story(tmp_path_factory):
    root = tmp_path_factory.mktemp("w5-story") / "campaign"
    world = world_support.delivered_world(root)
    out: Dict[str, Any] = {"world": world}
    context = world_support.context_for(world)
    tip0 = world_support.git_tip(world)
    out["tip_after_build"] = tip0
    out["build_heads"] = {n: s for n, s in world.bridge.branches.items() if n.startswith("collegue/issue-")}
    r04, r05 = w5.run_improvement_phase, w5.run_incident_phase
    step4, step5 = "R04-improvement", "R05-incident-rollback"

    # 1. refus de R04 : couverture 90 → 80 (le « modèle » fait pourtant un vrai gain de sécurité)
    out["r04_coverage_drop"] = run(
        world, world_support.services_for(world, measure_fn=ScriptedMeasure(measure)), r04, step4, context
    )
    out["tip_after_coverage_refusal"] = world_support.git_tip(world)
    # 2. refus de R04 : revue bloquante (veto)
    out["r04_blocking_review"] = run(
        world, world_support.services_for(world, reviewer=FakeReviewer(blocking=True)), r04, step4, context
    )
    # 3. refus de R04 : le « modèle » rend un export PDF VALIDE aux mauvaises données et affaiblit les tests du projet (qui passent) :
    #    seul le contrat scellé de la tâche 3, rejoué, le voit
    breaker = harness.ImprovementAgent(
        harness.replace_docs(
            {
                "app/export.py": fixture.WRONG_DATA_STAGE_3["app/export.py"],
                "tests/test_export.py": fixture.WEAK_EXPORT_TEST,
            }
        )
    )
    out["r04_broken_contract"] = run(world, world_support.services_for(world, model=breaker), r04, step4, context)
    out["tip_after_refusals"] = world_support.git_tip(world)
    out["prs_after_refusals"] = sorted(world.bridge.prs)
    # 4. R04 réussie : identifiants d'exemple retirés du runbook (gain mesuré par le vrai scan), fusionnée par Phase 5
    out["r04"] = run(world, world_support.services_for(world), r04, step4, context)
    out["context_after_r04"] = dict(context)
    out["tip_after_r04"] = world_support.git_tip(world)
    # 5. R05 : une garde RÉELLE (plafond de lignes) refuse la contribution d'incident → rollback NON exercé
    refused_services = world_support.services_for(world, settings_overrides={"AUTO_MERGE_MAX_LOC": 1})
    out["r05_refused"] = run(world, refused_services, r05, step5, context)
    out["tip_after_r05_refused"] = world_support.git_tip(world)
    out["incident_after_refusal"] = world.manager().get_phase5_incident(world.project_id)

    # 6. R05 : le PROCESSUS MEURT pendant la publication de la PR de revert (après fusion et santé rouge) : l'incident reste ACTIF
    class Crash(BaseException):
        """Mort du processus (pas une ``Exception`` : rien ne la rattrape)."""

    def die_on_revert_pr():
        raise Crash()

    world.bridge.before_create_pr = die_on_revert_pr
    try:
        run(world, world_support.services_for(world), r05, step5, context)
        out["r05_crash"] = {"crashed": False}
    except Crash:
        out["r05_crash"] = {"crashed": True}
    finally:
        world.bridge.before_create_pr = None
    incident = world.manager().get_phase5_incident(world.project_id)
    out["incident_after_crash"] = (incident.state, incident.revision) if incident else None
    # 6b. reprise IMMÉDIATE : le lease de revert persisté n'a pas expiré → l'incident reste actif, R05 n'affirme rien
    out["r05_resume_too_early"] = run(world, world_support.services_for(world), r05, step5, context)
    # 6c. le temps passe (le lease persisté expire) — seule manipulation du scénario, sur la ligne durable de l'incident
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import update

    from collegue.state.models import Phase5Incident

    with world.manager().session() as session:
        session.execute(
            update(Phase5Incident).values(revert_claim_expires_at=datetime.now(timezone.utc) - timedelta(seconds=5))
        )
    # 7. R05 : REPRISE — l'incident durable est réconcilié, rollback prouvé, acquittement CAS, reprise libre
    out["r05_resumed"] = run(world, world_support.services_for(world), r05, step5, context)
    out["incident_after_ack"] = world.manager().get_phase5_incident(world.project_id)
    out["tip_final"] = world_support.git_tip(world)
    out["heads_final"] = {n: world.bridge.branches.get(n) for n in out["build_heads"]}
    # 8. contrat altéré : une source d'oracle scellée est modifiée → plus d'approbation du contenu → R04 ne démarre pas
    manager = world.manager()
    task = manager.get_tasks(world.project_id)[0]
    with manager.session() as session:
        from collegue.state.models import Task

        row = session.get(Task, task.id)
        row.acceptance_test_source = row.acceptance_test_source + "\n# altéré après approbation\n"
    out["r04_tampered_contract"] = run(world, world_support.services_for(world), r04, step4, dict(context))
    return out


def test_the_socle_world_has_three_delivered_tasks_and_untouched_build_heads(story):
    world = story["world"]
    statuses = harness.task_statuses(world)
    assert set(statuses.values()) == {"merged"} and len(statuses) == 3
    assert set(story["build_heads"]) == {"collegue/issue-1", "collegue/issue-2", "collegue/issue-3"}
    assert story["heads_final"] == story["build_heads"], "les têtes BUILD n'ont jamais été réécrites"


@pytest.mark.parametrize(
    "key, needle",
    [
        ("r04_coverage_drop", "couverture"),
        ("r04_blocking_review", "revue"),
        ("r04_broken_contract", "contrat"),
    ],
)
def test_a_refused_improvement_is_incomplete_with_the_real_refusal_and_publishes_nothing(story, key, needle):
    outcome = story[key]
    assert outcome["state"] == "incomplete_validation", outcome
    assert "aucune amélioration promue" in outcome["detail"] and needle in outcome["detail"].lower(), outcome["detail"]
    assert story["tip_after_refusals"] == story["tip_after_build"] and story["prs_after_refusals"] == [101, 102, 103]


def test_r04_delivers_a_merged_improvement_with_a_measured_gain_contracts_replayed_and_distinct_documents(story):
    outcome = story["r04"]
    assert outcome["state"] == "succeeded", outcome
    evidence = outcome["evidence"]
    assert evidence["files"] == [w5.R04_DOC] and not set(evidence["files"]) & set(w5.INCIDENT_DOCS)
    assert evidence["delta"] > 0 and evidence["score_after"] > evidence["score_before"]
    assert evidence["contracts_replayed"] == 3
    assert evidence["merge_sha"] and evidence["tree_after"] != story["world"].bridge.remote.tree_of(
        story["tip_after_build"]
    )
    assert story["tip_after_r04"] != story["tip_after_build"], "livrée = fusionnée (la base a changé)"
    assert story["context_after_r04"]["r04"]["pr_number"] == evidence["pr_number"]


def test_a_guard_refusing_the_incident_contribution_is_preserved_and_the_rollback_is_declared_not_exercised(story):
    outcome = story["r05_refused"]
    assert outcome["state"] == "incomplete_validation", outcome
    assert "rollback" in outcome["detail"] and "non exercé" in outcome["detail"].lower()
    assert "trop volumineux" in outcome["detail"] or "plafond" in outcome["detail"], outcome["detail"]
    assert story["tip_after_r05_refused"] == story["tip_after_r04"], "rien n'a été fusionné"
    assert story["incident_after_refusal"] is None


def test_a_process_death_during_the_revert_leaves_a_durable_active_incident_and_no_success_is_claimed(story):
    assert story["r05_crash"] == {"crashed": True}
    state, _revision = story["incident_after_crash"]
    assert state in {"revert_pending", "revert_in_progress", "health_pending"}, state


def test_resuming_before_the_revert_lease_expires_keeps_the_incident_active_and_proves_nothing(story):
    outcome = story["r05_resume_too_early"]
    assert outcome["state"] == "incomplete_validation", outcome
    assert "ENCORE ACTIF" in outcome["detail"] and "lease" in outcome["detail"], outcome["detail"]


def test_r05_resumes_the_durable_incident_and_proves_the_real_rollback(story):
    outcome = story["r05_resumed"]
    assert outcome["state"] == "succeeded", outcome
    evidence = outcome["evidence"]
    assert (
        evidence["resumed"] is True
        and evidence["injection"]["deterministic"]
        and evidence["injection"]["model_calls"] == 0
    )
    assert evidence["incident_state"] == "recovered" and evidence["health_command_is_independent_probe"]
    assert evidence["tree_after"] == evidence["tree_before"] != evidence["tree_at_incident"], (
        "arbre restauré, régression fusionnée"
    )
    assert evidence["tip_after"] != evidence["tip_before"] and evidence["revert_pr"] > evidence["incident_pr"]
    assert all(evidence["revert_checks"][name][0] == "success" for name in world_support.harness_checks()), (
        "checks requis réels"
    )
    assert evidence["health_after"] == "passed"
    assert evidence["cas_stale_rejected"] and evidence["replay_rejected"], "acquittement CAS : périmé et rejeu refusés"
    assert evidence["recovery_found"] is False and evidence["recovery_continue"] is True
    assert story["incident_after_ack"] is None and story["tip_final"] == evidence["tip_after"]


def test_the_independent_health_probe_really_saw_the_pdf_regression_and_then_the_restoration(story):
    runs = [r for r in story["world"].sandbox.health_runs if r.get("independent_probe")]
    statuses = [r["status"] for r in runs]
    assert "failed" in statuses and statuses[-1] == "passed", statuses
    failed = next(r for r in runs if r["status"] == "failed")
    assert "legal_notice_present" in failed["output"], "la régression observée est la mention légale absente du PDF"


def test_the_incident_injection_is_announced_deterministic_and_costs_nothing(story):
    agent = w5.DeterministicIncidentAgent()
    assert "INJECTION DÉTERMINISTE" in agent.announced and agent.budget_enforcement == "test-double"
    assert sorted(story["r05_resumed"]["evidence"]["injection"]["files"]) == sorted(w5.INCIDENT_DOCS)


def test_an_altered_sealed_contract_blocks_the_phase_before_any_emission(story):
    outcome = story["r04_tampered_contract"]
    assert outcome["state"] == "failed" and "non approuvé" in outcome["detail"].lower(), outcome
    assert story["world"].bridge.branches["main"] == story["tip_final"], "rien n'a été publié après l'altération"


def test_the_incident_phase_without_a_delivered_improvement_is_not_exercised(tmp_path):
    report = world_support.campaign_report()
    services = SimpleNamespace(manager=lambda: None)
    step = report.step("R05-incident-rollback")
    report.run("R05-incident-rollback", lambda s: w5.run_incident_phase(report, {}, services))  # type: ignore[arg-type]
    assert step.state == "incomplete_validation" and "rollback non exercé" in step.detail


def test_an_injection_without_effect_is_incomplete_not_a_fake_incident(tmp_path):
    agent = w5.DeterministicIncidentAgent()
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "export_header.md").write_text("Rapport sans mention légale\n", encoding="utf-8")
    (tmp_path / "docs" / "deploiement.md").write_text("# Déploiement\nAucun identifiant ici.\n", encoding="utf-8")

    result = agent.implement_issue(SimpleNamespace(path=str(tmp_path)), SimpleNamespace(title="incident"))

    assert result.success is False and result.files_changed == () and agent.touched == []


def test_the_injection_removes_only_the_notice_line_and_the_fake_credentials(tmp_path):
    agent = w5.DeterministicIncidentAgent()
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "export_header.md").write_text(fixture.HEADER_DOC, encoding="utf-8")
    (tmp_path / "docs" / "deploiement.md").write_text(fixture.SOCLE_DEPLOY_DOC, encoding="utf-8")

    result = agent.implement_issue(SimpleNamespace(path=str(tmp_path)), SimpleNamespace(title="incident"))

    assert sorted(result.files_changed) == sorted(w5.INCIDENT_DOCS) and result.cost_usd == 0.0
    header = (tmp_path / "docs" / "export_header.md").read_text(encoding="utf-8")
    deploy = (tmp_path / "docs" / "deploiement.md").read_text(encoding="utf-8")
    assert business.LEGAL_NOTICE not in header and "Auditeur" in header
    assert not w5.FAKE_CREDENTIAL_LINE.search(deploy) and "alembic upgrade head" in deploy
