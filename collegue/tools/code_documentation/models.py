"""
Modèles Pydantic pour l'outil Code Documentation.
"""

from typing import Dict, List, Optional

from pydantic import BaseModel, Field


class DocumentationRequest(BaseModel):
    """Requête pour la génération de documentation."""

    code: str = Field(..., description="Code à documenter")
    language: str = Field(..., description="Langage de programmation du code")
    session_id: Optional[str] = Field(None, description="Identifiant de session")
    doc_style: Optional[str] = Field(
        "standard", description="Style de documentation (standard, detailed, minimal, api)"
    )
    doc_format: Optional[str] = Field(
        "markdown", description="Format de documentation (markdown, rst, html, docstring)"
    )
    include_examples: Optional[bool] = Field(False, description="Inclure des exemples d'utilisation")
    file_path: Optional[str] = Field(None, description="Chemin du fichier contenant le code")
    focus_on: Optional[str] = Field(None, description="Éléments à documenter (functions, classes, modules, all)")


class DocumentationResponse(BaseModel):
    """Réponse de la génération de documentation."""

    documentation: str = Field(..., description="Documentation générée")
    language: str = Field(..., description="Langage du code documenté")
    format: str = Field(..., description="Format de la documentation")
    documented_elements: List[Dict[str, str]] = Field(..., description="Éléments documentés (fonctions, classes, etc.)")
    coverage: float = Field(..., description="Pourcentage du code couvert par la documentation")
    suggestions: Optional[List[str]] = Field(None, description="Suggestions d'amélioration de la documentation")
    # Champs agentiques (optionnels pour rétrocompatibilité)
    agent_iterations: int = Field(default=0, description="Nombre d'itérations agentiques effectuées")
    agent_best_score: Optional[float] = Field(default=None, description="Meilleur score de qualité atteint (0.0-1.0)")
    agent_converged: Optional[bool] = Field(default=None, description="True si la boucle a convergé")
