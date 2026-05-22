"""
ToolLLMManager - Gestionnaire centralisé des appels LLM pour tous les outils
"""

import asyncio
from typing import Any, Dict, List, Optional, Tuple

from collegue.config import Settings
from collegue.config import settings as global_settings
from collegue.resources.llm.providers import LLMConfig, generate_text


class ToolLLMManager:
    def __init__(self, settings: Optional[Settings] = None):
        self.settings = settings or global_settings

        if not self.settings.llm_api_key:
            raise ValueError(
                "La clé API LLM n'est pas configurée. "
                "Veuillez l'ajouter via mcp_config.json, fichier .env ou variables d'environnement."
            )

        self.llm_config = LLMConfig(
            model_name=self.settings.llm_model,
            api_key=self.settings.llm_api_key,
            max_tokens=self.settings.MAX_TOKENS,
            temperature=0.7,
        )

        import logging

        logger = logging.getLogger(__name__)
        logger.info(f"ToolLLMManager initialisé avec le modèle: {self.settings.llm_model}")

    async def async_generate(self, prompt: str, system_prompt: Optional[str] = None) -> str:
        response = await generate_text(self.llm_config, prompt, system_prompt)
        return response.text

    def sync_generate(self, prompt: str, system_prompt: Optional[str] = None) -> str:
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
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
        self, prompt: str, system_prompt: Optional[str] = None, max_results: int = 5
    ) -> Tuple[str, List[Dict[str, Any]]]:
        import logging

        logger = logging.getLogger(__name__)

        web_config = LLMConfig(
            model_name=self.settings.llm_model,
            api_key=self.settings.llm_api_key,
            max_tokens=self.settings.MAX_TOKENS,
            temperature=0.7,
            use_search_grounding=True,
        )

        logger.info(f"🔍 Recherche web activée (max_results={max_results})")
        response = await generate_text(web_config, prompt, system_prompt)

        citations = []
        for annotation in response.annotations:
            if isinstance(annotation, dict) and annotation.get("type") == "url_citation":
                citation = annotation.get("url_citation", {})
                citations.append(
                    {
                        "url": citation.get("url", ""),
                        "title": citation.get("title", ""),
                        "content": citation.get("content", ""),
                    }
                )

        if citations:
            logger.info(f"📚 {len(citations)} source(s) web trouvée(s)")

        return response.text, citations

    def sync_generate_with_web_search(
        self, prompt: str, system_prompt: Optional[str] = None, max_results: int = 5
    ) -> Tuple[str, List[Dict[str, Any]]]:

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
