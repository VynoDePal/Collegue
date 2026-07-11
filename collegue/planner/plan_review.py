"""Gate de validation humaine du plan (P5, #356).

Garde-fou du brief (§Phase 1) : le plan **n'avance pas** sans accord humain.
``build_plan_preview`` agrège un résumé lisible (SPEC + tâches + critères
d'acceptation + dépendances ; les hypothèses sont déjà dans le SPEC, P1) à partir
du state store (via ``load_snapshot`` de C7). ``approve_plan`` lie l'approbation au
**contenu exact** du plan (empreinte SHA-256 persistée) ; ``is_approved`` recompute
l'empreinte et la compare → toute **mutation du plan après approbation invalide le
gate** (anti-TOCTOU). ``require_approved`` lève si non approuvé.

Contrat P4 (à respecter impérativement) : la couche d'écriture GitHub DOIT appeler
``require_approved`` avant toute écriture ; sans cela le garde-fou est contournable.

Module **isolé** : non câblé au runtime tant que le pilote (Phase 3) ne l'enchaîne pas.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from collegue.planner._parsing import inline
from collegue.planner.status import PROJECT_STATUS_APPROVED
from collegue.state import load_snapshot
from collegue.state.models import Decision, Project


class PlanNotApproved(Exception):
    """Le plan n'a pas été approuvé (ou a changé depuis) — P4 doit refuser d'écrire."""


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

    def to_markdown(self) -> str:
        lines = [
            f"# Plan : {inline(self.name)}",
            "",
            f"Statut : `{self.status}` · Tâches : {self.task_count} · "
            f"Approuvé : {'✅ oui' if self.approved else '❌ non'}",
            f"Oracles QA §4.7 : {'requis' if self.acceptance_tests_required else 'désactivés'}",
            "",
            "## SPEC",
            # SPEC clôturé dans un bloc de code : son Markdown (titres, cases) est
            # inerte → impossible de forger un faux bandeau « approuvé » dans l'aperçu.
            "```",
            (self.spec or "(aucun SPEC)").replace("```", "ʼʼʼ"),
            "```",
            "",
            "## Tâches",
        ]
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
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def build_plan_preview(manager: Any, project_id: int) -> Optional[PlanPreview]:
    """Construit l'aperçu d'un plan depuis le state store, ou ``None`` si absent."""
    snapshot = load_snapshot(manager, project_id)
    if snapshot is None:
        return None
    project = snapshot.project
    tasks = [
        {
            "id": t["id"],
            "title": t["title"],
            "acceptance": t.get("acceptance"),
            "depends_on": t.get("depends_on") or [],
            "acceptance_test_source": t.get("acceptance_test_source"),
            "acceptance_test_sha256": t.get("acceptance_test_sha256"),
            "acceptance_test_provenance": t.get("acceptance_test_provenance"),
        }
        for t in snapshot.tasks
    ]
    return PlanPreview(
        project_id=project_id,
        name=project.get("name", ""),
        status=project.get("status", ""),
        approved=is_approved(manager, project_id),
        spec=project.get("spec"),
        tasks=tasks,
        task_count=len(tasks),
        acceptance_tests_required=bool(project.get("acceptance_tests_required", False)),
    )


def is_approved(manager: Any, project_id: int) -> bool:
    """True si le plan est approuvé **et inchangé** depuis l'approbation (anti-TOCTOU)."""
    project = manager.get_project(project_id)
    if project is None or project.status != PROJECT_STATUS_APPROVED:
        return False
    tasks = manager.get_tasks(project_id)
    return bool(project.approved_plan_hash) and project.approved_plan_hash == _plan_hash(project, tasks)


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
) -> bool:
    """Approuve le plan (lie l'approbation à son contenu). Retourne False si projet absent.

    Préconditions : le projet a un SPEC et au moins une tâche. Idempotent : ré-approuver
    le même plan ne fait rien. Lève ``ValueError`` si le plan est incomplet.
    """
    project = manager.get_project(project_id)
    if project is None:
        return False
    tasks = manager.get_tasks(project_id)
    if not tasks or not (project.spec or "").strip():
        raise ValueError("Plan incomplet (SPEC ou tâches manquants) : rien à approuver.")
    _validate_acceptance_artifacts(
        tasks,
        required=require_acceptance_artifacts or bool(getattr(project, "acceptance_tests_required", False)),
    )

    current_hash = _plan_hash(project, tasks)
    if project.status == PROJECT_STATUS_APPROVED and project.approved_plan_hash == current_hash:
        return True  # idempotent : déjà approuvé pour CE plan exact

    # Atomique : statut + empreinte + décision dans une seule transaction.
    with manager.session() as session:
        row = session.get(Project, project_id)
        row.status = PROJECT_STATUS_APPROVED
        row.approved_plan_hash = current_hash
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
    if not is_approved(manager, project_id):
        raise PlanNotApproved(
            f"Projet {project_id} non approuvé (ou modifié depuis l'approbation) : "
            "validation humaine requise avant écriture GitHub."
        )
