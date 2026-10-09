"""Campagne W4 — rapport à états distincts, préflight sans dépense, enveloppe globale, vérification des plafonds.

Aucun appel de modèle ni de réseau : le préflight lit un faux serveur GitHub REST (vrais clients, vraie politique de fusion W3) et
interroge ``worker_budget.allocate_worker`` de production sur un registre jetable. Les cas « refus » vérifient le MOTIF précis et
que la campagne n'a émis AUCUNE action facturable.
"""

from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace

import pytest
from github_fake_server import FakeGitHubServer

from collegue.pilot import w4_business as business
from collegue.pilot.w4_business import (
    STEP_BUDGET_STOP,
    STEP_FAILED,
    STEP_INCOMPLETE,
    STEP_NOT_EXECUTED,
    STEP_SUCCEEDED,
    BudgetStop,
    CampaignReport,
    IncompleteValidation,
)

SECRET = "sk-test-SECRET-1234567890"
GOOD_ENV = {
    **business.campaign_environment("w4-test", "/var/lib/collegue-w4"),
    "GITHUB_EVENT_NAME": "workflow_dispatch",
    "GITHUB_RUN_ATTEMPT": "1",
    "GITHUB_RUN_ID": "4242",
    "W4_BUSINESS_CONFIRM": business.LAUNCH_CONFIRMATION,
}


# ── rapport : cinq états, verdict, rendu humain et machine ───────────────────────────────────────────────────────────────


def report_with(*outcomes):
    report = CampaignReport("deterministic", "unit", secrets=[SECRET])
    for index, outcome in enumerate(outcomes):
        report.declare(f"S{index}", f"étape {index}")

    def make(outcome):
        def run(step):
            step.evidence["token"] = SECRET  # une fuite éventuelle doit être masquée
            if outcome == "failed":
                raise AssertionError("contrat violé")
            if outcome == "budget":
                raise BudgetStop("enveloppe atteinte")
            if outcome == "incomplete":
                raise IncompleteValidation("prérequis absent")

        return run

    for index, outcome in enumerate(outcomes):
        report.run(f"S{index}", make(outcome))
    return report


def states(report):
    return [s.state for s in report.steps]


def test_all_steps_succeeding_is_the_only_way_to_a_validated_verdict_with_exit_zero():
    report = report_with("ok", "ok")
    assert states(report) == [STEP_SUCCEEDED, STEP_SUCCEEDED]
    assert (report.verdict(), report.exit_code()) == ("validated", 0)


@pytest.mark.parametrize(
    "outcome, state, verdict, code",
    [
        ("failed", STEP_FAILED, "failed", 1),
        ("incomplete", STEP_INCOMPLETE, "incomplete_validation", 3),
        ("budget", STEP_BUDGET_STOP, "budget_stop", 4),
    ],
)
def test_a_non_success_halts_the_campaign_and_leaves_the_rest_not_executed(outcome, state, verdict, code):
    report = report_with("ok", outcome, "ok", "ok")

    assert states(report) == [STEP_SUCCEEDED, state, STEP_NOT_EXECUTED, STEP_NOT_EXECUTED]
    assert (report.verdict(), report.exit_code()) == (verdict, code)
    assert report.to_machine()["counts"][STEP_NOT_EXECUTED] == 2


def test_a_required_step_never_run_is_never_read_as_a_success_and_an_optional_one_is_ignored():
    report = CampaignReport("deterministic", "unit")
    report.declare("A", "faite")
    report.declare("B", "requise mais jamais jouée")
    report.declare("C", "facultative", required=False)
    report.run("A", lambda s: None)
    assert report.verdict() == "incomplete_validation" and report.exit_code() == 3
    optional_only = CampaignReport("deterministic", "unit")
    optional_only.declare("A", "faite")
    optional_only.declare("C", "facultative jamais jouée", required=False)
    optional_only.run("A", lambda s: None)
    assert optional_only.verdict() == "validated"


def test_failed_beats_incomplete_and_budget_in_the_verdict():
    report = CampaignReport("deterministic", "unit")
    report.declare("A", "a")
    report.declare("B", "b")
    report.step("A").state = STEP_INCOMPLETE
    report.step("B").state = STEP_FAILED
    assert report.verdict() == "failed"
    report.step("B").state = STEP_BUDGET_STOP
    assert report.verdict() == "budget_stop"


