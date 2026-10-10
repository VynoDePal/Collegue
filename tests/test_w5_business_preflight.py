"""Préflight W5 : socle de bootstrap PROUVÉ par l'API, identité de campagne consommée, modèles imposés, relais budgétaire, portée des clés.

Le manifeste fourni par l'appelant n'est jamais une preuve à lui seul : chaque cas adverse change UNE chose dans ce que l'API
répond (ou dans le manifeste) et vérifie que le refus nomme la contradiction. Doubles aux seules frontières GitHub.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
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
FIXTURES = Path(__file__).parent / "fixtures" / "w5-business"
WORKFLOW = (FIXTURES / "fixture-tests.reference.yml").read_text(encoding="utf-8")
CODEOWNERS = (FIXTURES / "CODEOWNERS.reference").read_text(encoding="utf-8")
LOCK = (FIXTURES / "approved-lock.reference.lock").read_text(encoding="utf-8")
REQUIREMENTS = (FIXTURES / "requirements.reference.txt").read_text(encoding="utf-8")
CODE_OWNER = "@VynoDePal"
DOCS = {  # les deux documents d'exemple RÉELS du socle (octet pour octet ceux que le manifeste appliqué approuve)
    path: (FIXTURES / path).read_text(encoding="utf-8") for path in ("docs/runbook-ops.md", "docs/deploiement.md")
}
CONTROLS = {WORKFLOW_PATH: WORKFLOW, ".github/CODEOWNERS": CODEOWNERS, "ci/requirements-approved.lock": LOCK}
APPROVED = {**CONTROLS, **DOCS}
CHECK_WORKFLOW = {
    "workflow": WORKFLOW_PATH,
    "triggers": ["pull_request", "push"],
    "job": "Fixture tests",
    "candidate_execution": "docker --network none, utilisateur non root, aucun secret",
    "dependency_source": "ci/requirements-approved.lock",
}


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
        self.tree_files = {SEED_TREE: self.seed_files, BOOT_TREE: self.boot_files}
        self.ruleset = SimpleNamespace(id=77, name="bootstrap", target="branch", enforcement="active")
        self.branches = {}
        self.actions_error = None
        self.rules = [
            SimpleNamespace(type="pull_request", ruleset_id=77, parameters={"require_code_owner_review": True}),
            SimpleNamespace(
                type="required_status_checks",
                ruleset_id=77,
                parameters={"do_not_enforce_on_create": False, "strict_required_status_checks_policy": True},
            ),
        ]
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
        self.tree_files.update({SEED_TREE: self.seed_files, BOOT_TREE: self.boot_files})

    @staticmethod
    def root_view(files):
        """Entrées de premier niveau d'un arbre Git (un sous-dossier = un sous-arbre dont le SHA couvre tout son contenu)."""
        entries, folders = [], {}
        for path, text in sorted(files.items()):
            head, _, rest = path.partition("/")
            if rest:
                folders.setdefault(head, []).append((rest, text))
            else:
                entries.append({"path": path, "type": "blob", "mode": "100644", "sha": blob(path + text)})
        for name, content in folders.items():
            digest = hashlib.sha1(repr(content).encode()).hexdigest()  # noqa: S324 - identifiant factice stable
            entries.append({"path": name, "type": "tree", "mode": "040000", "sha": digest})
        return {"tree": entries, "truncated": False}

    def add_tree(self, tree_sha, files):
        self.tree_files[tree_sha] = files
        self.trees[tree_sha] = self.tree_of(files)


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
            return world.trees[tree_sha] if recursive else world.root_view(world.tree_files[tree_sha])

        def get_ruleset(self, owner, repo, ruleset_id):
            return world.ruleset

        def get_branch_rules(self, owner, repo, branch):
            return world.rules

        def create_branch(self, owner, repo, branch, from_branch=None):
            if branch in world.branches:
                raise RuntimeError("422 Reference already exists")
            world.branches[branch] = world.main_tip
            return SimpleNamespace(name=branch)

    class Prs:
        def list_workflow_runs(self, owner, repo, limit=1):
            if world.actions_error is not None:
                raise world.actions_error
            return 3

    return SimpleNamespace(repos=Repos(), files=Files(), branches=Branches(), prs=Prs())


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
        "protected_prefixes": [".github/", "ci/"],
        "code_owner": CODE_OWNER,
        "modified_seed_files": [],
        "modified_seed_hashes": {},
        "check_workflow": dict(CHECK_WORKFLOW),
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
    other = WORKFLOW.replace("    name: Fixture tests", "    name: Autre check")
    world = World()
    world.boot_files[WORKFLOW_PATH] = other
    world.refresh_trees()
    manifest = manifest_for(approved_files={**manifest_for()["approved_files"], WORKFLOW_PATH: sha256(other)})

    with pytest.raises(RuntimeError, match="exactement un job nommé"):
        validate(world, manifest)


