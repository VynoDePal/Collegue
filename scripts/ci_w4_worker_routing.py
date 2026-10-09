#!/usr/bin/env python3
"""Contrôle CI SANS MODÈLE du routage du worker OpenHands, dans l'image qui embarque le VRAI SDK (propriété C, vague 4).

Exécuté par le job « Docker build » dans un conteneur **sans réseau** et **sans aucun secret hôte** : le script est transmis
sur l'entrée standard (``python - < scripts/ci_w4_worker_routing.py``), donc aucun montage. L'image n'installe pas le paquet
``collegue`` : le runner embarqué ``/opt/oh_runner.py`` est chargé **par chemin** (``importlib``), jamais importé comme module
du paquet. Aucun appel LLM, aucun login, aucune requête : les identifiants ci-dessous sont des valeurs FACTICES.

Ce que le script établit (et ce qu'il n'établit pas) :

1. **API du runner** : ``LLM_CONSTRUCTOR_KWARGS``, ``llm_kwargs``, ``resolve_credential`` et ``main`` existent ; chaque argument
   utilisé par le runner figure dans ``openhands.sdk.LLM.model_fields`` (condition NÉCESSAIRE, pas suffisante) ;
2. **construction réelle** : ``oh_runner.main()`` est exécuté tel quel (mêmes variables d'environnement que celles que l'hôte
   transmet au worker) et construit un VRAI ``openhands.sdk.LLM`` ; seuls ``Conversation`` et ``get_default_agent`` sont
   remplacés par des doublures inertes (aucun appel de modèle). Pour chaque scénario on lit, sur l'objet construit :
   ``model`` (préfixe du fournisseur), ``base_url`` et la clé API effective (comparée à la valeur factice attendue) :

   * Gemini par défaut (clé ``LLM_API_KEY``) ; l'ancienne ``GEMINI_API_KEY`` reste réservée aux modèles ``gemini/…`` ;
   * OpenAI avec endpoint propre : endpoint et clé du rôle, jamais la clé Gemini présente dans l'environnement ;
   * fournisseur local OpenAI-compatible sans clé : clé fictive explicite ``local``, jamais une clé hôte ;
   * fournisseur non Gemini sans clé ni endpoint : refus (code 2) avant toute construction ;
   * replis : la chaîne ``OH_FALLBACK_MODELS`` garde le fournisseur, l'endpoint et la clé du primaire ;
   * arguments d'allocation (``max_output_tokens``, ``num_retries``…) acceptés par le vrai constructeur ;
3. **modules d'oracle** (``--require-modules``, par défaut ``pypdf``) : importables dans CETTE image, ``pypdf`` lisant un PDF.
   Cela ne prouve PAS quelle image le gate utilise réellement : ``_build_gate_sandbox`` prend ``SANDBOX_IMAGE`` (comme le
   codeur, sans ses credentials) et ce contrôle ne lit ni cette configuration ni l'image du run de campagne.

4. **identité du `LLM`** : le SDK déclare ``extra="ignore"`` — un argument inconnu (l'ancien ``service_id``) est ignoré SANS erreur.
   Le champ canonique est ``usage_id`` : sur CHAQUE vrai ``LLM`` construit (primaire et replis, clé API et abonnement) on
   asserte dynamiquement ``llm.usage_id == "coder"`` (``"sampler"`` pour l'échantillonneur), en plus de la garde
   ``model_fields`` qui reste ;
5. **abonnement** (sans login, sans connexion, sans réseau) : ``LLM.subscription_login`` n'est remplacé que par la fourniture
   de credentials FACTICES à la VRAIE ``OpenAISubscriptionAuth.create_llm`` (et le seul assistant réseau de ``create_llm``,
   l'extraction du compte par JWKS, est neutralisé ; le rapport le consigne). On vérifie l'identité coder/sampler,
   ``max_output_tokens is None`` (fixé par le SDK, jamais transmis), et la contre-épreuve : un ``max_output_tokens`` passé en plus
   lève ``TypeError`` (doublon) — ce que le runner ne fait plus.

Hors périmètre : le login réel d'abonnement (session ChatGPT), les appels réseau, le budget.
Sortie : un rapport JSON sur stdout (aucune clé) ; code 0 si tout est vérifié, 1 sinon.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib
import importlib.util
import io
import json
import os
import re
import sys
import tempfile
import types

DEFAULT_RUNNER = "/opt/oh_runner.py"
DEFAULT_SAMPLER = "/opt/oh_sampler.py"
CODER_USAGE_ID = "coder"
SAMPLER_USAGE_ID = "sampler"
SDK_DEFAULT_USAGE_ID = "default"
ANY = object()  # « n'importe quelle valeur non nulle » dans une attente
SECRET_LIKE = re.compile(
    r"(?i)(^LLM_|^OH_|GEMINI|OPENAI|ANTHROPIC|GOOGLE|API_KEY|TOKEN|SECRET|AUTH|PASSWORD|CREDENTIAL)"
)

# Valeurs FACTICES, distinctes pour détecter toute fuite d'une clé vers une autre destination.
FAKE_ROLE_KEY = "w4-fake-role-key-0001"
FAKE_GEMINI_KEY = "w4-fake-gemini-key-0002"
FAKE_OTHER_KEY = "w4-fake-other-key-0003"
OPENAI_ENDPOINT = "https://llm.example.invalid/v1"
LOCAL_ENDPOINT = "http://127.0.0.1:11434/v1"

FAKE_ACCESS_TOKEN = "w4-fake-oauth-access-0004"
FAKE_REFRESH_TOKEN = "w4-fake-oauth-refresh-0005"
FAKE_EXPIRES_AT_MS = 4_102_444_800_000  # 2100-01-01, en millisecondes

REQUIRED_RUNNER_API = (
    "LLM_CONSTRUCTOR_KWARGS",
    "LLM_SUBSCRIPTION_KWARGS",
    "llm_kwargs",
    "resolve_credential",
    "main",
)


class Failures:
    def __init__(self) -> None:
        self.items: list[str] = []

    def check(self, condition: bool, message: str) -> bool:
        if not condition:
            self.items.append(message)
        return bool(condition)


def fingerprint(value) -> str:
    return "none" if value is None else hashlib.sha256(str(value).encode()).hexdigest()[:10]


def secret_value(secret) -> str | None:
    """Valeur d'un ``SecretStr`` (ou d'une chaîne) ; ``None`` si absente."""
    if secret is None:
        return None
    getter = getattr(secret, "get_secret_value", None)
    return getter() if callable(getter) else str(secret)


def load_runner(path: str):
    """Charge le runner embarqué PAR CHEMIN (le paquet ``collegue`` n'est pas installé dans l'image)."""
    spec = importlib.util.spec_from_file_location("oh_runner_embedded", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"runner illisible : {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["oh_runner_embedded"] = module
    spec.loader.exec_module(module)
    return module


class _InertConversation:
    """Doublure : aucune conversation, aucun appel de modèle. ``run`` échoue pour les modèles listés (test des replis)."""

    fail_for: set[str] = set()
    created: list = []

    def __init__(self, agent=None, workspace=None, max_iteration_per_run=None, **_kw) -> None:
        self.agent = agent
        type(self).created.append(self)

    def send_message(self, _task) -> None:
        return None

    def run(self) -> None:
        model = getattr(getattr(self.agent, "llm", None), "model", None)
        if model in type(self).fail_for:
            raise RuntimeError(f"échec simulé du modèle {model} (repli attendu)")


class Scenario:
    def __init__(
        self,
        name: str,
        env: dict,
        *,
        expect_rc: int = 0,
        fail_models: tuple = (),
        expect: list | None = None,
        subscription: bool = False,
    ):
        self.name = name
        self.env = env
        self.subscription = subscription  # credentials FACTICES fournis à la vraie create_llm
        self.expect_rc = expect_rc
        self.fail_models = set(fail_models)
        # [(model, base_url ou None, clé factice attendue)] dans l'ordre de construction
        self.expect = expect or []


def scenarios() -> list[Scenario]:
    return [
        Scenario(
            "gemini_par_defaut",
            {"LLM_MODEL": "gemini/gemma-4-31b-it", "LLM_API_KEY": FAKE_GEMINI_KEY},
            expect=[("gemini/gemma-4-31b-it", None, FAKE_GEMINI_KEY)],
        ),
        Scenario(
            "gemini_ancienne_variable_de_cle",
            {"LLM_MODEL": "gemini/gemma-4-31b-it", "GEMINI_API_KEY": FAKE_GEMINI_KEY},
            expect=[("gemini/gemma-4-31b-it", None, FAKE_GEMINI_KEY)],
        ),
        Scenario(
            "openai_endpoint_propre_sans_fuite_de_la_cle_gemini",
            {
                "LLM_MODEL": "openai/gpt-5.4",
                "LLM_BASE_URL": OPENAI_ENDPOINT,
                "LLM_API_KEY": FAKE_ROLE_KEY,
                "GEMINI_API_KEY": FAKE_GEMINI_KEY,
            },
            expect=[("openai/gpt-5.4", OPENAI_ENDPOINT, FAKE_ROLE_KEY)],
        ),
        Scenario(
            "local_sans_cle_ni_cle_hote",
            {"LLM_MODEL": "openai/local-model", "LLM_BASE_URL": LOCAL_ENDPOINT, "GEMINI_API_KEY": FAKE_GEMINI_KEY},
            expect=[("openai/local-model", LOCAL_ENDPOINT, "local")],
        ),
        Scenario(
            "fournisseur_non_gemini_sans_cle_refuse_avant_construction",
            {"LLM_MODEL": "openai/gpt-5.4", "GEMINI_API_KEY": FAKE_GEMINI_KEY},
            expect_rc=2,
            expect=[],
        ),
        Scenario(
            "replis_meme_fournisseur_meme_endpoint_meme_cle",
            {
                "LLM_MODEL": "openai/gpt-5.5",
                "LLM_BASE_URL": OPENAI_ENDPOINT,
                "LLM_API_KEY": FAKE_ROLE_KEY,
                "GEMINI_API_KEY": FAKE_GEMINI_KEY,
                "OH_FALLBACK_MODELS": "openai/gpt-5.4,openai/gpt-5.5",
            },
            fail_models=("openai/gpt-5.5",),
            expect=[
                ("openai/gpt-5.5", OPENAI_ENDPOINT, FAKE_ROLE_KEY),
                ("openai/gpt-5.4", OPENAI_ENDPOINT, FAKE_ROLE_KEY),
            ],
        ),
        Scenario(
            "replis_gemini",
            {
                "LLM_MODEL": "gemini/gemma-4-31b-it",
                "LLM_API_KEY": FAKE_GEMINI_KEY,
                "OH_FALLBACK_MODELS": "gemini/gemma-4-26b-a4b-it",
            },
            fail_models=("gemini/gemma-4-31b-it",),
            expect=[
                ("gemini/gemma-4-31b-it", None, FAKE_GEMINI_KEY),
                ("gemini/gemma-4-26b-a4b-it", None, FAKE_GEMINI_KEY),
            ],
        ),
        Scenario(
            "abonnement_identite_coder_et_replis",
            {"LLM_MODEL": "gpt-5.5", "LLM_SUBSCRIPTION": "1", "OH_FALLBACK_MODELS": "gpt-5.4"},
            fail_models=("openai/gpt-5.5",),
            subscription=True,
            expect=[
                ("openai/gpt-5.5", ANY, FAKE_ACCESS_TOKEN),
                ("openai/gpt-5.4", ANY, FAKE_ACCESS_TOKEN),
            ],
        ),
    ]


@contextlib.contextmanager
def no_jwks_fetch():
    """Neutralise l'UNIQUE assistant réseau de ``create_llm`` (extraction du compte ChatGPT par JWKS) ; tout le reste est réel."""
    module = importlib.import_module("openhands.sdk.llm.auth.openai")
    original = module._extract_chatgpt_account_id
    module._extract_chatgpt_account_id = lambda _token: None
    try:
        yield
    finally:
        module._extract_chatgpt_account_id = original


