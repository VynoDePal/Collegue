"""Promotion IMPROVE (vague 3) : les contraintes bloquantes ne sont jamais rachetées par un meilleur score.

Entrée publique ``run_improvement`` ; Git local, SQLite, contrats scellés, mesure réelle (``measure``) ou scriptée ;
transport GitHub factice mais COMPLET (``tests/github_fakes.py``). Chaque refus a son témoin bénin et son motif précis.
"""

from __future__ import annotations

import functools
from pathlib import Path
from types import SimpleNamespace

import pytest
from github_fakes import FakeRemote, make_source_repo
from oracle_sandbox import LocalOracleSandbox

from collegue.executor import AgentResult, FakeReviewer
from collegue.executor.delivery_proof import load_delivery_proof
from collegue.executor.quality_gate import ReviewFindingLite
from collegue.improve import ProjectQualityMetrics, composite_score, run_improvement
from collegue.improve.metrics import measure
from collegue.pilot import ACTION_CONTINUE, ContinueDecision
from collegue.planner import generate_acceptance_tests
from collegue.planner.plan_review import approve_plan
from collegue.sandbox import SandboxResult
from collegue.state import ProjectStateManager

OWNER, REPO = "o", "r"
CONT = ContinueDecision(action=ACTION_CONTINUE, reason="ok")


class Budget:
    def should_continue(self):
        return CONT


def metrics(
    coverage,
    *,
    lint=0,
    measured=True,
    tests=True,
    review="clean",
    security_weighted=0.0,
    composite=None,
):
    """Instantané tel que `measure` le produirait avec un reviewer (review='clean' | 'blocking' | 'none' | 'error')."""
    return ProjectQualityMetrics(
        coverage_pct=coverage,
        security_findings=0,
        security_weighted=security_weighted,
        tests_passed=tests,
        composite=composite
        if composite is not None
        else composite_score(coverage, security_weighted, lint_violations=lint),
        coverage_measured=measured,
        lint_violations=lint,
        review_measured=review in ("clean", "blocking"),
        review_blocking=review == "blocking",
        review_error="reviewer indisponible" if review == "error" else "",
    )


class Scripted:
    def __init__(self, sequence):
        self.sequence = list(sequence)
        self.calls = 0

    async def __call__(self, workspace, ctx, *, sandbox=None, reviewer=None, diff="", weights=None, **_):
        value = self.sequence[min(self.calls, len(self.sequence) - 1)]
        self.calls += 1
        return value


class Feature:
    """Agent : ajoute/écrit des fichiers texte (un nouveau à chaque round)."""

    budget_enforcement = "test-double"

    def __init__(self, files=None):
        self.files = files
        self.n = 0

    def implement_issue(self, workspace, issue):
        self.n += 1
        files = self.files or {f"gain_{self.n}.py": f"GAIN = {self.n}\n"}
        for rel, text in files.items():
            (Path(workspace) / rel).write_text(text)
        return AgentResult(success=True)


@pytest.fixture
def world(tmp_path):
    source = make_source_repo(tmp_path / "source", {"README.md": "# base\n", "feature.py": "VALUE = 42\n"})
    manager = ProjectStateManager.from_url(f"sqlite:///{tmp_path / 'state.db'}", create=True)
    return SimpleNamespace(
        source=source,
        manager=manager,
        project_id=manager.create_project(name="improve"),
        remote=FakeRemote(tmp_path, source),
        tmp=tmp_path,
    )


async def improve(world, sequence, *, agent=None, dry_run=False, **kwargs):
    measure_fn = sequence if callable(sequence) else Scripted(sequence)
    return await run_improvement(
        world.project_id,
        world.source,
        None,
        agent=agent or Feature(),
        owner=OWNER,
        repo=REPO,
        manager=world.manager,
        budget=Budget(),
        clients=world.remote.clients(),
        dry_run=dry_run,
        plateau_rounds=kwargs.pop("plateau_rounds", 1),
        measure_fn=measure_fn,
        **kwargs,
    )


def no_pr(world):
    return "create_pr" not in world.remote.calls and "update_file" not in world.remote.calls


# --- témoin bénin ------------------------------------------------------------------------------------------------------


