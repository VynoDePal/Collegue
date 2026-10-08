"""Mise à jour en place : l'état de prompts de l'ancienne installation survit au déplacement vers COLLEGUE_HOME.

Défaut d'origine (W2, relevé par le contre-test du manager) : avant la mise à jour, les templates JSON, les catégories
et l'historique de versions vivaient DANS le paquet (``collegue/prompts/templates/{categories.json,templates/}``,
``collegue/prompts/versions/versions.json``). Le moteur lisait ensuite ``$COLLEGUE_HOME/prompts`` et les personnalisations
disparaissaient (template=None, versions=[]).

Ces tests exécutent le vrai code dans une copie jetable du paquet (sous-processus, ``PYTHONPATH`` = la copie, cwd ailleurs).
L'ancien état est fabriqué avec les API publiques et des chemins explicites (même disposition de fichiers que l'ancien
défaut). Aucun état utilisateur réel n'est touché.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CUSTOM = "CONSERVE MES DONNEES"
HISTORY = "MON HISTORIQUE"


def _tracked_package_files() -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard", "collegue"],
        cwd=ROOT,
        capture_output=True,
        check=True,
    ).stdout
    files = [Path(item.decode()) for item in out.split(b"\0") if item]
    return [p for p in dict.fromkeys(files) if (ROOT / p).is_file()]


@pytest.fixture(scope="module")
def package_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    base = tmp_path_factory.mktemp("pkg-template")
    for relative in _tracked_package_files():
        target = base / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, target)
    return base


@pytest.fixture
def world(tmp_path: Path, package_template: Path) -> dict:
    """Une « installation » jetable : paquet copié, COLLEGUE_HOME vide, cwd hors paquet."""
    pkg = tmp_path / "site"
    shutil.copytree(package_template, pkg)
    home = tmp_path / "home"
    cwd = tmp_path / "elsewhere"
    cwd.mkdir()
    return {"pkg": pkg, "home": home, "cwd": cwd, "prompts": pkg / "collegue" / "prompts", "tmp": tmp_path}


def _env(world: dict, **extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in {"PYTHONPATH", "COLLEGUE_HOME", "STATE_DATABASE_URL"}}
    env.update(PYTHONPATH=str(world["pkg"]), COLLEGUE_HOME=str(world["home"]), PYTHONDONTWRITEBYTECODE="1")
    env.update(extra)
    return env


def _run(world: dict, code: str, *args: str, **extra_env: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", code, *args],
        cwd=world["cwd"],
        env=_env(world, **extra_env),
        capture_output=True,
        text=True,
        timeout=300,
    )


def _json(completed: subprocess.CompletedProcess) -> dict:
    assert completed.returncode == 0, completed.stdout[-2000:] + completed.stderr[-3000:]
    return json.loads([ln for ln in completed.stdout.splitlines() if ln.startswith("{")][-1])


PLANT = r"""
import json, sys
from pathlib import Path
from collegue.prompts.engine.prompt_engine import PromptEngine
from collegue.prompts.engine.versioning import PromptVersionManager
prompts = Path(sys.argv[1])
spec = json.loads(sys.argv[2])
engine = PromptEngine(storage_path=str(prompts / "templates"))     # chemins EXPLICITES = disposition de l'ancien défaut
for tpl in spec.get("templates", []):
    engine.create_template(tpl)
for cat in spec.get("categories", []):
    engine.create_category(cat)
versions = PromptVersionManager(storage_path=str(prompts / "versions"))
for tid, content, version in spec.get("versions", []):
    versions.create_version(tid, content, [], version=version)
print(json.dumps({"ok": True}))
"""

READ = r"""
import json
from collegue.prompts.engine.prompt_engine import PromptEngine
from collegue.prompts.engine.versioning import PromptVersionManager
engine = PromptEngine()
versions = PromptVersionManager()
def tpl(i):
    t = engine.get_template(i)
    return None if t is None else t.template
