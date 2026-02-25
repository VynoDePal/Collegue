"""
Modèles Pydantic pour l'outil IaC Guardrails Scan.
"""
from typing import Optional, Dict, Any, List
from pydantic import BaseModel, Field, field_validator
from ...core.shared import FileInput, validate_fast_deep


class CustomPolicy(BaseModel):
    model_config = {'extra': 'forbid'}
    """Policy personnalisée pour le scan IaC."""
    id: str = Field(..., description="Identifiant unique de la policy")
    description: Optional[str] = Field(None, description="Description de la policy")
    content: str = Field(..., description="Contenu de la règle (regex ou YAML)")
    language: str = Field("yaml-rules", description="Format: 'regex' ou 'yaml-rules'")
    severity: str = Field("medium", description="Sévérité: low, medium, high, critical")


class IacGuardrailsRequest(BaseModel):
    model_config = {'extra': 'forbid'}
    """Requête pour le scan IaC Guardrails."""
    files: List[FileInput] = Field(
        ...,
        description="Liste des fichiers IaC à scanner [{path, content}, ...]",
        min_length=1
    )
    policy_profile: str = Field(
        "baseline",
        description="Profil de policy: 'baseline' (recommandé) ou 'strict' (plus restrictif)"
    )
    platform: Optional[Dict[str, str]] = Field(
        None,
        description="Plateforme cible: {cloud?: 'aws'|'gcp'|'azure', k8s_version?: '1.28'}"
    )
    engines: List[str] = Field(
        ["embedded-rules"],
        description="Moteurs à utiliser: 'embedded-rules', 'opa-lite'"
    )
    custom_policies: Optional[List[CustomPolicy]] = Field(
        None,
        description="Policies personnalisées à ajouter"
    )
    output_format: str = Field(
        "json",
        description="Format de sortie: 'json' ou 'sarif'"
    )
    analysis_depth: str = Field(
        "fast",
        description="Profondeur IA: 'fast' (règles seules) ou 'deep' (enrichissement LLM avec scoring)"
    )
    auto_chain: bool = Field(
        False,
        description="Si True et security_score < seuil, déclenche automatiquement la remédiation"
    )
    remediation_threshold: float = Field(
        0.5,
        description="Seuil de security_score (0.0-1.0) sous lequel déclencher auto_chain",
        ge=0.0,
        le=1.0
    )

    @field_validator('policy_profile')
    def validate_profile(cls, v):
        valid = ['baseline', 'strict']
        if v not in valid:
            raise ValueError(f"Profil '{v}' invalide. Utilisez: {', '.join(valid)}")
        return v

    @field_validator('engines')
    def validate_engines(cls, v):
        valid = ['embedded-rules', 'opa-lite']
        for engine in v:
            if engine not in valid:
                raise ValueError(f"Engine '{engine}' invalide. Utilisez: {', '.join(valid)}")
        return v

    @field_validator('analysis_depth')
    def validate_analysis_depth(cls, v):
        return validate_fast_deep(v)


class IacFinding(BaseModel):
    model_config = {'extra': 'forbid'}
    """Un finding de sécurité détecté."""
    rule_id: str = Field(..., description="Identifiant de la règle")
    severity: str = Field(..., description="Sévérité: low, medium, high, critical")
    path: str = Field(..., description="Chemin du fichier")
    line: Optional[int] = Field(None, description="Numéro de ligne")
    resource: Optional[str] = Field(None, description="Ressource concernée")
    title: str = Field(..., description="Titre court du problème")
    description: str = Field(..., description="Description détaillée")
    remediation: str = Field(..., description="Comment corriger")
    references: List[str] = Field(default_factory=list, description="Liens de référence")
    engine: str = Field("embedded-rules", description="Moteur qui a détecté")


class LLMSecurityInsight(BaseModel):
    model_config = {'extra': 'forbid'}
    """Insight généré par le LLM en mode deep analysis."""
    category: str = Field(..., description="Catégorie: vulnerability, misconfiguration, compliance, best_practice")
    insight: str = Field(..., description="L'insight détaillé")
    risk_level: str = Field("medium", description="Niveau de risque: low, medium, high, critical")
    affected_resources: List[str] = Field(default_factory=list, description="Ressources concernées")
    compliance_frameworks: List[str] = Field(default_factory=list, description="Standards impactés: CIS, SOC2, HIPAA, etc.")


class RemediationAction(BaseModel):
    model_config = {'extra': 'forbid'}
    """Action de remédiation suggérée."""
    tool_name: str = Field(..., description="Nom du tool à appeler (ex: code_refactoring)")
    action_type: str = Field(..., description="Type: fix_config, add_security, remove_exposure")
    rationale: str = Field(..., description="Pourquoi cette action")
    priority: str = Field("medium", description="Priorité: low, medium, high, critical")
    params: Dict[str, Any] = Field(default_factory=dict, description="Paramètres pour le tool")
    score: float = Field(0.0, description="Score de pertinence (0.0-1.0)", ge=0.0, le=1.0)


class IacGuardrailsResponse(BaseModel):
    model_config = {'extra': 'forbid'}
    """Réponse du scan IaC Guardrails."""
    passed: bool = Field(..., description="True si aucun problème critique/high")
    summary: Dict[str, int] = Field(
        ...,
        description="Résumé: {total, critical, high, medium, low, passed, failed, skipped}"
    )
    findings: List[IacFinding] = Field(
        default_factory=list,
        description="Liste des problèmes détectés"
    )
    files_scanned: int = Field(..., description="Nombre de fichiers scannés")
    rules_evaluated: int = Field(..., description="Nombre de règles évaluées")
    scan_summary: str = Field(..., description="Résumé du scan")
    sarif: Optional[Dict] = Field(None, description="Sortie SARIF si demandée")

    analysis_depth_used: str = Field("fast", description="Profondeur d'analyse utilisée")
    llm_insights: Optional[List[LLMSecurityInsight]] = Field(
        None,
        description="Insights IA (mode deep): vulnérabilités, compliance, best practices"
    )

    security_score: float = Field(
        1.0,
        description="Score de sécurité global (0.0=critique, 1.0=sécurisé)",
        ge=0.0,
        le=1.0
    )
    compliance_score: float = Field(
        1.0,
        description="Score de conformité (0.0=non conforme, 1.0=conforme)",
        ge=0.0,
        le=1.0
    )
    risk_level: str = Field(
        "low",
        description="Niveau de risque global: low, medium, high, critical"
    )
    suggested_remediations: List[RemediationAction] = Field(
        default_factory=list,
        description="Actions de remédiation suggérées"
    )

    auto_remediation_triggered: bool = Field(
        False,
        description="True si la remédiation automatique a été déclenchée"
    )
    auto_remediation_result: Optional[Dict[str, Any]] = Field(
        None,
        description="Résultat de la remédiation automatique (si déclenchée)"
    )
