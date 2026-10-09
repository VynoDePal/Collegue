#!/usr/bin/env python3
"""Socle, protection et nettoyage du dépôt fixture W5 (propriété C) — plan hors ligne, application sur ordre seulement.

Dépôt : ``VynoDePal/collegue-e2e-fixture`` (id 1298596453). ``main`` est la GRAINE immuable (``8e3691d8…``, ruleset 18840666
« Immutable nightly seed ») : ce script n'y touche jamais. Le socle W5 est un commit DÉTERMINISTE au-dessus de la graine, publié sur
la branche ``collegue-business/bootstrap-w5`` ; il ajoute uniquement le workflow de confiance « Fixture tests », des documents de
scénario de B (deux runbooks d'exemples FACTICES pour R04/R05), **sans modifier aucun fichier de la graine** ni implémenter les trois
tâches métier. Les bases éphémères des campagnes se créent depuis ce commit, sous ``collegue-business/<run>``.

Sous-commandes (une seule lance des écritures distantes : ``apply``, ``probe`` et ``cleanup``, sur jeton d'ordre) :

* ``plan DIR`` — HORS LIGNE : calcule le commit du socle (SHA identique à ``git``), les payloads REST exacts, le ruleset, le manifeste
  ``collegue-fixture-bootstrap/1`` (squelette), les diffs et un journal ordonné des appels ; écrit ``DIR/*`` et son empreinte ;
* ``inspect`` — LECTURE SEULE de l'état distant (identité, graine, rulesets, branche du socle, PR étrangères) ;
* ``verify`` — LECTURE SEULE : l'état distant est exactement celui du plan (arbre du socle, workflow, ruleset, ``main`` intact) ;
* ``apply --order-token T`` — crée la branche du socle puis le ruleset ; idempotent, refuse toute collision ou ressource non possédée ;
* ``probe --order-token T`` — contre-épreuves du check (test rouge, workflow altéré, check manquant) sur PR jetables, toutes
  nettoyées ;
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
PROTECTED_PREFIXES = (".github/",)  # chemins dont toute modification est une atteinte à l'intégrité des contrôles

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

TRUSTED_WORKFLOW = r"""# Workflow de CONFIANCE du dépôt fixture (socle W5, approuvé par empreinte dans le manifeste).
#
# ``pull_request_target`` : GitHub exécute le fichier du workflow tel qu'il est sur la BRANCHE DE BASE de la PR (la base éphémère
# ``collegue-business/<run>``, issue du socle), jamais celui de la tête de la PR : une PR qui modifie ce fichier ne change pas le
# contrôle qui la juge (et l'étape « garde » refuse toute modification de ``.github/``).
#
# Le check automatique d'un job ``pull_request_target`` porte sur la BASE, pas sur la tête de la PR : il n'est donc JAMAIS le check
# requis. Le check requis « Fixture tests » est publié par la dernière étape de confiance, par l'API Checks et le jeton de
# l'application github-actions, sur la tête EXACTE (``head.sha``) ; une PAT ne peut pas créer de check-run. La contre-épreuve
# distante (``probe``) vérifie ``head_sha``, l'application et le nom ; tant qu'elle n'a pas réussi, rien n'est présenté comme prêt.
#
# Le code candidat (``requirements.txt`` compris) ne s'exécute jamais sur l'hôte du runner : téléchargement des roues dans un
# conteneur sans privilège ni secret ni socket Docker, puis installation et tests dans un second conteneur SANS RÉSEAU, utilisateur
# non root, capacités retirées, système de fichiers en lecture seule. Le jeton du workflow n'est visible que de l'étape de garde et de
# l'étape de publication, jamais d'un conteneur candidat.
name: Fixture tests

on:
  pull_request_target:
    types: [opened, synchronize, reopened]
    branches:
      - "collegue-business/**"

permissions:
  contents: read
  pull-requests: read
  checks: write

concurrency:
  group: fixture-tests-${{ github.event.pull_request.number }}
  cancel-in-progress: true

