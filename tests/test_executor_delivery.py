"""Livraison BUILD de bout en bout (vague 3) : ce qui est testé est ce qui est publié, la preuve est durable.

Entrée publique ``execute_issue`` ; Git local, SQLite, oracles pytest et persistance RÉELS ; seul le transport GitHub est
factice mais COMPLET (``tests/github_fakes.py`` : un vrai dépôt Git distant). Chaque refus est accompagné d'un témoin
bénin qui aboutit par le même chemin et du motif précis du refus (jamais un ``AttributeError`` de double incomplet).
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from github_fakes import FakeRemote, git, make_source_repo
from oracle_sandbox import LocalOracleSandbox

from collegue.executor import AgentResult, FakeReviewer, IssueSpec, execute_issue
from collegue.executor.delivery_proof import (
    DeliveryProofError,
    Verdict,
    compute_proof_id,
    load_delivery_proof,
    persist_delivery_proof,
)
from collegue.executor.quality_gate import ReviewFindingLite, StoredAcceptanceChecker
from collegue.planner import generate_acceptance_tests
from collegue.planner.plan_review import approve_plan
from collegue.sandbox import SandboxResult
from collegue.state import ProjectStateManager

OWNER, REPO = "o", "r"
ISSUE = IssueSpec(number=21, title="Livrer la chose")


class GreenSandbox:
    """Tests du projet verts (le gate n'est pas le sujet) ; n'exécute rien."""

    def run_tests(self, workspace, command="pytest -q"):
        return SandboxResult(exit_code=0, stdout="3 passed", stderr="")


class Writes:
    """Agent déterministe : écrit/supprime des fichiers dans le workspace."""

    budget_enforcement = "test-double"

    def __init__(self, files=None, *, delete=(), binary=None, links=None, executable=()):
        self.files, self.delete, self.binary, self.links, self.executable = (
            files or {},
            delete,
            binary or {},
            links or {},
            executable,
        )

    def implement_issue(self, workspace, issue):
        root = Path(workspace)
        for rel, text in self.files.items():
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_text(text)
        for rel in self.delete:
            (root / rel).unlink()
        for rel, data in self.binary.items():
            (root / rel).write_bytes(data)
        for rel, target in self.links.items():
            (root / rel).symlink_to(target)
        for rel in self.executable:
            (root / rel).chmod(0o755)
        return AgentResult(success=True)


@pytest.fixture
def world(tmp_path):
    source = make_source_repo(
        tmp_path / "source",
        {"base.py": "BASE = 'original'\n", "existing.txt": "original\n"},
        gitignore="hidden_contract.py\nhidden_pkg/\n",
    )
    manager = ProjectStateManager.from_url(f"sqlite:///{tmp_path / 'state.db'}", create=True)
    project_id = manager.create_project(name="demo")
    remote = FakeRemote(tmp_path, source)
    return type(
        "World", (), {"source": source, "manager": manager, "project_id": project_id, "remote": remote, "tmp": tmp_path}
    )()


async def deliver(world, agent, *, sandbox=None, reviewer=None, issue=ISSUE, manager=True, gate_options=None, **kw):
    return await execute_issue(
        issue,
        world.source,
        None,
        dry_run=False,
        agent=agent,
        owner=OWNER,
        repo=REPO,
        sandbox=sandbox or GreenSandbox(),
        reviewer=reviewer or FakeReviewer(),
        clients=world.remote.clients(),
        manager=world.manager if manager else None,
        project_id=world.project_id if manager else None,
        gate_options=gate_options,
        **kw,
    )


# --- témoin bénin : livraison complète, preuve durable ---------------------------------------------------------------


async def test_benign_delivery_publishes_the_tested_tree_and_persists_a_loadable_proof(world):
    outcome = await deliver(
        world, Writes({"feature.py": "FEATURE = True\n", "existing.txt": "modifié\n"}, delete=("base.py",))
    )

    assert outcome.success, outcome.error
    remote, proof = world.remote, outcome.proof
    head = outcome.pr.head
    assert remote.files_at(head) == {
        "feature.py": "FEATURE = True\n",
        "existing.txt": "modifié\n",
        ".gitignore": "hidden_contract.py\nhidden_pkg/\n",
    }
    # arbre publié == arbre testé, calculé par git côté distant (pas par le code de production)
    assert remote.tree_of(remote.branch_sha(head)) == outcome.tested_content.tree_sha == proof.tree_sha
    assert proof.head_sha == remote.branch_sha(head) == outcome.pr.head_sha
    assert proof.base_sha == remote.branch_sha("main") and proof.base_tree_sha == remote.tree_of(proof.base_sha)
    assert proof.passed is True and proof.phase == "build"
    assert {v.name for v in proof.verdicts} >= {"content_integrity", "tests", "review", "gate"}
    assert set(proof.delivered_paths) == {"feature.py", "existing.txt", "base.py"}

    # preuve durable : relue par une NOUVELLE instance du manager, sans objet en mémoire
    fresh = ProjectStateManager.from_url(f"sqlite:///{world.tmp / 'state.db'}", create=False)
    loaded = load_delivery_proof(
        fresh, world.project_id, owner=OWNER, repo=REPO, pr_number=outcome.pr.number, head_sha=outcome.pr.head_sha
    )
    assert loaded == proof and loaded.proof_id == proof.proof_id
    # le corps de la PR n'est qu'une trace humaine : la preuve ne se déduit pas de lui
    body = world.remote.prs[outcome.pr.number].body
    assert f"collegue-tree-sha:{proof.tree_sha}" in body
    with pytest.raises(Exception, match="aucune preuve"):
        load_delivery_proof(
            fresh, world.project_id, owner=OWNER, repo=REPO, pr_number=outcome.pr.number, head_sha="1" * 40
        )


async def test_rerun_on_the_same_head_reuses_the_immutable_proof(world):
    agent = Writes({"feature.py": "FEATURE = True\n"})
    first = await deliver(world, agent)
    second = await deliver(world, agent)  # même branche, même contenu : PR existante re-vérifiée
    assert first.success and second.success, second.error
    assert second.pr.skipped is True and second.pr.number == first.pr.number
    assert second.proof.proof_id == first.proof.proof_id  # pas de seconde vérité
    assert len(world.manager.get_decision_journal(world.project_id, "delivery-proof:v1:")) == 1


async def test_identical_rerun_with_a_new_manager_instance_is_idempotent_and_a_real_conflict_is_refused(world):
    agent = Writes({"feature.py": "FEATURE = True\n"})
    first = await deliver(world, agent)
    url = f"sqlite:///{world.tmp / 'state.db'}"
    world.manager = ProjectStateManager.from_url(url, create=False)  # reprise après redémarrage
    second = await deliver(world, agent)
    assert first.success and second.success, second.error
    assert second.proof.proof_id == first.proof.proof_id
    third_instance = ProjectStateManager.from_url(url, create=False)
    loaded = load_delivery_proof(
        third_instance, world.project_id, owner=OWNER, repo=REPO, pr_number=first.pr.number, head_sha=first.pr.head_sha
    )
    assert loaded == first.proof
    assert len(third_instance.get_decision_journal(world.project_id, "delivery-proof:v1:")) == 1

    # VRAI conflit : une autre preuve valide, de contenu différent, existe déjà pour la même tête ⇒ refus, pas d'écrasement
    other = replace(first.proof, verdicts=first.proof.verdicts + (Verdict("extra", False, True, "autre vérité"),))
    other = replace(other, proof_id=compute_proof_id(other))
    persist_delivery_proof(world.manager, other)
    refused = await deliver(world, agent)
    assert not refused.success and refused.proof is None
    assert "preuve" in refused.error
    with pytest.raises(DeliveryProofError, match="distinctes"):
        load_delivery_proof(
            world.manager,
            world.project_id,
            owner=OWNER,
            repo=REPO,
            pr_number=first.pr.number,
            head_sha=first.pr.head_sha,
        )


# --- format non représentable : refus explicite AVANT toute écriture --------------------------------------------------


@pytest.mark.parametrize(
    ("agent", "fragment"),
    [
        (Writes({"feature.py": "F = 1\n"}, binary={"required.bin": b"\xff\xfe\x00business-data"}), "required.bin"),
        (Writes({"feature.py": "F = 1\n"}, links={"required-link": "feature.py"}), "required-link"),
        (Writes({"deploy.sh": "#!/bin/sh\n"}, executable=("deploy.sh",)), "deploy.sh (100755)"),
    ],
    ids=["binaire", "lien-symbolique", "script-exécutable"],
)
async def test_unrepresentable_formats_are_refused_before_any_remote_write(world, agent, fragment):
    outcome = await deliver(world, agent)
    assert not outcome.success and outcome.reason == "gate_failed" and outcome.stage == "gate"
    assert "LIVRAISON REFUSÉE" in outcome.error and fragment in outcome.error
    assert outcome.proof is None and outcome.pr is None
    assert world.remote.calls == []  # aucune écriture distante, pas même une branche
    assert world.manager.get_decision_journal(world.project_id, "delivery-proof") == []


# --- contenu qui change pendant les contrôles ------------------------------------------------------------------------


class MutatingSandbox(GreenSandbox):
    def __init__(self, path, text):
        self.path, self.text = path, text

    def run_tests(self, workspace, command="pytest -q"):
        (Path(workspace) / self.path).write_text(self.text)
        return super().run_tests(workspace, command)


async def test_a_base_file_modified_by_the_gate_invalidates_the_delivery(world):
    outcome = await deliver(
        world,
        Writes({"feature.py": "F = 1\n"}),
        sandbox=MutatingSandbox("base.py", "BASE = 'changé pendant les tests'\n"),
    )
    assert not outcome.success and "INTÉGRITÉ DU LIVRABLE REFUSÉE" in outcome.error and "base.py" in outcome.error
    assert world.remote.calls == [] and outcome.proof is None


async def test_a_delivered_file_modified_by_the_gate_invalidates_the_delivery(world):
    outcome = await deliver(
        world, Writes({"feature.py": "F = 1\n"}), sandbox=MutatingSandbox("feature.py", "F = 'substitué'\n")
    )
    assert not outcome.success and "INTÉGRITÉ DU LIVRABLE REFUSÉE" in outcome.error and "feature.py" in outcome.error
    assert world.remote.calls == []


# --- entrée ignorée indispensable aux tests ------------------------------------------------------------------------------


class ImportSandbox:
    """Exécute VRAIMENT le code métier du projet (comme un gate réel) : ``from feature import FEATURE``."""

    def __init__(self):
        self.runs = []

    def run_tests(self, workspace, command="pytest -q"):
        proc = subprocess.run(
            [sys.executable, "-B", "-c", "from feature import FEATURE; assert FEATURE == 42"],
            cwd=workspace,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.runs.append(proc.returncode)
        return SandboxResult(exit_code=proc.returncode, stdout=proc.stdout, stderr=proc.stderr)


async def test_self_contained_feature_passes_and_is_delivered(world):
    sandbox = ImportSandbox()
    outcome = await deliver(world, Writes({"feature.py": "FEATURE = 42\n"}), sandbox=sandbox)
    assert outcome.success, outcome.error
    assert sandbox.runs == [0] and "feature.py" in world.remote.files_at(outcome.pr.head)


async def test_an_ignored_module_needed_by_the_tests_cannot_make_them_pass(world):
    """`hidden_contract.py` est ignoré (.gitignore) : les tests y trouveraient VALUE alors que la livraison l'omet."""
    sandbox = ImportSandbox()
    agent = Writes(
        {"hidden_contract.py": "VALUE = 42\n", "feature.py": "from hidden_contract import VALUE\nFEATURE = VALUE\n"}
    )
    outcome = await deliver(world, agent, sandbox=sandbox)
    assert sandbox.runs and sandbox.runs[0] != 0  # purgé avant les contrôles : les tests échouent honnêtement
    assert not outcome.success and outcome.stage == "gate" and outcome.proof is None
    assert "hidden_contract" in outcome.quality_report.test_output
    assert world.remote.calls == []


class _PayloadAgent(Writes):
    """Écrit un module/une donnée dans une entrée Git NON livrable, lue ensuite par le code métier."""

    def __init__(self, mode):
        super().__init__({})
        self.mode = mode

    def implement_issue(self, workspace, issue):
        root = Path(workspace)
        if self.mode == "nested-ignored-repo":
            package = root / "hidden_pkg"
            package.mkdir()
            (package / "__init__.py").write_text("VALUE = 42\n")
            scratch = root.parent / "inert-source"
            scratch.mkdir()
            git(scratch, "init", "-q", "-b", "main")
            shutil.copytree(scratch / ".git", package / ".git")  # vrai `.git` inerte dans le package ignoré
            (root / "feature.py").write_text("from hidden_pkg import VALUE\nFEATURE = VALUE\n")
        elif self.mode == "ignored-plain":
            (root / "hidden_pkg").mkdir()
            (root / "hidden_pkg" / "__init__.py").write_text("VALUE = 42\n")
            (root / "feature.py").write_text("from hidden_pkg import VALUE\nFEATURE = VALUE\n")
        elif self.mode == "root-git-payload":
            (root / ".git" / "manager_payload.txt").write_text("42\n")
            (root / "feature.py").write_text(
                "from pathlib import Path\n"
                "FEATURE = int((Path(__file__).parent / '.git' / 'manager_payload.txt').read_text())\n"
            )
        else:  # témoin autonome
            (root / "feature.py").write_text("FEATURE = 42\n")
        return AgentResult(success=True)


@pytest.mark.parametrize("mode", ["nested-ignored-repo", "ignored-plain", "root-git-payload"])
async def test_inputs_hidden_in_git_metadata_or_ignored_packages_cannot_make_the_checks_pass(world, mode):
    """Famille « contenu non livré servant aux tests » : le gate exécute le vrai code métier ; l'entrée est purgée
    (package ignoré, dépôt imbriqué) ou reconstruite (copie `.git` de l'agent) AVANT les contrôles."""
    sandbox = ImportSandbox()
    outcome = await deliver(world, _PayloadAgent(mode), sandbox=sandbox)
    assert sandbox.runs and sandbox.runs[0] != 0, "les tests ont réussi grâce à une entrée non livrée"
    assert not outcome.success and outcome.proof is None and outcome.stage == "gate"
    assert world.remote.calls == [] and world.manager.get_decision_journal(world.project_id, "delivery-proof") == []


async def test_the_self_contained_witness_still_succeeds_and_rebuilds_from_the_published_tree(world):
    sandbox = ImportSandbox()
    outcome = await deliver(world, _PayloadAgent("self-contained"), sandbox=sandbox)
    assert outcome.success, outcome.error
    assert sandbox.runs == [0]
    delivered = world.tmp / "delivered"
    delivered.mkdir()
    for path, text in world.remote.files_at(outcome.pr.head).items():
        (delivered / path).write_text(text)
    rebuilt = subprocess.run(
        [sys.executable, "-B", "-c", "from feature import FEATURE; assert FEATURE == 42"],
        cwd=delivered,
        capture_output=True,
    )
    assert rebuilt.returncode == 0  # reconstruction depuis l'arbre PUBLIÉ : même résultat que dans le workspace


# --- revue bloquante ---------------------------------------------------------------------------------------------------


async def test_a_blocking_review_yields_no_delivery_and_no_proof(world):
    reviewer = FakeReviewer(blocking=True, findings=[ReviewFindingLite("security", "critical", "RCE")])
    outcome = await deliver(world, Writes({"feature.py": "F = 1\n"}), reviewer=reviewer)
    assert not outcome.success and outcome.stage == "gate" and outcome.proof is None
    assert world.remote.calls == []


# --- état durable obligatoire ------------------------------------------------------------------------------------------


async def test_a_real_delivery_without_a_state_manager_is_refused_explicitly(world):
    outcome = await deliver(world, Writes({"feature.py": "F = 1\n"}), manager=False)
    assert not outcome.success and "manager et project_id requis" in outcome.error
    assert world.remote.calls == []


# --- course distante --------------------------------------------------------------------------------------------------


async def test_base_moved_before_publication_is_refused_without_writes(world):
    world.remote.advance_base()
    outcome = await deliver(world, Writes({"feature.py": "F = 1\n"}))
    assert not outcome.success and "base distante" in outcome.error
    assert "update_file" not in world.remote.calls and "create_pr" not in world.remote.calls
    assert world.manager.get_decision_journal(world.project_id, "delivery-proof") == []


async def test_base_moved_during_publication_never_yields_a_proof(world):
    moved = []

    def on_write(remote, path):
        if not moved:
            moved.append(remote.advance_base())

    world.remote.on_write = on_write
    outcome = await deliver(world, Writes({"feature.py": "F = 1\n"}))
    assert moved and not outcome.success and outcome.proof is None
    assert "base" in outcome.error  # la chaîne de commits ne rejoint plus la base vérifiée
    assert world.manager.get_decision_journal(world.project_id, "delivery-proof") == []


async def test_head_changed_by_someone_else_before_the_pr_is_bound_is_refused(world):
    def on_create_pr(remote):
        remote._commit_change("collegue/issue-21", "intrus.txt", "poussé par un tiers\n", "tiers")

    world.remote.on_create_pr = on_create_pr
    outcome = await deliver(world, Writes({"feature.py": "F = 1\n"}))
    assert not outcome.success and outcome.proof is None
    assert "tête" in outcome.error
    assert world.manager.get_decision_journal(world.project_id, "delivery-proof") == []


async def test_a_lost_remote_write_is_detected_by_the_tree_comparison(world):
    world.remote.lost_writes.add("feature.py")  # le transport « réussit » mais le fichier n'arrive pas
    outcome = await deliver(world, Writes({"feature.py": "F = 1\n", "other.py": "O = 1\n"}))
    assert not outcome.success and "arbre publié" in outcome.error
    assert world.manager.get_decision_journal(world.project_id, "delivery-proof") == []


async def test_an_existing_pr_with_another_revision_is_never_presented_as_the_delivery(world):
    clients = world.remote.clients()
    clients.branches.ensure_branch(OWNER, REPO, "collegue/issue-21", from_branch="main")
    clients.files.update_file(OWNER, REPO, "intrus.py", "m", "pas la livraison\n", branch="collegue/issue-21")
    clients.prs.create_pr(OWNER, REPO, "ancienne", "collegue/issue-21", "main", "ancienne PR")
    outcome = await deliver(world, Writes({"feature.py": "F = 1\n"}))
    assert not outcome.success and outcome.proof is None
    assert "arbre publié" in outcome.error
    assert world.manager.get_decision_journal(world.project_id, "delivery-proof") == []


async def test_a_pr_that_hides_its_head_is_refused(world):
    clients = world.remote.clients()
    clients.branches.ensure_branch(OWNER, REPO, "collegue/issue-21", from_branch="main")
    clients.prs.create_pr(OWNER, REPO, "ancienne", "collegue/issue-21", "main", "x")
    world.remote.hide_pr_head_sha = True
    outcome = await deliver(world, Writes({"feature.py": "F = 1\n"}))
    assert not outcome.success and outcome.proof is None
    assert "tête" in outcome.error


# --- dry-run : aperçu fidèle, aucune écriture ---------------------------------------------------------------------------


async def test_dry_run_previews_without_remote_writes_or_proof(world):
    outcome = await execute_issue(
        ISSUE,
        world.source,
        None,
        dry_run=True,
        agent=Writes({"feature.py": "F = 1\n"}),
        owner=OWNER,
        repo=REPO,
        sandbox=GreenSandbox(),
        reviewer=FakeReviewer(),
        clients=world.remote.clients(),
    )
    assert outcome.success and outcome.pr.dry_run is True and outcome.proof is None
    assert world.remote.calls == []


# --- contrats scellés : BUILD de bout en bout -----------------------------------------------------------------------------

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
    "def test_other_value():\n"
    "    other = Path.cwd() / 'other.py'\n"
    "    assert other.is_file(), 'other.py est requis par le critère'\n"
    "    namespace = {}\n"
    "    exec(other.read_text(), namespace)\n"
    "    assert namespace['OTHER'] == 7\n"
)
SETTINGS = type(
    "S",
    (),
    dict(LLM_PROVIDER="gemini", LLM_MODEL="default", LLM_PROVIDER_QA="openai", LLM_MODEL_QA="qa", LLM_CALL_TIMEOUT=0),
)()


