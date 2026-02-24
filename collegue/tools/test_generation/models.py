"""
Modèles Pydantic pour l'outil Test Generation.
"""
from typing import Optional, Dict, Any, List
from pydantic import BaseModel, Field, field_validator


class TestGenerationRequest(BaseModel):
    """Requête pour la génération de tests."""
    __test__ = False
    
    code: str = Field(..., description="Code à tester", min_length=1)
    language: str = Field(..., description="Langage de programmation du code")
    session_id: Optional[str] = Field(None, description="Identifiant de session")
    test_framework: Optional[str] = Field(None, description="Framework de test à utiliser (unittest, pytest, jest, etc.)")
    file_path: Optional[str] = Field(None, description="Chemin du fichier contenant le code")
    output_dir: Optional[str] = Field(None, description="Répertoire de sortie pour les tests générés")
    include_mocks: Optional[bool] = Field(False, description="Inclure des mocks dans les tests")
    coverage_target: Optional[float] = Field(0.8, description="Couverture de code cible (0.0-1.0)", ge=0.0, le=1.0)

    @field_validator('language')
    def validate_language_field(cls, v):
        if not v or not v.strip():
            raise ValueError("Le langage ne peut pas être vide")
        return v.strip().lower()

    @field_validator('coverage_target')
    def validate_coverage_target(cls, v):
        if v is not None and (v < 0.0 or v > 1.0):
            raise ValueError("La cible de couverture doit être entre 0.0 et 1.0")
        return v


class TestGenerationResponse(BaseModel):
    """Réponse de la génération de tests."""
    __test__ = False
    
    test_code: str = Field(..., description="Code de test généré")
    language: str = Field(..., description="Langage du code de test")
    framework: str = Field(..., description="Framework de test utilisé")
    test_file_path: Optional[str] = Field(None, description="Chemin du fichier de test généré")
    estimated_coverage: float = Field(..., description="Estimation de la couverture de code")
    tested_elements: List[Dict[str, str]] = Field(..., description="Éléments testés (fonctions, classes, etc.)")


class LLMTestGenerationResult(BaseModel):
    """Résultat structuré de la génération de tests par LLM."""
    test_code: str = Field(..., description="Code de test complet et exécutable")
    test_count: int = Field(..., description="Nombre de tests générés")
    coverage_estimate: float = Field(
        default=0.8,
        description="Estimation de la couverture de code (0.0 à 1.0)",
        ge=0.0,
        le=1.0
    )
    tested_functions: List[str] = Field(
        default_factory=list,
        description="Liste des noms de fonctions testées"
    )
    tested_classes: List[str] = Field(
        default_factory=list,
        description="Liste des noms de classes testées"
    )
    imports_required: List[str] = Field(
        default_factory=list,
        description="Imports nécessaires pour les tests"
    )
