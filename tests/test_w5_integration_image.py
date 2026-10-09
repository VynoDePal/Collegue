"""PREUVE COMPOSÉE W5 en conteneur réel (propriété C) — exécutée par le job « Docker build » APRÈS intégration de A et B.

Composition : image finale ``collegue-sandbox-broker`` (VRAI SDK OpenHands, VRAI ``oh_runner`` et relais) lancée par le VRAI ``DockerSandbox``
(``--network none``, socket du courtier monté en lecture seule) ; VRAI ``BrokerRuntime`` / ``BrokerService`` / ``BrokerSocketServer`` et
registre budgétaire SQLite sur l'hôte ; FAUX fournisseur Google derrière le service, qui observe ``countTokens`` / ``generateContent``.
Les mesures d'usage viennent du registre, jamais du stdout du worker.

Ces tests sont marqués ``w5_image`` : exclus de la suite générale (aucun Docker ni image), sélectionnés explicitement (``-m w5_image``) par le
job « Docker build », collectés et exécutés SANS saut (``scripts/ci_require_junit.py`` rejette tout test sauté ou manquant). Sans Docker ou
sans l'image, ils ÉCHOUENT (``DockerUnavailable``). **Non exécutés dans cette passe** (aucun Docker, aucun SDK local, A et B non intégrés) :
leur statut est « préparés », pas « prouvés » ; la liste des interfaces de A dont ils dépendent est ``INTERFACE_CONTRACT`` du harnais.

Échéance : la VIE du worker est mesurée contre l'échéance ABSOLUE persistée (horloge murale partagée, battement du worker toutes les 0,2 s),
séparément du temps de collecte et de nettoyage ; aucune tolérance de « grâce de travail » (voir ``SCHEDULING_TOLERANCE``).
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest
from w5_integration_harness import (
    HOSTILE,
    IMAGE,
    PROVIDER_KEY,
    SDK_WORKER,
    WORKER,
    FakeGoogle,
    HostStack,
    containers_of,
    declared_functions,
    google_response,
    heartbeat_epochs,
    parse_facts,
    parse_sdk_facts,
    require_docker_and_image,
    sdk_script,
    with_first_rejected,
)

pytestmark = pytest.mark.w5_image

DEADLINE = 20  # secondes de l'échéance globale persistée (ouverte avant le run)
TIMEOUT_NOTE = "délai dépassé"
PRIMARY, FALLBACK = "gemma-4-31b-it", "gemma-4-26b-a4b-it"
#: Tolérance d'ordonnancement entre l'échéance absolue et le DERNIER battement du worker : période du battement (0,2 s) + latence d'envoi du
#: signal / du KILL + arrondi de l'horloge. C'est une tolérance de MESURE, pas une grâce de travail : aucun délai TERM→KILL de 15 s, aucune marge
#: de watchdog n'y entre. Un worker qui vit plus de ce délai après l'échéance a TRAVAILLÉ au-delà d'elle : la preuve échoue.
SCHEDULING_TOLERANCE = 2.0
#: Borne de COLLECTE (retour du client docker, fermeture et consolidation du courtier, nettoyage) : distincte du temps de travail, volontairement large.
COLLECTION_BOUND = 30.0


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


def usage_of(stack):
    snap = stack.ledger.snapshot(stack.scope)
    return snap.consumed_tokens, snap.reserved_tokens, snap.unknown_tokens, bool(snap.blocked)


def assert_worker_life_bounded(stack, persisted, t_return, *, started_expected=True):
    """La VIE du worker (battements) ne dépasse pas l'échéance ABSOLUE ; la collecte est mesurée à part ; le processus est réellement mort."""
    deadline = persisted.timestamp()
    beats = heartbeat_epochs(stack.workspace)
    if not started_expected:
        assert beats == [] and not (stack.workspace / "started.txt").exists(), "le worker ne devait JAMAIS démarrer"
        return
    assert (stack.workspace / "started.txt").exists() and beats, "le worker a démarré"
    over = beats[-1] - deadline
    assert over <= SCHEDULING_TOLERANCE, (
        f"le worker a TRAVAILLÉ {over:.1f} s après l'échéance absolue (tolérance {SCHEDULING_TOLERANCE} s)"
    )
    assert beats[-1] >= deadline - 4.0, (
        "le worker n'a pas été arrêté bien avant l'échéance (aucun arrêt prématuré non expliqué)"
    )
    assert t_return - deadline <= COLLECTION_BOUND, (
        f"collecte trop longue : {t_return - deadline:.1f} s après l'échéance"
    )
    count = len(beats)
    time.sleep(1.5)
    assert len(heartbeat_epochs(stack.workspace)) == count, "le worker bat encore : le PROCESSUS n'est pas mort"


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
        assert counted["model"] == generated["model"] == PRIMARY
        assert generated["body"]["model"] == f"models/{PRIMARY}"
        assert (
            generated["body"]["generationConfig"]["candidateCount"] == 1
            and generated["body"]["generationConfig"]["maxOutputTokens"] > 0
        )
        assert declared_functions(generated["body"]), "les outils du SDK sont traduits en functionDeclarations"
    consumed, reserved, unknown, blocked = usage_of(stack)
    assert (consumed, reserved, unknown, blocked) == (15 * n, 0, 0, False), (
        "usage = REGISTRE, règlement exact des n générations"
    )
    assert (result.prompt_tokens, result.completion_tokens) == (10 * n, 5 * n) and result.usage_source == "broker"
    assert PROVIDER_KEY not in result.logs
    assert_nothing_left(stack)


