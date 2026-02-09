"""
Providers LLM - Intégration avec Google Gemini uniquement
"""
from pydantic import BaseModel
from typing import Dict, List, Optional, Any
import json
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class LLMConfig(BaseModel):
	"""Configuration pour Google Gemini."""
	model_name: str
	api_key: Optional[str] = None
	max_tokens: int = 8192
	temperature: float = 0.7
	top_p: Optional[float] = None
	stop_sequences: List[str] = []
	use_search_grounding: bool = False


class LLMResponse(BaseModel):
	"""Réponse de Google Gemini."""
	text: str
	usage: Dict[str, int] = {}
	model: str
	finish_reason: Optional[str] = None
	additional_info: Dict[str, Any] = {}
	annotations: List[Dict[str, Any]] = []


def _extract_google_citations(response) -> List[Dict[str, Any]]:
	"""Extrait les citations web depuis la réponse Google Gemini."""
	annotations = []
	
	if not response.candidates:
		return annotations
	
	candidate = response.candidates[0]
	if not hasattr(candidate, 'grounding_metadata') or not candidate.grounding_metadata:
		return annotations
	
	metadata = candidate.grounding_metadata
	chunks = getattr(metadata, 'grounding_chunks', None) or []
	
	for i, chunk in enumerate(chunks):
		if hasattr(chunk, 'web') and chunk.web:
			annotations.append({
				'type': 'url_citation',
				'url_citation': {
					'url': getattr(chunk.web, 'uri', ''),
					'title': getattr(chunk.web, 'title', ''),
					'content': ''
				}
			})
	
	return annotations


def _extract_usage(response) -> Dict[str, int]:
	"""Extrait les tokens utilisés depuis la réponse Gemini."""
	usage = {}
	if hasattr(response, 'usage_metadata') and response.usage_metadata:
		metadata = response.usage_metadata
		if hasattr(metadata, 'prompt_token_count'):
			usage['prompt_tokens'] = metadata.prompt_token_count
		if hasattr(metadata, 'candidates_token_count'):
			usage['completion_tokens'] = metadata.candidates_token_count
		if hasattr(metadata, 'total_token_count'):
			usage['total_tokens'] = metadata.total_token_count
	return usage


def initialize_llm_client(api_key: Optional[str] = None):
	"""Initialise le client Google Gemini."""
	try:
		from google import genai
		client = genai.Client(api_key=api_key)
		logger.info("Client Google Gemini initialisé avec succès.")
		return client
	except ImportError:
		logger.error("Package google-genai non installé. Exécutez: pip install google-genai")
		return None
	except Exception as e:
		logger.error(f"Échec d'initialisation du client Gemini: {str(e)}")
		return None


_llm_client = None


def get_llm_client(config: LLMConfig):
	"""Récupère ou initialise le client global."""
	global _llm_client
	if _llm_client is None:
		_llm_client = initialize_llm_client(config.api_key)
	return _llm_client


async def generate_text(config: LLMConfig, prompt: str, system_prompt: Optional[str] = None) -> LLMResponse:
	"""Génère du texte avec Google Gemini."""
	from google.genai import types
	
	client = get_llm_client(config)
	if not client:
		return LLMResponse(
			text="Erreur: Impossible d'initialiser le client Google Gemini. Vérifiez que google-genai est installé et que LLM_API_KEY est configurée.",
			model=config.model_name
		)
	
	try:
		gen_config = types.GenerateContentConfig(
			system_instruction=system_prompt,
			max_output_tokens=config.max_tokens,
			temperature=config.temperature,
			top_p=config.top_p if config.top_p else 1.0,
			stop_sequences=config.stop_sequences if config.stop_sequences else None,
		)
		
		if config.use_search_grounding:
			gen_config.tools = [types.Tool(google_search=types.GoogleSearch())]
			logger.info("🔍 Google Search Grounding activé")
		
		response = await client.aio.models.generate_content(
			model=config.model_name,
			contents=prompt,
			config=gen_config
		)
		
		annotations = _extract_google_citations(response)
		
		usage = _extract_usage(response)
		
		finish_reason = None
		if response.candidates and len(response.candidates) > 0:
			candidate = response.candidates[0]
			if hasattr(candidate, 'finish_reason') and candidate.finish_reason:
				finish_reason = str(candidate.finish_reason)
		
		return LLMResponse(
			text=response.text or "",
			usage=usage,
			model=config.model_name,
			finish_reason=finish_reason,
			annotations=annotations
		)
	
	except Exception as e:
		logger.error(f"Erreur lors de la génération avec Gemini: {str(e)}")
		return LLMResponse(
			text=f"Erreur lors de la génération de texte: {str(e)}",
			model=config.model_name
		)


def get_available_models() -> List[str]:
	"""Récupère la liste des modèles Gemini disponibles."""
	return [
		"gemini-3-flash-preview",
		"gemini-3-flash",
		"gemini-2.5-flash",
		"gemini-2.5-pro",
		"gemini-2.5-flash-lite",
	]


def register_providers(app, app_state):
	"""Enregistre les ressources Google Gemini."""
	
	@app.resource("collegue://llm/models/index")
	def get_llm_models_index() -> str:
		"""Liste tous les modèles Gemini disponibles."""
		return json.dumps(get_available_models())
	
	@app.resource("collegue://llm/models/{model_name}")
	def get_model_config_resource(model_name: str) -> str:
		"""Récupère la configuration d'un modèle spécifique."""
		available = get_available_models()
		if model_name in available:
			config = LLMConfig(model_name=model_name)
			return config.model_dump_json()
		return json.dumps({"error": f"Modèle {model_name} non trouvé"})
	
	app_state["llm_generate"] = generate_text
