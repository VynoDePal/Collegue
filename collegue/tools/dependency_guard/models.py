"""
Modèles Pydantic pour l'outil Dependency Guard.
"""
from typing import Optional, List
from pydantic import BaseModel, Field, field_validator


class DependencyIssue(BaseModel):
    model_config = {'extra': 'forbid'}
    """Un problème détecté sur une dépendance."""
    package: str = Field(..., description="Nom du package")
    version: Optional[str] = Field(None, description="Version concernée")
    issue_type: str = Field(..., description="Type: not_found, vulnerable, deprecated, blocked, version_conflict")
    severity: str = Field(..., description="Sévérité: low, medium, high, critical")
    message: str = Field(..., description="Description du problème")
    recommendation: str = Field(..., description="Recommandation pour corriger")
    cve_ids: Optional[List[str]] = Field(None, description="IDs CVE si vulnérabilité")


class DependencyGuardRequest(BaseModel):
    model_config = {'extra': 'forbid'}
    """Requête pour la validation des dépendances."""
    content: str = Field(
        ...,
        description="Contenu du fichier de dépendances (package-lock.json, requirements.txt, pyproject.toml). Le type est auto-détecté."
    )
    language: str = Field(
        ...,
        description="Langage: python ou typescript/javascript"
    )
    check_vulnerabilities: Optional[bool] = Field(
        True,
        description="Vérifier les vulnérabilités connues (CVEs) via l'API OSV de Google"
    )
    check_existence: Optional[bool] = Field(
        True,
        description="Vérifier que les packages existent sur le registre"
    )
    allowlist: Optional[List[str]] = Field(
        None,
        description="Liste blanche de packages autorisés"
    )
    blocklist: Optional[List[str]] = Field(
        None,
        description="Liste noire de packages interdits"
    )

    @field_validator('language')
    def validate_language(cls, v):
        v = v.strip().lower()
        if v in ['typescript', 'javascript', 'js', 'ts']:
            return 'javascript'
        if v not in ['python', 'javascript', 'php']:
            raise ValueError(f"Langage '{v}' non supporté. Utilisez: python, typescript, javascript, php")
        return v


class DependencyGuardResponse(BaseModel):
    model_config = {'extra': 'forbid'}
    """Réponse de la validation des dépendances."""
    valid: bool = Field(..., description="True si aucune vulnérabilité critique/haute")
    summary: str = Field(..., description="Résumé de l'analyse")
    total_dependencies: int = Field(..., description="Nombre total de dépendances analysées")
    vulnerabilities: int = Field(0, description="Nombre de vulnérabilités détectées")
    critical: int = Field(0, description="Vulnérabilités critiques")
    high: int = Field(0, description="Vulnérabilités hautes")
    medium: int = Field(0, description="Vulnérabilités moyennes")
    low: int = Field(0, description="Vulnérabilités basses")
    issues: List[DependencyIssue] = Field(
        default_factory=list,
        description="Liste des problèmes détectés (vulnérabilités, packages bloqués, etc.)"
    )
