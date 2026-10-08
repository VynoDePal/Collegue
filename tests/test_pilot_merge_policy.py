"""Politique de fusion COMMUNE (``collegue.pilot.merge_policy``) contre un faux serveur GitHub REST.

Les VRAIS clients ``PRCommands`` / ``BranchCommands`` sont branchés sur ``tests/github_fake_server.py`` : pagination,
parsing, erreurs et corps de requêtes sont ceux de la production. Chaque test adverse a son TÉMOIN BÉNIN (le monde
sain est approuvé) et vérifie le MOTIF du refus (code + texte), jamais un ``AttributeError`` de double incomplet.
"""

from __future__ import annotations

import pytest
from github_fake_server import (
    AUDIT,
    DOCKER,
    FIVE_CHECKS,
    GITHUB_ACTIONS_APP,
    OWNER,
    PYTEST311,
    REPO,
    RUFF,
    FakeGitHubServer,
    ProofStore,
    sha_of,
)

from collegue.pilot import merge_policy as mp
from collegue.pilot.merge_policy import MergeRefused, verify_merge_candidate
from collegue.state import ProjectStateManager

HEAD_REF = "collegue/issue-1"


@pytest.fixture
def world(tmp_path):
    server = FakeGitHubServer()
    server.add_ruleset(1)  # chemin sain par défaut : ruleset actif, strict, non contournable, cinq checks
    manager = ProjectStateManager.from_url(f"sqlite:///{tmp_path / 's.db'}", create=True)
    project_id = manager.create_project(name="merge-policy")
    proofs = ProofStore()
    server.open_pr(11, head_ref=HEAD_REF, tree="tree-task-1")
    proofs.add(server, project_id, 11)

    class W:
        pass

    w = W()
    w.server, w.manager, w.project_id, w.proofs, w.clients = server, manager, project_id, proofs, server.clients()
    return w


def verify(w, **overrides):
    kwargs = dict(
        project_id=w.project_id,
        owner=OWNER,
        repo=REPO,
        base="main",
        pr_number=11,
        expected_phase="build",
        expected_head_branch=HEAD_REF,
        proof_loader=w.proofs.loader,
    )
    kwargs.update(overrides)
    return verify_merge_candidate(w.clients, w.manager, **kwargs)


def refused(w, code=None, text=None, **overrides):
    with pytest.raises(MergeRefused) as excinfo:
        verify(w, **overrides)
    if code is not None:
        assert excinfo.value.code == code, excinfo.value.reason
    if text is not None:
        assert text in excinfo.value.reason, excinfo.value.reason
    assert not w.server.merge_calls(), "une validation ne fusionne jamais"
    return excinfo.value


# ── témoin bénin ─────────────────────────────────────────────────────────────────────────────────


def test_healthy_ruleset_world_is_approved_with_verified_anchors(world):
    w = world
    approval = verify(w)

    pr = w.server.prs[11]
    assert approval.head_sha == pr["head"]["sha"]
    assert approval.base_sha == w.server.base_tip == pr["base"]["sha"]
    assert approval.tree_sha == sha_of("tree-task-1")
    assert approval.proof_id == w.proofs.proofs[(w.project_id, OWNER, REPO, 11, approval.head_sha)].proof_id
    assert approval.method == "squash" and approval.phase == "build"
    assert {c.context for c in approval.server_policy.required_checks} == set(FIVE_CHECKS)
    assert approval.server_policy.strict_sources == ("ruleset:1",)
    assert w.proofs.requests == [(w.project_id, OWNER, REPO, 11, approval.head_sha)], (
        "preuve chargée pour la tête observée"
    )
    assert not w.server.merge_calls()


def test_classic_only_world_is_approved(world):
    w = world
    w.server.rules.clear()
    w.server.rulesets.clear()
    w.server.protect_classic()

    approval = verify(w)

    assert approval.server_policy.strict_sources == ("classic",)
    assert {c.context for c in approval.server_policy.required_checks} == set(FIVE_CHECKS)


def test_legacy_contexts_only_classic_protection_is_approved(world):
    w = world
    w.server.rules.clear()
    w.server.rulesets.clear()
    w.server.protect_classic(legacy_contexts_only=True)

    approval = verify(w)

    assert all(c.app_id is None for c in approval.server_policy.required_checks)


# ── PR et identités ──────────────────────────────────────────────────────────────────────────────


def test_unknown_pr_is_refused_as_api_error(world):
    refused(world, mp.CODE_API, "PR #99", pr_number=99)