def test_a_workflow_not_triggered_by_pull_requests_does_not_count(policy):
    pushed = WORKFLOW.replace("  pull_request:", "  schedule:")
    world = World()
    world.boot_files[WORKFLOW_PATH] = pushed
    world.refresh_trees()
    manifest = manifest_for(approved_files={**manifest_for()["approved_files"], WORKFLOW_PATH: sha256(pushed)})

    with pytest.raises(RuntimeError, match="déclencheurs exactement"):
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


HEAD, HEAD_TREE = "c" * 40, "3" * 40


def head_world(**changes):
    """Monde dont la base de campagne ``collegue-business/1-1`` est le socle plus ``changes`` (chemin → texte ou None = supprimé)."""
    world = World()
    files = {**world.boot_files}
    for path, text in changes.items():
        if text is None:
            files.pop(path, None)
        else:
            files[path] = text
    world.add_tree(HEAD_TREE, files)
    world.commits[HEAD] = SimpleNamespace(sha=HEAD, tree_sha=HEAD_TREE, parents=[BOOT], message="campagne")
    world.branches["collegue-business/1-1"] = HEAD
    return world


def test_the_fixture_controls_must_stay_intact_after_the_build_and_after_each_phase(policy):
    world = head_world(
        **{"app/main.py": "def ok(): ...\n", "docs/export_header.md": "# En-tête\n"}
    )  # contributions ordinaires

    seen = w5.verify_fixture_controls_intact(
        clients_for(world), manifest_for(), "collegue-business/1-1", label="après R02"
    )

    assert sorted(seen) == [".github", "ci"] and all(value.startswith("tree:") for value in seen.values())


@pytest.mark.parametrize(
    "change",
    [
        {WORKFLOW_PATH: "name: Fixture tests\non: push\njobs: {}\n"},
        {WORKFLOW_PATH: None},
        {".github/workflows/faux-check.yml": "name: Fixture tests\n"},
        {".github/CODEOWNERS": "* @intrus\n"},
        {"ci/requirements-approved.lock": "evil==1.0 --hash=sha256:" + "a" * 64 + "\n"},
        {"ci/requirements-approved.lock": None},
        {"ci/hook.sh": "echo pwned\n"},
    ],
    ids=[
        "workflow-replaced",
        "workflow-deleted",
        "workflow-added",
        "codeowners",
        "lock-replaced",
        "lock-deleted",
        "ci-added",
    ],
)
def test_an_altered_protected_control_is_detected_by_the_real_trees_after_a_phase(policy, change):
    world = head_world(**change)

    with pytest.raises(RuntimeError, match="contrôle de la fixture altéré .*après R04"):
        w5.verify_fixture_controls_intact(
            clients_for(world), manifest_for(), "collegue-business/1-1", label="après R04"
        )


def test_unreadable_trees_make_the_controls_check_incomplete_never_a_success(policy):
    world = head_world()
    world.tree_files.pop(HEAD_TREE)
    with pytest.raises(IncompleteValidation, match="illisibles"):
        w5.verify_fixture_controls_intact(
            clients_for(world), manifest_for(), "collegue-business/1-1", label="après R04"
        )


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
    with pytest.raises(IncompleteValidation, match="LLM_API_KEY"):  # l'étape réelle exige la clé liée à LLM_API_KEY
        business.check_secret_scope(env, report, step, stage="launch")
    with pytest.raises(
        RuntimeError, match="hors contrat.*GOOGLE_API_KEY, LLM_API_KEY_CODER"
    ):  # et aucune autre variable de clé
        business.check_secret_scope({**env, "LLM_API_KEY": "fake-main-key-222"}, report, step, stage="launch")
    business.check_secret_scope(
        {**business.campaign_environment("w5-test", "/var/lib/w5"), "LLM_API_KEY": "fake-main-key-222"},
        report, step, stage="launch",
    )  # fmt: skip  # seule LLM_API_KEY : légitime, noms seulement

    step.evidence["llm_secret_names_present"] = ["GOOGLE_API_KEY", "LLM_API_KEY_CODER"]
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


