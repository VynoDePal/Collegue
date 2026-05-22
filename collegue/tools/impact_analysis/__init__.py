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
from .engine import ImpactAnalysisEngine
from .models import (
    FollowupAction,
    ImpactAnalysisRequest,
    ImpactAnalysisResponse,
    ImpactedFile,
    LLMInsight,
    RiskNote,
    SearchQuery,
    TestRecommendation,
)
from .tool import ImpactAnalysisTool

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
