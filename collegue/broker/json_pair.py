"""Paire JSON ciblée (W5) : UNE comparaison, 31B seul, sans puis avec ``responseMimeType``, bornée DURABLEMENT.

Le service du courtier borne les jetons, les dollars et l'échéance, mais PAS le nombre d'émissions : il rouvre une tentative
``released`` quand le même ``request_id`` est renvoyé (renvoi après un 400) et admet une troisième génération sous un
nouvel identifiant. Ce lanceur impose donc lui-même, dans un journal SQLite écrit AVANT chaque étape irréversible :

* une seule exécution par registre (le premier ``BEGIN IMMEDIATE`` gagne ; toute autre entrée réconcilie, rapporte et refuse) ;
* au plus DEUX générations, UNE tentative par variante, identifiants fixes dérivés de la campagne, jamais de renvoi ;
* l'admission d'émission (``claimed`` → ``emitting``) est un compare-and-set commité AVANT l'appel au transport ;
* arrêt de toute la paire après un 4xx, un timeout, une réponse perdue, une erreur de comptage, un refus local ou un inconnu ;
* l'échéance de 900 s s'ouvre dans le registre AVANT le premier ``countTokens`` et borne aussi l'appel en vol.

Le journal vit dans le MÊME fichier SQLite que le registre budgétaire, sans migration applicative : tables ``w5_json_pair_*``
créées par le lanceur. Le registre reste l'autorité d'usage ; le journal ne compte jamais un jeton. Les rapports ne portent
que des codes fermés : jamais de corps, de clé, d'en-tête ni de texte d'exception.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import re
import socket
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from collegue.broker.errors import BrokerError
from collegue.broker.policy import PRIMARY_MODEL
from collegue.broker.service import BrokerConfig, BrokerService
from collegue.broker.store import process_start_ticks
from collegue.broker.translate import canonical_json, normalize_chat_request
from collegue.broker.upstream import UpstreamBadBody, UpstreamHTTPError, UpstreamTransportError

MODEL = PRIMARY_MODEL
VARIANT_NAMES = ("prompt_only", "native_json")
REPORT_SCHEMA = "w5-json-pair-report/1"
JOURNAL_SCHEMA = "w5-json-pair-journal/1"
ROLE = "planner"  # le 31B seul (jamais de repli 26B)
CAMPAIGN_ID = re.compile(r"[a-z0-9][a-z0-9-]{2,39}")  # identique à ``claim_ref``

#: Enveloppe unique et immuable : USD, jetons, secondes depuis l'ouverture réelle, générations.
ENVELOPE = {"usd": 2.0, "tokens": 250_000, "seconds": 900, "generations": 2}
REQUEST_SHA256 = (
    "2edf377188f9ee909f3a349d5443810f437ac4068d930cf5708430ee8badecc2",
    "98d8c3444dcbe29c66bac30e5fc631c7ecff87aec5f2dfe57958aba1a4d3a300",
)
CONFIRMATION = "LANCER-UNE-PAIRE-JSON-2GENERATIONS-2USD-250000TOKENS-900S"
EXIT_OBSERVED, EXIT_FAILED, EXIT_INCOMPLETE = 0, 1, 3
EXIT_CODES = {"observed": EXIT_OBSERVED, "failed": EXIT_FAILED, "incomplete": EXIT_INCOMPLETE}

STATUSES = ("observed", "failed", "incomplete")
VARIANT_STATES = (
    "not_started",
    "in_progress",
    "observed",
    "parsing_failed",
    "rejected",
    "count_failed",
    "refused",
    "unknown",
    "bound_violation",
    "interrupted",
    "skipped",
)
REASONS = (
    "ok",
    "json_output_invalid",
    "upstream_rejected",
    "count_failed",
    "bound_violation",
    "upstream_ambiguous",
    "count_unavailable",
    "local_refusal",
    "not_sent",
    "interrupted",
    "deadline_reached",
    "in_progress",
    "not_started",
    "budget_unknown",
    "internal_error",
    "registry_unreadable",
    "journal_corrupt",
    "campaign_mismatch",
    "foreign_registry",
    "payload_drift",
)
FINISH_REASONS = ("stop", "length", "tool_calls", "content_filter")
#: États d'une variante qui autorisent la suivante (une sortie 200 non JSON ne l'interdit pas).
_CONTINUE = ("observed", "parsing_failed")
_TERMINAL = frozenset(VARIANT_STATES) - {"not_started", "in_progress", "skipped"}
_PROBLEM_STATES = ("rejected", "count_failed", "refused", "unknown", "bound_violation", "interrupted", "in_progress")
_FINISH_MAP = {"stop": "stop", "length": "length", "tool_calls": "tool_calls", "content_filter": "content_filter"}
_PROMPT = 'Réponds uniquement avec l\'objet JSON {"ok": true}.'
_SQLITE_TIMEOUT = 30.0


class JsonPairRefused(ValueError):
    """Refus d'entrée du lanceur (identifiant invalide…) : ``code`` fermé, jamais de texte libre."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def payloads() -> Tuple[dict, dict]:
    """Les deux requêtes de chat, copies neuves à chaque appel (jamais d'alias mutable). Seule la seconde demande du JSON natif."""
    base = {"model": MODEL, "max_tokens": 256, "messages": [{"role": "user", "content": _PROMPT}]}
    native = json.loads(json.dumps(base))
    native["response_format"] = {"type": "json_object"}
    return json.loads(json.dumps(base)), native


def request_fingerprints() -> Tuple[str, str]:
    """Empreintes NORMALISÉES des deux corps tels que le service les émettra (même normalisation que le courtier)."""
    config = BrokerConfig()
    out = []
    for payload in payloads():
        normalized = normalize_chat_request(
            payload,
            allowed_models=(MODEL,),
            default_output_tokens=min(config.default_output_tokens, config.max_output_tokens),
            max_output_tokens=config.max_output_tokens,
        )
        out.append(normalized.sha256)
    return out[0], out[1]


def scope_key_for(campaign_id: str) -> str:
    return f"json-pair:{campaign_id}"