async def sealed(world, oracles):
    spec = "# SPEC\nproduit de test\n"
    manager = world.manager
    pid = manager.create_project(name="contrats", spec=spec)
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
    world.project_id = pid
    return ids


def task_issue(index, task_id):
    return IssueSpec(
        number=100 + index,
        title=f"Tâche {index}",
        acceptance_criteria=(f"Critère {index}",),
        source_task_id=task_id,
    )


def contract_gate(world):
    return {"acceptance_checker": StoredAcceptanceChecker(manager=world.manager, project_id=world.project_id)}


def commit_to_source(world, files):
    for rel, text in files.items():
        (Path(world.source) / rel).write_text(text)
    git(world.source, "add", "-A")
    git(world.source, "-c", "user.name=f", "-c", "user.email=f@e.invalid", "commit", "-q", "-m", "livré")
    fresh = world.tmp / f"remote-{len(list(world.tmp.glob('remote-*')))}"
    fresh.mkdir()
    world.remote = FakeRemote(fresh, world.source)


async def test_first_task_delivery_carries_the_negative_proof_of_its_sealed_oracle(world):
    ids = await sealed(world, [FEATURE_ORACLE])
    sandbox = LocalOracleSandbox(fallback=GreenSandbox())
    outcome = await deliver(
        world,
        Writes({"feature.py": "VALUE = 42\n"}),
        sandbox=sandbox,
        issue=task_issue(1, ids[0]),
        gate_options=contract_gate(world),
    )
    assert outcome.success, outcome.error
    proof = outcome.proof
    assert proof.contracts_required is True and proof.passed is True
    (oracle,) = proof.oracles
    assert oracle.role == "current" and oracle.expected_preimage == "red-assertion" and oracle.passed
    assert (
        oracle.preimage.status == "red-assertion" and oracle.candidate.status == "green"
    )  # même SHA-256 des deux côtés
    assert oracle.source_sha256 == world.manager.get_task(ids[0]).acceptance_test_sha256
    assert proof.verdict("contracts").passed is True


