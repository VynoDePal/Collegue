"""CI générale de la vague 5 : PostgreSQL obligatoire, preuve du transport dans l'image sans réseau, pile hors ligne verrouillée.

Contrôles STATIQUES des fichiers de CI et du verrou (rien n'est exécuté, aucun modèle, aucun Docker). Ils gardent les garanties que
la CI distante doit tenir : une preuve PostgreSQL ne peut être ni sautée ni abaissée, le conteneur du transport n'a ni réseau ni secret
ni montage, la pile du gate (FastAPI, SQLAlchemy, Alembic, PDF) est dans le verrou haché de l'image, et les cinq checks requis
gardent leur nom.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
TESTS_YML = ROOT / ".github" / "workflows" / "tests.yml"
DOCKERFILE = ROOT / "docker" / "sandbox" / "Dockerfile.broker"
LEGACY_DOCKERFILE = ROOT / "docker" / "sandbox" / "Dockerfile.openhands"
SANDBOX_LOCK = ROOT / "locks" / "sandbox-broker.txt"
RELAY_SOURCE = ROOT / "collegue" / "executor" / "oh_broker_relay.py"
A_NOT_INTEGRATED = pytest.mark.xfail(
    not RELAY_SOURCE.exists(),
    strict=True,
    reason="le relais de A n'est pas encore intégré : ce marqueur casse (donc se retire) à l'intégration de A",
)


@pytest.fixture(scope="module")
def workflow() -> dict:
    return yaml.safe_load(TESTS_YML.read_text(encoding="utf-8"))


def step(job: dict, prefix: str) -> dict:
    for item in job["steps"]:
        if item.get("name", "").startswith(prefix):
            return item
    raise AssertionError(f"étape introuvable : {prefix}")


def test_the_five_required_check_names_are_unchanged(workflow):
    jobs = workflow["jobs"]
    assert {key: job["name"] for key, job in jobs.items()} == {
        "lint": "Ruff",
        "pytest": "Pytest (Python ${{ matrix.python-version }})",
        "dependency-audit": "Dependency audit",
        "docker-build": "Docker build",
    }
    assert jobs["pytest"]["strategy"]["matrix"]["python-version"] == ["3.11", "3.12"]


def test_the_broker_postgresql_proof_is_mandatory_never_skipped_and_has_a_floor(workflow):
    job = workflow["jobs"]["pytest"]
    broker = step(job, "PostgreSQL broker state")
    run = broker["run"]
    assert "continue-on-error" not in broker and "|| true" not in run and "if" not in broker, "étape inconditionnelle"
    assert 'test -n "${COLLEGUE_TEST_POSTGRES_URL:-}"' in run, "service PostgreSQL absent = job rouge"
    assert "test -f tests/test_w5_broker_postgres.py" in run, "fichier absent = job rouge (jamais un pas sauté)"
    assert "--collect-only" in run and "--junitxml=pg-broker.xml" in run and "-rA" in run
    floor = re.search(r"ci_require_junit\.py pg-broker\.xml --min-tests (\d+) --expect-tests \"\$\{expected\}\"", run)
    assert floor and int(floor.group(1)) >= 2, "plancher présent ; fixé exactement à l'intégration de A"
    assert "-o addopts=" in run, "mêmes options pour la collecte et l'exécution"
    names = [item.get("name", "") for item in job["steps"]]
    assert names.index(broker["name"]) < names.index(step(job, "Run pytest with coverage")["name"])
    upload = step(job, "Upload PostgreSQL proof report")
    assert upload["if"] == "always()" and "pg-broker.xml" in upload["with"]["path"]
    assert job["services"]["postgres"]["image"].startswith("postgres:")


def test_every_postgresql_proof_step_goes_through_the_junit_gate(workflow):
    job = workflow["jobs"]["pytest"]
    proofs = [item for item in job["steps"] if item.get("name", "").startswith("PostgreSQL")]
    assert len(proofs) == 4
    for item in proofs:
        assert "ci_require_junit.py" in item["run"] and "--expect-tests" in item["run"] and "--min-tests" in item["run"]
        assert "test -n" in item["run"] and "set -euo pipefail" in item["run"]


def test_the_transport_proof_runs_in_the_built_image_without_network_secret_or_mount(workflow):
    job = workflow["jobs"]["docker-build"]
    proof = step(job, "Verify OpenHands broker transport")
    run = proof["run"]
    assert "--network none" in run and "collegue-sandbox-broker:pr-check" in run and "openhands:pr-check" not in run
    assert "python - < scripts/ci_w5_broker_transport.py" in run, "script par l'entrée standard : aucun montage"
    for forbidden in (" -v ", "--volume", "--mount", " -e ", "--env", "--privileged", "docker.sock", "--network host"):
        assert forbidden not in run, forbidden
    assert "secrets." not in str(proof) and "continue-on-error" not in proof
    names = [item.get("name", "") for item in job["steps"]]
    assert names.index(step(job, "Build OpenHands broker sandbox image")["name"]) < names.index(proof["name"])
    assert names.index(step(job, "Verify OpenHands worker routing in the broker image")["name"]) < names.index(
        proof["name"]
    )


def test_the_general_ci_never_receives_a_model_key_nor_a_real_provider(workflow):
    text = TESTS_YML.read_text(encoding="utf-8")
    assert "secrets." not in text.replace("secrets.GITHUB_TOKEN", "")
    assert not re.search(r"GOOGLE_API_KEY|GEMINI_API_KEY|LLM_API_KEY|OPENAI_API_KEY|ANTHROPIC_API_KEY", text)
    assert "generativelanguage.googleapis.com" not in text


@A_NOT_INTEGRATED
def test_the_image_embeds_the_relay_next_to_the_runner_before_dropping_privileges():
    text = DOCKERFILE.read_text(encoding="utf-8")
    copy = "COPY collegue/executor/oh_broker_relay.py /opt/oh_broker_relay.py"
    assert text.count(copy) == 1
    assert text.index(copy) < text.index("USER sandbox")
    assert text.index(copy) > text.index("COPY collegue/executor/oh_runner.py /opt/oh_runner.py")
    assert RELAY_SOURCE.is_file(), "la source copiée doit exister dans le dépôt"


def test_the_broker_dockerfile_installs_the_audited_lock_in_one_step_without_the_legacy_web_app_or_a_key():
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert "uv pip install --system --require-hashes --no-deps -r /opt/locks/sandbox-broker.txt" in text
    code = [ln for ln in text.splitlines() if not ln.lstrip().startswith("#")]
    assert sum("pip install" in ln for ln in code) == 1, "une seule installation, aucune résolution flottante"
    assert not re.search(r"API_KEY|GOOGLE|GEMINI", text), "aucune clé fournisseur dans l'image"
    assert re.search(r"^FROM python:3\.12-slim@sha256:[0-9a-f]{64}$", text, re.M), "base épinglée par condensat"
    assert "COPY --from=ghcr.io/astral-sh/uv:0.11.33 /uv" in text
    assert "--mode sdk-only" in text and text.index("sandbox-broker.txt") < text.index("--mode sdk-only")
    assert not re.search(
        r"openhands-ai|nodejs|playwright|chromium", "\n".join(code)
    )  # (les commentaires expliquent l'absence)
    for source in ("oh_runner.py", "oh_sampler.py"):
        assert f"COPY collegue/executor/{source} /opt/{source}" in text
    assert "ENV RUNTIME=process" in text and text.index("USER sandbox") > text.index("oh_broker_relay.py")


def test_the_legacy_direct_image_is_kept_unchanged_in_its_install_and_patch_contract():
    text = LEGACY_DOCKERFILE.read_text(encoding="utf-8")
    assert "locks/sandbox-openhands.txt" in text and "python /opt/patch_openhands_gemma4_terminal.py" in text
    assert "--mode" not in text, (
        "le mode par défaut (legacy) exige openhands-ai 1.7.0 : comportement historique inchangé"
    )


def _locked() -> dict:
    pins = {}
    for line in SANDBOX_LOCK.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^([A-Za-z0-9_.\-]+)==([^\s;\\]+)", line)
        if match:
            pins[match.group(1).lower().replace("_", "-")] = match.group(2)
    return pins


def test_the_offline_business_stack_is_hash_locked_in_the_broker_image_lock_and_the_legacy_web_app_is_absent():
    pins = _locked()
    stack = (
        "fastapi",
        "uvicorn",
        "httpx",
        "sqlalchemy",
        "alembic",
        "pypdf",
        "reportlab",
        "python-multipart",
        "pytest",
        "pytest-asyncio",
        "pip-audit",
    )
    missing = [name for name in stack if name not in pins]
    assert not missing, f"absents du verrou de l'image (pile hors ligne du gate) : {missing}"
    assert not {"openhands-ai", "python-jose", "ecdsa", "passlib", "bcrypt"} & set(pins), (
        "application web legacy et chaîne jose absentes"
    )
    assert {"openhands-sdk", "openhands-tools"} <= set(pins) and pins["openhands-sdk"] == pins[
        "openhands-tools"
    ] == "1.19.1"
    text = SANDBOX_LOCK.read_text(encoding="utf-8")
    assert text.count("--hash=sha256:") >= len(pins), "chaque paquet verrouillé porte des empreintes"
    assert int(pins["pytest"].split(".")[0]) >= 9 and int(pins["pytest-asyncio"].split(".")[0]) >= 1, (
        "outils de test corrigés (avis pytest < 9.0.3)"
    )
    assert pins["lmnr"] == "0.7.52", "épinglage hérité de l'image legacy (observe(rollout_entrypoint=…))"


def test_the_socle_requirements_are_satisfied_by_the_broker_image_lock_so_the_gate_needs_no_network():
    """Remplace l'ancien xfail : les versions de la pile approuvée du socle SONT celles du verrou de l'image (décision du manager)."""
    import tomllib

    group = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["dependency-groups"]["fixture-stack"]
    pins = _locked()
    wanted = dict(item.split("==") for item in group)
    assert wanted and {name.lower().replace("_", "-"): pins.get(name.lower().replace("_", "-")) for name in wanted} == {
        name.lower().replace("_", "-"): version for name, version in wanted.items()
    }


def test_the_runner_and_sampler_only_import_what_the_sdk_only_image_provides():
    import ast

    allowed_prefixes = ("openhands.sdk", "openhands.tools.preset.default")
    for name in ("oh_runner.py", "oh_sampler.py", "oh_broker_relay.py"):
        path = ROOT / "collegue" / "executor" / name
        if not path.exists():
            continue  # le relais n'existe qu'après l'intégration de A
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
        openhands = {m for m in imported if m == "openhands" or m.startswith("openhands.")}
        assert all(m.startswith(allowed_prefixes) for m in openhands), (name, sorted(openhands))
        assert not {m.split(".")[0] for m in imported} & {"collegue", "jose", "ecdsa", "passlib"}, name


def test_the_dependency_audit_job_audits_the_broker_and_fixture_locks_strictly_and_not_the_legacy_one(workflow):
    job = workflow["jobs"]["dependency-audit"]
    commands = [item.get("run", "") for item in job["steps"]]
    for lock in ("sandbox-broker", "fixture-stack"):
        assert any(
            f"pip-audit --strict --desc --no-deps --disable-pip -r locks/{lock}.txt" in run for run in commands
        ), lock
    assert not any("locks/sandbox-openhands.txt" in run for run in commands), (
        "le verrou legacy est rouge : documenté, pas ignoré"
    )


def test_the_docker_build_job_proves_the_broker_image_end_to_end_without_a_model(workflow):
    job = workflow["jobs"]["docker-build"]
    names = [item.get("name", "") for item in job["steps"]]
    build = step(job, "Build OpenHands broker sandbox image")
    assert (
        build["with"]["file"] == "docker/sandbox/Dockerfile.broker"
        and build["with"]["tags"] == "collegue-sandbox-broker:pr-check"
    )
    gemma = step(job, "Verify the broker image Gemma 4 terminal contract")["run"]
    assert "--network none" in gemma and "collegue-sandbox-broker:pr-check" in gemma and "TerminalAction" in gemma
    routing = step(job, "Verify OpenHands worker routing in the broker image")["run"]
    assert (
        "--network none" in routing
        and "collegue-sandbox-broker:pr-check" in routing
        and "ci_w4_worker_routing.py" in routing
    )
    verifier = step(job, "Prove the business verifier in the broker image")["run"]
    assert "--image collegue-sandbox-broker:pr-check" in verifier
    assert names.index(build["name"]) < names.index(
        step(job, "Verify the broker image Gemma 4 terminal contract")["name"]
    )
    assert "docker/sandbox/Dockerfile.openhands" in str(step(job, "Build OpenHands sandbox image")["with"]), (
        "image directe conservée"
    )


def test_the_patch_script_has_two_explicit_modes_and_refuses_the_legacy_app_in_sdk_only_mode(monkeypatch):
    import importlib.metadata as metadata
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "patch_under_test", ROOT / "scripts" / "patch_openhands_gemma4_terminal.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.MODES == ("legacy", "sdk-only")
    versions = {"openhands-sdk": "1.19.1", "openhands-tools": "1.19.1"}

    class Dist:
        def __init__(self, version):
            self.version = version

        def locate_file(self, path):
            return Path("/nonexistent") / path

    def fake(name):
        if name not in versions:
            raise metadata.PackageNotFoundError(name)
        return Dist(versions[name])

    monkeypatch.setattr(metadata, "distribution", fake)
    monkeypatch.setattr(metadata, "version", lambda name: fake(name).version)
    with pytest.raises(module.PatchError, match="missing"):
        module.locate_target("legacy")  # openhands-ai absent : refusé en mode legacy
    with pytest.raises(module.PatchError, match="is missing"):
        module.locate_target("sdk-only")  # ...mais le fichier n'existe pas : on a passé les gardes de versions
    versions["openhands-ai"] = "1.7.0"
    with pytest.raises(module.PatchError, match="sdk-only mode refuses an image carrying openhands-ai"):
        module.locate_target("sdk-only")
    versions["openhands-tools"] = "1.19.2"
    with pytest.raises(module.PatchError, match="unsupported openhands-tools version"):
        module.locate_target("legacy")
    with pytest.raises(module.PatchError, match="unknown mode"):
        module.locate_target("autre")
