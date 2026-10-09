"""Câblage CI de la vague 4 (propriété C) : audit des dépendances de test et contrôle du routage du worker.

Ces tests sont STRUCTURELS et de détection d'écart au niveau du script :

* le workflow ``tests.yml`` garde les cinq jobs requis, audite explicitement ``locks/dev.txt`` en mode strict sans rien
  ignorer, et lance le contrôle d'image dans un conteneur sans réseau, sans secret hôte, script sur l'entrée standard ;
* ``scripts/ci_w4_worker_routing.py`` détecte, sur un FAUX ``openhands.sdk`` et un faux runner, un argument de
  constructeur inconnu et une mauvaise destination (endpoint ou clé).

Ils ne prouvent PAS le comportement du vrai SDK 1.19.1 : ce contrôle ne s'exécute que dans l'image OpenHands, dans le
job « Docker build », après intégration (voir ``reports/w4-c-ci-preparation.md``). Un faux SDK vert ne vaut jamais preuve.
"""

from __future__ import annotations

import importlib.util
import io
import re
import sys
import textwrap
import types
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "tests.yml"
SCRIPT = ROOT / "scripts" / "ci_w4_worker_routing.py"
REQUIRED_NAMES = {"Ruff", "Pytest (Python 3.11)", "Pytest (Python 3.12)", "Dependency audit", "Docker build"}
AUDIT_STEP = "Audit locked dev dependencies (strict)"
ROUTING_STEP = "Verify OpenHands worker routing (real SDK, no model)"
OH_IMAGE = "collegue-sandbox-openhands:pr-check"


def _jobs() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]


def _step(job: str, name: str) -> dict:
    return next(step for step in _jobs()[job]["steps"] if step.get("name") == name)


def _index(job: str, name: str) -> int:
    return [step.get("name") for step in _jobs()[job]["steps"]].index(name)


# --------------------------------------------------------------------------------------------- workflow


def test_the_five_required_job_names_are_unchanged():
    assert {job["name"] for job in _jobs().values()} == {
        "Ruff",
        "Pytest (Python ${{ matrix.python-version }})",
        "Dependency audit",
        "Docker build",
    }
    matrix = _jobs()["pytest"]["strategy"]["matrix"]["python-version"]
    assert {f"Pytest (Python {v})" for v in matrix} == {"Pytest (Python 3.11)", "Pytest (Python 3.12)"}
    assert REQUIRED_NAMES == {"Ruff", "Dependency audit", "Docker build"} | {f"Pytest (Python {v})" for v in matrix}


def test_dev_lock_audit_is_strict_explicit_and_ignores_nothing():
    step = _step("dependency-audit", AUDIT_STEP)
    command = " ".join(step["run"].split())
    assert command == "pip-audit --strict --desc --no-deps --disable-pip -r locks/dev.txt"
    assert "continue-on-error" not in step
    assert "--ignore-vuln" not in command and "--ignore" not in command
    assert "if" not in step, "l'audit ne doit pas être conditionnel"


def test_existing_environment_audit_is_kept_and_runs_before_the_dev_lock_audit():
    existing = _step("dependency-audit", "Run pip-audit")
    assert " ".join(existing["run"].split()) == "pip-audit --strict --desc"
    assert _index("dependency-audit", "Run pip-audit") < _index("dependency-audit", AUDIT_STEP)
    assert "uv" in _step("dependency-audit", "Verify locks match pyproject.toml")["run"]
    assert "continue-on-error" not in existing


def test_no_audit_step_or_job_is_advisory_or_ignores_an_advisory():
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "continue-on-error" not in text
    assert "--ignore-vuln" not in text
    assert "pip-audit" in text and "--strict" in text


def test_audit_documents_its_real_scope():
    """La limite de portée (marqueurs évalués pour 3.12/Linux) est écrite à côté de l'étape, pas seulement dans le rapport."""
    lines = WORKFLOW.read_text(encoding="utf-8").splitlines()
    at = next(i for i, line in enumerate(lines) if AUDIT_STEP in line)
    comment = "\n".join(lines[max(0, at - 10) : at])
    assert "marqueurs" in comment and "3.12" in comment
    for package in ("backports-tarfile", "importlib-metadata", "zipp"):
        assert package in comment