async def test_an_oracle_that_cannot_fail_on_the_preimage_blocks_the_delivery(world):
    tautology_free_but_import_style = (
        "def test_feature_value():\n    from feature import VALUE\n    assert VALUE == 42\n"
    )
    ids = await sealed(world, [tautology_free_but_import_style])
    outcome = await deliver(
        world,
        Writes({"feature.py": "VALUE = 42\n"}),
        sandbox=LocalOracleSandbox(fallback=GreenSandbox()),
        issue=task_issue(1, ids[0]),
        gate_options=contract_gate(world),
    )
    assert not outcome.success and outcome.stage == "gate" and outcome.proof is None
    assert "préimage" in (outcome.quality_report.acceptance_error or "")
    assert world.remote.calls == []


async def test_the_next_task_cannot_break_a_delivered_contract(world):
    ids = await sealed(world, [FEATURE_ORACLE, OTHER_ORACLE])
    world.manager.update_task_status(ids[0], "merged")
    commit_to_source(world, {"feature.py": "VALUE = 42\n"})  # tâche 1 livrée : elle fait partie de la base
    sandbox = LocalOracleSandbox(fallback=GreenSandbox())

    breaking = await deliver(
        world,
        Writes({"other.py": "OTHER = 7\n", "feature.py": "VALUE = 0\n"}),  # tâche 2 casse le contrat de la tâche 1
        sandbox=sandbox,
        issue=task_issue(2, ids[1]),
        gate_options=contract_gate(world),
    )
    assert not breaking.success and breaking.proof is None
    assert f"tâche {ids[0]}" in breaking.quality_report.acceptance_error
    assert "delivered" in breaking.quality_report.acceptance_error

    good = await deliver(
        world,
        Writes({"other.py": "OTHER = 7\n"}),
        sandbox=sandbox,
        issue=task_issue(2, ids[1]),
        gate_options=contract_gate(world),
    )
    assert good.success, good.error
    assert {(o.task_id, o.role) for o in good.proof.oracles} == {(ids[1], "current"), (ids[0], "delivered")}
    assert good.proof.passed and good.proof.verdict("contracts").passed
    loaded = load_delivery_proof(
        world.manager, world.project_id, owner=OWNER, repo=REPO, pr_number=good.pr.number, head_sha=good.pr.head_sha
    )
    assert loaded.oracles == good.proof.oracles


