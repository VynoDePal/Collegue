"""Identité des branches BUILD / IMPROVE au niveau PUBLICATION (Git distant réel derrière les vrais clients).

Défaut reproduit par le manager : la boucle d'amélioration nommait sa tête ``collegue/issue-<round>``, comme une tâche BUILD.
Le compteur de round repart à 1 à chaque passe : la première amélioration retombait sur la tête de la tâche BUILD n° 1
(conservée quand le dépôt ne supprime pas les têtes fusionnées, ce qui est la configuration réelle de la fixture) et
la publication était refusée, ou pire, rattachée à une branche historique.

Ces tests n'exécutent que des API publiques (``run_project_from_settings`` puis ``run_improvement``) sur un vrai dépôt bare ;
la mesure de qualité et l'agent sont des doublons déterministes (ce n'est pas la preuve métier : voir la campagne).
"""

from __future__ import annotations

import socket
from types import SimpleNamespace

import pytest
from github_fakes import make_source_repo
from test_w3_integration_build import linear_project, open_manager, run_pass, statuses
from test_w3_integration_improve import FilesFeature, improve, metrics, phase5_hook
from w3_remote_bridge import make_bridge

from collegue.executor.workspace import (
    BRANCH_PREFIX,
    IMPROVEMENT_BRANCH_PREFIX,
    branch_for_improvement,
    branch_for_issue,
    resync_repository_base,
)

TREE_A = "a" * 40
TREE_B = "b" * 40
REV_A = "c" * 40
REV_B = "d" * 40


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("réseau interdit")

    monkeypatch.setattr(socket.socket, "connect", refuse)


async def built_world(tmp_path, *, delete_merged_heads):
    """Deux tâches BUILD fusionnées par le produit ; les têtes fusionnées sont conservées ou supprimées."""
    source = make_source_repo(tmp_path / "source", {"README.md": "# fixture\n"})
    bridge = make_bridge(tmp_path, source)
    url = f"sqlite:///{tmp_path / 'state.db'}"
    pid = linear_project(url, 2)
    result = await run_pass(url, source, bridge, pid)
    assert result.stop_reason == "completed" and set(statuses(url, pid).values()) == {"merged"}
    build_heads = sorted(pr["head"]["ref"] for pr in bridge.prs.values() if pr["merged"])
    assert build_heads == [branch_for_issue(1), branch_for_issue(2)] or len(build_heads) == 2
    historical = {name: bridge.branches[name] for name in build_heads}
    if delete_merged_heads:
        for name in build_heads:
            del bridge.branches[name]
    world = SimpleNamespace(
        source=source, manager=open_manager(url), url=url, project_id=pid, bridge=bridge, tmp=tmp_path
    )
    return world, build_heads, historical


def improvement_heads(world):
    return sorted(
        pr["head"]["ref"] for pr in world.bridge.prs.values() if pr["head"]["ref"].startswith(IMPROVEMENT_BRANCH_PREFIX)
    )


@pytest.mark.parametrize("delete_merged_heads", [False, True], ids=["heads-kept", "heads-deleted-on-merge"])
async def test_first_improvement_after_build_is_published_on_its_own_branch_and_never_touches_build_heads(
    tmp_path, delete_merged_heads
):
    world, build_heads, historical = await built_world(tmp_path, delete_merged_heads=delete_merged_heads)

    result = await improve(world, [metrics(80), metrics(90)], agent=FilesFeature({"docs/gain.md": "# gain\n"}))

    assert [p.pr_number for p in result.promoted] != [] and len(result.promoted) == 1, result.rejected
    (head,) = improvement_heads(world)
    assert head.startswith(f"{IMPROVEMENT_BRANCH_PREFIX}r1-") and not head.startswith(BRANCH_PREFIX)
    assert head not in build_heads
    if not delete_merged_heads:  # têtes historiques conservées : jamais ré-aiguillées ni réécrites
        assert {name: world.bridge.branches[name] for name in build_heads} == historical
    published = world.bridge.prs[result.promoted[0].pr_number]
    assert published["head"]["ref"] == head and published["state"] == "open" and not published["merged"]
    assert sorted(world.bridge.merged_pr_numbers()) == sorted(
        n for n, pr in world.bridge.prs.items() if pr["head"]["ref"] in build_heads
    ), "seules les PR BUILD sont fusionnées : l'amélioration reste ouverte"