def request_id_for(campaign_id: str, name: str) -> str:
    return f"jp-{campaign_id}-{name.replace('_', '-')}"


def _check_campaign(campaign_id: Any) -> str:
    if not isinstance(campaign_id, str) or not CAMPAIGN_ID.fullmatch(campaign_id):
        raise JsonPairRefused("invalid_campaign_id")
    return campaign_id


# ── journal ───────────────────────────────────────────────────────────────────────────────────────────────────────────

_DDL = (
    """CREATE TABLE IF NOT EXISTS w5_json_pair_run (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        schema TEXT NOT NULL, campaign_id TEXT NOT NULL, model TEXT NOT NULL, scope_key TEXT NOT NULL,
        limits TEXT NOT NULL, fingerprints TEXT NOT NULL,
        state TEXT NOT NULL CHECK (state IN ('running', 'closed')),
        halt_reason TEXT, opened_at TEXT NOT NULL, deadline_at TEXT, closed_at TEXT,
        conductor_host TEXT NOT NULL, conductor_pid INTEGER NOT NULL, conductor_start_ticks INTEGER
    )""",
    """CREATE TABLE IF NOT EXISTS w5_json_pair_variant (
        name TEXT PRIMARY KEY, position INTEGER NOT NULL UNIQUE, request_id TEXT NOT NULL UNIQUE,
        request_sha256 TEXT NOT NULL, state TEXT NOT NULL,
        count_calls INTEGER NOT NULL DEFAULT 0, claimed_at TEXT, sent_at TEXT, ended_at TEXT,
        json_valid INTEGER, finish_reason TEXT, http_status INTEGER, failure TEXT
    )""",
)
_VARIANT_COLUMNS = (
    "name, position, request_id, request_sha256, state, count_calls, claimed_at, sent_at, ended_at, "
    "json_valid, finish_reason, http_status, failure"
)


@dataclass(frozen=True)
class _Variant:
    name: str
    position: int
    request_id: str
    request_sha256: str
    state: str
    count_calls: int
    sent_at: Optional[str]
    json_valid: Optional[bool]
    finish_reason: Optional[str]
    http_status: Optional[int]
    failure: Optional[str]


@dataclass(frozen=True)
class _Run:
    campaign_id: str
    scope_key: str
    state: str
    halt_reason: Optional[str]
    deadline_at: Optional[datetime]
    conductor_host: str
    conductor_pid: int
    conductor_start_ticks: Optional[int]


def _iso(value: Optional[datetime]) -> Optional[str]:
    return None if value is None else value.astimezone(timezone.utc).isoformat()


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _variant_of(row: tuple) -> _Variant:
    (name, position, request_id, sha, state, count_calls, _claimed, sent, _ended, valid, finish, http, failure) = row
    return _Variant(
        name,
        position,
        request_id,
        sha,
        state,
        int(count_calls),
        sent,
        None if valid is None else bool(valid),
        finish,
        http,
        failure,
    )


def _run_of(row: tuple) -> _Run:
    campaign, scope, state, halt, deadline, host, pid, ticks = row
    return _Run(campaign, scope, state, halt, _parse_iso(deadline), host, int(pid), ticks)


_RUN_COLUMNS = (
    "campaign_id, scope_key, state, halt_reason, deadline_at, conductor_host, conductor_pid, conductor_start_ticks"
)


@contextlib.contextmanager
def _writer(path: Path):
    """Connexion d'écriture à transactions explicites ; ``tx()`` prend le verrou d'écriture d'emblée (``BEGIN IMMEDIATE``)."""
    conn = sqlite3.connect(str(path), timeout=_SQLITE_TIMEOUT, isolation_level=None)
    try:
        yield conn
    finally:
        conn.close()


@contextlib.contextmanager
def _tx(conn: sqlite3.Connection):
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


@contextlib.contextmanager
def _reader(path: Path):
    """Lecture SEULE : le fichier n'est ni créé ni modifié (mode ``ro``)."""
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=_SQLITE_TIMEOUT)
    try:
        yield conn
    finally:
        conn.close()


def _tables(conn: sqlite3.Connection) -> List[str]:
    return [
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")
    ]


def _is_virgin(conn: sqlite3.Connection) -> bool:
    """Schéma vierge : aucune table, ou des tables de migration TOUTES vides (hors ``alembic_version``)."""
    for table in _tables(conn):
        if table == "alembic_version":
            continue
        if conn.execute(f'SELECT 1 FROM "{table}" LIMIT 1').fetchone() is not None:  # noqa: S608 - nom issu de sqlite_master
            return False
    return True


#: Registres dont CE processus est le conducteur en cours (un second appel du même processus ne doit pas le « réparer »).
_ACTIVE: set = set()


