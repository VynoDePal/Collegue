"""
ToolLLMManager - Gestionnaire centralisé des appels LLM pour tous les outils
"""
from typing import Optional
from collegue.config import Settings
from collegue.resources.llm.providers import LLMConfig, LLMProvider, generate_text
import asyncio

class ToolLLMManager:
    """Gestionnaire unique pour l'appel au LLM OpenRouter DeepSeek via OpenRouter."""
    def __init__(self, settings: Optional[Settings] = None):
        self.settings = settings or Settings()
        
        # Utilise les propriétés avec priorité MCP > env > default
        if not self.settings.llm_api_key:
            raise ValueError(
                "La clé API LLM n'est pas configurée. "
                "Veuillez l'ajouter via mcp_config.json, fichier .env ou variables d'environnement."
            )

        # Configuration avec les propriétés qui gèrent la priorité MCP
        self.llm_config = LLMConfig(
            provider=LLMProvider.OPENAI,  # OpenRouter compatible avec OpenAI API
            model_name=self.settings.llm_model,  # Utilise la propriété avec priorité
            api_key=self.settings.llm_api_key,    # Utilise la propriété avec priorité
            api_base=self.settings.LLM_BASE_URL,
            max_tokens=self.settings.MAX_TOKENS,
            temperature=0.7,  # Valeur par défaut raisonnable
            additional_params={
                "http_client": {
                    "headers": {
                        "HTTP-Referer": "https://github.com/your-org/collegue-mcp",
                        "X-Title": "Collègue MCP"
                    }
                }
            }
        )
        
        # Log le modèle utilisé (sans la clé API pour la sécurité)
        import logging
        logger = logging.getLogger(__name__)
        logger.info(f"ToolLLMManager initialisé avec le modèle: {self.settings.llm_model}")

    async def async_generate(self, prompt: str, system_prompt: Optional[str] = None) -> str:
        """Appel asynchrone au LLM pour générer du texte."""
        response = await generate_text(self.llm_config, prompt, system_prompt)
        return response.text

    def sync_generate(self, prompt: str, system_prompt: Optional[str] = None) -> str:
        """
        Appel synchrone au LLM (pour outils non async).
        
        Args:
            prompt: Le prompt à envoyer au LLM
            system_prompt: Message système optionnel pour configurer le comportement du LLM
            
        Returns:
            La réponse générée par le LLM
        """
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # Exécuter dans un thread séparé pour éviter le deadlock
                import threading
                result_container: dict[str, str] = {}

                def _runner() -> None:
                    result_container["value"] = asyncio.run(self.async_generate(prompt, system_prompt))

                t = threading.Thread(target=_runner, daemon=True)
                t.start()
                t.join()
                return result_container["value"]
        except RuntimeError:
            # Si aucune boucle n'est en cours, on utilise run
            pass
            
        return asyncio.run(self.async_generate(prompt, system_prompt))
