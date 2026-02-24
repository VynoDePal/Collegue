"""
Repo Consistency Check - Outil de détection d'incohérences dans le code.

Ce module a été refactorisé pour respecter l'architecture modulaire:
- models.py : Modèles Pydantic
- config.py : Constantes et configurations
- engine.py : Logique métier pure
- tool.py : Orchestration du Tool

Usage:
    from collegue.tools.repo_consistency_check import RepoConsistencyCheckTool
"""
from .tool import RepoConsistencyCheckTool
from .models import ConsistencyCheckRequest, ConsistencyCheckResponse, LLMInsight, SuggestedAction

__all__ = [
    'RepoConsistencyCheckTool',
    'ConsistencyCheckRequest',
    'ConsistencyCheckResponse',
    'LLMInsight',
    'SuggestedAction'
]