async def test_benign_coverage_gain_is_promoted_with_a_persisted_improve_proof(world):
    result = await improve(world, [metrics(80), metrics(90)])

    assert len(result.promoted) == 1, result.rejected
    promoted = result.promoted[0]
    proof = promoted.proof
    assert proof.phase == "improve" and proof.passed is True
    assert {v.name for v in proof.verdicts} >= {
        "content_integrity",
        "tests",
        "review",
        "coverage",
        "secret_scan",
        "gate",
    }
    assert proof.verdict("coverage").passed and "80.0% → 90.0%" in proof.verdict("coverage").reason
    assert proof.verdict("secret_scan").reason.startswith("scan statique de secrets")
    assert "pas un audit de sécurité" in proof.verdict("secret_scan").reason
    # publié == testé, vérifié côté distant par git
    assert world.remote.tree_of(promoted.head_sha) == proof.tree_sha
    # preuve relue par une NOUVELLE instance du manager
    fresh = ProjectStateManager.from_url(f"sqlite:///{world.tmp / 'state.db'}", create=False)
    loaded = load_delivery_proof(
        fresh, world.project_id, owner=OWNER, repo=REPO, pr_number=promoted.pr_number, head_sha=promoted.head_sha
    )
    assert loaded == proof


# --- couverture ----------------------------------------------------------------------------------------------------------


async def test_coverage_90_to_80_is_refused_even_when_lint_improves_and_the_score_rises(world):
    before, after = metrics(90, lint=20), metrics(80, lint=0)
    assert after.composite > before.composite  # le score seul promouvrait : défaut reproduit
    result = await improve(world, [before, after])
    assert result.promoted == []
    assert "couverture" in result.rejected[0][1]
    assert no_pr(world) and world.remote.calls == []


async def test_missing_indispensable_coverage_measure_is_refused_and_cannot_be_waived(world):
    sequence = [metrics(0, lint=20, measured=False), metrics(0, lint=0, measured=False)]
    refused = await improve(world, sequence)
    assert refused.promoted == [] and "indispensable" in refused.rejected[0][1]
    assert world.remote.calls == []
    for waiver in ({"coverage_required": False}, {"coverage_slack": 50.0}):
        with pytest.raises(TypeError):  # aucune voie de contournement n'existe côté appelant
            await improve(world, sequence, **waiver)
    assert world.remote.calls == []


# --- revue ----------------------------------------------------------------------------------------------------------------


async def test_a_blocking_review_vetoes_a_much_better_score(world):
    result = await improve(world, [metrics(40), metrics(95, review="blocking")])
    assert result.promoted == [] and "revue bloquante" in result.rejected[0][1]
    assert world.remote.calls == []


@pytest.mark.parametrize(
    ("review", "fragment"),
    [("none", "aucune revue"), ("error", "revue indisponible")],
    ids=["aucune-revue", "reviewer-en-panne"],
)
async def test_a_missing_or_failed_review_is_not_a_clean_review(world, review, fragment):
    result = await improve(world, [metrics(40), metrics(95, review=review)])
    assert result.promoted == [] and fragment in result.rejected[0][1]
    assert world.remote.calls == []


async def test_the_review_cannot_be_waived(world):
    with pytest.raises(TypeError):
        await improve(world, [metrics(40), metrics(95, review="none")], review_required=False)
    assert world.remote.calls == []


async def test_red_tests_are_refused_whatever_the_score(world):
    result = await improve(world, [metrics(40), metrics(99, tests=False)])
    assert result.promoted == [] and "tests rouges" in result.rejected[0][1]
    assert world.remote.calls == []


async def test_a_worse_secret_scan_is_refused_and_named_precisely(world):
    result = await improve(world, [metrics(40), metrics(95, security_weighted=5.0)])
    assert result.promoted == [] and "scan de secrets" in result.rejected[0][1]


# --- mesure RÉELLE de bout en bout (reviewer, couverture parsée, lint) ----------------------------------------------------


