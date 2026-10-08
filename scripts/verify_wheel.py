#!/usr/bin/env python3
"""Vérification de bout en bout du wheel : environnement VIERGE, dépendances VERROUILLÉES, hors checkout.

Étapes (toutes obligatoires, la première défaillance fait échouer le script avec un code non nul) :

1. exporte les fichiers du dépôt (suivis + nouveaux non ignorés) et construit le wheel ;
2. crée un virtualenv neuf sans pip (``--python`` au choix : 3.11, 3.12…), y installe
   ``pip install --require-hashes --no-deps -r locks/<verrou>.txt`` puis le wheel (NON éditable, ``--no-deps``) ;
3. lance, avec ``PYTHONPATH`` nettoyé et depuis un répertoire vide hors checkout, un contrôle qui vérifie :
   le chemin effectif d'import (venv, jamais le dépôt), toutes les ressources annoncées (skills, templates,
   règles, catégories), l'absence d'écriture dans le paquet installé, les migrations jusqu'à ``head`` sur une base
   SQLite vide, une seconde exécution idempotente, la reprise d'une base partielle, et le parcours minimal du
   serveur (démarrage, outils, skills) **sans appel réseau** (``socket.connect`` interdit, aucun LLM).

Sortie : un rapport JSON (empreintes du wheel et des verrous, versions Python et paquets) sur stdout et, avec
``--report``, dans un fichier. ``--keep`` conserve le répertoire de travail pour inspection.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Exécuté DANS le venv, hors checkout. Imprime un JSON ; lève au premier écart.
CHECK_CODE = r"""
import asyncio, json, os, re, socket, subprocess, sys, sqlite3
from pathlib import Path

repo_root = os.environ["VERIFY_REPO_ROOT"]
home = Path(os.environ["COLLEGUE_HOME"])
report = {"python": sys.version.split()[0]}

def fail(message):
    raise SystemExit("ÉCART: " + message)

import collegue
pkg = Path(collegue.__file__).resolve()
report["collegue"] = str(pkg)
if str(pkg).startswith(repo_root):
    fail(f"collegue importé depuis le checkout : {pkg}")
if "site-packages" not in pkg.parts:
    fail(f"collegue n'est pas dans un site-packages : {pkg}")
if Path.cwd().resolve().as_posix().startswith(repo_root):
    fail("cwd dans le checkout")
if any(Path(p).resolve().as_posix() == repo_root for p in sys.path if p):
    fail("le checkout est dans sys.path")
site = pkg.parent

def snapshot():
    return {str(p.relative_to(site)): p.stat().st_size for p in sorted(site.rglob("*")) if p.is_file() and "__pycache__" not in p.parts}

before = snapshot()

# --- ressources ------------------------------------------------------------------------------------------
from collegue.resources.skills import get_skills_dir
from collegue.prompts.templates import list_available_templates
from collegue.core.shared import load_rules
from collegue.prompts.storage import seed_categories_file, seed_templates_dir

skills = get_skills_dir()
if not str(skills).startswith(str(site)):
    fail(f"skills hors du paquet installé : {skills}")
skill_names = sorted(p.parent.name for p in skills.glob("*/SKILL.md"))
if skill_names != ["cicd-pipeline", "code-review", "collegue-toolkit", "refactoring-guide", "security-audit"]:
    fail(f"skills incomplètes : {skill_names}")
templates = list_available_templates()
yaml_count = len(list(seed_templates_dir().rglob("*.yaml")))
if yaml_count < 10 or "refactoring" not in templates:
    fail(f"templates incomplets : {yaml_count} yaml, {sorted(templates)}")
if not json.loads(seed_categories_file().read_text("utf-8")):
    fail("categories.json vide")
rules = {n: len(load_rules(n)) for n in ("k8s.yaml", "terraform.yaml", "dockerfile.yaml")}
if not all(rules.values()):
    fail(f"règles IaC vides : {rules}")
report["resources"] = {"skills": skill_names, "yaml_templates": yaml_count, "template_tools": sorted(templates), "rules": rules}

