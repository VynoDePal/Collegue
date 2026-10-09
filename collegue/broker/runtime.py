"""Runtime du courtier d'un processus : configuration de confiance, services par registre, attachement des workers.

``BrokerRuntime`` est le SEUL objet qui touche la clé Google (via ``GoogleUpstream``). Il fabrique un :class:`BrokerService`
par registre budgétaire et, pour chaque allocation de worker, une session + un socket Unix + un sandbox raccordé dont la
capacité est PROUVÉE avant le lancement (:mod:`collegue.broker.capability`).

Aucun réglage, aucune variable d'environnement ne désactive une garde : ``from_settings`` refuse toute configuration hors du
contrat W5 (fournisseur Google, deux Gemma officiels). Les tests injectent un faux fournisseur en construisant
``BrokerRuntime(upstream=...)`` directement ; il n'existe aucun drapeau de production qui saute une garde.
"""

from __future__ import annotations

import asyncio
import math
import threading
import weakref
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterator, Optional, Tuple

from collegue.broker.capability import TransportProof, prove_worker_transport
from collegue.broker.errors import BrokerForbidden
from collegue.broker.policy import FALLBACK_MODEL, PRIMARY_MODEL
from collegue.broker.server import BrokerSocketServer
from collegue.broker.service import BrokerConfig, BrokerService, OpenedSession, SessionSummary
from collegue.broker.upstream import GoogleUpstream, Upstream

TRANSPORT_DIRECT = "direct"
TRANSPORT_BROKER = "budget_broker"


class BrokerConfigurationError(ValueError):
    """Configuration hors du contrat W5 (fournisseur, modèle, repli, transport) : refusée avant tout lancement."""


def _secret(value: Any) -> str:
    reveal = getattr(value, "get_secret_value", None)
    return str(reveal() if callable(reveal) else (value or ""))


def transport_of(settings: Any) -> str:
    """Transport configuré (``direct`` par défaut) ; toute autre valeur que les deux connues est refusée."""
    raw = str(getattr(settings, "LLM_TRANSPORT", "") or TRANSPORT_DIRECT).strip().lower()
    if raw not in (TRANSPORT_DIRECT, TRANSPORT_BROKER):
        raise BrokerConfigurationError(f"LLM_TRANSPORT={raw!r} inconnu (valeurs : direct, budget_broker)")
    return raw


def is_broker_mode(settings: Any) -> bool:
    return transport_of(settings) == TRANSPORT_BROKER