jobs:
  fixture-runner:
    name: Fixture runner
    runs-on: ubuntu-latest
    timeout-minutes: 15
    env:
      HEAD_SHA: ${{ github.event.pull_request.head.sha }}
      PR_NUMBER: ${{ github.event.pull_request.number }}
      PY_IMAGE: python:3.12-slim
    steps:
      - name: Extraire la tête exacte de la PR (sans identifiants conservés)
        uses: actions/checkout@v4
        with:
          ref: ${{ github.event.pull_request.head.sha }}
          persist-credentials: false
          fetch-depth: 1

      - name: Garde - la PR ne modifie pas le contrôle (.github/)
        id: guard
        env:
          GH_TOKEN: ${{ github.token }}
        run: |
          gh api --paginate "repos/${GITHUB_REPOSITORY}/pulls/${PR_NUMBER}/files" \
            --jq '.[] | .filename, (.previous_filename // empty)' > "$RUNNER_TEMP/changed-files.txt"
          if grep -E '^\.github(/|$)' "$RUNNER_TEMP/changed-files.txt"; then
            echo "::error::la PR modifie .github/ : refus (le contrôle ne se juge pas lui-même)"
            exit 1
          fi

      - name: Télécharger les roues déclarées (conteneur sans privilège, sans secret)
        id: wheels
        if: steps.guard.outcome == 'success'
        run: |
          chmod -R a+rX "$GITHUB_WORKSPACE"
          mkdir -p "$RUNNER_TEMP/wheels" && chmod 777 "$RUNNER_TEMP/wheels"
          docker run --rm --user 65534:65534 --cap-drop ALL --security-opt no-new-privileges \
            --read-only --tmpfs /tmp:rw,size=256m --memory 2g --pids-limit 256 -e HOME=/tmp \
            -v "$GITHUB_WORKSPACE/requirements.txt:/in/requirements.txt:ro" -v "$RUNNER_TEMP/wheels:/out:rw" \
            "$PY_IMAGE" python -m pip download --only-binary=:all: --no-input --disable-pip-version-check \
            -r /in/requirements.txt -d /out

      - name: Installer et tester (conteneur sans réseau, sans privilège, sans secret)
        id: tests
        if: steps.wheels.outcome == 'success'
        run: |
          docker run --rm --network none --user 65534:65534 --cap-drop ALL --security-opt no-new-privileges \
            --read-only --tmpfs /tmp:rw,exec,size=768m --memory 2g --pids-limit 512 \
            -e HOME=/tmp -e PYTHONDONTWRITEBYTECODE=1 \
            -v "$GITHUB_WORKSPACE:/src:ro" -v "$RUNNER_TEMP/wheels:/wheels:ro" \
            "$PY_IMAGE" bash -c 'set -euo pipefail
              mkdir /tmp/work && cp -R --no-preserve=all /src/. /tmp/work/ && cd /tmp/work
              python -m venv /tmp/venv
              /tmp/venv/bin/python -m pip install --no-index --find-links /wheels --only-binary=:all: --no-input -r requirements.txt
              /tmp/venv/bin/python -m pytest -q -p no:cacheprovider'

      - name: Publier le check « Fixture tests » sur la tête exacte
        if: ${{ !cancelled() }}
        env:
          GH_TOKEN: ${{ github.token }}
          GUARD: ${{ steps.guard.outcome }}
          WHEELS: ${{ steps.wheels.outcome }}
          TESTS: ${{ steps.tests.outcome }}
        run: |
          conclusion=failure
          if [ "$GUARD" = success ] && [ "$WHEELS" = success ] && [ "$TESTS" = success ]; then conclusion=success; fi
          gh api -X POST "repos/${GITHUB_REPOSITORY}/check-runs" \
            -f name="Fixture tests" -f head_sha="$HEAD_SHA" -f status=completed -f conclusion="$conclusion" \
            -f details_url="${GITHUB_SERVER_URL}/${GITHUB_REPOSITORY}/actions/runs/${GITHUB_RUN_ID}" \
            -f "output[title]=Fixture tests : ${conclusion}" \
            -f "output[summary]=garde=${GUARD} roues=${WHEELS} tests=${TESTS} tête=${HEAD_SHA}"
          test "$conclusion" = success
