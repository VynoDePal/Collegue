"""Client ``chat.completions.create`` branché sur le service du courtier EN PROCESSUS (planner, QA, reviewer, boucle agentique…).

Même interface que le SDK OpenAI pour les appelants existants (ctx offline, handler serveur FastMCP), mais aucun réseau et
aucune clé : la génération passe par :meth:`BrokerService.sampling_completion` dans le scope GLOBAL du budget lié au contexte
(:func:`collegue.core.llm.budget_guard.current_binding`). Un contexte sans registre lié n'est JAMAIS une exemption : refus.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from collegue.broker.errors import BrokerForbidden
from collegue.broker.runtime import runtime_for


class BrokerChatClient:
    """Imite ``AsyncOpenAI`` sur la seule surface ``chat.completions.create`` ; le rôle est fixé par la ROUTE, pas par l'appel."""

    def __init__(self, settings: Any, role: str):
        self._settings = settings
        self._role = str(role)
        self.base_url = None  # aucune destination réseau : le courtier est la seule destination
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def with_options(self, **_options: Any) -> "BrokerChatClient":
        """Compat des enveloppes de budget historiques : aucune option (retries, timeout…) ne change le courtier."""
        return self

    async def create(self, **kwargs: Any) -> Any:
        from openai.types.chat import ChatCompletion

        from collegue.core.llm.budget_guard import current_binding

        binding = current_binding()
        if binding is None:
            raise BrokerForbidden(
                "mode courtier : aucun registre budgétaire lié au contexte — le courtier n'admet pas de contexte absent "
                "comme exemption",
                code="no_budget_context",
            )
        completion = (
            await runtime_for(self._settings)
            .service_for(binding.ledger)
            .sampling_completion(binding.scope_key, self._role, dict(kwargs))
        )
        return ChatCompletion.model_validate(completion)

    async def close(self) -> None:  # symétrie avec les clients réseau
        return None
