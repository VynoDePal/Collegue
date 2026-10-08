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

from sqlalchemy import and_, case, func, or_, select, update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session, sessionmaker

from collegue.state.models import (
    BUDGET_COMMITTED,
    BUDGET_RELEASED,
    BUDGET_RESERVED,
    BUDGET_UNKNOWN,
    BudgetBlock,
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

# Causes de blocage INDÉPENDANTES (hors usage inconnu d'un appel, porté par l'état de la réservation).
BLOCK_BOUND_VIOLATION = "bound_violation"  # une borne de transport/tokenizer a été démentie par le fournisseur
BLOCK_AMBIGUOUS_HISTORY = "ambiguous_history"  # l'historique legacy importé ne prouve pas la dépense passée
BLOCK_MANUAL = "manual"
BLOCK_KINDS = (BLOCK_BOUND_VIOLATION, BLOCK_AMBIGUOUS_HISTORY, BLOCK_MANUAL)

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


class PlanningCycleError(BudgetLedgerError):
    """Le cycle de planification ne peut pas (re)démarrer : déjà abouti (``project_id``) ou en cours ailleurs."""

    def __init__(self, message: str, *, project_id: Optional[int] = None, busy: bool = False):
        self.project_id = project_id
        self.busy = busy
        super().__init__(message)


class BudgetIdentityError(BudgetLedgerError):
    """Une identité (``reservation_id``/``event_key``) est rejouée avec un SENS contradictoire : refusée telle quelle."""


def _decimal(value, label: str) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"{label} invalide (bool): {value!r}")
    if value is None:
        raise ValueError(f"{label} manquant")
    try:
        amount = Decimal(str(value).strip())
    except Exception as exc:  # noqa: BLE001 - valeur illisible
        raise ValueError(f"{label} illisible: {value!r}") from exc
    if not amount.is_finite():
        raise ValueError(f"{label} non fini: {value!r}")
    return amount


def usd_to_micro(value) -> int:
    """USD → micro-USD, arrondi vers le HAUT (conservateur). Refuse NaN/inf/négatif/bool/illisible."""
    amount = _decimal(value, "montant USD")
    if amount < 0:
        raise ValueError(f"montant USD négatif: {value!r}")
    return int((amount * MICRO).to_integral_value(rounding=ROUND_CEILING))


def validate_micro(value, label: str = "micro-USD") -> int:
    """Entier de micro-USD ≥ 0 (ni bool, ni fraction, ni négatif, ni non fini)."""
    if isinstance(value, bool) or not isinstance(value, int):
        if isinstance(value, float) and math.isfinite(value) and value == int(value) and value >= 0:
            return int(value)
        raise ValueError(f"{label} invalide (entier ≥ 0 requis): {value!r}")
    if value < 0:
        raise ValueError(f"{label} négatif: {value!r}")
    return int(value)


def cap_to_micro(value) -> Optional[int]:
    """Plafond USD → micro-USD, arrondi vers le BAS (conservateur).

    ``None`` et ``0`` = pas de plafond (convention historique de ``MAX_COST_USD=0`` : désactivé,
    documentée). Toute valeur INVALIDE (NaN/inf/bool/illisible/négative) est REFUSÉE : elle ne désactive
    jamais silencieusement un plafond.
    """
    if value is None:
        return None
    amount = _decimal(value, "plafond USD")
    if amount < 0:
        raise ValueError(f"plafond USD négatif: {value!r}")
    if amount == 0:
        return None
    return int((amount * MICRO).to_integral_value(rounding=ROUND_FLOOR))


def cap_to_tokens(value) -> Optional[int]:
    """Plafond de tokens (entier). ``None``/``0`` = pas de plafond ; invalide/fractionnaire/négatif ⇒ refusé."""
    if value is None:
        return None
    amount = _decimal(value, "plafond de tokens")
    if amount < 0 or amount != amount.to_integral_value():
        raise ValueError(f"plafond de tokens invalide (entier ≥ 0 requis): {value!r}")
    return int(amount) or None


