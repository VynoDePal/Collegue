"""
IaC Guardrails Scan - Outil de scan de sécurité pour Infrastructure as Code

Cet outil scanne les fichiers IaC (Terraform, Kubernetes, Dockerfile) pour détecter:
- Violations du principe de moindre privilège
- Configurations par défaut dangereuses
- Expositions réseau non sécurisées
- Secrets hardcodés dans l'infrastructure

Problème résolu: L'IA génère souvent des configurations IaC avec des defaults dangereux
(privilèges excessifs, ports ouverts, images root, etc.).
Valeur: Empêche les erreurs de sécurité infra coûteuses.
Bénéfice: Rend l'IA utilisable pour IaC sans "roulette russe" sécurité.
"""
import re
import yaml
import asyncio
import json
from typing import Optional, Dict, Any, List, Type, Tuple
from pydantic import BaseModel, Field, field_validator
from .base import BaseTool, ToolError, ToolValidationError, ToolExecutionError


class FileInput(BaseModel):
    """Un fichier avec son chemin et contenu."""
    path: str = Field(..., description="Chemin relatif du fichier")
    content: str = Field(..., description="Contenu du fichier")


class CustomPolicy(BaseModel):
    """Une policy personnalisée."""
    id: str = Field(..., description="Identifiant unique de la policy")
    description: Optional[str] = Field(None, description="Description de la policy")
    content: str = Field(..., description="Contenu de la règle (regex ou YAML)")
    language: str = Field("yaml-rules", description="Format: 'regex' ou 'yaml-rules'")
    severity: str = Field("medium", description="Sévérité: low, medium, high, critical")


class IacGuardrailsRequest(BaseModel):
    """Modèle de requête pour le scan IaC."""
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
        valid = ['fast', 'deep']
        if v not in valid:
            raise ValueError(f"Depth '{v}' invalide. Utilisez: {', '.join(valid)}")
        return v


class IacFinding(BaseModel):
    """Un problème de sécurité IaC détecté."""
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
    """Un insight de sécurité généré par l'IA en mode deep."""
    category: str = Field(..., description="Catégorie: vulnerability, misconfiguration, compliance, best_practice")
    insight: str = Field(..., description="L'insight détaillé")
    risk_level: str = Field("medium", description="Niveau de risque: low, medium, high, critical")
    affected_resources: List[str] = Field(default_factory=list, description="Ressources concernées")
    compliance_frameworks: List[str] = Field(default_factory=list, description="Standards impactés: CIS, SOC2, HIPAA, etc.")


class RemediationAction(BaseModel):
    """Une action de remédiation suggérée (potentiellement auto-exécutable)."""
    tool_name: str = Field(..., description="Nom du tool à appeler (ex: code_refactoring)")
    action_type: str = Field(..., description="Type: fix_config, add_security, remove_exposure")
    rationale: str = Field(..., description="Pourquoi cette action")
    priority: str = Field("medium", description="Priorité: low, medium, high, critical")
    params: Dict[str, Any] = Field(default_factory=dict, description="Paramètres pour le tool")
    score: float = Field(0.0, description="Score de pertinence (0.0-1.0)", ge=0.0, le=1.0)


class IacGuardrailsResponse(BaseModel):
    """Modèle de réponse pour le scan IaC."""
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
    # Champs enrichis par IA (mode deep)
    analysis_depth_used: str = Field("fast", description="Profondeur d'analyse utilisée")
    llm_insights: Optional[List[LLMSecurityInsight]] = Field(
        None,
        description="Insights IA (mode deep): vulnérabilités, compliance, best practices"
    )
    # Scoring sécurité
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
    # Résultat du chaînage automatique
    auto_remediation_triggered: bool = Field(
        False,
        description="True si la remédiation automatique a été déclenchée"
    )
    auto_remediation_result: Optional[Dict[str, Any]] = Field(
        None,
        description="Résultat de la remédiation automatique (si déclenchée)"
    )


