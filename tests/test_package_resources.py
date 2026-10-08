"""Loaders de ressources et migrations embarquées, exercés sur le code du dépôt (le wheel est couvert à part).

Ces tests vérifient des résultats réels : fichiers lus, écrits ou absents, schéma SQLite migré, codes de sortie.
"""

from __future__ import annotations

import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "collegue"


def _package_files() -> set[str]:
    return {
        str(p.relative_to(PACKAGE))
        for p in PACKAGE.rglob("*")
        if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"
    }


# --- pkgdata -----------------------------------------------------------------------------------------


def test_resource_dir_resolves_inside_the_package_regardless_of_cwd(tmp_path: Path, monkeypatch) -> None:
    from collegue.pkgdata import resource_dir, resource_file

    (tmp_path / "skills").mkdir()  # leurre dans le cwd
    monkeypatch.chdir(tmp_path)

    assert resource_dir("skills") == PACKAGE / "skills"
    assert resource_file("tools", "rules", "k8s.yaml") == PACKAGE / "tools" / "rules" / "k8s.yaml"
    assert resource_dir() == PACKAGE


def test_missing_resource_is_an_explicit_error_not_a_silent_fallback(tmp_path: Path, monkeypatch) -> None:
    from collegue.pkgdata import resource_dir, resource_file

    monkeypatch.chdir(tmp_path)

    with pytest.raises(FileNotFoundError, match="collegue/nope"):
        resource_dir("nope")
    with pytest.raises(FileNotFoundError, match="absent.yaml"):
        resource_file("tools", "rules", "absent.yaml")


# --- skills ------------------------------------------------------------------------------------------


