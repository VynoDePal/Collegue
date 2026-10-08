"""Registre budgétaire durable et transactionnel (vague 2).

**Autorité** de la dépense LLM d'un projet/cycle, commune à la planification, BUILD,
IMPROVE, au sampling de tous les rôles, aux retries, aux replis de modèle et aux reprises.
Les métriques (``MetricsCollector``) et l'audit (``RunAuditLog``) n'en sont que des
projections d'affichage : plus aucune décision de budget n'en dépend.

Interface publique
------------------
- :meth:`BudgetLedger.reserve` — réserve AVANT toute dépense, sous un identifiant durable
  unique (``reservation_id``). Atomique : un ``UPDATE`` conditionnel à ``consommé + réservé +
  inconnu + montant <= plafond`` (compare-and-set) dans la même transaction que l'insertion
  de la réservation. Valable SQLite ET PostgreSQL — aucun verrou process, aucun
  lire-puis-écrire en Python. Rejouer le même ``reservation_id`` renvoie la réservation
  existante sans rien réserver de plus.
- :meth:`BudgetLedger.commit` — engage la consommation réelle, idempotent (``event_key``
  unique) ; libère le reliquat réservé non consommé (la consommation réelle est établie).
- :meth:`BudgetLedger.release` — libère UNIQUEMENT quand l'absence de consommation est
  établie (ex. réponse d'erreur du fournisseur).
- :meth:`BudgetLedger.mark_unknown` — usage inconnu : la réservation est CONSERVÉE comme borne
  haute et, en mode strict, le scope est bloqué avec un motif durable.
- :meth:`BudgetLedger.snapshot` — consommé / réservé / inconnu / solde / motif de blocage.

Précision monétaire
-------------------
Entiers en micro-USD (1e-6 $), arrondis TOUJOURS vers le haut (``usd_to_micro``) : jamais
sous-estimé. Les plafonds sont arrondis vers le bas.

Hypothèses (documentées dans ``docs/consolidation/w2-budget.md``) : un fournisseur qui a
répondu en erreur HTTP n'a rien facturé ; un appel interrompu après émission (timeout,
annulation) est d'usage INCONNU.
"""

from __future__ import annotations

import math
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Callable, Optional

from sqlalchemy import and_, case, exists, func, or_, select, update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session, sessionmaker

from collegue.state.models import (
    BUDGET_COMMITTED,
    BUDGET_RELEASED,
    BUDGET_RESERVED,
    BUDGET_UNKNOWN,
    BudgetEvent,
    BudgetReservation,
    BudgetScope,
    Metric,
)

MICRO = 1_000_000

# Codes de refus (``BudgetRefused.code``).
REFUSED_CAP_USD = "cap_usd"
REFUSED_CAP_TOKENS = "cap_tokens"
REFUSED_BLOCKED = "blocked_unknown_usage"
REFUSED_LEDGER = "ledger_unavailable"
REFUSED_UNBOUNDED = "unbounded_transport"
REFUSED_DEADLINE = "deadline"
REFUSED_SCOPE = "scope_missing"

# Noms des métriques historiques importées UNE fois (snapshots cumulatifs ordonnés par id).
LEGACY_COST_METRIC = "run_cost_usd"
LEGACY_TOKENS_METRIC = "run_tokens"


class BudgetRefused(BaseException):
    """Réservation/dépense REFUSÉE avant émission (plafond, blocage, ledger indisponible…).

    Hérite de ``BaseException`` à dessein, comme ``BudgetExceeded`` (C4) : les chemins LLM
    enveloppent ``ctx.sample()`` dans des ``except Exception`` qui dégraderaient un refus en
    simple « erreur LLM » récupérable — l'auto-pause deviendrait illusoire. Le pilote
    la convertit en arrêt ``paused_budget`` à la frontière d'une tâche.
    """

    def __init__(self, code: str, message: str, *, snapshot: Optional["ScopeSnapshot"] = None):
        self.code = code
        self.snapshot = snapshot
        super().__init__(message)


class BudgetLedgerError(RuntimeError):
    """Incohérence ou conflit du registre (double règlement avec une autre clé, état invalide…)."""


def usd_to_micro(value) -> int:
    """USD → micro-USD, arrondi vers le HAUT (conservateur). Refuse NaN/inf/négatif/bool."""
    if isinstance(value, bool):
        raise ValueError("montant USD invalide (bool)")
    try:
        amount = Decimal(str(value))
    except Exception as exc:  # noqa: BLE001 - montant illisible
        raise ValueError(f"montant USD illisible: {value!r}") from exc
    if not amount.is_finite() or amount < 0:
        raise ValueError(f"montant USD invalide: {value!r}")
    return int((amount * MICRO).to_integral_value(rounding=ROUND_CEILING))


