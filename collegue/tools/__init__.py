"""
Tools Package - Système d'enregistrement centralisé des outils
"""
import os
import importlib
import inspect
from typing import Dict, List, Type, Any, Optional
from fastmcp import FastMCP
from pydantic import BaseModel
from .base import BaseTool


class ToolInfoResponse(BaseModel):
    """Modèle de réponse pour les informations détaillées d'un outil."""
    name: str
    description: str
    supported_languages: List[str]
    request_model: str
    response_model: str
    required_config: List[str]
    metrics_count: int
    success_rate: float
    # Nouvelles informations détaillées
    usage_description: str
    parameters: Dict[str, Any]
    examples: List[Dict[str, Any]]
    capabilities: List[str]
    limitations: Optional[List[str]] = None
    best_practices: Optional[List[str]] = None


class ToolMetricsResponse(BaseModel):
    """Modèle de réponse pour les métriques d'un outil."""
    tool_name: str
    total_executions: int
    success_rate: float
    average_execution_time: float
    recent_metrics: List[dict]


class ToolRegistry:
    """Registry centralisé pour tous les outils."""

    def __init__(self):
        self._tools: Dict[str, Type[BaseTool]] = {}
        self._instances: Dict[str, BaseTool] = {}
        self._auto_discover_tools()

    def _auto_discover_tools(self):
        """Auto-découverte des outils dans le package."""
        current_dir = os.path.dirname(__file__)

        for filename in os.listdir(current_dir):
            if (filename.endswith('.py') and
                not filename.startswith('__') and
                filename != 'base.py'):

                module_name = filename[:-3]
                try:
                    module = importlib.import_module(f'collegue.tools.{module_name}')

                    # Recherche des classes qui héritent de BaseTool
                    for name, obj in inspect.getmembers(module):
                        if (inspect.isclass(obj) and
                            issubclass(obj, BaseTool) and
                            obj != BaseTool):
                            self._tools[obj.__name__] = obj

                except ImportError as e:
                    print(f"Erreur lors de l'import de {module_name}: {e}")

    def register_tool(self, tool_class: Type[BaseTool], config: Dict[str, Any] = None):
        """
        Enregistre manuellement un outil.

        Args:
            tool_class: Classe de l'outil à enregistrer
            config: Configuration pour l'outil
        """
        if not issubclass(tool_class, BaseTool):
            raise ValueError(f"{tool_class.__name__} doit hériter de BaseTool")

        self._tools[tool_class.__name__] = tool_class

        # Créer une instance si une configuration est fournie
        if config is not None:
            self._instances[tool_class.__name__] = tool_class(config)

    def get_tool_class(self, name: str) -> Type[BaseTool]:
        """Récupère une classe d'outil par son nom."""
        return self._tools.get(name)

    def get_tool_instance(self, name: str, config: Dict[str, Any] = None) -> BaseTool:
        """
        Récupère ou crée une instance d'outil.

        Args:
            name: Nom de l'outil
            config: Configuration pour l'outil

        Returns:
            Instance de l'outil
        """
        if name in self._instances and config is None:
            return self._instances[name]

        tool_class = self.get_tool_class(name)
        if tool_class is None:
            raise ValueError(f"Outil '{name}' non trouvé")

        instance = tool_class(config)
        if config is None:
            self._instances[name] = instance

        return instance

    def list_tools(self) -> List[str]:
        """Retourne la liste des outils disponibles."""
        return list(self._tools.keys())

    def get_tools_info(self) -> Dict[str, Dict[str, Any]]:
        """Retourne les informations de tous les outils."""
        info = {}
        for name, tool_class in self._tools.items():
            try:
                # Créer une instance temporaire pour récupérer les infos
                temp_instance = tool_class({})
                info[name] = temp_instance.get_info()
            except Exception as e:
                info[name] = {
                    "name": name,
                    "error": f"Impossible de récupérer les infos: {e}"
                }
        return info


# Instance globale du registry
_registry = ToolRegistry()


def get_registry() -> ToolRegistry:
    """Retourne l'instance globale du registry."""
    return _registry


def register_tools(app: FastMCP, app_state: dict):
    """
    Enregistre tous les outils découverts dans l'application FastMCP.

    Args:
        app: Instance FastMCP
        app_state: État de l'application
    """
    registry = get_registry()

    # Configuration par défaut pour les outils
    default_config = app_state.get('tools_config', {})

    for tool_name in registry.list_tools():
        try:
            tool_class = registry.get_tool_class(tool_name)

            # Configuration spécifique à l'outil
            tool_config = default_config.get(tool_name.lower(), {})

            # Créer une instance avec la configuration
            tool_instance = registry.get_tool_instance(tool_name, tool_config)

            # Enregistrer les endpoints de l'outil dans FastMCP
            _register_tool_endpoints(app, tool_instance, app_state)

            print(f"Outil '{tool_name}' enregistré avec succès")

        except Exception as e:
            print(f"Erreur lors de l'enregistrement de '{tool_name}': {e}")


def _register_tool_endpoints(app: FastMCP, tool: BaseTool, app_state: dict):
    """
    Enregistre les endpoints d'un outil dans FastMCP.

    Args:
        app: Instance FastMCP
        tool: Instance de l'outil
        app_state: État de l'application
    """
    tool_name = tool.get_name()
    request_model = tool.get_request_model()
    response_model = tool.get_response_model()

    # Endpoint principal de l'outil
    @app.tool(name=tool_name, description=tool.get_description())
    def tool_endpoint(request: request_model) -> response_model:
        """Endpoint généré automatiquement pour l'outil."""
        try:
            # Récupération des services depuis app_state
            parser = app_state.get('parser')
            llm_manager = app_state.get('llm_manager')
            context_manager = app_state.get('context_manager')

            # Exécution de l'outil avec les services
            return tool.execute(
                request,
                parser=parser,
                llm_manager=llm_manager,
                context_manager=context_manager
            )

        except Exception as e:
            # Log de l'erreur et re-raise
            tool.logger.error(f"Erreur dans l'endpoint {tool_name}: {e}")
            raise

    # Endpoint d'information sur l'outil
    @app.tool(name=f"{tool_name}_info", description=f"Informations sur l'outil {tool_name}")
    def tool_info_endpoint() -> ToolInfoResponse:
        """Endpoint d'information généré automatiquement."""
        try:
            info = tool.get_info()
            return ToolInfoResponse(**info)
        except Exception as e:
            tool.logger.error(f"Erreur dans l'endpoint info {tool_name}: {e}")
            raise

    # Endpoint de métriques
    @app.tool(name=f"{tool_name}_metrics", description=f"Métriques de l'outil {tool_name}")
    def tool_metrics_endpoint() -> ToolMetricsResponse:
        """Endpoint de métriques généré automatiquement."""
        try:
            metrics = tool.get_metrics()
            return ToolMetricsResponse(
                tool_name=tool_name,
                total_executions=len(metrics),
                success_rate=tool._calculate_success_rate(),
                average_execution_time=sum(m.execution_time for m in metrics) / len(metrics) if metrics else 0,
                recent_metrics=[m.model_dump() for m in metrics[-10:]]  # 10 dernières métriques
            )
        except Exception as e:
            tool.logger.error(f"Erreur dans l'endpoint metrics {tool_name}: {e}")
            raise
