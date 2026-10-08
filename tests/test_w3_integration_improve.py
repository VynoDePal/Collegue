"""Raccord de la vague 3, IMPROVE : promotion (lot A) → preuve ``improve`` durable → Phase 5 (lot B) sur un Git distant réel.

Propriété C. Entrées publiques : ``run_improvement`` (avec le ``promotion_hook`` de production ``auto_merge_promotion``),
``verify_merge_candidate``. La preuve est relue par une NOUVELLE instance du manager (``load_delivery_proof`` réel), la
resynchronisation et la garde post-fusion sont les vraies (``resync_repository_base``, ``guard_post_merge``) et le dépôt
distant est un vrai dépôt Git derrière les vrais clients (``tests/w3_remote_bridge.py``). Aucune dérogation : ni
``proof_loader``, ni ``verify_fn``, ni ``merge_gate`` injectés.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from github_fakes import make_source_repo
from oracle_sandbox import LocalOracleSandbox
from test_improve_promotion import FEATURE_ORACLE, Budget, Feature, Scripted, metrics, seal_delivered
from w3_remote_bridge import make_bridge

from collegue.executor.delivery_proof import load_delivery_proof
from collegue.improve import run_improvement
from collegue.pilot.automerge import RiskPolicy, auto_merge_promotion
from collegue.pilot.guard import RevertPolicy
from collegue.pilot.merge_policy import MergeRefused, verify_merge_candidate
from collegue.sandbox import SandboxResult
from collegue.state import ProjectStateManager

OWNER = REPO = "fixture"


class _Sandbox:
    """Tests du projet et santé de ``main`` verts (la garde post-fusion exécute sa vraie logique)."""

    def run_tests(self, workspace, command="pytest -q"):
        return SandboxResult(exit_code=0, stdout="ok", stderr="")


class FilesFeature(Feature):
    """Écrit un jeu de fichiers fixe (chemins avec sous-dossiers)."""

    def implement_issue(self, workspace, issue):
        for rel in self.files:
            (Path(workspace) / rel).parent.mkdir(parents=True, exist_ok=True)
        return super().implement_issue(workspace, issue)


@pytest.fixture
def world(tmp_path):
    source = make_source_repo(tmp_path / "source", {"README.md": "# base\n", "feature.py": "VALUE = 42\n"})
    url = f"sqlite:///{tmp_path / 'state.db'}"
    manager = ProjectStateManager.from_url(url, create=True)
    return SimpleNamespace(
        source=source,
        manager=manager,
        url=url,
        project_id=manager.create_project(name="improve"),
        bridge=make_bridge(tmp_path, source),
        tmp=tmp_path,
    )


def phase5_hook(world, *, allowlist=None):
    """``promotion_hook`` de production : la vraie ``auto_merge_promotion`` (preuve, politique, resync, garde)."""
    policy = RiskPolicy(enabled=True, **({"path_allowlist": tuple(allowlist)} if allowlist else {}))

    async def hook(pr):
        return await auto_merge_promotion(
            pr,
            policy=policy,
            revert_policy=RevertPolicy(enabled=True, revert_enabled=True),
            clients=world.bridge.clients(),
            owner=OWNER,
            repo=REPO,
            repo_source=world.source,
            base="main",
            sandbox=_Sandbox(),
            manager=world.manager,
            project_id=world.project_id,
            ci_timeout_seconds=0,
            ci_poll_seconds=0,
        )

    return hook


async def improve(world, sequence, *, agent=None, promotion_hook=None, **kwargs):
    return await run_improvement(
        world.project_id,
        world.source,
        None,
        agent=agent or Feature(),
        owner=OWNER,
        repo=REPO,
        manager=world.manager,
        budget=Budget(),
        clients=world.bridge.clients(),
        dry_run=False,
        plateau_rounds=kwargs.pop("plateau_rounds", 1),
        measure_fn=Scripted(sequence),
        promotion_hook=promotion_hook,
        **kwargs,
    )


def nothing_published(world):
    return world.bridge.prs == {} and world.bridge.remote.writes == [] and world.bridge.merge_calls() == []


def git_out(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def reloaded_proof(world, number, head):
    fresh = ProjectStateManager.from_url(world.url, create=False)  # NOUVELLE instance du manager
    return load_delivery_proof(fresh, world.project_id, owner=OWNER, repo=REPO, pr_number=number, head_sha=head)


# ── témoin bénin : 80 → 90, preuve relue, Phase 5 fusionne au SHA exact puis resynchronise ───────────────────────────


async def test_benign_improvement_is_promoted_proved_and_merged_by_phase_5_at_the_exact_sha(world):
    result = await improve(
        world,
        [metrics(80), metrics(90)],
        agent=FilesFeature({"docs/gain.md": "# gain\n"}),
        promotion_hook=phase5_hook(world),
    )

    assert len(result.promoted) == 1, result.rejected
    promoted = result.promoted[0]
    proof = reloaded_proof(world, promoted.pr_number, promoted.head_sha)
    assert proof.phase == "improve" and proof.passed is True
    assert proof.tree_sha == world.bridge.remote.tree_of(promoted.head_sha)
    # Phase 5 : un seul appel de fusion, avec le SHA de tête exact ; le commit de fusion est un vrai objet Git ; le checkout suit
    (body,) = world.bridge.merge_bodies()
    assert body["sha"] == promoted.head_sha and world.bridge.merged_pr_numbers() == [promoted.pr_number]
    tip = world.bridge.branches["main"]
    assert world.bridge.remote.tree_of(tip) == proof.tree_sha
    assert git_out(world.source, "rev-parse", "HEAD") == tip
    assert (Path(world.source) / "docs" / "gain.md").read_text() == "# gain\n"
    assert world.manager.get_phase5_incident(world.project_id) is None, "incident clos : santé de main confirmée"


# ── contraintes bloquantes (lot A) vues depuis l'entrée publique, sans aucune publication ────────────────────────────


@pytest.mark.parametrize(
    "sequence, fragment",
    [
        pytest.param(
            [metrics(90, lint=20), metrics(80, lint=0)], "couverture", id="couverture_90_vers_80_lint_meilleur"
        ),
        pytest.param(
            [metrics(0, lint=20, measured=False), metrics(0, lint=0, measured=False)],
            "indispensable",
            id="mesure_indispensable_absente",
        ),
        pytest.param(
            [metrics(40), metrics(95, review="blocking")], "revue bloquante", id="revue_bloquante_score_eleve"
        ),
        pytest.param([metrics(40), metrics(95, review="none")], "aucune revue", id="aucune_revue"),
        pytest.param([metrics(40), metrics(99, tests=False)], "tests rouges", id="tests_rouges"),
    ],
)
async def test_a_blocking_constraint_is_never_bought_back_by_the_score_and_nothing_is_published(
    world, sequence, fragment
):
    result = await improve(world, sequence, promotion_hook=phase5_hook(world))

    assert result.promoted == [] and fragment in result.rejected[0][1], result.rejected
    assert nothing_published(world)


async def test_a_delivered_contract_broken_by_an_improvement_blocks_it_while_a_harmless_one_is_promoted(world):
    await seal_delivered(
        world, [FEATURE_ORACLE]
    )  # le projet exige DURABLEMENT cet oracle (plan approuvé, tâche livrée)

    class Breaking(Feature):
        def implement_issue(self, workspace, issue):
            (Path(workspace) / "feature.py").write_text("VALUE = 0\n")  # casse le contrat livré (VALUE == 42)
            return super().implement_issue(workspace, issue)

    refused = await improve(world, [metrics(80), metrics(90)], agent=Breaking(), sandbox=LocalOracleSandbox())
    assert refused.promoted == [] and "contrat" in refused.rejected[0][1], refused.rejected
    assert nothing_published(world)

    kept = await improve(
        world, [metrics(80), metrics(90)], sandbox=LocalOracleSandbox()
    )  # témoin : même chemin, contrat intact
    assert len(kept.promoted) == 1, kept.rejected
    proof = reloaded_proof(world, kept.promoted[0].pr_number, kept.promoted[0].head_sha)
    assert proof.contracts_required is True and [o.role for o in proof.oracles] == ["delivered"]


# ── Phase 5 : les chemins sensibles restent bloqués même avec une allowlist élargie à tout ──────────────────────────

SENSITIVE = [
    "locks/dev.txt",
    "requirements.txt",
    "requirements-lock.txt",
    "docker/sandbox/Dockerfile.openhands",
    "Dockerfile.openhands",
    "collegue/migrations/versions/0099_nouvelle.py",
    "pyproject.toml",
]


@pytest.mark.parametrize("path", SENSITIVE, ids=[p.replace("/", "_") for p in SENSITIVE])
async def test_phase_5_never_auto_merges_dependency_lock_dockerfile_or_migration_files_even_with_a_widened_allowlist(
    world, path
):
    hook = phase5_hook(world, allowlist=("*", "**/*"))  # allowlist élargie à TOUT : seule la garde dure peut arrêter
    result = await improve(
        world, [metrics(80), metrics(90)], agent=FilesFeature({path: "x = 1\n"}), promotion_hook=hook
    )

    assert len(result.promoted) == 1, result.rejected  # l'amélioration est légitime : PR ouverte avec sa preuve
    assert world.bridge.merge_calls() == [] and world.bridge.merged_pr_numbers() == [], (
        "mais jamais fusionnée automatiquement"
    )
    assert world.bridge.prs[result.promoted[0].pr_number]["state"] == "open"


async def test_phase_5_merges_the_same_change_when_it_is_a_documentation_file_under_the_same_widened_allowlist(world):
    hook = phase5_hook(world, allowlist=("*", "**/*"))
    result = await improve(
        world, [metrics(80), metrics(90)], agent=FilesFeature({"docs/note.md": "doc\n"}), promotion_hook=hook
    )
    assert len(result.promoted) == 1 and world.bridge.merged_pr_numbers() == [result.promoted[0].pr_number]


# ── promotions empilées : la base de la 2ᵉ PR est la branche de la 1ʳᵉ ; la politique de fusion de B refuse sa base ─────────


async def test_stacked_promotions_have_distinct_bases_and_the_second_is_not_mergeable_into_main_before_the_first(world):
    # chaque round mesure AVANT puis APRÈS : deux promotions = [avant 1, après 1, avant 2, après 2]
    result = await improve(world, [metrics(60), metrics(75), metrics(75), metrics(90)], plateau_rounds=2)

    assert len(result.promoted) == 2, result.rejected
    first, second = result.promoted
    bridge = world.bridge
    assert bridge.prs[first.pr_number]["base"]["ref"] == "main"
    assert bridge.prs[second.pr_number]["base"]["ref"] == bridge.prs[first.pr_number]["head"]["ref"], "PR empilée"
    proof_first = reloaded_proof(world, first.pr_number, first.head_sha)
    proof_second = reloaded_proof(world, second.pr_number, second.head_sha)
    assert proof_first.base_sha == bridge.branches["main"]
    assert proof_second.base_sha == first.head_sha, "la base prouvée de la PR empilée est la tête de la PR parente"
    assert proof_second.tree_sha == bridge.remote.tree_of(second.head_sha)

    manager = ProjectStateManager.from_url(world.url, create=False)
    common = dict(project_id=world.project_id, owner=OWNER, repo=REPO, base="main", expected_phase="improve")
    approval = verify_merge_candidate(bridge.clients(), manager, pr_number=first.pr_number, **common)
    assert approval.head_sha == first.head_sha and bridge.merge_calls() == []  # la 1ʳᵉ est validable (aucune écriture)
    with pytest.raises(MergeRefused, match="base de la PR inattendue"):
        verify_merge_candidate(bridge.clients(), manager, pr_number=second.pr_number, **common)
    assert bridge.merge_calls() == []