def fake_credentials(module):
    return module.OAuthCredentials(
        vendor="openai", access_token=FAKE_ACCESS_TOKEN, refresh_token=FAKE_REFRESH_TOKEN, expires_at=FAKE_EXPIRES_AT_MS
    )


def real_create_llm(model: str, **llm_kwargs):
    """VRAIE ``OpenAISubscriptionAuth.create_llm`` avec credentials FACTICES, magasin temporaire : aucun login, aucune connexion."""
    module = importlib.import_module("openhands.sdk.llm.auth.openai")
    credentials_module = importlib.import_module("openhands.sdk.llm.auth.credentials")
    with tempfile.TemporaryDirectory(prefix="w4-oauth-") as folder, no_jwks_fetch():
        auth = module.OpenAISubscriptionAuth(
            credential_store=credentials_module.CredentialStore(credentials_dir=__import__("pathlib").Path(folder))
        )
        return auth.create_llm(model=model, credentials=fake_credentials(module), **llm_kwargs)


def run_scenario(runner, sdk, preset, scenario: Scenario, failures: Failures) -> dict:
    """Exécute ``runner.main()`` pour un scénario, avec le VRAI ``LLM`` et des doublures inertes de conversation."""
    real_llm = sdk.LLM
    constructed: list = []

    def recording_llm(*args, **kwargs):
        instance = real_llm(*args, **kwargs)
        constructed.append(instance)
        return instance

    def subscription_login(vendor="openai", model="gpt-5.2-codex", force_login=False, open_browser=True, **llm_kwargs):
        # Seule la FOURNITURE des credentials remplace le login : la construction est celle de la vraie create_llm.
        instance = real_create_llm(model, **llm_kwargs)
        constructed.append(instance)
        return instance

    if scenario.subscription:
        recording_llm.subscription_login = subscription_login

    # Les doublures remplacent UNIQUEMENT ce qui exécuterait un agent ; le constructeur de LLM reste le vrai.
    saved = (sdk.LLM, sdk.Conversation, preset.get_default_agent, dict(os.environ), list(sys.argv))
    _InertConversation.fail_for = set(scenario.fail_models)
    _InertConversation.created = []
    out, err = io.StringIO(), io.StringIO()
    rc = None
    error = None
    try:
        sdk.LLM = recording_llm
        sdk.Conversation = _InertConversation
        preset.get_default_agent = lambda llm=None, **_kw: types.SimpleNamespace(llm=llm)
        for key in list(os.environ):
            if SECRET_LIKE.search(key):
                del os.environ[key]
        os.environ.update(scenario.env)
        with tempfile.TemporaryDirectory(prefix="w4-routing-") as workspace:
            sys.argv = ["oh_runner", "-t", "ping (aucun appel de modèle)", "--workspace", workspace]
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                try:
                    rc = runner.main()
                except SystemExit as exc:
                    rc = exc.code if isinstance(exc.code, int) else 1
    except Exception as exc:  # noqa: BLE001 - toute exception est un échec du scénario, avec sa cause
        error = f"{type(exc).__name__}: {exc}"
    finally:
        sdk.LLM, sdk.Conversation, preset.get_default_agent = saved[0], saved[1], saved[2]
        os.environ.clear()
        os.environ.update(saved[3])
        sys.argv = saved[4]

    observed = [
        (
            getattr(llm, "model", None),
            getattr(llm, "base_url", None) or None,
            secret_value(getattr(llm, "api_key", None)),
            getattr(llm, "usage_id", None),
            getattr(llm, "max_output_tokens", None),
        )
        for llm in constructed
    ]
    name = scenario.name
    failures.check(error is None, f"[{name}] exception pendant main(): {error}")
    failures.check(rc == scenario.expect_rc, f"[{name}] code de sortie {rc!r} != {scenario.expect_rc!r}")
    failures.check(
        len(observed) == len(scenario.expect),
        f"[{name}] {len(observed)} LLM construit(s), {len(scenario.expect)} attendu(s) : {[o[0] for o in observed]}",
    )
    for index, (expected, got) in enumerate(zip(scenario.expect, observed, strict=False)):
        model, base_url, key = expected
        failures.check(got[0] == model, f"[{name}#{index}] modèle {got[0]!r} != {model!r}")
        if base_url is ANY:
            failures.check(
                got[1] is not None, f"[{name}#{index}] endpoint absent (attendu : celui du backend abonnement)"
            )
        else:
            failures.check(got[1] == base_url, f"[{name}#{index}] endpoint {got[1]!r} != {base_url!r}")
        failures.check(
            got[2] == key,
            f"[{name}#{index}] clé effective {fingerprint(got[2])} != clé attendue {fingerprint(key)} "
            "(fuite d'une clé vers une autre destination ?)",
        )
    for index, got in enumerate(observed):
        failures.check(
            got[3] == CODER_USAGE_ID,
            f'[{name}#{index}] usage_id {got[3]!r} != {CODER_USAGE_ID!r} (argument inconnu ignoré par extra="ignore" ? '
            "l'identité du codeur n'est pas portée par le vrai LLM)",
        )
        if scenario.subscription:
            failures.check(
                got[4] is None, f"[{name}#{index}] max_output_tokens {got[4]!r} : jamais de borne locale en abonnement"
            )
    foreign = {FAKE_GEMINI_KEY, FAKE_OTHER_KEY} - {e[2] for e in scenario.expect}
    for index, got in enumerate(observed):
        failures.check(
            got[2] not in foreign, f"[{name}#{index}] une clé destinée à un autre fournisseur a été utilisée"
        )
    return {
        "scenario": name,
        "rc": rc,
        "constructed": [
            {
                "model": m,
                "base_url": b,
                "key": ("local" if k == "local" else f"factice:{fingerprint(k)}"),
                "usage_id": u,
                "max_output_tokens": t,
            }
            for m, b, k, u, t in observed
        ],
    }