"""

# Documents d'EXEMPLE du socle, octet pour octet ceux de B (``tests/fixtures/w5-business/docs/``) : un test de recoupement les compare
# dès que le dossier de B est intégré. Identifiants FACTICES (exemples publiés dans la documentation d'AWS) ; R04 retire ceux du
# runbook, R05 garde ceux du déploiement. ``docs/export_header.md`` n'est PAS fourni : les trois tâches BUILD le créent.
RUNBOOK_OPS_DOC = "# Runbook d'exploitation\n\nProcédure d'exploitation du service d'audits (document d'EXEMPLE du socle de la fixture W5).\n\n## Accès à l'archivage des PDF\n\nPour exporter les rapports vers le stockage d'archivage, configurer les identifiants ci-dessous\n(valeurs factices publiées dans la documentation d'AWS, sans aucun accès réel) :\n\n    AWS_ACCESS_KEY_ID=AKIAI44QH8DHBEXAMPLE\n    AWS_SECRET_ACCESS_KEY=je7MtGbClwBF/2Zp9Utk/h3yCo8nvbEXAMPLEKEY\n\n## Sauvegarde\n\nSauvegarder le fichier SQLite chaque nuit puis vérifier que `alembic current` répond `0001`.\n"
DEPLOY_DOC = "# Déploiement\n\nL'export lit le fichier `docs/export_header.md`. Pour publier le service, exporter les identifiants :\n\n    AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE\n    AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY\n\nPuis lancer `alembic upgrade head` avant le premier démarrage.\n"


def scaffold_files() -> Dict[str, str]:
    """Fichiers AJOUTÉS par le socle à la graine (la graine est conservée telle quelle, aucun de ses fichiers n'est modifié)."""
    return {
        WORKFLOW_PATH: TRUSTED_WORKFLOW,
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
    added = dict(scaffold_files())
    already = sorted(path for path in added if path in seed)
    if already:
        raise FixtureError(f"le socle ne peut pas modifier la graine immuable : {already}")
    files = {**seed, **added}
    file_bytes = {path: text.encode("utf-8") for path, text in files.items()}
    tree = git_tree_sha(file_bytes)
    bootstrap_sha = git_commit_sha(tree, SEED_SHA)
    # fichiers approuvés = EXACTEMENT ce que le socle ajoute à la graine (contrat commun : B compare à l'arbre Git réel)
    approved = {path: hashlib.sha256(file_bytes[path]).hexdigest() for path in sorted(added)}
    changed: List[str] = []
    created = sorted(added)
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
        "modified_seed_files": changed,
        "protected_prefixes": list(PROTECTED_PREFIXES),
        "ruleset": ruleset,
        "ruleset_sha256": hashlib.sha256(json.dumps(ruleset, sort_keys=True).encode()).hexdigest(),
        "required_check": REQUIRED_CHECK,
        "check_app_id": ACTIONS_APP_ID,
        "branch_pattern": BRANCH_PATTERN,
    }


def ruleset_payload() -> Dict[str, Any]:
    """Ruleset ACTIF des bases éphémères : PR obligatoire, check « Fixture tests » de l'application Actions, base à jour, aucun bypass.

    ``do_not_enforce_on_create`` : la CRÉATION d'une base éphémère (ou d'une branche de sonde) ne peut pas exiger un check déjà passé sur
    un commit qui n'en a pas encore ; la protection porte sur les FUSIONS (PR + check sur la tête + base à jour), et la règle « PR
    obligatoire » interdit toute mise à jour directe d'une base existante. Pas de règle ``deletion`` : le nettoyage de campagne supprime ses propres bases éphémères ; pas de règle ``update`` pure : les
    fusions passent par la PR. ``main`` et son ruleset (graine) ne sont pas concernés par ce motif."""
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
                    "require_code_owner_review": False,
                    "require_last_push_approval": False,
                    "required_review_thread_resolution": False,
                    "allowed_merge_methods": ["merge", "squash", "rebase"],
                },
            },
            {
                "type": "required_status_checks",
                "parameters": {
                    "strict_required_status_checks_policy": True,
                    "do_not_enforce_on_create": True,
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
        # Le check requis n'est PAS le check automatique d'un job : il est publié par l'étape de confiance du workflow (voir plus haut).
        "check_producer": {
            "workflow": WORKFLOW_PATH,
            "trigger": "pull_request_target",
            "job": "Fixture runner",
            "publishes": REQUIRED_CHECK,
            "head_sha_expression": "github.event.pull_request.head.sha",
            "app_id": ACTIONS_APP_ID,
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
        "# Contre-épreuves du check « Fixture tests » (`probe`, sur ordre seulement)",
        "",
        f"Jeton d'ordre requis : `{plan['order_token']}`. Précondition : `verify` réussi (socle, ruleset et graine conformes au plan).",
        "Chaque scénario crée une base `collegue-business/probe-<id>-<n>` et une tête `collegue-probe/<id>-<n>`, ouvre une PR, observe le check "
        "sur la TÊTE EXACTE (nom, application 15368, `head_sha`), l'état de fusion, puis ferme la PR et supprime ses deux branches (même sur échec).",
        "",
        "| # | Scénario | Base | Fichiers ajoutés à la tête | Check attendu | PR fusionnable |",
        "|---|---|---|---|---|---|",
    ]
    for index, (scenario, files, base_kind, expected, mergeable) in enumerate(PROBE_SCENARIOS):
        base = "socle (avec workflow)" if base_kind == "bootstrap" else "graine (SANS workflow)"
        out.append(
            f"| {index} | `{scenario}` | {base} | {', '.join('`' + path + '`' for path in files)} | `{expected}` | {'oui' if mergeable else 'non'} |"
        )
    out += [
        "",
        "* `green` : prouve que le workflow SE DÉCLENCHE sur une PR vers une base éphémère et que le check est publié sur la tête exacte par "
        "l'application Actions. Absence de check dans le délai = échec de la contre-épreuve (jamais un succès présumé).",
        "* `red-test` : un vrai échec `pytest` donne un check ROUGE et une PR non fusionnable. Il tente ensuite de publier un faux check "
        "`Fixture tests` vert avec le jeton de la campagne : le refus de l'API (cas nominal) ou un check d'une autre application ne doit "
        "JAMAIS compter.",
        "* `forged-workflow` : la tête remplace le workflow de confiance par une version qui ne lance plus pytest ET contient un test rouge. "
        "Le workflow exécuté est celui de la BASE (`pull_request_target`) et la garde refuse toute modification de `.github/` : check rouge.",
        "* `missing-check` : base créée depuis la graine (sans workflow) : aucun check n'apparaît dans la fenêtre d'observation et la PR reste "
        "bloquée par le ruleset (un check manquant n'est pas un succès).",
        "",
        "Permissions nécessaires du jeton de la campagne (à confirmer par le manager avant `probe`) : contenu en écriture sur la fixture "
        "(branches, fichiers), pull requests en écriture ; si le dépôt refuse les workflows déclenchés par ce jeton ou si les Actions sont "
        "désactivées, `green` échoue et c'est le résultat à rapporter. Aucune PR étrangère (#4, #7) n'est touchée.",
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


def apply_plan(api: Api, plan: Mapping[str, Any], *, order_token: Optional[str]) -> Dict[str, Any]:
    """Crée la branche du socle puis le ruleset. Idempotent ; refuse toute collision ; ne touche ni ``main`` ni les ressources étrangères."""
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
PROBE_WORKFLOW_FORGED = TRUSTED_WORKFLOW.replace(
    "/tmp/venv/bin/python -m pytest -q -p no:cacheprovider", "true  # workflow altéré : toujours vert"
)
assert PROBE_WORKFLOW_FORGED != TRUSTED_WORKFLOW

PROBE_SCENARIOS = (
    # (id, fichiers ajoutés à la tête, base : « bootstrap » ou « seed », verdict attendu du check, PR fusionnable)
    ("green", {"tests/test_probe.py": PROBE_TEST_GREEN}, "bootstrap", "success", True),
    ("red-test", {"tests/test_probe.py": PROBE_TEST_RED}, "bootstrap", "failure", False),
    (
        "forged-workflow",
        {"tests/test_probe.py": PROBE_TEST_RED, WORKFLOW_PATH: PROBE_WORKFLOW_FORGED},
        "bootstrap",
        "failure",
        False,
    ),
    ("missing-check", {"tests/test_probe.py": PROBE_TEST_GREEN}, "seed", "missing", False),
)


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
    """Quatre PR jetables vers des bases éphémères de sonde ; toutes les ressources créées sont supprimées, même sur échec."""
    _require_order(plan, order_token)
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{2,30}", probe_id):
        raise FixtureError("identifiant de sonde invalide")
    verdict = verify_remote(api, plan)
    if not verdict["ok"]:
        raise FixtureError("l'état distant n'est pas celui du plan (verify) : " + " ; ".join(verdict["problems"]))
    results = []
    for index, (scenario, files, base_kind, expected, mergeable) in enumerate(PROBE_SCENARIOS):
        base = f"collegue-business/probe-{probe_id}-{index}"
        head = f"collegue-probe/{probe_id}-{index}"
        base_sha = plan["bootstrap_sha"] if base_kind == "bootstrap" else SEED_SHA
        owned_refs: List[str] = []
        pr_number: Optional[int] = None
        outcome: Dict[str, Any] = {"scenario": scenario, "expected": expected}
        try:
            api("POST", f"/repos/{REPOSITORY}/git/refs", {"ref": f"refs/heads/{base}", "sha": base_sha})
            owned_refs.append(base)
            api("POST", f"/repos/{REPOSITORY}/git/refs", {"ref": f"refs/heads/{head}", "sha": base_sha})
            owned_refs.append(head)
            head_sha = base_sha
            for path, text in files.items():
                existing = _get(api, f"/repos/{REPOSITORY}/contents/{path}?ref={head}", missing_ok=True)
                body: Dict[str, Any] = {
                    "message": f"sonde {scenario}",
                    "content": base64.b64encode(text.encode()).decode(),
                    "branch": head,
                }
                if existing:
                    body["sha"] = existing["sha"]
                head_sha = api("PUT", f"/repos/{REPOSITORY}/contents/{path}", body)["commit"]["sha"]
            pr = api(
                "POST",
                f"/repos/{REPOSITORY}/pulls",
                {
                    "title": f"sonde W5 {scenario}",
                    "head": head,
                    "base": base,
                    "body": "Contre-épreuve du check (jetable).",
                },
            )
            pr_number = pr["number"]
            started = clock()
            state, reason = "missing", "non observé"
            while True:
                runs = api("GET", f"/repos/{REPOSITORY}/commits/{head_sha}/check-runs", None).get("check_runs", [])
                state, reason = evaluate_check(runs, head_sha=head_sha)
                elapsed = clock() - started
                if expected == "missing":
                    if state != "missing" or elapsed >= observe_missing_seconds:
                        break  # un check apparu avant la fin de la fenêtre = échec de la contre-épreuve
                elif state in ("success", "failure") or elapsed >= timeout:
                    break
                sleep(10)
            outcome.update(
                observed=state,
                reason=reason,
                head_sha=head_sha,
                checks_seen=[
                    {"name": r.get("name"), "app_id": (r.get("app") or {}).get("id"), "head_sha": r.get("head_sha")}
                    for r in runs
                ],
            )
            if scenario == "red-test":
                outcome["spoof"] = _attempt_spoof(api, head_sha, runs)
            info = api("GET", f"/repos/{REPOSITORY}/pulls/{pr_number}", None)
            blocked = info.get("mergeable_state") in ("blocked", "behind", "dirty")
            outcome.update(mergeable_state=info.get("mergeable_state"), merge_blocked=blocked)
            outcome["ok"] = (
                state == expected
                and blocked == (not mergeable)
                and outcome.get("spoof", {}).get("counted", False) is False
            )
        except ApiError as exc:
            outcome.update(ok=False, error=str(exc))
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
