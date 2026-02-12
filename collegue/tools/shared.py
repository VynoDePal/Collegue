"""
Shared utilities for Collegue tools.

This module contains common models and functions used across multiple tools
to avoid duplication and ensure consistency.
"""
import json
import asyncio
import concurrent.futures
from pathlib import Path
from typing import Any, Dict, Optional, List

import yaml
from pydantic import BaseModel, Field


class ConsistencyIssue(BaseModel):
	"""Issue de cohérence détectée dans le code.

	Modèle Pydantic partagé entre repo_consistency_check et les analyzers.
	"""
	kind: str = Field(..., description="Type: unused_import, unused_var, dead_code, duplication, unresolved_symbol")
	severity: str = Field(..., description="Sévérité: info, low, medium, high")
	path: str = Field(..., description="Chemin du fichier")
	line: Optional[int] = Field(None, description="Numéro de ligne")
	column: Optional[int] = Field(None, description="Numéro de colonne")
	message: str = Field(..., description="Description du problème")
	confidence: int = Field(..., description="Confiance 0-100")
	suggested_fix: Optional[str] = Field(None, description="Suggestion de correction")
	engine: str = Field("embedded-rules", description="Moteur utilisé")


class FileInput(BaseModel):
    """Un fichier avec son chemin et contenu."""
    path: str = Field(..., description="Chemin relatif du fichier")
    content: str = Field(..., description="Contenu du fichier")
    language: Optional[str] = Field(None, description="Langage (auto-détecté si absent)")

FileContent = FileInput

def validate_fast_deep(v: str) -> str:
    valid = ['fast', 'deep']
    if v not in valid:
        raise ValueError(f"Valeur '{v}' invalide. Utilisez: {', '.join(valid)}")
    return v

def detect_language_from_extension(filepath: str) -> str:

    ext_map = {

        '.py': 'python',
        '.pyi': 'python',

        '.ts': 'typescript',
        '.tsx': 'typescript',
        '.js': 'javascript',
        '.jsx': 'javascript',
        '.mjs': 'javascript',
        '.cjs': 'javascript',

        '.java': 'java',

        '.cs': 'c#',
        '.csx': 'c#',

        '.go': 'go',

        '.rs': 'rust',

        '.rb': 'ruby',

        '.php': 'php',

        '.c': 'c',
        '.cpp': 'cpp',
        '.cc': 'cpp',
        '.h': 'c',
        '.hpp': 'cpp',

        '.swift': 'swift',

        '.kt': 'kotlin',
        '.kts': 'kotlin',

        '.scala': 'scala',
        '.sc': 'scala',

        '.sh': 'shell',
        '.bash': 'shell',
        '.zsh': 'shell',

        '.html': 'html',
        '.htm': 'html',
        '.css': 'css',
        '.scss': 'scss',
        '.sass': 'sass',
        '.less': 'less',

        '.json': 'json',
        '.yaml': 'yaml',
        '.yml': 'yaml',
        '.toml': 'toml',
        '.xml': 'xml',

        '.md': 'markdown',
        '.markdown': 'markdown',

        '.sql': 'sql',

        '.tf': 'terraform',
        '.tfvars': 'terraform',

        'dockerfile': 'dockerfile',
        '.dockerfile': 'dockerfile',

        '.k8s.yaml': 'kubernetes',
    }

    filepath_lower = filepath.lower()

    if 'dockerfile' in filepath_lower:
        return 'dockerfile'

    if '.' in filepath:
        ext = '.' + filepath.split('.')[-1].lower()
        return ext_map.get(ext, 'unknown')

    return 'unknown'

def parse_llm_json_response(raw: str) -> Dict[str, Any]:
    clean = raw.strip()
    if clean.startswith('```'):
        clean = clean.split('\n', 1)[1]
    if clean.endswith('```'):
        clean = clean.rsplit('```', 1)[0]
    clean = clean.strip()
    return json.loads(clean)

_RULES_DIR = Path(__file__).parent / 'rules'
_rules_cache: Dict[str, Dict[str, list]] = {}

def load_rules(rule_file: str) -> Dict[str, list]:
    if rule_file in _rules_cache:
        return _rules_cache[rule_file]
    filepath = _RULES_DIR / rule_file
    with open(filepath, 'r', encoding='utf-8') as f:
        data = yaml.safe_load(f)
    _rules_cache[rule_file] = data
    return data

def run_async_from_sync(coro, timeout: int = 30):
    try:
        asyncio.get_running_loop()
        with concurrent.futures.ThreadPoolExecutor() as executor:
            future = executor.submit(asyncio.run, coro)
            return future.result(timeout=timeout)
    except RuntimeError:
        return asyncio.run(coro)

