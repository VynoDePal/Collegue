"""Utilitaires de parsing partagés par le planificateur (P1/P2).

Source unique pour extraire un objet JSON d'une sortie LLM (souvent entourée de
prose ou de blocs ```json). Évite la duplication entre ``spec_generator`` et
``decomposer`` (deux implémentations divergeraient).
"""

from __future__ import annotations

import json
import re
from typing import Optional

# ``inline`` vit dans un module léger et sans dépendance (importable hors planner,
# ex. par l'exécuteur Phase 2) ; on le ré-exporte ici pour ne pas changer la
# surface publique de ``_parsing`` (les modules du planner l'importent d'ici).
from collegue.textnorm import inline

__all__ = ["inline", "json_from_text"]


_LEADING_THOUGHT_RE = re.compile(r"\A<thought>.*?</thought>", re.DOTALL | re.IGNORECASE)
_THOUGHT_TAG_RE = re.compile(r"</?thought>", re.IGNORECASE)
_THOUGHT_MARKER_RE = re.compile(r"<\s*/?\s*thought\b", re.IGNORECASE)


def _without_leading_thought(text: str) -> Optional[str]:
    """Retire l'unique enveloppe de raisonnement Gemma, sinon échoue fermé.

    On n'active ce traitement que si le premier tag ``thought`` précède le
    premier objet JSON potentiel. Cela conserve notamment les chaînes JSON qui
    contiennent littéralement ``<thought>``. Dès qu'une enveloppe est détectée,
    elle doit en revanche être complète, unique et strictement en tête : aucun
    JSON de raisonnement ne doit pouvoir être pris pour la réponse finale.
    """

    markers = list(_THOUGHT_MARKER_RE.finditer(text))
    if not markers:
        return text

    first_brace = text.find("{")
    if first_brace >= 0 and first_brace < markers[0].start():
        return text

    tags = list(_THOUGHT_TAG_RE.finditer(text))
    thought = _LEADING_THOUGHT_RE.match(text)
    if (
        thought is None
        or len(markers) != 2
        or len(tags) != 2
        or tags[0].start() != 0
        or tags[0].group(0).lower() != "<thought>"
        or tags[1].group(0).lower() != "</thought>"
        or tags[1].end() != thought.end()
    ):
        return None

    final = text[thought.end() :].strip()
    return final or None


def json_from_text(text: str) -> Optional[dict]:
    """Extrait le premier objet JSON exploitable d'un texte (fallback structured-output).

    Une unique enveloppe Gemma ``<thought>...</thought>`` complète et placée
    strictement en tête est ignorée avant le scan. Les enveloppes ambiguës sont
    rejetées afin de ne jamais sélectionner un objet JSON issu du raisonnement.

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
    cleaned = _without_leading_thought(cleaned)
    if cleaned is None:
        return None
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
