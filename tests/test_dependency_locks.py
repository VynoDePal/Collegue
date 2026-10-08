"""Verrous de dépendances : pyproject.toml est l'unique source, les verrous en dérivent et toute dérive est détectée.

Défaut d'origine (W2) : ``pyproject.toml`` et ``requirements*.txt`` divergeaient (dashboard, aiohttp, pytest) et la
CI/Docker installaient des plages non verrouillées (le résultat changeait avec la date). Ces tests exécutent le
VRAI ``scripts/locks.py`` — sur les verrous commités puis sur des copies volontairement dérivées — et vérifient le
contenu réel des verrous, pas la simple présence de chaînes dans pyproject.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
import yaml
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

ROOT = Path(__file__).resolve().parents[1]
LOCKS_SCRIPT = ROOT / "scripts" / "locks.py"
PYPROJECT = ROOT / "pyproject.toml"
TARGETS = ("lint", "runtime", "dev", "audit", "sandbox", "sandbox-openhands")


def _load_locks_module():
    import importlib.util

    spec = importlib.util.spec_from_file_location("locks_tool", LOCKS_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["locks_tool"] = module  # dataclasses résout les annotations via sys.modules
    spec.loader.exec_module(module)
    return module


locks = _load_locks_module()


@pytest.fixture
def sandbox_repo(tmp_path: Path) -> Path:
    """Copie isolée du dépôt (pyproject, locks, requirements, script) qu'on peut dériver sans risque."""
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    shutil.copy2(LOCKS_SCRIPT, repo / "scripts" / "locks.py")
    shutil.copy2(PYPROJECT, repo / "pyproject.toml")
    shutil.copytree(ROOT / "locks", repo / "locks")
    for name in ("requirements.txt", "requirements-dev.txt"):
        shutil.copy2(ROOT / name, repo / name)
    return repo


def _run_check(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(repo / "scripts" / "locks.py"), "check", *args],
        capture_output=True,
        text=True,
        cwd=repo,
        timeout=120,
    )


def _pyproject() -> dict:
    return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))


# --- les verrous commités sont cohérents avec la source ---------------------------------------------


