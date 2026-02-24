"""
Modèles Pydantic pour l'outil Repo Consistency Check.
"""
from typing import Optional, List, Dict, Any
from pydantic import BaseModel, Field, field_validator
from ...core.shared import validate_fast_deep


class LLMInsight(BaseModel):
    """Insight généré par le LLM en mode deep analysis."""
    category: str = Field(..., description="Catégorie: pattern, architecture, debt, suggestion")
    insight: str = Field(..., description="L'insight détaillé")
    confidence: str = Field("medium", description="Confiance: low, medium, high")
    affected_files: List[str] = Field(default_factory=list, description="Fichiers concernés")


class SuggestedAction(BaseModel):
    """Action suggérée pour corriger les problèmes."""
    tool_name: str = Field(..., description="Nom du tool à appeler (ex: code_refactoring)")
    action_type: str = Field(..., description="Type: refactor, cleanup, restructure")
    rationale: str = Field(..., description="Pourquoi cette action")
    priority: str = Field("medium", description="Priorité: low, medium, high, critical")
    params: Dict[str, Any] = Field(default_factory=dict, description="Paramètres pour le tool")
    score: float = Field(0.0, description="Score de pertinence (0.0-1.0)", ge=0.0, le=1.0)


class ConsistencyCheckRequest(BaseModel):
    """Requête pour la vérification de cohérence."""
    files: List = Field(
        ...,
        description="Liste des fichiers à analyser [{path, content, language?}, ...]",
        min_length=1
    )
    language: str = Field(
        "auto",
        description="Langage principal: 'python', 'typescript', 'javascript', 'php', 'auto'"
    )
    checks: Optional[List[str]] = Field(
        None,
        description="Checks à exécuter: 'unused_imports', 'unused_vars', 'dead_code', 'duplication', 'unresolved_symbol'. Tous par défaut."
    )
    diff: Optional[str] = Field(
        None,
        description="Diff unifié optionnel pour focaliser l'analyse sur les changements"
    )
    mode: str = Field(
        "fast",
        description="Mode: 'fast' (heuristiques rapides) ou 'deep' (analyse plus complète)"
    )
    analysis_depth: str = Field(
        "fast",
        description="Profondeur IA: 'fast' (heuristiques seules) ou 'deep' (enrichissement LLM avec scoring)"
    )
    auto_chain: bool = Field(
        False,
        description="Si True et score refactoring > seuil, déclenche automatiquement code_refactoring"
    )
    refactoring_threshold: float = Field(
        0.7,
        description="Seuil de score (0.0-1.0) pour déclencher auto_chain",
        ge=0.0,
        le=1.0
    )
    min_confidence: int = Field(
        60,
        description="Confiance minimum (0-100) pour reporter un issue",
        ge=0,
        le=100
    )

    @field_validator('mode')
    def validate_mode(cls, v):
        return validate_fast_deep(v)

    @field_validator('analysis_depth')
    def validate_analysis_depth(cls, v):
        return validate_fast_deep(v)

    @field_validator('checks')
    def validate_checks(cls, v):
        if v is None:
            return v
        valid = ['unused_imports', 'unused_vars', 'dead_code', 'duplication', 'unresolved_symbol']
        for check in v:
            if check not in valid:
                raise ValueError(f"Check '{check}' invalide. Utilisez: {', '.join(valid)}")
        return v


class ConsistencyCheckResponse(BaseModel):
    """Réponse de la vérification de cohérence."""
    valid: bool = Field(..., description="True si aucun problème trouvé")
    summary: Dict[str, int] = Field(
        ...,
        description="Résumé par sévérité {total, high, medium, low, info}"
    )
    issues: List = Field(
        default_factory=list,
        description="Liste des problèmes détectés"
    )
    files_analyzed: int = Field(..., description="Nombre de fichiers analysés")
    checks_performed: List[str] = Field(..., description="Checks exécutés")
    analysis_summary: str = Field(..., description="Résumé de l'analyse")
    analysis_depth_used: str = Field("fast", description="Profondeur d'analyse utilisée")
    llm_insights: Optional[List[LLMInsight]] = Field(
        None,
        description="Insights IA (mode deep): patterns, architecture, dette technique"
    )
    refactoring_score: float = Field(
        0.0,
        description="Score de refactoring recommandé (0.0-1.0)",
        ge=0.0,
        le=1.0
    )
    refactoring_priority: str = Field(
        "none",
        description="Priorité: none, suggested, recommended, critical"
    )
    suggested_actions: List[SuggestedAction] = Field(
        default_factory=list,
        description="Actions suggérées (tools à appeler)"
    )
    auto_refactoring_triggered: bool = Field(
        False,
        description="True si le refactoring automatique a été déclenché"
    )
    auto_refactoring_result: Optional[Dict[str, Any]] = Field(
        None,
        description="Résultat du refactoring automatique (si déclenché)"
    )
