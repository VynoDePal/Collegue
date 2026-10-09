"""Campagne métier W4 DÉTERMINISTE, de bout en bout (entrées publiques du produit, doubles aux seules frontières externes).

Un SEUL déroulé complet sert tous les constats (fixture de module) : plan → approbation → trois tâches dépendantes (dont une fusion
confirmée mais resynchronisation interrompue, reprise sans seconde fusion) → vérification métier → amélioration réelle (lint) →
incident contrôlé et rollback Phase 5 → acquittement → registre. Les états de rapport sont asserts ; aucun skip, aucune dérogation.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
import w4_business_campaign as campaign
import w4_business_fixture as fixture

from collegue.pilot import w4_business as business

pytestmark = [pytest.mark.asyncio(loop_scope="module"), pytest.mark.slow]


def _dump(name: str, report) -> None:
    folder = os.environ.get("W4_BUSINESS_REPORT_DIR")
    if folder:  # preuves rejouables : écriture OPT-IN (jamais dans le dépôt)
        Path(folder, f"{name}.json").write_text(report.to_json(), encoding="utf-8")
        Path(folder, f"{name}.txt").write_text(report.to_human(), encoding="utf-8")


@pytest.fixture(scope="module")
async def main_report(tmp_path_factory):
    report = await campaign.run_deterministic_campaign(tmp_path_factory.mktemp("w4-main") / "campaign")
    _dump("campaign-deterministic", report)
    return report


@pytest.fixture(scope="module")
async def negative_report(tmp_path_factory):
    report = await campaign.run_negative_witnesses(tmp_path_factory.mktemp("w4-negative") / "campaign")
    _dump("campaign-negative", report)
    return report


def evidence(report, step_id):
    return report.step(step_id).evidence


async def test_every_step_succeeds_and_the_report_separates_the_five_states(main_report):
    machine = main_report.to_machine()

    assert machine["verdict"] == "validated" and machine["exit_code"] == 0, main_report.to_human()
    assert machine["counts"] == {
        "succeeded": 12,
        "not_executed": 0,
        "budget_stop": 0,
        "failed": 0,
        "incomplete_validation": 0,
    }
    assert [s["state"] for s in machine["steps"]] == ["succeeded"] * 12
    assert main_report.facts["llm_calls_emitted"] == 0 and main_report.facts["billable_actions_emitted"] == 0
    assert (
        main_report.facts["simulated_steps"] == ["D08-operator-merge"]
        and evidence(main_report, "D08-operator-merge")["simulated"]
    )


async def test_three_dependent_tasks_are_planned_with_sealed_oracles_at_plan_time(main_report):
    plan = evidence(main_report, "D01-plan")
    assert plan["planning_calls"] == ["spec", "decompose", "qa", "qa", "qa"]
    assert plan["oracle_fingerprints"] == main_report.facts["oracle_sources_sha256"]
    assert len(set(plan["oracle_fingerprints"].values())) == 3


async def test_every_oracle_was_red_by_assertion_before_and_green_after_with_the_same_fingerprint(main_report):
    for number in (1, 2, 3):
        step = evidence(main_report, {1: "D03-task-1", 2: "D04-interrupted-sync", 3: "D05-restart-resume"}[number])
        current = next(o for o in step["oracles"] if o["role"] == "current")
        assert current["preimage"]["status"] == "red-assertion"
        assert current["preimage"]["assertion_failures"] >= 1 and current["preimage"]["errors"] == 0
        assert current["preimage"]["collection_errors"] == 0 and current["preimage"]["skipped"] == 0
        assert current["candidate"]["status"] == "green" and current["candidate"]["failed"] == 0
        assert current["source_sha256"] == main_report.facts["oracle_sources_sha256"][str(number)]


async def test_delivered_contracts_are_replayed_task_after_task(main_report):
    roles = [sorted(o["role"] for o in evidence(main_report, "D03-task-1")["oracles"])]
    assert roles == [["current"]]
    assert sorted(o["role"] for o in evidence(main_report, "D04-interrupted-sync")["oracles"]) == [
        "current",
        "delivered",
    ]
    assert sorted(o["role"] for o in evidence(main_report, "D05-restart-resume")["oracles"]) == [
        "current",
        "delivered",
        "delivered",
    ]


async def test_dependencies_are_integrated_in_the_base_before_the_next_task_starts(main_report):
    started = set(evidence(main_report, "D05-restart-resume")["started_from_files"])
    assert set(fixture.stage_files(2)) <= started, "la tâche 3 démarre sur main qui contient les tâches 1 ET 2"
    assert evidence(main_report, "D05-restart-resume")["merge_puts_for_pr_102"] == 1


async def test_an_interrupted_resync_is_a_durable_stop_and_the_resume_never_merges_twice(main_report):
    stop = evidence(main_report, "D04-interrupted-sync")
    assert stop["stop_reason"] == "merge_sync_pending" and stop["cycle_state"] == "merged_unsynced"
    assert stop["agent_calls"] == 2 and stop["checkout_head"] != stop["remote_tip"]
    assert main_report.facts["merge_requests"].count("/repos/fixture/fixture/pulls/102/merge") == 1


async def test_the_merged_main_passes_every_business_observation_with_a_real_pdf_reader(main_report):
    business_step = evidence(main_report, "D06-business")
    assert business_step["status"] == "passed" and all(business_step["checks"].values())
    seen = business_step["observations"]
    assert seen["write.db_existed_before"] is False and seen["write.alembic_version"] == ["0001"]
    assert seen["write.pdf_reader"].startswith("pypdf") and seen["write.pdf_missing_data"] == []
    assert seen["write.pdf_raw_bytes_contain_title"] is False
    assert seen["reread.reread_status"] == 200, "l'audit survit à un redémarrage du process"


async def test_the_improvement_is_real_policy_bound_and_regression_free(main_report):
    improve = evidence(main_report, "D07-improve")
    assert improve["dimension"] and improve["delta"] > 0 and "interdit à l'auto-merge" in improve["policy_refusal"]
    assert improve["main_tip_unchanged"] == evidence(main_report, "D08-operator-merge")["tip_before"]
    result = evidence(main_report, "D09-no-regression")
    before, after = result["metrics_before"], result["metrics_after"]
    assert after["tests_passed"] and after["coverage_pct"] >= before["coverage_pct"]
    assert after["lint_violations"] < before["lint_violations"] and after["composite"] > before["composite"]
    assert result["status"] == "passed"


async def test_the_incident_is_a_real_behavioural_regression_rolled_back_to_the_exact_prior_tree(main_report):
    incident = evidence(main_report, "D10-incident-rollback")
    assert incident["stop_reason"] == "auto_revert_recovered" and incident["incident_state"] == "recovered"
    assert incident["tree_after"] == incident["tree_before"] and incident["tip_after"] != incident["tip_before"]
    first, last = incident["health_runs"][0], incident["health_runs"][-1]
    assert first["status"] == "failed" and set(first["failed_checks"]) == {
        "write:legal_notice_present",
        "reread:legal_notice_present",
    }
    assert fixture.LEGAL_NOTICE not in first["pdf_text_excerpt"] and fixture.LEGAL_NOTICE in last["pdf_text_excerpt"]
    assert incident["status"] == "passed" and all(incident["checks"].values()), (
        "comportement métier restauré après le rollback"
    )


async def test_the_recovered_incident_needs_an_acknowledgement_and_then_releases_the_next_run(main_report):
    ack = evidence(main_report, "D11-acknowledge")
    assert ack["recovery_found"] is False and ack["recovery_continue"] is True


async def test_the_registry_enforces_the_global_envelope_and_leaves_nothing_reserved_or_unknown(main_report):
    final = main_report.facts["registry"]["final"]
    assert final["cap_usd"] == 2.0 and final["cap_tokens"] == 250_000 and final["strict"] is True
    assert final["reserved_tokens"] == final["unknown_tokens"] == 0 and final["blocked_reason"] is None
    assert 0 < final["consumed_tokens"] < 250_000 and 0 < final["consumed_micro_usd"] < 2_000_000
    series = [
        main_report.facts["registry"][k]["consumed_tokens"]
        for k in ("after-plan", "after-task-1", "after-interrupted-sync", "after-task-3", "after-improvement")
    ]
    assert series == sorted(series) and len(set(series)) == len(series), (
        "la dépense croît à chaque étape et jamais ailleurs"
    )
    assert (
        main_report.facts["registry"]["after-interrupted-sync"]["consumed_tokens"]
        - main_report.facts["registry"]["after-task-1"]["consumed_tokens"]
        == 14_500
    ), "la tâche 2 est comptée UNE fois (interrompue puis reprise)"


async def test_the_report_is_serialisable_machine_json_and_readable_human_text(main_report):
    machine = json.loads(main_report.to_json())
    assert machine["schema"] == business.REPORT_SCHEMA and machine["campaign_id"] == "w4-business-deterministic"
    assert "validated" in main_report.to_human() and "SIMULÉE" in main_report.to_human()
    assert all(len(v) == 64 for v in machine["facts"]["oracle_sources_sha256"].values())


# ── témoins négatifs et arrêt budget ───────────────────────────────────────────────────────────────────────────────────


async def test_a_valid_pdf_with_the_wrong_data_is_never_delivered_even_though_its_own_tests_pass(negative_report):
    assert negative_report.verdict() == "validated", negative_report.to_human()
    refused = evidence(negative_report, "N02-wrong-data-pdf-refused")
    assert refused["task_status"] not in {"merged", "in_review"}
    assert (
        "ORACLE D'ACCEPTATION REFUSÉ" in refused["task_error_excerpt"]
        and "AssertionError" in refused["task_error_excerpt"]
    )
    assert evidence(negative_report, "N03-main-untouched")["prs"] == [101, 102]


async def test_a_budget_stop_is_reported_distinctly_leaves_the_rest_unexecuted_and_spends_nothing_more(tmp_path):
    report = await campaign.run_budget_stop_campaign(tmp_path / "budget")

    assert [s.state for s in report.steps] == ["succeeded", "succeeded", "budget_stop", "not_executed"], (
        report.to_human()
    )
    assert report.verdict() == "budget_stop" and report.exit_code() == 4
    stop = report.step("B03-task-3-refused").evidence
    assert stop["restarts"] == 2 and stop["stop_reason"] == "paused_budget"
    assert stop["registry_before"]["consumed_tokens"] == stop["registry_after"]["consumed_tokens"]
    assert (
        report.facts["stop_point"] == "B03-task-3-refused"
        and "plafond de tokens" in report.step("B03-task-3-refused").detail
    )
