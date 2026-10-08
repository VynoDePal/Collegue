"""Preuve de livraison (vague 3) : contenu scellé, vérification du distant, persistance et relecture.

Opérations Git locales, SQLite et assertions métier RÉELLES ; seul le transport GitHub est factice, et il est
complet (``tests/github_fakes.py`` : un vrai dépôt Git distant). Un refus dû à un mock incomplet ne compte jamais.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest
from github_fakes import FakeRemote, git, make_source_repo

from collegue.executor.agent import IssueSpec
from collegue.executor.delivery_proof import (
    MANDATORY_VERDICTS,
    PHASE_BUILD,
    PHASE_IMPROVE,
    DeliveryDriftError,
    DeliveryProofError,
    OracleEvidence,
    OracleRun,
    ProofDraft,
    Verdict,
    compute_proof_id,
    derive_passed,
    git_blob_sha1,
    load_delivery_proof,
    persist_delivery_proof,
    seal_proof,
    seal_tested_content,
    tree_sha_from_entries,
    verify_remote_base,
    verify_remote_head,
    verify_tested_content,
)
from collegue.executor.pr import DeliveryRefusedError, assert_representable
from collegue.executor.workspace import prepare_workspace
from collegue.state import ProjectStateManager

ISSUE = IssueSpec(number=3, title="Preuve")
OWNER, REPO = "o", "r"


# --- banc : dépôt source réel, workspace géré, distant fidèle ------------------------------------------


@pytest.fixture
def bench(tmp_path):
    source = make_source_repo(
        tmp_path / "source",
        {"README.md": "# base\n", "pkg/mod.py": "VALUE = 1\n", "run.sh": "#!/bin/sh\n"},
        gitignore="build/\n*.cache\n",
    )
    os.chmod(Path(source) / "run.sh", 0o755)
    git(source, "add", "-A")
    git(source, "-c", "user.name=f", "-c", "user.email=f@e.invalid", "commit", "-q", "-m", "mode", "--allow-empty")
    workspace = prepare_workspace(source, ISSUE, dest_root=str(tmp_path / "ws"))
    remote = FakeRemote(tmp_path, source)
    return type("Bench", (), {"source": source, "ws": workspace, "remote": remote, "tmp": tmp_path})()


def _draft(content, phase=PHASE_BUILD, *, delivered=("pkg/mod.py",)):
    draft = ProofDraft(phase=phase, content=content, delivered_paths=tuple(delivered))
    for name in MANDATORY_VERDICTS[phase]:
        draft.add(name, True, "ok")
    return draft


# --- hachage Git pur, vérifié contre git ----------------------------------------------------------------


def test_blob_and_tree_hashing_match_real_git(tmp_path):
    repo = tmp_path / "plain"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    files = {
        "a.txt": (b"alpha\n", "100644"),
        "dir/b.py": (b"print('b')\n", "100644"),
        "dir/sub/run.sh": (b"#!/bin/sh\n", "100755"),
        "dir-x/c.txt": (b"c\n", "100644"),  # tri Git : « dir/ » avant « dir-x/ »
        "dir.txt": (b"dot\n", "100644"),
        "é.txt": ("accentué\n".encode(), "100644"),
    }
    entries = {}
    for rel, (data, mode) in files.items():
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        os.chmod(target, 0o755 if mode == "100755" else 0o644)
        assert git_blob_sha1(data) == git(repo, "hash-object", rel)
        entries[rel] = (mode, git_blob_sha1(data))
    git(repo, "add", "-A")
    assert tree_sha_from_entries(entries) == git(repo, "write-tree")


def test_tree_hashing_refuses_file_directory_conflicts():
    with pytest.raises(DeliveryProofError, match="conflit"):
        tree_sha_from_entries({"a": ("100644", "0" * 40), "a/b": ("100644", "1" * 40)})


# --- scellement du contenu testé --------------------------------------------------------------------------


def _independent_tree(directory: Path) -> str:
    """Arbre calculé par un dépôt Git SANS lien avec le workspace géré (index jetable, hors du code testé)."""
    scratch = directory.parent / f"scratch-{directory.name}"
    scratch.mkdir()
    git(scratch, "init", "-q", "-b", "main")
    env = {"GIT_DIR": str(scratch / ".git"), "GIT_WORK_TREE": str(directory)}
    git(directory, "add", "-A", env=env)
    return git(directory, "write-tree", env=env)


def test_sealed_tree_is_the_whole_tree_with_modes_deletions_and_new_files(bench):
    ws = Path(bench.ws.path)
    (ws / "pkg" / "mod.py").write_text("VALUE = 2\n")
    (ws / "README.md").unlink()
    (ws / "new.py").write_text("NEW = True\n")
    os.chmod(ws / "new.py", 0o755)

    content = seal_tested_content(bench.ws.path)

    assert content.tree_sha == _independent_tree(ws)
    assert content.base_tree_sha != content.tree_sha and content.files_count == 4
    listing = subprocess.run(
        ["git", "ls-tree", "-r", content.tree_sha],
        cwd=bench.ws.path + ".control",
        env={"GIT_DIR": bench.ws.path + ".control", "PATH": os.environ["PATH"]},
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "100755 blob" in listing and "new.py" in listing and "README.md" not in listing
    assert "100755" in next(line for line in listing.splitlines() if line.endswith("run.sh"))  # base conservée


def test_ignored_and_residual_inputs_are_removed_before_the_checks(bench):
    ws = Path(bench.ws.path)
    (ws / "build").mkdir()
    (ws / "build" / "gen.py").write_text("GENERATED = 1\n")
    (ws / "x.cache").write_text("cache")
    (ws / "feature.py").write_text("FEATURE = 1\n")

    content = seal_tested_content(bench.ws.path)

    assert not (ws / "build").exists() and not (ws / "x.cache").exists()  # fichiers ignorés retirés du workspace
    assert (ws / "feature.py").exists()
    assert content.purged_count >= 2 and any("build" in name for name in content.ignored_inputs_removed)
    assert content.tree_sha == _independent_tree(ws)


def test_sealing_is_deterministic_and_content_sha_follows_the_content(bench):
    first = seal_tested_content(bench.ws.path)
    again = seal_tested_content(bench.ws.path)
    assert first == again
    (Path(bench.ws.path) / "pkg" / "mod.py").write_text("VALUE = 3\n")
    changed = seal_tested_content(bench.ws.path)
    assert changed.tree_sha != first.tree_sha and changed.content_sha256 != first.content_sha256
    assert changed.base_tree_sha == first.base_tree_sha and changed.base_sha == first.base_sha


def test_unmanaged_workspace_is_refused(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    git(plain, "init", "-q", "-b", "main")
    with pytest.raises(DeliveryProofError, match="non géré"):
        seal_tested_content(str(plain))


# --- dérive après le début des contrôles ---------------------------------------------------------------------


def test_verify_accepts_an_unchanged_tree_and_untracked_gate_outputs(bench):
    (Path(bench.ws.path) / "feature.py").write_text("F = 1\n")
    content = seal_tested_content(bench.ws.path)
    (Path(bench.ws.path) / "node_modules").mkdir()
    (Path(bench.ws.path) / "node_modules" / "artefact.js").write_text("// sortie du gate\n")
    verify_tested_content(bench.ws.path, content)  # sorties régénérables hors arbre : sans effet


def test_verify_detects_a_base_file_modified_while_testing(bench):
    """Un fichier de BASE (hors diff) modifié pendant le gate invalide la preuve — pas seulement le diff."""
    (Path(bench.ws.path) / "feature.py").write_text("F = 1\n")
    content = seal_tested_content(bench.ws.path)
    (Path(bench.ws.path) / "pkg" / "mod.py").write_text("VALUE = 'changé pendant les tests'\n")
    with pytest.raises(DeliveryDriftError, match=r"pkg/mod\.py"):
        verify_tested_content(bench.ws.path, content)


def test_verify_detects_content_mode_and_deletion_drift(bench):
    ws = Path(bench.ws.path)
    (ws / "feature.py").write_text("F = 1\n")
    content = seal_tested_content(bench.ws.path)
    os.chmod(ws / "run.sh", 0o644)  # mode perdu
    with pytest.raises(DeliveryDriftError, match=r"run\.sh"):
        verify_tested_content(bench.ws.path, content)
    os.chmod(ws / "run.sh", 0o755)
    verify_tested_content(bench.ws.path, content)
    (ws / "README.md").unlink()  # suppression pendant le gate
    with pytest.raises(DeliveryDriftError, match=r"README\.md"):
        verify_tested_content(bench.ws.path, content)


def test_verify_allows_only_the_named_paths(bench):
    ws = Path(bench.ws.path)
    (ws / "requirements.txt").write_text("fastapi\n")
    content = seal_tested_content(bench.ws.path)
    (ws / "requirements.txt").write_text("fastapi\nhttpx\n")
    with pytest.raises(DeliveryDriftError):
        verify_tested_content(bench.ws.path, content)
    verify_tested_content(bench.ws.path, content, allowed_paths=("requirements.txt",))
    (ws / "pkg" / "mod.py").write_text("VALUE = 9\n")
    with pytest.raises(DeliveryDriftError, match=r"pkg/mod\.py"):
        verify_tested_content(bench.ws.path, content, allowed_paths=("requirements.txt",))


def test_resealing_only_the_named_path_keeps_gate_outputs_out_of_the_tree(bench):
    ws = Path(bench.ws.path)
    (ws / "requirements.txt").write_text("fastapi\n")
    first = seal_tested_content(bench.ws.path)
    (ws / "node_modules").mkdir()
    (ws / "node_modules" / "artefact.js").write_text("// sortie du gate\n")
    (ws / "requirements.txt").write_text("fastapi\nhttpx\n")

    second = seal_tested_content(bench.ws.path, only_paths=("requirements.txt",))

    assert second.tree_sha != first.tree_sha
    assert not (ws / "node_modules").exists()  # purgé : les contrôles suivants ne l'utilisent pas
    assert second.tree_sha == _independent_tree(ws)


# --- verdicts et obligations ------------------------------------------------------------------------------


def test_passed_is_derived_never_declared(bench):
    content = seal_tested_content(bench.ws.path)
    draft = _draft(content)
    assert draft.passed is True
    for name in MANDATORY_VERDICTS[PHASE_BUILD]:
        incomplete = ProofDraft(phase=PHASE_BUILD, content=content)
        for other in MANDATORY_VERDICTS[PHASE_BUILD]:
            if other != name:
                incomplete.add(other, True)
        assert incomplete.passed is False, f"verdict obligatoire absent : {name}"


def test_a_failed_required_verdict_cannot_be_overwritten_by_a_later_success(bench):
    draft = _draft(seal_tested_content(bench.ws.path))
    draft.add("tests", False, "rouge")
    draft.add("tests", True, "vert tardif")
    assert draft.passed is False
    assert next(v for v in draft.verdicts if v.name == "tests").passed is False


def test_optional_verdicts_never_block_but_required_ones_do():
    verdicts = (
        Verdict("content_integrity", True, True),
        Verdict("tests", True, True),
        Verdict("review", True, True),
        Verdict("adequacy", False, False, "facultatif"),
    )
    assert derive_passed(verdicts, (), PHASE_BUILD, False) is True
    assert derive_passed(verdicts + (Verdict("extra", True, False),), (), PHASE_BUILD, False) is False


def test_improve_phase_demands_coverage_and_secret_scan_verdicts():
    base = (Verdict("content_integrity", True, True), Verdict("tests", True, True), Verdict("review", True, True))
    assert derive_passed(base, (), PHASE_IMPROVE, False) is False
    assert derive_passed(
        base + (Verdict("coverage", True, True), Verdict("secret_scan", True, True)), (), PHASE_IMPROVE, False
    )


def _oracle(task_id=1, *, passed=True):
    run = OracleRun(phase="candidate", status="green", executed=1, passed=1)
    return OracleEvidence(
        task_id=task_id,
        role="delivered",
        source_sha256="a" * 64,
        contract_sha256="b" * 64,
        provenance_sha256="c" * 64,
        expected_preimage="not-required",
        preimage=None,
        candidate=run,
        passed=passed,
    )


def test_required_contracts_need_oracles_and_a_contracts_verdict(bench):
    content = seal_tested_content(bench.ws.path)
    draft = _draft(content)
    draft.contracts_required = True
    assert draft.passed is False  # aucune preuve d'oracle, aucun verdict « contracts »
    draft.oracles.append(_oracle())
    assert draft.passed is False  # verdict « contracts » toujours absent
    draft.add("contracts", True, "ok")
    assert draft.passed is True
    draft.oracles.append(_oracle(2, passed=False))
    assert draft.passed is False  # un seul oracle refusé suffit


# --- liaison à la tête distante, persistance, relecture ---------------------------------------------------


def _sealed(bench, *, pr=7, project_id=1, phase=PHASE_BUILD, head=None):
    ws = Path(bench.ws.path)
    (ws / "pkg" / "mod.py").write_text("VALUE = 5\n")
    content = seal_tested_content(bench.ws.path)
    draft = _draft(content, phase)
    base = bench.remote.branch_sha("main")
    return seal_proof(
        draft,
        owner=OWNER,
        repo=REPO,
        project_id=project_id,
        pr_number=pr,
        head_sha=head or "1" * 40,
        base_sha=base,
    )


@pytest.fixture
def state_url(tmp_path):
    return f"sqlite:///{tmp_path / 'state.db'}"


def test_proof_id_is_the_hash_of_the_canonical_content_and_changes_with_it(bench):
    proof = _sealed(bench)
    assert proof.proof_id == compute_proof_id(proof)
    assert compute_proof_id(replace(proof, tree_sha="2" * 40)) != proof.proof_id
    assert compute_proof_id(replace(proof, passed=not proof.passed)) != proof.proof_id


def test_persisted_proof_is_reloaded_by_a_new_manager_with_exact_identities(bench, state_url):
    manager = ProjectStateManager.from_url(state_url, create=True)
    project_id = manager.create_project(name="demo")
    proof = _sealed(bench, project_id=project_id)
    persist_delivery_proof(manager, proof)

    fresh = ProjectStateManager.from_url(state_url, create=False)  # NOUVELLE instance : rien en mémoire
    loaded = load_delivery_proof(fresh, project_id, owner=OWNER, repo=REPO, pr_number=7, head_sha=proof.head_sha)

    assert loaded == proof
    assert (loaded.owner, loaded.repo, loaded.project_id, loaded.pr_number) == (OWNER, REPO, project_id, 7)
    assert (loaded.head_sha, loaded.base_sha, loaded.tree_sha) == (proof.head_sha, proof.base_sha, proof.tree_sha)
    assert loaded.passed is True and loaded.phase == PHASE_BUILD
    assert isinstance(loaded.project_id, int) and isinstance(loaded.pr_number, int) and loaded.passed is True
    assert {v.name for v in loaded.verdicts} == set(MANDATORY_VERDICTS[PHASE_BUILD])
    with pytest.raises(Exception):  # dataclass figée : attributs en lecture seule
        loaded.passed = False


def test_persisting_the_same_proof_twice_is_idempotent_but_a_conflicting_one_is_refused(bench, state_url):
    manager = ProjectStateManager.from_url(state_url, create=True)
    project_id = manager.create_project(name="demo")
    proof = _sealed(bench, project_id=project_id)
    persist_delivery_proof(manager, proof)
    persist_delivery_proof(manager, proof)
    assert (
        load_delivery_proof(manager, project_id, owner=OWNER, repo=REPO, pr_number=7, head_sha=proof.head_sha).proof_id
        == proof.proof_id
    )

    other = replace(proof, passed=False, verdicts=proof.verdicts[:-1])
    other = replace(other, proof_id=compute_proof_id(other))
    manager.record_decision(
        project_id,
        f"delivery-proof:v1:{OWNER}/{REPO}#7@{proof.head_sha}:{other.proof_id}",
        json.dumps({"x": 1}),
    )
    with pytest.raises(DeliveryProofError):
        load_delivery_proof(manager, project_id, owner=OWNER, repo=REPO, pr_number=7, head_sha=proof.head_sha)


@pytest.mark.parametrize(
    "change",
    [
        {"owner": "autre"},
        {"repo": "autre"},
        {"pr_number": 8},
        {"head_sha": "2" * 40},
        {"project_id": 99},
    ],
    ids=["owner", "repo", "pr", "head", "project"],
)
def test_proof_of_another_identity_is_never_returned(bench, state_url, change):
    manager = ProjectStateManager.from_url(state_url, create=True)
    project_id = manager.create_project(name="demo")
    proof = _sealed(bench, project_id=project_id)
    persist_delivery_proof(manager, proof)
    query = dict(project_id=project_id, owner=OWNER, repo=REPO, pr_number=7, head_sha=proof.head_sha)
    query.update(change)
    pid = query.pop("project_id")
    with pytest.raises(DeliveryProofError, match="aucune preuve|illisible|invalide"):
        load_delivery_proof(manager, pid, **query)


def test_unknown_or_missing_proof_is_a_refusal_not_a_reconstruction(bench, state_url):
    manager = ProjectStateManager.from_url(state_url, create=True)
    project_id = manager.create_project(name="demo")
    # le texte d'une PR (ou une décision libre) ne fait jamais foi
    manager.record_decision(project_id, "PR #7 ouverte pour l'issue #3", "passed=true tree=" + "a" * 40)
    with pytest.raises(DeliveryProofError, match="revalidation"):
        load_delivery_proof(manager, project_id, owner=OWNER, repo=REPO, pr_number=7, head_sha="1" * 40)
    with pytest.raises(DeliveryProofError):
        load_delivery_proof(None, project_id, owner=OWNER, repo=REPO, pr_number=7, head_sha="1" * 40)
    with pytest.raises(DeliveryProofError):
        load_delivery_proof(manager, project_id, owner=OWNER, repo=REPO, pr_number=True, head_sha="1" * 40)
    with pytest.raises(DeliveryProofError):
        load_delivery_proof(manager, project_id, owner=OWNER, repo=REPO, pr_number=7, head_sha="pas-un-sha")


def _tampered(proof, **changes):
    record = json.loads(_rationale(proof))
    record.update(changes)
    return record


def _rationale(proof):
    from collegue.executor.delivery_proof import _canonical, _record_without_id

    record = _record_without_id(proof)
    record["proof_id"] = proof.proof_id
    return _canonical(record)


@pytest.mark.parametrize(
    ("label", "mutate"),
    [
        ("passed forgé", lambda r: r.update(passed=True, verdicts=[v for v in r["verdicts"] if v["name"] != "review"])),
        ("tree altéré", lambda r: r.update(tree_sha="3" * 40)),
        ("verdict inversé", lambda r: r["verdicts"][1].update(passed=False)),
        ("champ manquant", lambda r: r.pop("base_tree_sha")),
        ("schéma inconnu", lambda r: r.update(schema="collegue.delivery-proof/99")),
        ("passed non booléen", lambda r: r.update(passed="true")),
        ("project_id textuel", lambda r: r.update(project_id="1")),
        ("JSON tronqué", None),
    ],
)
def test_tampered_or_incomplete_records_are_refused(bench, state_url, label, mutate):
    manager = ProjectStateManager.from_url(state_url, create=True)
    project_id = manager.create_project(name="demo")
    proof = _sealed(bench, project_id=project_id)
    summary = f"delivery-proof:v1:{OWNER}/{REPO}#7@{proof.head_sha}:{proof.proof_id}"
    if mutate is None:
        rationale = _rationale(proof)[:-20]
    else:
        record = json.loads(_rationale(proof))
        mutate(record)
        rationale = json.dumps(record)
    manager.record_decision(project_id, summary, rationale)
    with pytest.raises(DeliveryProofError):
        load_delivery_proof(manager, project_id, owner=OWNER, repo=REPO, pr_number=7, head_sha=proof.head_sha)


def test_a_proof_missing_a_mandatory_obligation_cannot_be_forged_into_passed(bench):
    content = seal_tested_content(bench.ws.path)
    draft = ProofDraft(phase=PHASE_IMPROVE, content=content)
    for name in MANDATORY_VERDICTS[PHASE_BUILD]:  # IMPROVE sans « coverage » ni « secret_scan »
        draft.add(name, True)
    proof = seal_proof(draft, owner=OWNER, repo=REPO, project_id=1, pr_number=1, head_sha="1" * 40, base_sha="2" * 40)
    assert proof.passed is False


# --- vérification du distant ---------------------------------------------------------------------------------


def _publish(bench, branch, files: dict, deletions=()):
    clients = bench.remote.clients()
    clients.branches.ensure_branch(OWNER, REPO, branch, from_branch="main")
    for path, text in files.items():
        clients.files.update_file(OWNER, REPO, path, "m", text, branch=branch)
    for path in deletions:
        clients.files.delete_file(OWNER, REPO, path, "m", branch=branch)
    return clients, bench.remote.branch_sha(branch)


def test_remote_head_is_accepted_when_the_published_tree_is_the_tested_tree(bench):
    ws = Path(bench.ws.path)
    (ws / "pkg" / "mod.py").write_text("VALUE = 5\n")
    (ws / "README.md").unlink()
    content = seal_tested_content(bench.ws.path)
    clients, head = _publish(bench, "feat", {"pkg/mod.py": "VALUE = 5\n"}, deletions=("README.md",))

    remote_base = verify_remote_base(clients.branches, OWNER, REPO, "main", content)
    # ... sauf le mode : la Contents API écrit tout en 100644, run.sh (100755 en base) est préservé (inchangé)
    assert (
        verify_remote_head(
            clients.branches, OWNER, REPO, head_sha=head, content=content, remote_base_sha=remote_base.sha
        )
        == 2
    )


def test_remote_head_refuses_an_omitted_file(bench):
    ws = Path(bench.ws.path)
    (ws / "pkg" / "mod.py").write_text("VALUE = 5\n")
    (ws / "extra.py").write_text("EXTRA = 1\n")
    content = seal_tested_content(bench.ws.path)
    clients, head = _publish(bench, "feat", {"pkg/mod.py": "VALUE = 5\n"})  # extra.py jamais poussé
    base = verify_remote_base(clients.branches, OWNER, REPO, "main", content)
    with pytest.raises(DeliveryDriftError, match="arbre publié"):
        verify_remote_head(clients.branches, OWNER, REPO, head_sha=head, content=content, remote_base_sha=base.sha)


def test_remote_head_refuses_an_unexpected_deletion_and_a_wrong_payload(bench):
    ws = Path(bench.ws.path)
    (ws / "pkg" / "mod.py").write_text("VALUE = 5\n")
    content = seal_tested_content(bench.ws.path)
    clients, _ = _publish(bench, "wrong-payload", {"pkg/mod.py": "VALUE = 6\n"})
    base = verify_remote_base(clients.branches, OWNER, REPO, "main", content)
    for branch, files, deletions in (
        ("wrong-payload", None, ()),
        ("deleted-too-much", {"pkg/mod.py": "VALUE = 5\n"}, ("README.md",)),
    ):
        if files is not None:
            clients, _ = _publish(bench, branch, files, deletions)
        head = bench.remote.branch_sha(branch)
        with pytest.raises(DeliveryDriftError, match="arbre publié"):
            verify_remote_head(clients.branches, OWNER, REPO, head_sha=head, content=content, remote_base_sha=base.sha)


def test_a_new_executable_script_loses_its_mode_on_publication_and_is_caught_before_and_after(bench):
    """La Contents API écrit un fichier neuf en 100644 : le bit exécutable se perdrait en silence."""
    ws = Path(bench.ws.path)
    (ws / "deploy.sh").write_text("#!/bin/sh\necho ok\n")
    os.chmod(ws / "deploy.sh", 0o755)
    content = seal_tested_content(bench.ws.path)
    assert content.special_modes == ("deploy.sh (100755)",)
    with pytest.raises(DeliveryRefusedError, match=r"deploy\.sh \(100755\)"):
        assert_representable(content)  # refus AVANT toute écriture distante
    # défense en profondeur : même publié, l'arbre distant diffère de l'arbre testé
    clients, head = _publish(bench, "feat", {"deploy.sh": "#!/bin/sh\necho ok\n"})
    base = verify_remote_base(clients.branches, OWNER, REPO, "main", content)
    with pytest.raises(DeliveryDriftError, match="arbre publié"):
        verify_remote_head(clients.branches, OWNER, REPO, head_sha=head, content=content, remote_base_sha=base.sha)


def test_existing_executable_files_untouched_or_edited_keep_their_mode_and_are_representable(bench):
    ws = Path(bench.ws.path)
    (ws / "run.sh").write_text("#!/bin/sh\necho modifié\n")  # fichier de base exécutable, contenu modifié
    content = seal_tested_content(bench.ws.path)
    assert content.special_modes == ()  # base inchangée côté mode : rien de nouveau à représenter
    assert_representable(content)
    clients, head = _publish(bench, "feat", {"run.sh": "#!/bin/sh\necho modifié\n"})
    base = verify_remote_base(clients.branches, OWNER, REPO, "main", content)
    assert (
        verify_remote_head(clients.branches, OWNER, REPO, head_sha=head, content=content, remote_base_sha=base.sha) == 1
    )


def test_remote_base_moved_since_the_checks_is_refused(bench):
    content = seal_tested_content(bench.ws.path)
    bench.remote.advance_base()
    with pytest.raises(DeliveryDriftError, match="base distante"):
        verify_remote_base(bench.remote.clients().branches, OWNER, REPO, "main", content)


def test_remote_head_requires_a_linear_chain_to_the_verified_base(bench):
    ws = Path(bench.ws.path)
    (ws / "pkg" / "mod.py").write_text("VALUE = 5\n")
    content = seal_tested_content(bench.ws.path)
    clients, head = _publish(bench, "feat", {"pkg/mod.py": "VALUE = 5\n"})
    other_base = "9" * 40
    with pytest.raises(DeliveryProofError):  # la chaîne ne rejoint pas cette base (parent illisible ou racine)
        verify_remote_head(clients.branches, OWNER, REPO, head_sha=head, content=content, remote_base_sha=other_base)


def test_unreadable_remote_is_a_refusal(bench):
    content = seal_tested_content(bench.ws.path)

    class _Down:
        def get_branch_sha(self, *args):
            raise ConnectionError("502")

        def get_git_commit(self, *args):
            raise ConnectionError("502")

    with pytest.raises(DeliveryProofError, match="illisible"):
        verify_remote_base(_Down(), OWNER, REPO, "main", content)
    with pytest.raises(DeliveryProofError, match="illisible"):
        verify_remote_head(_Down(), OWNER, REPO, head_sha="1" * 40, content=content, remote_base_sha="2" * 40)
