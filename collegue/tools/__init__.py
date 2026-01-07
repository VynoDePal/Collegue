"""
Tools Package - Système d'enregistrement centralisé des outils
"""
import os
import importlib
import inspect
from typing import Dict, List, Type, Any, Optional
from fastmcp import FastMCP, Context
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

    default_config = app_state.get('tools_config', {})

    for tool_name in registry.list_tools():
        try:
            tool_class = registry.get_tool_class(tool_name)
            tool_config = default_config.get(tool_name.lower(), {})
            tool_instance = registry.get_tool_instance(tool_name, tool_config)
            _register_tool_endpoints(app, tool_instance, app_state)

            print(f"Outil '{tool_name}' enregistré avec succès")

        except Exception as e:
            print(f"Erreur lors de l'enregistrement de '{tool_name}': {e}")
    
    register_admin_tool(app, app_state)


def _register_tool_endpoints(app: FastMCP, tool: BaseTool, app_state: dict):
    """
    Enregistre les endpoints d'un outil dans FastMCP.
    
    Note: Les endpoints _info et _metrics ont été supprimés pour simplifier l'interface.
    Utilisez l'outil collegue_admin pour obtenir les infos et métriques de tous les outils.
    
    Les outils longs (is_long_running=True) sont enregistrés avec task=True pour
    s'exécuter en tâche de fond avec support de progression (FastMCP v2.14+).

    Args:
        app: Instance FastMCP
        tool: Instance de l'outil
        app_state: État de l'application
    """
    tool_name = tool.get_name()
    request_model = tool.get_request_model()
    response_model = tool.get_response_model()
    is_long_running = tool.is_long_running()

    if is_long_running:
        @app.tool(name=tool_name, description=tool.get_description(), task=True)
        async def tool_endpoint_async(
            request: request_model,
            ctx: Context
        ) -> response_model:
            """Endpoint async généré automatiquement pour l'outil long-running."""
            try:
                parser = app_state.get('parser')
                llm_manager = app_state.get('llm_manager')
                context_manager = app_state.get('context_manager')
                return await tool.execute_async(
                    request,
                    parser=parser,
                    llm_manager=llm_manager,
                    context_manager=context_manager,
                    ctx=ctx
                )

            except Exception as e:
                tool.logger.error(f"Erreur dans l'endpoint async {tool_name}: {e}")
                raise
    else:
        @app.tool(name=tool_name, description=tool.get_description())
        async def tool_endpoint(request: request_model, ctx: Context) -> response_model:
            """Endpoint généré automatiquement pour l'outil."""
            try:
                parser = app_state.get('parser')
                llm_manager = app_state.get('llm_manager')
                context_manager = app_state.get('context_manager')
                return tool.execute(
                    request,
                    parser=parser,
                    llm_manager=llm_manager,
                    context_manager=context_manager,
                    ctx=ctx
                )

            except Exception as e:
                tool.logger.error(f"Erreur dans l'endpoint {tool_name}: {e}")
                raise


class AdminRequest(BaseModel):
    """Modèle de requête pour l'outil d'administration."""
    action: str  # "list", "info", "metrics", "all_info", "all_metrics"
    tool_name: Optional[str] = None  # Requis pour "info" et "metrics"


class AdminResponse(BaseModel):
    """Modèle de réponse pour l'outil d'administration."""
    success: bool
    action: str
    data: Dict[str, Any]
    message: Optional[str] = None


def register_admin_tool(app: FastMCP, app_state: dict):
    """
    Enregistre l'outil d'administration centralisé collegue_admin.
    Remplace les 10 outils _info et _metrics par un seul outil unifié.
    
    Args:
        app: Instance FastMCP
        app_state: État de l'application
    """
    registry = get_registry()
    
    @app.tool(
        name="collegue_admin",
        description="Outil d'administration pour obtenir les informations et métriques de tous les outils Collègue. Actions: list, info, metrics, all_info, all_metrics"
    )
    def collegue_admin(request: AdminRequest) -> AdminResponse:
        """Outil d'administration centralisé pour Collègue."""
        action = request.action.lower()
        tool_name = request.tool_name
        
        try:
            if action == "list":
                tools = registry.list_tools()
                tool_names = []
                for t in tools:
                    try:
                        instance = registry.get_tool_instance(t)
                        tool_names.append(instance.get_name())
                    except:
                        tool_names.append(t)
                return AdminResponse(
                    success=True,
                    action=action,
                    data={"tools": tool_names, "count": len(tool_names)}
                )
            
            elif action == "info":
                if not tool_name:
                    return AdminResponse(
                        success=False,
                        action=action,
                        data={},
                        message="tool_name requis pour l'action 'info'"
                    )
                
                tool_instance = None
                for t in registry.list_tools():
                    try:
                        instance = registry.get_tool_instance(t)
                        if instance.get_name() == tool_name:
                            tool_instance = instance
                            break
                    except:
                        pass
                
                if not tool_instance:
                    return AdminResponse(
                        success=False,
                        action=action,
                        data={},
                        message=f"Outil '{tool_name}' non trouvé"
                    )
                
                info = tool_instance.get_info()
                return AdminResponse(
                    success=True,
                    action=action,
                    data=info
                )
            
            elif action == "metrics":
                if not tool_name:
                    return AdminResponse(
                        success=False,
                        action=action,
                        data={},
                        message="tool_name requis pour l'action 'metrics'"
                    )
                
                tool_instance = None
                for t in registry.list_tools():
                    try:
                        instance = registry.get_tool_instance(t)
                        if instance.get_name() == tool_name:
                            tool_instance = instance
                            break
                    except:
                        pass
                
                if not tool_instance:
                    return AdminResponse(
                        success=False,
                        action=action,
                        data={},
                        message=f"Outil '{tool_name}' non trouvé"
                    )
                
                metrics = tool_instance.get_metrics()
                return AdminResponse(
                    success=True,
                    action=action,
                    data={
                        "tool_name": tool_name,
                        "total_executions": len(metrics),
                        "success_rate": tool_instance._calculate_success_rate(),
                        "average_execution_time": sum(m.execution_time for m in metrics) / len(metrics) if metrics else 0,
                        "recent_metrics": [m.model_dump() for m in metrics[-10:]]
                    }
                )
            
            elif action == "all_info":
                all_info = registry.get_tools_info()
                return AdminResponse(
                    success=True,
                    action=action,
                    data={"tools": all_info, "count": len(all_info)}
                )
            
            elif action == "all_metrics":
                all_metrics = {}
                for t in registry.list_tools():
                    try:
                        instance = registry.get_tool_instance(t)
                        metrics = instance.get_metrics()
                        all_metrics[instance.get_name()] = {
                            "total_executions": len(metrics),
                            "success_rate": instance._calculate_success_rate(),
                            "average_execution_time": sum(m.execution_time for m in metrics) / len(metrics) if metrics else 0
                        }
                    except Exception as e:
                        all_metrics[t] = {"error": str(e)}
                
                return AdminResponse(
                    success=True,
                    action=action,
                    data={"metrics": all_metrics, "count": len(all_metrics)}
                )
            
            else:
                return AdminResponse(
                    success=False,
                    action=action,
                    data={},
                    message=f"Action '{action}' non reconnue. Actions valides: list, info, metrics, all_info, all_metrics"
                )
        
        except Exception as e:
            return AdminResponse(
                success=False,
                action=action,
                data={},
                message=f"Erreur: {str(e)}"
            )
    
    print("Outil 'collegue_admin' enregistré avec succès")
