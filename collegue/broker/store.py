"""État durable du courtier (migration 0013) : sessions, tentatives, horloge globale.

Toutes les transitions sont des ``UPDATE`` conditionnels (compare-and-set) dans une transaction du registre budgétaire :
valables à l'identique sur SQLite et PostgreSQL, sans verrou process ni lire-puis-écrire en Python. Le magasin ne compte
AUCUN montant : les montants vivent dans ``budget_scopes`` / ``budget_reservations`` (autorité unique, aucun double comptage).

Machine d'états d'une tentative ::

    prepared ──mark_emitting──▶ emitting ──settle──▶ settled
        │                           ├──release──▶ released   (rejet démontré avant traitement)
        └────────release────────────┘
                                    └──mark_unknown──▶ unknown   (échec ambigu, crash, borne démentie)

``emitting`` est écrit et COMMITÉ avant l'envoi au fournisseur : une tentative encore ``prepared`` après un crash n'a
PROUVABLEMENT rien émis (libérable) ; une tentative ``emitting`` ne peut jamais être rejouée ni libérée sans preuve.
"""

from __future__ import annotations

import json
import os
import socket
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import List, Optional, Tuple

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from collegue.broker.errors import BrokerForbidden, BrokerRequestRefused
from collegue.broker.policy import FALLBACK_MODEL, PRIMARY_MODEL
from collegue.state.budget_ledger import (
    REFUSED_BLOCKED,
    REFUSED_SCOPE,
    BudgetLedger,
    BudgetRefused,
)
from collegue.state.models import (
    BROKER_ATTEMPT_EMITTING,
    BROKER_ATTEMPT_PREPARED,
    BROKER_ATTEMPT_RELEASED,
    BROKER_ATTEMPT_SETTLED,
    BROKER_ATTEMPT_UNKNOWN,
    BROKER_SESSION_CLOSED,
    BROKER_SESSION_CLOSING,
    BROKER_SESSION_OPEN,
    BUDGET_RESERVED,
    BrokerAttempt,
    BrokerClock,
    BrokerOwner,
    BrokerSession,
    BudgetReservation,
    BudgetScope,
)

# Préfixe du motif d'un refus d'admission dû à la règle de séquencement des modèles (voir ``_model_sequence_refusal``).
FALLBACK_REFUSAL_PREFIX = "séquence des modèles : "


@dataclass(frozen=True)
class SessionRecord:
    session_id: str
    owner_id: Optional[str]
    role: str
    scope_key: str
    parent_scope_key: Optional[str]
    parent_reservation_id: Optional[str]
    allowed_models: Tuple[str, ...]
    max_output_tokens: int
    state: str
    in_flight: int
    deadline_at: Optional[datetime]
    consolidated: bool
    close_reason: Optional[str]
    unknown_reason: Optional[str]


@dataclass(frozen=True)
class AttemptRecord:
    attempt_id: str
    request_id: Optional[str]
    owner_id: Optional[str]
    runs: int
    session_id: Optional[str]
    scope_key: str
    role: str
    model: str
    request_sha256: str
    state: str
    reservation_id: Optional[str]
    counted_tokens: Optional[int]
    reserved_tokens: int
    output_cap: int
    usage_prompt: Optional[int]
    usage_candidates: Optional[int]
    usage_thoughts: Optional[int]
    usage_total: Optional[int]
    response_json: Optional[str]
    error_code: Optional[str]
    error_detail: Optional[str]


@dataclass(frozen=True)
class OwnerRecord:
    owner_id: str
    host: str
    pid: int
    start_ticks: Optional[int]
    heartbeat_at: datetime
    ended_at: Optional[datetime]


OWNER_REMOTE_TTL_SECONDS = 3600  # propriétaire d'un AUTRE hôte : vivant tant que son battement de cœur est plus récent


