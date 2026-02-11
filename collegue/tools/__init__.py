"""
Tools Package - Enregistrement direct des outils avec FastMCP
"""
import os
import importlib
import inspect
from typing import Dict, List, Type, Any

try:
    from fastmcp import FastMCP, Context
except ImportError:
    FastMCP = Any
    Context = Any

from .base import BaseTool


def register_tools(app: FastMCP):
    """
    Enregistre tous les tools BaseTool avec FastMCP.
    Les composants partagés sont accessibles via ctx.lifespan_context à runtime.
    """
    tools = _discover_tools()
    
    for tool_class in tools:
        try:
            tool_instance = tool_class({})
            _register_tool_with_fastmcp(app, tool_instance)
            print(f"Outil '{tool_class.__name__}' enregistré avec succès")
        except Exception as e:
            print(f"Erreur lors de l'enregistrement de '{tool_class.__name__}': {e}")


def _discover_tools() -> List[Type[BaseTool]]:
    """
    Découvre automatiquement toutes les classes qui héritent de BaseTool
    """
    tools = []
    current_dir = os.path.dirname(__file__)
    
    for filename in os.listdir(current_dir):
        if (filename.endswith('.py') and
            not filename.startswith('_') and
            filename != 'base.py'):
            
            module_name = filename[:-3]
            try:
                module = importlib.import_module(f'collegue.tools.{module_name}')
                
                for name, obj in inspect.getmembers(module):
                    if (inspect.isclass(obj) and
                        issubclass(obj, BaseTool) and
                        obj != BaseTool and
                        obj.__module__ == module.__name__):
                        tools.append(obj)
                        
            except ImportError as e:
                print(f"Erreur lors de l'import de {module_name}: {e}")
    
    return tools


def _register_tool_with_fastmcp(app: FastMCP, tool: BaseTool):
    """
    Enregistre un tool avec FastMCP.
    Les composants partagés sont lus depuis ctx.lifespan_context à runtime.
    """
    tool_name = tool.get_name()
    request_model = tool.get_request_model()
    response_model = tool.get_response_model()
    is_long_running = tool.is_long_running()
    
    decorator_kwargs = {
        "name": tool_name,
        "description": tool.get_description(),
    }
    
    if tool.tags:
        decorator_kwargs["tags"] = tool.tags
    
    if is_long_running:
        decorator_kwargs["task"] = True
    
    @app.tool(**decorator_kwargs)
    async def tool_endpoint(
        request: request_model,
        ctx: Context
    ) -> response_model:
        try:
            lc = ctx.lifespan_context or {}
            kwargs = {
                "parser": lc.get('parser'),
                "llm_manager": lc.get('llm_manager'),
                "context_manager": lc.get('context_manager'),
                "ctx": ctx,
            }
            
            if is_long_running:
                return await tool.execute_async(request, **kwargs)
            else:
                return tool.execute(request, **kwargs)
                
        except Exception as e:
            tool.logger.error(f"Erreur dans l'endpoint {tool_name}: {e}")
            raise


def get_tool_info() -> Dict[str, Dict[str, Any]]:
    """
    Retourne les informations sur tous les tools découverts
    """
    tools_info = {}
    tools = _discover_tools()
    
    for tool_class in tools:
        try:
            temp_instance = tool_class({})
            tools_info[tool_class.__name__] = temp_instance.get_info()
        except Exception as e:
            tools_info[tool_class.__name__] = {
                "name": tool_class.__name__,
                "error": f"Impossible de récupérer les infos: {e}"
            }
    
    return tools_info
