"""Campagne W4 — séquencement de l'invocation réelle (doubles : adaptateur, commandes ; aucun réseau, aucun modèle).

Ce que ces tests prouvent : l'ORDRE (garde fixture → base éphémère → draft à TROIS tâches → approbation → label → sync → UN run
produit → clone final), le nettoyage TOUJOURS exécuté, l'absence de retry, la traduction des arrêts (budget/échéance, produit non
terminé) en états de rapport distincts, et que l'échéance globale tue la commande en cours. Ils ne prouvent PAS le déroulé contre le
GitHub réel (aucun lancement dans cette vague).
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from collegue.pilot import w4_business as business
from collegue.pilot.w4_business import (
    STEP_BUDGET_STOP,
    STEP_FAILED,
    STEP_INCOMPLETE,
    STEP_NOT_EXECUTED,
    STEP_SUCCEEDED,
    BudgetStop,
    CampaignReport,
)

ENV = {
    "GITHUB_TOKEN": "ghp_unit_test_token_value",
    "GITHUB_RUN_ID": "777",
    "GITHUB_RUN_ATTEMPT": "1",
    "STATE_DATABASE_URL": "sqlite:////tmp/unit-w4.sqlite3",
}


class FakeAdapter:
    """Même interface que :class:`NightlyAdapter`, journalise l'ordre des appels."""

    def __init__(self, tmp_path, *, tasks=3, issues=3, stop_reason="completed", fail_at=None):
        self.config = business.business_config({**ENV, "COLLEGUE_NIGHTLY_MANIFEST": str(tmp_path / "manifest.json")})
        self.calls = []
        self.tasks, self.issues, self.stop_reason, self.fail_at = tasks, issues, stop_reason, fail_at
        self.tmp_path = tmp_path
        branches = SimpleNamespace(get_branch_sha=lambda o, r, b: ("a" if b == self.config.base_branch else "b") * 40)
        self.inner = SimpleNamespace(clients=SimpleNamespace(branches=branches))

    def _maybe_fail(self, name):
        if self.fail_at == name:
            raise RuntimeError(f"panne simulée à {name}")

    def guard_fixture(self):
        self.calls.append("guard")
        self._maybe_fail("guard")
        return business.FIXTURE_SEED_SHA

    def create_base(self, manifest):
        self.calls.append("base")
        self._maybe_fail("base")
        return "c" * 40

    def create_label(self, manifest):
        self.calls.append("label")

    def product(self, *args, accepted_codes=(0,)):
        verb = args[1] if args and args[0] == "plan" else "run"
        self.calls.append(verb)
        self._maybe_fail(verb)
        if verb == "draft":
            assert "--nightly-exact-task-count" in args and args[args.index("--nightly-exact-task-count") + 1] == "3"
            assert business.BUSINESS_PROBLEM in args
            return {"action": "draft", "project_id": 9, "plan_hash": "f" * 64, "task_count": self.tasks}
        if verb == "approve":
            return {"action": "approve", "project_id": 9, "plan_hash": "f" * 64, "task_count": 3}
        if verb == "sync":
            return {"action": "sync", "issues": [{"issue_number": 10 + i} for i in range(self.issues)]}
        assert "--execute" in args and args.count("--execute") == 1 and accepted_codes == (0, 1, 2, 3, 4, 5)
        return {"stop_reason": self.stop_reason, "opened_prs": [1, 2, 3]}

    def clone(self, base_sha):
        self.calls.append("clone")
        folder = self.tmp_path / f"clone-{len(self.calls)}" / "fixture"
        folder.mkdir(parents=True)
        return str(folder)

    def cleanup(self):
        self.calls.append("cleanup")


def launch(tmp_path, **kwargs):
    adapter = FakeAdapter(tmp_path, **kwargs)
    report = CampaignReport("campaign", "unit")
    return adapter, report


def test_business_base_branch_lives_under_the_dedicated_prefix():
    config = business.business_config(ENV)
    assert config.base_branch == "collegue-business/777-1" and config.repository == business.FIXTURE_REPOSITORY
    assert config.repository_id == business.FIXTURE_REPOSITORY_ID and config.seed_sha == business.FIXTURE_SEED_SHA