def check_allocation_kwargs(runner, sdk, failures: Failures) -> dict:
    """Le VRAI constructeur accepte et conserve les arguments d'allocation que le runner transmet sous plafond."""
    common = dict(
        usage_id=CODER_USAGE_ID,
        num_retries=0,
        retry_min_wait=8,
        retry_max_wait=90,
        timeout=300,
        max_output_tokens=4096,
    )
    kwargs = runner.llm_kwargs("gemini/gemma-4-31b-it", FAKE_GEMINI_KEY, None, dict(common))
    fields = set(sdk.LLM.model_fields)
    unknown = sorted(set(kwargs) - fields)
    failures.check(not unknown, f"arguments absents de LLM.model_fields : {unknown}")
    report = {"kwargs": sorted(kwargs)}
    try:
        llm = sdk.LLM(**kwargs)
    except Exception as exc:  # noqa: BLE001
        failures.check(
            False, f"construction de LLM avec les arguments d'allocation refusée : {type(exc).__name__}: {exc}"
        )
        return report
    cap = getattr(llm, "max_output_tokens", None)  # le SDK peut plafonner plus bas, jamais perdre ni relever le plafond
    failures.check(isinstance(cap, int) and 0 < cap <= 4096, f"max_output_tokens non porté par LLM : {cap!r}")
    failures.check(getattr(llm, "num_retries", None) == 0, "num_retries non conservé par LLM")
    failures.check(getattr(llm, "usage_id", None) == CODER_USAGE_ID, f"usage_id non porté : {llm.usage_id!r}")
    failures.check(
        secret_value(getattr(llm, "api_key", None)) == FAKE_GEMINI_KEY, "clé effective différente de la clé donnée"
    )
    report["effective"] = {"max_output_tokens": llm.max_output_tokens, "num_retries": llm.num_retries}
    return report


