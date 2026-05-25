"""
Modèles Pydantic pour l'outil Architecture Analysis.
"""

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator


class ArchitectureAnalysisRequest(BaseModel):
    """Requête pour l'analyse architecturale."""

    code: str = Field(..., description="Code source ou structure du projet à analyser", min_length=1)
    language: str = Field(..., description="Langage de programmation principal")
    session_id: Optional[str] = Field(None, description="Identifiant de session")
    file_path: Optional[str] = Field(None, description="Chemin du fichier ou répertoire")
    analysis_types: List[str] = Field(
        default_factory=lambda: [
            "dependencies",
            "coupling",
            "cohesion",
            "patterns",
            "debt",
        ],
        description=("Types d'analyse: dependencies, coupling, cohesion, patterns, debt, metrics, circular_deps"),
    )
    context: Optional[str] = Field(None, description="Contexte additionnel sur le projet")

    @field_validator("language")
    def validate_language_field(cls, v):
        if not v or not v.strip():
            raise ValueError("Le langage ne peut pas être vide")
        return v.strip().lower()


class DependencyInfo(BaseModel):
    """Information sur une dépendance entre modules."""

    source: str = Field(..., description="Module source")
    target: str = Field(..., description="Module cible (importé)")
    import_type: str = Field("direct", description="Type: direct, transitive, circular")


class ArchitecturalIssue(BaseModel):
    """Un problème architectural détecté."""

    category: str = Field(
        ...,
        description=(
            "Catégorie: circular_dependency, high_coupling, low_cohesion, "
            "god_class, missing_abstraction, layer_violation"
        ),
    )
    severity: str = Field(..., description="Sévérité: info, warning, error, critical")
    title: str = Field(..., description="Titre court")
    description: str = Field(..., description="Description détaillée")
    affected_modules: List[str] = Field(default_factory=list, description="Modules affectés")
    recommendation: Optional[str] = Field(None, description="Recommandation de correction")


class ArchitectureAnalysisResponse(BaseModel):
    """Réponse de l'analyse architecturale."""

    architecture_score: float = Field(..., description="Score architecturale global (0.0-1.0)", ge=0.0, le=1.0)
    detected_patterns: List[str] = Field(
        default_factory=list,
        description="Patterns architecturaux détectés (MVC, Layered, etc.)",
    )
    dependencies: List[DependencyInfo] = Field(default_factory=list, description="Graphe de dépendances")
    issues: List[ArchitecturalIssue] = Field(default_factory=list, description="Problèmes architecturaux")
    metrics: Dict[str, Any] = Field(
        default_factory=dict,
        description="Métriques: modules, coupling, cohesion, depth, fan_in, fan_out",
    )
    debt_score: float = Field(0.0, description="Score de dette technique (0.0=aucune, 1.0=critique)", ge=0.0, le=1.0)
    recommendations: List[str] = Field(default_factory=list, description="Recommandations architecturales")
    summary: str = Field(..., description="Résumé de l'analyse")
    language: str = Field(..., description="Langage analysé")
    # Champs agentiques
    agent_iterations: int = Field(default=0, description="Nombre d'itérations agentiques")
    agent_best_score: Optional[float] = Field(default=None, description="Meilleur score atteint")
    agent_converged: Optional[bool] = Field(default=None, description="Convergence de la boucle")
    delegation_triggered: bool = Field(
        default=False, description="True si une délégation inter-experts a été déclenchée"
    )
    delegation_results: Optional[List[Dict[str, Any]]] = Field(
        default=None, description="Résultats des délégations inter-experts"
    )