def test_machine_and_human_reports_carry_the_five_states_and_never_the_secret():
    report = report_with("ok", "failed", "x")
    report.step("S2").state = STEP_INCOMPLETE  # force les cinq états dans le même rapport
    report.declare("S3", "budget")
    report.step("S3").state = STEP_BUDGET_STOP
    machine, human = report.to_machine(), report.to_human()

    assert machine["schema"] == business.REPORT_SCHEMA and machine["verdict"] == "failed"
    assert set(machine["counts"]) == set(business.STEP_STATES)
    assert all(
        machine["counts"][state] >= 1 for state in (STEP_SUCCEEDED, STEP_FAILED, STEP_INCOMPLETE, STEP_BUDGET_STOP)
    )
    for marker in ("OK", "ÉCHEC", "VALIDATION INCOMPLÈTE", "ARRÊT BUDGET"):
        assert marker in human
    assert SECRET not in json.dumps(machine) and SECRET not in human and SECRET not in report.to_json()
    assert business.REDACTION in json.dumps(machine)
    with pytest.raises(ValueError):
        report.declare("S0", "doublon")


# ── enveloppe globale et environnement de l'invocation réelle ───────────────────────────────────────────────────────────


def test_the_campaign_environment_is_the_exact_global_envelope():
    env = business.campaign_environment("w4-final", "/var/lib/collegue-w4")
    assert business.validate_campaign_environment(env) == []
    assert (env["MAX_COST_USD"], env["MAX_TOKENS_BUDGET"], env["COLLEGUE_RUN_DEADLINE_SECONDS"]) == (
        "2",
        "250000",
        "900",
    )
    assert env["STATE_DATABASE_URL"] == "sqlite:////var/lib/collegue-w4/w4-final.sqlite3", (
        "registre durable PROPRE à la campagne"
    )
    assert env["BUDGET_MODE"] == "strict" and env["TASK_MAX_ATTEMPTS"] == "1" and env["STRICT_MAX_INFLIGHT_PRS"] == "1"


@pytest.mark.parametrize(
    "override, fragment",
    [
        ({"MAX_COST_USD": "3"}, "MAX_COST_USD=3 dépasse"),
        ({"MAX_TOKENS_BUDGET": "250001"}, "MAX_TOKENS_BUDGET=250001 dépasse"),
        ({"COLLEGUE_RUN_DEADLINE_SECONDS": "901"}, "COLLEGUE_RUN_DEADLINE_SECONDS=901 dépasse"),
        ({"MAX_COST_USD": ""}, "MAX_COST_USD absent"),
        ({"MAX_TOKENS_BUDGET": "0"}, "MAX_TOKENS_BUDGET absent ou invalide"),
        ({"BUDGET_MODE": "advisory"}, "BUDGET_MODE doit valoir 'strict'"),
        ({"TASK_MAX_ATTEMPTS": "3"}, "TASK_MAX_ATTEMPTS doit valoir '1'"),
        ({"STRICT_MAX_INFLIGHT_PRS": "2"}, "STRICT_MAX_INFLIGHT_PRS doit valoir '1'"),
        ({"BUILD_AUTO_MERGE": "false"}, "BUILD_AUTO_MERGE doit valoir 'true'"),
        ({"COLLEGUE_HOME": "relatif/home"}, "COLLEGUE_HOME doit être un chemin absolu"),
        ({"STATE_DATABASE_URL": "sqlite:///relatif.db"}, "STATE_DATABASE_URL doit viser une SQLite absolue"),
        ({"INTEGRATION_E2E_ENABLED": "true"}, "récurrence"),
    ],
)
def test_any_drift_from_the_envelope_is_reported_and_never_silently_corrected(override, fragment):
    problems = business.validate_campaign_environment({**GOOD_ENV, **override})
    assert any(fragment in problem for problem in problems), problems


@pytest.mark.parametrize(
    "override, fragment",
    [
        ({"GITHUB_EVENT_NAME": "schedule"}, "seule une exécution manuelle ponctuelle"),
        ({"GITHUB_EVENT_NAME": "push"}, "seule une exécution manuelle ponctuelle"),
        ({"GITHUB_RUN_ATTEMPT": "2"}, "aucune relance payante automatique"),
        ({"W4_BUSINESS_CONFIRM": "oui"}, "confirmation de lancement unique"),
        ({"INTEGRATION_E2E_ENABLED": "true"}, "récurrence interdite"),
    ],
)
def test_the_launch_context_refuses_recurrence_reruns_and_unconfirmed_dispatches(override, fragment):
    report = CampaignReport("preflight", "unit")
    step = report.declare("P01", "contexte")
    with pytest.raises(IncompleteValidation, match=fragment):
        business.check_launch_context({**GOOD_ENV, **override}, report, step)
    business.check_launch_context(GOOD_ENV, report, step)  # témoin : le contexte conforme passe


