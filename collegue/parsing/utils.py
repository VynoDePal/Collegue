import os
from typing import Optional, Dict, List, Tuple
from .base import ParseResult, Import
from .javascript import JSParser
from .python import PythonParser


def detect_language(content: str, filename: Optional[str] = None) -> str:
    """Détecte le langage de programmation à partir du contenu et/ou du nom de fichier."""
    if filename:
        ext = os.path.splitext(filename)[1].lower()
        if ext == '.py':
            return 'python'
        elif ext in ('.ts', '.tsx'):
            return 'typescript'
        elif ext in ('.js', '.jsx', '.mjs', '.cjs'):
            return 'javascript'

    python_score = 0
    js_score = 0
    ts_score = 0

    if "def " in content:
        python_score += 2
    if "class " in content and ":" in content:
        python_score += 2
    if "import " in content or "from " in content:
        python_score += 2
    if ":" in content and "#" in content:
        python_score += 1
    if "self." in content:
        python_score += 1

    if "function " in content or "=>" in content:
        js_score += 2
    if "const " in content or "let " in content or "var " in content:
        js_score += 2
    if "require(" in content:
        js_score += 2
    if "{" in content and "}" in content:
        js_score += 1

    if ": string" in content or ": number" in content or ": boolean" in content:
        ts_score += 3
    if "interface " in content:
        ts_score += 3
    if "type " in content and "=" in content:
        ts_score += 2

    ts_score += js_score

    scores = {
        'python': python_score,
        'typescript': ts_score,
        'javascript': js_score,
    }

    best = max(scores, key=scores.get)
    if scores[best] == 0:
        return 'any'
    return best


def parse_file(content: str, filename: Optional[str] = None) -> ParseResult:
    """Parse un fichier en détectant automatiquement le langage."""
    language = detect_language(content, filename)

    if language == 'python':
        parser = PythonParser(content, filename)
    elif language in ('javascript', 'typescript'):
        parser = JSParser(content, filename)
    else:
        return ParseResult(language=language, raw=content)

    return parser.parse()


def resolve_relative_import(
    source: str,
    current_file: str,
    file_modules: Dict[str, str],
) -> Optional[str]:
    """Résout un import relatif vers un chemin absolu.

    Supporte les chemins multi-niveaux (../../foo/bar).
    """
    if not source.startswith('.'):
        return None

    current_dir = os.path.dirname(current_file)
    resolved = os.path.normpath(os.path.join(current_dir, source))

    for path in file_modules:
        normalized = os.path.normpath(path)
        base_no_ext = os.path.splitext(normalized)[0]
        if normalized == resolved or base_no_ext == resolved:
            return path

    extensions = ['.py', '.js', '.ts', '.jsx', '.tsx', '.mjs', '.cjs']
    for ext in extensions:
        candidate = resolved + ext
        for path in file_modules:
            if os.path.normpath(path) == candidate:
                return path

    index_names = ['index.js', 'index.ts', 'index.tsx', '__init__.py']
    for idx in index_names:
        candidate = os.path.join(resolved, idx)
        for path in file_modules:
            if os.path.normpath(path) == candidate:
                return path

    return None


def resolve_module_to_file(
    module: str,
    file_modules: Dict[str, str],
    current_file: Optional[str] = None,
) -> Optional[str]:
    """Résout un nom de module vers un chemin de fichier."""
    if module.startswith('.'):
        if current_file:
            return resolve_relative_import(module, current_file, file_modules)
        return None

    module_path = module.replace('.', os.sep)

    for path in file_modules:
        normalized = os.path.normpath(path)
        base_no_ext = os.path.splitext(normalized)[0]
        if base_no_ext.endswith(module_path):
            return path

    return None


def get_unused_imports(parse_result: ParseResult) -> List[Import]:
    """Détecte les imports non utilisés dans un fichier."""
    if not parse_result.imports:
        return []

    used_names = set(name for _, name in parse_result.identifiers)
    unused = []

    for imp in parse_result.imports:
        all_unused = True
        for name, alias in imp.names:
            used_name = alias if alias else name
            if used_name in used_names:
                all_unused = False
                break

        if all_unused and imp.names:
            unused.append(imp)

    return unused


def get_unused_declarations(parse_result: ParseResult) -> List[str]:
    """Détecte les déclarations non utilisées dans un fichier."""
    if not parse_result.declarations:
        return []

    used_names = set(name for _, name in parse_result.identifiers)

    unused = []
    for name, decl in parse_result.declarations.items():
        if name not in used_names:
            unused.append(name)

    return unused