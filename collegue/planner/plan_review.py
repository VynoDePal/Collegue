"""Gate de validation humaine du plan (P5, #356).

Garde-fou du brief (§Phase 1) : le plan **n'avance pas** sans accord humain.
``build_plan_preview`` agrège un résumé lisible (SPEC + tâches + critères
d'acceptation + dépendances ; les hypothèses sont déjà dans le SPEC, P1) à partir
du state store en un snapshot verrouillé cohérent. ``approve_plan`` lie l'approbation au
**contenu exact** du plan (empreinte SHA-256 persistée) ; ``is_approved`` recalcule
l'empreinte et la compare → toute **mutation du plan après approbation invalide le
gate** (anti-TOCTOU). ``require_approved`` lève si non approuvé.

Contrat P4 : la couche d'écriture GitHub DOIT charger un snapshot avec
``require_approval=True`` avant toute écriture ; sans cela le garde-fou est contournable.

Module **isolé** : non câblé au runtime tant que le pilote (Phase 3) ne l'enchaîne pas.
"""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import select

from collegue.planner._parsing import inline
from collegue.planner.plan_target import PlanTargetError, normalize_plan_sync_config
from collegue.planner.status import PROJECT_STATUS_APPROVED
from collegue.state.models import Decision, Project, Task


class PlanNotApproved(Exception):
    """Le plan n'a pas été approuvé (ou a changé depuis) — P4 doit refuser d'écrire."""


class PlanHashMismatch(ValueError):
    """L'aperçu approuvé par l'humain ne correspond plus au plan courant."""


@dataclass(frozen=True)
class PlanTaskSnapshot:
    """Copie immuable des champs d'une tâche couverts par le plan/livraison."""

    id: int
    title: str
    acceptance: Optional[str]
    depends_on: tuple[int, ...]
    acceptance_test_source: Optional[str]
    acceptance_test_sha256: Optional[str]
    acceptance_test_provenance: Any
    issue_number: Optional[int]


@dataclass(frozen=True)
class PlanStateSnapshot:
    """Snapshot cohérent chargé sous verrou avant une action externe."""

    project_id: int
    name: str
    status: str
    spec: Optional[str]
    deadline: Optional[datetime]
    acceptance_tests_required: bool
    _plan_sync_config_json: Optional[str]
    approved_plan_hash: Optional[str]
    tasks: tuple[PlanTaskSnapshot, ...]
    plan_hash: str
    approved: bool

    @property
    def plan_sync_config(self) -> Optional[Dict[str, Any]]:
        """Rend une copie neuve : l'état interne du snapshot reste immuable."""
        return json.loads(self._plan_sync_config_json) if self._plan_sync_config_json is not None else None