def test_the_launch_runs_the_public_steps_in_order_with_one_product_run_and_always_cleans_up(tmp_path):
    adapter, report = launch(tmp_path)

    context = business.launch_campaign(report, adapter=adapter, env=ENV)

    assert adapter.calls == ["guard", "base", "draft", "approve", "label", "sync", "clone", "run", "clone", "cleanup"]
    assert adapter.calls.count("run") == 1, "aucun retry payant : une seule exécution du produit"
    assert context["stop_reason"] == "completed" and context["project_id"] == 9
    assert Path(context["final_checkout"]).name == "fixture" and context["final_sha"] == "a" * 40
    assert report.facts["launch"]["issue_numbers"] == [10, 11, 12]


@pytest.mark.parametrize("failing", ["guard", "base", "draft", "approve", "sync", "run"])
def test_any_failure_still_cleans_up_and_never_retries(tmp_path, failing):
    adapter, report = launch(tmp_path, fail_at=failing)

    with pytest.raises(RuntimeError, match="panne simulée"):
        business.launch_campaign(report, adapter=adapter, env=ENV)

    assert adapter.calls[-1] == "cleanup" and adapter.calls.count(failing if failing != "run" else "run") == 1


def test_a_plan_without_exactly_three_tasks_or_issues_is_refused_before_any_paid_run(tmp_path):
    adapter, report = launch(tmp_path, tasks=2)
    with pytest.raises(RuntimeError, match="trois tâches"):
        business.launch_campaign(report, adapter=adapter, env=ENV)
    assert "run" not in adapter.calls and adapter.calls[-1] == "cleanup"
    (tmp_path / "b").mkdir()
    adapter, report = launch(tmp_path / "b", issues=2)
    with pytest.raises(RuntimeError, match="trois issues"):
        business.launch_campaign(report, adapter=adapter, env=ENV)
    assert "run" not in adapter.calls and adapter.calls[-1] == "cleanup"


@pytest.mark.parametrize("stop", ["paused_budget", "deadline_reached"])
def test_a_budget_or_deadline_stop_of_the_product_is_a_budget_stop_not_a_failure(tmp_path, stop):
    adapter, report = launch(tmp_path, stop_reason=stop)

    with pytest.raises(BudgetStop, match=stop):
        business.launch_campaign(report, adapter=adapter, env=ENV)

    assert report.facts["launch"]["stop_reason"] == stop and adapter.calls[-1] == "cleanup"
    assert adapter.calls.count("clone") == 1, "aucun clone final : rien à vérifier après un arrêt budget"


def test_an_unfinished_product_run_is_a_failure_not_a_success(tmp_path):
    adapter, report = launch(tmp_path, stop_reason="awaiting_merge")
    with pytest.raises(RuntimeError, match="awaiting_merge"):
        business.launch_campaign(report, adapter=adapter, env=ENV)


# ── run_campaign : R01 / R02 / R03 ───────────────────────────────────────────────────────────────────────────────────────


def validated_preflight():
    report = CampaignReport("preflight", "unit")
    report.declare("P01", "ok")
    report.run("P01", lambda s: None)
    return report


GOOD_COUNTERS = dict(
    scope="project:9", strict=True, cap_usd=2.0, cap_tokens=250_000, consumed_micro_usd=1_000_000, consumed_tokens=90_000,
    reserved_micro_usd=0, reserved_tokens=0, unknown_micro_usd=0, unknown_tokens=0, blocked_reason=None, revision=9,
)  # fmt: skip


def test_a_fully_successful_campaign_validates_only_after_the_business_check_and_the_registry(tmp_path):
    adapter, _ = launch(tmp_path)

    def verify(report, context):
        report.step("R02-business").evidence["checked"] = context["final_checkout"]

    report = business.run_campaign(
        ENV,
        preflight=validated_preflight(),
        launch=lambda r: business.launch_campaign(r, adapter=adapter, env=ENV),
        verify=verify,
        read_registry=lambda ctx: GOOD_COUNTERS,
    )

    assert [s.state for s in report.steps if s.id.startswith("R")] == [STEP_SUCCEEDED] * 3
    assert report.verdict() == "validated" and report.facts["registry_final"]["consumed_tokens"] == 90_000


def test_a_failed_business_check_makes_the_campaign_fail_and_leaves_the_registry_step_unexecuted(tmp_path):
    adapter, _ = launch(tmp_path)

    def verify(report, context):
        raise AssertionError("assertions métier fausses : write:pdf_text_has_the_persisted_audit_data")

    report = business.run_campaign(
        ENV,
        preflight=validated_preflight(),
        launch=lambda r: business.launch_campaign(r, adapter=adapter, env=ENV),
        verify=verify,
        read_registry=lambda ctx: GOOD_COUNTERS,
    )

    assert report.step("R01-run").state == STEP_SUCCEEDED and report.step("R02-business").state == STEP_FAILED
    assert report.step("R03-registry").state == STEP_NOT_EXECUTED and report.verdict() == "failed"


