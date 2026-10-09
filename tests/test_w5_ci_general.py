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
DOCKERFILE = ROOT / "docker" / "sandbox" / "Dockerfile.openhands"
SANDBOX_LOCK = ROOT / "locks" / "sandbox-openhands.txt"
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
    assert "--network none" in run and "collegue-sandbox-openhands:pr-check" in run
    assert "python - < scripts/ci_w5_broker_transport.py" in run, "script par l'entrée standard : aucun montage"
    for forbidden in (" -v ", "--volume", "--mount", " -e ", "--env", "--privileged", "docker.sock", "--network host"):
        assert forbidden not in run, forbidden
    assert "secrets." not in str(proof) and "continue-on-error" not in proof
    names = [item.get("name", "") for item in job["steps"]]
    assert names.index(step(job, "Build OpenHands sandbox image")["name"]) < names.index(proof["name"])
    assert names.index(step(job, "Verify OpenHands worker routing")["name"]) < names.index(proof["name"])


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


def test_the_dockerfile_installs_the_hash_locked_stack_in_one_step_and_holds_no_provider_key():
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert "uv pip install --system --require-hashes --no-deps -r /opt/locks/sandbox-openhands.txt" in text
    assert not re.search(r"API_KEY|GOOGLE|GEMINI", text), "aucune clé fournisseur dans l'image"
    assert "ENV RUNTIME=process" in text


def _locked() -> dict:
    pins = {}
    for line in SANDBOX_LOCK.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^([A-Za-z0-9_.\-]+)==([^\s;\\]+)", line)
        if match:
            pins[match.group(1).lower().replace("_", "-")] = match.group(2)
    return pins


def test_the_offline_business_stack_is_hash_locked_in_the_image_lock():
    pins = _locked()
    stack = (
        "fastapi",
        "uvicorn",
        "httpx",
        "sqlalchemy",
        "alembic",
        "pypdf",
        "reportlab",
        "pytest",
        "pytest-asyncio",
        "pip-audit",
    )
    missing = [name for name in stack if name not in pins]
    assert not missing, f"absents du verrou de l'image (pile hors ligne du gate) : {missing}"
    text = SANDBOX_LOCK.read_text(encoding="utf-8")
    assert text.count("--hash=sha256:") >= len(pins), "chaque paquet verrouillé porte des empreintes"
    assert pins["pytest"].startswith("8."), "pytest < 9 : pytest-asyncio 0.23.6 casse avec 9.x"


@pytest.mark.xfail(
    strict=True,
    reason=(
        "BESOIN ouvert (décision manager, docs/consolidation/w5-integration.md § 8) : 3 des 4 versions épinglées par la graine immuable de la "
        "fixture (fastapi 0.116.1, uvicorn 0.35.0, pytest 8.4.1) ne sont pas celles du verrou de l'image (0.141.1, 0.54.0, 8.4.2) : un "
        "`pip install -r requirements.txt` du gate sans réseau échouerait. Ce marqueur casse (donc se retire) dès que le verrou les épingle."
    ),
)
def test_the_image_lock_satisfies_the_fixture_seed_pins_so_the_gate_needs_no_network():
    import sys

    sys.path.insert(0, str(ROOT / "tests"))
    from w4_business_fixture import SEED

    wanted = {}
    for line in SEED["requirements.txt"].splitlines():
        match = re.match(r"^([A-Za-z0-9_.\-]+)==(\S+)$", line.strip())
        if match:
            wanted[match.group(1).lower().replace("_", "-")] = match.group(2)
    assert wanted, "la graine épingle des versions"
    pins = _locked()
    assert {name: pins.get(name) for name in wanted} == wanted