def process_start_ticks(pid: int) -> Optional[int]:
    """Date de démarrage du processus (champ 22 de ``/proc/<pid>/stat``) ; ``None`` si indisponible. Évite la réutilisation d'un pid."""
    try:
        with open(f"/proc/{int(pid)}/stat", "rb") as handle:
            data = handle.read().decode("latin-1")
        return int(data.rsplit(")", 1)[1].split()[19])
    except (OSError, ValueError, IndexError):
        return None


def owner_is_alive(owner: Optional[OwnerRecord], now: datetime) -> bool:
    """Un propriétaire INCONNU, terminé ou dont le processus a disparu n'a plus de tentative vivante."""
    if owner is None or owner.ended_at is not None:
        return False
    if owner.host == socket.gethostname():
        if owner.start_ticks is not None:
            return process_start_ticks(owner.pid) == owner.start_ticks
        try:
            os.kill(owner.pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True
    return (now - owner.heartbeat_at).total_seconds() < OWNER_REMOTE_TTL_SECONDS


def _session_of(row: BrokerSession) -> SessionRecord:
    return SessionRecord(
        session_id=row.session_id,
        owner_id=row.owner_id,
        role=row.role,
        scope_key=row.scope_key,
        parent_scope_key=row.parent_scope_key,
        parent_reservation_id=row.parent_reservation_id,
        allowed_models=tuple(json.loads(row.allowed_models)),
        max_output_tokens=int(row.max_output_tokens),
        state=row.state,
        in_flight=int(row.in_flight),
        deadline_at=row.deadline_at,
        consolidated=bool(row.consolidated),
        close_reason=row.close_reason,
        unknown_reason=row.unknown_reason,
    )


def _attempt_of(row: BrokerAttempt, session_id: Optional[str]) -> AttemptRecord:
    return AttemptRecord(
        attempt_id=row.attempt_id,
        request_id=row.request_id,
        owner_id=row.owner_id,
        runs=int(row.runs or 0),
        session_id=session_id,
        scope_key=row.scope_key,
        role=row.role,
        model=row.model,
        request_sha256=row.request_sha256,
        state=row.state,
        reservation_id=row.reservation_id,
        counted_tokens=None if row.counted_tokens is None else int(row.counted_tokens),
        reserved_tokens=int(row.reserved_tokens),
        output_cap=int(row.output_cap),
        usage_prompt=row.usage_prompt,
        usage_candidates=row.usage_candidates,
        usage_thoughts=row.usage_thoughts,
        usage_total=row.usage_total,
        response_json=row.response_json,
        error_code=row.error_code,
        error_detail=row.error_detail,
    )


def _begin_write(session: Session) -> None:
    """Démarre la transaction D'ÉCRITURE dès la première lecture sur SQLite.

    Le mode historique de ``pysqlite`` n'ouvre pas de transaction pour un ``SELECT`` : les lectures d'une admission seraient
    alors validées séparément de la mise à jour qui les suit (course possible avec un blocage validé entre-temps). ``BEGIN
    IMMEDIATE`` prend le verrou d'écriture d'emblée : lectures et mise à jour sont atomiques. PostgreSQL n'en a pas besoin
    (transaction implicite + ``FOR UPDATE``).
    """
    if session.get_bind().dialect.name == "sqlite":
        session.connection().exec_driver_sql("BEGIN IMMEDIATE")


class BrokerStore:
    """Opérations durables du courtier sur le registre budgétaire fourni."""

    def __init__(self, ledger: BudgetLedger):
        self.ledger = ledger

    def _run(self, fn):
        return self.ledger._run(fn)

    # ── sessions ─────────────────────────────────────────────────────────────────────────────────────────

    def open_session(
        self,
        *,
        session_id: str,
        token_sha256: str,
        role: str,
        parent_scope_key: str,
        parent_reservation_id: str,
        allowed_models: Tuple[str, ...],
        max_output_tokens: int,
        deadline_at: Optional[datetime],
        owner_id: Optional[str] = None,
    ) -> SessionRecord:
        """Active une session SUR une réservation parent déjà prise (``worker``, ``reserved``) — jamais avant.

        Dans UNE transaction : vérifie la réservation parent, crée le scope enfant (plafonds = montants réservés) et la
        session. Le scope enfant est strict : un usage inconnu l'y bloque aussitôt.
        """
        child_key = f"child:{session_id}"

        def _do(session: Session):
            _begin_write(session)
            parent = session.scalar(select(BudgetScope).where(BudgetScope.scope_key == parent_scope_key))
            if parent is None:
                raise BudgetRefused(REFUSED_SCOPE, f"scope parent inconnu : {parent_scope_key}")
            if parent.strict and parent.blocked_reason:
                raise BudgetRefused(REFUSED_BLOCKED, f"scope parent bloqué : {parent.blocked_reason}")
            reservation = session.scalar(
                select(BudgetReservation).where(BudgetReservation.reservation_id == parent_reservation_id)
            )
            if (
                reservation is None
                or reservation.scope_id != parent.id
                or reservation.kind != "worker"
                or reservation.state != BUDGET_RESERVED
            ):
                raise BrokerForbidden(
                    "aucune réservation parent active pour cette session : l'activation exige une réservation prise avant",
                    code="no_parent_reservation",
                )
            if session.scalar(
                select(BrokerSession).where(BrokerSession.parent_reservation_id == parent_reservation_id)
            ):
                raise BrokerForbidden("une session existe déjà pour cette réservation parent", code="session_exists")
            if int(reservation.reserved_tokens) <= 0:
                # Une allocation NULLE n'autorise aucune consommation positive : jamais traduite en « sans plafond ».
                raise BrokerForbidden(
                    "allocation de tokens nulle : la réservation parent n'autorise aucune génération (plafond zéro, "
                    "jamais illimité)",
                    code="zero_allocation",
                )
            child = BudgetScope(
                scope_key=child_key,
                project_id=None,
                kind="child",
                # Plafonds EXACTS de la réservation parent (0 reste 0) : la dimension USD est indépendante des tokens.
                cap_micro_usd=int(reservation.reserved_micro_usd),
                cap_tokens=int(reservation.reserved_tokens),
                strict=True,
            )
            row = BrokerSession(
                session_id=session_id,
                token_sha256=token_sha256,
                role=role,
                owner_id=owner_id,
                scope_key=child_key,
                parent_scope_key=parent_scope_key,
                parent_reservation_id=parent_reservation_id,
                allowed_models=json.dumps(list(allowed_models)),
                max_output_tokens=int(max_output_tokens),
                state=BROKER_SESSION_OPEN,
                deadline_at=deadline_at,
            )
            session.add(child)
            session.add(row)
            session.flush()
            return _session_of(row)

        return self._run(_do)

    def get_session(self, session_id: str) -> Optional[SessionRecord]:
        def _do(session: Session):
            row = session.scalar(select(BrokerSession).where(BrokerSession.session_id == session_id))
            return None if row is None else _session_of(row)

        return self._run(_do)

    def session_token_hash(self, session_id: str) -> Optional[str]:
        def _do(session: Session):
            return session.scalar(select(BrokerSession.token_sha256).where(BrokerSession.session_id == session_id))

        return self._run(_do)

    def begin_call(self, session_id: str, now: datetime) -> SessionRecord:
        """``in_flight += 1`` SEULEMENT si la session est ouverte et non expirée (CAS) ; sinon refus explicite."""

        def _do(session: Session):
            row = session.scalar(select(BrokerSession).where(BrokerSession.session_id == session_id))
            if row is None:
                raise BrokerForbidden("session inconnue", code="session_unknown")
            claimed = session.execute(
                update(BrokerSession)
                .where(
                    BrokerSession.id == row.id,
                    BrokerSession.state == BROKER_SESSION_OPEN,
                    (BrokerSession.deadline_at.is_(None)) | (BrokerSession.deadline_at > now),
                )
                .values(in_flight=BrokerSession.in_flight + 1)
            )
            session.refresh(row)
            if claimed.rowcount != 1:
                if row.state != BROKER_SESSION_OPEN:
                    raise BrokerForbidden(f"session {row.state} : aucune nouvelle génération", code="session_closed")
                raise BrokerForbidden(
                    "échéance de la session atteinte : aucune nouvelle génération", code="session_expired"
                )
            return _session_of(row)

        return self._run(_do)

    def end_call(self, session_id: str) -> None:
        def _do(session: Session):
            session.execute(
                update(BrokerSession)
                .where(BrokerSession.session_id == session_id, BrokerSession.in_flight > 0)
                .values(in_flight=BrokerSession.in_flight - 1)
            )

        self._run(_do)

    def begin_close(self, session_id: str, reason: str) -> SessionRecord:
        """Passe ``open`` → ``closing`` (CAS) ; plus aucune nouvelle génération n'est admise. Idempotent."""

        def _do(session: Session):
            session.execute(
                update(BrokerSession)
                .where(BrokerSession.session_id == session_id, BrokerSession.state == BROKER_SESSION_OPEN)
                .values(state=BROKER_SESSION_CLOSING, close_reason=reason[:500])
            )
            row = session.scalar(select(BrokerSession).where(BrokerSession.session_id == session_id))
            if row is None:
                raise BrokerForbidden("session inconnue", code="session_unknown")
            session.refresh(row)
            return _session_of(row)

        return self._run(_do)

    def finish_close(self, session_id: str, *, now: datetime) -> bool:
        """``closing`` → ``closed`` + consolidée, SEULEMENT sans appel en vol (CAS). ``False`` si un appel est encore en vol."""

        def _do(session: Session):
            done = session.execute(
                update(BrokerSession)
                .where(
                    BrokerSession.session_id == session_id,
                    BrokerSession.state == BROKER_SESSION_CLOSING,
                    BrokerSession.in_flight == 0,
                )
                .values(state=BROKER_SESSION_CLOSED, consolidated=True, closed_at=now)
            )
            return done.rowcount == 1

        return self._run(_do)

    def note_session_unknown(self, session_id: str, reason: str) -> None:
        def _do(session: Session):
            session.execute(
                update(BrokerSession)
                .where(BrokerSession.session_id == session_id, BrokerSession.unknown_reason.is_(None))
                .values(unknown_reason=reason[:500])
            )

        self._run(_do)

    def open_sessions(self) -> List[SessionRecord]:
        def _do(session: Session):
            rows = session.scalars(select(BrokerSession).where(BrokerSession.state != BROKER_SESSION_CLOSED)).all()
            return [_session_of(r) for r in rows]

        return self._run(_do)

    # ── tentatives ───────────────────────────────────────────────────────────────────────────────────────

    def create_attempt(
        self,
        *,
        attempt_id: str,
        request_id: Optional[str],
        session_id: Optional[str],
        scope_key: str,
        role: str,
        model: str,
        request_sha256: str,
        output_cap: int,
        owner_id: Optional[str] = None,
    ) -> Tuple[AttemptRecord, bool]:
        """Crée la tentative ``prepared`` ; ``(enregistrement, rejouée)``. Un ``request_id`` rejoué est le MÊME événement."""

        def _find(session: Session):
            if request_id is None:
                return None
            return session.scalar(
                select(BrokerAttempt).where(
                    BrokerAttempt.scope_key == scope_key, BrokerAttempt.request_id == request_id
                )
            )

        def _session_pk(session: Session) -> Optional[int]:
            if session_id is None:
                return None
            return session.scalar(select(BrokerSession.id).where(BrokerSession.session_id == session_id))

        def _do(session: Session):
            existing = _find(session)
            if existing is not None:
                if existing.request_sha256 != request_sha256:
                    raise BrokerRequestRefused(
                        "request_id réutilisé avec un contenu différent : un identifiant d'idempotence ne change pas de sens",
                        code="request_id_conflict",
                        status=409,
                    )
                return _attempt_of(existing, session_id), True
            row = BrokerAttempt(
                attempt_id=attempt_id,
                request_id=request_id,
                owner_id=owner_id,
                session_id=_session_pk(session),
                scope_key=scope_key,
                role=role,
                model=model,
                request_sha256=request_sha256,
                state=BROKER_ATTEMPT_PREPARED,
                output_cap=output_cap,
            )
            session.add(row)
            session.flush()
            return _attempt_of(row, session_id), False

        try:
            return self._run(_do)
        except IntegrityError as conflict:
            original = conflict  # le nom lié par ``as`` disparaît à la sortie du bloc ``except``

            # Course sur le même (scope, request_id) : l'autre appelant a gagné — rejeu SEULEMENT si c'est le même contenu.
            def _again(session: Session):
                existing = _find(session)
                if existing is None:
                    raise original
                if existing.request_sha256 != request_sha256:
                    raise BrokerRequestRefused(
                        "request_id réutilisé avec un contenu différent", code="request_id_conflict", status=409
                    )
                return _attempt_of(existing, session_id), True

            return self._run(_again)

    def find_attempt(self, scope_key: str, request_id: str) -> Optional[AttemptRecord]:
        """La tentative déjà enregistrée pour ``(scope, request_id)`` — le rejeu d'un même événement."""

        def _do(session: Session):
            row = session.scalar(
                select(BrokerAttempt).where(
                    BrokerAttempt.scope_key == scope_key, BrokerAttempt.request_id == request_id
                )
            )
            return None if row is None else _attempt_of(row, None)

        return self._run(_do)

    def get_attempt(self, attempt_id: str) -> Optional[AttemptRecord]:
        def _do(session: Session):
            row = session.scalar(select(BrokerAttempt).where(BrokerAttempt.attempt_id == attempt_id))
            if row is None:
                return None
            sid = (
                session.scalar(select(BrokerSession.session_id).where(BrokerSession.id == row.session_id))
                if row.session_id
                else None
            )
            return _attempt_of(row, sid)

        return self._run(_do)

    def _transition(self, attempt_id: str, allowed: Tuple[str, ...], **values) -> bool:
        def _do(session: Session):
            done = session.execute(
                update(BrokerAttempt)
                .where(BrokerAttempt.attempt_id == attempt_id, BrokerAttempt.state.in_(allowed))
                .values(**values)
            )
            return done.rowcount == 1

        return self._run(_do)

    def reopen_released(self, attempt_id: str, *, owner_id: Optional[str] = None) -> bool:
        """Rouvre une tentative ``released`` (absence d'émission ÉTABLIE) pour un renvoi du MÊME ``request_id`` (CAS).

        ``runs`` augmente : la réservation de la nouvelle exécution a un identifiant distinct de celle, libérée, de la précédente.
        """
        return self._transition(
            attempt_id,
            (BROKER_ATTEMPT_RELEASED,),
            state=BROKER_ATTEMPT_PREPARED,
            runs=BrokerAttempt.runs + 1,
            owner_id=owner_id,
            reservation_id=None,
            counted_tokens=None,
            reserved_tokens=0,
            error_code=None,
            error_detail=None,
            settled_at=None,
        )

    def attach_reservation(
        self, attempt_id: str, *, reservation_id: str, counted_tokens: int, reserved_tokens: int
    ) -> bool:
        return self._transition(
            attempt_id,
            (BROKER_ATTEMPT_PREPARED,),
            reservation_id=reservation_id,
            counted_tokens=counted_tokens,
            reserved_tokens=reserved_tokens,
        )

    def mark_emitting(self, attempt_id: str, now: datetime) -> bool:
        """Écrit ``emitting`` AVANT l'envoi. ``False`` si la tentative n'est plus ``prepared`` : ne PAS émettre."""
        return self._transition(attempt_id, (BROKER_ATTEMPT_PREPARED,), state=BROKER_ATTEMPT_EMITTING, emitted_at=now)

    def admit_emission(
        self,
        attempt_id: str,
        now: datetime,
        *,
        session_id: Optional[str],
        scope_keys: Tuple[str, ...],
        parent_reservation_id: Optional[str],
    ) -> Tuple[bool, str]:
        """Admission TRANSACTIONNELLE à l'émission : ``prepared`` → ``emitting`` si, DANS LA MÊME transaction, tout est encore valide.

        Revérifie, sous verrou de ligne (``FOR UPDATE`` sur PostgreSQL ; sérialisation d'écriture de SQLite, avec nouvelle
        tentative sur conflit), au point exact de l'émission : aucun scope concerné (enfant, parent / racine) n'est bloqué, la
        session est toujours ``open``, sans inconnue et dans son échéance, la réservation parent est toujours ``reserved``, la
        réservation de la tentative existe (``reserved``). Un blocage / une fermeture qui précèdent cette transaction l'emportent ;
        ceux qui la suivent trouvent une émission déjà marquée (en vol, donc légitime). ``(False, motif)`` ⇒ ne RIEN émettre.
        """

        def _do(session: Session):
            _begin_write(session)
            scopes = session.scalars(
                select(BudgetScope)
                .where(BudgetScope.scope_key.in_(scope_keys))
                .order_by(BudgetScope.id)
                .with_for_update()
            ).all()
            for scope in scopes:
                if scope.strict and scope.blocked_reason:
                    return (
                        False,
                        f"scope {scope.scope_key} bloqué (usage inconnu, mode strict) : {scope.blocked_reason}",
                    )
            if session_id is not None:
                row = session.scalar(
                    select(BrokerSession).where(BrokerSession.session_id == session_id).with_for_update()
                )
                if row is None or row.state != BROKER_SESSION_OPEN:
                    return False, f"session {'inconnue' if row is None else row.state} : aucune nouvelle génération"
                if row.unknown_reason:
                    return False, f"session bloquée : {row.unknown_reason}"
                if row.deadline_at is not None and now >= row.deadline_at:
                    return False, "échéance de la session atteinte"
            if parent_reservation_id is not None:
                parent = session.scalar(
                    select(BudgetReservation).where(BudgetReservation.reservation_id == parent_reservation_id)
                )
                if parent is None or parent.state != BUDGET_RESERVED:
                    return (
                        False,
                        f"réservation parent {'absente' if parent is None else parent.state} : aucune émission",
                    )
            attempt = session.scalar(select(BrokerAttempt).where(BrokerAttempt.attempt_id == attempt_id))
            if attempt is None or attempt.reservation_id is None:
                return False, "tentative sans réservation"
            if session_id is not None:
                refused = self._model_sequence_refusal(session, row, attempt)
                if refused:
                    return False, f"{FALLBACK_REFUSAL_PREFIX}{refused}"
            reservation = session.scalar(
                select(BudgetReservation).where(BudgetReservation.reservation_id == attempt.reservation_id)
            )
            if reservation is None or reservation.state != BUDGET_RESERVED:
                return False, "réservation de la tentative absente ou déjà réglée"
            done = session.execute(
                update(BrokerAttempt)
                .where(BrokerAttempt.attempt_id == attempt_id, BrokerAttempt.state == BROKER_ATTEMPT_PREPARED)
                .values(state=BROKER_ATTEMPT_EMITTING, emitted_at=now)
            )
            return (True, "") if done.rowcount == 1 else (False, "tentative reprise par une autre exécution")

        return self._run(_do)

    @staticmethod
    def _model_sequence_refusal(session: Session, row: BrokerSession, attempt: BrokerAttempt) -> str:
        """Règle SERVEUR de séquencement des modèles d'une session (le client, même hostile, ne la décide jamais).

        * Le repli (26B) n'est admis qu'APRÈS un antécédent qui l'autorise : la dernière tentative du modèle principal de la
          session est terminée avec une absence d'émission ÉTABLIE (``released`` : refus avant traitement) ou une consommation
          CONNUE (``settled``). Une réservation bornée n'est PAS un antécédent ; sans aucune tentative du principal, pas de repli.
        * Jamais de repli (ni de retour au principal) tant qu'une AUTRE tentative de la session dont le modèle diffère est en
          vol (``emitting``) ou d'usage inconnu : le client a pu perdre la réponse, le fournisseur la traite peut-être encore.
        La vérification est faite dans la transaction d'admission : deux connexions de la même session ne peuvent pas la contourner
        en course. Les autres sessions (autres rôles / allocations) ne sont pas concernées.
        Retourne le motif de refus, ou ``""``.
        """
        others = session.scalars(
            select(BrokerAttempt)
            .where(BrokerAttempt.session_id == row.id, BrokerAttempt.attempt_id != attempt.attempt_id)
            .order_by(BrokerAttempt.id.desc())
        ).all()
        for other in others:
            if other.model != attempt.model and other.state in (BROKER_ATTEMPT_EMITTING, BROKER_ATTEMPT_UNKNOWN):
                status = "en vol" if other.state == BROKER_ATTEMPT_EMITTING else "d'usage inconnu"
                return (
                    f"une génération {other.model} de cette session est {status} : "
                    f"aucune génération {attempt.model} tant que son issue n'est pas établie"
                )
        if attempt.model == FALLBACK_MODEL:
            primary = next((o for o in others if o.model == PRIMARY_MODEL), None)
            if primary is None:
                return f"repli {FALLBACK_MODEL} sans antécédent autorisant : aucune tentative du modèle principal dans la session"
            if primary.state not in (BROKER_ATTEMPT_RELEASED, BROKER_ATTEMPT_SETTLED):
                return (
                    f"repli {FALLBACK_MODEL} refusé : la dernière tentative principale est {primary.state} "
                    "(ni refus établi avant traitement, ni consommation connue)"
                )
        return ""

    def settle(
        self,
        attempt_id: str,
        *,
        now: datetime,
        prompt: int,
        candidates: int,
        thoughts: int,
        total: int,
        response_json: str,
    ) -> bool:
        return self._transition(
            attempt_id,
            (BROKER_ATTEMPT_EMITTING,),
            state=BROKER_ATTEMPT_SETTLED,
            settled_at=now,
            usage_prompt=prompt,
            usage_candidates=candidates,
            usage_thoughts=thoughts,
            usage_total=total,
            response_json=response_json,
        )

    def release(self, attempt_id: str, *, now: datetime, code: str, detail: str) -> bool:
        """Absence d'émission / de consommation ÉTABLIE (avant envoi, ou rejet démontré)."""
        return self._transition(
            attempt_id,
            (BROKER_ATTEMPT_PREPARED, BROKER_ATTEMPT_EMITTING),
            state=BROKER_ATTEMPT_RELEASED,
            settled_at=now,
            error_code=code[:48],
            error_detail=detail[:500],
        )

    def mark_unknown(self, attempt_id: str, *, now: datetime, code: str, detail: str) -> bool:
        return self._transition(
            attempt_id,
            (BROKER_ATTEMPT_EMITTING,),
            state=BROKER_ATTEMPT_UNKNOWN,
            settled_at=now,
            error_code=code[:48],
            error_detail=detail[:500],
        )

    def pending_attempts(
        self,
        *,
        scope_key: Optional[str] = None,
        session_id: Optional[str] = None,
        scope_keys: Optional[Tuple[str, ...]] = None,
    ) -> List[AttemptRecord]:
        """Tentatives ``prepared`` / ``emitting`` (travail interrompu ou en vol), de tous les producteurs ; filtrables par scope(s)."""

        def _do(session: Session):
            query = select(BrokerAttempt).where(
                BrokerAttempt.state.in_((BROKER_ATTEMPT_PREPARED, BROKER_ATTEMPT_EMITTING))
            )
            if scope_key is not None:
                query = query.where(BrokerAttempt.scope_key == scope_key)
            if scope_keys is not None:
                query = query.where(BrokerAttempt.scope_key.in_(scope_keys))
            if session_id is not None:
                pk = session.scalar(select(BrokerSession.id).where(BrokerSession.session_id == session_id))
                query = query.where(BrokerAttempt.session_id == pk)
            rows = session.scalars(query.order_by(BrokerAttempt.id)).all()
            sessions = {
                r.id: sid
                for r, sid in (
                    (r, session.scalar(select(BrokerSession.session_id).where(BrokerSession.id == r.session_id)))
                    for r in rows
                    if r.session_id
                )
            }
            return [_attempt_of(r, session_id or sessions.get(r.id)) for r in rows]

        return self._run(_do)

    def scope_keys_with_attempts(self) -> List[str]:
        """Scopes portant au moins une tentative (parcours de réparation globale au démarrage)."""

        def _do(session: Session):
            return sorted(set(session.scalars(select(BrokerAttempt.scope_key)).all()))

        return self._run(_do)

    def attempts(self, *, scope_key: str) -> List[AttemptRecord]:
        def _do(session: Session):
            rows = session.scalars(
                select(BrokerAttempt).where(BrokerAttempt.scope_key == scope_key).order_by(BrokerAttempt.id)
            ).all()
            return [_attempt_of(r, None) for r in rows]

        return self._run(_do)

    # ── horloge globale ──────────────────────────────────────────────────────────────────────────────────

    def ensure_clock(self, scope_key: str, seconds: int, now: datetime) -> datetime:
        """Ouvre l'échéance globale à la PREMIÈRE ouverture réelle (CAS) et la renvoie ; jamais remise à zéro."""

        def _read(session: Session):
            return session.scalar(select(BrokerClock).where(BrokerClock.scope_key == scope_key))

        def _do(session: Session):
            row = _read(session)
            if row is None:
                row = BrokerClock(
                    scope_key=scope_key,
                    seconds=int(seconds),
                    opened_at=now,
                    deadline_at=now + timedelta(seconds=int(seconds)),
                )
                session.add(row)
                session.flush()
            return row.deadline_at

        try:
            return self._run(_do)
        except IntegrityError:
            return self._run(lambda session: _read(session).deadline_at)

    def clock_deadline(self, scope_key: str) -> Optional[datetime]:
        def _do(session: Session):
            row = session.scalar(select(BrokerClock).where(BrokerClock.scope_key == scope_key))
            return None if row is None else row.deadline_at

        return self._run(_do)

    # ── propriétaires (instances de service) ─────────────────────────────────────────────────────────────

    def register_owner(self, owner_id: str, now: datetime) -> None:
        """Déclare cette instance (processus) comme propriétaire de ses tentatives ; idempotent."""
        host, pid = socket.gethostname(), os.getpid()
        ticks = process_start_ticks(pid)

        def _do(session: Session):
            if session.scalar(select(BrokerOwner.id).where(BrokerOwner.owner_id == owner_id)) is None:
                session.add(
                    BrokerOwner(
                        owner_id=owner_id, host=host, pid=pid, start_ticks=ticks, created_at=now, heartbeat_at=now
                    )
                )

        try:
            self._run(_do)
        except IntegrityError:
            pass  # enregistrée par un appel concurrent de la même instance

    def heartbeat(self, owner_id: str, now: datetime) -> None:
        def _do(session: Session):
            session.execute(update(BrokerOwner).where(BrokerOwner.owner_id == owner_id).values(heartbeat_at=now))

        self._run(_do)

    def end_owner(self, owner_id: str, now: datetime) -> None:
        def _do(session: Session):
            session.execute(
                update(BrokerOwner)
                .where(BrokerOwner.owner_id == owner_id, BrokerOwner.ended_at.is_(None))
                .values(ended_at=now)
            )

        self._run(_do)

    def get_owner(self, owner_id: Optional[str]) -> Optional[OwnerRecord]:
        if owner_id is None:
            return None

        def _do(session: Session):
            row = session.scalar(select(BrokerOwner).where(BrokerOwner.owner_id == owner_id))
            if row is None:
                return None
            return OwnerRecord(row.owner_id, row.host, int(row.pid), row.start_ticks, row.heartbeat_at, row.ended_at)

        return self._run(_do)
