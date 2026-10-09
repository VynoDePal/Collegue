"""Garde de PUBLICATION du chemin PRODUIT (propriété C ; le garde est celui de B23, ``executor/pr.py``) : une tâche qui ALTÈRE un contrôle de la fixture
n'écrit RIEN à distance.

Constat C47 : sur la fixture, une PR de l'auteur-propriétaire qui modifie ``.github/`` ou ``ci/`` fusionne avec un check vert (CODEOWNERS à 0 approbation inefficace).
La sécurité du chemin Collègue repose donc sur le produit : contrôle complet de ``.github/`` ET ``ci/`` AVANT toute publication distante. Ces tests traversent la VRAIE
entrée publique (``run_project_from_settings`` → ``execute_issue`` → ``open_pr``, vrais clients GitHub derrière un VRAI dépôt Git distant, ``tests/w3_remote_bridge.py``) et
exigent ZÉRO écriture distante (ni branche, ni fichier, ni PR) : un faux GitHub qui compte les écritures, jamais un garde appelé directement.

Chaque famille adverse a son témoin bénin sur le même chemin. Le garde de B (``executor/pr.py``, ``w5_business_policy``) n'est actif que sur la fixture de campagne : ces tests
y placent le dépôt (identité et socle de confiance réels de la campagne, ``tests/w5_campaign_support.py``) ; sur un autre dépôt la publication reste inchangée (cas bénin de B).
"""

from __future__ import annotations

import os

import pytest
import w5_campaign_support as support
from test_w3_integration_build import (  # fixtures et aides du raccord de la vague 3 (aucune fonction de test importée)
    FilesAgent,
    bridge,
    linear_project,
    run_pass,
    state_url,
    statuses,
)

BASE_FILES = {
    "README.md": "# fixture\n",
    "app/main.py": "def health():\n    return {'status': 'ok'}\n",
    ".github/workflows/fixture-tests.yml": "name: Fixture tests\non:\n  pull_request:\njobs:\n  fixture-tests:\n    name: Fixture tests\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo base\n",
    ".github/CODEOWNERS": "/.github/ @owner\n/ci/ @owner\n",
    "ci/requirements-approved.lock": "fastapi==0.141.1 \\\n    --hash=sha256:" + "0" * 64 + "\n",
    "requirements.txt": "fastapi==0.141.1\n",
}


SEED = {k: v for k, v in BASE_FILES.items() if not k.startswith((".github", "ci/"))}
CONTROLS = {k: v for k, v in BASE_FILES.items() if k.startswith((".github", "ci/"))}


@pytest.fixture
def campaign(tmp_path):
    return support.campaign_source(tmp_path / "source", seed_files=SEED, controls=CONTROLS)


@pytest.fixture
def source(campaign):
    return campaign.path


@pytest.fixture(autouse=True)
def _campaign_identity(monkeypatch, campaign, tmp_path):
    support.attach_identity(monkeypatch, campaign, directory=tmp_path)


def _write(path):
    def apply(workspace):
        target = os.path.join(workspace, path)
        os.makedirs(os.path.dirname(target) or workspace, exist_ok=True)
        with open(target, "w", encoding="utf-8") as handle:
            handle.write("# altéré par la tâche\n")

    return apply


def _delete(path):
    def apply(workspace):
        os.remove(os.path.join(workspace, path))

    return apply


def _rename(old, new):
    def apply(workspace):
        os.makedirs(os.path.dirname(os.path.join(workspace, new)), exist_ok=True)
        os.rename(os.path.join(workspace, old), os.path.join(workspace, new))

    return apply


def _link(workspace):
    os.symlink("../.github/workflows/fixture-tests.yml", os.path.join(workspace, "ci", "lien.yml"))


TAMPERINGS = {
    "workflow-modified": _write(".github/workflows/fixture-tests.yml"),
    "workflow-added": _write(".github/workflows/second.yml"),
    "codeowners-modified": _write(".github/CODEOWNERS"),
    "codeowners-deleted": _delete(".github/CODEOWNERS"),
    "approved-lock-modified": _write("ci/requirements-approved.lock"),
    "approved-lock-deleted": _delete("ci/requirements-approved.lock"),
    "ci-file-added": _write("ci/extra.sh"),
    "workflow-renamed": _rename(".github/workflows/fixture-tests.yml", ".github/workflows/renamed.yml"),
    "link-into-ci": _link,
}


@pytest.mark.parametrize(
    "name",
    [
        # le lien symbolique est DÉJÀ refusé par la livraison (format non représentable, vague 3) : témoin que le chemin refuse sans écrire
        name
        for name in sorted(TAMPERINGS)
    ],
)
async def test_a_task_that_alters_a_control_publishes_nothing_at_all(name, bridge, source, state_url):
    pid = linear_project(state_url, 1)
    await run_pass(
        state_url,
        source,
        bridge,
        pid,
        agent=FilesAgent(extra=TAMPERINGS[name]),
        settings={"BUILD_AUTO_MERGE": False, "TASK_MAX_ATTEMPTS": 1},
    )
    assert bridge.prs == {} and bridge.remote.writes == [], "ZÉRO écriture distante : ni fichier, ni branche, ni PR"
    assert not [b for b in bridge.branches if b.startswith("collegue/")]
    from test_w3_integration_build import open_manager

    task = open_manager(state_url).get_tasks(pid)[0]
    error = task.last_error or ""
    assert task.status != "in_review" and "LIVRAISON REFUSÉE" in error, error
    assert any(token in error for token in (".github", "ci/", "contrôle")), (
        f"le motif doit nommer les contrôles : {error}"
    )


@pytest.mark.parametrize(
    "files",
    [
        {"app/main.py": "def health():\n    return {'status': 'ok', 'v': 2}\n"},
        {"docs/note.md": "# note\n"},
        {"requirements.txt": "fastapi==0.141.1\nhttpx==0.28.1\n"},
        {"github-notes.md": "# pas un contrôle : le nom ressemble à .github\n"},
        {"ci-notes/readme.md": "# pas le répertoire ci/\n"},
    ],
    ids=["code", "docs", "requirements", "lookalike-file", "lookalike-directory"],
)
async def test_the_same_path_publishes_a_task_that_touches_no_control(files, bridge, source, state_url):
    """Témoin bénin : les chemins qui NE SONT PAS des contrôles (y compris des noms voisins) restent publiables."""
    pid = linear_project(state_url, 1)
    await run_pass(state_url, source, bridge, pid, agent=FilesAgent(files=files), settings={"BUILD_AUTO_MERGE": False})
    assert sorted(bridge.prs) == [101] and statuses(state_url, pid) == {"T0": "in_review"}
