"""
Refactoring - Outil de refactoring de code intelligent.

Ce module a été refactorisé pour respecter l'architecture modulaire:
- models.py : Modèles Pydantic
- config.py : Constantes et configurations
- engine.py : Logique métier pure
- tool.py : Orchestration du Tool

Usage:
    from collegue.tools.refactoring import RefactoringTool, RefactoringRequest, RefactoringResponse
"""
from .tool import RefactoringTool
from .models import RefactoringRequest, RefactoringResponse, LLMRefactoringResult
from .engine import RefactoringEngine

__all__ = [
    'RefactoringTool',
    'RefactoringRequest',
    'RefactoringResponse',
    'LLMRefactoringResult',
    'RefactoringEngine'
]
