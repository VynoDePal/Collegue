"""
Endpoints - Points d'entrée API pour le Core Engine
"""
from fastapi import Depends
from fastmcp import FastMCP
from pydantic import BaseModel
from typing import Optional, List, Dict, Any

class CodeAnalysisRequest(BaseModel):
    """Modèle de requête pour l'analyse de code."""
    code: str
    language: Optional[str] = None
    session_id: Optional[str] = None
    file_path: Optional[str] = None

class SessionRequest(BaseModel):
    """Modèle de requête pour la gestion des sessions."""
    session_id: str

def register(app: FastMCP, app_state: dict):
    """Enregistre les endpoints du Core Engine dans l'application FastMCP."""
    
    @app.tool()
    def analyze_code(request: CodeAnalysisRequest) -> Dict[str, Any]:
        """
        Analyse un extrait de code et retourne sa structure.
        
        Args:
            request: Les détails de la requête d'analyse
            
        Returns:
            Dict[str, Any]: La représentation structurée du code
        """
        parser = app_state["parser"]
        context_manager = app_state["context_manager"]
        
        # Créer ou récupérer le contexte de session
        session_id = request.session_id or "default"
        context = context_manager.get_context(session_id)
        if context is None:
            context = context_manager.create_context(session_id)
        
        # Analyser le code
        result = parser.parse(request.code, request.language)
        
        # Mettre à jour le contexte avec le code analysé
        context_manager.add_code_to_context(
            session_id, 
            request.code, 
            result.get("language"), 
            request.file_path
        )
        
        return result
    
    @app.tool()
    def get_session_context(request: SessionRequest) -> Dict[str, Any]:
        """
        Récupère le contexte d'une session.
        
        Args:
            request: Les détails de la requête de session
            
        Returns:
            Dict[str, Any]: Le contexte de la session ou une erreur
        """
        context_manager = app_state["context_manager"]
        context = context_manager.get_context(request.session_id)
        
        if context is None:
            return {"error": f"Session non trouvée: {request.session_id}"}
        
        return context
    
    @app.tool()
    def create_session() -> Dict[str, Any]:
        """
        Crée une nouvelle session avec un identifiant unique.
        
        Returns:
            Dict[str, Any]: Les informations sur la nouvelle session
        """
        import uuid
        context_manager = app_state["context_manager"]
        
        session_id = str(uuid.uuid4())
        context = context_manager.create_context(session_id)
        
        return {
            "session_id": session_id,
            "message": "Nouvelle session créée avec succès",
            "context": context
        }
    
    @app.tool()
    def suggest_tools_for_query(query: str, session_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        Suggère des outils pertinents en fonction d'une requête.
        
        Args:
            query: La requête de l'utilisateur
            session_id: Identifiant de la session pour le contexte
            
        Returns:
            List[Dict[str, Any]]: Liste des outils suggérés
        """
        orchestrator = app_state["orchestrator"]
        context_manager = app_state["context_manager"]
        
        context = None
        if session_id:
            context = context_manager.get_context(session_id)
            
        return orchestrator.suggest_tools(query, context)
