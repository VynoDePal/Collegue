"""Socle, protection et contre-épreuves du dépôt fixture W5 (propriété C) : tout est vérifiable HORS LIGNE.

* le commit du socle calculé par le script est IDENTIQUE à celui de ``git`` réel (même arbre, même SHA) et l'arbre de la graine
  recalculé est celui du dépôt distant (``c8bffa32…``) ;
* le workflow de confiance, le ruleset et le manifeste ont le contenu exigé (statique) ;
* ``apply`` / ``cleanup`` / ``probe`` sont éprouvés sur un faux serveur GitHub EN MÉMOIRE : jeton d'ordre, idempotence, collisions,
  ressources non possédées, ``main`` et PR étrangères intacts, nettoyage restreint aux ressources de la campagne ;
* l'évaluation des checks et le contrôle d'intégrité (fonctions pures utilisables par B) refusent faux succès, autre application,
  autre tête, check manquant et workflow altéré.

Rien de tout cela ne prouve le comportement de GitHub Actions : la contre-épreuve réelle (``probe``) n'est exécutable que sur ordre.
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
    seed_tree_files = {p: t for p, t in plan["files"].items() if p in fx.SEED_PATHS}
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


def test_the_scaffold_adds_only_the_workflow_and_the_two_example_runbooks_and_never_edits_the_seed(fx, plan):
    assert plan["created_files"] == [
        ".github/workflows/fixture-tests.yml",
        "docs/deploiement.md",
        "docs/runbook-ops.md",
    ]
    assert plan["modified_seed_files"] == []
    assert list(plan["approved_files"]) == plan["created_files"], "approved_files = exactement ce que le socle ajoute"
    seed = fx.load_seed()
    assert not set(plan["approved_files"]) & set(seed)
    assert all(plan["files"][path] == text for path, text in seed.items()), "la graine est conservée octet pour octet"
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
    assert "AKIAIOSFODNN7EXAMPLE" in plan["files"]["docs/deploiement.md"]
    assert "AKIAI44QH8DHBEXAMPLE" in plan["files"]["docs/runbook-ops.md"]
    assert both.count("EXAMPLE") >= 4, "uniquement des identifiants d'EXEMPLE publiés (distincts entre R04 et R05)"


def test_the_scaffold_satisfies_the_closed_shape_b_validates_against_the_real_tree(fx, plan):
    safe = re.compile(r"[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)*")
    for path, digest in plan["approved_files"].items():
        assert safe.fullmatch(path) and (not path.startswith(".") or path.startswith(".github/"))
        assert re.fullmatch(r"[0-9a-f]{64}", digest)


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
    producer = manifest["check_producer"]
    assert producer["trigger"] == "pull_request_target" and producer["publishes"] == "Fixture tests"
    assert producer["workflow"] in manifest["approved_files"] and producer["app_id"] == manifest["check_app_id"]
    assert producer["job"] != manifest["required_check"], "le check requis n'est pas le check automatique d'un job"


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
    diff = (tmp_path / "plan" / "bootstrap.diff").read_text(encoding="utf-8")
    assert "+++ b/.github/workflows/fixture-tests.yml" in diff and "app/main.py" not in diff


# ── workflow de confiance et ruleset (statique) ───────────────────────────────────────────────────────────────────────────


def _workflow(fx):
    return yaml.safe_load(fx.TRUSTED_WORKFLOW)


def test_the_trusted_workflow_is_judged_from_the_base_and_never_runs_candidate_code_on_the_host(fx):
    wf = _workflow(fx)
    on = wf.get(True) or wf.get("on")  # PyYAML lit « on » comme un booléen
    assert set(on) == {"pull_request_target"} and on["pull_request_target"]["branches"] == ["collegue-business/**"]
    assert wf["permissions"] == {"contents": "read", "pull-requests": "read", "checks": "write"}
    job = wf["jobs"]["fixture-runner"]
    assert job["name"] == "Fixture runner" != fx.REQUIRED_CHECK, (
        "le check automatique (porté par la base) ne peut pas être confondu avec le check requis"
    )
    steps = job["steps"]
    checkout = next(s for s in steps if str(s.get("uses", "")).startswith("actions/checkout"))
    assert steps.index(checkout) == 0
    assert (
        checkout["with"]["ref"] == "${{ github.event.pull_request.head.sha }}"
        and checkout["with"]["persist-credentials"] is False
    )
    text = fx.TRUSTED_WORKFLOW
    assert "secrets." not in text, "aucun secret du dépôt n'est référencé"
    assert "github.event.pull_request.title" not in text and "github.event.pull_request.body" not in text
    # aucune installation ni exécution sur l'hôte : tout passe par des conteneurs sans privilège
    host_runs = "\n".join(s.get("run", "") for s in steps)
    assert "pip install" not in host_runs.replace("python -m pip install", "").replace(
        "/tmp/venv/bin/python -m pip install", ""
    )
    assert "pytest" not in host_runs.replace("/tmp/venv/bin/python -m pytest", "")
    assert "sudo" not in host_runs and "docker.sock" not in host_runs
    download = next(s for s in steps if s.get("id") == "wheels")["run"]
    tests = next(s for s in steps if s.get("id") == "tests")["run"]
    for command in (download, tests):
        for flag in ("--user 65534:65534", "--cap-drop ALL", "--security-opt no-new-privileges", "--read-only"):
            assert flag in command
        assert "docker.sock" not in command and "--privileged" not in command and "-e GH_TOKEN" not in command
    assert "--only-binary=:all:" in download and "--network none" not in download
    assert "--network none" in tests and "--no-index" in tests, (
        "installation et tests sans réseau, depuis les roues téléchargées"
    )
    assert '"$GITHUB_WORKSPACE:/src:ro"' in tests


def test_the_workflow_hands_the_token_only_to_the_guard_and_the_publisher_never_to_a_candidate_container(fx):
    steps = _workflow(fx)["jobs"]["fixture-runner"]["steps"]
    with_token = [s["name"] for s in steps if "GH_TOKEN" in (s.get("env") or {})]
    assert len(with_token) == 2 and with_token[0].startswith("Garde") and with_token[1].startswith("Publier")
    job_env = _workflow(fx)["jobs"]["fixture-runner"]["env"]
    assert not any("token" in k.lower() or "secret" in k.lower() for k in job_env)


def test_the_workflow_refuses_a_pr_that_modifies_dot_github_and_publishes_the_required_check_on_the_exact_head(fx):
    steps = _workflow(fx)["jobs"]["fixture-runner"]["steps"]
    guard = next(s for s in steps if s.get("id") == "guard")
    assert (
        "pulls/${PR_NUMBER}/files" in guard["run"] and "previous_filename" in guard["run"] and "exit 1" in guard["run"]
    )
    publish = steps[-1]
    assert publish["if"] == "${{ !cancelled() }}", "publié même si une étape a échoué (un échec est un check rouge)"
    run = publish["run"]
    assert 'name="Fixture tests"' in run and 'head_sha="$HEAD_SHA"' in run and "check-runs" in run
    assert run.count("success") >= 4 and 'test "$conclusion" = success' in run
    assert (
        'if [ "$GUARD" = success ] && [ "$WHEELS" = success ] && [ "$TESTS" = success ]; then conclusion=success; fi'
        in run
    ), "succès seulement si garde, roues ET tests ont réussi"
    assert _workflow(fx)["jobs"]["fixture-runner"]["env"]["HEAD_SHA"] == "${{ github.event.pull_request.head.sha }}"


def test_the_ruleset_is_active_without_bypass_and_binds_the_check_to_the_actions_app(fx, plan):
    rs = plan["ruleset"]
    assert rs["enforcement"] == "active" and rs["bypass_actors"] == [] and rs["target"] == "branch"
    assert rs["conditions"]["ref_name"] == {"include": ["refs/heads/collegue-business/*"], "exclude": []}
    types = {r["type"] for r in rs["rules"]}
    assert types == {"pull_request", "required_status_checks"}, (
        "pas de règle deletion : le nettoyage supprime ses propres bases"
    )
    checks = next(r for r in rs["rules"] if r["type"] == "required_status_checks")["parameters"]
    assert checks["strict_required_status_checks_policy"] is True
    assert checks["do_not_enforce_on_create"] is True, (
        "créer une base éphémère depuis le socle ne peut pas exiger un check déjà passé ; les FUSIONS restent protégées"
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


# ── évaluation des checks et intégrité (fonctions pures) ──────────────────────────────────────────────────────────────────

HEAD = "a" * 40


def run(name="Fixture tests", app=15368, head=HEAD, status="completed", conclusion="success"):
    return {"name": name, "app": {"id": app}, "head_sha": head, "status": status, "conclusion": conclusion}


@pytest.mark.parametrize(
    "runs, expected",
    [
        ([run()], "success"),
        ([run(conclusion="failure")], "failure"),  # test rouge -> check rouge
        ([run(conclusion="cancelled")], "failure"),
        ([run(status="in_progress", conclusion=None)], "pending"),
        ([], "missing"),  # check manquant
        ([run(app=99999)], "missing"),  # faux succès d'une AUTRE application
        ([run(name="fixture tests")], "missing"),  # autre nom
        ([run(head="b" * 40)], "missing"),  # succès d'une AUTRE tête
        ([run(), run(conclusion="failure")], "failure"),  # un homonyme rouge suffit
        ([run(), run(app=99999, conclusion="failure")], "success"),  # un autre émetteur ne retire rien
    ],
)
def test_check_evaluation_never_turns_a_wrong_check_into_a_success(fx, runs, expected):
    assert fx.evaluate_check(runs, head_sha=HEAD)[0] == expected


def test_integrity_flags_a_modified_removed_or_added_protected_file_only(fx, plan):
    tree = dict(plan["approved_files"])
    assert fx.integrity_violations(tree, plan["approved_files"]) == []
    tree[fx.WORKFLOW_PATH] = "0" * 64
    assert any("modifié" in m for m in fx.integrity_violations(tree, plan["approved_files"]))
    del tree[fx.WORKFLOW_PATH]
    assert any("supprimé" in m for m in fx.integrity_violations(tree, plan["approved_files"]))
    tree[fx.WORKFLOW_PATH] = plan["approved_files"][fx.WORKFLOW_PATH]
    tree[".github/workflows/evil.yml"] = "1" * 64
    assert any("ajouté" in m for m in fx.integrity_violations(tree, plan["approved_files"]))
    tree = dict(plan["approved_files"])
    tree["app/main.py"] = "2" * 64  # le code métier n'est PAS protégé : c'est ce que la campagne produit
    tree["app/new.py"] = "3" * 64
    assert fx.integrity_violations(tree, plan["approved_files"]) == []


# ── faux serveur GitHub en mémoire ────────────────────────────────────────────────────────────────────────────────────────


class FakeGitHub:
    """Modélise seulement ce dont le script a besoin ; chaque écriture est journalisée pour prouver ce qui n'a PAS été touché."""

    def __init__(self, fx, plan, *, main_sha=None):
        self.fx, self.plan = fx, plan
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
        self.check_runs = {}
        self.blobs: dict = {}
        self.contents_by_ref: dict = {}

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