def test_the_real_runner_falls_back_to_the_26b_only_after_an_established_refusal_with_known_usage(stacks):
    """Rejet DÉMONTRÉ du 31B (4xx avant traitement, réservation libérée) : le repli 26B est légitime, le reste de la session suit le 26B."""
    fake = FakeGoogle(script=with_first_rejected(sdk_script("/workspace/proof.txt")))
    stack = stacks(upstream=fake, extra_env={"OH_NUM_RETRIES": "1", "OH_LLM_TIMEOUT": "60"})
    result = stack.run()

    assert result.success, result.logs[-2000:]
    assert (stack.workspace / "proof.txt").read_text().strip() == "composed-proof"
    models = fake.models_seen
    assert models[0] == PRIMARY and len(models) >= 3 and set(models[1:]) == {FALLBACK}, models
    consumed, reserved, unknown, blocked = usage_of(stack)
    assert (consumed, reserved, unknown, blocked) == (15 * (len(models) - 1), 0, 0, False), (
        "le rejet est LIBÉRÉ, seules les générations réglées comptent"
    )
    assert_nothing_left(stack)


def test_the_real_runner_never_falls_back_nor_retries_after_an_ambiguous_timeout(stacks):
    """Requête 31B émise dont la réponse est perdue (timeout CLIENT, traitement serveur indéterminé) : ni repli 26B, ni retry du SDK, ni nouvelle émission."""
    fake = FakeGoogle(script=sdk_script("/workspace/proof.txt"))
    fake.gate = asyncio.Event()  # le fournisseur ne répond JAMAIS à la première génération
    fake.gate_first_n = 1
    stack = stacks(upstream=fake, extra_env={"OH_LLM_TIMEOUT": "5", "OH_NUM_RETRIES": "3"})
    result = stack.run()

    assert not result.success and result.usage_status == "incomplete", result.logs[-2000:]
    assert fake.models_seen == [PRIMARY], f"UNE seule émission, jamais de repli ni de retry : {fake.models_seen}"
    assert len(fake.count_calls) <= 1 + 0 or len(fake.generate_calls) == 1
    consumed, _reserved, unknown, blocked = usage_of(stack)
    assert consumed == 0 and unknown > 0 and blocked, (
        "la réservation de l'émission indéterminée reste conservée et bloquante"
    )
    assert not (stack.workspace / "proof.txt").exists()
    assert_nothing_left(stack, in_flight=True)


