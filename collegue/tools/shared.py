"""
Shared utilities for Collegue tools.

This module contains common models and functions used across multiple tools
to avoid duplication and ensure consistency.
"""
from typing import Optional
from pydantic import BaseModel, Field


class FileInput(BaseModel):

    path: str = Field(..., description="Chemin relatif du fichier")
    content: str = Field(..., description="Contenu du fichier")
    language: Optional[str] = Field(None, description="Langage du fichier (auto-détecté si non fourni)")


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
