"""Raccord CI de la preuve du vérificateur métier réel (propriété C, vague 4).

Ces tests sont STRUCTURELS et de propagation d'erreurs : ils n'éprouvent NI Docker, NI l'image, NI le vrai vérificateur de B
(absent du tronc C, seulement disponible après intégration). Le module de B, la fixture, l'horloge et Docker y sont des DOUBLES :
ils servent à prouver que le script ne rend jamais de faux vert (cas non exécuté, image absente, assertion manquante ou fausse,
fin par la relève hôte, import du témoin non atteint, durée hors fenêtre, conteneur résiduel, exception) et que le workflow le
câble sans secret ni contournement. La preuve réelle viendra de la CI de la révision intégrée.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "tests.yml"
SCRIPT = ROOT / "scripts" / "ci_w4_business_verifier.py"
OH_IMAGE = "collegue-sandbox-openhands:pr-check"
STEP_PROOF = "Prove the business verifier in the built image (public path, no model)"
STEP_UPLOAD = "Upload business verifier proof report"


def _load():
    spec = importlib.util.spec_from_file_location("ci_w4_business_verifier_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def proof():
    return _load()


# --------------------------------------------------------------------------------------------- workflow


def _steps() -> list:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]["docker-build"]["steps"]


def _names() -> list:
    return [step.get("name") for step in _steps()]


def test_required_job_names_and_earlier_proofs_are_preserved():
    jobs = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]
    assert {job["name"] for job in jobs.values()} == {
        "Ruff",
        "Pytest (Python ${{ matrix.python-version }})",
        "Dependency audit",
        "Docker build",
    }
    names = _names()
    for kept in (
        "Build OpenHands sandbox image",
        "Verify OpenHands Gemma 4 terminal contract",
        "Verify OpenHands worker routing (real SDK, no model)",
        "Smoke test — container starts and serves",
        "Upload smoke logs",
    ):
        assert kept in names
    audit = [step.get("name") for step in jobs["dependency-audit"]["steps"]]
    assert "Run pip-audit" in audit and "Audit locked dev dependencies (strict)" in audit


def test_the_proof_runs_after_the_openhands_build_and_before_the_smoke_test_with_locked_dependencies():
    names = _names()
    build = names.index("Build OpenHands sandbox image")
    install = names.index("Install locked test dependencies for the verifier proof")
    assert build < names.index("Set up Python 3.12 for the verifier proof") < install < names.index(STEP_PROOF)
    assert names.index(STEP_PROOF) < names.index(STEP_UPLOAD) < names.index("Smoke test — container starts and serves")
    step = next(s for s in _steps() if s.get("name") == "Install locked test dependencies for the verifier proof")
    assert " ".join(step["run"].split()) == "pip install --require-hashes --no-deps -r locks/dev.txt"


def test_the_proof_step_targets_the_built_image_without_secret_or_bypass():
    step = next(s for s in _steps() if s.get("name") == STEP_PROOF)
    command = " ".join(step["run"].split())
    assert command == f"python scripts/ci_w4_business_verifier.py --image {OH_IMAGE} --report-dir w4-verifier-report"
    built = next(s for s in _steps() if s.get("name") == "Build OpenHands sandbox image")
    assert built["with"]["tags"] == OH_IMAGE
    assert "continue-on-error" not in step and "if" not in step, "la preuve est inconditionnelle et bloquante"
    assert step.get("env") is None and "secrets" not in yaml.safe_dump(step)
    assert step.get("timeout-minutes")
    assert "continue-on-error" not in WORKFLOW.read_text(encoding="utf-8")


def test_the_report_is_uploaded_even_on_failure():
    step = next(s for s in _steps() if s.get("name") == STEP_UPLOAD)
    assert step["if"] == "always()" and step["uses"].startswith("actions/upload-artifact@")
    assert step["with"]["path"] == "w4-verifier-report/"
    assert step["with"]["if-no-files-found"] == "warn"


def test_the_workflow_documents_the_scope_of_the_deadline_proof():
    text = WORKFLOW.read_text(encoding="utf-8")
    at = text.index("Preuve du VRAI vérificateur métier public")
    comment = text[at : at + 1400]
    for fragment in ("runner omis", "sans réseau", "SIGALRM", "relève hôte", "crash réel de l'hôte"):
        assert fragment in comment


def test_the_script_builds_no_mount_and_runs_nothing_generated_on_the_host():
    source = SCRIPT.read_text(encoding="utf-8")
    for forbidden in (
        '"-v"',
        "--volume",
        "--mount",
        "docker_verifier_command(",
        "trusted_local_runner",
        "exec(",
        "os.system",
    ):
        assert forbidden not in source
    assert "runner=" not in source.replace("runner omis", ""), "le chemin public est appelé SANS runner"
    assert "verify_business_checkout(" in source


# --------------------------------------------------------------------------------------------- doubles du script


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class FakeDocker:
    """Double de la frontière Docker de l'orchestrateur (lectures seules)."""

    def __init__(self, *, image_ok=True, leftover=()):
        self.image_ok = image_ok
        self.leftover = set(leftover)
        self.calls = []

    def __call__(self, args, timeout=120.0):
        self.calls.append(list(args))
        if args[:2] == ["image", "inspect"]:
            return subprocess.CompletedProcess(
                args, 0 if self.image_ok else 1, "sha256:abc\n" if self.image_ok else "", ""
            )
        if args[0] == "version":
            return subprocess.CompletedProcess(args, 0, "27.0 / 27.0\n", "")
        if args[0] == "run":
            return subprocess.CompletedProcess(args, 0, "Python 3.12.1\ntimeout (GNU coreutils) 9.4\n", "")
        if args[0] == "ps":
            name = args[-1].removeprefix("name=^/").removesuffix("$")
            return subprocess.CompletedProcess(args, 0, "abc\n" if name in self.leftover else "", "")
        raise AssertionError(args)


