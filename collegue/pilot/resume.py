"""Reprise après crash : ancrage du début de run (H5, epic #391, Phase 5).

Pour qu'un run de **plusieurs jours** reprenne sans perte d'état, la deadline
budget-temps doit être **absolue** : ``started_at + deadline_seconds``. Or à chaque
reprise, le pilote reconstruit un :class:`BudgetTimeController` avec ``started_at =
maintenant`` → la deadline **glisse** d'autant à chaque redémarrage et ne se
déclenche jamais.

Ce module persiste le ``started_at`` du run (métrique ``run_started_epoch``, écrite
une seule fois) et le relit à la reprise. Le runtime (F4) reconstruit alors le
contrôleur depuis cette valeur d'origine, et la deadline reste fixe quel que soit le
nombre de reprises.

Portée : seule la **deadline** (durée mur) est ancrée ici. Le plafond **$/tokens**
(C4) survit déjà aux redémarrages via le ``metrics.json`` du ``MetricsCollector`` —
à condition d'un ``COLLEGUE_HOME`` **absolu et stable** entre les processus (sinon le
cumul repart de zéro). À documenter côté exploitation pour les runs longs.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

# Métrique d'état stockant le début (epoch UTC) du run. Écrite une seule fois.
METRIC_RUN_STARTED_EPOCH = "run_started_epoch"


def _aware_utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def load_run_start(manager: object, project_id: int) -> Optional[datetime]:
    """``started_at`` **d'origine** du run (aware UTC), ou ``None`` si jamais persisté.

    ``get_metrics`` ordonne par ``id`` croissant : la **1re** ligne ``run_started_epoch``
    est le début le plus ancien (le vrai départ du run), pas une reprise.
    """
    for metric in manager.get_metrics(project_id, name=METRIC_RUN_STARTED_EPOCH):
        return datetime.fromtimestamp(metric.value, tz=timezone.utc)
    return None


def persist_run_start(manager: object, project_id: int, started_at: datetime) -> datetime:
    """Persiste le début de run, **idempotent** (n'écrase jamais une valeur existante).

    Renvoie le ``started_at`` **effectif** : la valeur d'origine si déjà présente
    (reprise), sinon ``started_at`` qu'on vient d'ancrer. Ainsi un appelant peut
    toujours connaître le vrai départ du run.
    """
    existing = load_run_start(manager, project_id)
    if existing is not None:
        return existing
    aware = _aware_utc(started_at)
    manager.add_metric(project_id, METRIC_RUN_STARTED_EPOCH, aware.timestamp())
    return aware