class CoverageByFeature:
    """Couverture 90 % tant que le projet n'a pas reçu `gain_*.py`, puis 80 % ; sortie parsée par `measure` réel."""

    def run_tests(self, workspace, command="pytest -q"):
        improved = any(Path(workspace).glob("gain_*.py"))
        return SandboxResult(
            exit_code=0, stdout=f"TOTAL  100  {20 if improved else 10}  {80 if improved else 90}%", stderr=""
        )


def real_measure(world, *, lint_before=20):
    def quality(workspace):
        improved = any(Path(workspace).glob("gain_*.py"))
        return (0 if improved else lint_before), 0, True

    return functools.partial(
        measure,
        security_scan_fn=lambda ws: (0, 0.0),
        quality_scan_fn=quality,
        doc_coverage_fn=lambda ws: 1.0,
    )


async def test_real_measure_refuses_a_coverage_drop_hidden_by_a_lint_gain(world):
    result = await improve(
        world, real_measure(world), sandbox=CoverageByFeature(), reviewer=FakeReviewer(), min_gain=0.01
    )
    assert result.promoted == [] and "couverture" in result.rejected[0][1]
    assert world.remote.calls == []


async def test_real_measure_carries_the_blocking_review_into_the_decision(world):
    class Improves:
        def run_tests(self, workspace, command="pytest -q"):
            improved = any(Path(workspace).glob("gain_*.py"))
            return SandboxResult(exit_code=0, stdout=f"TOTAL  100  10  {95 if improved else 40}%", stderr="")

    blocking = FakeReviewer(blocking=True, findings=[ReviewFindingLite("security", "critical", "RCE")])
    refused = await improve(world, real_measure(world, lint_before=0), sandbox=Improves(), reviewer=blocking)
    assert refused.promoted == [] and "revue bloquante" in refused.rejected[0][1]

    clean = await improve(world, real_measure(world, lint_before=0), sandbox=Improves(), reviewer=FakeReviewer())
    assert len(clean.promoted) == 1, clean.rejected  # témoin : même chemin, revue propre

    unreviewed = await improve(world, real_measure(world, lint_before=0), sandbox=Improves())
    assert unreviewed.promoted == [] and "aucune revue" in unreviewed.rejected[0][1]


# --- contrats déjà livrés ------------------------------------------------------------------------------------------------

FEATURE_ORACLE = (
    "from pathlib import Path\n"
    "def test_feature_value():\n"
    "    feature = Path.cwd() / 'feature.py'\n"
    "    assert feature.is_file(), 'feature.py est requis par le critère'\n"
    "    namespace = {}\n"
    "    exec(feature.read_text(), namespace)\n"
    "    assert namespace['VALUE'] == 42\n"
)
OTHER_ORACLE = (
    "from pathlib import Path\n"
    "def test_readme_title():\n"
    "    readme = Path.cwd() / 'README.md'\n"
    "    assert readme.is_file(), 'README.md est requis par le critère'\n"
    "    assert readme.read_text().startswith('# base'), 'titre attendu'\n"
)
SETTINGS = SimpleNamespace(
    LLM_PROVIDER="gemini", LLM_MODEL="default", LLM_PROVIDER_QA="openai", LLM_MODEL_QA="qa", LLM_CALL_TIMEOUT=0
)


async def seal_delivered(world, oracles):
    """Projet BUILD terminé : tâches livrées (`merged`) avec oracles scellés et plan approuvé."""
    spec = "# SPEC\nproduit de test\n"
    manager = world.manager
    pid = manager.create_project(name="build-livré", spec=spec)
    ids = []
    for index in range(len(oracles)):
        ids.append(
            manager.add_task(pid, title=f"Tâche {index + 1}", acceptance=f"Critère {index + 1}", depends_on=ids[-1:])
        )
    sources = iter(oracles)
    await generate_acceptance_tests(
        spec,
        manager.get_tasks(pid),
        None,
        manager=manager,
        project_id=pid,
        settings_obj=SETTINGS,
        sample_fn=lambda _p, _s: next(sources),
    )
    approve_plan(manager, pid, require_acceptance_artifacts=True)
    for task_id in ids:
        manager.update_task_status(task_id, "merged")
    world.project_id = pid
    return ids


