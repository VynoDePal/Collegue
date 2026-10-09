"""Barrière d'intégrité des contrôles (W5) dans le chemin de fusion COMMUN : une contribution ne modifie jamais ``.github/``.

Un workflow qui se juge lui-même est le cas à fermer : la contribution remplace SON workflow par un succès et obtient un check
vert (bon nom, bonne application, bon SHA de tête). La barrière compare les sous-arbres Git RÉELS de la base de confiance et de la
tête ; elle ne dépend ni du check, ni d'une liste de fichiers qui peut être incomplète. Doubles aux seules frontières GitHub.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from github_fakes import make_source_repo
from test_w3_integration_build import DeliveringAgent, linear_project, open_manager, run_pass, statuses
from w3_remote_bridge import make_bridge

from collegue.pilot import merge_policy
from collegue.pilot.merge_policy import CODE_API, CODE_CONTROLS, MergeRefused, assert_controls_untouched

WORKFLOW_PATH = ".github/workflows/fixture-tests.yml"
TRUSTED_WORKFLOW = (
    "name: Fixture tests\non:\n  pull_request_target:\njobs:\n  fixture-runner:\n    name: Fixture runner\n"
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


def tree(github=None, **others):
    entries = [{"path": name, "type": "blob", "mode": "100644", "sha": sha} for name, sha in others.items()]
    if github is not None:
        entries.append({"path": ".github", "type": "tree", "mode": "040000", "sha": github})
    return {"tree": entries, "truncated": False}


A, B = "a" * 40, "b" * 40


@pytest.mark.parametrize(
    "base, head",
    [
        (
            tree(github=A, README="1" * 40),
            tree(github=A, README="2" * 40),
        ),  # le contenu du reste change, pas les contrôles
        (tree(README="1" * 40), tree(README="3" * 40)),  # dépôt sans .github des deux côtés
    ],
    ids=["controls-identical", "no-controls-at-all"],
)
def test_an_untouched_or_absent_controls_directory_is_accepted(base, head):
    assert_controls_untouched(Trees({"base": base, "head": head}), "o", "r", base_tree="base", head_tree="head")


@pytest.mark.parametrize(
    "base, head, label",
    [
        (tree(github=A), tree(github=B), "modified"),
        (tree(github=A), tree(), "deleted-or-renamed-away"),
        (tree(), tree(github=B), "added"),
        (tree(github=A), tree(**{".github": "c" * 40}), "replaced-by-a-file"),
    ],
)
def test_adding_modifying_deleting_or_renaming_a_control_is_refused(base, head, label):
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
    branches = SimpleNamespace(_api_get=lambda path, params: seen.append(path) or tree(github=A))

    assert_controls_untouched(branches, "o", "r", base_tree="1" * 40, head_tree="2" * 40)

    assert seen == ["/repos/o/r/git/trees/" + "1" * 40, "/repos/o/r/git/trees/" + "2" * 40]


# ── chemin public BUILD (merge-bot) : un workflow qui se juge lui-même n'est jamais fusionné ─────────────────────────────


@pytest.fixture
def source(tmp_path):
    return make_source_repo(tmp_path / "source", {"README.md": "# fixture\n", WORKFLOW_PATH: TRUSTED_WORKFLOW})


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
        TRUSTED_WORKFLOW.replace("fixture-runner:", "fixture-runner:\n    if: false  # toujours vert"),
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


CONTRIBUTIONS = {"modified": _modify, "deleted": _delete, "added": _add, "renamed": _rename}


@pytest.mark.parametrize("name", sorted(CONTRIBUTIONS))
async def test_a_delivery_that_touches_the_trusted_workflow_is_never_merged_even_with_a_green_required_check(
    name, bridge, source, state_url, caplog
):
    pid = linear_project(state_url, 1)
    agent = ControlTamperingAgent(CONTRIBUTIONS[name])
    first = await run_pass(state_url, source, bridge, pid, agent=agent, settings={"BUILD_AUTO_MERGE": False})
    assert first.stop_reason == "awaiting_merge" and statuses(state_url, pid) == {"T0": "in_review"}
    bridge.green(101)  # check requis fabriqué : bon nom, bonne application, bon SHA de tête
    assert (
        all(state == "success" for state in bridge.check_states(101).values())
        if hasattr(bridge, "check_states")
        else True
    )

    caplog.clear()
    with caplog.at_level("WARNING"):
        await run_pass(state_url, source, bridge, pid)

    assert bridge.merge_calls() == [], "aucun appel de fusion : le garde refuse AVANT l'API"
    assert statuses(state_url, pid) == {"T0": "in_review"} and bridge.merged_pr_numbers() == []
    assert "modifie les contrôles de la fixture" in caplog.text, caplog.text[-1500:]
    assert all(c.state != "synced" for c in open_manager(state_url).list_task_merges(pid))


async def test_the_same_delivery_without_touching_the_controls_is_merged_by_the_same_path(bridge, source, state_url):
    pid = linear_project(state_url, 1)
    await run_pass(state_url, source, bridge, pid, settings={"BUILD_AUTO_MERGE": False})

    await run_pass(state_url, source, bridge, pid)

    assert len(bridge.merge_calls()) == 1 and statuses(state_url, pid) == {"T0": "merged"}
    tip = bridge.branches["main"]
    assert bridge.remote.files_at(tip)[WORKFLOW_PATH] == TRUSTED_WORKFLOW, "le contrôle de confiance est resté intact"


# ── Phase 5 : une liste de fichiers INCOMPLÈTE ne cache pas la modification du contrôle ─────────────────────────────────


async def test_phase_5_with_an_incomplete_file_list_still_refuses_a_control_change(tmp_path):
    from test_improve_promotion import Budget, Scripted, metrics  # noqa: F401 - doubles de mesure partagés
    from test_w3_integration_improve import FilesFeature, improve, phase5_hook

    from collegue.state import ProjectStateManager

    source = make_source_repo(tmp_path / "source", {"README.md": "# base\n", WORKFLOW_PATH: TRUSTED_WORKFLOW})
    url = f"sqlite:///{tmp_path / 'state.db'}"
    manager = ProjectStateManager.from_url(url, create=True)
    world = SimpleNamespace(
        source=source, manager=manager, url=url, project_id=manager.create_project(name="integrity"),
        bridge=make_bridge(tmp_path, source), tmp=tmp_path,
    )  # fmt: skip
    clients = world.bridge.clients()
    real_snapshot = clients.prs.get_pr_files_snapshot

    def forged_complete_list(owner, repo, number, **kwargs):
        """Liste « complète » (nombre attendu = nombre reçu) qui ne mentionne PAS le contrôle modifié."""
        kwargs.setdefault("expected_count", real_get_pr(owner, repo, number).changed_files)
        honest = real_snapshot(owner, repo, number, **kwargs)
        visible = [item for item in honest.files if not str(item.filename).startswith(".github")]
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
