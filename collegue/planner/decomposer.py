"""Décomposition d'un SPEC en graphe de tâches (P2, #353).

Transforme le `SPEC.md` (P1) en tâches exécutables (titre, critère d'acceptation,
dépendances), valide le graphe (dépendances dans les bornes, **acyclique**) et les
persiste dans le state store (C6/C7) en résolvant les dépendances (référencées par
**index** côté LLM) en vrais IDs de tâches. Journalise le découpage (Decision).

Frontière de confiance LLM : on **valide** le graphe avant d'écrire (un graphe
cyclique ou des index hors bornes sont rejetés, pas persistés). Module **isolé**.
"""

from __future__ import annotations

from collections import deque
from typing import Any, List, Optional

from pydantic import BaseModel, Field, ValidationError

from collegue.core.llm import LLMRole, model_preferences_for_role
from collegue.core.llm.client import accounted_sample
from collegue.planner._parsing import json_from_text
from collegue.state.models import Decision, Task

# Plafond dur sur le nombre de tâches d'une décomposition : une décomposition
# hallucinée ne doit pas créer des milliers de lignes (et autant d'issues en P4).
MAX_TASKS = 200


class DecompositionCardinalityError(ValueError):
    """Le LLM n'a pas respecté une cardinalité explicitement scellée.

    Cette erreur est distincte des sorties momentanément vides/malformées : le
    runtime ne doit pas dépenser plusieurs appels LLM en retentant un plan qui
    viole une contrainte déterministe du caller.
    """


DECOMPOSE_SYSTEM_PROMPT = """Tu es un planificateur logiciel senior. À partir d'un SPEC de projet, \
tu le découpes en tâches exécutables et ordonnées.

Réponds UNIQUEMENT par un objet JSON valide, sans texte autour :
{"tasks": [{"title": "...", "acceptance": "...", "depends_on": [indices]}]}

Règles :
- "title": tâche concrète et atomique (string).
- "acceptance": critère d'acceptation TESTABLE de la tâche (string).
- "depends_on": liste d'INDEX (0-based) des AUTRES tâches de CETTE liste dont celle-ci dépend.
- Le graphe de dépendances doit être ACYCLIQUE (pas de dépendance circulaire).
- Une tâche ne dépend pas d'elle-même ; les index référencent des tâches existantes de la liste.
- Au moins une tâche."""


class _PlannedTask(BaseModel):
    """Tâche telle que proposée par le LLM (dépendances par index, pas par ID DB)."""

    title: str
    acceptance: str = ""
    depends_on: List[int] = Field(default_factory=list)


class _Decomposition(BaseModel):
    tasks: List[_PlannedTask] = Field(default_factory=list)


def _system_prompt(*, exact_task_count: Optional[int] = None) -> str:
    if exact_task_count is None:
        return DECOMPOSE_SYSTEM_PROMPT
    return (
        DECOMPOSE_SYSTEM_PROMPT
        + f"\n- Contrainte prioritaire du caller : retourne EXACTEMENT {exact_task_count} tâche(s) ; "
        "regroupe l'implémentation et ses tests dans la même tâche."
    )


def _coerce(data: dict) -> _Decomposition:
    try:
        return _Decomposition.model_validate(data)
    except ValidationError as exc:
        raise ValueError(f"Décomposition LLM invalide (champs manquants/typés): {exc}") from exc


def _extract_decomposition(result: Any) -> _Decomposition:
    candidate = getattr(result, "result", None)
    if isinstance(candidate, _Decomposition):
        return candidate
    if isinstance(candidate, BaseModel):
        candidate = candidate.model_dump()
    if isinstance(candidate, str):
        candidate = json_from_text(candidate)
    if isinstance(candidate, dict):
        return _coerce(candidate)
    data = json_from_text(getattr(result, "text", "") or "")
    if data is None:
        raise ValueError("Le planificateur n'a pas retourné de décomposition exploitable (ni structuré ni JSON).")
    return _coerce(data)


def _topo_order(tasks: List[_PlannedTask]) -> List[int]:
    """Ordre topologique des index ; lève ``ValueError`` si index invalide ou cycle."""
    n = len(tasks)
    indegree = [0] * n
    adjacency: List[List[int]] = [[] for _ in range(n)]
    for i, task in enumerate(tasks):
        for dep in dict.fromkeys(task.depends_on):  # dédupe en gardant l'ordre
            if not (0 <= dep < n):
                raise ValueError(f"tâche #{i}: dépendance index {dep} hors limites (0..{n - 1}).")
            if dep == i:
                raise ValueError(f"tâche #{i} ne peut pas dépendre d'elle-même.")
            adjacency[dep].append(i)
            indegree[i] += 1
    queue = deque(i for i in range(n) if indegree[i] == 0)
    order: List[int] = []
    while queue:
        node = queue.popleft()
        order.append(node)
        for nxt in adjacency[node]:
            indegree[nxt] -= 1
            if indegree[nxt] == 0:
                queue.append(nxt)
    if len(order) != n:
        raise ValueError("graphe de tâches cyclique : dépendances circulaires détectées.")
    return order


