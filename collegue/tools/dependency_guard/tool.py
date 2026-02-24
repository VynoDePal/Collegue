"""
Dependency Guard - Outil de validation des dépendances.

Cet outil vérifie la validité et la sécurité des dépendances d'un projet:
- Existence des packages sur les registres officiels (PyPI, npm)
- Versions valides et non dépréciées
- Vulnérabilités connues (CVEs)
- Conflits de versions
- Packages obsolètes

Refactorisé: Le fichier original faisait 834 lignes, maintenant ~200 lignes.
"""
from typing import List, Dict, Any
from concurrent.futures import ThreadPoolExecutor

from ..base import BaseTool
from ...core.shared import aggregate_severities
from .models import DependencyGuardRequest, DependencyGuardResponse, DependencyIssue
from .engine import DependencyAnalysisEngine
from .config import KNOWN_MALICIOUS_PACKAGES, DEPRECATED_PACKAGES, LANGUAGE_ECOSYSTEM


class DependencyGuardTool(BaseTool):
    """
    Outil de validation des dépendances.
    
    Détecte les packages inexistant (hallucinations IA), les vulnérabilités
    connues, les packages dépréciés et les risques supply-chain.
    """
    
    tool_name = "dependency_guard"
    tool_description = "Valide les dépendances d'un projet (existence, versions, vulnérabilités, supply chain)"
    tags = {"security", "analysis"}
    request_model = DependencyGuardRequest
    response_model = DependencyGuardResponse
    supported_languages = ["python", "typescript", "javascript", "php"]
    long_running = True
    
    def __init__(self, config=None, app_state=None):
        super().__init__(config, app_state)
        self._engine = DependencyAnalysisEngine(logger=self.logger)
    
    def get_usage_description(self) -> str:
        return (
            "Outil de validation des dépendances qui vérifie l'existence des packages, "
            "les versions, les vulnérabilités connues et les risques supply-chain. "
            "Supporte Python, JavaScript/TypeScript et PHP."
        )
    
    def get_examples(self) -> List[Dict[str, Any]]:
        return [
            {
                "title": "Vérifier des dépendances Python (requirements.txt)",
                "request": {
                    "content": "django>=4.0\nrequests>=2.28\nflask>=2.0",
                    "language": "python"
                }
            },
            {
                "title": "Vérifier des vulnérabilités JS/TS (package-lock.json)",
                "request": {
                    "content": '{ "lockfileVersion": 3, "packages": { ... } }',
                    "language": "typescript",
                    "check_vulnerabilities": True
                }
            },
            {
                "title": "Vérifier avec allowlist",
                "request": {
                    "content": "django>=4.0\nrequests>=2.28",
                    "language": "python",
                    "allowlist": ["django", "flask", "fastapi", "requests"]
                }
            }
        ]
    
    def get_capabilities(self) -> List[str]:
        return [
            "Vérification d'existence des packages sur PyPI/npm/Packagist",
            "Détection de packages inventés (hallucinations IA)",
            "Détection de vulnérabilités connues (CVEs)",
            "Détection de packages dépréciés",
            "Support des allowlists et blocklists",
            "Détection de typosquatting",
            "Analyse de requirements.txt, pyproject.toml, package.json, composer.json"
        ]
    
    def _check_single_dep(self, dep: Dict[str, str], language: str, check_existence: bool,
                         allowlist: List[str], blocklist: List[str]) -> List[DependencyIssue]:
        """Vérifie une seule dépendance."""
        issues = []
        name = dep['name']
        version = dep['version']
        
        # Vérifier la blocklist
        if blocklist and name.lower() in [b.lower() for b in blocklist]:
            issues.append(DependencyIssue(
                package=name, version=version, issue_type='blocked', severity='high',
                message=f"Package '{name}' est dans la liste noire",
                recommendation="Supprimez ce package ou trouvez une alternative"
            ))
        
        # Vérifier l'allowlist
        if allowlist and name.lower() not in [a.lower() for a in allowlist]:
            issues.append(DependencyIssue(
                package=name, version=version, issue_type='not_allowed', severity='medium',
                message=f"Package '{name}' n'est pas dans la liste blanche",
                recommendation=f"Ajoutez '{name}' à l'allowlist ou supprimez-le"
            ))
        
        # Vérifier les packages malveillants
        malicious = KNOWN_MALICIOUS_PACKAGES.get(language, [])
        if name.lower() in [m.lower() for m in malicious]:
            issues.append(DependencyIssue(
                package=name, version=version, issue_type='malicious', severity='critical',
                message=f"Package '{name}' est connu comme malveillant ou typosquat",
                recommendation="Supprimez immédiatement ce package!"
            ))
        
        # Vérifier les packages dépréciés
        deprecated = DEPRECATED_PACKAGES.get(language, {})
        if name.lower() in [d.lower() for d in deprecated.keys()]:
            replacement = deprecated.get(name, 'une alternative')
            issues.append(DependencyIssue(
                package=name, version=version, issue_type='deprecated', severity='low',
                message=f"Package '{name}' est déprécié",
                recommendation=f"Utilisez {replacement} à la place"
            ))
        
        # Vérifier l'existence sur le registre
        if check_existence:
            check = self._engine.check_package_existence(name, language)
            if check.get('exists') is False:
                issues.append(DependencyIssue(
                    package=name, version=version, issue_type='not_found', severity='critical',
                    message=f"Package '{name}' n'existe pas sur le registre officiel",
                    recommendation="Vérifiez l'orthographe. Ce package pourrait être une hallucination IA ou un typosquat."
                ))
        
        return issues
    
    def _execute_core_logic(self, request: DependencyGuardRequest, **kwargs) -> DependencyGuardResponse:
        """Exécute la validation des dépendances."""
        issues = []
        
        # Détecter le type de fichier
        content_type = self._engine.detect_content_type(request.content, request.language)
        self.logger.info(f"Type de fichier détecté: {content_type}")
        
        # Parser les dépendances
        parsers = {
            'requirements.txt': self._engine.parse_requirements_txt,
            'package.json': self._engine.parse_package_json,
            'package-lock.json': self._engine.parse_package_lock,
            'pyproject.toml': self._engine.parse_pyproject_toml,
            'composer.json': self._engine.parse_composer_json,
            'composer.lock': self._engine.parse_composer_lock,
        }
        
        parser = parsers.get(content_type)
        if not parser:
            raise ToolValidationError(f"Type de fichier '{content_type}' non supporté")
        
        deps = parser(request.content)
        
        # Vérifier les dépendances en parallèle
        def check_dep(dep):
            return self._check_single_dep(
                dep, request.language, request.check_existence,
                request.allowlist or [], request.blocklist or []
            )
        
        with ThreadPoolExecutor(max_workers=10) as executor:
            results = list(executor.map(check_dep, deps))
            for res in results:
                issues.extend(res)
        
        # Vérifier les vulnérabilités
        if request.check_vulnerabilities:
            ecosystem = LANGUAGE_ECOSYSTEM.get(request.language, 'npm')
            vulns = self._engine.check_osv_vulnerabilities(deps, ecosystem)
            
            for vuln in vulns:
                severity = vuln.get('severity', 'high')
                if severity not in ['low', 'medium', 'high', 'critical']:
                    severity = 'high'
                
                issues.append(DependencyIssue(
                    package=vuln['package'],
                    version=vuln.get('version'),
                    issue_type='vulnerable',
                    severity=severity,
                    message=vuln.get('description', 'Vulnérabilité connue'),
                    recommendation=f"Mettez à jour vers: {vuln.get('fix_versions', ['dernière version'])}",
                    cve_ids=[vuln.get('vulnerability_id')] if vuln.get('vulnerability_id') else None
                ))
        
        # Calculer les statistiques
        severity_counts = aggregate_severities(issues)
        total_deps = len(deps)
        total_issues = len(issues)
        
        # Construire le résumé
        if total_issues == 0:
            summary = f"✅ {total_deps} dépendance(s) analysée(s), aucun problème détecté."
        else:
            summary = (
                f"⚠️ {total_deps} dépendance(s) analysée(s), {total_issues} problème(s) détecté(s). "
                f"Critique: {severity_counts['critical']}, Haute: {severity_counts['high']}, "
                f"Moyenne: {severity_counts['medium']}, Basse: {severity_counts['low']}."
            )
        
        return DependencyGuardResponse(
            valid=total_issues == 0 or (severity_counts['critical'] == 0 and severity_counts['high'] == 0),
            summary=summary,
            total_dependencies=total_deps,
            vulnerabilities=total_issues,
            critical=severity_counts['critical'],
            high=severity_counts['high'],
            medium=severity_counts['medium'],
            low=severity_counts['low'],
            issues=issues
        )
