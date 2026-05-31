"""Tarifs LLM par modèle pour le calcul de coût du dashboard.

Coûts en USD par token (et non par million), pour pouvoir multiplier
directement par un nombre de tokens. Source : grille publique Google
Gemini Developer API (https://ai.google.dev/gemini-api/docs/pricing),
relevée en mai 2026.

Les tarifs ``input``/``output`` sont des tarifs standard (tier payant,
prompts ≤ 200k pour les modèles Pro). Les variantes audio / cache /
grands prompts ne sont pas distinguées : l'objectif est une estimation
de coût lisible sur le dashboard, pas une facturation exacte.
"""

from __future__ import annotations

from typing import Dict, Tuple

# (input_usd_per_token, output_usd_per_token)
_PER_1M = 1_000_000.0

# Grille par modèle, exprimée en USD / 1M tokens pour rester lisible,
# convertie en USD / token plus bas.
_MODEL_PRICES_PER_1M: Dict[str, Tuple[float, float]] = {
    # Gemini 3.x
    "gemini-3.5-flash": (1.50, 9.00),
    "gemini-3-flash-preview": (1.50, 9.00),
    "gemini-3.1-flash-lite": (0.25, 1.50),
    "gemini-3.1-pro-preview": (2.00, 12.00),
    # Gemini 2.5
    "gemini-2.5-flash": (0.30, 2.50),
    "gemini-2.5-flash-lite": (0.10, 0.40),
    "gemini-2.5-pro": (1.25, 10.00),
}

# Repli quand le modèle configuré n'est pas dans la table : on reprend les
# anciens défauts du collecteur (Gemma 4 via Gemini API), volontairement bas
# pour ne pas surévaluer le coût.
_DEFAULT_PER_1M: Tuple[float, float] = (0.15, 0.60)


def _normalize(model: str) -> str:
    """Réduit un nom de modèle à sa clé de tarif (sans préfixe ``models/`` ni suffixe de date)."""
    name = (model or "").strip().lower()
    if name.startswith("models/"):
        name = name[len("models/") :]
    return name


def cost_per_token(model: str) -> Tuple[float, float]:
    """Retourne ``(input_usd_per_token, output_usd_per_token)`` pour un modèle.

    Tente une correspondance exacte, puis par préfixe (``gemini-3.5-flash-05-2026``
    → ``gemini-3.5-flash``), et retombe sur un tarif par défaut sinon.
    """
    key = _normalize(model)
    prices = _MODEL_PRICES_PER_1M.get(key)
    if prices is None:
        # Correspondance par préfixe pour absorber les suffixes de version/date.
        for known, known_prices in _MODEL_PRICES_PER_1M.items():
            if key.startswith(known):
                prices = known_prices
                break
    if prices is None:
        prices = _DEFAULT_PER_1M
    return prices[0] / _PER_1M, prices[1] / _PER_1M
