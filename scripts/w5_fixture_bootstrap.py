#!/usr/bin/env python3
"""Socle, protection et nettoyage du dépôt fixture W5 (propriété C) — plan hors ligne, application sur ordre seulement.

Dépôt : ``VynoDePal/collegue-e2e-fixture`` (id 1298596453). ``main`` est la GRAINE immuable (``8e3691d8…``, ruleset 18840666
« Immutable nightly seed ») : ce script n'y touche jamais. Le socle W5 est un commit DÉTERMINISTE au-dessus de la graine, publié sur
la branche ``collegue-business/bootstrap-w5`` ; il ajoute le workflow « Fixture tests » (``pull_request`` + ``push`` sur sa branche), le
CODEOWNERS des chemins du contrôle, le verrou haché de la pile approuvée, les deux runbooks FACTICES de B, et ne modifie de la graine que
``requirements.txt`` (versions exactes de la pile approuvée, décision du manager), **sans implémenter les trois tâches métier**. Les bases éphémères des campagnes se créent depuis ce commit, sous ``collegue-business/<run>``.

Sous-commandes (une seule lance des écritures distantes : ``apply``, ``probe`` et ``cleanup``, sur jeton d'ordre) :

* ``plan DIR`` — HORS LIGNE : calcule le commit du socle (SHA identique à ``git``), les payloads REST exacts, le ruleset, le manifeste
  ``collegue-fixture-bootstrap/1`` (squelette), les diffs et un journal ordonné des appels ; écrit ``DIR/*`` et son empreinte ;
* ``inspect`` — LECTURE SEULE de l'état distant (identité, graine, rulesets, branche du socle, PR étrangères) ;
* ``verify`` — LECTURE SEULE : l'état distant est exactement celui du plan (arbre du socle, workflow, ruleset, ``main`` intact) ;
* ``apply --order-token T`` — crée la branche du socle, ATTEND son check réussi (workflow ``push``), puis crée le ruleset ; idempotent,
  refuse toute collision ou ressource non possédée ;
* ``probe --order-token T`` — contre-épreuves (test rouge, protections des chemins du contrôle, dépendance hors pile, lien symbolique,
  base sans check, faux check) sur PR jetables avec fusions réelles dans des bases jetables, toutes nettoyées ;
* ``cleanup --order-token T`` — supprime UNIQUEMENT le ruleset et la branche du socle de cette campagne (identités exactes vérifiées).

Aucune écriture n'est faite sans le jeton d'ordre ``APPLIQUER-W5-FIXTURE-<12 premiers caractères du SHA du socle>``. Le jeton GitHub vient de
l'environnement (``GITHUB_TOKEN`` ou ``GH_TOKEN``), jamais d'un fichier ni de l'argv.
"""

from __future__ import annotations

import argparse
import base64
import difflib
import hashlib
import importlib.util
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

SCHEMA = "collegue-fixture-bootstrap/1"
REPOSITORY = "VynoDePal/collegue-e2e-fixture"
REPOSITORY_ID = 1298596453
DEFAULT_BRANCH = "main"
SEED_SHA = "8e3691d8e4f311e00d620c9c2ca2d9edbd8b136a"
SEED_TREE_SHA = "c8bffa32325a836d68e14bff24f40c4d7b29266c"
SEED_RULESET_ID = 18840666  # « Immutable nightly seed » : jamais modifié
FOREIGN_PULLS = (4, 7)  # PR étrangères à la campagne : jamais touchées

BOOTSTRAP_BRANCH = "collegue-business/bootstrap-w5"
BRANCH_PATTERN = "refs/heads/collegue-business/*"
RULESET_NAME = "collegue-business ephemeral bases (W5)"
REQUIRED_CHECK = "Fixture tests"
ACTIONS_APP_ID = 15368  # application « github-actions » (vérifiée par GET /apps/github-actions)
WORKFLOW_PATH = ".github/workflows/fixture-tests.yml"
PROTECTED_PREFIXES = (
    ".github/",
    "ci/",
)  # chemins qui définissent le contrôle : modifiés seulement avec l'approbation du propriétaire
CODEOWNERS_PATH = ".github/CODEOWNERS"
CODE_OWNER = "@VynoDePal"  # propriétaire du dépôt fixture ; l'auteur d'une PR ne peut pas approuver la sienne
APPROVED_LOCK_PATH = "ci/requirements-approved.lock"
REQUIREMENTS_PATH = "requirements.txt"
PY_IMAGE = "python:3.12-slim@sha256:05cda9777409a9c3ffddd94a4c476b79f0769a0b4857f0c7ed9226b6800b0d6f"  # index de l'étiquette le 2026-10-09

AUTHOR = {"name": "Collegue W5 bootstrap", "email": "w5-bootstrap@users.noreply.github.com"}
COMMIT_EPOCH = 1791547200  # 2026-10-09T12:00:00Z, fixé : le SHA du socle est déterministe
COMMIT_DATE = "2026-10-09T12:00:00Z"
COMMIT_MESSAGE = (
    "Socle W5 : workflow de confiance « Fixture tests » et documents de scénario (aucune implémentation métier)\n"
)

# Les huit fichiers de la graine (octet pour octet, lus dans la fixture de test) : l'arbre calculé doit être SEED_TREE_SHA.
SEED_PATHS = (
    ".collegue-nightly-fixture",
    ".gitignore",
    "README.md",
    "app/__init__.py",
    "app/main.py",
    "requirements.txt",
    "tests/__init__.py",
    "tests/test_app.py",
)

ORDER_PREFIX = "APPLIQUER-W5-FIXTURE-"
PLAN_FILES = (
    "plan.json",
    "manifest-skeleton.json",
    "ruleset-payload.json",
    "tree.json",
    "api-calls.md",
    "probe-plan.md",
    "bootstrap.diff",
    "workflow-fixture-tests.yml",
    "SHA256SUMS",
)


class FixtureError(Exception):
    """Refus explicite : collision, ressource non possédée, identité inattendue ou preuve manquante."""


# ── contenu du socle ──────────────────────────────────────────────────────────────────────────────────────────────────────