async def test_improvement_that_keeps_every_delivered_contract_is_promoted_with_their_evidence(world):
    ids = await seal_delivered(world, [FEATURE_ORACLE])
    result = await improve(world, [metrics(80), metrics(90)], sandbox=LocalOracleSandbox())
    assert len(result.promoted) == 1, result.rejected
    proof = result.promoted[0].proof
    assert proof.contracts_required is True and proof.verdict("contracts").passed
    (oracle,) = proof.oracles
    assert (oracle.task_id, oracle.role) == (ids[0], "delivered") and oracle.candidate.status == "green"
    assert oracle.preimage is None and oracle.expected_preimage == "not-required"


async def test_improvement_breaking_a_delivered_contract_is_refused_whatever_the_score(world):
    ids = await seal_delivered(world, [FEATURE_ORACLE])
    breaking = Feature({"feature.py": "VALUE = 0\n"})  # « optimise » en cassant le contrat livré
    result = await improve(world, [metrics(80), metrics(99)], agent=breaking, sandbox=LocalOracleSandbox())
    assert result.promoted == []
    assert f"tâche {ids[0]}" in result.rejected[0][1] and "cassé" in result.rejected[0][1]
    assert world.remote.calls == []


async def test_improvement_cannot_substitute_workspace_tests_for_the_sealed_contract(world):
    await seal_delivered(world, [FEATURE_ORACLE])
    cheat = Feature(
        {
            "feature.py": "VALUE = 0\n",
            "test_feature.py": "def test_feature_value():\n    assert True\n",
            "conftest.py": "def pytest_collection_modifyitems(items):\n    items.clear()\n",
        }
    )
    result = await improve(world, [metrics(80), metrics(99)], agent=cheat, sandbox=LocalOracleSandbox())
    assert result.promoted == [] and world.remote.calls == []


async def test_a_delivered_task_without_its_sealed_oracle_blocks_the_improvement(world):
    ids = await seal_delivered(world, [FEATURE_ORACLE, OTHER_ORACLE])
    with world.manager.session() as session:
        from collegue.state.models import Task

        task = session.get(Task, ids[1])  # l'oracle d'UNE tâche livrée disparaît de l'état
        task.acceptance_test_source = task.acceptance_test_sha256 = task.acceptance_test_provenance = None
    result = await improve(world, [metrics(80), metrics(90)], sandbox=LocalOracleSandbox())
    assert result.promoted == [] and "contracts" in result.rejected[0][1]  # non-régression invérifiable : refus
    assert world.remote.calls == []


async def test_explicitly_required_contracts_with_no_delivered_oracle_refuse_the_improvement(world):
    ids = await seal_delivered(world, [FEATURE_ORACLE])
    world.manager.update_task_status(ids[0], "in_review")  # rien de livré : aucun contrat à rejouer
    plain = ProjectStateManager.from_url(f"sqlite:///{world.tmp / 'plain.db'}", create=True)
    world_plain = SimpleNamespace(
        **{**vars(world), "manager": plain, "project_id": plain.create_project(name="sans-oracle")}
    )
    benign = await improve(world_plain, [metrics(80), metrics(90)], sandbox=LocalOracleSandbox(), dry_run=True)
    assert len(benign.promoted) == 1  # témoin : un projet sans exigence ni oracle n'a rien à rejouer
    # l'ÉTAT exige des contrats (acceptance_tests_required) mais aucune tâche livrée n'en porte : refus
    assert world.manager.get_project(world.project_id).acceptance_tests_required is True
    required = await improve(world, [metrics(80), metrics(90)], sandbox=LocalOracleSandbox())
    assert required.promoted == [] and "aucune tâche livrée ne porte d'oracle" in required.rejected[0][1]
    assert world.remote.calls == []
    with pytest.raises(TypeError):  # un paramètre d'appelant ne peut pas lever l'exigence de l'état
        await improve(world, [metrics(80), metrics(90)], sandbox=LocalOracleSandbox(), contracts_required=False)


async def test_unapproved_plan_blocks_the_replay_of_delivered_contracts(world):
    await seal_delivered(world, [FEATURE_ORACLE])
    world.manager.update_project(world.project_id, spec="# SPEC modifiée après approbation\n")
    result = await improve(world, [metrics(80), metrics(90)], sandbox=LocalOracleSandbox())
    assert result.promoted == [] and "non approuvé" in result.rejected[0][1]
    assert world.remote.calls == []


