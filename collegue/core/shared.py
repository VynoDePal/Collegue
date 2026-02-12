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

import re

from . import validators as _validators


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
	return _validators.validate_fast_deep(v)

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


_CAMEL_1 = re.compile(r'(.)([A-Z][a-z]+)')
_CAMEL_2 = re.compile(r'([a-z0-9])([A-Z])')
_ACRONYM_BOUNDARY = re.compile(r'([A-Z]+)([A-Z][a-z])')


def to_snake_case(name: str) -> str:
	if not name:
		return name

	# Handle acronym boundaries: eventID -> event_ID, HTTPServer -> HTTP_Server
	name = _ACRONYM_BOUNDARY.sub(r'\1_\2', name)
	name = _CAMEL_1.sub(r'\1_\2', name)
	name = _CAMEL_2.sub(r'\1_\2', name)
	return name.lower()


def normalize_keys(obj: Any) -> Any:
	"""Normalise récursivement les clés dict (camelCase/PascalCase -> snake_case).

	- dict: transforme toutes les clés string
	- list/tuple: normalise chaque élément
	- autres: renvoie tel quel
	"""
	if isinstance(obj, dict):
		out: Dict[Any, Any] = {}
		for k, v in obj.items():
			key = to_snake_case(k) if isinstance(k, str) else k
			out[key] = normalize_keys(v)
		return out

	if isinstance(obj, list):
		return [normalize_keys(i) for i in obj]

	if isinstance(obj, tuple):
		return tuple(normalize_keys(i) for i in obj)

	return obj

_RULES_DIR = Path(__file__).parent.parent / 'tools' / 'rules'
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
	return _validators.normalize_language(language)


# Validators standardisés pour Pydantic field_validator
def validate_in_list(valid_values: List[str], value: str) -> str:
	"""Valide qu'une valeur est dans une liste de valeurs autorisées."""
	return _validators.validate_in_list(valid_values, value)


def validate_language(value: str, supported: Optional[List[str]] = None) -> str:
	"""Valide et normalise un langage de programmation."""
	return _validators.validate_language(value, supported)


def validate_confidence_mode(value: str) -> str:
	"""Valide un mode de confiance pour l'analyse d'impact."""
	return _validators.validate_confidence_mode(value)


def validate_refactoring_type(value: str) -> str:
	"""Valide un type de refactoring."""
	return _validators.validate_refactoring_type(value)


def validate_doc_format(value: str) -> str:
	"""Valide un format de documentation."""
	return _validators.validate_doc_format(value)


def validate_doc_style(value: str) -> str:
	"""Valide un style de documentation."""
	return _validators.validate_doc_style(value)


def validate_test_framework(value: str) -> str:
	"""Valide un framework de test."""
	return _validators.validate_test_framework(value)


def validate_k8s_command(value: str) -> str:
	"""Valide une commande Kubernetes."""
	return _validators.validate_k8s_command(value)


def validate_postgres_command(value: str) -> str:
	"""Valide une commande PostgreSQL."""
	return _validators.validate_postgres_command(value)


def validate_sentry_command(value: str) -> str:
	"""Valide une commande Sentry."""
	return _validators.validate_sentry_command(value)


def validate_github_command(value: str) -> str:
	"""Valide une commande GitHub."""
	return _validators.validate_github_command(value)


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
        return _validators.validate_in_list(valid_commands, v)
    return validator
