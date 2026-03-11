"""
IaC Guardrails Scan - Outil de scan de sécurité pour Infrastructure as Code.

Cet outil scanne les fichiers IaC (Terraform, Kubernetes, Dockerfile) pour détecter:
- Violations du principe de moindre privilège
- Configurations par défaut dangereuses
- Expositions réseau non sécurisées
- Secrets hardcodés dans l'infrastructure

Refactorisé: Le fichier original faisait 956 lignes, maintenant ~200 lignes.
La logique métier a été déplacée dans engine.py, les modèles dans models.py.
"""

from typing import List, Dict, Any, Optional
import asyncio
import json

from ..base import BaseTool
from ...core.shared import load_rules, aggregate_severities
from .models import (
    IacGuardrailsRequest,
    IacGuardrailsResponse,
    IacFinding,
    RemediationAction,
    LLMSecurityInsight,
    FileInput,
    CustomPolicy,
)
from .engine import IacAnalysisEngine
from .config import DEEP_ANALYSIS_PROMPT_TEMPLATE, FALLBACK_PROMPT_TEMPLATE
from ..scanners.kubernetes import KubernetesScanner
from ..scanners.terraform import TerraformScanner
from ..scanners.dockerfile import DockerfileScanner


class IacGuardrailsScanTool(BaseTool):
    """Tool de scan de sécurité IaC."""

    # Chargement des règles YAML
    K8S_RULES = load_rules("k8s.yaml")
    TF_RULES = load_rules("terraform.yaml")
    DOCKERFILE_RULES = load_rules("dockerfile.yaml")

    tool_name = "iac_guardrails_scan"
    tool_description = (
        "Scanne les fichiers Infrastructure as Code (Terraform, Kubernetes, Dockerfile) pour détecter les failles de sécurité.\n"
        "\n"
        "PARAMÈTRES REQUIS:\n"
        "- files: Liste des fichiers à scanner. Format: [{'path': '...', 'content': '...'}].\n"
        "\n"
        "PARAMÈTRES OPTIONNELS:\n"
        "- policy_profile: Profil de sécurité. Options: 'baseline' (défaut, recommandé) ou 'strict'.\n"
        "- platform: Dictionnaire décrivant la cible. Ex: {'cloud': 'aws'} ou {'k8s_version': '1.28'}.\n"
        "- engines: Moteurs de scan. Options: ['embedded-rules', 'opa-lite']. Défaut: ['embedded-rules'].\n"
        "- custom_policies: Règles spécifiques au projet (regex ou yaml-rules).\n"
        "- analysis_depth: Profondeur d'analyse. Options: 'fast' (heuristiques, par défaut) ou 'deep' (insights LLM).\n"
        "- auto_chain: Booléen. Si True, enclenche une remédiation LLM automatique si le score est trop bas.\n"
        "- remediation_threshold: Float (0.0-1.0). Seuil de déclenchement pour 'auto_chain'. Défaut: 0.5.\n"
        "- output_format: 'json' ou 'sarif'.\n"
        "\n"
        "UTILISATION:\n"
        "Idéal pour valider des ressources cloud avant déploiement, vérifier les privilèges (least privilege), "
        "et éviter les fuites de secrets dans du Terraform, Kubernetes YAML, ou Dockerfile."
    )

    tags = {"security", "analysis", "devops"}
    request_model = IacGuardrailsRequest
    response_model = IacGuardrailsResponse
    supported_languages = ["terraform", "kubernetes", "dockerfile", "yaml", "hcl", "tf"]
    long_running = False

    def __init__(self, config=None, app_state=None):
        super().__init__(config, app_state)
        self._engine = IacAnalysisEngine(
            k8s_rules=self.K8S_RULES,
            tf_rules=self.TF_RULES,
            dockerfile_rules=self.DOCKERFILE_RULES,
            logger=self.logger,
        )
        self._k8s_scanner = KubernetesScanner(logger=self.logger)
        self._tf_scanner = TerraformScanner(logger=self.logger)
        self._dockerfile_scanner = DockerfileScanner(logger=self.logger)
    
    def cleanup(self) -> None:
        """Nettoie les ressources pour éviter les fuites mémoire."""
        super().cleanup()
        
        # Libérer les références aux scanners et engine
        self._engine = None
        self._k8s_scanner = None
        self._tf_scanner = None
        self._dockerfile_scanner = None
        
        self.logger.info("IaC Guardrails Scan tool cleaned up")

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
                    "files": [
                        {
                            "path": "deployment.yaml",
                            "content": "apiVersion: apps/v1\nkind: Deployment...",
                        }
                    ],
                    "policy_profile": "baseline",
                },
            },
            {
                "title": "Scanner Terraform avec profil strict",
                "request": {
                    "files": [
                        {"path": "main.tf", "content": 'resource "aws_s3_bucket"...'}
                    ],
                    "policy_profile": "strict",
                    "platform": {"cloud": "aws"},
                },
            },
        ]

    def get_capabilities(self) -> List[str]:
        return [
            "Scan de sécurité Kubernetes (basé sur Pod Security Standards)",
            "Scan de sécurité Terraform (AWS, GCP, Azure)",
            "Scan de sécurité Dockerfile (best practices)",
            "Profils baseline et strict",
            "Support de policies personnalisées",
            "Sortie SARIF pour intégration CI/CD",
        ]

    def _apply_custom_policies(
        self, content: str, filepath: str, policies: List[CustomPolicy]
    ) -> List[IacFinding]:
        """Applique les policies personnalisées sur un fichier."""
        import re
        import yaml

        findings = []

        for policy in policies:
            if policy.language == "regex":
                try:
                    matches = list(
                        re.finditer(
                            policy.content, content, re.MULTILINE | re.IGNORECASE
                        )
                    )
                    for match in matches:
                        line_num = content[: match.start()].count("\n") + 1
                        findings.append(
                            IacFinding(
                                rule_id=policy.id,
                                severity=policy.severity,
                                path=filepath,
                                line=line_num,
                                title=policy.description
                                or f"Custom policy {policy.id}",
                                description=policy.description
                                or "Policy personnalisée déclenchée",
                                remediation="Voir la documentation de la policy personnalisée",
                                references=[],
                                engine="custom-policy",
                            )
                        )
                except re.error as e:
                    self.logger.warning(f"Erreur regex dans policy {policy.id}: {e}")

            elif policy.language == "yaml-rules":
                try:
                    rule_def = yaml.safe_load(policy.content)
                    if isinstance(rule_def, dict):
                        pattern = rule_def.get("pattern", "")
                        if pattern and re.search(
                            pattern, content, re.MULTILINE | re.IGNORECASE
                        ):
                            findings.append(
                                IacFinding(
                                    rule_id=policy.id,
                                    severity=policy.severity,
                                    path=filepath,
                                    line=1,
                                    title=rule_def.get(
                                        "title", policy.description or policy.id
                                    ),
                                    description=rule_def.get(
                                        "description", policy.description or ""
                                    ),
                                    remediation=rule_def.get(
                                        "remediation", "Voir documentation"
                                    ),
                                    references=rule_def.get("references", []),
                                    engine="custom-yaml-policy",
                                )
                            )
                except yaml.YAMLError as e:
                    self.logger.warning(f"Erreur YAML dans policy {policy.id}: {e}")

        return findings

    def _generate_remediation_actions(
        self, findings: List[IacFinding], files: List[FileInput], security_score: float
    ) -> List[RemediationAction]:
        """Génère les actions de remédiation suggérées."""
        actions = []
        file_findings = {}

        for f in findings:
            file_findings.setdefault(f.path, []).append(f)

        for filepath, file_issues in file_findings.items():
            critical_high = [
                f for f in file_issues if f.severity in ("critical", "high")
            ]
            if not critical_high:
                continue

            file_content = next((f.content for f in files if f.path == filepath), "")
            file_type = self._engine.detect_file_type(filepath, file_content)
            remediations = [f"{f.title}: {f.remediation}" for f in critical_high[:3]]

            actions.append(
                RemediationAction(
                    tool_name="code_refactoring",
                    action_type="fix_config",
                    rationale=f"{len(critical_high)} problème(s) critique(s)/haut(s) dans {filepath}",
                    priority="critical"
                    if any(f.severity == "critical" for f in critical_high)
                    else "high",
                    params={
                        "code": file_content[:5000],
                        "language": file_type,
                        "refactoring_type": "clean",
                        "file_path": filepath,
                        "instructions": "; ".join(remediations),
                    },
                    score=1.0 - security_score,
                )
            )

        return actions[:5]

    def _build_deep_analysis_prompt(
        self, request: IacGuardrailsRequest, findings: List[IacFinding]
    ) -> str:
        """Construit le prompt pour l'analyse LLM deep."""
        files_summary = []
        for f in request.files[:3]:
            file_type = self._engine.detect_file_type(f.path, f.content)
            preview = f.content[:500] + "..." if len(f.content) > 500 else f.content
            files_summary.append(f"### {f.path} ({file_type})\n```\n{preview}\n```")

        findings_summary = []
        for finding in findings[:10]:
            findings_summary.append(
                f"- [{finding.severity.upper()}] {finding.rule_id}: {finding.title} @ {finding.path}"
            )

        cloud = request.platform.get("cloud", "aws") if request.platform else "aws"

        return DEEP_ANALYSIS_PROMPT_TEMPLATE.format(
            files_summary="\n".join(files_summary),
            findings_count=len(findings),
            findings_summary="\n".join(findings_summary)
            if findings_summary
            else "Aucun finding détecté",
            cloud=cloud,
            policy_profile=request.policy_profile,
        )

    async def _deep_analysis_with_llm(
        self, request: IacGuardrailsRequest, findings: List[IacFinding], ctx=None
    ):
        """Effectue l'analyse approfondie avec le LLM."""
        from ...core.shared import parse_llm_json_response

        if ctx is None:
            self.logger.warning("ctx non disponible pour analyse deep")
            return None, *self._engine.calculate_security_scores(findings)

        try:
            prompt = self._build_deep_analysis_prompt(request, findings)
            result = await ctx.sample(messages=prompt, temperature=0.5, max_tokens=2000)
            response = result.text

            if not response:
                return None, *self._engine.calculate_security_scores(findings)

            data = parse_llm_json_response(response)

            llm_security = float(data.get("security_score", 0.5))
            llm_compliance = float(data.get("compliance_score", 0.5))

            heur_security, heur_compliance, _ = self._engine.calculate_security_scores(
                findings
            )
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
                    insights.append(
                        LLMSecurityInsight(
                            category=item.get("category", "best_practice"),
                            insight=item["insight"],
                            risk_level=item.get("risk_level", "medium"),
                            affected_resources=item.get("affected_resources", []),
                            compliance_frameworks=item.get("compliance_frameworks", []),
                        )
                    )

            self.logger.info(
                f"Analyse deep: {len(insights)} insights, security={final_security:.2f}"
            )
            return insights, final_security, final_compliance, risk_level

        except Exception as e:
            self.logger.error(f"Erreur analyse deep: {e}")
            return None, *self._engine.calculate_security_scores(findings)

    def _execute_core_logic(
        self, request: IacGuardrailsRequest, **kwargs
    ) -> IacGuardrailsResponse:
        """Logique principale du scan."""
        self.logger.info(
            f"Scan IaC de {len(request.files)} fichier(s) avec profil '{request.policy_profile}'"
        )

        all_findings = []
        rules_count = 0

        for file in request.files:
            file_type = self._engine.detect_file_type(file.path, file.content)
            self.logger.debug(f"Fichier {file.path}: type={file_type}")

            if file_type == "kubernetes":
                rules_count += len(self.K8S_RULES.get("baseline", []))
                if request.policy_profile == "strict":
                    rules_count += len(self.K8S_RULES.get("strict", []))
                findings = self._k8s_scanner.scan(
                    file.content, file.path, request.policy_profile
                )
                all_findings.extend(self._engine.convert_findings(findings))

            elif file_type == "terraform":
                rules_count += len(self.TF_RULES.get("baseline", []))
                if request.policy_profile == "strict":
                    rules_count += len(self.TF_RULES.get("strict", []))
                findings = self._tf_scanner.scan(
                    file.content, file.path, request.policy_profile
                )
                all_findings.extend(self._engine.convert_findings(findings))

            elif file_type == "dockerfile":
                rules_count += len(self.DOCKERFILE_RULES.get("baseline", []))
                if request.policy_profile == "strict":
                    rules_count += len(self.DOCKERFILE_RULES.get("strict", []))
                findings = self._dockerfile_scanner.scan(
                    file.content, file.path, request.policy_profile
                )
                all_findings.extend(self._engine.convert_findings(findings))

            if request.custom_policies:
                rules_count += len(request.custom_policies)
                all_findings.extend(
                    self._apply_custom_policies(
                        file.content, file.path, request.custom_policies
                    )
                )

        unique_findings = self._engine.deduplicate_findings(all_findings)
        severity_counts = aggregate_severities(unique_findings)

        passed = severity_counts["critical"] == 0 and severity_counts["high"] == 0

        summary = {
            "total": len(unique_findings),
            "critical": severity_counts["critical"],
            "high": severity_counts["high"],
            "medium": severity_counts["medium"],
            "low": severity_counts["low"],
            "passed": rules_count - len(unique_findings),
            "failed": len(unique_findings),
            "skipped": 0,
        }

        security_score, compliance_score, risk_level = (
            self._engine.calculate_security_scores(unique_findings)
        )
        suggested_remediations = self._generate_remediation_actions(
            unique_findings, request.files, security_score
        )

        scan_summary = self._engine.build_summary(
            unique_findings, len(request.files), severity_counts
        )

        sarif_output = None
        if request.output_format == "sarif":
            sarif_output = self._engine.generate_sarif(
                unique_findings, len(request.files)
            )

        return IacGuardrailsResponse(
            passed=passed,
            summary=summary,
            findings=unique_findings[:100],
            files_scanned=len(request.files),
            rules_evaluated=rules_count,
            scan_summary=scan_summary,
            sarif=sarif_output,
            analysis_depth_used="fast",
            security_score=security_score,
            compliance_score=compliance_score,
            risk_level=risk_level,
            suggested_remediations=suggested_remediations,
        )

    async def _execute_core_logic_async(self, request: IacGuardrailsRequest, **kwargs):
        """Version async avec support deep analysis et auto-remediation."""
        ctx = kwargs.get("ctx")

        # Exécution synchrone de base
        response = await asyncio.to_thread(self._execute_core_logic, request)

        if ctx is None:
            return response

        llm_insights = response.llm_insights
        security_score = response.security_score
        compliance_score = response.compliance_score
        risk_level = response.risk_level
        auto_remediation_triggered = False
        auto_remediation_result = None
        scan_summary = response.scan_summary

        # Deep analysis si demandé
        if request.analysis_depth == "deep":
            try:
                (
                    llm_insights,
                    security_score,
                    compliance_score,
                    risk_level,
                ) = await self._deep_analysis_with_llm(
                    request, response.findings, ctx=ctx
                )
            except Exception as e:
                self.logger.warning(f"Fallback mode fast suite à erreur deep: {e}")

        # Génération des actions de remédiation
        suggested_remediations = self._generate_remediation_actions(
            response.findings, request.files, security_score
        )

        # Auto-remediation si activée et score bas
        if request.auto_chain and security_score < request.remediation_threshold:
            # TODO: Implémenter l'auto-remediation complète
            pass

        # Mise à jour du résumé pour le mode deep
        if request.analysis_depth == "deep":
            scan_summary += f" 🔒 Score sécurité: {security_score:.0%}, Compliance: {compliance_score:.0%} (risque: {risk_level})."
            if llm_insights:
                scan_summary += f" {len(llm_insights)} insight(s) IA."

        return response.model_copy(
            update={
                "llm_insights": llm_insights,
                "security_score": security_score,
                "compliance_score": compliance_score,
                "risk_level": risk_level,
                "analysis_depth_used": request.analysis_depth,
                "suggested_remediations": suggested_remediations,
                "auto_remediation_triggered": auto_remediation_triggered,
                "auto_remediation_result": auto_remediation_result,
                "scan_summary": scan_summary,
            }
        )
