"""Préflight W5 : socle de bootstrap PROUVÉ par l'API, identité de campagne consommée, modèles imposés, relais budgétaire, portée des clés.

Le manifeste fourni par l'appelant n'est jamais une preuve à lui seul : chaque cas adverse change UNE chose dans ce que l'API
répond (ou dans le manifeste) et vérifie que le refus nomme la contradiction. Doubles aux seules frontières GitHub.
"""

from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

import pytest

from collegue.pilot import w4_business as business
from collegue.pilot import w5_business as w5
from collegue.pilot.merge_policy import RequiredCheck, ServerPolicy
from collegue.pilot.w4_business import (
    FIXTURE_REPOSITORY,
    FIXTURE_REPOSITORY_ID,
    FIXTURE_SEED_FILES,
    FIXTURE_SEED_SHA,
    CampaignReport,
    IncompleteValidation,
)

BOOT = "b" * 40
SEED_TREE, BOOT_TREE = "1" * 40, "2" * 40
CHECK_APP = 15368
WORKFLOW_PATH = ".github/workflows/fixture-tests.yml"
WORKFLOW = """name: Fixture
on:
  pull_request:
    branches: ["collegue-business/**"]
jobs:
  tests:
    name: Fixture tests
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: python -m pytest -q
"""
DOCS = {"docs/runbook-ops.md": "# Runbook\nclé factice\n", "docs/deploiement.md": "# Déploiement\nclé factice\n"}
APPROVED = {WORKFLOW_PATH: WORKFLOW, **DOCS}


class NotFound(Exception):
    status_code = 404


