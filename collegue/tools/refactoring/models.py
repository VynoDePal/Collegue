"""
Modèles Pydantic pour l'outil Refactoring.
"""
from typing import Optional, Dict, Any, List
from pydantic import BaseModel, Field


class RefactoringRequest(BaseModel):
    model_config = {'extra': 'forbid'}
    """Requête pour le refactoring de code."""
    code: str = Field(..., description="Code à refactorer")
    language: str = Field(..., description="Langage de programmation du code")
    session_id: Optional[str] = Field(None, description="Identifiant de session")
    refactoring_type: str = Field(..., description="Type de refactoring à appliquer (rename, extract, simplify, optimize, clean)")
    parameters: Optional[Dict[str, Any]] = Field(None, description="Paramètres spécifiques au type de refactoring")
    file_path: Optional[str] = Field(None, description="Chemin du fichier contenant le code")


class RefactoringResponse(BaseModel):
    model_config = {'extra': 'forbid'}
    """Réponse du refactoring de code."""
    refactored_code: str = Field(..., description="Code refactoré")
    original_code: str = Field(..., description="Code original")
    language: str = Field(..., description="Langage du code")
    changes: List[Dict[str, Any]] = Field(..., description="Description des changements effectués")
    explanation: str = Field(..., description="Explication des modifications apportées")
    improvement_metrics: Optional[Dict[str, Any]] = Field(None, description="Métriques d'amélioration")


class LLMRefactoringResult(BaseModel):
    model_config = {'extra': 'forbid'}
    """Résultat structuré du refactoring par LLM."""
    refactored_code: str = Field(..., description="Code refactoré complet")
    changes_summary: str = Field(..., description="Résumé des changements effectués")
    changes_count: int = Field(default=0, description="Nombre de modifications")
    improved_areas: List[str] = Field(
        default_factory=list,
        description="Liste des aspects améliorés (lisibilité, performance, etc.)"
    )
    complexity_reduction: float = Field(
        default=0.0,
        description="Estimation de la réduction de complexité (0.0 à 1.0)",
        ge=0.0,
        le=1.0
    )
