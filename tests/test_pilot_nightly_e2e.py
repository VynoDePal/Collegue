"""Tests hors réseau du driver nightly produit (#404)."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from collegue.pilot.nightly_e2e import (
    CommandResult,
    NightlyClients,
    NightlyConfig,
    NightlyE2EError,
    NightlyE2ERunner,
    NightlyManifest,
    _write_manifest,
)
from collegue.tools.base import ToolExecutionError

SEED = "a" * 40
BASE_SHA = "b" * 40
HEAD_SHA = "c" * 40
BASE_OWNER_SHA = "d" * 40
ORACLE_SHA = "e" * 64
ROOT_TREE_SHA = "1" * 40


def _env(tmp_path, **overrides):
    values = {
        "COLLEGUE_NIGHTLY_E2E": "true",
        "INTEGRATION_FIXTURE_REPOSITORY": "fixture/app",
        "INTEGRATION_FIXTURE_REPOSITORY_ID": "4242",
        "INTEGRATION_FIXTURE_ROOT_BRANCH": "main",
        "INTEGRATION_FIXTURE_SEED_SHA": SEED,
        "GITHUB_REPOSITORY": "VynoDePal/Collegue",
        "GITHUB_RUN_ID": "12345",
        "GITHUB_RUN_ATTEMPT": "2",
        "COLLEGUE_NIGHTLY_MANIFEST": str(tmp_path / "manifest.json"),
        "GATE_ACCEPTANCE_TESTS": "true",
        "GATE_SMOKE_RUN": "true",
        "GATE_SMOKE_PATHS": "/nightly",
        "REQUIRE_COST_PRICING": "true",
        "BUILD_AUTO_MERGE": "false",
        "AUTO_MERGE_ENABLED": "false",
        "BUDGET_EXHAUSTED_ACTION": "pause",
        "MAX_COST_USD": "2",
        "MAX_TOKENS_BUDGET": "200000",
        "LLM_CALL_TIMEOUT": "180",
        "COLLEGUE_RUN_DEADLINE_SECONDS": "900",
        "COLLEGUE_NIGHTLY_COMMAND_TIMEOUT_SECONDS": "2700",
        "SANDBOX_TIMEOUT": "720",
        "TASK_MAX_ATTEMPTS": "1",
        "LLM_API_KEY": "secret-llm",
        "LLM_PROVIDER": "gemini",
        "LLM_MODEL": "gemini-2.5-flash",
        "LLM_PRICE_PROMPT_PER_1M": "0.1",
        "LLM_PRICE_COMPLETION_PER_1M": "0.2",
        "GITHUB_TOKEN": "secret-github",
        "COLLEGUE_HOME": str(tmp_path / "home"),
        "STATE_DATABASE_URL": f"sqlite:///{tmp_path / 'state.db'}",
        "SANDBOX_IMAGE": "collegue-sandbox-openhands:ci",
    }
    values.update(overrides)
    return values


def test_config_is_opt_in_bounded_and_does_not_repr_token(tmp_path):
    config = NightlyConfig.from_env(_env(tmp_path))
    assert config.repository == "fixture/app"
    assert config.base_branch == "collegue-nightly/12345-2"
    assert config.issue_label == "collegue-nightly-12345-2"
    assert "secret-github" not in repr(config)


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"COLLEGUE_NIGHTLY_E2E": "false"}, "opt-in"),
        ({"INTEGRATION_FIXTURE_REPOSITORY": "VynoDePal/Collegue"}, "distinct"),
        ({"INTEGRATION_FIXTURE_SEED_SHA": "short"}, "SHA Git complet"),
        ({"BUILD_AUTO_MERGE": "true"}, "explicitement false"),
        ({"BUILD_AUTO_MERGE": ""}, "explicitement false"),
        ({"GATE_ACCEPTANCE_TESTS": "false"}, "GATE_ACCEPTANCE_TESTS"),
        ({"GATE_SMOKE_PATHS": "/"}, "exactement /nightly"),
        ({"LLM_PROVIDER": "openai"}, "provider global gemini"),
        ({"LLM_PROVIDER_QA": "openai"}, "LLM_PROVIDER_QA"),
        ({"LLM_MODEL": "openai/gpt-5.4"}, "modèle Gemini API non préfixé"),
        ({"LLM_MODEL_QA": "models/gemini-2.5-flash"}, "modèle Gemini API non préfixé"),
        ({"LLM_MODEL_QA": "gemma-4-inconnu"}, "modèle Gemini API non préfixé"),
        ({"LLM_MODEL_REVIEWER": "gemini-inconnu"}, "tarif explicite"),
        ({"LLM_PRICE_PROMPT_PER_1M": "0", "LLM_PRICE_COMPLETION_PER_1M": "0"}, "prix LLM"),
        ({"MAX_COST_USD": "0"}, "doit être fini"),
        ({"MAX_COST_USD": "inf"}, "doit être fini"),
        ({"LLM_CALL_TIMEOUT": "inf"}, "doit être fini"),
        ({"COLLEGUE_RUN_DEADLINE_SECONDS": "inf"}, "doit être fini"),
        ({"SANDBOX_TIMEOUT": "1801"}, "doit être fini"),
        ({"TASK_MAX_ATTEMPTS": "3"}, "doit valoir 1"),
        ({"COLLEGUE_NIGHTLY_MANIFEST": "relative.json"}, "chemin absolu"),
    ],
)
def test_config_refuses_unsafe_or_unbounded_modes(tmp_path, override, message):
    with pytest.raises(NightlyE2EError, match=message):
        NightlyConfig.from_env(_env(tmp_path, **override))


@pytest.mark.parametrize("model", ["gemma-4-31b-it", "gemma-4-26b-a4b-it"])
def test_config_accepts_explicitly_free_gemma_4_without_fallback_prices(tmp_path, model):
    config = NightlyConfig.from_env(
        _env(
            tmp_path,
            LLM_MODEL=model,
            LLM_PRICE_PROMPT_PER_1M="0",
            LLM_PRICE_COMPLETION_PER_1M="0",
        )
    )

    assert config.repository == "fixture/app"


def test_config_accepts_free_coder_override_with_paid_other_roles(tmp_path):
    config = NightlyConfig.from_env(
        _env(
            tmp_path,
            LLM_MODEL="gemini-2.5-flash",
            LLM_MODEL_CODER="gemma-4-26b-a4b-it",
            LLM_PRICE_PROMPT_PER_1M="0",
            LLM_PRICE_COMPLETION_PER_1M="0",
        )
    )

    assert config.repository == "fixture/app"


def test_cleanup_config_does_not_require_llm_or_run_policy(tmp_path):
    env = _env(
        tmp_path,
        LLM_API_KEY="",
        LLM_PROVIDER="",
        LLM_MODEL="",
        GATE_ACCEPTANCE_TESTS="false",
        BUILD_AUTO_MERGE="true",
    )

    config = NightlyConfig.from_env(env, action="cleanup")

    assert config.token == "secret-github"


class _Repos:
    def __init__(self, *, repo_id=4242, full_name="fixture/app", private=False):
        self.repo_id = repo_id
        self.full_name = full_name
        self.private = private

    def get_repo(self, owner, repo):
        return SimpleNamespace(
            id=self.repo_id,
            full_name=self.full_name,
            is_private=self.private,
            default_branch="main",
        )


class _Files:
    def __init__(self):
        self.spec = None

    def get_file_content(self, owner, repo, path, branch=None):
        if path == ".collegue-nightly-fixture":
            return {"content": "COLLEGUE_NIGHTLY_FIXTURE_V1\n"}
        if path == "SPEC.md" and self.spec is not None:
            return {"content": self.spec}
        raise ToolExecutionError("fichier absent")


class _Branches:
    def __init__(self):
        self.refs = {"main": SEED}
        self.deleted = []
        self.commits = {
            SEED: SimpleNamespace(
                sha=SEED,
                tree_sha=ROOT_TREE_SHA,
                parents=[],
                message="fixture seed",
            )
        }

    def get_branch_sha(self, owner, repo, branch):
        if branch not in self.refs:
            raise ToolExecutionError("branche absente", status_code=404)
        return self.refs[branch]

    def create_branch(self, owner, repo, branch, from_branch):
        if branch in self.refs:
            raise ToolExecutionError("branche déjà présente")
        self.refs[branch] = self.refs[from_branch]
        return SimpleNamespace(name=branch, commit_sha=self.refs[branch])

    def ensure_commit_branch(self, owner, repo, branch, *, parent_sha, tree_sha, message):
        if branch in self.refs:
            current = self.refs[branch]
            commit = self.commits.get(current)
            if (
                commit is None
                or commit.parents != [parent_sha]
                or commit.tree_sha != tree_sha
                or commit.message != message
            ):
                raise ToolExecutionError("branche propriétaire divergente")
            return SimpleNamespace(name=branch, commit_sha=current)
        self.commits[BASE_OWNER_SHA] = SimpleNamespace(
            sha=BASE_OWNER_SHA,
            tree_sha=tree_sha,
            parents=[parent_sha],
            message=message,
        )
        self.refs[branch] = BASE_OWNER_SHA
        return SimpleNamespace(name=branch, commit_sha=BASE_OWNER_SHA)

    def get_git_commit(self, owner, repo, sha):
        if sha not in self.commits:
            raise ToolExecutionError("commit absent")
        return self.commits[sha]

    def delete_branch(self, owner, repo, branch, *, default_branch, expected_sha):
        if branch == default_branch:
            raise AssertionError("suppression de la branche par défaut")
        if self.refs.get(branch) != expected_sha:
            raise ToolExecutionError("SHA déplacé")
        self.refs.pop(branch)
        self.deleted.append((branch, expected_sha))
        return True


class _PRs:
    def __init__(self):
        self.items = {}
        self.closed = []

    def list_prs(self, owner, repo, state="open", limit=30, *, base=None):
        return [
            item for item in self.items.values() if item.state == state and (base is None or item.base_branch == base)
        ]

    def get_pr(self, owner, repo, number):
        return self.items[number]

    def close_pr(
        self,
        owner,
        repo,
        number,
        *,
        expected_head_sha,
        expected_head_branch,
        expected_base_branch,
        body_marker,
    ):
        item = self.items[number]
        assert item.head_sha == expected_head_sha
        assert item.head_branch == expected_head_branch
        assert item.base_branch == expected_base_branch
        assert body_marker in item.body
        item.state = "closed"
        self.closed.append(number)
        return item


class _Issues:
    def __init__(self):
        self.items = {}
        self.closed = []

    def list_issues(self, owner, repo, state="open", limit=30, *, labels=None):
        return [
            item for item in self.items.values() if item.state == state and (labels is None or labels in item.labels)
        ]

    def get_issue(self, owner, repo, number):
        return self.items[number]

    def close_issue(self, owner, repo, number, *, expected_labels, body_marker):
        item = self.items[number]
        assert set(expected_labels).issubset(item.labels)
        assert body_marker in item.body
        item.state = "closed"
        self.closed.append(number)
        return item


class _Labels:
    def __init__(self):
        self.items = {}
        self.deleted = []
        self.ensure_calls = []
        self.fail_after_create = False
        self.on_ensure = None

    def list_labels(self, owner, repo):
        return list(self.items.values())

    def ensure_label(self, owner, repo, name, color="ededed", description=None):
        self.ensure_calls.append(name)
        if self.on_ensure is not None:
            self.on_ensure()
        existing = next(
            (label for label in self.items.values() if label.name.lower() == name.lower()),
            None,
        )
        if existing is None:
            existing = SimpleNamespace(name=name, color=color, description=description)
            self.items[name] = existing
        if self.fail_after_create:
            raise ToolExecutionError("réponse ensure_label perdue")
        return existing

    def delete_label(
        self,
        owner,
        repo,
        name,
        *,
        expected_name,
        expected_color=None,
        expected_description=None,
    ):
        assert name == expected_name
        existing = self.items.get(name)
        if existing is not None:
            assert expected_color is None or existing.color.lower() == expected_color.lower()
            assert expected_description is None or existing.description == expected_description
        self.deleted.append(name)
        self.items.pop(name, None)
        return True


def _pr(config, number=88, issue_number=77):
    return SimpleNamespace(
        number=number,
        state="open",
        merged=False,
        base_branch=config.base_branch,
        head_branch=f"collegue/issue-{issue_number}",
        head_sha=HEAD_SHA,
        base_sha=BASE_SHA,
        body=(
            f"<!-- collegue-exec:{issue_number} -->\n"
            f"<!-- collegue-diff-sha256:{'d' * 64} -->\n"
            f"**Tests d'acceptation (§4.7)** : ✅ réussis — oracle `sha256:{ORACLE_SHA}`"
        ),
        changed_files=2,
    )


def _issue(config, number=77, task_id=None, *, project_id=None, plan_hash=None):
    task_id = number if task_id is None else task_id
    body = f"critère\n\n<!-- collegue-task:{task_id} -->"
    if project_id is not None and plan_hash is not None:
        body += f"\n\n<!-- collegue-plan:{plan_hash};project:{project_id};task:{task_id} -->"
    return SimpleNamespace(
        number=number,
        state="open",
        labels=[config.issue_label],
        body=body,
    )


def _runner(tmp_path, *, repos=None):
    config = NightlyConfig.from_env(_env(tmp_path))
    branches = _Branches()
    prs = _PRs()
    issues = _Issues()
    labels = _Labels()
    files = _Files()
    clients = NightlyClients(
        repos=repos or _Repos(),
        files=files,
        branches=branches,
        prs=prs,
        issues=issues,
        labels=labels,
    )
    return NightlyE2ERunner(config, clients=clients), branches, prs, issues, files


def _own_run_label(runner, manifest):
    runner._create_owned_label(manifest)
    assert manifest.label_creation_started is True
    assert manifest.label_created is True


def test_preexisting_run_label_is_never_adopted_or_deleted(tmp_path):
    runner, _branches, _prs, _issues, _files = _runner(tmp_path)
    cfg = runner.config
    labels = runner.clients.labels
    labels.items[cfg.issue_label] = SimpleNamespace(
        name=cfg.issue_label,
        color="abcdef",
        description="créé avant le run",
    )
    manifest = NightlyManifest.for_config(cfg)
    _write_manifest(cfg.manifest_path, manifest)

    with pytest.raises(NightlyE2EError, match="refus de l'adopter"):
        runner._create_owned_label(manifest)

    assert manifest.label_creation_started is False
    assert labels.ensure_calls == []
    assert runner.cleanup() == {"status": "clean", "closed_prs": [], "closed_issues": []}
    assert cfg.issue_label in labels.items
    assert labels.deleted == []


def test_lost_ensure_label_response_is_recovered_from_exact_owned_name(tmp_path):
    runner, _branches, _prs, _issues, _files = _runner(tmp_path)
    cfg = runner.config
    labels = runner.clients.labels
    checkpoints = []
    labels.fail_after_create = True
    labels.on_ensure = lambda: checkpoints.append(json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8")))
    manifest = NightlyManifest.for_config(cfg)
    _write_manifest(cfg.manifest_path, manifest)

    runner._create_owned_label(manifest)

    assert checkpoints[0]["label_creation_started"] is True
    assert checkpoints[0]["label_created"] is False
    assert manifest.label_created is True
    assert labels.items[cfg.issue_label].description == runner._label_owner_description()
    assert runner.cleanup()["status"] == "clean"
    assert labels.deleted == [cfg.issue_label]


def test_cleanup_recovers_owned_label_created_before_checkpoint(tmp_path):
    runner, _branches, _prs, _issues, _files = _runner(tmp_path)
    cfg = runner.config
    labels = runner.clients.labels
    manifest = NightlyManifest.for_config(cfg)
    manifest.label_creation_started = True
    _write_manifest(cfg.manifest_path, manifest)
    labels.items[cfg.issue_label] = SimpleNamespace(
        name=cfg.issue_label,
        color="1d76db",
        description=runner._label_owner_description(),
    )

    assert runner.cleanup()["status"] == "clean"
    assert labels.deleted == [cfg.issue_label]


def test_fixture_identity_guard_fails_before_any_mutation(tmp_path):
    runner, branches, prs, issues, _files = _runner(tmp_path, repos=_Repos(repo_id=999))
    with pytest.raises(NightlyE2EError, match="identité immuable"):
        runner.cleanup()
    assert branches.deleted == [] and prs.closed == [] and issues.closed == []


def test_cleanup_is_exact_verified_and_idempotent(tmp_path):
    runner, branches, prs, issues, _files = _runner(tmp_path)
    cfg = runner.config
    branches.refs[cfg.base_branch] = BASE_SHA
    branches.refs["collegue/issue-77"] = HEAD_SHA
    prs.items[88] = _pr(cfg)
    issues.items[77] = _issue(cfg)
    manifest = NightlyManifest.for_config(cfg)
    manifest.base_created = True
    manifest.base_sha = BASE_SHA
    manifest.issue_numbers = [77]
    manifest.pr_numbers = [88]
    manifest.head_shas = {"collegue/issue-77": HEAD_SHA}
    _own_run_label(runner, manifest)
    runner._task_correlations = lambda _manifest: {77: 77}

    result = runner.cleanup()
    assert result == {"status": "clean", "closed_prs": [88], "closed_issues": [77]}
    assert prs.closed == [88] and issues.closed == [77]
    assert ("collegue/issue-77", HEAD_SHA) in branches.deleted
    assert (cfg.base_branch, BASE_SHA) in branches.deleted
    assert branches.refs == {"main": SEED}

    # Relance indépendante du finalizer : aucune ressource, toujours un succès.
    assert runner.cleanup() == {"status": "clean", "closed_prs": [], "closed_issues": []}


def test_cleanup_refuses_a_moved_base_instead_of_deleting_it(tmp_path):
    runner, branches, _prs, _issues, _files = _runner(tmp_path)
    cfg = runner.config
    branches.refs[cfg.base_branch] = "e" * 40
    manifest = NightlyManifest.for_config(cfg)
    manifest.base_created = True
    manifest.base_sha = BASE_SHA
    _write_manifest(cfg.manifest_path, manifest)

    with pytest.raises(NightlyE2EError, match="base nightly déplacée"):
        runner.cleanup()
    assert branches.refs[cfg.base_branch] == "e" * 40


def test_cleanup_propagates_non_404_base_read_failure(tmp_path):
    runner, branches, _prs, _issues, _files = _runner(tmp_path)
    cfg = runner.config
    branches.refs[cfg.base_branch] = BASE_SHA
    manifest = NightlyManifest.for_config(cfg)
    manifest.base_created = True
    manifest.base_sha = BASE_SHA
    _write_manifest(cfg.manifest_path, manifest)
    original = branches.get_branch_sha

    def fail_base(owner, repo, branch):
        if branch == cfg.base_branch:
            raise ToolExecutionError("GitHub indisponible", status_code=500)
        return original(owner, repo, branch)

    branches.get_branch_sha = fail_base

    with pytest.raises(ToolExecutionError, match="indisponible"):
        runner.cleanup()

    assert branches.refs[cfg.base_branch] == BASE_SHA
    assert branches.deleted == []


def test_create_branch_collision_never_deletes_the_preexisting_branch(tmp_path):
    runner, branches, _prs, _issues, _files = _runner(tmp_path)
    cfg = runner.config
    branches.refs[cfg.base_branch] = SEED
    runner._manager = lambda: SimpleNamespace(list_projects=lambda: [])

    with pytest.raises(NightlyE2EError, match="existe avant"):
        runner.run()

    assert branches.refs[cfg.base_branch] == SEED
    assert branches.deleted == []


def test_base_creation_recovers_a_lost_ref_response(tmp_path):
    runner, branches, _prs, _issues, _files = _runner(tmp_path)
    cfg = runner.config
    manifest = NightlyManifest.for_config(cfg)
    _write_manifest(cfg.manifest_path, manifest)
    original = branches.ensure_commit_branch

    def lose_response(*args, **kwargs):
        original(*args, **kwargs)
        raise ToolExecutionError("réponse create ref perdue", status_code=500)

    branches.ensure_commit_branch = lose_response

    recovered_sha = runner._create_owned_base(manifest)

    assert recovered_sha == BASE_OWNER_SHA
    assert manifest.base_creation_started is True
    assert manifest.base_created is True
    assert manifest.base_sha == BASE_OWNER_SHA
    assert branches.refs[cfg.base_branch] == BASE_OWNER_SHA
    assert runner.cleanup()["status"] == "clean"
    assert cfg.base_branch not in branches.refs


def test_inventory_failure_keeps_every_remote_anchor(tmp_path):
    runner, branches, prs, _issues, _files = _runner(tmp_path)
    cfg = runner.config
    branches.refs[cfg.base_branch] = BASE_SHA
    manifest = NightlyManifest.for_config(cfg)
    manifest.base_created = True
    manifest.base_sha = BASE_SHA
    _write_manifest(cfg.manifest_path, manifest)
    prs.list_prs = lambda *args, **kwargs: (_ for _ in ()).throw(ToolExecutionError("API down"))

    with pytest.raises(NightlyE2EError, match="inventaire GitHub incomplet"):
        runner.cleanup()

    assert branches.refs[cfg.base_branch] == BASE_SHA
    assert branches.deleted == []


def test_close_failure_keeps_head_and_base_refs(tmp_path):
    runner, branches, prs, issues, _files = _runner(tmp_path)
    cfg = runner.config
    branches.refs[cfg.base_branch] = BASE_SHA
    branches.refs["collegue/issue-77"] = HEAD_SHA
    prs.items[88] = _pr(cfg)
    issues.items[77] = _issue(cfg)
    manifest = NightlyManifest.for_config(cfg)
    manifest.base_created = True
    manifest.base_sha = BASE_SHA
    manifest.issue_numbers = [77]
    manifest.pr_numbers = [88]
    manifest.head_shas = {"collegue/issue-77": HEAD_SHA}
    _own_run_label(runner, manifest)
    runner._task_correlations = lambda _manifest: {77: 77}
    prs.close_pr = lambda *args, **kwargs: (_ for _ in ()).throw(ToolExecutionError("close down"))

    with pytest.raises(NightlyE2EError, match="refs conservées"):
        runner.cleanup()

    assert branches.refs[cfg.base_branch] == BASE_SHA
    assert branches.refs["collegue/issue-77"] == HEAD_SHA
    assert branches.deleted == []


def test_issue_alone_never_authorizes_deleting_an_unproved_head(tmp_path):
    runner, branches, _prs, issues, _files = _runner(tmp_path)
    cfg = runner.config
    branches.refs[cfg.base_branch] = BASE_SHA
    branches.refs["collegue/issue-77"] = HEAD_SHA
    issues.items[77] = _issue(cfg)
    manifest = NightlyManifest.for_config(cfg)
    manifest.base_created = True
    manifest.base_sha = BASE_SHA
    manifest.issue_numbers = [77]
    _own_run_label(runner, manifest)
    runner._task_correlations = lambda _manifest: {77: 77}

    with pytest.raises(NightlyE2EError, match="SHA non prouvé"):
        runner.cleanup()

    assert branches.refs["collegue/issue-77"] == HEAD_SHA
    assert branches.refs[cfg.base_branch] == BASE_SHA
    assert branches.deleted == []


def test_cleanup_recovers_head_pushed_before_pr_creation(tmp_path):
    runner, branches, _prs, issues, _files = _runner(tmp_path)
    cfg = runner.config
    branches.refs[cfg.base_branch] = BASE_SHA
    branches.refs["collegue/issue-77"] = HEAD_SHA
    branches.commits[HEAD_SHA] = SimpleNamespace(
        sha=HEAD_SHA,
        tree_sha="2" * 40,
        parents=[BASE_SHA],
        message="collegue: issue #77 — app/main.py",
    )
    issues.items[77] = _issue(cfg, task_id=3, project_id=9, plan_hash="f" * 64)
    manifest = NightlyManifest.for_config(cfg)
    manifest.base_created = True
    manifest.base_sha = BASE_SHA
    manifest.project_id = 9
    manifest.plan_hash = "f" * 64
    manifest.issue_numbers = [77]
    manifest.head_creation_started = True
    manifest.expected_head_branch = "collegue/issue-77"
    _own_run_label(runner, manifest)
    runner._task_correlations = lambda _manifest: {77: 3}
    runner._approved_task_ids = lambda _manifest: {3}

    result = runner.cleanup()

    assert result["closed_issues"] == [77]
    assert ("collegue/issue-77", HEAD_SHA) in branches.deleted
    assert (cfg.base_branch, BASE_SHA) in branches.deleted


def test_cleanup_refuses_unowned_head_before_closing_issue(tmp_path):
    runner, branches, _prs, issues, _files = _runner(tmp_path)
    cfg = runner.config
    branches.refs[cfg.base_branch] = BASE_SHA
    branches.refs["collegue/issue-77"] = HEAD_SHA
    branches.commits[HEAD_SHA] = SimpleNamespace(
        sha=HEAD_SHA,
        tree_sha="2" * 40,
        parents=[BASE_SHA],
        message="commit humain sans marqueur",
    )
    issues.items[77] = _issue(cfg, task_id=3, project_id=9, plan_hash="f" * 64)
    manifest = NightlyManifest.for_config(cfg)
    manifest.base_created = True
    manifest.base_sha = BASE_SHA
    manifest.project_id = 9
    manifest.plan_hash = "f" * 64
    manifest.issue_numbers = [77]
    manifest.head_creation_started = True
    manifest.expected_head_branch = "collegue/issue-77"
    _own_run_label(runner, manifest)
    runner._task_correlations = lambda _manifest: {77: 3}
    runner._approved_task_ids = lambda _manifest: {3}

    with pytest.raises(NightlyE2EError, match="SHA non prouvé"):
        runner.cleanup()

    assert issues.items[77].state == "open"
    assert branches.deleted == []


def test_cleanup_recovers_a_lost_spec_commit_checkpoint(tmp_path):
    runner, branches, _prs, _issues, files = _runner(tmp_path)
    cfg = runner.config
    branches.refs[cfg.base_branch] = BASE_SHA
    branches.commits[BASE_SHA] = SimpleNamespace(parents=[SEED])
    files.spec = "# SPEC fixture"
    manifest = NightlyManifest.for_config(cfg)
    manifest.base_created = True
    manifest.base_sha = SEED
    manifest.project_id = 9
    manifest.plan_hash = "f" * 64
    _write_manifest(cfg.manifest_path, manifest)
    project = SimpleNamespace(
        approved_plan_hash="f" * 64,
        spec="# SPEC fixture",
        plan_sync_config={
            "owner": cfg.owner,
            "repo": cfg.repo,
            "base_branch": cfg.base_branch,
            "spec_filename": "SPEC.md",
        },
    )
    runner._manager = lambda: SimpleNamespace(get_project=lambda project_id: project, get_tasks=lambda project_id: [])

    assert runner.cleanup()["status"] == "clean"
    assert cfg.base_branch not in branches.refs


def test_cleanup_recovers_issue_created_before_db_persistence(tmp_path):
    runner, branches, _prs, issues, _files = _runner(tmp_path)
    cfg = runner.config
    branches.refs[cfg.base_branch] = BASE_SHA
    issues.items[77] = _issue(cfg, task_id=3, project_id=9, plan_hash="f" * 64)
    manifest = NightlyManifest.for_config(cfg)
    manifest.base_created = True
    manifest.base_sha = BASE_SHA
    manifest.project_id = 9
    manifest.plan_hash = "f" * 64
    _own_run_label(runner, manifest)
    updates = []
    project = SimpleNamespace(approved_plan_hash="f" * 64)
    task = SimpleNamespace(id=3, issue_number=None)
    manager = SimpleNamespace(
        get_project=lambda project_id: project,
        get_tasks=lambda project_id: [task],
        update_task=lambda task_id, **fields: updates.append((task_id, fields)) or True,
    )
    runner._manager = lambda: manager

    result = runner.cleanup()

    assert result["closed_issues"] == [77]
    assert updates == [(3, {"issue_number": 77})]
    assert cfg.base_branch not in branches.refs


def test_cleanup_retries_after_label_deleted_before_manifest_checkpoint(tmp_path):
    runner, _branches, _prs, issues, _files = _runner(tmp_path)
    cfg = runner.config
    manifest = NightlyManifest.for_config(cfg)
    manifest.project_id = 9
    manifest.plan_hash = "f" * 64
    manifest.issue_numbers = [77]
    _own_run_label(runner, manifest)

    # Simule le crash juste après DELETE label : GitHub retire aussi le label
    # des issues, tandis que le manifeste garde label_deleted=false.
    runner.clients.labels.items.clear()
    issues.items[77] = _issue(cfg, task_id=3, project_id=9, plan_hash="f" * 64)
    issues.items[77].state = "closed"
    issues.items[77].labels = []
    runner._task_correlations = lambda _manifest: {77: 3}
    runner._approved_task_ids = lambda _manifest: {3}

    assert runner.cleanup() == {"status": "clean", "closed_prs": [], "closed_issues": []}
    assert runner.clients.labels.deleted == []


def test_cleanup_never_accepts_an_open_issue_after_owned_label_disappears(tmp_path):
    runner, branches, _prs, issues, _files = _runner(tmp_path)
    cfg = runner.config
    branches.refs[cfg.base_branch] = BASE_SHA
    manifest = NightlyManifest.for_config(cfg)
    manifest.base_created = True
    manifest.base_sha = BASE_SHA
    manifest.project_id = 9
    manifest.plan_hash = "f" * 64
    manifest.issue_numbers = [77]
    _own_run_label(runner, manifest)
    runner.clients.labels.items.clear()
    issues.items[77] = _issue(cfg, task_id=3, project_id=9, plan_hash="f" * 64)
    issues.items[77].labels = []
    runner._task_correlations = lambda _manifest: {77: 3}
    runner._approved_task_ids = lambda _manifest: {3}

    with pytest.raises(NightlyE2EError, match="non corrélée exactement"):
        runner.cleanup()

    assert issues.items[77].state == "open"
    assert cfg.base_branch in branches.refs
    assert branches.deleted == []


def test_corrupt_manifest_is_rejected_before_remote_mutation(tmp_path):
    runner, branches, _prs, _issues, _files = _runner(tmp_path)
    cfg = runner.config
    manifest = NightlyManifest.for_config(cfg)
    manifest.root_sha = "0" * 40
    _write_manifest(cfg.manifest_path, manifest)

    with pytest.raises(NightlyE2EError, match="manifeste ne correspond"):
        runner.cleanup()
    assert branches.deleted == []


def test_full_driver_uses_four_product_processes_and_cleans(tmp_path):
    runner, branches, prs, issues, files = _runner(tmp_path)
    cfg = runner.config
    calls = []

    def command(argv, *, cwd=None):
        calls.append((list(argv), cwd))
        if argv[:3] == ["git", "clone", "--quiet"]:
            raise AssertionError("_clone_base est remplacé dans ce test")
        if "plan" in argv and "draft" in argv:
            return CommandResult(
                0,
                json.dumps(
                    {
                        "project_id": 9,
                        "plan_hash": "f" * 64,
                        "task_count": 1,
                        "action": "draft",
                        "issues": [],
                    }
                ),
            )
        if "plan" in argv and "approve" in argv:
            return CommandResult(
                0,
                json.dumps(
                    {
                        "project_id": 9,
                        "plan_hash": "f" * 64,
                        "task_count": 1,
                        "action": "approve",
                        "issues": [],
                    }
                ),
            )
        if "plan" in argv and "sync" in argv:
            branches.refs[cfg.base_branch] = BASE_SHA
            files.spec = "# SPEC fixture"
            issues.items[77] = _issue(cfg, task_id=3, project_id=9, plan_hash="f" * 64)
            return CommandResult(
                0,
                json.dumps(
                    {
                        "project_id": 9,
                        "plan_hash": "f" * 64,
                        "task_count": 1,
                        "action": "sync",
                        "issues": [{"task_id": 3, "issue_number": 77}],
                    }
                ),
            )
        if "--execute" in argv:
            branches.refs["collegue/issue-77"] = HEAD_SHA
            prs.items[88] = _pr(cfg)
            return CommandResult(
                1,
                json.dumps(
                    {
                        "project_id": 9,
                        "stop_reason": "awaiting_merge",
                        "iterations": 1,
                        "opened_prs": [88],
                        "pending_reviews": [3],
                        "project_status": "active",
                        "processed": [
                            {
                                "task_id": 3,
                                "title": "route nightly",
                                "success": True,
                                "stage": "pr",
                                "pr_number": 88,
                                "acceptance_passed": True,
                                "acceptance_error": None,
                                "acceptance_oracle_sha256": ORACLE_SHA,
                            }
                        ],
                    }
                ),
            )
        raise AssertionError(f"commande inattendue: {argv}")

    runner.command_runner = command
    runner._manager = lambda: SimpleNamespace(list_projects=lambda: [])
    runner._inspect_draft = lambda project_id, plan_hash: (3, "# SPEC fixture", ORACLE_SHA)
    runner._task_correlations = lambda _manifest: {77: 3}
    runner._approved_task_ids = lambda _manifest: {3}
    source = tmp_path / "clone-parent" / "fixture"
    source.mkdir(parents=True)
    runner._clone_base = lambda base_sha: str(source)

    result = runner.run()
    assert result["status"] == "passed"
    assert result["acceptance_passed"] is True
    assert result["acceptance_oracle_sha256"] == ORACLE_SHA
    product_calls = [argv for argv, _ in calls if argv[1:3] == ["-m", "collegue.pilot"]]
    assert len(product_calls) == 4
    assert any("draft" in argv for argv in product_calls)
    assert any("approve" in argv for argv in product_calls)
    assert any("sync" in argv for argv in product_calls)
    assert any("--repo-source" in argv for argv in product_calls)
    assert branches.refs == {"main": SEED}
    assert prs.items[88].state == "closed" and issues.items[77].state == "closed"


def test_run_failure_still_invokes_cleanup(tmp_path):
    runner, branches, _prs, _issues, _files = _runner(tmp_path)
    cfg = runner.config
    runner._manager = lambda: SimpleNamespace(list_projects=lambda: [])
    runner.command_runner = lambda argv, cwd=None: CommandResult(2, "", "planner cassé")
    with pytest.raises(NightlyE2EError, match="commande produit échouée"):
        runner.run()
    assert cfg.base_branch not in branches.refs
    assert branches.refs["main"] == SEED
