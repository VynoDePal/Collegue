"""Câblage CI des preuves PostgreSQL réelles (vagues 2 et 3).

Une garantie de concurrence ou de contrainte ne se prouve pas sur SQLite ni sur un double : le job ``Pytest`` requis
doit lancer, contre un VRAI service, ``tests/test_budget_ledger_postgres.py`` (registre de budget, vague 2),
``tests/test_task_merge_postgres.py`` (état durable de fusion, vague 3) et ``tests/test_delivery_proof_postgres.py``
(persistance des preuves de livraison, vague 3), et devenir rouge si un test échoue, est sauté, est désélectionné ou si
la collecte est tronquée. Ces tests vérifient (1) la structure du workflow, (2) le script de garde JUnit, (3) le texte
RÉEL de chaque étape exécuté avec un faux ``pytest``.
"""

from __future__ import annotations

import os
import re
import stat
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "tests.yml"
GATE = ROOT / "scripts" / "ci_require_junit.py"
PG_TEST_FILE = "tests/test_budget_ledger_postgres.py"
STEP_NAME = "PostgreSQL budget ledger - real concurrency proof"
REQUIRED_NAMES = {"Ruff", "Pytest (Python 3.11)", "Pytest (Python 3.12)", "Dependency audit", "Docker build"}
FLOOR = 21  # plancher du workflow : à relever si des cas sont ajoutés, jamais à abaisser

# (nom de l'étape, fichier de tests, plancher exact, rapport JUnit)
PROOF_STEPS = [
    (STEP_NAME, PG_TEST_FILE, FLOOR, "pg-budget.xml"),
    (
        "PostgreSQL task merges - real durable-state proof",
        "tests/test_task_merge_postgres.py",
        23,
        "pg-task-merges.xml",
    ),
    (
        "PostgreSQL delivery proofs - real persistence proof",
        "tests/test_delivery_proof_postgres.py",
        5,
        "pg-delivery-proofs.xml",
    ),
]
PROOF_IDS = ["budget", "task_merges", "delivery_proofs"]


def _workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _pytest_job() -> dict:
    return _workflow()["jobs"]["pytest"]


def _step(name: str = STEP_NAME) -> dict:
    return next(step for step in _pytest_job()["steps"] if step.get("name") == name)


# --- (1) structure du workflow ---------------------------------------------------------------------------------


def test_required_check_names_and_matrix_are_unchanged() -> None:
    jobs = _workflow()["jobs"]
    job = jobs["pytest"]
    assert job["name"] == "Pytest (Python ${{ matrix.python-version }})"
    assert job["strategy"]["matrix"]["python-version"] == ["3.11", "3.12"]
    names = {
        jobs["lint"]["name"],
        jobs["dependency-audit"]["name"],
        jobs["docker-build"]["name"],
        "Pytest (Python 3.11)",
        "Pytest (Python 3.12)",
    }
    assert names == REQUIRED_NAMES


def test_postgres_service_is_explicit_ephemeral_and_secret_free() -> None:
    job = _pytest_job()
    service = job["services"]["postgres"]
    assert service["image"] == "postgres:16"
    assert "5432:5432" in [str(port) for port in service["ports"]]
    assert "pg_isready" in service["options"], "le job ne démarre qu'une fois le service prêt"
    assert job["env"]["COLLEGUE_TEST_POSTGRES_URL"] == "postgresql+psycopg2://postgres:postgres@localhost:5432/postgres"
    text = WORKFLOW.read_text(encoding="utf-8")
    segment = text[text.index("  pytest:") : text.index("  dependency-audit:")]
    assert "secrets." not in segment, "la preuve PostgreSQL n'utilise aucun secret"
    assert "continue-on-error" not in segment, "un échec doit rendre le job requis rouge"


@pytest.mark.parametrize("name, test_file, floor, report", PROOF_STEPS, ids=PROOF_IDS)
def test_each_proof_runs_in_both_required_python_jobs_before_the_general_run(name, test_file, floor, report) -> None:
    steps = [step.get("name") for step in _pytest_job()["steps"]]
    assert name in steps
    assert steps.index(name) < steps.index("Run pytest with coverage")
    step = _step(name)
    assert step.get("shell") == "bash", "pipefail explicite : le compte de collecte passe par un pipe"
    assert "if" not in step, "l'étape n'est jamais conditionnelle"
    assert not step.get("continue-on-error")
    script = str(step["run"])
    assert test_file in script and report in script and "scripts/ci_require_junit.py" in script
    assert "-o addopts=" in script, "mêmes options pour la collecte et l'exécution"


