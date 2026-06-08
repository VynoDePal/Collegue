"""Normalisation de texte partagée (anti-injection Markdown).

Module **léger et sans dépendance** (seulement la stdlib) : il peut être importé
par n'importe quel sous-système sans tirer de dépendance lourde. C'est pourquoi
``inline`` vit ici plutôt que dans ``collegue.planner._parsing`` (dont le package
``planner`` charge l'état SQLAlchemy au moindre import de sous-module).
"""

from __future__ import annotations

import re
from typing import Any


def inline(text: Any) -> str:
    """Réduit une valeur à une seule ligne (anti-injection Markdown).

    Écrase tout enchaînement d'espaces/sauts de ligne en une espace : un champ
    issu d'une source non fiable (LLM, issue) ne peut donc pas démarrer une
    nouvelle ligne dans un document rendu (pas de fausse section ``## ...`` ni de
    case ``- [x] ...``).
    """
    return re.sub(r"\s+", " ", str(text)).strip()