def test_apply_without_the_exact_order_token_writes_nothing(fx, plan, remote):
    for token in (None, "", "APPLIQUER-W5-FIXTURE-000000000000", plan["order_token"] + "x"):
        with pytest.raises(fx.FixtureError, match="jeton d'ordre"):
            fx.apply_plan(remote, plan, order_token=token)
    assert remote.writes == []


def test_apply_creates_the_bootstrap_branch_and_the_ruleset_and_never_touches_main_or_foreign_resources(
    fx, plan, remote
):
    result = fx.apply_plan(remote, plan, order_token=plan["order_token"])
    assert result["created"] == ["branch", "ruleset"] and result["idempotent"] is False
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


def test_apply_is_idempotent_and_a_second_run_writes_nothing(fx, plan, remote):
    fx.apply_plan(remote, plan, order_token=plan["order_token"])
    writes = len(remote.writes)
    again = fx.apply_plan(remote, plan, order_token=plan["order_token"])
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
        fx.apply_plan(remote, plan, order_token=plan["order_token"])
    del remote.rulesets[555]
    remote.refs["refs/heads/collegue-business/bootstrap-w5"] = "f" * 40
    with pytest.raises(fx.FixtureError, match="ressource non possédée"):
        fx.apply_plan(remote, plan, order_token=plan["order_token"])
    del remote.refs["refs/heads/collegue-business/bootstrap-w5"]
    remote.rulesets[600] = {
        **json.loads(json.dumps(plan["ruleset"])),
        "id": 600,
        "bypass_actors": [{"actor_id": 1, "actor_type": "RepositoryRole"}],
    }
    with pytest.raises(fx.FixtureError, match="autre contenu"):
        fx.apply_plan(remote, plan, order_token=plan["order_token"])
    assert remote.writes == []


