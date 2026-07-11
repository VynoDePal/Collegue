"""``ProjectStateManager`` : CRUD sur l'état projet durable (C6, brief §4.6).

Accepte une ``session_factory`` (``sessionmaker``) injectable → testable sur
SQLite en mémoire sans Postgres. En production : ``from_url(STATE_DATABASE_URL)``.
``expire_on_commit=False`` garde les attributs scalaires lisibles après la
fermeture de session (objets détachés). ``get_project`` eager-load les relations
pour qu'elles restent accessibles hors session ; les getters dédiés
(``get_tasks`` …) restent disponibles pour ne charger qu'une collection.

Note : ``updated_at`` est mis à jour par le hook ORM ``onupdate`` — il faut donc
passer par l'API ORM (les méthodes de ce manager), pas par des ``UPDATE`` bruts.

Module **isolé** : non importé par ``app.py`` (le pilote, Phase 3, le câblera).
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator, List, Mapping, Optional

from sqlalchemy import create_engine, delete, event, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload, sessionmaker

from collegue.state.models import (
    PHASE5_ATTENTION,
    PHASE5_HEALTH_PENDING,
    PHASE5_INCIDENT_STATES,
    PHASE5_MERGE_METHODS,
    PHASE5_MERGE_PENDING,
    PHASE5_RECOVERED,
    PHASE5_REVERT_IN_PROGRESS,
    PHASE5_REVERT_PENDING,
    Base,
    Checkpoint,
    Decision,
    Metric,
    Phase5Incident,
    Project,
    Task,
)

_FULL_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")
_UNSET = object()
_PHASE5_TRANSITIONS = {
    PHASE5_MERGE_PENDING: frozenset({PHASE5_HEALTH_PENDING, PHASE5_ATTENTION}),
    PHASE5_HEALTH_PENDING: frozenset({PHASE5_REVERT_PENDING, PHASE5_ATTENTION}),
    PHASE5_REVERT_PENDING: frozenset({PHASE5_ATTENTION}),
    PHASE5_REVERT_IN_PROGRESS: frozenset({PHASE5_REVERT_PENDING, PHASE5_RECOVERED, PHASE5_ATTENTION}),
    PHASE5_ATTENTION: frozenset(),
    PHASE5_RECOVERED: frozenset(),
}


class Phase5IncidentConflictError(RuntimeError):
    """Une création/transition Phase 5 ne correspond plus à l'état durable."""


