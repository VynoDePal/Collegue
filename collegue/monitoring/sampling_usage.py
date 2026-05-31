"""Capture des tokens réels renvoyés par le provider lors d'un ``ctx.sample()``.

FastMCP expose ``SamplingResult(text, result, history)`` sans le ``usage`` du
provider, et le ``OpenAISamplingHandler`` par défaut ne propage pas les tokens.
Le handler de sampling s'exécutant dans la même task asyncio que l'outil
appelant, on partage le dernier ``usage`` via un ``ContextVar`` : le handler y
écrit après chaque appel LLM, l'outil le lit juste après ``ctx.sample()``.
"""

from __future__ import annotations

import contextvars
from typing import Optional, Tuple

# (prompt_tokens, completion_tokens) du dernier appel LLM dans cette task, ou None.
_last_usage: contextvars.ContextVar[Optional[Tuple[int, int]]] = contextvars.ContextVar(
    "collegue_last_sampling_usage", default=None
)


def record_usage(prompt_tokens: int, completion_tokens: int) -> None:
    """Cumule les tokens réels d'un appel LLM (appelé par le handler).

    On accumule plutôt qu'on n'écrase : un seul ``ctx.sample()`` peut déclencher
    plusieurs appels LLM (boucle d'outils / structured output), et l'appelant ne
    consomme la valeur qu'une fois via :func:`take_usage`.
    """
    prev = _last_usage.get() or (0, 0)
    _last_usage.set((prev[0] + int(prompt_tokens or 0), prev[1] + int(completion_tokens or 0)))


def take_usage() -> Optional[Tuple[int, int]]:
    """Récupère et remet à zéro les tokens cumulés (None si aucun appel suivi)."""
    usage = _last_usage.get()
    _last_usage.set(None)
    return usage