def test_a_budget_stop_is_reported_as_such_and_still_records_the_registry_stop_point(tmp_path):
    adapter, _ = launch(tmp_path, stop_reason="paused_budget")
    consumed = dict(GOOD_COUNTERS, consumed_tokens=249_000)

    report = business.run_campaign(
        ENV,
        preflight=validated_preflight(),
        launch=lambda r: business.launch_campaign(r, adapter=adapter, env=ENV),
        verify=lambda r, c: None,
        read_registry=lambda ctx: consumed,
    )

    assert report.step("R01-run").state == STEP_BUDGET_STOP
    assert report.step("R02-business").state == STEP_NOT_EXECUTED, "pas de vérification métier d'un livrable inachevé"
    assert (
        report.step("R03-registry").state == STEP_SUCCEEDED
        and report.facts["registry_final"]["consumed_tokens"] == 249_000
    )
    assert report.verdict() == "budget_stop" and report.exit_code() == 4


def test_missing_verification_or_registry_is_an_incomplete_validation_never_a_success(tmp_path):
    adapter, _ = launch(tmp_path)
    report = business.run_campaign(
        ENV, preflight=validated_preflight(), launch=lambda r: business.launch_campaign(r, adapter=adapter, env=ENV)
    )
    assert report.step("R02-business").state == STEP_INCOMPLETE and report.verdict() == "incomplete_validation"


# ── vérification en conteneur et échéance globale ─────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "status, state",
    [("passed", STEP_SUCCEEDED), ("failed", STEP_FAILED), ("incomplete", STEP_INCOMPLETE)],
)
def test_the_container_verification_maps_its_status_and_removes_the_clone(tmp_path, monkeypatch, status, state):
    clone_parent = tmp_path / "clone"
    checkout = clone_parent / "fixture"
    checkout.mkdir(parents=True)
    seen = {}

    def fake_verify(path, **kwargs):
        seen.update(path=path, **{k: v for k, v in kwargs.items() if k in {"python", "database_dir"}})
        return business.BusinessObservation(
            status, {"x": status == "passed"}, {"o": 1}, [] if status == "passed" else ["x"], "détail"
        )

    monkeypatch.setattr(business, "verify_business_checkout", fake_verify)
    report = CampaignReport("campaign", "unit")
    report.declare("R02-business", "métier")

    report.run(
        "R02-business", lambda s: business.verify_in_container(report, {"final_checkout": str(checkout)}, env={})
    )

    assert report.step("R02-business").state == state
    assert seen["python"] == "python" and seen["database_dir"] == "/scratch"
    assert not clone_parent.exists(), "le clone généré est supprimé après la vérification"


def test_the_global_deadline_forbids_new_commands_and_kills_the_running_one_with_its_process_group(tmp_path):
    expired = business.bounded_command_runner(time.monotonic() - 1)
    with pytest.raises(BudgetStop, match="avant le lancement"):
        expired(["echo", "ne doit jamais tourner"])

    marker = tmp_path / "survivor"
    child = tmp_path / "child.py"
    child.write_text(
        f"import pathlib, time\ntime.sleep(3)\npathlib.Path({str(marker)!r}).write_text('x')\n", encoding="utf-8"
    )
    parent = tmp_path / "parent.py"
    parent.write_text(
        f"import subprocess, sys, time\nsubprocess.Popen([sys.executable, {str(child)!r}])\ntime.sleep(30)\n",
        encoding="utf-8",
    )
    runner = business.bounded_command_runner(time.monotonic() + 0.3, grace=0.2)
    started = time.monotonic()

    with pytest.raises(BudgetStop, match="groupe de processus"):
        runner([sys.executable, str(parent)])

    assert time.monotonic() - started < 10
    time.sleep(3.5)
    assert not marker.exists(), "aucun enfant n'a survécu à l'échéance (rien ne continue à tourner hors surveillance)"


def test_a_command_finishing_before_the_deadline_returns_its_streams():
    runner = business.bounded_command_runner(time.monotonic() + 30)
    result = runner([sys.executable, "-c", "print('ok')"])
    assert (result.returncode, result.stdout.strip()) == (0, "ok")