@pytest.mark.parametrize(
    "mutate, code, text",
    [
        (lambda s: s.prs[11].update(draft=True), mp.CODE_STATE, "brouillon"),
        (lambda s: s.prs[11].update(state="closed"), mp.CODE_STATE, "fermée"),
        (lambda s: s.prs[11]["base"].update(ref="develop"), mp.CODE_STATE, "base de la PR inattendue"),
    ],
    ids=["draft", "closed", "other-base"],
)
def test_pr_state_refusals(world, mutate, code, text):
    mutate(world.server)
    refused(world, code, text)


def test_already_merged_pr_is_never_re_approved(world):
    world.server.prs[11].update(state="closed", merged=True, merge_commit_sha=sha_of("m"))
    refused(world, mp.CODE_ALREADY_MERGED)


def test_preexisting_pr_on_another_head_branch_is_refused(world):
    refused(world, mp.CODE_STATE, "PR préexistante différente", expected_head_branch="collegue/issue-2")


def test_rebase_method_is_refused(world):
    refused(world, None, "rebase", method="rebase")


# ── preuve de livraison ──────────────────────────────────────────────────────────────────────────


def test_missing_proof_for_the_observed_head_is_refused(world):
    w = world
    w.proofs.proofs.clear()
    refused(w, mp.CODE_NO_PROOF, "preuve de livraison absente ou invalide")


def test_head_moved_after_the_proof_has_no_proof(world):
    w = world
    w.server.push_to_pr_head(11, tree="tree-sneaky")
    w.server.set_checks(w.server.prs[11]["head"]["sha"], {n: "success" for n in FIVE_CHECKS})
    error = refused(w, mp.CODE_NO_PROOF)
    assert w.proofs.requests[-1][-1] == w.server.prs[11]["head"]["sha"], "la preuve est demandée pour la NOUVELLE tête"
    assert "aucune preuve durable" in error.reason


@pytest.mark.parametrize(
    "override, text",
    [
        ({"passed": False}, "passed n'est pas strictement vrai"),
        ({"passed": "yes"}, "passed n'est pas strictement vrai"),
        ({"phase": "improve"}, "phase 'improve' != 'build'"),
        ({"project_id": 999}, "projet différent"),
        ({"pr_number": 12}, "PR différente"),
        ({"owner": "other"}, "dépôt différent"),
        ({"base_sha": "not-a-sha"}, "base_sha invalide"),
        ({"tree_sha": ""}, "tree_sha invalide"),
        ({"proof_id": "short"}, "proof_id invalide"),
    ],
)
def test_incoherent_proof_is_refused(world, override, text):
    w = world
    w.proofs.proofs.clear()
    # le chargeur renvoie bien l'objet demandé, mais ses champs sont incohérents : revalidés par B
    proof = w.proofs.add(w.server, w.project_id, 11, **override)
    w.proofs.proofs[(w.project_id, OWNER, REPO, 11, w.server.prs[11]["head"]["sha"])] = proof
    refused(w, mp.CODE_NO_PROOF, text)


def test_proof_tree_different_from_the_remote_head_tree_is_refused(world):
    w = world
    w.proofs.proofs.clear()
    w.proofs.add(w.server, w.project_id, 11, tree_sha=sha_of("tree-reviewed-but-not-published"))
    refused(w, mp.CODE_NO_PROOF, "tree distant")


def test_missing_proof_module_is_never_a_dispensation(world, monkeypatch):
    import sys

    monkeypatch.setitem(sys.modules, "collegue.executor.delivery_proof", None)  # import impossible
    with pytest.raises(MergeRefused) as excinfo:
        verify_merge_candidate(
            world.clients,
            world.manager,
            project_id=world.project_id,
            owner=OWNER,
            repo=REPO,
            base="main",
            pr_number=11,
            expected_phase="build",
        )
    assert excinfo.value.code == mp.CODE_NO_PROOF and "indisponible" in excinfo.value.reason
    assert not world.server.merge_calls()


def test_loader_failure_is_a_refusal_not_a_crash(world):
    def boom(manager, project_id, **kw):
        raise OSError("base d'état illisible")

    refused(world, mp.CODE_NO_PROOF, "illisible", proof_loader=boom)


# ── base et tête ─────────────────────────────────────────────────────────────────────────────────


def test_base_moved_before_validation_is_refused(world):
    w = world
    w.server.advance_base(tree="tree-someone-else")
    refused(w, mp.CODE_MOVED, "a avancé depuis les contrôles")