def sha256(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def blob(path):
    return hashlib.sha1(path.encode()).hexdigest()  # noqa: S324 - identifiant factice stable


class World:
    """État GitHub factice mais COHÉRENT du dépôt fixture ; chaque test en dérive une contradiction."""

    def __init__(self):
        self.main_tip = FIXTURE_SEED_SHA
        self.seed_files = {path: f"contenu de la graine {path}\n" for path in FIXTURE_SEED_FILES}
        self.boot_files = {**self.seed_files, **APPROVED}
        self.commits = {
            FIXTURE_SEED_SHA: SimpleNamespace(
                sha=FIXTURE_SEED_SHA, tree_sha=SEED_TREE, parents=["f" * 40], message="seed"
            ),
            BOOT: SimpleNamespace(sha=BOOT, tree_sha=BOOT_TREE, parents=[FIXTURE_SEED_SHA], message="bootstrap"),
        }
        self.compare = SimpleNamespace(status="ahead", ahead_by=1, behind_by=0, merge_base_sha=FIXTURE_SEED_SHA)
        self.trees = {SEED_TREE: self.tree_of(self.seed_files), BOOT_TREE: self.tree_of(self.boot_files)}
        self.ruleset = SimpleNamespace(id=77, name="bootstrap", target="branch", enforcement="active")
        self.branches = {}
        self.repo = SimpleNamespace(
            id=FIXTURE_REPOSITORY_ID,
            full_name=FIXTURE_REPOSITORY,
            is_private=False,
            archived=False,
            default_branch="main",
        )

    @staticmethod
    def tree_of(files):
        entries = [{"path": p, "type": "blob", "mode": "100644", "sha": blob(p + text)} for p, text in files.items()]
        return {"tree": entries, "truncated": False}

    def refresh_trees(self):
        self.trees[SEED_TREE] = self.tree_of(self.seed_files)
        self.trees[BOOT_TREE] = self.tree_of(self.boot_files)


def clients_for(world):
    class Repos:
        def get_repo(self, owner, repo):
            return world.repo

    class Files:
        def get_file_content(self, owner, repo, path, branch=None):
            source = (
                world.boot_files if branch == BOOT else world.seed_files if branch in (FIXTURE_SEED_SHA, "main") else {}
            )
            if path not in source:
                raise NotFound(path)
            return {"content": source[path], "sha": blob(path + source[path])}

    class Branches:
        def get_branch_sha(self, owner, repo, branch):
            if branch == "main":
                return world.main_tip
            if branch in world.branches:
                return world.branches[branch]
            raise NotFound(branch)

        def get_git_commit(self, owner, repo, sha):
            return world.commits[sha]

        def compare_commits(self, owner, repo, base, head):
            return world.compare

        def get_git_tree(self, owner, repo, tree_sha, recursive=False):
            return world.trees[tree_sha]

        def get_ruleset(self, owner, repo, ruleset_id):
            return world.ruleset

        def create_branch(self, owner, repo, branch, from_branch=None):
            if branch in world.branches:
                raise RuntimeError("422 Reference already exists")
            world.branches[branch] = world.main_tip
            return SimpleNamespace(name=branch)

    return SimpleNamespace(repos=Repos(), files=Files(), branches=Branches())


def manifest_for(world=None, **overrides):
    manifest = {
        "schema": w5.BOOTSTRAP_SCHEMA,
        "repository": FIXTURE_REPOSITORY,
        "repository_id": FIXTURE_REPOSITORY_ID,
        "seed_sha": FIXTURE_SEED_SHA,
        "bootstrap_sha": BOOT,
        "approved_files": {path: sha256(text) for path, text in APPROVED.items()},
        "required_check": w5.REQUIRED_CHECK,
        "check_app_id": CHECK_APP,
        "ruleset_id": 77,
        "branch_pattern": w5.RULESET_BRANCH_PATTERN,
    }
    manifest.update(overrides)
    return manifest


@pytest.fixture
def policy(monkeypatch):
    state = {"checks": (RequiredCheck(w5.REQUIRED_CHECK, CHECK_APP, "ruleset:77"),), "strict": ("ruleset:77",)}

    def discover(clients, owner, repo, branch):
        return ServerPolicy(branch, "collegue-bot", "write", state["checks"], state["strict"])

    monkeypatch.setattr("collegue.pilot.merge_policy.discover_server_policy", discover)
    return state


def validate(world, manifest=None):
    step = CampaignReport("preflight", "unit").declare("P11", "socle")
    return w5.validate_bootstrap_manifest(manifest or manifest_for(), clients_for(world), step, run_tag="1-1"), step


def test_a_genuine_bootstrap_is_proved_by_the_api_and_the_proofs_are_recorded(policy):
    world = World()

    evidence, step = validate(world)

    assert evidence["bootstrap_sha"] == BOOT and evidence["approved_files"] == sorted(APPROVED)
    assert evidence["workflow_jobs"] == [w5.REQUIRED_CHECK] and evidence["check_app_id"] == CHECK_APP
    assert step.evidence["main_tip"] == FIXTURE_SEED_SHA and step.evidence["ruleset_id"] == 77


def mutate(change):
    def apply(world):
        change(world)
        world.refresh_trees()

    return apply


CONTRADICTIONS = {
    "main-moved": (lambda w: setattr(w, "main_tip", "e" * 40), RuntimeError, "main n'est plus la graine"),
    "wrong-repository-id": (lambda w: setattr(w.repo, "id", 1), RuntimeError, "identité du dépôt"),
    "private-repository": (lambda w: setattr(w.repo, "is_private", True), RuntimeError, "public"),
    "not-a-direct-child": (
        lambda w: setattr(w.commits[BOOT], "parents", ["a" * 40]),
        RuntimeError,
        "descendant direct",
    ),
    "merge-commit": (
        lambda w: setattr(w.commits[BOOT], "parents", [FIXTURE_SEED_SHA, "a" * 40]),
        RuntimeError,
        "descendant direct",
    ),
    "diverged": (lambda w: setattr(w.compare, "status", "diverged"), RuntimeError, "ascendance"),
    "two-commits-ahead": (lambda w: setattr(w.compare, "ahead_by", 2), RuntimeError, "ascendance"),
    "seed-file-modified": (
        mutate(lambda w: w.boot_files.update({FIXTURE_SEED_FILES[1]: "falsifié\n"})),
        RuntimeError,
        "modifie des fichiers de la graine",
    ),
    "extra-file": (mutate(lambda w: w.boot_files.update({"app/backdoor.py": "x\n"})), RuntimeError, "en trop"),
    "missing-approved-file": (mutate(lambda w: w.boot_files.pop("docs/deploiement.md")), RuntimeError, "manquants"),
    "bytes-differ": (
        mutate(lambda w: w.boot_files.update({"docs/runbook-ops.md": "# Runbook\nclé RÉELLE\n"})),
        RuntimeError,
        "sha256 approuvé",
    ),
    "approved-file-in-seed": (
        lambda w: w.seed_files.update({"docs/runbook-ops.md": "déjà là\n"}),
        RuntimeError,
        "déjà présent dans la graine",
    ),
    "inactive-ruleset": (lambda w: setattr(w.ruleset, "enforcement", "evaluate"), RuntimeError, "non actif"),
    "tree-truncated": (lambda w: w.trees[BOOT_TREE].update(truncated=True), IncompleteValidation, "tronqué"),
    "symlink-in-tree": (
        lambda w: w.trees[BOOT_TREE]["tree"].append(
            {"path": "lien", "type": "blob", "mode": "120000", "sha": "9" * 40}
        ),
        RuntimeError,
        "non régulier",
    ),
    "submodule-in-tree": (
        lambda w: w.trees[BOOT_TREE]["tree"].append(
            {"path": "sub", "type": "commit", "mode": "160000", "sha": "9" * 40}
        ),
        RuntimeError,
        "non régulier",
    ),
}


@pytest.mark.parametrize("name", sorted(CONTRADICTIONS))
def test_a_manifest_contradicted_by_the_api_is_refused_with_the_contradiction_named(policy, name):
    change, error, needle = CONTRADICTIONS[name]
    world = World()
    change(world)

    with pytest.raises(error, match=needle):
        validate(world)


def test_a_workflow_that_does_not_produce_the_required_check_is_refused(policy):
    other = WORKFLOW.replace("name: Fixture tests", "name: Autre check")
    world = World()
    world.boot_files[WORKFLOW_PATH] = other
    world.refresh_trees()
    manifest = manifest_for(approved_files={**manifest_for()["approved_files"], WORKFLOW_PATH: sha256(other)})

    with pytest.raises(RuntimeError, match="ne produit le check requis"):
        validate(world, manifest)


def test_a_workflow_not_triggered_by_pull_requests_does_not_count(policy):
    pushed = WORKFLOW.replace("pull_request:", "push:")
    world = World()
    world.boot_files[WORKFLOW_PATH] = pushed
    world.refresh_trees()
    manifest = manifest_for(approved_files={**manifest_for()["approved_files"], WORKFLOW_PATH: sha256(pushed)})

    with pytest.raises(RuntimeError, match="ne produit le check requis"):
        validate(world, manifest)


@pytest.mark.parametrize(
    "checks, strict, needle",
    [
        ((RequiredCheck(w5.REQUIRED_CHECK, 1, "ruleset:77"),), ("ruleset:77",), "pas associé à l'application"),
        ((RequiredCheck(w5.REQUIRED_CHECK, None, "ruleset:77"),), ("ruleset:77",), "pas associé à l'application"),
        ((RequiredCheck("Autre", CHECK_APP, "ruleset:77"),), ("ruleset:77",), "pas associé à l'application"),
        ((RequiredCheck(w5.REQUIRED_CHECK, CHECK_APP, "ruleset:77"),), (), "pas associé à l'application"),
    ],
    ids=["other-app", "any-source", "other-check", "not-strict"],
)
def test_the_required_check_must_belong_to_the_declared_real_application_on_a_strict_base(
    policy, checks, strict, needle
):
    policy["checks"], policy["strict"] = checks, strict

    with pytest.raises(RuntimeError, match=needle):
        validate(World())


@pytest.mark.parametrize(
    "override, needle",
    [
        ({"schema": "autre/1"}, "schema"),
        ({"repository": "autre/depot"}, "repository"),
        ({"repository_id": 7}, "repository_id"),
        ({"seed_sha": "c" * 40}, "seed_sha"),
        ({"required_check": "Ruff"}, "required_check"),
        ({"branch_pattern": "refs/heads/**"}, "branch_pattern"),
        ({"bootstrap_sha": "court"}, "bootstrap_sha"),
        ({"check_app_id": 0}, "check_app_id"),
        ({"ruleset_id": True}, "ruleset_id"),
        ({"approved_files": {}}, "approved_files"),
        ({"approved_files": {"../etc/passwd": "0" * 64}}, "chemin approuvé invalide"),
        ({"approved_files": {"app/main.py": "0" * 64}}, "graine immuable"),
        ({"approved_files": {"docs/a.md": "xyz"}}, "sha256 invalide"),
    ],
)
def test_a_malformed_or_foreign_manifest_is_refused_before_any_api_call(policy, override, needle):
    calls = []
    world = World()
    clients = clients_for(world)
    clients.repos.get_repo = lambda *a: calls.append("api")  # noqa: B023

    step = CampaignReport("preflight", "unit").declare("P11", "socle")
    with pytest.raises(RuntimeError, match=needle):
        w5.validate_bootstrap_manifest(manifest_for(**override), clients, step, run_tag="1-1")

    assert calls == []


def test_the_manifest_alone_proves_nothing_a_loaded_file_is_still_checked_against_the_api(policy, tmp_path):
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest_for()), encoding="utf-8")
    world = World()
    world.main_tip = "d" * 40

    manifest = w5.load_bootstrap_manifest(str(path))

    assert manifest["bootstrap_sha"] == BOOT
    with pytest.raises(RuntimeError, match="main n'est plus la graine"):
        validate(world, manifest)
    with pytest.raises(IncompleteValidation, match="absent"):
        w5.load_bootstrap_manifest("")
    path.write_text("pas du json", encoding="utf-8")
    with pytest.raises(IncompleteValidation, match="illisible"):
        w5.load_bootstrap_manifest(str(path))