def _build_prompt(spec: Any, *, exact_task_count: Optional[int] = None) -> str:
    spec_text = spec.to_markdown() if hasattr(spec, "to_markdown") else str(spec)
    constraint = ""
    if exact_task_count is not None:
        constraint = (
            f"\n\nCONTRAINTE FERME : retourne exactement {exact_task_count} tâche(s). "
            "Une tâche doit couvrir ensemble l'implémentation et ses tests ; "
            "ne les scinde pas en tâches distinctes."
        )
    return (
        f"SPEC du projet :\n{spec_text}\n\n"
        f"Découpe ce SPEC en tâches (JSON décrit par tes instructions système).{constraint}"
    )


async def decompose(
    spec: Any,
    ctx: Any,
    *,
    manager: Any,
    project_id: int,
    settings_obj: Optional[object] = None,
    temperature: float = 0.2,
    max_tokens: int = 4096,
    exact_task_count: Optional[int] = None,
) -> List[Any]:
    """Décompose ``spec`` en tâches persistées pour ``project_id`` ; retourne les tâches créées.

    ``spec`` peut être un :class:`~collegue.planner.spec_generator.Spec` ou le SPEC.md
    (texte). Lève ``ValueError`` si la décomposition est vide/trop grande, non
    exploitable, si le projet a déjà des tâches (idempotence), ou si le graphe de
    dépendances est invalide (index hors bornes, auto-dépendance, cycle).

    Persistance **atomique** : toutes les tâches + la décision sont écrites dans une
    **seule** transaction — un échec en cours de route ne laisse aucun graphe partiel.
    """
    if exact_task_count is not None and (
        not isinstance(exact_task_count, int)
        or isinstance(exact_task_count, bool)
        or not 1 <= exact_task_count <= MAX_TASKS
    ):
        raise ValueError(f"exact_task_count doit être un entier dans [1, {MAX_TASKS}].")

    # Idempotence : ne pas ré-décomposer (un retry dupliquerait le graphe → des
    # issues en double en P4). Le caller doit vider les tâches pour re-planifier.
    if manager.get_tasks(project_id):
        raise ValueError("projet déjà décomposé : vider les tâches existantes avant de redécomposer.")

    sample_kwargs = {
        "messages": _build_prompt(spec, exact_task_count=exact_task_count),
        "system_prompt": _system_prompt(exact_task_count=exact_task_count),
        "result_type": _Decomposition,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    prefs = model_preferences_for_role(LLMRole.PLANNER, settings_obj)
    if prefs:
        sample_kwargs["model_preferences"] = prefs

    result = await accounted_sample(
        ctx,
        role=LLMRole.PLANNER,
        operation="planner.decompose",
        settings_obj=settings_obj,
        **sample_kwargs,
    )
    planned = _extract_decomposition(result).tasks
    if not planned:
        raise ValueError("Décomposition vide : aucune tâche produite.")
    if len(planned) > MAX_TASKS:
        raise ValueError(f"Décomposition trop grande ({len(planned)} tâches > MAX_TASKS={MAX_TASKS}).")
    if exact_task_count is not None and len(planned) != exact_task_count:
        raise DecompositionCardinalityError(
            "Décomposition refusée avant persistance : "
            f"{len(planned)} tâche(s) produite(s), exactement {exact_task_count} attendue(s)."
        )

    order = _topo_order(planned)  # valide le graphe (bornes + acyclique) AVANT toute écriture

    # Tout dans une seule transaction (atomique : tout ou rien).
    with manager.session() as session:
        index_to_id: dict[int, int] = {}
        for idx in order:
            task = planned[idx]
            dep_ids = [index_to_id[d] for d in dict.fromkeys(task.depends_on)]
            row = Task(
                project_id=project_id,
                title=task.title,
                acceptance=(task.acceptance or None),
                depends_on=dep_ids,  # toujours une liste (jamais None) pour les consommateurs
            )
            session.add(row)
            session.flush()  # obtient row.id pour résoudre les dépendances suivantes
            index_to_id[idx] = row.id
        session.add(
            Decision(
                project_id=project_id,
                summary=f"Décomposition du SPEC en {len(planned)} tâches",
                rationale="Graphe de dépendances validé (acyclique) ; dépendances résolues en IDs de tâches.",
            )
        )

    return manager.get_tasks(project_id)