def test_dev_lock_pins_pypdf_and_the_audited_file_exists():
    lock = (ROOT / "locks" / "dev.txt").read_text(encoding="utf-8")
    assert re.search(r"^pypdf==\d+\.\d+\.\d+ \\$", lock, re.MULTILINE)
    assert re.search(r"^# uv: 0\.11\.33$", lock, re.MULTILINE)


def test_routing_step_is_after_the_openhands_build_and_before_the_smoke_test():
    names = [step.get("name") for step in _jobs()["docker-build"]["steps"]]
    assert names.index("Build OpenHands sandbox image") < names.index(ROUTING_STEP)
    assert names.index(ROUTING_STEP) < names.index("Smoke test — container starts and serves")


def test_routing_step_runs_offline_without_secret_and_reads_the_script_on_stdin():
    step = _step("docker-build", ROUTING_STEP)
    command = " ".join(step["run"].replace("\\\n", " ").split())
    assert command == f"docker run --rm -i --network none {OH_IMAGE} python - < scripts/ci_w4_worker_routing.py"
    for forbidden in (
        " -e ",
        "--env",
        "--env-file",
        "-v ",
        "--volume",
        "--mount",
        "secrets.",
        "--network host",
        "bridge",
    ):
        assert forbidden not in f" {command} "
    assert "continue-on-error" not in step
    assert step.get("env") is None and "secrets" not in yaml.safe_dump(step)
    assert step.get("timeout-minutes")


def test_routing_step_targets_the_image_that_was_just_built_and_claims_no_gate_proof():
    built = _step("docker-build", "Build OpenHands sandbox image")
    assert built["with"]["tags"] == OH_IMAGE
    lines = WORKFLOW.read_text(encoding="utf-8").splitlines()
    at = next(i for i, line in enumerate(lines) if ROUTING_STEP in line)
    comment = "\n".join(lines[max(0, at - 8) : at])
    assert "SANS MODÈLE" in comment and "FACTICES" in comment and "SANS réseau" in comment
    assert "ne prouve PAS que le gate" in comment


# --------------------------------------------------------------------------------------------- script


