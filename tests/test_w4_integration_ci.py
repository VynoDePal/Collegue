"""Câblage CI de la vague 4 (propriété C) : audit des dépendances de test et contrôle du routage du worker.

Ces tests sont STRUCTURELS et de détection d'écart au niveau du script :

* le workflow ``tests.yml`` garde les cinq jobs requis, audite explicitement ``locks/dev.txt`` en mode strict sans rien
  ignorer, et lance le contrôle d'image dans un conteneur sans réseau, sans secret hôte, script sur l'entrée standard ;
* ``scripts/ci_w4_worker_routing.py`` détecte, sur un FAUX ``openhands.sdk`` et de faux runner/échantillonneur, un argument de
  constructeur inconnu, une mauvaise destination (endpoint ou clé), une identité ``usage_id`` perdue (primaire, replis,
  abonnement, échantillonneur ; l'ancien ``service_id`` est rejeté par COMPORTEMENT, pas seulement par introspection de noms)
  et un ``max_output_tokens`` transmis au login d'abonnement (doublon).

Ils ne prouvent PAS le comportement du vrai SDK 1.19.1 : ce contrôle ne s'exécute que dans l'image OpenHands, dans le
job « Docker build », après intégration (voir ``reports/w4-c-ci-preparation.md``). Un faux SDK vert ne vaut jamais preuve.
"""

from __future__ import annotations

import importlib.util
import io
import itertools
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
    comment = "\n".join(lines[max(0, at - 12) : at])
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
    """FAUX ``LLM`` : ce n'est PAS le SDK. Il reproduit seulement ses deux traits utiles à ces tests : ``extra="ignore"`` (un
    argument inconnu est ignoré SANS erreur) et ``usage_id`` (défaut ``"default"``). Il ne prouve rien du vrai SDK 1.19.1."""

    model_fields = {
        name: None
        for name in (
            "model",
            "api_key",
            "base_url",
            "usage_id",
            "num_retries",
            "retry_min_wait",
            "retry_max_wait",
            "timeout",
            "max_output_tokens",
        )
    }

    def __init__(self, model, api_key=None, base_url=None, usage_id="default", max_output_tokens=None, **extra):
        self.model = model
        self.api_key = _Secret(api_key)
        self.base_url = base_url
        self.usage_id = usage_id
        self.num_retries = extra.get("num_retries")
        self.max_output_tokens = max_output_tokens
        self.is_subscription = False  # les arguments inconnus de ``extra`` sont ignorés, comme extra="ignore"


class FakeConversation:
    def __init__(self, **_kwargs):
        pass


class FakeOAuthCredentials:
    def __init__(self, vendor, access_token, refresh_token, expires_at):
        self.vendor, self.access_token, self.refresh_token, self.expires_at = (
            vendor,
            access_token,
            refresh_token,
            expires_at,
        )


class FakeCredentialStore:
    def __init__(self, credentials_dir=None):
        self.credentials_dir = credentials_dir


class FakeSubscriptionAuth:
    """Imite la SEULE sémantique de ``create_llm`` utile ici : ``max_output_tokens=None`` fixé PUIS ``**llm_kwargs`` (donc un
    ``max_output_tokens`` en plus lève ``TypeError`` par la mécanique de Python), modèle préfixé ``openai/``."""

    def __init__(self, credential_store=None):
        self.credential_store = credential_store

    def create_llm(self, model, credentials, instructions=None, **llm_kwargs):
        llm = FakeLLM(
            model=f"openai/{model}",
            api_key=credentials.access_token,
            base_url="https://backend.example.invalid/codex",
            max_output_tokens=None,
            **llm_kwargs,
        )
        llm.is_subscription = True
        return llm


