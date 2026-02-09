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
import urllib.request
import urllib.error
from typing import Optional, Dict, Any, List, Type
from pydantic import BaseModel, Field, field_validator
from .base import BaseTool, ToolError, ToolValidationError, ToolExecutionError


class DependencyGuardRequest(BaseModel):
    """Modèle de requête pour la validation des dépendances.

    NOTE: Cet outil utilise l'API OSV de Google pour scanner les vulnérabilités.
    Passez le contenu du fichier via 'content'. Le type est auto-détecté.

    Pour JS/TS: passez le contenu de package-lock.json (contient les versions exactes).
    Pour Python: passez le contenu de requirements.txt ou pyproject.toml.
    """
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
        if v not in ['python', 'javascript']:
            raise ValueError(f"Langage '{v}' non supporté. Utilisez: python, typescript, javascript")
        return v


class DependencyIssue(BaseModel):
    """Un problème détecté sur une dépendance."""
    package: str = Field(..., description="Nom du package")
    version: Optional[str] = Field(None, description="Version concernée")
    issue_type: str = Field(..., description="Type: not_found, vulnerable, deprecated, blocked, version_conflict")
    severity: str = Field(..., description="Sévérité: low, medium, high, critical")
    message: str = Field(..., description="Description du problème")
    recommendation: str = Field(..., description="Recommandation pour corriger")
    cve_ids: Optional[List[str]] = Field(None, description="IDs CVE si vulnérabilité")


