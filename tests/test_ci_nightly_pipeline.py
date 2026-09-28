"""Fidélité du statut CI du nightly d'intégration.

Défaut d'origine (run 34577743360) : l'étape « Run integration suite » exécutait
``pytest ... | tee log`` sous ``bash -e`` SANS ``pipefail``. Le statut du pipeline
était celui de ``tee`` (0) : un run contenant ``1 failed, 7 passed, 7 skipped``
était marqué « success ».

Ces tests exécutent RÉELLEMENT le script de l'étape déclarée dans le workflow
(avec le shell que GitHub lui appliquerait) contre un faux ``pytest`` dont on
maîtrise le code de sortie, puis vérifient le bilan structuré (JUnit XML).
"""

from __future__ import annotations

import os
import stat
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
NIGHTLY = ROOT / ".github" / "workflows" / "integration-nightly.yml"
TESTS_WORKFLOW = ROOT / ".github" / "workflows" / "tests.yml"
BILAN = ROOT / "scripts" / "ci_integration_bilan.py"


def _load(path: Path) -> dict:
    parsed = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(parsed, dict)
    return parsed


def _step(workflow: dict, job: str, *, step_id: str | None = None, name: str | None = None) -> dict:
    for step in workflow["jobs"][job]["steps"]:
        if step_id is not None and step.get("id") == step_id:
            return step
        if name is not None and step.get("name", "").startswith(name):
            return step
    raise AssertionError(f"étape introuvable: job={job} id={step_id} name={name}")


def _github_shell(step: dict) -> list[str]:
    """Reproduit le shell appliqué par GitHub Actions à une étape ``run`` (Linux).

    - ``shell`` absent  → ``bash -e {0}``
    - ``shell: bash``   → ``bash --noprofile --norc -eo pipefail {0}``
    """

    shell = step.get("shell")
    if shell is None:
        return ["bash", "-e"]
    assert shell == "bash", f"shell inattendu dans le test: {shell}"
    return ["bash", "--noprofile", "--norc", "-eo", "pipefail"]


def _fake_pytest(bin_dir: Path) -> None:
    bin_dir.mkdir(parents=True, exist_ok=True)
    fake = bin_dir / "pytest"
    fake.write_text(
        textwrap.dedent(
            """\
            #!/bin/sh
            echo "FAKE-PYTEST-OUTPUT args: $*"
            echo "FAKE-PYTEST-STDERR" >&2
            exit "${FAKE_PYTEST_EXIT:-0}"
            """
        ),
        encoding="utf-8",
    )
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)