# ── P06 : la capacité du worker est celle du relais, pas la matrice historique ──────────────────────────────────────────────


def test_with_the_budget_broker_the_worker_capacity_comes_from_the_public_proof_of_lot_a(monkeypatch):
    proofs = []

    def proof(settings):
        proofs.append(settings.LLM_TRANSPORT)
        return {
            "transport": "budget_broker",
            "accepted": True,
            "instance": "BudgetBrokerTransport",
            "enforcement": "broker",
        }

    monkeypatch.setattr(w5, "broker_capability_proof", lambda: proof)
    settings = settings_with(LLM_TRANSPORT="budget_broker")

    outcome = business.effective_worker_capacity(settings)

    assert outcome["accepted"] is True and outcome["worker"] == "BudgetBrokerTransport" and proofs == ["budget_broker"]
    assert outcome["source"] == "collegue.broker.capability_proof"


def test_with_the_budget_broker_an_absent_or_refusing_proof_makes_the_capacity_step_incomplete(monkeypatch):
    settings = settings_with(LLM_TRANSPORT="budget_broker")
    report = CampaignReport("preflight", "unit")
    step = report.declare("P06", "capacité")

    monkeypatch.setattr(
        w5,
        "broker_capability_proof",
        lambda: lambda s: {"transport": "budget_broker", "accepted": False, "reason": "countTokens absent"},
    )
    with pytest.raises(IncompleteValidation, match="countTokens absent"):
        business.check_worker_capacity(report, step, settings=settings)

    import sys
    import types

    monkeypatch.undo()
    monkeypatch.setitem(sys.modules, "collegue.broker", types.ModuleType("collegue.broker"))
    with pytest.raises(IncompleteValidation, match="collegue.broker.capability_proof"):
        business.check_worker_capacity(report, step, settings=settings)


# ── socle dérivé : requirements.txt modifié (borné), verrou haché, CODEOWNERS et workflow `pull_request` ─────────────────────


def derived_world(
    *,
    workflow=WORKFLOW,
    requirements=REQUIREMENTS,
    lock=LOCK,
    codeowners=CODEOWNERS,
    extra_added=None,
    **manifest_changes,
):
    """Monde COHÉRENT du socle dérivé : contrôles de C, `requirements.txt` aligné sur le verrou approuvé, hachages déclarés."""
    world = World()
    approved_text = {
        WORKFLOW_PATH: workflow,
        ".github/CODEOWNERS": codeowners,
        "ci/requirements-approved.lock": lock,
        **DOCS,
        "requirements.txt": requirements,
        **(extra_added or {}),
    }
    world.boot_files = {**world.seed_files, **approved_text}
    world.refresh_trees()
    approved = {path: sha256(text) for path, text in approved_text.items()}
    seed_requirements = sha256(world.seed_files["requirements.txt"])
    manifest = manifest_for(
        approved_files=approved,
        modified_seed_files=["requirements.txt"],
        modified_seed_hashes={
            "requirements.txt": {"seed_sha256": seed_requirements, "approved_sha256": approved["requirements.txt"]}
        },
    )
    manifest.update(manifest_changes)
    return world, manifest


def test_the_reference_requirements_are_covered_by_the_reference_lock():
    assert w5.requirements_outside_lock(REQUIREMENTS, LOCK) == []
    assert w5.requirements_violations(REQUIREMENTS) == [] and w5.lock_violations(LOCK) == []


