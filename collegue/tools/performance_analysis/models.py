"""
Modèles Pydantic pour l'outil Performance Analysis.
"""

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator


class PerformanceAnalysisRequest(BaseModel):
    """Requête pour l'analyse de performance."""

    code: str = Field(..., description="Code source à analyser", min_length=1)
    language: str = Field(..., description="Langage de programmation")
    session_id: Optional[str] = Field(None, description="Identifiant de session")
    file_path: Optional[str] = Field(None, description="Chemin du fichier")
    analysis_categories: List[str] = Field(
        default_factory=lambda: ["cpu", "memory", "io", "algorithmic"],
        description="Catégories: cpu, memory, io, network, algorithmic, parallelism",
    )
    context: Optional[str] = Field(
        None, description="Contexte additionnel (taille des données, fréquence d'appel, etc.)"
    )

    @field_validator("language")
    def validate_language_field(cls, v):
        if not v or not v.strip():
            raise ValueError("Le langage ne peut pas être vide")
        return v.strip().lower()


class PerformanceIssue(BaseModel):
    """Un problème de performance détecté."""

    category: str = Field(
        ...,
        description="Catégorie: cpu, memory, io, network, algorithmic, parallelism",
    )
    severity: str = Field(..., description="Sévérité: info, warning, error, critical")
    line: Optional[int] = Field(None, description="Numéro de ligne")
    title: str = Field(..., description="Titre court")
    description: str = Field(..., description="Description détaillée")
    estimated_complexity: Optional[str] = Field(None, description="Complexité estimée (O(n), O(n²), etc.)")
    suggestion: Optional[str] = Field(None, description="Code optimisé proposé")


class PerformanceAnalysisResponse(BaseModel):
    """Réponse de l'analyse de performance."""

    performance_score: float = Field(..., description="Score de performance global (0.0-1.0)", ge=0.0, le=1.0)
    issues: List[PerformanceIssue] = Field(default_factory=list, description="Problèmes de performance")
    category_scores: Dict[str, float] = Field(default_factory=dict, description="Score par catégorie")
    hotspots: List[Dict[str, Any]] = Field(default_factory=list, description="Points chauds identifiés")
    optimizations: List[str] = Field(default_factory=list, description="Optimisations proposées")
    summary: str = Field(..., description="Résumé de l'analyse")
    language: str = Field(..., description="Langage analysé")
    lines_analyzed: int = Field(0, description="Nombre de lignes analysées")
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
