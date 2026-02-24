"""
Moteur d'analyse des dépendances pour l'outil Dependency Guard.

Contient la logique métier pure : parsing des fichiers, vérification
d'existence, détection de vulnérabilités.
"""
import re
import json
import urllib.request
import urllib.error
import urllib.parse
from typing import List, Dict, Any, Optional
from concurrent.futures import ThreadPoolExecutor
from .config import (
    REGISTRY_URLS, OSV_BATCH_URL, OSV_VULN_URL,
    OSV_CHUNK_SIZE, LANGUAGE_ECOSYSTEM
)
from .models import DependencyIssue
from ..base import ToolValidationError


class DependencyAnalysisEngine:
    """Moteur d'analyse des dépendances."""
    
    def __init__(self, logger=None):
        self.logger = logger
    
    # ==================== Parsing des fichiers ====================
    
    def parse_requirements_txt(self, content: str) -> List[Dict[str, str]]:
        """Parse le contenu d'un requirements.txt."""
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
    
    def parse_package_json(self, content: str) -> List[Dict[str, str]]:
        """Parse le contenu d'un package.json."""
        dependencies = []
        try:
            data = json.loads(content)
            all_deps = {**data.get('dependencies', {}), **data.get('devDependencies', {})}
            for name, version in all_deps.items():
                dependencies.append({'name': name, 'version': version})
        except json.JSONDecodeError as e:
            raise ToolValidationError(f"Erreur de parsing JSON: {e}")
        return dependencies
    
    def parse_package_lock(self, content: str) -> List[Dict[str, str]]:
        """Parse le contenu d'un package-lock.json."""
        deps = []
        try:
            data = json.loads(content)
            root_pkg = data.get('packages', {}).get('', {})
            dependencies = {**root_pkg.get('dependencies', {}), **root_pkg.get('devDependencies', {})}
            if not dependencies:
                dependencies = data.get('dependencies', {})
            for name, info in dependencies.items():
                version = info if isinstance(info, str) else info.get('version', '*')
                deps.append({'name': name, 'version': version})
        except json.JSONDecodeError as e:
            raise ToolValidationError(f"Erreur de parsing package-lock.json: {e}")
        return deps
    
    def parse_pyproject_toml(self, content: str) -> List[Dict[str, str]]:
        """Parse le contenu d'un pyproject.toml."""
        dependencies = []
        try:
            import tomllib
            with __import__('io').BytesIO(content.encode('utf-8')) as f:
                data = tomllib.load(f)
        except ImportError:
            deps_match = re.search(r'dependencies\s*=\s*\[(.*?)\]', content, re.DOTALL)
            if deps_match:
                deps_str = deps_match.group(1)
                for line in deps_str.split('\n'):
                    line = line.strip().strip(',').strip('"\'')
                    if line:
                        match = re.match(r'^([a-zA-Z0-9_-]+)', line)
                        if match:
                            dependencies.append({'name': match.group(1), 'version': '*'})
            return dependencies
        
        for dep in data.get('project', {}).get('dependencies', []):
            match = re.match(r'^([a-zA-Z0-9_-]+)', dep)
            if match:
                dependencies.append({'name': match.group(1), 'version': '*'})
        return dependencies
    
    def parse_composer_json(self, content: str) -> List[Dict[str, str]]:
        """Parse le contenu d'un composer.json."""
        dependencies = []
        try:
            data = json.loads(content)
            all_deps = {**data.get('require', {}), **data.get('require-dev', {})}
            for name, version in all_deps.items():
                if name == 'php' or name.startswith('ext-') or name.startswith('lib-'):
                    continue
                dependencies.append({'name': name, 'version': version})
        except json.JSONDecodeError as e:
            raise ToolValidationError(f"Erreur de parsing composer.json: {e}")
        return dependencies
    
    def parse_composer_lock(self, content: str) -> List[Dict[str, str]]:
        """Parse le contenu d'un composer.lock."""
        dependencies = []
        try:
            data = json.loads(content)
            for pkg in data.get('packages', []) + data.get('packages-dev', []):
                dependencies.append({'name': pkg.get('name'), 'version': pkg.get('version')})
        except json.JSONDecodeError as e:
            raise ToolValidationError(f"Erreur de parsing composer.lock: {e}")
        return dependencies
    
    def detect_content_type(self, content: str, language: str) -> str:
        """Détecte le type de fichier de dépendances."""
        content_stripped = content.strip()
        
        if content_stripped.startswith('{'):
            try:
                data = json.loads(content_stripped)
                if 'lockfileVersion' in data:
                    return 'package-lock.json'
                if 'dependencies' in data or 'devDependencies' in data:
                    return 'package.json'
                if 'require' in data and 'name' in data:
                    return 'composer.json'
                if 'packages' in data and 'packages-dev' in data:
                    return 'composer.lock'
            except json.JSONDecodeError:
                pass
        
        if '[project]' in content or '[tool.' in content:
            return 'pyproject.toml'
        
        return 'requirements.txt' if language == 'python' else 'package.json'
    
    # ==================== Vérification d'existence ====================
    
    def check_pypi_existence(self, package_name: str) -> Dict[str, Any]:
        """Vérifie si un package existe sur PyPI."""
        try:
            url = REGISTRY_URLS['pypi'].format(package=package_name)
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=10) as response:
                if response.status == 200:
                    data = json.loads(response.read().decode('utf-8'))
                    return {'exists': True, 'latest_version': data.get('info', {}).get('version')}
            return {'exists': False}
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return {'exists': False}
            return {'exists': None, 'error': str(e)}
        except Exception as e:
            return {'exists': None, 'error': str(e)}
    
    def check_npm_existence(self, package_name: str) -> Dict[str, Any]:
        """Vérifie si un package existe sur npm."""
        try:
            safe_name = urllib.parse.quote(package_name, safe='@')
            url = REGISTRY_URLS['npm'].format(package=safe_name)
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=10) as response:
                if response.status == 200:
                    data = json.loads(response.read().decode('utf-8'))
                    latest = data.get('dist-tags', {}).get('latest')
                    return {'exists': True, 'latest_version': latest}
            return {'exists': False}
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return {'exists': False}
            return {'exists': None, 'error': str(e)}
        except Exception as e:
            return {'exists': None, 'error': str(e)}
    
    def check_packagist_existence(self, package_name: str) -> Dict[str, Any]:
        """Vérifie si un package existe sur Packagist."""
        try:
            url = REGISTRY_URLS['packagist'].format(package=package_name)
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=10) as response:
                if response.status == 200:
                    data = json.loads(response.read().decode('utf-8'))
                    versions = list(data.get('package', {}).get('versions', {}).keys())
                    latest = next((v for v in versions if 'dev' not in v), versions[0] if versions else None)
                    return {'exists': True, 'latest_version': latest}
            return {'exists': False}
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return {'exists': False}
            return {'exists': None, 'error': f"HTTP {e.code}"}
        except Exception as e:
            return {'exists': None, 'error': str(e)}
    
    def check_package_existence(self, package_name: str, language: str) -> Dict[str, Any]:
        """Vérifie l'existence d'un package selon le langage."""
        if language == 'python':
            return self.check_pypi_existence(package_name)
        elif language == 'php':
            return self.check_packagist_existence(package_name)
        else:
            return self.check_npm_existence(package_name)
    
    # ==================== Vérification des vulnérabilités ====================
    
    def extract_version(self, version_spec: str) -> str:
        """Extrait la version nette d'une spécification."""
        if not version_spec or version_spec == '*':
            return ''
        version = version_spec.strip()
        for prefix in ['==', '>=', '<=', '>', '<', '~=', '!=', '^', '~']:
            if version.startswith(prefix):
                version = version[len(prefix):]
                break
        match = re.match(r'^(\d+(?:\.\d+)*(?:-[a-zA-Z0-9.]+)?)', version)
        return match.group(1) if match else version.strip()
    
    def extract_osv_severity(self, vuln_details: dict) -> str:
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
        
        return 'medium'
    
    def check_osv_vulnerabilities(self, deps: List[Dict[str, str]], ecosystem: str) -> List[Dict[str, Any]]:
        """Vérifie les vulnérabilités via l'API OSV."""
        vulnerabilities = []
        queries = []
        dep_map = {}
        
        for i, dep in enumerate(deps):
            name = dep['name']
            version = self.extract_version(dep.get('version', '*'))
            if not version or version == '*':
                continue
            queries.append({
                "package": {"name": name, "ecosystem": ecosystem},
                "version": version
            })
            dep_map[len(queries) - 1] = {'name': name, 'version': version}
        
        if not queries:
            return vulnerabilities
        
        if self.logger:
            self.logger.info(f"Vérification OSV batch pour {len(queries)} packages ({ecosystem})...")
        
        try:
            vuln_to_packages = {}
            
            for chunk_start in range(0, len(queries), OSV_CHUNK_SIZE):
                chunk_queries = queries[chunk_start:chunk_start + OSV_CHUNK_SIZE]
                batch_data = {"queries": chunk_queries}
                
                req = urllib.request.Request(
                    OSV_BATCH_URL,
                    data=json.dumps(batch_data).encode('utf-8'),
                    headers={'Content-Type': 'application/json'}
                )
                
                with urllib.request.urlopen(req, timeout=60) as response:
                    result = json.loads(response.read().decode('utf-8'))
                
                for chunk_idx, result_item in enumerate(result.get('results', [])):
                    global_idx = chunk_start + chunk_idx
                    if global_idx not in dep_map:
                        continue
                    pkg_info = dep_map[global_idx]
                    
                    for vuln in result_item.get('vulns', []):
                        vuln_id = vuln.get('id', '')
                        if vuln_id:
                            if vuln_id not in vuln_to_packages:
                                vuln_to_packages[vuln_id] = []
                            vuln_to_packages[vuln_id].append(pkg_info)
            
            if self.logger:
                self.logger.info(f"Récupération des détails pour {len(vuln_to_packages)} vulnérabilités...")
            
            for vuln_id, packages in vuln_to_packages.items():
                try:
                    vuln_req = urllib.request.Request(f"{OSV_VULN_URL}{vuln_id}")
                    with urllib.request.urlopen(vuln_req, timeout=10) as resp:
                        vuln_details = json.loads(resp.read().decode('utf-8'))
                    
                    severity = self.extract_osv_severity(vuln_details)
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
                    if self.logger:
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
        
        except Exception as e:
            if self.logger:
                self.logger.warning(f"OSV check error: {e}")
        
        if self.logger:
            self.logger.info(f"OSV: {len(vulnerabilities)} vulnérabilité(s) détectée(s)")
        
        return vulnerabilities
