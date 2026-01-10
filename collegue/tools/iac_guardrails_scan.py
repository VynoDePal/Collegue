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
        
        # Générer SARIF si demandé
        sarif_output = None
        if request.output_format == 'sarif':
            sarif_output = self._generate_sarif(unique_findings, len(request.files))
        
        return IacGuardrailsResponse(
            passed=passed,
            summary=summary,
            findings=unique_findings[:100],  # Limiter
            files_scanned=len(request.files),
            rules_evaluated=rules_count,
            scan_summary=scan_summary,
            sarif=sarif_output
        )