@pytest.mark.parametrize("name, test_file, floor, report", PROOF_STEPS, ids=PROOF_IDS)
def test_the_floor_is_the_exact_count_the_file_really_collects(name, test_file, floor, report) -> None:
    script = str(_step(name)["run"])
    assert int(re.search(r"--min-tests\s+(\d+)", script).group(1)) == floor
    collected = subprocess.run(
        [sys.executable, "-m", "pytest", test_file, "-o", "addopts=", "--collect-only", "-q", "-p", "no:cacheprovider"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, "PYTHONPATH": str(ROOT)},
    ).stdout
    count = sum(1 for line in collected.splitlines() if "::" in line)
    # plancher == compte exact : un test supprimé ne se perd pas en silence, un test ajouté impose de relever le plancher
    assert count == floor, f"{test_file} collecte {count} tests mais le plancher du workflow est {floor}"


def test_the_upload_of_the_reports_never_hides_a_failure() -> None:
    upload = _step("Upload PostgreSQL proof report")
    assert upload["if"] == "always()"
    for _name, _file, _floor, report in PROOF_STEPS:
        assert report in upload["with"]["path"]


# --- (2) script de garde JUnit ------------------------------------------------------------------------------------


def _junit(path: Path, *, passed: int = 0, skipped: int = 0, failed: int = 0, errors: int = 0) -> Path:
    cases = [f'<testcase classname="tests.t" name="ok{i}"/>' for i in range(passed)]
    cases += [f'<testcase classname="tests.t" name="skip{i}"><skipped message="x"/></testcase>' for i in range(skipped)]
    cases += [f'<testcase classname="tests.t" name="bad{i}"><failure message="x"/></testcase>' for i in range(failed)]
    cases += [f'<testcase classname="tests.t" name="err{i}"><error message="x"/></testcase>' for i in range(errors)]
    total = passed + skipped + failed + errors
    path.write_text(
        f'<?xml version="1.0"?><testsuites><testsuite name="pytest" tests="{total}">{"".join(cases)}</testsuite></testsuites>',
        encoding="utf-8",
    )
    return path


def _gate(report: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(GATE), str(report), *args], capture_output=True, text=True, timeout=60, check=False
    )


def test_gate_accepts_a_complete_report(tmp_path: Path) -> None:
    report = _junit(tmp_path / "r.xml", passed=21)
    result = _gate(report, "--min-tests", "21", "--expect-tests", "21")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "tests=21 skipped=0 failed=0" in result.stdout


@pytest.mark.parametrize(
    "kwargs, args, needle",
    [
        ({"passed": 20}, ("--min-tests", "21"), "minimum"),
        ({"passed": 20, "skipped": 1}, ("--min-tests", "21"), "sauté"),
        ({"passed": 21, "failed": 1}, ("--min-tests", "21"), "échec"),
        ({"passed": 21, "errors": 1}, ("--min-tests", "21"), "échec"),
        ({"passed": 21}, ("--min-tests", "21", "--expect-tests", "22"), "collecté"),
        ({"passed": 22}, ("--min-tests", "21", "--expect-tests", "21"), "collecté"),
    ],
)
def test_gate_rejects_missing_proof(tmp_path: Path, kwargs: dict, args: tuple, needle: str) -> None:
    result = _gate(_junit(tmp_path / "r.xml", **kwargs), *args)
    assert result.returncode == 1, result.stdout
    assert needle in result.stdout


def test_gate_rejects_absent_unreadable_and_dtd_reports(tmp_path: Path) -> None:
    assert _gate(tmp_path / "absent.xml", "--min-tests", "1").returncode == 1
    broken = tmp_path / "broken.xml"
    broken.write_text("<testsuites><testsuite", encoding="utf-8")
    assert _gate(broken, "--min-tests", "1").returncode == 1
    dtd = tmp_path / "dtd.xml"
    dtd.write_text(
        '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "b">]><testsuites><testsuite><testcase name="t"/></testsuite></testsuites>',
        encoding="utf-8",
    )
    refused = _gate(dtd, "--min-tests", "1")
    assert refused.returncode == 1 and "DTD" in refused.stdout


