"""
Client Python pour interagir avec le serveur Collègue MCP
"""
import os
import sys
import json
import asyncio
from typing import Dict, List, Any, Optional, Union
import logging

# Configurer le logger
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Importer le client FastMCP
try:
    from fastmcp import Client
except ImportError:
    logger.error("La bibliothèque fastmcp n'est pas installée. Installez-la avec 'pip install fastmcp'")
    sys.exit(1)

class CollegueClient:
    """Client Python pour interagir avec le serveur Collègue MCP."""
    
    def __init__(self, server_path: str = None, host: str = "localhost", port: int = None):
        """
        Initialise le client Collègue MCP.
        
        Args:
            server_path: Chemin vers le script app.py du serveur Collègue MCP
            host: Hôte du serveur Collègue MCP si déjà en cours d'exécution
            port: Port du serveur Collègue MCP si déjà en cours d'exécution
        """
        self.client = None
        self.session_id = None
        
        # Si le chemin du serveur est fourni, configurer pour lancer le serveur
        if server_path:
            if not os.path.exists(server_path):
                raise FileNotFoundError(f"Le chemin du serveur {server_path} n'existe pas")
            
            self.config = {
                "mcpServers": {
                    "collegue": {
                        "command": "python",
                        "args": [server_path]
                    }
                }
            }
        # Sinon, configurer pour se connecter à un serveur existant
        elif host and port:
            self.config = {
                "mcpServers": {
                    "collegue": {
                        "url": f"http://{host}:{port}"
                    }
                }
            }
        else:
            # Essayer de trouver le chemin du serveur automatiquement
            current_dir = os.path.dirname(os.path.abspath(__file__))
            parent_dir = os.path.abspath(os.path.join(current_dir, '..', '..'))
            server_path = os.path.join(parent_dir, "collegue", "app.py")
            
            if os.path.exists(server_path):
                self.config = {
                    "mcpServers": {
                        "collegue": {
                            "command": "python",
                            "args": [server_path]
                        }
                    }
                }
            else:
                raise ValueError("Impossible de trouver le serveur Collègue MCP. Veuillez spécifier le chemin du serveur ou l'hôte et le port.")
    
    async def __aenter__(self):
        """Méthode pour utiliser le client comme gestionnaire de contexte asynchrone."""
        self.client = await Client(self.config).__aenter__()
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Méthode pour fermer le client lors de la sortie du gestionnaire de contexte."""
        if self.client:
            await self.client.__aexit__(exc_type, exc_val, exc_tb)
    
    async def list_tools(self) -> List[str]:
        """
        Liste tous les outils disponibles sur le serveur.
        
        Returns:
            List[str]: Liste des noms d'outils disponibles
        """
        if not self.client:
            raise RuntimeError("Le client n'est pas initialisé. Utilisez 'async with CollegueClient() as client:'")
        
        tools = await self.client.list_tools()
        return [tool.name for tool in tools]
    
    async def create_session(self) -> Dict[str, Any]:
        """
        Crée une nouvelle session sur le serveur.
        
        Returns:
            Dict[str, Any]: Informations sur la session créée
        """
        if not self.client:
            raise RuntimeError("Le client n'est pas initialisé. Utilisez 'async with CollegueClient() as client:'")
        
        tool_name = "create_session"
        result = await self.client.call_tool(tool_name, {})
        self.session_id = result.data["session_id"]
        return result.data
    
    async def get_session_context(self, session_id: str = None) -> Dict[str, Any]:
        """
        Récupère le contexte d'une session.
        
        Args:
            session_id: ID de la session (utilise la session courante si non spécifié)
            
        Returns:
            Dict[str, Any]: Contexte de la session
        """
        if not self.client:
            raise RuntimeError("Le client n'est pas initialisé. Utilisez 'async with CollegueClient() as client:'")
        
        session_id = session_id or self.session_id
        if not session_id:
            raise ValueError("Aucune session active. Appelez create_session() d'abord.")
        
        tool_name = "get_session_context"
        result = await self.client.call_tool(tool_name, {
            "request": {
                "session_id": session_id
            }
        })
        return result.data
    
    async def analyze_code(self, code: str, language: str = "python", file_path: str = None) -> Dict[str, Any]:
        """
        Analyse un extrait de code.
        
        Args:
            code: Code à analyser
            language: Langage du code
            file_path: Chemin du fichier contenant le code
            
        Returns:
            Dict[str, Any]: Résultat de l'analyse
        """
        if not self.client:
            raise RuntimeError("Le client n'est pas initialisé. Utilisez 'async with CollegueClient() as client:'")
        
        tool_name = "analyze_code"
        result = await self.client.call_tool(tool_name, {
            "request": {
                "code": code,
                "language": language,
                "session_id": self.session_id,
                "file_path": file_path
            }
        })
        return result.data
    
    async def suggest_tools_for_query(self, query: str) -> List[Dict[str, Any]]:
        """
        Suggère des outils pertinents en fonction d'une requête.
        
        Args:
            query: Requête de l'utilisateur
            
        Returns:
            List[Dict[str, Any]]: Liste des outils suggérés
        """
        if not self.client:
            raise RuntimeError("Le client n'est pas initialisé. Utilisez 'async with CollegueClient() as client:'")
        
        tool_name = "suggest_tools_for_query"
        result = await self.client.call_tool(tool_name, {
            "request": {
                "query": query,
                "session_id": self.session_id
            }
        })
        return result.data
    
    async def generate_code_from_description(self, description: str, language: str, 
                                           constraints: List[str] = None) -> Dict[str, Any]:
        """
        Génère du code à partir d'une description textuelle.
        
        Args:
            description: Description du code à générer
            language: Langage de programmation cible
            constraints: Contraintes spécifiques pour la génération
            
        Returns:
            Dict[str, Any]: Code généré avec explications
        """
        if not self.client:
            raise RuntimeError("Le client n'est pas initialisé. Utilisez 'async with CollegueClient() as client:'")
        
        tool_name = "generate_code_from_description"
        result = await self.client.call_tool(tool_name, {
            "request": {
                "description": description,
                "language": language,
                "session_id": self.session_id,
                "constraints": constraints
            }
        })
        return result.data
    
    async def explain_code_snippet(self, code: str, language: str = None, 
                                 detail_level: str = "medium", focus_on: List[str] = None) -> Dict[str, Any]:
        """
        Analyse et explique un extrait de code.
        
        Args:
            code: Code à expliquer
            language: Langage de programmation du code
            detail_level: Niveau de détail de l'explication (basic, medium, detailed)
            focus_on: Aspects spécifiques à expliquer (algorithmes, structures, etc.)
            
        Returns:
            Dict[str, Any]: Explication du code avec analyses
        """
        if not self.client:
            raise RuntimeError("Le client n'est pas initialisé. Utilisez 'async with CollegueClient() as client:'")
        
        tool_name = "explain_code_snippet"
        result = await self.client.call_tool(tool_name, {
            "request": {
                "code": code,
                "language": language,
                "session_id": self.session_id,
                "detail_level": detail_level,
                "focus_on": focus_on
            }
        })
        return result.data
    
    async def refactor_code_snippet(self, code: str, language: str, refactoring_type: str, 
                                  parameters: Dict[str, Any] = None) -> Dict[str, Any]:
        """
        Refactore un extrait de code selon le type de refactoring demandé.
        
        Args:
            code: Code à refactorer
            language: Langage de programmation du code
            refactoring_type: Type de refactoring à appliquer (rename, extract, simplify, optimize)
            parameters: Paramètres spécifiques au type de refactoring
            
        Returns:
            Dict[str, Any]: Code refactoré avec explications
        """
        if not self.client:
            raise RuntimeError("Le client n'est pas initialisé. Utilisez 'async with CollegueClient() as client:'")
        
        tool_name = "refactor_code_snippet"
        result = await self.client.call_tool(tool_name, {
            "request": {
                "code": code,
                "language": language,
                "refactoring_type": refactoring_type,
                "session_id": self.session_id,
                "parameters": parameters
            }
        })
        return result.data
    
    async def generate_code_documentation(self, code: str, language: str, doc_style: str = "standard",
                                        doc_format: str = None, include_examples: bool = False) -> Dict[str, Any]:
        """
        Génère de la documentation pour un extrait de code.
        
        Args:
            code: Code à documenter
            language: Langage de programmation du code
            doc_style: Style de documentation (standard, detailed, minimal)
            doc_format: Format de documentation (markdown, rst, html)
            include_examples: Inclure des exemples d'utilisation
            
        Returns:
            Dict[str, Any]: Documentation générée
        """
        if not self.client:
            raise RuntimeError("Le client n'est pas initialisé. Utilisez 'async with CollegueClient() as client:'")
        
        tool_name = "generate_code_documentation"
        result = await self.client.call_tool(tool_name, {
            "request": {
                "code": code,
                "language": language,
                "session_id": self.session_id,
                "doc_style": doc_style,
                "doc_format": doc_format,
                "include_examples": include_examples
            }
        })
        return result.data