class IacGuardrailsScanTool(BaseTool):
    """
    Outil de scan de sécurité pour Infrastructure as Code.
    
    Scanne Terraform, Kubernetes YAML, et Dockerfiles pour détecter:
    - Privilèges excessifs (least privilege violations)
    - Configurations dangereuses par défaut
    - Expositions réseau (0.0.0.0/0, ports dangereux)
    - Containers root, capabilities dangereuses
    - Secrets et credentials hardcodés
    
    Basé sur les Pod Security Standards (K8s) et les best practices Terraform/Docker.
    Supporte des policies personnalisées (Option B).
    """

    # ==================== KUBERNETES RULES ====================
    # Basées sur Pod Security Standards (PSS) Baseline & Restricted
    
    K8S_RULES = {
        'baseline': [
            {
                'id': 'K8S-001',
                'title': 'Container running as privileged',
                'severity': 'critical',
                'pattern': r'privileged:\s*true',
                'description': 'Les containers privilégiés désactivent la plupart des mécanismes de sécurité.',
                'remediation': 'Définir privileged: false ou supprimer le champ.',
                'references': ['https://kubernetes.io/docs/concepts/security/pod-security-standards/'],
            },
            {
                'id': 'K8S-002',
                'title': 'Host network enabled',
                'severity': 'high',
                'pattern': r'hostNetwork:\s*true',
                'description': 'Partager le namespace réseau de l\'hôte expose le pod aux attaques réseau.',
                'remediation': 'Supprimer hostNetwork ou le définir à false.',
                'references': ['https://kubernetes.io/docs/concepts/security/pod-security-standards/'],
            },
            {
                'id': 'K8S-003',
                'title': 'Host PID namespace enabled',
                'severity': 'high',
                'pattern': r'hostPID:\s*true',
                'description': 'Partager le namespace PID permet de voir/tuer les processus de l\'hôte.',
                'remediation': 'Supprimer hostPID ou le définir à false.',
                'references': ['https://kubernetes.io/docs/concepts/security/pod-security-standards/'],
            },
            {
                'id': 'K8S-004',
                'title': 'Host IPC namespace enabled',
                'severity': 'high',
                'pattern': r'hostIPC:\s*true',
                'description': 'Partager le namespace IPC permet l\'accès à la mémoire partagée de l\'hôte.',
                'remediation': 'Supprimer hostIPC ou le définir à false.',
                'references': ['https://kubernetes.io/docs/concepts/security/pod-security-standards/'],
            },
            {
                'id': 'K8S-005',
                'title': 'HostPath volume mount',
                'severity': 'high',
                'pattern': r'hostPath:\s*\n\s+path:',
                'description': 'Les volumes hostPath exposent le filesystem de l\'hôte.',
                'remediation': 'Utiliser des volumes persistants (PVC) au lieu de hostPath.',
                'references': ['https://kubernetes.io/docs/concepts/security/pod-security-standards/'],
            },
            {
                'id': 'K8S-006',
                'title': 'Dangerous capabilities added',
                'severity': 'high',
                'pattern': r'capabilities:\s*\n\s+add:\s*\n\s+-\s*(SYS_ADMIN|NET_ADMIN|SYS_PTRACE|CAP_SYS_ADMIN)',
                'description': 'Ces capabilities permettent des opérations privilégiées dangereuses.',
                'remediation': 'Supprimer les capabilities dangereuses ou utiliser des alternatives sécurisées.',
                'references': ['https://kubernetes.io/docs/concepts/security/pod-security-standards/'],
            },
            {
                'id': 'K8S-007',
                'title': 'Container without resource limits',
                'severity': 'medium',
                'pattern': r'containers:\s*\n(?:(?!limits:).)*?name:',
                'description': 'Sans limites de ressources, un container peut consommer toutes les ressources du node.',
                'remediation': 'Définir resources.limits.cpu et resources.limits.memory.',
                'references': ['https://kubernetes.io/docs/concepts/configuration/manage-resources-containers/'],
            },
            {
                'id': 'K8S-008',
                'title': 'Image tag latest or missing',
                'severity': 'medium',
                'pattern': r'image:\s*[\w\-./]+(?::latest)?(?:\s|$)',
                'description': 'Utiliser latest ou pas de tag rend les déploiements non reproductibles.',
                'remediation': 'Spécifier un tag de version spécifique (ex: image: nginx:1.25.3).',
                'references': ['https://kubernetes.io/docs/concepts/containers/images/'],
            },
        ],
        'strict': [
            {
                'id': 'K8S-101',
                'title': 'Container not running as non-root',
                'severity': 'high',
                'pattern': r'(?!.*runAsNonRoot:\s*true)',
                'check_type': 'absence',
                'search_scope': 'securityContext',
                'description': 'Les containers devraient s\'exécuter en tant qu\'utilisateur non-root.',
                'remediation': 'Ajouter securityContext.runAsNonRoot: true.',
                'references': ['https://kubernetes.io/docs/concepts/security/pod-security-standards/'],
            },
            {
                'id': 'K8S-102',
                'title': 'AllowPrivilegeEscalation not disabled',
                'severity': 'high',
                'pattern': r'(?!.*allowPrivilegeEscalation:\s*false)',
                'check_type': 'absence',
                'search_scope': 'securityContext',
                'description': 'L\'escalade de privilèges devrait être explicitement désactivée.',
                'remediation': 'Ajouter securityContext.allowPrivilegeEscalation: false.',
                'references': ['https://kubernetes.io/docs/concepts/security/pod-security-standards/'],
            },
            {
                'id': 'K8S-103',
                'title': 'Capabilities not dropped',
                'severity': 'medium',
                'pattern': r'(?!.*capabilities:\s*\n\s+drop:\s*\n\s+-\s*ALL)',
                'check_type': 'absence',
                'search_scope': 'securityContext',
                'description': 'Les capabilities devraient être explicitement supprimées.',
                'remediation': 'Ajouter securityContext.capabilities.drop: ["ALL"].',
                'references': ['https://kubernetes.io/docs/concepts/security/pod-security-standards/'],
            },
            {
                'id': 'K8S-104',
                'title': 'ReadOnlyRootFilesystem not enabled',
                'severity': 'medium',
                'pattern': r'(?!.*readOnlyRootFilesystem:\s*true)',
                'check_type': 'absence',
                'search_scope': 'securityContext',
                'description': 'Le filesystem root devrait être en lecture seule.',
                'remediation': 'Ajouter securityContext.readOnlyRootFilesystem: true.',
                'references': ['https://kubernetes.io/docs/concepts/security/pod-security-standards/'],
            },
        ],
    }

    # ==================== TERRAFORM RULES ====================
    
    TF_RULES = {
        'baseline': [
            {
                'id': 'TF-001',
                'title': 'Security group allows all inbound traffic',
                'severity': 'critical',
                'pattern': r'cidr_blocks\s*=\s*\[\s*"0\.0\.0\.0/0"\s*\]',
                'description': 'La règle autorise le trafic depuis n\'importe quelle adresse IP.',
                'remediation': 'Restreindre cidr_blocks aux IPs nécessaires uniquement.',
                'references': ['https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/security_group'],
            },
            {
                'id': 'TF-002',
                'title': 'S3 bucket with public access',
                'severity': 'critical',
                'pattern': r'acl\s*=\s*"public-read(?:-write)?"',
                'description': 'Le bucket S3 est accessible publiquement.',
                'remediation': 'Utiliser acl = "private" et configurer des policies explicites.',
                'references': ['https://docs.aws.amazon.com/AmazonS3/latest/userguide/access-control-block-public-access.html'],
            },
            {
                'id': 'TF-003',
                'title': 'RDS instance publicly accessible',
                'severity': 'critical',
                'pattern': r'publicly_accessible\s*=\s*true',
                'description': 'L\'instance RDS est accessible depuis Internet.',
                'remediation': 'Définir publicly_accessible = false.',
                'references': ['https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/CHAP_SettingUp.html'],
            },
            {
                'id': 'TF-004',
                'title': 'SSH port open to world',
                'severity': 'critical',
                'pattern': r'(?:from_port|to_port)\s*=\s*22[\s\S]*?cidr_blocks\s*=\s*\[\s*"0\.0\.0\.0/0"\s*\]',
                'description': 'Le port SSH (22) est ouvert à tout Internet.',
                'remediation': 'Restreindre l\'accès SSH aux IPs de confiance ou utiliser un bastion.',
                'references': ['https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/authorizing-access-to-an-instance.html'],
            },
            {
                'id': 'TF-005',
                'title': 'Encryption at rest not enabled',
                'severity': 'high',
                'pattern': r'(?:aws_db_instance|aws_ebs_volume|aws_s3_bucket)[\s\S]*?(?!encrypted\s*=\s*true)',
                'check_type': 'absence',
                'description': 'Le chiffrement au repos n\'est pas activé.',
                'remediation': 'Ajouter encrypted = true pour les ressources de stockage.',
                'references': ['https://docs.aws.amazon.com/whitepapers/latest/introduction-aws-security/data-encryption.html'],
            },
            {
                'id': 'TF-006',
                'title': 'IAM policy with wildcard actions',
                'severity': 'high',
                'pattern': r'"Action"\s*:\s*(?:\[\s*)?"[\w:]*\*"',
                'description': 'La policy IAM utilise des actions wildcard (*), violant le principe du moindre privilège.',
                'remediation': 'Spécifier les actions exactes nécessaires au lieu de wildcards.',
                'references': ['https://docs.aws.amazon.com/IAM/latest/UserGuide/best-practices.html'],
            },
            {
                'id': 'TF-007',
                'title': 'IAM policy with wildcard resources',
                'severity': 'high',
                'pattern': r'"Resource"\s*:\s*(?:\[\s*)?"\*"',
                'description': 'La policy IAM s\'applique à toutes les ressources (*).',
                'remediation': 'Spécifier les ARN des ressources exactes.',
                'references': ['https://docs.aws.amazon.com/IAM/latest/UserGuide/best-practices.html'],
            },
            {
                'id': 'TF-008',
                'title': 'Hardcoded secret in Terraform',
                'severity': 'critical',
                'pattern': r'(?:password|secret|api_key|access_key)\s*=\s*"[^"$]{8,}"',
                'description': 'Un secret semble être hardcodé dans le code Terraform.',
                'remediation': 'Utiliser des variables sensibles, Vault, ou AWS Secrets Manager.',
                'references': ['https://developer.hashicorp.com/terraform/tutorials/configuration-language/sensitive-variables'],
            },
        ],
        'strict': [
            {
                'id': 'TF-101',
                'title': 'CloudTrail logging not enabled',
                'severity': 'medium',
                'pattern': r'aws_cloudtrail',
                'check_type': 'absence_in_project',
                'description': 'CloudTrail n\'est pas configuré pour l\'audit.',
                'remediation': 'Configurer aws_cloudtrail pour auditer les actions API.',
                'references': ['https://docs.aws.amazon.com/awscloudtrail/latest/userguide/cloudtrail-create-and-update-a-trail.html'],
            },
            {
                'id': 'TF-102',
                'title': 'VPC flow logs not enabled',
                'severity': 'medium',
                'pattern': r'aws_vpc(?![\s\S]*?aws_flow_log)',
                'description': 'Les VPC Flow Logs ne sont pas activés.',
                'remediation': 'Configurer aws_flow_log pour chaque VPC.',
                'references': ['https://docs.aws.amazon.com/vpc/latest/userguide/flow-logs.html'],
            },
        ],
    }

    # ==================== DOCKERFILE RULES ====================
    
    DOCKERFILE_RULES = {
        'baseline': [
            {
                'id': 'DOCKER-001',
                'title': 'Running as root user',
                'severity': 'high',
                'pattern': r'^(?!.*USER\s+(?!root)\w+).*$',
                'check_type': 'absence',
                'description': 'Le container s\'exécute en tant que root par défaut.',
                'remediation': 'Ajouter USER <non-root-user> après l\'installation des dépendances.',
                'references': ['https://docs.docker.com/develop/develop-images/instructions/#user'],
            },
            {
                'id': 'DOCKER-002',
                'title': 'Using latest tag',
                'severity': 'medium',
                'pattern': r'FROM\s+[\w\-./]+:latest',
                'description': 'L\'image de base utilise le tag latest, ce qui rend les builds non reproductibles.',
                'remediation': 'Spécifier une version spécifique (ex: FROM python:3.11-slim).',
                'references': ['https://docs.docker.com/develop/develop-images/instructions/#from'],
            },
            {
                'id': 'DOCKER-003',
                'title': 'No tag specified for base image',
                'severity': 'medium',
                'pattern': r'FROM\s+[\w\-./]+\s*$',
                'description': 'L\'image de base n\'a pas de tag, ce qui équivaut à latest.',
                'remediation': 'Spécifier un tag de version explicite.',
                'references': ['https://docs.docker.com/develop/develop-images/instructions/#from'],
            },
            {
                'id': 'DOCKER-004',
                'title': 'ADD used instead of COPY',
                'severity': 'low',
                'pattern': r'^ADD\s+(?!https?://)',
                'description': 'ADD a un comportement auto-extract qui peut être dangereux. COPY est préféré.',
                'remediation': 'Utiliser COPY au lieu de ADD pour les fichiers locaux.',
                'references': ['https://docs.docker.com/develop/develop-images/instructions/#add-or-copy'],
            },
            {
                'id': 'DOCKER-005',
                'title': 'Hardcoded secret in Dockerfile',
                'severity': 'critical',
                'pattern': r'(?:ENV|ARG)\s+(?:\w+_)?(?:PASSWORD|SECRET|TOKEN|API_KEY|ACCESS_KEY)\s*=\s*["\']?[^\s"\'$]+["\']?',
                'description': 'Un secret semble être hardcodé dans le Dockerfile.',
                'remediation': 'Utiliser des secrets Docker ou des variables d\'environnement runtime.',
                'references': ['https://docs.docker.com/engine/swarm/secrets/'],
            },
            {
                'id': 'DOCKER-006',
                'title': 'Curl or wget piped to shell',
                'severity': 'high',
                'pattern': r'(?:curl|wget)\s+[^\|]+\|\s*(?:bash|sh)',
                'description': 'Télécharger et exécuter un script est dangereux (man-in-the-middle).',
                'remediation': 'Télécharger, vérifier le checksum, puis exécuter séparément.',
                'references': ['https://blog.aquasec.com/docker-security-best-practices'],
            },
            {
                'id': 'DOCKER-007',
                'title': 'Apt-get without cleanup',
                'severity': 'low',
                'pattern': r'apt-get\s+install(?![\s\S]*?rm\s+-rf\s+/var/lib/apt)',
                'description': 'apt-get install sans nettoyage augmente la taille de l\'image.',
                'remediation': 'Ajouter && rm -rf /var/lib/apt/lists/* après apt-get install.',
                'references': ['https://docs.docker.com/develop/develop-images/dockerfile_best-practices/'],
            },
            {
                'id': 'DOCKER-008',
                'title': 'HEALTHCHECK not defined',
                'severity': 'low',
                'pattern': r'HEALTHCHECK',
                'check_type': 'absence',
                'description': 'Pas de HEALTHCHECK défini pour vérifier la santé du container.',
                'remediation': 'Ajouter une instruction HEALTHCHECK.',
                'references': ['https://docs.docker.com/engine/reference/builder/#healthcheck'],
            },
        ],
        'strict': [
            {
                'id': 'DOCKER-101',
                'title': 'Non-specific base image digest',
                'severity': 'medium',
                'pattern': r'FROM\s+[\w\-./]+:[\w\-\.]+(?!@sha256:)',
                'description': 'L\'image n\'utilise pas de digest SHA256 pour garantir l\'intégrité.',
                'remediation': 'Utiliser une image avec digest (ex: python:3.11@sha256:abc...).',
                'references': ['https://docs.docker.com/engine/reference/builder/#from'],
            },
        ],
    }

    def get_name(self) -> str:
        return "iac_guardrails_scan"

    def get_description(self) -> str:
        return "Scanne Terraform/K8s/Dockerfile pour détecter les configurations dangereuses (least privilege)"

    def get_request_model(self) -> Type[BaseModel]:
        return IacGuardrailsRequest

    def get_response_model(self) -> Type[BaseModel]:
        return IacGuardrailsResponse

    def get_supported_languages(self) -> List[str]:
        return ["terraform", "kubernetes", "dockerfile", "yaml", "hcl", "tf"]

    def is_long_running(self) -> bool:
        return False

    def get_usage_description(self) -> str:
        return (
            "Scanne les fichiers Infrastructure as Code pour détecter les violations de sécurité: "
            "privilèges excessifs, expositions réseau, secrets hardcodés, configurations dangereuses."
        )

    def get_examples(self) -> List[Dict[str, Any]]:
        return [
            {
                "title": "Scanner un Deployment Kubernetes",
                "request": {
                    "files": [{"path": "deployment.yaml", "content": "apiVersion: apps/v1\nkind: Deployment..."}],
                    "policy_profile": "baseline"
                }
            },
            {
                "title": "Scanner Terraform avec profil strict",
                "request": {
                    "files": [{"path": "main.tf", "content": "resource \"aws_s3_bucket\"..."}],
                    "policy_profile": "strict",
                    "platform": {"cloud": "aws"}
                }
            },
            {
                "title": "Scanner avec policy personnalisée",
                "request": {
                    "files": [{"path": "Dockerfile", "content": "FROM ubuntu:latest..."}],
                    "custom_policies": [{
                        "id": "CUSTOM-001",
                        "content": "FROM.*ubuntu",
                        "language": "regex",
                        "severity": "medium",
                        "description": "Notre org utilise Alpine, pas Ubuntu"
                    }]
                }
            }
        ]

    def get_capabilities(self) -> List[str]:
        return [
            "Scan de sécurité Kubernetes (basé sur Pod Security Standards)",
            "Scan de sécurité Terraform (AWS, GCP, Azure)",
            "Scan de sécurité Dockerfile (best practices)",
            "Profils baseline et strict",
            "Support de policies personnalisées",
            "Sortie SARIF pour intégration CI/CD"
        ]

    def _detect_file_type(self, filepath: str, content: str) -> str:
        """Détecte le type de fichier IaC."""
        lower_path = filepath.lower()
        
        if lower_path.endswith('.tf') or lower_path.endswith('.tf.json'):
            return 'terraform'
        elif lower_path == 'dockerfile' or lower_path.endswith('/dockerfile') or 'dockerfile' in lower_path:
            return 'dockerfile'
        elif lower_path.endswith(('.yaml', '.yml')):
            # Vérifier si c'est du K8s
            if any(kw in content for kw in ['apiVersion:', 'kind:', 'metadata:']):
                return 'kubernetes'
            return 'yaml'
        elif 'resource' in content and ('aws_' in content or 'azurerm_' in content or 'google_' in content):
            return 'terraform'
        elif content.strip().startswith('FROM '):
            return 'dockerfile'
        
        return 'unknown'

    def _apply_regex_rule(self, rule: Dict, content: str, filepath: str, lines: List[str]) -> List[IacFinding]:
        """Applique une règle regex sur le contenu."""
        findings = []
        pattern = rule['pattern']
        check_type = rule.get('check_type', 'presence')
        
        try:
            if check_type == 'presence':
                matches = list(re.finditer(pattern, content, re.MULTILINE | re.IGNORECASE))
                for match in matches:
                    line_num = content[:match.start()].count('\n') + 1
                    findings.append(IacFinding(
                        rule_id=rule['id'],
                        severity=rule['severity'],
                        path=filepath,
                        line=line_num,
                        title=rule['title'],
                        description=rule['description'],
                        remediation=rule['remediation'],
                        references=rule.get('references', []),
                        engine='embedded-rules'
                    ))
            
            elif check_type == 'absence':
                # Pour les checks d'absence, on vérifie que le pattern N'EST PAS trouvé
                if not re.search(pattern, content, re.MULTILINE | re.IGNORECASE):
                    findings.append(IacFinding(
                        rule_id=rule['id'],
                        severity=rule['severity'],
                        path=filepath,
                        line=1,
                        title=rule['title'],
                        description=rule['description'],
                        remediation=rule['remediation'],
                        references=rule.get('references', []),
                        engine='embedded-rules'
                    ))
        except re.error as e:
            self.logger.warning(f"Erreur regex pour {rule['id']}: {e}")
        
        return findings

    def _scan_kubernetes(self, content: str, filepath: str, profile: str) -> List[IacFinding]:
        """Scanne un fichier Kubernetes YAML."""
        findings = []
        lines = content.split('\n')
        
        # Appliquer les règles baseline
        for rule in self.K8S_RULES['baseline']:
            findings.extend(self._apply_regex_rule(rule, content, filepath, lines))
        
        # Appliquer les règles strict si demandé
        if profile == 'strict':
            # Pour les règles strict, vérifier la présence de securityContext
            if 'securityContext' in content:
                for rule in self.K8S_RULES['strict']:
                    # Ces règles vérifient l'absence de bonnes pratiques
                    findings.extend(self._apply_regex_rule(rule, content, filepath, lines))
            else:
                # Pas de securityContext du tout = plusieurs violations
                findings.append(IacFinding(
                    rule_id='K8S-100',
                    severity='high',
                    path=filepath,
                    line=1,
                    title='No securityContext defined',
                    description='Aucun securityContext n\'est défini pour les containers.',
                    remediation='Ajouter un securityContext avec runAsNonRoot, allowPrivilegeEscalation: false, etc.',
                    references=['https://kubernetes.io/docs/concepts/security/pod-security-standards/'],
                    engine='embedded-rules'
                ))
        
        return findings

    def _scan_terraform(self, content: str, filepath: str, profile: str) -> List[IacFinding]:
        """Scanne un fichier Terraform."""
        findings = []
        lines = content.split('\n')
        
        # Appliquer les règles baseline
        for rule in self.TF_RULES['baseline']:
            findings.extend(self._apply_regex_rule(rule, content, filepath, lines))
        
        # Appliquer les règles strict si demandé
        if profile == 'strict':
            for rule in self.TF_RULES['strict']:
                findings.extend(self._apply_regex_rule(rule, content, filepath, lines))
        
        return findings

    def _scan_dockerfile(self, content: str, filepath: str, profile: str) -> List[IacFinding]:
        """Scanne un Dockerfile."""
        findings = []
        lines = content.split('\n')
        
        # Vérification spéciale pour USER non-root
        has_user_instruction = False
        last_user_is_root = True
        
        for i, line in enumerate(lines, 1):
            line_stripped = line.strip()
            if line_stripped.startswith('USER '):
                has_user_instruction = True
                user = line_stripped[5:].strip()
                last_user_is_root = user.lower() in ('root', '0')
        
        if not has_user_instruction or last_user_is_root:
            findings.append(IacFinding(
                rule_id='DOCKER-001',
                severity='high',
                path=filepath,
                line=1,
                title='Running as root user',
                description='Le container s\'exécute en tant que root par défaut.',
                remediation='Ajouter USER <non-root-user> après l\'installation des dépendances.',
                references=['https://docs.docker.com/develop/develop-images/instructions/#user'],
                engine='embedded-rules'
            ))
        
        # Appliquer les autres règles baseline
        for rule in self.DOCKERFILE_RULES['baseline']:
            if rule['id'] == 'DOCKER-001':
                continue  # Déjà traité
            findings.extend(self._apply_regex_rule(rule, content, filepath, lines))
        
        # Appliquer les règles strict si demandé
        if profile == 'strict':
            for rule in self.DOCKERFILE_RULES['strict']:
                findings.extend(self._apply_regex_rule(rule, content, filepath, lines))
        
        return findings

    def _apply_custom_policies(self, content: str, filepath: str, 
                                policies: List[CustomPolicy]) -> List[IacFinding]:
        """Applique les policies personnalisées."""
        findings = []
        
        for policy in policies:
            if policy.language == 'regex':
                try:
                    matches = list(re.finditer(policy.content, content, re.MULTILINE | re.IGNORECASE))
                    for match in matches:
                        line_num = content[:match.start()].count('\n') + 1
                        findings.append(IacFinding(
                            rule_id=policy.id,
                            severity=policy.severity,
                            path=filepath,
                            line=line_num,
                            title=policy.description or f"Custom policy {policy.id}",
                            description=policy.description or "Policy personnalisée déclenchée",
                            remediation="Voir la documentation de la policy personnalisée",
                            references=[],
                            engine='custom-policy'
                        ))
                except re.error as e:
                    self.logger.warning(f"Erreur regex dans policy {policy.id}: {e}")
            
            elif policy.language == 'yaml-rules':
                # Format simplifié: check si une clé/valeur existe
                try:
                    rule_def = yaml.safe_load(policy.content)
                    if isinstance(rule_def, dict):
                        pattern = rule_def.get('pattern', '')
                        if pattern and re.search(pattern, content, re.MULTILINE | re.IGNORECASE):
                            findings.append(IacFinding(
                                rule_id=policy.id,
                                severity=policy.severity,
                                path=filepath,
                                line=1,
                                title=rule_def.get('title', policy.description or policy.id),
                                description=rule_def.get('description', policy.description or ''),
                                remediation=rule_def.get('remediation', 'Voir documentation'),
                                references=rule_def.get('references', []),
                                engine='custom-yaml-policy'
                            ))
                except yaml.YAMLError as e:
                    self.logger.warning(f"Erreur YAML dans policy {policy.id}: {e}")
        
        return findings

    def _generate_sarif(self, findings: List[IacFinding], files_scanned: int) -> Dict:
        """Génère une sortie au format SARIF."""
        rules = {}
        results = []
        
        for finding in findings:
            # Ajouter la règle si pas encore vue
            if finding.rule_id not in rules:
                rules[finding.rule_id] = {
                    'id': finding.rule_id,
                    'shortDescription': {'text': finding.title},
                    'fullDescription': {'text': finding.description},
                    'help': {'text': finding.remediation},
                    'defaultConfiguration': {
                        'level': 'error' if finding.severity in ('critical', 'high') else 'warning'
                    }
                }
            
            # Ajouter le résultat
            results.append({
                'ruleId': finding.rule_id,
                'level': 'error' if finding.severity in ('critical', 'high') else 'warning',
                'message': {'text': finding.description},
                'locations': [{
                    'physicalLocation': {
                        'artifactLocation': {'uri': finding.path},
                        'region': {'startLine': finding.line or 1}
                    }
                }]
            })
        
        return {
            '$schema': 'https://raw.githubusercontent.com/oasis-tcs/sarif-spec/master/Schemata/sarif-schema-2.1.0.json',
            'version': '2.1.0',
            'runs': [{
                'tool': {
                    'driver': {
                        'name': 'iac_guardrails_scan',
                        'version': '1.0.0',
                        'informationUri': 'https://github.com/collegue/collegue',
                        'rules': list(rules.values())
                    }
                },
                'results': results
            }]
        }

    def _calculate_security_scores(self, findings: List[IacFinding]) -> Tuple[float, float, str]:
        """Calcule les scores de sécurité et compliance basés sur les findings."""
        if not findings:
            return 1.0, 1.0, "low"
        
        # Pondération par sévérité
        severity_weights = {'critical': 0.4, 'high': 0.25, 'medium': 0.1, 'low': 0.05}
        total_weight = sum(severity_weights.get(f.severity, 0.05) for f in findings)
        
        # Score de sécurité (1.0 = parfait, 0.0 = critique)
        security_score = max(0.0, 1.0 - (total_weight / 2.0))
        
        # Score de compliance (basé sur les règles K8S/TF qui mappent aux standards)
        compliance_related = [f for f in findings if f.rule_id.startswith(('K8S-', 'TF-'))]
        compliance_score = max(0.0, 1.0 - (len(compliance_related) * 0.1))
        
        # Niveau de risque global
        critical_count = sum(1 for f in findings if f.severity == 'critical')
        high_count = sum(1 for f in findings if f.severity == 'high')
        
        if critical_count > 0:
            risk_level = "critical"
        elif high_count >= 2:
            risk_level = "high"
        elif high_count > 0 or len(findings) >= 5:
            risk_level = "medium"
        else:
            risk_level = "low"
        
        return security_score, compliance_score, risk_level

    def _generate_remediation_actions(
        self,
        findings: List[IacFinding],
        files: List[FileInput],
        security_score: float
    ) -> List[RemediationAction]:
        """Génère les actions de remédiation suggérées."""
        actions = []
        
        # Grouper par fichier
        file_findings = {}
        for f in findings:
            file_findings.setdefault(f.path, []).append(f)
        
        # Générer une action par fichier avec des problèmes critiques/high
        for filepath, file_issues in file_findings.items():
            critical_high = [f for f in file_issues if f.severity in ('critical', 'high')]
            if not critical_high:
                continue
            
            file_content = next((f.content for f in files if f.path == filepath), "")
            file_type = self._detect_file_type(filepath, file_content)
            
            # Construire les remédiations à appliquer
            remediations = [f"{f.title}: {f.remediation}" for f in critical_high[:3]]
            
            actions.append(RemediationAction(
                tool_name="code_refactoring",
                action_type="fix_config",
                rationale=f"{len(critical_high)} problème(s) critique(s)/haut(s) dans {filepath}",
                priority="critical" if any(f.severity == 'critical' for f in critical_high) else "high",
                params={
                    "code": file_content[:5000],
                    "language": file_type,
                    "refactoring_type": "clean",
                    "file_path": filepath,
                    "instructions": "; ".join(remediations)
                },
                score=1.0 - security_score
            ))
        
        return actions[:5]

    async def _deep_analysis_with_llm(
        self,
        request: IacGuardrailsRequest,
        findings: List[IacFinding],
        llm_manager=None
    ) -> Tuple[Optional[List[LLMSecurityInsight]], float, float, str]:
        """
        Enrichit l'analyse avec le LLM (mode deep).
        
        Retourne (insights, security_score, compliance_score, risk_level).
        """
        try:
            manager = llm_manager or self.llm_manager
            if not manager:
                self.logger.warning("LLM manager non disponible pour analyse deep")
                return None, *self._calculate_security_scores(findings)
            
            # Préparer le contexte
            files_summary = []
            for f in request.files[:3]:
                file_type = self._detect_file_type(f.path, f.content)
                preview = f.content[:500] + "..." if len(f.content) > 500 else f.content
                files_summary.append(f"### {f.path} ({file_type})\n```\n{preview}\n```")
            
            findings_summary = []
            for finding in findings[:10]:
                findings_summary.append(
                    f"- [{finding.severity.upper()}] {finding.rule_id}: {finding.title} @ {finding.path}"
                )
            
            cloud = request.platform.get('cloud', 'aws') if request.platform else 'aws'
            
            prompt = f"""Analyse les configurations IaC et les problèmes de sécurité détectés.

## Fichiers IaC analysés
{chr(10).join(files_summary)}

## Findings ({len(findings)} total)
{chr(10).join(findings_summary) if findings_summary else "Aucun finding détecté"}

## Contexte
- Cloud provider: {cloud}
- Profil: {request.policy_profile}

---

Fournis une analyse de sécurité enrichie au format JSON strict:
{{
  "security_score": 0.0-1.0,
  "compliance_score": 0.0-1.0,
  "risk_level": "low|medium|high|critical",
  "insights": [
    {{
      "category": "vulnerability|misconfiguration|compliance|best_practice",
      "insight": "Description détaillée du problème ou de la recommandation",
      "risk_level": "low|medium|high|critical",
      "affected_resources": ["resource1", "resource2"],
      "compliance_frameworks": ["CIS", "SOC2", "HIPAA"]
    }}
  ]
}}

Catégories d'insights:
- **vulnerability**: Failles de sécurité exploitables
- **misconfiguration**: Configurations incorrectes ou dangereuses
- **compliance**: Non-conformité aux standards (CIS, SOC2, HIPAA, PCI-DSS)
- **best_practice**: Recommandations d'amélioration

Scores:
- `security_score`: 1.0 = sécurisé, 0.0 = critique
- `compliance_score`: 1.0 = conforme, 0.0 = non conforme

Réponds UNIQUEMENT avec le JSON, sans markdown ni explication."""

            response = await manager.async_generate(prompt)
            
            if not response:
                return None, *self._calculate_security_scores(findings)
            
            # Parser la réponse
            try:
                clean_response = response.strip()
                if clean_response.startswith("```"):
                    clean_response = clean_response.split("\n", 1)[1]
                if clean_response.endswith("```"):
                    clean_response = clean_response.rsplit("```", 1)[0]
                clean_response = clean_response.strip()
                
                data = json.loads(clean_response)
                
                llm_security = float(data.get("security_score", 0.5))
                llm_compliance = float(data.get("compliance_score", 0.5))
                llm_risk = data.get("risk_level", "medium")
                
                # Combiner avec les scores heuristiques
                heur_security, heur_compliance, _ = self._calculate_security_scores(findings)
                final_security = (llm_security * 0.6) + (heur_security * 0.4)
                final_compliance = (llm_compliance * 0.6) + (heur_compliance * 0.4)
                
                # Niveau de risque basé sur le score final
                if final_security < 0.3:
                    risk_level = "critical"
                elif final_security < 0.5:
                    risk_level = "high"
                elif final_security < 0.7:
                    risk_level = "medium"
                else:
                    risk_level = "low"
                
                # Parser les insights
                insights = []
                for item in data.get("insights", [])[:10]:
                    if isinstance(item, dict) and "insight" in item:
                        insights.append(LLMSecurityInsight(
                            category=item.get("category", "best_practice"),
                            insight=item["insight"],
                            risk_level=item.get("risk_level", "medium"),
                            affected_resources=item.get("affected_resources", []),
                            compliance_frameworks=item.get("compliance_frameworks", [])
                        ))
                
                self.logger.info(f"Analyse deep: {len(insights)} insights, security={final_security:.2f}")
                return insights, final_security, final_compliance, risk_level
                
            except json.JSONDecodeError as e:
                self.logger.warning(f"Erreur parsing réponse LLM: {e}")
                return None, *self._calculate_security_scores(findings)
                
        except Exception as e:
            self.logger.error(f"Erreur analyse deep: {e}")
            return None, *self._calculate_security_scores(findings)

    async def _execute_auto_remediation(
        self,
        request: IacGuardrailsRequest,
        findings: List[IacFinding],
        remediations: List[RemediationAction],
        llm_manager=None,
        ctx=None
    ) -> Optional[Dict[str, Any]]:
        """Exécute automatiquement la remédiation si le seuil est atteint."""
        try:
            from .refactoring import RefactoringTool, RefactoringRequest
            
            if not remediations:
                return None
            
            # Prendre l'action avec le score le plus élevé
            best_action = max(remediations, key=lambda a: a.score)
            
            if best_action.tool_name != "code_refactoring":
                return None
            
            params = best_action.params
            if not params.get("code"):
                return None
            
            refactoring_request = RefactoringRequest(
                code=params.get("code", ""),
                language=params.get("language", "yaml"),
                refactoring_type=params.get("refactoring_type", "clean"),
                file_path=params.get("file_path"),
                parameters={
                    "context": "auto-triggered from iac_guardrails_scan",
                    "security_fix": True,
                    "instructions": params.get("instructions", "")
                }
            )
            
            refactoring_tool = RefactoringTool(app_state=self.app_state)
            result = refactoring_tool.execute(
                refactoring_request,
                llm_manager=llm_manager,
                ctx=ctx
            )
            
            self.logger.info(f"Auto-remediation exécutée sur {params.get('file_path', 'fichier')}")
            
            return {
                "file_path": params.get("file_path"),
                "issues_fixed": len([f for f in findings if f.path == params.get("file_path")]),
                "original_preview": params.get("code", "")[:200] + "...",
                "remediated_preview": result.refactored_code[:200] + "..." if result.refactored_code else None,
                "changes_count": len(result.changes),
                "explanation": result.explanation
            }
            
        except Exception as e:
            self.logger.error(f"Erreur auto-remediation: {e}")
            return None

    def _execute_core_logic(self, request: IacGuardrailsRequest, **kwargs) -> IacGuardrailsResponse:
        """Exécute le scan IaC."""
        self.logger.info(f"Scan IaC de {len(request.files)} fichier(s) avec profil '{request.policy_profile}'")
        
        all_findings = []
        rules_count = 0
        
        for file in request.files:
            file_type = self._detect_file_type(file.path, file.content)
            self.logger.debug(f"Fichier {file.path}: type={file_type}")
            
            if file_type == 'kubernetes':
                rules_count += len(self.K8S_RULES['baseline'])
                if request.policy_profile == 'strict':
                    rules_count += len(self.K8S_RULES['strict'])
                all_findings.extend(self._scan_kubernetes(file.content, file.path, request.policy_profile))
            
            elif file_type == 'terraform':
                rules_count += len(self.TF_RULES['baseline'])
                if request.policy_profile == 'strict':
                    rules_count += len(self.TF_RULES['strict'])
                all_findings.extend(self._scan_terraform(file.content, file.path, request.policy_profile))
            
            elif file_type == 'dockerfile':
                rules_count += len(self.DOCKERFILE_RULES['baseline'])
                if request.policy_profile == 'strict':
                    rules_count += len(self.DOCKERFILE_RULES['strict'])
                all_findings.extend(self._scan_dockerfile(file.content, file.path, request.policy_profile))
            
            # Appliquer les policies personnalisées
            if request.custom_policies:
                rules_count += len(request.custom_policies)
                all_findings.extend(self._apply_custom_policies(file.content, file.path, request.custom_policies))
        
        # Dédupliquer les findings (même rule_id + même fichier + même ligne)
        seen = set()
        unique_findings = []
        for f in all_findings:
            key = (f.rule_id, f.path, f.line)
            if key not in seen:
                seen.add(key)
                unique_findings.append(f)
        
        # Compter par sévérité
        severity_counts = {'critical': 0, 'high': 0, 'medium': 0, 'low': 0}
        for finding in unique_findings:
            severity_counts[finding.severity] = severity_counts.get(finding.severity, 0) + 1
        
        # Déterminer si passé (pas de critical/high)
        passed = severity_counts['critical'] == 0 and severity_counts['high'] == 0
        
        summary = {
            'total': len(unique_findings),
            'critical': severity_counts['critical'],
            'high': severity_counts['high'],
            'medium': severity_counts['medium'],
            'low': severity_counts['low'],
            'passed': rules_count - len(unique_findings),
            'failed': len(unique_findings),
            'skipped': 0,
        }
        
        # Récupérer les services depuis kwargs
        llm_manager = kwargs.get('llm_manager') or self.llm_manager
        ctx = kwargs.get('ctx')
        
        # Mode deep: enrichissement IA
        llm_insights = None
        analysis_depth_used = "fast"
        security_score = 1.0
        compliance_score = 1.0
        risk_level = "low"
        
        if request.analysis_depth == "deep":
            self.logger.info("Mode deep: enrichissement IA sécurité en cours...")
            analysis_depth_used = "deep"
            
            try:
                coro = self._deep_analysis_with_llm(request, unique_findings, llm_manager)
                try:
                    loop = asyncio.get_running_loop()
                    import concurrent.futures
                    with concurrent.futures.ThreadPoolExecutor() as executor:
                        future = executor.submit(asyncio.run, coro)
                        llm_insights, security_score, compliance_score, risk_level = future.result(timeout=30)
                except RuntimeError:
                    llm_insights, security_score, compliance_score, risk_level = asyncio.run(coro)
            except Exception as e:
                self.logger.warning(f"Fallback mode fast suite à erreur deep: {e}")
                security_score, compliance_score, risk_level = self._calculate_security_scores(unique_findings)
        else:
            # Mode fast: scoring heuristique uniquement
            security_score, compliance_score, risk_level = self._calculate_security_scores(unique_findings)
        
        # Générer les actions de remédiation
        suggested_remediations = self._generate_remediation_actions(unique_findings, request.files, security_score)
        
        # Auto-chain: déclencher la remédiation si seuil atteint
        auto_remediation_triggered = False
        auto_remediation_result = None
        
        if request.auto_chain and security_score < request.remediation_threshold and suggested_remediations:
            self.logger.info(f"Auto-remediation: security_score {security_score:.2f} < seuil {request.remediation_threshold}")
            try:
                coro = self._execute_auto_remediation(
                    request, unique_findings, suggested_remediations, llm_manager, ctx
                )
                try:
                    loop = asyncio.get_running_loop()
                    import concurrent.futures
                    with concurrent.futures.ThreadPoolExecutor() as executor:
                        future = executor.submit(asyncio.run, coro)
                        auto_remediation_result = future.result(timeout=60)
                except RuntimeError:
                    auto_remediation_result = asyncio.run(coro)
                
                if auto_remediation_result:
                    auto_remediation_triggered = True
            except Exception as e:
                self.logger.warning(f"Erreur auto-remediation: {e}")
        
        # Construire le résumé
        if passed and not unique_findings:
            scan_summary = f"✅ Aucun problème de sécurité détecté dans {len(request.files)} fichier(s) IaC."
        elif passed:
            scan_summary = (
                f"⚠️ {len(unique_findings)} problème(s) mineur(s) détecté(s) dans {len(request.files)} fichier(s). "
                f"Moyenne({severity_counts['medium']}), Basse({severity_counts['low']})."
            )
        else:
            scan_summary = (
                f"🚨 {len(unique_findings)} problème(s) de sécurité dans {len(request.files)} fichier(s)! "
                f"Critique({severity_counts['critical']}), Haute({severity_counts['high']}), "
                f"Moyenne({severity_counts['medium']}), Basse({severity_counts['low']})."
            )
        
        if analysis_depth_used == "deep":
            scan_summary += f" 🔒 Score sécurité: {security_score:.0%}, Compliance: {compliance_score:.0%} (risque: {risk_level})."
            if llm_insights:
                scan_summary += f" {len(llm_insights)} insight(s) IA."
        
        if auto_remediation_triggered:
            scan_summary += " 🔧 Remédiation auto-déclenchée."
        
        # Générer SARIF si demandé
        sarif_output = None
        if request.output_format == 'sarif':
            sarif_output = self._generate_sarif(unique_findings, len(request.files))
        
        return IacGuardrailsResponse(
            passed=passed,
            summary=summary,
            findings=unique_findings[:100],
            files_scanned=len(request.files),
            rules_evaluated=rules_count,
            scan_summary=scan_summary,
            sarif=sarif_output,
            analysis_depth_used=analysis_depth_used,
            llm_insights=llm_insights,
            security_score=security_score,
            compliance_score=compliance_score,
            risk_level=risk_level,
            suggested_remediations=suggested_remediations,
            auto_remediation_triggered=auto_remediation_triggered,
            auto_remediation_result=auto_remediation_result
        )