async def test_workspace_tests_cannot_stand_in_for_the_sealed_contract(world):
    ids = await sealed(world, [FEATURE_ORACLE])
    cheating = Writes(
        {
            "feature.py": "VALUE = 0\n",
            f"task-{ids[0]}.py": "def test_feature_value():\n    assert True\n",
            "tests/test_feature.py": "def test_feature_value():\n    assert True\n",
            "conftest.py": "def pytest_collection_modifyitems(items):\n    items.clear()\n",
        }
    )
    outcome = await deliver(
        world,
        cheating,
        sandbox=LocalOracleSandbox(fallback=GreenSandbox()),
        issue=task_issue(1, ids[0]),
        gate_options=contract_gate(world),
    )
    assert not outcome.success and outcome.proof is None
    assert world.remote.calls == []


# --- l'ÉTAT durable exige des contrats : aucun défaut de câblage ne produit une preuve sans eux ------------------------


async def test_a_project_that_requires_contracts_gets_no_proof_when_the_checker_is_not_wired(world):
    """GATE_ACCEPTANCE_TESTS=false (ou checker oublié) face à un projet qui exige des oracles : refus, pas dispense."""
    ids = await sealed(world, [FEATURE_ORACLE])
    assert world.manager.get_project(world.project_id).acceptance_tests_required is True
    outcome = await deliver(
        world,
        Writes({"feature.py": "VALUE = 42\n"}),
        sandbox=LocalOracleSandbox(fallback=GreenSandbox()),
        issue=task_issue(1, ids[0]),
        gate_options=None,  # aucun acceptance_checker câblé
    )
    assert not outcome.success and outcome.stage == "gate" and outcome.proof is None
    assert outcome.quality_report.passed is True  # le gate seul aurait laissé passer : c'est l'état qui refuse
    assert "contracts" in outcome.error and "checker non câblé" in outcome.error
    assert world.remote.calls == []
    assert world.manager.get_decision_journal(world.project_id, "delivery-proof") == []


