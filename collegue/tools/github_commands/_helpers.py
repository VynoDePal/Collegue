"""Helpers partagés des commandes GitHub de planification (P3, #354).

- ``validate_ref`` : valide owner/repo avant interpolation dans un chemin d'URL
  (défense en profondeur contre l'injection de chemin / ``..``).
- ``paginate`` : suit toutes les pages d'un endpoint de liste (sinon ``ensure_*``
  raterait un élément au-delà de la 1re page de 100 et créerait un doublon).
"""

from __future__ import annotations

import re
from typing import Any, Callable, Dict, List, Optional

from ..base import ToolExecutionError

# Règles de nommage GitHub : alphanum, tiret, underscore, point. Pas de '/' ni '..'.
_REF_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def validate_ref(value: str, name: str) -> None:
    """Lève ``ToolExecutionError`` si ``value`` n'est pas un nom GitHub valide."""
    if not value or not _REF_RE.match(value):
        raise ToolExecutionError(f"{name} GitHub invalide: {value!r}")


def paginate(
    api_get: Callable[[str, Optional[Dict[str, Any]]], Any],
    endpoint: str,
    params: Optional[Dict[str, Any]] = None,
    *,
    max_pages: int = 50,
) -> List[Any]:
    """Récupère TOUTES les pages (per_page=100) jusqu'à une page incomplète.

    ``max_pages`` borne la boucle (garde-fou contre une pagination infinie).
    """
    base = dict(params or {})
    results: List[Any] = []
    for page in range(1, max_pages + 1):
        batch = api_get(endpoint, {**base, "per_page": 100, "page": page}) or []
        results.extend(batch)
        if len(batch) < 100:
            break
    return results
