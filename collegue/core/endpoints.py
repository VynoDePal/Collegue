"""
Endpoints - Points d'entrée API pour le Core Engine

NOTE: Les outils MCP internes (analyze_code, get_session_context, create_session, suggest_tools_for_query)
ont été supprimés car ils représentent des détails d'implémentation qui ne devraient pas être exposés
comme outils MCP. Ces fonctionnalités sont gérées en interne ou via les paramètres session_id
des outils principaux.
"""
from fastapi import Depends
from fastmcp import FastMCP
from pydantic import BaseModel
from typing import Optional, List, Dict, Any

def register(app: FastMCP, app_state: dict):
    """
    Enregistre les endpoints du Core Engine dans l'application FastMCP.
    
    Note: Plus aucun outil MCP n'est enregistré depuis ce module.
    Les fonctionnalités internes sont gérées par les outils principaux
    et le système de sessions automatique.
    """
    pass