CHECKS = lambda proof, **override: {name: True for name in proof.EXPECTED_CHECKS} | override  # noqa: E731


def make_business(proof, clock, scenario):
    """Double du module de B : appelle ``run_in_named_container`` PAR L'ATTRIBUT du module, comme le vrai chemin."""
    module = SimpleNamespace(
        HOST_KILL_MARGIN=2.0, WATCHDOG_KILL_AFTER=3, seen_margins=[], images=[], container_names=[]
    )

    def run_in_named_container(argv, *, name, timeout, runner=None, env=None):
        module.container_names.append(name)
        if module.hostile is None:
            clock.now += 3
            return subprocess.CompletedProcess([], 0, '{"ok": 1}', "")
        return scenario["hostile_process"](clock, module.hostile)

    def verify_business_checkout(checkout, *, image=None, timeout=120.0, **_kw):
        module.seen_margins.append(module.HOST_KILL_MARGIN)
        module.images.append(image)
        text = (Path(checkout) / "app" / "main.py").read_text(encoding="utf-8")
        name = f"w4-verify-{len(module.container_names):012d}"
        if proof.HOSTILE_MARKER not in text:
            module.hostile = None
            for _ in range(2):
                module.run_in_named_container(["docker", "run"], name=name, timeout=timeout)
            return scenario["observation"](proof)
        module.hostile = "term" if "SIGTERM" in text else "alarm"
        try:
            proc = module.run_in_named_container(["docker", "run", "-e", "X=" + "x" * 400], name=name, timeout=timeout)
        except subprocess.TimeoutExpired:  # comme B : la relève hôte conclut « incomplete »
            return SimpleNamespace(
                status="incomplete", checks={}, observations={}, failed=[], detail="délai de vérification dépassé"
            )
        return scenario["hostile_observation"](proc)

    module.run_in_named_container = run_in_named_container
    module.verify_business_checkout = verify_business_checkout
    module.hostile = None
    return module