def test_apply_refuses_when_main_is_no_longer_the_seed_or_the_identity_changes(fx, plan):
    moved = FakeGitHub(fx, plan, main_sha="c" * 40)
    with pytest.raises(fx.FixtureError, match="graine immuable"):
        fx.apply_plan(moved, plan, order_token=plan["order_token"])
    assert moved.writes == []
    inactive = FakeGitHub(fx, plan)
    inactive.rulesets[fx.SEED_RULESET_ID]["enforcement"] = "disabled"
    with pytest.raises(fx.FixtureError, match="ruleset de la graine"):
        fx.apply_plan(inactive, plan, order_token=plan["order_token"])
    assert inactive.writes == []


def test_verify_detects_a_tampered_branch_file_or_ruleset(fx, plan, remote):
    fx.apply_plan(remote, plan, order_token=plan["order_token"])
    tampered = FakeGitHub(fx, plan)
    fx.apply_plan(tampered, plan, order_token=plan["order_token"])
    rid = next(i for i, r in tampered.rulesets.items() if r["name"] == fx.RULESET_NAME)
    tampered.rulesets[rid]["bypass_actors"] = [{"actor_id": 5, "actor_type": "RepositoryRole"}]
    result = fx.verify_remote(tampered, plan)
    assert not result["ok"] and any("ruleset différent" in p for p in result["problems"])
    moved = FakeGitHub(fx, plan)
    fx.apply_plan(moved, plan, order_token=plan["order_token"])
    moved.refs["refs/heads/collegue-business/bootstrap-w5"] = "d" * 40
    assert any("au lieu de" in p for p in fx.verify_remote(moved, plan)["problems"])
    assert not fx.verify_remote(FakeGitHub(fx, plan), plan)["ok"], (
        "rien d'appliqué : vérification rouge, pas verte par défaut"
    )