def test_the_fixture_controls_must_stay_intact_after_the_build_and_after_each_phase(policy):
    world = World()
    manifest = manifest_for()
    world.seed_files = {}  # le contrôle lit la base de campagne : ici, le « ref » est celui du socle
    clients = clients_for(world)
    clients.files.get_file_content = lambda o, r, path, branch=None: {"content": world.boot_files[path]}

    seen = w5.verify_fixture_controls_intact(clients, manifest, "collegue-business/1-1", label="après R02")
    assert seen == {WORKFLOW_PATH: sha256(WORKFLOW)}

    world.boot_files[WORKFLOW_PATH] = WORKFLOW.replace("name: Fixture tests", "name: Fixture tests\n    if: false")
    with pytest.raises(RuntimeError, match="contrôle de la fixture altéré .*après R02"):
        w5.verify_fixture_controls_intact(clients, manifest, "collegue-business/1-1", label="après R02")


# ── identité de campagne ────────────────────────────────────────────────────────────────────────────────────────────────────


def test_a_fresh_identity_is_claimed_durably_and_a_consumed_one_never_gives_a_free_retry():
    world = World()
    clients = clients_for(world)
    step = CampaignReport("preflight", "unit").declare("P12", "identité")
    report = CampaignReport("campaign", "unit")

    w5.check_campaign_identity_unused(clients, "w5-camp-001", step)
    ref = w5.claim_campaign_identity(clients, "w5-camp-001", report)

    assert ref == "collegue-business-claims/w5-camp-001" and ref in world.branches
    assert report.facts["campaign_identity_claim"] == {"ref": ref, "durable": True, "deleted_by_cleanup": False}
    with pytest.raises(IncompleteValidation, match="déjà consommé"):
        w5.check_campaign_identity_unused(clients, "w5-camp-001", step)
    with pytest.raises(IncompleteValidation, match="déjà consommé"):
        w5.claim_campaign_identity(clients, "w5-camp-001", CampaignReport("campaign", "unit"))
    w5.check_campaign_identity_unused(clients, "w5-camp-002", step)  # un identifiant NEUF reste possible


