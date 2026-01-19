"""
Providers LLM - Intégration avec différents fournisseurs de modèles de langage
"""
from pydantic import BaseModel
from typing import Dict, List, Optional, Any, Callable
import json
import os
import logging
from enum import Enum

# Configurer le logger
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class LLMProvider(str, Enum):
    """Enum des fournisseurs de LLM supportés."""
    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    LOCAL = "local"
    HUGGINGFACE = "huggingface"
    AZURE = "azure"

class LLMConfig(BaseModel):
    """Configuration pour un modèle LLM."""
    provider: LLMProvider
    model_name: str
    api_key: Optional[str] = None
    api_base: Optional[str] = None
    max_tokens: int = 4096
    temperature: float = 0.7
    top_p: Optional[float] = None
    stop_sequences: List[str] = []
    additional_params: Dict[str, Any] = {}
    plugins: List[Dict[str, Any]] = []

class LLMResponse(BaseModel):
    """Réponse d'un modèle LLM."""
    text: str
    usage: Dict[str, int] = {}
    model: str
    provider: LLMProvider
    finish_reason: Optional[str] = None
    additional_info: Dict[str, Any] = {}
    annotations: List[Dict[str, Any]] = []

DEFAULT_MODEL_CONFIGS = {
    "gpt-4": {
        "provider": LLMProvider.OPENAI,
        "model_name": "gpt-4",
        "max_tokens": 8192,
        "temperature": 0.7
    },
    "gpt-3.5-turbo": {
        "provider": LLMProvider.OPENAI,
        "model_name": "gpt-3.5-turbo",
        "max_tokens": 4096,
        "temperature": 0.7
    },
    "claude-3-opus": {
        "provider": LLMProvider.ANTHROPIC,
        "model_name": "claude-3-opus-20240229",
        "max_tokens": 4096,
        "temperature": 0.7
    },
    "claude-3-sonnet": {
        "provider": LLMProvider.ANTHROPIC,
        "model_name": "claude-3-sonnet-20240229",
        "max_tokens": 4096,
        "temperature": 0.7
    }
}

llm_clients = {}

def initialize_llm_client(config: LLMConfig):
    """Initialise un client LLM en fonction de la configuration."""
    provider = config.provider
    
    if provider == LLMProvider.OPENAI:
        try:
            import openai
            # Détection de version
            version = getattr(openai, "__version__", "0.0.0")
            # Interface v0 (<=0.28) --------------------------------------------------
            if version.startswith("0."):
                if config.api_key:
                    openai.api_key = config.api_key
                if config.api_base:
                    openai.api_base = config.api_base
                # Test de connexion (ancienne API)
                _ = openai.Model.list()
                logger.info("OpenAI client (legacy) initialised successfully.")
                return openai  # On retourne directement le module comme client
            # Interface v1 (>=1.0) ---------------------------------------------------
            from openai import OpenAI as _OpenAIClient  # type: ignore
            client = _OpenAIClient(api_key=config.api_key, base_url=config.api_base)
            # Test de connexion (nouvelle API)
            _ = client.models.list()
            logger.info("OpenAI client (v1) initialised successfully.")
            return client
        except ImportError:
            logger.error("OpenAI package not installed. Run 'pip install openai'.")
            return None
        except Exception as e:
            logger.error(f"Failed to initialize OpenAI client: {str(e)}")
            return None
    
    elif provider == LLMProvider.ANTHROPIC:
        try:
            import anthropic
            client = anthropic.Anthropic(api_key=config.api_key)
            
            logger.info(f"Anthropic client initialized successfully.")
            
            return client
        except ImportError:
            logger.error("Anthropic package not installed. Run 'pip install anthropic'.")
            return None
        except Exception as e:
            logger.error(f"Failed to initialize Anthropic client: {str(e)}")
            return None
    
    elif provider == LLMProvider.HUGGINGFACE:
        try:
            from huggingface_hub import InferenceClient
            client = InferenceClient(token=config.api_key)
            
            logger.info(f"HuggingFace client initialized successfully.")
            
            return client
        except ImportError:
            logger.error("HuggingFace package not installed. Run 'pip install huggingface_hub'.")
            return None
        except Exception as e:
            logger.error(f"Failed to initialize HuggingFace client: {str(e)}")
            return None
    
    elif provider == LLMProvider.LOCAL:
        logger.info("Local LLM provider not fully implemented yet.")
        return None
    
    else:
        logger.error(f"Unsupported LLM provider: {provider}")
        return None

