"""Tarifs LLM par modèle pour le calcul de coût du dashboard.

Coûts en USD par token (et non par million), pour pouvoir multiplier
directement par un nombre de tokens. Sources des grilles publiques (relevées
2026) : Google Gemini (https://ai.google.dev/gemini-api/docs/pricing),
OpenAI (https://openai.com/api/pricing/), Anthropic
(https://www.anthropic.com/pricing).

Les tarifs ``input``/``output`` sont des tarifs standard (tier payant,
prompts ≤ 200k pour les modèles Pro). Les variantes audio / cache /
grands prompts ne sont pas distinguées : l'objectif est une estimation
de coût lisible sur le dashboard, pas une facturation exacte. Les providers
locaux (LM Studio, Ollama, Unsloth) sont gratuits → coût 0, mais uniquement si
``provider`` est passé à :func:`cost_per_token` (le nom du modèle seul ne suffit
pas à savoir qu'un modèle tourne en local).

Note (périmètre) : le ``MetricsCollector`` calcule le coût avec un tarif unique
résolu au démarrage depuis ``settings.LLM_MODEL``/``LLM_PROVIDER``. Le modèle
réellement utilisé par appel est capturé pour l'``activity_log`` (affichage),
mais le coût par-modèle-capturé n'est pas encore branché dans ``record_execution``
(amélioration ultérieure).
"""

from __future__ import annotations

from typing import Dict, Optional, Tuple

# (input_usd_per_token, output_usd_per_token)
_PER_1M = 1_000_000.0

# Providers locaux : exécution sur la machine → coût nul.
_LOCAL_PROVIDERS = ("lmstudio", "ollama", "unsloth")

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
    # OpenAI GPT-5.x
    "gpt-5.4": (2.50, 15.00),
    "gpt-5.4-mini": (0.75, 4.50),
    # Anthropic Claude 4.x
    "claude-opus-4": (5.00, 25.00),
    "claude-sonnet-4": (3.00, 15.00),
    "claude-haiku-4": (1.00, 5.00),
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


def cost_per_token(model: str, provider: Optional[str] = None) -> Tuple[float, float]:
    """Retourne ``(input_usd_per_token, output_usd_per_token)`` pour un modèle.

    Si ``provider`` est un provider local (LM Studio/Ollama/Unsloth), le coût est
    nul. Sinon : correspondance exacte sur le modèle, puis par préfixe
    (``gemini-3.5-flash-05-2026`` → ``gemini-3.5-flash``), puis tarif par défaut.
    """
    if provider and provider.strip().lower() in _LOCAL_PROVIDERS:
        return 0.0, 0.0
    key = _normalize(model)
    prices = _MODEL_PRICES_PER_1M.get(key)
    if prices is None:
        # Correspondance par préfixe pour absorber les suffixes de version/date
        # (``gpt-5.4-mini-2026-01`` → ``gpt-5.4-mini``). On teste les clés de la
        # plus longue à la plus courte et seulement sur une frontière ``-``, pour
        # éviter qu'une clé courte (``gpt-5.4``) ne masque une plus spécifique
        # (``gpt-5.4-mini``) et surfacture.
        for known in sorted(_MODEL_PRICES_PER_1M, key=len, reverse=True):
            if key == known or key.startswith(known + "-"):
                prices = _MODEL_PRICES_PER_1M[known]
                break
    if prices is None:
        prices = _DEFAULT_PER_1M
    return prices[0] / _PER_1M, prices[1] / _PER_1M