def cap_to_micro(value) -> Optional[int]:
    """Plafond USD → micro-USD, arrondi vers le BAS (conservateur). ``None``/<=0 → pas de plafond."""
    if value is None:
        return None
    try:
        amount = Decimal(str(value))
    except Exception:  # noqa: BLE001
        return None
    if not amount.is_finite() or amount <= 0:
        return None
    return int((amount * MICRO).to_integral_value(rounding=ROUND_FLOOR))


def micro_to_usd(micro: int) -> float:
    return int(micro) / MICRO


def _tokens(value) -> int:
    if isinstance(value, bool):
        raise ValueError("nombre de tokens invalide (bool)")
    if isinstance(value, float):
        if not math.isfinite(value) or value < 0:
            raise ValueError(f"nombre de tokens invalide: {value!r}")
        return math.ceil(value)
    number = int(value)
    if number < 0:
        raise ValueError(f"nombre de tokens négatif: {value!r}")
    return number


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _block_expr(reason: str):
    """``blocked_reason`` après un usage inconnu : motif durable (le plus ancien est conservé), mais
    seulement si le scope a un plafond — sans plafond il n'y a rien à protéger (l'inconnu reste tracé)."""
    return case(
        (
            and_(BudgetScope.cap_micro_usd.is_(None), BudgetScope.cap_tokens.is_(None)),
            BudgetScope.blocked_reason,
        ),
        else_=func.coalesce(BudgetScope.blocked_reason, reason),
    )


@dataclass(frozen=True)
class ScopeSnapshot:
    """Photo cohérente d'un scope (une seule lecture)."""

    scope_key: str
    project_id: Optional[int]
    kind: str
    strict: bool
    cap_micro_usd: Optional[int]
    cap_tokens: Optional[int]
    consumed_micro_usd: int
    consumed_tokens: int
    reserved_micro_usd: int
    reserved_tokens: int
    unknown_micro_usd: int
    unknown_tokens: int
    blocked_reason: Optional[str]
    last_error: Optional[str]
    revision: int

    @property
    def used_micro_usd(self) -> int:
        """Borne haute de la dépense : consommé + réservé + inconnu (jamais sous-estimée)."""
        return self.consumed_micro_usd + self.reserved_micro_usd + self.unknown_micro_usd

    @property
    def used_tokens(self) -> int:
        return self.consumed_tokens + self.reserved_tokens + self.unknown_tokens

    @property
    def consumed_usd(self) -> float:
        return micro_to_usd(self.consumed_micro_usd)

    @property
    def reserved_usd(self) -> float:
        return micro_to_usd(self.reserved_micro_usd)

    @property
    def unknown_usd(self) -> float:
        return micro_to_usd(self.unknown_micro_usd)

    @property
    def spent_usd(self) -> float:
        """Dépense affichée = borne haute (consommé + réservé + inconnu)."""
        return micro_to_usd(self.used_micro_usd)

    @property
    def balance_micro_usd(self) -> Optional[int]:
        return None if self.cap_micro_usd is None else self.cap_micro_usd - self.used_micro_usd

    @property
    def balance_usd(self) -> Optional[float]:
        balance = self.balance_micro_usd
        return None if balance is None else micro_to_usd(balance)

    @property
    def balance_tokens(self) -> Optional[int]:
        return None if self.cap_tokens is None else self.cap_tokens - self.used_tokens

    @property
    def exhausted(self) -> bool:
        """Vrai si un plafond est atteint ou dépassé (même sémantique ``>=`` que le garde C4)."""
        usd = self.balance_micro_usd
        tokens = self.balance_tokens
        return (usd is not None and usd <= 0) or (tokens is not None and tokens <= 0)

    @property
    def blocked(self) -> bool:
        return bool(self.strict and self.blocked_reason)

    def to_dict(self) -> dict:
        return {
            "scope_key": self.scope_key,
            "project_id": self.project_id,
            "strict": self.strict,
            "cap_usd": None if self.cap_micro_usd is None else micro_to_usd(self.cap_micro_usd),
            "cap_tokens": self.cap_tokens,
            "consumed_usd": self.consumed_usd,
            "consumed_tokens": self.consumed_tokens,
            "reserved_usd": self.reserved_usd,
            "reserved_tokens": self.reserved_tokens,
            "unknown_usd": self.unknown_usd,
            "unknown_tokens": self.unknown_tokens,
            "balance_usd": self.balance_usd,
            "balance_tokens": self.balance_tokens,
            "blocked_reason": self.blocked_reason,
            "last_error": self.last_error,
            "revision": self.revision,
        }