def test_llm_keys_may_not_reach_the_keyless_preflight_stages():
    report = CampaignReport("preflight", "unit")
    step = report.declare("P03", "secrets")
    business.check_secret_scope(GOOD_ENV, report, step)
    for stage in ("static", "full"):
        with pytest.raises(RuntimeError, match="LLM_API_KEY"):
            business.check_secret_scope({**GOOD_ENV, "LLM_API_KEY": SECRET}, report, step, stage=stage)


def test_the_launch_stage_legitimately_holds_the_key_of_the_chosen_transport_and_records_only_names():
    report = CampaignReport("preflight", "unit", secrets=[SECRET])
    step = report.declare("P03", "secrets")
    env = {**GOOD_ENV, "LLM_API_KEY": SECRET, "LLM_API_KEY_CODER": SECRET + "-coder"}

    business.check_secret_scope(env, report, step, stage="launch")

    assert step.evidence["llm_secret_names_present"] == ["LLM_API_KEY", "LLM_API_KEY_CODER"]
    assert SECRET not in report.to_json() and SECRET not in report.to_human()


def test_every_secret_looking_value_of_the_environment_is_masked_including_role_keys():
    values = business.secret_values(
        {"GITHUB_TOKEN": "t-1234", "LLM_API_KEY_QA": "k-5678", "PATH": "/bin", "EMPTY_KEY": ""}
    )
    assert sorted(values) == ["k-5678", "t-1234"]


# ── capacité du transport de worker (règles de production, aucune copie) ────────────────────────────────────────────────


def test_the_informative_matrix_documents_the_general_picture_but_never_decides():
    matrix = business.worker_capacity_matrix()

    assert [row["transport"] for row in matrix] == [
        "OHSdkAgent / clé API facturable",
        "OHSdkAgent / abonnement",
        "OpenHandsAgent (legacy) / clé API facturable",
    ]
    assert not any(row["accepted"] for row in matrix), matrix
    assert all(row["code"] == "unbounded_transport" for row in matrix)


EFFECTIVE_API_KEY = {"CODER_SUBSCRIPTION": "false"}  # le modèle imposé (31B) reste celui de la campagne
EFFECTIVE_SUBSCRIPTION = {"LLM_PROVIDER": "openai", "LLM_MODEL": "gpt-5.5", "CODER_SUBSCRIPTION": "true"}


@pytest.mark.parametrize(
    "overrides, reason",
    [(EFFECTIVE_API_KEY, "FACTURABLE"), (EFFECTIVE_SUBSCRIPTION, "plafond de TOKENS strict")],
    ids=["api-key", "subscription"],
)
def test_the_effective_worker_is_a_real_ohsdk_agent_judged_by_the_production_rules(overrides, reason):
    settings = business.effective_settings({**GOOD_ENV, **overrides})

    outcome = business.effective_worker_capacity(settings)

    assert outcome["worker"] == "OHSdkAgent" and outcome["declared_enforcement"] == "in-runner"
    assert outcome["accepted"] is False and outcome["code"] == "unbounded_transport" and reason in outcome["reason"]


def test_the_capacity_step_judges_the_chosen_configuration_and_the_matrix_never_decides():
    report = CampaignReport("preflight", "unit")
    step = report.declare("P06", "capacité")
    settings = business.effective_settings({**GOOD_ENV, **EFFECTIVE_API_KEY})
    accepting = [{"transport": "futur", "accepted": True, "max_micro_usd": 1, "max_tokens": 1}]

    with pytest.raises(IncompleteValidation, match="zéro appel émis"):
        business.check_worker_capacity(
            report, step, settings=settings, matrix=accepting
        )  # la matrice accepte, pas le choix

    assert step.evidence["llm_calls_emitted"] == 0 and step.evidence["matrix_informative"] == accepting
    assert step.evidence["effective"]["worker"] == "OHSdkAgent" and step.evidence["effective"]["accepted"] is False
    business.check_worker_capacity(report, step, settings=settings, capacity=accepting_capacity)  # témoin : acceptation


def test_an_unreadable_effective_configuration_is_an_incomplete_validation_never_an_assumed_default():
    report = CampaignReport("preflight", "unit")
    step = report.declare("P06", "capacité")
    with pytest.raises(IncompleteValidation, match="configuration effective illisible"):
        business.check_worker_capacity(report, step, settings=None)


