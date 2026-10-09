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
    MAX_OUTPUT_TOKENS_CEILING,
    MAX_REQUEST_BYTES,
    ROLES,
    models_for_role,
)
from collegue.broker.store import AttemptRecord, BrokerStore, SessionRecord
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

    def _now(self) -> datetime:
        return self._clock()

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
        payload = parse_json_strict(raw, max_bytes=self.config.max_request_bytes)
        return normalize_chat_request(
            payload,
            allowed_models=allowed,
            default_output_tokens=min(self.config.default_output_tokens, cap),
            max_output_tokens=cap,
        )

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
        self._check_global_deadline(root_scope_key, now)
        admitted = False
        if session is not None:
            self.store.begin_call(session.session_id, now)
            admitted = True
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
        if request_id is not None:
            known = self.store.find_attempt(scope_key, request_id)
            if known is not None:
                if known.request_sha256 != nr.sha256:
                    raise BrokerRequestRefused(
                        "request_id réutilisé avec un contenu différent : un identifiant d'idempotence ne change pas de sens",
                        code="request_id_conflict",
                        status=409,
                    )
                return self._replay(known)  # AVANT tout contrôle de blocage : un résultat déjà obtenu se rend tel quel
        self._refuse_if_blocked(scope_key, session, root_scope_key)
        attempt_id = f"{session.session_id if session else 'direct'}:{uuid.uuid4().hex[:16]}"
        attempt, replayed = self.store.create_attempt(
            attempt_id=attempt_id,
            request_id=request_id,
            session_id=session.session_id if session else None,
            scope_key=scope_key,
            role=role,
            model=nr.model,
            request_sha256=nr.sha256,
            output_cap=nr.output_cap,
        )
        if replayed:
            return self._replay(attempt)
        attempt_id = attempt.attempt_id

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
        reservation_id = f"broker:{attempt_id}"
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
        self.store.attach_reservation(
            attempt_id, reservation_id=reservation_id, counted_tokens=counted, reserved_tokens=reserve_tokens
        )
        attempt = self.store.get_attempt(attempt_id)

        # 6. échéances revérifiées (la latence de countTokens a pu les franchir), puis émission MARQUÉE avant l'envoi.
        now = self._now()
        try:
            self._check_global_deadline(root_scope_key, now)
            if session is not None and session.deadline_at is not None and now >= session.deadline_at:
                raise BrokerForbidden(
                    "échéance de la session atteinte : aucune nouvelle génération", code="session_expired"
                )
        except BrokerForbidden as exc:
            self._release(attempt, exc.code, str(exc))
            raise
        if not self.store.mark_emitting(attempt_id, now):
            # Une autre exécution (rejeu concurrent, fermeture) a repris la tentative : ne RIEN émettre.
            raise BrokerBlocked("tentative reprise par une autre exécution : aucune émission", code="attempt_taken")

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
        if attempt.state == BROKER_ATTEMPT_RELEASED:
            raise BrokerForbidden(
                "cette requête a été libérée sans génération : renvoyer avec un nouveau request_id",
                code="attempt_released",
            )
        raise BrokerBlocked(
            f"rejeu refusé : la tentative est {attempt.state} (émission possible, résultat inconnu)",
            code="replay_refused",
        )

    # ── règlements (état durable PUIS registre ; réparables) ─────────────────────────────────────────────

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
        """L'inconnue d'un enfant bloque IMMÉDIATEMENT le projet : la réservation parent passe ``unknown`` (borne haute)."""
        if not session.parent_reservation_id:
            return
        try:
            self.ledger.mark_unknown(session.parent_reservation_id, reason=reason[:300])
        except BudgetLedgerError:
            pass  # déjà réglée / déjà inconnue : le scope parent porte alors déjà sa cause

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
        if attempt is None or not attempt.reservation_id:
            return
        rid = attempt.reservation_id
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

    async def close_session(self, session_id: str, reason: str = "closed") -> SessionSummary:
        """Ferme la session (plus aucune génération), attend les appels en vol, consolide dans la réservation parent.

        Concurrent-sûr : ``open → closing`` est un CAS, les appels en vol se terminent (et libèrent ou règlent) AVANT la
        consolidation ; ceux qui dépassent l'attente sont déclarés INCONNUS (le projet est bloqué). Idempotent.
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
        self.repair(session_id=session_id)
        return self._consolidate(session_id)

    def _consolidate(self, session_id: str) -> SessionSummary:
        record = self.store.get_session(session_id)
        child = self.ledger.snapshot(record.scope_key)
        unknown = (
            child.unknown_micro_usd > 0 or child.unknown_tokens > 0 or child.blocked_reason or record.unknown_reason
        )
        settlement = "none"
        if record.parent_reservation_id:
            parent = self.ledger.get_reservation(record.parent_reservation_id)
            if parent is not None and parent.state == "reserved":
                if unknown:
                    reason = record.unknown_reason or child.blocked_reason or "consommation de l'enfant non établie"
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
            child.unknown_micro_usd > 0 or child.unknown_tokens > 0 or child.blocked_reason or record.unknown_reason
        )
        if settlement is None:
            parent = self.ledger.get_reservation(record.parent_reservation_id) if record.parent_reservation_id else None
            settlement = "none" if parent is None else ("committed" if parent.state == "committed" else parent.state)
        return SessionSummary(
            session_id=record.session_id,
            state=record.state,
            consumed_tokens=child.consumed_tokens,
            consumed_micro_usd=child.consumed_micro_usd,
            unknown=unknown,
            unknown_reason=record.unknown_reason or child.blocked_reason,
            blocked_reason=child.blocked_reason,
            attempts=len(self.store.attempts(scope_key=record.scope_key)),
            parent_settlement=settlement,
        )

    # ── réparation après arrêt / crash ───────────────────────────────────────────────────────────────────

    def repair(self, *, session_id: Optional[str] = None, scope_key: Optional[str] = None) -> int:
        """Répare les tentatives interrompues. À appeler au démarrage et par ``close_session``.

        ``prepared`` : l'émission n'a PAS été marquée ⇒ rien n'est parti : réservation libérée. ``emitting`` : l'envoi a pu
        partir ⇒ usage INCONNU (conservé, bloquant), jamais rejoué. ``settled``/``released``/``unknown`` dont le registre
        n'a pas suivi : l'opération du registre manquante est rejouée (idempotente).
        """
        repaired = 0
        session = self.store.get_session(session_id) if session_id else None
        scope = session.scope_key if session else scope_key
        for attempt in self.store.pending_attempts(scope_key=scope):
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
        for attempt in self.store.attempts(scope_key=scope) if scope else []:
            if attempt.state in (BROKER_ATTEMPT_SETTLED, BROKER_ATTEMPT_RELEASED, BROKER_ATTEMPT_UNKNOWN):
                self._apply_ledger(attempt)
        return repaired

    async def recover_all(self) -> int:
        """Au démarrage du processus : répare chaque session non close puis la ferme (consolidation incluse)."""
        total = 0
        for record in self.store.open_sessions():
            total += self.repair(session_id=record.session_id)
            await self.close_session(record.session_id, reason="recovery")
        return total