def _alive(run: _Run, path: Path) -> bool:
    """Le conducteur est-il encore vivant ? Un autre hôte est supposé vivant (aucune preuve du contraire)."""
    if run.conductor_host != socket.gethostname():
        return True
    if run.conductor_pid == os.getpid():
        return str(Path(path).resolve()) in _ACTIVE
    try:
        os.kill(run.conductor_pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    ticks = process_start_ticks(run.conductor_pid)
    return not (ticks is not None and run.conductor_start_ticks is not None and ticks != run.conductor_start_ticks)


_RUN_COLUMN_SET = {
    "id", "schema", "campaign_id", "model", "scope_key", "limits", "fingerprints", "state", "halt_reason", "opened_at",
    "deadline_at", "closed_at", "conductor_host", "conductor_pid", "conductor_start_ticks",
}  # fmt: skip
_VARIANT_COLUMN_SET = {
    "name", "position", "request_id", "request_sha256", "state", "count_calls", "claimed_at", "sent_at", "ended_at",
    "json_valid", "finish_reason", "http_status", "failure",
}  # fmt: skip
_JOURNAL_VARIANT_STATES = frozenset(_TERMINAL | {"pending", "claimed", "emitting"})
_HALT_REASONS = (None, "internal_error", "interrupted", "deadline_reached", "local_refusal")


def _journal_is_sound(conn: sqlite3.Connection) -> bool:
    """Le journal durable a-t-il EXACTEMENT la structure que ce lanceur écrit ? Toute dérive (ligne ou colonne manquante ou en
    trop, schéma, modèle, bornes, empreintes, identifiants, états contradictoires) est une corruption : jamais un succès, jamais réparée."""
    try:
        if conn.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
            return False
        run_columns = {row[1] for row in conn.execute("PRAGMA table_info(w5_json_pair_run)")}
        variant_columns = {row[1] for row in conn.execute("PRAGMA table_info(w5_json_pair_variant)")}
        if run_columns != _RUN_COLUMN_SET or variant_columns != _VARIANT_COLUMN_SET:
            return False
        runs = conn.execute(
            "SELECT id, schema, campaign_id, model, scope_key, limits, fingerprints, state, halt_reason, opened_at, "
            "deadline_at, closed_at, conductor_host, conductor_pid, conductor_start_ticks FROM w5_json_pair_run"
        ).fetchall()
        if len(runs) != 1:
            return False
        (
            rid,
            schema,
            campaign,
            model,
            scope,
            limits,
            prints,
            state,
            halt,
            opened,
            deadline,
            closed,
            host,
            pid,
            _ticks,
        ) = runs[0]
        if rid != 1 or schema != JOURNAL_SCHEMA or model != MODEL or not CAMPAIGN_ID.fullmatch(str(campaign)):
            return False
        if (
            scope != scope_key_for(campaign)
            or json.loads(limits) != ENVELOPE
            or tuple(json.loads(prints)) != REQUEST_SHA256
        ):
            return False
        if (
            state not in ("running", "closed")
            or halt not in _HALT_REASONS
            or (state == "closed") != (closed is not None)
        ):
            return False
        if _parse_iso(opened) is None or not isinstance(host, str) or not host or not isinstance(pid, int):
            return False
        if deadline is not None:
            _parse_iso(deadline)
        if closed is not None:
            _parse_iso(closed)
        rows = conn.execute(f"SELECT {_VARIANT_COLUMNS} FROM w5_json_pair_variant ORDER BY position").fetchall()
        if len(rows) != len(VARIANT_NAMES):
            return False
        variants = []
        for position, (row, name, sha) in enumerate(zip(rows, VARIANT_NAMES, REQUEST_SHA256, strict=True), start=1):
            variant = _variant_of(row)
            if (variant.name, variant.position) != (name, position) or variant.request_sha256 != sha:
                return False
            if variant.request_id != request_id_for(campaign, name) or variant.state not in _JOURNAL_VARIANT_STATES:
                return False
            if variant.count_calls not in (0, 1) or row[5] != variant.count_calls:
                return False
            if row[9] not in (None, 0, 1) or (variant.finish_reason not in (None, *FINISH_REASONS, "other")):
                return False
            if variant.http_status is not None and not (
                isinstance(variant.http_status, int) and 100 <= variant.http_status <= 599
            ):
                return False
            for stamp in (row[6], row[7], row[8]):
                if stamp is not None:
                    _parse_iso(stamp)
            if variant.state == "pending" and (
                variant.sent_at or variant.json_valid is not None or variant.count_calls
            ):
                return False
            if variant.state in ("emitting", "observed", "parsing_failed", "bound_violation") and not variant.sent_at:
                return False
            if variant.state in ("observed", "parsing_failed") and variant.json_valid is not (
                variant.state == "observed"
            ):
                return False
            if variant.state == "bound_violation" and variant.json_valid is None:
                return False
            if variant.state in _TERMINAL and row[8] is None:
                return False
            variants.append(variant)
        if sum(1 for v in variants if v.state in ("claimed", "emitting")) > 1:
            return False
        if sum(1 for v in variants if v.sent_at) > ENVELOPE["generations"]:
            return False
        if variants[1].state != "pending" and variants[0].state not in _CONTINUE:
            return False
        if any(v.state != "pending" for v in variants) and deadline is None:
            return False
    except (sqlite3.Error, ValueError, TypeError, KeyError, IndexError):
        return False
    return True


class _Journal:
    """Écritures du journal : chaque admission est un compare-and-set dans UNE transaction, commité avant l'étape qu'elle autorise."""

    def __init__(self, path: Path, campaign_id: str, clock: Callable[[], datetime]):
        self.path = path
        self.campaign_id = campaign_id
        self.clock = clock

    # -- création / revendication ---------------------------------------------------------------------------------------

    @staticmethod
    def claim(path: Path, campaign_id: str, clock: Callable[[], datetime]) -> str:
        """``new`` (ce processus est le conducteur) | ``existing`` (même campagne) | code de refus fermé."""
        fingerprints = request_fingerprints()
        if fingerprints != REQUEST_SHA256:
            return "payload_drift"
        with _writer(path) as conn, _tx(conn):
            tables = _tables(conn)
            if "w5_json_pair_run" in tables:
                if "w5_json_pair_variant" not in tables or not _journal_is_sound(conn):
                    return "journal_corrupt"
                row = conn.execute("SELECT campaign_id FROM w5_json_pair_run").fetchone()
                return "existing" if row[0] == campaign_id else "campaign_mismatch"
            if not _is_virgin(conn):
                return "foreign_registry"
            for statement in _DDL:
                conn.execute(statement)
            now = clock()
            conn.execute(
                "INSERT INTO w5_json_pair_run (id, schema, campaign_id, model, scope_key, limits, fingerprints, state, "
                "opened_at, conductor_host, conductor_pid, conductor_start_ticks) "
                "VALUES (1, ?, ?, ?, ?, ?, ?, 'running', ?, ?, ?, ?)",
                (
                    JOURNAL_SCHEMA,
                    campaign_id,
                    MODEL,
                    scope_key_for(campaign_id),
                    json.dumps(ENVELOPE, sort_keys=True),
                    json.dumps(list(fingerprints)),
                    _iso(now),
                    socket.gethostname(),
                    os.getpid(),
                    process_start_ticks(os.getpid()),
                ),
            )
            for position, (name, sha) in enumerate(zip(VARIANT_NAMES, fingerprints, strict=True), start=1):
                conn.execute(
                    "INSERT INTO w5_json_pair_variant (name, position, request_id, request_sha256, state) "
                    "VALUES (?, ?, ?, ?, 'pending')",
                    (name, position, request_id_for(campaign_id, name), sha),
                )
        return "new"

    # -- lecture interne ------------------------------------------------------------------------------------------------

    def _run_row(self, conn: sqlite3.Connection) -> _Run:
        return _run_of(conn.execute(f"SELECT {_RUN_COLUMNS} FROM w5_json_pair_run").fetchone())

    def _variants(self, conn: sqlite3.Connection) -> List[_Variant]:
        rows = conn.execute(f"SELECT {_VARIANT_COLUMNS} FROM w5_json_pair_variant ORDER BY position").fetchall()
        return [_variant_of(row) for row in rows]

    def _gate(self, conn: sqlite3.Connection, now: datetime) -> Tuple[Optional[_Run], str]:
        if not _journal_is_sound(conn):
            return None, "journal_corrupt"
        run = self._run_row(conn)
        if run.state != "running" or run.halt_reason:
            return run, "run_closed"
        if run.deadline_at is None:
            return run, "deadline_unset"
        if now >= run.deadline_at:
            return run, "deadline"
        return run, ""

    def deadline(self) -> Optional[datetime]:
        with _writer(self.path) as conn:
            return self._run_row(conn).deadline_at

    # -- transitions ----------------------------------------------------------------------------------------------------

    def set_deadline(self, deadline: datetime) -> None:
        with _writer(self.path) as conn, _tx(conn):
            conn.execute("UPDATE w5_json_pair_run SET deadline_at = ? WHERE deadline_at IS NULL", (_iso(deadline),))

    def claim_variant(self, name: str) -> str:
        """CAS ``pending`` → ``claimed`` ; ``""`` si admis, sinon code de refus fermé. Une seule variante en vol, dans l'ordre."""
        now = self.clock()
        with _writer(self.path) as conn, _tx(conn):
            _, refusal = self._gate(conn, now)
            if refusal:
                return refusal
            variants = self._variants(conn)
            target = next(v for v in variants if v.name == name)
            if target.state != "pending":
                return "already_started"
            if any(v.state in ("claimed", "emitting") for v in variants):
                return "other_in_flight"
            if any(v.position < target.position and v.state not in _CONTINUE for v in variants):
                return "previous_not_continuable"
            conn.execute(
                "UPDATE w5_json_pair_variant SET state = 'claimed', claimed_at = ? WHERE name = ? AND state = 'pending'",
                (_iso(now), name),
            )
        return ""

    def note_count(self, name: str) -> str:
        """Un seul ``countTokens`` par variante, seulement tant qu'elle est ``claimed`` et dans l'échéance."""
        now = self.clock()
        with _writer(self.path) as conn, _tx(conn):
            _, refusal = self._gate(conn, now)
            if refusal:
                self._refuse(conn, name, refusal)
                return refusal
            changed = conn.execute(
                "UPDATE w5_json_pair_variant SET count_calls = count_calls + 1 "
                "WHERE name = ? AND state = 'claimed' AND count_calls = 0",
                (name,),
            ).rowcount
            if not changed:
                self._refuse(conn, name, "count_repeat")
                return "count_repeat"
        return ""

    def admit_send(self, name: str) -> str:
        """CAS ``claimed`` → ``emitting`` AVANT l'appel au transport : au plus deux admissions, jamais deux fois la même variante."""
        now = self.clock()
        with _writer(self.path) as conn, _tx(conn):
            _, refusal = self._gate(conn, now)
            if not refusal:
                variants = self._variants(conn)
                if sum(1 for v in variants if v.sent_at) >= ENVELOPE["generations"]:
                    refusal = "emission_cap"
                elif any(v.state == "emitting" for v in variants):
                    refusal = "other_in_flight"
            if refusal:
                self._refuse(conn, name, refusal)
                return refusal
            changed = conn.execute(
                "UPDATE w5_json_pair_variant SET state = 'emitting', sent_at = ? "
                "WHERE name = ? AND state = 'claimed' AND sent_at IS NULL AND count_calls = 1 AND failure IS NULL",
                (_iso(now), name),
            ).rowcount
            if not changed:
                self._refuse(conn, name, "not_admitted")
                return "not_admitted"
        return ""

    @staticmethod
    def _refuse(conn: sqlite3.Connection, name: str, code: str) -> None:
        conn.execute(
            "UPDATE w5_json_pair_variant SET failure = ? WHERE name = ? AND state IN ('claimed', 'emitting') "
            "AND failure IS NULL",
            (f"local_{code}", name),
        )

    def note_failure(self, name: str, *, http_status: Optional[int], kind: str) -> None:
        """Mémorise ce que le garde a vu passer (statut HTTP, nature du transport) : codes fermés seulement."""
        with _writer(self.path) as conn, _tx(conn):
            conn.execute(
                "UPDATE w5_json_pair_variant SET http_status = ?, failure = COALESCE(failure, ?) "
                "WHERE name = ? AND state IN ('claimed', 'emitting')",
                (http_status, kind, name),
            )

    def finish_variant(self, resolved: "_Resolved", name: str) -> None:
        with _writer(self.path) as conn, _tx(conn):
            conn.execute(
                "UPDATE w5_json_pair_variant SET state = ?, json_valid = ?, finish_reason = ?, http_status = COALESCE(?, "
                "http_status), failure = COALESCE(?, failure), ended_at = ? WHERE name = ? AND state IN ('claimed', 'emitting')",
                (
                    resolved.state,
                    None if resolved.json_valid is None else int(resolved.json_valid),
                    resolved.finish_reason,
                    resolved.http_status,
                    resolved.failure,
                    _iso(self.clock()),
                    name,
                ),
            )

    def halt(self, reason: str) -> None:
        """Arrêt durable : plus aucune variante n'est admise (``internal_error``…)."""
        with _writer(self.path) as conn, _tx(conn):
            conn.execute("UPDATE w5_json_pair_run SET halt_reason = COALESCE(halt_reason, ?)", (reason,))

    def close_run(self) -> None:
        with _writer(self.path) as conn, _tx(conn):
            conn.execute(
                "UPDATE w5_json_pair_run SET state = 'closed', closed_at = ? WHERE state = 'running'",
                (_iso(self.clock()),),
            )

    def variants(self) -> List[_Variant]:
        with _writer(self.path) as conn:
            return self._variants(conn)


# ── registre (lecture seule) ──────────────────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _Attempt:
    state: str
    error_code: Optional[str]
    error_detail: Optional[str]
    response_json: Optional[str]


@dataclass(frozen=True)
class _Registry:
    present: bool
    consumed_tokens: Optional[int] = None
    unknown_tokens: Optional[int] = None
    reserved_tokens: Optional[int] = None
    blocked: Optional[bool] = None
    attempts: Optional[Dict[str, _Attempt]] = None


def _read_registry(conn: sqlite3.Connection, scope_key: str) -> _Registry:
    tables = _tables(conn)
    if "budget_scopes" not in tables or "broker_attempts" not in tables:
        return _Registry(present=False)
    scope = conn.execute(
        "SELECT consumed_tokens, unknown_tokens, reserved_tokens, blocked_reason FROM budget_scopes WHERE scope_key = ?",
        (scope_key,),
    ).fetchone()
    if scope is None:
        return _Registry(present=False)
    attempts = {
        row[0]: _Attempt(row[1], row[2], row[3], row[4])
        for row in conn.execute(
            "SELECT request_id, state, error_code, error_detail, response_json FROM broker_attempts WHERE scope_key = ?",
            (scope_key,),
        )
    }
    return _Registry(
        present=True,
        consumed_tokens=int(scope[0]),
        unknown_tokens=int(scope[1]),
        reserved_tokens=int(scope[2]),
        blocked=bool(scope[3]),
        attempts=attempts,
    )


# ── résolution d'une variante ─────────────────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _Resolved:
    state: str
    json_valid: Optional[bool] = None
    finish_reason: Optional[str] = None
    http_status: Optional[int] = None
    failure: Optional[str] = None


def _reject_constant(_value: str):
    raise ValueError("constante JSON non standard")


def _inspect_completion(response_json: Optional[str]) -> Tuple[bool, Optional[str]]:
    """``(json_valid, finish_reason)`` d'une complétion règlée : JSON valide = un OBJET JSON strict. Le contenu n'est jamais rendu."""
    try:
        completion = json.loads(response_json or "")
        choice = completion["choices"][0]
        finish = choice.get("finish_reason")
        content = (choice.get("message") or {}).get("content")
    except (ValueError, KeyError, IndexError, TypeError, AttributeError):
        return False, None
    reason = _FINISH_MAP.get(finish) if isinstance(finish, str) else None
    if isinstance(finish, str) and reason is None:
        reason = "other"
    if not isinstance(content, str) or not content.strip():
        return False, reason
    try:
        parsed = json.loads(content, parse_constant=_reject_constant)
    except (ValueError, RecursionError):
        return False, reason
    return isinstance(parsed, dict), reason


_HTTP_DETAIL = re.compile(r"HTTP (\d{3})")


def _resolve(variant: _Variant, attempt: Optional[_Attempt], blocked: bool, *, called: bool) -> _Resolved:
    """Décide l'état d'une variante ``claimed``/``emitting`` à partir du registre (autorité) et de ce que le garde a noté.

    ``called`` : le service vient de rendre la main (retour ou exception) ; sinon c'est une reprise après arrêt du conducteur.
    """
    local = variant.failure if (variant.failure or "").startswith("local_") else None
    if attempt is None:
        if variant.state == "emitting":
            return _Resolved("unknown", failure="ambiguous")
        return _Resolved("refused", failure=local or "pre_attempt") if called else _Resolved("interrupted")
    if attempt.state == "settled":
        valid, finish = _inspect_completion(attempt.response_json)
        state = "bound_violation" if blocked else ("observed" if valid else "parsing_failed")
        return _Resolved(state, json_valid=valid, finish_reason=finish)
    if attempt.state == "unknown":
        return _Resolved("unknown", http_status=variant.http_status, failure="ambiguous")
    if attempt.state == "released":
        if local:
            return _Resolved("refused", failure=local)
        if attempt.error_code == "interrupted_before_emission":
            return _Resolved("interrupted")
        if attempt.error_code == "upstream_rejected":
            status = variant.http_status
            if status is None:
                match = _HTTP_DETAIL.match(attempt.error_detail or "")
                status = int(match.group(1)) if match else None
            return _Resolved("rejected", http_status=status, failure="rejected")
        if attempt.error_code == "count_tokens_failed":
            return _Resolved("count_failed", http_status=variant.http_status, failure="count")
        return _Resolved("refused", failure="not_sent")
    if attempt.state == "prepared" and variant.state == "claimed":
        return _Resolved(
            "interrupted"
        )  # annulée avant tout marquage d'émission : rien n'est parti, aucune consommation supposée
    return _Resolved("unknown", failure="ambiguous") if called else _Resolved("in_progress")


def _effective(variant: _Variant, registry: _Registry, run: _Run, path: Path) -> _Resolved:
    """État EXPOSÉ d'une variante : le journal fait foi pour les états finis, contrôlés contre le registre."""
    attempts = registry.attempts or {}
    attempt = attempts.get(variant.request_id)
    if variant.state == "pending":
        return _Resolved("skipped" if run.state == "closed" else "not_started")
    if variant.state in ("claimed", "emitting"):
        resolved = _resolve(variant, attempt, bool(registry.blocked), called=False)
        if resolved.state in ("interrupted", "in_progress") and run.state == "running" and _alive(run, path):
            return _Resolved("in_progress")
        return resolved
    if variant.state in _CONTINUE:
        # Une observation n'est jamais crue sans règlement dans le registre.
        if attempt is None or attempt.state != "settled":
            return _Resolved("unknown", failure="ambiguous")
        return _Resolved(variant.state, json_valid=variant.json_valid, finish_reason=variant.finish_reason)
    return _Resolved(
        variant.state,
        json_valid=variant.json_valid,
        finish_reason=variant.finish_reason,
        http_status=variant.http_status,
        failure=variant.failure,
    )


# ── rapport ───────────────────────────────────────────────────────────────────────────────────────────────────────────


def _empty_report(campaign_id: str, status: str, reason: str, *, state: str = "not_started") -> dict:
    return {
        "schema": REPORT_SCHEMA,
        "campaign_id": campaign_id,
        "status": status,
        "reason": reason,
        "variants": [
            {"name": name, "state": state, "json_valid": None, "finish_reason": None, "request_sha256": sha}
            for name, sha in zip(VARIANT_NAMES, REQUEST_SHA256, strict=True)
        ],
        "budget": {
            "consumed_tokens": None,
            "unknown_tokens": None,
            "reserved_tokens": None,
            "blocked": None,
            "deadline_at": None,
        },
        "emissions": {
            "basis": "admission_durable_avant_appel_transport",
            "admitted": None,
            "settled": None,
            "released": None,
            "unknown": None,
        },
        "business_validation": "not_executed",
    }


def _verdict(run: _Run, resolved: List[_Resolved], registry: _Registry) -> Tuple[str, str]:
    for item in resolved:
        if item.state == "rejected":
            return "failed", "upstream_rejected"
        if item.state == "count_failed":
            return ("failed", "count_failed") if item.http_status is not None else ("incomplete", "count_unavailable")
        if item.state == "bound_violation":
            return "failed", "bound_violation"
        if item.state == "unknown":
            return "incomplete", "upstream_ambiguous"
        if item.state in ("refused", "interrupted") and run.halt_reason == "internal_error":
            return "incomplete", "internal_error"  # un incident interne prime sur l'état local qu'il a laissé
        if item.state == "refused":
            local = (item.failure or "").startswith("local_") or item.failure == "pre_attempt"
            return "incomplete", "local_refusal" if local else "not_sent"
        if item.state == "interrupted":
            return "incomplete", "interrupted"
        if item.state == "in_progress":
            return "incomplete", "in_progress"
    states = [item.state for item in resolved]
    if states[-1] == "parsing_failed" and states[0] in _CONTINUE:
        return "failed", "json_output_invalid"
    if all(state in _CONTINUE for state in states):
        if registry.present and (registry.blocked or (registry.unknown_tokens or 0) > 0):
            return "incomplete", "budget_unknown"
        if run.halt_reason:
            return "incomplete", run.halt_reason
        return "observed", "ok"
    if run.halt_reason:
        return "incomplete", run.halt_reason
    if all(state == "not_started" for state in states):
        return "incomplete", "not_started"
    return "incomplete", "interrupted"


def _build(campaign_id: str, run: _Run, variants: List[_Variant], registry: _Registry, path: Path) -> dict:
    resolved = [_effective(variant, registry, run, path) for variant in variants]
    status, reason = _verdict(run, resolved, registry)
    attempts = registry.attempts if registry.present else None
    counts = None if attempts is None else [a.state for a in attempts.values()]
    report = _empty_report(campaign_id, status, reason)
    report["variants"] = [
        {
            "name": variant.name,
            "state": item.state,
            "json_valid": item.json_valid,
            "finish_reason": item.finish_reason,
            "request_sha256": variant.request_sha256,
        }
        for variant, item in zip(variants, resolved, strict=True)
    ]
    report["budget"] = {
        "consumed_tokens": registry.consumed_tokens,
        "unknown_tokens": registry.unknown_tokens,
        "reserved_tokens": registry.reserved_tokens,
        "blocked": registry.blocked,
        "deadline_at": _iso(run.deadline_at),
    }
    report["emissions"] = {
        "basis": "admission_durable_avant_appel_transport",
        "admitted": sum(1 for variant in variants if variant.sent_at),
        "settled": None if counts is None else counts.count("settled"),
        "released": None if counts is None else counts.count("released"),
        "unknown": None if counts is None else counts.count("unknown"),
    }
    return report


def _corrupt_report(conn: sqlite3.Connection, campaign_id: str) -> dict:
    """Journal illisible comme structure : aucun état de variante n'est cru (``unknown``), aucune émission n'est comptée ; le budget
    est relu dans le registre (autorité) quand il est lisible."""
    report = _empty_report(campaign_id, "incomplete", "journal_corrupt", state="unknown")
    report["emissions"] = {key: (value if key == "basis" else None) for key, value in report["emissions"].items()}
    try:
        registry = _read_registry(conn, scope_key_for(campaign_id))
    except (sqlite3.Error, ValueError, TypeError):
        return report
    if registry.present:
        report["budget"].update(
            consumed_tokens=registry.consumed_tokens,
            unknown_tokens=registry.unknown_tokens,
            reserved_tokens=registry.reserved_tokens,
            blocked=registry.blocked,
        )
    return report


def read_report(*, database: Path, campaign_id: str) -> dict:
    """Relecture SÛRE : aucun fournisseur, aucune écriture (le fichier est ouvert en lecture seule), utilisable après interruption."""
    _check_campaign(campaign_id)
    path = Path(database)
    if not path.is_file():
        return _empty_report(campaign_id, "incomplete", "not_started")
    try:
        with _reader(path) as conn:
            tables = _tables(conn)
            if "w5_json_pair_run" not in tables:
                reason = "foreign_registry" if not _is_virgin(conn) else "not_started"
                return _empty_report(campaign_id, "incomplete", reason)
            if "w5_json_pair_variant" not in tables or not _journal_is_sound(conn):
                return _corrupt_report(conn, campaign_id)
            run = _run_of(conn.execute(f"SELECT {_RUN_COLUMNS} FROM w5_json_pair_run").fetchone())
            if run.campaign_id != campaign_id:
                return _empty_report(campaign_id, "incomplete", "campaign_mismatch")
            variants = [
                _variant_of(r)
                for r in conn.execute(
                    f"SELECT {_VARIANT_COLUMNS} FROM w5_json_pair_variant ORDER BY position"
                ).fetchall()
            ]
            registry = _read_registry(conn, run.scope_key)
        return _build(campaign_id, run, variants, registry, path)
    except (sqlite3.Error, ValueError, KeyError, TypeError):
        return _empty_report(campaign_id, "incomplete", "registry_unreadable")


# ── garde du transport ────────────────────────────────────────────────────────────────────────────────────────────────

_SAFE_KIND = re.compile(r"[A-Za-z0-9_]{1,40}")


class _Guard:
    """Enveloppe du fournisseur : le journal admet CHAQUE étape avant qu'elle parte, l'échéance borne l'appel en vol, et
    aucune exception libre du fournisseur ne traverse (le service n'en voit que des types fermés)."""

    def __init__(self, inner: Any, journal: _Journal, clock: Callable[[], datetime]):
        self._inner = inner
        self._journal = journal
        self._clock = clock
        self.current: Optional[str] = None

    def _check_request(self, request: Any) -> bool:
        name = self.current
        if name is None or request.model != MODEL:
            return False
        sha = REQUEST_SHA256[VARIANT_NAMES.index(name)]
        body_sha = hashlib.sha256(canonical_json(request.body).encode("utf-8")).hexdigest()
        return request.sha256 == sha and body_sha == sha and request.generate_body() is request.body

    def _remaining(self) -> float:
        deadline = self._journal.deadline()
        return 0.0 if deadline is None else max(0.0, (deadline - self._clock()).total_seconds())

    def _refuse(self, code: str, *, before_send: bool = True) -> UpstreamTransportError:
        return UpstreamTransportError(f"local_{code}", before_send=before_send)

    async def _call(self, name: str, phase: str, coroutine_factory: Callable[[], Any], *, before_send: bool):
        remaining = self._remaining()
        if remaining <= 0:
            self._journal.note_failure(name, http_status=None, kind="local_deadline")
            raise self._refuse("deadline") from None
        try:
            return await asyncio.wait_for(coroutine_factory(), timeout=remaining)
        except asyncio.TimeoutError:
            self._journal.note_failure(name, http_status=None, kind=f"{phase}_timeout")
            raise UpstreamTransportError("deadline_timeout", before_send=before_send) from None
        except UpstreamHTTPError as exc:
            status = int(exc.status)
            self._journal.note_failure(name, http_status=status, kind=f"{phase}_http")
            raise UpstreamHTTPError(status, "", diagnostic=getattr(exc, "diagnostic", None)) from None
        except UpstreamTransportError as exc:
            self._journal.note_failure(name, http_status=None, kind=f"{phase}_transport")
            kind = exc.kind if isinstance(exc.kind, str) and _SAFE_KIND.fullmatch(exc.kind) else "transport"
            raise UpstreamTransportError(kind, before_send=bool(exc.before_send)) from None
        except UpstreamBadBody:
            self._journal.note_failure(name, http_status=None, kind=f"{phase}_bad_body")
            raise UpstreamBadBody("réponse du fournisseur inexploitable") from None
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - exception libre d'un fournisseur : seul le NOM de classe (filtré) survit
            self._journal.note_failure(name, http_status=None, kind=f"{phase}_unexpected")
            kind = type(exc).__name__ if _SAFE_KIND.fullmatch(type(exc).__name__) else "unexpected"
            raise UpstreamTransportError(kind, before_send=before_send) from None

    async def count_tokens(self, request: Any) -> dict:
        name = self.current
        if name is None or not self._check_request(request):
            if name is not None:
                self._journal.note_failure(name, http_status=None, kind="local_payload_mismatch")
            raise self._refuse("payload_mismatch")
        code = self._journal.note_count(name)
        if code:
            raise self._refuse(code)
        return await self._call(name, "count", lambda: self._inner.count_tokens(request), before_send=True)

    async def generate(self, request: Any) -> dict:
        name = self.current
        if name is None or not self._check_request(request):
            if name is not None:
                self._journal.note_failure(name, http_status=None, kind="local_payload_mismatch")
            raise self._refuse("payload_mismatch")
        code = self._journal.admit_send(name)  # commité AVANT l'appel au transport
        if code:
            raise self._refuse(code)
        return await self._call(name, "generate", lambda: self._inner.generate(request), before_send=False)


class _NoUpstream:
    """Fournisseur de la reprise : toute utilisation est une erreur (réconcilier ne génère, ne compte et n'émet jamais)."""

    async def count_tokens(self, request: Any) -> dict:  # pragma: no cover - ne doit jamais être atteint
        raise UpstreamTransportError("no_provider", before_send=True)

    async def generate(self, request: Any) -> dict:  # pragma: no cover - ne doit jamais être atteint
        raise UpstreamTransportError("no_provider", before_send=True)


# ── orchestration ─────────────────────────────────────────────────────────────────────────────────────────────────────


def _database_url(path: Path) -> str:
    return f"sqlite:///{path}"


def _snapshot_registry(path: Path, scope_key: str) -> _Registry:
    with _reader(path) as conn:
        return _read_registry(conn, scope_key)


def _finish(journal: _Journal, path: Path, run_scope: str, name: str) -> _Resolved:
    """Fixe l'état d'une variante APRÈS le retour (ou l'exception) du service, d'après le registre (autorité)."""
    variant = next(v for v in journal.variants() if v.name == name)
    if variant.state not in ("claimed", "emitting"):
        return _Resolved(variant.state)
    registry = _snapshot_registry(path, run_scope)
    attempt = (registry.attempts or {}).get(variant.request_id)
    resolved = _resolve(variant, attempt, bool(registry.blocked), called=True)
    journal.finish_variant(resolved, name)
    return resolved


async def run_pair(
    *, database: Path, campaign_id: str, upstream: Any, clock: Optional[Callable[[], datetime]] = None
) -> dict:
    """Orchestre la paire. Le premier appel sur un registre NEUF est le seul conducteur ; tout autre appel (même campagne)
    réconcilie, rapporte et refuse de poursuivre, sans jamais toucher au fournisseur."""
    _check_campaign(campaign_id)
    path = Path(database)
    clock = clock or _utcnow
    try:
        outcome = _Journal.claim(path, campaign_id, clock)
    except (sqlite3.Error, OSError):
        return _empty_report(campaign_id, "incomplete", "registry_unreadable")
    if outcome == "existing":
        return reconcile_pair(database=path, campaign_id=campaign_id)
    if outcome == "payload_drift":
        return _empty_report(campaign_id, "incomplete", outcome)
    if outcome != "new":
        return read_report(database=path, campaign_id=campaign_id)  # refus : ni écriture ni réparation du fichier
    return await _conduct(path, campaign_id, upstream, clock)


async def _conduct(path: Path, campaign_id: str, upstream: Any, clock: Callable[[], datetime]) -> dict:
    from collegue.state import ProjectStateManager

    journal = _Journal(path, campaign_id, clock)
    scope_key = scope_key_for(campaign_id)
    service: Optional[BrokerService] = None
    _ACTIVE.add(str(path.resolve()))
    try:
        manager = ProjectStateManager.from_url(_database_url(path), create=True)
        ledger = manager.budget_ledger
        ledger.create_planning_scope(
            max_cost_usd=ENVELOPE["usd"], max_tokens=ENVELOPE["tokens"], strict=True, scope_key=scope_key
        )
        guard = _Guard(upstream, journal, clock)
        service = BrokerService(
            ledger, guard, config=BrokerConfig(global_deadline_seconds=ENVELOPE["seconds"]), clock=clock
        )
        deadline = service.open_clock(scope_key)  # l'horloge de 900 s s'ouvre ICI, avant le premier countTokens
        if deadline is None:
            raise RuntimeError("horloge globale non ouverte")
        journal.set_deadline(deadline)
        for name, payload in zip(VARIANT_NAMES, payloads(), strict=True):
            refusal = journal.claim_variant(name)
            if refusal:
                journal.halt("deadline_reached" if refusal == "deadline" else "local_refusal")
                break
            guard.current = name
            try:
                await service.sampling_completion(
                    scope_key, ROLE, payload, request_id=request_id_for(campaign_id, name)
                )
            except asyncio.CancelledError:
                with contextlib.suppress(Exception):
                    service.repair(
                        scope_keys=(scope_key,), include_own=True, sweep=False
                    )  # inconnu conservé, jamais rejoué
                _finish(journal, path, scope_key, name)
                raise
            except BrokerError:
                pass  # attendu : la classification vient du registre, jamais du texte de l'exception
            except Exception:  # noqa: BLE001 - incident interne : arrêt durable, rien n'est supposé favorable
                journal.halt("internal_error")
            resolved = _finish(journal, path, scope_key, name)
            guard.current = None
            if resolved.state not in _CONTINUE:
                break
    except asyncio.CancelledError:
        journal.halt("interrupted")
        raise
    except Exception:  # noqa: BLE001 - tout incident du lanceur est durable et refuse la suite
        with contextlib.suppress(Exception):
            journal.halt("internal_error")
    finally:
        _ACTIVE.discard(str(path.resolve()))
        with contextlib.suppress(Exception):
            if service is not None:
                service.shutdown()
        with contextlib.suppress(Exception):
            journal.close_run()
    return read_report(database=path, campaign_id=campaign_id)


def reconcile_pair(*, database: Path, campaign_id: str) -> dict:
    """Reprise sans fournisseur : répare le registre (les tentatives d'un conducteur disparu deviennent inconnues et BLOQUANTES,
    jamais rejouées), fixe l'état des variantes restées en vol, ferme le journal, puis rapporte. N'émet, ne compte ni ne génère."""
    _check_campaign(campaign_id)
    path = Path(database)
    report = read_report(database=path, campaign_id=campaign_id)
    if report["reason"] in (
        "not_started",
        "campaign_mismatch",
        "foreign_registry",
        "registry_unreadable",
        "journal_corrupt",
    ):
        return report
    try:
        journal = _Journal(path, campaign_id, _utcnow)
        with _reader(path) as conn:
            run = _run_of(conn.execute(f"SELECT {_RUN_COLUMNS} FROM w5_json_pair_run").fetchone())
        if run.state == "closed":
            return report
        if _alive(run, path):
            return report  # un conducteur vivant : on ne touche à rien
        from collegue.state import ProjectStateManager

        with _reader(path) as conn:
            registry = _read_registry(conn, run.scope_key)
        if registry.present:
            ledger = ProjectStateManager.from_url(_database_url(path), create=False).budget_ledger
            service = BrokerService(ledger, _NoUpstream())
            service.repair(scope_keys=(run.scope_key,), sweep=True)
            service.shutdown()
        registry = _snapshot_registry(path, run.scope_key)
        for variant in journal.variants():
            if variant.state not in ("claimed", "emitting"):
                continue
            resolved = _resolve(
                variant, (registry.attempts or {}).get(variant.request_id), bool(registry.blocked), called=False
            )
            if resolved.state == "in_progress":
                resolved = _Resolved("unknown", failure="ambiguous")
            journal.finish_variant(resolved, variant.name)
        journal.close_run()
    except Exception:  # noqa: BLE001 - une réconciliation qui échoue ne rend jamais un rapport favorable
        return read_report(database=path, campaign_id=campaign_id) | {
            "reason": "internal_error",
            "status": "incomplete",
        }
    return read_report(database=path, campaign_id=campaign_id)


def report_exit_code(report: dict) -> int:
    return EXIT_CODES.get(report.get("status"), EXIT_INCOMPLETE)


__all__ = [
    "CONFIRMATION",
    "ENVELOPE",
    "EXIT_FAILED",
    "EXIT_INCOMPLETE",
    "EXIT_OBSERVED",
    "JsonPairRefused",
    "MODEL",
    "REQUEST_SHA256",
    "VARIANT_NAMES",
    "payloads",
    "read_report",
    "reconcile_pair",
    "report_exit_code",
    "request_fingerprints",
    "run_pair",
]