async def test_a_checker_that_skips_the_delivered_contracts_cannot_yield_a_proof(world):
    ids = await sealed(world, [FEATURE_ORACLE, OTHER_ORACLE])
    world.manager.update_task_status(ids[0], "merged")
    commit_to_source(world, {"feature.py": "VALUE = 42\n"})
    partial = {
        "acceptance_checker": StoredAcceptanceChecker(
            manager=world.manager, project_id=world.project_id, include_delivered=False
        )
    }
    outcome = await deliver(
        world,
        Writes({"other.py": "OTHER = 7\n"}),
        sandbox=LocalOracleSandbox(fallback=GreenSandbox()),
        issue=task_issue(2, ids[1]),
        gate_options=partial,
    )
    assert not outcome.success and outcome.proof is None
    assert f"{[ids[0]]}" in outcome.error and "non rejoués" in outcome.error
    assert world.remote.calls == []
    # témoin : le même appel avec le checker complet aboutit
    good = await deliver(
        world,
        Writes({"other.py": "OTHER = 7\n"}),
        sandbox=LocalOracleSandbox(fallback=GreenSandbox()),
        issue=task_issue(2, ids[1]),
        gate_options=contract_gate(world),
    )
    assert good.success, good.error


# --- panne du distant : retentable, jamais présentée comme un défaut du code ----------------------------------------------