def _like_escape(query: str) -> str:
    """Échappe les métacaractères LIKE (``%``, ``_``, ``\\``) d'une requête utilisateur.

    Sans cela, une recherche ``"%"`` matcherait tout et ``"100%"`` ne trouverait
    pas le texte littéral « 100% ». À utiliser avec ``.ilike(pattern, escape="\\")``.
    """
    return query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _normalize_acceptance_test_artifact(source: Any, provenance: Any) -> tuple[str, dict]:
    """Valide/copie un artefact QA et retourne ``(sha256, provenance JSON)``."""
    if not isinstance(source, str) or not source.strip():
        raise ValueError("source du test d'acceptation vide ou invalide")
    if not isinstance(provenance, dict) or not provenance:
        raise ValueError("provenance du test d'acceptation vide ou invalide")
    try:
        provenance_blob = json.dumps(
            provenance,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        normalized_provenance = json.loads(provenance_blob)
    except (TypeError, ValueError) as exc:
        raise ValueError("provenance du test d'acceptation non sérialisable en JSON") from exc
    return hashlib.sha256(source.encode("utf-8")).hexdigest(), normalized_provenance


def _phase5_text(value: Any, label: str, *, max_length: Optional[int] = None) -> str:
    """Valide un texte d'ancrage Phase 5 sans le normaliser silencieusement."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} Phase 5 vide ou invalide")
    if max_length is not None and len(value) > max_length:
        raise ValueError(f"{label} Phase 5 trop long ({len(value)} > {max_length})")
    return value


def _phase5_sha(value: Any, label: str, *, nullable: bool = False) -> Optional[str]:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or not _FULL_SHA_RE.fullmatch(value):
        raise ValueError(f"{label} Phase 5 invalide: {value!r}")
    return value.lower()


def _phase5_state(value: Any) -> str:
    if value not in PHASE5_INCIDENT_STATES:
        raise ValueError(f"état d'incident Phase 5 invalide: {value!r}")
    return str(value)


def _phase5_revision(value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"révision CAS Phase 5 invalide: {value!r}")
    return value


def _phase5_identity_matches(incident: Phase5Incident, payload: Mapping[str, Any]) -> bool:
    """Identité immuable d'une promotion, utilisée pour un begin idempotent."""
    fields = (
        "owner",
        "repo",
        "base_branch",
        "source_pr_number",
        "source_head_sha",
        "base_sha_before_merge",
        "merge_method",
        "health_command",
        "revert_enabled",
    )
    return all(getattr(incident, field) == payload[field] for field in fields)


class ProjectStateManager:
    """CRUD sur le store d'état (projects/tasks/decisions/metrics/checkpoints)."""

    def __init__(self, session_factory: sessionmaker):
        self._session_factory = session_factory

    @classmethod
    def from_url(cls, url: str, *, create: bool = False, echo: bool = False) -> "ProjectStateManager":
        """Construit un manager depuis une URL SQLAlchemy.

        ``create=True`` crée les tables via ``Base.metadata.create_all`` (pratique
        pour SQLite/tests). En production PostgreSQL, préférer les migrations
        Alembic et laisser ``create=False``.
        """
        engine = create_engine(url, echo=echo, future=True)
        # SQLite n'applique pas les FK par défaut → les violations (ex. tâche sur
        # un project_id inexistant) passeraient en test mais planteraient sur
        # PostgreSQL. On active PRAGMA foreign_keys=ON pour aligner les deux.
        if engine.dialect.name == "sqlite":

            @event.listens_for(engine, "connect")
            def _enable_sqlite_fk(dbapi_conn, _record):  # pragma: no cover - hook DBAPI
                cursor = dbapi_conn.cursor()
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.close()

        if create:
            Base.metadata.create_all(engine)
        return cls(sessionmaker(bind=engine, expire_on_commit=False))

    @contextmanager
    def session(self) -> Iterator[Session]:
        """Session transactionnelle : commit au succès, rollback sinon, close toujours."""
        session = self._session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    # ── projects ──────────────────────────────────────────────────────────────

    def create_project(
        self,
        name: str,
        spec: Optional[str] = None,
        deadline: Optional[datetime] = None,
        phase: str = "0",
        status: str = "active",
        plan_sync_config: Optional[dict] = None,
    ) -> int:
        with self.session() as s:
            project = Project(
                name=name,
                spec=spec,
                deadline=deadline,
                phase=phase,
                status=status,
                plan_sync_config=plan_sync_config,
            )
            s.add(project)
            s.flush()
            return project.id

    def get_project(self, project_id: int) -> Optional[Project]:
        # Eager-load des relations : l'objet retourné est détaché (session fermée)
        # mais ``project.tasks`` / ``.decisions`` … restent accessibles sans
        # DetachedInstanceError (sinon un accès paresseux post-close planterait).
        with self.session() as s:
            return s.get(
                Project,
                project_id,
                options=[
                    selectinload(Project.tasks),
                    selectinload(Project.decisions),
                    selectinload(Project.metrics),
                    selectinload(Project.checkpoints),
                    selectinload(Project.phase5_incident),
                ],
            )

    def list_projects(self) -> List[Project]:
        with self.session() as s:
            return list(s.scalars(select(Project).order_by(Project.id)))

    def update_project(self, project_id: int, **fields: Any) -> bool:
        """Met à jour des champs (status, phase, spec, deadline…). Retourne False si absent."""
        with self.session() as s:
            project = s.get(Project, project_id)
            if project is None:
                return False
            for key, value in fields.items():
                if hasattr(project, key):
                    setattr(project, key, value)
            return True

    # ── tasks ─────────────────────────────────────────────────────────────────

    def add_task(
        self,
        project_id: int,
        title: str,
        acceptance: Optional[str] = None,
        status: str = "todo",
        depends_on: Optional[List[int]] = None,
    ) -> int:
        with self.session() as s:
            task = Task(
                project_id=project_id,
                title=title,
                acceptance=acceptance,
                status=status,
                depends_on=depends_on,
            )
            s.add(task)
            s.flush()
            return task.id

    def get_tasks(self, project_id: int) -> List[Task]:
        with self.session() as s:
            return list(s.scalars(select(Task).where(Task.project_id == project_id).order_by(Task.id)))

    def get_task(self, task_id: int) -> Optional[Task]:
        """Une tâche par id (``None`` si absente)."""
        with self.session() as s:
            return s.get(Task, task_id)

    def update_task_status(self, task_id: int, status: str) -> bool:
        with self.session() as s:
            task = s.get(Task, task_id)
            if task is None:
                return False
            task.status = status
            return True

    def update_task(self, task_id: int, **fields: Any) -> bool:
        """Met à jour des champs d'une tâche (status, issue_number…). False si absente."""
        with self.session() as s:
            task = s.get(Task, task_id)
            if task is None:
                return False
            for key, value in fields.items():
                if hasattr(task, key):
                    setattr(task, key, value)
            return True

    # ── artefacts QA ──────────────────────────────────────────────────────────

    def set_acceptance_test_artifact(self, task_id: int, source: str, provenance: dict) -> bool:
        """Persiste atomiquement l'artefact QA plan-time d'une tâche (§4.7).

        L'appelant ne fournit jamais l'empreinte : elle est calculée ici sur les
        octets UTF-8 exacts du source, dans la même transaction que le source et
        la provenance. La provenance est validée comme objet JSON strict puis
        copiée par round-trip pour qu'une mutation ultérieure du dictionnaire de
        l'appelant ne modifie pas silencieusement la valeur persistée.

        Retourne ``False`` si la tâche n'existe pas. Un artefact vide ou une
        provenance vide/non sérialisable lève ``ValueError`` : mieux vaut ne rien
        persister qu'un oracle incomplet pris pour une preuve d'acceptation.
        """
        digest, normalized_provenance = _normalize_acceptance_test_artifact(source, provenance)
        with self.session() as s:
            task = s.get(Task, task_id)
            if task is None:
                return False
            task.acceptance_test_source = source
            task.acceptance_test_sha256 = digest
            task.acceptance_test_provenance = normalized_provenance
            return True

    def set_acceptance_test_artifacts(
        self,
        project_id: int,
        artifacts: Mapping[int, Mapping[str, Any]],
    ) -> bool:
        """Persiste en une transaction le jeu COMPLET d'oracles QA d'un projet.

        ``artifacts`` mappe chaque ``task_id`` vers exactement ``source`` et
        ``provenance``. L'ensemble des IDs doit être identique à celui des tâches
        du projet : une omission ou un ID étranger annule toute l'opération. Tous
        les payloads sont normalisés avant la première affectation ; le context
        transactionnel garantit ensuite le rollback sur toute erreur SQL.

        Retourne ``False`` si le projet n'existe pas, ``True`` après persistance
        (y compris pour un projet sans tâche avec un mapping vide).
        """
        if not isinstance(artifacts, Mapping):
            raise ValueError("artefacts d'acceptation invalides : mapping attendu")

        normalized: dict[int, tuple[str, str, dict]] = {}
        for task_id, artifact in artifacts.items():
            if not isinstance(task_id, int) or isinstance(task_id, bool):
                raise ValueError(f"ID de tâche invalide dans les artefacts : {task_id!r}")
            if not isinstance(artifact, Mapping) or set(artifact) != {"source", "provenance"}:
                raise ValueError(f"artefact de la tâche {task_id} invalide : clés source/provenance exactes requises")
            source = artifact["source"]
            digest, provenance = _normalize_acceptance_test_artifact(source, artifact["provenance"])
            normalized[task_id] = (source, digest, provenance)

        with self.session() as s:
            if s.get(Project, project_id) is None:
                return False
            tasks = list(s.scalars(select(Task).where(Task.project_id == project_id).order_by(Task.id)))
            expected_ids = {task.id for task in tasks}
            supplied_ids = set(normalized)
            if supplied_ids != expected_ids:
                missing = sorted(expected_ids - supplied_ids)
                foreign = sorted(supplied_ids - expected_ids)
                raise ValueError(
                    "jeu d'artefacts incomplet ou étranger au projet "
                    f"{project_id} (manquants={missing}, étrangers={foreign})"
                )
            for task in tasks:
                source, digest, provenance = normalized[task.id]
                task.acceptance_test_source = source
                task.acceptance_test_sha256 = digest
                task.acceptance_test_provenance = provenance
            # Politique durable : un plan qui contient ces oracles doit toujours
            # les exécuter, même si l'environnement du run omet ensuite le flag.
            project = s.get(Project, project_id)
            project.acceptance_tests_required = True
            return True

    # ── decisions ──────────────────────────────────────────────────

    def add_decision(self, project_id: int, summary: str, rationale: Optional[str] = None) -> int:
        with self.session() as s:
            decision = Decision(project_id=project_id, summary=summary, rationale=rationale)
            s.add(decision)
            s.flush()
            return decision.id

    def get_decisions(self, project_id: int) -> List[Decision]:
        """Toutes les décisions d'un projet (délègue à :meth:`get_decision_journal`)."""
        return self.get_decision_journal(project_id)

    def record_decision(self, project_id: int, summary: str, rationale: Optional[str] = None) -> int:
        """Journalise une décision (nom du brief C7 ; alias de :meth:`add_decision`)."""
        return self.add_decision(project_id, summary, rationale)

    def get_decision_journal(self, project_id: int, query: Optional[str] = None) -> List[Decision]:
        """Journal de décisions, avec recherche optionnelle par sous-chaîne.

        ``query`` filtre sur ``summary``/``rationale`` via ``ilike`` (métacaractères
        LIKE échappés). Casse : insensible **ASCII** sur SQLite (``lower()`` ASCII),
        selon la collation sur PostgreSQL — les caractères accentués peuvent donc
        différer entre backends. Pas d'index dédié (un GIN pg_trgm serait l'optim
        prod, différée : extension PG only, non applicable sur SQLite).
        """
        with self.session() as s:
            stmt = select(Decision).where(Decision.project_id == project_id)
            if query:
                like = f"%{_like_escape(query)}%"
                stmt = stmt.where(
                    or_(Decision.summary.ilike(like, escape="\\"), Decision.rationale.ilike(like, escape="\\"))
                )
            return list(s.scalars(stmt.order_by(Decision.id)))

    def search_tasks(self, project_id: int, query: str) -> List[Task]:
        """Recherche de tâches par sous-chaîne sur titre/critère (cf. casse: get_decision_journal)."""
        like = f"%{_like_escape(query)}%"
        with self.session() as s:
            stmt = (
                select(Task)
                .where(Task.project_id == project_id)
                .where(or_(Task.title.ilike(like, escape="\\"), Task.acceptance.ilike(like, escape="\\")))
                .order_by(Task.id)
            )
            return list(s.scalars(stmt))

    # ── metrics ─────────────────────────────────────────────────────────────────

    def begin_phase5_incident(
        self,
        project_id: int,
        *,
        owner: str,
        repo: str,
        base_branch: str,
        source_pr_number: int,
        source_head_sha: str,
        base_sha_before_merge: str,
        merge_method: str,
        health_command: str,
        revert_enabled: bool,
    ) -> Phase5Incident:
        """Insère le write-ahead Phase 5 avant le merge GitHub.

        L'opération est idempotente uniquement pour la même identité immuable.
        Un autre incident actif lève :class:`Phase5IncidentConflictError`.
        """
        if not isinstance(project_id, int) or isinstance(project_id, bool) or project_id <= 0:
            raise ValueError(f"project_id Phase 5 invalide: {project_id!r}")
        if not isinstance(source_pr_number, int) or isinstance(source_pr_number, bool) or source_pr_number <= 0:
            raise ValueError(f"numéro de PR Phase 5 invalide: {source_pr_number!r}")
        if merge_method not in PHASE5_MERGE_METHODS:
            raise ValueError(f"méthode de merge Phase 5 invalide: {merge_method!r}")
        if not isinstance(revert_enabled, bool):
            raise ValueError(f"revert_enabled Phase 5 doit être booléen: {revert_enabled!r}")
        payload = {
            "owner": _phase5_text(owner, "owner", max_length=255),
            "repo": _phase5_text(repo, "repo", max_length=255),
            "base_branch": _phase5_text(base_branch, "branche de base", max_length=255),
            "source_pr_number": source_pr_number,
            "source_head_sha": _phase5_sha(source_head_sha, "SHA source"),
            "base_sha_before_merge": _phase5_sha(base_sha_before_merge, "SHA de base"),
            "merge_method": str(merge_method),
            "health_command": _phase5_text(health_command, "commande de santé"),
            "revert_enabled": revert_enabled,
        }

        try:
            with self.session() as s:
                if s.get(Project, project_id) is None:
                    raise ValueError(f"projet Phase 5 introuvable: {project_id}")
                existing = s.get(Phase5Incident, project_id)
                if existing is not None:
                    if _phase5_identity_matches(existing, payload):
                        return existing
                    raise Phase5IncidentConflictError(
                        f"le projet {project_id} possède déjà un autre incident Phase 5 "
                        f"({existing.state}, PR #{existing.source_pr_number})"
                    )
                incident = Phase5Incident(
                    project_id=project_id,
                    state=PHASE5_MERGE_PENDING,
                    revision=0,
                    **payload,
                )
                s.add(incident)
                s.flush()
                return incident
        except IntegrityError as exc:
            # Course d'insertion : le PK project_id garantit un gagnant.
            existing = self.get_phase5_incident(project_id)
            if existing is not None and _phase5_identity_matches(existing, payload):
                return existing
            raise Phase5IncidentConflictError(
                f"création concurrente d'un incident Phase 5 pour le projet {project_id}"
            ) from exc

    def get_phase5_incident(self, project_id: int) -> Optional[Phase5Incident]:
        """Incident Phase 5 actif du projet, ou ``None``."""
        with self.session() as s:
            return s.get(Phase5Incident, project_id)

    def claim_phase5_revert(
        self,
        project_id: int,
        *,
        expected_state: str,
        expected_revision: int,
        expected_source_pr_number: int,
        expected_source_head_sha: str,
        lease_seconds: float = 300.0,
        now: Optional[datetime] = None,
        claim_token: Optional[str] = None,
    ) -> Phase5Incident:
        """Acquiert par CAS un lease avant toute écriture GitHub de rollback.

        Un lease actif bloque les autres workers. Après expiration, un nouveau
        worker peut le reprendre avec la révision et le token persistés.
        """
        if expected_state not in {PHASE5_REVERT_PENDING, PHASE5_REVERT_IN_PROGRESS}:
            raise ValueError(f"état non réclamable pour le revert Phase 5: {expected_state!r}")
        expected_revision = _phase5_revision(expected_revision)
        expected_head = _phase5_sha(expected_source_head_sha, "SHA source attendu")
        if not isinstance(expected_source_pr_number, int) or expected_source_pr_number <= 0:
            raise ValueError("numéro de PR CAS Phase 5 invalide")
        duration = float(lease_seconds)
        if duration <= 0:
            raise ValueError("durée de lease Phase 5 invalide")
        now = now or datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        token = claim_token or secrets.token_hex(16)
        if not isinstance(token, str) or not token.strip() or len(token) > 64:
            raise ValueError("token de lease Phase 5 invalide")

        filters = [
            Phase5Incident.project_id == project_id,
            Phase5Incident.state == expected_state,
            Phase5Incident.revision == expected_revision,
            Phase5Incident.source_pr_number == expected_source_pr_number,
            Phase5Incident.source_head_sha == expected_head,
        ]
        if expected_state == PHASE5_REVERT_IN_PROGRESS:
            filters.append(Phase5Incident.revert_claim_expires_at <= now)
        with self.session() as s:
            changed = s.execute(
                update(Phase5Incident)
                .where(*filters)
                .values(
                    state=PHASE5_REVERT_IN_PROGRESS,
                    revision=expected_revision + 1,
                    revert_claim_token=token,
                    revert_claim_expires_at=now + timedelta(seconds=duration),
                    updated_at=now,
                )
            )
            if changed.rowcount != 1:
                raise Phase5IncidentConflictError("lease de revert Phase 5 déjà détenu ou CAS périmé")
            incident = s.get(Phase5Incident, project_id, populate_existing=True)
            if incident is None:  # pragma: no cover
                raise Phase5IncidentConflictError("incident disparu après acquisition du lease")
            return incident

    def transition_phase5_incident(
        self,
        project_id: int,
        *,
        expected_state: str,
        expected_revision: int,
        expected_source_pr_number: int,
        expected_source_head_sha: str,
        new_state: str,
        merge_sha: Any = _UNSET,
        last_error: Any = _UNSET,
        expected_revert_claim_token: Optional[str] = None,
    ) -> Phase5Incident:
        """Transition CAS stricte : compare état/révision/PR/SHA puis incrémente."""
        expected_state = _phase5_state(expected_state)
        new_state = _phase5_state(new_state)
        expected_revision = _phase5_revision(expected_revision)
        if (
            not isinstance(expected_source_pr_number, int)
            or isinstance(expected_source_pr_number, bool)
            or expected_source_pr_number <= 0
        ):
            raise ValueError(f"numéro de PR CAS Phase 5 invalide: {expected_source_pr_number!r}")
        expected_head = _phase5_sha(expected_source_head_sha, "SHA source attendu")
        if new_state != expected_state and new_state not in _PHASE5_TRANSITIONS[expected_state]:
            raise ValueError(f"transition Phase 5 interdite: {expected_state!r} -> {new_state!r}")
        if new_state == PHASE5_REVERT_IN_PROGRESS:
            raise ValueError("utiliser claim_phase5_revert pour entrer en revert_in_progress")
        if expected_state == PHASE5_REVERT_IN_PROGRESS:
            if not expected_revert_claim_token:
                raise ValueError("token du lease requis pour quitter revert_in_progress")
        elif expected_revert_claim_token is not None:
            raise ValueError("token de lease inattendu hors revert_in_progress")

        values: dict[str, Any] = {
            "state": new_state,
            "revision": expected_revision + 1,
            "updated_at": datetime.now(timezone.utc),
        }
        if merge_sha is not _UNSET:
            values["merge_sha"] = _phase5_sha(merge_sha, "SHA de merge", nullable=True)
        if new_state in {PHASE5_HEALTH_PENDING, PHASE5_REVERT_PENDING}:
            if expected_state == PHASE5_MERGE_PENDING and values.get("merge_sha") is None:
                raise ValueError("SHA de merge requis pour quitter merge_pending")
            if merge_sha is not _UNSET and values["merge_sha"] is None:
                raise ValueError(f"SHA de merge interdit à NULL pour l'état {new_state}")
        if new_state == PHASE5_MERGE_PENDING and merge_sha is not _UNSET and values["merge_sha"] is not None:
            raise ValueError("merge_pending ne peut pas porter de SHA de merge")
        if last_error is not _UNSET:
            if last_error is not None and not isinstance(last_error, str):
                raise ValueError("last_error Phase 5 doit être une chaîne ou None")
            values["last_error"] = last_error
        if expected_state == PHASE5_REVERT_IN_PROGRESS:
            values["revert_claim_token"] = None
            values["revert_claim_expires_at"] = None

        with self.session() as s:
            filters = [
                Phase5Incident.project_id == project_id,
                Phase5Incident.state == expected_state,
                Phase5Incident.revision == expected_revision,
                Phase5Incident.source_pr_number == expected_source_pr_number,
                Phase5Incident.source_head_sha == expected_head,
            ]
            if expected_state == PHASE5_REVERT_IN_PROGRESS:
                filters.append(Phase5Incident.revert_claim_token == expected_revert_claim_token)
            result = s.execute(update(Phase5Incident).where(*filters).values(**values))
            if result.rowcount != 1:
                actual = s.get(Phase5Incident, project_id)
                observed = (
                    "absent"
                    if actual is None
                    else f"state={actual.state}, revision={actual.revision}, PR=#{actual.source_pr_number}"
                )
                raise Phase5IncidentConflictError(
                    f"CAS Phase 5 refusé pour le projet {project_id}: attendu "
                    f"state={expected_state}, revision={expected_revision}, PR=#{expected_source_pr_number}; "
                    f"observé {observed}"
                )
            incident = s.get(Phase5Incident, project_id, populate_existing=True)
            if incident is None:  # pragma: no cover - protégé par rowcount=1
                raise Phase5IncidentConflictError("incident Phase 5 disparu après son CAS")
            return incident

    def clear_phase5_incident(
        self,
        project_id: int,
        *,
        expected_state: str,
        expected_revision: int,
        expected_source_pr_number: int,
        expected_source_head_sha: str,
        expected_revert_claim_token: Optional[str] = None,
    ) -> bool:
        """Supprime un incident résolu via le même CAS strict."""
        expected_state = _phase5_state(expected_state)
        expected_revision = _phase5_revision(expected_revision)
        if (
            not isinstance(expected_source_pr_number, int)
            or isinstance(expected_source_pr_number, bool)
            or expected_source_pr_number <= 0
        ):
            raise ValueError(f"numéro de PR CAS Phase 5 invalide: {expected_source_pr_number!r}")
        expected_head = _phase5_sha(expected_source_head_sha, "SHA source attendu")
        if expected_state == PHASE5_REVERT_IN_PROGRESS and not expected_revert_claim_token:
            raise ValueError("token du lease requis pour supprimer revert_in_progress")
        filters = [
            Phase5Incident.project_id == project_id,
            Phase5Incident.state == expected_state,
            Phase5Incident.revision == expected_revision,
            Phase5Incident.source_pr_number == expected_source_pr_number,
            Phase5Incident.source_head_sha == expected_head,
        ]
        if expected_state == PHASE5_REVERT_IN_PROGRESS:
            filters.append(Phase5Incident.revert_claim_token == expected_revert_claim_token)
        with self.session() as s:
            result = s.execute(delete(Phase5Incident).where(*filters))
            if result.rowcount != 1:
                raise Phase5IncidentConflictError(
                    f"suppression CAS Phase 5 refusée pour le projet {project_id} "
                    f"(state={expected_state}, revision={expected_revision}, PR=#{expected_source_pr_number})"
                )
            return True

    def acknowledge_phase5_incident(self, project_id: int, *, expected_revision: int) -> bool:
        """Acquitte explicitement un incident terminal après inspection humaine."""
        incident = self.get_phase5_incident(project_id)
        if incident is None:
            return False
        if incident.state not in {PHASE5_ATTENTION, PHASE5_RECOVERED}:
            raise Phase5IncidentConflictError(f"incident Phase 5 non terminal ({incident.state}) — acquittement refusé")
        if incident.revision != _phase5_revision(expected_revision):
            raise Phase5IncidentConflictError("révision d'acquittement Phase 5 périmée")
        return self.clear_phase5_incident(
            project_id,
            expected_state=incident.state,
            expected_revision=incident.revision,
            expected_source_pr_number=incident.source_pr_number,
            expected_source_head_sha=incident.source_head_sha,
        )

    def add_metric(self, project_id: int, name: str, value: float) -> int:
        with self.session() as s:
            metric = Metric(project_id=project_id, name=name, value=float(value))
            s.add(metric)
            s.flush()
            return metric.id

    def get_metrics(self, project_id: int, name: Optional[str] = None) -> List[Metric]:
        with self.session() as s:
            stmt = select(Metric).where(Metric.project_id == project_id)
            if name is not None:
                stmt = stmt.where(Metric.name == name)
            return list(s.scalars(stmt.order_by(Metric.id)))

    # ── checkpoints ───────────────────────────────────────────────────────────────

    def save_checkpoint(self, project_id: int, iteration: int, state_json: Optional[dict] = None) -> int:
        """Enregistre (upsert) le checkpoint d'une itération.

        Un seul checkpoint par ``(project_id, iteration)`` : si l'itération existe
        déjà (ex. itération relancée après crash), on met à jour son ``state_json``
        au lieu d'insérer un doublon qui masquerait l'original. Garanti aussi au
        niveau DB par une contrainte d'unicité (migration 0002).
        """
        with self.session() as s:
            existing = s.scalars(
                select(Checkpoint).where(Checkpoint.project_id == project_id, Checkpoint.iteration == iteration)
            ).first()
            if existing is not None:
                existing.state_json = state_json
                s.flush()
                return existing.id
            checkpoint = Checkpoint(project_id=project_id, iteration=iteration, state_json=state_json)
            s.add(checkpoint)
            s.flush()
            return checkpoint.id

    def get_latest_checkpoint(self, project_id: int) -> Optional[Checkpoint]:
        """Dernier checkpoint (itération la plus élevée) d'un projet, ou None."""
        with self.session() as s:
            stmt = (
                select(Checkpoint)
                .where(Checkpoint.project_id == project_id)
                .order_by(Checkpoint.iteration.desc(), Checkpoint.id.desc())
                .limit(1)
            )
            return s.scalars(stmt).first()

    def load_checkpoint(self, project_id: int, iteration: Optional[int] = None) -> Optional[Checkpoint]:
        """Charge un checkpoint : celui de l'``iteration`` donnée, sinon le dernier.

        Cœur de la **reprise** (C7) : après un redémarrage, on recharge l'état
        depuis la DB pour repartir de la dernière itération checkpointée.
        """
        if iteration is None:
            return self.get_latest_checkpoint(project_id)
        with self.session() as s:
            stmt = (
                select(Checkpoint)
                .where(Checkpoint.project_id == project_id, Checkpoint.iteration == iteration)
                .order_by(Checkpoint.id.desc())
                .limit(1)
            )
            return s.scalars(stmt).first()
