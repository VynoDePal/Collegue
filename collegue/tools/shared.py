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