out = {
    "templates": {t.id: t.template for t in engine.get_all_templates()},
    "categories": sorted(c.id for c in engine.get_all_categories()),
    "versions": {tid: [v.content for v in vs] for tid, vs in versions.versions_cache.items()},
    "storage": str(engine.storage_path),
}
print(json.dumps(out))
"""


def plant(world: dict, *, templates=(), categories=(), versions=()) -> None:
    spec = {"templates": list(templates), "categories": list(categories), "versions": list(versions)}
    done = _run(world, PLANT, str(world["prompts"]), json.dumps(spec))
    assert done.returncode == 0, done.stdout + done.stderr


def custom_template(template_id="fixture-custom", text=CUSTOM, name="Custom audit"):
    return {"id": template_id, "name": name, "description": "Operator fixture", "template": text, "category": "custom"}


def legacy_digest(world: dict) -> dict[str, str]:
    """Empreinte de tous les fichiers d'état de l'ancien emplacement (pour prouver qu'ils ne sont jamais modifiés)."""
    files = [world["prompts"] / "templates" / "categories.json", world["prompts"] / "versions" / "versions.json"]
    files += sorted((world["prompts"] / "templates" / "templates").glob("*.json"))
    return {str(f.relative_to(world["pkg"])): hashlib.sha256(f.read_bytes()).hexdigest() for f in files if f.exists()}


def marker(world: dict) -> dict:
    return json.loads((world["home"] / "prompts" / ".legacy-import.json").read_text(encoding="utf-8"))


# --- le défaut du manager -----------------------------------------------------------------------


def test_in_place_upgrade_keeps_the_custom_template_its_category_and_its_history(world: dict) -> None:
    plant(
        world,
        templates=[custom_template()],
        categories=[{"id": "ops-custom", "name": "Ops", "description": "catégorie opérateur"}],
        versions=[("fixture-custom", HISTORY, "7.0.0"), ("fixture-custom", "SECONDE VERSION", "7.1.0")],
    )
    before = legacy_digest(world)
    assert before, "fixture : l'ancien état doit exister"

    after = _json(_run(world, READ))

    assert after["templates"]["fixture-custom"] == CUSTOM
    assert "ops-custom" in after["categories"]
    assert after["versions"]["fixture-custom"] == [HISTORY, "SECONDE VERSION"]
    assert after["storage"] == str(world["home"] / "prompts")
    assert legacy_digest(world) == before, "l'ancien état (dans le paquet) ne doit être ni modifié ni supprimé"
    done = marker(world)["sources"][str(world["prompts"])]
    assert done["status"] == "complete" and done["errors"] == []
    assert done["imported"]["templates"] == 1 and done["imported"]["version_keys"] == 1


def test_enhanced_engine_startup_keeps_the_custom_data_and_seeds_the_rest(world: dict) -> None:
    plant(world, templates=[custom_template()], versions=[("fixture-custom", HISTORY, "7.0.0")])
    code = (
        "import json\n"
        "from collegue.prompts.engine.enhanced_prompt_engine import EnhancedPromptEngine\n"
        "e = EnhancedPromptEngine()\n"
        "print(json.dumps({'custom': e.get_template('fixture-custom').template,"
        " 'versions': [v.content for v in e.version_manager.get_all_versions('fixture-custom')],"
        " 'n': len(e.library.templates)}))"
    )

    out = _json(_run(world, code))

    assert out["custom"] == CUSTOM
    assert out["versions"] == [HISTORY]
    assert out["n"] >= 11, "les graines livrées doivent être chargées en plus de la personnalisation"


def test_legacy_package_can_be_read_only_and_is_never_written(world: dict) -> None:
    plant(world, templates=[custom_template()], versions=[("fixture-custom", HISTORY, "7.0.0")])
    code = (
        "import json, os, sys\n"
        "site = os.environ['PKG']\n"
        "attempts = []\n"
        "def guard(event, args):\n"
        "    path = None\n"
        "    if event == 'open':\n"
        "        mode = args[1] if isinstance(args[1], str) else ''\n"
        "        flags = args[2] if len(args) > 2 and isinstance(args[2], int) else 0\n"
        "        if any(c in mode for c in 'wax+') or flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT):\n"
        "            path = args[0]\n"
        "    elif event in ('os.mkdir', 'os.rename', 'os.remove', 'os.rmdir', 'os.link', 'shutil.copyfile'):\n"
        "        path = args[0]\n"
        "    if path is not None and not isinstance(path, int) and str(os.fspath(path)).startswith(site):\n"
        "        attempts.append(f'{event}:{path}')\n"
        "        raise PermissionError(f'écriture interdite dans le paquet: {path}')\n"
        "sys.addaudithook(guard)\n"
        + READ.replace("print(json.dumps(out))", "out['attempts'] = attempts\nprint(json.dumps(out))")
    )

    out = _json(_run(world, code, PKG=str(world["pkg"])))

    assert out["attempts"] == []
    assert out["templates"]["fixture-custom"] == CUSTOM
    assert out["versions"]["fixture-custom"] == [HISTORY]


def test_second_start_is_idempotent_and_does_not_resurrect_deleted_data(world: dict) -> None:
    plant(
        world, templates=[custom_template(), custom_template("other", "AUTRE", "Other")], versions=[("other", "V", "1")]
    )
    first = _json(_run(world, READ))
    assert set(first["templates"]) == {"fixture-custom", "other"} or {"fixture-custom", "other"} <= set(
        first["templates"]
    )
    first_marker = marker(world)

    # L'opérateur supprime ensuite un template dans le NOUVEL état : un redémarrage ne doit pas le ressusciter.
    (world["home"] / "prompts" / "templates" / "other.json").unlink()
    second = _json(_run(world, READ))
    third = _json(_run(world, READ))

    assert "other" not in second["templates"] and "other" not in third["templates"]
    assert second["templates"]["fixture-custom"] == CUSTOM
    assert marker(world)["sources"] == first_marker["sources"], "le marqueur d'import ne change plus"
    assert len(list((world["home"] / "prompts" / "templates").glob("fixture-custom*.json"))) == 1


def test_fresh_install_is_seeded_from_packaged_seeds_without_marker(world: dict) -> None:
    code = (
        "import json\n"
        "from collegue.prompts.engine.enhanced_prompt_engine import EnhancedPromptEngine\n"
        "e = EnhancedPromptEngine()\n"
        "print(json.dumps({'n': len(e.library.templates), 'cats': sorted(e.library.categories)}))"
    )

    out = _json(_run(world, code))

    assert out["n"] >= 10 and out["cats"]
    assert not (world["home"] / "prompts" / ".legacy-import.json").exists()


# --- conflits, états invalides, interruption ------------------------------------------------------


def test_populated_destination_always_wins_and_conflicts_are_reported(world: dict) -> None:
    plant(
        world,
        templates=[
            custom_template("shared", "ANCIEN CONTENU", "Shared"),
            custom_template("legacy-only", "ANCIEN SEUL", "Legacy only"),
        ],
        versions=[("shared", "ANCIEN HISTORIQUE", "1"), ("legacy-only", "H2", "1")],
    )
    # Nouvel état déjà peuplé AVANT la reprise (même id, autre contenu ; versions pour le même id).
    seed = _run(
        world,
        "import json\nfrom pathlib import Path\nfrom collegue.prompts.engine.prompt_engine import PromptEngine\n"
        "from collegue.prompts.engine.versioning import PromptVersionManager\n"
        "import os\nhome = Path(os.environ['COLLEGUE_HOME']) / 'prompts'\n"
        "e = PromptEngine(storage_path=str(home))\n"
        "e.create_template({'id': 'shared', 'name': 'Shared', 'description': 'd', 'template': 'NOUVEAU CONTENU', 'category': 'custom'})\n"
        "v = PromptVersionManager(storage_path=str(home / 'versions'))\n"
        "v.create_version('shared', 'NOUVEL HISTORIQUE', [], version='9')\n"
        "print(json.dumps({'ok': 1}))",
    )
    assert seed.returncode == 0, seed.stderr

    out = _json(_run(world, READ))

    assert out["templates"]["shared"] == "NOUVEAU CONTENU", "le nouvel état n'est jamais écrasé"
    assert out["templates"]["legacy-only"] == "ANCIEN SEUL"
    assert out["versions"]["shared"] == ["NOUVEL HISTORIQUE"]
    assert out["versions"]["legacy-only"] == ["H2"]
    conflicts = marker(world)["sources"][str(world["prompts"])]["conflicts"]
    assert any("shared" in c for c in conflicts), conflicts
    assert marker(world)["sources"][str(world["prompts"])]["status"] == "complete"


def test_same_name_with_another_id_in_the_new_state_is_a_reported_conflict(world: dict) -> None:
    plant(world, templates=[custom_template("old-id", "ANCIEN", "Same name")])
    seed = _run(
        world,
        "import json, os\nfrom pathlib import Path\nfrom collegue.prompts.engine.prompt_engine import PromptEngine\n"
        "e = PromptEngine(storage_path=str(Path(os.environ['COLLEGUE_HOME']) / 'prompts'))\n"
        "e.create_template({'id': 'new-id', 'name': 'Same name', 'description': 'd', 'template': 'NOUVEAU', 'category': 'custom'})\n"
        "print(json.dumps({'ok': 1}))",
    )
    assert seed.returncode == 0, seed.stderr

    out = _json(_run(world, READ))

    assert out["templates"]["new-id"] == "NOUVEAU" and "old-id" not in out["templates"]
    assert any("Same name" in c for c in marker(world)["sources"][str(world["prompts"])]["conflicts"])


def test_every_write_to_the_new_state_is_atomic_temp_then_rename(world: dict) -> None:
    plant(world, templates=[custom_template()], versions=[("fixture-custom", HISTORY, "7.0.0")])
    code = (
        "import json, os, sys\n"
        "home = os.environ['COLLEGUE_HOME']\n"
        "direct = []\n"
        "def guard(event, args):\n"
        "    if event == 'open' and args[0] is not None and not isinstance(args[0], int):\n"
        "        mode = args[1] if isinstance(args[1], str) else ''\n"
        "        flags = args[2] if len(args) > 2 and isinstance(args[2], int) else 0\n"
        "        path = str(os.fspath(args[0]))\n"
        "        writes = any(c in mode for c in 'wax+') or flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT)\n"
        "        if writes and path.startswith(home) and not path.endswith('.tmp') and not path.endswith('.legacy-import.lock'):\n"
        "            direct.append(path)\n"
        "sys.addaudithook(guard)\n"
        "from pathlib import Path\nfrom collegue.prompts import legacy\n"
        "report = legacy.import_legacy_state(Path(sys.argv[1]))\n"
        "print(json.dumps({'direct': direct, 'templates': report.templates}))"
    )

    out = _json(_run(world, code, str(world["prompts"])))

    assert out["templates"] == 1
    assert out["direct"] == [], f"écriture directe non atomique : {out['direct']}"


def test_invalid_legacy_files_are_reported_not_silently_dropped_and_retried_after_repair(world: dict) -> None:
    plant(
        world,
        templates=[custom_template("good", "BON", "Good"), custom_template("bad", "MAUVAIS", "Bad")],
        versions=[("good", "H", "1")],
    )
    templates_dir = world["prompts"] / "templates" / "templates"
    good_bad = (templates_dir / "bad.json").read_text(encoding="utf-8")
    (templates_dir / "bad.json").write_text("{ pas du json", encoding="utf-8")
    (templates_dir / "incomplete.json").write_text(
        json.dumps({"id": "incomplete"}), encoding="utf-8"
    )  # schéma invalide
    versions_file = world["prompts"] / "versions" / "versions.json"
    good_versions = versions_file.read_text(encoding="utf-8")
    versions_file.write_text("[1, 2", encoding="utf-8")
    before = legacy_digest(world)

    first = _json(_run(world, READ))

    assert first["templates"]["good"] == "BON", "les fichiers valides sont repris malgré les invalides"
    assert "bad" not in first["templates"] and "incomplete" not in first["templates"]
    done = marker(world)["sources"][str(world["prompts"])]
    assert done["status"] == "incomplete"
    reported = " ".join(done["errors"])
    assert "bad.json" in reported and "incomplete.json" in reported and "versions.json" in reported
    assert legacy_digest(world) == before, "les fichiers invalides restent intacts pour réparation"

    # Réparation par l'opérateur : le redémarrage suivant termine la reprise (sans doublon).
    (templates_dir / "bad.json").write_text(good_bad, encoding="utf-8")
    (templates_dir / "incomplete.json").unlink()
    versions_file.write_text(good_versions, encoding="utf-8")
    second = _json(_run(world, READ))

    assert second["templates"]["bad"] == "MAUVAIS" and second["templates"]["good"] == "BON"
    assert second["versions"]["good"] == ["H"]
    done = marker(world)["sources"][str(world["prompts"])]
    assert done["status"] == "complete" and done["errors"] == []


def test_interrupted_import_loses_nothing_and_completes_on_the_next_start(world: dict) -> None:
    plant(
        world,
        templates=[custom_template(f"t{i}", f"CONTENU {i}", f"Name {i}") for i in range(4)],
        versions=[("t0", "H0", "1")],
    )
    before = legacy_digest(world)
    crash = (
        "import sys\nfrom pathlib import Path\nfrom collegue.prompts import legacy\n"
        "calls = {'n': 0}\n"
        "real = legacy._publish\n"
        "def flaky(tmp, dest):\n"
        "    calls['n'] += 1\n"
        "    if calls['n'] == 3:\n"
        "        raise SystemExit(42)   # interruption brutale au milieu de la reprise\n"
        "    return real(tmp, dest)\n"
        "legacy._publish = flaky\n"
        "legacy.import_legacy_state(Path(sys.argv[1]))\n"
    )
    interrupted = _run(world, crash, str(world["prompts"]))
    assert interrupted.returncode == 42, interrupted.stdout + interrupted.stderr

    copied = sorted((world["home"] / "prompts" / "templates").glob("t*.json"))
    assert 0 < len(copied) < 4, "reprise partielle attendue"
    for path in copied:
        json.loads(path.read_text(encoding="utf-8"))  # jamais de fichier tronqué : publication atomique
    assert (
        not (world["home"] / "prompts" / ".legacy-import.json").exists()
        or marker(world)["sources"].get(str(world["prompts"]), {}).get("status") != "complete"
    )
    assert not list((world["home"] / "prompts").rglob("*.tmp")), "aucun fichier temporaire résiduel"
    assert legacy_digest(world) == before

    resumed = _json(_run(world, READ))

    assert {f"t{i}" for i in range(4)} <= set(resumed["templates"])
    assert resumed["versions"]["t0"] == ["H0"]
    assert marker(world)["sources"][str(world["prompts"])]["status"] == "complete"
    assert len(list((world["home"] / "prompts" / "templates").glob("t*.json"))) == 4


def test_explicit_storage_overrides_are_unchanged_and_never_trigger_the_import(world: dict, tmp_path: Path) -> None:
    plant(world, templates=[custom_template()], versions=[("fixture-custom", HISTORY, "7.0.0")])
    explicit = tmp_path / "operator-store"
    code = (
        "import json, sys\n"
        "from collegue.prompts.engine.prompt_engine import PromptEngine\n"
        "from collegue.prompts.engine.versioning import PromptVersionManager\n"
        "e = PromptEngine(storage_path=sys.argv[1])\n"
        "v = PromptVersionManager(storage_path=sys.argv[1] + '/versions')\n"
        "print(json.dumps({'custom': e.get_template('fixture-custom') and 'present', 'versions': len(v.versions_cache)}))"
    )

    out = _json(_run(world, code, str(explicit)))

    assert out == {"custom": None, "versions": 0}
    assert not (world["home"] / "prompts").exists(), "un override explicite ne doit rien écrire sous COLLEGUE_HOME"


# --- import explicite depuis un autre chemin -------------------------------------------------------


def _cli(world: dict, *args: str, **extra_env: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "collegue.prompts.legacy", *args],
        cwd=world["cwd"],
        env=_env(world, **extra_env),
        capture_output=True,
        text=True,
        timeout=300,
    )