def test_the_capacity_probe_runs_nothing_and_a_sentinel_sandbox_refuses_any_execution():
    sentinel = business.SentinelSandbox()
    with pytest.raises(AssertionError, match="interdit pendant le préflight"):
        sentinel.run_command(["echo"], "/tmp")
    with pytest.raises(AssertionError):
        sentinel.run_tests("/tmp")


# ── routes effectives (API publique du lot A) ───────────────────────────────────────────────────────────────────────────


def test_route_validation_uses_the_public_api_of_lot_a_and_reports_its_refusal_without_secret(monkeypatch):
    report = CampaignReport("preflight", "unit")
    step = report.declare("P05", "routes")
    seen = []

    def validator(settings, *, require_credential):
        seen.append(require_credential)
        raise ValueError("fournisseur openai contradictoire avec le modèle gemini-2.5-flash")

    with pytest.raises(IncompleteValidation, match="route de rôle refusée.*contradictoire"):
        business.check_effective_routes(report, step, settings=object(), require_credential=True, validator=validator)
    assert seen == [True] and step.evidence["llm_calls_emitted"] == 0
    business.check_effective_routes(report, step, settings=object(), require_credential=False, validator=ok_routes)
    assert step.evidence["routes"]["CODER"]["model"] == "gpt-5.5"


def test_route_validation_is_an_explicit_incomplete_validation_when_the_public_api_is_absent(monkeypatch):
    import sys
    import types

    stub = types.ModuleType("collegue.core.llm")  # code sans l'API de routage : jamais un succès par défaut
    monkeypatch.setitem(sys.modules, "collegue.core.llm", stub)

    with pytest.raises(IncompleteValidation, match="validate_role_routes"):
        business.route_validator()


def test_route_validation_asks_the_four_called_roles_through_validate_role_routes(monkeypatch):
    import sys
    import types

    calls = []
    stub = types.ModuleType("collegue.core.llm")
    stub.LLMRole = types.SimpleNamespace(PLANNER="planner", QA="qa", REVIEWER="reviewer", CODER="coder")
    stub.validate_role_routes = lambda settings, roles, require_credential: (
        calls.append((roles, require_credential)) or {}
    )
    monkeypatch.setitem(sys.modules, "collegue.core.llm", stub)

    business.route_validator()(object(), require_credential=False)

    assert calls == [(["planner", "qa", "reviewer", "coder"], False)]


# ── protections W3 de la base éphémère ─────────────────────────────────────────────────────────────────────────────────


class FixtureNamedServer(FakeGitHubServer):
    """Faux serveur REST dont les routes sont celles du dépôt fixture réel (``VynoDePal/collegue-e2e-fixture``)."""

    @staticmethod
    def _rename(endpoint: str) -> str:
        return endpoint.replace("/repos/VynoDePal/collegue-e2e-fixture", "/repos/fixture/fixture")

    def api_get(self, endpoint, params=None):
        return super().api_get(self._rename(endpoint), params)


def server_clients(server):
    wrapped = server.clients()
    return SimpleNamespace(prs=wrapped.prs, branches=wrapped.branches)


def test_the_real_fixture_ruleset_alone_is_not_enough_for_a_protected_ephemeral_base():
    """Reproduit le ruleset RÉEL du dépôt fixture (suppression / non-fast-forward / update de la branche par défaut) :
    aucun check requis, aucune règle « à jour » sur la base éphémère ⇒ préflight incomplet, pas une fusion non protégée."""
    server = FixtureNamedServer()
    server.rulesets[18840666] = {
        "id": 18840666,
        "name": "Immutable nightly seed",
        "target": "branch",
        "enforcement": "active",
        "current_user_can_bypass": "never",
    }
    server.rules = [
        {"type": t, "ruleset_id": 18840666, "ruleset_source_type": "Repository", "parameters": {}}
        for t in ("deletion", "non_fast_forward", "update")
    ]
    report = CampaignReport("preflight", "unit")
    step = report.declare("P06", "protections")

    with pytest.raises(IncompleteValidation, match="protections W3 absentes ou non établies") as excinfo:
        business.check_base_protection(
            server_clients(server), report, step, owner="VynoDePal", repo="collegue-e2e-fixture", run_tag="1-1"
        )

    assert "aucun check requis" in str(excinfo.value) and "aucune protection n'est modifiée" in str(excinfo.value)
    assert step.evidence["refusal_code"] == "policy"
    assert step.evidence["probed_branch"] == "collegue-business/1-1"
    assert server.merge_calls() == [] and not [c for c in server.calls if c[0] in {"PUT", "POST", "DELETE"}]