def test_base_moved_during_validation_is_refused(world):
    w = world
    w.server.on_get(r"/git/ref/heads/main$", lambda s: s.advance_base(tree="tree-race"), nth=2)
    refused(w, mp.CODE_MOVED, "a bougé pendant la validation")


def test_head_moved_during_validation_is_refused(world):
    w = world
    w.server.on_get(r"/pulls/11$", lambda s: s.push_to_pr_head(11, tree="tree-late"), nth=2)
    # 1re lecture = avant la preuve, 2e lecture = relecture finale : la tête a changé entre-temps
    error = refused(w, mp.CODE_MOVED)
    assert "a changé pendant la validation" in error.reason


def test_head_not_descending_from_the_proof_base_is_refused(world):
    w = world
    w.server.advance_base(tree="tree-x")
    w.proofs.proofs.clear()
    stale_base = w.server.commits[w.server.base_tip]["parents"][0]
    w.server.prs[11]["head"]["sha"] = w.server.commit([stale_base], tree="tree-task-1", message="diverged")
    w.server.set_checks(w.server.prs[11]["head"]["sha"], {n: "success" for n in FIVE_CHECKS})
    w.proofs.add(w.server, w.project_id, 11, base_sha=w.server.base_tip)
    refused(w, mp.CODE_MOVED, "ne descend pas de la base contrôlée")


def test_expected_head_from_a_previous_evaluation_must_still_match(world):
    refused(world, mp.CODE_MOVED, "tête de la PR a bougé", expected_head_sha=sha_of("another-head"))


# ── checks requis ────────────────────────────────────────────────────────────────────────────────


def set_states(w, **states):
    head = w.server.prs[11]["head"]["sha"]
    w.server.set_checks(
        head, {name: states.get(name, "success") for name in FIVE_CHECKS if states.get(name) != "absent"}
    )


@pytest.mark.parametrize(
    "state", ["failure", "cancelled", "timed_out", "skipped", "neutral", "action_required", "stale"]
)
def test_required_check_not_successful_is_refused(world, state):
    set_states(world, **{DOCKER: state})
    error = refused(world, mp.CODE_FAILED_CHECK)
    assert DOCKER in error.reason and state in error.reason


@pytest.mark.parametrize("state", ["pending", "queued", "in_progress"])
def test_pending_required_check_is_retryable_not_merged(world, state):
    set_states(world, **{PYTEST311: state})
    error = refused(world, mp.CODE_PENDING)
    assert error.retryable and PYTEST311 in error.reason


def test_missing_required_check_is_refused_and_retryable(world):
    set_states(world, **{AUDIT: "absent"})
    error = refused(world, mp.CODE_MISSING_CHECK)
    assert error.retryable and AUDIT in error.reason


def test_all_checks_absent_is_refused(world):
    world.server.check_runs.clear()
    refused(world, mp.CODE_MISSING_CHECK)


def test_non_required_red_check_blocks_but_a_skipped_optional_one_does_not(world):
    w = world
    head = w.server.prs[11]["head"]["sha"]
    w.server.set_checks(head, {**{n: "success" for n in FIVE_CHECKS}, "optional-e2e": "skipped"})
    assert verify(w).head_sha == head  # témoin : un optionnel sauté ne bloque pas

    w.server.set_checks(head, {**{n: "success" for n in FIVE_CHECKS}, "optional-lint": "failure"})
    refused(w, mp.CODE_FAILED_CHECK, "optional-lint")


def test_check_from_the_wrong_app_does_not_satisfy_a_required_app(world):
    w = world
    head = w.server.prs[11]["head"]["sha"]
    w.server.set_checks(head, {n: "success" for n in FIVE_CHECKS if n != RUFF}, app_id=GITHUB_ACTIONS_APP)
    w.server.check_runs[head].append(
        {"name": RUFF, "status": "completed", "conclusion": "success", "app": {"id": 424242}}
    )
    error = refused(w, mp.CODE_MISSING_CHECK)
    assert RUFF in error.reason and str(GITHUB_ACTIONS_APP) in error.reason


def test_a_red_check_from_another_app_blocks_even_when_the_required_app_is_green(world):
    """Fail-closed : un échec terminal observé bloque, même si l'exigence (app_id) est satisfaite par une autre source."""
    w = world
    head = w.server.prs[11]["head"]["sha"]
    assert verify(w).head_sha == head  # témoin
    w.server.check_runs[head].append(
        {"name": RUFF, "status": "completed", "conclusion": "failure", "app": {"id": 424242}}
    )
    refused(w, mp.CODE_FAILED_CHECK, RUFF)