def test_committed_locks_match_pyproject_offline() -> None:
    completed = subprocess.run(
        [sys.executable, str(LOCKS_SCRIPT), "check"], capture_output=True, text=True, cwd=ROOT, timeout=120
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "6 verrou(s) cohérent(s)" in completed.stdout


@pytest.mark.parametrize("target", TARGETS)
def test_every_lock_is_fully_pinned_and_hashed(target: str) -> None:
    lock = locks.parse_lock((ROOT / "locks" / f"{target}.txt").read_text(encoding="utf-8"))

    assert lock.entries
    assert lock.headers["target"] == target
    assert re.fullmatch(r"[0-9a-f]{64}", lock.headers["source-sha256"])
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", lock.headers["exclude-newer"])
    for entry in lock.entries:
        assert entry.hashes, f"{target}: {entry.name}=={entry.version} sans empreinte"
        assert all(re.fullmatch(r"sha256:[0-9a-f]{64}", h) for h in entry.hashes)


def test_every_runtime_dependency_of_pyproject_is_locked_within_its_range() -> None:
    project = _pyproject()["project"]
    lock = locks.parse_lock((ROOT / "locks" / "runtime.txt").read_text(encoding="utf-8"))
    locked = {}
    for entry in lock.entries:
        locked.setdefault(entry.name, []).append(entry.version)

    declared = project["dependencies"] + project["optional-dependencies"]["dashboard"]
    for raw in declared:
        requirement = Requirement(raw)
        versions = locked.get(canonicalize_name(requirement.name))
        assert versions, f"{requirement.name} absent du verrou runtime"
        assert any(requirement.specifier.contains(v, prereleases=True) for v in versions), (raw, versions)


def test_dashboard_dependencies_live_in_pyproject_and_in_the_runtime_lock() -> None:
    """Défaut d'origine : streamlit/pandas n'existaient que dans requirements.txt, pas dans pyproject."""

    optional = _pyproject()["project"]["optional-dependencies"]
    names = {canonicalize_name(Requirement(r).name) for r in optional["dashboard"]}
    lock = locks.parse_lock((ROOT / "locks" / "runtime.txt").read_text(encoding="utf-8"))

    assert {"streamlit", "pandas"} <= names
    assert names <= {e.name for e in lock.entries}


def test_security_constraints_are_preserved_in_source_and_lock() -> None:
    project = _pyproject()["project"]
    declared = {canonicalize_name(Requirement(r).name): Requirement(r) for r in project["dependencies"]}
    assert str(declared["aiohttp"].specifier) == ">=3.14.0", "pin de sécurité aiohttp (CVE-2026-34993/47265)"
    assert "!=0.136.3" in str(declared["fastapi"].specifier)

    for target in ("runtime", "dev", "audit"):
        lock = locks.parse_lock((ROOT / "locks" / f"{target}.txt").read_text(encoding="utf-8"))
        versions = {e.name: e.version for e in lock.entries}
        assert tuple(int(p) for p in versions["aiohttp"].split(".")[:2]) >= (3, 14), target
        assert versions["fastapi"] != "0.136.3"


def test_requirements_files_only_include_the_hashed_locks() -> None:
    assert locks.check_requirements_files() == []
    runtime = [ln for ln in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines() if ln and ln[0] != "#"]
    dev = [ln for ln in (ROOT / "requirements-dev.txt").read_text(encoding="utf-8").splitlines() if ln and ln[0] != "#"]
    assert runtime == ["-r locks/runtime.txt"]
    assert dev == ["-r locks/dev.txt"]


def test_openhands_lock_keeps_the_versions_required_by_the_gemma4_patch() -> None:
    """scripts/patch_openhands_gemma4_terminal.py n'accepte que cette préimage : toute dérive casserait le build."""

    patch = (ROOT / "scripts" / "patch_openhands_gemma4_terminal.py").read_text(encoding="utf-8")
    supported = dict(re.findall(r'"(openhands-[a-z]+)": "([0-9.]+)"', patch))
    lock = locks.parse_lock((ROOT / "locks" / "sandbox-openhands.txt").read_text(encoding="utf-8"))
    versions = {e.name: e.version for e in lock.entries}

    assert supported == {"openhands-ai": "1.7.0", "openhands-sdk": "1.19.1", "openhands-tools": "1.19.1"}
    for name, version in supported.items():
        assert versions[name] == version, name
    assert versions["lmnr"] == "0.7.52"
    assert versions["opentelemetry-semantic-conventions"] == "0.60b1"
    assert "pytest-asyncio" in versions and versions["pytest-asyncio"] == "0.23.6"
    assert tuple(int(p) for p in versions["pytest"].split(".")[:1]) < (9,), "pytest>=9 casse pytest-asyncio<0.24"


def test_sandbox_images_embed_pip_audit_from_their_locks() -> None:
    for target in ("sandbox", "sandbox-openhands"):
        lock = locks.parse_lock((ROOT / "locks" / f"{target}.txt").read_text(encoding="utf-8"))
        assert "pip-audit" in {e.name for e in lock.entries}, target


# --- toute dérive est détectée -----------------------------------------------------------------------


def test_adding_a_dependency_to_pyproject_without_relocking_is_detected(sandbox_repo: Path) -> None:
    text = (sandbox_repo / "pyproject.toml").read_text(encoding="utf-8")
    (sandbox_repo / "pyproject.toml").write_text(
        text.replace('"httpx>=0.28.1",', '"httpx>=0.28.1",\n    "rich>=13",', 1), encoding="utf-8"
    )

    completed = _run_check(sandbox_repo)

    assert completed.returncode == 1, completed.stdout
    assert "DÉRIVE" in completed.stdout
    assert "runtime.txt" in completed.stdout and "dev.txt" in completed.stdout


def test_tightening_a_specifier_is_detected(sandbox_repo: Path) -> None:
    text = (sandbox_repo / "pyproject.toml").read_text(encoding="utf-8")
    (sandbox_repo / "pyproject.toml").write_text(
        text.replace('"aiohttp>=3.14.0"', '"aiohttp>=99.0.0"'), encoding="utf-8"
    )

    completed = _run_check(sandbox_repo, "runtime")

    assert completed.returncode == 1
    assert "DÉRIVE" in completed.stdout


def test_changing_a_group_only_flags_the_locks_that_use_it(sandbox_repo: Path) -> None:
    text = (sandbox_repo / "pyproject.toml").read_text(encoding="utf-8")
    (sandbox_repo / "pyproject.toml").write_text(
        text.replace('"aiosqlite",', '"aiosqlite",\n    "orjson",'), encoding="utf-8"
    )

    completed = _run_check(sandbox_repo)

    assert completed.returncode == 1
    flagged = {m for m in re.findall(r"([\w-]+)\.txt: DÉRIVE", completed.stdout)}
    assert flagged == {"sandbox-openhands"}


def test_lock_edited_by_hand_to_violate_the_source_is_detected(sandbox_repo: Path) -> None:
    path = sandbox_repo / "locks" / "runtime.txt"
    text = path.read_text(encoding="utf-8")
    path.write_text(re.sub(r"^aiohttp==\S+", "aiohttp==3.13.0", text, count=1, flags=re.M), encoding="utf-8")

    completed = _run_check(sandbox_repo, "runtime")

    assert completed.returncode == 1
    assert "aiohttp" in completed.stdout and "hors de" in completed.stdout


def test_dependency_missing_from_the_lock_is_detected(sandbox_repo: Path) -> None:
    path = sandbox_repo / "locks" / "runtime.txt"
    text = path.read_text(encoding="utf-8")
    stripped = re.sub(r"^pyyaml==.*?(?=^\S)", "", text, count=1, flags=re.M | re.S)
    assert stripped != text
    path.write_text(stripped, encoding="utf-8")

    completed = _run_check(sandbox_repo, "runtime")

    assert completed.returncode == 1
    assert "pyyaml" in completed.stdout.lower()


def test_entry_without_hash_is_detected(sandbox_repo: Path) -> None:
    path = sandbox_repo / "locks" / "lint.txt"
    path.write_text(
        re.sub(r"\s*\\\n\s*--hash=sha256:[0-9a-f]+", "", path.read_text(encoding="utf-8")), encoding="utf-8"
    )

    completed = _run_check(sandbox_repo, "lint")

    assert completed.returncode == 1
    assert "sans empreinte" in completed.stdout


def test_unpinned_or_handwritten_lock_is_rejected(sandbox_repo: Path) -> None:
    (sandbox_repo / "locks" / "lint.txt").write_text("ruff>=0.15\n", encoding="utf-8")

    completed = _run_check(sandbox_repo, "lint")

    assert completed.returncode == 1
    assert "GENERATED" in completed.stdout


def test_missing_lock_is_detected(sandbox_repo: Path) -> None:
    (sandbox_repo / "locks" / "audit.txt").unlink()

    completed = _run_check(sandbox_repo, "audit")

    assert completed.returncode == 1
    assert "absent" in completed.stdout


def test_requirements_file_that_diverges_from_the_lock_is_detected(sandbox_repo: Path) -> None:
    (sandbox_repo / "requirements.txt").write_text("fastmcp>=3\npytest>=8\n", encoding="utf-8")

    completed = _run_check(sandbox_repo, "lint")

    assert completed.returncode == 1
    assert "requirements.txt" in completed.stdout


# --- CI et Docker installent depuis les verrous ------------------------------------------------------

HASHED = "--require-hashes"


def _workflow(name: str) -> dict:
    return yaml.safe_load((ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8"))


def _run_steps(job: dict) -> list[str]:
    return [str(step["run"]) for step in job["steps"] if "run" in step]


def test_ci_never_installs_unlocked_dependencies() -> None:
    for name in ("tests.yml", "integration-nightly.yml"):
        for job_id, job in _workflow(name)["jobs"].items():
            for script in _run_steps(job):
                for line in script.splitlines():
                    if "pip install" not in line or "pip install --upgrade pip" in line:
                        continue
                    allowed = (
                        HASHED in line
                        or "pip install uv==" in line
                        or re.search(r"pip install (--no-deps )?(-e )?\.", line)
                    )
                    assert allowed, f"{name}:{job_id}: installation non verrouillée -> {line.strip()}"


def test_pytest_and_lint_jobs_install_their_hashed_locks() -> None:
    jobs = _workflow("tests.yml")["jobs"]

    assert any("locks/lint.txt" in s and HASHED in s for s in _run_steps(jobs["lint"]))
    assert any("locks/dev.txt" in s and HASHED in s for s in _run_steps(jobs["pytest"]))
    audit = _run_steps(jobs["dependency-audit"])
    assert any("locks/audit.txt" in s and HASHED in s for s in audit)
    assert any("scripts/locks.py check" in s for s in audit), "la dérive source→lock doit échouer la CI"
    pytest_steps = _run_steps(jobs["pytest"])
    assert any("scripts/locks.py check" in s for s in pytest_steps)


def test_dockerfiles_install_from_hashed_locks() -> None:
    expected = {
        "docker/collegue/Dockerfile": "locks/runtime.txt",
        "docker/sandbox/Dockerfile": "locks/sandbox.txt",
        "docker/sandbox/Dockerfile.openhands": "locks/sandbox-openhands.txt",
    }
    for dockerfile, lock in expected.items():
        text = (ROOT / dockerfile).read_text(encoding="utf-8")
        assert lock in text, f"{dockerfile} ne référence pas {lock}"
        installs = [ln for ln in text.splitlines() if re.search(r"(pip install|uv pip install)", ln)]
        assert installs
        for line in installs:
            assert HASHED in line or line.strip().startswith("#") or "playwright install" in line, (
                f"{dockerfile}: installation non verrouillée -> {line.strip()}"
            )


def test_openhands_dockerfile_keeps_the_gemma4_patch_validation() -> None:
    text = (ROOT / "docker" / "sandbox" / "Dockerfile.openhands").read_text(encoding="utf-8")

    assert "scripts/patch_openhands_gemma4_terminal.py" in text
    assert "python /opt/patch_openhands_gemma4_terminal.py" in text
    # le patch s'exécute APRÈS l'installation verrouillée et avant tout autre usage du SDK
    assert text.index("locks/sandbox-openhands.txt") < text.index("python /opt/patch_openhands_gemma4_terminal.py")
    assert "COPY collegue/executor/oh_runner.py" in text and "COPY collegue/executor/oh_sampler.py" in text


def test_collegue_image_ships_skills_inside_the_package() -> None:
    text = (ROOT / "docker" / "collegue" / "Dockerfile").read_text(encoding="utf-8")

    assert "./skills" not in text, "les skills vivent dans collegue/skills (copié avec le paquet)"
    assert "COPY --chown=collegue:collegue ./collegue ./collegue" in text
    assert "requirements.txt" not in text.replace("locks/runtime.txt", ""), "plus de requirements non verrouillé"


def test_required_check_names_are_unchanged() -> None:
    workflow = _workflow("tests.yml")
    jobs = workflow["jobs"]
    matrix = jobs["pytest"]["strategy"]["matrix"]["python-version"]
    names = {jobs["lint"]["name"], jobs["dependency-audit"]["name"], jobs["docker-build"]["name"]}
    names |= {jobs["pytest"]["name"].replace("${{ matrix.python-version }}", str(v)) for v in matrix}

    assert names == {"Ruff", "Pytest (Python 3.11)", "Pytest (Python 3.12)", "Dependency audit", "Docker build"}
    assert set(jobs) == {"lint", "pytest", "dependency-audit", "docker-build"}


def test_ci_verifies_the_built_wheel_in_a_clean_environment() -> None:
    steps = _run_steps(_workflow("tests.yml")["jobs"]["dependency-audit"])

    assert any("scripts/verify_wheel.py" in s for s in steps)
