"""Modèles SQLAlchemy de l'état projet durable (C6, brief §4.6).

Store d'état pour le moteur autonome — projet, graphe de tâches, journal de
décisions, métriques, checkpoints — afin de survivre aux redémarrages sur des
runs de plusieurs jours. **Distinct** de l'outil read-only
``collegue/tools/postgres_db.py`` (inspection de BDD utilisateur).

Types **portables** : les modèles sont testables sur SQLite (CI, sans Postgres) et
ciblent PostgreSQL en production via les migrations Alembic. Deux précautions de
portabilité :

- :class:`UTCDateTime` (TypeDecorator) force ``tzinfo=UTC`` à la lecture, sinon
  SQLite rend des datetimes *naïfs* et PostgreSQL des *aware* → comparaisons qui
  planteraient dans un seul des deux environnements.
- Les ``server_default`` des modèles **correspondent** à ceux de la migration
  Alembic : le modèle reste la source de vérité (pas de drift autogenerate).

Module **isolé**, non câblé au runtime tant que le pilote (Phase 3) ne l'utilise pas.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import List, Optional

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    false,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import TypeDecorator


def _utcnow() -> datetime:
    """Horodatage UTC timezone-aware (défaut Python des colonnes temporelles)."""
    return datetime.now(timezone.utc)


class UTCDateTime(TypeDecorator):
    """``DateTime(timezone=True)`` qui garantit des valeurs UTC *aware*.

    SQLite ne conserve pas la tzinfo (rend du naïf) alors que PostgreSQL rend de
    l'aware : sans normalisation, ``deadline < datetime.now(timezone.utc)``
    planterait sur un backend et pas l'autre. On force donc UTC en écriture comme
    en lecture, de façon identique partout.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is not None and value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value

    def process_result_value(self, value, dialect):
        if value is not None and value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value


class Base(DeclarativeBase):
    """Base déclarative commune ; ``Base.metadata`` alimente create_all / Alembic."""


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    spec: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    deadline: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, nullable=True)
    phase: Mapped[str] = mapped_column(String(64), nullable=False, default="0", server_default="0")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active", server_default="active")
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, nullable=False, default=_utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime, nullable=False, default=_utcnow, onupdate=_utcnow, server_default=func.now()
    )
    # Empreinte du plan (SPEC + tâches) au moment de l'approbation humaine (P5).
    # Lie l'approbation à un contenu précis : toute mutation ultérieure du plan
    # invalide le gate (anti-TOCTOU). NULL tant que non approuvé.
    approved_plan_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    # Politique §4.7 persistée avec le projet : une fois les oracles QA générés,
    # désactiver accidentellement le flag d'environnement au run ne doit pas les
    # contourner. Toute modification de cette valeur invalide aussi le plan hashé.
    acceptance_tests_required: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )

    # Cascade gérée par l'ORM (cascade="all, delete-orphan") : marche partout.
    # ondelete="CASCADE" reste sur la FK pour PostgreSQL (défense en profondeur ;
    # SQLite l'applique aussi car from_url active PRAGMA foreign_keys=ON).
    tasks: Mapped[List["Task"]] = relationship(back_populates="project", cascade="all, delete-orphan")
    decisions: Mapped[List["Decision"]] = relationship(back_populates="project", cascade="all, delete-orphan")
    metrics: Mapped[List["Metric"]] = relationship(back_populates="project", cascade="all, delete-orphan")
    checkpoints: Mapped[List["Checkpoint"]] = relationship(back_populates="project", cascade="all, delete-orphan")


class Task(Base):
    __tablename__ = "tasks"
    # Un numéro d'issue GitHub ne mappe qu'une seule tâche (NULL multiples permis :
    # tâches non encore synchronisées). Intégrité du mapping task↔issue (P4).
    __table_args__ = (
        UniqueConstraint("project_id", "issue_number", name="uq_tasks_project_issue"),
        # §4.7 : l'artefact QA constitue un triplet indivisible. Un source sans
        # empreinte/provenance (ou l'inverse) ne doit jamais survivre à un crash
        # ni à une mise à jour partielle. ``JSON(none_as_null=True)`` ci-dessous
        # garantit que Python ``None`` devient bien SQL NULL pour cette contrainte.
        CheckConstraint(
            "(acceptance_test_source IS NULL "
            "AND acceptance_test_sha256 IS NULL "
            "AND acceptance_test_provenance IS NULL) "
            "OR (acceptance_test_source IS NOT NULL "
            "AND acceptance_test_sha256 IS NOT NULL "
            "AND acceptance_test_provenance IS NOT NULL)",
            name="ck_tasks_acceptance_test_artifact_complete",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    acceptance: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Artefact de test d'acceptation produit au plan-time par le rôle QA (§4.7).
    # Le source exact est conservé pour rejouer le même oracle ; son SHA-256 et
    # sa provenance rendent toute substitution détectable et auditable. Les trois
    # colonnes sont nullables pour les projets historiques, mais la contrainte de
    # table impose qu'elles soient renseignées atomiquement ou toutes absentes.
    acceptance_test_source: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    acceptance_test_sha256: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    acceptance_test_provenance: Mapped[Optional[dict]] = mapped_column(JSON(none_as_null=True), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="todo", server_default="todo")
    # Liste d'IDs de tâches dont celle-ci dépend (graphe de tâches).
    depends_on: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    # Numéro d'issue GitHub une fois la tâche synchronisée (P4) ; NULL sinon.
    # Sert de mapping task↔issue et de garde d'idempotence (ne pas recréer).
    issue_number: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # Tentatives d'exécution déjà consommées (retry au niveau tâche, #420) :
    # persistées pour que le plafond max_attempts survive aux redémarrages.
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    # Dernier motif d'échec connu (stage/raison + extrait) — diagnostic post-mortem
    # et ré-injection de feedback à la tentative suivante (#420/#424).
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Mémoire de la MEILLEURE tentative (#436) : diff + score de tests + échecs
    # restants. Le retry réensemence son workspace avec ce diff (réparation
    # incrémentale ciblée) au lieu de tout régénérer — sans elle, une tentative
    # quasi-verte (26/27) peut être JETÉE puis remplacée par pire (oscillation).
    best_diff: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    best_passed: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    best_failed: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    best_feedback: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # #466 : chemin du workspace d'échec CONSERVÉ pour debug (#443) — persisté
    # pour que la purge au succès/merge fonctionne à travers les restarts.
    kept_workspace: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, nullable=False, default=_utcnow, server_default=func.now()
    )

    project: Mapped["Project"] = relationship(back_populates="tasks")


class Decision(Base):
    __tablename__ = "decisions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    ts: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, default=_utcnow, server_default=func.now())
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    rationale: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    project: Mapped["Project"] = relationship(back_populates="decisions")


class Metric(Base):
    __tablename__ = "metrics"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    ts: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, default=_utcnow, server_default=func.now())
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    value: Mapped[float] = mapped_column(Float, nullable=False)

    project: Mapped["Project"] = relationship(back_populates="metrics")


class Checkpoint(Base):
    __tablename__ = "checkpoints"
    # Un seul checkpoint par itération (la reprise doit pointer un état non ambigu).
    __table_args__ = (UniqueConstraint("project_id", "iteration", name="uq_checkpoints_project_iteration"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    iteration: Mapped[int] = mapped_column(Integer, nullable=False)
    # Snapshot d'état sérialisable (repris par C7 pour la reprise).
    state_json: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    ts: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, default=_utcnow, server_default=func.now())

    project: Mapped["Project"] = relationship(back_populates="checkpoints")