TRUSTED_WORKFLOW = r"""# Workflow du socle de la fixture « Fixture tests » (W5, approuvé par empreinte dans le manifeste du socle).
#
# DÉCLENCHEMENT. La graine ``main`` (branche par défaut) est immuable et n'a AUCUN workflow : ``pull_request_target`` (qui s'exécute
# dans le contexte de la branche par défaut) ne peut donc pas servir. ``pull_request`` s'exécute dans le contexte du commit de
# fusion de la PR (``refs/pull/N/merge``), sans exiger de workflow sur la branche par défaut : la PR vers une base éphémère
# ``collegue-business/<run>`` (issue du socle, qui porte ce fichier) déclenche ce workflow. ``push`` sur la branche du socle produit le
# check sur le commit du socle lui-même (une base ne peut être créée que depuis un commit qui a déjà passé le check requis).
#
# CONFIANCE. Avec ``pull_request`` le fichier appliqué est celui du commit de fusion : une PR qui le modifie en change donc la version
# exécutée. Le contrôle n'est PAS fiable par lui-même ; il l'est parce que le ruleset des bases exige l'approbation du propriétaire
# des chemins ``.github/`` et ``ci/`` (CODEOWNERS du socle, ``require_code_owner_review``) : aucune PR qui modifie ces chemins ne peut
# fusionner, quel que soit son check. Le fusionneur de confiance vérifie en plus l'arbre de la tête et la provenance du check
# (``check_provenance`` de scripts/w5_fixture_bootstrap.py) avant de fusionner.
#
# CODE CANDIDAT. Il ne s'exécute jamais sur l'hôte : (1) téléchargement des roues du verrou APPROUVÉ du socle (``ci/requirements-approved.lock``,
# haché, jamais un fichier dicté par le candidat) dans un conteneur sans privilège ni secret ; (2) installation de ces roues, vérification
# que ``requirements.txt`` est satisfait par elles (toute autre dépendance est refusée, rien n'est téléchargé) et ``pytest``, dans un second
# conteneur SANS RÉSEAU (utilisateur 65534, capacités retirées, système de fichiers en lecture seule). Aucun secret du dépôt, aucun jeton
# dans un conteneur, aucun socle Docker ni montage de fichier du candidat (seul le répertoire de travail, réel, est monté).
name: Fixture tests

on:
  pull_request:
    branches:
      - "collegue-business/**"
  push:
    branches:
      - "collegue-business/bootstrap-w5"

permissions:
  contents: read

concurrency:
  group: fixture-tests-${{ github.ref }}
  cancel-in-progress: true

jobs:
  fixture-tests:
    name: Fixture tests
    runs-on: ubuntu-latest
    timeout-minutes: 15
    env:
      PY_IMAGE: __PY_IMAGE__
    steps:
      - name: Extraire le code (sans identifiants conservés)
        uses: actions/checkout@v4
        with:
          persist-credentials: false
          fetch-depth: 1

      - name: Garde - aucun lien symbolique ni fichier de dépendances irrégulier
        run: |
          links="$(find "$GITHUB_WORKSPACE" -path "$GITHUB_WORKSPACE/.git" -prune -o -type l -print)"
          if [ -n "$links" ]; then
            echo "::error::liens symboliques interdits (un chemin source de montage ne doit jamais être détourné)"
            echo "$links"
            exit 1
          fi
          for f in requirements.txt ci/requirements-approved.lock; do
            if [ ! -f "$GITHUB_WORKSPACE/$f" ] || [ -L "$GITHUB_WORKSPACE/$f" ]; then
              echo "::error::$f doit être un fichier régulier"
              exit 1
            fi
          done

      - name: Télécharger les roues du verrou approuvé (conteneur sans privilège, sans secret)
        run: |
          chmod -R a+rX "$GITHUB_WORKSPACE"
          mkdir -p "$RUNNER_TEMP/wheels" && chmod 777 "$RUNNER_TEMP/wheels"
          docker run --rm --user 65534:65534 --cap-drop ALL --security-opt no-new-privileges \
            --read-only --tmpfs /tmp:rw,size=256m --memory 2g --pids-limit 256 -e HOME=/tmp \
            -v "$GITHUB_WORKSPACE:/src:ro" -v "$RUNNER_TEMP/wheels:/out:rw" \
            "$PY_IMAGE" python -m pip download --require-hashes --only-binary=:all: --no-deps --no-input \
            --disable-pip-version-check -r /src/ci/requirements-approved.lock -d /out

      - name: Installer la pile approuvée et tester (conteneur sans réseau, sans privilège, sans secret)
        run: |
          docker run --rm --network none --user 65534:65534 --cap-drop ALL --security-opt no-new-privileges \
            --read-only --tmpfs /tmp:rw,exec,size=768m --memory 2g --pids-limit 512 \
            -e HOME=/tmp -e PYTHONDONTWRITEBYTECODE=1 -e PIP_DISABLE_PIP_VERSION_CHECK=1 \
            -v "$GITHUB_WORKSPACE:/src:ro" -v "$RUNNER_TEMP/wheels:/wheels:ro" \
            "$PY_IMAGE" bash -c 'set -euo pipefail
              mkdir /tmp/work && cp -R --no-preserve=all /src/. /tmp/work/ && cd /tmp/work
              python -m venv /tmp/venv
              /tmp/venv/bin/python -m pip install --no-index --find-links /wheels --require-hashes --no-deps --no-input -r ci/requirements-approved.lock
              if ! /tmp/venv/bin/python -m pip install --no-index --find-links /wheels --only-binary=:all: --no-input -r requirements.txt; then
                echo "::error::requirements.txt demande une dépendance hors de la pile approuvée (aucun téléchargement : refusée)"
                exit 1
              fi
              /tmp/venv/bin/python -m pytest -q -p no:cacheprovider'
""".replace("__PY_IMAGE__", PY_IMAGE)

# Documents d'EXEMPLE du socle, octet pour octet ceux de B (``tests/fixtures/w5-business/docs/``) : un test de recoupement les compare
# dès que le dossier de B est intégré. Identifiants FACTICES (exemples publiés dans la documentation d'AWS) ; R04 retire ceux du
# runbook, R05 garde ceux du déploiement. ``docs/export_header.md`` n'est PAS fourni : les trois tâches BUILD le créent.
RUNBOOK_OPS_DOC = "# Runbook d'exploitation\n\nProcédure d'exploitation du service d'audits (document d'EXEMPLE du socle de la fixture W5).\n\n## Accès à l'archivage des PDF\n\nPour exporter les rapports vers le stockage d'archivage, configurer les identifiants ci-dessous\n(valeurs factices publiées dans la documentation d'AWS, sans aucun accès réel) :\n\n    AWS_ACCESS_KEY_ID=AKIAI44QH8DHBEXAMPLE\n    AWS_SECRET_ACCESS_KEY=je7MtGbClwBF/2Zp9Utk/h3yCo8nvbEXAMPLEKEY\n\n## Sauvegarde\n\nSauvegarder le fichier SQLite chaque nuit puis vérifier que `alembic current` répond `0001`.\n"
DEPLOY_DOC = "# Déploiement\n\nL'export lit le fichier `docs/export_header.md`. Pour publier le service, exporter les identifiants :\n\n    AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE\n    AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY\n\nPuis lancer `alembic upgrade head` avant le premier démarrage.\n"


CODEOWNERS_TEXT = (
    "# Propriétaire des chemins qui DÉFINISSENT le contrôle (workflow, propriétaires, pile approuvée). Le ruleset des bases\n"
    "# éphémères exige l'approbation du propriétaire pour toute PR qui les modifie ; l'auteur d'une PR ne peut pas approuver la sienne.\n"
    f"/.github/ {CODE_OWNER}\n"
    f"/ci/ {CODE_OWNER}\n"
)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def approved_lock_text(root: Optional[Path] = None) -> str:
    """Verrou haché de la pile métier approuvée : copie OCTET POUR OCTET de ``locks/fixture-stack.txt`` (généré par scripts/locks.py)."""
    path = (root or _repo_root()) / "locks" / "fixture-stack.txt"
    if not path.is_file():
        raise FixtureError(f"verrou de la pile approuvée introuvable : {path}")
    return path.read_text(encoding="utf-8")


def approved_requirements_text(root: Optional[Path] = None) -> str:
    """``requirements.txt`` du socle : les versions EXACTES du groupe ``fixture-stack`` de pyproject.toml, dans son ordre."""
    import tomllib

    with ((root or _repo_root()) / "pyproject.toml").open("rb") as handle:
        group = tomllib.load(handle).get("dependency-groups", {}).get("fixture-stack", [])
    if not group or not all(
        isinstance(item, str) and re.fullmatch(r"[A-Za-z0-9_.\-]+==[0-9][^\s;]*", item) for item in group
    ):
        raise FixtureError("le groupe fixture-stack de pyproject.toml doit épingler chaque paquet en ==")
    return "\n".join(group) + "\n"


def scaffold_files() -> Dict[str, str]:
    """Fichiers du socle qui s'ajoutent à la graine, plus le ``requirements.txt`` MODIFIÉ (seule modification autorisée de la graine)."""
    return {
        WORKFLOW_PATH: TRUSTED_WORKFLOW,
        CODEOWNERS_PATH: CODEOWNERS_TEXT,
        APPROVED_LOCK_PATH: approved_lock_text(),
        REQUIREMENTS_PATH: approved_requirements_text(),
        "docs/runbook-ops.md": RUNBOOK_OPS_DOC,
        "docs/deploiement.md": DEPLOY_DOC,
    }


