"""
ToolLLMManager - Gestionnaire centralisé des appels LLM pour tous les outils
"""
from typing import Optional, List, Dict, Tuple, Any
from collegue.config import Settings, settings as global_settings
from collegue.resources.llm.providers import LLMConfig, LLMProvider, generate_text
import asyncio

class ToolLLMManager:
    """Gestionnaire unique pour l'appel au LLM OpenRouter DeepSeek via OpenRouter."""
    def __init__(self, settings: Optional[Settings] = None):
        # Utiliser l'instance globale par défaut afin de bénéficier des mises à jour runtime
        # (priorité MCP via settings.update_from_mcp)
        self.settings = settings or global_settings
        
        if not self.settings.llm_api_key:
            raise ValueError(
                "La clé API LLM n'est pas configurée. "
                "Veuillez l'ajouter via mcp_config.json, fichier .env ou variables d'environnement."
            )

        # Configuration avec les propriétés qui gèrent la priorité MCP
        self.llm_config = LLMConfig(
            provider=LLMProvider.OPENAI,  # OpenRouter compatible avec OpenAI API
            model_name=self.settings.llm_model,
            api_key=self.settings.llm_api_key,
            api_base=self.settings.LLM_BASE_URL,
            max_tokens=self.settings.MAX_TOKENS,
            temperature=0.7,
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
            pass
            
        return asyncio.run(self.async_generate(prompt, system_prompt))

    async def async_generate_with_web_search(
        self, 
        prompt: str, 
        system_prompt: Optional[str] = None,
        max_results: int = 5
    ) -> Tuple[str, List[Dict[str, Any]]]:
        """
        Appel asynchrone au LLM avec recherche web activée (OpenRouter plugin).
        
        Args:
            prompt: Le prompt à envoyer au LLM
            system_prompt: Message système optionnel
            max_results: Nombre max de résultats web (défaut: 5, max recommandé: 5)
            
        Returns:
            Tuple (response_text, annotations) où annotations contient les citations web
        """
        import logging
        logger = logging.getLogger(__name__)
        
        # Configuration avec plugin web search
        web_config = LLMConfig(
            provider=LLMProvider.OPENAI,
            model_name=self.settings.llm_model,
            api_key=self.settings.llm_api_key,
            api_base=self.settings.LLM_BASE_URL,
            max_tokens=self.settings.MAX_TOKENS,
            temperature=0.7,
            plugins=[{
                "id": "web",
                "max_results": min(max_results, 5)
            }]
        )
        
        logger.info(f"🔍 Recherche web activée (max_results={max_results})")
        response = await generate_text(web_config, prompt, system_prompt)
        
        # Extraire les citations
        citations = []
        for annotation in response.annotations:
            if isinstance(annotation, dict) and annotation.get("type") == "url_citation":
                citation = annotation.get("url_citation", {})
                citations.append({
                    "url": citation.get("url", ""),
                    "title": citation.get("title", ""),
                    "content": citation.get("content", "")
                })
        
        if citations:
            logger.info(f"📚 {len(citations)} source(s) web trouvée(s)")
        
        return response.text, citations

    def sync_generate_with_web_search(
        self, 
        prompt: str, 
        system_prompt: Optional[str] = None,
        max_results: int = 5
    ) -> Tuple[str, List[Dict[str, Any]]]:
        """
        Appel synchrone au LLM avec recherche web (pour outils non async).
        """
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                import threading
                result_container: Dict[str, Any] = {}

                def _runner() -> None:
                    result_container["value"] = asyncio.run(
                        self.async_generate_with_web_search(prompt, system_prompt, max_results)
                    )

                t = threading.Thread(target=_runner, daemon=True)
                t.start()
                t.join()
                return result_container["value"]
        except RuntimeError:
            pass
            
        return asyncio.run(self.async_generate_with_web_search(prompt, system_prompt, max_results))
