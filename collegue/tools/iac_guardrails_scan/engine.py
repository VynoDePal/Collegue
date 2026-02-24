"""
Moteur d'analyse pour l'outil IaC Guardrails Scan.

Contient la logique métier pure, séparée de l'orchestration du Tool.
"""
import re
import yaml
import json
from typing import List, Tuple, Optional, Dict, Any
from ...core.shared import aggregate_severities, parse_llm_json_response
from .models import IacFinding, IacGuardrailsRequest, FileInput
from .config import SEVERITY_WEIGHTS, RISK_THRESHOLDS


class IacAnalysisEngine:
    """Moteur d'analyse des fichiers IaC."""

    def __init__(self, k8s_rules: dict, tf_rules: dict, dockerfile_rules: dict, logger=None):
        self.k8s_rules = k8s_rules
        self.tf_rules = tf_rules
        self.dockerfile_rules = dockerfile_rules
        self.logger = logger

    def detect_file_type(self, filepath: str, content: str) -> str:
        """Détecte le type de fichier IaC."""
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

    def apply_regex_rule(self, rule: Dict, content: str, filepath: str, lines: List[str]) -> List[IacFinding]:
        """Applique une règle regex sur le contenu d'un fichier."""
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
            if self.logger:
                self.logger.warning(f"Erreur regex pour {rule['id']}: {e}")

        return findings

    def scan_kubernetes(self, content: str, filepath: str, profile: str) -> List[IacFinding]:
        """Scan un fichier Kubernetes."""
        findings = []
        lines = content.split('\n')

        for rule in self.k8s_rules.get('baseline', []):
            findings.extend(self.apply_regex_rule(rule, content, filepath, lines))

        if profile == 'strict':
            if 'securityContext' in content:
                for rule in self.k8s_rules.get('strict', []):
                    findings.extend(self.apply_regex_rule(rule, content, filepath, lines))
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

    def scan_terraform(self, content: str, filepath: str, profile: str) -> List[IacFinding]:
        """Scan un fichier Terraform."""
        findings = []
        lines = content.split('\n')

        for rule in self.tf_rules.get('baseline', []):
            findings.extend(self.apply_regex_rule(rule, content, filepath, lines))

        if profile == 'strict':
            for rule in self.tf_rules.get('strict', []):
                findings.extend(self.apply_regex_rule(rule, content, filepath, lines))

        return findings

    def scan_dockerfile(self, content: str, filepath: str, profile: str) -> List[IacFinding]:
        """Scan un Dockerfile."""
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

        for rule in self.dockerfile_rules.get('baseline', []):
            if rule['id'] == 'DOCKER-001':
                continue
            findings.extend(self.apply_regex_rule(rule, content, filepath, lines))

        if profile == 'strict':
            for rule in self.dockerfile_rules.get('strict', []):
                findings.extend(self.apply_regex_rule(rule, content, filepath, lines))

        return findings

    def convert_findings(self, scanner_findings: list) -> list:
        """Convertit les findings des scanners en IacFinding."""
        result = []
        for f in scanner_findings:
            # Si c'est déjà un IacFinding de notre module, le garder tel quel
            if isinstance(f, IacFinding):
                result.append(f)
                continue
                
            # Sinon, essayer de convertir depuis le scanner
            if hasattr(f, 'rule_id'):
                # C'est un finding du scanner, le convertir
                title = getattr(f, 'rule_title', getattr(f, 'title', 'Unknown'))
                description = getattr(f, 'message', getattr(f, 'description', ''))
                result.append(IacFinding(
                    rule_id=f.rule_id,
                    severity=f.severity,
                    path=f.path,
                    line=f.line,
                    title=title,
                    description=description,
                    remediation=getattr(f, 'remediation', ''),
                    references=getattr(f, 'references', []),
                    engine=getattr(f, 'engine', 'embedded-rules')
                ))
            else:
                # Type inconnu, l'ajouter tel quel
                result.append(f)
        return result

    def calculate_security_scores(self, findings: List[IacFinding]) -> Tuple[float, float, str]:
        """Calcule les scores de sécurité et de conformité."""
        if not findings:
            return 1.0, 1.0, "low"

        total_weight = sum(SEVERITY_WEIGHTS.get(f.severity, 0.05) for f in findings)
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

    def deduplicate_findings(self, findings: List[IacFinding]) -> List[IacFinding]:
        """Déduplique les findings basés sur (rule_id, path, line)."""
        seen = set()
        unique_findings = []
        for f in findings:
            key = (f.rule_id, f.path, f.line)
            if key not in seen:
                seen.add(key)
                unique_findings.append(f)
        return unique_findings

    def generate_sarif(self, findings: List[IacFinding], files_scanned: int) -> Dict:
        """Génère la sortie au format SARIF."""
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

    def build_summary(self, findings: List[IacFinding], files_count: int, severity_counts: dict) -> str:
        """Construit le résumé textuel du scan."""
        if not findings:
            return f"✅ Aucun problème de sécurité détecté dans {files_count} fichier(s) IaC."

        passed = severity_counts['critical'] == 0 and severity_counts['high'] == 0

        if passed:
            return (
                f"⚠️ {len(findings)} problème(s) mineur(s) détecté(s) dans {files_count} fichier(s). "
                f"Moyenne({severity_counts['medium']}), Basse({severity_counts['low']})."
            )
        else:
            return (
                f"🚨 {len(findings)} problème(s) de sécurité dans {files_count} fichier(s)! "
                f"Critique({severity_counts['critical']}), Haute({severity_counts['high']}), "
                f"Moyenne({severity_counts['medium']}), Basse({severity_counts['low']})."
            )