@pytest.fixture
def fake_sdk(monkeypatch):
    sdk = types.ModuleType("openhands.sdk")
    sdk.LLM = FakeLLM
    sdk.Conversation = FakeConversation
    preset = types.ModuleType("openhands.tools.preset.default")
    preset.get_default_agent = lambda **_kw: None
    auth_openai = types.ModuleType("openhands.sdk.llm.auth.openai")
    auth_openai.OAuthCredentials = FakeOAuthCredentials
    auth_openai.OpenAISubscriptionAuth = FakeSubscriptionAuth
    auth_openai.OPENAI_CODEX_MODELS = frozenset({"gpt-5.2-codex"})
    auth_openai._extract_chatgpt_account_id = lambda token: (
        "reseau-interdit"
    )  # remplacé par le script (aucun accès réseau)
    auth_credentials = types.ModuleType("openhands.sdk.llm.auth.credentials")
    auth_credentials.CredentialStore = FakeCredentialStore
    for name, module in (
        ("openhands", types.ModuleType("openhands")),
        ("openhands.sdk", sdk),
        ("openhands.sdk.llm", types.ModuleType("openhands.sdk.llm")),
        ("openhands.sdk.llm.auth", types.ModuleType("openhands.sdk.llm.auth")),
        ("openhands.sdk.llm.auth.openai", auth_openai),
        ("openhands.sdk.llm.auth.credentials", auth_credentials),
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
LLM_SUBSCRIPTION_KWARGS = ({subscription_kwargs})
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
    subscription = os.environ.get("LLM_SUBSCRIPTION", "") == "1"
    api_key, _ = resolve_credential(primary)
    base_url = os.environ.get("LLM_BASE_URL") or None
    local = bool(base_url) and primary.lower().startswith("openai/")
    if not subscription and not api_key and not local:
        return 2
    if not subscription and not api_key:
        api_key = "local"
    for model in [primary, *[m for m in fallbacks if m != primary]]:
        key = api_key
        if BUG == "fallback_loses_key" and model != primary:
            key = os.environ.get("GEMINI_API_KEY") or "x"
        identity = {{"service_id": "coder"}} if BUG == "legacy_service_id" else {{"usage_id": "coder"}}
        common = dict(identity, num_retries=0, retry_min_wait=8, retry_max_wait=90, timeout=300)
        if BUG == "fallback_loses_identity" and model != primary:
            common.pop("usage_id", None)
        try:
            if subscription:
                if BUG == "subscription_passes_max_output":
                    common["max_output_tokens"] = 4096
                    login_kwargs = dict(common)
                else:
                    login_kwargs = {{k: v for k, v in common.items() if k in LLM_SUBSCRIPTION_KWARGS}}
                llm = LLM.subscription_login(vendor="openai", model=model, open_browser=False, **login_kwargs)
            else:
                llm = LLM(**llm_kwargs(model, key, base_url, common))
            agent = get_default_agent(llm=llm, cli_mode=True)
            conversation = Conversation(agent=agent, workspace=".", max_iteration_per_run=1)
            conversation.send_message("x")
            conversation.run()
            return 0
        except Exception:
            continue
    return 1
"""

SAMPLER_TEMPLATE = """
import json
import os
import sys


def main():
    from openhands.sdk import LLM

    data = json.load(sys.stdin)
    extra = {{"max_output_tokens": int(data.get("max_output_tokens") or 0)}} if data.get("strict") and {pass_max} else {{}}
    identity = {{"service_id": "sampler"}} if {legacy} else {{"usage_id": "sampler"}}
    LLM.subscription_login(
        vendor="openai", model=os.environ.get("LLM_MODEL", "gpt-5.4"), open_browser=False,
        num_retries=0, retry_min_wait=5, retry_max_wait=60, timeout=180, **identity, **extra,
    )
    return 0
"""

_COUNTER = itertools.count()
ALL_KWARGS = "'model','api_key','base_url','usage_id','num_retries','retry_min_wait','retry_max_wait','timeout'"
SUBSCRIPTION_KWARGS = "'usage_id','num_retries','retry_min_wait','retry_max_wait','timeout'"


def _write_runner(
    tmp_path: Path, *, kwargs: str = ALL_KWARGS, subscription_kwargs: str = SUBSCRIPTION_KWARGS, bug: str = ""
) -> str:
    path = (
        tmp_path / f"oh_runner_{next(_COUNTER)}.py"
    )  # nom unique : jamais de bytecode périmé (même taille, même seconde)
    path.write_text(
        textwrap.dedent(RUNNER_TEMPLATE).format(
            kwargs=kwargs + ",", subscription_kwargs=subscription_kwargs + ",", bug=bug
        ),
        encoding="utf-8",
    )
    return str(path)


def _write_sampler(tmp_path: Path, *, legacy: bool = False, pass_max: bool = False) -> str:
    path = tmp_path / f"oh_sampler_{next(_COUNTER)}.py"
    path.write_text(textwrap.dedent(SAMPLER_TEMPLATE).format(legacy=legacy, pass_max=pass_max), encoding="utf-8")
    return str(path)


def _check(routing, tmp_path, *, runner=None, sampler=None, modules=()):
    return routing.run_checks(runner or _write_runner(tmp_path), list(modules), sampler or _write_sampler(tmp_path))


def _quiet_environ(monkeypatch):
    for name in list(__import__("os").environ):
        if re.search(r"(?i)^LLM_|^OH_|GEMINI|OPENAI|API_KEY|TOKEN|SECRET", name):
            monkeypatch.delenv(name, raising=False)


def test_correct_runner_passes_every_scenario_against_the_fake_sdk(routing, fake_sdk, tmp_path, monkeypatch):
    _quiet_environ(monkeypatch)
    report = _check(routing, tmp_path)
    assert report["failures"] == [], report["failures"]
    names = [item["scenario"] for item in report["scenarios"]]
    assert {
        "gemini_par_defaut",
        "openai_endpoint_propre_sans_fuite_de_la_cle_gemini",
        "local_sans_cle_ni_cle_hote",
        "replis_meme_fournisseur_meme_endpoint_meme_cle",
        "abonnement_identite_coder_et_replis",
    } <= set(names)
    local = next(item for item in report["scenarios"] if item["scenario"] == "local_sans_cle_ni_cle_hote")
    assert local["constructed"][0]["key"] == "local"


def test_every_real_llm_built_carries_the_canonical_coder_identity_primary_and_fallbacks(
    routing, fake_sdk, tmp_path, monkeypatch
):
    _quiet_environ(monkeypatch)
    report = _check(routing, tmp_path)
    built = [llm for scenario in report["scenarios"] for llm in scenario["constructed"]]
    assert len(built) >= 9 and {llm["usage_id"] for llm in built} == {"coder"}
    fallback = next(s for s in report["scenarios"] if s["scenario"] == "replis_meme_fournisseur_meme_endpoint_meme_cle")
    assert [llm["usage_id"] for llm in fallback["constructed"]] == ["coder", "coder"]
    assert report["allocation"]["effective"]["max_output_tokens"] == 4096


def test_the_legacy_service_id_runner_is_rejected_by_behaviour_not_only_by_field_names(
    routing, fake_sdk, tmp_path, monkeypatch
):
    """L'ancien runner passe ``service_id`` ; ici il DÉCLARE pourtant ``usage_id`` (l'introspection de noms est trompée) : seule
    l'assertion dynamique ``llm.usage_id == "coder"`` sur le LLM construit le rejette (le SDK ignore l'argument inconnu)."""
    _quiet_environ(monkeypatch)
    report = _check(routing, tmp_path, runner=_write_runner(tmp_path, bug="legacy_service_id"))
    failures = report["failures"]
    assert not any("absents de LLM.model_fields" in m for m in failures), "la garde de noms est bien trompée ici"
    identity = [m for m in failures if "usage_id 'default' != 'coder'" in m]
    assert len(identity) >= 9, failures  # primaire et replis, clé API et abonnement
    assert any("[abonnement_identite_coder_et_replis#0]" in m for m in identity)


def test_declaring_the_old_service_id_is_still_caught_by_the_field_guard(routing, fake_sdk, tmp_path, monkeypatch):
    _quiet_environ(monkeypatch)
    legacy = ALL_KWARGS.replace("'usage_id'", "'service_id'")
    report = _check(routing, tmp_path, runner=_write_runner(tmp_path, kwargs=legacy))
    assert any("absents de LLM.model_fields" in m and "service_id" in m for m in report["failures"])


def test_a_fallback_that_loses_the_coder_identity_is_reported(routing, fake_sdk, tmp_path, monkeypatch):
    _quiet_environ(monkeypatch)
    report = _check(routing, tmp_path, runner=_write_runner(tmp_path, bug="fallback_loses_identity"))
    assert any("replis" in m and "usage_id 'default'" in m for m in report["failures"]), report["failures"]


def test_a_constructor_kwarg_unknown_to_the_sdk_is_reported(routing, fake_sdk, tmp_path, monkeypatch):
    _quiet_environ(monkeypatch)
    path = _write_runner(tmp_path, kwargs=ALL_KWARGS + ",'reasoning_budget'")
    report = _check(routing, tmp_path, runner=path)
    assert any("absents de LLM.model_fields" in m and "reasoning_budget" in m for m in report["failures"])


def test_a_runner_that_no_longer_passes_base_url_is_reported(routing, fake_sdk, tmp_path, monkeypatch):
    _quiet_environ(monkeypatch)
    kwargs = ALL_KWARGS.replace("'base_url',", "")
    report = _check(routing, tmp_path, runner=_write_runner(tmp_path, kwargs=kwargs))
    assert any("base_url n'est pas transmis" in m for m in report["failures"])


def test_a_dropped_endpoint_is_reported_as_a_wrong_destination(routing, fake_sdk, tmp_path, monkeypatch):
    _quiet_environ(monkeypatch)
    report = _check(routing, tmp_path, runner=_write_runner(tmp_path, bug="drops_base_url"))
    wrong = [m for m in report["failures"] if "endpoint" in m]
    assert wrong, report["failures"]
    assert any("openai_endpoint_propre" in m for m in wrong)


def test_a_key_sent_to_another_provider_is_reported_without_printing_it(routing, fake_sdk, tmp_path, monkeypatch):
    _quiet_environ(monkeypatch)
    report = _check(routing, tmp_path, runner=_write_runner(tmp_path, bug="global_key_for_any_provider"))
    leaks = [m for m in report["failures"] if "clé" in m]
    assert leaks, report["failures"]
    text = "\n".join(report["failures"]) + str(report["scenarios"])
    for secret in (routing.FAKE_GEMINI_KEY, routing.FAKE_ROLE_KEY, routing.FAKE_ACCESS_TOKEN):
        assert secret not in text, "une clé (même factice) ne doit jamais apparaître dans le rapport"


def test_a_fallback_that_loses_the_role_key_is_reported(routing, fake_sdk, tmp_path, monkeypatch):
    _quiet_environ(monkeypatch)
    report = _check(routing, tmp_path, runner=_write_runner(tmp_path, bug="fallback_loses_key"))
    assert any("replis" in m and "clé effective" in m for m in report["failures"]), report["failures"]


# ---- abonnement : identité, absence de borne locale, doublon (doublures : portée limitée, aucun login, aucun réseau) ----


def test_the_subscription_contract_keeps_the_identity_and_never_carries_a_local_output_bound(
    routing, fake_sdk, tmp_path, monkeypatch
):
    _quiet_environ(monkeypatch)
    report = _check(routing, tmp_path)
    sub = report["subscription"]
    assert report["failures"] == [], report["failures"]
    assert sub["effective"] == {"usage_id": "coder", "max_output_tokens": None}
    assert sub["duplicate_max_output_tokens"] == "TypeError" and sub["legacy_service_id_usage_id"] == "default"
    assert sub["jwks_helper_replaced"] is True and sub["login"] is False and sub["network"] is False
    assert {item["payload"]: item["usage_id"] for item in sub["sampler"]} == {
        "non_strict": "sampler",
        "strict_borne_hote": "sampler",
    }
    assert all(item["max_output_tokens"] is None for item in sub["sampler"])


def test_a_subscription_llm_that_carries_a_local_output_bound_is_reported(routing, fake_sdk, tmp_path, monkeypatch):
    """Un SDK (ou un runner) qui laisserait une borne locale en abonnement serait une FAUSSE borne : refusé."""
    _quiet_environ(monkeypatch)
    original = FakeSubscriptionAuth.create_llm

    def with_bound(self, model, credentials, instructions=None, **kwargs):
        llm = original(self, model, credentials, instructions, **kwargs)
        llm.max_output_tokens = 4096
        return llm

    monkeypatch.setattr(FakeSubscriptionAuth, "create_llm", with_bound)
    report = _check(routing, tmp_path)
    assert any("[abonnement_identite_coder_et_replis#0] max_output_tokens 4096" in m for m in report["failures"])
    assert any("[sampler:strict_borne_hote] max_output_tokens 4096" in m for m in report["failures"])
    assert any("max_output_tokens 4096 : doit rester None" in m for m in report["failures"])


def test_a_sdk_change_on_service_id_or_on_the_duplicate_is_flagged_for_review(routing, fake_sdk, tmp_path, monkeypatch):
    _quiet_environ(monkeypatch)
    original = FakeSubscriptionAuth.create_llm

    def honours_service_id(self, model, credentials, instructions=None, **kwargs):
        if "service_id" in kwargs:
            kwargs["usage_id"] = kwargs.pop("service_id")
        return original(self, model, credentials, instructions, **kwargs)

    monkeypatch.setattr(FakeSubscriptionAuth, "create_llm", honours_service_id)
    assert any("service_id est désormais honoré" in m for m in _check(routing, tmp_path)["failures"])

    def tolerates_duplicate(self, model, credentials, instructions=None, **kwargs):
        kwargs.pop("max_output_tokens", None)
        return original(self, model, credentials, instructions, **kwargs)

    monkeypatch.setattr(FakeSubscriptionAuth, "create_llm", tolerates_duplicate)
    assert any("n'a pas levé TypeError" in m for m in _check(routing, tmp_path)["failures"])


def test_a_runner_that_passes_max_output_tokens_to_the_subscription_login_is_reported(
    routing, fake_sdk, tmp_path, monkeypatch
):
    _quiet_environ(monkeypatch)
    bad = SUBSCRIPTION_KWARGS + ",'max_output_tokens'"
    report = _check(routing, tmp_path, runner=_write_runner(tmp_path, subscription_kwargs=bad))
    assert any("max_output_tokens ne doit pas être transmis" in m for m in report["failures"]), report["failures"]
    behavioural = _check(routing, tmp_path, runner=_write_runner(tmp_path, bug="subscription_passes_max_output"))
    assert any("[abonnement_identite_coder_et_replis]" in m for m in behavioural["failures"]), (
        "le doublon (TypeError) rend la construction impossible : scénario en échec, pas un succès"
    )


def test_a_subscription_login_that_drops_the_usage_id_is_reported(routing, fake_sdk, tmp_path, monkeypatch):
    _quiet_environ(monkeypatch)
    report = _check(routing, tmp_path, runner=_write_runner(tmp_path, subscription_kwargs="'num_retries','timeout'"))
    assert any("usage_id doit être transmis au login d'abonnement" in m for m in report["failures"])


def test_the_sampler_with_the_legacy_name_or_the_duplicate_bound_is_rejected(routing, fake_sdk, tmp_path, monkeypatch):
    _quiet_environ(monkeypatch)
    legacy = _check(routing, tmp_path, sampler=_write_sampler(tmp_path, legacy=True))
    assert any("[sampler:non_strict] usage_id 'default' != 'sampler'" in m for m in legacy["failures"])
    duplicate = _check(routing, tmp_path, sampler=_write_sampler(tmp_path, pass_max=True))
    assert any("[sampler:strict_borne_hote]" in m and "construction impossible" in m for m in duplicate["failures"]), (
        duplicate["failures"]
    )
    assert not any("[sampler:non_strict]" in m for m in duplicate["failures"])


def test_a_missing_sampler_is_a_failure_not_a_skip(routing, fake_sdk, tmp_path, monkeypatch):
    _quiet_environ(monkeypatch)
    report = _check(routing, tmp_path, sampler=str(tmp_path / "absent_sampler.py"))
    assert any("échantillonneur embarqué non chargeable" in m for m in report["failures"])


def test_the_jwks_network_helper_is_restored_after_the_subscription_checks(routing, fake_sdk, tmp_path, monkeypatch):
    _quiet_environ(monkeypatch)
    module = sys.modules["openhands.sdk.llm.auth.openai"]
    original = module._extract_chatgpt_account_id
    _check(routing, tmp_path)
    assert module._extract_chatgpt_account_id is original and original("t") == "reseau-interdit"
    with routing.no_jwks_fetch():
        assert module._extract_chatgpt_account_id("t") is None


# ---- API absente, SDK absent, hygiène ----


def test_a_runner_without_the_w4_api_is_reported_not_skipped(routing, fake_sdk, tmp_path, monkeypatch):
    _quiet_environ(monkeypatch)
    old = tmp_path / "old_runner.py"
    old.write_text("def main():\n    return 0\n", encoding="utf-8")
    report = _check(routing, tmp_path, runner=str(old))
    assert any("API du runner absente" in m for m in report["failures"])
    assert report["scenarios"] == []


def test_a_runner_without_the_subscription_api_is_reported(routing, fake_sdk, tmp_path, monkeypatch):
    _quiet_environ(monkeypatch)
    older = tmp_path / "older_runner.py"
    older.write_text(
        "LLM_CONSTRUCTOR_KWARGS = ('model',)\ndef llm_kwargs(*a):\n    return {}\n"
        "def resolve_credential(*a):\n    return None, ''\ndef main():\n    return 0\n",
        encoding="utf-8",
    )
    report = _check(routing, tmp_path, runner=str(older))
    assert any("API du runner absente" in m and "LLM_SUBSCRIPTION_KWARGS" in m for m in report["failures"])


def test_a_missing_sdk_is_a_failure(routing, monkeypatch, tmp_path):
    monkeypatch.setitem(sys.modules, "openhands.sdk", None)  # force ImportError
    report = _check(routing, tmp_path)
    assert any("SDK OpenHands non importable" in m for m in report["failures"])


def test_main_exit_code_and_report_do_not_leak_environment_secrets(routing, fake_sdk, tmp_path, monkeypatch, capsys):
    _quiet_environ(monkeypatch)
    monkeypatch.setenv("LLM_API_KEY", "host-secret-must-not-appear")
    rc = routing.main(
        [
            "--runner",
            _write_runner(tmp_path),
            "--sampler",
            _write_sampler(tmp_path),
            "--require-modules",
            "",
        ]
    )
    captured = capsys.readouterr()
    assert rc == 0, captured.err
    assert "host-secret-must-not-appear" not in captured.out + captured.err
    assert "LLM_API_KEY" in captured.out  # le NOM de la variable présente au départ est signalé, jamais sa valeur
    assert routing.FAKE_ACCESS_TOKEN not in captured.out + captured.err


def test_the_process_environment_is_restored_after_each_scenario(routing, fake_sdk, tmp_path, monkeypatch):
    _quiet_environ(monkeypatch)
    monkeypatch.setenv("LLM_MODEL", "sentinel-model")
    before = dict(__import__("os").environ)
    argv = list(sys.argv)
    stdin = sys.stdin
    _check(routing, tmp_path)
    assert dict(__import__("os").environ) == before
    assert sys.argv == argv and sys.stdin is stdin
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