# ── 1 bis. le VRAI SDK (openhands.sdk.LLM) : formats, repli et retries contrôlés, concurrence ───────────────────────────────


def sdk_stack(stacks, fake, scenario, **extra):
    return stacks(upstream=fake, worker_source=SDK_WORKER, extra_env={"W5_SCENARIO": scenario, **extra})


def formats_script(body, **_kw):
    if body["generationConfig"].get("responseMimeType") == "application/json":
        return google_response('{"answer": 42}')
    return google_response("bonjour")


def test_the_real_sdk_gets_free_text_and_structured_json_through_the_real_broker(stacks):
    fake = FakeGoogle(script=formats_script)
    stack = sdk_stack(stacks, fake, "formats")
    facts = parse_sdk_facts(stack.run().logs)

    assert facts["text"] == {"ok": True, "text": "bonjour"}, facts
    assert facts["json_object"]["ok"] is True and json.loads(facts["json_object"]["text"]) == {"answer": 42}, facts
    assert len(fake.generate_calls) == 2
    assert "responseMimeType" not in fake.generate_calls[0]["body"]["generationConfig"], (
        "texte libre : aucune contrainte de format"
    )
    assert fake.generate_calls[1]["body"]["generationConfig"]["responseMimeType"] == "application/json", (
        "response_format traduit nativement"
    )
    assert usage_of(stack) == (30, 0, 0, False)
    assert_nothing_left(stack)


def test_the_real_sdk_may_use_the_fallback_after_an_established_refusal_and_the_registry_counts_only_the_settled_call(
    stacks,
):
    fake = FakeGoogle(script=with_first_rejected(lambda body, **_kw: google_response("repli")))
    stack = sdk_stack(stacks, fake, "fallback_after_rejection")
    facts = parse_sdk_facts(stack.run().logs)

    assert facts["primary"]["ok"] is False and facts["fallback"] == {"ok": True, "text": "repli"}, facts
    assert fake.models_seen == [PRIMARY, FALLBACK]
    assert usage_of(stack) == (15, 0, 0, False), (
        "le rejet est libéré ; la génération de repli est réglée une seule fois"
    )
    assert_nothing_left(stack)


@pytest.mark.parametrize("scenario", ["no_fallback_after_ambiguous_timeout", "same_model_retry_after_lost_response"])
def test_the_real_sdk_cannot_duplicate_or_divert_an_emission_whose_response_was_lost(stacks, scenario):
    fake = FakeGoogle()
    fake.gate = asyncio.Event()
    fake.gate_first_n = 1
    stack = sdk_stack(stacks, fake, scenario)
    facts = parse_sdk_facts(stack.run().logs)

    for label, value in facts.items():
        assert value["ok"] is False, (label, value)
    assert fake.models_seen == [PRIMARY], f"aucune seconde émission (repli ou retry du SDK) : {fake.models_seen}"
    consumed, _reserved, unknown, blocked = usage_of(stack)
    assert consumed == 0 and unknown > 0 and blocked, "réservation conservée et bloquante tant que l'usage est inconnu"
    assert_nothing_left(stack, in_flight=True)


def test_two_connections_of_one_session_cannot_overlap_a_fallback_but_another_role_keeps_working_concurrently(stacks):
    fake = FakeGoogle()
    fake.gate = asyncio.Event()
    fake.gate_first_n = 1  # seule la PREMIÈRE génération (le 31B du worker) est bloquée en vol
    stack = sdk_stack(stacks, fake, "concurrent_connections")
    planner = {}

    def other_role():
        deadline = time.monotonic() + 90
        while len(fake.generate_calls) < 1 and time.monotonic() < deadline:
            time.sleep(0.2)
        body = {"model": PRIMARY, "messages": [{"role": "user", "content": "planifier"}], "max_tokens": 64}
        try:
            planner["completion"] = asyncio.run(stack.service.sampling_completion(stack.scope, "planner", body))
        except BaseException as exc:  # noqa: BLE001
            planner["error"] = type(exc).__name__

    thread = threading.Thread(target=other_role, daemon=True)
    thread.start()
    facts = parse_sdk_facts(stack.run().logs)
    thread.join(timeout=60)

    assert facts["fallback_while_primary_in_flight"]["ok"] is False, (
        "le repli ne peut pas chevaucher une émission primaire indéterminée de la MÊME session"
    )
    assert "error" not in planner and planner["completion"]["choices"], (
        "la concurrence LÉGITIME entre rôles / allocations est conservée"
    )
    assert FALLBACK not in fake.models_seen and fake.models_seen.count(PRIMARY) == 2
    assert_nothing_left(stack, in_flight=True)