@pytest.mark.parametrize("bad", ["", "X", "ab", "Majuscule", "a b c", "../x", "a" * 41])
def test_invalid_campaign_identifiers_are_refused(bad):
    with pytest.raises(IncompleteValidation, match="identifiant de campagne invalide"):
        w5.claim_ref(bad)


def test_an_unreadable_claim_state_refuses_instead_of_assuming_the_identity_is_free():
    world = World()
    clients = clients_for(world)

    def broken(owner, repo, branch):
        if branch.startswith(w5.CLAIM_PREFIX):
            raise RuntimeError("GitHub indisponible")
        return world.main_tip

    clients.branches.get_branch_sha = broken
    step = CampaignReport("preflight", "unit").declare("P12", "identité")

    with pytest.raises(IncompleteValidation, match="lecture de la revendication impossible"):
        w5.check_campaign_identity_unused(clients, "w5-camp-001", step)


def test_the_claim_survives_the_cleanup_of_the_run():
    from test_w4_business_launch import FakeAdapter

    world = World()
    clients = clients_for(world)
    w5.claim_campaign_identity(clients, "w5-camp-001", CampaignReport("campaign", "unit"))

    business.cleanup_campaign(CampaignReport("campaign", "unit"), SimpleNamespace(cleanup=lambda: {"closed": []}))

    assert "collegue-business-claims/w5-camp-001" in world.branches
    assert FakeAdapter is not None


