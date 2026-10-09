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


def test_the_launch_runs_the_public_steps_in_order_with_one_product_run_and_leaves_the_resources_alive(tmp_path):
    adapter, report = launch(tmp_path)

    context = business.launch_campaign(report, adapter=adapter, env=ENV)

    assert adapter.calls == ["guard", "base", "draft", "approve", "label", "sync", "clone", "run", "clone"]
    assert adapter.calls.count("run") == 1, "aucun retry payant : une seule exécution du produit"
    assert context["stop_reason"] == "completed" and context["project_id"] == 9
    assert Path(context["final_checkout"]).name == "fixture" and context["final_sha"] == "a" * 40
    assert report.facts["launch"]["issue_numbers"] == [10, 11, 12]


@pytest.mark.parametrize("failing", ["guard", "base", "draft", "approve", "sync", "run"])
def test_any_failure_never_retries_and_never_cleans_up_early(tmp_path, failing):
    adapter, report = launch(tmp_path, fail_at=failing)

    with pytest.raises(RuntimeError, match="panne simulée"):
        business.launch_campaign(report, adapter=adapter, env=ENV)

    assert "cleanup" not in adapter.calls, "le nettoyage n'a plus lieu dans le lancement : il suit TOUTES les phases"
    assert adapter.calls.count(failing if failing != "run" else "run") == 1


def test_a_plan_without_exactly_three_tasks_or_issues_is_refused_before_any_paid_run(tmp_path):
    adapter, report = launch(tmp_path, tasks=2)
    with pytest.raises(RuntimeError, match="trois tâches"):
        business.launch_campaign(report, adapter=adapter, env=ENV)
    assert "run" not in adapter.calls and "cleanup" not in adapter.calls
    (tmp_path / "b").mkdir()
    adapter, report = launch(tmp_path / "b", issues=2)
    with pytest.raises(RuntimeError, match="trois issues"):
        business.launch_campaign(report, adapter=adapter, env=ENV)
    assert "run" not in adapter.calls and "cleanup" not in adapter.calls


@pytest.mark.parametrize("stop", ["paused_budget", "deadline_reached"])
def test_a_budget_or_deadline_stop_of_the_product_is_a_budget_stop_not_a_failure(tmp_path, stop):
    adapter, report = launch(tmp_path, stop_reason=stop)

    with pytest.raises(BudgetStop, match=stop):
        business.launch_campaign(report, adapter=adapter, env=ENV)

    assert report.facts["launch"]["stop_reason"] == stop and "cleanup" not in adapter.calls
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

    states = {s.id: s.state for s in report.steps if s.id.startswith("R")}
    assert [states[i] for i in ("R01-run", "R02-business", "R03-registry")] == [STEP_SUCCEEDED] * 3
    assert report.facts["registry_final"]["consumed_tokens"] == 90_000
    # Un BUILD réussi n'est PAS la validation finale : la portée annoncée contient aussi l'amélioration et l'incident /
    # rollback, déclarés requis, jamais joués par ce lancement et rapportés avec leur point d'arrêt exact.
    assert states["R04-improvement"] == STEP_INCOMPLETE and states["R05-incident-rollback"] == STEP_NOT_EXECUTED
    assert "point d'arrêt documenté" in report.step("R04-improvement").detail
    assert report.verdict() == "incomplete_validation" and report.exit_code() == 3
    assert report.facts["scope"]["not_wired"] == ["improvement", "incident_rollback"]


@pytest.mark.parametrize(
    "outcome, state, verdict",
    [
        ("failed", STEP_FAILED, "failed"),
        ("incomplete", STEP_INCOMPLETE, "incomplete_validation"),
        ("budget", STEP_BUDGET_STOP, "budget_stop"),
    ],
)
def test_a_stopped_business_check_still_reads_the_registry_of_the_project_that_spent(tmp_path, outcome, state, verdict):
    """Une vérification métier qui échoue, est incomplète ou dépasse l'échéance ne fait pas disparaître la dépense réalisée."""
    adapter, _ = launch(tmp_path)
    seen = []

    def verify(report, context):
        if outcome == "failed":
            raise AssertionError("assertions métier fausses : write:pdf_text_has_the_persisted_audit_data")
        if outcome == "incomplete":
            raise business.IncompleteValidation("Docker indisponible")
        raise BudgetStop("échéance globale atteinte")

    def registry(context):
        seen.append(dict(context))
        return GOOD_COUNTERS

    report = business.run_campaign(
        ENV,
        preflight=validated_preflight(),
        launch=lambda r: business.launch_campaign(r, adapter=adapter, env=ENV),
        verify=verify,
        read_registry=registry,
    )

    assert report.step("R01-run").state == STEP_SUCCEEDED and report.step("R02-business").state == state
    assert {c["project_id"] for c in seen} == {9} and len(seen) >= 2, (
        "le MÊME projet est relu après chaque phase et à la sortie"
    )
    assert {"after-R01-run", "after-R02-business", "exit"} <= set(report.facts["registry"]), report.facts[
        "registry"
    ].keys()
    assert report.step("R03-registry").state == STEP_SUCCEEDED
    assert report.facts["registry_final"]["consumed_tokens"] == 90_000
    assert report.verdict() == verdict, "le verdict d'origine est conservé"
    for step_id in ("R04-improvement", "R05-incident-rollback"):
        assert report.step(step_id).state == STEP_NOT_EXECUTED