def test_same_name_requirement_without_app_needs_every_source_green(world):
    w = world
    w.server.rules.clear()
    w.server.rulesets.clear()
    w.server.protect_classic(legacy_contexts_only=True)
    head = w.server.prs[11]["head"]["sha"]
    assert verify(w).head_sha == head  # témoin
    w.server.check_runs[head].append(
        {"name": RUFF, "status": "completed", "conclusion": "skipped", "app": {"id": 424242}}
    )
    error = refused(w, mp.CODE_FAILED_CHECK, RUFF)
    assert "skipped" in error.reason


def test_commit_status_satisfies_a_context_without_app_requirement(world):
    w = world
    w.server.rules.clear()
    w.server.rulesets.clear()
    w.server.protect_classic(legacy_contexts_only=True)
    head = w.server.prs[11]["head"]["sha"]
    w.server.check_runs[head] = [r for r in w.server.check_runs[head] if r["name"] != DOCKER]
    refused(w, mp.CODE_MISSING_CHECK, DOCKER)
    w.server.statuses[head] = [{"context": DOCKER, "state": "success"}]
    assert verify(w).head_sha == head


def test_legacy_status_cannot_satisfy_an_app_scoped_requirement(world):
    w = world
    head = w.server.prs[11]["head"]["sha"]
    w.server.check_runs[head] = [r for r in w.server.check_runs[head] if r["name"] != DOCKER]
    w.server.statuses[head] = [{"context": DOCKER, "state": "success"}]
    refused(w, mp.CODE_MISSING_CHECK, DOCKER)


def test_check_pagination_beyond_one_page_is_read_completely(world):
    w = world
    head = w.server.prs[11]["head"]["sha"]
    noise = [
        {"name": f"noise-{i}", "status": "completed", "conclusion": "success", "app": {"id": 1}} for i in range(130)
    ]
    w.server.check_runs[head] = noise + w.server.check_runs[head]  # les 5 requis arrivent sur la page 2
    assert verify(w).head_sha == head
    pages = [c[2]["page"] for c in w.server.calls if c[1].endswith("/check-runs")]
    assert max(pages) == 2


def test_truncated_check_list_is_refused(world):
    w = world
    head = w.server.prs[11]["head"]["sha"]
    original = w.server.api_get

    def lying_total(endpoint, params=None):
        out = original(endpoint, params)
        if endpoint.endswith("/check-runs"):
            out = dict(out, total_count=out["total_count"] + 7)  # le serveur annonce plus que ce qui est livré
        return out

    for client in (w.clients.prs, w.clients.branches):
        client._api_get = lying_total
    refused(w, mp.CODE_API, "incomplète")
    assert head


def test_check_endpoint_error_is_refused_not_ignored(world):
    world.server.fail("GET", r"/check-runs$", 500)
    refused(world, mp.CODE_API, "lecture des checks impossible")


def test_required_check_known_only_to_the_classic_protection_is_enforced(world):
    w = world
    w.server.protect_classic(checks=(*FIVE_CHECKS, "Security scan"), strict=False)
    error = refused(w, mp.CODE_MISSING_CHECK)
    assert "Security scan" in error.reason, "union des exigences classiques ET rulesets"


# ── précondition serveur contre la course sur la base ────────────────────────────────────────────


def test_no_protection_at_all_means_no_required_checks_and_no_merge(world):
    w = world
    w.server.rules.clear()
    w.server.rulesets.clear()
    refused(w, mp.CODE_POLICY, "aucun check requis")


def test_ruleset_in_evaluate_mode_does_not_count(world):
    w = world
    w.server.rules.clear()
    w.server.rulesets.clear()
    w.server.add_ruleset(2, enforcement="evaluate", listed=True)
    error = refused(w, mp.CODE_POLICY)
    assert "non actif" in error.reason or "aucun check requis" in error.reason


def test_disabled_ruleset_does_not_count(world):
    w = world
    w.server.rules.clear()
    w.server.rulesets.clear()
    w.server.add_ruleset(3, enforcement="disabled", listed=True)
    refused(w, mp.CODE_POLICY)


@pytest.mark.parametrize("bypass", ["always", "pull_requests_only", "exempt", None])
def test_ruleset_the_actor_can_bypass_is_not_a_guarantee(world, bypass):
    w = world
    w.server.rules.clear()
    w.server.rulesets.clear()
    w.server.add_ruleset(4, can_bypass=bypass)
    error = refused(w, mp.CODE_POLICY, "aucune protection stricte")
    assert "ruleset 4" in error.reason