async def test_a_second_pass_restarting_at_round_one_gets_a_distinct_branch_and_keeps_the_first_pr_untouched(tmp_path):
    world, build_heads, historical = await built_world(tmp_path, delete_merged_heads=False)
    first = await improve(world, [metrics(80), metrics(90)], agent=FilesFeature({"docs/one.md": "# 1\n"}))
    (first_head,) = improvement_heads(world)
    first_sha = world.bridge.branches[first_head]

    second = await improve(world, [metrics(80), metrics(90)], agent=FilesFeature({"docs/two.md": "# 2\n"}))

    assert len(first.promoted) == len(second.promoted) == 1, (first.rejected, second.rejected)
    first_head_again, second_head = sorted(
        improvement_heads(world), key=lambda name: world.bridge.branches[name] != first_sha
    )
    assert first_head_again == first_head and first_head != second_head
    assert second_head.startswith(f"{IMPROVEMENT_BRANCH_PREFIX}r1-")  # le compteur de round repart à 1
    assert world.bridge.branches[first_head] == first_sha, "la tête de la première amélioration n'a pas bougé"
    assert second.promoted[0].pr_number != first.promoted[0].pr_number
    assert {name: world.bridge.branches[name] for name in build_heads} == historical


async def test_resuming_the_same_improvement_reuses_its_pr_and_its_proof_without_any_new_remote_write(tmp_path):
    world, _, _ = await built_world(tmp_path, delete_merged_heads=False)
    first = await improve(world, [metrics(80), metrics(90)], agent=FilesFeature({"docs/gain.md": "# gain\n"}))
    (head,) = improvement_heads(world)
    sha = world.bridge.branches[head]
    writes = list(world.bridge.remote.writes)

    again = await improve(world, [metrics(80), metrics(90)], agent=FilesFeature({"docs/gain.md": "# gain\n"}))

    assert again.promoted[0].pr_number == first.promoted[0].pr_number
    assert again.promoted[0].proof.proof_id == first.promoted[0].proof.proof_id
    assert improvement_heads(world) == [head] and world.bridge.branches[head] == sha
    assert world.bridge.remote.writes == writes, "aucune réécriture distante à la reprise"


async def test_a_historical_branch_squatting_the_old_improvement_name_is_irrelevant_to_the_new_identity(tmp_path):
    """Le nom historique ``collegue/issue-1`` (tête BUILD conservée avec sa PR fusionnée) n'est ni lu ni écrit."""
    world, build_heads, historical = await built_world(tmp_path, delete_merged_heads=False)
    assert branch_for_issue(1) in world.bridge.branches
    writes_before = len(world.bridge.remote.writes)

    result = await improve(world, [metrics(80), metrics(90)], agent=FilesFeature({"docs/gain.md": "# gain\n"}))

    assert len(result.promoted) == 1, result.rejected
    new_writes = world.bridge.remote.writes[writes_before:]
    assert new_writes and all(path.endswith("gain.md") for path in new_writes), new_writes
    assert world.bridge.branches[branch_for_issue(1)] == historical[branch_for_issue(1)]


def test_branch_identity_is_revision_and_content_bound_namespaced_and_validated():
    one = branch_for_improvement(1, REV_A, TREE_A, TREE_B)
    assert one == branch_for_improvement(1, REV_A, TREE_A, TREE_B), "déterministe : la reprise retrouve la même tête"
    assert one.startswith(IMPROVEMENT_BRANCH_PREFIX) and not one.startswith(BRANCH_PREFIX)
    variants = {
        one,
        branch_for_improvement(2, REV_A, TREE_A, TREE_B),  # autre round
        branch_for_improvement(1, REV_B, TREE_A, TREE_B),  # autre RÉVISION de base, mêmes arbres
        branch_for_improvement(1, REV_A, TREE_B, TREE_A),
    }
    assert len(variants) == 4
    assert one != branch_for_issue(1)
    for bad in ("", "xyz", "A" * 40, "a" * 39, "../" + "a" * 40, None):
        for position in range(3):
            args = [REV_A, TREE_A, TREE_B]
            args[position] = bad
            with pytest.raises(ValueError):
                branch_for_improvement(1, *args)


