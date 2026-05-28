"""
Modèles Pydantic pour l'outil Expert Dashboard.
"""

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class DashboardRequest(BaseModel):
    """Requête pour le tableau de bord des experts."""

    session_id: Optional[str] = Field(None, description="Identifiant de session")
    include_memory: bool = Field(True, description="Inclure les données de la mémoire projet")
    include_recommendations: bool = Field(True, description="Inclure les recommandations")
    top_recommendations: int = Field(10, description="Nombre max de recommandations")
    language_filter: Optional[str] = Field(None, description="Filtrer par langage")


class ExpertStatus(BaseModel):
    """Statut d'un expert."""

    name: str = Field(..., description="Nom de l'expert")
    total_executions: int = Field(0, description="Nombre total d'exécutions")
    last_score: Optional[float] = Field(None, description="Dernier score")
    categories: List[str] = Field(default_factory=list, description="Catégories couvertes")
    recent_findings: int = Field(0, description="Findings récents en mémoire")


class Recommendation(BaseModel):
    """Une recommandation d'expert."""

    expert: str = Field(..., description="Expert source")
    priority: int = Field(5, description="Priorité (1-10)")
    title: str = Field(..., description="Titre")
    description: str = Field("", description="Description")
    category: str = Field("", description="Catégorie")
    file_path: Optional[str] = Field(None, description="Fichier concerné")


class ProjectHealth(BaseModel):
    """Santé globale du projet."""

    overall_score: float = Field(0.0, description="Score global (0.0-1.0)", ge=0.0, le=1.0)
    quality_score: Optional[float] = Field(None, description="Score qualité code")
    architecture_score: Optional[float] = Field(None, description="Score architecture")
    performance_score: Optional[float] = Field(None, description="Score performance")
    security_score: Optional[float] = Field(None, description="Score sécurité")


class DelegationActivity(BaseModel):
    """Activité de délégation récente."""

    total_chains: int = Field(0, description="Nombre total de chaînes")
    total_rules: int = Field(0, description="Nombre total de règles")
    most_active_source: Optional[str] = Field(None, description="Expert source le plus actif")
    most_active_target: Optional[str] = Field(None, description="Expert cible le plus actif")


class DashboardResponse(BaseModel):
    """Réponse du tableau de bord."""

    project_health: ProjectHealth = Field(default_factory=ProjectHealth, description="Santé du projet")
    expert_statuses: List[ExpertStatus] = Field(default_factory=list, description="Statut de chaque expert")
    recommendations: List[Recommendation] = Field(
        default_factory=list, description="Recommandations triées par priorité"
    )
    delegation_activity: DelegationActivity = Field(
        default_factory=DelegationActivity, description="Activité de délégation"
    )
    memory_stats: Dict[str, Any] = Field(default_factory=dict, description="Statistiques de la mémoire")
    monitor_stats: Dict[str, Any] = Field(default_factory=dict, description="Statistiques du moniteur proactif")
    metrics: Dict[str, Any] = Field(
        default_factory=dict,
        description="Métriques de performance des experts (latence, coûts, erreurs)",
    )
    summary: str = Field("", description="Résumé textuel")
