"""Service de confiance du courtier budgétaire (W5) : l'UNIQUE chemin de génération Gemma en mode ``budget_broker``.

Flux d'une génération (``_execute``), identique pour une session de worker et pour un producteur en processus ::

    1. valider la requête (JSON strict, liste blanche, UNE limite de sortie) → objet Google normalisé
    2. vérifier les échéances (globale persistée, session) et admettre l'appel (session ouverte, ``in_flight`` + 1)
    3. tentative ``prepared`` (idempotente sur ``request_id``)
    4. ``countTokens(generateContentRequest complet)`` — aucune dépense, aucune estimation de remplacement
    5. réservation ``prompt + sortie max`` dans le scope (enfant pour un worker, global sinon) — refus ⇒ rien n'est émis
    6. échéances revérifiées, puis ``emitting`` ÉCRIT ET COMMITÉ avant l'envoi
    7. ``generateContent`` avec le MÊME objet
    8. règlement : usage validé ⇒ ``settled`` + commit ; rejet démontré ⇒ ``released`` ; tout le reste ⇒ ``unknown``
       (réservation conservée, scope enfant ET réservation parent bloqués : le projet est bloqué immédiatement)

L'ordre « état durable PUIS registre » fait que tout crash est réparable de façon déterministe (:meth:`repair`) : une
tentative ``prepared`` n'a PROUVABLEMENT rien émis (libérée), une tentative ``emitting`` est d'usage inconnu (jamais
rejouée, jamais libérée sans preuve).

Les droits (rôle, modèles, plafond de sortie, échéance) viennent de la session enregistrée CÔTÉ SERVEUR ; rien de ce que
soumet le client ne les élargit. La clé Google n'existe que dans l'objet ``upstream``.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import secrets
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Optional, Tuple, Union

from collegue.broker.errors import (
    BrokerAuthError,
    BrokerBlocked,
    BrokerBoundViolation,
    BrokerBudgetRefused,
    BrokerError,
    BrokerForbidden,
    BrokerRequestRefused,
    BrokerUnsupported,
    BrokerUpstreamAmbiguous,
    BrokerUpstreamRejected,
)
from collegue.broker.policy import (
    DEFAULT_MAX_OUTPUT_TOKENS,
    FALLBACK_MODEL,
    MAX_OUTPUT_TOKENS_CEILING,
    MAX_REQUEST_BYTES,
    OFFICIAL_MODELS,
    ROLES,
    models_for_role,
)
from collegue.broker.store import FALLBACK_REFUSAL_PREFIX, AttemptRecord, BrokerStore, SessionRecord, owner_is_alive
from collegue.broker.translate import (
    NormalizedRequest,
    canonical_json,
    normalize_chat_request,
    parse_count_tokens,
    parse_json_strict,
    translate_response,
)
from collegue.broker.upstream import (
    Upstream,
    UpstreamBadBody,
    UpstreamHTTPError,
    UpstreamTransportError,
)
from collegue.state.budget_ledger import (
    BLOCK_BOUND_VIOLATION,
    REFUSED_BLOCKED,
    BudgetLedger,
    BudgetLedgerError,
    BudgetRefused,
)
from collegue.state.models import (
    BROKER_ATTEMPT_EMITTING,
    BROKER_ATTEMPT_PREPARED,
    BROKER_ATTEMPT_RELEASED,
    BROKER_ATTEMPT_SETTLED,
    BROKER_ATTEMPT_UNKNOWN,
    BROKER_SESSION_CLOSED,
)

# Rejets AVANT traitement (même ensemble que ``budget_guard`` de la vague 2) : le fournisseur n'a rien exécuté.
PROVEN_REJECTED_STATUS = frozenset({400, 401, 403, 404, 405, 413, 415, 422, 429})
TRANSPORT = "broker"

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class BrokerConfig:
    """Réglages du service (jamais de secret). ``global_deadline_seconds`` : 0 = aucune échéance globale."""

    global_deadline_seconds: int = 0
    default_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS
    max_output_tokens: int = MAX_OUTPUT_TOKENS_CEILING
    max_request_bytes: int = MAX_REQUEST_BYTES
    close_wait_seconds: float = 30.0
    reservation_ttl_seconds: float = 600.0


@dataclass(frozen=True)
class OpenedSession:
    """Résultat de ``open_session``. ``token`` n'est visible QU'ICI (seul son hash est stocké) et ne vaut pas clé fournisseur."""

    session_id: str
    token: str = field(repr=False)
    scope_key: str
    role: str
    allowed_models: Tuple[str, ...]
    max_output_tokens: int
    deadline_at: Optional[datetime]

    def __repr__(self) -> str:  # le jeton n'apparaît jamais dans un journal
        return f"OpenedSession(session_id={self.session_id!r}, role={self.role!r}, scope={self.scope_key!r})"


@dataclass(frozen=True)
class SessionSummary:
    """Consommation ÉTABLIE d'une session close — la seule autorité (jamais les journaux de l'agent)."""

    session_id: str
    state: str
    consumed_tokens: int
    consumed_micro_usd: int
    unknown: bool
    unknown_reason: Optional[str]
    blocked_reason: Optional[str]
    attempts: int
    parent_settlement: str  # "committed" | "unknown" | "none"
    prompt_tokens: int = 0
    completion_tokens: int = 0  # candidats + raisonnement

    @property
    def prompt_completion_known(self) -> bool:
        return not self.unknown


class BrokerService:
    """Service transactionnel du courtier. Aucun état de budget en mémoire ; sûr entre threads et boucles asyncio."""

    def __init__(
        self,
        ledger: BudgetLedger,
        upstream: Upstream,
        *,
        config: Optional[BrokerConfig] = None,
        clock: Optional[Callable[[], datetime]] = None,
    ):
        self.ledger = ledger
        self.store = BrokerStore(ledger)
        self._upstream = upstream
        self.config = config or BrokerConfig()
        self._clock = clock or _utcnow
        # Identité de CETTE instance (processus) : propriétaire de ses tentatives en vol. Une tentative d'un propriétaire
        # vivant n'est jamais réparée par un autre ; celle d'un propriétaire disparu (ou sans propriétaire) l'est.
        self.owner_id = "own_" + uuid.uuid4().hex[:24]
        self._owner_registered = False
        self._local_inflight = 0

    def _now(self) -> datetime:
        return self._clock()

    def _ensure_owner(self) -> str:
        if not self._owner_registered:
            self.store.register_owner(self.owner_id, self._now())
            self._owner_registered = True
        return self.owner_id

    def shutdown(self) -> None:
        """Arrêt propre : cette instance n'a plus aucune tentative vivante (ses éventuels restes sont réparables)."""
        if self._owner_registered:
            self.store.end_owner(self.owner_id, self._now())

    # ── sessions ─────────────────────────────────────────────────────────────────────────────────────────

    def open_session(
        self,
        *,
        parent_scope_key: str,
        parent_reservation_id: str,
        role: str,
        deadline: Optional[datetime] = None,
        max_output_tokens: Optional[int] = None,
    ) -> OpenedSession:
        """Active une session SUR une réservation ``worker`` déjà prise. Les droits sont ceux du RÔLE, fixés ici."""
        name = str(role).strip().lower()
        if name not in ROLES:
            raise BrokerForbidden(f"rôle inconnu : {role!r}", code="role_unknown")
        cap = int(max_output_tokens or min(self.config.default_output_tokens, self.config.max_output_tokens))
        if not 0 < cap <= self.config.max_output_tokens:
            raise BrokerForbidden("plafond de sortie de session hors des limites du serveur", code="invalid_cap")
        session_id = "bks_" + uuid.uuid4().hex[:24]
        token = "cbk_" + secrets.token_urlsafe(32)
        try:
            record = self.store.open_session(
                session_id=session_id,
                token_sha256=hashlib.sha256(token.encode()).hexdigest(),
                role=name,
                parent_scope_key=parent_scope_key,
                parent_reservation_id=parent_reservation_id,
                allowed_models=models_for_role(name),
                max_output_tokens=cap,
                deadline_at=deadline,
                owner_id=self._ensure_owner(),
            )
        except BudgetRefused as exc:  # BaseException du registre : traduit en erreur du courtier, jamais propagé nu
            if exc.code == REFUSED_BLOCKED:
                raise BrokerBlocked(str(exc)) from None
            raise BrokerBudgetRefused(str(exc), code=f"budget_{exc.code}") from None
        return OpenedSession(
            session_id=record.session_id,
            token=token,
            scope_key=record.scope_key,
            role=record.role,
            allowed_models=record.allowed_models,
            max_output_tokens=record.max_output_tokens,
            deadline_at=record.deadline_at,
        )

    def authenticate(self, session_id: str, token: str) -> SessionRecord:
        """Le jeton ouvre CETTE session et aucune autre ; une session fermée n'authentifie plus rien."""
        expected = self.store.session_token_hash(session_id)
        supplied = hashlib.sha256(str(token or "").encode()).hexdigest()
        if expected is None or not hmac.compare_digest(expected, supplied):
            raise BrokerAuthError("jeton de session invalide")
        record = self.store.get_session(session_id)
        if record is None or record.state == BROKER_SESSION_CLOSED:
            raise BrokerForbidden("session fermée", code="session_closed")
        return record

    # ── génération : session de worker ───────────────────────────────────────────────────────────────────

    async def chat_completion(
        self, session_id: str, token: str, body: Union[bytes, dict], *, request_id: Optional[str] = None
    ) -> dict:
        """Génération d'un worker : authentifie, puis exécute SOUS les droits enregistrés de la session."""
        session = self.authenticate(session_id, token)
        return await self._execute(
            scope_key=session.scope_key,
            root_scope_key=session.parent_scope_key or session.scope_key,
            role=session.role,
            session=session,
            allowed_models=session.allowed_models,
            max_output_tokens=session.max_output_tokens,
            body=body,
            request_id=request_id,
        )

    # ── génération : producteurs en processus (planner, QA, reviewer…) ───────────────────────────────────

    async def sampling_completion(
        self, scope_key: str, role: str, body: Union[bytes, dict], *, request_id: Optional[str] = None
    ) -> dict:
        """Génération d'un producteur hors worker dans le scope GLOBAL du projet (``scope_key`` vient du contexte lié)."""
        name = str(role).strip().lower()
        if name not in ROLES:
            raise BrokerForbidden(f"rôle inconnu : {role!r}", code="role_unknown")
        return await self._execute(
            scope_key=scope_key,
            root_scope_key=scope_key,
            role=name,
            session=None,
            allowed_models=models_for_role(name),
            max_output_tokens=min(self.config.max_output_tokens, MAX_OUTPUT_TOKENS_CEILING),
            body=body,
            request_id=request_id,
        )

    # ── noyau ────────────────────────────────────────────────────────────────────────────────────────────

    def _normalize(self, body: Union[bytes, dict], allowed: Tuple[str, ...], cap: int) -> NormalizedRequest:
        if isinstance(body, dict):
            try:
                raw = canonical_json(body).encode("utf-8")
            except (TypeError, ValueError) as exc:
                raise BrokerRequestRefused(
                    f"requête non sérialisable en JSON strict : {exc}", code="invalid_json"
                ) from exc
        else:
            raw = bytes(body)
        try:
            payload = parse_json_strict(raw, max_bytes=self.config.max_request_bytes)
            return normalize_chat_request(
                payload,
                allowed_models=allowed,
                default_output_tokens=min(self.config.default_output_tokens, cap),
                max_output_tokens=cap,
            )
        except BrokerError as exc:
            # Trace DIAGNOSTIQUE d'un refus AVANT toute tentative (rien n'est journalisé en base pour lui) : code et motif seulement
            # (noms de champs, jamais le contenu de la requête, jamais un secret), bornés. Un client qui n'affiche que la classe
            # de l'exception (ex. ``LLMBadRequestError``) laisse ainsi la cause lisible dans le journal du service.
            logger.warning("courtier : requête refusée avant tentative [%s] %.300s", getattr(exc, "code", "?"), exc)
            raise

    def _check_global_deadline(self, root_scope_key: str, now: datetime) -> None:
        deadline = self.store.clock_deadline(root_scope_key)
        if deadline is not None and now >= deadline:
            raise BrokerForbidden("échéance globale atteinte : aucune nouvelle génération", code="global_deadline")

    def _open_global_clock(self, root_scope_key: str, now: datetime) -> None:
        """Première ouverture RÉELLE du fournisseur pour ce scope racine : l'échéance globale démarre ici, une seule fois."""
        if self.config.global_deadline_seconds > 0:
            self.store.ensure_clock(root_scope_key, self.config.global_deadline_seconds, now)

    async def _execute(
        self,
        *,
        scope_key: str,
        root_scope_key: str,
        role: str,
        session: Optional[SessionRecord],
        allowed_models: Tuple[str, ...],
        max_output_tokens: int,
        body: Union[bytes, dict],
        request_id: Optional[str],
    ) -> dict:
        nr = self._normalize(body, allowed_models, max_output_tokens)
        now = self._now()
        self._ensure_owner()
        # Les restes d'un processus disparu (tentative en vol, réserve orpheline) sont réparés — et leurs inconnues bloquent —
        # AVANT toute nouvelle émission sur ces scopes.
        self.repair(scope_keys=tuple(dict.fromkeys((scope_key, root_scope_key))), sweep=False)
        self._check_global_deadline(root_scope_key, now)
        admitted = False
        if session is not None:
            self.store.begin_call(session.session_id, now)
            admitted = True
        self._local_inflight += 1
        try:
            return await self._generate(
                nr,
                scope_key=scope_key,
                root_scope_key=root_scope_key,
                role=role,
                session=session,
                request_id=request_id,
            )
        finally:
            self._local_inflight -= 1
            if admitted:
                self.store.end_call(session.session_id)

    async def _generate(
        self,
        nr: NormalizedRequest,
        *,
        scope_key: str,
        root_scope_key: str,
        role: str,
        session: Optional[SessionRecord],
        request_id: Optional[str],
    ) -> dict:
        reopened: Optional[AttemptRecord] = None
        if request_id is not None:
            known = self.store.find_attempt(scope_key, request_id)
            if known is not None:
                if known.request_sha256 != nr.sha256:
                    raise BrokerRequestRefused(
                        "request_id réutilisé avec un contenu différent : un identifiant d'idempotence ne change pas de sens",
                        code="request_id_conflict",
                        status=409,
                    )
                if known.state != BROKER_ATTEMPT_RELEASED:
                    return self._replay(
                        known
                    )  # AVANT tout contrôle de blocage : un résultat déjà obtenu se rend tel quel
                reopened = known
        self._refuse_if_blocked(scope_key, session, root_scope_key)
        if reopened is not None:
            # Libérée = absence d'émission ÉTABLIE : le renvoi du même request_id est une NOUVELLE exécution légitime.
            if not self.store.reopen_released(reopened.attempt_id, owner_id=self.owner_id):
                return self._replay(self.store.get_attempt(reopened.attempt_id))
        attempt_id = f"{session.session_id if session else 'direct'}:{uuid.uuid4().hex[:16]}"
        if reopened is not None:
            attempt = self.store.get_attempt(reopened.attempt_id)
        else:
            attempt, replayed = self.store.create_attempt(
                attempt_id=attempt_id,
                request_id=request_id,
                session_id=session.session_id if session else None,
                scope_key=scope_key,
                role=role,
                model=nr.model,
                request_sha256=nr.sha256,
                output_cap=nr.output_cap,
                owner_id=self.owner_id,
            )
            if replayed:
                return self._replay(attempt)
        attempt_id = attempt.attempt_id

        # 3 bis. PRÉCONTRÔLE local du droit de séquence, AVANT toute requête fournisseur : un repli sans antécédent ou une nouvelle
        # génération sur une session occupée est refusé sans countTokens, sans réserve (rien n'est pris) et sans toucher à l'usage connu.
        # Ce n'est qu'un précontrôle : l'admission transactionnelle APRÈS countTokens (étape 6) reste obligatoire et décisive.
        if session is not None:
            early_code, early_detail = self.store.sequence_refusal(attempt_id, session.session_id)
            if early_code:
                self._raise_sequence_refusal(attempt, early_code, early_detail)

        # 4. countTokens — aucune dépense ; n'importe quel échec ici ne peut PAS avoir généré.
        self._open_global_clock(root_scope_key, self._now())
        try:
            counted = parse_count_tokens(await self._upstream.count_tokens(nr))
        except BrokerError as exc:
            self._release(attempt, exc.code, str(exc))
            raise
        except (UpstreamHTTPError, UpstreamTransportError, UpstreamBadBody) as exc:
            detail = f"countTokens indisponible ({type(exc).__name__})"
            self._release(attempt, "count_tokens_failed", detail)
            retry = isinstance(exc, UpstreamHTTPError) and exc.status == 429
            raise BrokerUnsupported(detail, code="count_tokens_failed", status=429 if retry else 502) from None

        # 5. réservation ATOMIQUE dans le scope concerné (CAS du registre) — refus ⇒ rien n'est émis.
        reserve_tokens = counted + nr.output_cap
        reservation_id = self._rid(
            attempt
        )  # déterministe : retrouvable par la réparation même si on meurt avant la suite
        # L'identifiant est ÉCRIT dans la tentative AVANT de réserver : un crash entre la réservation et la suite laisse une
        # tentative qui désigne sa réserve (jamais une réserve orpheline). Un échec du CAS = la tentative a changé d'état
        # (fermeture, réparation, autre exécution) : on ne réserve rien.
        if not self.store.attach_reservation(
            attempt_id, reservation_id=reservation_id, counted_tokens=counted, reserved_tokens=reserve_tokens
        ):
            raise BrokerBlocked("tentative reprise par une autre exécution : aucune réservation", code="attempt_taken")
        attempt = self.store.get_attempt(attempt_id)
        try:
            self.ledger.reserve(
                scope_key,
                micro_usd=0,  # identités Gemma 4 officielles sur l'endpoint officiel : 0 $ (attesté par la politique)
                tokens=reserve_tokens,
                kind="call",
                role=role,
                model=nr.model,
                transport=TRANSPORT,
                reservation_id=reservation_id,
                ttl_seconds=self.config.reservation_ttl_seconds,
            )
        except BudgetRefused as exc:
            self._release(attempt, "budget_refused", str(exc), reserved=False)
            if exc.code == REFUSED_BLOCKED:
                raise BrokerBlocked(str(exc)) from None
            raise BrokerBudgetRefused(str(exc), code=f"budget_{exc.code}") from None
        except BudgetLedgerError as exc:
            self._release(attempt, "ledger_error", str(exc), reserved=False)
            raise BrokerBudgetRefused(f"registre budgétaire indisponible : {exc}", code="ledger_unavailable") from None

        # 6. échéances revérifiées (la latence de countTokens a pu les franchir), puis ADMISSION TRANSACTIONNELLE : au point exact
        # de l'émission, scopes (enfant et parent), session et réservations sont revalidés dans la MÊME transaction que le
        # marquage ``emitting`` — un blocage survenu pendant countTokens (autre rôle, autre processus) interdit l'émission.
        now = self._now()
        try:
            self._check_global_deadline(root_scope_key, now)
        except BrokerForbidden as exc:
            self._release(attempt, exc.code, str(exc))
            raise
        admitted_ok, why = self.store.admit_emission(
            attempt_id,
            now,
            session_id=session.session_id if session else None,
            scope_keys=tuple(dict.fromkeys((scope_key, root_scope_key))),
            parent_reservation_id=session.parent_reservation_id if session else None,
        )
        if not admitted_ok:
            if why.startswith(FALLBACK_REFUSAL_PREFIX):
                code, _, detail = why[len(FALLBACK_REFUSAL_PREFIX) :].partition("|")
                self._raise_sequence_refusal(attempt, code, detail)
            self._release(attempt, "admission_refused", why)
            if "échéance" in why:
                raise BrokerForbidden(why, code="session_expired")
            if "session" in why and "bloquée" not in why:
                raise BrokerForbidden(why, code="session_closed")
            raise BrokerBlocked(why, code="admission_refused")

        # 7. émission — tout ce qui n'est pas un rejet démontré laisse la réserve en place.
        try:
            raw = await self._upstream.generate(nr)
        except UpstreamHTTPError as exc:
            if exc.status in PROVEN_REJECTED_STATUS:
                self._release(attempt, "upstream_rejected", f"HTTP {exc.status}")
                raise BrokerUpstreamRejected(
                    f"le fournisseur a refusé la requête (HTTP {exc.status})", upstream_status=exc.status
                ) from None
            self._block_unknown(attempt, scope_key, session, "upstream_ambiguous", f"HTTP {exc.status} après émission")
            raise BrokerUpstreamAmbiguous(
                f"échec du fournisseur après émission (HTTP {exc.status}) : usage inconnu"
            ) from None
        except UpstreamTransportError as exc:
            if exc.before_send:
                self._release(attempt, "upstream_unreachable", exc.kind)
                raise BrokerUpstreamRejected(
                    f"fournisseur injoignable ({exc.kind}) : rien n'a été émis", upstream_status=503
                ) from None
            self._block_unknown(attempt, scope_key, session, "upstream_ambiguous", f"transport interrompu ({exc.kind})")
            raise BrokerUpstreamAmbiguous(f"transport interrompu après émission ({exc.kind}) : usage inconnu") from None
        except UpstreamBadBody as exc:
            self._block_unknown(attempt, scope_key, session, "response_invalid", str(exc))
            raise BrokerBoundViolation(
                f"réponse du fournisseur inexploitable : {exc}", code="response_invalid"
            ) from None
        except BaseException as exc:  # annulation, arrêt, bug : l'émission a pu partir
            self._block_unknown(attempt, scope_key, session, "interrupted", f"interrompu ({type(exc).__name__})")
            raise

        # 8. règlement.
        try:
            completion, usage = translate_response(raw, nr)
        except BrokerBoundViolation as exc:
            self._block_unknown(attempt, scope_key, session, exc.code, str(exc))
            raise
        violation = None
        if usage.output_tokens > nr.output_cap:
            violation = f"sortie {usage.output_tokens} > limite {nr.output_cap} (raisonnement compris)"
        elif usage.prompt + usage.tool_use_prompt > counted:
            violation = f"entrée {usage.prompt + usage.tool_use_prompt} > countTokens {counted}"
        elif usage.consumed_tokens > reserve_tokens:
            violation = f"consommation {usage.consumed_tokens} > réservation {reserve_tokens}"
        response_json = json.dumps(completion, separators=(",", ":"), ensure_ascii=False)
        settled = self.store.settle(
            attempt_id,
            now=self._now(),
            prompt=usage.prompt + usage.tool_use_prompt,
            candidates=usage.candidates,
            thoughts=usage.thoughts,
            total=usage.total,
            response_json=response_json,
        )
        if not settled:
            # Fermeture / réparation concurrente : la tentative a été déclarée inconnue ; la consommation n'est pas imputée ici.
            raise BrokerBlocked("session fermée pendant l'appel : usage laissé inconnu", code="closed_during_call")
        self._apply_ledger(self.store.get_attempt(attempt_id))
        if violation is not None:
            self._signal_violation(scope_key, session, violation)
            raise BrokerBoundViolation(f"borne démentie par le fournisseur : {violation}", code="bound_violation")
        return completion

    def _refuse_if_blocked(self, scope_key: str, session: Optional[SessionRecord], root_scope_key: str) -> None:
        """Un scope bloqué (usage inconnu, borne démentie) n'admet AUCUNE nouvelle émission — avant même ``countTokens``."""
        if session is not None and session.unknown_reason:
            raise BrokerBlocked(f"session bloquée : {session.unknown_reason}")
        for key in dict.fromkeys((scope_key, root_scope_key)):
            snap = self.ledger.snapshot(key)
            if snap.blocked:
                raise BrokerBlocked(f"scope {key} bloqué (usage inconnu, mode strict) : {snap.blocked_reason}")

    # ── rejeu ────────────────────────────────────────────────────────────────────────────────────────────

    def _replay(self, attempt: AttemptRecord) -> dict:
        """Même ``request_id`` : le résultat déjà obtenu est rendu, JAMAIS une seconde génération."""
        if attempt.state == BROKER_ATTEMPT_SETTLED and attempt.response_json:
            return json.loads(attempt.response_json)
        raise BrokerBlocked(
            f"rejeu refusé : la tentative est {attempt.state} (émission possible, résultat inconnu)",
            code="replay_refused",
        )

    @staticmethod
    def _rid(attempt: AttemptRecord) -> str:
        """Identifiant DÉTERMINISTE de la réservation de l'exécution courante de la tentative."""
        return f"broker:{attempt.attempt_id}" + (f"#{attempt.runs}" if attempt.runs else "")

    # ── règlements (état durable PUIS registre ; réparables) ─────────────────────────────────────────────

    def _raise_sequence_refusal(self, attempt: AttemptRecord, code: str, detail: str):
        """Règle serveur de séquencement (repli sans antécédent ; une seule génération en vol par session) : libère la tentative et refuse.

        Appelée par le précontrôle (avant countTokens) ET par l'admission finale : même erreur, même journal (tentative ``released``
        portant ``code``) ; aucune inconnue artificielle, la réserve de la génération en vol n'est pas touchée.
        """
        self._release(attempt, code, detail)
        if code == "generation_in_flight":
            # 429 : seul statut que le SDK réessaie ; la requête n'a RIEN émis et pourra être renvoyée une fois l'issue connue.
            raise BrokerRequestRefused(detail, code=code, status=429)
        raise BrokerForbidden(detail, code=code)

    def _release(self, attempt: AttemptRecord, code: str, detail: str, *, reserved: bool = True) -> None:
        if self.store.release(attempt.attempt_id, now=self._now(), code=code, detail=detail):
            self._apply_ledger(self.store.get_attempt(attempt.attempt_id))

    def _block_unknown(
        self, attempt: AttemptRecord, scope_key: str, session: Optional[SessionRecord], code: str, detail: str
    ) -> None:
        """Usage inconnu : réservation conservée, scope BLOQUÉ, et — pour un worker — le projet bloqué aussitôt."""
        self.store.mark_unknown(attempt.attempt_id, now=self._now(), code=code, detail=detail)
        self._apply_ledger(self.store.get_attempt(attempt.attempt_id))
        if session is not None:
            self.store.note_session_unknown(session.session_id, f"{code}: {detail}")
            self._block_parent(session, f"enfant {session.session_id} : {code} — {detail}")

    def _block_parent(self, session: SessionRecord, reason: str) -> None:
        """L'inconnue d'un enfant bloque IMMÉDIATEMENT le projet : la réservation parent passe ``unknown`` (borne haute).

        Une erreur du registre n'est JAMAIS prise pour « le parent est forcément inconnu » : seul un parent réellement
        ``unknown`` rend l'échec acceptable ; un parent déjà réglé est bloqué au niveau du scope ; toute autre erreur se propage.
        """
        if not session.parent_reservation_id:
            return
        try:
            self.ledger.mark_unknown(session.parent_reservation_id, reason=reason[:300])
            return
        except BudgetLedgerError:
            parent = self.ledger.get_reservation(session.parent_reservation_id)
            if parent is not None and parent.state == "unknown":
                return  # déjà inconnu (même cause ou autre) : le scope parent porte sa cause
            if parent is not None and session.parent_scope_key and parent.state in ("committed", "released"):
                # Réglée (committed / released) : la réserve ne peut plus porter l'inconnue, le scope porte le blocage.
                self.ledger.block(
                    session.parent_scope_key,
                    reason=f"broker enfant {session.session_id}: {reason}"[:300],
                    kind=BLOCK_BOUND_VIOLATION,
                    event_key=f"broker-child-unknown:{session.session_id}",
                )
                return
            raise

    def _signal_violation(self, scope_key: str, session: Optional[SessionRecord], reason: str) -> None:
        """Borne démentie MAIS consommation mesurée : elle est engagée telle quelle, puis scope(s) bloqué(s) et signalé(s)."""
        self.ledger.block(
            scope_key,
            reason=f"broker: {reason}",
            kind=BLOCK_BOUND_VIOLATION,
            event_key=f"broker-violation:{scope_key}:{uuid.uuid4().hex[:8]}",
        )
        if session is not None:
            self.store.note_session_unknown(session.session_id, f"bound_violation: {reason}")
            if session.parent_scope_key:
                self.ledger.block(
                    session.parent_scope_key,
                    reason=f"broker enfant {session.session_id}: {reason}",
                    kind=BLOCK_BOUND_VIOLATION,
                    event_key=f"broker-violation:{session.parent_scope_key}:{session.session_id}",
                )

    def _apply_ledger(self, attempt: Optional[AttemptRecord]) -> None:
        """Dérive l'opération du registre de l'ÉTAT DURABLE de la tentative (idempotente : clés d'événement déterministes)."""
        if attempt is None:
            return
        rid = attempt.reservation_id or self._rid(attempt)
        if self.ledger.get_reservation(rid) is None:
            return  # rien n'a été réservé (échec avant le registre) : rien à régler
        try:
            if attempt.state == BROKER_ATTEMPT_SETTLED:
                self.ledger.commit(rid, micro_usd=0, tokens=int(attempt.usage_total or 0))
            elif attempt.state == BROKER_ATTEMPT_RELEASED:
                self.ledger.release(rid, reason=attempt.error_code or "released")
            elif attempt.state == BROKER_ATTEMPT_UNKNOWN:
                self.ledger.mark_unknown(rid, reason=f"{attempt.error_code}: {attempt.error_detail}"[:300])
        except BudgetLedgerError:
            # Déjà réglée avec le même sens (rejeu) → rien ; un sens différent est une incohérence qu'on ne masque pas.
            reservation = self.ledger.get_reservation(rid)
            expected = {
                BROKER_ATTEMPT_SETTLED: "committed",
                BROKER_ATTEMPT_RELEASED: "released",
                BROKER_ATTEMPT_UNKNOWN: "unknown",
            }[attempt.state]
            if reservation is None or reservation.state != expected:
                raise

    # ── fermeture / consolidation ────────────────────────────────────────────────────────────────────────

    async def close_session(
        self, session_id: str, reason: str = "closed", *, consolidate_parent: bool = True
    ) -> SessionSummary:
        """Ferme la session (plus aucune génération), attend les appels en vol, consolide dans la réservation parent.

        Concurrent-sûr : ``open → closing`` est un CAS, les appels en vol se terminent (et libèrent ou règlent) AVANT la
        consolidation ; ceux qui dépassent l'attente sont déclarés INCONNUS (le projet est bloqué). Idempotent. Une session
        close ne laisse AUCUNE réservation enfant non consolidée : une réserve sans état durable est inconnue (jamais libérée).
        """
        record = self.store.begin_close(session_id, reason)
        if record.state == BROKER_SESSION_CLOSED:
            return self._summary(record)
        deadline = time.monotonic() + self.config.close_wait_seconds
        while record.in_flight > 0 and time.monotonic() < deadline:
            await asyncio.sleep(0.02)
            record = self.store.get_session(session_id)
        if record.in_flight > 0:
            for pending in self.store.pending_attempts(session_id=session_id):
                if pending.state == BROKER_ATTEMPT_EMITTING:
                    self._block_unknown(
                        pending, record.scope_key, record, "closed_in_flight", "fermeture avant la fin de l'appel"
                    )
        self.repair(session_id=session_id, include_own=True, sweep=True)
        return self._consolidate(session_id, consolidate_parent=consolidate_parent)

    def _consolidate(self, session_id: str, *, consolidate_parent: bool = True) -> SessionSummary:
        record = self.store.get_session(session_id)
        child = self.ledger.snapshot(record.scope_key)
        unsettled = child.reserved_micro_usd > 0 or child.reserved_tokens > 0
        unknown = (
            child.unknown_micro_usd > 0
            or child.unknown_tokens > 0
            or unsettled  # filet : une réserve enfant encore ouverte ne s'engage jamais comme une consommation établie
            or child.blocked_reason
            or record.unknown_reason
        )
        settlement = "none"
        if record.parent_reservation_id and consolidate_parent:
            parent = self.ledger.get_reservation(record.parent_reservation_id)
            if parent is not None and parent.state == "reserved":
                if unknown:
                    reason = (
                        record.unknown_reason
                        or child.blocked_reason
                        or (
                            "réserve enfant non réglée à la fermeture"
                            if unsettled
                            else "consommation de l'enfant non établie"
                        )
                    )
                    self.ledger.mark_unknown(
                        record.parent_reservation_id,
                        reason=f"enfant {session_id}: {reason}"[:300],
                        event_key=f"consolidate:{session_id}",
                    )
                    settlement = "unknown"
                else:
                    self.ledger.commit(
                        record.parent_reservation_id,
                        micro_usd=child.consumed_micro_usd,
                        tokens=child.consumed_tokens,
                        event_key=f"consolidate:{session_id}",
                    )
                    settlement = "committed"
            elif parent is not None:
                settlement = "unknown" if parent.state == "unknown" else parent.state
        self.store.finish_close(session_id, now=self._now())
        return self._summary(self.store.get_session(session_id), child=child, settlement=settlement)

    def _summary(self, record: SessionRecord, *, child=None, settlement: Optional[str] = None) -> SessionSummary:
        child = child or self.ledger.snapshot(record.scope_key)
        unknown = bool(
            child.unknown_micro_usd > 0
            or child.unknown_tokens > 0
            or child.reserved_tokens > 0
            or child.blocked_reason
            or record.unknown_reason
        )
        if settlement is None:
            parent = self.ledger.get_reservation(record.parent_reservation_id) if record.parent_reservation_id else None
            settlement = "none" if parent is None else ("committed" if parent.state == "committed" else parent.state)
        attempts = self.store.attempts(scope_key=record.scope_key)
        return SessionSummary(
            session_id=record.session_id,
            state=record.state,
            consumed_tokens=child.consumed_tokens,
            consumed_micro_usd=child.consumed_micro_usd,
            unknown=unknown,
            unknown_reason=record.unknown_reason or child.blocked_reason,
            blocked_reason=child.blocked_reason,
            attempts=len(attempts),
            parent_settlement=settlement,
            prompt_tokens=sum(int(a.usage_prompt or 0) for a in attempts if a.state == BROKER_ATTEMPT_SETTLED),
            completion_tokens=sum(
                int(a.usage_candidates or 0) + int(a.usage_thoughts or 0)
                for a in attempts
                if a.state == BROKER_ATTEMPT_SETTLED
            ),
        )

    # ── réparation après arrêt / crash ───────────────────────────────────────────────────────────────────

    def _is_abandoned(self, attempt: AttemptRecord, *, include_own: bool, now: datetime) -> bool:
        """Contrat de PROPRIÉTÉ : une tentative est réparable si son propriétaire est introuvable / terminé / disparu.

        Celle d'un propriétaire VIVANT (un autre processus) n'est jamais touchée ; celle de cette instance ne l'est que sur
        demande explicite (``include_own``, instance au repos : démarrage du processus ou fermeture de session).
        """
        if attempt.owner_id is None:
            return True
        if attempt.owner_id == self.owner_id:
            return include_own
        return not owner_is_alive(self.store.get_owner(attempt.owner_id), now)

    def repair(
        self,
        *,
        session_id: Optional[str] = None,
        scope_key: Optional[str] = None,
        scope_keys: Optional[Tuple[str, ...]] = None,
        include_own: bool = False,
        sweep: bool = True,
    ) -> int:
        """Répare les tentatives ABANDONNÉES (tous producteurs : workers, planner, QA, reviewer…), sans jamais émettre.

        ``prepared`` : l'émission n'a PAS été marquée ⇒ rien n'est parti : réservation libérée (même si l'identifiant n'a pas été
        écrit dans la tentative : il est déterministe). ``emitting`` : l'envoi a pu partir ⇒ usage INCONNU (conservé, bloquant),
        jamais rejoué. ``sweep`` : rejoue aussi l'opération du registre qui aurait pris du retard sur l'état durable, puis traite
        toute réserve ``broker`` encore ouverte SANS tentative vivante : sans état durable elle est inconnue — jamais libérée.
        """
        repaired = 0
        session = self.store.get_session(session_id) if session_id else None
        scope = session.scope_key if session else scope_key
        now = self._now()
        for attempt in self.store.pending_attempts(scope_key=scope, scope_keys=scope_keys):
            if not self._is_abandoned(attempt, include_own=include_own, now=now):
                continue
            owner = session
            if owner is None and attempt.session_id:
                owner = self.store.get_session(attempt.session_id)
            if attempt.state == BROKER_ATTEMPT_PREPARED:
                self._release(attempt, "interrupted_before_emission", "arrêt avant le marquage d'émission")
            elif attempt.state == BROKER_ATTEMPT_EMITTING:
                self._block_unknown(
                    attempt, attempt.scope_key, owner, "interrupted", "arrêt ou crash après le marquage d'émission"
                )
            repaired += 1
        if sweep:
            scopes = (scope,) if scope else tuple(self.store.scope_keys_with_attempts())
            for key in scopes:
                for attempt in self.store.attempts(scope_key=key):
                    if attempt.state in (BROKER_ATTEMPT_SETTLED, BROKER_ATTEMPT_RELEASED, BROKER_ATTEMPT_UNKNOWN):
                        self._apply_ledger(attempt)
                repaired += self._sweep_orphan_reservations(key, include_own=include_own, session=session)
        return repaired

    def _sweep_orphan_reservations(self, scope_key: str, *, include_own: bool, session: Optional[SessionRecord]) -> int:
        """Réserve ``broker`` ouverte dans ``scope_key`` sans tentative qui la porte : consommation INCONNUE, jamais libérée."""
        swept = 0
        now = self._now()
        for reservation in self.ledger.reservations(scope_key, states=("reserved",)):
            if not reservation.reservation_id.startswith("broker:"):
                continue
            base = reservation.reservation_id[len("broker:") :].split("#", 1)[0]
            attempt = self.store.get_attempt(base)
            if attempt is not None:
                if attempt.state in (BROKER_ATTEMPT_PREPARED, BROKER_ATTEMPT_EMITTING) and not self._is_abandoned(
                    attempt, include_own=include_own, now=now
                ):
                    continue  # tentative vivante : sa réserve est légitime
                if self._rid(attempt) == reservation.reservation_id and attempt.state != BROKER_ATTEMPT_RELEASED:
                    continue  # portée par une tentative (réparée plus haut ou réglée)
                if self._rid(attempt) == reservation.reservation_id:
                    self._apply_ledger(attempt)  # libérée durablement, registre en retard : on rejoue la libération
                    swept += 1
                    continue
            # Aucune tentative ne porte cette réserve : on ne sait pas si elle a servi ⇒ inconnue (conservée), jamais libérée.
            self.ledger.mark_unknown(
                reservation.reservation_id, reason="réserve du courtier sans état durable (orpheline) : usage inconnu"
            )
            owner = session or self._session_of_scope(scope_key)
            if owner is not None:
                self.store.note_session_unknown(owner.session_id, "réserve orpheline")
                self._block_parent(owner, f"enfant {owner.session_id} : réserve orpheline")
            swept += 1
        return swept

    def _session_of_scope(self, scope_key: str) -> Optional[SessionRecord]:
        for record in self.store.open_sessions():
            if record.scope_key == scope_key:
                return record
        return None

    async def recover_all(self, *, include_own: bool = True) -> int:
        """Au DÉMARRAGE du processus, avant tout appel : répare les tentatives de TOUS les producteurs et ferme les sessions orphelines.

        Contrat de propriété : les tentatives et sessions d'un propriétaire VIVANT (un autre processus) ne sont jamais touchées.
        Les restes d'un processus disparu, ceux sans propriétaire, et — ``include_own`` — ceux de CETTE instance le sont ; appeler
        avec ``include_own`` pendant que cette instance a des appels en vol est refusé.
        """
        if include_own and self._local_inflight:
            raise BrokerForbidden(
                "recover_all(include_own) refusé : cette instance a des appels en vol", code="recovery_while_busy"
            )
        self._ensure_owner()
        total = self.repair(include_own=include_own, sweep=True)
        now = self._now()
        for record in self.store.open_sessions():
            own = record.owner_id == self.owner_id
            abandoned = record.owner_id is None or (
                not own and not owner_is_alive(self.store.get_owner(record.owner_id), now)
            )
            if not (abandoned or (own and include_own)):
                continue
            total += self.repair(session_id=record.session_id, include_own=True, sweep=True)
            await self.close_session(record.session_id, reason="recovery")
        return total

    # ── échéance persistée ───────────────────────────────────────────────────────────────────────────────

    def persisted_deadline(self, scope_key: str) -> Optional[datetime]:
        """Échéance GLOBALE persistée du scope (ouverte à la première ouverture réelle du fournisseur), ou ``None`` si pas encore ouverte."""
        return self.store.clock_deadline(scope_key)

    def remaining_seconds(self, scope_key: str) -> Optional[float]:
        """Secondes restantes avant l'échéance persistée (négatif = dépassée) ; ``None`` si l'horloge n'est pas ouverte."""
        deadline = self.persisted_deadline(scope_key)
        return None if deadline is None else (deadline - self._now()).total_seconds()

    def open_clock(self, scope_key: str) -> Optional[datetime]:
        """Ouvre EXPLICITEMENT l'horloge globale du scope (idempotent : l'échéance existante est rendue, jamais déplacée)."""
        if self.config.global_deadline_seconds <= 0:
            return None
        return self.store.ensure_clock(scope_key, self.config.global_deadline_seconds, self._now())

    # ── qualification des deux modèles (canaris) ─────────────────────────────────────────────────────────

    async def qualify_models(
        self, scope_key: str, *, models: Tuple[str, ...] = OFFICIAL_MODELS
    ) -> "QualificationReport":
        """Qualifie les DEUX identités officielles sur ``scope_key`` par le VRAI pipeline (normalisation → countTokens complet →
        réservation → émission marquée → usage vérifié) : texte, JSON, appel d'outil.

        Chaque canari a une identité durable (``qualify:<scope>:<modèle>:<capacité>``) : relancer ne réémet pas un canari déjà
        réglé. Le premier refus ou la première ambiguïté ARRÊTE la qualification (rapport incomplet, aucun repli, aucune estimation,
        aucun renvoi). L'échéance globale est ouverte ICI si elle ne l'est pas (premier accès réel au fournisseur).
        """
        results = []
        failure: Optional[str] = None
        self.open_clock(scope_key)
        for model in models:
            role = "coder" if model == FALLBACK_MODEL else "default"
            capabilities = []
            for capability, body, check in _canaries(model):
                request_id = f"qualify:{scope_key}:{model}:{capability}"
                if failure is not None:
                    capabilities.append(
                        CapabilityResult(capability, False, "non exécuté (qualification interrompue)", request_id)
                    )
                    continue
                try:
                    completion = await self.sampling_completion(scope_key, role, body, request_id=request_id)
                    ok, detail = check(completion)
                    tokens = int(completion.get("usage", {}).get("total_tokens", 0))
                except BrokerError as exc:
                    ok, detail, tokens = False, f"{exc.code}: {exc}", 0
                if not ok:
                    failure = f"{model}/{capability}: {detail}"
                capabilities.append(CapabilityResult(capability, ok, detail, request_id, tokens))
            results.append(
                ModelQualification(
                    model=model, role=role, capabilities=tuple(capabilities), ok=all(c.ok for c in capabilities)
                )
            )
        snapshot = self.ledger.snapshot(scope_key)
        deadline = self.persisted_deadline(scope_key)
        if failure is None and self.config.global_deadline_seconds > 0 and deadline is None:
            failure = "échéance globale non ouverte après la qualification"
        return QualificationReport(
            scope_key=scope_key,
            ok=failure is None and all(m.ok for m in results),
            reason=failure or "",
            models=tuple(results),
            deadline_at=deadline,
            consumed_tokens=snapshot.consumed_tokens,
            blocked=bool(snapshot.blocked),
            destination="generativelanguage.googleapis.com/v1beta (natif)",
        )