def test_a_strict_non_bypassable_ruleset_with_required_checks_satisfies_the_protection_step():
    server = FixtureNamedServer()
    server.add_ruleset(1)
    report = CampaignReport("preflight", "unit")
    step = report.declare("P06", "protections")

    business.check_base_protection(
        server_clients(server), report, step, owner="VynoDePal", repo="collegue-e2e-fixture", run_tag="1-1"
    )

    assert step.evidence["strict_sources"] == ["ruleset:1"] and len(step.evidence["required_checks"]) == 5
    assert step.evidence["actor"] == "collegue-bot"


def test_a_token_that_can_bypass_the_strict_rule_is_refused():
    server = FixtureNamedServer()
    server.add_ruleset(1, can_bypass="always")
    report = CampaignReport("preflight", "unit")
    step = report.declare("P06", "protections")
    with pytest.raises(IncompleteValidation, match="contournable"):
        business.check_base_protection(
            server_clients(server), report, step, owner="VynoDePal", repo="collegue-e2e-fixture", run_tag="1-1"
        )


# ── identité du dépôt fixture ──────────────────────────────────────────────────────────────────────────────────────────


def identity_clients(**overrides):
    values = dict(
        id=business.FIXTURE_REPOSITORY_ID,
        full_name=business.FIXTURE_REPOSITORY,
        is_private=False,
        default_branch="main",
        sentinel=business.FIXTURE_SENTINEL,
        head=business.FIXTURE_SEED_SHA,
    )
    values.update(overrides)
    return SimpleNamespace(
        repos=SimpleNamespace(
            get_repo=lambda o, r: SimpleNamespace(
                id=values["id"],
                full_name=values["full_name"],
                is_private=values["is_private"],
                default_branch=values["default_branch"],
            )
        ),
        files=SimpleNamespace(get_file_content=lambda *a, **k: {"content": values["sentinel"]}),
        branches=SimpleNamespace(get_branch_sha=lambda *a, **k: values["head"]),
    )


def test_fixture_identity_passes_for_the_pinned_repository_and_seed():
    report = CampaignReport("preflight", "unit")
    step = report.declare("P04", "identité")
    business.check_fixture_identity(identity_clients(), report, step, owner="VynoDePal", repo="collegue-e2e-fixture")
    assert step.evidence["root_sha"] == business.FIXTURE_SEED_SHA and step.evidence["repository_id"] == 1298596453


@pytest.mark.parametrize(
    "override, fragment",
    [
        ({"id": 1}, "identité immuable"),
        ({"full_name": "someone/else"}, "coordonnée"),
        ({"is_private": True}, "public"),
        ({"default_branch": "trunk"}, "branche par défaut"),
        ({"sentinel": "autre"}, "sentinelle"),
        ({"head": "0" * 40}, "seed de la fixture a bougé"),
    ],
)
def test_fixture_identity_refuses_any_drift_before_any_write(override, fragment):
    report = CampaignReport("preflight", "unit")
    step = report.declare("P04", "identité")
    with pytest.raises(RuntimeError, match=fragment):
        business.check_fixture_identity(
            identity_clients(**override), report, step, owner="VynoDePal", repo="collegue-e2e-fixture"
        )


# ── environnement des oracles (image du sandbox) ───────────────────────────────────────────────────────────────────────


def test_the_oracle_environment_needs_the_fixture_stack_and_a_real_pdf_reader_in_the_image():
    calls = []

    def runner(argv):
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    report = CampaignReport("preflight", "unit")
    step = report.declare("P07", "oracles")
    business.check_oracle_environment(report, step, image="img:ci", runner=runner)
    argv = calls[0]
    assert "--network" in argv and argv[argv.index("--network") + 1] == "none" and "pypdf" in argv[-1]

    def missing(argv):
        return subprocess.CompletedProcess(argv, 1, stdout="pypdf\n", stderr="")

    with pytest.raises(IncompleteValidation, match="manquants : pypdf"):
        business.check_oracle_environment(report, step, image="img:ci", runner=missing)

    def broken(argv):
        raise FileNotFoundError("docker")

    with pytest.raises(IncompleteValidation, match="non vérifiable"):
        business.check_oracle_environment(report, step, image="img:ci", runner=broken)