# ── 2. code hostile appelant le socket ───────────────────────────────────────────────────────────────────────────────────────


def test_hostile_code_cannot_widen_its_session_reach_the_internet_or_see_a_provider_key(stacks):
    fake = FakeGoogle()
    stack = stacks(upstream=fake, worker_source=HOSTILE)
    result = stack.run()
    facts = parse_facts(result.logs)

    refused = {
        "fallback_without_antecedent": {403, 409},
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
    assert set(fake.models_seen) == {PRIMARY}
    # isolation observée DEPUIS le conteneur
    assert facts["external_connections"] == [] and facts["interfaces"] == ["lo"]
    assert facts["secret_like_env"] == ["LLM_API_KEY"] and facts["llm_api_key_is_a_session_token"] is True
    assert facts["env_has_provider_key_value"] is False and facts["docker_socket_visible"] is False
    assert set(facts["mounts"]) <= {"/run/collegue-broker", "/workspace"}, facts["mounts"]
    assert PROVIDER_KEY not in result.logs
    assert usage_of(stack) == (30, 0, 0, False)
    assert_nothing_left(stack)


# ── 3. échéance persistée commune appliquée à la VIE du processus du worker ──────────────────────────────────────────────────


@pytest.mark.parametrize("behaviour", ["sleep_only", "sleep_after_call", "compute_after_call", "ignore_sigterm"])
def test_a_worker_without_a_new_call_is_dead_at_the_absolute_persisted_deadline_even_if_it_ignores_sigterm(
    stacks, behaviour
):
    fake = FakeGoogle()
    stack = stacks(upstream=fake, deadline=DEADLINE, worker_source=WORKER, extra_env={"W5_BEHAVIOUR": behaviour})
    stack.service.open_clock(stack.scope)  # le temps global a commencé AVANT ce run (planification / canaris)
    persisted = stack.service.persisted_deadline(stack.scope)
    assert persisted is not None

    result = stack.run(window=3600)  # la fenêtre LOCALE du run (une heure) n'est pas l'échéance
    t_return = time.time()

    assert not result.success and TIMEOUT_NOTE in result.logs, result.logs[-1500:]
    assert_worker_life_bounded(stack, persisted, t_return)
    consumed, reserved, unknown, blocked = usage_of(stack)
    if behaviour == "sleep_only":
        assert (consumed, reserved, unknown) == (0, 0, 0) and fake.generate_calls == []
    else:
        assert (consumed, reserved, unknown, blocked) == (15, 0, 0, False) and len(fake.generate_calls) == 1
    assert_nothing_left(stack)


@pytest.mark.parametrize("setup_delay", [10])
def test_a_late_setup_does_not_extend_the_absolute_deadline_of_a_worker_that_still_starts(stacks, setup_delay):
    """Le socket du courtier est prêt avec ``setup_delay`` s de retard : le délai du conteneur est celui du RESTANT au lancement, pas calculé avant."""
    fake = FakeGoogle()
    stack = stacks(
        upstream=fake,
        deadline=DEADLINE,
        worker_source=WORKER,
        extra_env={"W5_BEHAVIOUR": "sleep_after_call"},
        setup_delay=setup_delay,
    )
    stack.service.open_clock(stack.scope)
    persisted = stack.service.persisted_deadline(stack.scope)
    stack.run(window=3600)
    t_return = time.time()
    assert_worker_life_bounded(stack, persisted, t_return)
    assert_nothing_left(stack)


def test_a_setup_that_ends_after_the_deadline_never_starts_the_worker(stacks):
    fake = FakeGoogle()
    stack = stacks(
        upstream=fake,
        deadline=DEADLINE,
        worker_source=WORKER,
        extra_env={"W5_BEHAVIOUR": "sleep_after_call"},
        setup_delay=DEADLINE + 5,
    )
    stack.service.open_clock(stack.scope)
    persisted = stack.service.persisted_deadline(stack.scope)
    from collegue.sandbox.executor import SandboxRefused
    from collegue.state import BudgetRefused

    try:
        result = stack.run(window=3600)
        refused = not result.success
    except (SandboxRefused, BudgetRefused):
        refused = True
    assert refused, "un worker dont la préparation dépasse l'échéance persistée ne doit PAS être lancé"
    assert_worker_life_bounded(stack, persisted, time.time(), started_expected=False)
    assert fake.generate_calls == []
    assert_nothing_left(stack)


@pytest.mark.parametrize("slow_start", [8])
def test_a_slow_container_start_does_not_extend_the_absolute_deadline(stacks, slow_start):
    """Le VRAI docker démarre le conteneur avec ``slow_start`` s de retard (enveloppeur) : la supervision porte l'échéance ABSOLUE, pas un délai relatif."""
    fake = FakeGoogle()
    stack = stacks(
        upstream=fake,
        deadline=DEADLINE,
        worker_source=WORKER,
        extra_env={"W5_BEHAVIOUR": "ignore_sigterm"},
        slow_docker_start=slow_start,
    )
    stack.service.open_clock(stack.scope)
    persisted = stack.service.persisted_deadline(stack.scope)
    stack.run(window=3600)
    t_return = time.time()
    assert_worker_life_bounded(stack, persisted, t_return)
    assert_nothing_left(stack)


def test_a_request_in_flight_when_the_deadline_stops_the_worker_stays_reserved_as_unknown(stacks):
    fake = FakeGoogle()
    fake.gate = asyncio.Event()  # le fournisseur ne répond JAMAIS : requête émise, worker bloqué en vol
    stack = stacks(
        upstream=fake, deadline=DEADLINE, close_wait=1.0, worker_source=WORKER, extra_env={"W5_BEHAVIOUR": "quick"}
    )
    stack.service.open_clock(stack.scope)
    persisted = stack.service.persisted_deadline(stack.scope)

    result = stack.run(window=3600)
    t_return = time.time()

    assert not result.success and result.usage_status == "incomplete" and result.usage_source == "broker"
    assert_worker_life_bounded(stack, persisted, t_return)
    consumed, _reserved, unknown, blocked = usage_of(stack)
    assert blocked and unknown > 0 and consumed == 0, "réservation CONSERVÉE, jamais libérée"
    assert len(fake.generate_calls) == 1, "une seule émission, aucune autre ensuite"
    assert_nothing_left(stack, in_flight=True)


def test_no_new_generation_or_worker_starts_after_the_persisted_deadline(stacks):
    from collegue.sandbox.executor import SandboxRefused
    from collegue.state import BudgetRefused

    fake = FakeGoogle()
    stack = stacks(upstream=fake, deadline=DEADLINE, worker_source=WORKER, extra_env={"W5_BEHAVIOUR": "quick"})
    stack.service.open_clock(stack.scope)
    persisted = stack.service.persisted_deadline(stack.scope)
    time.sleep(max(0.0, (persisted - datetime.now(timezone.utc)).total_seconds() + 1))

    before = len(fake.generate_calls)
    try:
        refused = not stack.run(window=3600).success
    except (SandboxRefused, BudgetRefused):
        refused = True
    assert refused, "un worker ne doit plus démarrer après l'échéance persistée"
    assert len(fake.generate_calls) == before == 0, "aucune génération après l'échéance"
    assert datetime.now(timezone.utc) > persisted + timedelta(seconds=0)
    assert_worker_life_bounded(stack, persisted, time.time(), started_expected=False)
    assert_nothing_left(stack)
