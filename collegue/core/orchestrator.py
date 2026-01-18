"""
Tool Orchestrator - Coordination des outils du MCP
"""
import inspect
import asyncio
from typing import Dict, List, Any, Optional, Callable, Union, Set, Tuple

class ToolOrchestrator:
    """
    Coordonne l'exécution des différents outils du MCP en fonction du contexte
    et de l'intention de l'utilisateur.
    """
    
    def __init__(self, context_manager=None, app_state=None):
        """
        Initialise un nouvel orchestrateur d'outils.
        
        Args:
            context_manager: Gestionnaire de contexte (optionnel)
            app_state: État global de l'application (optionnel)
        """
        self.tools = {}  # Dictionnaire des outils disponibles
        self.tool_dependencies = {}  # Dépendances entre outils
        self.execution_history = []  # Historique d'exécution des outils
        self.max_history_size = 100  # Taille maximale de l'historique
        self.context_manager = context_manager
        self.app_state = app_state or {}  # État global de l'application
    
    def register_tool(self, name: str, tool_func: Callable, description: str, 
                     category: str = "general", required_args: List[str] = None,
                     optional_args: List[str] = None, dependencies: List[str] = None) -> bool:
        """
        Enregistre un nouvel outil dans l'orchestrateur.
        
        Args:
            name (str): Nom unique de l'outil
            tool_func (callable): Fonction à exécuter
            description (str): Description de l'outil
            category (str, optional): Catégorie de l'outil
            required_args (List[str], optional): Liste des arguments requis
            optional_args (List[str], optional): Liste des arguments optionnels
            dependencies (List[str], optional): Liste des outils dont cet outil dépend
            
        Returns:
            bool: True si l'enregistrement a réussi, False sinon
        """
        if name in self.tools:
            return False
        
        # Extraire les arguments de la signature de la fonction si non fournis
        if required_args is None or optional_args is None:
            required_args, optional_args = self._extract_function_args(tool_func)
            
        self.tools[name] = {
            "function": tool_func,
            "description": description,
            "category": category,
            "required_args": required_args or [],
            "optional_args": optional_args or [],
            "is_async": asyncio.iscoroutinefunction(tool_func)
        }
        
        # Enregistrer les dépendances
        if dependencies:
            self.tool_dependencies[name] = set(dependencies)
        
        return True
    
    def get_tool(self, name: str) -> Optional[Dict[str, Any]]:
        """
        Récupère un outil par son nom.
        
        Args:
            name (str): Nom de l'outil
            
        Returns:
            dict: Les informations sur l'outil ou None si non trouvé
        """
        tool = self.tools.get(name)
        if tool:
            tool_info = tool.copy()
            tool_info["name"] = name
            return tool_info
        return None
    
    def list_tools(self, category: str = None) -> List[Dict[str, Any]]:
        """
        Liste les outils disponibles, éventuellement filtrés par catégorie.
        
        Args:
            category (str, optional): Catégorie pour filtrer les outils
            
        Returns:
            list: Liste des outils disponibles avec leurs métadonnées
        """
        tools_dict = {}
        if category:
            tools_dict = {name: tool for name, tool in self.tools.items() 
                   if tool["category"] == category}
        else:
            tools_dict = self.tools
            
        tools_list = []
        for name, tool in tools_dict.items():
            tool_info = tool.copy()
            tool_info["name"] = name
            tools_list.append(tool_info)
            
        return tools_list
    
    def validate_args(self, tool_name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        """
        Valide les arguments pour un outil donné.
        
        Args:
            tool_name (str): Nom de l'outil
            args (dict): Arguments à valider
            
        Returns:
            dict: Résultat de la validation avec erreurs éventuelles
        """
        tool = self.get_tool(tool_name)
        if tool is None:
            return {"valid": False, "error": f"Outil non trouvé: {tool_name}"}
        
        missing_args = [arg for arg in tool["required_args"] if arg not in args]
        if missing_args:
            return {
                "valid": False, 
                "error": f"Arguments requis manquants: {', '.join(missing_args)}",
                "missing_args": missing_args
            }
        
        valid_args = set(tool["required_args"] + tool["optional_args"])
        unknown_args = [arg for arg in args if arg not in valid_args and arg != "context"]
        
        result = {"valid": True}
        if unknown_args:
            result["warnings"] = [f"Arguments non reconnus: {', '.join(unknown_args)}"]
        
        return result
    
    async def execute_tool_async(self, name: str, args: Dict[str, Any], context: Dict[str, Any] = None) -> Any:
        """
        Exécute un outil de manière asynchrone avec les arguments fournis.
        
        Args:
            name (str): Nom de l'outil à exécuter
            args (dict): Arguments à passer à l'outil
            context (dict, optional): Contexte d'exécution
            
        Returns:
            Any: Le résultat de l'exécution de l'outil
        """
        tool = self.get_tool(name)
        if tool is None:
            return {"error": f"Outil non trouvé: {name}"}
        
        # Valider les arguments
        validation = self.validate_args(name, args)
        if not validation["valid"]:
            return {"error": validation["error"]}
        
        try:
            if context:
                args["context"] = context
            
            # Exécuter la fonction de manière asynchrone ou synchrone selon son type
            if tool["is_async"]:
                result = await tool["function"](**args)
            else:
                # Exécuter les fonctions synchrones dans un thread séparé pour ne pas bloquer
                loop = asyncio.get_event_loop()
                result = await loop.run_in_executor(None, lambda: tool["function"](**args))
                
            return result
        except Exception as e:
            return {"error": str(e), "exception_type": type(e).__name__}
    
    def execute_tool(self, name: str, args: Dict[str, Any], context: Dict[str, Any] = None) -> Any:
        """
        Exécute un outil avec les arguments fournis.
        
        Args:
            name (str): Nom de l'outil à exécuter
            args (dict): Arguments à passer à l'outil
            context (dict, optional): Contexte d'exécution
            
        Returns:
            Any: Le résultat de l'exécution de l'outil
        """
        tool = self.get_tool(name)
        if tool is None:
            return {"error": f"Outil non trouvé: {name}"}
        
        # Valider les arguments
        validation = self.validate_args(name, args)
        if not validation["valid"]:
            return {"error": validation["error"]}
            
        try:
            if context:
                args["context"] = context
                
            if tool["is_async"]:
                result = asyncio.run(tool["function"](**args))
            else:
                result = tool["function"](**args)
            
            self._add_to_execution_history(name, args, result)
                
            return result
        except Exception as e:
            error_result = {"error": str(e), "exception_type": type(e).__name__}
            self._add_to_execution_history(name, args, error_result)
            return error_result
    
    def suggest_tools(self, query: str, context: Dict[str, Any] = None) -> List[Dict[str, Any]]:
        """
        Suggère des outils pertinents en fonction d'une requête et du contexte.
        
        Args:
            query (str): La requête de l'utilisateur
            context (dict, optional): Le contexte actuel
            
        Returns:
            list: Liste des outils suggérés avec leur pertinence
        """
        # Implémentation améliorée avec analyse de mots-clés et contexte
        suggestions = []
        
        query_lower = query.lower()
        query_words = set(query_lower.split())
        
        for name, tool in self.tools.items():
            score = 0
            
            description_lower = tool["description"].lower()
            description_words = set(description_lower.split())
            
            common_words = query_words.intersection(description_words)
            score += len(common_words) * 2
            
            if name.lower() in query_lower:
                score += 5
            
            keywords = {
                "générer": ["generation", "code", "créer", "nouveau"],
                "expliquer": ["explication", "comprendre", "clarifier"],
                "refactorer": ["refactoring", "améliorer", "optimiser"],
                "documenter": ["documentation", "commentaires", "docstring"],
                "tester": ["test", "vérifier", "valider"]
            }
            
            for action, related_words in keywords.items():
                if action in query_lower:
                    score += 3
                for word in related_words:
                    if word in query_lower and tool["category"].lower() == action:
                        score += 2
            
            if context:
                if "language_context" in context and context["language_context"]:
                    lang = context["language_context"].get("language", "").lower()
                    if lang and lang in tool["category"].lower():
                        score += 3
                
                if "current_file" in context and context["current_file"]:
                    if "fichier" in description_lower or "file" in description_lower:
                        score += 2
            
            if score > 0:
                suggestions.append({
                    "name": name,
                    "description": tool["description"],
                    "category": tool["category"],
                    "relevance": score,
                    "required_args": tool["required_args"]
                })
                
        suggestions.sort(key=lambda x: x["relevance"], reverse=True)
        return suggestions
    
    def get_tool_dependencies(self, name: str, recursive: bool = False) -> Set[str]:
        """
        Récupère les dépendances d'un outil.
        
        Args:
            name (str): Nom de l'outil
            recursive (bool): Si True, inclut les dépendances des dépendances
            
        Returns:
            set: Ensemble des noms des outils dont dépend l'outil spécifié
        """
        if name not in self.tool_dependencies:
            return set()
            
        dependencies = self.tool_dependencies[name].copy()
        
        if recursive:
            for dep in list(dependencies):
                dependencies.update(self.get_tool_dependencies(dep, recursive=True))
                
        return dependencies
        
    def _add_to_execution_history(self, tool_name: str, args: Dict[str, Any], result: Any) -> None:
        """
        Ajoute une entrée à l'historique d'exécution.
        
        Args:
            tool_name (str): Nom de l'outil exécuté
            args (dict): Arguments utilisés
            result (Any): Résultat de l'exécution
        """
        entry = {
            "timestamp": self._get_timestamp(),
            "tool_name": tool_name,
            "args": args.copy(),  # Copie pour éviter les modifications ultérieures
            "result": result,
            "success": "error" not in result if isinstance(result, dict) else True
        }
        
        self.execution_history.append(entry)
        if len(self.execution_history) > self.max_history_size:
            self.execution_history.pop(0)
    
    def get_execution_history(self, limit: int = None, tool_name: str = None, 
                             success_only: bool = False) -> List[Dict[str, Any]]:
        """
        Récupère l'historique d'exécution des outils, avec filtrage optionnel.
        
        Args:
            limit (int, optional): Nombre maximum d'entrées à retourner
            tool_name (str, optional): Filtrer par nom d'outil
            success_only (bool, optional): Si True, ne retourne que les exécutions réussies
            
        Returns:
            list: Liste des entrées d'historique correspondant aux critères
        """
        filtered_history = self.execution_history
        
        if tool_name:
            filtered_history = [entry for entry in filtered_history 
                              if entry["tool_name"] == tool_name]
                              
        if success_only:
            filtered_history = [entry for entry in filtered_history 
                              if entry["success"]]
        
        if limit and limit > 0:
            filtered_history = filtered_history[-limit:]
            
        return filtered_history
    
    def clear_execution_history(self) -> None:
        """
        Efface l'historique d'exécution des outils.
        """
        self.execution_history = []
    
    def create_tool_chain(self, chain_name: str, tools: List[Dict[str, Any]]) -> bool:
        """
        Crée une chaîne d'outils qui seront exécutés séquentiellement.
        
        Args:
            chain_name (str): Nom de la chaîne d'outils
            tools (list): Liste de dictionnaires décrivant les outils à exécuter
                Chaque dictionnaire doit contenir:
                - "name": Nom de l'outil
                - "args": Arguments fixes (dict)
                - "result_mapping": Comment mapper les résultats aux outils suivants (dict)
            
        Returns:
            bool: True si la chaîne a été créée avec succès, False sinon
        """
        for tool_config in tools:
            if tool_config["name"] not in self.tools:
                return False
        
        async def tool_chain_func(context=None):
            results = []
            current_args = {}
            
            for i, tool_config in enumerate(tools):
                tool_name = tool_config["name"]
                
                tool_args = tool_config.get("args", {}).copy()
                tool_args.update(current_args)
                
                result = await self.execute_tool_async(tool_name, tool_args, context)
                
                # Stocker le résultat tel quel, sans encapsulation supplémentaire
                results.append(result)
                
                if isinstance(result, dict) and "error" in result:
                    return {
                        "chain_name": chain_name,
                        "completed_steps": i + 1,
                        "total_steps": len(tools),
                        "results": results,
                        "error": f"Échec à l'étape {i + 1}: {result['error']}"
                    }
                
                if i < len(tools) - 1 and "result_mapping" in tool_config:
                    for dest_arg, source_path in tool_config["result_mapping"].items():
                        value = self._extract_result_value(result, source_path)
                        if value is not None:
                            current_args[dest_arg] = value
            
            return {
                "chain_name": chain_name,
                "completed_steps": len(tools),
                "total_steps": len(tools),
                "results": results
            }
        
        description = f"Chaîne d'outils exécutant séquentiellement: {', '.join([t['name'] for t in tools])}"
        self.register_tool(
            chain_name, 
            tool_chain_func, 
            description, 
            category="tool_chain",
            dependencies=[t["name"] for t in tools]
        )
        
        return True
    
    def _extract_result_value(self, result: Dict[str, Any], path: str) -> Any:
        """
        Extrait une valeur d'un résultat selon un chemin spécifié.
        
        Args:
            result (dict): Résultat d'exécution d'un outil
            path (str): Chemin d'accès à la valeur (ex: "result.data.items[0]" ou "data.items.0.value")
            
        Returns:
            Any: La valeur extraite ou None si le chemin est invalide
        """
        if not path:
            return None
            
        current = result
        
        segments = path.split(".")
        
        try:
            for segment in segments:
                # Gérer les accès par index dans les listes avec la notation [index]
                if "[" in segment and segment.endswith("]"):
                    name, index_str = segment.split("[", 1)
                    index = int(index_str[:-1])
                    current = current[name][index]
                # Gérer les accès par index dans les listes avec la notation .index
                elif segment.isdigit():
                    current = current[int(segment)]
                else:
                    current = current[segment]
            return current
        except (KeyError, IndexError, TypeError):
            return None
    
    def _get_timestamp(self) -> str:
        """
        Retourne l'horodatage actuel au format ISO.
        
        Returns:
            str: Horodatage au format ISO
        """
        from datetime import datetime
        return datetime.now().isoformat()
    
    def _extract_function_args(self, func: Callable) -> tuple:
        """
        Extrait les arguments requis et optionnels d'une fonction.
        
        Args:
            func (callable): La fonction à analyser
            
        Returns:
            tuple: (required_args, optional_args)
        """
        required_args = []
        optional_args = []
        
        try:
            sig = inspect.signature(func)
            for param_name, param in sig.parameters.items():
                if param_name == "self":
                    continue
                    
                if param.default == inspect.Parameter.empty:
                    required_args.append(param_name)
                else:
                    optional_args.append(param_name)
        except (ValueError, TypeError):
            pass
            
        return required_args, optional_args
    
    def _validate_args(self, tool_name: str, args: Dict[str, Any]) -> Tuple[bool, List[str]]:
        """
        Valide les arguments pour un outil donné et retourne un tuple (is_valid, missing_args).
        
        Args:
            tool_name (str): Nom de l'outil
            args (dict): Arguments à valider
            
        Returns:
            tuple: (is_valid, missing_args)
        """
        tool = self.get_tool(tool_name)
        if tool is None:
            return False, ["Tool not found"]
        
        missing_args = [arg for arg in tool["required_args"] if arg not in args]
        
        return len(missing_args) == 0, missing_args