def test_skills_default_to_the_packaged_directory_not_cwd_or_app(tmp_path: Path, monkeypatch) -> None:
    from collegue.resources.skills import get_skills_dir

    monkeypatch.delenv("COLLEGUE_SKILLS_DIR", raising=False)
    (tmp_path / "skills" / "decoy").mkdir(parents=True)
    (tmp_path / "skills" / "decoy" / "SKILL.md").write_text("# decoy", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    resolved = get_skills_dir()

    assert resolved == PACKAGE / "skills"
    assert sorted(p.parent.name for p in resolved.glob("*/SKILL.md")) == [
        "cicd-pipeline",
        "code-review",
        "collegue-toolkit",
        "refactoring-guide",
        "security-audit",
    ]


def test_skills_operator_override_wins_and_a_bad_override_is_not_silently_replaced(tmp_path: Path, monkeypatch) -> None:
    from collegue.resources.skills import get_skills_dir

    custom = tmp_path / "mine"
    custom.mkdir()
    monkeypatch.setenv("COLLEGUE_SKILLS_DIR", str(custom))
    assert get_skills_dir() == custom

    missing = tmp_path / "does-not-exist"
    monkeypatch.setenv("COLLEGUE_SKILLS_DIR", str(missing))
    assert get_skills_dir() == missing  # pas de repli sur les skills embarquées : l'erreur reste visible


def test_the_old_top_level_skills_directory_is_gone() -> None:
    assert not (ROOT / "skills").exists(), "les skills vivent dans collegue/skills (embarquées dans le wheel)"


# --- prompts : graines en lecture seule, état sous COLLEGUE_HOME -------------------------------------


def test_prompt_engine_default_state_lives_under_collegue_home_and_the_package_stays_untouched(
    tmp_path: Path, monkeypatch
) -> None:
    from collegue.prompts.engine.enhanced_prompt_engine import EnhancedPromptEngine

    monkeypatch.setenv("COLLEGUE_HOME", str(tmp_path / "home"))
    before = _package_files()

    engine = EnhancedPromptEngine()
    engine._save_library()

    assert engine.storage_path == str(tmp_path / "home" / "prompts")
    assert engine.tool_templates_dir == str(PACKAGE / "prompts" / "templates" / "tools")
    assert len(engine.library.templates) >= 10
    assert engine.library.categories, "les catégories graines du paquet doivent être chargées"
    assert (tmp_path / "home" / "prompts" / "categories.json").is_file()
    assert any((tmp_path / "home" / "prompts" / "templates").glob("*.json"))
    assert (tmp_path / "home" / "prompts" / "versions" / "versions.json").is_file()
    assert _package_files() == before, "aucun fichier ne doit être écrit dans le paquet"


def test_prompt_engine_explicit_storage_override_is_respected(tmp_path: Path, monkeypatch) -> None:
    from collegue.prompts.engine.enhanced_prompt_engine import EnhancedPromptEngine

    monkeypatch.setenv("COLLEGUE_HOME", str(tmp_path / "home"))
    explicit = tmp_path / "explicit-store"

    engine = EnhancedPromptEngine(storage_dir=str(explicit))
    engine._save_library()

    assert engine.storage_path == str(explicit)
    assert (explicit / "templates").is_dir()
    assert not (tmp_path / "home" / "prompts").exists(), "l'override opérateur ne doit rien écrire sous COLLEGUE_HOME"


def test_prompt_engine_missing_packaged_seeds_fail_loudly(tmp_path: Path, monkeypatch) -> None:
    import collegue.prompts.storage as storage

    monkeypatch.setenv("COLLEGUE_HOME", str(tmp_path / "home"))

    def boom():
        raise FileNotFoundError("ressource embarquée introuvable : collegue/prompts/templates/tools")

    monkeypatch.setattr(storage, "seed_templates_dir", boom)
    from collegue.prompts.engine.enhanced_prompt_engine import EnhancedPromptEngine

    with pytest.raises(FileNotFoundError, match="embarquée"):
        EnhancedPromptEngine()


def test_version_metrics_are_stored_under_collegue_home(tmp_path: Path, monkeypatch) -> None:
    from collegue.prompts.versions import get_versions_file, load_version_metrics, save_version_metrics

    monkeypatch.setenv("COLLEGUE_HOME", str(tmp_path / "home"))
    legacy = PACKAGE / "prompts" / "versions" / "versions.json"  # ancien emplacement (ignoré par git)
    legacy_before = legacy.read_bytes() if legacy.exists() else None

    assert get_versions_file() == tmp_path / "home" / "prompts" / "versions" / "versions.json"
    assert save_version_metrics("tool", "v1", {"score": 1.5}) is True
    assert load_version_metrics("tool", "v1") == {"score": 1.5}
    assert (legacy.read_bytes() if legacy.exists() else None) == legacy_before, "le paquet ne doit plus être écrit"


def test_register_prompts_creates_no_directory_inside_the_package(monkeypatch, tmp_path: Path) -> None:
    import collegue.prompts as prompts

    monkeypatch.setenv("COLLEGUE_HOME", str(tmp_path / "home"))
    before = {p for p in PACKAGE.rglob("*") if p.is_dir() and "__pycache__" not in p.parts}

    prompts.register_prompts(app=None, app_state={})

    assert {p for p in PACKAGE.rglob("*") if p.is_dir() and "__pycache__" not in p.parts} == before


# --- migrations embarquées ---------------------------------------------------------------------------


def test_migration_graph_is_linear_and_existing_ids_are_unchanged() -> None:
    from alembic.script import ScriptDirectory

    from collegue.migrations import alembic_config, head_revisions, migrations_dir

    script = ScriptDirectory.from_config(alembic_config())
    chain = {r.revision: r.down_revision for r in script.walk_revisions()}

    assert migrations_dir() == PACKAGE / "migrations"
    assert len(head_revisions()) == 1
    for number in range(1, 11):
        revision = f"{number:04d}"
        assert chain[revision] == (None if number == 1 else f"{number - 1:04d}"), revision
    versions = sorted(
        re.match(r"(\d{4})_", p.name).group(1) for p in (PACKAGE / "migrations" / "versions").glob("[0-9]*.py")
    )
    assert head_revisions() == [versions[-1]]


def test_upgrade_creates_the_schema_and_is_idempotent(tmp_path: Path) -> None:
    from collegue import migrations

    url = f"sqlite:///{tmp_path / 'state.sqlite3'}"

    assert migrations.current_revision(url) == []
    migrations.upgrade(url)
    first = migrations.current_revision(url)
    migrations.upgrade(url)

    assert first == migrations.head_revisions() == migrations.current_revision(url)
    connection = sqlite3.connect(tmp_path / "state.sqlite3")
    try:
        tables = {r[0] for r in connection.execute("select name from sqlite_master where type='table'")}
    finally:
        connection.close()
    assert {"projects", "tasks", "decisions", "metrics", "checkpoints", "alembic_version"} <= tables


def test_upgrade_resumes_an_existing_database_without_losing_data(tmp_path: Path) -> None:
    from collegue import migrations

    db = tmp_path / "old.sqlite3"
    url = f"sqlite:///{db}"
    migrations.upgrade(url, "0004")
    connection = sqlite3.connect(db)
    connection.execute("insert into projects (name) values ('legacy-project')")
    connection.commit()
    connection.close()

    migrations.upgrade(url)

    connection = sqlite3.connect(db)
    try:
        assert connection.execute("select name from projects").fetchall() == [("legacy-project",)]
    finally:
        connection.close()
    assert migrations.current_revision(url) == migrations.head_revisions()


def test_explicit_url_wins_over_the_environment(tmp_path: Path, monkeypatch) -> None:
    from collegue import migrations

    env_db, explicit_db = tmp_path / "env.sqlite3", tmp_path / "explicit.sqlite3"
    monkeypatch.setenv("STATE_DATABASE_URL", f"sqlite:///{env_db}")

    migrations.upgrade(f"sqlite:///{explicit_db}")

    assert explicit_db.is_file() and not env_db.exists()


def test_url_containing_a_percent_sign_is_not_mangled(tmp_path: Path) -> None:
    from collegue import migrations

    folder = tmp_path / "100%done"
    folder.mkdir()
    url = f"sqlite:///{folder / 'state.sqlite3'}"

    migrations.upgrade(url)

    assert (folder / "state.sqlite3").is_file()


def test_alembic_ini_still_drives_the_same_embedded_migrations_from_a_checkout(tmp_path: Path) -> None:
    """`alembic upgrade head` depuis la racine du dépôt reste valable : même dossier de migrations."""

    db = tmp_path / "cli.sqlite3"
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env["STATE_DATABASE_URL"] = f"sqlite:///{db}"

    done = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert done.returncode == 0, done.stdout + done.stderr
    connection = sqlite3.connect(db)
    try:
        from collegue import migrations

        assert connection.execute("select version_num from alembic_version").fetchall() == [
            (migrations.head_revisions()[0],)
        ]
    finally:
        connection.close()


def test_migrations_cli_runs_from_any_directory_and_reports_exit_codes(tmp_path: Path) -> None:
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env["PYTHONPATH"] = str(ROOT)  # le paquet du dépôt ; cwd volontairement ailleurs
    env["COLLEGUE_HOME"] = str(tmp_path / "home")
    db = tmp_path / "cli.sqlite3"

    def cli(*args: str, **extra_env: str):
        return subprocess.run(
            [sys.executable, "-m", "collegue.migrations", *args],
            cwd=tmp_path,
            env={**env, **extra_env},
            capture_output=True,
            text=True,
            timeout=120,
        )

    assert cli("upgrade", "--url", f"sqlite:///{db}").returncode == 0
    assert cli("upgrade", "--url", f"sqlite:///{db}").returncode == 0
    assert cli("current", "--url", f"sqlite:///{db}").stdout.strip() == cli("heads").stdout.strip()
    no_url = cli("upgrade", STATE_DATABASE_URL="")
    assert no_url.returncode == 2 and "STATE_DATABASE_URL" in no_url.stderr
    bad = cli("upgrade", "--url", f"sqlite:///{db}", "--revision", "nope")
    assert bad.returncode == 1 and "nope" in bad.stderr
