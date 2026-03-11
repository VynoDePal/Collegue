"""
Base Tool - Classe de base pour tous les outils du projet Collègue

Le timing, logging, error handling et caching sont gérés par les
middleware FastMCP natifs configurés dans app.py.
"""
import os
import time
import logging
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, Type, Union
from pydantic import BaseModel, ValidationError
from pydantic_core import PydanticUndefined
import asyncio
from ..core.security_logger import security_logger
from .rate_limiter import (
    get_rate_limiter_manager,
    RateLimitExceeded,
    RateLimitConfig,
)
from .quotas import (
    get_global_quota_manager,
    QuotaExceeded,
    QuotaManager,
)


class ToolError(Exception):
    pass


class ToolValidationError(ToolError):
    pass


class ToolExecutionError(ToolError):
    pass


class ToolConfigurationError(ToolError):
    pass


class ToolRateLimitError(ToolExecutionError):
    """Erreur de rate limiting spécifique aux tools."""
    pass


class ToolQuotaError(ToolExecutionError):
    """Erreur de quota spécifique aux tools."""
    pass


class BaseTool(ABC):
    """
    Classe de base pour tous les outils Collegue.
    
    Fournit:
    - Validation des requêtes et réponses
    - Rate limiting par tool
    - Gestion des quotas (tokens, fichiers, temps d'exécution)
    - Logging de sécurité
    - Support async/sync
    """
    
    tool_name: str = ""
    tool_description: str = ""
    request_model: Optional[Type[BaseModel]] = None
    response_model: Optional[Type[BaseModel]] = None
    supported_languages: List[str] = ["python", "javascript", "typescript"]
    long_running: bool = False
    tags: set = set()
    
    # Configuration du rate limiting et des quotas
    rate_limit_enabled: bool = True
    quota_enabled: bool = True
    custom_rate_limit: Optional[RateLimitConfig] = None

    def __init__(self, config: Optional[Dict[str, Any]] = None, app_state: Optional[Dict[str, Any]] = None):
        self.config = config or {}
        self.app_state = app_state or {}
        self.logger = logging.getLogger(f"tools.{self.__class__.__name__}")

        self.prompt_engine = self.app_state.get('prompt_engine')
        self.context_manager = self.app_state.get('context_manager')
        
        # Gestionnaire de quotas pour cette session
        self._quota_manager: Optional[QuotaManager] = None
        self._session_id: str = "default"

        self._validate_config()

    def _validate_config(self):
        required_configs = self.get_required_config_keys()
        missing_keys = [key for key in required_configs if key not in self.config]

        if missing_keys:
            raise ToolConfigurationError(
                f"Configuration manquante pour {self.__class__.__name__}: {missing_keys}"
            )

    def get_name(self) -> str:
        return self.tool_name

    def get_description(self) -> str:
        return self.tool_description

    def get_request_model(self) -> Type[BaseModel]:
        return self.request_model

    def get_response_model(self) -> Type[BaseModel]:
        return self.response_model

    def get_required_config_keys(self) -> List[str]:
        return []

    def get_supported_languages(self) -> List[str]:
        return self.supported_languages

    def is_long_running(self) -> bool:
        return self.long_running
    
    def _get_session_id(self, **kwargs) -> str:
        """Récupère l'ID de session depuis les headers ou kwargs."""
        # Essayer de récupérer depuis les headers HTTP
        try:
            from fastmcp.server.dependencies import get_http_headers
            headers = get_http_headers() or {}
            session_id = headers.get('x-session-id') or headers.get('x-collegue-session-id')
            if session_id:
                return str(session_id)
        except Exception:
            pass
        
        # Sinon depuis les kwargs
        return kwargs.get('session_id', self._session_id)
    
    def _check_rate_limit(self):
        """
        Vérifie le rate limiting pour ce tool.
        
        Raises:
            ToolRateLimitError: Si la limite de taux est dépassée
        """
        if not self.rate_limit_enabled:
            return
        
        try:
            manager = get_rate_limiter_manager()
            # Utiliser la configuration personnalisée si définie
            if self.custom_rate_limit is not None:
                manager.get_limiter(self.tool_name, self.custom_rate_limit)
            manager.check_rate_limit(self.tool_name)
        except RateLimitExceeded as e:
            self.logger.warning(f"Rate limit exceeded for {self.tool_name}: {e}")
            raise ToolRateLimitError(str(e))
    
    def _get_quota_manager(self, **kwargs) -> QuotaManager:
        """Récupère ou crée le gestionnaire de quotas pour cette session."""
        if self._quota_manager is None:
            session_id = self._get_session_id(**kwargs)
            global_manager = get_global_quota_manager()
            self._quota_manager = global_manager.get_session_manager(session_id)
            self._session_id = session_id
        return self._quota_manager
    
    def _check_quotas(self, request: BaseModel, **kwargs):
        """
        Vérifie les quotas avant l'exécution.
        
        Raises:
            ToolQuotaError: Si un quota est dépassé
        """
        if not self.quota_enabled:
            return
        
        try:
            manager = self._get_quota_manager(**kwargs)
            manager.start_execution()
            
            # Vérifier la taille de la requête si applicable
            if hasattr(request, 'model_dump'):
                import json
                try:
                    request_json = json.dumps(request.model_dump())
                except (TypeError, ValueError):
                    # Ignorer les erreurs de sérialisation JSON lors de l'estimation de la taille
                    pass
                else:
                    request_size = len(request_json.encode('utf-8'))
                    manager.check_request_size(request_size)
            
            # Vérifier les fichiers si présents dans la requête
            file_paths = []
            if hasattr(request, 'file_path'):
                file_paths.append(request.file_path)
            if hasattr(request, 'file_paths'):
                file_paths.extend(request.file_paths)
            if hasattr(request, 'path'):
                file_paths.append(request.path)
            
            for path in file_paths:
                if path and os.path.exists(path):
                    size = manager.check_file_size(path)
                    manager.record_file_processed(path, size)
                    
        except QuotaExceeded as e:
            self.logger.warning(f"Quota exceeded for {self.tool_name}: {e}")
            raise ToolQuotaError(str(e))
    
    def _record_llm_tokens(self, tokens: int, **kwargs):
        """Enregistre l'utilisation de tokens LLM."""
        if self.quota_enabled and self._quota_manager:
            try:
                self._quota_manager.record_llm_tokens(tokens)
            except QuotaExceeded as e:
                self.logger.warning(f"LLM token quota exceeded: {e}")
                raise ToolQuotaError(str(e))
    
    def _check_execution_time(self, **kwargs) -> float:
        """
        Vérifie le temps d'exécution.
        
        Returns:
            Temps d'exécution actuel
        
        Raises:
            ToolQuotaError: Si le temps max est dépassé
        """
        if not self.quota_enabled or not self._quota_manager:
            return 0.0
        
        try:
            return self._quota_manager.check_execution_time()
        except QuotaExceeded as e:
            self.logger.warning(f"Execution time quota exceeded: {e}")
            raise ToolQuotaError(str(e))

    async def prepare_prompt(self, request: BaseModel, template_name: Optional[str] = None) -> str:
        if not self.prompt_engine:
            if hasattr(self, '_build_prompt'):
                self.logger.warning("Prompt engine non disponible, utilisation du fallback")
                return self._build_prompt(request)
            raise ToolExecutionError("Prompt engine non configuré et pas de méthode fallback")

        from ..prompts.engine.enhanced_prompt_engine import EnhancedPromptEngine

        if isinstance(self.prompt_engine, EnhancedPromptEngine):
            tool_name = template_name or self.get_name().lower().replace(' ', '_')
            language = getattr(request, 'language', None)

            context = request.model_dump() if hasattr(request, 'model_dump') else {}

            try:
                if asyncio.iscoroutinefunction(self.prompt_engine.get_optimized_prompt):
                    prompt, version = await self.prompt_engine.get_optimized_prompt(
                        tool_name=tool_name,
                        context=context,
                        language=language
                    )
                else:
                    prompt, version = self.prompt_engine.get_optimized_prompt(
                        tool_name=tool_name,
                        context=context,
                        language=language
                    )

                self.logger.info(f"Prompt préparé avec version {version} pour {tool_name}")
                return prompt
            except Exception as e:
                self.logger.warning(f"Erreur lors de la préparation du prompt optimisé: {e}")
                if hasattr(self, '_build_prompt'):
                    return self._build_prompt(request)
                raise

        else:
            tool_name = self.get_name().lower().replace(' ', '_')
            templates = self.prompt_engine.get_templates_by_category(f"tool/{tool_name}")

            if templates:
                template = templates[0]
                context = request.model_dump() if hasattr(request, 'model_dump') else {}
                prompt = self.prompt_engine.format_prompt(template.id, context)
                return prompt

            if hasattr(self, '_build_prompt'):
                self.logger.warning(f"Pas de template trouvé pour {tool_name}, utilisation du fallback")
                return self._build_prompt(request)

            raise ToolExecutionError(f"Aucun template trouvé pour l'outil {tool_name}")

    def validate_language(self, language: str) -> bool:
        supported = self.get_supported_languages()
        if language.lower() not in [lang.lower() for lang in supported]:
            raise ToolValidationError(
                f"Langage '{language}' non supporté. Langages supportés: {supported}"
            )
        return True

    def validate_request(self, request: Union[BaseModel, Dict[str, Any]]) -> bool:
        """
        Valide la requête sans la normaliser.
        
        Cette méthode peut être surchargée par les sous-classes pour
        ajouter des validations spécifiques. Elle retourne True si la 
        validation réussit. La normalisation est gérée par normalize_request().
        
        Args:
            request: Requête à valider (BaseModel ou dict)
        
        Returns:
            bool: True si la validation réussit
        
        Raises:
            ToolValidationError: Si la validation échoue
        """
        try:
            expected_model = self.get_request_model()
            
            # Vérifier que la requête est du bon type ou convertible
            if isinstance(request, expected_model):
                validated_request = request
            elif isinstance(request, dict):
                validated_request = expected_model(**request)
            elif hasattr(request, 'model_dump'):
                validated_request = expected_model(**request.model_dump())
            else:
                raise ToolValidationError(
                    f"Type de requête invalide. Attendu: {expected_model.__name__}, "
                    f"reçu: {type(request).__name__}"
                )

            # Valider le langage si présent
            if hasattr(validated_request, 'language') and validated_request.language:
                self.validate_language(validated_request.language)

            return True

        except ValidationError as e:
            raise ToolValidationError(f"Validation de la requête échouée: {e}")

    def normalize_request(self, request: Union[BaseModel, Dict[str, Any]]) -> BaseModel:
        """
        Normalise la requête vers le modèle attendu.
        
        Cette méthode convertit un dict ou un modèle compatible vers
        le type de requête attendu par le tool.
        
        Args:
            request: Requête à normaliser (BaseModel ou dict)
        
        Returns:
            BaseModel: La requête normalisée
        
        Raises:
            ToolValidationError: Si la normalisation échoue
        """
        try:
            expected_model = self.get_request_model()
            
            if isinstance(request, expected_model):
                return request
            elif isinstance(request, dict):
                return expected_model(**request)
            elif hasattr(request, 'model_dump'):
                return expected_model(**request.model_dump())
            else:
                raise ToolValidationError(
                    f"Type de requête invalide. Attendu: {expected_model.__name__}, "
                    f"reçu: {type(request).__name__}"
                )

        except ValidationError as e:
            raise ToolValidationError(f"Normalisation de la requête échouée: {e}")

    @abstractmethod
    def _execute_core_logic(self, request: BaseModel, **kwargs) -> BaseModel:
        pass

    def _validate_result(self, result: BaseModel) -> None:
        expected = self.get_response_model()
        if not isinstance(result, expected):
            raise ToolExecutionError(
                f"Type de réponse invalide. Attendu: {expected.__name__}"
            )

    def execute(self, request: Union[BaseModel, Dict[str, Any]], **kwargs) -> BaseModel:
        """
        Exécute le tool avec rate limiting et vérification des quotas.
        
        Args:
            request: Requête validée
            **kwargs: Arguments additionnels
        
        Returns:
            Résultat du tool
        
        Raises:
            ToolRateLimitError: Si le rate limiting est dépassé
            ToolQuotaError: Si un quota est dépassé
            ToolValidationError: Si la requête est invalide
            ToolExecutionError: En cas d'erreur d'exécution
        """
        # Vérifier rate limiting
        self._check_rate_limit()
        
        normalized_request = self.normalize_request(request)
        self.validate_request(normalized_request)
        self._check_quotas(normalized_request, **kwargs)
        
        # Log l'accès aux données sensibles
        try:
            client_ip = None
            user_id = None
            from fastmcp.server.dependencies import get_http_headers
            headers = get_http_headers() or {}
            client_ip = headers.get('x-forwarded-for') or headers.get('x-real-ip')
            user_id = headers.get('x-user-id') or headers.get('x-collegue-user-id')
        except Exception:
            pass
        
        security_logger.log_data_access(
            user_id=user_id or "anonymous",
            resource=self.tool_name,
            action="execute",
            client_ip=client_ip,
            extra={
                "request_type": normalized_request.__class__.__name__,
                "session_id": self._session_id,
            }
        )
        
        start_time = time.time()
        try:
            result = self._execute_core_logic(normalized_request, **kwargs)
            self._validate_result(result)
            
            execution_time = time.time() - start_time
            self.logger.debug(f"Tool {self.tool_name} executed in {execution_time:.2f}s")
            
            return result
            
        except Exception as e:
            self.logger.error(f"Error executing {self.tool_name}: {e}")
            raise

    async def execute_async(self, request: Union[BaseModel, Dict[str, Any]], **kwargs) -> BaseModel:
        """
        Exécute le tool de manière asynchrone avec rate limiting et quotas.
        
        Args:
            request: Requête validée
            **kwargs: Arguments additionnels (peut inclure ctx, session_id)
        
        Returns:
            Résultat du tool
        """
        ctx = kwargs.get('ctx')
        
        # Vérifier rate limiting
        self._check_rate_limit()
        
        normalized_request = self.normalize_request(request)
        self.validate_request(normalized_request)
        self._check_quotas(normalized_request, **kwargs)

        if not self.prompt_engine and kwargs.get('prompt_engine'):
            prompt_engine = kwargs.get('prompt_engine')
            if hasattr(prompt_engine, 'get_engine'):
                self.prompt_engine = await prompt_engine.get_engine()
            else:
                self.prompt_engine = prompt_engine
        if not getattr(self, 'parser', None) and kwargs.get('parser'):
            self.parser = kwargs.get('parser')
        if not self.context_manager and kwargs.get('context_manager'):
            self.context_manager = kwargs.get('context_manager')

        total_steps = 3

        if ctx:
            await ctx.report_progress(progress=0, total=total_steps)

        if ctx:
            await ctx.report_progress(progress=1, total=total_steps)

        start_time = time.time()
        try:
            if hasattr(self, '_execute_core_logic_async'):
                result = await self._execute_core_logic_async(normalized_request, **kwargs)
            else:
                result = await asyncio.to_thread(self._execute_core_logic, normalized_request, **kwargs)
            
            if self.quota_enabled and self._quota_manager:
                elapsed = time.time() - start_time
                if elapsed > 1.0:
                    self._check_execution_time(**kwargs)

            self._validate_result(result)
            
            if ctx:
                await ctx.report_progress(progress=total_steps, total=total_steps)
            
            return result
            
        except Exception as e:
            self.logger.error(f"Error in async execution of {self.tool_name}: {e}")
            raise

    async def sample_llm(
        self,
        prompt: str,
        ctx=None,
        system_prompt: Optional[str] = None,
        result_type: Optional[Type[BaseModel]] = None,
        temperature: float = 0.7
    ) -> Any:

        if ctx is not None:
            messages = prompt
            if system_prompt:
                messages = [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": prompt}
                ]

            result = await ctx.sample(
                messages=messages,
                result_type=result_type,
                temperature=temperature
            )
            
            # Estimer et enregistrer les tokens utilisés (approximation)
            # GPT-4 ~ 4 chars/token en moyenne
            total_chars = len(prompt)
            if system_prompt:
                total_chars += len(system_prompt)
            estimated_tokens = total_chars // 4
            self._record_llm_tokens(estimated_tokens)
            
            return result.result if result_type else (result.text or "")

        raise ToolExecutionError(
            "Aucun backend LLM disponible. Fournissez ctx (FastMCP)."
        )

    def get_info(self) -> Dict[str, Any]:
        """Retourne les informations détaillées du tool."""
        base_info = {
            "name": self.get_name(),
            "description": self.get_description(),
            "supported_languages": self.get_supported_languages(),
            "request_model": self.get_request_model().__name__,
            "response_model": self.get_response_model().__name__,
            "required_config": self.get_required_config_keys(),
            "usage_description": self.get_usage_description(),
            "parameters": self.get_parameters_info(),
            "examples": self.get_examples(),
            "capabilities": self.get_capabilities(),
            "rate_limiting": {
                "enabled": self.rate_limit_enabled,
            },
            "quotas": {
                "enabled": self.quota_enabled,
            },
        }
        
        # Ajouter les statistiques de rate limiting si disponibles
        if self.rate_limit_enabled:
            try:
                manager = get_rate_limiter_manager()
                stats = manager.get_stats(self.tool_name)
                if stats:
                    base_info["rate_limiting"]["stats"] = stats.get(self.tool_name)
            except Exception:
                pass
        
        return base_info

    def get_usage_description(self) -> str:
        return f"Outil {self.get_name()}: {self.get_description()}"

    def get_parameters_info(self) -> Dict[str, Any]:
        request_model = self.get_request_model()
        parameters = {}

        if hasattr(request_model, 'model_fields'):
            for field_name, field_info in request_model.model_fields.items():
                parameters[field_name] = {
                    "type": str(field_info.annotation),
                    "required": field_info.is_required(),
                    "description": getattr(field_info, 'description', None) or "Aucune description disponible",
                    "default": None if field_info.default is PydanticUndefined else field_info.default
                }

        return parameters

    def get_examples(self) -> List[Dict[str, Any]]:
        return [
            {
                "title": f"Exemple d'utilisation de {self.get_name()}",
                "description": "Exemple de base",
                "request": {"code": "print('Hello, World!')", "language": "python"},
                "expected_response": "Réponse selon la logique de l'outil"
            }
        ]
    
    def get_capabilities(self) -> Dict[str, Any]:
        """Retourne les capacités spéciales du tool."""
        return {
            "supports_streaming": False,
            "supports_cancellation": False,
            "requires_filesystem": False,
            "requires_network": False,
        }