def good_scenario(proof):
    def hostile_process(clock, mode):
        # le témoin ignore SIGALRM (et SIGTERM) : le superviseur conclut à l'échéance (+3 s s'il faut un KILL)
        clock.now += 8 if mode == "alarm" else 11
        return subprocess.CompletedProcess([], 124 if mode == "alarm" else 137, "", f"{proof.HOSTILE_MARKER}\n")

    return {
        "observation": lambda p: SimpleNamespace(
            status="passed",
            checks=CHECKS(p),
            observations={
                "write.db_existed_before": False,
                "write.pdf_reader": "pypdf 6.19.0",
                "write.pdf_raw_bytes_contain_title": False,
            },
            failed=[],
            detail="",
        ),
        "hostile_observation": lambda proc: SimpleNamespace(
            status="incomplete", checks={}, observations={}, failed=[], detail="échéance de la vérification"
        ),
        "hostile_process": hostile_process,
    }


@pytest.fixture
def harness(proof, monkeypatch):
    clock = Clock()
    monkeypatch.setattr(proof, "_clock", clock)
    scenario = good_scenario(proof)
    business = make_business(proof, clock, scenario)

    def stage_files(stage):
        assert stage == 3
        return {"app/main.py": '"""fixture"""\napp = object()\n', "alembic.ini": "[alembic]\n"}

    return SimpleNamespace(clock=clock, business=business, scenario=scenario, stage_files=stage_files)


def run(proof, harness, **kw):
    return proof.run_proof(
        OH_IMAGE,
        docker=kw.pop("docker", FakeDocker()),
        business=harness.business,
        stage_files=harness.stage_files,
        **kw,
    )


# --------------------------------------------------------------------------------------------- propagation d'erreurs


def test_all_required_cases_passing_is_the_only_success(proof, harness):
    report = run(proof, harness)
    assert report["ok"] is True and report["failures"] == []
    assert set(report["cases"]) == set(proof.REQUIRED_CASES)
    assert all(case["status"] == "passed" for case in report["cases"].values())
    assert report["cases"][proof.CASE_ALARM]["expected_returncode"] == 124
    assert report["cases"][proof.CASE_TERM]["expected_returncode"] == 137
    assert report["host"]["image_id"] == "sha256:abc" and "timeout" in report["host"]["image_timeout"]
    assert harness.business.images == [OH_IMAGE] * 3, (
        "l'image construite est passée explicitement à chaque appel public"
    )


def test_the_host_relief_is_lengthened_only_during_the_hostile_cases_and_restored(proof, harness):
    run(proof, harness)
    assert harness.business.seen_margins == [2.0, proof.HOST_RELIEF_MARGIN, proof.HOST_RELIEF_MARGIN]
    assert harness.business.HOST_KILL_MARGIN == 2.0


def test_the_container_spy_is_removed_after_each_case_and_masks_the_inline_script(proof, harness):
    original = harness.business.run_in_named_container
    report = run(proof, harness)
    assert harness.business.run_in_named_container is original
    hostile = report["cases"][proof.CASE_ALARM]["containers"][0]
    assert all(len(arg) <= 300 for arg in hostile["argv"]) and any("caractères" in arg for arg in hostile["argv"])


def test_a_missing_image_fails_every_case_without_running_any(proof, harness):
    report = run(proof, harness, docker=FakeDocker(image_ok=False))
    assert report["ok"] is False and "absente" in report["failures"][0]
    assert {case["status"] for case in report["cases"].values()} == {"not_executed"}
    assert harness.business.container_names == []


def test_a_missing_business_module_is_a_failure_not_a_skip(proof, harness, monkeypatch):
    def absent(name, *args, **kwargs):  # le lot B peut être intégré : l'absence est SIMULÉE à la frontière d'import
        raise ModuleNotFoundError(f"No module named {name!r}")

    monkeypatch.setattr(proof.importlib, "import_module", absent)
    report = proof.run_proof(OH_IMAGE, docker=FakeDocker(), stage_files=harness.stage_files)
    assert report["ok"] is False
    assert any("lot B non intégré" in failure for failure in report["failures"])
    assert {case["status"] for case in report["cases"].values()} == {"not_executed"}