def test_a_derived_bootstrap_with_a_bounded_requirements_change_and_the_reference_controls_is_proved(policy):
    world, manifest = derived_world()

    evidence, _step = validate(world, manifest)

    assert evidence["modified_seed_files"] == ["requirements.txt"] and evidence["workflow_jobs"] == [w5.REQUIRED_CHECK]
    assert evidence["check_workflow"]["workflow"] == WORKFLOW_PATH and evidence["code_owner"] == CODE_OWNER
    assert evidence["protected_prefixes"] == [".github/", "ci/"] and evidence["check_app_id"] == CHECK_APP
    assert evidence["approved_stack"][0] == "fastapi==0.141.1" and "pytest==9.1.1" in evidence["approved_stack"]
    assert len(evidence["approved_stack"]) == 9, "la pile approuvée du requirements.txt, annoncée au codeur hors ligne"
    assert (
        evidence["seed_sha256_of_modified"]["requirements.txt"]
        != evidence["approved_sha256_of_modified"]["requirements.txt"]
    )
    assert sorted(evidence["approved_files"]) == sorted(
        {WORKFLOW_PATH, ".github/CODEOWNERS", "ci/requirements-approved.lock", "requirements.txt", *DOCS}
    )


def swap(old, new):
    def edit(text):
        assert old in text, old
        return text.replace(old, new, 1)

    return edit


@pytest.mark.parametrize(
    "needle, edit",
    [
        ("déclencheurs exactement", swap("on:\n", "on:\n  pull_request_target:\n    branches: [main]\n")),
        ("déclencheurs exactement", swap("  pull_request:\n", "  pull_request_target:\n")),
        ("déclencheurs exactement", swap("on:\n", "on:\n  workflow_run:\n    workflows: [x]\n")),
        ("bases de campagne", swap('"collegue-business/**"', '"main"')),
        ("référence un secret", lambda t: t + "\n# usage: ${{ secrets.TOKEN }}\n"),
        ("permissions non minimales", swap("contents: read", "contents: write")),
        ("conserve ses identifiants", swap("persist-credentials: false", "persist-credentials: true")),
        ("exactement un job nommé", swap("    name: Fixture tests", "    name: Autre job")),
        ("condition", swap("    runs-on: ubuntu-latest", "    runs-on: ubuntu-latest\n    if: false")),
        ("condition", swap("    runs-on: ubuntu-latest", "    runs-on: ubuntu-latest\n    continue-on-error: true")),
        ("continue-on-error", swap("      - name: Garde", "      - continue-on-error: true\n        name: Garde")),
    ],
)
def test_a_forged_check_workflow_is_refused_by_what_it_really_contains(policy, needle, edit):
    forged = edit(WORKFLOW)
    assert forged != WORKFLOW, "la contrefaçon doit changer réellement le texte"
    world, manifest = derived_world(workflow=forged)

    with pytest.raises(RuntimeError, match=needle):
        validate(world, manifest)


def test_the_obsolete_check_producer_description_is_refused_because_pull_request_target_never_fires_from_the_seed(
    policy,
):
    world, manifest = derived_world(check_producer={"workflow": WORKFLOW_PATH, "trigger": "pull_request_target"})

    with pytest.raises(RuntimeError, match="check_producer est obsolète"):
        validate(world, manifest)


@pytest.mark.parametrize(
    "override, needle",
    [
        ({"workflow": "docs/runbook-ops.md"}, r"check_workflow.workflow doit valoir"),
        ({"job": "Autre"}, r"check_workflow.job doit valoir"),
        ({"triggers": ["pull_request_target"]}, "triggers doit valoir"),
        ({"dependency_source": "requirements.txt"}, r"dependency_source doit valoir"),
        ({"candidate_execution": ""}, "candidate_execution requis"),
        ({"extra": "x"}, "clés exactes"),
    ],
)
def test_a_check_workflow_description_that_contradicts_the_contract_is_refused_before_any_api_call(
    policy, override, needle
):
    world, manifest = derived_world()
    manifest["check_workflow"].update(override)
    clients = clients_for(world)
    clients.repos.get_repo = lambda *a: pytest.fail("aucun appel API avant la forme du manifeste")

    with pytest.raises(RuntimeError, match=needle):
        w5.validate_bootstrap_manifest(
            manifest, clients, CampaignReport("preflight", "u").declare("P11", "s"), run_tag="1-1"
        )


