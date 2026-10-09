"""PREUVE COMPOSÉE W5 en conteneur réel (propriété C) — exécutée par le job « Docker build » APRÈS intégration de A et B.

Composition : image finale ``collegue-sandbox-broker`` (VRAI SDK OpenHands, VRAI ``oh_runner`` et relais) lancée par le VRAI ``DockerSandbox``
(``--network none``, socket du courtier monté en lecture seule) ; VRAI ``BrokerRuntime`` / ``BrokerService`` / ``BrokerSocketServer`` et
registre budgétaire SQLite sur l'hôte ; FAUX fournisseur Google derrière le service, qui observe ``countTokens`` / ``generateContent``.
Les mesures d'usage viennent du registre, jamais du stdout du worker.

Ces tests sont marqués ``w5_image`` : exclus de la suite générale (aucun Docker ni image), sélectionnés explicitement (``-m w5_image``) par le
job « Docker build », collectés et exécutés SANS saut (``scripts/ci_require_junit.py`` rejette tout test sauté ou manquant). Sans Docker ou
sans l'image, ils ÉCHOUENT (``DockerUnavailable``). **Non exécutés dans cette passe** (aucun Docker, aucun SDK local, A et B non intégrés) :
leur statut est « préparés », pas « prouvés » ; la liste des interfaces de A dont ils dépendent est ``INTERFACE_CONTRACT`` du harnais.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone

import pytest
from w5_integration_harness import (
    HOSTILE,
    IMAGE,
    PROVIDER_KEY,
    WORKER,
    FakeGoogle,
    HostStack,
    containers_of,
    declared_functions,
    parse_facts,
    require_docker_and_image,
    sdk_script,
)

pytestmark = pytest.mark.w5_image

DEADLINE = 20  # secondes de l'échéance globale persistée (ouverte avant le run)
TIMEOUT_NOTE = "délai dépassé"


@pytest.fixture(autouse=True)
def docker_and_image():
    require_docker_and_image(IMAGE)  # absence = ÉCHEC, jamais un saut
    assert containers_of(IMAGE) == [], "aucun conteneur de l'image ne doit préexister à la preuve"


@pytest.fixture
def stacks(tmp_path):
    made = []

    def build(**kwargs):
        sub = tmp_path / f"s{len(made)}"
        sub.mkdir()
        built = HostStack(sub, **kwargs)
        made.append(built)
        return built

    yield build
    for built in made:
        built.close()


def assert_nothing_left(stack, image=IMAGE, *, in_flight=False):
    assert containers_of(image) == [], "le conteneur du worker doit être ABSENT (tué ET supprimé)"
    sessions = stack.service.store.open_sessions()
    if in_flight:
        # une requête émise coupée par l'arrêt laisse la session en fermeture, EXPLICITEMENT marquée inconnue (réparée au démarrage suivant)
        assert sessions and all(item.unknown_reason for item in sessions), sessions
    else:
        assert sessions == [], "aucune session du courtier ne doit rester ouverte"


# ── 1. le VRAI SDK / runner à travers le VRAI courtier ───────────────────────────────────────────────────────────────────────


def test_the_real_sdk_runner_traverses_the_real_broker_service_and_registry_with_a_fake_google_behind(stacks):
    fake = FakeGoogle(script=sdk_script("/workspace/proof.txt"))
    stack = stacks(upstream=fake)
    result = stack.run()

    assert result.success, result.logs[-2000:]
    assert (stack.workspace / "proof.txt").read_text().strip() == "composed-proof", (
        "l'outil terminal du SDK a réellement agi"
    )
    n = len(fake.generate_calls)
    assert n >= 2 and len(fake.count_calls) == n, "chaque génération est précédée de SON countTokens"
    for counted, generated in zip(fake.count_calls, fake.generate_calls, strict=True):
        assert counted["body"] == {"generateContentRequest": generated["body"]}, (
            "countTokens et generateContent portent le MÊME objet"
        )
        assert counted["model"] == generated["model"] == "gemma-4-31b-it"
        assert generated["body"]["model"] == "models/gemma-4-31b-it"
        assert (
            generated["body"]["generationConfig"]["candidateCount"] == 1
            and generated["body"]["generationConfig"]["maxOutputTokens"] > 0
        )
        assert declared_functions(generated["body"]), "les outils du SDK sont traduits en functionDeclarations"
    # usage = REGISTRE (jamais stdout) : règlement exact des n générations, rien de réservé ni d'inconnu, rien de bloqué
    project = stack.ledger.snapshot(stack.scope)
    assert (project.consumed_tokens, project.reserved_tokens, project.unknown_tokens) == (
        15 * n,
        0,
        0,
    ) and not project.blocked
    assert (result.prompt_tokens, result.completion_tokens) == (10 * n, 5 * n) and result.usage_source == "broker"
    assert PROVIDER_KEY not in result.logs
    assert_nothing_left(stack)


# ── 2. code hostile appelant le socket ───────────────────────────────────────────────────────────────────────────────────────


def test_hostile_code_cannot_widen_its_session_reach_the_internet_or_see_a_provider_key(stacks):
    fake = FakeGoogle()
    stack = stacks(upstream=fake, worker_source=HOSTILE)
    result = stack.run()
    facts = parse_facts(result.logs)

    refused = {
        "unauthorized_model": {403},
        "unknown_session_token": {401},
        "no_token": {401},
        "other_route": {404},
        "administration_route": {404},
        "absolute_url_request_line": {404},
        "injected_base_url": {400},
        "injected_api_key_field": {400},
        "injected_scope": {400},
        "stream": {400, 422},
        "hosted_tool": {400, 422},
    }
    for name, allowed in refused.items():
        assert facts[name] in allowed, (name, facts[name])
    assert facts["control_legitimate_call"] == 200
    assert facts["provider_key_header"] == 200, (
        "un en-tête de clé fourni par le client est ignoré (jamais relayé), la requête reste ordinaire"
    )
    # le fournisseur n'a VU que les deux appels ordinaires : toute autre tentative est refusée AVANT l'émission
    assert len(fake.generate_calls) == len(fake.count_calls) == 2
    assert {call["body"]["model"] for call in fake.generate_calls} == {"models/gemma-4-31b-it"}
    # isolation observée DEPUIS le conteneur
    assert facts["external_connections"] == [] and facts["interfaces"] == ["lo"]
    assert facts["secret_like_env"] == ["LLM_API_KEY"] and facts["llm_api_key_is_a_session_token"] is True
    assert facts["env_has_provider_key_value"] is False and facts["docker_socket_visible"] is False
    assert set(facts["mounts"]) <= {"/run/collegue-broker", "/workspace"}, facts["mounts"]
    assert PROVIDER_KEY not in result.logs
    project = stack.ledger.snapshot(stack.scope)
    assert project.consumed_tokens == 30 and project.reserved_tokens == 0 and project.unknown_tokens == 0
    assert_nothing_left(stack)


# ── 3. échéance persistée commune appliquée au PROCESSUS du worker ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("behaviour", ["sleep_only", "sleep_after_call", "compute_after_call"])
def test_a_worker_that_sleeps_or_computes_without_a_new_call_is_stopped_at_the_persisted_deadline(stacks, behaviour):
    fake = FakeGoogle()
    stack = stacks(upstream=fake, deadline=DEADLINE, worker_source=WORKER, extra_env={"W5_BEHAVIOUR": behaviour})
    stack.service.open_clock(stack.scope)  # le temps global a commencé AVANT ce run (planification / canaris)
    persisted = stack.service.persisted_deadline(stack.scope)
    assert persisted is not None

    started = time.monotonic()
    result = stack.run(window=3600)  # la fenêtre LOCALE du run (une heure) n'est pas l'échéance
    elapsed = time.monotonic() - started

    assert not result.success and TIMEOUT_NOTE in result.logs, result.logs[-1500:]
    assert 3 < elapsed < DEADLINE + 15 + 25, (
        elapsed
    )  # arrêté après l'échéance (+ TERM puis KILL + démarrage), pas après 600 s
    # le délai du conteneur est un entier INFÉRIEUR à l'échéance restante : l'arrêt ne la dépasse jamais, il peut la précéder de moins d'une seconde
    assert datetime.now(timezone.utc) >= persisted - timedelta(seconds=2)
    project = stack.ledger.snapshot(stack.scope)
    if behaviour == "sleep_only":
        assert (project.consumed_tokens, project.reserved_tokens, project.unknown_tokens) == (0, 0, 0)
        assert fake.generate_calls == []
    else:
        assert (project.consumed_tokens, project.reserved_tokens, project.unknown_tokens) == (
            15,
            0,
            0,
        ) and not project.blocked
        assert len(fake.generate_calls) == 1, "aucune génération après le dernier appel"
    assert_nothing_left(stack)


def test_a_request_in_flight_when_the_deadline_stops_the_worker_stays_reserved_as_unknown(stacks):
    fake = FakeGoogle()
    fake.gate = asyncio.Event()  # le fournisseur ne répond JAMAIS : requête émise, worker bloqué en vol
    stack = stacks(
        upstream=fake, deadline=DEADLINE, close_wait=1.0, worker_source=WORKER, extra_env={"W5_BEHAVIOUR": "quick"}
    )
    stack.service.open_clock(stack.scope)

    started = time.monotonic()
    result = stack.run(window=3600)
    elapsed = time.monotonic() - started

    assert not result.success and result.usage_status == "incomplete" and result.usage_source == "broker"
    assert elapsed < DEADLINE + 15 + 25, elapsed
    project = stack.ledger.snapshot(stack.scope)
    assert project.blocked and project.unknown_tokens > 0 and project.consumed_tokens == 0, (
        "réservation CONSERVÉE, jamais libérée"
    )
    assert len(fake.generate_calls) == 1, "une seule émission, aucune autre ensuite"
    assert_nothing_left(stack, in_flight=True)


def test_no_new_generation_or_worker_starts_after_the_persisted_deadline(stacks):
    from collegue.broker import BrokerConfig  # noqa: F401  (contrat : l'échéance vient de la configuration du courtier)

    from collegue.sandbox.executor import SandboxRefused
    from collegue.state import BudgetRefused

    fake = FakeGoogle()
    stack = stacks(upstream=fake, deadline=DEADLINE, worker_source=WORKER, extra_env={"W5_BEHAVIOUR": "quick"})
    stack.service.open_clock(stack.scope)
    persisted = stack.service.persisted_deadline(stack.scope)
    wait = (persisted - datetime.now(timezone.utc)).total_seconds() + 1
    time.sleep(max(0.0, wait))

    before = len(fake.generate_calls)
    try:
        result = stack.run(window=3600)
        refused = not result.success
    except (SandboxRefused, BudgetRefused):
        refused = True
    assert refused, "un worker ne doit plus démarrer après l'échéance persistée"
    assert len(fake.generate_calls) == before == 0, "aucune génération après l'échéance"
    assert_nothing_left(stack)
