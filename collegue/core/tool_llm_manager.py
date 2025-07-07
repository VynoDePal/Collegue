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
        
        if not self.settings.LLM_API_KEY:
            raise ValueError(
                "La clé API LLM (LLM_API_KEY) n'est pas configurée. "
                "Veuillez l'ajouter à votre fichier .env ou à vos variables d'environnement."
            )

        # Configuration unique imposée par la politique projet
        self.llm_config = LLMConfig(
            provider=LLMProvider.OPENAI,  # OpenRouter compatible avec OpenAI API
            model_name=self.settings.LLM_MODEL,
            api_key=self.settings.LLM_API_KEY,
            api_base="https://openrouter.ai/api/v1",  # Endpoint fixe pour OpenRouter
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