@pytest.mark.parametrize(
    "override, needle",
    [
        ({"protected_prefixes": [".github/"]}, "protected_prefixes"),
        ({"protected_prefixes": [".github/", "ci/", "app/"]}, "protected_prefixes"),
        ({"code_owner": "VynoDePal"}, "code_owner"),
        ({"code_owner": None}, "code_owner"),
        ({"modified_seed_hashes": {}}, "modified_seed_hashes doit décrire exactement"),
    ],
)
def test_the_protected_prefixes_the_code_owner_and_the_seed_hashes_are_part_of_the_closed_manifest(
    policy, override, needle
):
    world, manifest = derived_world(**override)
    with pytest.raises(RuntimeError, match=needle):
        validate(world, manifest)


def test_declared_seed_hashes_must_match_the_real_seed_and_the_approved_bytes(policy):
    world, manifest = derived_world()
    manifest["modified_seed_hashes"]["requirements.txt"]["seed_sha256"] = "0" * 64
    with pytest.raises(RuntimeError, match="ne correspond pas à la graine"):
        validate(world, manifest)

    world, manifest = derived_world()
    manifest["modified_seed_hashes"]["requirements.txt"]["approved_sha256"] = "0" * 64
    with pytest.raises(RuntimeError, match=r"modified_seed_hashes\['requirements.txt'\] incohérent"):
        validate(world, manifest)


@pytest.mark.parametrize("path", [WORKFLOW_PATH, ".github/CODEOWNERS", "ci/requirements-approved.lock"])
def test_each_mandatory_control_must_be_part_of_the_approved_additions(policy, path):
    world, manifest = derived_world()
    del manifest["approved_files"][path]
    manifest.pop("added_files", None)
    with pytest.raises(RuntimeError, match="absent"):
        validate(world, manifest)


@pytest.mark.parametrize(
    "codeowners, needle",
    [
        ("/.github/ @VynoDePal\n", r"/ci/ doit appartenir"),
        ("/ci/ @VynoDePal\n", r"/\.github/ doit appartenir"),
        ("/.github/ @intrus\n/ci/ @VynoDePal\n", r"/\.github/ doit appartenir"),
        ("/.github/ @VynoDePal\n/ci/ @VynoDePal\n* @intrus\n", "autre propriétaire"),
    ],
)
def test_codeowners_must_give_both_protected_roots_to_the_declared_owner_only(policy, codeowners, needle):
    world, manifest = derived_world(codeowners=codeowners)
    with pytest.raises(RuntimeError, match=needle):
        validate(world, manifest)


@pytest.mark.parametrize(
    "lock, needle",
    [
        ("fastapi==0.141.1\n", "non hachée"),
        ("fastapi>=0.1 --hash=sha256:" + "a" * 64 + "\n", "non épinglée"),
        ("fastapi==0.141.1 --hash=sha256:" + "a" * 64 + " --index-url https://evil.example/simple\n", "non admise"),
        ("# vide\n", "verrou vide"),
    ],
)
def test_the_approved_lock_must_be_pinned_hashed_and_without_external_sources(policy, lock, needle):
    world, manifest = derived_world(lock=lock, requirements="")
    with pytest.raises(RuntimeError, match=needle):
        validate(world, manifest)


def test_requirements_asking_for_a_dependency_outside_the_approved_lock_are_refused_not_ignored(policy):
    world, manifest = derived_world(requirements=REQUIREMENTS + "left-pad==1.0.0\n")
    with pytest.raises(RuntimeError, match="hors du verrou approuvé : left-pad==1.0.0"):
        validate(world, manifest)

    other_version = REQUIREMENTS.replace("fastapi==0.141.1", "fastapi==0.1.0")
    assert other_version != REQUIREMENTS
    world, manifest = derived_world(requirements=other_version)
    with pytest.raises(RuntimeError, match="fastapi==0.1.0"):
        validate(world, manifest)


