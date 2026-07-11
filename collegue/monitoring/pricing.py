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

Le collecteur conserve son tarif global pour les outils historiques. Le chemin
planner/QA comptabilisé fournit désormais un ``cost_usd`` explicite calculé avec
le provider et le modèle réellement capturés. Sous ``MAX_COST_USD``, un modèle
remote absent de la grille est refusé avant l'appel plutôt que valorisé au repli.
"""

from __future__ import annotations

from typing import Dict, Optional, Tuple

# (input_usd_per_token, output_usd_per_token)
_PER_1M = 1_000_000.0

# Providers locaux : exécution sur la machine → coût nul.
_LOCAL_PROVIDERS = ("lmstudio", "ollama", "unsloth")

# Modèles distants à coût nul, liés à leur provider. Contrairement aux tarifs
# payants historiques, un zéro ne doit jamais être réutilisé pour un endpoint
# homonyme d'un autre provider ni pour un suffixe de modèle inconnu.
_FREE_REMOTE_MODELS = {"gemini": frozenset({"gemma-4-31b-it", "gemma-4-26b-a4b-it"})}

# Grille par modèle, exprimée en USD / 1M tokens pour rester lisible,
# convertie en USD / token plus bas.
_MODEL_PRICES_PER_1M: Dict[str, Tuple[float, float]] = {
    # Gemma 4 via Gemini API — disponible uniquement sur le Free Tier : le coût
    # API est explicitement nul. Ces zéros sont autoritaires, contrairement au
    # repli d'un modèle distant inconnu.
    "gemma-4-31b-it": (0.0, 0.0),
    "gemma-4-26b-a4b-it": (0.0, 0.0),
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

# Repli historique quand le modèle configuré n'est pas dans la table,
# volontairement bas pour ne pas surévaluer le coût. Ce repli n'est jamais
# autoritaire : :func:`has_explicit_pricing` reste faux pour un modèle inconnu.
_DEFAULT_PER_1M: Tuple[float, float] = (0.15, 0.60)


def _normalize(model: str) -> str:
    """Réduit un nom de modèle à sa clé de tarif (sans préfixe ``models/`` ni suffixe de date)."""
    name = (model or "").strip().lower()
    if name.startswith("models/"):
        name = name[len("models/") :]
    return name


def _free_remote_model_is_valid(key: str, provider: Optional[str]) -> bool:
    provider_key = (provider or "gemini").strip().lower()
    return key in _FREE_REMOTE_MODELS.get(provider_key, ())


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
    if prices == (0.0, 0.0) and not _free_remote_model_is_valid(key, provider):
        prices = None
    if prices is None:
        # Correspondance par préfixe pour absorber les suffixes de version/date
        # (``gpt-5.4-mini-2026-01`` → ``gpt-5.4-mini``). On teste les clés de la
        # plus longue à la plus courte et seulement sur une frontière ``-``, pour
        # éviter qu'une clé courte (``gpt-5.4``) ne masque une plus spécifique
        # (``gpt-5.4-mini``) et surfacture.
        for known in sorted(_MODEL_PRICES_PER_1M, key=len, reverse=True):
            if _MODEL_PRICES_PER_1M[known] == (0.0, 0.0):
                continue
            if key == known or key.startswith(known + "-"):
                prices = _MODEL_PRICES_PER_1M[known]
                break
    if prices is None:
        prices = _DEFAULT_PER_1M
    return prices[0] / _PER_1M, prices[1] / _PER_1M


def has_explicit_pricing(model: str, provider: Optional[str] = None) -> bool:
    """Vrai si le coût est autoritaire (provider local ou modèle dans la grille)."""
    if provider and provider.strip().lower() in _LOCAL_PROVIDERS:
        return True
    key = _normalize(model)
    for known, prices in _MODEL_PRICES_PER_1M.items():
        if prices == (0.0, 0.0):
            if key == known and _free_remote_model_is_valid(key, provider):
                return True
            continue
        if key == known or key.startswith(known + "-"):
            return True
    return False


def is_explicitly_free(model: str, provider: Optional[str] = None) -> bool:
    """Vrai seulement quand la grille autoritaire fixe les deux prix à zéro.

    Le double contrôle est essentiel : :func:`cost_per_token` retourne aussi un
    tarif de repli pour les modèles distants inconnus, qui ne doivent jamais être
    interprétés comme gratuits ni contourner les garde-fous de budget.
    """
    return has_explicit_pricing(model, provider=provider) and cost_per_token(model, provider=provider) == (0.0, 0.0)