# ── canaris de qualification ─────────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class CapabilityResult:
    capability: str
    ok: bool
    detail: str
    request_id: str
    tokens: int = 0


@dataclass(frozen=True)
class ModelQualification:
    model: str
    role: str
    capabilities: Tuple[CapabilityResult, ...]
    ok: bool


@dataclass(frozen=True)
class QualificationReport:
    """Résultat de la qualification (sans secret). ``ok`` seulement si les DEUX modèles ont réussi TOUTES les capacités."""

    scope_key: str
    ok: bool
    reason: str
    models: Tuple[ModelQualification, ...]
    deadline_at: Optional[datetime]
    consumed_tokens: int
    blocked: bool
    destination: str

    def to_dict(self) -> dict:
        return {
            "scope_key": self.scope_key,
            "ok": self.ok,
            "reason": self.reason,
            "destination": self.destination,
            "deadline_at": None if self.deadline_at is None else self.deadline_at.isoformat(),
            "consumed_tokens": self.consumed_tokens,
            "blocked": self.blocked,
            "models": [
                {
                    "model": m.model,
                    "role": m.role,
                    "ok": m.ok,
                    "capabilities": [
                        {
                            "capability": c.capability,
                            "ok": c.ok,
                            "detail": c.detail,
                            "request_id": c.request_id,
                            "tokens": c.tokens,
                        }
                        for c in m.capabilities
                    ],
                }
                for m in self.models
            ],
        }