@dataclass
class PlanPreview:
    """Résumé lisible d'un plan, soumis à validation humaine avant écriture GitHub."""

    project_id: int
    name: str
    status: str
    approved: bool
    spec: Optional[str]
    tasks: List[Dict[str, Any]]
    task_count: int
    acceptance_tests_required: bool = False
    plan_sync_config: Optional[Dict[str, Any]] = None
    deadline: Optional[datetime] = None
    plan_hash: str = ""

    def to_markdown(self) -> str:
        def code(value: Any) -> str:
            """Texte sûr à l'intérieur d'un code span Markdown."""
            return inline(value).replace("`", "ʼ")

        lines = [
            f"# Plan : {inline(self.name)}",
            "",
            f"Statut : `{self.status}` · Tâches : {self.task_count} · "
            f"Approuvé : {'✅ oui' if self.approved else '❌ non'}",
            f"Oracles QA §4.7 : {'requis' if self.acceptance_tests_required else 'désactivés'}",
            f"Deadline absolue : `{_deadline_value(self.deadline) or 'aucune'}`",
            f"Empreinte : `{self.plan_hash}`",
            "",
        ]
        if self.plan_sync_config is not None:
            target = self.plan_sync_config
            labels = ", ".join(f"`{code(label)}`" for label in (target.get("labels") or [])) or "_(aucun)_"
            lines.extend(
                [
                    "## Cible GitHub",
                    "",
                    f"- Dépôt : `{code(target.get('owner') or '')}/{code(target.get('repo') or '')}`",
                    f"- SPEC : `{code(target.get('spec_filename') or '')}`",
                    f"- Branche de base : `{code(target.get('base_branch') or '')}`",
                    f"- Labels : {labels}",
                    f"- Milestone : `{code(target.get('milestone_title') or '')}`",
                    f"- Board : `{code(target.get('board_title') or '')}`",
                    "",
                ]
            )
        lines.extend(
            [
                "## SPEC",
                # SPEC clôturé dans un bloc de code : son Markdown (titres, cases) est
                # inerte → impossible de forger un faux bandeau « approuvé » dans l'aperçu.
                "```",
                (self.spec or "(aucun SPEC)").replace("```", "ʼʼʼ"),
                "```",
                "",
                "## Tâches",
            ]
        )
        if not self.tasks:
            lines.append("_(aucune tâche)_")
        for task in self.tasks:
            deps = ", ".join(f"#{d}" for d in (task.get("depends_on") or []))
            suffix = f" — dépend de {deps}" if deps else ""
            lines.append(f"- [{task['id']}] {inline(task['title'])}{suffix}")
            acceptance = inline(task.get("acceptance") or "")
            if acceptance:
                lines.append(f"  - critère : {acceptance}")
            artifact_sha = inline(task.get("acceptance_test_sha256") or "")
            artifact_source = task.get("acceptance_test_source") or ""
            provenance = task.get("acceptance_test_provenance") or {}
            if artifact_sha:
                role = inline(str(provenance.get("role", ""))) if isinstance(provenance, dict) else ""
                role_label = f" · rôle `{role}`" if role else ""
                lines.append(f"  - oracle QA : `sha256:{artifact_sha}`{role_label}")
                lines.extend(
                    [
                        "",
                        "  ```python",
                        *[f"  {line}" for line in artifact_source.replace("```", "ʼʼʼ").splitlines()],
                        "  ```",
                    ]
                )
        return "\n".join(lines)


def _deadline_value(value: Any) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, datetime):
        raise ValueError("Deadline du plan invalide : datetime attendu.")
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _plan_hash(project: Any, tasks: List[Any]) -> str:
    """Empreinte stable du plan (SPEC + tâches) — base de l'anti-TOCTOU."""
    payload = {
        "spec": getattr(project, "spec", None) or "",
        "acceptance_tests_required": bool(getattr(project, "acceptance_tests_required", False)),
        "tasks": sorted(
            (
                {
                    "id": t.id,
                    "title": t.title,
                    "acceptance": t.acceptance,
                    "depends_on": t.depends_on or [],
                    # L'approbation couvre l'oracle exact ET ses métadonnées. Le
                    # SHA seul ne suffit pas : une mutation coordonnée
                    # source+digest, ou un changement de provenance, doit aussi
                    # invalider le plan approuvé.
                    "acceptance_test_source": getattr(t, "acceptance_test_source", None),
                    "acceptance_test_sha256": getattr(t, "acceptance_test_sha256", None),
                    "acceptance_test_provenance": getattr(t, "acceptance_test_provenance", None),
                }
                for t in tasks
            ),
            key=lambda d: d["id"],
        ),
    }
    # Compatibilité avec les plans historiques : l'absence de cible conserve
    # exactement l'ancien payload et donc les empreintes déjà approuvées.
    plan_sync_config = getattr(project, "plan_sync_config", None)
    if plan_sync_config is not None:
        payload["plan_sync_config"] = plan_sync_config
    # Comme la cible, la deadline n'était pas présente dans les plans
    # historiques : ne l'ajouter que lorsqu'elle existe préserve leurs hashes.
    deadline = _deadline_value(getattr(project, "deadline", None))
    if deadline is not None:
        payload["deadline"] = deadline
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def current_plan_hash(manager: Any, project_id: int) -> Optional[str]:
    """Retourne l'empreinte du plan courant, ou ``None`` si le projet est absent."""
    snapshot = load_plan_snapshot(manager, project_id)
    return snapshot.plan_hash if snapshot is not None else None