def test_gate_usage_errors_are_not_successes(tmp_path: Path) -> None:
    report = _junit(tmp_path / "r.xml", passed=1)
    assert _gate(report, "--min-tests", "0").returncode == 2


# --- (3) le texte RÉEL de l'étape, exécuté avec un faux pytest ---------------------------------------------------

FAKE_PYTEST = r"""#!/usr/bin/env python3
import os, sys
args = sys.argv[1:]
scenario = os.environ["FAKE_SCENARIO"]
total = int(os.environ.get("FAKE_TOTAL", "21"))
if "--collect-only" in args:
    for i in range(total):
        print(f"{os.environ['FAKE_FILE']}::test_{i}")
    print(f"\n{total} tests collected in 0.1s")
    sys.exit(0)
report = next(a.split("=", 1)[1] for a in args if a.startswith("--junitxml="))
cases = [f'<testcase classname="t" name="n{i}"/>' for i in range(total)]
exit_code = 0
if scenario == "skip":
    cases[0] = '<testcase classname="t" name="n0"><skipped message="x"/></testcase>'
elif scenario == "fail":
    cases[0] = '<testcase classname="t" name="n0"><failure message="x"/></testcase>'; exit_code = 1
elif scenario == "short":
    cases = cases[:-1]
elif scenario == "exit1":
    exit_code = 1
if scenario != "nofile":
    with open(report, "w", encoding="utf-8") as handle:
        handle.write('<?xml version="1.0"?><testsuites><testsuite>' + "".join(cases) + "</testsuite></testsuites>")
sys.exit(exit_code)
"""


def _run_step(
    tmp_path: Path,
    scenario: str,
    *,
    url: str | None = "postgresql+psycopg2://x@localhost:5432/p",
    total: int = 21,
    step: str = STEP_NAME,
    test_file: str = PG_TEST_FILE,
):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "pytest"
    fake.write_text(FAKE_PYTEST, encoding="utf-8")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    (bin_dir / "python").symlink_to(sys.executable)
    work = tmp_path / "work"
    work.mkdir()
    (work / "scripts").symlink_to(ROOT / "scripts")
    env = {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "HOME": str(tmp_path),
        "FAKE_SCENARIO": scenario,
        "FAKE_TOTAL": str(total),
        "FAKE_FILE": test_file,
        "PYTHONPATH": ".",
    }
    if url is not None:
        env["COLLEGUE_TEST_POSTGRES_URL"] = url
    script = str(_step(step)["run"])
    # GitHub lance « shell: bash » comme : bash --noprofile --norc -eo pipefail {0}
    return subprocess.run(
        ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", script],
        cwd=work,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


@pytest.mark.parametrize("name, test_file, floor, report", PROOF_STEPS, ids=PROOF_IDS)
def test_real_step_passes_only_when_every_collected_test_ran_without_skip(
    tmp_path: Path, name, test_file, floor, report
) -> None:
    result = _run_step(tmp_path, "ok", total=floor, step=name, test_file=test_file)
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"tests PostgreSQL collectés: {floor}" in result.stdout


@pytest.mark.parametrize("name, test_file, floor, report", PROOF_STEPS, ids=PROOF_IDS)
@pytest.mark.parametrize("scenario", ["skip", "fail", "short", "exit1", "nofile"])
def test_real_step_is_red_on_any_missing_proof(tmp_path: Path, scenario: str, name, test_file, floor, report) -> None:
    result = _run_step(tmp_path, scenario, total=floor, step=name, test_file=test_file)
    assert result.returncode != 0, f"{scenario}: l'étape ne doit pas passer\n{result.stdout}{result.stderr}"


@pytest.mark.parametrize("name, test_file, floor, report", PROOF_STEPS, ids=PROOF_IDS)
def test_real_step_is_red_without_the_service_url(tmp_path: Path, name, test_file, floor, report) -> None:
    result = _run_step(tmp_path, "ok", url=None, total=floor, step=name, test_file=test_file)
    assert result.returncode != 0


@pytest.mark.parametrize("name, test_file, floor, report", PROOF_STEPS, ids=PROOF_IDS)
def test_real_step_is_red_when_collection_loses_tests_below_the_floor(
    tmp_path: Path, name, test_file, floor, report
) -> None:
    result = _run_step(tmp_path, "ok", total=floor - 1, step=name, test_file=test_file)
    assert result.returncode != 0
    assert "minimum" in result.stdout