def _run_sync(coro):
    """Exécute une coroutine depuis du code synchrone, y compris sous une boucle déjà active (autre thread)."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


@dataclass
class AttachedWorker:
    """Session ouverte + sandbox raccordé + preuve de transport ; ``summary`` est renseigné à la fermeture."""

    sandbox: Any
    session: OpenedSession
    proof: TransportProof
    service: BrokerService
    summary: Optional[SessionSummary] = None
    server: Optional[BrokerSocketServer] = field(default=None, repr=False)
    # Délai effectif (secondes) du conteneur : min(allocation, échéance globale persistée − maintenant) ; ``None`` = non borné.
    timeout_seconds: Optional[float] = None


class BrokerRuntime:
    def __init__(
        self,
        *,
        upstream: Upstream,
        config: Optional[BrokerConfig] = None,
        run_root: Optional[str] = None,
        provider_keys: Callable[[], Tuple[str, ...]] = lambda: (),
    ):
        self._upstream = upstream
        self.config = config or BrokerConfig()
        self.run_root = run_root
        self._provider_keys = provider_keys
        self._services: Dict[int, Tuple[Any, BrokerService]] = {}
        self._lock = threading.Lock()

    # ── construction ─────────────────────────────────────────────────────────────────────────────────────

    @classmethod
    def from_settings(cls, settings: Any, *, run_root: Optional[str] = None) -> "BrokerRuntime":
        """Runtime de production : client Google natif, clé lue UNIQUEMENT ici (``LLM_API_KEY`` du fournisseur ``gemini``)."""
        validate_broker_settings(settings)
        timeout = float(getattr(settings, "BROKER_UPSTREAM_TIMEOUT", 120.0) or 120.0)

        def key() -> str:
            return _secret(getattr(settings, "LLM_API_KEY", ""))

        config = BrokerConfig(
            global_deadline_seconds=int(getattr(settings, "BROKER_GLOBAL_DEADLINE_SECONDS", 0) or 0),
            max_output_tokens=int(getattr(settings, "BROKER_MAX_OUTPUT_TOKENS", 8192) or 8192),
            default_output_tokens=int(getattr(settings, "BROKER_MAX_OUTPUT_TOKENS", 8192) or 8192),
        )
        return cls(
            upstream=GoogleUpstream(key, timeout=timeout),
            config=config,
            run_root=run_root or (str(getattr(settings, "BROKER_RUN_DIR", "") or "") or None),
            provider_keys=lambda: (key(),) if key() else (),
        )

    def provider_keys(self) -> Tuple[str, ...]:
        return tuple(self._provider_keys())

    def service_for(self, ledger: Any) -> BrokerService:
        """Un service par registre budgétaire (même base ⇒ mêmes sessions, mêmes tentatives, même horloge globale)."""
        with self._lock:
            held = self._services.get(id(ledger))
            if held is None or held[0] is not ledger:
                held = (ledger, BrokerService(ledger, self._upstream, config=self.config))
                self._services[id(ledger)] = held
            return held[1]

    # ── attachement d'un worker ──────────────────────────────────────────────────────────────────────────

    @contextmanager
    def attach_worker(self, *, ledger: Any, allocation: Any, role: str, sandbox: Any) -> Iterator[AttachedWorker]:
        """Ouvre la session SUR la réservation parent, lie un socket, raccorde le sandbox et PROUVE l'isolation.

        Sortie normale : fermeture + consolidation dans la réservation parent (autorité = le courtier). Sortie par exception :
        fermeture SANS toucher au parent, qui reste à l'appelant (``release`` si rien n'a été lancé, ``mark_unknown`` si le
        worker a été interrompu) — exactement la sémantique de la vague 2.
        """
        service = self.service_for(ledger)
        deadline = None
        if getattr(allocation, "deadline_epoch", None) is not None:
            deadline = datetime.fromtimestamp(float(allocation.deadline_epoch), tz=timezone.utc)
        # Échéance globale PERSISTÉE : ouverte ici si elle ne l'est pas encore (lancement réel du worker), jamais déplacée, et
        # elle borne TOUJOURS la session et le délai du conteneur — quelle que soit la fenêtre locale du run en cours.
        persisted = service.open_clock(allocation.scope_key)
        if persisted is not None:
            deadline = persisted if deadline is None else min(deadline, persisted)
        timeout_seconds = None
        if deadline is not None:
            # Entier INFÉRIEUR : le sandbox arrondit au supérieur, le délai du conteneur ne dépasse donc jamais l'échéance.
            timeout_seconds = float(math.floor((deadline - datetime.now(timezone.utc)).total_seconds()))
            if timeout_seconds < 1:
                from collegue.sandbox.executor import SandboxRefused

                # Rien n'a été lancé : l'appelant libère la réservation parent (sémantique « worker non lancé »).
                raise SandboxRefused("échéance globale persistée atteinte avant le lancement : worker non lancé")
        opened = service.open_session(
            parent_scope_key=allocation.scope_key,
            parent_reservation_id=allocation.reservation_id,
            role=role,
            deadline=deadline,
        )
        server: Optional[BrokerSocketServer] = None
        attached: Optional[AttachedWorker] = None
        try:
            server = BrokerSocketServer(service, opened.session_id, run_root=self.run_root).start()
            derived = sandbox.with_broker(
                server.directory,
                env={"OH_MAX_OUTPUT_TOKENS": str(opened.max_output_tokens)},
                env_secrets={"LLM_API_KEY": opened.token},  # jeton de SESSION, jamais une clé fournisseur
            )
            proof = prove_worker_transport(derived, provider_keys=self.provider_keys(), session_dir=server.directory)
            if not proof.ok:
                raise BrokerForbidden(f"transport du worker non prouvé : {proof.failures}", code="transport_unproven")
            attached = AttachedWorker(
                sandbox=derived,
                session=opened,
                proof=proof,
                service=service,
                server=server,
                timeout_seconds=timeout_seconds,
            )
        except BaseException:
            self._close(service, opened, consolidate=False)
            if server is not None:
                server.stop()
            raise
        try:
            yield attached
        except BaseException:
            attached.summary = self._close(service, opened, consolidate=False)
            raise
        else:
            attached.summary = self._close(service, opened, consolidate=True)
        finally:
            server.stop()

    @staticmethod
    def _close(service: BrokerService, opened: OpenedSession, *, consolidate: bool) -> Optional[SessionSummary]:
        try:
            return _run_sync(
                service.close_session(
                    opened.session_id,
                    "worker terminé" if consolidate else "worker interrompu",
                    consolidate_parent=consolidate,
                )
            )
        except BaseException:  # noqa: BLE001 - la fermeture ne masque jamais l'erreur d'origine ; le parent reste réservé
            if consolidate:
                raise
            return None


def validate_broker_settings(settings: Any) -> None:
    """Contrat W5 : destination sémantique Google, deux Gemma officiels, repli du codeur = le 26B seulement."""
    if not is_broker_mode(settings):
        raise BrokerConfigurationError("LLM_TRANSPORT n'est pas 'budget_broker'")
    provider = str(getattr(settings, "LLM_PROVIDER", "") or "gemini").strip().lower()
    if provider != "gemini":
        raise BrokerConfigurationError(f"le courtier W5 ne sert que Google (LLM_PROVIDER=gemini), pas {provider!r}")
    from collegue.broker.policy import canonical_model

    names = {"LLM_MODEL": getattr(settings, "LLM_MODEL", "")}
    for role in ("CODER", "QA", "REVIEWER", "PLANNER"):
        value = getattr(settings, f"LLM_MODEL_{role}", None)
        if value:
            names[f"LLM_MODEL_{role}"] = value
        role_provider = str(getattr(settings, f"LLM_PROVIDER_{role}", "") or "").strip().lower()
        if role_provider and role_provider != "gemini":
            raise BrokerConfigurationError(f"LLM_PROVIDER_{role}={role_provider!r} : seul Google passe par le courtier")
    for label, value in names.items():
        try:
            canonical_model(str(value))
        except ValueError as exc:
            raise BrokerConfigurationError(f"{label}: {exc}") from None
    for role in ("QA", "REVIEWER", "PLANNER"):
        value = getattr(settings, f"LLM_MODEL_{role}", None)
        if value and canonical_model(str(value)) != PRIMARY_MODEL:
            raise BrokerConfigurationError(f"LLM_MODEL_{role}: le repli {FALLBACK_MODEL} est réservé au codeur")
    fallbacks = [m.strip() for m in str(getattr(settings, "CODER_FALLBACK_MODELS", "") or "").split(",") if m.strip()]
    for name in fallbacks:
        try:
            ok = canonical_model(name) == FALLBACK_MODEL
        except ValueError:
            ok = False
        if not ok:
            raise BrokerConfigurationError(
                f"CODER_FALLBACK_MODELS: seul {FALLBACK_MODEL} est autorisé en repli ({name!r})"
            )
    for flag in ("CODER_SUBSCRIPTION",):
        if getattr(settings, flag, False):
            raise BrokerConfigurationError(f"{flag} est incompatible avec le courtier (aucun mode de substitution)")
    for role in ("CODER", "QA", "REVIEWER", "PLANNER"):
        if str(getattr(settings, f"LLM_AUTH_{role}", "") or "").strip().lower() not in ("", "api_key"):
            raise BrokerConfigurationError(f"LLM_AUTH_{role}: seule la clé API (portée par le courtier) est admise")
        if getattr(settings, f"LLM_BASE_URL_{role}", None):
            raise BrokerConfigurationError(f"LLM_BASE_URL_{role}: l'endpoint Google est fixe en mode courtier")
    if getattr(settings, "LLM_BASE_URL", None):
        raise BrokerConfigurationError("LLM_BASE_URL: l'endpoint Google est fixe en mode courtier")


# ── runtime des producteurs en processus (planner, QA, reviewer…) ──────────────────────────────────────────────

_RUNTIMES: "weakref.WeakKeyDictionary[Any, BrokerRuntime]" = weakref.WeakKeyDictionary()
_RUNTIMES_BY_ID: Dict[int, BrokerRuntime] = {}
_installed: Optional[BrokerRuntime] = None


def install_runtime_for_tests(runtime: Optional[BrokerRuntime]) -> None:
    """RÉSERVÉ AUX TESTS : impose ``runtime`` (faux fournisseur) à tous les producteurs. ``None`` rétablit le défaut.

    Ce n'est ni un réglage ni une variable d'environnement : seul du code de test peut l'appeler.
    """
    global _installed
    _installed = runtime


def runtime_for(settings: Any) -> BrokerRuntime:
    """Runtime des producteurs en processus pour ``settings`` (un par objet de réglages)."""
    if _installed is not None:
        return _installed
    try:
        found = _RUNTIMES.get(settings)
        if found is None:
            found = BrokerRuntime.from_settings(settings)
            _RUNTIMES[settings] = found
        return found
    except TypeError:  # objet non référençable faiblement
        found = _RUNTIMES_BY_ID.get(id(settings))
        if found is None:
            found = BrokerRuntime.from_settings(settings)
            _RUNTIMES_BY_ID[id(settings)] = found
        return found


async def qualify_models(settings: Any, ledger: Any, scope_key: str):
    """Qualification des DEUX modèles sur un scope existant (voir :meth:`BrokerService.qualify_models`).

    Ouvre l'échéance globale à la première émission réelle, avec le runtime de production du processus. Le rapport
    (``QualificationReport``) ne contient aucun secret ; ``ok`` est faux au premier refus / à la première ambiguïté.
    """
    return await runtime_for(settings).service_for(ledger).qualify_models(scope_key)
