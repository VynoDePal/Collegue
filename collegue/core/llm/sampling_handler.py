"""Handler de sampling FastMCP avec capture d'usage et routage par modèle.

Défini au niveau module (et non dans un ``if`` de ``app.py``) pour être
importable et testable. Deux différences avec ``OpenAISamplingHandler`` :

1. **Capture des tokens réels** : le handler de base ne propage pas
   ``response.usage`` ; on enveloppe ``chat.completions.create`` pour exposer
   l'usage aux outils via un ContextVar (``collegue/monitoring/sampling_usage``).
2. **Honore un modèle arbitraire** : le handler de base ne retient que les
   modèles OpenAI connus (``ChatModel``) et ignorerait silencieusement un modèle
   Gemini/local passé en préférence → le routage par rôle serait un no-op. Ici,
   toute préférence explicite non vide prime, sinon le modèle par défaut.

``build_sampling_handler`` est tolérant : il retourne ``None`` si ``fastmcp`` /
``openai`` ne sont pas disponibles, pour ne pas casser le démarrage.
"""

from __future__ import annotations

from typing import Any, Optional


def _make_handler_class():
    """Construit la classe handler (import paresseux de fastmcp/openai)."""
    from fastmcp.client.sampling.handlers.openai import OpenAISamplingHandler

    from collegue.monitoring.sampling_usage import record_usage

    class UsageTrackingSamplingHandler(OpenAISamplingHandler):
        """Handler OpenAI-compatible : capture d'usage + modèle arbitraire honoré."""

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            inner = self.client.chat.completions.create

            async def _create(*a, **kw):
                response = await inner(*a, **kw)
                usage = getattr(response, "usage", None)
                if usage is not None:
                    record_usage(
                        getattr(usage, "prompt_tokens", 0) or 0,
                        getattr(usage, "completion_tokens", 0) or 0,
                    )
                return response

            self.client.chat.completions.create = _create

        def _select_model_from_preferences(self, model_preferences):
            for name in self._iter_models_from_preferences(model_preferences):
                if name:
                    return name
            return self.default_model

    return UsageTrackingSamplingHandler


def build_sampling_handler(default_model: str, api_key: Optional[str], base_url: Optional[str]) -> Optional[Any]:
    """Instancie le handler de sampling, ou ``None`` si les dépendances manquent."""
    try:
        from openai import AsyncOpenAI
    except ImportError:
        return None
    try:
        handler_cls = _make_handler_class()
    except ImportError:
        return None
    client = AsyncOpenAI(api_key=api_key, base_url=base_url)
    return handler_cls(default_model=default_model, client=client)