class _StopAfterConstruction(BaseException):
    """Interrompt l'échantillonneur APRÈS la construction du LLM (aucune complétion, aucun appel) ; pas une ``Exception``."""


SAMPLER_PAYLOADS = (
    ("non_strict", {"system": "s", "prompt": "p"}),
    ("strict_borne_hote", {"system": "s", "prompt": "p", "strict": True, "max_output_tokens": 512}),
)


def _sampler_case(module, sdk, label: str, payload: dict, failures: Failures) -> dict:
    built: list = []
    real_llm = sdk.LLM

    def recording_llm(*args, **kwargs):  # pragma: no cover - le sampler n'instancie pas LLM directement
        return real_llm(*args, **kwargs)

    def subscription_login(vendor="openai", model="gpt-5.2-codex", force_login=False, open_browser=True, **kwargs):
        built.append(real_create_llm(model, **kwargs))
        raise _StopAfterConstruction

    recording_llm.subscription_login = subscription_login
    saved_stdin, saved_env, error = sys.stdin, dict(os.environ), None
    try:
        sdk.LLM = recording_llm
        for key in list(os.environ):
            if SECRET_LIKE.search(key):
                del os.environ[key]
        os.environ["LLM_MODEL"] = "gpt-5.4"
        sys.stdin = io.StringIO(json.dumps(payload))
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            try:
                module.main()
            except _StopAfterConstruction:
                pass
    except Exception as exc:  # noqa: BLE001
        error = f"{type(exc).__name__}: {exc}"
    finally:
        sdk.LLM = real_llm
        sys.stdin = saved_stdin
        os.environ.clear()
        os.environ.update(saved_env)
    failures.check(error is None, f"[sampler:{label}] construction impossible : {error}")
    failures.check(len(built) == 1, f"[sampler:{label}] {len(built)} LLM construit(s), 1 attendu")
    for llm in built:
        failures.check(
            getattr(llm, "usage_id", None) == SAMPLER_USAGE_ID,
            f"[sampler:{label}] usage_id {getattr(llm, 'usage_id', None)!r} != {SAMPLER_USAGE_ID!r}",
        )
        failures.check(
            getattr(llm, "max_output_tokens", "absent") is None,
            f"[sampler:{label}] max_output_tokens {getattr(llm, 'max_output_tokens', None)!r} : jamais de borne locale",
        )
        failures.check(bool(getattr(llm, "is_subscription", False)), f"[sampler:{label}] LLM non marqué abonnement")
    return {
        "payload": label,
        "usage_id": getattr(built[0], "usage_id", None) if built else None,
        "max_output_tokens": getattr(built[0], "max_output_tokens", "absent") if built else "absent",
    }


