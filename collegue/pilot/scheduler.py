"""Ordonnanceur de graphe de tâches (F1, epic #373, brief §7 Phase 3).

Sélectionne, dans le graphe de tâches **persisté**, les tâches **prêtes** à
exécuter : celles encore à faire (``todo``) dont **toutes** les dépendances sont
terminées. Calcul **pur** sur l'état (aucune exécution ici) ; le pilote (F3)
consomme la sélection.

Statuts (alignés Phase 2 ``todo``→``in_progress``→``in_review``) :
- **satisfaisants** (débloquent un dépendant) : ``in_review``, ``done``, ``merged``.
  Une PR ouverte (``in_review``) suffit à débloquer la suite — on n'attend pas le
  merge humain pour enchaîner la construction du MVP.
- **en cours** : ``in_progress`` (ni prête ni terminée).
- **prête** : ``todo`` avec toutes ses dépendances satisfaites.
- tout autre statut non satisfaisant et non actif (ex. ``failed``) bloque ses
  dépendants → peut mener à un **blocage** (cf. :func:`is_blocked`).

Module **isolé** : non câblé au runtime (F4 câblera le pilote).
"""

from __future__ import annotations

from typing import Dict, List, Optional, Sequence

# Statuts qui SATISFONT une dépendance (débloquent un dépendant).
SATISFIED_STATUSES = frozenset({"in_review", "done", "merged"})
# Statuts « en cours » : du travail progresse, pas un blocage.
ACTIVE_STATUSES = frozenset({"in_progress"})
# Statut d'une tâche pas encore démarrée.
PENDING_STATUS = "todo"


class SchedulerError(RuntimeError):
    """Graphe de tâches invalide (cycle, dépendance absente)."""


def _by_id(tasks: Sequence) -> Dict[int, object]:
    return {task.id: task for task in tasks}


def _deps(task) -> List[int]:
    return list(task.depends_on or [])


def _validate_graph(tasks: Sequence) -> None:
    """Lève :class:`SchedulerError` si une dépendance est absente ou si le graphe a un cycle."""
    by_id = _by_id(tasks)
    for task in tasks:
        for dep in _deps(task):
            if dep not in by_id:
                raise SchedulerError(f"la tâche {task.id} dépend d'un id absent du graphe: {dep}")

    # Détection de cycle par coloriage (DFS itératif → pas de limite de récursion ;
    # le graphe est borné par MAX_TASKS=200 côté planner, mais on reste prudent).
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {task.id: WHITE for task in tasks}
    for root in tasks:
        if color[root.id] != WHITE:
            continue
        stack = [(root.id, iter(_deps(root)))]
        color[root.id] = GRAY
        while stack:
            node, deps_iter = stack[-1]
            advanced = False
            for dep in deps_iter:
                if color[dep] == GRAY:
                    raise SchedulerError(f"cycle de dépendances détecté impliquant la tâche {dep}")
                if color[dep] == WHITE:
                    color[dep] = GRAY
                    stack.append((dep, iter(_deps(by_id[dep]))))
                    advanced = True
                    break
            if not advanced:
                color[node] = BLACK
                stack.pop()


def ready_tasks(tasks: Sequence) -> List:
    """Tâches prêtes (``todo`` + dépendances satisfaites), en ordre déterministe (par id).

    Les tâches prêtes sont à la « frontière » du graphe (dépendances déjà
    terminées) donc mutuellement indépendantes : trier par id est déterministe et
    cohérent avec un ordre topologique. Lève :class:`SchedulerError` si le graphe
    est invalide (cycle / dépendance absente).
    """
    _validate_graph(tasks)
    by_id = _by_id(tasks)
    ready = [
        task
        for task in tasks
        if task.status == PENDING_STATUS and all(by_id[dep].status in SATISFIED_STATUSES for dep in _deps(task))
    ]
    return sorted(ready, key=lambda task: task.id)


def next_task(tasks: Sequence) -> Optional[object]:
    """Prochaine tâche prête (la plus prioritaire), ou ``None`` s'il n'y en a pas."""
    ready = ready_tasks(tasks)
    return ready[0] if ready else None


def remaining_tasks(tasks: Sequence) -> List:
    """Tâches non terminées (statut hors :data:`SATISFIED_STATUSES`)."""
    return [task for task in tasks if task.status not in SATISFIED_STATUSES]


def is_blocked(tasks: Sequence) -> bool:
    """True si des tâches restent, mais aucune n'est prête **et** aucune n'est en cours.

    Signale un graphe « coincé » (p.ex. un dépendant d'une tâche ``failed``), pour
    que le pilote (F3) s'arrête au lieu de boucler. Lève :class:`SchedulerError`
    sur un graphe invalide (via :func:`ready_tasks`).
    """
    remaining = remaining_tasks(tasks)
    if not remaining:
        return False
    if ready_tasks(tasks):
        return False
    return not any(task.status in ACTIVE_STATUSES for task in remaining)