def test_cleanup_removes_only_this_campaigns_bootstrap_resources(fx, plan, remote):
    fx.apply_plan(remote, plan, order_token=plan["order_token"])
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
    fx.apply_plan(remote, plan, order_token=plan["order_token"])
    remote.refs["refs/heads/collegue-business/bootstrap-w5"] = "9" * 40
    remote.writes.clear()
    with pytest.raises(fx.FixtureError, match="non possédée"):
        fx.cleanup_bootstrap(remote, plan, order_token=plan["order_token"])
    assert not any(m == "DELETE" and "git/refs" in p for m, p in remote.writes)


# ── probe : contre-épreuves du check (simulées) ───────────────────────────────────────────────────────────────────────────


class ProbeServer(FakeGitHub):
    """Ajoute pulls, contenus et check-runs : le comportement de chaque scénario est INJECTÉ pour éprouver l'évaluation du probe."""

    def __init__(self, fx, plan, outcomes):
        super().__init__(fx, plan)
        self.outcomes = outcomes  # scénario -> (conclusion | None, mergeable_state)
        self.counter = 0
        self.heads = {}
        self.prs = {}
        self.spoof_mode = "refused"  # refused | foreign (acceptée, autre application) | counted (acceptée, application Actions : défaut)
        self.spoofed = {}
        fx.apply_plan(self, plan, order_token=plan["order_token"])
        self.writes.clear()

    def __call__(self, method, path, payload):
        repo = f"/repos/{self.fx.REPOSITORY}"
        if method == "PUT" and path.startswith(f"{repo}/contents/"):
            self.writes.append((method, path))
            self.counter += 1
            sha = f"{self.counter:040x}"
            self.heads[payload["branch"]] = sha
            return {"commit": {"sha": sha}}
        if method == "POST" and path == f"{repo}/pulls":
            self.writes.append((method, path))
            number = 100 + len(self.prs)
            self.prs[number] = {"head": payload["head"], "base": payload["base"], "state": "open"}
            self.pulls[number] = self.prs[number]
            return {"number": number}
        if method == "POST" and path == f"{repo}/check-runs":
            self.writes.append((method, path))
            if self.spoof_mode == "refused":
                raise self.fx.ApiError(403, "Resource not accessible by personal access token")
            self.spoofed.setdefault(payload["head_sha"], []).append(
                {
                    "name": payload["name"],
                    "app": {"id": 15368 if self.spoof_mode == "counted" else 99},
                    "head_sha": payload["head_sha"],
                    "status": "completed",
                    "conclusion": payload["conclusion"],
                }
            )
            return {"id": 1}
        m = re.fullmatch(rf"{repo}/commits/([0-9a-f]{{40}})/check-runs", path)
        if m:
            head = next(h for h, s in self.heads.items() if s == m.group(1))
            scenario = head.rsplit("-", 1)[-1]
            conclusion, _state = self.outcomes[int(scenario)]
            runs = list(self.spoofed.get(m.group(1), []))
            if conclusion is not None:
                runs.append(
                    {
                        "name": "Fixture tests",
                        "app": {"id": 15368},
                        "head_sha": m.group(1),
                        "status": "completed",
                        "conclusion": conclusion,
                    }
                )
            return {"check_runs": runs}
        m = re.fullmatch(rf"{repo}/pulls/(\d+)", path)
        if m and method == "GET":
            pr = self.prs[int(m.group(1))]
            idx = int(pr["head"].rsplit("-", 1)[-1])
            return {"mergeable_state": self.outcomes[idx][1], "state": pr["state"]}
        if method == "GET" and "/contents/tests/test_probe.py" in path:
            raise self.fx.ApiError(404, "Not Found")
        return super().__call__(method, path, payload)