def _canaries(model: str):
    """``(capacité, corps Chat Completions, vérification)`` — les trois capacités RÉELLEMENT utilisées par la campagne."""

    def text_check(completion):
        choice = completion["choices"][0]
        content = choice["message"].get("content")
        ok = isinstance(content, str) and bool(content.strip()) and choice["finish_reason"] in ("stop", "length")
        return ok, "texte reçu" if ok else f"réponse texte inexploitable (finish={choice['finish_reason']!r})"

    def json_check(completion):
        content = completion["choices"][0]["message"].get("content")
        try:
            value = json.loads(content or "")
        except ValueError:
            return False, "la réponse n'est pas du JSON"
        ok = isinstance(value, dict)
        return ok, "objet JSON reçu" if ok else "la réponse JSON n'est pas un objet"

    def tool_check(completion):
        message = completion["choices"][0]["message"]
        calls = message.get("tool_calls") or []
        if len(calls) != 1 or calls[0]["function"]["name"] != "report_status":
            return False, "aucun appel de l'outil demandé"
        try:
            arguments = json.loads(calls[0]["function"]["arguments"])
        except ValueError:
            return False, "arguments d'outil illisibles"
        ok = isinstance(arguments, dict) and "status" in arguments
        return ok, "appel d'outil reçu" if ok else "arguments d'outil sans 'status'"

    common = {"model": model, "max_tokens": 256}
    return (
        (
            "text",
            {
                **common,
                "messages": [
                    {"role": "system", "content": "Réponds en un mot."},
                    {"role": "user", "content": "Dis OK."},
                ],
            },
            text_check,
        ),
        (
            "json",
            {
                **common,
                "messages": [{"role": "user", "content": 'Réponds uniquement avec l\'objet JSON {"ok": true}.'}],
                "response_format": {"type": "json_object"},
            },
            json_check,
        ),
        (
            "tools",
            {
                **common,
                "messages": [{"role": "user", "content": "Appelle l'outil report_status avec status=ok."}],
                "tools": [
                    {
                        "type": "function",
                        "function": {
                            "name": "report_status",
                            "description": "Rapporte l'état.",
                            "parameters": {
                                "type": "object",
                                "properties": {"status": {"type": "string"}},
                                "required": ["status"],
                            },
                        },
                    }
                ],
                "tool_choice": "required",
            },
            tool_check,
        ),
    )
