"""
Impact Analysis - Outil d'analyse d'impact des changements de code.

Ce module a été refactorisé pour respecter l'architecture modulaire:
- models.py : Modèles Pydantic
- config.py : Constantes et configurations
- engine.py : Logique métier pure
- tool.py : Orchestration du Tool

Usage:
    from collegue.tools.impact_analysis import ImpactAnalysisTool, ImpactAnalysisRequest, ImpactAnalysisResponse
"""
from .tool import ImpactAnalysisTool
from .engine import ImpactAnalysisEngine
from .models import (
    ImpactAnalysisRequest, ImpactAnalysisResponse,
    ImpactedFile, RiskNote, SearchQuery, TestRecommendation, FollowupAction, LLMInsight
)

__all__ = [
    'ImpactAnalysisTool',
    'ImpactAnalysisEngine',
    'ImpactAnalysisRequest',
    'ImpactAnalysisResponse',
    'ImpactedFile',
    'RiskNote',
    'SearchQuery',
    'TestRecommendation',
    'FollowupAction',
    'LLMInsight'
]
