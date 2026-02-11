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
from .shared import FileInput, aggregate_severities, load_rules, parse_llm_json_response, run_async_from_sync, validate_fast_deep
from .scanners.kubernetes import KubernetesScanner
from .scanners.terraform import TerraformScanner
from .scanners.dockerfile import DockerfileScanner


class CustomPolicy(BaseModel):
    id: str = Field(..., description="Identifiant unique de la policy")
    description: Optional[str] = Field(None, description="Description de la policy")
    content: str = Field(..., description="Contenu de la règle (regex ou YAML)")
    language: str = Field("yaml-rules", description="Format: 'regex' ou 'yaml-rules'")
    severity: str = Field("medium", description="Sévérité: low, medium, high, critical")

class IacGuardrailsRequest(BaseModel):
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
    category: str = Field(..., description="Catégorie: vulnerability, misconfiguration, compliance, best_practice")
    insight: str = Field(..., description="L'insight détaillé")
    risk_level: str = Field("medium", description="Niveau de risque: low, medium, high, critical")
    affected_resources: List[str] = Field(default_factory=list, description="Ressources concernées")
    compliance_frameworks: List[str] = Field(default_factory=list, description="Standards impactés: CIS, SOC2, HIPAA, etc.")

class RemediationAction(BaseModel):
    tool_name: str = Field(..., description="Nom du tool à appeler (ex: code_refactoring)")
    action_type: str = Field(..., description="Type: fix_config, add_security, remove_exposure")
    rationale: str = Field(..., description="Pourquoi cette action")
    priority: str = Field("medium", description="Priorité: low, medium, high, critical")
    params: Dict[str, Any] = Field(default_factory=dict, description="Paramètres pour le tool")
    score: float = Field(0.0, description="Score de pertinence (0.0-1.0)", ge=0.0, le=1.0)

class IacGuardrailsResponse(BaseModel):
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