def _load_script():
    spec = importlib.util.spec_from_file_location("ci_w4_worker_routing_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def routing():
    return _load_script()


class _Secret:
    def __init__(self, value):
        self._value = value

    def get_secret_value(self):
        return self._value


class FakeLLM:
    """FAUX ``LLM`` : ce n'est PAS le SDK. Il ne sert qu'à vérifier que le script détecte les écarts."""

    model_fields = {
        "model": None,
        "api_key": None,
        "base_url": None,
        "service_id": None,
        "num_retries": None,
        "retry_min_wait": None,
        "retry_max_wait": None,
        "timeout": None,
        "max_output_tokens": None,
    }

    def __init__(self, **kwargs):
        unknown = set(kwargs) - set(self.model_fields)
        if unknown:
            raise TypeError(f"arguments inconnus {sorted(unknown)}")
        self.model = kwargs["model"]
        self.api_key = _Secret(kwargs.get("api_key"))
        self.base_url = kwargs.get("base_url")
        self.num_retries = kwargs.get("num_retries")
        self.max_output_tokens = kwargs.get("max_output_tokens")


class FakeConversation:
    def __init__(self, **_kwargs):
        pass


@pytest.fixture
def fake_sdk(monkeypatch):
    sdk = types.ModuleType("openhands.sdk")
    sdk.LLM = FakeLLM
    sdk.Conversation = FakeConversation
    preset = types.ModuleType("openhands.tools.preset.default")
    preset.get_default_agent = lambda **_kw: None
    for name, module in (
        ("openhands", types.ModuleType("openhands")),
        ("openhands.sdk", sdk),
        ("openhands.tools", types.ModuleType("openhands.tools")),
        ("openhands.tools.preset", types.ModuleType("openhands.tools.preset")),
        ("openhands.tools.preset.default", preset),
    ):
        monkeypatch.setitem(sys.modules, name, module)
    return sdk


RUNNER_TEMPLATE = """
import os
import sys

LLM_CONSTRUCTOR_KWARGS = ({kwargs})
BUG = {bug!r}


def resolve_credential(model, environ=None):
    environ = os.environ if environ is None else environ
    if environ.get("LLM_API_KEY"):
        return environ["LLM_API_KEY"], "LLM_API_KEY"
    if BUG == "global_key_for_any_provider" and environ.get("GEMINI_API_KEY"):
        return environ["GEMINI_API_KEY"], "GEMINI_API_KEY"
    if str(model).lower().startswith("gemini/") and environ.get("GEMINI_API_KEY"):
        return environ["GEMINI_API_KEY"], "GEMINI_API_KEY"
    return None, ""


def llm_kwargs(model, api_key, base_url, common):
    kwargs = dict(model=model, api_key=api_key)
    if base_url and BUG != "drops_base_url":
        kwargs["base_url"] = base_url
    kwargs.update(common)
    return kwargs


def main():
    from openhands.sdk import LLM, Conversation
    from openhands.tools.preset.default import get_default_agent

    primary = os.environ.get("LLM_MODEL", "gemini/gemma-4-31b-it")
    fallbacks = [m for m in os.environ.get("OH_FALLBACK_MODELS", "gemini/gemma-4-26b-a4b-it").split(",") if m]
    api_key, _ = resolve_credential(primary)
    base_url = os.environ.get("LLM_BASE_URL") or None
    local = bool(base_url) and primary.lower().startswith("openai/")
    if not api_key and not local:
        return 2
    if not api_key:
        api_key = "local"
    for model in [primary, *[m for m in fallbacks if m != primary]]:
        key = api_key
        if BUG == "fallback_loses_key" and model != primary:
            key = os.environ.get("GEMINI_API_KEY") or "x"
        common = dict(service_id="coder", num_retries=0, retry_min_wait=8, retry_max_wait=90, timeout=300)
        llm = LLM(**llm_kwargs(model, key, base_url, common))
        agent = get_default_agent(llm=llm, cli_mode=True)
        try:
            conversation = Conversation(agent=agent, workspace=".", max_iteration_per_run=1)
            conversation.send_message("x")
            conversation.run()
            return 0
        except Exception:
            continue
    return 1
"""

ALL_KWARGS = "'model','api_key','base_url','service_id','num_retries','retry_min_wait','retry_max_wait','timeout'"


def _write_runner(tmp_path: Path, *, kwargs: str = ALL_KWARGS, bug: str = "") -> str:
    path = tmp_path / "oh_runner.py"
    path.write_text(textwrap.dedent(RUNNER_TEMPLATE).format(kwargs=kwargs + ",", bug=bug), encoding="utf-8")
    return str(path)


def _quiet_environ(monkeypatch):
    for name in list(__import__("os").environ):
        if re.search(r"(?i)^LLM_|^OH_|GEMINI|OPENAI|API_KEY|TOKEN|SECRET", name):
            monkeypatch.delenv(name, raising=False)


def test_correct_runner_passes_every_scenario_against_the_fake_sdk(routing, fake_sdk, tmp_path, monkeypatch):
    _quiet_environ(monkeypatch)
    report = routing.run_checks(_write_runner(tmp_path), [])
    assert report["failures"] == [], report["failures"]
    names = [item["scenario"] for item in report["scenarios"]]
    assert {
        "gemini_par_defaut",
        "openai_endpoint_propre_sans_fuite_de_la_cle_gemini",
        "local_sans_cle_ni_cle_hote",
        "replis_meme_fournisseur_meme_endpoint_meme_cle",
    } <= set(names)
    local = next(item for item in report["scenarios"] if item["scenario"] == "local_sans_cle_ni_cle_hote")
    assert local["constructed"][0]["key"] == "local"


def test_a_constructor_kwarg_unknown_to_the_sdk_is_reported(routing, fake_sdk, tmp_path, monkeypatch):
    _quiet_environ(monkeypatch)
    path = _write_runner(tmp_path, kwargs=ALL_KWARGS + ",'reasoning_budget'")
    report = routing.run_checks(path, [])
    assert any("absents de LLM.model_fields" in m and "reasoning_budget" in m for m in report["failures"])


def test_a_runner_that_no_longer_passes_base_url_is_reported(routing, fake_sdk, tmp_path, monkeypatch):
    _quiet_environ(monkeypatch)
    kwargs = ALL_KWARGS.replace("'base_url',", "")
    report = routing.run_checks(_write_runner(tmp_path, kwargs=kwargs), [])
    assert any("base_url n'est pas transmis" in m for m in report["failures"])


def test_a_dropped_endpoint_is_reported_as_a_wrong_destination(routing, fake_sdk, tmp_path, monkeypatch):
    _quiet_environ(monkeypatch)
    report = routing.run_checks(_write_runner(tmp_path, bug="drops_base_url"), [])
    wrong = [m for m in report["failures"] if "endpoint" in m]
    assert wrong, report["failures"]
    assert any("openai_endpoint_propre" in m for m in wrong)


def test_a_key_sent_to_another_provider_is_reported_without_printing_it(
    routing, fake_sdk, tmp_path, monkeypatch, capsys
):
    _quiet_environ(monkeypatch)
    report = routing.run_checks(_write_runner(tmp_path, bug="global_key_for_any_provider"), [])
    leaks = [m for m in report["failures"] if "clé" in m]
    assert leaks, report["failures"]
    text = "\n".join(report["failures"]) + str(report["scenarios"])
    for secret in (routing.FAKE_GEMINI_KEY, routing.FAKE_ROLE_KEY):
        assert secret not in text, "une clé (même factice) ne doit jamais apparaître dans le rapport"


def test_a_fallback_that_loses_the_role_key_is_reported(routing, fake_sdk, tmp_path, monkeypatch):
    _quiet_environ(monkeypatch)
    report = routing.run_checks(_write_runner(tmp_path, bug="fallback_loses_key"), [])
    assert any("replis" in m and "clé effective" in m for m in report["failures"]), report["failures"]


def test_a_runner_without_the_w4_api_is_reported_not_skipped(routing, fake_sdk, tmp_path, monkeypatch):
    _quiet_environ(monkeypatch)
    old = tmp_path / "old_runner.py"
    old.write_text("def main():\n    return 0\n", encoding="utf-8")
    report = routing.run_checks(str(old), [])
    assert any("API du runner absente" in m for m in report["failures"])
    assert report["scenarios"] == []


def test_a_missing_sdk_is_a_failure(routing, monkeypatch, tmp_path):
    monkeypatch.setitem(sys.modules, "openhands.sdk", None)  # force ImportError
    report = routing.run_checks(_write_runner(tmp_path), [])
    assert any("SDK OpenHands non importable" in m for m in report["failures"])


def test_main_exit_code_and_report_do_not_leak_environment_secrets(routing, fake_sdk, tmp_path, monkeypatch, capsys):
    _quiet_environ(monkeypatch)
    monkeypatch.setenv("LLM_API_KEY", "host-secret-must-not-appear")
    rc = routing.main(["--runner", _write_runner(tmp_path), "--require-modules", ""])
    captured = capsys.readouterr()
    assert rc == 0
    assert "host-secret-must-not-appear" not in captured.out + captured.err
    assert "LLM_API_KEY" in captured.out  # le NOM de la variable présente au départ est signalé, jamais sa valeur


def test_the_process_environment_is_restored_after_each_scenario(routing, fake_sdk, tmp_path, monkeypatch):
    _quiet_environ(monkeypatch)
    monkeypatch.setenv("LLM_MODEL", "sentinel-model")
    before = dict(__import__("os").environ)
    argv = list(sys.argv)
    routing.run_checks(_write_runner(tmp_path), [])
    assert dict(__import__("os").environ) == before
    assert sys.argv == argv
    assert fake_sdk.LLM is FakeLLM and fake_sdk.Conversation is FakeConversation


# ------------------------------------------------------------------------- modules d'oracle (pypdf réel)


def test_the_minimal_pdf_is_read_by_the_real_pypdf(routing):
    import pypdf  # extra dev (locks/dev.txt) : son absence est un échec, pas un skip

    reader = pypdf.PdfReader(io.BytesIO(routing.minimal_pdf("Audit 42 - conformite OK")))
    assert "Audit 42" in reader.pages[0].extract_text()


def test_module_check_reports_a_missing_oracle_module_and_reads_a_pdf(routing):
    import pypdf  # noqa: F401 - extra dev : absence = échec

    failures = routing.Failures()
    report = routing.check_modules(["pypdf", "module_absent_w4_xyz"], failures)
    assert report["pypdf"] is True and "Audit 42" in report["pypdf_extrait"]
    assert report["module_absent_w4_xyz"] is False
    assert any("module_absent_w4_xyz" in m for m in failures.items)
    assert not any("pypdf" in m for m in failures.items)


def test_script_is_self_contained_and_does_not_import_the_collegue_package():
    source = SCRIPT.read_text(encoding="utf-8")
    assert not re.search(r"^\s*(from|import)\s+collegue\b", source, re.MULTILINE)
    assert "/opt/oh_runner.py" in source
    assert "requests" not in source and "urllib" not in source and "socket" not in source