def _run_step(step: dict, *, workdir: Path, fake_exit: int, shell: list[str] | None = None):
    bin_dir = workdir / "bin"
    _fake_pytest(bin_dir)
    script = workdir / "step.sh"
    script.write_text(step["run"], encoding="utf-8")
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    env["FAKE_PYTEST_EXIT"] = str(fake_exit)
    return subprocess.run(
        [*(shell or _github_shell(step)), str(script)],
        cwd=workdir,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_premise_naive_pipeline_masks_the_producer_failure(tmp_path: Path) -> None:
    """Caractérise le défaut : sous ``bash -e`` sans pipefail, tee (exit 0) masque exit 1."""

    completed = subprocess.run(
        ["bash", "-e", "-c", 'sh -c "exit 1" | tee "$0"; echo after', str(tmp_path / "log")],
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0
    assert "after" in completed.stdout


@pytest.mark.parametrize("fake_exit", [1, 2, 3, 5])
def test_pytest_step_propagates_the_exact_pytest_exit_code(tmp_path: Path, fake_exit: int) -> None:
    step = _step(_load(NIGHTLY), "integration", step_id="pytest")

    completed = _run_step(step, workdir=tmp_path, fake_exit=fake_exit)

    assert completed.returncode == fake_exit, completed.stdout + completed.stderr


@pytest.mark.parametrize("fake_exit", [1, 5])
def test_pytest_step_does_not_depend_on_the_runner_default_pipefail(tmp_path: Path, fake_exit: int) -> None:
    """Même sous ``bash -e`` nu (défaut sans ``shell:``), l'échec doit être propagé."""

    step = _step(_load(NIGHTLY), "integration", step_id="pytest")

    completed = _run_step(step, workdir=tmp_path, fake_exit=fake_exit, shell=["bash", "-e"])

    assert completed.returncode == fake_exit, completed.stdout + completed.stderr


def test_pytest_step_success_returns_zero_and_keeps_the_log(tmp_path: Path) -> None:
    step = _step(_load(NIGHTLY), "integration", step_id="pytest")

    completed = _run_step(step, workdir=tmp_path, fake_exit=0)

    assert completed.returncode == 0, completed.stdout + completed.stderr
    log = (tmp_path / "pytest-integration.log").read_text(encoding="utf-8")
    assert "FAKE-PYTEST-OUTPUT" in log
    assert "-m integration" in log
    assert "--junitxml=integration-report.xml" in log


def test_pytest_step_keeps_the_full_log_even_when_pytest_fails(tmp_path: Path) -> None:
    step = _step(_load(NIGHTLY), "integration", step_id="pytest")

    completed = _run_step(step, workdir=tmp_path, fake_exit=1)

    assert completed.returncode == 1
    log = (tmp_path / "pytest-integration.log").read_text(encoding="utf-8")
    assert "FAKE-PYTEST-OUTPUT" in log
    assert "FAKE-PYTEST-STDERR" in log  # stderr fusionné dans le journal conservé


def test_pytest_step_reports_a_failing_log_writer(tmp_path: Path) -> None:
    """Si le journal ne peut pas être écrit, le run n'est pas vert même si pytest passe."""

    step = _step(_load(NIGHTLY), "integration", step_id="pytest")
    (tmp_path / "pytest-integration.log").mkdir()  # tee ne peut pas ouvrir un répertoire

    completed = _run_step(step, workdir=tmp_path, fake_exit=0)

    assert completed.returncode != 0, completed.stdout + completed.stderr


def test_report_and_bilan_run_even_when_pytest_fails() -> None:
    workflow = _load(NIGHTLY)
    bilan = _step(workflow, "integration", name="Bilan")
    upload = _step(workflow, "integration", name="Upload report")

    assert bilan["if"] == "always()"
    assert upload["if"] == "always()"
    assert upload["uses"].startswith("actions/upload-artifact@")
    assert "integration-report.xml" in upload["with"]["path"]
    assert "pytest-integration.log" in upload["with"]["path"]


def test_bilan_does_not_use_pytest_wording_as_a_success_criterion() -> None:
    bilan = _step(_load(NIGHTLY), "integration", name="Bilan")["run"]

    assert "ci_integration_bilan.py" in bilan
    assert "tail -n" not in bilan
    assert "grep" not in bilan


def test_product_e2e_status_is_explicit_and_never_green_by_skip() -> None:
    workflow = _load(NIGHTLY)
    job = workflow["jobs"]["e2e-status"]

    assert job["needs"] in ("product-e2e", ["product-e2e"])
    assert job["if"] == "always()"
    script = " ".join(str(step.get("run", "")) for step in job["steps"])
    assert "ci_integration_bilan.py e2e" in script


def test_required_pull_request_check_names_are_unchanged() -> None:
    workflow = _load(TESTS_WORKFLOW)
    jobs = workflow["jobs"]

    names = {
        jobs["lint"]["name"],
        jobs["dependency-audit"]["name"],
        jobs["docker-build"]["name"],
    }
    matrix = jobs["pytest"]["strategy"]["matrix"]["python-version"]
    pytest_name = jobs["pytest"]["name"]
    names |= {pytest_name.replace("${{ matrix.python-version }}", version) for version in matrix}

    assert names == {
        "Ruff",
        "Pytest (Python 3.11)",
        "Pytest (Python 3.12)",
        "Dependency audit",
        "Docker build",
    }


# --- Bilan structuré (JUnit XML) -------------------------------------------------


def _junit(tmp_path: Path, cases: list[tuple[str, str, str | None]]) -> Path:
    """cases: (classname::name, statut, message) avec statut ∈ passed/failed/error/skipped."""

    body = []
    for ident, status, message in cases:
        classname, name = ident.split("::")
        inner = ""
        if status == "failed":
            inner = f'<failure message="{message or "boom"}">trace</failure>'
        elif status == "error":
            inner = f'<error message="{message or "boom"}">trace</error>'
        elif status == "skipped":
            inner = f'<skipped type="pytest.skip" message="{message or "raison"}">skipped</skipped>'
        body.append(f'<testcase classname="{classname}" name="{name}" time="0.01">{inner}</testcase>')
    path = tmp_path / "integration-report.xml"
    path.write_text(
        '<?xml version="1.0" encoding="utf-8"?><testsuites><testsuite name="pytest" '
        f'tests="{len(cases)}">{"".join(body)}</testsuite></testsuites>',
        encoding="utf-8",
    )
    return path


def _bilan(*args: str, summary: Path | None = None):
    env = dict(os.environ)
    env.pop("GITHUB_STEP_SUMMARY", None)
    if summary is not None:
        env["GITHUB_STEP_SUMMARY"] = str(summary)
    return subprocess.run(
        [sys.executable, str(BILAN), *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )


def test_bilan_accepts_a_real_green_run_and_lists_skips(tmp_path: Path) -> None:
    report = _junit(
        tmp_path,
        [
            ("tests.test_a::test_one", "passed", None),
            ("tests.test_a::test_two", "passed", None),
            ("tests.test_b::test_live", "skipped", "GEMINI_API_KEY not set"),
        ],
    )
    summary = tmp_path / "summary.md"

    completed = _bilan("junit", str(report), "--pytest-outcome", "success", summary=summary)

    assert completed.returncode == 0, completed.stdout + completed.stderr
    text = summary.read_text(encoding="utf-8")
    assert "GEMINI_API_KEY not set" in text
    assert "passed" in text.lower()


def test_bilan_fails_when_a_test_failed_even_if_the_step_outcome_says_success(tmp_path: Path) -> None:
    report = _junit(
        tmp_path,
        [
            ("tests.test_a::test_one", "passed", None),
            ("tests.test_a::test_two", "failed", "assert 3 == 2"),
        ],
    )

    completed = _bilan("junit", str(report), "--pytest-outcome", "success")

    assert completed.returncode == 1, completed.stdout + completed.stderr
    assert "::error::" in completed.stdout


def test_bilan_fails_on_collection_errors(tmp_path: Path) -> None:
    report = _junit(tmp_path, [("tests.test_a::test_one", "passed", None), ("tests.test_b::<module>", "error", None)])

    completed = _bilan("junit", str(report), "--pytest-outcome", "success")

    assert completed.returncode == 1


def test_bilan_fails_when_pytest_step_failed_even_with_a_clean_report(tmp_path: Path) -> None:
    report = _junit(tmp_path, [("tests.test_a::test_one", "passed", None)])

    completed = _bilan("junit", str(report), "--pytest-outcome", "failure")

    assert completed.returncode == 1


def test_bilan_fails_when_everything_was_skipped(tmp_path: Path) -> None:
    report = _junit(
        tmp_path, [("tests.test_a::test_one", "skipped", "no key"), ("tests.test_a::test_two", "skipped", "")]
    )

    completed = _bilan("junit", str(report), "--pytest-outcome", "success")

    assert completed.returncode == 1
    assert "skip" in completed.stdout.lower()


@pytest.mark.parametrize("content", [None, "", "<not-xml"])
def test_bilan_fails_without_a_usable_report(tmp_path: Path, content: str | None) -> None:
    report = tmp_path / "integration-report.xml"
    if content is not None:
        report.write_text(content, encoding="utf-8")

    completed = _bilan("junit", str(report), "--pytest-outcome", "success")

    assert completed.returncode == 1
    assert "::error::" in completed.stdout


def test_bilan_refuses_reports_with_entity_declarations(tmp_path: Path) -> None:
    report = tmp_path / "integration-report.xml"
    report.write_text(
        '<?xml version="1.0"?><!DOCTYPE r [<!ENTITY x "boom">]>'
        '<testsuites><testsuite><testcase classname="a" name="b"/></testsuite></testsuites>',
        encoding="utf-8",
    )

    completed = _bilan("junit", str(report), "--pytest-outcome", "success")

    assert completed.returncode == 1
    assert "DTD" in completed.stdout


def test_bilan_does_not_depend_on_pytest_wording(tmp_path: Path) -> None:
    """Le mot « failed » dans un identifiant de test n'est ni un succès ni un échec en soi."""

    report = _junit(tmp_path, [("tests.test_failed_error::test_error_handling_failed", "passed", None)])

    completed = _bilan("junit", str(report), "--pytest-outcome", "success")

    assert completed.returncode == 0, completed.stdout + completed.stderr


@pytest.mark.parametrize(
    ("result", "expected_rc", "needle"),
    [
        ("success", 0, "exécuté"),
        ("skipped", 0, "NON EXÉCUTÉ"),
        ("failure", 1, "échec"),
        ("cancelled", 1, "annulé"),
        ("", 1, "inconnu"),
    ],
)
def test_e2e_status_is_never_presented_as_green_when_not_run(
    tmp_path: Path, result: str, expected_rc: int, needle: str
) -> None:
    summary = tmp_path / "summary.md"

    completed = _bilan("e2e", "--result", result, summary=summary)

    assert completed.returncode == expected_rc, completed.stdout + completed.stderr
    text = summary.read_text(encoding="utf-8")
    assert needle in text
    if result == "skipped":
        assert "::warning::" in completed.stdout
        assert "PAS" in text  # « ne prouve PAS le cycle produit »
        assert "✅" not in text
