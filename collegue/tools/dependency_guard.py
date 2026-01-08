"""
Dependency Guard - Outil de validation des dépendances

Cet outil vérifie la validité et la sécurité des dépendances d'un projet:
- Existence des packages sur les registres officiels (PyPI, npm)
- Versions valides et non dépréciées
- Vulnérabilités connues (CVEs)
- Conflits de versions
- Packages obsolètes

Problème résolu: L'IA invente souvent des packages inexistants ("hallucinations")
ou recommande des versions obsolètes/vulnérables issues de son training data.
Valeur: Empêche les compromissions supply-chain et les erreurs de build.
Bénéfice: Évite les attaques typosquatting et les vulnérabilités connues.
"""
import os
import re
import json
import subprocess
import tempfile
import shutil
from typing import Optional, Dict, Any, List, Type
from pydantic import BaseModel, Field, field_validator
from .base import BaseTool, ToolError, ToolValidationError, ToolExecutionError


class DependencyGuardRequest(BaseModel):
    """Modèle de requête pour la validation des dépendances."""
    target: Optional[str] = Field(
        None, 
        description="Cible: fichier manifest (requirements.txt, package.json) ou répertoire"
    )
    manifest_content: Optional[str] = Field(
        None,
        description="Contenu du fichier manifest (alternative à target pour environnements isolés comme MCP)"
    )
    lock_content: Optional[str] = Field(
        None,
        description="Contenu du fichier lock (package-lock.json) - REQUIS pour npm audit en mode MCP"
    )
    manifest_type: Optional[str] = Field(
        None,
        description="Type de manifest si manifest_content fourni: requirements.txt, package.json, pyproject.toml"
    )
    language: str = Field(
        ..., 
        description="Langage: python ou typescript/javascript"
    )
    check_vulnerabilities: Optional[bool] = Field(
        True,
        description="Vérifier les vulnérabilités connues (CVEs) via npm audit / pip-audit"
    )
    check_existence: Optional[bool] = Field(
        True,
        description="Vérifier que les packages existent sur le registre"
    )
    check_versions: Optional[bool] = Field(
        True,
        description="Vérifier les versions (obsolètes, dépréciées)"
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
            return 'javascript'  # Normaliser
        if v not in ['python', 'javascript']:
            raise ValueError(f"Langage '{v}' non supporté. Utilisez: python, typescript, javascript")
        return v
    
    def model_post_init(self, __context):
        """Valide que target ou manifest_content est fourni."""
        if not self.target and not self.manifest_content:
            raise ValueError("Vous devez fournir 'target' (chemin) ou 'manifest_content' (contenu du fichier)")


class DependencyIssue(BaseModel):
    """Un problème détecté sur une dépendance."""
    package: str = Field(..., description="Nom du package")
    version: Optional[str] = Field(None, description="Version concernée")
    issue_type: str = Field(..., description="Type: not_found, vulnerable, deprecated, blocked, version_conflict")
    severity: str = Field(..., description="Sévérité: low, medium, high, critical")
    message: str = Field(..., description="Description du problème")
    recommendation: str = Field(..., description="Recommandation pour corriger")
    cve_ids: Optional[List[str]] = Field(None, description="IDs CVE si vulnérabilité")


class DependencyInfo(BaseModel):
    """Information sur une dépendance."""
    name: str = Field(..., description="Nom du package")
    version_spec: str = Field(..., description="Spécification de version demandée")
    resolved_version: Optional[str] = Field(None, description="Version résolue")
    latest_version: Optional[str] = Field(None, description="Dernière version disponible")
    is_outdated: bool = Field(False, description="True si une version plus récente existe")
    status: str = Field("ok", description="Statut: ok, warning, error")


class DependencyGuardResponse(BaseModel):
    """Modèle de réponse pour la validation des dépendances."""
    valid: bool = Field(..., description="True si toutes les dépendances sont valides")
    total_dependencies: int = Field(..., description="Nombre total de dépendances")
    issues_count: int = Field(..., description="Nombre de problèmes détectés")
    critical_issues: int = Field(0, description="Problèmes critiques")
    high_issues: int = Field(0, description="Problèmes haute sévérité")
    medium_issues: int = Field(0, description="Problèmes moyenne sévérité")
    low_issues: int = Field(0, description="Problèmes basse sévérité")
    dependencies: List[DependencyInfo] = Field(
        default_factory=list,
        description="Liste des dépendances analysées"
    )
    issues: List[DependencyIssue] = Field(
        default_factory=list,
        description="Liste des problèmes détectés"
    )
    manifest_file: str = Field(..., description="Fichier manifest analysé")
    summary: str = Field(..., description="Résumé de l'analyse")


class DependencyGuardTool(BaseTool):
    """
    Outil de validation des dépendances.
    
    Vérifie:
    - Existence des packages (PyPI, npm)
    - Versions valides
    - Vulnérabilités connues (via pip-audit, npm audit)
    - Packages dans la blocklist
    - Packages non autorisés (si allowlist définie)
    
    Prévient:
    - Hallucinations IA (packages inventés)
    - Attaques typosquatting
    - Vulnérabilités supply-chain
    """

    # Packages connus comme malveillants ou typosquats
    KNOWN_MALICIOUS_PACKAGES = {
        'python': [
            'python-dateutil',  # Typosquat de python-dateutil
            'jeIlyfish',  # Typosquat de jellyfish (avec I majuscule)
            'python3-dateutil',
            'request',  # Typosquat de requests
            'beautifulsoup',  # Typosquat de beautifulsoup4
        ],
        'javascript': [
            'crossenv',  # Malveillant
            'event-stream',  # Compromis en 2018
            'flatmap-stream',  # Malveillant
            'eslint-scope',  # Version compromise
        ]
    }
    
    # Packages dépréciés avec leurs remplaçants
    DEPRECATED_PACKAGES = {
        'python': {
            'pycrypto': 'pycryptodome',
            'PIL': 'pillow',
            'distribute': 'setuptools',
            'nose': 'pytest',
            'mock': 'unittest.mock (built-in)',
        },
        'javascript': {
            'request': 'axios ou node-fetch',
            'moment': 'dayjs ou date-fns',
            'underscore': 'lodash',
            'bower': 'npm ou yarn',
        }
    }

    def get_name(self) -> str:
        return "dependency_guard"

    def get_description(self) -> str:
        return "Valide les dépendances d'un projet (existence, versions, vulnérabilités, supply chain)"

    def get_request_model(self) -> Type[BaseModel]:
        return DependencyGuardRequest

    def get_response_model(self) -> Type[BaseModel]:
        return DependencyGuardResponse

    def get_supported_languages(self) -> List[str]:
        return ["python", "typescript", "javascript"]

    def is_long_running(self) -> bool:
        return True  # Les vérifications peuvent prendre du temps

    def get_usage_description(self) -> str:
        return (
            "Outil de validation des dépendances qui vérifie l'existence des packages, "
            "les versions, les vulnérabilités connues et les risques supply-chain. "
            "Supporte Python (requirements.txt, pyproject.toml) et JavaScript/TypeScript (package.json)."
        )

    def get_examples(self) -> List[Dict[str, Any]]:
        return [
            {
                "title": "Vérifier un requirements.txt",
                "request": {
                    "target": "requirements.txt",
                    "language": "python"
                }
            },
            {
                "title": "Vérifier un package.json",
                "request": {
                    "target": "package.json",
                    "language": "typescript"
                }
            },
            {
                "title": "Vérifier avec allowlist",
                "request": {
                    "target": "requirements.txt",
                    "language": "python",
                    "allowlist": ["django", "flask", "fastapi", "requests"]
                }
            },
            {
                "title": "Vérifier uniquement les vulnérabilités",
                "request": {
                    "target": ".",
                    "language": "python",
                    "check_existence": False,
                    "check_versions": False,
                    "check_vulnerabilities": True
                }
            }
        ]

    def get_capabilities(self) -> List[str]:
        return [
            "Vérification d'existence des packages sur PyPI/npm",
            "Détection de packages inventés (hallucinations IA)",
            "Détection de vulnérabilités connues (CVEs)",
            "Détection de packages dépréciés",
            "Support des allowlists et blocklists",
            "Détection de typosquatting",
            "Analyse de requirements.txt, pyproject.toml, package.json"
        ]

    def get_required_config_keys(self) -> List[str]:
        return []

    def _parse_requirements_txt(self, filepath: str) -> List[Dict[str, str]]:
        """Parse un fichier requirements.txt."""
        dependencies = []
        
        with open(filepath, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                
                # Ignorer commentaires et lignes vides
                if not line or line.startswith('#') or line.startswith('-'):
                    continue
                
                # Parser le nom et la version
                # Formats: package, package==1.0, package>=1.0, package[extra]
                match = re.match(r'^([a-zA-Z0-9_-]+)(?:\[.*\])?\s*((?:==|>=|<=|>|<|~=|!=)[^\s;#]+)?', line)
                if match:
                    name = match.group(1)
                    version = match.group(2) or '*'
                    dependencies.append({'name': name, 'version': version})
        
        return dependencies

    def _parse_pyproject_toml(self, filepath: str) -> List[Dict[str, str]]:
        """Parse les dépendances d'un pyproject.toml."""
        dependencies = []
        
        try:
            import tomllib
        except ImportError:
            try:
                import tomli as tomllib
            except ImportError:
                # Fallback: parsing simple
                with open(filepath, 'r', encoding='utf-8') as f:
                    content = f.read()
                
                # Chercher la section dependencies
                deps_match = re.search(r'\[project\].*?dependencies\s*=\s*\[(.*?)\]', content, re.DOTALL)
                if deps_match:
                    deps_str = deps_match.group(1)
                    for line in deps_str.split('\n'):
                        line = line.strip().strip(',').strip('"\'')
                        if line:
                            match = re.match(r'^([a-zA-Z0-9_-]+)', line)
                            if match:
                                dependencies.append({'name': match.group(1), 'version': '*'})
                return dependencies
        
        with open(filepath, 'rb') as f:
            data = tomllib.load(f)
        
        # project.dependencies
        for dep in data.get('project', {}).get('dependencies', []):
            match = re.match(r'^([a-zA-Z0-9_-]+)', dep)
            if match:
                dependencies.append({'name': match.group(1), 'version': '*'})
        
        return dependencies

    def _parse_package_json(self, filepath: str) -> List[Dict[str, str]]:
        """Parse un fichier package.json."""
        dependencies = []
        
        with open(filepath, 'r', encoding='utf-8') as f:
            data = json.load(f)
        
        # dependencies et devDependencies
        all_deps = {
            **data.get('dependencies', {}),
            **data.get('devDependencies', {})
        }
        
        for name, version in all_deps.items():
            dependencies.append({'name': name, 'version': version})
        
        return dependencies

    def _check_pypi_existence(self, package_name: str) -> Dict[str, Any]:
        """Vérifie si un package existe sur PyPI."""
        try:
            result = subprocess.run(
                ['pip', 'index', 'versions', package_name],
                capture_output=True,
                text=True,
                timeout=30
            )
            
            if result.returncode == 0:
                # Extraire la dernière version
                match = re.search(r'Available versions: ([^\s,]+)', result.stdout)
                latest = match.group(1) if match else None
                return {'exists': True, 'latest_version': latest}
            else:
                return {'exists': False, 'latest_version': None}
        except subprocess.TimeoutExpired:
            return {'exists': None, 'latest_version': None, 'error': 'timeout'}
        except Exception as e:
            return {'exists': None, 'latest_version': None, 'error': str(e)}

    def _check_npm_existence(self, package_name: str) -> Dict[str, Any]:
        """Vérifie si un package existe sur npm."""
        try:
            result = subprocess.run(
                ['npm', 'view', package_name, 'version'],
                capture_output=True,
                text=True,
                timeout=30
            )
            
            if result.returncode == 0:
                latest = result.stdout.strip()
                return {'exists': True, 'latest_version': latest}
            else:
                return {'exists': False, 'latest_version': None}
        except subprocess.TimeoutExpired:
            return {'exists': None, 'latest_version': None, 'error': 'timeout'}
        except Exception as e:
            return {'exists': None, 'latest_version': None, 'error': str(e)}

    def _check_pip_audit(self, working_dir: str) -> List[Dict[str, Any]]:
        """Exécute pip-audit pour trouver les vulnérabilités."""
        vulnerabilities = []
        
        try:
            result = subprocess.run(
                ['pip-audit', '--format', 'json'],
                capture_output=True,
                text=True,
                timeout=120,
                cwd=working_dir
            )
            
            if result.stdout:
                data = json.loads(result.stdout)
                for vuln in data:
                    vulnerabilities.append({
                        'package': vuln.get('name'),
                        'version': vuln.get('version'),
                        'vulnerability_id': vuln.get('id'),
                        'description': vuln.get('description', ''),
                        'fix_versions': vuln.get('fix_versions', [])
                    })
        except (subprocess.TimeoutExpired, FileNotFoundError, json.JSONDecodeError):
            pass
        
        return vulnerabilities

    def _check_npm_audit(self, working_dir: str) -> List[Dict[str, Any]]:
        """Exécute npm audit pour trouver les vulnérabilités."""
        vulnerabilities = []
        
        try:
            result = subprocess.run(
                ['npm', 'audit', '--json'],
                capture_output=True,
                text=True,
                timeout=120,
                cwd=working_dir
            )
            
            if result.stdout:
                data = json.loads(result.stdout)
                
                # npm audit v7+ format
                for name, advisory in data.get('vulnerabilities', {}).items():
                    vulnerabilities.append({
                        'package': name,
                        'version': advisory.get('range', '*'),
                        'vulnerability_id': advisory.get('via', [{}])[0].get('url', '') if isinstance(advisory.get('via', []), list) else '',
                        'severity': advisory.get('severity', 'unknown'),
                        'description': advisory.get('via', [{}])[0].get('title', '') if isinstance(advisory.get('via', []), list) else str(advisory.get('via', '')),
                        'fix_available': advisory.get('fixAvailable', False)
                    })
        except (subprocess.TimeoutExpired, FileNotFoundError, json.JSONDecodeError):
            pass
        
        return vulnerabilities

    def _parse_requirements_txt_content(self, content: str) -> List[Dict[str, str]]:
        """Parse le contenu d'un fichier requirements.txt."""
        dependencies = []
        for line in content.split('\n'):
            line = line.strip()
            if not line or line.startswith('#') or line.startswith('-'):
                continue
            match = re.match(r'^([a-zA-Z0-9_-]+)(?:\[.*\])?\s*((?:==|>=|<=|>|<|~=|!=)[^\s;#]+)?', line)
            if match:
                name = match.group(1)
                version = match.group(2) or '*'
                dependencies.append({'name': name, 'version': version})
        return dependencies

    def _parse_package_json_content(self, content: str) -> List[Dict[str, str]]:
        """Parse le contenu d'un fichier package.json."""
        dependencies = []
        try:
            data = json.loads(content)
            all_deps = {
                **data.get('dependencies', {}),
                **data.get('devDependencies', {})
            }
            for name, version in all_deps.items():
                dependencies.append({'name': name, 'version': version})
        except json.JSONDecodeError as e:
            raise ToolValidationError(f"Erreur de parsing JSON: {e}")
        return dependencies

    def _execute_core_logic(self, request: DependencyGuardRequest, **kwargs) -> DependencyGuardResponse:
        """Exécute la validation des dépendances."""
        issues = []
        dependencies_info = []
        manifest_file = ""
        working_dir = os.getcwd()
        temp_dir = None
        deps = []
        
        try:
            # Mode 1: Contenu fourni directement (pour MCP et environnements isolés)
            if request.manifest_content:
                manifest_type = request.manifest_type or ('package.json' if request.language == 'javascript' else 'requirements.txt')
                manifest_file = f"[content:{manifest_type}]"
                
                if manifest_type in ['requirements.txt', 'requirements']:
                    deps = self._parse_requirements_txt_content(request.manifest_content)
                    # Pour pip-audit, créer un fichier temporaire
                    if request.check_vulnerabilities:
                        temp_dir = tempfile.mkdtemp(prefix="collegue_dep_guard_")
                        req_path = os.path.join(temp_dir, 'requirements.txt')
                        with open(req_path, 'w', encoding='utf-8') as f:
                            f.write(request.manifest_content)
                        working_dir = temp_dir
                        
                elif manifest_type in ['package.json', 'package']:
                    deps = self._parse_package_json_content(request.manifest_content)
                    # Pour npm audit, créer un répertoire temporaire avec package.json et lock
                    if request.check_vulnerabilities:
                        temp_dir = tempfile.mkdtemp(prefix="collegue_dep_guard_")
                        pkg_path = os.path.join(temp_dir, 'package.json')
                        with open(pkg_path, 'w', encoding='utf-8') as f:
                            f.write(request.manifest_content)
                        
                        if request.lock_content:
                            lock_path = os.path.join(temp_dir, 'package-lock.json')
                            with open(lock_path, 'w', encoding='utf-8') as f:
                                f.write(request.lock_content)
                            working_dir = temp_dir
                            self.logger.info(f"Répertoire temporaire créé pour npm audit: {temp_dir}")
                        else:
                            self.logger.warning("lock_content non fourni - npm audit sera limité")
                            # Ajouter un avertissement dans les issues
                            issues.append(DependencyIssue(
                                package="[npm audit]",
                                version=None,
                                issue_type='warning',
                                severity='low',
                                message="package-lock.json non fourni - impossible de détecter toutes les vulnérabilités",
                                recommendation="Passez le contenu de package-lock.json via le paramètre 'lock_content' pour une analyse complète"
                            ))
                            working_dir = temp_dir
                        
                elif manifest_type in ['pyproject.toml', 'pyproject']:
                    # Pour pyproject.toml, on utilise le parsing simplifié
                    deps_match = re.search(r'dependencies\s*=\s*\[(.*?)\]', request.manifest_content, re.DOTALL)
                    if deps_match:
                        deps_str = deps_match.group(1)
                        for line in deps_str.split('\n'):
                            line = line.strip().strip(',').strip('"\'')
                            if line:
                                match = re.match(r'^([a-zA-Z0-9_-]+)', line)
                                if match:
                                    deps.append({'name': match.group(1), 'version': '*'})
                else:
                    raise ToolValidationError(f"Type de manifest '{manifest_type}' non supporté")
            
            # Mode 2: Chemin de fichier (mode classique)
            elif request.target:
                target = request.target
                if os.path.isdir(target):
                    if request.language == 'python':
                        candidates = ['requirements.txt', 'pyproject.toml', 'setup.py']
                    else:
                        candidates = ['package.json']
                    
                    for candidate in candidates:
                        path = os.path.join(target, candidate)
                        if os.path.exists(path):
                            target = path
                            break
                    else:
                        raise ToolValidationError(f"Aucun fichier manifest trouvé dans {request.target}")
                
                if not os.path.isfile(target):
                    raise ToolValidationError(f"Fichier '{target}' inexistant. Utilisez 'manifest_content' pour passer le contenu directement.")
                
                manifest_file = os.path.basename(target)
                working_dir = os.path.dirname(os.path.abspath(target))
                
                # Parser les dépendances depuis le fichier
                if request.language == 'python':
                    if target.endswith('.txt'):
                        deps = self._parse_requirements_txt(target)
                    elif target.endswith('.toml'):
                        deps = self._parse_pyproject_toml(target)
                else:  # javascript
                    if target.endswith('.json'):
                        deps = self._parse_package_json(target)
            
            # Analyser chaque dépendance
            for dep in deps:
                name = dep['name']
                version = dep['version']
                dep_info = DependencyInfo(
                    name=name,
                    version_spec=version,
                    status='ok'
                )
                
                # Vérifier blocklist
                if request.blocklist and name.lower() in [b.lower() for b in request.blocklist]:
                    issues.append(DependencyIssue(
                        package=name,
                        version=version,
                        issue_type='blocked',
                        severity='high',
                        message=f"Package '{name}' est dans la liste noire",
                        recommendation=f"Supprimez ce package ou trouvez une alternative"
                    ))
                    dep_info.status = 'error'
                
                # Vérifier allowlist
                if request.allowlist and name.lower() not in [a.lower() for a in request.allowlist]:
                    issues.append(DependencyIssue(
                        package=name,
                        version=version,
                        issue_type='not_allowed',
                        severity='medium',
                        message=f"Package '{name}' n'est pas dans la liste blanche",
                        recommendation=f"Ajoutez '{name}' à l'allowlist ou supprimez-le"
                    ))
                    dep_info.status = 'warning'
                
                # Vérifier packages malveillants connus
                malicious = self.KNOWN_MALICIOUS_PACKAGES.get(request.language, [])
                if name.lower() in [m.lower() for m in malicious]:
                    issues.append(DependencyIssue(
                        package=name,
                        version=version,
                        issue_type='malicious',
                        severity='critical',
                        message=f"Package '{name}' est connu comme malveillant ou typosquat",
                        recommendation="Supprimez immédiatement ce package!"
                    ))
                    dep_info.status = 'error'
                
                # Vérifier packages dépréciés
                deprecated = self.DEPRECATED_PACKAGES.get(request.language, {})
                if name.lower() in [d.lower() for d in deprecated.keys()]:
                    replacement = deprecated.get(name, 'une alternative')
                    issues.append(DependencyIssue(
                        package=name,
                        version=version,
                        issue_type='deprecated',
                        severity='low',
                        message=f"Package '{name}' est déprécié",
                        recommendation=f"Utilisez {replacement} à la place"
                    ))
                    dep_info.status = 'warning' if dep_info.status == 'ok' else dep_info.status
                
                # Vérifier existence sur le registre
                if request.check_existence:
                    if request.language == 'python':
                        check = self._check_pypi_existence(name)
                    else:
                        check = self._check_npm_existence(name)
                    
                    if check.get('exists') is False:
                        issues.append(DependencyIssue(
                            package=name,
                            version=version,
                            issue_type='not_found',
                            severity='critical',
                            message=f"Package '{name}' n'existe pas sur le registre officiel",
                            recommendation="Vérifiez l'orthographe. Ce package pourrait être une hallucination IA ou un typosquat."
                        ))
                        dep_info.status = 'error'
                    elif check.get('latest_version'):
                        dep_info.latest_version = check['latest_version']
                        # Vérifier si outdated (simpliste)
                        if version.startswith('=='):
                            current = version[2:]
                            if current != check['latest_version']:
                                dep_info.is_outdated = True
                
                dependencies_info.append(dep_info)
            
            # Vérifier vulnérabilités
            if request.check_vulnerabilities:
                if request.language == 'python':
                    vulns = self._check_pip_audit(working_dir)
                else:
                    vulns = self._check_npm_audit(working_dir)
                
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
                        recommendation=f"Mettez à jour vers une version corrigée: {vuln.get('fix_versions', ['dernière version'])}",
                        cve_ids=[vuln.get('vulnerability_id')] if vuln.get('vulnerability_id') else None
                    ))
            
            # Compter par sévérité
            severity_counts = {'critical': 0, 'high': 0, 'medium': 0, 'low': 0}
            for issue in issues:
                severity_counts[issue.severity] = severity_counts.get(issue.severity, 0) + 1
            
            # Construire le résumé
            total_deps = len(deps)
            total_issues = len(issues)
            
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
                total_dependencies=total_deps,
                issues_count=total_issues,
                critical_issues=severity_counts['critical'],
                high_issues=severity_counts['high'],
                medium_issues=severity_counts['medium'],
                low_issues=severity_counts['low'],
                dependencies=dependencies_info,
                issues=issues,
                manifest_file=manifest_file,
                summary=summary
            )
        
        finally:
            # Nettoyer le répertoire temporaire
            if temp_dir and os.path.exists(temp_dir):
                try:
                    shutil.rmtree(temp_dir)
                except Exception as e:
                    self.logger.warning(f"Impossible de supprimer le répertoire temporaire: {e}")
