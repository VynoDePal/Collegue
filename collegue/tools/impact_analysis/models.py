"""
Modèles Pydantic pour l'outil Impact Analysis.
"""
from typing import Optional, List
from pydantic import BaseModel, Field, field_validator
from ...core.shared import validate_fast_deep, validate_confidence_mode


class ImpactAnalysisRequest(BaseModel):
    model_config = {'extra': 'forbid'}
    """Requête pour l'analyse d'impact."""
    change_intent: str = Field(
        ...,
        description="Description du changement prévu (ex: 'renommer UserService en AuthService')"
    )
    files: List = Field(
        ...,
        description="Liste des fichiers à analyser [{path, content, language?}, ...]",
        min_length=1
    )
    diff: Optional[str] = Field(
        None,
        description="Diff unifié optionnel du changement proposé"
    )
    entry_points: Optional[List[str]] = Field(
        None,
        description="Points d'entrée importants (ex: 'main.py', 'api/router.ts')"
    )
    assumptions: Optional[List[str]] = Field(
        None,
        description="Contraintes ou hypothèses du projet"
    )
    confidence_mode: str = Field(
        "balanced",
        description="Mode de confiance: 'conservative', 'balanced', 'aggressive'"
    )
    analysis_depth: str = Field(
        "fast",
        description="Profondeur: 'fast' (heuristiques) ou 'deep' (enrichissement IA)"
    )

    @field_validator('confidence_mode')
    def _validate_confidence_mode(cls, v):
        return validate_confidence_mode(v)

    @field_validator('analysis_depth')
    def _validate_analysis_depth(cls, v):
        return validate_fast_deep(v)


class ImpactedFile(BaseModel):
    model_config = {'extra': 'forbid'}
    """Fichier impacté par le changement."""
    path: str = Field(..., description="Chemin du fichier")
    reason: str = Field(..., description="Raison de l'impact")
    confidence: str = Field(..., description="Niveau de confiance: high, medium, low")
    impact_type: str = Field("direct", description="Type: direct, indirect, test")


class RiskNote(BaseModel):
    model_config = {'extra': 'forbid'}
    """Note de risque identifiée."""
    category: str = Field(..., description="Catégorie: breaking_change, security, data_migration, performance, compat")
    note: str = Field(..., description="Description du risque")
    confidence: str = Field(..., description="Niveau de confiance")
    severity: str = Field("medium", description="Sévérité: low, medium, high, critical")


class SearchQuery(BaseModel):
    model_config = {'extra': 'forbid'}
    """Requête de recherche pour compléter l'analyse."""
    query: str = Field(..., description="Pattern de recherche")
    rationale: str = Field(..., description="Pourquoi cette recherche")
    search_type: str = Field("text", description="Type: text, regex, symbol")


class TestRecommendation(BaseModel):
    model_config = {'extra': 'forbid'}
    """Recommandation de test à exécuter."""
    command: str = Field(..., description="Commande à exécuter")
    rationale: str = Field(..., description="Pourquoi ce test")
    scope: str = Field("unit", description="Scope: unit, integration, e2e")
    priority: str = Field("medium", description="Priorité: low, medium, high")


class FollowupAction(BaseModel):
    model_config = {'extra': 'forbid'}
    """Action de suivi recommandée."""
    action: str = Field(..., description="Action à effectuer")
    rationale: str = Field(..., description="Pourquoi cette action")


class LLMInsight(BaseModel):
    model_config = {'extra': 'forbid'}
    """Insight généré par le LLM en mode deep."""
    category: str = Field(..., description="Catégorie: semantic, architectural, business, suggestion")
    insight: str = Field(..., description="L'insight détaillé")
    confidence: str = Field("medium", description="Confiance: low, medium, high")


class ImpactAnalysisResponse(BaseModel):
    model_config = {'extra': 'forbid'}
    """Réponse de l'analyse d'impact."""
    change_summary: str = Field(..., description="Résumé du changement analysé")
    impacted_files: List[ImpactedFile] = Field(default_factory=list, description="Fichiers impactés")
    risk_notes: List[RiskNote] = Field(default_factory=list, description="Risques identifiés")
    search_queries: List[SearchQuery] = Field(default_factory=list, description="Requêtes de recherche")
    tests_to_run: List[TestRecommendation] = Field(default_factory=list, description="Tests recommandés")
    followups: List[FollowupAction] = Field(default_factory=list, description="Actions de suivi")
    analysis_summary: str = Field(..., description="Résumé de l'analyse")
    llm_insights: Optional[List[LLMInsight]] = Field(None, description="Insights IA (mode deep)")
    semantic_summary: Optional[str] = Field(None, description="Résumé sémantique (mode deep)")
    analysis_depth_used: str = Field("fast", description="Profondeur d'analyse utilisée")
