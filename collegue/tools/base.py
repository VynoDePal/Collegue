"""
Base Tool - Classe de base pour tous les outils du projet Collègue
"""
import logging
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, Type, Callable
from contextlib import asynccontextmanager
from pydantic import BaseModel, ValidationError
from pydantic_core import PydanticUndefined
from datetime import datetime
import asyncio


class ToolError(Exception):
    pass

class ToolValidationError(ToolError):
    pass

class ToolExecutionError(ToolError):
    pass

class ToolConfigurationError(ToolError):
    pass

class ToolMetrics(BaseModel):
    tool_name: str
    execution_time: float
    success: bool
    timestamp: datetime
    input_size: Optional[int] = None
    output_size: Optional[int] = None
    error_message: Optional[str] = None

class BaseTool(ABC):
    tool_name: str = ""
    tool_description: str = ""
    request_model: Optional[Type[BaseModel]] = None
    response_model: Optional[Type[BaseModel]] = None
    supported_languages: List[str] = ["python", "javascript", "typescript"]
    long_running: bool = False

    def __init__(self, config: Optional[Dict[str, Any]] = None, app_state: Optional[Dict[str, Any]] = None):
        self.config = config or {}
        self.app_state = app_state or {}
        self.logger = logging.getLogger(f"tools.{self.__class__.__name__}")
        self.metrics: List[ToolMetrics] = []

        self.prompt_engine = self.app_state.get('prompt_engine')
        self.llm_manager = self.app_state.get('llm_manager')
        self.context_manager = self.app_state.get('context_manager')

        self._setup_logging()
        self._validate_config()

    def _setup_logging(self):
        if not self.logger.handlers:
            handler = logging.StreamHandler()
            formatter = logging.Formatter(
                '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
            )
            handler.setFormatter(formatter)
            self.logger.addHandler(handler)
            self.logger.setLevel(self.config.get('log_level', logging.INFO))

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

    def validate_request(self, request: BaseModel) -> bool:
        try:
            expected_model = self.get_request_model()
            if not isinstance(request, expected_model):
                if hasattr(request, 'model_dump'):
                    request = expected_model(**request.model_dump())
                else:
                    raise ToolValidationError(
                        f"Type de requête invalide. Attendu: {expected_model.__name__}"
                    )

            if hasattr(request, 'language') and request.language:
                self.validate_language(request.language)

            return True

        except ValidationError as e:
            raise ToolValidationError(f"Validation de la requête échouée: {e}")

    @abstractmethod
    def _execute_core_logic(self, request: BaseModel, **kwargs) -> BaseModel:
        pass

    def _record_metrics(
        self, start_time: datetime, success: bool,
        request: Optional[BaseModel] = None, result: Optional[BaseModel] = None,
        error_message: Optional[str] = None
    ) -> float:
        execution_time = (datetime.now() - start_time).total_seconds()
        metrics = ToolMetrics(
            tool_name=self.get_name(),
            execution_time=execution_time,
            success=success,
            timestamp=start_time,
            input_size=len(str(request)) if request else None,
            output_size=len(str(result)) if result else None,
            error_message=error_message,
        )
        self.metrics.append(metrics)
        return execution_time

    def _validate_result(self, result: BaseModel) -> None:
        expected = self.get_response_model()
        if not isinstance(result, expected):
            raise ToolExecutionError(
                f"Type de réponse invalide. Attendu: {expected.__name__}"
            )

    def execute(self, request: BaseModel, **kwargs) -> BaseModel:
        start_time = datetime.now()
        tool_name = self.get_name()

        try:
            self.logger.info(f"Début d'exécution de {tool_name}")
            self.validate_request(request)
            result = self._execute_core_logic(request, **kwargs)
            self._validate_result(result)
            execution_time = self._record_metrics(start_time, True, request, result)
            self.logger.info(f"Exécution de {tool_name} réussie en {execution_time:.2f}s")
            return result

        except (ToolError, ValidationError) as e:
            self._record_metrics(start_time, False, error_message=str(e))
            self.logger.error(f"Erreur dans {tool_name}: {e}")
            raise

        except Exception as e:
            error_msg = f"Erreur inattendue: {e}"
            self._record_metrics(start_time, False, error_message=error_msg)
            self.logger.error(f"Erreur inattendue dans {tool_name}: {e}")
            raise ToolExecutionError(error_msg)

    async def execute_async(self, request: BaseModel, **kwargs) -> BaseModel:
        start_time = datetime.now()
        tool_name = self.get_name()
        ctx = kwargs.get('ctx')
        total_steps = 4

        try:
            self.logger.info(f"Début d'exécution async de {tool_name}")
            if ctx:
                await ctx.report_progress(progress=0, total=total_steps)
            self.validate_request(request)
            if ctx:
                await ctx.report_progress(progress=1, total=total_steps)

            if hasattr(self, '_execute_core_logic_async'):
                result = await self._execute_core_logic_async(request, **kwargs)
            else:
                result = await asyncio.to_thread(self._execute_core_logic, request, **kwargs)

            if ctx:
                await ctx.report_progress(progress=3, total=total_steps)
            self._validate_result(result)
            execution_time = self._record_metrics(start_time, True, request, result)
            if ctx:
                await ctx.report_progress(progress=total_steps, total=total_steps)
            self.logger.info(f"Exécution async de {tool_name} réussie en {execution_time:.2f}s")
            return result

        except (ToolError, ValidationError) as e:
            self._record_metrics(start_time, False, error_message=str(e))
            self.logger.error(f"Erreur dans {tool_name}: {e}")
            raise

        except Exception as e:
            error_msg = f"Erreur inattendue: {e}"
            self._record_metrics(start_time, False, error_message=error_msg)
            self.logger.error(f"Erreur inattendue dans {tool_name}: {e}")
            raise ToolExecutionError(error_msg)

    async def sample_llm(
        self,
        prompt: str,
        ctx=None,
        llm_manager=None,
        system_prompt: Optional[str] = None,
        result_type: Optional[Type[BaseModel]] = None,
        temperature: float = 0.7
    ) -> Any:

        if ctx is not None:
            try:
                self.logger.debug(f"Utilisation de ctx.sample() pour {self.get_name()}")


                messages = prompt
                if system_prompt:
                    messages = [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": prompt}
                    ]

                if result_type:
                    result = await ctx.sample(
                        messages=messages,
                        result_type=result_type,
                        temperature=temperature
                    )
                    return result.result
                else:
                    result = await ctx.sample(
                        messages=messages,
                        temperature=temperature
                    )
                    return result.text or ""

            except Exception as e:
                self.logger.warning(f"ctx.sample() a échoué, fallback vers llm_manager: {e}")

        if llm_manager is not None:
            self.logger.debug(f"Utilisation de ToolLLMManager pour {self.get_name()}")
            text = await llm_manager.async_generate(prompt, system_prompt)


            if result_type:
                import json
                try:
                    data = json.loads(text)
                    return result_type(**data)
                except (json.JSONDecodeError, ValidationError) as e:
                    self.logger.warning(f"Impossible de parser en {result_type.__name__}: {e}")
                    return text
            return text

        raise ToolExecutionError(
            "Aucun backend LLM disponible. Fournissez ctx (FastMCP) ou llm_manager."
        )

    def get_metrics(self) -> List[ToolMetrics]:
        return self.metrics.copy()

    def clear_metrics(self):
        self.metrics.clear()

    def get_info(self) -> Dict[str, Any]:
        return {
            "name": self.get_name(),
            "description": self.get_description(),
            "supported_languages": self.get_supported_languages(),
            "request_model": self.get_request_model().__name__,
            "response_model": self.get_response_model().__name__,
            "required_config": self.get_required_config_keys(),
            "metrics_count": len(self.metrics),
            "success_rate": self._calculate_success_rate(),

            "usage_description": self.get_usage_description(),
            "parameters": self.get_parameters_info(),
            "examples": self.get_examples(),
            "capabilities": self.get_capabilities()
        }

    def _calculate_success_rate(self) -> float:
        """Calcule le taux de succès de l'outil."""
        if not self.metrics:
            return 0.0

        successful = sum(1 for m in self.metrics if m.success)
        return (successful / len(self.metrics)) * 100.0

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

    async def execute_with_llm_fallback(
        self,
        request: BaseModel,
        llm_manager: Optional[Any],
        context_builder: Callable[[BaseModel], Dict[str, Any]],
        llm_processor: Callable[[str], BaseModel],
        fallback: Callable[[BaseModel], BaseModel],
        ctx: Optional[Any] = None,
        timeout: int = 30
    ) -> BaseModel:
        """Execute with LLM fallback pattern - standardise la logique LLM + fallback local.

        Pattern utilisé par 6 tools: documentation, refactoring, test_generation,
        repo_consistency_check, iac_guardrails_scan, impact_analysis.

        Args:
            request: La requête à traiter
            llm_manager: Le gestionnaire LLM (peut être None)
            context_builder: Fonction pour construire le contexte du prompt
            llm_processor: Fonction pour traiter la réponse LLM et créer le résultat
            fallback: Fonction de fallback appelée si LLM échoue ou est indisponible
            ctx: Contexte de progression (optionnel)
            timeout: Timeout en secondes pour l'appel LLM

        Returns:
            Le résultat du traitement (LLM ou fallback)
        """
        if llm_manager is None:
            self.logger.debug("LLM manager non disponible, utilisation du fallback")
            return fallback(request)

        try:
            if ctx:
                await ctx.info("Préparation du prompt...")

            context = context_builder(request)

            if asyncio.iscoroutinefunction(self.prepare_prompt):
                prompt = await self.prepare_prompt(request, context=context)
            else:
                prompt = self.prepare_prompt(request, context=context)

            if ctx:
                await ctx.info("Génération via LLM...")

            raw_response = llm_manager.sync_generate(prompt)
            result = llm_processor(raw_response)

            if ctx:
                await ctx.info("Traitement terminé")

            return result

        except Exception as e:
            self.logger.warning(f"Erreur LLM, fallback local: {e}")
            if ctx:
                await ctx.info(f"Fallback: {str(e)[:50]}")
            return fallback(request)

    @asynccontextmanager
    async def progress_tracker(self, ctx: Optional[Any], total_steps: int):
        """Context manager pour le suivi de progression dans les outils async.

        Usage:
            async with self.progress_tracker(ctx, 4) as progress:
                # step 1
                progress.step = 1
                # step 2
                progress.step = 2
        """
        class Progress:
            def __init__(self, tool, ctx, total):
                self.tool = tool
                self.ctx = ctx
                self.total = total
                self._step = 0

            @property
            def step(self):
                return self._step

            @step.setter
            def step(self, value):
                self._step = value
                if self.ctx:
                    asyncio.create_task(
                        self.ctx.report_progress(progress=value, total=self.total)
                    )

        progress = Progress(self, ctx, total_steps)
        if ctx:
            await ctx.report_progress(progress=0, total=total_steps)
        yield progress
        if ctx:
            await ctx.report_progress(progress=total_steps, total=total_steps)