def check_sampler(sdk, sampler_path: str, failures: Failures) -> list:
    """Exécute le VRAI ``main()`` de l'échantillonneur embarqué jusqu'à la construction de son LLM (vraie ``create_llm``)."""
    report = []
    try:
        spec = importlib.util.spec_from_file_location("oh_sampler_embedded", sampler_path)
        if spec is None or spec.loader is None:
            raise RuntimeError("illisible")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    except Exception as exc:  # noqa: BLE001
        failures.check(False, f"échantillonneur embarqué non chargeable ({sampler_path}) : {type(exc).__name__}: {exc}")
        return report
    failures.check(callable(getattr(module, "main", None)), "échantillonneur sans main()")
    for label, payload in SAMPLER_PAYLOADS:
        report.append(_sampler_case(module, sdk, label, payload, failures))
    return report


def check_subscription_contract(runner, sdk, failures: Failures, sampler_path: str) -> dict:
    """Contrat d'abonnement du VRAI SDK : identité, absence de borne locale, contre-épreuves (doublon, ancien nom)."""
    report: dict = {"jwks_helper_replaced": True, "credentials": "factices", "login": False, "network": False}
    allowed = list(getattr(runner, "LLM_SUBSCRIPTION_KWARGS", ()))
    fields = set(sdk.LLM.model_fields)
    failures.check(
        "max_output_tokens" not in allowed,
        "max_output_tokens ne doit pas être transmis au login d'abonnement (doublon)",
    )
    failures.check(
        not (set(allowed) - fields),
        f"arguments d'abonnement absents de LLM.model_fields : {sorted(set(allowed) - fields)}",
    )
    failures.check("usage_id" in allowed, "usage_id doit être transmis au login d'abonnement")
    common = dict(usage_id=CODER_USAGE_ID, num_retries=0, retry_min_wait=8, retry_max_wait=90, timeout=300)
    allocation = dict(common, max_output_tokens=4096)  # ce que le runner construit sous allocation budgétaire
    filtered = {k: v for k, v in allocation.items() if k in allowed}
    try:
        llm = real_create_llm("gpt-5.2-codex", **filtered)
    except Exception as exc:  # noqa: BLE001
        failures.check(
            False, f"create_llm refuse les arguments du runner sous allocation : {type(exc).__name__}: {exc}"
        )
        llm = None
    if llm is not None:
        failures.check(llm.usage_id == CODER_USAGE_ID, f"usage_id {llm.usage_id!r} != {CODER_USAGE_ID!r}")
        failures.check(llm.max_output_tokens is None, f"max_output_tokens {llm.max_output_tokens!r} : doit rester None")
        failures.check(bool(llm.is_subscription), "LLM non marqué abonnement")
        report["effective"] = {"usage_id": llm.usage_id, "max_output_tokens": llm.max_output_tokens}
    # Contre-épreuve : le doublon que l'ancien runner provoquait sous allocation lève TypeError dans le VRAI SDK.
    try:
        real_create_llm("gpt-5.2-codex", **allocation)
    except TypeError as exc:
        report["duplicate_max_output_tokens"] = "TypeError"
        failures.check("max_output_tokens" in str(exc), f"TypeError inattendu : {exc}")
    except Exception as exc:  # noqa: BLE001
        failures.check(False, f"doublon : exception inattendue {type(exc).__name__}: {exc}")
    else:
        failures.check(
            False, "le doublon de max_output_tokens n'a pas levé TypeError : contrat du SDK modifié, à réexaminer"
        )
    # Contre-épreuve : l'ancien nom est ignoré par le SDK (extra="ignore") et l'identité reste « default ».
    legacy = real_create_llm("gpt-5.2-codex", service_id=CODER_USAGE_ID)
    report["legacy_service_id_usage_id"] = legacy.usage_id
    failures.check(
        legacy.usage_id == SDK_DEFAULT_USAGE_ID,
        f"service_id est désormais honoré (usage_id {legacy.usage_id!r}) : contrat du SDK modifié, à réexaminer",
    )
    report["sampler"] = check_sampler(sdk, sampler_path, failures)
    return report