GOOD = {0: ("success", "clean"), 1: ("failure", "blocked"), 2: ("failure", "blocked"), 3: (None, "blocked")}


def no_sleep(_seconds):
    return None


def test_the_probe_passes_when_each_counter_proof_behaves_and_cleans_up_everything(fx, plan):
    server = ProbeServer(fx, plan, GOOD)
    ticks = iter(range(10_000))
    result = fx.run_probe(
        server,
        plan,
        order_token=plan["order_token"],
        probe_id="probe-1",
        sleep=no_sleep,
        clock=lambda: float(next(ticks)) * 100,
    )
    assert result["ok"] is True, result
    assert [r["scenario"] for r in result["results"]] == ["green", "red-test", "forged-workflow", "missing-check"]
    assert not [r for r in server.refs if "probe" in r], "toutes les branches de sonde sont supprimées"
    assert all(pr["state"] == "closed" for pr in server.prs.values())
    red = result["results"][1]
    assert red["spoof"]["accepted_by_api"] is False and red["spoof"]["counted"] is False
    assert red["checks_seen"] == [{"name": "Fixture tests", "app_id": 15368, "head_sha": red["head_sha"]}]
    assert server.refs["refs/heads/collegue-business/bootstrap-w5"] == plan["bootstrap_sha"]


@pytest.mark.parametrize(
    "bad",
    [
        {1: ("success", "clean")},  # un test rouge qui donnerait un check vert
        {2: ("success", "clean")},  # un workflow altéré qui donnerait un faux succès
        {3: ("success", "clean")},  # un check qui apparaît là où il ne devait pas exister
        {0: ("failure", "blocked")},  # le nominal rouge
        {1: ("failure", "clean")},  # check rouge mais PR fusionnable
    ],
)
def test_the_probe_fails_on_a_false_success_or_a_mergeable_red_pr(fx, plan, bad):
    server = ProbeServer(fx, plan, {**GOOD, **bad})
    ticks = iter(range(10_000))
    result = fx.run_probe(
        server,
        plan,
        order_token=plan["order_token"],
        probe_id="probe-2",
        sleep=no_sleep,
        clock=lambda: float(next(ticks)) * 100,
    )
    assert result["ok"] is False
    assert not [r for r in server.refs if "probe" in r], "même en échec, rien ne reste"


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


def test_the_script_never_reads_a_credentials_file_nor_prints_the_token(fx):
    source = SCRIPT.read_text(encoding="utf-8")
    assert "GITHUB_TOKEN" in source and not re.search(r"(?<![A-Za-z_.])open\(", source)
    assert re.search(r"credentials?\.(json|txt)|\.openhands|\.config/gh|hosts\.yml", source) is None
    with pytest.raises(fx.FixtureError):
        fx.GitHubApi("")


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
    ticks = iter(range(10_000))
    result = fx.run_probe(
        server,
        plan,
        order_token=plan["order_token"],
        probe_id="probe-4",
        sleep=no_sleep,
        clock=lambda: float(next(ticks)) * 100,
    )
    assert result["ok"] is ok, result
    assert not [r for r in server.refs if "probe" in r]