from collegue.prompts.engine.enhanced_prompt_engine import EnhancedPromptEngine
engine = EnhancedPromptEngine()
engine._save_library()
if len(engine.library.templates) < 10:
    fail(f"moteur de prompts : {len(engine.library.templates)} templates chargés")
if not (home / "prompts" / "categories.json").is_file():
    fail("état des prompts non écrit sous COLLEGUE_HOME")
report["prompt_engine_templates"] = len(engine.library.templates)

# --- migrations (CLI documentée, puis API) -------------------------------------------------------------
heads = subprocess.run([sys.executable, "-m", "collegue.migrations", "heads"], capture_output=True, text=True)
if heads.returncode != 0 or not heads.stdout.strip():
    fail("collegue.migrations heads : " + heads.stderr)
head = heads.stdout.split()[-1]
versions = sorted(re.match(r"(\d{4})_", p.name).group(1) for p in (site / "migrations" / "versions").glob("[0-9]*.py"))
if versions[:10] != [f"{i:04d}" for i in range(1, 11)] or versions[-1] != head:
    fail(f"graphe de migrations incohérent : {versions} head={head}")

def migrate(*args):
    return subprocess.run([sys.executable, "-m", "collegue.migrations", *args], capture_output=True, text=True)

work = Path(os.environ["VERIFY_WORKDIR"])
fresh = f"sqlite:///{work / 'fresh.sqlite3'}"
for attempt in ("première exécution", "seconde exécution (idempotente)"):
    run = migrate("upgrade", "--url", fresh)
    if run.returncode != 0:
        fail(f"migrations {attempt}: {run.stdout}{run.stderr}")
con = sqlite3.connect(work / "fresh.sqlite3")
tables = {r[0] for r in con.execute("select name from sqlite_master where type='table'")}
version = con.execute("select version_num from alembic_version").fetchall()
con.close()
if version != [(head,)] or not {"projects", "tasks", "decisions", "metrics", "checkpoints"} <= tables:
    fail(f"schéma inattendu : {version} {sorted(tables)}")

partial = f"sqlite:///{work / 'partial.sqlite3'}"
if migrate("upgrade", "--url", partial, "--revision", "0005").returncode != 0:
    fail("migration partielle 0005")
if migrate("current", "--url", partial).stdout.strip() != "0005":
    fail("révision partielle inattendue")
if migrate("upgrade", "--url", partial).returncode != 0:
    fail("reprise de la base partielle")
if migrate("current", "--url", partial).stdout.strip() != head:
    fail("la reprise n'atteint pas head")
report["migrations"] = {"head": head, "count": len(versions), "tables": sorted(tables)}

# --- parcours minimal du serveur, sans réseau ---------------------------------------------------------
def _no_network(*a, **k):
    raise AssertionError("appel réseau interdit pendant la vérification")
socket.socket.connect = _no_network
os.environ["STATE_DATABASE_URL"] = f"sqlite:///{work / 'server.sqlite3'}"
import collegue.app as server
from fastmcp import Client

async def serve():
    async with Client(server.app) as client:
        tools = await client.list_tools()
        resources = await client.list_resources()
        return len(tools), sorted(str(r.uri) for r in resources if str(r.uri).startswith("skill://") and str(r.uri).endswith("/SKILL.md"))

tool_count, skill_uris = asyncio.run(serve())
if tool_count < 15 or len(skill_uris) != 5:
    fail(f"serveur : {tool_count} outils, skills {skill_uris}")
report["server"] = {"tools": tool_count, "skill_resources": skill_uris, "module": server.__file__}

if snapshot() != before:
    fail("le paquet installé a été modifié pendant l'exécution")
