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

from contextlib import contextmanager
from datetime import datetime
from typing import Any, Iterator, List, Optional

from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session, selectinload, sessionmaker

from collegue.state.models import Base, Checkpoint, Decision, Metric, Project, Task


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
    ) -> int:
        with self.session() as s:
            project = Project(name=name, spec=spec, deadline=deadline, phase=phase, status=status)
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

    def update_task_status(self, task_id: int, status: str) -> bool:
        with self.session() as s:
            task = s.get(Task, task_id)
            if task is None:
                return False
            task.status = status
            return True

    # ── decisions ───────────────────────────────────────────────────────────────

    def add_decision(self, project_id: int, summary: str, rationale: Optional[str] = None) -> int:
        with self.session() as s:
            decision = Decision(project_id=project_id, summary=summary, rationale=rationale)
            s.add(decision)
            s.flush()
            return decision.id

    def get_decisions(self, project_id: int) -> List[Decision]:
        with self.session() as s:
            return list(s.scalars(select(Decision).where(Decision.project_id == project_id).order_by(Decision.id)))

    # ── metrics ─────────────────────────────────────────────────────────────────

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
        with self.session() as s:
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