# ── modèles imposés ─────────────────────────────────────────────────────────────────────────────────────────────────────────


def settings_with(**overrides):
    values = {
        "LLM_PROVIDER": "gemini",
        "LLM_MODEL": business.MODEL_PRIMARY,
        "CODER_FALLBACK_MODELS": business.MODEL_CODER_FALLBACK,
        "CODER_SUBSCRIPTION": False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def check_models(settings):
    step = CampaignReport("preflight", "unit").declare("P09", "modèles")
    w5.check_gemma_models(settings, step)
    return step


def test_the_imposed_models_pass_and_are_recorded():
    step = check_models(settings_with())
    assert step.evidence == {
        "primary": "gemma-4-31b-it",
        "coder_fallback": "gemma-4-26b-a4b-it",
        "roles": list(w5.ROLES),
    }


@pytest.mark.parametrize(
    "overrides, needle",
    [
        ({"LLM_MODEL": "gemini-2.5-flash"}, "LLM_MODEL doit valoir"),
        ({"LLM_PROVIDER": "openai"}, "LLM_PROVIDER doit valoir 'gemini'"),
        ({"LLM_MODEL_QA": "gemma-4-26b-a4b-it"}, "LLM_MODEL_QA"),
        ({"LLM_MODEL_CODER": "gemma-4-26b-a4b-it"}, "LLM_MODEL_CODER"),
        ({"LLM_PROVIDER_REVIEWER": "openai"}, "LLM_PROVIDER_REVIEWER"),
        ({"CODER_FALLBACK_MODELS": ""}, "CODER_FALLBACK_MODELS"),
        ({"CODER_FALLBACK_MODELS": "gemma-4-31b-it"}, "CODER_FALLBACK_MODELS"),
        ({"CODER_FALLBACK_MODELS": "gemma-4-26b-a4b-it,gemini-2.5-flash"}, "CODER_FALLBACK_MODELS"),
        ({"LLM_BASE_URL": "https://relais.exemple.test/v1"}, "endpoint officiel"),
        ({"LLM_BASE_URL_PLANNER": "http://127.0.0.1:9000"}, "endpoint officiel"),
        ({"CODER_SUBSCRIPTION": True}, "substitution interdit"),
    ],
)
def test_any_other_model_provider_endpoint_fallback_or_substitute_is_refused(overrides, needle):
    with pytest.raises(IncompleteValidation, match=needle):
        check_models(settings_with(**overrides))


def test_the_fallback_is_for_the_coder_only_no_role_override_may_carry_it():
    check_models(settings_with(LLM_MODEL_CODER=business.MODEL_PRIMARY, LLM_PROVIDER_CODER="gemini"))
    with pytest.raises(IncompleteValidation, match="LLM_MODEL_PLANNER"):
        check_models(settings_with(LLM_MODEL_PLANNER=business.MODEL_CODER_FALLBACK))


# ── relais budgétaire : jamais déduit d'un nom, d'une classe ou d'un flag ───────────────────────────────────────────────────


def check_broker(settings, proof=None):
    step = CampaignReport("preflight", "unit").declare("P10", "relais")
    w5.check_broker_selection(settings, step, proof=proof)
    return step


def test_a_direct_transport_is_refused_before_any_capability_is_asked():
    asked = []
    with pytest.raises(IncompleteValidation, match="LLM_TRANSPORT doit valoir 'budget_broker'"):
        check_broker(settings_with(LLM_TRANSPORT="direct"), proof=lambda s: asked.append(s))
    assert asked == []


def test_the_broker_selection_requires_the_public_capability_proof_of_lot_a(monkeypatch):
    import sys
    import types

    monkeypatch.setitem(sys.modules, "collegue.broker", types.ModuleType("collegue.broker"))
    with pytest.raises(IncompleteValidation, match="collegue.broker.capability_proof"):
        check_broker(settings_with(LLM_TRANSPORT="budget_broker"))


@pytest.mark.parametrize(
    "proof, needle",
    [
        (
            {"transport": "budget_broker", "accepted": False, "reason": "countTokens indisponible"},
            "countTokens indisponible",
        ),
        ({"transport": "direct", "accepted": True}, "non établie"),
        ({"transport": "budget_broker"}, "non établie"),
        ({"transport": "budget_broker", "accepted": "yes"}, "non établie"),
    ],
)
def test_a_capability_that_is_not_an_explicit_acceptance_is_refused(proof, needle):
    with pytest.raises(IncompleteValidation, match=needle):
        check_broker(settings_with(LLM_TRANSPORT="budget_broker"), proof=lambda settings: proof)


def test_an_accepted_capability_is_recorded_without_any_key_like_field():
    proof = {
        "transport": "budget_broker",
        "accepted": True,
        "instance": "BudgetBrokerTransport",
        "api_key": "NE-DOIT-PAS-FUIR",
    }

    step = check_broker(settings_with(LLM_TRANSPORT="budget_broker"), proof=lambda settings: proof)

    assert step.evidence["capability"] == {
        "transport": "budget_broker",
        "accepted": True,
        "instance": "BudgetBrokerTransport",
    }


# ── portée et masquage des clés (GOOGLE_API_KEY, clés par rôle) ────────────────────────────────────────────────────────────


def test_google_and_per_role_keys_are_keys_in_the_keyless_stages_and_masked_everywhere():
    env = {
        **business.campaign_environment("w5-test", "/var/lib/w5"),
        "GOOGLE_API_KEY": "AIza-fake-google-key-000",
        "LLM_API_KEY_CODER": "fake-coder-key-111",
    }
    report = CampaignReport("preflight", "unit", secrets=business.secret_values(env))
    step = report.declare("P03", "secrets")

    with pytest.raises(RuntimeError, match="GOOGLE_API_KEY"):
        business.check_secret_scope(env, report, step, stage="full")
    business.check_secret_scope(env, report, step, stage="launch")  # étape de lancement : légitime, noms seulement

    assert step.evidence["llm_secret_names_present"] == ["GOOGLE_API_KEY", "LLM_API_KEY_CODER"]
    step.evidence["oops"] = "AIza-fake-google-key-000 et fake-coder-key-111"
    machine = json.dumps(report.to_machine()) + report.to_human()
    assert "AIza-fake-google-key-000" not in machine and "fake-coder-key-111" not in machine


def test_the_health_command_is_the_independent_probe_and_free_of_shell_operators():
    from collegue.pilot.guard import _SHELL_OPERATORS

    command = business.health_command()

    assert not [op for op in _SHELL_OPERATORS if op in command], (
        "la garde de Phase 5 refuserait une commande à opérateurs shell"
    )
    assert command.startswith('python -c "exec(') and "\n" not in command
    assert business.validate_campaign_environment({**business.campaign_environment("w5-test", "/var/lib/w5")}) == []
    tampered = {**business.campaign_environment("w5-test", "/var/lib/w5"), "AUTO_REVERT_HEALTH_COMMAND": "pytest -q"}
    assert any("AUTO_REVERT_HEALTH_COMMAND" in problem for problem in business.validate_campaign_environment(tampered))


def test_the_w5_preflight_checks_are_hooked_into_the_preflight_in_order(policy):
    world = World()
    manifest_env = {}
    checks = w5.w5_preflight_checks(
        manifest_env, settings=settings_with(LLM_TRANSPORT="budget_broker"), clients=clients_for(world),
        campaign_id="w5-camp-001", run_tag="1-1", broker_proof=lambda s: {"transport": "budget_broker", "accepted": True},
    )  # fmt: skip

    assert [c[0] for c in checks] == ["P09-gemma-models", "P10-budget-broker", "P11-bootstrap", "P12-campaign-identity"]
    report = CampaignReport("preflight", "unit")
    for step_id, title, _check in checks:
        report.declare(step_id, title)
    for step_id, _title, check in checks:
        report.run(step_id, check)
    assert (
        report.step("P09-gemma-models").state == "succeeded" and report.step("P10-budget-broker").state == "succeeded"
    )
    assert report.step("P11-bootstrap").state == "incomplete_validation", (
        "aucun manifeste fourni : jamais une preuve par défaut"
    )
    assert report.step("P12-campaign-identity").state == "not_executed"