def test_ruleset_without_strict_policy_is_not_a_guarantee(world):
    w = world
    w.server.rules.clear()
    w.server.rulesets.clear()
    w.server.add_ruleset(5, strict=False)
    refused(w, mp.CODE_POLICY, "sans 'à jour avant fusion'")


def test_classic_strict_false_is_not_a_guarantee(world):
    w = world
    w.server.rules.clear()
    w.server.rulesets.clear()
    w.server.protect_classic(strict=False)
    refused(w, mp.CODE_POLICY, "strict=false")


def test_classic_strict_bypassable_by_an_admin_actor_is_not_a_guarantee(world):
    w = world
    w.server.rules.clear()
    w.server.rulesets.clear()
    w.server.protect_classic(strict=True, enforce_admins=False)
    w.server.actor_role = "admin"
    refused(w, mp.CODE_POLICY, "contournable par l'acteur")
    # témoin : le même dépôt est sain si les administrateurs sont soumis à la règle, ou si l'acteur n'est pas admin
    w.server.classic["enforce_admins"]["enabled"] = True
    assert verify(w).server_policy.strict_sources == ("classic",)
    w.server.classic["enforce_admins"]["enabled"] = False
    w.server.actor_role = "write"
    assert verify(w).server_policy.strict_sources == ("classic",)


@pytest.mark.parametrize("role", ["release-manager-with-bypass", "custom-role", "Security-Lead", "unknown"])
def test_classic_strict_is_not_a_guarantee_for_custom_roles_that_may_bypass(world, role):
    """GitHub : sans « Do not allow bypassing », la protection ne lie ni les admins NI les rôles personnalisés
    dotés de « bypass branch protections ». Un nom de rôle hors rôles de base est donc présumé contournant."""
    w = world
    w.server.rules.clear()
    w.server.rulesets.clear()
    w.server.protect_classic(strict=True, enforce_admins=False)
    w.server.actor_role = role
    refused(w, mp.CODE_POLICY, "contournable par l'acteur")
    w.server.classic["enforce_admins"]["enabled"] = True  # témoin : « ne pas autoriser le contournement »
    assert verify(w).server_policy.strict_sources == ("classic",)


@pytest.mark.parametrize("role", ["read", "triage", "write", "maintain"])
def test_classic_strict_binds_the_base_roles_without_bypass(world, role):
    w = world
    w.server.rules.clear()
    w.server.rulesets.clear()
    w.server.protect_classic(strict=True, enforce_admins=False)
    w.server.actor_role = role
    assert verify(w).server_policy.strict_sources == ("classic",)


def test_a_non_bypassable_strict_ruleset_compensates_a_bypassable_classic_one(world):
    w = world
    w.server.protect_classic(strict=True, enforce_admins=False)
    w.server.actor_role = "admin"
    approval = verify(w)
    assert approval.server_policy.strict_sources == ("ruleset:1",)
    assert any("contournable" in note for note in approval.server_policy.notes)


def test_merge_queue_rule_is_refused(world):
    w = world
    w.server.rules.append(
        {"type": "merge_queue", "ruleset_id": 1, "ruleset_source_type": "Repository", "parameters": {}}
    )
    refused(w, mp.CODE_POLICY, "file de fusion")


@pytest.mark.parametrize(
    "route, status",
    [
        (r"/user$", 403),  # jeton d'application : acteur non établi
        (r"/collaborators/.+/permission$", 403),
        (r"/rules/branches/", 500),
        (r"/rulesets/1$", 404),
        (r"/protection$", 403),
        (r"/protection$", 500),
    ],
)
def test_unreadable_server_preconditions_are_refused(world, route, status):
    world.server.fail("GET", route, status)
    refused(world, mp.CODE_API)


def test_classic_protection_404_is_tolerated_when_a_ruleset_gives_the_guarantee(world):
    assert world.server.classic is None  # 404 sur /protection dans le monde sain
    assert verify(world).server_policy.strict_sources == ("ruleset:1",)


def test_rules_are_read_across_pages(world):
    w = world
    filler = [
        {"type": "deletion", "ruleset_id": 1, "ruleset_source_type": "Repository", "parameters": {}} for _ in range(120)
    ]
    w.server.rules[:0] = filler  # la règle de checks passe en page 2
    assert verify(w).server_policy.strict_sources == ("ruleset:1",)
    assert max(c[2]["page"] for c in w.server.calls if "/rules/branches/" in c[1]) == 2