def test_load_fixture_refuses_an_absent_file(proof, tmp_path):
    with pytest.raises(proof.ProofError):
        proof.load_fixture(tmp_path / "absent.py")


@pytest.mark.parametrize(
    "mutate, fragment",
    [
        (lambda p, c: c.pop("write:pdf_served"), "assertions métier absentes"),
        (lambda p, c: c.__setitem__("reread:audit_survives_restart", False), "assertions métier fausses"),
    ],
)
def test_a_missing_or_false_business_assertion_fails_the_pass_case(proof, harness, mutate, fragment):
    def observation(p):
        checks = CHECKS(p)
        mutate(p, checks)
        return SimpleNamespace(
            status="passed",
            checks=checks,
            observations={
                "write.db_existed_before": False,
                "write.pdf_reader": "pypdf 6.19.0",
                "write.pdf_raw_bytes_contain_title": False,
            },
            failed=[],
            detail="",
        )

    harness.scenario["observation"] = observation
    report = run(proof, harness)
    assert report["ok"] is False and fragment in report["cases"][proof.CASE_PASS]["detail"]


@pytest.mark.parametrize("status", ["failed", "incomplete"])
def test_a_non_passed_observation_fails_the_pass_case(proof, harness, status):
    harness.scenario["observation"] = lambda p: SimpleNamespace(
        status=status, checks=CHECKS(p), observations={}, failed=["write:audit_created"], detail="x"
    )
    report = run(proof, harness)
    assert report["ok"] is False and report["cases"][proof.CASE_PASS]["status"] == "failed"
    # les cas suivants sont tout de même exécutés et rapportés (un échec n'en masque pas d'autre)
    assert report["cases"][proof.CASE_ALARM]["status"] == "passed"


def test_a_pdf_readable_by_byte_search_is_not_a_real_reader_proof(proof, harness):
    base = harness.scenario["observation"]

    def observation(p):
        value = base(p)
        value.observations["write.pdf_raw_bytes_contain_title"] = True
        return value

    harness.scenario["observation"] = observation
    assert run(proof, harness)["ok"] is False


def test_a_hostile_witness_that_completes_normally_is_not_a_deadline_proof(proof, harness):
    harness.scenario["hostile_observation"] = lambda proc: SimpleNamespace(
        status="passed", checks={}, observations={}, failed=[], detail=""
    )
    report = run(proof, harness)
    assert report["cases"][proof.CASE_ALARM]["status"] == "failed"
    assert "au lieu de 'incomplete'" in report["cases"][proof.CASE_ALARM]["detail"]


def test_ending_by_the_host_relief_is_not_credited_to_the_container_supervisor(proof, harness):
    def hostile_process(clock, mode):
        clock.now += 8
        raise subprocess.TimeoutExpired("docker", 70)

    harness.scenario["hostile_process"] = hostile_process
    report = run(proof, harness)
    assert report["ok"] is False
    assert "fin par exception hôte (TimeoutExpired)" in report["cases"][proof.CASE_ALARM]["detail"]


def test_a_wrong_container_return_code_fails(proof, harness):
    def hostile_process(clock, mode):
        clock.now += 8
        return subprocess.CompletedProcess([], 1, "", f"{proof.HOSTILE_MARKER}\n")

    harness.scenario["hostile_process"] = hostile_process
    assert "au lieu de 124" in run(proof, harness)["cases"][proof.CASE_ALARM]["detail"]


def test_an_import_that_was_not_reached_is_not_an_alarm_proof(proof, harness):
    def hostile_process(clock, mode):
        clock.now += 8
        return subprocess.CompletedProcess([], 124, "", "migration échouée\n")

    harness.scenario["hostile_process"] = hostile_process
    assert "import du témoin n'a pas été atteint" in run(proof, harness)["cases"][proof.CASE_ALARM]["detail"]