class DependencyGuardResponse(BaseModel):
    """Modèle de réponse pour la validation des dépendances."""
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


    KNOWN_MALICIOUS_PACKAGES = {
        'python': [
            'python-dateutil',
            'jeIlyfish',
            'python3-dateutil',
            'request',
            'beautifulsoup',
        ],
        'javascript': [
            'crossenv',
            'event-stream',
            'flatmap-stream',
            'eslint-scope',
        ]
    }


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
        return True

    def get_usage_description(self) -> str:
        return (
            "Outil de validation des dépendances qui vérifie l'existence des packages, "
            "les versions, les vulnérabilités connues et les risques supply-chain. "
            "Supporte Python (requirements.txt, pyproject.toml) et JavaScript/TypeScript (package.json)."
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
                    "content": "{ \"lockfileVersion\": 3, \"packages\": { ... } }",
                    "language": "typescript",
                    "check_vulnerabilities": True
                },
                "note": "Passez le contenu complet de package-lock.json"
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


    def _parse_package_lock_content(self, content: str) -> List[Dict[str, str]]:
        """Extrait les dépendances directes du package-lock.json."""
        deps = []
        try:
            data = json.loads(content)

            root_pkg = data.get('packages', {}).get('', {})
            dependencies = {
                **root_pkg.get('dependencies', {}),
                **root_pkg.get('devDependencies', {})
            }


            if not dependencies:

                dependencies = data.get('dependencies', {})

            for name, info in dependencies.items():
                version = info if isinstance(info, str) else info.get('version', '*')
                deps.append({'name': name, 'version': version})

        except json.JSONDecodeError as e:
            raise ToolValidationError(f"Erreur de parsing package-lock.json: {e}")
        return deps

    def _parse_requirements_txt(self, filepath: str) -> List[Dict[str, str]]:
        """Parse un fichier requirements.txt."""
        dependencies = []

        with open(filepath, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()


                if not line or line.startswith('#') or line.startswith('-'):
                    continue


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

                with open(filepath, 'r', encoding='utf-8') as f:
                    content = f.read()


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

    def _check_osv_vulnerabilities(self, deps: List[Dict[str, str]], ecosystem: str = "npm") -> List[Dict[str, Any]]:
        """Vérifie les vulnérabilités via l'API batch OSV de Google (gratuite, rapide).

        Args:
            deps: Liste de dépendances [{'name': 'pkg', 'version': '1.0.0'}, ...]
            ecosystem: 'npm' pour JS/TS, 'PyPI' pour Python

        Returns:
            Liste de vulnérabilités trouvées
        """
        vulnerabilities = []
        OSV_BATCH_URL = "https://api.osv.dev/v1/querybatch"
        OSV_VULN_URL = "https://api.osv.dev/v1/vulns/"


        queries = []
        dep_map = {}

        for i, dep in enumerate(deps):
            name = dep['name']
            version = self._extract_version(dep.get('version', '*'))
            if not version or version == '*':
                continue

            queries.append({
                "package": {"name": name, "ecosystem": ecosystem},
                "version": version
            })
            dep_map[len(queries) - 1] = {'name': name, 'version': version}

        if not queries:
            return vulnerabilities

        self.logger.info(f"Vérification OSV batch pour {len(queries)} packages ({ecosystem})...")

        try:

            batch_data = {"queries": queries}
            req = urllib.request.Request(
                OSV_BATCH_URL,
                data=json.dumps(batch_data).encode('utf-8'),
                headers={'Content-Type': 'application/json'}
            )

            with urllib.request.urlopen(req, timeout=60) as response:
                result = json.loads(response.read().decode('utf-8'))


            vuln_to_packages = {}

            for idx, result_item in enumerate(result.get('results', [])):
                if idx not in dep_map:
                    continue
                pkg_info = dep_map[idx]

                for vuln in result_item.get('vulns', []):
                    vuln_id = vuln.get('id', '')
                    if vuln_id:
                        if vuln_id not in vuln_to_packages:
                            vuln_to_packages[vuln_id] = []
                        vuln_to_packages[vuln_id].append(pkg_info)


            self.logger.info(f"Récupération des détails pour {len(vuln_to_packages)} vulnérabilités uniques...")

            for vuln_id, packages in vuln_to_packages.items():
                try:

                    vuln_req = urllib.request.Request(f"{OSV_VULN_URL}{vuln_id}")
                    with urllib.request.urlopen(vuln_req, timeout=10) as resp:
                        vuln_details = json.loads(resp.read().decode('utf-8'))


                    severity = self._extract_osv_severity(vuln_details)


                    description = vuln_details.get('summary', vuln_details.get('details', ''))[:200]
                    if not description:
                        description = f"Vulnérabilité {vuln_id}"


                    aliases = vuln_details.get('aliases', [])
                    cve_id = next((a for a in aliases if a.startswith('CVE-')), vuln_id)


                    fix_versions = []
                    for affected in vuln_details.get('affected', []):
                        for range_info in affected.get('ranges', []):
                            for event in range_info.get('events', []):
                                if 'fixed' in event:
                                    fix_versions.append(event['fixed'])


                    for pkg_info in packages:
                        vulnerabilities.append({
                            'package': pkg_info['name'],
                            'version': pkg_info['version'],
                            'vulnerability_id': cve_id,
                            'severity': severity,
                            'description': description,
                            'fix_versions': fix_versions or ['dernière version stable']
                        })

                except Exception as e:
                    self.logger.debug(f"Erreur récupération détails {vuln_id}: {e}")

                    for pkg_info in packages:
                        vulnerabilities.append({
                            'package': pkg_info['name'],
                            'version': pkg_info['version'],
                            'vulnerability_id': vuln_id,
                            'severity': 'medium',
                            'description': f"Vulnérabilité {vuln_id}",
                            'fix_versions': ['dernière version stable']
                        })

        except urllib.error.URLError as e:
            self.logger.warning(f"OSV batch API error: {e}")
        except json.JSONDecodeError as e:
            self.logger.warning(f"OSV JSON error: {e}")
        except Exception as e:
            self.logger.warning(f"OSV check error: {e}")

        self.logger.info(f"OSV: {len(vulnerabilities)} vulnérabilité(s) détectée(s)")
        return vulnerabilities

    def _extract_osv_severity(self, vuln_details: dict) -> str:
        """Extrait la sévérité d'une vulnérabilité OSV."""

        db_specific = vuln_details.get('database_specific', {})
        if 'severity' in db_specific:
            sev = str(db_specific['severity']).upper()
            if sev == 'CRITICAL':
                return 'critical'
            elif sev == 'HIGH':
                return 'high'
            elif sev in ('MODERATE', 'MEDIUM'):
                return 'medium'
            elif sev == 'LOW':
                return 'low'


        for affected in vuln_details.get('affected', []):
            eco_specific = affected.get('ecosystem_specific', {})
            if 'severity' in eco_specific:
                sev = str(eco_specific['severity']).upper()
                if sev == 'CRITICAL':
                    return 'critical'
                elif sev == 'HIGH':
                    return 'high'
                elif sev in ('MODERATE', 'MEDIUM'):
                    return 'medium'
                elif sev == 'LOW':
                    return 'low'


        for sev_info in vuln_details.get('severity', []):
            score_str = sev_info.get('score', '')
            if score_str:
                try:

                    if score_str.replace('.', '').replace('-', '').isdigit():
                        score = float(score_str)
                    else:
                        continue
                    if score >= 9.0:
                        return 'critical'
                    elif score >= 7.0:
                        return 'high'
                    elif score >= 4.0:
                        return 'medium'
                    else:
                        return 'low'
                except:
                    pass

        return 'medium'

    def _extract_version(self, version_spec: str) -> str:
        """Extrait une version exacte d'un spécificateur de version.

        Args:
            version_spec: Spécificateur comme '==1.0.0', '^1.0.0', '>=1.0.0', '1.0.0'

        Returns:
            Version extraite ou chaîne vide
        """
        if not version_spec or version_spec == '*':
            return ''


        version = version_spec.strip()
        for prefix in ['==', '>=', '<=', '>', '<', '~=', '!=', '^', '~']:
            if version.startswith(prefix):
                version = version[len(prefix):]
                break


        match = re.match(r'^(\d+(?:\.\d+)*(?:-[a-zA-Z0-9.]+)?)', version)
        if match:
            return match.group(1)

        return version.strip()

    def _extract_all_packages_from_lock(self, lock_content: str) -> List[Dict[str, str]]:
        """Extrait TOUTES les dépendances avec leurs versions exactes du package-lock.json.

        Args:
            lock_content: Contenu du package-lock.json

        Returns:
            Liste de toutes les dépendances avec versions exactes
        """
        deps = []
        try:
            data = json.loads(lock_content)


            packages = data.get('packages', {})
            for path, info in packages.items():
                if not path:
                    continue

                parts = path.split('node_modules/')
                if len(parts) > 1:
                    name = parts[-1]
                    version = info.get('version', '')
                    if name and version:
                        deps.append({'name': name, 'version': version})


            if not deps:
                def extract_deps_v1(dependencies: dict, prefix: str = ''):
                    for name, info in dependencies.items():
                        if isinstance(info, dict):
                            version = info.get('version', '')
                            if version:
                                deps.append({'name': name, 'version': version})

                            if 'dependencies' in info:
                                extract_deps_v1(info['dependencies'])

                if 'dependencies' in data:
                    extract_deps_v1(data['dependencies'])

            self.logger.info(f"Extrait {len(deps)} packages du lockfile pour scan OSV")

        except json.JSONDecodeError as e:
            self.logger.warning(f"Erreur parsing package-lock.json: {e}")
        except Exception as e:
            self.logger.warning(f"Erreur extraction packages: {e}")

        return deps

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

    def _detect_content_type(self, content: str, language: str) -> str:
        """Auto-détecte le type de fichier à partir du contenu."""
        content_stripped = content.strip()


        if content_stripped.startswith('{'):
            try:
                data = json.loads(content_stripped)

                if 'lockfileVersion' in data or (isinstance(data.get('packages'), dict) and len(data.get('packages', {})) > 1):
                    return 'package-lock.json'

                if 'dependencies' in data or 'devDependencies' in data:
                    return 'package.json'
            except json.JSONDecodeError:
                pass


        if '[project]' in content or '[tool.' in content or 'dependencies = [' in content:
            return 'pyproject.toml'


        if language == 'python':
            return 'requirements.txt'
        return 'package.json'

    def _execute_core_logic(self, request: DependencyGuardRequest, **kwargs) -> DependencyGuardResponse:
        """Exécute la validation des dépendances."""
        issues = []
        deps = []


        content_type = self._detect_content_type(request.content, request.language)
        manifest_file = f"[content:{content_type}]"
        self.logger.info(f"Type de fichier détecté: {content_type}")


        if content_type == 'package-lock.json':
            deps = self._parse_package_lock_content(request.content)
        elif content_type == 'package.json':
            deps = self._parse_package_json_content(request.content)
        elif content_type == 'requirements.txt':
            deps = self._parse_requirements_txt_content(request.content)
        elif content_type == 'pyproject.toml':
            deps_match = re.search(r'dependencies\s*=\s*\[(.*?)\]', request.content, re.DOTALL)
            if deps_match:
                deps_str = deps_match.group(1)
                for line in deps_str.split('\n'):
                    line = line.strip().strip(',').strip('"\'')
                    if line:
                        match = re.match(r'^([a-zA-Z0-9_-]+)', line)
                        if match:
                            deps.append({'name': match.group(1), 'version': '*'})
        else:
            raise ToolValidationError(f"Type de fichier '{content_type}' non supporté")


        for dep in deps:
            name = dep['name']
            version = dep['version']


            if request.blocklist and name.lower() in [b.lower() for b in request.blocklist]:
                issues.append(DependencyIssue(
                    package=name,
                    version=version,
                    issue_type='blocked',
                    severity='high',
                    message=f"Package '{name}' est dans la liste noire",
                    recommendation=f"Supprimez ce package ou trouvez une alternative"
                ))


            if request.allowlist and name.lower() not in [a.lower() for a in request.allowlist]:
                issues.append(DependencyIssue(
                    package=name,
                    version=version,
                    issue_type='not_allowed',
                    severity='medium',
                    message=f"Package '{name}' n'est pas dans la liste blanche",
                    recommendation=f"Ajoutez '{name}' à l'allowlist ou supprimez-le"
                ))


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


        if request.check_vulnerabilities:
            ecosystem = 'PyPI' if request.language == 'python' else 'npm'


            all_deps = deps.copy()
            if content_type == 'package-lock.json' and request.language != 'python':
                all_deps = self._extract_all_packages_from_lock(request.content)

            vulns = self._check_osv_vulnerabilities(all_deps, ecosystem)

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


        severity_counts = {'critical': 0, 'high': 0, 'medium': 0, 'low': 0}
        for issue in issues:
            severity_counts[issue.severity] = severity_counts.get(issue.severity, 0) + 1


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
            summary=summary,
            total_dependencies=total_deps,
            vulnerabilities=total_issues,
            critical=severity_counts['critical'],
            high=severity_counts['high'],
            medium=severity_counts['medium'],
            low=severity_counts['low'],
            issues=issues
        )