async def _first_improvement(tmp_path, *, phase5):
    world, build_heads, historical = await built_world(tmp_path, delete_merged_heads=False)
    initial_base = world.bridge.branches["main"]
    initial_tree = world.bridge.remote.tree_of(initial_base)
    hook = phase5_hook(world) if phase5 else None
    first = await improve(
        world, [metrics(80), metrics(90)], agent=FilesFeature({"docs/gain.md": "# gain\n"}), promotion_hook=hook
    )
    assert len(first.promoted) == 1, first.rejected
    first_head = world.bridge.prs[first.promoted[0].pr_number]["head"]["ref"]
    return world, build_heads, historical, first, first_head, initial_base, initial_tree


def _advance_base_with_same_tree(world, initial_tree, message):
    tip = world.bridge.branches["main"]
    new_base = world.bridge.commit([tip], tree=initial_tree, message=message)
    world.bridge.branches["main"] = new_base
    assert world.bridge.remote.tree_of(new_base) == initial_tree
    assert resync_repository_base(world.source, "main")
    return new_base


async def test_a_base_advanced_by_an_empty_commit_gets_a_new_branch_and_pr_and_keeps_the_first_head_untouched(tmp_path):
    world, build_heads, historical, first, first_head, initial_base, initial_tree = await _first_improvement(
        tmp_path, phase5=False
    )
    first_sha = world.bridge.branches[first_head]
    new_base = _advance_base_with_same_tree(world, initial_tree, "commit vide")
    assert new_base != initial_base
    writes_before = list(world.bridge.remote.writes)

    second = await improve(world, [metrics(80), metrics(90)], agent=FilesFeature({"docs/gain.md": "# gain\n"}))

    assert len(second.promoted) == 1, second.rejected  # même contenu, NOUVELLE révision : nouvelle livraison
    assert second.promoted[0].pr_number != first.promoted[0].pr_number
    second_head = world.bridge.prs[second.promoted[0].pr_number]["head"]["ref"]
    assert second_head != first_head and second_head.startswith(f"{IMPROVEMENT_BRANCH_PREFIX}r1-")
    assert world.bridge.branches[first_head] == first_sha, "la tête de la première amélioration n'a pas bougé"
    assert {name: world.bridge.branches[name] for name in build_heads} == historical
    touched = {path for path in world.bridge.remote.writes[len(writes_before) :]}
    assert touched and all(path.endswith("gain.md") for path in touched)
    assert world.bridge.prs[second.promoted[0].pr_number]["base"]["sha"] == new_base


async def test_the_same_improvement_in_a_new_cycle_after_a_merged_one_never_rewrites_the_merged_historical_head(
    tmp_path,
):
    """Phase 5 fusionne la première amélioration ; un commit distant rend ensuite l'arbre initial (état après revert)."""
    world, build_heads, historical, first, first_head, initial_base, initial_tree = await _first_improvement(
        tmp_path, phase5=True
    )
    first_pr = first.promoted[0].pr_number
    assert world.bridge.prs[first_pr]["merged"], "la vraie Phase 5 a fusionné la première amélioration"
    first_sha = world.bridge.branches[first_head]
    new_base = _advance_base_with_same_tree(world, initial_tree, "retour au contenu initial")
    assert new_base != initial_base
    writes_before = list(world.bridge.remote.writes)

    second = await improve(world, [metrics(80), metrics(90)], agent=FilesFeature({"docs/gain.md": "# gain\n"}))

    assert len(second.promoted) == 1, second.rejected
    assert second.promoted[0].pr_number != first_pr
    second_head = world.bridge.prs[second.promoted[0].pr_number]["head"]["ref"]
    assert second_head != first_head
    assert world.bridge.branches[first_head] == first_sha, "tête historique déjà fusionnée : aucune écriture"
    assert {name: world.bridge.branches[name] for name in build_heads} == historical
    assert all(path.endswith("gain.md") for path in world.bridge.remote.writes[len(writes_before) :])