@pytest.mark.parametrize("seconds", [1.0, 600.0])
def test_a_duration_outside_the_deadline_window_fails(proof, harness, seconds):
    def hostile_process(clock, mode):
        clock.now += seconds
        return subprocess.CompletedProcess([], 124, "", f"{proof.HOSTILE_MARKER}\n")

    harness.scenario["hostile_process"] = hostile_process
    assert "hors de la fenêtre d'échéance" in run(proof, harness)["cases"][proof.CASE_ALARM]["detail"]


def test_a_leftover_container_fails(proof, harness):
    docker = FakeDocker(leftover={"w4-verify-000000000000"})
    report = run(proof, harness, docker=docker)
    assert report["ok"] is False and "résiduel" in report["cases"][proof.CASE_PASS]["detail"]


def test_an_exception_in_a_case_is_a_failed_case_and_the_other_cases_still_run(proof, harness):
    def boom(p):
        raise RuntimeError("docker en panne")

    harness.scenario["observation"] = boom
    report = run(proof, harness)
    assert report["cases"][proof.CASE_PASS]["status"] == "failed"
    assert "docker en panne" in report["cases"][proof.CASE_PASS]["detail"]
    assert report["cases"][proof.CASE_ALARM]["status"] == "passed" and report["ok"] is False


def test_the_report_is_written_with_the_real_return_codes_and_no_secret(proof, harness, tmp_path, monkeypatch):
    monkeypatch.setenv("W4_FAKE_SECRET_TOKEN", "sk-super-secret-value")
    report = run(proof, harness)
    report["host"]["leak"] = "sk-super-secret-value"
    path = proof.write_report(report, tmp_path / "out")
    text = path.read_text(encoding="utf-8")
    assert "sk-super-secret-value" not in text
    data = json.loads(text)
    assert [c["returncode"] for c in data["cases"][proof.CASE_PASS]["containers"]] == [0, 0]
    assert data["cases"][proof.CASE_TERM]["containers"][0]["returncode"] == 137


def test_main_exit_code_and_report_on_failure(proof, harness, tmp_path, monkeypatch, capsys):
    original = proof.run_proof
    monkeypatch.setattr(proof, "run_proof", lambda image: original(image, docker=FakeDocker(image_ok=False)))
    rc = proof.main(["--image", OH_IMAGE, "--report-dir", str(tmp_path / "r")])
    assert rc == 1
    assert json.loads((tmp_path / "r" / "report.json").read_text())["ok"] is False
    assert "ÉCHEC" in capsys.readouterr().out


def test_main_writes_the_report_even_when_interrupted(proof, tmp_path, monkeypatch):
    def interrupted(image):
        raise KeyboardInterrupt

    monkeypatch.setattr(proof, "run_proof", interrupted)
    with pytest.raises(KeyboardInterrupt):
        proof.main(["--image", OH_IMAGE, "--report-dir", str(tmp_path / "r")])
    assert json.loads((tmp_path / "r" / "report.json").read_text())["ok"] is False


def test_the_hostile_preambles_neutralise_the_alarm_and_mark_the_import(proof):
    for preamble in (proof.HOSTILE_ALARM, proof.HOSTILE_TERM):
        compile(preamble, "<hostile>", "exec")
        assert "signal.alarm(0)" in preamble and "SIGALRM, signal.SIG_IGN" in preamble
        assert proof.HOSTILE_MARKER in preamble and f"time.sleep({proof.HOSTILE_SLEEP_SECONDS})" in preamble
    assert "SIGTERM, signal.SIG_IGN" in proof.HOSTILE_TERM and "SIGTERM" not in proof.HOSTILE_ALARM
    assert proof.HOSTILE_SLEEP_SECONDS > proof.HOSTILE_LIMIT_SECONDS * 10
