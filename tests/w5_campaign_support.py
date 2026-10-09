"""Aides de test de la politique de campagne W5 (garde de PUBLICATION et garde de FUSION), partagées entre les lots.

La politique est identifiée par le dépôt fixture et la base ``collegue-business/*`` (jamais par un drapeau). Les tests la rattachent
au dépôt de test en substituant les constantes de MODULE ``CAMPAIGN_REPOSITORY`` / ``CAMPAIGN_BASE_PREFIX`` / ``CAMPAIGN_SEED_SHA`` de
``collegue.pilot.w5_business_policy`` (monkeypatch, restauré en fin de test) : aucun paramètre de production ne la désactive.

Le socle de confiance est un VRAI historique Git à deux commits, comme le socle réel : une graine, puis un commit de bootstrap qui
n'en est que le descendant direct et porte les contrôles. Le manifeste (``W5_BOOTSTRAP_MANIFEST``) ne désigne que ce commit.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Dict, Optional

from github_fake_server import FIVE_CHECKS, GITHUB_ACTIONS_APP, OWNER, REPO
from github_fakes import _AUTHOR_ENV, git, make_source_repo

from collegue.pilot import w5_business_policy as fixture_policy

WORKFLOW_PATH = ".github/workflows/fixture-tests.yml"
CODEOWNERS_PATH = ".github/CODEOWNERS"
LOCK_PATH = "ci/requirements-approved.lock"
CHECK = fixture_policy.CAMPAIGN_CHECK
TRUSTED_WORKFLOW = "name: Fixture tests\non:\n  pull_request:\njobs:\n  fixture-tests:\n    name: Fixture tests\n    runs-on: ubuntu-latest\n"
SEED_FILES: Dict[str, str] = {"README.md": "# fixture\n", "app/main.py": "def health():\n    return {'status': 'ok'}\n"}
CONTROL_FILES: Dict[str, str] = {
    WORKFLOW_PATH: TRUSTED_WORKFLOW,
    CODEOWNERS_PATH: "/.github/ @owner\n/ci/ @owner\n",
    LOCK_PATH: "pytest==8.0.0 \\\n    --hash=sha256:" + "a" * 64 + "\n",
}


def campaign_source(root, *, seed_files: Optional[Dict[str, str]] = None, controls: Optional[Dict[str, str]] = None):
    """Dépôt source à DEUX commits : la graine, puis le socle (descendant direct qui ajoute les contrôles). ``main`` = le socle."""
    source = Path(make_source_repo(Path(root), dict(seed_files or SEED_FILES)))
    seed = git(source, "rev-parse", "HEAD")
    for name, content in (controls or CONTROL_FILES).items():
        target = source / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    git(source, "add", "-A")
    git(
        source,
        "-c",
        "user.name=fixture",
        "-c",
        "user.email=f@e.invalid",
        "commit",
        "-q",
        "-m",
        "socle",
        env=_AUTHOR_ENV,
    )
    return SimpleNamespace(path=str(source), seed_sha=seed, bootstrap_sha=git(source, "rev-parse", "HEAD"))


def write_manifest(directory, campaign, **overrides) -> str:
    manifest = {
        "schema": fixture_policy.BOOTSTRAP_SCHEMA,
        "repository": fixture_policy.CAMPAIGN_REPOSITORY,  # (lu à l'appel : après la substitution de l'identité)
        "seed_sha": campaign.seed_sha,
        "bootstrap_sha": campaign.bootstrap_sha,
        "protected_prefixes": [".github/", "ci/"],
    }
    manifest.update(overrides)
    path = Path(directory) / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return str(path)


def attach_identity(monkeypatch, campaign, *, directory=None, **manifest_overrides) -> Optional[str]:
    """Rattache la politique au dépôt de test. ``directory`` : où écrire le manifeste du socle de confiance (``W5_BOOTSTRAP_MANIFEST``) ;
    ``None`` : AUCUN socle fourni. Le manifeste est écrit APRÈS la substitution des constantes (il désigne le dépôt de test)."""
    monkeypatch.setattr(fixture_policy, "CAMPAIGN_REPOSITORY", f"{OWNER}/{REPO}")
    monkeypatch.setattr(fixture_policy, "CAMPAIGN_BASE_PREFIX", "main")
    monkeypatch.setattr(fixture_policy, "CAMPAIGN_SEED_SHA", campaign.seed_sha)
    if directory is None:
        monkeypatch.delenv(fixture_policy.TRUST_ANCHOR_ENV, raising=False)
        return None
    path = write_manifest(directory, campaign, **manifest_overrides)
    monkeypatch.setenv(fixture_policy.TRUST_ANCHOR_ENV, path)
    return path


def campaign_mode(monkeypatch, bridge, *, campaign=None, directory=None, job=True, **job_overrides):
    """Politique de campagne complète : identité, socle de confiance, check requis ``Fixture tests`` et son job Actions réel.

    ``job=True`` : le check est un job réel du workflow approuvé (``job_overrides`` en fait varier un champ) ; ``job=False`` : le
    check est publié SANS job (par l'API des checks) — la forgerie à refuser."""
    if campaign is not None:
        attach_identity(monkeypatch, campaign, directory=directory)
    else:
        monkeypatch.setattr(fixture_policy, "CAMPAIGN_REPOSITORY", f"{OWNER}/{REPO}")
        monkeypatch.setattr(fixture_policy, "CAMPAIGN_BASE_PREFIX", "main")
    bridge.protect_classic(checks=FIVE_CHECKS + (CHECK,))
    original = bridge.set_checks

    def set_checks(sha, states, *, app_id=GITHUB_ACTIONS_APP):
        original(sha, {**states, CHECK: "success"}, app_id=app_id)
        if job:
            produced = next(run for run in bridge.check_runs[sha] if run["name"] == CHECK)
            bridge.register_actions_job(produced["id"], head_sha=sha, name=CHECK, **job_overrides)

    bridge.set_checks = set_checks
    return bridge