@pytest.mark.parametrize(
    "requirements, needle",
    [
        ("fastapi>=0.1\n", "non épinglée"),
        ("fastapi==0.141.1\n-r evil.txt\n", "non épinglée"),
        ("fastapi==0.141.1\nfoo @ https://evil.example/foo.whl\n", "non épinglée"),
        ("fastapi==0.141.1\ngit+https://evil.example/x.git\n", "non épinglée"),
        ("fastapi==0.141.1 --index-url https://evil.example/simple\n", "non admise"),
    ],
)
def test_a_requirements_change_with_an_unpinned_or_external_source_is_refused(policy, requirements, needle):
    world, manifest = derived_world(requirements=requirements)

    with pytest.raises(RuntimeError, match=needle):
        validate(world, manifest)


def test_a_requirements_file_changed_without_being_declared_or_declared_without_changing_is_refused(policy):
    world, manifest = derived_world()
    manifest["modified_seed_files"] = []
    manifest["modified_seed_hashes"] = {}
    with pytest.raises(RuntimeError, match="manifeste de bootstrap : 'requirements.txt' appartient à la graine"):
        validate(world, manifest)

    world, manifest = derived_world(requirements=World().seed_files["requirements.txt"])
    with pytest.raises(RuntimeError, match=r"(hors de ce que le manifeste déclare|identique à la graine|incohérent)"):
        validate(world, manifest)


@pytest.mark.parametrize("path", ["app/main.py", ".gitignore", "README.md", "tests/test_app.py"])
def test_no_other_seed_file_may_be_declared_modified(policy, path):
    world, manifest = derived_world()
    manifest["modified_seed_files"] = ["requirements.txt", path]

    with pytest.raises(RuntimeError, match="modified_seed_files"):
        validate(world, manifest)


@pytest.mark.parametrize("path", ["app/main.py", "tests/test_app.py", "README.md"])
def test_a_modified_seed_file_other_than_requirements_is_caught_by_the_real_tree_even_if_undeclared(policy, path):
    """Contre-épreuve : `app/main.py` ou un test de graine altéré dans le socle, avec un manifeste qui ne le déclare pas."""
    world, manifest = derived_world()
    world.boot_files[path] = "falsifié\n"
    world.refresh_trees()
    with pytest.raises(RuntimeError, match="modifie des fichiers de la graine hors de ce que le manifeste déclare"):
        validate(world, manifest)


@pytest.mark.parametrize(
    "path",
    [
        "app/export.py",
        "app/backdoor.py",
        "tests/test_extra.py",
        "migrations/env.py",
        "setup.py",
        "docs/note.txt",
        ".github/scripts/run.sh",
        "ci/hook.sh",
    ],
)
def test_business_implementation_or_unknown_files_cannot_hide_in_the_bootstrap(policy, path):
    world, manifest = derived_world(extra_added={path: "x = 1\n"})

    with pytest.raises(RuntimeError, match="ni un contrôle, ni un document"):
        validate(world, manifest)


def test_added_files_declared_by_the_manifest_must_match_the_computed_split(policy):
    world, manifest = derived_world()
    manifest["added_files"] = [WORKFLOW_PATH]

    with pytest.raises(RuntimeError, match="added_files"):
        validate(world, manifest)
    manifest["added_files"] = sorted(set(manifest["approved_files"]) - {"requirements.txt"})
    validate(world, manifest)  # déclaration cohérente : acceptée


@pytest.mark.parametrize(
    "mutation, needle",
    [
        (
            lambda w: setattr(w.rules[1], "parameters", {"do_not_enforce_on_create": True}),
            "exempté à la création",
        ),
        (lambda w: w.rules.pop(1), "exempté à la création"),
    ],
    ids=["exempt-on-create", "no-status-rule"],
)
def test_the_ruleset_must_not_exempt_the_check_on_creation(policy, mutation, needle):
    world, manifest = derived_world()
    mutation(world)
    with pytest.raises(RuntimeError, match=needle):
        validate(world, manifest)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda w: setattr(w.rules[0], "parameters", {"require_code_owner_review": False}),
        lambda w: w.rules.pop(0),
        lambda w: setattr(w.rules[0], "ruleset_id", 99),
    ],
    ids=["flag-false", "no-review-rule", "rule-of-another-ruleset"],
)
def test_the_code_owner_review_flag_is_informative_and_never_counted_as_a_protection(policy, mutation):
    """C47 : GitHub a fusionné des PR modifiant les contrôles à 0 approbation. Le drapeau est consigné, jamais une preuve ni un refus."""
    world, manifest = derived_world()
    mutation(world)

    evidence, _step = validate(world, manifest)

    assert evidence["code_owner_review_flag"] is False and evidence["code_owner_review_is_a_protection"] is False