def minimal_pdf(text: str) -> bytes:
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode("latin-1")
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    buffer = io.BytesIO()
    buffer.write(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objs, 1):
        offsets.append(buffer.tell())
        buffer.write(f"{number} 0 obj\n".encode() + body + b"\nendobj\n")
    xref = buffer.tell()
    buffer.write(f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode())
    for offset in offsets:
        buffer.write(f"{offset:010d} 00000 n \n".encode())
    buffer.write(f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return buffer.getvalue()


def check_modules(modules: list[str], failures: Failures) -> dict:
    report: dict = {}
    for name in modules:
        spec = importlib.util.find_spec(name) if name else None
        failures.check(spec is not None, f"module d'oracle absent de l'image : {name!r}")
        report[name] = spec is not None
        if name == "pypdf" and spec is not None:
            from pypdf import PdfReader  # lecteur réel : texte extrait, pas de recherche d'octets

            text = PdfReader(io.BytesIO(minimal_pdf("Audit 42 - conformite OK"))).pages[0].extract_text()
            failures.check("Audit 42" in text, f"pypdf n'extrait pas le texte attendu : {text!r}")
            report["pypdf_extrait"] = text
    return report


def run_checks(runner_path: str, modules: list[str], sampler_path: str = DEFAULT_SAMPLER) -> dict:
    failures = Failures()
    report: dict = {"runner": runner_path, "scenarios": []}
    stripped = [k for k in os.environ if SECRET_LIKE.search(k)]
    report["variables_sensibles_presentes_au_depart"] = sorted(stripped)  # noms seulement ; doit être vide en CI
    try:
        sdk = importlib.import_module("openhands.sdk")
        preset = importlib.import_module("openhands.tools.preset.default")
    except Exception as exc:  # noqa: BLE001
        failures.check(False, f"SDK OpenHands non importable dans l'image : {type(exc).__name__}: {exc}")
        report["failures"] = failures.items
        return report
    report["sdk"] = {"LLM_fields": len(sdk.LLM.model_fields), "base_url_is_a_field": "base_url" in sdk.LLM.model_fields}
    try:
        runner = load_runner(runner_path)
    except Exception as exc:  # noqa: BLE001
        failures.check(False, f"runner embarqué non chargeable ({runner_path}) : {type(exc).__name__}: {exc}")
        report["failures"] = failures.items
        return report
    missing = [name for name in REQUIRED_RUNNER_API if not hasattr(runner, name)]
    if not failures.check(not missing, f"API du runner absente : {missing} (runner antérieur à la vague 4 ?)"):
        report["failures"] = failures.items
        return report
    declared = list(runner.LLM_CONSTRUCTOR_KWARGS)
    unknown = sorted(set(declared) - set(sdk.LLM.model_fields))
    failures.check(not unknown, f"arguments du runner absents de LLM.model_fields : {unknown}")
    failures.check("base_url" in declared, "base_url n'est pas transmis au constructeur de LLM par le runner")
    report["kwargs_declares"] = declared
    for scenario in scenarios():
        report["scenarios"].append(run_scenario(runner, sdk, preset, scenario, failures))
    report["allocation"] = check_allocation_kwargs(runner, sdk, failures)
    report["subscription"] = check_subscription_contract(runner, sdk, failures, sampler_path)
    report["modules"] = check_modules(modules, failures)
    report["failures"] = failures.items
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--runner", default=DEFAULT_RUNNER)
    parser.add_argument("--sampler", default=DEFAULT_SAMPLER)
    parser.add_argument("--require-modules", default="pypdf", help="modules d'oracle exigés dans l'image (CSV)")
    args = parser.parse_args(argv)
    modules = [m.strip() for m in args.require_modules.split(",") if m.strip()]
    report = run_checks(args.runner, modules, args.sampler)
    report["ok"] = not report["failures"]
    print(json.dumps(report, indent=1, ensure_ascii=False, sort_keys=True))
    for message in report["failures"]:
        print(f"ECART: {message}", file=sys.stderr)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