async def generate_text(config: LLMConfig, prompt: str, system_prompt: Optional[str] = None) -> LLMResponse:
    """Génère du texte à partir d'un prompt en utilisant le LLM configuré."""
    provider = config.provider
    
    # Récupérer ou initialiser le client
    if provider not in llm_clients:
        llm_clients[provider] = initialize_llm_client(config)
    
    client = llm_clients[provider]
    if not client:
        return LLMResponse(
            text="Erreur: Le client LLM n'a pas pu être initialisé.",
            model=config.model_name,
            provider=config.provider
        )
    
    try:
        if provider == LLMProvider.OPENAI:
            messages = []
            if system_prompt:
                messages.append({"role": "system", "content": system_prompt})
            messages.append({"role": "user", "content": prompt})
            
            try:
                # Nouvelle interface (client.chat.completions.create)
                if hasattr(client, "chat") and hasattr(client.chat, "completions"):
                    # Paramètres supplémentaires autorisés en v1
                    _allowed_extra = {"seed", "frequency_penalty", "presence_penalty", "response_format", "stream"}
                    extra_v1 = {k: v for k, v in config.additional_params.items() if k in _allowed_extra}
                    
                    # Support des plugins OpenRouter (web search, etc.)
                    if config.plugins:
                        extra_v1["extra_body"] = {"plugins": config.plugins}
                    
                    response = client.chat.completions.create(
                        model=config.model_name,
                        messages=messages,
                        max_tokens=config.max_tokens,
                        temperature=config.temperature,
                        top_p=config.top_p if config.top_p else 1.0,
                        stop=config.stop_sequences if config.stop_sequences else None,
                        **extra_v1
                    )
                    
                    # Extraire les annotations (citations web) si présentes
                    annotations = []
                    if hasattr(response.choices[0].message, 'annotations') and response.choices[0].message.annotations:
                        raw_annotations = response.choices[0].message.annotations
                        for ann in raw_annotations:
                            # Convertir l'objet Annotation en dict
                            if hasattr(ann, 'model_dump'):
                                annotations.append(ann.model_dump())
                            elif hasattr(ann, '__dict__'):
                                annotations.append(dict(ann.__dict__))
                            elif isinstance(ann, dict):
                                annotations.append(ann)
                            else:
                                # Fallback: extraire manuellement les champs connus
                                ann_dict = {'type': getattr(ann, 'type', 'unknown')}
                                if hasattr(ann, 'url_citation'):
                                    citation = ann.url_citation
                                    ann_dict['url_citation'] = {
                                        'url': getattr(citation, 'url', ''),
                                        'title': getattr(citation, 'title', ''),
                                        'content': getattr(citation, 'content', '')
                                    }
                                annotations.append(ann_dict)
                    
                    return LLMResponse(
                        text=response.choices[0].message.content,
                        usage={"prompt_tokens": response.usage.prompt_tokens, "completion_tokens": response.usage.completion_tokens},
                        model=response.model,
                        provider=config.provider,
                        finish_reason=response.choices[0].finish_reason,
                        annotations=annotations
                    )
                # Ancienne interface (openai.ChatCompletion.create)
                response = client.ChatCompletion.create(
                    model=config.model_name,
                    messages=messages,
                    max_tokens=config.max_tokens,
                    temperature=config.temperature,
                    top_p=config.top_p if config.top_p else 1.0,
                    stop=config.stop_sequences if config.stop_sequences else None,
                    **config.additional_params
                )
                
                return LLMResponse(
                    text=response.choices[0].message.content,
                    usage=response.usage,
                    model=response.model,
                    provider=config.provider,
                    finish_reason=response.choices[0].finish_reason
                )
            except Exception as ee:
                logger.error(f"OpenAI generation failed: {ee}")
                raise
        
        elif provider == LLMProvider.ANTHROPIC:
            message = client.messages.create(
                model=config.model_name,
                max_tokens=config.max_tokens,
                temperature=config.temperature,
                system=system_prompt if system_prompt else None,
                messages=[{"role": "user", "content": prompt}],
                **config.additional_params
            )
            
            return LLMResponse(
                text=message.content[0].text,
                usage={"input_tokens": message.usage.input_tokens, "output_tokens": message.usage.output_tokens},
                model=config.model_name,
                provider=config.provider,
                finish_reason=message.stop_reason
            )
        
        elif provider == LLMProvider.HUGGINGFACE:
            response = client.text_generation(
                prompt,
                model=config.model_name,
                max_new_tokens=config.max_tokens,
                temperature=config.temperature,
                **config.additional_params
            )
            
            return LLMResponse(
                text=response,
                model=config.model_name,
                provider=config.provider
            )
        
        else:
            return LLMResponse(
                text="Erreur: Fournisseur LLM non supporté.",
                model=config.model_name,
                provider=config.provider
            )
    
    except Exception as e:
        logger.error(f"Error generating text with {provider}: {str(e)}")
        return LLMResponse(
            text=f"Erreur lors de la génération de texte: {str(e)}",
            model=config.model_name,
            provider=config.provider
        )

def get_default_model_config(model_name: str) -> Optional[LLMConfig]:
    """Récupère la configuration par défaut pour un modèle."""
    if model_name in DEFAULT_MODEL_CONFIGS:
        return LLMConfig(**DEFAULT_MODEL_CONFIGS[model_name])
    return None

def get_available_models() -> List[str]:
    """Récupère la liste des modèles disponibles par défaut."""
    return list(DEFAULT_MODEL_CONFIGS.keys())

def register_providers(app, app_state):
    """Enregistre les ressources des fournisseurs LLM."""
    
    @app.resource("collegue://llm/models/index")
    def get_llm_models_index() -> str:
        """Liste tous les modèles LLM disponibles."""
        return json.dumps(get_available_models())
    
    @app.resource("collegue://llm/models/{model_name}")
    def get_model_config_resource(model_name: str) -> str:
        """Récupère la configuration d'un modèle spécifique."""
        config = get_default_model_config(model_name)
        if config:
            return config.model_dump_json()
        return json.dumps({"error": f"Modèle {model_name} non trouvé"})
    
    app_state["llm_generate"] = generate_text
