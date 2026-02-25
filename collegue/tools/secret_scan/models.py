"""
Modèles Pydantic pour l'outil Secret Scan.
"""
from typing import Optional, List
from pydantic import BaseModel, Field, field_validator


class SecretFinding(BaseModel):
    model_config = {'extra': 'forbid'}
    """Un secret détecté dans le code."""
    type: str = Field(..., description="Type de secret (api_key, password, token, etc.)")
    severity: str = Field(..., description="Sévérité: low, medium, high, critical")
    file: Optional[str] = Field(None, description="Fichier contenant le secret")
    line: Optional[int] = Field(None, description="Numéro de ligne")
    column: Optional[int] = Field(None, description="Numéro de colonne")
    match: str = Field(..., description="Extrait du code (masqué partiellement)")
    rule: str = Field(..., description="Règle de détection déclenchée")
    recommendation: str = Field(..., description="Recommandation pour corriger")


class SecretScanRequest(BaseModel):
    model_config = {'extra': 'forbid'}
    """Requête pour le scan de secrets."""
    target: Optional[str] = Field(
        None,
        description="Cible du scan: fichier ou dossier (utiliser 'content' ou 'files' pour MCP)"
    )
    content: Optional[str] = Field(
        None,
        description="Contenu d'un seul fichier à scanner"
    )
    files: Optional[List] = Field(
        None,
        description="Liste de fichiers à scanner en batch [{path, content}, ...] - RECOMMANDÉ pour MCP"
    )
    scan_type: str = Field(
        "auto",
        description="Type de scan: 'file', 'directory', 'content', 'batch' ou 'auto'"
    )
    language: Optional[str] = Field(
        None,
        description="Langage du code (optionnel, pour filtrer les patterns)"
    )
    include_patterns: Optional[List[str]] = Field(
        None,
        description="Patterns de fichiers à inclure (ex: ['*.py', '*.ts'])"
    )
    exclude_patterns: Optional[List[str]] = Field(
        None,
        description="Patterns de fichiers à exclure (ex: ['*.min.js', 'node_modules/*'])"
    )
    severity_threshold: Optional[str] = Field(
        "low",
        description="Seuil de sévérité minimum: 'low', 'medium', 'high', 'critical'"
    )
    max_file_size: Optional[int] = Field(
        1024 * 1024,
        description="Taille max des fichiers à scanner (bytes)",
        ge=1024,
        le=10 * 1024 * 1024
    )

    @field_validator('scan_type')
    def validate_scan_type(cls, v):
        valid = ['auto', 'file', 'directory', 'content', 'batch']
        if v not in valid:
            raise ValueError(f"Type de scan '{v}' invalide. Utilisez: {', '.join(valid)}")
        return v

    @field_validator('severity_threshold')
    def validate_severity(cls, v):
        valid = ['low', 'medium', 'high', 'critical']
        if v not in valid:
            raise ValueError(f"Sévérité '{v}' invalide. Utilisez: {', '.join(valid)}")
        return v

    def model_post_init(self, __context):
        """Valide qu'au moins une source de données est fournie."""
        if not self.target and not self.content and not self.files:
            raise ValueError(
                "Vous devez fournir 'target' (chemin), 'content' (code), "
                "ou 'files' (liste de fichiers pour scan batch)"
            )


class SecretScanResponse(BaseModel):
    model_config = {'extra': 'forbid'}
    """Réponse du scan de secrets."""
    clean: bool = Field(..., description="True si aucun secret trouvé")
    total_findings: int = Field(..., description="Nombre total de secrets détectés")
    critical: int = Field(0, description="Nombre de secrets critiques")
    high: int = Field(0, description="Nombre de secrets haute sévérité")
    medium: int = Field(0, description="Nombre de secrets moyenne sévérité")
    low: int = Field(0, description="Nombre de secrets basse sévérité")
    files_scanned: int = Field(..., description="Nombre de fichiers scannés")
    files_with_secrets: List[str] = Field(
        default_factory=list,
        description="Liste des fichiers contenant des secrets"
    )
    findings: List[SecretFinding] = Field(
        default_factory=list,
        description="Liste des secrets trouvés (max 100)"
    )
    scan_summary: str = Field(..., description="Résumé du scan")
