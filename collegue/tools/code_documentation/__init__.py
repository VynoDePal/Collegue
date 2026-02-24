"""
Code Documentation - Outil de génération automatique de documentation.

Ce module a été refactorisé pour respecter l'architecture modulaire:
- models.py : Modèles Pydantic
- config.py : Constantes et configurations  
- engine.py : Logique métier pure
- tool.py : Orchestration du Tool

Usage:
    from collegue.tools.code_documentation import DocumentationTool, DocumentationRequest, DocumentationResponse
"""
from .tool import DocumentationTool
from .models import DocumentationRequest, DocumentationResponse
from .engine import DocumentationEngine

__all__ = [
    'DocumentationTool',
    'DocumentationRequest',
    'DocumentationResponse',
    'DocumentationEngine'
]