class IacGuardrailsScanTool(BaseTool):
    K8S_RULES = load_rules('k8s.yaml')
    TF_RULES = load_rules('terraform.yaml')
    DOCKERFILE_RULES = load_rules('dockerfile.yaml')

    tool_name = "iac_guardrails_scan"
    tool_description = "Scanne Terraform/K8s/Dockerfile pour détecter les configurations dangereuses (least privilege)"
    tags = {"security", "analysis", "devops"}
    request_model = IacGuardrailsRequest
    response_model = IacGuardrailsResponse
    supported_languages = ["terraform", "kubernetes", "dockerfile", "yaml", "hcl", "tf"]
    long_running = False

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

    def __init__(self, config=None, app_state=None):
        super().__init__(config, app_state)
        self._k8s_scanner = KubernetesScanner(logger=self.logger)
        self._tf_scanner = TerraformScanner(logger=self.logger)
        self._dockerfile_scanner = DockerfileScanner(logger=self.logger)

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
        lower_path = filepath.lower()

        if lower_path.endswith('.tf') or lower_path.endswith('.tf.json'):
            return 'terraform'
        elif lower_path == 'dockerfile' or lower_path.endswith('/dockerfile') or 'dockerfile' in lower_path:
            return 'dockerfile'
        elif lower_path.endswith(('.yaml', '.yml')):

            if any(kw in content for kw in ['apiVersion:', 'kind:', 'metadata:']):
                return 'kubernetes'
            return 'yaml'
        elif 'resource' in content and ('aws_' in content or 'azurerm_' in content or 'google_' in content):
            return 'terraform'
        elif content.strip().startswith('FROM '):
            return 'dockerfile'

        return 'unknown'

    def _apply_regex_rule(self, rule: Dict, content: str, filepath: str, lines: List[str]) -> List[IacFinding]:
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
        findings = []
        lines = content.split('\n')

        for rule in self.K8S_RULES['baseline']:
            findings.extend(self._apply_regex_rule(rule, content, filepath, lines))


        if profile == 'strict':

            if 'securityContext' in content:
                for rule in self.K8S_RULES['strict']:

                    findings.extend(self._apply_regex_rule(rule, content, filepath, lines))
            else:

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
        findings = []
        lines = content.split('\n')


        for rule in self.TF_RULES['baseline']:
            findings.extend(self._apply_regex_rule(rule, content, filepath, lines))


        if profile == 'strict':
            for rule in self.TF_RULES['strict']:
                findings.extend(self._apply_regex_rule(rule, content, filepath, lines))

        return findings

    def _scan_dockerfile(self, content: str, filepath: str, profile: str) -> List[IacFinding]:
        findings = []
        lines = content.split('\n')


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

        for rule in self.DOCKERFILE_RULES['baseline']:
            if rule['id'] == 'DOCKER-001':
                continue
            findings.extend(self._apply_regex_rule(rule, content, filepath, lines))

        if profile == 'strict':
            for rule in self.DOCKERFILE_RULES['strict']:
                findings.extend(self._apply_regex_rule(rule, content, filepath, lines))

        return findings

    def _convert_findings(self, scanner_findings: list) -> list:
        """Convert scanner findings to IacFinding objects."""
        from .scanners import IacFinding as ScannerFinding
        result = []
        for f in scanner_findings:
            if isinstance(f, ScannerFinding):
                result.append(IacFinding(
                    rule_id=f.rule_id,
                    severity=f.severity,
                    path=f.path,
                    line=f.line,
                    title=f.rule_title,
                    description=f.message,
                    remediation=f.remediation,
                    references=f.references,
                    engine=f.engine
                ))
            else:
                result.append(f)
        return result

    def _apply_custom_policies(self, content: str, filepath: str,
                                policies: List[CustomPolicy]) -> List[IacFinding]:
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
        rules = {}
        results = []

        for finding in findings:

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
        if not findings:
            return 1.0, 1.0, "low"

        severity_weights = {'critical': 0.4, 'high': 0.25, 'medium': 0.1, 'low': 0.05}
        total_weight = sum(severity_weights.get(f.severity, 0.05) for f in findings)

        security_score = max(0.0, 1.0 - (total_weight / 2.0))

        compliance_related = [f for f in findings if f.rule_id.startswith(('K8S-', 'TF-'))]
        compliance_score = max(0.0, 1.0 - (len(compliance_related) * 0.1))

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
        actions = []

        file_findings = {}
        for f in findings:
            file_findings.setdefault(f.path, []).append(f)

        for filepath, file_issues in file_findings.items():
            critical_high = [f for f in file_issues if f.severity in ('critical', 'high')]
            if not critical_high:
                continue

            file_content = next((f.content for f in files if f.path == filepath), "")
            file_type = self._detect_file_type(filepath, file_content)

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
        try:
            manager = llm_manager or self.llm_manager
            if not manager:
                self.logger.warning("LLM manager non disponible pour analyse deep")
                return None, *self._calculate_security_scores(findings)


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
{
  "security_score": 0.0-1.0,
  "compliance_score": 0.0-1.0,
  "risk_level": "low|medium|high|critical",
  "insights": [
    {
      "category": "vulnerability|misconfiguration|compliance|best_practice",
      "insight": "Description détaillée du problème ou de la recommandation",
      "risk_level": "low|medium|high|critical",
      "affected_resources": ["resource1", "resource2"],
      "compliance_frameworks": ["CIS", "SOC2", "HIPAA"]
    }
  ]
}

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

            try:
                data = parse_llm_json_response(response)

                llm_security = float(data.get("security_score", 0.5))
                llm_compliance = float(data.get("compliance_score", 0.5))
                llm_risk = data.get("risk_level", "medium")

                heur_security, heur_compliance, _ = self._calculate_security_scores(findings)
                final_security = (llm_security * 0.6) + (heur_security * 0.4)
                final_compliance = (llm_compliance * 0.6) + (heur_compliance * 0.4)

                if final_security < 0.3:
                    risk_level = "critical"
                elif final_security < 0.5:
                    risk_level = "high"
                elif final_security < 0.7:
                    risk_level = "medium"
                else:
                    risk_level = "low"

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
        try:
            from .refactoring import RefactoringTool, RefactoringRequest

            if not remediations:
                return None

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
                findings = self._k8s_scanner.scan(file.content, file.path, request.policy_profile)
                all_findings.extend(self._convert_findings(findings))

            elif file_type == 'terraform':
                rules_count += len(self.TF_RULES['baseline'])
                if request.policy_profile == 'strict':
                    rules_count += len(self.TF_RULES['strict'])
                findings = self._tf_scanner.scan(file.content, file.path, request.policy_profile)
                all_findings.extend(self._convert_findings(findings))

            elif file_type == 'dockerfile':
                rules_count += len(self.DOCKERFILE_RULES['baseline'])
                if request.policy_profile == 'strict':
                    rules_count += len(self.DOCKERFILE_RULES['strict'])
                findings = self._dockerfile_scanner.scan(file.content, file.path, request.policy_profile)
                all_findings.extend(self._convert_findings(findings))

            if request.custom_policies:
                rules_count += len(request.custom_policies)
                all_findings.extend(self._apply_custom_policies(file.content, file.path, request.custom_policies))

        seen = set()
        unique_findings = []
        for f in all_findings:
            key = (f.rule_id, f.path, f.line)
            if key not in seen:
                seen.add(key)
                unique_findings.append(f)

        severity_counts = aggregate_severities(unique_findings)

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

        llm_manager = kwargs.get('llm_manager') or self.llm_manager
        ctx = kwargs.get('ctx')

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
                llm_insights, security_score, compliance_score, risk_level = run_async_from_sync(coro, timeout=30)
            except Exception as e:
                self.logger.warning(f"Fallback mode fast suite à erreur deep: {e}")
                security_score, compliance_score, risk_level = self._calculate_security_scores(unique_findings)
        else:

            security_score, compliance_score, risk_level = self._calculate_security_scores(unique_findings)

        suggested_remediations = self._generate_remediation_actions(unique_findings, request.files, security_score)

        auto_remediation_triggered = False
        auto_remediation_result = None

        if request.auto_chain and security_score < request.remediation_threshold and suggested_remediations:
            self.logger.info(f"Auto-remediation: security_score {security_score:.2f} < seuil {request.remediation_threshold}")
            try:
                coro = self._execute_auto_remediation(
                    request, unique_findings, suggested_remediations, llm_manager, ctx
                )
                auto_remediation_result = run_async_from_sync(coro, timeout=60)

                if auto_remediation_result:
                    auto_remediation_triggered = True
            except Exception as e:
                self.logger.warning(f"Erreur auto-remediation: {e}")

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
