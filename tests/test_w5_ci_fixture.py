"""Socle, protection et contre-épreuves du dépôt fixture W5 (propriété C) : tout est vérifiable HORS LIGNE.

* le commit du socle calculé par le script est IDENTIQUE à celui de ``git`` réel (même arbre, même SHA) et l'arbre de la graine
  recalculé est celui du dépôt distant (``c8bffa32…``) ;
* le workflow (``pull_request`` + ``push`` : jamais ``pull_request_target``, inutilisable avec une branche par défaut immuable sans workflow),
  le CODEOWNERS, le verrou approuvé, le ruleset et le manifeste ont le contenu exigé (statique) ;
* ``apply`` / ``cleanup`` / ``probe`` sont éprouvés sur un faux serveur GitHub EN MÉMOIRE : jeton d'ordre, idempotence, collisions,
  ressources non possédées, ``main`` et PR étrangères intacts, nettoyage restreint aux ressources de la campagne ;
* l'évaluation des checks et le contrôle d'intégrité (fonctions pures utilisables par B) refusent faux succès, autre application,
  autre tête, check manquant et workflow altéré.

Rien de tout cela ne prouve le comportement de GitHub (déclenchement, code owner à 0 approbation, règle de création) : la contre-épreuve réelle
(``probe``) n'est exécutable que sur ordre du manager.
"""

from __future__ import annotations

import base64
import fnmatch
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "w5_fixture_bootstrap.py"


