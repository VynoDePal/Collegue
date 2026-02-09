"""
Base Tool - Classe de base pour tous les outils du projet Collègue
"""
import logging
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, Type
from pydantic import BaseModel, ValidationError
from pydantic_core import PydanticUndefined
from datetime import datetime
import inspect
import functools
import asyncio


class ToolError(Exception):
    """Exception de base pour les outils."""
    pass


class ToolValidationError(ToolError):
    """Erreur de validation des inputs d'un outil."""
    pass


class ToolExecutionError(ToolError):
    """Erreur d'exécution d'un outil."""
    pass


class ToolConfigurationError(ToolError):
    """Erreur de configuration d'un outil."""
    pass


class ToolMetrics(BaseModel):
    """Métriques d'exécution d'un outil."""
    tool_name: str
    execution_time: float
    success: bool
    timestamp: datetime
    input_size: Optional[int] = None
    output_size: Optional[int] = None
    error_message: Optional[str] = None


class BaseTool(ABC):
    """
    Classe de base abstraite pour tous les outils.

    Fournit une interface commune et des fonctionnalités de base :
    - Validation des inputs
    - Gestion d'erreurs standardisée
    - Logging unifié
    - Collecte de métriques
    - Configuration
    """

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
        """Configure le logging pour l'outil."""
        if not self.logger.handlers:
            handler = logging.StreamHandler()
            formatter = logging.Formatter(
                '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
            )
            handler.setFormatter(formatter)
            self.logger.addHandler(handler)
            self.logger.setLevel(self.config.get('log_level', logging.INFO))

    def _validate_config(self):
        """Valide la configuration de l'outil."""
        required_configs = self.get_required_config_keys()
        missing_keys = [key for key in required_configs if key not in self.config]

        if missing_keys:
            raise ToolConfigurationError(
                f"Configuration manquante pour {self.__class__.__name__}: {missing_keys}"
            )

    @abstractmethod
    def get_name(self) -> str:
        """Retourne le nom unique de l'outil."""
        pass

    @abstractmethod
    def get_description(self) -> str:
        """Retourne la description de l'outil."""
        pass

    @abstractmethod
    def get_request_model(self) -> Type[BaseModel]:
        """Retourne le modèle Pydantic pour les requêtes."""
        pass

    @abstractmethod
    def get_response_model(self) -> Type[BaseModel]:
        """Retourne le modèle Pydantic pour les réponses."""
        pass

    def get_required_config_keys(self) -> List[str]:
        """
        Retourne les clés de configuration requises.

        Returns:
            Liste des clés de configuration obligatoires
        """
        return []

    def get_supported_languages(self) -> List[str]:
        """
        Retourne la liste des langages supportés par l'outil.

        Returns:
            Liste des langages supportés
        """
        return ["python", "javascript", "typescript"]

    def is_long_running(self) -> bool:
        """
        Indique si l'outil est susceptible de prendre du temps (> 10s).
        Les outils longs seront exécutés comme tâches de fond avec FastMCP task=True.

        Returns:
            True si l'outil est long-running, False sinon
        """
        return False

    async def prepare_prompt(self, request: BaseModel, template_name: Optional[str] = None) -> str:
        """
        Prépare un prompt optimisé en utilisant le système de prompts amélioré.

        Args:
            request: Requête contenant les variables pour le prompt
            template_name: Nom du template à utiliser (par défaut: nom de l'outil)

        Returns:
            Prompt formaté et optimisé

        Raises:
            ToolExecutionError: Si le prompt engine n'est pas disponible
        """
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
        """
        Valide qu'un langage est supporté par l'outil.

        Args:
            language: Langage à valider

        Returns:
            True si le langage est supporté

        Raises:
            ToolValidationError: Si le langage n'est pas supporté
        """
        supported = self.get_supported_languages()
        if language.lower() not in [lang.lower() for lang in supported]:
            raise ToolValidationError(
                f"Langage '{language}' non supporté. Langages supportés: {supported}"
            )
        return True

    def validate_request(self, request: BaseModel) -> bool:
        """
        Valide une requête selon le modèle de l'outil.

        Args:
            request: Requête à valider

        Returns:
            True si la requête est valide

        Raises:
            ToolValidationError: Si la validation échoue
        """
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
        """
        Exécute la logique principale de l'outil.

        Args:
            request: Requête validée
            **kwargs: Arguments supplémentaires (parser, llm_manager, etc.)

        Returns:
            Réponse de l'outil
        """
        pass

    def execute(self, request: BaseModel, **kwargs) -> BaseModel:
        """
        Exécute l'outil avec gestion complète des erreurs et métriques.

        Args:
            request: Requête à traiter
            **kwargs: Arguments supplémentaires

        Returns:
            Réponse de l'outil

        Raises:
            ToolError: En cas d'erreur d'exécution
        """
        start_time = datetime.now()
        tool_name = self.get_name()

        try:
            self.logger.info(f"Début d'exécution de {tool_name}")
            self.validate_request(request)
            result = self._execute_core_logic(request, **kwargs)
            expected_response = self.get_response_model()
            if not isinstance(result, expected_response):
                raise ToolExecutionError(
                    f"Type de réponse invalide. Attendu: {expected_response.__name__}"
                )

            execution_time = (datetime.now() - start_time).total_seconds()
            metrics = ToolMetrics(
                tool_name=tool_name,
                execution_time=execution_time,
                success=True,
                timestamp=start_time,
                input_size=len(str(request)) if hasattr(request, '__str__') else None,
                output_size=len(str(result)) if hasattr(result, '__str__') else None
            )
            self.metrics.append(metrics)

            self.logger.info(f"Exécution de {tool_name} réussie en {execution_time:.2f}s")
            return result

        except (ToolError, ValidationError) as e:
            execution_time = (datetime.now() - start_time).total_seconds()
            error_msg = str(e)
            metrics = ToolMetrics(
                tool_name=tool_name,
                execution_time=execution_time,
                success=False,
                timestamp=start_time,
                error_message=error_msg
            )
            self.metrics.append(metrics)

            self.logger.error(f"Erreur dans {tool_name}: {error_msg}")
            raise

        except Exception as e:
            execution_time = (datetime.now() - start_time).total_seconds()
            error_msg = f"Erreur inattendue: {str(e)}"
            metrics = ToolMetrics(
                tool_name=tool_name,
                execution_time=execution_time,
                success=False,
                timestamp=start_time,
                error_message=error_msg
            )
            self.metrics.append(metrics)

            self.logger.error(f"Erreur inattendue dans {tool_name}: {str(e)}")
            raise ToolExecutionError(error_msg)

    async def execute_async(self, request: BaseModel, **kwargs) -> BaseModel:
        """
        Exécute l'outil de manière asynchrone avec support Context (FastMCP 2.14+).

        Cette méthode est utilisée pour les outils long-running avec task=True.
        Elle supporte:
        - ctx.report_progress(): Reporting de progression via Context
        - ctx.sample(): Appels LLM avec structured output

        Args:
            request: Requête à traiter
            **kwargs: Arguments supplémentaires incluant:
                - ctx: Context FastMCP pour ctx.sample() et ctx.report_progress()
                - parser, llm_manager, context_manager: Services standard

        Returns:
            Réponse de l'outil
        """
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
            expected_response = self.get_response_model()
            if not isinstance(result, expected_response):
                raise ToolExecutionError(
                    f"Type de réponse invalide. Attendu: {expected_response.__name__}"
                )

            execution_time = (datetime.now() - start_time).total_seconds()
            metrics = ToolMetrics(
                tool_name=tool_name,
                execution_time=execution_time,
                success=True,
                timestamp=start_time,
                input_size=len(str(request)) if hasattr(request, '__str__') else None,
                output_size=len(str(result)) if hasattr(result, '__str__') else None
            )
            self.metrics.append(metrics)

            if ctx:
                await ctx.report_progress(progress=total_steps, total=total_steps)

            self.logger.info(f"Exécution async de {tool_name} réussie en {execution_time:.2f}s")
            return result

        except (ToolError, ValidationError) as e:
            execution_time = (datetime.now() - start_time).total_seconds()
            error_msg = str(e)

            metrics = ToolMetrics(
                tool_name=tool_name,
                execution_time=execution_time,
                success=False,
                timestamp=start_time,
                error_message=error_msg
            )
            self.metrics.append(metrics)

            self.logger.error(f"Erreur dans {tool_name}: {error_msg}")
            raise

        except Exception as e:
            execution_time = (datetime.now() - start_time).total_seconds()
            error_msg = f"Erreur inattendue: {str(e)}"

            metrics = ToolMetrics(
                tool_name=tool_name,
                execution_time=execution_time,
                success=False,
                timestamp=start_time,
                error_message=error_msg
            )
            self.metrics.append(metrics)

            self.logger.error(f"Erreur inattendue dans {tool_name}: {str(e)}")
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
        """
        Appelle le LLM via ctx.sample() (FastMCP 2.14+) ou fallback vers ToolLLMManager.

        Cette méthode permet d'utiliser le nouveau ctx.sample() de FastMCP tout en
        conservant la compatibilité avec ToolLLMManager pour les environnements
        qui ne supportent pas le sampling MCP.

        Args:
            prompt: Le prompt à envoyer au LLM
            ctx: Context FastMCP (si disponible, utilise ctx.sample())
            llm_manager: ToolLLMManager (fallback si ctx non disponible)
            system_prompt: Message système optionnel
            result_type: Type Pydantic pour structured output (FastMCP 2.14.1+)
            temperature: Température pour la génération

        Returns:
            - Si result_type: Instance validée du modèle Pydantic
            - Sinon: Texte brut de la réponse LLM
        """

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
        """
        Retourne les métriques collectées par l'outil.

        Returns:
            Liste des métriques d'exécution
        """
        return self.metrics.copy()

    def clear_metrics(self):
        """Efface les métriques collectées."""
        self.metrics.clear()

    def get_info(self) -> Dict[str, Any]:
        """
        Retourne les informations détaillées sur l'outil.

        Returns:
            Dictionnaire avec les informations complètes de l'outil
        """
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
            "capabilities": self.get_capabilities(),
            "limitations": self.get_limitations(),
            "best_practices": self.get_best_practices()
        }

    def _calculate_success_rate(self) -> float:
        """Calcule le taux de succès de l'outil."""
        if not self.metrics:
            return 0.0

        successful = sum(1 for m in self.metrics if m.success)
        return (successful / len(self.metrics)) * 100.0

    def get_usage_description(self) -> str:
        """
        Retourne une description détaillée de l'utilisation de l'outil.

        Returns:
            Description détaillée de l'utilisation
        """
        return f"Outil {self.get_name()}: {self.get_description()}"

    def get_parameters_info(self) -> Dict[str, Any]:
        """
        Retourne les informations détaillées sur les paramètres de l'outil.

        Returns:
            Dictionnaire avec les informations sur chaque paramètre
        """
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
        """
        Retourne des exemples d'utilisation de l'outil.

        Returns:
            Liste d'exemples avec requête et réponse attendue
        """
        return [
            {
                "title": f"Exemple d'utilisation de {self.get_name()}",
                "description": "Exemple de base",
                "request": {"code": "print('Hello, World!')", "language": "python"},
                "expected_response": "Réponse selon la logique de l'outil"
            }
        ]

    def get_capabilities(self) -> List[str]:
        """
        Retourne la liste des capacités de l'outil.

        Returns:
            Liste des capacités
        """
        return [
            f"Traitement de code en {', '.join(self.get_supported_languages())}",
            "Validation des entrées",
            "Collecte de métriques",
            "Gestion d'erreurs"
        ]

    def get_limitations(self) -> List[str]:
        """
        Retourne la liste des limitations de l'outil.

        Returns:
            Liste des limitations
        """
        return [
            "Dépend de la qualité du code fourni",
            "Performance variable selon la complexité",
            "Nécessite une configuration appropriée"
        ]

    def get_best_practices(self) -> List[str]:
        """
        Retourne la liste des bonnes pratiques pour utiliser l'outil.

        Returns:
            Liste des bonnes pratiques
        """
        return [
            "Fournir du code bien formaté",
            "Spécifier le langage de programmation",
            "Utiliser un identifiant de session pour le suivi",
            "Vérifier les métriques régulièrement"
        ]


def tool_method(func):
    """
    Décorateur pour marquer une méthode comme endpoint d'outil.

    Ajoute automatiquement la validation et la gestion d'erreurs.
    """
    @functools.wraps(func)
    def wrapper(self, *args, **kwargs):
        if not isinstance(self, BaseTool):
            raise TypeError("tool_method ne peut être utilisé que sur des classes BaseTool")


        sig = inspect.signature(func)
        bound = sig.bind(self, *args, **kwargs)
        bound.apply_defaults()


        args_list = list(bound.arguments.values())[1:]
        if args_list:
            request = args_list[0]
            remaining_kwargs = dict(bound.arguments)
            del remaining_kwargs[list(bound.arguments.keys())[0]]
            del remaining_kwargs[list(bound.arguments.keys())[1]]

            return self.execute(request, **remaining_kwargs)
        else:
            return func(self, *args, **kwargs)

    return wrapper