def load_plan_snapshot(
    manager: Any,
    project_id: int,
    *,
    require_approval: bool = False,
    require_target: bool = False,
) -> Optional[PlanStateSnapshot]:
    """Charge projet+tâches dans une transaction verrouillée puis les détache.

    Le verrou projet sérialise approbation/réapprobation et mutations de ses
    champs ; les verrous tâches couvrent le DAG. Les appels GitHub consomment
    ensuite uniquement cette copie, jamais une série de lectures indépendantes.
    """
    with manager.session() as session:
        project = session.scalar(select(Project).where(Project.id == project_id).with_for_update())
        if project is None:
            if require_approval:
                raise PlanNotApproved(f"Projet {project_id} introuvable : approbation impossible.")
            return None
        rows = list(
            session.scalars(select(Task).where(Task.project_id == project_id).order_by(Task.id).with_for_update())
        )
        tasks = tuple(
            PlanTaskSnapshot(
                id=row.id,
                title=row.title,
                acceptance=row.acceptance,
                depends_on=tuple(deepcopy(row.depends_on or [])),
                acceptance_test_source=row.acceptance_test_source,
                acceptance_test_sha256=row.acceptance_test_sha256,
                acceptance_test_provenance=deepcopy(row.acceptance_test_provenance),
                issue_number=row.issue_number,
            )
            for row in rows
        )
        target = deepcopy(project.plan_sync_config)
        if require_target:
            normalized = normalize_plan_sync_config(target)
            if normalized != target:
                raise PlanTargetError("La cible GitHub persistée n'est pas canonique.")
        plan_hash = _plan_hash(project, list(tasks))
        approved = (
            bool(project.approved_plan_hash)
            and project.status == PROJECT_STATUS_APPROVED
            and project.approved_plan_hash == plan_hash
        )
        if require_approval and not approved:
            raise PlanNotApproved(
                f"Projet {project_id} non approuvé (ou modifié depuis l'approbation) : "
                "validation humaine requise avant écriture GitHub."
            )
        return PlanStateSnapshot(
            project_id=project.id,
            name=project.name,
            status=project.status,
            spec=project.spec,
            deadline=project.deadline,
            acceptance_tests_required=bool(project.acceptance_tests_required),
            _plan_sync_config_json=(
                json.dumps(target, sort_keys=True, ensure_ascii=False) if target is not None else None
            ),
            approved_plan_hash=project.approved_plan_hash,
            tasks=tasks,
            plan_hash=plan_hash,
            approved=approved,
        )


def build_plan_preview(manager: Any, project_id: int) -> Optional[PlanPreview]:
    """Construit l'aperçu d'un plan depuis le state store, ou ``None`` si absent."""
    snapshot = load_plan_snapshot(manager, project_id)
    if snapshot is None:
        return None
    tasks = [
        {
            "id": task.id,
            "title": task.title,
            "acceptance": task.acceptance,
            "depends_on": task.depends_on or [],
            "acceptance_test_source": task.acceptance_test_source,
            "acceptance_test_sha256": task.acceptance_test_sha256,
            "acceptance_test_provenance": task.acceptance_test_provenance,
        }
        for task in snapshot.tasks
    ]
    return PlanPreview(
        project_id=project_id,
        name=snapshot.name,
        status=snapshot.status,
        approved=snapshot.approved,
        spec=snapshot.spec,
        tasks=tasks,
        task_count=len(tasks),
        acceptance_tests_required=snapshot.acceptance_tests_required,
        plan_sync_config=snapshot.plan_sync_config,
        deadline=snapshot.deadline,
        plan_hash=snapshot.plan_hash,
    )


def is_approved(manager: Any, project_id: int) -> bool:
    """True si le plan est approuvé **et inchangé** depuis l'approbation (anti-TOCTOU)."""
    snapshot = load_plan_snapshot(manager, project_id)
    return bool(snapshot and snapshot.approved)