async def test_an_unreadable_remote_is_a_retryable_infrastructure_failure_not_a_gate_failure(world):
    clients = world.remote.clients()

    def down(*args, **kwargs):
        raise ConnectionError("GitHub 502 simulé")

    clients.branches.get_branch_sha = down
    outcome = await execute_issue(
        ISSUE,
        world.source,
        None,
        dry_run=False,
        agent=Writes({"feature.py": "F = 1\n"}),
        owner=OWNER,
        repo=REPO,
        sandbox=GreenSandbox(),
        reviewer=FakeReviewer(),
        clients=clients,
        manager=world.manager,
        project_id=world.project_id,
    )
    assert not outcome.success and outcome.reason == "engine_error" and outcome.stage == "pr"
    assert "illisible" in outcome.error and "502" in outcome.error
    assert outcome.proof is None and world.remote.calls == []  # rien n'a été écrit
    # témoin : le même appel avec un distant sain aboutit
    assert (await deliver(world, Writes({"feature.py": "F = 1\n"}))).success


async def test_a_publication_refusal_carries_a_single_refusal_prefix(world):
    world.remote.advance_base()
    outcome = await deliver(world, Writes({"feature.py": "F = 1\n"}))
    assert not outcome.success and outcome.reason == "gate_failed"
    assert outcome.error.count("LIVRAISON REFUSÉE") == 1