# --- intégrité du contenu ---------------------------------------------------------------------------------------------------


async def test_a_measure_that_modifies_a_base_file_invalidates_the_round(world):
    sequence = [metrics(80), metrics(90)]
    calls = {"n": 0}

    async def mutating(workspace, ctx, *, sandbox=None, reviewer=None, diff="", weights=None, **_):
        value = sequence[min(calls["n"], 1)]
        if calls["n"] == 1:  # la mesure « après » altère un fichier de BASE (hors diff)
            (Path(workspace) / "README.md").write_text("# modifié pendant la mesure\n")
        calls["n"] += 1
        return value

    result = await improve(world, mutating)
    assert result.promoted == [] and "intégrité du livrable refusée" in result.rejected[0][1]
    assert "README.md" in result.rejected[0][1] and world.remote.calls == []


async def test_a_binary_or_executable_improvement_is_refused_before_measuring(world):
    scripted = Scripted([metrics(80), metrics(90)])

    class Binary:
        budget_enforcement = "test-double"

        def implement_issue(self, workspace, issue):
            (Path(workspace) / "asset.bin").write_bytes(b"\xff\xfe\x00")
            return AgentResult(success=True)

    result = await improve(world, scripted, agent=Binary())
    assert result.promoted == [] and "asset.bin" in result.rejected[0][1]
    assert scripted.calls == 1  # seule la baseline a été mesurée
    assert world.remote.calls == []


async def test_publication_refusal_discards_the_round_and_records_no_proof(world):
    world.remote.advance_base()  # la base distante a bougé depuis le clone local
    result = await improve(world, [metrics(80), metrics(90)])
    assert result.promoted == [] and "livraison refusée" in result.rejected[0][1]
    assert world.manager.get_decision_journal(world.project_id, "delivery-proof") == []


async def test_dry_run_applies_the_same_constraints_without_any_remote_call(world):
    refused = await improve(world, [metrics(90, lint=20), metrics(80)], dry_run=True)
    assert refused.promoted == [] and "couverture" in refused.rejected[0][1]
    ok = await improve(world, [metrics(80), metrics(90)], dry_run=True)
    assert len(ok.promoted) == 1 and ok.promoted[0].proof is None  # aperçu : pas de preuve
    assert world.remote.calls == []


async def test_rerunning_the_same_improvement_reuses_the_verified_pr_and_its_immutable_proof(world):
    agent = Feature({"gain.py": "GAIN = 1\n"})
    first = await improve(world, [metrics(80), metrics(90)], agent=agent)
    again = await improve(world, [metrics(80), metrics(90)], agent=Feature({"gain.py": "GAIN = 1\n"}))
    assert len(first.promoted) == len(again.promoted) == 1
    assert again.promoted[0].pr_number == first.promoted[0].pr_number  # PR retrouvée, tête RE-VÉRIFIÉE
    assert again.promoted[0].proof.proof_id == first.promoted[0].proof.proof_id
    assert len(world.manager.get_decision_journal(world.project_id, "delivery-proof:v1:")) == 1


async def test_a_found_pr_of_another_revision_is_not_an_improvement_delivery(world):
    await improve(world, [metrics(80), metrics(90)], agent=Feature({"gain.py": "GAIN = 1\n"}))
    refs = world.remote._git("for-each-ref", "--format=%(refname:strip=2)", "refs/heads/collegue/").splitlines()
    (head,) = refs  # la tête d'AMÉLIORATION publiée (jamais ``collegue/issue-<round>``)
    assert head.startswith("collegue/improve-r1-") and not head.startswith("collegue/issue-")
    world.remote._commit_change(head, "intrus.py", "autre révision\n", "tiers")  # un tiers pousse sur la tête
    result = await improve(world, [metrics(80), metrics(90)], agent=Feature({"gain.py": "GAIN = 1\n"}))
    assert result.promoted == [] and "arbre publié" in result.rejected[0][1]
    assert len(world.manager.get_decision_journal(world.project_id, "delivery-proof:v1:")) == 1  # pas de 2e preuve