# ── préflight complet ───────────────────────────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def no_llm(monkeypatch, tmp_path):
    """Toute tentative d'émission LLM pendant le préflight échoue le test (zéro appel prouvé).

    Répertoire courant vierge : un ``.env`` local rendrait la configuration effective ambiguë (voir test_w4_business_cli)."""
    monkeypatch.chdir(tmp_path)
    emitted = []

    async def forbidden(*args, **kwargs):
        emitted.append(kwargs)
        raise AssertionError("appel LLM émis pendant le préflight")

    monkeypatch.setattr("collegue.core.llm.client.sample_with_timeout", forbidden)
    monkeypatch.setattr("collegue.core.llm.budget_guard.guarded_call", forbidden)
    return emitted


def ok_runner(argv):
    return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")


class PinnedSeedBranches:
    """Branches du faux serveur (protections, règles) avec la tête de ``main`` épinglée sur la graine du dépôt fixture."""

    def __init__(self, inner):
        self._inner = inner

    def get_branch_sha(self, owner, repo, branch):
        if branch == "main":
            return business.FIXTURE_SEED_SHA
        return self._inner.get_branch_sha(owner, repo, branch)

    def __getattr__(self, name):
        return getattr(self._inner, name)


def full_clients(server):
    ident = identity_clients()
    wrapped = server_clients(server)
    return SimpleNamespace(
        repos=ident.repos, files=ident.files, branches=PinnedSeedBranches(wrapped.branches), prs=wrapped.prs
    )


def ok_routes(settings, *, require_credential):
    return {"CODER": {"provider": "openai", "model": "gpt-5.5", "credential_present": require_credential}}


def accepting_capacity(settings):
    return {"worker": "OHSdkAgent", "accepted": True, "max_micro_usd": 1, "max_tokens": 1}


def preflight(server, env=GOOD_ENV, **kwargs):
    kwargs.setdefault("route_check", ok_routes)
    kwargs.setdefault("image_runner", ok_runner)
    return business.run_preflight(env, clients=full_clients(server), campaign_id="w4-test", run_tag="4242-1", **kwargs)


def test_the_real_preflight_stops_incomplete_with_zero_billable_action_when_the_chosen_worker_holds_no_ceiling(no_llm):
    server = FixtureNamedServer()
    server.add_ruleset(1)

    report = preflight(server, {**GOOD_ENV, **EFFECTIVE_API_KEY})

    by_id = {s.id: s.state for s in report.steps}
    assert by_id["P01-launch-context"] == by_id["P02-environment"] == by_id["P03-secret-scope"] == STEP_SUCCEEDED
    assert by_id["P04-fixture-identity"] == by_id["P05-role-routes"] == STEP_SUCCEEDED
    assert by_id["P06-worker-capacity"] == STEP_INCOMPLETE, "le worker choisi ne tient pas les trois plafonds"
    assert by_id["P07-base-protection"] == by_id["P08-oracle-environment"] == STEP_NOT_EXECUTED
    assert (report.verdict(), report.exit_code()) == ("incomplete_validation", 3)
    assert report.facts["llm_calls_emitted"] == 0 and report.facts["billable_actions_emitted"] == 0 and no_llm == []
    assert "zéro appel émis" in report.step("P06-worker-capacity").detail
    assert server.calls == [c for c in server.calls if c[0] == "GET"], "lectures seules"


def test_with_a_capable_worker_the_preflight_still_refuses_an_unprotected_base(no_llm):
    server = FixtureNamedServer()  # aucune protection ni ruleset

    report = preflight(server, capacity=accepting_capacity)

    assert report.step("P06-worker-capacity").state == STEP_SUCCEEDED
    assert report.step("P07-base-protection").state == STEP_INCOMPLETE
    assert (
        report.step("P08-oracle-environment").state == STEP_NOT_EXECUTED and report.verdict() == "incomplete_validation"
    )


def test_with_every_prerequisite_met_the_preflight_validates_without_any_llm_call(no_llm):
    server = FixtureNamedServer()
    server.add_ruleset(1)

    report = preflight(server, capacity=accepting_capacity)

    assert report.verdict() == "validated" and report.exit_code() == 0 and no_llm == []


def test_a_route_refused_or_missing_api_blocks_the_preflight_before_the_worker_and_the_image(no_llm):
    server = FixtureNamedServer()
    server.add_ruleset(1)

    def refuse(settings, *, require_credential):
        raise ValueError("contradiction fournisseur/modèle")

    report = preflight(server, capacity=accepting_capacity, route_check=refuse)

    assert report.step("P05-role-routes").state == STEP_INCOMPLETE
    assert "contradiction" in report.step("P05-role-routes").detail
    assert report.step("P06-worker-capacity").state == STEP_NOT_EXECUTED and report.verdict() == "incomplete_validation"


