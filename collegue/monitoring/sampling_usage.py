"""Capture des tokens réels renvoyés par le provider lors d'un ``ctx.sample()``.

FastMCP expose ``SamplingResult(text, result, history)`` sans le ``usage`` du
provider, et le ``OpenAISamplingHandler`` par défaut ne propage pas les tokens.
Le handler de sampling s'exécutant dans la même task asyncio que l'outil
appelant, on partage le dernier ``usage`` via un ``ContextVar`` : le handler y
écrit après chaque appel LLM, l'outil le lit juste après ``ctx.sample()``.
"""

from __future__ import annotations

import contextvars
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Optional, Tuple

# (prompt_tokens, completion_tokens, model) du dernier appel LLM dans cette task,
# ou None. Le modèle est celui réellement renvoyé par le provider.
_last_usage: contextvars.ContextVar[Optional[Tuple[int, int, str]]] = contextvars.ContextVar(
    "collegue_last_sampling_usage", default=None
)


@dataclass
class UsageCapture:
    """Résultat d'une capture isolée, renseigné à la sortie du contexte."""

    usage: Optional[Tuple[int, int, str]] = None


@contextmanager
def capture_usage():
    """Isole l'usage d'un appel et restaure exactement le contexte parent.

    Une capture imbriquée ne vole pas l'usage déjà accumulé par son parent et
    son résultat ne peut pas fuir vers le prochain outil exécuté dans la task.
    """
    captured = UsageCapture()
    token = _last_usage.set(None)
    try:
        yield captured
    finally:
        captured.usage = _last_usage.get()
        _last_usage.reset(token)


def record_usage(prompt_tokens: int, completion_tokens: int, model: str = "") -> None:
    """Cumule les tokens réels d'un appel LLM (appelé par le handler).

    On accumule les tokens plutôt qu'on n'écrase : un seul ``ctx.sample()`` peut
    déclencher plusieurs appels LLM (boucle d'outils / structured output), et
    l'appelant ne consomme la valeur qu'une fois via :func:`take_usage`. Le
    modèle conservé est le dernier non vide rencontré.
    """
    prompt_tokens = int(prompt_tokens or 0)
    completion_tokens = int(completion_tokens or 0)
    if prompt_tokens < 0 or completion_tokens < 0:
        raise ValueError("Les compteurs d'usage LLM ne peuvent pas être négatifs.")
    prev_p, prev_c, prev_m = _last_usage.get() or (0, 0, "")
    model = model or prev_m
    _last_usage.set((prev_p + prompt_tokens, prev_c + completion_tokens, model))


def take_usage() -> Optional[Tuple[int, int, str]]:
    """Récupère et remet à zéro l'usage cumulé.

    Retourne ``(prompt_tokens, completion_tokens, model)`` ou ``None`` si aucun
    appel n'a été suivi dans cette task.
    """
    usage = _last_usage.get()
    _last_usage.set(None)
    return usage