@pytest.mark.parametrize("outcome", ["failed", "incomplete", "budget"])
def test_an_unreadable_registry_after_a_verification_stop_keeps_the_stop_and_names_the_missing_proof(tmp_path, outcome):
    adapter, _ = launch(tmp_path)
    raised = {
        "failed": AssertionError("assertions métier fausses"),
        "incomplete": business.IncompleteValidation("Docker indisponible"),
        "budget": BudgetStop("échéance globale atteinte"),
    }[outcome]

    def verify(report, context):
        raise raised

    def unreadable(context):
        raise OSError("base illisible")

    report = business.run_campaign(
        ENV,
        preflight=validated_preflight(),
        launch=lambda r: business.launch_campaign(r, adapter=adapter, env=ENV),
        verify=verify,
        read_registry=unreadable,
    )

    expected = {"failed": "failed", "incomplete": "incomplete_validation", "budget": "budget_stop"}[outcome]
    assert report.verdict() == expected
    assert report.step("R03-registry").state == STEP_INCOMPLETE
    assert "dépense non établie" in report.step("R03-registry").detail and "registry_final" not in report.facts


def test_the_registry_is_reread_from_a_second_real_ledger_instance_after_any_verification_stop(tmp_path):
    """Vrai registre SQLite : consommation persistée, relue depuis une AUTRE instance par le vrai ``registry_reader``."""
    from collegue.state import ProjectStateManager

    url = f"sqlite:///{tmp_path / 'state.sqlite3'}"
    manager = ProjectStateManager.from_url(url, create=True)
    for _ in range(8):  # project_id 9 : celui que le faux adaptateur « crée »
        project_id = manager.create_project(name="registre", spec="x")
    assert project_id == 8
    project_id = manager.create_project(name="registre", spec="x")
    assert project_id == 9
    ledger = manager.budget_ledger
    scope = ledger.scope_for_project(9, max_cost_usd=2, max_tokens=250000, strict=True)
    reservation = ledger.reserve(scope.scope_key, usd=0.2, tokens=1000)
    ledger.commit(reservation.reservation_id, usd=0.125, tokens=750)
    adapter, _ = launch(tmp_path)

    def verify(report, context):
        raise business.IncompleteValidation("Docker indisponible")

    env = {**ENV, "STATE_DATABASE_URL": url}
    report = business.run_campaign(
        env,
        preflight=validated_preflight(),
        launch=lambda r: business.launch_campaign(r, adapter=adapter, env=env),
        verify=verify,
        read_registry=business.registry_reader(env),
    )

    counters = report.facts["registry_final"]
    assert (counters["consumed_micro_usd"], counters["consumed_tokens"]) == (125_000, 750)
    assert report.step("R03-registry").state == STEP_SUCCEEDED and report.verdict() == "incomplete_validation"


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


def test_the_scope_steps_are_declared_required_and_stay_unexecuted_after_an_upstream_stop(tmp_path):
    adapter, _ = launch(tmp_path, stop_reason="paused_budget")

    report = business.run_campaign(
        ENV,
        preflight=validated_preflight(),
        launch=lambda r: business.launch_campaign(r, adapter=adapter, env=ENV),
        verify=lambda r, c: None,
        read_registry=lambda ctx: GOOD_COUNTERS,
    )

    for step_id in ("R04-improvement", "R05-incident-rollback"):
        assert report.step(step_id).required and report.step(step_id).state == STEP_NOT_EXECUTED


# ── arrêt budget : l'identité du projet survit, le MÊME registre est lu (chemin composé launch + run) ─────────────────────


class RecordingRegistry:
    """Faux ``ProjectStateManager`` : ne répond que pour le projet demandé et journalise la lecture."""

    def __init__(self):
        self.read = []
        registry = self

        class Ledger:
            def snapshot_for_project(self, project_id):
                registry.read.append(project_id)
                return SimpleNamespace(
                    scope_key=f"project:{project_id}", strict=True, cap_micro_usd=2_000_000, cap_tokens=250_000,
                    consumed_micro_usd=1_900_000, consumed_tokens=240_000, reserved_micro_usd=0, reserved_tokens=0,
                    unknown_micro_usd=0, unknown_tokens=0, blocked_reason=None, revision=3,
                )  # fmt: skip

        self.budget_ledger = Ledger()