def test_the_static_and_full_stages_check_routes_without_credential_and_launch_requires_it(no_llm):
    server = FixtureNamedServer()
    server.add_ruleset(1)
    required = {}

    def spy(stage):
        def route_check(settings, *, require_credential):
            required[stage] = require_credential
            return {}

        return route_check

    for stage in ("static", "full", "launch"):
        preflight(
            server,
            {**GOOD_ENV, "LLM_API_KEY": SECRET} if stage == "launch" else GOOD_ENV,
            stage=stage,
            route_check=spy(stage),
            capacity=accepting_capacity,
        )

    assert required == {"static": False, "full": False, "launch": True}


def test_the_static_stage_leaves_the_image_unplayed_and_optional_but_full_and_launch_require_it(no_llm):
    server = FixtureNamedServer()
    server.add_ruleset(1)

    static = preflight(server, stage="static", capacity=accepting_capacity)
    full = preflight(server, stage="full", capacity=accepting_capacity)
    launch = preflight(server, {**GOOD_ENV, "LLM_API_KEY": SECRET}, stage="launch", capacity=accepting_capacity)

    assert static.step("P08-oracle-environment").required is False
    assert static.step("P08-oracle-environment").state == STEP_NOT_EXECUTED and static.verdict() == "validated"
    for report in (full, launch):
        assert report.step("P08-oracle-environment").required is True
        assert report.step("P08-oracle-environment").state == STEP_SUCCEEDED
    assert launch.step("P03-secret-scope").state == STEP_SUCCEEDED, "clé légitime à l'étape de lancement"
    assert (
        preflight(server, {**GOOD_ENV, "LLM_API_KEY": SECRET}, stage="full").step("P03-secret-scope").state
        == STEP_FAILED
    )


def test_the_image_checked_is_the_one_the_gate_runs_and_it_is_never_pulled(no_llm):
    server = FixtureNamedServer()
    server.add_ruleset(1)
    seen = []

    def runner(argv):
        seen.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    preflight(server, capacity=accepting_capacity, image_runner=runner)
    preflight(
        server,
        {**GOOD_ENV, "SANDBOX_IMAGE": "registry.example/oh:pinned"},
        capacity=accepting_capacity,
        image_runner=runner,
    )

    default_image, chosen_image = (argv[argv.index("--cap-drop") + 2] for argv in seen)
    assert default_image == "collegue-sandbox:latest" and chosen_image == "registry.example/oh:pinned"
    assert all("--pull" in argv and argv[argv.index("--pull") + 1] == "never" for argv in seen)
    assert all("pypdf" in argv[-1] for argv in seen), (
        "le lecteur PDF fait partie de la pile exigée AVANT le rouge de préimage"
    )


def test_an_image_without_the_oracle_stack_is_an_incomplete_validation_not_a_valid_red(no_llm):
    server = FixtureNamedServer()
    server.add_ruleset(1)

    def missing(argv):
        return subprocess.CompletedProcess(argv, 1, stdout="pypdf", stderr="")

    report = preflight(server, capacity=accepting_capacity, image_runner=missing)

    assert report.step("P08-oracle-environment").state == STEP_INCOMPLETE
    assert "pypdf" in report.step("P08-oracle-environment").detail and report.verdict() == "incomplete_validation"


def test_a_recurring_or_rerun_trigger_stops_at_the_first_step_and_touches_nothing(no_llm):
    server = FixtureNamedServer()
    report = business.run_preflight(
        {**GOOD_ENV, "GITHUB_EVENT_NAME": "schedule", "GITHUB_RUN_ATTEMPT": "2"},
        clients=full_clients(server),
        campaign_id="w4-test",
        run_tag="4242-2",
        image_runner=ok_runner,
    )
    assert report.step("P01-launch-context").state == STEP_INCOMPLETE
    assert all(s.state == STEP_NOT_EXECUTED for s in report.steps[1:]) and server.calls == []


def test_secrets_from_the_environment_never_appear_in_the_preflight_report(no_llm):
    env = {**GOOD_ENV, "GITHUB_TOKEN": SECRET, "LLM_API_KEY": SECRET}
    server = FixtureNamedServer()
    report = preflight(server, env)
    assert (
        report.step("P03-secret-scope").state == STEP_FAILED
    )  # une clé de modèle dans l'étape de préflight est un échec
    assert SECRET not in report.to_json() and SECRET not in report.to_human()