def _validate_acceptance_artifacts(tasks: List[Any], *, required: bool) -> None:
    """Valide l'intégrité minimale des oracles QA avant approbation.

    Quand §4.7 est activé, toute tâche doit posséder un triplet complet. Même
    lorsqu'il est désactivé, un triplet partiel ou un digest faux est refusé :
    on ne scelle jamais silencieusement un artefact incohérent.
    """
    for task in tasks:
        source = getattr(task, "acceptance_test_source", None)
        digest = getattr(task, "acceptance_test_sha256", None)
        provenance = getattr(task, "acceptance_test_provenance", None)
        present = (source is not None, digest is not None, provenance is not None)
        if not any(present):
            if required:
                raise ValueError(f"Oracle QA manquant pour la tâche {task.id}.")
            continue
        if not all(present):
            raise ValueError(f"Oracle QA partiel pour la tâche {task.id}.")
        if not isinstance(source, str) or not source.strip():
            raise ValueError(f"Source de l'oracle QA vide pour la tâche {task.id}.")
        expected = hashlib.sha256(source.encode("utf-8")).hexdigest()
        if digest != expected:
            raise ValueError(f"SHA-256 de l'oracle QA invalide pour la tâche {task.id}.")
        if not isinstance(provenance, dict):
            raise ValueError(f"Provenance de l'oracle QA invalide pour la tâche {task.id}.")


def approve_plan(
    manager: Any,
    project_id: int,
    *,
    actor: str = "human",
    require_acceptance_artifacts: bool = False,
    expected_plan_hash: Optional[str] = None,
    require_target: bool = False,
) -> bool:
    """Approuve le plan (lie l'approbation à son contenu). Retourne False si projet absent.

    Préconditions : le projet a un SPEC et au moins une tâche. Idempotent : ré-approuver
    le même plan ne fait rien. Lève ``ValueError`` si le plan est incomplet.
    """
    # Une seule transaction verrouille le contrat relu (projet + tâches), le
    # recalcule puis persiste statut, hash et décision. Aucun snapshot lu avant
    # le verrou ne peut donc être approuvé par erreur (anti-TOCTOU).
    with manager.session() as session:
        project = session.scalar(select(Project).where(Project.id == project_id).with_for_update())
        if project is None:
            return False
        tasks = list(
            session.scalars(select(Task).where(Task.project_id == project_id).order_by(Task.id).with_for_update())
        )
        if not tasks or not (project.spec or "").strip():
            raise ValueError("Plan incomplet (SPEC ou tâches manquants) : rien à approuver.")
        if require_target and project.plan_sync_config is None:
            raise PlanTargetError(
                f"Projet {project_id} sans cible GitHub persistée : recréer un draft avec le nouveau flux."
            )
        if project.plan_sync_config is not None:
            normalized_target = normalize_plan_sync_config(project.plan_sync_config)
            if normalized_target != project.plan_sync_config:
                raise PlanTargetError(
                    "La cible GitHub persistée n'est pas normalisée : le plan doit être régénéré avant approbation."
                )
        _validate_acceptance_artifacts(
            tasks,
            required=require_acceptance_artifacts or bool(getattr(project, "acceptance_tests_required", False)),
        )

        current_hash = _plan_hash(project, tasks)
        if expected_plan_hash is not None and expected_plan_hash != current_hash:
            raise PlanHashMismatch(
                f"Le plan a changé depuis sa revue (attendu={expected_plan_hash}, courant={current_hash})."
            )
        if project.status == PROJECT_STATUS_APPROVED and project.approved_plan_hash == current_hash:
            return True  # idempotent : déjà approuvé pour CE plan exact
        if (
            project.approved_plan_hash
            and project.approved_plan_hash != current_hash
            and any(task.issue_number for task in tasks)
        ):
            raise ValueError(
                "Plan déjà matérialisé sur GitHub : une révision différente ne peut pas être "
                "réapprouvée tant que les liaisons d'issues existantes n'ont pas été réconciliées."
            )

        project.status = PROJECT_STATUS_APPROVED
        project.approved_plan_hash = current_hash
        session.add(
            Decision(
                project_id=project_id,
                summary="Plan approuvé (validation humaine)",
                rationale=f"actor={actor}; plan_hash={current_hash}",
            )
        )
    return True


def require_approved(manager: Any, project_id: int) -> None:
    """Lève :class:`PlanNotApproved` si le plan n'est pas approuvé/inchangé (garde pour P4)."""
    load_plan_snapshot(manager, project_id, require_approval=True)
