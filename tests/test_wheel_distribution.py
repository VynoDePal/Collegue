"""Le wheel construit depuis les fichiers du dépôt est autonome : ressources, skills, templates, migrations.

Défaut d'origine (W2) : le wheel ne contenait AUCUNE ressource YAML/JSON, aucun ``SKILL.md`` ni aucune
migration Alembic, et les loaders supposaient le checkout (``Path(__file__).parent.parent.parent``), le
répertoire courant ou ``/app``. Un paquet installé démarrait donc sans skills, sans templates et sans migrations.

Ces tests construisent un VRAI wheel à partir des fichiers du dépôt (suivis, y compris nouveaux et non
ignorés), l'installent (non éditable) dans un dossier vierge puis exécutent le code installé depuis un
répertoire quelconque, SANS ``PYTHONPATH`` ni accès au checkout. Aucun appel réseau applicatif, aucun LLM.
La construction utilise setuptools local s'il est présent, sinon l'isolation de build de pip (index de paquets).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BASELINE_MIGRATIONS = [
    ("0001", None),
    ("0002", "0001"),
    ("0003", "0002"),
    ("0004", "0003"),
    ("0005", "0004"),
    ("0006", "0005"),
    ("0007", "0006"),
    ("0008", "0007"),
    ("0009", "0008"),
    ("0010", "0009"),
]


def _repo_files() -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=ROOT,
        capture_output=True,
        check=True,
    ).stdout
    files = [Path(item.decode()) for item in out.split(b"\0") if item]
    return [path for path in dict.fromkeys(files) if (ROOT / path).is_file()]


def _clean_env(**extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in {"PYTHONPATH", "PYTHONHOME", "COLLEGUE_HOME"}}
    env.update(extra)
    return env


def _tree_digest(path: Path) -> dict[str, int]:
    return {str(p.relative_to(path)): p.stat().st_size for p in sorted(path.rglob("*")) if p.is_file()}


@pytest.fixture(scope="module")
def dist(tmp_path_factory: pytest.TempPathFactory) -> dict:
    base = tmp_path_factory.mktemp("wheel")
    export = base / "export"
    for relative in _repo_files():
        target = export / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, target)

    wheelhouse = base / "wheelhouse"
    command = [sys.executable, "-m", "pip", "wheel", "--no-deps", "--disable-pip-version-check", "-w", str(wheelhouse)]
    try:
        import setuptools  # noqa: F401

        command.append("--no-build-isolation")
    except ImportError:
        pass  # isolation de build : setuptools est tiré de l'index (version figée par [build-system])
    built = subprocess.run(command + ["."], cwd=export, env=_clean_env(), capture_output=True, text=True, timeout=600)
    assert built.returncode == 0, built.stdout[-3000:] + built.stderr[-3000:]
    wheels = list(wheelhouse.glob("collegue-*.whl"))
    assert len(wheels) == 1, wheels

    site = base / "site"
    installed = subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--no-deps",
            "--no-index",
            "--disable-pip-version-check",
            "--target",
            str(site),
            str(wheels[0]),
        ],
        env=_clean_env(),
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert installed.returncode == 0, installed.stdout[-2000:] + installed.stderr[-2000:]

    workdir = base / "elsewhere"  # répertoire quelconque, vide : aucun checkout, aucune ressource à portée
    workdir.mkdir()
    return {"wheel": wheels[0], "site": site, "workdir": workdir, "export": export, "base": base}


def _run(dist: dict, code: str, *, home: Path | None = None, extra_env: dict[str, str] | None = None, args=()):
    """Exécute ``code`` avec le paquet INSTALLÉ (site en tête de sys.path), cwd hors checkout, sans PYTHONPATH."""
    env = _clean_env(
        COLLEGUE_SITE=str(dist["site"]),
        LLM_PROVIDER="anthropic",
        LLM_API_KEY="test-key",
        LLM_MODEL="test-model",
        FASTMCP_CHECK_FOR_UPDATES="off",
        **(extra_env or {}),
    )
    if home is not None:
        env["COLLEGUE_HOME"] = str(home)
    prologue = "import os, sys; sys.path.insert(0, os.environ['COLLEGUE_SITE'])\n"
    return subprocess.run(
        [sys.executable, "-c", prologue + code, *args],
        cwd=dist["workdir"],
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )


def _json_out(completed: subprocess.CompletedProcess) -> dict:
    assert completed.returncode == 0, completed.stdout[-2000:] + completed.stderr[-3000:]
    line = [ln for ln in completed.stdout.splitlines() if ln.startswith("JSON:")][-1]
    return json.loads(line[5:])


def _wheel_names(dist: dict) -> list[str]:
    with zipfile.ZipFile(dist["wheel"]) as archive:
        return archive.namelist()


# --- contenu du wheel --------------------------------------------------------------------------


def test_wheel_embeds_every_skill_template_rule_and_migration(dist: dict) -> None:
    names = set(_wheel_names(dist))
    tracked = {p.as_posix() for p in _repo_files()}

    skills = {n for n in names if n.startswith("collegue/skills/")}
    assert {n for n in names if n.endswith("SKILL.md")} == {
        f"collegue/skills/{d}/SKILL.md"
        for d in ("cicd-pipeline", "code-review", "collegue-toolkit", "refactoring-guide", "security-audit")
    }
    assert skills >= {t for t in tracked if t.startswith("collegue/skills/")}, "fichiers de skills manquants"

    for prefix, suffixes in (
        ("collegue/prompts/templates/", (".yaml", ".json")),
        ("collegue/tools/rules/", (".yaml",)),
    ):
        expected = {t for t in tracked if t.startswith(prefix) and t.endswith(suffixes)}
        assert expected, prefix
        assert expected <= names, sorted(expected - names)

    versions = {n for n in names if re.match(r"collegue/migrations/versions/\d{4}_.*\.py$", n)}
    expected_versions = {t for t in tracked if re.match(r"collegue/migrations/versions/\d{4}_.*\.py$", t)}
    assert len(expected_versions) >= 10
    assert versions == expected_versions
    assert {"collegue/migrations/env.py", "collegue/migrations/script.py.mako"} <= names


def test_wheel_does_not_ship_tests_or_local_state(dist: dict) -> None:
    names = _wheel_names(dist)

    assert not [n for n in names if n.startswith(("tests/", "locks/", "scripts/", "docs/"))]
    assert not [n for n in names if "/__pycache__/" in n or n.endswith(".pyc")]
    assert not [n for n in names if n.endswith("versions.json")]


def test_wheel_declares_the_migration_entry_point_and_the_dashboard_extra(dist: dict) -> None:
    with zipfile.ZipFile(dist["wheel"]) as archive:
        entry = archive.read(next(n for n in archive.namelist() if n.endswith("entry_points.txt"))).decode()
        metadata = archive.read(next(n for n in archive.namelist() if n.endswith("METADATA"))).decode()

    assert "collegue-migrate = collegue.migrations.__main__:main" in entry
    assert "Provides-Extra: dashboard" in metadata
    assert re.search(r"Requires-Dist: streamlit>=1\.45\.0; extra == ['\"]dashboard['\"]", metadata)
    assert re.search(r"Requires-Dist: aiohttp>=3\.14\.0", metadata), "pin de sécurité aiohttp absent des métadonnées"
    assert re.search(r"Requires-Dist: fastapi(>=0\.116\.1,!=0\.136\.3|!=0\.136\.3,>=0\.116\.1)", metadata)


# --- le paquet installé fonctionne hors checkout -----------------------------------------------


def test_installed_package_is_used_not_the_checkout(dist: dict, tmp_path: Path) -> None:
    result = _json_out(
        _run(
            dist,
            "import json, collegue\n"
            "from collegue.resources.skills import get_skills_dir\n"
            "print('JSON:' + json.dumps({'pkg': collegue.__file__, 'cwd': os.getcwd(),"
            " 'path': sys.path[:3], 'skills': str(get_skills_dir())}))",
            home=tmp_path / "home",
        )
    )

    assert result["pkg"].startswith(str(dist["site"]))
    assert not result["pkg"].startswith(str(ROOT))
    assert result["skills"].startswith(str(dist["site"]))
    assert not any(str(ROOT) == entry for entry in result["path"])


def test_installed_skills_templates_and_rules_resolve_from_the_package(dist: dict, tmp_path: Path) -> None:
    (dist["workdir"] / "skills").mkdir(exist_ok=True)  # leurre dans le cwd : ne doit JAMAIS être utilisé
    (dist["workdir"] / "skills" / "decoy").mkdir(exist_ok=True)
    result = _json_out(
        _run(
            dist,
            "import json\n"
            "from collegue.resources.skills import get_skills_dir\n"
            "from collegue.prompts.templates import list_available_templates, get_template_path\n"
            "from collegue.core.shared import load_rules\n"
            "d = get_skills_dir()\n"
            "k8s = load_rules('k8s.yaml')\n"
            "print('JSON:' + json.dumps({'dir': str(d), 'skills': sorted(p.parent.name for p in d.glob('*/SKILL.md')),"
            " 'templates': list_available_templates(),"
            " 'tpl': str(get_template_path('refactoring', 'v2')),"
            " 'rules': {name: sorted(load_rules(name)) for name in ('k8s.yaml', 'terraform.yaml', 'dockerfile.yaml')}}))",
            home=tmp_path / "home",
        )
    )

    assert result["dir"].startswith(str(dist["site"]))
    assert result["skills"] == [
        "cicd-pipeline",
        "code-review",
        "collegue-toolkit",
        "refactoring-guide",
        "security-audit",
    ]
    assert "refactoring" in result["templates"] and result["templates"]["refactoring"] == [
        "default",
        "experimental",
        "v2",
    ]
    assert result["tpl"].startswith(str(dist["site"])) and result["tpl"].endswith("v2.yaml")
    assert all(result["rules"][name] for name in result["rules"])


def test_skills_directory_override_is_honoured_by_the_installed_package(dist: dict, tmp_path: Path) -> None:
    custom = tmp_path / "custom-skills"
    (custom / "mine").mkdir(parents=True)
    (custom / "mine" / "SKILL.md").write_text("---\nname: mine\ndescription: x\n---\n# x\n", encoding="utf-8")

    result = _json_out(
        _run(
            dist,
            "import json\nfrom collegue.resources.skills import get_skills_dir\n"
            "print('JSON:' + json.dumps({'dir': str(get_skills_dir())}))",
            home=tmp_path / "home",
            extra_env={"COLLEGUE_SKILLS_DIR": str(custom)},
        )
    )

    assert result["dir"] == str(custom)


def test_prompt_engine_serves_packaged_seeds_and_writes_only_under_collegue_home(dist: dict, tmp_path: Path) -> None:
    home = tmp_path / "state-home"
    before = _tree_digest(dist["site"])
    result = _json_out(
        _run(
            dist,
            "import json\n"
            "from collegue.prompts.engine.enhanced_prompt_engine import EnhancedPromptEngine\n"
            "e = EnhancedPromptEngine()\n"
            "e._save_library()\n"
            "print('JSON:' + json.dumps({'templates': len(e.library.templates), 'categories': sorted(e.library.categories),"
            " 'seed': e.tool_templates_dir, 'storage': e.storage_path}))",
            home=home,
        )
    )

    assert result["templates"] >= 10
    assert result["categories"], "catégories graines non chargées"
    assert result["seed"].startswith(str(dist["site"]))
    assert result["storage"] == str(home / "prompts")
    assert (home / "prompts" / "categories.json").is_file()
    assert (home / "prompts" / "versions" / "versions.json").is_file()
    assert _tree_digest(dist["site"]) == before, "aucune écriture n'est permise dans le paquet installé"


def test_prompt_engine_never_attempts_a_write_inside_the_installed_package(dist: dict, tmp_path: Path) -> None:
    """Garde d'audit Python : toute tentative d'écriture sous le paquet installé lève (valable aussi en root)."""

    code = (
        "import json\n"
        "site = os.environ['COLLEGUE_SITE']\n"
        "attempts = []\n"
        "def guard(event, args):\n"
        "    path = None\n"
        "    if event == 'open':\n"
        "        mode = args[1] if isinstance(args[1], str) else ''\n"
        "        flags = args[2] if len(args) > 2 and isinstance(args[2], int) else 0\n"
        "        if any(c in mode for c in 'wax+') or flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT):\n"
        "            path = args[0]\n"
        "    elif event in ('os.mkdir', 'os.rename', 'os.remove', 'os.rmdir', 'shutil.copyfile'):\n"
        "        path = args[0]\n"
        "    if path is not None and str(os.fspath(path) if not isinstance(path, int) else '').startswith(site):\n"
        "        attempts.append(f'{event}:{path}')\n"
        "        raise PermissionError(f'écriture interdite dans le paquet installé: {path}')\n"
        "sys.addaudithook(guard)\n"
        "from collegue.prompts.engine.enhanced_prompt_engine import EnhancedPromptEngine\n"
        "engine = EnhancedPromptEngine()\n"
        "engine._save_library()\n"
        "print('JSON:' + json.dumps({'n': len(engine.library.templates), 'attempts': attempts}))"
    )
    result = _json_out(_run(dist, code, home=tmp_path / "rohome"))

    assert result["n"] >= 10
    assert result["attempts"] == []


# --- migrations embarquées ---------------------------------------------------------------------


def _migrate(dist: dict, *args: str, home: Path, extra_env: dict[str, str] | None = None):
    return _run(
        dist,
        "import sys\nfrom collegue.migrations.__main__ import main\nsys.exit(main(sys.argv[1:]))",
        home=home,
        extra_env=extra_env,
        args=args,
    )


def _heads_in_source() -> str:
    numbers = sorted(
        re.match(r"(\d{4})_", p.name).group(1)
        for p in (ROOT / "collegue" / "migrations" / "versions").glob("[0-9]*.py")
    )
    return numbers[-1]


def test_migrations_run_to_head_on_an_empty_sqlite_database_from_any_directory(dist: dict, tmp_path: Path) -> None:
    db = tmp_path / "fresh" / "state.sqlite3"
    db.parent.mkdir()
    url = f"sqlite:///{db}"

    first = _migrate(dist, "upgrade", "--url", url, home=tmp_path / "h")

    assert first.returncode == 0, first.stdout + first.stderr
    import sqlite3

    connection = sqlite3.connect(db)
    try:
        tables = {row[0] for row in connection.execute("select name from sqlite_master where type='table'")}
        version = connection.execute("select version_num from alembic_version").fetchall()
    finally:
        connection.close()
    assert {"projects", "tasks", "decisions", "metrics", "checkpoints", "alembic_version"} <= tables
    assert version == [(_heads_in_source(),)]


def test_second_migration_run_is_idempotent_and_keeps_the_data(dist: dict, tmp_path: Path) -> None:
    db = tmp_path / "state.sqlite3"
    url = f"sqlite:///{db}"
    assert _migrate(dist, "upgrade", "--url", url, home=tmp_path / "h").returncode == 0
    import sqlite3

    connection = sqlite3.connect(db)
    connection.execute("insert into projects (name) values (?)", ("keep-me",))
    connection.commit()
    connection.close()

    second = _migrate(dist, "upgrade", "--url", url, home=tmp_path / "h")

    assert second.returncode == 0, second.stdout + second.stderr
    connection = sqlite3.connect(db)
    try:
        assert connection.execute("select name from projects").fetchall() == [("keep-me",)]
        assert connection.execute("select version_num from alembic_version").fetchall() == [(_heads_in_source(),)]
    finally:
        connection.close()


def test_migrations_resume_an_existing_partially_migrated_database(dist: dict, tmp_path: Path) -> None:
    db = tmp_path / "old.sqlite3"
    url = f"sqlite:///{db}"
    partial = _migrate(dist, "upgrade", "--url", url, "--revision", "0005", home=tmp_path / "h")
    assert partial.returncode == 0, partial.stdout + partial.stderr
    current = _migrate(dist, "current", "--url", url, home=tmp_path / "h")
    assert current.stdout.strip() == "0005"

    resumed = _migrate(dist, "upgrade", "--url", url, home=tmp_path / "h")

    assert resumed.returncode == 0, resumed.stdout + resumed.stderr
    final = _migrate(dist, "current", "--url", url, home=tmp_path / "h")
    assert final.stdout.strip() == _heads_in_source()


def test_migration_cli_uses_state_database_url_and_reports_errors(dist: dict, tmp_path: Path) -> None:
    db = tmp_path / "env.sqlite3"
    ok = _migrate(dist, "upgrade", home=tmp_path / "h", extra_env={"STATE_DATABASE_URL": f"sqlite:///{db}"})
    assert ok.returncode == 0, ok.stdout + ok.stderr
    assert db.is_file()

    missing_url = _migrate(dist, "upgrade", home=tmp_path / "h", extra_env={"STATE_DATABASE_URL": ""})
    assert missing_url.returncode == 2
    assert "STATE_DATABASE_URL" in missing_url.stderr

    bad_revision = _migrate(dist, "upgrade", "--url", f"sqlite:///{db}", "--revision", "9999", home=tmp_path / "h")
    assert bad_revision.returncode == 1
    assert "9999" in bad_revision.stderr

    heads = _migrate(dist, "heads", home=tmp_path / "h")
    assert heads.stdout.strip() == _heads_in_source()


def test_existing_migration_ids_and_chain_are_preserved(dist: dict, tmp_path: Path) -> None:
    result = _json_out(
        _run(
            dist,
            "import json\nfrom alembic.script import ScriptDirectory\nfrom collegue.migrations import alembic_config\n"
            "s = ScriptDirectory.from_config(alembic_config())\n"
            "revs = sorted(((r.revision, r.down_revision) for r in s.walk_revisions()), key=lambda t: t[0])\n"
            "print('JSON:' + json.dumps(revs))",
            home=tmp_path / "h",
        )
    )

    assert [tuple(item) for item in result[:10]] == BASELINE_MIGRATIONS


# --- parcours minimal du serveur, sans LLM -----------------------------------------------------


def test_installed_server_starts_and_serves_skills_without_any_llm_call(dist: dict, tmp_path: Path) -> None:
    code = (
        "import asyncio, json, socket\n"
        "def _no_network(*a, **k):\n"
        "    raise AssertionError('appel réseau interdit')\n"
        "socket.socket.connect = _no_network\n"
        "import collegue.app as m\n"
        "from fastmcp import Client\n"
        "async def run():\n"
        "    async with Client(m.app) as c:\n"
        "        tools = await c.list_tools()\n"
        "        resources = await c.list_resources()\n"
        "        return {'tools': len(tools), 'skills': sorted(str(r.uri) for r in resources if str(r.uri).startswith('skill://')),\n"
        "                'app': m.__file__}\n"
        "print('JSON:' + json.dumps(asyncio.run(run())))\n"
    )
    result = _json_out(
        _run(dist, code, home=tmp_path / "home", extra_env={"STATE_DATABASE_URL": f"sqlite:///{tmp_path}/s.db"})
    )

    assert result["app"].startswith(str(dist["site"]))
    assert result["tools"] >= 15
    assert [u for u in result["skills"] if u.endswith("/SKILL.md")] == [
        f"skill://{name}/SKILL.md"
        for name in ("cicd-pipeline", "code-review", "collegue-toolkit", "refactoring-guide", "security-audit")
    ]
