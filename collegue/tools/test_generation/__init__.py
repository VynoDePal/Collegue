"""
Test Generation - Outil de génération automatique de tests unitaires.

Ce module a été refactorisé pour respecter l'architecture modulaire:
- models.py : Modèles Pydantic
- config.py : Templates et configurations
- engine.py : Logique métier pure
- tool.py : Orchestration du Tool

Usage:
    from collegue.tools.test_generation import TestGenerationTool, TestGenerationRequest, TestGenerationResponse
"""
from .engine import TestGenerationEngine
from .models import LLMTestGenerationResult, TestGenerationRequest, TestGenerationResponse
from .tool import TestGenerationTool

__all__ = [
    'TestGenerationTool',
    'TestGenerationRequest',
    'TestGenerationResponse',
    'LLMTestGenerationResult',
    'TestGenerationEngine'
]