def micro_to_usd(micro: int) -> float:
    return int(micro) / MICRO


def _tokens(value) -> int:
    """Nombre de tokens : entier ≥ 0 (une fraction ou un non-fini est une erreur, pas un arrondi)."""
    amount = _decimal(value, "nombre de tokens")
    if amount < 0 or amount != amount.to_integral_value():
        raise ValueError(f"nombre de tokens invalide (entier ≥ 0 requis): {value!r}")
    return int(amount)


def _is_unique_violation(exc: IntegrityError) -> bool:
    """Vrai pour une violation d'UNICITÉ (rejeu), faux pour CHECK/FK/NOT NULL (vraie erreur)."""
    orig = getattr(exc, "orig", None)
    code = getattr(orig, "pgcode", None)
    if code is not None:
        return str(code) == "23505"
    text = str(orig).lower()
    return "unique constraint failed" in text or "duplicate key" in text


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def analyze_legacy_series(values) -> tuple:
    """``(borne, anomalies)`` d'une série de snapshots CUMULATIFS (ordre d'écriture).

    ``borne`` = maximum des valeurs valides : égal au DERNIER snapshot quand la série est croissante (le
    protocole : ne jamais sommer les cumuls). Une série qui DÉCROÎT (compteur remis à zéro, double
    comptage corrigé…) ou qui contient une valeur invalide (NaN, inf, négatif, nulle) ne prouve pas la
    dépense passée : ``anomalies`` la décrit et l'appelant bloque le strict plutôt que de présenter ``borne``
    comme la dépense totale établie.
    """
    best, previous = 0.0, None
    issues = []
    for index, value in enumerate(values):
        if value is None or isinstance(value, bool) or not math.isfinite(value) or value < 0:
            issues.append(f"valeur invalide #{index + 1} ({value!r})")
            continue
        if previous is not None and value < previous:
            issues.append(f"série décroissante #{index + 1} ({previous!r} → {value!r})")
        previous = float(value)
        best = max(best, float(value))
    return best, issues


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
        cap_tok = cap_to_tokens(max_tokens)

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
            self._sync_block(session, row)
            return self._snapshot_of(row)

        return self._run(_do)

    def _import_legacy(self, session: Session, scope: BudgetScope) -> None:
        """Importe UNE fois les cumuls historiques (snapshots cumulatifs, pas des deltas).

        ``run_cost_usd``/``run_tokens`` sont écrits par l'ancien audit comme des totaux
        croissants ordonnés par ``id`` : seul le DERNIER cumul compte (les sommer compterait N fois).
        Une série décroissante ou invalide est AMBIGUË (voir :func:`analyze_legacy_series`) : on importe la
        borne au maximum observé, mais le strict est bloqué par une cause durable ``ambiguous_history`` tant
        qu'un opérateur ne l'a pas résolue — la borne n'est jamais présentée comme la dépense totale établie.
        L'unicité de ``scope_key``/``project_id`` rend l'import exactement-une-fois.
        """
        series: dict = {LEGACY_COST_METRIC: [], LEGACY_TOKENS_METRIC: []}
        for metric in session.scalars(select(Metric).where(Metric.project_id == scope.project_id).order_by(Metric.id)):
            if metric.name in series:
                series[metric.name].append(metric.value)
        usd, usd_issues = analyze_legacy_series(series[LEGACY_COST_METRIC])
        tokens, token_issues = analyze_legacy_series(series[LEGACY_TOKENS_METRIC])
        micro, tok = usd_to_micro(usd), _tokens(math.ceil(tokens))
        for name, bound, issues in (
            (LEGACY_COST_METRIC, usd, usd_issues),
            (LEGACY_TOKENS_METRIC, tokens, token_issues),
        ):
            if issues:
                self._add_block(
                    session,
                    scope,
                    key=f"legacy-history:{scope.scope_key}:{name}",
                    kind=BLOCK_AMBIGUOUS_HISTORY,
                    reason=(
                        f"historique {name} ambigu ({'; '.join(issues[:4])}) : importé = borne au MAXIMUM observé "
                        f"({bound!r}), PAS la dépense totale établie — résolution explicite requise"
                    ),
                )
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

    def _add_block(self, session: Session, scope: BudgetScope, *, key: str, kind: str, reason: str) -> bool:
        """Enregistre une cause de blocage (idempotent par ``key``) ; renvoie vrai si elle est nouvelle."""
        if kind not in BLOCK_KINDS:
            raise ValueError(f"cause de blocage invalide: {kind!r}")
        text = str(reason)[:2000]
        existing = session.scalar(select(BudgetBlock).where(BudgetBlock.block_key == key))
        if existing is not None:
            # Un rejeu est la MÊME cause : scope, type et motif identiques. Une clé réutilisée avec un autre sens
            # (autre scope, autre cause) est refusée — jamais une cause perdue en silence.
            if (existing.scope_id, existing.kind, existing.reason) != (scope.id, kind, text):
                raise BudgetIdentityError(
                    f"clé de cause de blocage {key} réutilisée avec un sens contradictoire : "
                    f"demande ({scope.scope_key}, {kind}) ≠ enregistrée (scope #{existing.scope_id}, {existing.kind})"
                )
            return False
        session.add(BudgetBlock(block_key=key, scope_id=scope.id, kind=kind, reason=text))
        session.add(BudgetEvent(event_key=f"block-open:{key}", scope_id=scope.id, kind="note", detail=text))
        session.flush()
        session.execute(
            update(BudgetScope)
            .where(BudgetScope.id == scope.id)
            .values(blocked_reason=_block_expr(text), revision=BudgetScope.revision + 1)
        )
        session.refresh(scope)
        return True

    @staticmethod
    def _sync_block(session: Session, scope: BudgetScope) -> None:
        """Quand un plafond apparaît après coup, les causes déjà ouvertes (blocs, inconnus) deviennent bloquantes."""
        if scope.blocked_reason or (scope.cap_micro_usd is None and scope.cap_tokens is None):
            return
        reason = session.scalar(
            select(BudgetBlock.reason)
            .where(BudgetBlock.scope_id == scope.id, BudgetBlock.resolved_at.is_(None))
            .order_by(BudgetBlock.id)
            .limit(1)
        )
        if reason is None:
            reason = session.scalar(
                select(BudgetReservation.reason)
                .where(BudgetReservation.scope_id == scope.id, BudgetReservation.state == BUDGET_UNKNOWN)
                .order_by(BudgetReservation.id)
                .limit(1)
            )
        if reason is not None:
            session.execute(
                update(BudgetScope)
                .where(BudgetScope.id == scope.id, BudgetScope.blocked_reason.is_(None))
                .values(blocked_reason=str(reason)[:2000] or "usage inconnu", revision=BudgetScope.revision + 1)
            )
            session.refresh(scope)

    def open_planning_cycle(
        self,
        cycle_key: str,
        *,
        max_cost_usd=None,
        max_tokens=None,
        strict: bool = True,
        claim_ttl_seconds: float = 7200.0,
    ) -> tuple:
        """Ouvre (ou REPREND) le scope d'un cycle de planification et en prend le droit exclusif.

        ``cycle_key`` est l'identité durable du cycle (``planning:…``) : la même identité retrouve le MÊME
        scope — donc le même solde — avant que le projet existe, après la création du projet et après un
        redémarrage. Le droit exclusif (jeton + échéance en compare-and-set) garantit qu'un seul appelant
        planifie à la fois : un second appel reçoit :class:`PlanningCycleError` (``busy``) sans rien émettre.
        Renvoie ``(snapshot, claim_token)`` ; ``snapshot.project_id`` dit si le projet du cycle existe déjà
        (reprise) — c'est à l'appelant de décider si le cycle est abouti ou à reprendre.
        """
        self._open(
            cycle_key,
            project_id=None,
            kind="planning",
            max_cost_usd=max_cost_usd,
            max_tokens=max_tokens,
            strict=strict,
        )
        token = uuid.uuid4().hex
        ttl = float(claim_ttl_seconds)
        if not math.isfinite(ttl) or ttl <= 0:
            raise ValueError(f"durée de réservation du cycle invalide: {claim_ttl_seconds!r}")

        def _do(session: Session):
            row = session.scalar(select(BudgetScope).where(BudgetScope.scope_key == cycle_key))
            if row is None:
                raise BudgetRefused(REFUSED_SCOPE, f"scope budgétaire inconnu: {cycle_key}")
            now = self._clock()
            claimed = session.execute(
                update(BudgetScope)
                .where(
                    BudgetScope.id == row.id,
                    or_(BudgetScope.claim_token.is_(None), BudgetScope.claim_expires_at < now),
                )
                .values(
                    claim_token=token, claim_expires_at=now + timedelta(seconds=ttl), revision=BudgetScope.revision + 1
                )
            )
            if claimed.rowcount != 1:
                raise PlanningCycleError(
                    f"le cycle de planification {cycle_key} est déjà en cours dans un autre appel : "
                    "attendre sa fin (ou l'échéance de son droit exclusif) au lieu de le dupliquer.",
                    busy=True,
                )
            session.refresh(row)
            return self._snapshot_of(row)

        return self._run(_do), token

    def release_planning_claim(self, scope_key: str, claim_token: str) -> None:
        """Libère le droit exclusif d'un cycle (échec ou fin) ; sans effet si le jeton n'est plus le sien."""

        def _do(session: Session):
            session.execute(
                update(BudgetScope)
                .where(BudgetScope.scope_key == scope_key, BudgetScope.claim_token == claim_token)
                .values(claim_token=None, claim_expires_at=None, revision=BudgetScope.revision + 1)
            )

        self._run(_do)

    def bind_new_project_in_session(self, session: Session, scope_key: str, claim_token: str, project_id: int) -> None:
        """Lie le projet du cycle DANS la transaction de son appelant (création atomique projet + scope).

        Compare-and-set : le scope ne doit pas être déjà lié et le droit exclusif ``claim_token`` doit être
        encore le sien et non échu. Sinon :class:`PlanningCycleError` — l'appelant annule sa transaction, donc
        aucun projet n'existe sans scope ni scope lié à un projet fantôme. Le droit est conservé (il couvre la
        suite du cycle : décomposition, tests d'acceptation) jusqu'à :meth:`release_planning_claim`.
        """
        now = self._clock()
        moved = session.execute(
            update(BudgetScope)
            .where(
                BudgetScope.scope_key == scope_key,
                BudgetScope.project_id.is_(None),
                BudgetScope.claim_token == claim_token,
                BudgetScope.claim_expires_at >= now,
            )
            .values(project_id=project_id, revision=BudgetScope.revision + 1)
        )
        if moved.rowcount != 1:
            raise PlanningCycleError(
                f"création du projet refusée pour le cycle {scope_key} : droit exclusif perdu ou échu, ou cycle déjà "
                "lié à un projet. Aucun projet n'a été créé.",
                busy=True,
            )

    def bind_project(self, scope_key: str, project_id: int) -> ScopeSnapshot:
        """Lie un scope existant à un projet déjà créé. Idempotent (usage hors cycle de planification)."""

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
        need_micro = validate_micro(micro_usd) if micro_usd is not None else usd_to_micro(usd)
        need_tokens = _tokens(tokens)
        rid = reservation_id or f"{kind}:{uuid.uuid4().hex}"
        if expires_at is None and ttl_seconds is not None:
            expires_at = self._clock() + timedelta(seconds=float(ttl_seconds))

        def check_identity(existing: BudgetReservation, scope_id: int) -> None:
            """Un rejeu est le MÊME événement : scope, type, montants et libellés identiques."""
            wanted = (scope_id, kind, need_micro, need_tokens, str(role)[:48], str(model)[:160], str(transport)[:48])
            have = (
                existing.scope_id,
                existing.kind,
                int(existing.reserved_micro_usd),
                int(existing.reserved_tokens),
                existing.role,
                existing.model,
                existing.transport,
            )
            if wanted != have:
                raise BudgetIdentityError(
                    f"identité de réservation contradictoire pour {rid} : demande {wanted} ≠ enregistrée {have} "
                    "(un reservation_id ne peut pas changer de sens)"
                )

        def _do(session: Session):
            scope = session.scalar(select(BudgetScope).where(BudgetScope.scope_key == scope_key))
            if scope is None:
                raise BudgetRefused(REFUSED_SCOPE, f"scope budgétaire inconnu: {scope_key}")
            existing = session.scalar(select(BudgetReservation).where(BudgetReservation.reservation_id == rid))
            if existing is not None:
                check_identity(existing, scope.id)
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
        except IntegrityError as exc:
            if not _is_unique_violation(exc):
                # Une contrainte CHECK/FK n'est PAS un rejeu : ne jamais la masquer derrière une réservation existante.
                raise BudgetLedgerError(f"contrainte violée à la réservation de {rid} : {exc.orig}") from exc

            # Deux appelants avec le même reservation_id : l'un a gagné, l'autre rejoue — si c'est le MÊME sens.
            def _replay(session: Session):
                existing = session.scalar(select(BudgetReservation).where(BudgetReservation.reservation_id == rid))
                scope = session.scalar(select(BudgetScope).where(BudgetScope.scope_key == scope_key))
                if existing is None or scope is None:
                    raise BudgetLedgerError(f"contrainte violée sans réservation existante: {rid}")
                check_identity(existing, scope.id)
                return self._reservation_of(existing, scope_key, replayed=True)

            return self._run(_replay)
        except (BudgetRefused, BudgetIdentityError):
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
        def check_event_identity(prior: BudgetEvent) -> None:
            """Rejouer une clé = rejouer le MÊME événement (réservation, type et montants identiques)."""
            wanted = (reservation_id, kind, consumed_micro, consumed_tokens)
            have = (prior.reservation_id, prior.kind, int(prior.micro_usd), int(prior.tokens))
            if wanted != have:
                raise BudgetIdentityError(
                    f"clé d'événement {event_key} réutilisée avec un sens contradictoire : demande {wanted} ≠ "
                    f"enregistrée {have} (une clé d'idempotence ne peut pas changer de sens)"
                )

        def _do(session: Session):
            row = session.scalar(select(BudgetReservation).where(BudgetReservation.reservation_id == reservation_id))
            if row is None:
                raise BudgetLedgerError(f"réservation inconnue: {reservation_id}")
            prior = session.scalar(select(BudgetEvent).where(BudgetEvent.event_key == event_key))
            if prior is not None:
                check_event_identity(prior)
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
        except IntegrityError as exc:
            if not _is_unique_violation(exc):
                raise BudgetLedgerError(f"contrainte violée au règlement de {reservation_id} : {exc.orig}") from exc

            # Course de rejeu : la même clé vient d'être appliquée par un autre appelant — rejeu SEULEMENT
            # si c'est le même événement ; un sens contradictoire échoue sans toucher aux soldes.
            def _replay(session: Session):
                row = session.scalar(
                    select(BudgetReservation).where(BudgetReservation.reservation_id == reservation_id)
                )
                prior = session.scalar(select(BudgetEvent).where(BudgetEvent.event_key == event_key))
                if prior is None or row is None:
                    raise BudgetLedgerError(f"contrainte d'unicité violée sans événement existant: {event_key}")
                check_event_identity(prior)
                return Settlement(
                    reservation_id, row.state, int(row.consumed_micro_usd), int(row.consumed_tokens), replayed=True
                )

            return self._run(_replay)

    @staticmethod
    def _clear_block_if_resolved(session: Session, scope_id: int) -> None:
        """Recalcule ``blocked_reason`` d'après les causes RESTANTES (usage inconnu, blocs ouverts).

        La ligne du scope est verrouillée d'abord en ``FOR NO KEY UPDATE`` (PostgreSQL ; un ``FOR UPDATE`` complet
        entrerait en deadlock avec les verrous de clé étrangère que prennent les insertions d'événements) : les lectures suivantes sont de nouvelles
        instructions qui voient tout bloc validé entre-temps, donc une résolution sans rapport n'efface jamais
        un blocage posé en parallèle. Aucune cause restante ⇒ levé ; sinon le motif affiché est celui de la
        plus ancienne cause qui reste (pas celui de la cause qui vient d'être résolue).
        """
        scope = session.scalar(select(BudgetScope).where(BudgetScope.id == scope_id).with_for_update(key_share=True))
        if scope is None:
            return
        causes = [
            (created, str(reason))
            for created, reason in session.execute(
                select(BudgetBlock.created_at, BudgetBlock.reason).where(
                    BudgetBlock.scope_id == scope_id, BudgetBlock.resolved_at.is_(None)
                )
            )
        ] + [
            (created, str(reason or "usage inconnu"))
            for created, reason in session.execute(
                select(BudgetReservation.created_at, BudgetReservation.reason).where(
                    BudgetReservation.scope_id == scope_id, BudgetReservation.state == BUDGET_UNKNOWN
                )
            )
        ]
        remaining = min(causes, key=lambda cause: cause[0])[1][:2000] if causes else None
        if remaining is None or scope.cap_micro_usd is not None or scope.cap_tokens is not None:
            session.execute(
                update(BudgetScope)
                .where(BudgetScope.id == scope_id)
                .values(blocked_reason=remaining, revision=BudgetScope.revision + 1)
            )
        session.refresh(scope)

    def commit(
        self, reservation_id: str, *, usd=0.0, tokens: int = 0, micro_usd: Optional[int] = None, event_key=None
    ) -> Settlement:
        """Engage la consommation RÉELLE (la dépense est établie). Libère le reliquat réservé.

        Idempotent : ``event_key`` (défaut ``commit:<reservation_id>``) rejoué = aucun effet.
        La consommation réelle peut dépasser la réservation (le fournisseur a dépassé
        l'estimation) : elle est enregistrée intégralement, jamais tronquée.
        """
        micro = validate_micro(micro_usd) if micro_usd is not None else usd_to_micro(usd)
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
        micro = validate_micro(micro_usd) if micro_usd is not None else usd_to_micro(usd)
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

    def block(
        self, scope_key: str, *, reason: str, event_key: Optional[str] = None, kind: str = BLOCK_BOUND_VIOLATION
    ) -> None:
        """Bloque durablement le scope (strict) par une cause INDÉPENDANTE de l'usage d'un appel.

        Typiquement : une hypothèse de borne a été DÉMENTIE par la réalité. La cause reste ouverte jusqu'à
        :meth:`resolve_block` : régler l'usage inconnu d'un appel ne la lève pas. Idempotent par ``event_key``.
        Sans plafond le blocage n'a pas d'effet immédiat (rien à protéger) mais reste enregistré, et devient
        bloquant si un plafond est ensuite configuré.
        """
        key = event_key or f"block:{scope_key}:{uuid.uuid4().hex}"

        def _do(session: Session):
            scope = session.scalar(select(BudgetScope).where(BudgetScope.scope_key == scope_key))
            if scope is None:
                raise BudgetRefused(REFUSED_SCOPE, f"scope budgétaire inconnu: {scope_key}")
            self._add_block(session, scope, key=key, kind=kind, reason=reason)

        try:
            self._run(_do)
        except IntegrityError as exc:
            if not _is_unique_violation(exc):
                raise BudgetLedgerError(f"contrainte violée au blocage de {scope_key} : {exc.orig}") from exc
            self._run(_do)  # course d'unicité : la clé existe maintenant — rejeu identique ou contradiction

    def open_blocks(self, scope_key: str) -> list:
        """Causes de blocage ouvertes ``[{block_key, kind, reason}]`` (hors usage inconnu des réservations)."""

        def _do(session: Session):
            scope = session.scalar(select(BudgetScope).where(BudgetScope.scope_key == scope_key))
            if scope is None:
                return []
            rows = session.scalars(
                select(BudgetBlock)
                .where(BudgetBlock.scope_id == scope.id, BudgetBlock.resolved_at.is_(None))
                .order_by(BudgetBlock.id)
            )
            return [{"block_key": r.block_key, "kind": r.kind, "reason": r.reason} for r in rows]

        return self._run(_do)

    def resolve_block(
        self, scope_key: str, block_key: str, *, event_key: str, reason: str, usd=0.0, tokens: int = 0
    ) -> bool:
        """Résout EXPLICITEMENT une cause de blocage ; renvoie faux si elle était déjà résolue (rejeu).

        ``usd``/``tokens`` : dépense passée établie par l'opérateur (relevé fournisseur), ajoutée au consommé
        — c'est ainsi qu'un historique ambigu est tranché. Le blocage du scope n'est levé que s'il ne reste
        aucune autre cause (autre bloc ouvert, usage inconnu).
        """
        micro, tok = usd_to_micro(usd), _tokens(tokens)
        note = (str(reason).strip() or "résolu par l'opérateur")[:2000]

        def _do(session: Session):
            scope = session.scalar(select(BudgetScope).where(BudgetScope.scope_key == scope_key))
            if scope is None:
                raise BudgetRefused(REFUSED_SCOPE, f"scope budgétaire inconnu: {scope_key}")
            wanted = (scope.id, "resolve", micro, tok, f"{block_key}: {note}")
            prior = session.scalar(select(BudgetEvent).where(BudgetEvent.event_key == event_key))
            if prior is not None:
                # Rejeu = même scope, même cause, mêmes montants, même justification ; sinon contradiction.
                if (prior.scope_id, prior.kind, int(prior.micro_usd), int(prior.tokens), prior.detail) != wanted:
                    raise BudgetIdentityError(
                        f"clé de résolution {event_key} réutilisée avec un sens contradictoire "
                        f"(scope, cause, montants ou justification différents)"
                    )
                return False
            block = session.scalar(
                select(BudgetBlock).where(BudgetBlock.block_key == block_key, BudgetBlock.scope_id == scope.id)
            )
            if block is None:
                raise BudgetLedgerError(f"cause de blocage inconnue pour {scope_key}: {block_key}")
            if block.resolved_at is not None:
                return False
            session.add(
                BudgetEvent(
                    event_key=event_key,
                    scope_id=scope.id,
                    kind="resolve",
                    micro_usd=micro,
                    tokens=tok,
                    detail=f"{block_key}: {note}",
                )
            )
            if micro or tok:
                rid = f"block-adjust:{scope.scope_key}:{block_key}"[:96]
                session.add(
                    BudgetReservation(
                        reservation_id=rid,
                        scope_id=scope.id,
                        kind="import",
                        role="operator",
                        transport="block-resolution",
                        state=BUDGET_COMMITTED,
                        reserved_micro_usd=micro,
                        reserved_tokens=tok,
                        consumed_micro_usd=micro,
                        consumed_tokens=tok,
                        reason=f"dépense établie lors de la résolution de {block_key}",
                    )
                )
                session.execute(
                    update(BudgetScope)
                    .where(BudgetScope.id == scope.id)
                    .values(
                        consumed_micro_usd=BudgetScope.consumed_micro_usd + micro,
                        consumed_tokens=BudgetScope.consumed_tokens + tok,
                        revision=BudgetScope.revision + 1,
                    )
                )
            closed = session.execute(
                update(BudgetBlock)
                .where(BudgetBlock.id == block.id, BudgetBlock.resolved_at.is_(None))
                .values(resolved_at=self._clock(), resolution=note)
            )
            if closed.rowcount != 1:
                raise BudgetLedgerError(f"résolution concurrente de {block_key}")
            session.flush()
            self._clear_block_if_resolved(session, scope.id)
            return True

        try:
            return self._run(_do)
        except IntegrityError as exc:
            if not _is_unique_violation(exc):
                raise BudgetLedgerError(f"contrainte violée à la résolution de {block_key} : {exc.orig}") from exc
            return self._run(_do)  # course d'unicité : rejeu identique (False) ou BudgetIdentityError

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
