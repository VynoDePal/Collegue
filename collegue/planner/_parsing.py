"""Utilitaires de parsing partagés par le planificateur (P1/P2).

Source unique pour extraire un objet JSON d'une sortie LLM (souvent entourée de
prose ou de blocs ```json). Évite la duplication entre ``spec_generator`` et
``decomposer`` (deux implémentations divergeraient).
"""

from __future__ import annotations

import json
from typing import Optional


def json_from_text(text: str) -> Optional[dict]:
    """Extrait le premier objet JSON exploitable d'un texte (fallback structured-output).

    Scan brace-balanced via ``raw_decode`` depuis chaque ``{`` : on retourne le
    PREMIER objet complet qui décode (gère les blocs multiples, les ```json fences
    et la prose qui suit, contrairement à un ``{.*}`` glouton).
    """
    if not text:
        return None
    cleaned = text.strip()
    try:
        whole = json.loads(cleaned)
        if isinstance(whole, dict):
            return whole
    except (ValueError, TypeError):
        pass
    decoder = json.JSONDecoder()
    for idx, ch in enumerate(cleaned):
        if ch != "{":
            continue
        try:
            obj, _ = decoder.raw_decode(cleaned[idx:])
        except ValueError:
            continue
        if isinstance(obj, dict):
            return obj
    return None
