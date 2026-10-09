"""Garde de fusion de la fixture de campagne (W5), dans le chemin de fusion COMMUN : contrôles protégés et provenance du check.

Deux défauts à fermer, indépendamment du check lui-même :

* un workflow qui se juge lui-même : la contribution remplace SON workflow (ou ``ci/``, le verrou de la pile approuvée, ou
  CODEOWNERS) et obtient un check vert de bon nom, bonne application et bon SHA de tête — la barrière compare les sous-arbres Git
  RÉELS ``.github/`` et ``ci/`` de la base de confiance et de la tête ;
* un check forgé : un jeton Actions en écriture permet de publier, par l'API des checks, un ``Fixture tests`` de la bonne
  application (y compris sur une autre tête) — le check doit être un JOB réel du workflow approuvé exécuté pour la tête.

Doubles aux seules frontières GitHub (clients de production). La politique de campagne est identifiée par le dépôt fixture et la
base ``collegue-business/*`` : les tests la rattachent au dépôt de test en substituant ces deux constantes de module (jamais un
drapeau de production) ; hors campagne, rien ne change.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from github_fake_server import FIVE_CHECKS, OWNER, REPO
from test_w3_integration_build import DeliveringAgent, linear_project, open_manager, run_pass, statuses
from w3_remote_bridge import make_bridge
from w5_campaign_support import (
    CHECK,
    CODEOWNERS_PATH,
    LOCK_PATH,
    TRUSTED_WORKFLOW,
    WORKFLOW_PATH,
    campaign_mode,
    campaign_source,
)

from collegue.pilot import merge_policy
from collegue.pilot import w5_business_policy as fixture_policy
from collegue.pilot.merge_policy import (
    CODE_API,
    CODE_CONTROLS,
    CODE_PROVENANCE,
    MergeRefused,
    assert_controls_untouched,
    verify_required_checks,
)

# ── unités : sous-arbres réels, comparaison exhaustive, lecture impossible = refus ─────────────────────────────────────────


class Trees:
    """Faux ``BranchCommands`` : un arbre de premier niveau par SHA."""

    def __init__(self, trees):
        self.trees = trees
        self.reads = []

    def get_git_tree(self, owner, repo, sha):
        self.reads.append(sha)
        if isinstance(self.trees[sha], Exception):
            raise self.trees[sha]
        return self.trees[sha]


def tree(github=None, ci=None, **others):
    entries = [{"path": name, "type": "blob", "mode": "100644", "sha": sha} for name, sha in others.items()]
    if github is not None:
        entries.append({"path": ".github", "type": "tree", "mode": "040000", "sha": github})
    if ci is not None:
        entries.append({"path": "ci", "type": "tree", "mode": "040000", "sha": ci})
    return {"tree": entries, "truncated": False}


A, B = "a" * 40, "b" * 40


@pytest.mark.parametrize(
    "base, head",
    [
        (
            tree(github=A, ci=B, README="1" * 40),
            tree(github=A, ci=B, README="2" * 40),
        ),  # le contenu du reste change, pas les contrôles
        (tree(README="1" * 40), tree(README="3" * 40)),  # dépôt sans contrôles des deux côtés
    ],
    ids=["controls-identical", "no-controls-at-all"],
)
def test_untouched_or_absent_controls_are_accepted(base, head):
    assert_controls_untouched(Trees({"base": base, "head": head}), "o", "r", base_tree="base", head_tree="head")


@pytest.mark.parametrize(
    "base, head, label",
    [
        (tree(github=A, ci=A), tree(github=B, ci=A), "github-modified"),
        (tree(github=A, ci=A), tree(ci=A), "github-deleted-or-renamed-away"),
        (tree(ci=A), tree(github=B, ci=A), "github-added"),
        (tree(github=A, ci=A), tree(github=A, **{"ci": "c" * 40}), "ci-replaced-by-a-file"),
        (tree(github=A, ci=A), tree(github=A, ci=B), "ci-lock-modified"),
        (tree(github=A, ci=A), tree(github=A), "ci-deleted"),
        (tree(github=A), tree(github=A, ci=B), "ci-added"),
    ],
)
def test_adding_modifying_deleting_or_renaming_a_protected_root_is_refused(base, head, label):
    with pytest.raises(MergeRefused, match=r"modifie les contrôles de la fixture") as refusal:
        assert_controls_untouched(Trees({"base": base, "head": head}), "o", "r", base_tree="base", head_tree="head")
    assert refusal.value.code == CODE_CONTROLS


@pytest.mark.parametrize(
    "broken",
    [RuntimeError("HTTP 502"), {"tree": [], "truncated": True}, {"tree": "pas une liste", "truncated": False}, {}],
    ids=["http-error", "truncated", "malformed", "empty-answer"],
)
def test_an_unreadable_tree_proves_nothing_and_is_refused_fail_closed(broken):
    with pytest.raises(MergeRefused) as refusal:
        assert_controls_untouched(Trees({"base": broken, "head": tree()}), "o", "r", base_tree="base", head_tree="head")
    assert refusal.value.code == CODE_API


def test_the_barrier_reads_the_rest_route_when_the_client_has_no_public_tree_method():
    seen = []
    branches = SimpleNamespace(_api_get=lambda path, params: seen.append(path) or tree(github=A, ci=B))

    assert_controls_untouched(branches, "o", "r", base_tree="1" * 40, head_tree="2" * 40)

    assert seen == ["/repos/o/r/git/trees/" + "1" * 40, "/repos/o/r/git/trees/" + "2" * 40]


# ── identification de la politique : la campagne seulement, aucun drapeau ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    "owner, repo, base, expected",
    [
        ("VynoDePal", "collegue-e2e-fixture", "collegue-business/run-1", True),
        ("vynodepal", "Collegue-E2E-Fixture", "collegue-business/run-1", True),
        ("VynoDePal", "collegue-e2e-fixture", "main", False),
        ("VynoDePal", "collegue-e2e-fixture", "collegue-nightly/run-1", False),
        ("someone", "else", "collegue-business/run-1", False),
    ],
)
def test_the_campaign_policy_is_identified_by_repository_and_ephemeral_base(owner, repo, base, expected):
    assert fixture_policy.applies(owner, repo, base) is expected


def test_the_policy_constants_match_the_campaign_constants():
    from collegue.pilot import w4_business as business

    assert fixture_policy.CAMPAIGN_REPOSITORY == business.FIXTURE_REPOSITORY
    assert fixture_policy.CAMPAIGN_BASE_PREFIX == business.BASE_BRANCH_PREFIX + "/"
    assert fixture_policy.CAMPAIGN_CHECK == "Fixture tests"
    assert fixture_policy.PROTECTED_ROOTS == (".github", "ci")


# ── chemin public BUILD (merge-bot) ───────────────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def campaign(tmp_path):
    return campaign_source(tmp_path / "source")


@pytest.fixture
def source(campaign):
    return campaign.path


@pytest.fixture
def bridge(tmp_path, source):
    return make_bridge(tmp_path, source)


@pytest.fixture
def state_url(tmp_path):
    return f"sqlite:///{tmp_path / 'state.db'}"


class ControlTamperingAgent(DeliveringAgent):
    """Codeur qui, en plus de sa livraison, fait la contribution décrite par ``change`` sur le contrôle."""

    def __init__(self, change):
        super().__init__()
        self.change = change

    def implement_issue(self, workspace, issue):
        result = super().implement_issue(workspace, issue)
        self.change(workspace)
        return result


def _write(workspace, relative, text):
    import os

    target = os.path.join(workspace, relative)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, "w", encoding="utf-8") as handle:
        handle.write(text)


def _modify(workspace):
    _write(
        workspace,
        WORKFLOW_PATH,
        TRUSTED_WORKFLOW.replace("fixture-tests:", "fixture-tests:\n    if: false  # toujours vert"),
    )


def _delete(workspace):
    import os

    os.remove(os.path.join(workspace, WORKFLOW_PATH))


def _add(workspace):
    _write(
        workspace,
        ".github/workflows/faux-check.yml",
        "name: Fixture tests\non: pull_request\njobs:\n  x:\n    runs-on: ubuntu-latest\n",
    )


def _rename(workspace):
    import os

    os.rename(os.path.join(workspace, WORKFLOW_PATH), os.path.join(workspace, ".github/workflows/renamed.yml"))


def _codeowners(workspace):
    _write(workspace, CODEOWNERS_PATH, "* @intrus\n")


def _lock_modified(workspace):
    _write(workspace, LOCK_PATH, "evil==1.0 \\\n    --hash=sha256:" + "b" * 64 + "\n")


def _lock_deleted(workspace):
    import os

    os.remove(os.path.join(workspace, LOCK_PATH))


def _ci_added(workspace):
    _write(workspace, "ci/hook.sh", "echo pwned\n")


CONTRIBUTIONS = {
    "workflow-modified": _modify,
    "workflow-deleted": _delete,
    "workflow-added": _add,
    "workflow-renamed": _rename,
    "codeowners-modified": _codeowners,
    "approved-lock-modified": _lock_modified,
    "approved-lock-deleted": _lock_deleted,
    "ci-file-added": _ci_added,
}


@pytest.mark.parametrize("name", sorted(CONTRIBUTIONS))
async def test_a_delivery_that_touches_a_protected_control_is_never_merged_even_with_a_genuine_green_check(
    name, bridge, source, state_url, caplog, monkeypatch, campaign, tmp_path
):
    """Barrière de FUSION seule : la tête hostile est publiée HORS politique de campagne (la garde de publication, testée dans
    ``test_w5_business_prepublication``, la refuserait), puis la campagne s'applique — la barrière ne dépend d'aucune autre garde."""
    pid = linear_project(state_url, 1)
    agent = ControlTamperingAgent(CONTRIBUTIONS[name])
    first = await run_pass(state_url, source, bridge, pid, agent=agent, settings={"BUILD_AUTO_MERGE": False})
    assert first.stop_reason == "awaiting_merge" and statuses(state_url, pid) == {"T0": "in_review"}
    campaign_mode(
        monkeypatch, bridge, campaign=campaign, directory=tmp_path
    )  # le job du check est RÉEL : seule la barrière refuse
    bridge.green(101)  # check requis : bon nom, bonne application, bon SHA de tête, job réel

    caplog.clear()
    with caplog.at_level("WARNING"):
        await run_pass(state_url, source, bridge, pid)

    assert bridge.merge_calls() == [], "aucun appel de fusion : le garde refuse AVANT l'API"
    assert statuses(state_url, pid) == {"T0": "in_review"} and bridge.merged_pr_numbers() == []
    assert "modifie les contrôles de la fixture" in caplog.text, caplog.text[-1500:]
    assert all(c.state != "synced" for c in open_manager(state_url).list_task_merges(pid))


async def test_the_same_delivery_without_touching_the_controls_is_merged_by_the_same_path(
    bridge, source, state_url, monkeypatch, campaign, tmp_path
):
    campaign_mode(monkeypatch, bridge, campaign=campaign, directory=tmp_path)  # publication ET fusion sous la politique
    pid = linear_project(state_url, 1)
    await run_pass(state_url, source, bridge, pid, settings={"BUILD_AUTO_MERGE": False})

    await run_pass(state_url, source, bridge, pid)

    assert len(bridge.merge_calls()) == 1 and statuses(state_url, pid) == {"T0": "merged"}
    tip = bridge.branches["main"]
    assert bridge.remote.files_at(tip)[WORKFLOW_PATH] == TRUSTED_WORKFLOW, "le contrôle de confiance est resté intact"


async def test_outside_the_campaign_a_delivery_may_change_its_own_workflows(bridge, source, state_url):
    """Installations hors campagne inchangées : aucune barrière, aucune provenance (le dépôt n'est pas la fixture)."""
    pid = linear_project(state_url, 1)
    agent = ControlTamperingAgent(_modify)
    await run_pass(state_url, source, bridge, pid, agent=agent, settings={"BUILD_AUTO_MERGE": False})
    await run_pass(state_url, source, bridge, pid)

    assert len(bridge.merge_calls()) == 1 and statuses(state_url, pid) == {"T0": "merged"}


# ── provenance : un check de bon nom et de bonne application qui n'est pas un job du workflow approuvé ───────────────────


FORGERIES = {  # nom: (variation du job/de l'exécution, fragment du motif de refus attendu)
    "no-job-at-all-api-published": (dict(job=False), "n'est pas un job d'une exécution de workflow"),
    "job-of-another-head": (dict(run_head_sha="0" * 40), "tête de l'exécution"),
    "another-workflow-file": (
        dict(workflow_path=".github/workflows/evil.yml"),
        "workflow '.github/workflows/evil.yml'",
    ),
    "push-event": (dict(event="push"), "événement 'push' non admis"),
    "run-of-another-repository": (
        dict(repository="evil/fixture", head_repository="evil/fixture"),
        "dépôt de l'exécution",
    ),
    "run-from-a-fork": (dict(head_repository="evil/fixture"), "dépôt de l'exécution"),
    "job-not-successful": (dict(conclusion="failure"), "non réussie"),
    "run-not-successful": (dict(run_conclusion="failure"), "non réussie"),
}


@pytest.mark.parametrize("name", sorted(FORGERIES))
async def test_a_green_check_with_the_right_name_and_app_but_no_matching_job_is_never_merged(
    name, bridge, source, state_url, caplog, monkeypatch, campaign, tmp_path
):
    variation, expected = FORGERIES[name]
    campaign_mode(monkeypatch, bridge, campaign=campaign, directory=tmp_path, **variation)
    pid = linear_project(state_url, 1)
    first = await run_pass(state_url, source, bridge, pid, settings={"BUILD_AUTO_MERGE": False})
    assert first.stop_reason == "awaiting_merge"
    bridge.green(101)

    caplog.clear()
    with caplog.at_level("WARNING"):
        await run_pass(state_url, source, bridge, pid)

    assert bridge.merge_calls() == [], "refus AVANT l'API de fusion"
    assert statuses(state_url, pid) == {"T0": "in_review"} and bridge.merged_pr_numbers() == []
    assert expected in caplog.text, caplog.text[-1500:]


async def test_an_unavailable_actions_api_refuses_the_merge(bridge, source, state_url, monkeypatch, campaign, tmp_path):
    campaign_mode(monkeypatch, bridge, campaign=campaign, directory=tmp_path)
    pid = linear_project(state_url, 1)
    await run_pass(state_url, source, bridge, pid, settings={"BUILD_AUTO_MERGE": False})
    bridge.green(101)
    bridge.fail("GET", r"/actions/jobs/\d+", 503)

    await run_pass(state_url, source, bridge, pid)

    assert bridge.merge_calls() == [] and statuses(state_url, pid) == {"T0": "in_review"}


async def test_the_campaign_refuses_a_base_whose_server_protection_does_not_require_the_check(
    bridge, source, state_url, monkeypatch, campaign, tmp_path
):
    campaign_mode(monkeypatch, bridge, campaign=campaign, directory=tmp_path)
    bridge.protect_classic(checks=FIVE_CHECKS)  # la protection serveur n'exige plus « Fixture tests »
    pid = linear_project(state_url, 1)
    await run_pass(state_url, source, bridge, pid, settings={"BUILD_AUTO_MERGE": False})

    await run_pass(state_url, source, bridge, pid)

    assert bridge.merge_calls() == [] and statuses(state_url, pid) == {"T0": "in_review"}


# ── checks du revert : même garde, sans preuve de livraison ───────────────────────────────────────────────────────────────


def _revert_head(bridge, path, text):
    bridge.branches["revert-x"] = bridge.base_tip
    return bridge.write_remote_file("revert-x", path, text) if path else bridge.base_tip


def test_revert_checks_pass_for_a_genuine_job_and_untouched_controls(bridge, monkeypatch):
    campaign_mode(monkeypatch, bridge)
    head = _revert_head(bridge, "README.md", "# revert\n")
    bridge.set_checks(head, {name: "success" for name in FIVE_CHECKS})

    policy = verify_required_checks(bridge.clients(), owner=OWNER, repo=REPO, base="main", head_sha=head)

    assert any(c.context == CHECK for c in policy.required_checks)


@pytest.mark.parametrize("path", [WORKFLOW_PATH, CODEOWNERS_PATH, LOCK_PATH, "ci/hook.sh"])
def test_revert_checks_refuse_a_revert_that_changes_a_protected_control(bridge, monkeypatch, path):
    campaign_mode(monkeypatch, bridge)
    head = _revert_head(bridge, path, "name: tampered\n")
    bridge.set_checks(head, {name: "success" for name in FIVE_CHECKS})

    with pytest.raises(MergeRefused) as refusal:
        verify_required_checks(bridge.clients(), owner=OWNER, repo=REPO, base="main", head_sha=head)

    assert refusal.value.code == CODE_CONTROLS


def test_revert_checks_refuse_a_check_that_is_not_a_job_of_the_approved_workflow(bridge, monkeypatch):
    campaign_mode(monkeypatch, bridge, job=False)
    head = _revert_head(bridge, "README.md", "# revert\n")
    bridge.set_checks(head, {name: "success" for name in FIVE_CHECKS})

    with pytest.raises(MergeRefused) as refusal:
        verify_required_checks(bridge.clients(), owner=OWNER, repo=REPO, base="main", head_sha=head)

    assert refusal.value.code == CODE_PROVENANCE
    assert "n'est pas un job" in refusal.value.reason


def test_revert_checks_refuse_when_the_actions_api_is_unavailable(bridge, monkeypatch):
    campaign_mode(monkeypatch, bridge)
    head = _revert_head(bridge, "README.md", "# revert\n")
    bridge.set_checks(head, {name: "success" for name in FIVE_CHECKS})
    bridge.fail("GET", r"/actions/(jobs|runs)/\d+", 503)

    with pytest.raises(MergeRefused) as refusal:
        verify_required_checks(bridge.clients(), owner=OWNER, repo=REPO, base="main", head_sha=head)

    assert refusal.value.code == CODE_API


def test_revert_checks_outside_the_campaign_are_unchanged(bridge):
    head = _revert_head(bridge, WORKFLOW_PATH, "name: other\n")
    bridge.set_checks(head, {name: "success" for name in FIVE_CHECKS})

    verify_required_checks(bridge.clients(), owner=OWNER, repo=REPO, base="main", head_sha=head)


# ── Phase 5 : une liste de fichiers INCOMPLÈTE ne cache pas la modification du contrôle ─────────────────────────────────


async def test_phase_5_with_an_incomplete_file_list_still_refuses_a_control_change(tmp_path, monkeypatch):
    """Barrière de FUSION de Phase 5 seule : la garde de PUBLICATION est neutralisée ici (elle refuserait avant même la PR) pour prouver
    que la fusion ne dépend pas d'elle."""
    from test_improve_promotion import Budget, Scripted, metrics  # noqa: F401 - doubles de mesure partagés
    from test_w3_integration_improve import FilesFeature, improve, phase5_hook

    import collegue.executor.pr as pr_module
    from collegue.state import ProjectStateManager

    source = campaign_source(tmp_path / "source").path
    monkeypatch.setattr(pr_module, "_assert_publication_controls", lambda *args, **kwargs: None, raising=False)
    url = f"sqlite:///{tmp_path / 'state.db'}"
    manager = ProjectStateManager.from_url(url, create=True)
    world = SimpleNamespace(
        source=source, manager=manager, url=url, project_id=manager.create_project(name="integrity"),
        bridge=make_bridge(tmp_path, source), tmp=tmp_path,
    )  # fmt: skip
    campaign_mode(monkeypatch, world.bridge)
    clients = world.bridge.clients()
    real_snapshot = clients.prs.get_pr_files_snapshot

    def forged_complete_list(owner, repo, number, **kwargs):
        """Liste « complète » (nombre attendu = nombre reçu) qui ne mentionne PAS le contrôle modifié."""
        kwargs.setdefault("expected_count", real_get_pr(owner, repo, number).changed_files)
        honest = real_snapshot(owner, repo, number, **kwargs)
        visible = [item for item in honest.files if not str(item.filename).startswith((".github", "ci"))]
        return type(honest)(files=visible, complete=True, expected_count=len(visible))

    real_get_pr = clients.prs.get_pr

    def forged_count(owner, repo, number):
        info = real_get_pr(owner, repo, number)
        visible = forged_complete_list(owner, repo, number).files
        stats = {  # statistiques cohérentes avec la liste falsifiée : le contrôle modifié n'existe nulle part dans les chiffres
            "changed_files": len(visible),
            "additions": sum(item.additions for item in visible),
            "deletions": sum(item.deletions for item in visible),
        }
        return info.model_copy(update=stats)

    clients.prs.get_pr_files_snapshot = forged_complete_list
    clients.prs.get_pr = forged_count
    world.bridge.clients = lambda: clients

    result = await improve(
        world,
        [metrics(80), metrics(90)],
        agent=FilesFeature({"docs/gain.md": "# gain\n", WORKFLOW_PATH: "name: Fixture tests\non: push\njobs: {}\n"}),
        promotion_hook=phase5_hook(world),
    )

    assert world.bridge.merged_pr_numbers() == [] and world.bridge.merge_calls() == []
    assert result.stop_reason == "auto_merge_blocked", (result.stop_reason, result.rejected)
    assert any("modifie les contrôles de la fixture" in reason for _dim, reason in result.rejected), result.rejected
    assert merge_policy.CODE_CONTROLS == "controls_altered"


# ── méthodes ajoutées aux clients GitHub (lecture seule, réponses validées) ─────────────────────────────────────────────────


def _client(cls, answer):
    client = cls(token=None)
    client._request_json = lambda method, endpoint, **kw: answer(method, endpoint, kw) if callable(answer) else answer
    return client


def test_the_tree_client_returns_validated_entries_and_reports_truncation():
    from collegue.tools.github_commands import BranchCommands

    seen = []
    answer = {"tree": [{"path": ".github", "type": "tree", "sha": "a" * 40, "mode": "040000"}], "truncated": True}
    client = _client(BranchCommands, lambda m, e, kw: seen.append((m, e, kw.get("params"))) or answer)

    data = client.get_git_tree("o", "r", "b" * 40, recursive=True)

    assert data["truncated"] is True and data["tree"][0]["path"] == ".github"
    assert seen == [("GET", f"/repos/o/r/git/trees/{'b' * 40}", {"recursive": "1"})]


@pytest.mark.parametrize(
    "answer", [None, {}, {"tree": "x"}, {"tree": [{"path": 1, "type": "blob", "sha": "a"}]}, {"tree": [3]}]
)
def test_the_tree_client_refuses_malformed_answers(answer):
    from collegue.tools.base import ToolExecutionError
    from collegue.tools.github_commands import BranchCommands

    with pytest.raises(ToolExecutionError):
        _client(BranchCommands, answer).get_git_tree("o", "r", "b" * 40)


@pytest.mark.parametrize("job_id", [0, -4, True, "12"])
def test_the_job_and_run_clients_refuse_invalid_identifiers(job_id):
    from collegue.tools.base import ToolExecutionError
    from collegue.tools.github_commands import PRCommands

    client = _client(PRCommands, {})
    with pytest.raises(ToolExecutionError):
        client.get_workflow_job("o", "r", job_id)
    with pytest.raises(ToolExecutionError):
        client.get_workflow_run("o", "r", job_id)


def test_the_job_and_run_clients_normalise_what_the_provenance_compares():
    from collegue.tools.github_commands import PRCommands

    job = _client(
        PRCommands,
        {
            "id": 7,
            "run_id": 9,
            "name": "Fixture tests",
            "head_sha": "A" * 40,
            "status": "Completed",
            "conclusion": "Success",
        },
    )
    run = _client(
        PRCommands,
        {
            "id": 9, "path": ".github/workflows/fixture-tests.yml", "event": "Pull_Request", "head_sha": "A" * 40,
            "status": "completed", "conclusion": "success", "repository": {"full_name": "O/R"}, "head_repository": {"full_name": "O/R"},
        },
    )  # fmt: skip

    info = job.get_workflow_job("o", "r", 7)
    assert (info.id, info.run_id, info.head_sha, info.status, info.conclusion) == (
        7,
        9,
        "a" * 40,
        "completed",
        "success",
    )
    execution = run.get_workflow_run("o", "r", 9)
    assert (execution.event, execution.repository, execution.head_repository) == ("pull_request", "o/r", "o/r")


@pytest.mark.parametrize("answer", [None, {}, {"id": 7}, {"id": 7, "run_id": "x"}])
def test_a_malformed_job_is_refused(answer):
    from collegue.tools.base import ToolExecutionError
    from collegue.tools.github_commands import PRCommands

    with pytest.raises(ToolExecutionError):
        _client(PRCommands, answer).get_workflow_job("o", "r", 7)


def test_the_check_observation_exposes_the_check_run_identifier_and_keeps_old_callers_compatible():
    from collegue.tools.github_commands.prs import CheckObservation

    assert CheckObservation(name="x", state="success").check_run_id is None
    assert CheckObservation(name="x", state="success", check_run_id=5).check_run_id == 5