@pytest.mark.parametrize("stop", ["paused_budget", "deadline_reached"])
def test_a_budget_stop_keeps_the_project_identity_and_reads_the_same_registry(tmp_path, monkeypatch, stop):
    registry = RecordingRegistry()
    monkeypatch.setattr("collegue.state.ProjectStateManager.from_url", staticmethod(lambda url, **kw: registry))
    adapter, _ = launch(tmp_path, stop_reason=stop)

    report = business.run_campaign(
        ENV,
        preflight=validated_preflight(),
        launch=lambda r: business.launch_campaign(r, adapter=adapter, env=ENV),
        verify=lambda r, c: None,
        read_registry=business.registry_reader(ENV),
    )

    assert set(registry.read) == {9}, "le registre lu est celui du projet créé par la planification"
    assert report.step("R01-run").state == STEP_BUDGET_STOP
    assert report.step("R03-registry").state == STEP_SUCCEEDED
    assert (
        report.facts["registry_final"]["scope"] == "project:9"
        and report.facts["registry_final"]["consumed_tokens"] == 240_000
    )
    assert report.facts["launch"]["project_id"] == 9 and report.verdict() == "budget_stop" and report.exit_code() == 4


def test_an_unreadable_registry_keeps_the_original_stop_and_states_the_missing_proof(tmp_path):
    adapter, _ = launch(tmp_path, stop_reason="paused_budget")

    def unreadable(context):
        raise KeyError("project_id")  # n'importe quelle panne de lecture

    report = business.run_campaign(
        ENV,
        preflight=validated_preflight(),
        launch=lambda r: business.launch_campaign(r, adapter=adapter, env=ENV),
        verify=lambda r, c: None,
        read_registry=unreadable,
    )

    assert report.verdict() == "budget_stop", "l'arrêt d'origine n'est pas masqué par la panne de lecture"
    assert report.step("R03-registry").state == STEP_INCOMPLETE
    assert (
        "dépense non établie" in report.step("R03-registry").detail
        and "aucun zéro" in report.step("R03-registry").detail
    )
    assert "registry_final" not in report.facts, "aucun compteur inventé"


@pytest.mark.parametrize(
    "failing, project", [("guard", None), ("base", None), ("draft", None), ("approve", 9), ("sync", 9), ("run", 9)]
)
def test_an_error_exit_keeps_whatever_identity_was_created_and_never_invents_a_zero_spend(tmp_path, failing, project):
    adapter, _ = launch(tmp_path, fail_at=failing)
    seen = []

    def registry(context):
        seen.append(dict(context))
        return GOOD_COUNTERS

    report = business.run_campaign(
        ENV,
        preflight=validated_preflight(),
        launch=lambda r: business.launch_campaign(r, adapter=adapter, env=ENV),
        verify=lambda r, c: None,
        read_registry=registry,
    )

    assert report.step("R01-run").state == STEP_FAILED and report.verdict() == "failed"
    if project is None:
        assert seen == [] and report.step("R03-registry").state == STEP_INCOMPLETE
        assert "aucun projet créé" in report.step("R03-registry").detail and "registry_final" not in report.facts
    else:
        assert {c["project_id"] for c in seen} == {project} and report.step("R03-registry").state == STEP_SUCCEEDED


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
        seen.update(path=path, **{k: v for k, v in kwargs.items() if k in {"python", "image", "runner"}})
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
    assert seen["python"] == "python" and seen["image"] == business.DEFAULT_VERIFIER_IMAGE
    assert "runner" not in seen, "chemin par défaut : conteneur isolé, jamais le runner local de confiance"
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
    runner = business.bounded_command_runner(time.monotonic() + 0.3)
    started = time.monotonic()

    with pytest.raises(BudgetStop, match="groupe de processus"):
        runner([sys.executable, str(parent)])

    assert time.monotonic() - started < 10
    time.sleep(3.5)
    assert not marker.exists(), "aucun enfant n'a survécu à l'échéance (rien ne continue à tourner hors surveillance)"


def test_there_is_no_grace_after_the_deadline_a_command_that_would_finish_just_after_is_stopped(tmp_path):
    """Contre-épreuve du manager : 0,15 s restantes et un enfant qui dort 0,5 s ne doivent PAS aboutir normalement."""
    runner = business.bounded_command_runner(time.monotonic() + 0.15)
    started = time.monotonic()

    with pytest.raises(BudgetStop, match="échéance globale"):
        runner([sys.executable, "-c", "import time; time.sleep(0.5); print('FINISHED')"])

    assert time.monotonic() - started < 0.45, "arrêt immédiat à l'échéance, sans fenêtre de grâce"


def test_the_runner_signature_offers_no_grace_parameter():
    import inspect

    assert "grace" not in inspect.signature(business.bounded_command_runner).parameters


def test_the_deadline_clock_is_resolved_at_call_time_for_a_controlled_clock():
    now = [100.0]
    runner = business.bounded_command_runner(110.0, clock=lambda: now[0])
    assert runner([sys.executable, "-c", "print('ok')"]).stdout.strip() == "ok"
    now[0] = 110.5
    with pytest.raises(BudgetStop, match="avant le lancement"):
        runner([sys.executable, "-c", "print('jamais')"])


def test_a_command_finishing_before_the_deadline_returns_its_streams():
    runner = business.bounded_command_runner(time.monotonic() + 30)
    result = runner([sys.executable, "-c", "print('ok')"])
    assert (result.returncode, result.stdout.strip()) == (0, "ok")
