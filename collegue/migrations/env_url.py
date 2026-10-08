"""Résolution de l'URL de base du store d'état (partagée par ``env.py`` et l'API Python)."""

from __future__ import annotations

import os
from typing import Optional


def resolve_url(explicit: Optional[str] = None, ini_url: Optional[str] = None) -> str:
    """URL de connexion : explicite > ``STATE_DATABASE_URL`` (env) > ``settings`` > ``sqlalchemy.url`` (ini).

    Erreur claire si aucune n'est définie.
    """
    url = explicit or os.getenv("STATE_DATABASE_URL")
    if not url:
        # except ImportError seulement : une ValidationError pydantic / un .env cassé doit remonter
        # (sinon on masque la vraie cause en « pas d'URL »).
        try:
            from collegue.config import settings

            url = settings.STATE_DATABASE_URL
        except ImportError:
            url = None
    if not url:
        url = ini_url
    if not url:
        raise RuntimeError(
            "Aucune URL de base : définissez STATE_DATABASE_URL (env) ou "
            "settings.STATE_DATABASE_URL avant de lancer les migrations."
        )
    return url