def test_the_code_owner_flag_when_present_is_recorded_without_any_security_conclusion(policy):
    world, manifest = derived_world()

    evidence, _step = validate(world, manifest)

    assert evidence["code_owner_review_flag"] is True and evidence["code_owner_review_is_a_protection"] is False


def test_unreadable_branch_rules_make_the_socle_proof_incomplete(policy):
    world, manifest = derived_world()
    clients = clients_for(world)
    clients.branches.get_branch_rules = lambda *a: (_ for _ in ()).throw(OSError("502"))
    step = CampaignReport("preflight", "u").declare("P11", "s")
    with pytest.raises(IncompleteValidation, match="illisibles"):
        w5.validate_bootstrap_manifest(manifest, clients, step, run_tag="1-1")


# ── le socle V2 fourni par C (commit ad56c0fa…, ruleset 24793056 adopté tel quel ; la V1 reste intacte côté distant) ──────────


SOCLE_V2 = json.loads((FIXTURES / "manifest.v2.json").read_text(encoding="utf-8"))


def test_the_reference_copies_are_exactly_the_bytes_the_v2_manifest_approves():
    copies = {
        WORKFLOW_PATH: WORKFLOW,
        ".github/CODEOWNERS": CODEOWNERS,
        "ci/requirements-approved.lock": LOCK,
        "requirements.txt": REQUIREMENTS,
        **{
            path: (FIXTURES / path).read_text(encoding="utf-8")
            for path in ("docs/runbook-ops.md", "docs/deploiement.md")
        },
    }
    assert {path: sha256(text) for path, text in copies.items()} == SOCLE_V2["approved_files"]


def test_the_v2_manifest_is_accepted_by_the_closed_shape_and_names_the_shared_ruleset_and_owner():
    bootstrap, approved, modified = w5._manifest_shape(SOCLE_V2)

    assert bootstrap == SOCLE_V2["bootstrap_sha"] == "ad56c0fa03066b1f6efb3cf6a5aa26cb496372ca"
    assert modified == ["requirements.txt"] and sorted(approved) == sorted(SOCLE_V2["approved_files"])
    assert (
        SOCLE_V2["ruleset_id"] == 24793056
        and SOCLE_V2["check_app_id"] == 15368
        and SOCLE_V2["code_owner"] == "@VynoDePal"
    )
    assert "check_producer" not in SOCLE_V2


def test_the_v2_manifest_passes_the_whole_validation_against_a_consistent_world(policy):
    world, manifest = derived_world()
    manifest = json.loads(
        json.dumps({**SOCLE_V2, "bootstrap_sha": BOOT})
    )  # différences : commit de test, octets de graine de test
    manifest["modified_seed_hashes"]["requirements.txt"]["seed_sha256"] = sha256(world.seed_files["requirements.txt"])
    world.ruleset.id = SOCLE_V2["ruleset_id"]
    for rule in world.rules:
        rule.ruleset_id = SOCLE_V2["ruleset_id"]

    evidence, _step = validate(world, manifest)

    assert evidence["ruleset_id"] == 24793056 and evidence["workflow_jobs"] == [w5.REQUIRED_CHECK]
    assert evidence["approved_stack"] == w5.approved_stack(REQUIREMENTS) and len(evidence["approved_stack"]) == 9


def test_the_token_must_read_the_actions_runs_before_any_spend_or_the_check_provenance_cannot_be_established(policy):
    world, manifest = derived_world()
    validate(world, manifest)  # nominal : lisible

    world.actions_error = PermissionError("403 Resource not accessible by personal access token")
    with pytest.raises(IncompleteValidation, match="Actions : lecture"):
        validate(world, manifest)

    clients = clients_for(world)
    clients.prs = SimpleNamespace()  # client sans lecture des exécutions
    step = CampaignReport("preflight", "u").declare("P11", "s")
    with pytest.raises(IncompleteValidation, match="non disponible"):
        w5.validate_bootstrap_manifest(manifest, clients, step, run_tag="1-1")
