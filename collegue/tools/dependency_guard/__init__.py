"""
Dependency Guard - Outil de validation des dépendances.

Ce module a été refactorisé pour respecter l'architecture modulaire:
- models.py : Modèles Pydantic
- config.py : Constantes et configurations
- engine.py : Logique métier pure
- tool.py : Orchestration du Tool

Usage:
    from collegue.tools.dependency_guard import DependencyGuardTool
"""
from .tool import DependencyGuardTool
from .models import DependencyGuardRequest, DependencyGuardResponse, DependencyIssue

__all__ = [
    'DependencyGuardTool',
    'DependencyGuardRequest',
    'DependencyGuardResponse',
    'DependencyIssue'
]
