"""
Modèles Pydantic pour l'outil Code Review.
"""

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator


class CodeReviewRequest(BaseModel):
    """Requête pour la revue de code."""

    code: str = Field(..., description="Code à reviewer", min_length=1)
    language: str = Field(..., description="Langage de programmation du code")
    session_id: Optional[str] = Field(None, description="Identifiant de session")
    file_path: Optional[str] = Field(None, description="Chemin du fichier")
    review_standards: List[str] = Field(
        default_factory=lambda: ["naming", "complexity", "security", "dry", "solid"],
        description="Standards à vérifier: naming, complexity, security, performance, dry, solid, error_handling",
    )
    severity_threshold: str = Field(
        "info",
        description="Sévérité minimale à reporter: info, warning, error, critical",
    )
    context: Optional[str] = Field(
        None,
        description="Contexte additionnel (PR description, ticket, etc.)",
    )

    @field_validator("language")
    def validate_language_field(cls, v):
        if not v or not v.strip():
            raise ValueError("Le langage ne peut pas être vide")
        return v.strip().lower()

    @field_validator("severity_threshold")
    def validate_severity(cls, v):
        valid = ["info", "warning", "error", "critical"]
        if v not in valid:
            raise ValueError(f"Sévérité '{v}' invalide. Options: {', '.join(valid)}")
        return v


class ReviewFinding(BaseModel):
    """Un problème détecté lors de la revue."""

    category: str = Field(
        ...,
        description="Catégorie: naming, complexity, security, performance, dry, solid, error_handling, style",
    )
    severity: str = Field(..., description="Sévérité: info, warning, error, critical")
    line: Optional[int] = Field(None, description="Numéro de ligne")
    title: str = Field(..., description="Titre court du problème")
    description: str = Field(..., description="Description détaillée")
    suggestion: Optional[str] = Field(None, description="Code corrigé proposé")


class CodeReviewResponse(BaseModel):
    """Réponse de la revue de code."""

    quality_score: float = Field(..., description="Score de qualité global (0.0-1.0)", ge=0.0, le=1.0)
    findings: List[ReviewFinding] = Field(default_factory=list, description="Problèmes détectés")
    summary: str = Field(..., description="Résumé de la revue")
    category_scores: Dict[str, float] = Field(
        default_factory=dict,
        description="Score par catégorie (0.0-1.0)",
    )
    strengths: List[str] = Field(default_factory=list, description="Points forts du code")
    recommendations: List[str] = Field(default_factory=list, description="Recommandations d'amélioration")
    language: str = Field(..., description="Langage du code reviewé")
    lines_reviewed: int = Field(0, description="Nombre de lignes analysées")
    # Champs agentiques
    agent_iterations: int = Field(default=0, description="Nombre d'itérations agentiques")
    agent_best_score: Optional[float] = Field(default=None, description="Meilleur score de qualité atteint")
    agent_converged: Optional[bool] = Field(default=None, description="True si la boucle a convergé")
    delegation_triggered: bool = Field(
        default=False, description="True si une délégation inter-experts a été déclenchée"
    )
    delegation_results: Optional[List[Dict[str, Any]]] = Field(
        default=None, description="Résultats des délégations inter-experts"
    )