def test_explicit_cli_imports_an_old_installation_located_elsewhere(world: dict, tmp_path: Path) -> None:
    plant(world, templates=[custom_template()], versions=[("fixture-custom", HISTORY, "7.0.0")])
    old_root = tmp_path / "old-install"
    shutil.copytree(world["prompts"], old_root / "collegue" / "prompts")
    # La nouvelle installation vit ailleurs : son propre paquet n'a aucun ancien état.
    shutil.rmtree(world["prompts"] / "templates" / "templates")
    (world["prompts"] / "versions" / "versions.json").unlink()

    dry = _cli(world, "import", "--from", str(old_root), "--dry-run")
    assert dry.returncode == 0, dry.stdout + dry.stderr
    assert "1 template" in dry.stdout and not (world["home"] / "prompts").exists(), "dry-run : aucune écriture"

    real = _cli(world, "import", "--from", str(old_root / "collegue" / "prompts"))
    assert real.returncode == 0, real.stdout + real.stderr
    again = _cli(world, "import", "--from", str(old_root))
    assert again.returncode == 0 and "déjà" in again.stdout

    out = _json(_run(world, READ))
    assert out["templates"]["fixture-custom"] == CUSTOM
    assert out["versions"]["fixture-custom"] == [HISTORY]


def test_explicit_cli_rejects_a_missing_source_and_reports_errors_with_a_nonzero_code(
    world: dict, tmp_path: Path
) -> None:
    missing = _cli(world, "import", "--from", str(tmp_path / "nope"))
    assert missing.returncode == 2 and "introuvable" in missing.stderr

    plant(world, templates=[custom_template()])
    (world["prompts"] / "templates" / "templates" / "broken.json").write_text("{", encoding="utf-8")
    broken = _cli(world, "import", "--from", str(world["prompts"]))
    assert broken.returncode == 1, "une reprise incomplète ne doit pas sortir en succès"
    assert "broken.json" in broken.stderr