@dataclass(frozen=True)
class Reservation:
    """Réservation durable. ``replayed`` : le ``reservation_id`` existait déjà (rejeu)."""

    reservation_id: str
    scope_key: str
    kind: str
    state: str
    reserved_micro_usd: int
    reserved_tokens: int
    consumed_micro_usd: int
    consumed_tokens: int
    expires_at: Optional[datetime]
    replayed: bool = False

    @property
    def reserved_usd(self) -> float:
        return micro_to_usd(self.reserved_micro_usd)


@dataclass(frozen=True)
class Settlement:
    """Résultat d'un règlement. ``replayed`` : la clé d'événement était déjà appliquée."""

    reservation_id: str
    state: str
    consumed_micro_usd: int
    consumed_tokens: int
    replayed: bool = False

    @property
    def consumed_usd(self) -> float:
        return micro_to_usd(self.consumed_micro_usd)


_SQLITE_BUSY_RETRIES = 6


def _is_busy(exc: OperationalError) -> bool:
    text = str(getattr(exc, "orig", exc)).lower()
    return "database is locked" in text or "database table is locked" in text


class BudgetLedger:
    """Service transactionnel du registre. Ne garde AUCUN état de budget en mémoire."""

    def __init__(self, session_factory: sessionmaker, *, clock: Optional[Callable[[], datetime]] = None):
        self._session_factory = session_factory
        self._clock = clock or _utcnow

    # ── infrastructure transactionnelle ────────────────────────────────────────────

    def _run(self, fn: Callable[[Session], object]):
        """Exécute ``fn`` dans une transaction (commit/rollback), avec nouvelle tentative si
        SQLite est verrouillé par un autre écrivain. Toute autre erreur se propage."""
        delay = 0.02
        for attempt in range(_SQLITE_BUSY_RETRIES):
            session = self._session_factory()
            try:
                result = fn(session)
                session.commit()
                return result
            except OperationalError as exc:
                session.rollback()
                if _is_busy(exc) and attempt < _SQLITE_BUSY_RETRIES - 1:
                    time.sleep(delay)
                    delay = min(delay * 2, 0.5)
                    continue
                raise
            except BaseException:
                session.rollback()
                raise
            finally:
                session.close()
        raise BudgetLedgerError("registre budgétaire verrouillé")  # pragma: no cover

    # ── scopes ─────────────────────────────────────────────────────────────────────

    @staticmethod
    def _snapshot_of(row: BudgetScope) -> ScopeSnapshot:
        return ScopeSnapshot(
            scope_key=row.scope_key,
            project_id=row.project_id,
            kind=row.kind,
            strict=bool(row.strict),
            cap_micro_usd=row.cap_micro_usd,
            cap_tokens=row.cap_tokens,
            consumed_micro_usd=int(row.consumed_micro_usd),
            consumed_tokens=int(row.consumed_tokens),
            reserved_micro_usd=int(row.reserved_micro_usd),
            reserved_tokens=int(row.reserved_tokens),
            unknown_micro_usd=int(row.unknown_micro_usd),
            unknown_tokens=int(row.unknown_tokens),
            blocked_reason=row.blocked_reason,
            last_error=row.last_error,
            revision=int(row.revision),
        )

    def snapshot(self, scope_key: str) -> ScopeSnapshot:
        def _read(session: Session):
            row = session.scalar(select(BudgetScope).where(BudgetScope.scope_key == scope_key))
            if row is None:
                raise BudgetRefused(REFUSED_SCOPE, f"scope budgétaire inconnu: {scope_key}")
            return self._snapshot_of(row)

        return self._run(_read)

    def snapshot_for_project(self, project_id: int) -> Optional[ScopeSnapshot]:
        def _read(session: Session):
            row = session.scalar(select(BudgetScope).where(BudgetScope.project_id == project_id))
            return None if row is None else self._snapshot_of(row)

        return self._run(_read)

    def create_planning_scope(
        self,
        *,
        max_cost_usd=None,
        max_tokens=None,
        strict: bool = True,
        scope_key: Optional[str] = None,
    ) -> ScopeSnapshot:
        """Contexte durable créé AVANT la première dépense d'un projet qui n'existe pas encore."""
        key = scope_key or f"planning:{uuid.uuid4().hex}"
        return self._open(
            key, project_id=None, kind="planning", max_cost_usd=max_cost_usd, max_tokens=max_tokens, strict=strict
        )

    def scope_for_project(
        self, project_id: int, *, max_cost_usd=None, max_tokens=None, strict: bool = True
    ) -> ScopeSnapshot:
        """Scope d'un projet : retrouvé (y compris un scope de planification lié) ou créé, avec
        IMPORT UNIQUE des cumuls historiques ``run_cost_usd``/``run_tokens``. Les plafonds et le
        mode strict sont mis à jour depuis la configuration courante."""
        return self._open(
            f"project:{int(project_id)}",
            project_id=int(project_id),
            kind="project",
            max_cost_usd=max_cost_usd,
            max_tokens=max_tokens,
            strict=strict,
        )

    def _open(self, key, *, project_id, kind, max_cost_usd, max_tokens, strict) -> ScopeSnapshot:
        cap_usd = cap_to_micro(max_cost_usd)
        cap_tok = None if not max_tokens or int(max_tokens) <= 0 else int(max_tokens)

        def _do(session: Session):
            row = None
            if project_id is not None:
                row = session.scalar(select(BudgetScope).where(BudgetScope.project_id == project_id))
            if row is None:
                row = session.scalar(select(BudgetScope).where(BudgetScope.scope_key == key))
            if row is None:
                row = BudgetScope(
                    scope_key=key,
                    project_id=project_id,
                    kind=kind,
                    cap_micro_usd=cap_usd,
                    cap_tokens=cap_tok,
                    strict=bool(strict),
                )
                try:
                    session.add(row)
                    session.flush()
                    if project_id is not None:
                        self._import_legacy(session, row)
                except IntegrityError:
                    # Concurrent : un autre processus a créé (et importé) le scope. On le relit.
                    session.rollback()
                    row = session.scalar(
                        select(BudgetScope).where(
                            or_(BudgetScope.scope_key == key, BudgetScope.project_id == project_id)
                            if project_id is not None
                            else BudgetScope.scope_key == key
                        )
                    )
                    if row is None:
                        raise
            else:
                # Les plafonds/mode viennent de la configuration COURANTE : un opérateur peut
                # les relever. Écriture directe (pas de lecture-modification en Python).
                session.execute(
                    update(BudgetScope)
                    .where(BudgetScope.id == row.id)
                    .values(
                        cap_micro_usd=cap_usd,
                        cap_tokens=cap_tok,
                        strict=bool(strict),
                        revision=BudgetScope.revision + 1,
                    )
                )
                session.refresh(row)
            self._recover_expired(session, row.id)
            session.flush()
            session.refresh(row)
            return self._snapshot_of(row)

        return self._run(_do)

    def _import_legacy(self, session: Session, scope: BudgetScope) -> None:
        """Importe UNE fois les cumuls historiques (snapshots cumulatifs, pas des deltas).

        ``run_cost_usd``/``run_tokens`` sont écrits par l'ancien audit comme des totaux
        croissants ordonnés par ``id`` : seule la DERNIÈRE valeur de chaque nom compte (les
        sommer compterait N fois). L'unicité de ``scope_key``/``project_id`` rend l'import
        exactement-une-fois ; l'événement d'import porte une clé unique de plus.
        """
        usd = tokens = 0.0
        for metric in session.scalars(select(Metric).where(Metric.project_id == scope.project_id).order_by(Metric.id)):
            if not math.isfinite(metric.value) or metric.value < 0:
                continue
            if metric.name == LEGACY_COST_METRIC:
                usd = float(metric.value)
            elif metric.name == LEGACY_TOKENS_METRIC:
                tokens = float(metric.value)
        micro, tok = usd_to_micro(usd), _tokens(tokens)
        if not micro and not tok:
            return
        rid = f"legacy-import:{scope.scope_key}"
        session.add(
            BudgetReservation(
                reservation_id=rid,
                scope_id=scope.id,
                kind="import",
                role="legacy",
                transport="metrics",
                state=BUDGET_COMMITTED,
                reserved_micro_usd=micro,
                reserved_tokens=tok,
                consumed_micro_usd=micro,
                consumed_tokens=tok,
                reason="import unique des cumuls run_cost_usd/run_tokens",
            )
        )
        session.add(
            BudgetEvent(
                event_key=f"import:{scope.scope_key}",
                scope_id=scope.id,
                reservation_id=rid,
                kind="import",
                micro_usd=micro,
                tokens=tok,
            )
        )
        scope.consumed_micro_usd = micro
        scope.consumed_tokens = tok
        session.flush()

    def bind_project(self, scope_key: str, project_id: int) -> ScopeSnapshot:
        """Lie un scope de planification au projet créé ensuite. Idempotent."""

        def _do(session: Session):
            row = session.scalar(select(BudgetScope).where(BudgetScope.scope_key == scope_key))
            if row is None:
                raise BudgetRefused(REFUSED_SCOPE, f"scope budgétaire inconnu: {scope_key}")
            if row.project_id == project_id:
                return self._snapshot_of(row)
            if row.project_id is not None:
                raise BudgetLedgerError(f"scope {scope_key} déjà lié au projet {row.project_id}")
            other = session.scalar(select(BudgetScope).where(BudgetScope.project_id == project_id))
            if other is not None:
                raise BudgetLedgerError(f"le projet {project_id} possède déjà le scope {other.scope_key}")
            session.execute(
                update(BudgetScope)
                .where(BudgetScope.id == row.id, BudgetScope.project_id.is_(None))
                .values(project_id=project_id, revision=BudgetScope.revision + 1)
            )
            session.refresh(row)
            return self._snapshot_of(row)

        return self._run(_do)

    def note_failure(self, scope_key: str, message: str) -> None:
        """Conserve durablement l'échec d'une étape (ex. planification) : sa dépense reste au ledger."""

        def _do(session: Session):
            row = session.scalar(select(BudgetScope).where(BudgetScope.scope_key == scope_key))
            if row is None:
                return
            session.execute(
                update(BudgetScope)
                .where(BudgetScope.id == row.id)
                .values(last_error=str(message)[:2000], revision=BudgetScope.revision + 1)
            )
            key = f"note:{scope_key}:{uuid.uuid4().hex}"
            session.add(BudgetEvent(event_key=key, scope_id=row.id, kind="note", detail=str(message)[:2000]))

        self._run(_do)

    # ── réservation ────────────────────────────────────────────────────────────────

    def reserve(
        self,
        scope_key: str,
        *,
        usd=0.0,
        tokens: int = 0,
        micro_usd: Optional[int] = None,
        kind: str = "call",
        role: str = "",
        model: str = "",
        transport: str = "",
        reservation_id: Optional[str] = None,
        ttl_seconds: Optional[float] = None,
        expires_at: Optional[datetime] = None,
    ) -> Reservation:
        """Réserve ``usd``/``tokens`` AVANT une dépense. Atomique (CAS), idempotent sur ``reservation_id``.

        Lève :class:`BudgetRefused` si le plafond serait dépassé, si le scope est bloqué par un
        usage inconnu (mode strict), ou si le registre est indisponible. En cas d'erreur de
        persistance, RIEN n'est émis : l'appelant ne doit pas lancer la dépense.
        """
        if kind not in ("call", "worker"):
            raise ValueError(f"kind invalide: {kind!r}")
        need_micro = int(micro_usd) if micro_usd is not None else usd_to_micro(usd)
        need_tokens = _tokens(tokens)
        rid = reservation_id or f"{kind}:{uuid.uuid4().hex}"
        if expires_at is None and ttl_seconds is not None:
            expires_at = self._clock() + timedelta(seconds=float(ttl_seconds))

        def _do(session: Session):
            scope = session.scalar(select(BudgetScope).where(BudgetScope.scope_key == scope_key))
            if scope is None:
                raise BudgetRefused(REFUSED_SCOPE, f"scope budgétaire inconnu: {scope_key}")
            existing = session.scalar(select(BudgetReservation).where(BudgetReservation.reservation_id == rid))
            if existing is not None:
                return self._reservation_of(existing, scope_key, replayed=True)

            used_usd = BudgetScope.consumed_micro_usd + BudgetScope.reserved_micro_usd + BudgetScope.unknown_micro_usd
            used_tok = BudgetScope.consumed_tokens + BudgetScope.reserved_tokens + BudgetScope.unknown_tokens
            guard = and_(
                BudgetScope.id == scope.id,
                # Strict : refus si bloqué par un usage inconnu ; plafonds seulement en strict.
                or_(BudgetScope.strict.is_(False), BudgetScope.blocked_reason.is_(None)),
                or_(
                    BudgetScope.strict.is_(False),
                    BudgetScope.cap_micro_usd.is_(None),
                    used_usd + need_micro <= BudgetScope.cap_micro_usd,
                ),
                or_(
                    BudgetScope.strict.is_(False),
                    BudgetScope.cap_tokens.is_(None),
                    used_tok + need_tokens <= BudgetScope.cap_tokens,
                ),
            )
            claimed = session.execute(
                update(BudgetScope)
                .where(guard)
                .values(
                    reserved_micro_usd=BudgetScope.reserved_micro_usd + need_micro,
                    reserved_tokens=BudgetScope.reserved_tokens + need_tokens,
                    revision=BudgetScope.revision + 1,
                )
            )
            if claimed.rowcount != 1:
                session.rollback()
                fresh = session.scalar(select(BudgetScope).where(BudgetScope.scope_key == scope_key))
                snap = self._snapshot_of(fresh)
                if snap.strict and snap.blocked_reason:
                    raise BudgetRefused(
                        REFUSED_BLOCKED,
                        f"scope {scope_key} bloqué (usage inconnu, mode strict) : {snap.blocked_reason}",
                        snapshot=snap,
                    )
                if snap.cap_micro_usd is not None and snap.used_micro_usd + need_micro > snap.cap_micro_usd:
                    raise BudgetRefused(
                        REFUSED_CAP_USD,
                        f"plafond USD dépassé avant l'appel : {snap.spent_usd:.6f} + {micro_to_usd(need_micro):.6f} "
                        f"> {micro_to_usd(snap.cap_micro_usd):.6f}",
                        snapshot=snap,
                    )
                raise BudgetRefused(
                    REFUSED_CAP_TOKENS,
                    f"plafond tokens dépassé avant l'appel : {snap.used_tokens} + {need_tokens} > {snap.cap_tokens}",
                    snapshot=snap,
                )
            row = BudgetReservation(
                reservation_id=rid,
                scope_id=scope.id,
                kind=kind,
                role=str(role)[:48],
                model=str(model)[:160],
                transport=str(transport)[:48],
                state=BUDGET_RESERVED,
                reserved_micro_usd=need_micro,
                reserved_tokens=need_tokens,
                expires_at=expires_at,
            )
            session.add(row)
            session.add(
                BudgetEvent(
                    event_key=f"reserve:{rid}",
                    scope_id=scope.id,
                    reservation_id=rid,
                    kind="reserve",
                    micro_usd=need_micro,
                    tokens=need_tokens,
                )
            )
            session.flush()
            return self._reservation_of(row, scope_key, replayed=False)

        try:
            return self._run(_do)
        except IntegrityError:
            # Deux appelants avec le même reservation_id : l'un a gagné, l'autre rejoue.
            def _replay(session: Session):
                existing = session.scalar(select(BudgetReservation).where(BudgetReservation.reservation_id == rid))
                if existing is None:
                    raise BudgetLedgerError(f"contrainte violée sans réservation existante: {rid}")
                return self._reservation_of(existing, scope_key, replayed=True)

            return self._run(_replay)
        except BudgetRefused:
            raise
        except (OperationalError, BudgetLedgerError) as exc:
            raise BudgetRefused(REFUSED_LEDGER, f"registre budgétaire indisponible : {exc}") from exc

    @staticmethod
    def _reservation_of(row: BudgetReservation, scope_key: str, *, replayed: bool) -> Reservation:
        return Reservation(
            reservation_id=row.reservation_id,
            scope_key=scope_key,
            kind=row.kind,
            state=row.state,
            reserved_micro_usd=int(row.reserved_micro_usd),
            reserved_tokens=int(row.reserved_tokens),
            consumed_micro_usd=int(row.consumed_micro_usd),
            consumed_tokens=int(row.consumed_tokens),
            expires_at=row.expires_at,
            replayed=replayed,
        )

    def get_reservation(self, reservation_id: str) -> Optional[Reservation]:
        def _do(session: Session):
            row = session.scalar(select(BudgetReservation).where(BudgetReservation.reservation_id == reservation_id))
            if row is None:
                return None
            scope = session.get(BudgetScope, row.scope_id)
            return self._reservation_of(row, scope.scope_key, replayed=False)

        return self._run(_do)

    # ── règlements (idempotents) ───────────────────────────────────────────────────

    def _settle(
        self,
        reservation_id: str,
        *,
        event_key: str,
        kind: str,
        allowed_from: tuple,
        new_state: str,
        consumed_micro: int = 0,
        consumed_tokens: int = 0,
        reason: Optional[str] = None,
    ) -> Settlement:
        def _do(session: Session):
            row = session.scalar(select(BudgetReservation).where(BudgetReservation.reservation_id == reservation_id))
            if row is None:
                raise BudgetLedgerError(f"réservation inconnue: {reservation_id}")
            prior = session.scalar(select(BudgetEvent).where(BudgetEvent.event_key == event_key))
            if prior is not None:
                if prior.reservation_id != reservation_id:
                    raise BudgetLedgerError(f"clé d'événement {event_key} déjà utilisée pour une autre réservation")
                return Settlement(
                    reservation_id, row.state, int(row.consumed_micro_usd), int(row.consumed_tokens), replayed=True
                )
            if row.state not in allowed_from:
                raise BudgetLedgerError(
                    f"réservation {reservation_id} déjà réglée ({row.state}) : règlement {kind!r} avec une autre "
                    f"clé refusé (ne jamais compter deux fois)"
                )
            from_state = row.state
            session.add(
                BudgetEvent(
                    event_key=event_key,
                    scope_id=row.scope_id,
                    reservation_id=reservation_id,
                    kind=kind,
                    micro_usd=consumed_micro,
                    tokens=consumed_tokens,
                    detail=reason,
                )
            )
            session.flush()  # l'unicité de event_key tranche une course de rejeu AVANT toute mutation
            moved = session.execute(
                update(BudgetReservation)
                .where(BudgetReservation.id == row.id, BudgetReservation.state == from_state)
                .values(
                    state=new_state,
                    consumed_micro_usd=consumed_micro,
                    consumed_tokens=consumed_tokens,
                    reason=(reason or row.reason),
                    updated_at=self._clock(),
                )
            )
            if moved.rowcount != 1:
                raise BudgetLedgerError(f"règlement concurrent sur la réservation {reservation_id}")
            values: dict = {"revision": BudgetScope.revision + 1}
            held_usd, held_tok = int(row.reserved_micro_usd), int(row.reserved_tokens)
            if from_state == BUDGET_RESERVED:
                values["reserved_micro_usd"] = BudgetScope.reserved_micro_usd - held_usd
                values["reserved_tokens"] = BudgetScope.reserved_tokens - held_tok
            else:  # BUDGET_UNKNOWN
                values["unknown_micro_usd"] = BudgetScope.unknown_micro_usd - held_usd
                values["unknown_tokens"] = BudgetScope.unknown_tokens - held_tok
            if new_state == BUDGET_COMMITTED:
                values["consumed_micro_usd"] = BudgetScope.consumed_micro_usd + consumed_micro
                values["consumed_tokens"] = BudgetScope.consumed_tokens + consumed_tokens
            elif new_state == BUDGET_UNKNOWN:
                values["unknown_micro_usd"] = BudgetScope.unknown_micro_usd + held_usd
                values["unknown_tokens"] = BudgetScope.unknown_tokens + held_tok
                values["blocked_reason"] = _block_expr(reason or "usage inconnu")
            session.execute(update(BudgetScope).where(BudgetScope.id == row.scope_id).values(**values))
            if from_state == BUDGET_UNKNOWN and new_state != BUDGET_UNKNOWN:
                self._clear_block_if_resolved(session, row.scope_id)
            return Settlement(reservation_id, new_state, consumed_micro, consumed_tokens, replayed=False)

        try:
            return self._run(_do)
        except IntegrityError:
            # Course de rejeu : la même clé vient d'être appliquée par un autre appelant.
            def _replay(session: Session):
                row = session.scalar(
                    select(BudgetReservation).where(BudgetReservation.reservation_id == reservation_id)
                )
                return Settlement(
                    reservation_id, row.state, int(row.consumed_micro_usd), int(row.consumed_tokens), replayed=True
                )

            return self._run(_replay)

    @staticmethod
    def _clear_block_if_resolved(session: Session, scope_id: int) -> None:
        still_unknown = exists().where(
            BudgetReservation.scope_id == scope_id, BudgetReservation.state == BUDGET_UNKNOWN
        )
        session.execute(
            update(BudgetScope)
            .where(BudgetScope.id == scope_id, ~still_unknown)
            .values(blocked_reason=None, revision=BudgetScope.revision + 1)
        )

    def commit(
        self, reservation_id: str, *, usd=0.0, tokens: int = 0, micro_usd: Optional[int] = None, event_key=None
    ) -> Settlement:
        """Engage la consommation RÉELLE (la dépense est établie). Libère le reliquat réservé.

        Idempotent : ``event_key`` (défaut ``commit:<reservation_id>``) rejoué = aucun effet.
        La consommation réelle peut dépasser la réservation (le fournisseur a dépassé
        l'estimation) : elle est enregistrée intégralement, jamais tronquée.
        """
        micro = int(micro_usd) if micro_usd is not None else usd_to_micro(usd)
        return self._settle(
            reservation_id,
            event_key=event_key or f"commit:{reservation_id}",
            kind="commit",
            allowed_from=(BUDGET_RESERVED, BUDGET_UNKNOWN),
            new_state=BUDGET_COMMITTED,
            consumed_micro=micro,
            consumed_tokens=_tokens(tokens),
        )

    def release(self, reservation_id: str, *, reason: str, event_key=None) -> Settlement:
        """Libère la réservation : À N'UTILISER QUE si l'absence de consommation est ÉTABLIE."""
        return self._settle(
            reservation_id,
            event_key=event_key or f"release:{reservation_id}",
            kind="release",
            allowed_from=(BUDGET_RESERVED,),
            new_state=BUDGET_RELEASED,
            reason=reason,
        )

    def mark_unknown(self, reservation_id: str, *, reason: str, event_key=None) -> Settlement:
        """Usage inconnu : conserve la réservation (borne haute) et, en strict, bloque le scope."""
        return self._settle(
            reservation_id,
            event_key=event_key or f"unknown:{reservation_id}",
            kind="unknown",
            allowed_from=(BUDGET_RESERVED,),
            new_state=BUDGET_UNKNOWN,
            reason=reason,
        )

    def resolve_unknown(
        self, reservation_id: str, *, usd=0.0, tokens: int = 0, micro_usd=None, event_key: str, reason: str = ""
    ) -> Settlement:
        """Résout un usage inconnu avec la consommation réelle établie (facture, relevé fournisseur)."""
        micro = int(micro_usd) if micro_usd is not None else usd_to_micro(usd)
        return self._settle(
            reservation_id,
            event_key=event_key,
            kind="resolve",
            allowed_from=(BUDGET_UNKNOWN,),
            new_state=BUDGET_COMMITTED,
            consumed_micro=micro,
            consumed_tokens=_tokens(tokens),
            reason=reason or "résolu par l'opérateur",
        )

    # ── reprise après crash ────────────────────────────────────────────────────────

    def recover_expired(self, scope_key: str) -> int:
        """Réservations restées ``reserved`` après leur échéance (crash/abandon) → ``unknown``."""

        def _do(session: Session):
            scope = session.scalar(select(BudgetScope).where(BudgetScope.scope_key == scope_key))
            if scope is None:
                return 0
            return self._recover_expired(session, scope.id)

        return self._run(_do)

    def _recover_expired(self, session: Session, scope_id: int) -> int:
        now = self._clock()
        stale = list(
            session.scalars(
                select(BudgetReservation).where(
                    BudgetReservation.scope_id == scope_id,
                    BudgetReservation.state == BUDGET_RESERVED,
                    BudgetReservation.expires_at.is_not(None),
                    BudgetReservation.expires_at < now,
                )
            )
        )
        converted = 0
        for row in stale:
            key = f"unknown:{row.reservation_id}"
            if session.scalar(select(BudgetEvent).where(BudgetEvent.event_key == key)) is not None:
                continue
            reason = (
                f"réservation {row.reservation_id} ({row.kind}/{row.role or '?'}) non réglée à l'échéance : "
                "crash, abandon ou dépense non comptabilisée — usage inconnu"
            )
            session.add(
                BudgetEvent(
                    event_key=key,
                    scope_id=scope_id,
                    reservation_id=row.reservation_id,
                    kind="unknown",
                    detail=reason,
                )
            )
            session.flush()
            moved = session.execute(
                update(BudgetReservation)
                .where(BudgetReservation.id == row.id, BudgetReservation.state == BUDGET_RESERVED)
                .values(state=BUDGET_UNKNOWN, reason=reason, updated_at=now)
            )
            if moved.rowcount != 1:
                continue
            session.execute(
                update(BudgetScope)
                .where(BudgetScope.id == scope_id)
                .values(
                    reserved_micro_usd=BudgetScope.reserved_micro_usd - int(row.reserved_micro_usd),
                    reserved_tokens=BudgetScope.reserved_tokens - int(row.reserved_tokens),
                    unknown_micro_usd=BudgetScope.unknown_micro_usd + int(row.reserved_micro_usd),
                    unknown_tokens=BudgetScope.unknown_tokens + int(row.reserved_tokens),
                    blocked_reason=_block_expr(reason),
                    revision=BudgetScope.revision + 1,
                )
            )
            converted += 1
        return converted

    # ── lecture d'audit ────────────────────────────────────────────────────────────

    def events(self, scope_key: str, *, limit: int = 1000) -> list:
        def _do(session: Session):
            scope = session.scalar(select(BudgetScope).where(BudgetScope.scope_key == scope_key))
            if scope is None:
                return []
            rows = session.scalars(
                select(BudgetEvent).where(BudgetEvent.scope_id == scope.id).order_by(BudgetEvent.id).limit(limit)
            )
            return [
                {
                    "event_key": e.event_key,
                    "kind": e.kind,
                    "reservation_id": e.reservation_id,
                    "usd": micro_to_usd(e.micro_usd),
                    "tokens": int(e.tokens),
                    "detail": e.detail,
                }
                for e in rows
            ]

        return self._run(_do)

    def reservations(self, scope_key: str, *, states: Optional[tuple] = None) -> list:
        def _do(session: Session):
            scope = session.scalar(select(BudgetScope).where(BudgetScope.scope_key == scope_key))
            if scope is None:
                return []
            stmt = (
                select(BudgetReservation).where(BudgetReservation.scope_id == scope.id).order_by(BudgetReservation.id)
            )
            if states:
                stmt = stmt.where(BudgetReservation.state.in_(states))
            return [self._reservation_of(r, scope_key, replayed=False) for r in session.scalars(stmt)]

        return self._run(_do)
