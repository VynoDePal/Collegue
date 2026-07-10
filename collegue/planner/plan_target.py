"""Contrat durable de la cible GitHub d'un plan.

La cible est figée avant la revue humaine puis incluse dans l'empreinte du plan.
Cette normalisation évite qu'un même plan approuvé soit ensuite synchronisé vers
un autre dépôt, milestone ou board à cause d'arguments CLI reconstruits au run.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import PurePosixPath
from typing import Any

PLAN_SYNC_CONFIG_KEYS = frozenset(
    {
        "owner",
        "repo",
        "labels",
        "milestone_title",
        "board_title",
        "spec_filename",
        "base_branch",
    }
)


class PlanTargetError(ValueError):
    """La configuration de synchronisation du plan est absente ou invalide."""


def _contains_control(value: str) -> bool:
    return any(not character.isprintable() for character in value)


def _repository_coordinate(config: Mapping[str, Any], key: str) -> str:
    value = config.get(key)
    if not isinstance(value, str) or not value.strip():
        raise PlanTargetError(f"La cible du plan exige une valeur non vide pour `{key}`.")
    value = value.strip()
    pattern = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?" if key == "owner" else r"[A-Za-z0-9._-]{1,100}"
    if re.fullmatch(pattern, value) is None or value in {".", ".."}:
        raise PlanTargetError(f"La cible du plan contient une valeur invalide pour `{key}`.")
    return value


def _optional_string(config: Mapping[str, Any], key: str) -> str | None:
    value = config.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise PlanTargetError(f"La cible du plan exige une chaîne pour `{key}`.")
    value = value.strip()
    if _contains_control(value):
        raise PlanTargetError(f"La cible du plan contient une valeur invalide pour `{key}`.")
    return value or None


def _base_branch(config: Mapping[str, Any]) -> str:
    value = config.get("base_branch", "main")
    if not isinstance(value, str) or not value.strip():
        raise PlanTargetError("La cible du plan exige une branche non vide pour `base_branch`.")
    value = value.strip()
    path = PurePosixPath(value)
    if (
        len(value) > 255
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", value) is None
        or value.endswith(("/", ".", ".lock"))
        or ".." in value
        or "//" in value
        or "@{" in value
        or any(part in {"", ".", ".."} or part.endswith(".lock") for part in path.parts)
    ):
        raise PlanTargetError("`base_branch` doit être un nom de branche Git sûr.")
    return value


def normalize_plan_sync_config(config: Mapping[str, Any]) -> dict[str, Any]:
    """Valide et canonicalise la cible GitHub scellée avec le plan.

    Les clés étrangères sont refusées afin qu'un paramètre futur ne puisse pas
    être silencieusement omis de l'empreinte approuvée. Les labels sont nettoyés
    et dédupliqués en conservant leur premier ordre d'apparition.
    """
    if not isinstance(config, Mapping):
        raise PlanTargetError("La cible du plan doit être un mapping.")

    foreign = set(config) - PLAN_SYNC_CONFIG_KEYS
    if foreign:
        names = ", ".join(sorted(str(key) for key in foreign))
        raise PlanTargetError(f"Clés inconnues dans la cible du plan : {names}.")

    raw_labels = config.get("labels", ["autonome"])
    if not isinstance(raw_labels, (list, tuple)):
        raise PlanTargetError("La cible du plan exige une liste de chaînes pour `labels`.")
    labels: list[str] = []
    seen: set[str] = set()
    for raw_label in raw_labels:
        if not isinstance(raw_label, str):
            raise PlanTargetError("La cible du plan exige uniquement des chaînes dans `labels`.")
        label = raw_label.strip()
        if not label or _contains_control(label):
            raise PlanTargetError("La cible du plan interdit les labels vides ou contenant des contrôles.")
        if len(label) > 50:
            raise PlanTargetError("Un label GitHub ne peut pas dépasser 50 caractères.")
        if label not in seen:
            labels.append(label)
            seen.add(label)

    raw_spec_filename = config.get("spec_filename", "SPEC.md")
    if not isinstance(raw_spec_filename, str) or not raw_spec_filename.strip():
        raise PlanTargetError("La cible du plan exige une valeur non vide pour `spec_filename`.")
    spec_filename = raw_spec_filename.strip()
    path = PurePosixPath(spec_filename)
    if (
        path.is_absolute()
        or "\\" in spec_filename
        or _contains_control(spec_filename)
        or any(character.isspace() for character in spec_filename)
        or any(character in spec_filename for character in "`?#%")
        or ".." in path.parts
        or any(part in {"", "."} for part in spec_filename.split("/"))
    ):
        raise PlanTargetError("`spec_filename` doit être un chemin relatif sûr dans le dépôt.")

    return {
        "owner": _repository_coordinate(config, "owner"),
        "repo": _repository_coordinate(config, "repo"),
        "labels": labels,
        "milestone_title": _optional_string(config, "milestone_title"),
        "board_title": _optional_string(config, "board_title"),
        "spec_filename": spec_filename,
        "base_branch": _base_branch(config),
    }


__all__ = ["PLAN_SYNC_CONFIG_KEYS", "PlanTargetError", "normalize_plan_sync_config"]