# ── registre W2 : plafonds ───────────────────────────────────────────────────────────────────────────────────────────────


def counters(**overrides):
    values = dict(
        scope="project:1",
        strict=True,
        cap_usd=2.0,
        cap_tokens=250_000,
        consumed_micro_usd=100_000,
        consumed_tokens=20_000,
        reserved_micro_usd=0,
        reserved_tokens=0,
        unknown_micro_usd=0,
        unknown_tokens=0,
        blocked_reason=None,
        revision=3,
    )
    values.update(overrides)
    return values


def test_registry_bounds_come_from_the_durable_scope_and_never_exceed_the_envelope():
    business.assert_registry_within_bounds(counters())
    with pytest.raises(RuntimeError, match="plafond USD du registre hors enveloppe"):
        business.assert_registry_within_bounds(counters(cap_usd=3.0))
    with pytest.raises(RuntimeError, match="plafond de tokens du registre hors enveloppe"):
        business.assert_registry_within_bounds(counters(cap_tokens=300_000))
    with pytest.raises(RuntimeError, match="pas en mode strict"):
        business.assert_registry_within_bounds(counters(strict=False))
    with pytest.raises(IncompleteValidation, match="aucun scope"):
        business.assert_registry_within_bounds({"scope": None})
    with pytest.raises(BudgetStop, match="enveloppe atteinte"):
        business.assert_registry_within_bounds(counters(consumed_tokens=250_001))
    with pytest.raises(BudgetStop):  # le réservé et l'inconnu comptent comme dépensés (borne haute)
        business.assert_registry_within_bounds(counters(reserved_micro_usd=1_950_000, unknown_micro_usd=100_000))


# ── conteneur de vérification : surveillé jusqu'à l'échéance ─────────────────────────────────────────────────────────────


def test_the_verifier_container_is_hardened_named_and_secret_free():
    argv = business.docker_verifier_command(image="img:ci", name="w4-verify-1", checkout="/tmp/co", scratch="/scratch")
    joined = " ".join(argv)

    for flag in (
        "--rm",
        "--read-only",
        "--cap-drop ALL",
        "--network none",
        "--pids-limit",
        "--memory",
        "--name w4-verify-1",
    ):
        assert flag in joined
    assert "/tmp/co:/workspace:ro" in joined and "-e " in joined
    assert "KEY" not in joined and "TOKEN" not in joined and "SECRET" not in joined.upper().replace("SECURITY", "")


def test_a_deadline_kills_the_named_container_before_propagating():
    calls = []

    def runner(argv, **kwargs):
        calls.append(list(argv))
        if argv[:2] == ["docker", "run"]:
            raise subprocess.TimeoutExpired(argv, 1)
        return subprocess.CompletedProcess(argv, 0, "", "")

    with pytest.raises(subprocess.TimeoutExpired):
        business.run_in_named_container(
            ["docker", "run", "--name", "w4-verify-1", "img"], name="w4-verify-1", timeout=1, runner=runner
        )

    assert calls[-1] == ["docker", "kill", "w4-verify-1"], (
        "le conteneur facturable n'est jamais laissé hors surveillance"
    )


# ── invocation réelle : le lancement n'a lieu que si TOUT le préflight a réussi ────────────────────────────────────────


def test_the_real_invocation_never_launches_when_the_preflight_is_not_validated(no_llm):
    server = FixtureNamedServer()
    checked = preflight(server)
    launched = []

    report = business.run_campaign(GOOD_ENV, preflight=checked, launch=lambda r: launched.append(r))

    assert (
        launched == [] and report.facts["billable_actions_emitted"] == 0 and report.facts["stop_point"] == "preflight"
    )
    assert report.verdict() == "incomplete_validation"
    assert report.step("R01-run").state == STEP_NOT_EXECUTED and report.step("R02-business").state == STEP_NOT_EXECUTED


def test_the_real_invocation_launches_exactly_once_after_a_validated_preflight(no_llm):
    server = FixtureNamedServer()
    server.add_ruleset(1)
    checked = preflight(server, capacity=accepting_capacity)
    launched = []

    report = business.run_campaign(GOOD_ENV, preflight=checked, launch=lambda r: launched.append(r))

    assert len(launched) == 1 and report.step("R01-run").state == STEP_SUCCEEDED