def _load():
    spec = importlib.util.spec_from_file_location("w5_fixture_bootstrap_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def fx():
    return _load()


@pytest.fixture(scope="module")
def plan(fx):
    return fx.build_plan()


# ── identité avec git réel ────────────────────────────────────────────────────────────────────────────────────────────────


def _git(cwd, *args, env=None):
    base = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(cwd),
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    return subprocess.run(
        ["git", *args], cwd=cwd, env={**base, **(env or {})}, capture_output=True, text=True, check=True
    ).stdout.strip()


def test_the_computed_seed_tree_is_the_remote_seed_tree(fx):
    seed = fx.load_seed()
    assert (
        fx.git_tree_sha({p: t.encode() for p, t in seed.items()})
        == fx.SEED_TREE_SHA
        == "c8bffa32325a836d68e14bff24f40c4d7b29266c"
    )


def test_the_bootstrap_commit_is_byte_identical_to_what_real_git_produces(fx, plan, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    env = {
        "GIT_AUTHOR_NAME": fx.AUTHOR["name"],
        "GIT_AUTHOR_EMAIL": fx.AUTHOR["email"],
        "GIT_AUTHOR_DATE": f"{fx.COMMIT_EPOCH} +0000",
        "GIT_COMMITTER_NAME": fx.AUTHOR["name"],
        "GIT_COMMITTER_EMAIL": fx.AUTHOR["email"],
        "GIT_COMMITTER_DATE": f"{fx.COMMIT_EPOCH} +0000",
    }
    # parent local (le SHA de la graine distante n'existe pas ici) : la formule du commit est la même pour tout parent
    seed_tree_files = fx.load_seed()
    for path, text in seed_tree_files.items():
        target = repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(text.encode("utf-8"))
    _git(repo, "add", "-A", "-f")
    parent = _git(repo, "commit-tree", _git(repo, "write-tree"), "-m", "parent local", env=env)
    for path, text in plan["files"].items():
        target = repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(text.encode("utf-8"))
    _git(repo, "add", "-A", "-f")
    tree = _git(repo, "write-tree")
    commit = _git(repo, "commit-tree", tree, "-p", parent, "-m", plan["message"].rstrip("\n"), env=env)
    assert tree == plan["bootstrap_tree_sha"], "l'arbre calculé doit être celui de git"
    assert commit == fx.git_commit_sha(tree, parent), "le SHA du commit doit être celui que git calcule (déterminisme)"


# ── plan : contenu et déterminisme ────────────────────────────────────────────────────────────────────────────────────────


def test_the_plan_is_deterministic_and_its_hashes_match_the_contents(fx, plan):
    again = fx.build_plan()
    assert again == plan
    for path, digest in plan["approved_files"].items():
        assert hashlib.sha256(plan["files"][path].encode()).hexdigest() == digest
    assert plan["order_token"] == "APPLIQUER-W5-FIXTURE-" + plan["bootstrap_sha"][:12]


def test_the_scaffold_adds_the_control_files_and_changes_only_requirements_of_the_seed(fx, plan):
    assert (
        plan["created_files"]
        == plan["added_files"]
        == [
            ".github/CODEOWNERS",
            ".github/workflows/fixture-tests.yml",
            "ci/requirements-approved.lock",
            "docs/deploiement.md",
            "docs/runbook-ops.md",
        ]
    )
    assert plan["modified_seed_files"] == ["requirements.txt"], (
        "seule modification autorisée de la graine (décision du manager)"
    )
    assert set(plan["approved_files"]) == set(plan["added_files"]) | {"requirements.txt"}
    seed = fx.load_seed()
    for path, text in seed.items():
        if path != "requirements.txt":
            assert plan["files"][path] == text, f"{path} : la graine est conservée octet pour octet"
    assert plan["files"]["requirements.txt"] != seed["requirements.txt"]
    forbidden = (
        "alembic",
        "migrations",
        "app/db.py",
        "app/export.py",
        "app/repository.py",
        "tests/test_audit",
        "tests/test_export",
        "docs/export_header.md",
    )
    assert not [p for p in plan["files"] if p.startswith(forbidden)], (
        "aucune implémentation ni en-tête d'export du BUILD"
    )
    both = plan["files"]["docs/deploiement.md"] + plan["files"]["docs/runbook-ops.md"]
    assert (
        "AKIAIOSFODNN7EXAMPLE" in plan["files"]["docs/deploiement.md"]
        and "AKIAI44QH8DHBEXAMPLE" in plan["files"]["docs/runbook-ops.md"]
    )
    assert both.count("EXAMPLE") >= 4


def test_the_scaffold_refuses_any_other_modification_of_the_seed(fx, monkeypatch):
    original = fx.scaffold_files
    monkeypatch.setattr(fx, "scaffold_files", lambda: {**original(), "app/main.py": "print('x')\n"})
    with pytest.raises(fx.FixtureError, match="que requirements.txt"):
        fx.build_plan()
    monkeypatch.setattr(fx, "scaffold_files", lambda: {k: v for k, v in original().items() if k != "requirements.txt"})
    with pytest.raises(fx.FixtureError, match="que requirements.txt"):
        fx.build_plan()


def test_the_approved_stack_is_one_locked_source_for_the_socle_and_the_image(fx, plan):
    import tomllib

    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    group = pyproject["dependency-groups"]["fixture-stack"]
    assert plan["files"]["requirements.txt"] == "\n".join(group) + "\n"
    assert all(re.fullmatch(r"[A-Za-z0-9_.\-]+==\d[^\s;]*", item) for item in group), "versions EXACTES"
    lock = (ROOT / "locks" / "fixture-stack.txt").read_text(encoding="utf-8")
    assert plan["files"]["ci/requirements-approved.lock"] == lock, (
        "copie octet pour octet du verrou généré par scripts/locks.py"
    )
    assert "--hash=sha256:" in lock and lock.count("==") >= len(group)
    # le verrou de l'image du transport broker épingle les MÊMES versions de premier niveau
    broker = (ROOT / "locks" / "sandbox-broker.txt").read_text(encoding="utf-8")
    for item in group:
        name, version = item.split("==")
        assert re.search(rf"^{re.escape(name)}=={re.escape(version)}\b", broker, re.M | re.I), item
        assert re.search(rf"^{re.escape(name)}=={re.escape(version)}\b", lock, re.M | re.I), item


def test_the_codeowners_gives_the_control_paths_to_the_owner_and_nothing_else(fx, plan):
    entries = [ln.split() for ln in plan["files"][".github/CODEOWNERS"].splitlines() if ln and not ln.startswith("#")]
    assert entries == [["/.github/", "@VynoDePal"], ["/ci/", "@VynoDePal"]]
    assert set(fx.PROTECTED_PREFIXES) == {".github/", "ci/"}


def test_the_scaffold_satisfies_the_closed_shape_b_validates_against_the_real_tree(fx, plan):
    safe = re.compile(r"[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)*")
    for path, digest in plan["approved_files"].items():
        assert safe.fullmatch(path) and (not path.startswith(".") or path.startswith(".github/"))
        assert re.fullmatch(r"[0-9a-f]{64}", digest)
    assert plan["bootstrap_sha"] != fx.SEED_SHA and len(plan["approved_files"]) == 6


def test_the_example_documents_are_byte_identical_to_the_ones_b_ships_once_integrated(fx):
    root = Path(__file__).resolve().parent / "fixtures" / "w5-business" / "docs"
    if not root.is_dir():
        pytest.skip("dossier de B non intégré (recoupement fait à l'intégration, avant tout apply)")
    assert (root / "runbook-ops.md").read_bytes() == fx.RUNBOOK_OPS_DOC.encode("utf-8")
    assert (root / "deploiement.md").read_bytes() == fx.DEPLOY_DOC.encode("utf-8")


def test_the_manifest_skeleton_matches_the_common_contract_and_is_never_a_proof_by_itself(fx, plan):
    manifest = fx.manifest_skeleton(plan)
    assert manifest["schema"] == "collegue-fixture-bootstrap/1"
    assert manifest["repository"] == "VynoDePal/collegue-e2e-fixture" and manifest["repository_id"] == 1298596453
    assert (
        manifest["seed_sha"] == "8e3691d8e4f311e00d620c9c2ca2d9edbd8b136a"
        and manifest["bootstrap_sha"] == plan["bootstrap_sha"]
    )
    assert manifest["required_check"] == "Fixture tests" and manifest["check_app_id"] == 15368
    assert manifest["branch_pattern"] == "refs/heads/collegue-business/*" and manifest["ruleset_id"] is None
    assert manifest["approved_files"] == plan["approved_files"]
    assert manifest["added_files"] == plan["added_files"] and manifest["modified_seed_files"] == ["requirements.txt"]
    detail = manifest["modified_seed_hashes"]["requirements.txt"]
    seed = fx.load_seed()
    assert detail["seed_sha256"] == hashlib.sha256(seed["requirements.txt"].encode()).hexdigest()
    assert detail["approved_sha256"] == manifest["approved_files"]["requirements.txt"] != detail["seed_sha256"]
    workflow = manifest["check_workflow"]
    assert workflow["triggers"] == ["pull_request", "push"] and workflow["job"] == "Fixture tests"
    assert (
        workflow["workflow"] in manifest["approved_files"]
        and workflow["dependency_source"] in manifest["approved_files"]
    )
    assert manifest["protected_prefixes"] == [".github/", "ci/"] and manifest["code_owner"] == "@VynoDePal"
    assert "check_producer" not in manifest, "le check est celui d'un job : plus de publication manuelle"


def test_write_plan_produces_every_artifact_with_consistent_sums(fx, tmp_path):
    fx.write_plan(tmp_path / "plan")
    names = {p.name for p in (tmp_path / "plan").iterdir()}
    assert names == set(fx.PLAN_FILES)
    for line in (tmp_path / "plan" / "SHA256SUMS").read_text().splitlines():
        digest, name = line.split("  ")
        assert hashlib.sha256((tmp_path / "plan" / name).read_bytes()).hexdigest() == digest
    calls = (tmp_path / "plan" / "api-calls.md").read_text(encoding="utf-8")
    assert (
        "APPLIQUER-W5-FIXTURE-" in calls
        and "JAMAIS modifiés" in calls
        and "git/trees" in calls
        and "/rulesets" in calls
    )
    assert "check-runs" in calls, "l'attente du check du socle précède la création du ruleset"
    diff = (tmp_path / "plan" / "bootstrap.diff").read_text(encoding="utf-8")
    assert (
        "+++ b/.github/workflows/fixture-tests.yml" in diff
        and "+++ b/requirements.txt" in diff
        and "app/main.py" not in diff
    )
    probes = (tmp_path / "plan" / "probe-plan.md").read_text(encoding="utf-8")
    for scenario in (
        "green",
        "red-test",
        "workflow-touch",
        "codeowners-touch",
        "lock-touch",
        "unapproved-dependency",
        "symlink",
        "seed-base",
    ):
        assert f"`{scenario}`" in probes


# ── workflow du socle (statique) ──────────────────────────────────────────────────────────────────────────────────────────


def _workflow(fx):
    return yaml.safe_load(fx.TRUSTED_WORKFLOW)


def _steps(fx):
    return _workflow(fx)["jobs"]["fixture-tests"]["steps"]


def test_the_workflow_runs_on_pull_request_and_on_the_bootstrap_push_never_on_pull_request_target(fx):
    text = fx.TRUSTED_WORKFLOW
    wf = _workflow(fx)
    on = wf.get(True) or wf.get("on")  # PyYAML lit « on » comme un booléen
    assert set(on) == {"pull_request", "push"}, (
        "pull_request_target s'exécute sur la branche par défaut (graine sans workflow)"
    )
    assert on["pull_request"]["branches"] == ["collegue-business/**"]
    assert on["push"]["branches"] == [fx.BOOTSTRAP_BRANCH], "le commit du socle porte son propre check"
    assert re.search(r"^[^#\n]*pull_request_target", text, re.M) is None, (
        "jamais comme déclencheur ; seulement expliqué en commentaire"
    )
    assert wf["permissions"] == {"contents": "read"}
    job = wf["jobs"]["fixture-tests"]
    assert job["name"] == fx.REQUIRED_CHECK == "Fixture tests", (
        "le check requis EST le job (aucune publication manuelle)"
    )
    assert "secrets." not in text and "github.token" not in text and "GH_TOKEN" not in text and "checks:" not in text


def test_candidate_code_never_runs_on_the_host_and_dependencies_come_only_from_the_approved_hashed_lock(fx):
    steps = _steps(fx)
    assert str(steps[0]["uses"]).startswith("actions/checkout") and steps[0]["with"]["persist-credentials"] is False
    runs = [s.get("run", "") for s in steps]
    host = re.sub(
        r"bash -c '.*?'", "", "\n".join(runs), flags=re.S
    )  # le script entre quotes s'exécute DANS le conteneur
    assert "sudo" not in host and "docker.sock" not in host and "--privileged" not in host
    assert not re.search(r"^\s*(python|pytest|pip)\b", host, re.M), (
        "aucune commande Python sur l'hôte : tout passe par un conteneur"
    )
    download = next(r for r in runs if "pip download" in r)
    tests = next(r for r in runs if "pytest" in r)
    for command in (download, tests):
        for flag in ("--user 65534:65534", "--cap-drop ALL", "--security-opt no-new-privileges", "--read-only"):
            assert flag in command
        assert "-e GH_TOKEN" not in command and "GITHUB_TOKEN" not in command and "secrets" not in command
        assert '"$PY_IMAGE"' in command
    assert "--network none" not in download and "--network none" in tests
    assert (
        "--require-hashes" in download
        and "-r /src/ci/requirements-approved.lock" in download
        and "--only-binary=:all:" in download
    )
    assert "--no-index" in tests and "--require-hashes" in tests and "--find-links /wheels" in tests
    assert (
        "requirements.txt demande une dépendance hors de la pile approuvée" in tests
        and "pip install --no-index" in tests
    )
    assert "pip download" not in tests and "requirements.txt" not in download, (
        "le requirements.txt du candidat ne dicte aucun téléchargement"
    )


def test_mounts_are_directories_never_a_candidate_controlled_file_path(fx):
    for run in (s.get("run", "") for s in _steps(fx)):
        for source in re.findall(r'-v\s+"([^"]+)"', run):
            host = source.split(":")[0]
            assert host in ("$GITHUB_WORKSPACE", "$RUNNER_TEMP/wheels"), f"montage de source inattendue : {host}"
    guard = next(s for s in _steps(fx) if s.get("name", "").startswith("Garde"))["run"]
    assert "-type l" in guard and "requirements.txt ci/requirements-approved.lock" in guard and "[ -L" in guard
    assert "exit 1" in guard


def test_the_runner_image_is_pinned_by_digest(fx):
    image = _workflow(fx)["jobs"]["fixture-tests"]["env"]["PY_IMAGE"]
    assert re.fullmatch(r"python:3\.12-slim@sha256:[0-9a-f]{64}", image) and image == fx.PY_IMAGE


def test_the_ruleset_is_active_without_bypass_requires_the_owner_and_binds_the_check_to_the_actions_app(fx, plan):
    rs = plan["ruleset"]
    assert rs["enforcement"] == "active" and rs["bypass_actors"] == [] and rs["target"] == "branch"
    assert rs["conditions"]["ref_name"] == {"include": ["refs/heads/collegue-business/*"], "exclude": []}
    assert {r["type"] for r in rs["rules"]} == {"pull_request", "required_status_checks"}, (
        "pas de règle deletion : le nettoyage supprime ses bases"
    )
    pr = next(r for r in rs["rules"] if r["type"] == "pull_request")["parameters"]
    assert pr["require_code_owner_review"] is True and pr["required_approving_review_count"] == 0
    checks = next(r for r in rs["rules"] if r["type"] == "required_status_checks")["parameters"]
    assert checks["strict_required_status_checks_policy"] is True
    assert checks["do_not_enforce_on_create"] is False, (
        "une base ne peut être créée que depuis un commit qui a passé le check"
    )
    assert checks["required_status_checks"] == [{"context": "Fixture tests", "integration_id": 15368}]
    assert "~DEFAULT_BRANCH" not in json.dumps(rs) and "main" not in rs["conditions"]["ref_name"]["include"][0]


@pytest.mark.parametrize(
    "branch, expected",
    [
        ("collegue-business/run-1", True),
        ("collegue-business/bootstrap-w5", True),
        ("collegue-business/probe-x-0", True),
        ("main", False),
        ("collegue/issue-1", False),
        ("collegue-business-other/x", False),
    ],
)
def test_the_ruleset_pattern_covers_the_ephemeral_bases_and_never_main(fx, branch, expected):
    assert fnmatch.fnmatch(f"refs/heads/{branch}", fx.BRANCH_PATTERN) is expected


# ── évaluation des checks, arbre protégé et provenance (fonctions pures) ──────────────────────────────────────────────────

HEAD = "a" * 40


def run(name="Fixture tests", app=15368, head=HEAD, status="completed", conclusion="success"):
    return {"name": name, "app": {"id": app}, "head_sha": head, "status": status, "conclusion": conclusion}


@pytest.mark.parametrize(
    "runs, expected",
    [
        ([run()], "success"),
        ([run(conclusion="failure")], "failure"),
        ([run(conclusion="cancelled")], "failure"),
        ([run(status="in_progress", conclusion=None)], "pending"),
        ([], "missing"),
        ([run(app=99999)], "missing"),
        ([run(name="fixture tests")], "missing"),
        ([run(head="b" * 40)], "missing"),
        ([run(), run(conclusion="failure")], "failure"),
        ([run(), run(app=99999, conclusion="failure")], "success"),
    ],
)
def test_check_evaluation_never_turns_a_wrong_check_into_a_success(fx, runs, expected):
    assert fx.evaluate_check(runs, head_sha=HEAD)[0] == expected


def _entries(fx, plan, **changes):
    out = {}
    for path, text in plan["files"].items():
        out[path] = {"path": path, "type": "blob", "mode": "100644", "sha": fx.git_blob_sha(text.encode())}
    for path, value in changes.items():
        path = path.replace("__", "/").replace("_dot_", ".")
        if value is None:
            out.pop(path, None)
        else:
            out[path] = value
    return list(out.values())


def test_the_protected_tree_check_flags_a_modified_removed_added_or_irregular_object(fx, plan):
    assert fx.protected_tree_violations(_entries(fx, plan), plan) == []
    tampered = _entries(fx, plan)
    wf = next(e for e in tampered if e["path"] == fx.WORKFLOW_PATH)
    wf["sha"] = "0" * 40
    assert any("modifié" in m and fx.WORKFLOW_PATH in m for m in fx.protected_tree_violations(tampered, plan))
    removed = [e for e in _entries(fx, plan) if e["path"] != fx.CODEOWNERS_PATH]
    assert any("supprimé" in m for m in fx.protected_tree_violations(removed, plan))
    added = _entries(fx, plan) + [{"path": "ci/extra.sh", "type": "blob", "mode": "100644", "sha": "1" * 40}]
    assert any("ajouté" in m and "ci/extra.sh" in m for m in fx.protected_tree_violations(added, plan))
    link = _entries(fx, plan) + [{"path": "docs/lien", "type": "blob", "mode": "120000", "sha": "2" * 40}]
    assert any("irrégulier" in m for m in fx.protected_tree_violations(link, plan))
    sub = _entries(fx, plan) + [{"path": "vendor/x", "type": "commit", "mode": "160000", "sha": "3" * 40}]
    assert any("irrégulier" in m for m in fx.protected_tree_violations(sub, plan))
    business = _entries(fx, plan) + [{"path": "app/new.py", "type": "blob", "mode": "100644", "sha": "4" * 40}]
    assert fx.protected_tree_violations(business, plan) == [], (
        "le code métier n'est PAS protégé : c'est ce que la campagne produit"
    )


def test_integrity_flags_a_modified_removed_or_added_protected_file_only(fx, plan):
    tree = dict(plan["approved_files"])
    assert fx.integrity_violations(tree, plan["approved_files"]) == []
    tree[fx.WORKFLOW_PATH] = "0" * 64
    assert any("modifié" in m for m in fx.integrity_violations(tree, plan["approved_files"]))
    del tree[fx.WORKFLOW_PATH]
    assert any("supprimé" in m for m in fx.integrity_violations(tree, plan["approved_files"]))
    tree = dict(plan["approved_files"])
    tree[".github/workflows/evil.yml"] = "1" * 64
    tree["ci/evil.lock"] = "1" * 64
    assert sum("ajouté" in m for m in fx.integrity_violations(tree, plan["approved_files"])) == 2
    tree = dict(plan["approved_files"])
    tree["app/main.py"] = "2" * 64
    tree["app/new.py"] = "3" * 64
    assert fx.integrity_violations(tree, plan["approved_files"]) == []


# ── faux serveur GitHub en mémoire ────────────────────────────────────────────────────────────────────────────────────────


class FakeGitHub:
    """Modélise seulement ce dont le script a besoin ; chaque écriture est journalisée pour prouver ce qui n'a PAS été touché."""

    def __init__(self, fx, plan, *, main_sha=None, bootstrap_check="success"):
        self.fx, self.plan = fx, plan
        self.bootstrap_check = bootstrap_check  # success | failure | None (aucun check) | pending (jamais terminé)
        self.refs = {"refs/heads/main": main_sha or fx.SEED_SHA}
        self.commits = {fx.SEED_SHA: {"tree": fx.SEED_TREE_SHA, "parents": []}}
        self.rulesets = {
            fx.SEED_RULESET_ID: {
                "id": fx.SEED_RULESET_ID,
                "name": "Immutable nightly seed",
                "target": "branch",
                "enforcement": "active",
                "bypass_actors": [],
                "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}},
                "rules": [{"type": "deletion"}, {"type": "non_fast_forward"}, {"type": "update"}],
            }
        }
        self.next_ruleset = 100
        self.pulls = {4: {"state": "closed"}, 7: {"state": "closed"}}
        self.writes: list = []
        self.trees = {}
        self.contents_by_ref: dict = {}
        self.ruleset_created_after_check = None

    def bootstrap_runs(self):
        if self.bootstrap_check is None:
            return []
        done = self.bootstrap_check in ("success", "failure")
        return [
            {
                "id": 1,
                "name": "Fixture tests",
                "app": {"id": 15368},
                "head_sha": self.plan["bootstrap_sha"],
                "status": "completed" if done else "in_progress",
                "conclusion": self.bootstrap_check if done else None,
            }
        ]

    def check_runs_for(self, sha):
        if sha == self.plan["bootstrap_sha"]:
            return self.bootstrap_runs()
        return []

    def __call__(self, method, path, payload):
        if method != "GET":
            self.writes.append((method, path))
        repo = f"/repos/{self.fx.REPOSITORY}"
        if path == repo:
            return {
                "id": self.fx.REPOSITORY_ID,
                "full_name": self.fx.REPOSITORY,
                "private": False,
                "archived": False,
                "default_branch": "main",
            }
        if path == "/apps/github-actions":
            return {"id": 15368, "slug": "github-actions"}
        m = re.fullmatch(rf"{repo}/commits/([0-9a-f]{{40}})/check-runs", path)
        if m:
            return {"check_runs": self.check_runs_for(m.group(1))}
        m = re.fullmatch(rf"{repo}/git/ref/(heads/.+)", path)
        if m:
            ref = "refs/" + m.group(1)
            if ref not in self.refs:
                raise self.fx.ApiError(404, "Not Found")
            return {"object": {"sha": self.refs[ref]}}
        m = re.fullmatch(rf"{repo}/git/commits/([0-9a-f]{{40}})", path)
        if m:
            c = self.commits.get(m.group(1))
            if c is None:
                raise self.fx.ApiError(404, "Not Found")
            return {"tree": {"sha": c["tree"]}, "parents": [{"sha": p} for p in c["parents"]]}
        if path == f"{repo}/git/trees" and method == "POST":
            files = {p: t.encode() for p, t in self.plan["files"].items()}
            sha = self.fx.git_tree_sha(files)
            self.trees[sha] = files
            return {"sha": sha}
        if path == f"{repo}/git/commits" and method == "POST":
            sha = self.fx.git_commit_sha(payload["tree"], payload["parents"][0])
            self.commits[sha] = {"tree": payload["tree"], "parents": payload["parents"]}
            return {"sha": sha}
        if path == f"{repo}/git/refs" and method == "POST":
            if payload["ref"] in self.refs:
                raise self.fx.ApiError(422, "Reference already exists")
            self.refs[payload["ref"]] = payload["sha"]
            return {"object": {"sha": payload["sha"]}}
        m = re.fullmatch(rf"{repo}/git/refs/(heads/.+)", path)
        if m and method == "DELETE":
            self.refs.pop("refs/" + m.group(1), None)
            return None
        if path == f"{repo}/rulesets" and method == "GET":
            return [{"id": r["id"], "name": r["name"]} for r in self.rulesets.values()]
        if path == f"{repo}/rulesets" and method == "POST":
            self.ruleset_created_after_check = [r["conclusion"] for r in self.bootstrap_runs()] == ["success"]
            self.next_ruleset += 1
            made = {**json.loads(json.dumps(payload)), "id": self.next_ruleset}
            self.rulesets[made["id"]] = made
            return made
        m = re.fullmatch(rf"{repo}/rulesets/(\d+)", path)
        if m:
            rid = int(m.group(1))
            if method == "DELETE":
                self.rulesets.pop(rid, None)
                return None
            if rid not in self.rulesets:
                raise self.fx.ApiError(404, "Not Found")
            return self.rulesets[rid]
        m = re.fullmatch(rf"{repo}/contents/(.+)\?ref=(.+)", path)
        if m and method == "GET":
            c = self.commits.get(m.group(2)) or self.commits.get(self.refs.get(f"refs/heads/{m.group(2)}", ""))
            files = self.trees.get(c["tree"]) if c else None
            data = (files or {}).get(m.group(1))
            if data is None:
                data = self.contents_by_ref.get((m.group(2), m.group(1)))
            if data is None:
                raise self.fx.ApiError(404, "Not Found")
            return {"content": base64.b64encode(data).decode(), "sha": hashlib.sha1(data).hexdigest()}
        m = re.fullmatch(rf"{repo}/pulls/(\d+)", path)
        if m:
            n = int(m.group(1))
            if method == "PATCH":
                self.pulls[n]["state"] = payload["state"]
            if n not in self.pulls:
                raise self.fx.ApiError(404, "Not Found")
            return self.pulls[n]
        raise AssertionError(f"appel non modélisé : {method} {path}")


@pytest.fixture
def remote(fx, plan):
    return FakeGitHub(fx, plan)


class Clock:
    """Horloge simulée : chaque ``sleep`` fait avancer le temps, donc aucun test n'attend réellement."""

    def __init__(self):
        self.now = 0.0

    def sleep(self, seconds):
        self.now += seconds

    def __call__(self):
        return self.now


def apply(fx, plan, remote, **kw):
    clock = Clock()
    return fx.apply_plan(remote, plan, order_token=plan["order_token"], sleep=clock.sleep, clock=clock, **kw)


def test_apply_without_the_exact_order_token_writes_nothing(fx, plan, remote):
    for token in (None, "", "APPLIQUER-W5-FIXTURE-000000000000", plan["order_token"] + "x"):
        with pytest.raises(fx.FixtureError, match="jeton d'ordre"):
            fx.apply_plan(remote, plan, order_token=token)
    assert remote.writes == []


def test_apply_creates_the_branch_waits_for_its_check_then_creates_the_ruleset_and_never_touches_main_or_foreign_resources(
    fx, plan, remote
):
    result = apply(fx, plan, remote)
    assert result["created"] == ["branch", "ruleset"] and result["idempotent"] is False
    assert remote.ruleset_created_after_check is True, "le ruleset n'est créé qu'APRÈS le check réussi du socle"
    assert remote.refs["refs/heads/collegue-business/bootstrap-w5"] == plan["bootstrap_sha"]
    assert remote.refs["refs/heads/main"] == fx.SEED_SHA
    manifest = result["manifest"]
    assert manifest["ruleset_id"] in remote.rulesets and manifest["bootstrap_sha"] == plan["bootstrap_sha"]
    assert all(
        "/pulls/" not in path and f"/rulesets/{fx.SEED_RULESET_ID}" not in path and "heads/main" not in path
        for _m, path in remote.writes
    ), remote.writes
    assert remote.rulesets[fx.SEED_RULESET_ID]["enforcement"] == "active"
    assert remote.pulls == {4: {"state": "closed"}, 7: {"state": "closed"}}
    assert fx.verify_remote(remote, plan)["ok"] is True


@pytest.mark.parametrize(
    ("state", "message"),
    [
        (None, "ne s'est pas déclenché"),  # le workflow du socle ne tourne pas : jamais un succès présumé
        ("pending", "ne s'est pas déclenché"),
        ("failure", "check du socle est rouge"),
    ],
)
def test_apply_never_creates_the_ruleset_without_a_green_bootstrap_check(fx, plan, state, message):
    remote = FakeGitHub(fx, plan, bootstrap_check=state)
    with pytest.raises(fx.FixtureError, match=message):
        apply(fx, plan, remote, check_timeout=60.0)
    assert not any(path.endswith("/rulesets") for _m, path in remote.writes), "aucun ruleset"
    assert remote.refs["refs/heads/collegue-business/bootstrap-w5"] == plan["bootstrap_sha"], (
        "état conservé pour examen"
    )
    assert fx.cleanup_bootstrap(remote, plan, order_token=plan["order_token"])["removed"] == [
        "branche collegue-business/bootstrap-w5"
    ]


def test_apply_is_idempotent_and_a_second_run_writes_nothing(fx, plan, remote):
    apply(fx, plan, remote)
    writes = len(remote.writes)
    again = apply(fx, plan, remote)
    assert again["created"] == [] and again["idempotent"] is True and len(remote.writes) == writes


def test_apply_refuses_every_collision_and_unowned_resource_without_any_write(fx, plan, remote):
    other = {
        "id": 555,
        "name": "ruleset d'un tiers",
        "target": "branch",
        "enforcement": "active",
        "bypass_actors": [],
        "conditions": {"ref_name": {"include": [fx.BRANCH_PATTERN], "exclude": []}},
        "rules": [],
    }
    remote.rulesets[555] = other
    with pytest.raises(fx.FixtureError, match="collision de ruleset"):
        apply(fx, plan, remote)
    del remote.rulesets[555]
    remote.refs["refs/heads/collegue-business/bootstrap-w5"] = "f" * 40
    with pytest.raises(fx.FixtureError, match="ressource non possédée"):
        apply(fx, plan, remote)
    del remote.refs["refs/heads/collegue-business/bootstrap-w5"]
    remote.rulesets[600] = {
        **json.loads(json.dumps(plan["ruleset"])),
        "id": 600,
        "bypass_actors": [{"actor_id": 1, "actor_type": "RepositoryRole"}],
    }
    with pytest.raises(fx.FixtureError, match="autre contenu"):
        apply(fx, plan, remote)
    assert remote.writes == []


def test_apply_refuses_when_main_is_no_longer_the_seed_or_the_identity_changes(fx, plan):
    moved = FakeGitHub(fx, plan, main_sha="c" * 40)
    with pytest.raises(fx.FixtureError, match="graine immuable"):
        apply(fx, plan, moved)
    assert moved.writes == []
    inactive = FakeGitHub(fx, plan)
    inactive.rulesets[fx.SEED_RULESET_ID]["enforcement"] = "disabled"
    with pytest.raises(fx.FixtureError, match="ruleset de la graine"):
        apply(fx, plan, inactive)
    assert inactive.writes == []


def test_verify_detects_a_tampered_branch_file_ruleset_or_missing_bootstrap_check(fx, plan):
    good = FakeGitHub(fx, plan)
    apply(fx, plan, good)
    assert fx.verify_remote(good, plan)["ok"] is True
    tampered = FakeGitHub(fx, plan)
    apply(fx, plan, tampered)
    rid = next(i for i, r in tampered.rulesets.items() if r["name"] == fx.RULESET_NAME)
    tampered.rulesets[rid]["bypass_actors"] = [{"actor_id": 5, "actor_type": "RepositoryRole"}]
    result = fx.verify_remote(tampered, plan)
    assert not result["ok"] and any("ruleset différent" in p for p in result["problems"])
    weakened = FakeGitHub(fx, plan)
    apply(fx, plan, weakened)
    rid = next(i for i, r in weakened.rulesets.items() if r["name"] == fx.RULESET_NAME)
    pr_rule = next(r for r in weakened.rulesets[rid]["rules"] if r["type"] == "pull_request")
    pr_rule["parameters"]["require_code_owner_review"] = False
    assert any("ruleset différent" in p for p in fx.verify_remote(weakened, plan)["problems"]), (
        "code owner retiré = écart"
    )
    moved = FakeGitHub(fx, plan)
    apply(fx, plan, moved)
    moved.refs["refs/heads/collegue-business/bootstrap-w5"] = "d" * 40
    assert any("au lieu de" in p for p in fx.verify_remote(moved, plan)["problems"])
    nocheck = FakeGitHub(fx, plan)
    apply(fx, plan, nocheck)
    nocheck.bootstrap_check = None
    assert any("check du socle non réussi" in p for p in fx.verify_remote(nocheck, plan)["problems"])
    assert not fx.verify_remote(FakeGitHub(fx, plan), plan)["ok"], (
        "rien d'appliqué : vérification rouge, pas verte par défaut"
    )


def test_cleanup_removes_only_this_campaigns_bootstrap_resources(fx, plan, remote):
    apply(fx, plan, remote)
    remote.refs["refs/heads/collegue-business/run-etranger"] = "e" * 40
    remote.writes.clear()
    with pytest.raises(fx.FixtureError, match="jeton d'ordre"):
        fx.cleanup_bootstrap(remote, plan, order_token="nope")
    assert remote.writes == []
    removed = fx.cleanup_bootstrap(remote, plan, order_token=plan["order_token"])["removed"]
    assert len(removed) == 2
    assert "refs/heads/collegue-business/bootstrap-w5" not in remote.refs
    assert remote.refs["refs/heads/collegue-business/run-etranger"] == "e" * 40, "ressource non possédée conservée"
    assert remote.refs["refs/heads/main"] == fx.SEED_SHA and fx.SEED_RULESET_ID in remote.rulesets
    assert all(f"rulesets/{fx.SEED_RULESET_ID}" not in path for _m, path in remote.writes)
    assert fx.cleanup_bootstrap(remote, plan, order_token=plan["order_token"])["removed"] == []


def test_cleanup_refuses_a_foreign_branch_or_ruleset_under_our_names(fx, plan, remote):
    apply(fx, plan, remote)
    remote.refs["refs/heads/collegue-business/bootstrap-w5"] = "9" * 40
    remote.writes.clear()
    with pytest.raises(fx.FixtureError, match="non possédée"):
        fx.cleanup_bootstrap(remote, plan, order_token=plan["order_token"])
    assert not any(m == "DELETE" and "git/refs" in p for m, p in remote.writes)


# ── probe : contre-épreuves (simulées) ────────────────────────────────────────────────────────────────────────────────────

# scénario -> (conclusion du check | None, fusion « accepted » | « refused »)
GOOD = {
    "green": ("success", "accepted"),
    "red-test": ("failure", "refused"),
    "workflow-touch": ("success", "refused"),
    "codeowners-touch": ("success", "refused"),
    "lock-touch": ("success", "refused"),
    "unapproved-dependency": ("failure", "refused"),
    "symlink": ("failure", "refused"),
    "seed-base": (None, "refused"),
}


class ProbeServer(FakeGitHub):
    """Ajoute pulls, contenus, objets Git, fusions et check-runs : le comportement de chaque scénario est INJECTÉ."""

    def __init__(self, fx, plan, outcomes, **kw):
        super().__init__(fx, plan, **kw)
        self.outcomes = outcomes
        self.counter = 0
        self.heads = {}  # branche de tête -> SHA courant
        self.prs = {}
        self.merges = []
        self.spoof_mode = "refused"  # refused | foreign (acceptée, autre application) | counted (acceptée, application Actions : défaut)
        self.spoofed = {}
        self.refuse_seed_base = True
        self.provenance_ok = True
        self.tree_override = None
        self.symlink_commits = []
        self.head_base = {}  # branche de tête -> SHA de départ
        self.head_files = {}  # branche de tête -> {chemin: (mode, sha de blob)} modifiés / ajoutés
        apply(fx, plan, self)
        self.writes.clear()

    def scenario_of(self, branch):
        return self.fx.probe_scenarios(self.plan)[int(branch.rsplit("-", 1)[-1])]["id"]

    def head_branch(self, sha):
        return next(h for h, s in self.heads.items() if s == sha)

    def __call__(self, method, path, payload):
        repo = f"/repos/{self.fx.REPOSITORY}"
        if (
            method == "POST"
            and path == f"{repo}/git/refs"
            and payload["sha"] == self.fx.SEED_SHA
            and self.refuse_seed_base
        ):
            if payload["ref"].startswith("refs/heads/collegue-business/probe-"):
                self.writes.append((method, path))
                raise self.fx.ApiError(422, "Repository rule violations found: required status check")
        if method == "POST" and path == f"{repo}/git/refs" and payload["ref"].startswith("refs/heads/collegue-probe/"):
            self.heads[payload["ref"].removeprefix("refs/heads/")] = payload["sha"]
            self.head_base[payload["ref"].removeprefix("refs/heads/")] = payload["sha"]
        if method == "PUT" and path.startswith(f"{repo}/contents/"):
            self.writes.append((method, path))
            self.counter += 1
            sha = f"{self.counter:040x}"
            self.heads[payload["branch"]] = sha
            written = base64.b64decode(payload["content"])
            self.head_files.setdefault(payload["branch"], {})[path.split("/contents/", 1)[1]] = (
                "100644",
                self.fx.git_blob_sha(written),
            )
            return {"commit": {"sha": sha}}
        if method == "POST" and path == f"{repo}/git/blobs":
            self.writes.append((method, path))
            return {"sha": "b" * 40}
        if (
            method == "POST"
            and path == f"{repo}/git/trees"
            and "base_tree" in payload
            and payload["tree"][0]["mode"] == "120000"
        ):
            self.writes.append((method, path))
            self.symlink_commits.append(payload["tree"][0]["path"])
            self.pending_symlinks = {entry["path"]: ("120000", "9" * 40) for entry in payload["tree"]}
            return {"sha": "7" * 40}
        if method == "POST" and path == f"{repo}/git/commits" and payload["tree"] == "7" * 40:
            self.writes.append((method, path))
            self.counter += 1
            return {"sha": f"{self.counter:040x}"}
        m = re.fullmatch(rf"{repo}/git/refs/heads/(collegue-probe/.+)", path)
        if m and method == "PATCH":
            self.writes.append((method, path))
            self.heads[m.group(1)] = payload["sha"]
            self.head_files.setdefault(m.group(1), {}).update(getattr(self, "pending_symlinks", {}))
            self.pending_symlinks = {}
            return {"object": {"sha": payload["sha"]}}
        if method == "POST" and path == f"{repo}/pulls":
            self.writes.append((method, path))
            number = 100 + len(self.prs)
            self.prs[number] = {"head": payload["head"], "base": payload["base"], "state": "open"}
            self.pulls[number] = self.prs[number]
            return {"number": number, "html_url": f"https://github.com/{self.fx.REPOSITORY}/pull/{number}"}
        m = re.fullmatch(rf"{repo}/pulls/(\d+)/merge", path)
        if m and method == "PUT":
            self.writes.append((method, path))
            pr = self.prs[int(m.group(1))]
            self.merges.append((pr["head"], payload["sha"]))
            behaviour = self.outcomes[self.scenario_of(pr["head"])][1]
            if behaviour == "refused":
                raise self.fx.ApiError(405, "Repository rule violations found: Waiting on code owner review")
            return {"merged": True, "message": "Pull Request successfully merged"}
        if method == "POST" and path == f"{repo}/check-runs":
            self.writes.append((method, path))
            if self.spoof_mode == "refused":
                raise self.fx.ApiError(403, "Resource not accessible by personal access token")
            self.spoofed.setdefault(payload["head_sha"], []).append(
                {
                    "id": 5000,
                    "name": payload["name"],
                    "app": {"id": 15368 if self.spoof_mode == "counted" else 99},
                    "head_sha": payload["head_sha"],
                    "status": "completed",
                    "conclusion": payload["conclusion"],
                }
            )
            return {"id": 1}
        m = re.fullmatch(rf"{repo}/actions/runs\?head_sha=([0-9a-f]{{40}})", path)
        if m:
            return {
                "workflow_runs": [
                    {
                        "id": 7000,
                        "event": "pull_request",
                        "path": self.fx.WORKFLOW_PATH,
                        "head_sha": m.group(1),
                        "status": "completed",
                        "conclusion": "success",
                        "html_url": f"https://github.com/{self.fx.REPOSITORY}/actions/runs/7000",
                    }
                ]
            }
        m = re.fullmatch(rf"{repo}/actions/jobs/(\d+)", path)
        if m:
            head = self.head_for_job(int(m.group(1)))
            if head is None or not self.provenance_ok:
                raise self.fx.ApiError(404, "Not Found")
            return {"head_sha": head, "run_id": int(m.group(1)) + 1}
        m = re.fullmatch(rf"{repo}/actions/runs/(\d+)", path)
        if m:
            head = self.head_for_job(int(m.group(1)) - 1)
            return {"path": self.fx.WORKFLOW_PATH, "head_sha": head, "event": "pull_request"}
        m = re.fullmatch(rf"{repo}/git/trees/([0-9a-f]{{40}})\?recursive=1", path)
        if m:
            if self.tree_override is not None:
                return {"truncated": False, "tree": self.tree_override}
            branch = next((h for h, v in self.heads.items() if v == m.group(1)), None)
            from_seed = self.head_base.get(branch) == self.fx.SEED_SHA
            files = self.fx.load_seed() if from_seed else self.plan["files"]
            entries = {p: ("100644", self.fx.git_blob_sha(t.encode())) for p, t in files.items()}
            entries.update(self.head_files.get(branch, {}))
            return {
                "truncated": False,
                "tree": [{"path": p, "type": "blob", "mode": mode, "sha": sha} for p, (mode, sha) in entries.items()],
            }
        m = re.fullmatch(rf"{repo}/commits/([0-9a-f]{{40}})/check-runs", path)
        if m and m.group(1) in self.heads.values():
            sha = m.group(1)
            scenario = self.scenario_of(self.head_branch(sha))
            conclusion = self.outcomes[scenario][0]
            runs = list(self.spoofed.get(sha, []))
            if conclusion is not None:
                runs.append(
                    {
                        "id": 9000 + list(self.heads.values()).index(sha) * 10,
                        "name": "Fixture tests",
                        "app": {"id": 15368},
                        "head_sha": sha,
                        "status": "completed",
                        "conclusion": conclusion,
                    }
                )
            return {"check_runs": runs}
        m = re.fullmatch(rf"{repo}/pulls/(\d+)", path)
        if m and method == "GET":
            pr = self.prs[int(m.group(1))]
            return {"state": pr["state"]}
        if method == "GET" and "/contents/" in path and "?ref=collegue-probe/" in path:
            target = path.split("/contents/", 1)[1].split("?ref=")[0]
            if target == "tests/test_probe.py":
                raise self.fx.ApiError(404, "Not Found")
            return {"content": base64.b64encode(self.plan["files"][target].encode()).decode(), "sha": "5" * 40}
        return super().__call__(method, path, payload)

    def head_for_job(self, job_id):
        index = (job_id - 9000) // 10
        values = list(self.heads.values())
        return values[index] if 0 <= index < len(values) else None


def no_sleep(_seconds):
    return None


def probe(fx, plan, server, **kw):
    ticks = iter(range(100_000))
    return fx.run_probe(
        server,
        plan,
        order_token=plan["order_token"],
        probe_id="probe-1",
        sleep=no_sleep,
        clock=lambda: float(next(ticks)) * 100,
        **kw,
    )


def test_the_probe_passes_when_each_counter_proof_behaves_and_cleans_up_everything(fx, plan):
    server = ProbeServer(fx, plan, GOOD)
    result = probe(fx, plan, server)
    assert result["ok"] is True, result
    assert [r["scenario"] for r in result["results"]] == list(GOOD)
    assert not [r for r in server.refs if "probe" in r], "toutes les branches de sonde sont supprimées"
    assert all(pr["state"] == "closed" for pr in server.prs.values())
    assert server.refs["refs/heads/collegue-business/bootstrap-w5"] == plan["bootstrap_sha"]
    by = {r["scenario"]: r for r in result["results"]}
    assert by["green"]["merge"]["accepted"] is True and by["green"]["provenance"]["ok"] is True
    assert by["red-test"]["spoof"]["accepted_by_api"] is False and by["red-test"]["spoof"]["counted"] is False
    for touched in ("workflow-touch", "codeowners-touch", "lock-touch"):
        assert by[touched]["check_observed"] == "success" and by[touched]["merge"]["accepted"] is False, (
            "check VERT et fusion refusée : seul le propriétaire explique le refus"
        )
    assert by["seed-base"]["creation"] == "refused" and by["seed-base"]["ok"] is True
    assert server.symlink_commits == ["docs/lien-sonde"], (
        "le lien symbolique est poussé par l'API Git Data (mode 120000)"
    )
    # chaque fusion a été tentée sur la TÊTE EXACTE observée
    assert all(
        sha == by[name]["head_sha"]
        for (head, sha), name in zip(server.merges, [s for s in GOOD if s != "seed-base"], strict=True)
    )


@pytest.mark.parametrize(
    "scenario, outcome",
    [
        ("red-test", ("success", "refused")),  # un test rouge qui donnerait un check vert
        ("red-test", ("failure", "accepted")),  # check rouge mais fusion acceptée
        ("workflow-touch", ("success", "accepted")),  # la protection du propriétaire est INEFFICACE
        ("codeowners-touch", ("success", "accepted")),
        ("lock-touch", ("success", "accepted")),
        ("workflow-touch", ("failure", "refused")),  # refus mais pas isolé : le check n'était pas vert
        ("unapproved-dependency", ("success", "refused")),  # une dépendance hors pile ne rend pas le check rouge
        ("symlink", ("success", "refused")),
        ("green", ("success", "refused")),  # la voie nominale ne fusionne pas
        ("green", ("failure", "refused")),  # le workflow ne passe pas
        ("green", (None, "accepted")),  # le workflow ne se déclenche pas : jamais un succès présumé
    ],
)
def test_the_probe_fails_when_any_protection_does_not_hold(fx, plan, scenario, outcome):
    server = ProbeServer(fx, plan, {**GOOD, scenario: outcome})
    result = probe(fx, plan, server, observe_missing_seconds=1.0, timeout=3.0)
    assert result["ok"] is False and not {r["scenario"]: r["ok"] for r in result["results"]}[scenario]
    assert not [r for r in server.refs if "probe" in r], "même en échec, rien ne reste"


def test_the_probe_fails_if_a_base_can_be_created_from_the_seed_and_a_check_appears(fx, plan):
    server = ProbeServer(fx, plan, {**GOOD, "seed-base": ("success", "refused")})
    server.refuse_seed_base = False
    result = probe(fx, plan, server, observe_missing_seconds=1.0)
    seed = {r["scenario"]: r for r in result["results"]}["seed-base"]
    assert seed["creation"] == "accepted" and seed["ok"] is False and result["ok"] is False


def test_the_probe_accepts_an_unrefused_seed_base_only_if_no_check_exists_and_the_merge_is_refused(fx, plan):
    server = ProbeServer(fx, plan, GOOD)
    server.refuse_seed_base = False
    result = probe(fx, plan, server, observe_missing_seconds=1.0)
    seed = {r["scenario"]: r for r in result["results"]}["seed-base"]
    assert seed["creation"] == "accepted" and "note" in seed and seed["ok"] is True


@pytest.mark.parametrize(
    ("mode", "ok"),
    [
        ("refused", True),  # une PAT ne peut pas créer de check-run
        ("foreign", True),  # acceptée mais d'une autre application : ne compte pas pour le ruleset
        ("counted", False),  # un faux check qui compterait = le contrôle est contournable
    ],
)
def test_the_probe_proves_that_a_forged_check_from_another_actor_never_counts(fx, plan, mode, ok):
    server = ProbeServer(fx, plan, GOOD)
    server.spoof_mode = mode
    result = probe(fx, plan, server)
    assert result["ok"] is ok, result
    assert not [r for r in server.refs if "probe" in r]


def test_the_probe_requires_the_order_a_valid_id_and_a_verified_remote(fx, plan):
    server = ProbeServer(fx, plan, GOOD)
    with pytest.raises(fx.FixtureError, match="jeton d'ordre"):
        fx.run_probe(server, plan, order_token="x", probe_id="probe-3")
    with pytest.raises(fx.FixtureError, match="identifiant de sonde"):
        fx.run_probe(server, plan, order_token=plan["order_token"], probe_id="Mauvais ID")
    assert server.writes == []
    blank = FakeGitHub(fx, plan)
    with pytest.raises(fx.FixtureError, match="verify"):
        fx.run_probe(blank, plan, order_token=plan["order_token"], probe_id="probe-4")
    assert blank.writes == []


# ── provenance d'un check (lectures seules) ───────────────────────────────────────────────────────────────────────────────


def test_provenance_accepts_only_a_real_job_of_the_approved_workflow_with_an_intact_protected_tree(fx, plan):
    server = ProbeServer(fx, plan, GOOD)
    probe(fx, plan, server)  # remplit les têtes simulées
    head = next(iter(server.heads.values()))  # tête du scénario « green »
    server.outcomes = GOOD  # les têtes ont été supprimées côté refs, pas côté modèle
    ok, why = fx.check_provenance(server, head, plan)
    assert ok is True, why
    server.provenance_ok = False  # le check n'est pas un job réel (publié par l'API des checks)
    ok, why = fx.check_provenance(server, head, plan)
    assert ok is False and "pas un job" in why
    server.provenance_ok = True
    server.tree_override = [e for e in server_tree(fx, plan) if e["path"] != fx.CODEOWNERS_PATH]
    ok, why = fx.check_provenance(server, head, plan)
    assert ok is False and "supprimé" in why
    server.tree_override = None
    server.spoofed[head] = [run(app=99, head=head)]
    # un faux check d'une autre application ne remplace pas le vrai ; sans vrai check il n'y a pas de provenance
    server.outcomes = {**GOOD, "green": (None, "accepted")}
    ok, why = fx.check_provenance(server, head, plan)
    assert ok is False and "non réussi" in why


def server_tree(fx, plan):
    return [
        {"path": p, "type": "blob", "mode": "100644", "sha": fx.git_blob_sha(t.encode())}
        for p, t in plan["files"].items()
    ]


def test_the_script_never_reads_a_credentials_file_nor_prints_the_token(fx):
    source = SCRIPT.read_text(encoding="utf-8")
    assert "GITHUB_TOKEN" in source and not re.search(r"(?<![A-Za-z_.])open\(", source)
    assert re.search(r"credentials?\.(json|txt)|\.openhands|\.config/gh|hosts\.yml", source) is None
    with pytest.raises(fx.FixtureError):
        fx.GitHubApi("")


# ── exécution RÉELLE du script de garde du workflow (bash) ────────────────────────────────────────────────────────────────


def _run_guard(fx, tmp_path, build):
    import subprocess

    workspace = tmp_path / "ws"
    (workspace / "ci").mkdir(parents=True)
    (workspace / ".git").mkdir()
    (workspace / "requirements.txt").write_text("fastapi==1\n")
    (workspace / "ci" / "requirements-approved.lock").write_text("fastapi==1 \\\n    --hash=sha256:" + "0" * 64 + "\n")
    build(workspace)
    guard = next(s for s in _steps(fx) if s.get("name", "").startswith("Garde"))["run"]
    return subprocess.run(
        ["bash", "-eo", "pipefail", "-c", guard],
        env={"PATH": os.environ["PATH"], "GITHUB_WORKSPACE": str(workspace)},
        capture_output=True,
        text=True,
    )


def test_the_guard_script_accepts_a_regular_tree_and_refuses_every_symlink_and_irregular_dependency_file(fx, tmp_path):
    assert _run_guard(fx, tmp_path / "a", lambda w: None).returncode == 0
    docker_socket = lambda w: (w / "docs").mkdir() or (w / "docs" / "sock").symlink_to("/var/run/docker.sock")  # noqa: E731
    refused = _run_guard(fx, tmp_path / "b", docker_socket)
    assert refused.returncode == 1 and "liens symboliques" in refused.stdout + refused.stderr

    def requirements_to_socket(w):
        (w / "requirements.txt").unlink()
        (w / "requirements.txt").symlink_to("/var/run/docker.sock")

    assert _run_guard(fx, tmp_path / "c", requirements_to_socket).returncode == 1

    def lock_missing(w):
        (w / "ci" / "requirements-approved.lock").unlink()

    missing = _run_guard(fx, tmp_path / "d", lock_missing)
    assert missing.returncode == 1 and "fichier régulier" in missing.stdout + missing.stderr

    def link_inside_git_only(w):
        (w / ".git" / "lien").symlink_to("/etc/hostname")

    assert _run_guard(fx, tmp_path / "e", link_inside_git_only).returncode == 0, (
        "les liens internes à .git ne sont pas montés"
    )


def test_the_workflow_satisfies_the_job_detection_rule_b_applies_to_approved_workflows(fx):
    """Réplique de ``_workflow_jobs`` de B (SHA ``13548f6``) : déclencheur ``pull_request`` ET job nommé comme le check requis."""
    document = _workflow(fx)
    triggers = document.get("on", document.get(True))
    triggered = list(triggers) if isinstance(triggers, dict) else [triggers]
    assert "pull_request" in triggered
    names = [str((job or {}).get("name") or key) for key, job in document["jobs"].items()]
    assert fx.REQUIRED_CHECK in names


def test_the_probe_requires_the_merge_guard_to_refuse_every_head_that_touches_a_protected_path(fx, plan):
    """Un check vert falsifié n'est pas une réussite : le garde de fusion (arbre Git réel de la tête) doit REFUSER la tête altérée."""
    server = ProbeServer(fx, plan, GOOD)
    result = probe(fx, plan, server)
    by = {r["scenario"]: r for r in result["results"]}
    assert result["ok"] is True
    for name in ("workflow-touch", "codeowners-touch", "lock-touch", "symlink"):
        assert by[name]["guard"]["refuses"] is True and by[name]["guard"]["violations"], name
    assert by["green"]["guard"] == {"refuses": False, "violations": []}
    assert by["unapproved-dependency"]["guard"]["refuses"] is False, (
        "requirements.txt n'est pas un chemin protégé : c'est le check qui le refuse"
    )
    # un garde aveugle (arbre toujours identique au socle) fait ÉCHOUER la contre-épreuve : le garde n'a pas refusé la tête altérée
    blind = ProbeServer(fx, plan, GOOD)
    blind.tree_override = server_tree(fx, plan)
    failed = probe(fx, plan, blind)
    assert failed["ok"] is False
    assert {r["scenario"] for r in failed["results"] if not r["ok"]} >= {
        "workflow-touch",
        "codeowners-touch",
        "lock-touch",
        "symlink",
    }


# ── journal append-only des identités distantes ───────────────────────────────────────────────────────────────────────────────


def _events(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_the_event_log_is_append_only_ordered_and_never_holds_content_or_a_token(fx, plan, tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_FAKE" + "0123456789abcdefghijklmnopqrstuvwxyz")
    path = tmp_path / "evidence" / "events.jsonl"
    remote = FakeGitHub(fx, plan)
    api = fx.journaling(remote, fx.EventLog(path))
    fx.apply_plan(api, plan, order_token=plan["order_token"], sleep=lambda _s: None, clock=Clock())
    events = _events(path)
    writes = [(e["method"], e["path"]) for e in events if e["event"] == "request"]
    assert writes == [(m, p) for m, p in remote.writes], "chaque écriture est journalisée EN INTENTION, dans l'ordre"
    kinds = [e["event"] for e in events]
    for index, event in enumerate(events):
        if event["event"] == "response":
            assert events[index - 1]["event"] == "request" and events[index - 1]["path"] == event["path"], (
                "intention AVANT réponse"
            )
    assert not [e for e in events if e["event"] == "error"]
    branch = next(e for e in events if e["event"] == "response" and e["path"].endswith("/git/refs"))
    assert branch["object.sha"] == plan["bootstrap_sha"]
    ruleset = next(e for e in events if e["event"] == "response" and e["path"].endswith("/rulesets"))
    assert ruleset["enforcement"] == "active" and isinstance(ruleset["id"], int)
    text = path.read_text(encoding="utf-8")
    assert "ghp_FAKE" not in text and '"content"' not in text
    assert plan["files"][fx.WORKFLOW_PATH][:40] not in text, "aucun contenu de fichier dans le journal"
    # append-only : un second journal sur le même fichier ajoute, n'écrase jamais
    fx.EventLog(path).record(event="note", text="suite")
    assert _events(path)[: len(events)] == events and _events(path)[-1]["event"] == "note"
    assert kinds.count("request") == kinds.count("response")


def test_the_event_log_records_an_error_with_status_and_reason_and_an_unanswered_intention_is_visible(
    fx, plan, tmp_path
):
    path = tmp_path / "events.jsonl"
    remote = FakeGitHub(fx, plan)
    remote.refs["refs/heads/collegue-business/bootstrap-w5"] = "f" * 40  # collision : création refusée par 422
    api = fx.journaling(remote, fx.EventLog(path))
    with pytest.raises(fx.ApiError):
        api(
            "POST",
            f"/repos/{fx.REPOSITORY}/git/refs",
            {"ref": "refs/heads/collegue-business/bootstrap-w5", "sha": "1" * 40},
        )
    events = _events(path)
    assert [e["event"] for e in events] == ["request", "error"] and events[1]["status"] == 422

    class Crash(Exception):
        pass

    def exploding(method, path, payload):
        raise Crash("résultat inconnu")

    with pytest.raises(Crash):
        fx.journaling(exploding, fx.EventLog(path))("POST", "/repos/x/pulls", {"title": "t"})
    assert _events(path)[-1]["event"] == "request", (
        "intention sans réponse : à relire avant de retenter (pas de doublon)"
    )


def test_the_probe_records_every_branch_pull_request_url_check_and_workflow_run_as_it_creates_them(fx, plan, tmp_path):
    path = tmp_path / "events.jsonl"
    server = ProbeServer(fx, plan, GOOD)
    log = fx.EventLog(path)
    ticks = iter(range(100_000))
    result = fx.run_probe(
        fx.journaling(server, log),
        plan,
        order_token=plan["order_token"],
        probe_id="probe-ev",
        sleep=no_sleep,
        clock=lambda: float(next(ticks)) * 100,
        log=log,
    )
    assert result["ok"] is True
    events = _events(path)
    prs = [e for e in events if e["event"] == "pull_request"]
    assert [e["scenario"] for e in prs] == [name for name in GOOD if name != "seed-base"]
    assert all(
        e["url"].startswith(f"https://github.com/{fx.REPOSITORY}/pull/") and isinstance(e["number"], int) for e in prs
    )
    observed = next(e for e in events if e["event"] == "observed_checks" and e["scenario"] == "green")
    assert (
        observed["check_runs"][0]["name"] == "Fixture tests" and observed["workflow_runs"][0]["event"] == "pull_request"
    )
    assert observed["workflow_runs"][0]["html_url"].endswith("/actions/runs/7000")
    created = [e["path"] for e in events if e["event"] == "response" and e["path"].endswith("/git/refs")]
    assert len(created) >= 14, "chaque branche de base et de tête créée est journalisée à sa création"
    results = [e for e in events if e["event"] == "scenario_result"]
    assert [e["scenario"] for e in results] == list(GOOD) and all(e["outcome"]["ok"] for e in results)
    deleted = [e for e in events if e["event"] == "response" and e["method"] == "DELETE"]
    assert deleted, "le nettoyage des ressources jetables est journalisé aussi"


def test_server_added_default_parameters_are_ignored_only_when_they_have_exactly_the_observed_default(fx, plan):
    """GitHub ajoute ``required_reviewers: []`` et ``require_extra_approval_for_unattributed_changes: true`` à la création (observé en C47)."""
    remote = json.loads(json.dumps(plan["ruleset"]))
    pr = next(r for r in remote["rules"] if r["type"] == "pull_request")["parameters"]
    pr["required_reviewers"] = []
    pr["require_extra_approval_for_unattributed_changes"] = True
    assert fx._normalize_ruleset(remote) == fx._normalize_ruleset(plan["ruleset"])
    pr["required_reviewers"] = [{"reviewer": {"id": 1, "type": "Team"}}]
    assert fx._normalize_ruleset(remote) != fx._normalize_ruleset(plan["ruleset"]), (
        "un réviseur ajouté n'est PAS un défaut serveur"
    )
    pr["required_reviewers"] = []
    pr["require_extra_approval_for_unattributed_changes"] = False
    assert fx._normalize_ruleset(remote) != fx._normalize_ruleset(plan["ruleset"])
    pr["require_extra_approval_for_unattributed_changes"] = True
    pr["require_code_owner_review"] = False
    assert fx._normalize_ruleset(remote) != fx._normalize_ruleset(plan["ruleset"]), (
        "un affaiblissement réel reste un écart"
    )
    pr["require_code_owner_review"] = True
    pr["some_new_parameter"] = True
    assert fx._normalize_ruleset(remote) != fx._normalize_ruleset(plan["ruleset"]), (
        "tout autre paramètre ajouté reste un écart"
    )