print("VERIFY_JSON:" + json.dumps(report))
"""


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def repo_files() -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=ROOT,
        capture_output=True,
        check=True,
    ).stdout
    files = [Path(item.decode()) for item in out.split(b"\0") if item]
    return [path for path in dict.fromkeys(files) if (ROOT / path).is_file()]


def clean_env(**extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in {"PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "COLLEGUE_HOME"}}
    env.update(extra)
    return env


def run(command: list[str], **kwargs) -> subprocess.CompletedProcess:
    print("$", " ".join(str(c) for c in command), flush=True)
    result = subprocess.run(command, text=True, capture_output=True, **kwargs)
    if result.returncode != 0:
        sys.stderr.write(result.stdout[-4000:] + result.stderr[-4000:])
        raise SystemExit(f"échec ({result.returncode}) : {' '.join(str(c) for c in command[:6])}")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--python", default=sys.executable, help="interpréteur du venv jetable (3.11, 3.12…)")
    parser.add_argument("--lock", default="runtime", help="verrou installé avant le wheel (locks/<nom>.txt)")
    parser.add_argument("--report", type=Path, help="écrit le rapport JSON dans ce fichier")
    parser.add_argument("--workdir", type=Path, help="répertoire de travail (défaut : temporaire)")
    parser.add_argument("--keep", action="store_true", help="conserve le répertoire de travail")
    args = parser.parse_args(argv)

    lock = ROOT / "locks" / f"{args.lock}.txt"
    if not lock.is_file():
        raise SystemExit(f"verrou introuvable : {lock}")

    base = args.workdir or Path(tempfile.mkdtemp(prefix="verify-wheel-"))
    base.mkdir(parents=True, exist_ok=True)
    try:
        export = base / "export"
        for relative in repo_files():
            target = export / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / relative, target)

        wheelhouse = base / "wheelhouse"
        run(
            [
                sys.executable,
                "-m",
                "pip",
                "wheel",
                "--no-deps",
                "--disable-pip-version-check",
                "-w",
                str(wheelhouse),
                ".",
            ],
            cwd=export,
            env=clean_env(),
        )
        wheel = next(wheelhouse.glob("collegue-*.whl"))
        with zipfile.ZipFile(wheel) as archive:
            names = archive.namelist()

        venv = base / "venv"
        # Sans pip dans le venv (certains interpréteurs système n'ont pas ensurepip) : l'installation est pilotée
        # par le pip de l'interpréteur courant via `--python`, ce qui n'ajoute AUCUN outil non verrouillé au venv.
        run([args.python, "-m", "venv", "--without-pip", str(venv)], env=clean_env())
        python = venv / "bin" / "python"
        pip = [sys.executable, "-m", "pip", "--python", str(python), "--disable-pip-version-check"]
        run([*pip, "install", "--require-hashes", "--no-deps", "-r", str(lock)], env=clean_env())
        run([*pip, "install", "--no-deps", "--no-index", str(wheel)], env=clean_env())

        workdir = base / "elsewhere"
        workdir.mkdir()
        home = base / "collegue-home"
        completed = subprocess.run(
            [str(python), "-I", "-c", CHECK_CODE],
            cwd=workdir,
            env=clean_env(
                VERIFY_REPO_ROOT=str(ROOT.resolve()),
                VERIFY_WORKDIR=str(workdir),
                COLLEGUE_HOME=str(home),
                LLM_PROVIDER="anthropic",
                LLM_API_KEY="test-key",
                LLM_MODEL="test-model",
                FASTMCP_CHECK_FOR_UPDATES="off",
            ),
            text=True,
            capture_output=True,
            timeout=900,
        )
        if completed.returncode != 0:
            sys.stderr.write(completed.stdout[-4000:] + completed.stderr[-6000:])
            return 1
        payload = json.loads(next(ln for ln in completed.stdout.splitlines() if ln.startswith("VERIFY_JSON:"))[12:])

        freeze = run([*pip, "freeze"], env=clean_env()).stdout.splitlines()
        report = {
            "python": payload["python"],
            "wheel": {"name": wheel.name, "sha256": sha256(wheel), "entries": len(names)},
            "lock": {"name": lock.name, "sha256": sha256(lock)},
            "locks": {p.name: sha256(p) for p in sorted((ROOT / "locks").glob("*.txt"))},
            "pyproject_sha256": sha256(ROOT / "pyproject.toml"),
            "installed_packages": len(freeze),
            "checks": payload,
        }
        text = json.dumps(report, indent=2, sort_keys=True)
        print(text)
        if args.report:
            args.report.write_text(text + "\n", encoding="utf-8")
        return 0
    finally:
        if not args.keep and args.workdir is None:
            shutil.rmtree(base, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