def aggregate_severities(items: List[Any], severity_attr: str = 'severity', default_levels: Optional[List[str]] = None) -> Dict[str, int]:
    """Compte les occurrences par niveau de sévérité.

    Utilisé par dependency_guard, iac_guardrails_scan, secret_scan,
    repo_consistency_check pour agréger les résultats d'analyse.

    Args:
        items: Liste d'objets ayant un attribut sévérité
        severity_attr: Nom de l'attribut de sévérité (défaut: 'severity')
        default_levels: Liste des niveaux de sévérité attendus (défaut: critical, high, medium, low)

    Returns:
        Dict avec les comptes pour chaque niveau de sévérité
    """
    levels = default_levels or ['critical', 'high', 'medium', 'low']
    counts = {level: 0 for level in levels}
    for item in items:
        sev = getattr(item, severity_attr, 'low').lower()
        if sev in counts:
            counts[sev] += 1
    return counts


def normalize_language(language: str) -> str:

    normalized = language.strip().lower()

    aliases = {
        'js': 'javascript',
        'ts': 'typescript',
        'py': 'python',
        'rb': 'ruby',
        'golang': 'go',
        'c#': 'csharp',
        'c-sharp': 'csharp',
        'csharp': 'csharp',
        'f#': 'fsharp',
        'f-sharp': 'fsharp',
        'html': 'html',
        'htm': 'html',
    }

    return aliases.get(normalized, normalized)


# Validators standardisés pour Pydantic field_validator
def validate_in_list(valid_values: List[str], value: str) -> str:
    """Valide qu'une valeur est dans une liste de valeurs autorisées."""
    if value not in valid_values:
        raise ValueError(f"Valeur '{value}' invalide. Utilisez: {', '.join(valid_values)}")
    return value


def validate_language(value: str, supported: Optional[List[str]] = None) -> str:
    """Valide et normalise un langage de programmation."""
    normalized = normalize_language(value)
    if supported and normalized not in supported:
        raise ValueError(f"Langage '{value}' non supporté. Utilisez: {', '.join(supported)}")
    return normalized


def validate_confidence_mode(value: str) -> str:
    """Valide un mode de confiance pour l'analyse d'impact."""
    return validate_in_list(['conservative', 'balanced', 'aggressive'], value)


def validate_refactoring_type(value: str) -> str:
    """Valide un type de refactoring."""
    return validate_in_list(['rename', 'extract', 'simplify', 'optimize', 'clean', 'modernize', 'security'], value)


def validate_doc_format(value: str) -> str:
    """Valide un format de documentation."""
    return validate_in_list(['markdown', 'rst', 'html', 'docstring', 'json'], value)


def validate_doc_style(value: str) -> str:
    """Valide un style de documentation."""
    return validate_in_list(['standard', 'detailed', 'minimal', 'api'], value)


def validate_test_framework(value: str) -> str:
    """Valide un framework de test."""
    return validate_in_list(['pytest', 'jest', 'mocha', 'unittest', 'vitest'], value)


def validate_k8s_command(value: str) -> str:
    """Valide une commande Kubernetes."""
    valid_commands = [
        'list_pods', 'get_pod', 'pod_logs', 'list_deployments',
        'get_deployment', 'list_services', 'list_namespaces',
        'list_nodes', 'describe_resource', 'list_configmaps', 'list_secrets'
    ]
    return validate_in_list(valid_commands, value)


def validate_postgres_command(value: str) -> str:
    """Valide une commande PostgreSQL."""
    return validate_in_list(['list_schemas', 'list_tables', 'describe_table', 'query'], value)


def validate_sentry_command(value: str) -> str:
    """Valide une commande Sentry."""
    return validate_in_list([
        'list_projects', 'list_issues', 'get_issue',
        'issue_events', 'project_stats', 'list_releases'
    ], value)


def validate_github_command(value: str) -> str:
    """Valide une commande GitHub."""
    return validate_in_list([
        'list_repos', 'get_repo', 'get_file', 'list_prs', 'get_pr', 'create_pr',
        'list_issues', 'get_issue', 'create_issue', 'pr_files', 'pr_comments',
        'repo_branches', 'create_branch', 'update_file',
        'repo_commits', 'search_code', 'list_workflows', 'workflow_runs'
    ], value)


def create_command_validator(valid_commands: List[str], field_name: str = 'command'):
    """Crée un validateur Pydantic pour un champ commande.
    
    Usage:
        class MyRequest(BaseModel):
            command: str
            
            @field_validator('command')
            @classmethod
            def validate_command(cls, v: str) -> str:
                return validate_in_list(['cmd1', 'cmd2'], v)
    
    Args:
        valid_commands: Liste des commandes valides
        field_name: Nom du champ à valider (défaut: 'command')
    
    Returns:
        Fonction de validation compatible avec Pydantic field_validator
    """
    def validator(cls, v: str) -> str:
        return validate_in_list(valid_commands, v)
    return validator