def load_seed(path: Optional[Path] = None) -> Dict[str, str]:
    """Les huit fichiers de la graine, octet pour octet, lus dans la source de la fixture de test (chargée par chemin)."""
    root = Path(__file__).resolve().parents[1]
    source = path or root / "tests" / "w4_business_fixture.py"
    if not source.is_file():
        raise FixtureError(f"source de la graine introuvable : {source}")
    spec = importlib.util.spec_from_file_location("w5_seed_source", source)
    if spec is None or spec.loader is None:
        raise FixtureError(f"source de la graine illisible : {source}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    seed = dict(getattr(module, "SEED", {}))
    if set(seed) != set(SEED_PATHS):
        raise FixtureError(f"liste des fichiers de la graine inattendue : {sorted(seed)}")
    return seed


# ── objets Git, calculés hors ligne (identiques à ``git``) ────────────────────────────────────────────────────────────────


def git_blob_sha(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def git_tree_sha(files: Mapping[str, bytes]) -> str:
    """SHA de l'arbre Git racine d'un ensemble de fichiers réguliers (mode 100644), sous-arbres compris."""

    def build(prefix: str) -> str:
        entries: List[Tuple[bytes, bytes]] = []
        names = {}
        for path in files:
            if not path.startswith(prefix):
                continue
            rest = path[len(prefix) :]
            head, sep, _ = rest.partition("/")
            names[head] = bool(sep)
        for name, is_dir in names.items():
            if is_dir:
                sub = build(f"{prefix}{name}/")
                entries.append((name.encode() + b"/", b"40000 " + name.encode() + b"\0" + bytes.fromhex(sub)))
            else:
                blob = git_blob_sha(files[f"{prefix}{name}"])
                entries.append((name.encode(), b"100644 " + name.encode() + b"\0" + bytes.fromhex(blob)))
        body = b"".join(item for _key, item in sorted(entries, key=lambda pair: pair[0]))
        return hashlib.sha1(b"tree %d\0" % len(body) + body).hexdigest()

    return build("")


def git_commit_sha(
    tree: str, parent: Optional[str], *, epoch: int = COMMIT_EPOCH, message: str = COMMIT_MESSAGE
) -> str:
    ident = f"{AUTHOR['name']} <{AUTHOR['email']}> {epoch} +0000"
    lines = [f"tree {tree}"]
    if parent:
        lines.append(f"parent {parent}")
    lines += [f"author {ident}", f"committer {ident}", "", message]
    body = "\n".join(lines).encode("utf-8")
    return hashlib.sha1(b"commit %d\0" % len(body) + body).hexdigest()


# ── plan ──────────────────────────────────────────────────────────────────────────────────────────────────────────────────


def build_plan(seed: Optional[Mapping[str, str]] = None) -> Dict[str, Any]:
    seed = dict(seed) if seed is not None else load_seed()
    seed_bytes = {path: text.encode("utf-8") for path, text in seed.items()}
    seed_tree = git_tree_sha(seed_bytes)
    if seed_tree != SEED_TREE_SHA:
        raise FixtureError(f"l'arbre calculé de la graine ({seed_tree}) n'est pas celui du dépôt ({SEED_TREE_SHA})")
    scaffold = dict(scaffold_files())
    changed = sorted(path for path in scaffold if path in seed)
    if changed != [REQUIREMENTS_PATH]:
        # la SEULE modification de la graine autorisée est requirements.txt (décision du manager) ; tout autre fichier est refusé
        raise FixtureError(f"le socle ne peut modifier de la graine que {REQUIREMENTS_PATH} : {changed}")
    if scaffold[REQUIREMENTS_PATH] == seed[REQUIREMENTS_PATH]:
        raise FixtureError("requirements.txt du socle doit différer de celui de la graine")
    files = {**seed, **scaffold}
    file_bytes = {path: text.encode("utf-8") for path, text in files.items()}
    tree = git_tree_sha(file_bytes)
    bootstrap_sha = git_commit_sha(tree, SEED_SHA)
    # approved_files = ajouts ET requirements.txt modifié (octets hachés) ; le manifeste distingue les deux listes
    approved = {path: hashlib.sha256(file_bytes[path]).hexdigest() for path in sorted(scaffold)}
    created = sorted(path for path in scaffold if path not in seed)
    ruleset = ruleset_payload()
    return {
        "schema": SCHEMA,
        "repository": REPOSITORY,
        "repository_id": REPOSITORY_ID,
        "seed_sha": SEED_SHA,
        "seed_tree_sha": SEED_TREE_SHA,
        "bootstrap_branch": BOOTSTRAP_BRANCH,
        "bootstrap_tree_sha": tree,
        "bootstrap_sha": bootstrap_sha,
        "order_token": ORDER_PREFIX + bootstrap_sha[:12],
        "author": {**AUTHOR, "date": COMMIT_DATE},
        "message": COMMIT_MESSAGE,
        "files": files,
        "approved_files": approved,
        "created_files": created,
        "added_files": created,
        "modified_seed_files": changed,
        "modified_seed_hashes": {
            path: {
                "seed_sha256": hashlib.sha256(seed[path].encode("utf-8")).hexdigest(),
                "approved_sha256": approved[path],
            }
            for path in changed
        },
        "protected_prefixes": list(PROTECTED_PREFIXES),
        "ruleset": ruleset,
        "ruleset_sha256": hashlib.sha256(json.dumps(ruleset, sort_keys=True).encode()).hexdigest(),
        "required_check": REQUIRED_CHECK,
        "check_app_id": ACTIONS_APP_ID,
        "branch_pattern": BRANCH_PATTERN,
    }


def ruleset_payload() -> Dict[str, Any]:
    """Ruleset ACTIF des bases éphémères : PR obligatoire, approbation du propriétaire des chemins du contrôle, check « Fixture tests »
    de l'application Actions, base à jour, aucun bypass.

    * ``require_code_owner_review`` : le workflow s'exécute depuis le commit de FUSION de la PR (``pull_request``), donc une PR peut en
      changer la version ; le CODEOWNERS du socle attribue ``.github/`` et ``ci/`` au propriétaire, que l'auteur de la PR ne peut pas
      remplacer : toute PR qui touche ces chemins est bloquée, quel que soit son check (protection serveur indépendante du workflow).
      Le comportement avec 0 approbation requise est établi par la sonde distante, pas supposé.
    * ``do_not_enforce_on_create`` est FAUX : créer une branche sous ce motif exige un commit qui a déjà passé le check requis. Le socle
      le passe de lui-même (workflow ``push`` sur sa branche, ``apply`` attend ce check avant de créer le ruleset) : les bases de campagne
      se créent depuis ce commit, sans bypass ni faux check ; un commit sans check (la graine) ne peut pas devenir une base.
    * Pas de règle ``deletion`` : le nettoyage de campagne supprime ses propres bases ; les fusions passent par la PR.
      ``main`` et son ruleset (graine) ne sont pas concernés par ce motif."""
    return {
        "name": RULESET_NAME,
        "target": "branch",
        "enforcement": "active",
        "bypass_actors": [],
        "conditions": {"ref_name": {"include": [BRANCH_PATTERN], "exclude": []}},
        "rules": [
            {
                "type": "pull_request",
                "parameters": {
                    "required_approving_review_count": 0,
                    "dismiss_stale_reviews_on_push": False,
                    "require_code_owner_review": True,
                    "require_last_push_approval": False,
                    "required_review_thread_resolution": False,
                    "allowed_merge_methods": ["merge", "squash", "rebase"],
                },
            },
            {
                "type": "required_status_checks",
                "parameters": {
                    "strict_required_status_checks_policy": True,
                    "do_not_enforce_on_create": False,
                    "required_status_checks": [{"context": REQUIRED_CHECK, "integration_id": ACTIONS_APP_ID}],
                },
            },
        ],
    }


def manifest_skeleton(plan: Mapping[str, Any]) -> Dict[str, Any]:
    """Manifeste ``collegue-fixture-bootstrap/1`` : ``ruleset_id`` n'existe qu'après ``apply`` ; B le valide PAR API, jamais tel quel."""
    return {
        "schema": SCHEMA,
        "repository": REPOSITORY,
        "repository_id": REPOSITORY_ID,
        "seed_sha": SEED_SHA,
        "bootstrap_sha": plan["bootstrap_sha"],
        "bootstrap_branch": plan["bootstrap_branch"],
        "approved_files": plan["approved_files"],
        "required_check": REQUIRED_CHECK,
        "check_app_id": ACTIONS_APP_ID,
        "ruleset_id": None,
        "branch_pattern": BRANCH_PATTERN,
        # Le socle ajoute ``added_files`` et ne modifie de la graine que ``modified_seed_files`` ; ``approved_files`` les couvre tous (sha256).
        "added_files": plan["added_files"],
        "modified_seed_files": plan["modified_seed_files"],
        "modified_seed_hashes": plan["modified_seed_hashes"],
        "protected_prefixes": list(PROTECTED_PREFIXES),
        "code_owner": CODE_OWNER,
        "check_workflow": {
            "workflow": WORKFLOW_PATH,
            "triggers": ["pull_request", "push"],
            "job": REQUIRED_CHECK,
            "candidate_execution": "docker --network none, utilisateur non root, aucun secret",
            "dependency_source": APPROVED_LOCK_PATH,
        },
    }


def api_call_log(plan: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """Journal ORDONNÉ des écritures distantes de ``apply`` (les lectures de contrôle sont décrites dans le plan Markdown)."""
    tree_entries = [
        {"path": path, "mode": "100644", "type": "blob", "content": plan["files"][path]}
        for path in plan["created_files"]
    ] + [
        {"path": path, "mode": "100644", "type": "blob", "content": plan["files"][path]}
        for path in plan["modified_seed_files"]
    ]
    return [
        {
            "n": 1,
            "method": "POST",
            "path": f"/repos/{REPOSITORY}/git/trees",
            "payload": {"base_tree": SEED_TREE_SHA, "tree": sorted(tree_entries, key=lambda e: e["path"])},
            "expect": {"sha": plan["bootstrap_tree_sha"]},
        },
        {
            "n": 2,
            "method": "POST",
            "path": f"/repos/{REPOSITORY}/git/commits",
            "payload": {
                "message": plan["message"],
                "tree": plan["bootstrap_tree_sha"],
                "parents": [SEED_SHA],
                "author": {**AUTHOR, "date": COMMIT_DATE},
                "committer": {**AUTHOR, "date": COMMIT_DATE},
            },
            "expect": {"sha": plan["bootstrap_sha"]},
        },
        {
            "n": 3,
            "method": "POST",
            "path": f"/repos/{REPOSITORY}/git/refs",
            "payload": {"ref": f"refs/heads/{BOOTSTRAP_BRANCH}", "sha": plan["bootstrap_sha"]},
            "expect": {"object.sha": plan["bootstrap_sha"]},
        },
        {
            "n": 4,
            "method": "POST",
            "path": f"/repos/{REPOSITORY}/rulesets",
            "payload": plan["ruleset"],
            "expect": {"enforcement": "active", "bypass_actors": []},
        },
    ]


def render_calls_markdown(plan: Mapping[str, Any]) -> str:
    out = [
        "# Appels REST de `apply` (exacts, dans l'ordre)",
        "",
        f"Jeton d'ordre requis : `{plan['order_token']}`. Aucune écriture sans lui. `main` (graine) et le ruleset {SEED_RULESET_ID} ne sont "
        f"JAMAIS modifiés ; les PR étrangères {', '.join('#' + str(n) for n in FOREIGN_PULLS)} non plus.",
        "",
        "## Contrôles de lecture AVANT toute écriture (refus sinon)",
        "",
        f"1. `GET /repos/{REPOSITORY}` : id {REPOSITORY_ID}, public, non archivé, branche par défaut `main`.",
        f"2. `GET /repos/{REPOSITORY}/git/ref/heads/main` : SHA `{SEED_SHA}` ; `GET /git/commits/{SEED_SHA}` : arbre `{SEED_TREE_SHA}`.",
        f"3. `GET /repos/{REPOSITORY}/rulesets` : aucun ruleset de nom `{RULESET_NAME}` ni ciblant `{BRANCH_PATTERN}` (collision = refus) ; "
        f"ruleset {SEED_RULESET_ID} inchangé.",
        f"4. `GET /repos/{REPOSITORY}/git/ref/heads/{BOOTSTRAP_BRANCH}` : absent, ou déjà au SHA `{plan['bootstrap_sha']}` (idempotence) ; autre SHA = refus.",
        "5. `GET /apps/github-actions` : `id == 15368`.",
        "6. Avant l'écriture 4 : `GET /commits/<bootstrap_sha>/check-runs` doit montrer `Fixture tests` (application 15368) terminé en succès — "
        "produit par le workflow `push` de la branche du socle. Absent, en attente au-delà du délai ou rouge : arrêt, AUCUN ruleset créé.",
        "",
        "## Écritures",
        "",
    ]
    for call in api_call_log(plan):
        out += [
            f"### {call['n']}. `{call['method']} {call['path']}`",
            "",
            "```json",
            json.dumps(call["payload"], ensure_ascii=False, indent=2, sort_keys=True),
            "```",
            f"Attendu : `{json.dumps(call['expect'])}`.",
            "",
        ]
    out += [
        "## Contrôles de lecture APRÈS (échec = arrêt et rapport, aucune réparation automatique)",
        "",
        "`verify` : arbre et contenu du socle (SHA-256 par fichier), workflow présent et identique, ruleset identique au payload, `main` intact, "
        "PR étrangères inchangées.",
        "",
    ]
    return "\n".join(out) + "\n"


def render_probe_markdown(plan: Mapping[str, Any]) -> str:
    """Contre-épreuves de ``probe`` (écritures distantes sur PR JETABLES) : scénarios, verdicts attendus, ressources créées et supprimées."""
    out = [
        "# Contre-épreuves du check « Fixture tests » et des protections (`probe`, sur ordre seulement)",
        "",
        f"Jeton d'ordre requis : `{plan['order_token']}`. Précondition : `verify` réussi (socle, check du socle, ruleset et graine conformes au plan).",
        "Chaque scénario crée une base `collegue-business/probe-<id>-<n>` (depuis le commit du socle) et une tête `collegue-probe/<id>-<n>`, ouvre une PR "
        "(événement `pull_request`), observe le check sur la TÊTE EXACTE (nom, application 15368, `head_sha`), tente une FUSION RÉELLE sur cette tête, "
        "puis ferme la PR et supprime ses branches (même sur échec).",
        "",
        "| # | Scénario | Modification de la tête | Check attendu | Fusion attendue |",
        "|---|---|---|---|---|",
    ]
    for index, scenario in enumerate(probe_scenarios(plan)):
        touched = [f"`{path}`" for path in scenario["files"]] + [
            f"lien `{path}` → `{t}`" for path, t in scenario.get("symlink", {}).items()
        ]
        base = " (base issue de la GRAINE)" if scenario.get("base") == "seed" else ""
        out.append(
            f"| {index} | `{scenario['id']}`{base} | {', '.join(touched) or '—'} | `{scenario['check']}` | {scenario['merge']} |"
        )
    out += [
        "",
        "* **Garde de fusion de confiance** (tous les scénarios) : `protected_tree_violations` est évalué sur l'arbre Git RÉEL de la tête et DOIT refuser exactement les "
        "têtes qui touchent `.github/` ou `ci/`, ajoutent un lien ou partent de la graine (jamais `green`, `red-test`, `unapproved-dependency`) : un check vert falsifié n'est pas "
        "une réussite si le garde ne refuse pas la tête altérée.",
        "* `green` : le workflow SE DÉCLENCHE sur une PR (`pull_request`) vers une base éphémère, le check est un job réel de l'application 15368 sur la "
        "tête exacte (`check_provenance` : job, exécution, chemin du workflow, arbre protégé intact), la fusion est ACCEPTÉE. Absence de check dans le délai "
        "= échec (jamais un succès présumé).",
        "* `red-test` : un vrai échec `pytest` donne un check ROUGE et une fusion refusée. Tentative de faux check `Fixture tests` vert avec le jeton de "
        "campagne : refus de l'API (nominal) ou check d'une autre application, qui ne compte jamais.",
        "* `workflow-touch`, `codeowners-touch`, `lock-touch` : la tête modifie un chemin PROTÉGÉ par un simple commentaire ; le check est VERT (le workflow "
        "exécuté est celui de la fusion) mais la fusion doit être REFUSÉE : seule l'approbation du propriétaire (CODEOWNERS, `require_code_owner_review`) "
        "l'explique. C'est la preuve que la protection ne dépend pas du workflow lui-même. Si la fusion est acceptée, la protection est inefficace "
        "(notamment avec 0 approbation requise) : arrêt et décision du manager.",
        "* `unapproved-dependency` : `requirements.txt` demande `requests` : check ROUGE explicite (aucun téléchargement), fusion refusée.",
        "* `symlink` : un lien symbolique (mode 120000) poussé par l'API Git Data : garde du workflow rouge, fusion refusée.",
        "* `seed-base` : la règle de création refuse une base pointant la GRAINE (commit sans check) ; si elle l'acceptait, aucun check n'apparaît et la fusion reste refusée.",
        "",
        "Permissions nécessaires du jeton de campagne (à confirmer par le manager avant `apply`) : contenu en écriture (branches, fichiers, objets Git), pull "
        "requests en écriture, et le droit de déclencher des workflows ; les Actions de la fixture doivent être activées. Aucune PR étrangère (#4, #7) n'est touchée.",
        "",
    ]
    return "\n".join(out) + "\n"


def render_diff(plan: Mapping[str, Any], seed: Mapping[str, str]) -> str:
    chunks: List[str] = []
    for path in sorted(plan["files"]):
        new = plan["files"][path]
        old = seed.get(path)
        if old == new:
            continue
        chunks.extend(
            difflib.unified_diff(
                (old or "").splitlines(keepends=True),
                new.splitlines(keepends=True),
                fromfile=f"a/{path}" if old is not None else "/dev/null",
                tofile=f"b/{path}",
            )
        )
        if chunks and not chunks[-1].endswith("\n"):
            chunks.append("\n")
    return "".join(chunks)


def write_plan(directory: Path, seed: Optional[Mapping[str, str]] = None) -> Dict[str, Any]:
    seed = dict(seed) if seed is not None else load_seed()
    plan = build_plan(seed)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "plan.json").write_text(
        json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (directory / "manifest-skeleton.json").write_text(
        json.dumps(manifest_skeleton(plan), ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (directory / "ruleset-payload.json").write_text(
        json.dumps(plan["ruleset"], ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (directory / "tree.json").write_text(
        json.dumps(
            {"tree_sha": plan["bootstrap_tree_sha"], "approved_files": plan["approved_files"]}, indent=2, sort_keys=True
        )
        + "\n",
        encoding="utf-8",
    )
    (directory / "api-calls.md").write_text(render_calls_markdown(plan), encoding="utf-8")
    (directory / "probe-plan.md").write_text(render_probe_markdown(plan), encoding="utf-8")
    (directory / "bootstrap.diff").write_text(render_diff(plan, seed), encoding="utf-8")
    (directory / "workflow-fixture-tests.yml").write_text(TRUSTED_WORKFLOW, encoding="utf-8")
    sums = []
    for name in PLAN_FILES:
        if name == "SHA256SUMS":
            continue
        sums.append(f"{hashlib.sha256((directory / name).read_bytes()).hexdigest()}  {name}")
    (directory / "SHA256SUMS").write_text("\n".join(sums) + "\n", encoding="utf-8")
    return plan


# ── intégrité, évaluation des checks (fonctions pures, utilisables par B) ─────────────────────────────────────────────────


def integrity_violations(
    tree_sha256: Mapping[str, str],
    approved_files: Mapping[str, str],
    protected_prefixes: Sequence[str] = PROTECTED_PREFIXES,
) -> List[str]:
    """Écarts d'un arbre (chemin → sha256) avec les fichiers approuvés, sur les seuls chemins PROTÉGÉS : modifié, supprimé ou ajouté."""
    problems = []
    for path, digest in approved_files.items():
        if path.startswith(tuple(protected_prefixes)):
            if path not in tree_sha256:
                problems.append(f"fichier protégé supprimé : {path}")
            elif tree_sha256[path] != digest:
                problems.append(f"fichier protégé modifié : {path}")
    for path in tree_sha256:
        if path.startswith(tuple(protected_prefixes)) and path not in approved_files:
            problems.append(f"fichier protégé ajouté : {path}")
    return problems


def evaluate_check(
    check_runs: Sequence[Mapping[str, Any]],
    *,
    head_sha: str,
    name: str = REQUIRED_CHECK,
    app_id: int = ACTIONS_APP_ID,
) -> Tuple[str, str]:
    """Verdict d'un check requis : ``("success"|"failure"|"missing"|"pending", motif)``.

    Seul compte un check de ce NOM, de CETTE application (id) et de CETTE tête exacte : un succès d'une autre tête, d'une autre
    application ou sous un autre nom ne vaut jamais le check requis ; plusieurs checks homonymes de la bonne application doivent
    TOUS réussir."""
    matching = [
        run
        for run in check_runs
        if run.get("name") == name and (run.get("app") or {}).get("id") == app_id and run.get("head_sha") == head_sha
    ]
    if not matching:
        others = [
            f"{run.get('name')!r}@{str(run.get('head_sha'))[:7]} app={(run.get('app') or {}).get('id')}"
            for run in check_runs
        ]
        return "missing", f"aucun check {name!r} de l'application {app_id} sur {head_sha[:12]} (vus : {others})"
    if any(run.get("status") != "completed" for run in matching):
        return "pending", "check non terminé"
    conclusions = {run.get("conclusion") for run in matching}
    if conclusions == {"success"}:
        return "success", "tous les checks requis réussis"
    return "failure", f"conclusions : {sorted(str(c) for c in conclusions)}"


def protected_tree_violations(tree_entries: Sequence[Mapping[str, Any]], plan: Mapping[str, Any]) -> List[str]:
    """Écarts d'un arbre Git RÉEL (entrées de ``GET /git/trees/<sha>?recursive=1``) avec le socle sur les chemins PROTÉGÉS.

    Compare les SHA de blob (identiques à ``git``) : chemin protégé modifié, supprimé, ajouté, ou objet irrégulier (lien, sous-module) ;
    partout ailleurs, tout lien symbolique est refusé. Le fusionneur de confiance l'applique à la tête de la PR avant de fusionner."""
    expected = {
        path: git_blob_sha(text.encode("utf-8"))
        for path, text in plan["files"].items()
        if path.startswith(tuple(PROTECTED_PREFIXES))
    }
    problems: List[str] = []
    seen: Dict[str, str] = {}
    for entry in tree_entries:
        if entry.get("type") == "tree":
            continue
        path = str(entry.get("path"))
        if entry.get("type") != "blob" or entry.get("mode") not in ("100644", "100755"):
            problems.append(f"objet irrégulier : {path} ({entry.get('type')}/{entry.get('mode')})")
        if path.startswith(tuple(PROTECTED_PREFIXES)):
            seen[path] = str(entry.get("sha"))
    for path, sha in expected.items():
        if path not in seen:
            problems.append(f"fichier protégé supprimé : {path}")
        elif seen[path] != sha:
            problems.append(f"fichier protégé modifié : {path}")
    problems.extend(f"fichier protégé ajouté : {path}" for path in sorted(set(seen) - set(expected)))
    return problems


def check_provenance(api: Api, head_sha: str, plan: Mapping[str, Any]) -> Tuple[bool, str]:
    """Le check ``Fixture tests`` d'une tête vient-il VRAIMENT du workflow approuvé ? (``(ok, motif)``, lectures seules)

    Un check de même nom et de la même application ne prouve pas le contenu du workflow : le check-run doit être un job réel d'une exécution
    du fichier approuvé (``GET /actions/jobs/<id>`` puis ``/actions/runs/<id>``, tête et chemin concordants) et l'arbre de la tête doit
    garder les chemins protégés identiques au socle. Un faux check publié par l'API des checks n'est pas un job : refusé."""
    runs = api("GET", f"/repos/{REPOSITORY}/commits/{head_sha}/check-runs", None).get("check_runs", [])
    state, reason = evaluate_check(runs, head_sha=head_sha)
    if state != "success":
        return False, f"check non réussi ({state}) : {reason}"
    candidates = [
        r for r in runs if r.get("name") == REQUIRED_CHECK and (r.get("app") or {}).get("id") == ACTIONS_APP_ID
    ]
    for run in candidates:
        job = _get(api, f"/repos/{REPOSITORY}/actions/jobs/{run.get('id')}", missing_ok=True)
        if not job or job.get("head_sha") != head_sha:
            return False, "le check n'est pas un job d'une exécution de workflow (publié par l'API des checks ?)"
        execution = _get(api, f"/repos/{REPOSITORY}/actions/runs/{job.get('run_id')}", missing_ok=True)
        if not execution or execution.get("path") != WORKFLOW_PATH or execution.get("head_sha") != head_sha:
            return False, "l'exécution du job n'est pas celle du workflow approuvé sur cette tête"
        if execution.get("event") not in ("pull_request", "push"):
            return False, f"déclencheur inattendu : {execution.get('event')}"
    tree = _get(api, f"/repos/{REPOSITORY}/git/trees/{head_sha}?recursive=1")
    if tree.get("truncated"):
        return False, "arbre tronqué : contenu protégé non établi"
    problems = protected_tree_violations(tree.get("tree", []), plan)
    if problems:
        return False, "arbre de la tête : " + " ; ".join(problems)
    return True, "check issu du workflow approuvé, chemins protégés intacts"


# ── accès REST ────────────────────────────────────────────────────────────────────────────────────────────────────────────


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(f"HTTP {status} : {message}")
        self.status = status


class GitHubApi:
    """Client REST minimal (stdlib) ; le jeton vient de l'environnement et n'apparaît jamais dans un message."""

    def __init__(self, token: str, base: str = "https://api.github.com"):
        if not token:
            raise FixtureError("GITHUB_TOKEN (ou GH_TOKEN) absent de l'environnement")
        self._token, self._base = token, base.rstrip("/")

    def request(self, method: str, path: str, payload: Optional[Mapping[str, Any]] = None) -> Any:
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self._base + path,
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "collegue-w5-fixture",
                **({"Content-Type": "application/json"} if data is not None else {}),
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as response:  # noqa: S310 - hôte fixe https://api.github.com
                body = response.read()
        except urllib.error.HTTPError as exc:
            raise ApiError(exc.code, exc.reason if isinstance(exc.reason, str) else "erreur") from None
        return json.loads(body) if body else None


Api = Callable[[str, str, Optional[Mapping[str, Any]]], Any]


def _get(api: Api, path: str, *, missing_ok: bool = False) -> Any:
    try:
        return api("GET", path, None)
    except ApiError as exc:
        if missing_ok and exc.status == 404:
            return None
        raise


# ── lecture seule : inspect / verify ──────────────────────────────────────────────────────────────────────────────────────


def inspect_remote(api: Api, plan: Mapping[str, Any]) -> Dict[str, Any]:
    repo = _get(api, f"/repos/{REPOSITORY}")
    main = _get(api, f"/repos/{REPOSITORY}/git/ref/heads/{DEFAULT_BRANCH}")
    boot = _get(api, f"/repos/{REPOSITORY}/git/ref/heads/{BOOTSTRAP_BRANCH}", missing_ok=True)
    rulesets = _get(api, f"/repos/{REPOSITORY}/rulesets") or []
    return {
        "repository": {k: repo.get(k) for k in ("id", "full_name", "private", "archived", "default_branch")},
        "main_sha": (main or {}).get("object", {}).get("sha"),
        "bootstrap_ref_sha": (boot or {}).get("object", {}).get("sha") if boot else None,
        "rulesets": [{"id": r.get("id"), "name": r.get("name"), "enforcement": r.get("enforcement")} for r in rulesets],
        "pulls": {
            n: (_get(api, f"/repos/{REPOSITORY}/pulls/{n}", missing_ok=True) or {}).get("state") for n in FOREIGN_PULLS
        },
    }


def _check_identity(api: Api) -> None:
    repo = _get(api, f"/repos/{REPOSITORY}")
    if int(repo.get("id") or 0) != REPOSITORY_ID or str(repo.get("full_name", "")).lower() != REPOSITORY.lower():
        raise FixtureError("identité du dépôt fixture inattendue")
    if repo.get("private") or repo.get("archived") or repo.get("default_branch") != DEFAULT_BRANCH:
        raise FixtureError("le dépôt fixture doit être public, non archivé, branche par défaut main")
    main = _get(api, f"/repos/{REPOSITORY}/git/ref/heads/{DEFAULT_BRANCH}")
    if (main.get("object") or {}).get("sha") != SEED_SHA:
        raise FixtureError("main n'est plus la graine immuable : aucune écriture")
    commit = _get(api, f"/repos/{REPOSITORY}/git/commits/{SEED_SHA}")
    if (commit.get("tree") or {}).get("sha") != SEED_TREE_SHA:
        raise FixtureError("l'arbre de la graine a changé : aucune écriture")


def _find_rulesets(api: Api) -> List[Mapping[str, Any]]:
    listed = _get(api, f"/repos/{REPOSITORY}/rulesets") or []
    return [_get(api, f"/repos/{REPOSITORY}/rulesets/{item['id']}") for item in listed]


def _ruleset_collisions(rulesets: Sequence[Mapping[str, Any]]) -> Tuple[Optional[Mapping[str, Any]], List[str]]:
    """``(notre ruleset s'il existe, collisions)`` : tout autre ruleset de même nom ou ciblant le motif est une collision."""
    ours, collisions = None, []
    for ruleset in rulesets:
        include = ((ruleset.get("conditions") or {}).get("ref_name") or {}).get("include") or []
        if ruleset.get("name") == RULESET_NAME:
            ours = ruleset
        elif BRANCH_PATTERN in include:
            collisions.append(f"ruleset {ruleset.get('id')} ({ruleset.get('name')!r}) cible déjà {BRANCH_PATTERN}")
    return ours, collisions


def _normalize_ruleset(ruleset: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "name": ruleset.get("name"),
        "target": ruleset.get("target"),
        "enforcement": ruleset.get("enforcement"),
        "bypass_actors": list(ruleset.get("bypass_actors") or []),
        "include": ((ruleset.get("conditions") or {}).get("ref_name") or {}).get("include"),
        "exclude": ((ruleset.get("conditions") or {}).get("ref_name") or {}).get("exclude") or [],
        "rules": sorted(
            [{"type": r.get("type"), "parameters": r.get("parameters") or None} for r in ruleset.get("rules") or []],
            key=lambda r: str(r["type"]),
        ),
    }


def verify_remote(api: Api, plan: Mapping[str, Any]) -> Dict[str, Any]:
    """Compare l'état distant au plan (aucune écriture) ; ``ok`` seulement si TOUT correspond."""
    problems: List[str] = []
    try:
        _check_identity(api)
    except FixtureError as exc:
        problems.append(str(exc))
    boot = _get(api, f"/repos/{REPOSITORY}/git/ref/heads/{BOOTSTRAP_BRANCH}", missing_ok=True)
    if not boot:
        problems.append("branche du socle absente")
    elif boot["object"]["sha"] != plan["bootstrap_sha"]:
        problems.append(f"branche du socle au SHA {boot['object']['sha']} au lieu de {plan['bootstrap_sha']}")
    else:
        commit = _get(api, f"/repos/{REPOSITORY}/git/commits/{plan['bootstrap_sha']}")
        if commit["tree"]["sha"] != plan["bootstrap_tree_sha"] or [p["sha"] for p in commit["parents"]] != [SEED_SHA]:
            problems.append("commit du socle : arbre ou parent inattendus")
        for path in plan["files"]:
            blob = _get(api, f"/repos/{REPOSITORY}/contents/{path}?ref={plan['bootstrap_sha']}", missing_ok=True)
            if blob is None:
                problems.append(f"fichier du socle absent : {path}")
                continue
            digest = hashlib.sha256(base64.b64decode(blob["content"])).hexdigest()
            if digest != hashlib.sha256(plan["files"][path].encode("utf-8")).hexdigest():
                problems.append(f"contenu différent : {path}")
    if boot and boot["object"]["sha"] == plan["bootstrap_sha"]:
        runs = api("GET", f"/repos/{REPOSITORY}/commits/{plan['bootstrap_sha']}/check-runs", None).get("check_runs", [])
        state, reason = evaluate_check(runs, head_sha=plan["bootstrap_sha"])
        if state != "success":
            problems.append(f"check du socle non réussi ({state}) : {reason}")
    ours, collisions = _ruleset_collisions(_find_rulesets(api))
    problems.extend(collisions)
    if ours is None:
        problems.append("ruleset des bases éphémères absent")
    elif _normalize_ruleset(ours) != _normalize_ruleset(plan["ruleset"]):
        problems.append("ruleset différent du payload du plan")
    seed_ruleset = _get(api, f"/repos/{REPOSITORY}/rulesets/{SEED_RULESET_ID}", missing_ok=True)
    if not seed_ruleset or seed_ruleset.get("enforcement") != "active":
        problems.append("ruleset de la graine modifié ou absent")
    app = _get(api, "/apps/github-actions", missing_ok=True)
    if not app or app.get("id") != ACTIONS_APP_ID:
        problems.append("l'application github-actions n'a pas l'identifiant attendu")
    return {"ok": not problems, "problems": problems, "ruleset_id": (ours or {}).get("id")}


# ── écritures : apply / cleanup (jeton d'ordre obligatoire) ───────────────────────────────────────────────────────────────


def _require_order(plan: Mapping[str, Any], token: Optional[str]) -> None:
    if token != plan["order_token"]:
        raise FixtureError("jeton d'ordre absent ou inexact : aucune écriture distante")


def wait_for_bootstrap_check(
    api: Api,
    plan: Mapping[str, Any],
    *,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    timeout: float = 600.0,
) -> None:
    """Attend le check ``Fixture tests`` (application Actions) RÉUSSI sur le commit du socle ; sinon refuse (jamais un succès présumé)."""
    started = clock()
    while True:
        runs = api("GET", f"/repos/{REPOSITORY}/commits/{plan['bootstrap_sha']}/check-runs", None).get("check_runs", [])
        state, reason = evaluate_check(runs, head_sha=plan["bootstrap_sha"])
        if state == "success":
            return
        if state == "failure":
            raise FixtureError(f"le check du socle est rouge ({reason}) : aucun ruleset créé, état à examiner")
        if clock() - started >= timeout:
            raise FixtureError(
                f"aucun check réussi sur le socle après {timeout:.0f} s ({state} : {reason}) : le workflow ne s'est pas déclenché "
                "ou n'a pas terminé ; aucun ruleset créé (les bases ne pourraient pas être créées sous la règle de création)"
            )
        sleep(10)


def apply_plan(
    api: Api,
    plan: Mapping[str, Any],
    *,
    order_token: Optional[str],
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    check_timeout: float = 600.0,
) -> Dict[str, Any]:
    """Crée la branche du socle, ATTEND son check réussi, puis crée le ruleset. Idempotent ; refuse toute collision ; ne touche ni ``main`` ni les ressources étrangères."""
    _require_order(plan, order_token)
    _check_identity(api)
    app = _get(api, "/apps/github-actions")
    if app.get("id") != ACTIONS_APP_ID:
        raise FixtureError("l'application github-actions n'a pas l'identifiant attendu")
    seed_ruleset = _get(api, f"/repos/{REPOSITORY}/rulesets/{SEED_RULESET_ID}", missing_ok=True)
    if not seed_ruleset or seed_ruleset.get("enforcement") != "active":
        raise FixtureError("ruleset de la graine absent ou inactif : aucune écriture")
    ours, collisions = _ruleset_collisions(_find_rulesets(api))
    if collisions:
        raise FixtureError("collision de ruleset : " + " ; ".join(collisions))
    if ours is not None and _normalize_ruleset(ours) != _normalize_ruleset(plan["ruleset"]):
        # AVANT toute écriture : un ruleset homonyme au contenu différent n'est pas possédé (aucun état partiel).
        raise FixtureError(f"le ruleset {RULESET_NAME!r} existe avec un autre contenu : ressource non possédée")
    boot = _get(api, f"/repos/{REPOSITORY}/git/ref/heads/{BOOTSTRAP_BRANCH}", missing_ok=True)
    created: List[str] = []
    if boot is not None and boot["object"]["sha"] != plan["bootstrap_sha"]:
        raise FixtureError(
            f"la branche {BOOTSTRAP_BRANCH} existe à un autre SHA ({boot['object']['sha']}) : ressource non possédée"
        )
    if boot is None:
        calls = api_call_log(plan)
        tree = api(calls[0]["method"], calls[0]["path"], calls[0]["payload"])
        if tree.get("sha") != plan["bootstrap_tree_sha"]:
            raise FixtureError("l'arbre créé n'est pas celui du plan : arrêt avant tout commit")
        commit = api(calls[1]["method"], calls[1]["path"], calls[1]["payload"])
        if commit.get("sha") != plan["bootstrap_sha"]:
            raise FixtureError("le commit créé n'est pas celui du plan : arrêt avant toute branche")
        ref = api(calls[2]["method"], calls[2]["path"], calls[2]["payload"])
        if (ref.get("object") or {}).get("sha") != plan["bootstrap_sha"]:
            raise FixtureError("la branche créée ne pointe pas sur le commit du plan")
        created.append("branch")
    if ours is None:
        wait_for_bootstrap_check(api, plan, sleep=sleep, clock=clock, timeout=check_timeout)
        call = api_call_log(plan)[3]
        made = api(call["method"], call["path"], call["payload"])
        if made.get("enforcement") != "active" or made.get("bypass_actors"):
            raise FixtureError("le ruleset créé n'est pas actif sans bypass : à corriger avant toute campagne")
        ours = made
        created.append("ruleset")
    manifest = manifest_skeleton(plan)
    manifest["ruleset_id"] = ours.get("id")
    return {"created": created, "manifest": manifest, "idempotent": not created}


def cleanup_bootstrap(api: Api, plan: Mapping[str, Any], *, order_token: Optional[str]) -> Dict[str, Any]:
    """Supprime UNIQUEMENT le ruleset des bases éphémères et la branche du socle de CETTE campagne (identités exactes vérifiées)."""
    _require_order(plan, order_token)
    _check_identity(api)
    removed: List[str] = []
    ours, _ = _ruleset_collisions(_find_rulesets(api))
    if ours is not None:
        if _normalize_ruleset(ours) != _normalize_ruleset(plan["ruleset"]):
            raise FixtureError("le ruleset a un contenu inattendu : non possédé, non supprimé")
        api("DELETE", f"/repos/{REPOSITORY}/rulesets/{ours['id']}", None)
        removed.append(f"ruleset {ours['id']}")
    boot = _get(api, f"/repos/{REPOSITORY}/git/ref/heads/{BOOTSTRAP_BRANCH}", missing_ok=True)
    if boot is not None:
        if boot["object"]["sha"] != plan["bootstrap_sha"]:
            raise FixtureError("la branche du socle a un autre SHA : non possédée, non supprimée")
        api("DELETE", f"/repos/{REPOSITORY}/git/refs/heads/{BOOTSTRAP_BRANCH}", None)
        removed.append(f"branche {BOOTSTRAP_BRANCH}")
    return {"removed": removed}


# ── contre-épreuves du check (probe) ──────────────────────────────────────────────────────────────────────────────────────

PROBE_TEST_GREEN = "def test_probe_green() -> None:\n    assert True\n"
PROBE_TEST_RED = "def test_probe_red() -> None:\n    assert False, 'rouge volontaire (contre-épreuve du check)'\n"
PROBE_TOUCH = "\n# sonde : modification de test, sans effet fonctionnel\n"
PROBE_SYMLINK_PATH = "docs/lien-sonde"


def probe_scenarios(plan: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """Scénarios des contre-épreuves. ``check`` : verdict attendu du check requis ; ``merge`` : ``accepted`` ou ``refused`` (tentative RÉELLE
    de fusion dans la base jetable, sur la tête exacte). Les trois scénarios « touch » ont un check VERT : seul le propriétaire des chemins
    protégés peut alors expliquer le refus (isolation de la protection indépendante du workflow)."""
    files = plan["files"]
    green = {"tests/test_probe.py": PROBE_TEST_GREEN}
    scenarios = [
        {"id": "green", "files": green, "check": "success", "merge": "accepted", "provenance": True},
        {
            "id": "red-test",
            "files": {"tests/test_probe.py": PROBE_TEST_RED},
            "check": "failure",
            "merge": "refused",
            "spoof": True,
        },
        {
            "id": "workflow-touch",
            "files": {**green, WORKFLOW_PATH: files[WORKFLOW_PATH] + PROBE_TOUCH},
            "check": "success",
            "merge": "refused",
        },
        {
            "id": "codeowners-touch",
            "files": {**green, CODEOWNERS_PATH: files[CODEOWNERS_PATH] + PROBE_TOUCH},
            "check": "success",
            "merge": "refused",
        },
        {
            "id": "lock-touch",
            "files": {**green, APPROVED_LOCK_PATH: files[APPROVED_LOCK_PATH] + PROBE_TOUCH},
            "check": "success",
            "merge": "refused",
        },
        {
            "id": "unapproved-dependency",
            "files": {**green, REQUIREMENTS_PATH: files[REQUIREMENTS_PATH] + "requests==2.32.3\n"},
            "check": "failure",
            "merge": "refused",
        },
        {
            "id": "symlink",
            "files": {},
            "symlink": {PROBE_SYMLINK_PATH: REQUIREMENTS_PATH},
            "check": "failure",
            "merge": "refused",
        },
        {
            "id": "seed-base",
            "files": green,
            "base": "seed",
            "check": "missing",
            "merge": "refused",
            "creation": "refused",
        },
    ]
    for scenario in scenarios:
        # Le garde de fusion de confiance (arbre Git réel de la tête) doit REFUSER toute tête qui touche un chemin protégé ou ajoute un lien,
        # même quand le check est vert (workflow altéré qui s'est lui-même validé) ; il ne doit rien refuser d'autre.
        scenario["guard_refuses"] = (
            bool(scenario.get("symlink"))
            or scenario.get("base") == "seed"  # une base issue de la graine n'a pas les contrôles du socle
            or any(path.startswith(tuple(PROTECTED_PREFIXES)) for path in scenario["files"])
        )
    return scenarios


def _attempt_spoof(api: Api, head_sha: str, before: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    """Tente de publier un faux check « Fixture tests » vert avec le jeton de la campagne (une PAT n'est pas l'application Actions).

    Le refus de l'API est le cas nominal ; si elle accepte, le faux check ne doit tout de même PAS compter (autre application)."""
    try:
        api(
            "POST",
            f"/repos/{REPOSITORY}/check-runs",
            {"name": REQUIRED_CHECK, "head_sha": head_sha, "status": "completed", "conclusion": "success"},
        )
        accepted = True
    except ApiError as exc:
        accepted = False
        refusal = str(exc)
    runs = api("GET", f"/repos/{REPOSITORY}/commits/{head_sha}/check-runs", None).get("check_runs", [])
    state, _reason = evaluate_check(runs, head_sha=head_sha)

    def authentic(items: Sequence[Mapping[str, Any]]) -> int:
        return sum(
            1
            for run in items
            if run.get("name") == REQUIRED_CHECK
            and (run.get("app") or {}).get("id") == ACTIONS_APP_ID
            and run.get("head_sha") == head_sha
        )

    # « compte » : un check de l'application Actions est apparu du fait de la tentative, ou le verdict est devenu un succès.
    counted = authentic(runs) > authentic(before) or state == "success"
    result: Dict[str, Any] = {"accepted_by_api": accepted, "state_after": state, "counted": counted}
    if not accepted:
        result["refusal"] = refusal
    return result


def _put_files(api: Api, head: str, files: Mapping[str, str], message: str) -> str:
    head_sha = ""
    for path, text in files.items():
        existing = _get(api, f"/repos/{REPOSITORY}/contents/{path}?ref={head}", missing_ok=True)
        body: Dict[str, Any] = {
            "message": message,
            "content": base64.b64encode(text.encode()).decode(),
            "branch": head,
        }
        if existing:
            body["sha"] = existing["sha"]
        head_sha = api("PUT", f"/repos/{REPOSITORY}/contents/{path}", body)["commit"]["sha"]
    return head_sha


def _commit_symlinks(api: Api, head: str, head_sha: str, links: Mapping[str, str], message: str) -> str:
    """Pousse un commit contenant des LIENS SYMBOLIQUES (mode 120000) par l'API Git Data (l'API Contents ne sait pas en écrire)."""
    parent = api("GET", f"/repos/{REPOSITORY}/git/commits/{head_sha}", None)
    entries = []
    for path, target in links.items():
        blob = api("POST", f"/repos/{REPOSITORY}/git/blobs", {"content": target, "encoding": "utf-8"})
        entries.append({"path": path, "mode": "120000", "type": "blob", "sha": blob["sha"]})
    tree = api("POST", f"/repos/{REPOSITORY}/git/trees", {"base_tree": parent["tree"]["sha"], "tree": entries})
    commit = api(
        "POST",
        f"/repos/{REPOSITORY}/git/commits",
        {"message": message, "tree": tree["sha"], "parents": [head_sha]},
    )
    api("PATCH", f"/repos/{REPOSITORY}/git/refs/heads/{head}", {"sha": commit["sha"], "force": False})
    return commit["sha"]


def _try_merge(api: Api, pr_number: int, head_sha: str) -> Dict[str, Any]:
    """Tentative RÉELLE de fusion dans la base jetable, sur la tête exacte : acceptée, ou refusée avec le statut et le motif de l'API."""
    try:
        result = api(
            "PUT",
            f"/repos/{REPOSITORY}/pulls/{pr_number}/merge",
            {"sha": head_sha, "merge_method": "merge"},
        )
    except ApiError as exc:
        return {"accepted": False, "status": exc.status, "message": str(exc)[:300]}
    return {"accepted": bool(result.get("merged")), "status": 200, "message": str(result.get("message", ""))[:200]}


def run_probe(
    api: Api,
    plan: Mapping[str, Any],
    *,
    order_token: Optional[str],
    probe_id: str,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    timeout: float = 600.0,
    observe_missing_seconds: float = 120.0,
) -> Dict[str, Any]:
    """PR jetables vers des bases éphémères de sonde ; toutes les ressources créées sont supprimées, même sur échec."""
    _require_order(plan, order_token)
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{2,30}", probe_id):
        raise FixtureError("identifiant de sonde invalide")
    verdict = verify_remote(api, plan)
    if not verdict["ok"]:
        raise FixtureError("l'état distant n'est pas celui du plan (verify) : " + " ; ".join(verdict["problems"]))
    results = []
    for index, scenario in enumerate(probe_scenarios(plan)):
        name = scenario["id"]
        base = f"collegue-business/probe-{probe_id}-{index}"
        head = f"collegue-probe/{probe_id}-{index}"
        base_sha = SEED_SHA if scenario.get("base") == "seed" else plan["bootstrap_sha"]
        owned_refs: List[str] = []
        pr_number: Optional[int] = None
        outcome: Dict[str, Any] = {
            "scenario": name,
            "check_expected": scenario["check"],
            "merge_expected": scenario["merge"],
        }
        try:
            try:
                api("POST", f"/repos/{REPOSITORY}/git/refs", {"ref": f"refs/heads/{base}", "sha": base_sha})
                owned_refs.append(base)
                outcome["creation"] = "accepted"
            except ApiError as exc:
                outcome["creation"] = "refused"
                outcome["creation_error"] = str(exc)[:300]
                outcome["ok"] = scenario.get("creation") == "refused"
                continue
            if scenario.get("creation") == "refused":
                # la règle de création devait refuser une base sans check : acceptée, on observe alors la PR (le refus de fusion suffit)
                outcome["note"] = "création acceptée : la règle de création n'a pas refusé la base issue de la graine"
            api("POST", f"/repos/{REPOSITORY}/git/refs", {"ref": f"refs/heads/{head}", "sha": base_sha})
            owned_refs.append(head)
            head_sha = base_sha
            if scenario["files"]:
                head_sha = _put_files(api, head, scenario["files"], f"sonde {name}")
            if scenario.get("symlink"):
                head_sha = _commit_symlinks(api, head, head_sha, scenario["symlink"], f"sonde {name}")
            pr = api(
                "POST",
                f"/repos/{REPOSITORY}/pulls",
                {"title": f"sonde W5 {name}", "head": head, "base": base, "body": "Contre-épreuve du check (jetable)."},
            )
            pr_number = pr["number"]
            started = clock()
            state, reason = "missing", "non observé"
            runs: List[Mapping[str, Any]] = []
            while True:
                runs = api("GET", f"/repos/{REPOSITORY}/commits/{head_sha}/check-runs", None).get("check_runs", [])
                state, reason = evaluate_check(runs, head_sha=head_sha)
                elapsed = clock() - started
                if scenario["check"] == "missing":
                    if state != "missing" or elapsed >= observe_missing_seconds:
                        break  # un check apparu avant la fin de la fenêtre = échec de la contre-épreuve
                elif state in ("success", "failure") or elapsed >= timeout:
                    break
                sleep(10)
            outcome.update(
                check_observed=state,
                reason=reason,
                head_sha=head_sha,
                checks_seen=[
                    {"name": r.get("name"), "app_id": (r.get("app") or {}).get("id"), "head_sha": r.get("head_sha")}
                    for r in runs
                ],
            )
            if scenario.get("spoof"):
                outcome["spoof"] = _attempt_spoof(api, head_sha, runs)
            # garde de fusion de confiance sur l'arbre Git RÉEL de la tête, pour chaque scénario
            tree = api("GET", f"/repos/{REPOSITORY}/git/trees/{head_sha}?recursive=1", None)
            violations = ["arbre tronqué : contenu protégé non établi"] if tree.get("truncated") else []
            violations += protected_tree_violations(tree.get("tree", []), plan)
            outcome["guard"] = {"refuses": bool(violations), "violations": violations[:5]}
            if scenario.get("provenance"):
                ok, why = check_provenance(api, head_sha, plan)
                outcome["provenance"] = {"ok": ok, "reason": why}
            merge = _try_merge(api, pr_number, head_sha)
            outcome["merge"] = merge
            merged_as_expected = merge["accepted"] == (scenario["merge"] == "accepted")
            outcome["ok"] = (
                state == scenario["check"]
                and merged_as_expected
                and outcome.get("spoof", {}).get("counted", False) is False
                and outcome.get("provenance", {"ok": True})["ok"] is True
                and outcome["guard"]["refuses"] is scenario["guard_refuses"]
            )
        except ApiError as exc:
            outcome.update(ok=False, error=str(exc)[:300])
        finally:
            if pr_number is not None:
                try:
                    api("PATCH", f"/repos/{REPOSITORY}/pulls/{pr_number}", {"state": "closed"})
                except ApiError:
                    pass
            for ref in reversed(owned_refs):
                try:
                    api("DELETE", f"/repos/{REPOSITORY}/git/refs/heads/{ref}", None)
                except ApiError:
                    outcome.setdefault("cleanup_errors", []).append(ref)
            results.append(outcome)
    return {"ok": all(r.get("ok") for r in results), "results": results}


# ── CLI ───────────────────────────────────────────────────────────────────────────────────────────────────────────────────


def _api_from_env() -> Api:
    client = GitHubApi(os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or "")
    return client.request


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    plan_cmd = sub.add_parser("plan", help="plan HORS LIGNE (aucun accès réseau)")
    plan_cmd.add_argument("directory", type=Path)
    for name in ("inspect", "verify"):
        sub.add_parser(name, help="lecture seule")
    for name in ("apply", "probe", "cleanup"):
        cmd = sub.add_parser(name, help="écritures distantes, sur ordre")
        cmd.add_argument("--order-token", required=True)
        if name == "probe":
            cmd.add_argument("--probe-id", required=True)
        if name == "apply":
            cmd.add_argument("--manifest-out", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "plan":
            plan = write_plan(args.directory)
            print(json.dumps({"bootstrap_sha": plan["bootstrap_sha"], "order_token": plan["order_token"]}, indent=1))
            return 0
        plan = build_plan()
        api = _api_from_env()
        if args.command == "inspect":
            print(json.dumps(inspect_remote(api, plan), indent=1, sort_keys=True))
            return 0
        if args.command == "verify":
            result = verify_remote(api, plan)
            print(json.dumps(result, indent=1, sort_keys=True))
            return 0 if result["ok"] else 1
        if args.command == "apply":
            result = apply_plan(api, plan, order_token=args.order_token)
            args.manifest_out.write_text(
                json.dumps(result["manifest"], indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
            print(json.dumps({k: v for k, v in result.items() if k != "manifest"}, indent=1))
            return 0
        if args.command == "probe":
            result = run_probe(api, plan, order_token=args.order_token, probe_id=args.probe_id)
            print(json.dumps(result, indent=1, sort_keys=True))
            return 0 if result["ok"] else 1
        result = cleanup_bootstrap(api, plan, order_token=args.order_token)
        print(json.dumps(result, indent=1))
        return 0
    except (FixtureError, ApiError) as exc:
        print(f"REFUS: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
