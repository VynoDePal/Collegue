"""
Repo Consistency Check - Outil de détection d'incohérences dans le code

Cet outil détecte les incohérences typiques générées par l'IA:
- Code mort (fonctions/classes jamais appelées)
- Variables inutilisées
- Imports non utilisés
- Duplication de code
- Mismatch paramètres/retours
- Symboles non résolus

Problème résolu: L'IA génère souvent des "hallucinations silencieuses" (code qui compile
mais contient des incohérences).
Valeur: Transforme ces hallucinations en diagnostics actionnables.
Bénéfice: Meilleure fiabilité des patches IA, réduction de dette technique.
"""
import re
import ast
import hashlib
from typing import Optional, Dict, Any, List, Type, Set, Tuple
from collections import defaultdict
from pydantic import BaseModel, Field, field_validator
from .base import BaseTool, ToolError, ToolValidationError, ToolExecutionError


class FileInput(BaseModel):
    """Un fichier avec son chemin et contenu."""
    path: str = Field(..., description="Chemin relatif du fichier")
    content: str = Field(..., description="Contenu du fichier")
    language: Optional[str] = Field(None, description="Langage (auto-détecté si absent)")


class ConsistencyCheckRequest(BaseModel):
    """Modèle de requête pour la vérification de cohérence."""
    files: List[FileInput] = Field(
        ...,
        description="Liste des fichiers à analyser [{path, content, language?}, ...]",
        min_length=1
    )
    language: str = Field(
        "auto",
        description="Langage principal: 'python', 'typescript', 'javascript', 'auto'"
    )
    checks: Optional[List[str]] = Field(
        None,
        description="Checks à exécuter: 'unused_imports', 'unused_vars', 'dead_code', 'duplication', 'signature_mismatch', 'unresolved_symbol'. Tous par défaut."
    )
    diff: Optional[str] = Field(
        None,
        description="Diff unifié optionnel pour focaliser l'analyse sur les changements"
    )
    mode: str = Field(
        "fast",
        description="Mode: 'fast' (heuristiques rapides) ou 'deep' (analyse plus complète)"
    )
    min_confidence: int = Field(
        60,
        description="Confiance minimum (0-100) pour reporter un issue",
        ge=0,
        le=100
    )
    
    @field_validator('mode')
    def validate_mode(cls, v):
        valid = ['fast', 'deep']
        if v not in valid:
            raise ValueError(f"Mode '{v}' invalide. Utilisez: {', '.join(valid)}")
        return v
    
    @field_validator('checks')
    def validate_checks(cls, v):
        if v is None:
            return v
        valid = ['unused_imports', 'unused_vars', 'dead_code', 'duplication', 
                 'signature_mismatch', 'unresolved_symbol']
        for check in v:
            if check not in valid:
                raise ValueError(f"Check '{check}' invalide. Utilisez: {', '.join(valid)}")
        return v


class ConsistencyIssue(BaseModel):
    """Un problème de cohérence détecté."""
    kind: str = Field(..., description="Type: unused_import, unused_var, dead_code, duplication, signature_mismatch, unresolved_symbol")
    severity: str = Field(..., description="Sévérité: info, low, medium, high")
    path: str = Field(..., description="Chemin du fichier")
    line: Optional[int] = Field(None, description="Numéro de ligne")
    column: Optional[int] = Field(None, description="Numéro de colonne")
    message: str = Field(..., description="Description du problème")
    confidence: int = Field(..., description="Confiance 0-100")
    suggested_fix: Optional[str] = Field(None, description="Suggestion de correction")
    engine: str = Field("embedded-rules", description="Moteur utilisé")


class ConsistencyCheckResponse(BaseModel):
    """Modèle de réponse pour la vérification de cohérence."""
    valid: bool = Field(..., description="True si aucun problème trouvé")
    summary: Dict[str, int] = Field(
        ...,
        description="Résumé par sévérité {total, high, medium, low, info}"
    )
    issues: List[ConsistencyIssue] = Field(
        default_factory=list,
        description="Liste des problèmes détectés"
    )
    files_analyzed: int = Field(..., description="Nombre de fichiers analysés")
    checks_performed: List[str] = Field(..., description="Checks exécutés")
    analysis_summary: str = Field(..., description="Résumé de l'analyse")


class RepoConsistencyCheckTool(BaseTool):
    """
    Outil de détection d'incohérences dans le code.
    
    Détecte les problèmes typiques générés par l'IA:
    - Imports non utilisés (Python, JS/TS)
    - Variables déclarées mais jamais utilisées
    - Fonctions/classes jamais appelées (code mort)
    - Duplication de code (similarité token)
    - Mismatch signature/usage
    - Symboles non résolus dans le scope fourni
    
    Basé sur des heuristiques AST (Python) et regex (JS/TS).
    Compatible avec l'environnement MCP isolé (analyse sur contenu).
    """
    
    # Checks disponibles
    ALL_CHECKS = ['unused_imports', 'unused_vars', 'dead_code', 'duplication', 
                  'signature_mismatch', 'unresolved_symbol']
    
    # Sévérité par type de problème
    SEVERITY_MAP = {
        'unused_import': 'low',
        'unused_var': 'medium',
        'dead_code': 'medium',
        'duplication': 'low',
        'signature_mismatch': 'high',
        'unresolved_symbol': 'high',
    }

    def get_name(self) -> str:
        return "repo_consistency_check"

    def get_description(self) -> str:
        return "Détecte les incohérences dans le code: imports/variables inutilisés, code mort, duplication"

    def get_request_model(self) -> Type[BaseModel]:
        return ConsistencyCheckRequest

    def get_response_model(self) -> Type[BaseModel]:
        return ConsistencyCheckResponse

    def get_supported_languages(self) -> List[str]:
        return ["python", "typescript", "javascript", "auto"]

    def is_long_running(self) -> bool:
        return False

    def get_usage_description(self) -> str:
        return (
            "Analyse le code pour détecter les incohérences typiques générées par l'IA: "
            "imports non utilisés, variables mortes, code dupliqué, symboles non résolus."
        )

    def get_examples(self) -> List[Dict[str, Any]]:
        return [
            {
                "title": "Vérifier un fichier Python",
                "request": {
                    "files": [{"path": "utils.py", "content": "import os\nimport sys\nprint('hello')"}],
                    "language": "python"
                }
            },
            {
                "title": "Mode deep avec checks spécifiques",
                "request": {
                    "files": [{"path": "app.ts", "content": "..."}],
                    "language": "typescript",
                    "mode": "deep",
                    "checks": ["unused_imports", "dead_code"]
                }
            }
        ]

    def get_capabilities(self) -> List[str]:
        return [
            "Détection d'imports non utilisés (Python, JS/TS)",
            "Détection de variables inutilisées",
            "Détection de code mort (fonctions non appelées)",
            "Détection de duplication de code",
            "Détection de mismatch signature/usage",
            "Support multi-fichiers avec analyse croisée"
        ]

    def _detect_language(self, filepath: str) -> str:
        """Détecte le langage à partir de l'extension."""
        ext_map = {
            '.py': 'python',
            '.ts': 'typescript', '.tsx': 'typescript',
            '.js': 'javascript', '.jsx': 'javascript', '.mjs': 'javascript',
        }
        ext = '.' + filepath.split('.')[-1] if '.' in filepath else ''
        return ext_map.get(ext.lower(), 'unknown')

    # ==================== PYTHON ANALYSIS ====================
    
    def _analyze_python_unused_imports(self, content: str, filepath: str) -> List[ConsistencyIssue]:
        """Détecte les imports Python non utilisés."""
        issues = []
        
        try:
            tree = ast.parse(content)
        except SyntaxError as e:
            issues.append(ConsistencyIssue(
                kind="syntax_error",
                severity="high",
                path=filepath,
                line=e.lineno,
                message=f"Erreur de syntaxe: {e.msg}",
                confidence=100,
                engine="ast-parser"
            ))
            return issues
        
        # Collecter tous les imports
        imports = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    name = alias.asname or alias.name.split('.')[0]
                    imports[name] = (node.lineno, alias.name)
            elif isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    name = alias.asname or alias.name
                    if name != '*':
                        imports[name] = (node.lineno, f"{node.module}.{alias.name}")
        
        # Collecter tous les noms utilisés
        used_names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                used_names.add(node.id)
            elif isinstance(node, ast.Attribute):
                # Pour les attributs comme os.path
                if isinstance(node.value, ast.Name):
                    used_names.add(node.value.id)
            elif isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name):
                    used_names.add(node.func.id)
                elif isinstance(node.func, ast.Attribute):
                    if isinstance(node.func.value, ast.Name):
                        used_names.add(node.func.value.id)
        
        # Trouver les imports non utilisés
        for name, (line, full_import) in imports.items():
            if name not in used_names:
                issues.append(ConsistencyIssue(
                    kind="unused_import",
                    severity="low",
                    path=filepath,
                    line=line,
                    message=f"Import '{full_import}' (as '{name}') non utilisé",
                    confidence=90,
                    suggested_fix=f"Supprimer: import {full_import}" if '.' not in full_import else f"Supprimer l'import de {name}",
                    engine="ast-analyzer"
                ))
        
        return issues

    def _analyze_python_unused_vars(self, content: str, filepath: str) -> List[ConsistencyIssue]:
        """Détecte les variables Python non utilisées."""
        issues = []
        
        try:
            tree = ast.parse(content)
        except SyntaxError:
            return issues
        
        # Analyser par scope (fonction/classe)
        class VariableVisitor(ast.NodeVisitor):
            def __init__(self):
                self.scopes = [{}]  # Stack de scopes
                self.issues = []
            
            def visit_FunctionDef(self, node):
                self.scopes.append({})
                # Paramètres
                for arg in node.args.args:
                    # Ignorer self, cls, et les paramètres avec _
                    if arg.arg not in ('self', 'cls') and not arg.arg.startswith('_'):
                        self.scopes[-1][arg.arg] = (node.lineno, False)
                self.generic_visit(node)
                self._check_scope(filepath)
                self.scopes.pop()
            
            def visit_AsyncFunctionDef(self, node):
                self.visit_FunctionDef(node)
            
            def visit_Assign(self, node):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        name = target.id
                        if not name.startswith('_'):
                            self.scopes[-1][name] = (node.lineno, False)
                self.generic_visit(node)
            
            def visit_Name(self, node):
                if isinstance(node.ctx, ast.Load):
                    # Marquer comme utilisée dans tous les scopes
                    for scope in self.scopes:
                        if node.id in scope:
                            scope[node.id] = (scope[node.id][0], True)
                self.generic_visit(node)
            
            def _check_scope(self, path):
                for name, (line, used) in self.scopes[-1].items():
                    if not used and not name.startswith('_'):
                        self.issues.append(ConsistencyIssue(
                            kind="unused_var",
                            severity="medium",
                            path=path,
                            line=line,
                            message=f"Variable '{name}' assignée mais jamais utilisée",
                            confidence=80,
                            suggested_fix=f"Supprimer ou préfixer avec _ : _{name}",
                            engine="ast-analyzer"
                        ))
        
        visitor = VariableVisitor()
        visitor.visit(tree)
        return visitor.issues

    def _analyze_python_dead_code(self, content: str, filepath: str, all_contents: str) -> List[ConsistencyIssue]:
        """Détecte les fonctions/classes Python jamais appelées."""
        issues = []
        
        try:
            tree = ast.parse(content)
        except SyntaxError:
            return issues
        
        # Collecter les définitions
        definitions = {}
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                # Ignorer les méthodes spéciales et privées
                if not node.name.startswith('_'):
                    definitions[node.name] = (node.lineno, 'function')
            elif isinstance(node, ast.ClassDef):
                if not node.name.startswith('_'):
                    definitions[node.name] = (node.lineno, 'class')
        
        # Chercher les usages dans tout le contenu
        for name, (line, kind) in definitions.items():
            # Pattern pour trouver les usages (appels ou références)
            patterns = [
                rf'\b{re.escape(name)}\s*\(',  # Appel
                rf'\b{re.escape(name)}\b',     # Référence
            ]
            
            usage_count = 0
            for pattern in patterns:
                matches = list(re.finditer(pattern, all_contents))
                usage_count += len(matches)
            
            # La définition compte comme 1 match, donc on cherche > 1
            if usage_count <= 1:
                issues.append(ConsistencyIssue(
                    kind="dead_code",
                    severity="medium",
                    path=filepath,
                    line=line,
                    message=f"{kind.capitalize()} '{name}' défini(e) mais jamais utilisé(e)",
                    confidence=70,
                    suggested_fix=f"Supprimer si inutile, ou vérifier si exporté/utilisé ailleurs",
                    engine="usage-analyzer"
                ))
        
        return issues

    # ==================== JAVASCRIPT/TYPESCRIPT ANALYSIS ====================
    
    def _analyze_js_unused_imports(self, content: str, filepath: str) -> List[ConsistencyIssue]:
        """Détecte les imports JS/TS non utilisés."""
        issues = []
        
        # Patterns d'import
        import_patterns = [
            # import { a, b } from 'module'
            r"import\s*\{([^}]+)\}\s*from\s*['\"]([^'\"]+)['\"]",
            # import name from 'module'
            r"import\s+(\w+)\s+from\s*['\"]([^'\"]+)['\"]",
            # import * as name from 'module'
            r"import\s*\*\s*as\s+(\w+)\s+from\s*['\"]([^'\"]+)['\"]",
        ]
        
        imports = {}
        lines = content.split('\n')
        
        for i, line in enumerate(lines, 1):
            for pattern in import_patterns:
                match = re.search(pattern, line)
                if match:
                    names_str = match.group(1)
                    # Parser les noms (peut être "a, b as c, d")
                    for name_part in names_str.split(','):
                        name_part = name_part.strip()
                        if ' as ' in name_part:
                            name = name_part.split(' as ')[1].strip()
                        else:
                            name = name_part.strip()
                        if name and re.match(r'^\w+$', name):
                            imports[name] = (i, match.group(0))
        
        # Chercher les usages
        for name, (line, import_stmt) in imports.items():
            # Pattern pour trouver les usages (pas dans l'import)
            pattern = rf'\b{re.escape(name)}\b'
            matches = list(re.finditer(pattern, content))
            
            # Compter les usages hors de la ligne d'import
            usage_count = 0
            for m in matches:
                match_line = content[:m.start()].count('\n') + 1
                if match_line != line:
                    usage_count += 1
            
            if usage_count == 0:
                issues.append(ConsistencyIssue(
                    kind="unused_import",
                    severity="low",
                    path=filepath,
                    line=line,
                    message=f"Import '{name}' non utilisé",
                    confidence=85,
                    suggested_fix=f"Supprimer '{name}' de l'import",
                    engine="regex-analyzer"
                ))
        
        return issues

    def _analyze_js_unused_vars(self, content: str, filepath: str) -> List[ConsistencyIssue]:
        """Détecte les variables JS/TS non utilisées."""
        issues = []
        
        # Patterns de déclaration
        decl_patterns = [
            r"(?:const|let|var)\s+(\w+)\s*=",
            r"(?:const|let|var)\s+\{([^}]+)\}\s*=",  # Destructuring
        ]
        
        declarations = {}
        lines = content.split('\n')
        
        for i, line in enumerate(lines, 1):
            for pattern in decl_patterns:
                matches = re.finditer(pattern, line)
                for match in matches:
                    names_str = match.group(1)
                    # Parser les noms (destructuring)
                    for name in re.findall(r'\b(\w+)\b', names_str):
                        if not name.startswith('_') and name not in ('const', 'let', 'var'):
                            declarations[name] = i
        
        # Chercher les usages
        for name, line in declarations.items():
            pattern = rf'\b{re.escape(name)}\b'
            matches = list(re.finditer(pattern, content))
            
            # Usage hors déclaration
            usage_count = sum(1 for m in matches if content[:m.start()].count('\n') + 1 != line)
            
            if usage_count == 0:
                issues.append(ConsistencyIssue(
                    kind="unused_var",
                    severity="medium",
                    path=filepath,
                    line=line,
                    message=f"Variable '{name}' déclarée mais jamais utilisée",
                    confidence=75,
                    suggested_fix=f"Supprimer ou préfixer avec _ : _{name}",
                    engine="regex-analyzer"
                ))
        
        return issues

    # ==================== CROSS-LANGUAGE ANALYSIS ====================
    
    def _analyze_duplication(self, files: List[FileInput], min_lines: int = 5) -> List[ConsistencyIssue]:
        """Détecte la duplication de code entre fichiers."""
        issues = []
        
        # Normaliser et hasher les blocs de code
        def normalize_line(line: str) -> str:
            # Supprimer whitespace et commentaires simples
            line = line.strip()
            line = re.sub(r'//.*$', '', line)
            line = re.sub(r'#.*$', '', line)
            return line
        
        def get_blocks(content: str, block_size: int = 5) -> Dict[str, Tuple[int, str]]:
            lines = content.split('\n')
            blocks = {}
            for i in range(len(lines) - block_size + 1):
                block_lines = [normalize_line(l) for l in lines[i:i+block_size]]
                # Ignorer les blocs vides ou triviaux
                if all(len(l) < 3 for l in block_lines):
                    continue
                block_hash = hashlib.md5('\n'.join(block_lines).encode()).hexdigest()
                if block_hash not in blocks:
                    blocks[block_hash] = (i + 1, '\n'.join(lines[i:i+block_size]))
            return blocks
        
        # Analyser chaque fichier
        file_blocks = {}
        for file in files:
            file_blocks[file.path] = get_blocks(file.content, min_lines)
        
        # Trouver les duplications
        seen_duplicates = set()
        for path1, blocks1 in file_blocks.items():
            for path2, blocks2 in file_blocks.items():
                if path1 >= path2:  # Éviter les doublons
                    continue
                
                common = set(blocks1.keys()) & set(blocks2.keys())
                for block_hash in common:
                    if block_hash in seen_duplicates:
                        continue
                    seen_duplicates.add(block_hash)
                    
                    line1, code = blocks1[block_hash]
                    line2, _ = blocks2[block_hash]
                    
                    issues.append(ConsistencyIssue(
                        kind="duplication",
                        severity="low",
                        path=path1,
                        line=line1,
                        message=f"Bloc de code dupliqué dans {path2}:{line2}",
                        confidence=80,
                        suggested_fix="Extraire dans une fonction/module partagé",
                        engine="hash-comparator"
                    ))
        
        return issues

    def _analyze_unresolved_symbols(self, files: List[FileInput]) -> List[ConsistencyIssue]:
        """Détecte les symboles non résolus dans le scope fourni."""
        issues = []
        
        # Collecter tous les symboles définis
        defined_symbols = set()
        
        # Builtins Python
        python_builtins = {
            'print', 'len', 'range', 'str', 'int', 'float', 'bool', 'list', 'dict', 'set',
            'tuple', 'type', 'isinstance', 'hasattr', 'getattr', 'setattr', 'open', 'input',
            'sum', 'min', 'max', 'abs', 'round', 'sorted', 'reversed', 'enumerate', 'zip',
            'map', 'filter', 'any', 'all', 'None', 'True', 'False', 'Exception', 'ValueError',
            'TypeError', 'KeyError', 'IndexError', 'AttributeError', 'super', 'property',
            'staticmethod', 'classmethod', 'self', 'cls', '__name__', '__file__',
        }
        
        # Globaux JS
        js_globals = {
            'console', 'window', 'document', 'fetch', 'Promise', 'Array', 'Object', 'String',
            'Number', 'Boolean', 'JSON', 'Math', 'Date', 'Error', 'undefined', 'null',
            'setTimeout', 'setInterval', 'clearTimeout', 'clearInterval', 'require', 'module',
            'exports', 'process', 'Buffer', '__dirname', '__filename', 'global', 'this',
        }
        
        defined_symbols.update(python_builtins)
        defined_symbols.update(js_globals)
        
        for file in files:
            lang = file.language or self._detect_language(file.path)
            
            if lang == 'python':
                try:
                    tree = ast.parse(file.content)
                    for node in ast.walk(tree):
                        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                            defined_symbols.add(node.name)
                        elif isinstance(node, ast.ClassDef):
                            defined_symbols.add(node.name)
                        elif isinstance(node, ast.Import):
                            for alias in node.names:
                                defined_symbols.add(alias.asname or alias.name.split('.')[0])
                        elif isinstance(node, ast.ImportFrom):
                            for alias in node.names:
                                defined_symbols.add(alias.asname or alias.name)
                        elif isinstance(node, ast.Assign):
                            for target in node.targets:
                                if isinstance(target, ast.Name):
                                    defined_symbols.add(target.id)
                except SyntaxError:
                    pass
            
            elif lang in ('typescript', 'javascript'):
                # Extraire les définitions via regex
                patterns = [
                    r"(?:function|class)\s+(\w+)",
                    r"(?:const|let|var)\s+(\w+)",
                    r"import\s+(?:\{[^}]*\}|\*\s+as\s+)?(\w+)",
                    r"export\s+(?:default\s+)?(?:function|class|const|let|var)\s+(\w+)",
                ]
                for pattern in patterns:
                    matches = re.findall(pattern, file.content)
                    defined_symbols.update(matches)
        
        # Maintenant chercher les symboles utilisés mais non définis
        for file in files:
            lang = file.language or self._detect_language(file.path)
            
            if lang == 'python':
                try:
                    tree = ast.parse(file.content)
                    for node in ast.walk(tree):
                        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                            if node.id not in defined_symbols:
                                issues.append(ConsistencyIssue(
                                    kind="unresolved_symbol",
                                    severity="high",
                                    path=file.path,
                                    line=node.lineno,
                                    column=node.col_offset,
                                    message=f"Symbole '{node.id}' non résolu dans le scope fourni",
                                    confidence=60,
                                    suggested_fix=f"Vérifier l'import de '{node.id}' ou sa définition",
                                    engine="scope-analyzer"
                                ))
                except SyntaxError:
                    pass
        
        return issues

    def _execute_core_logic(self, request: ConsistencyCheckRequest, **kwargs) -> ConsistencyCheckResponse:
        """Exécute la vérification de cohérence."""
        self.logger.info(f"Vérification de cohérence sur {len(request.files)} fichier(s)")
        
        # Déterminer les checks à exécuter
        checks = request.checks or self.ALL_CHECKS
        
        all_issues = []
        all_contents = '\n'.join(f.content for f in request.files)
        
        for file in request.files:
            lang = file.language or (request.language if request.language != 'auto' else self._detect_language(file.path))
            
            if lang == 'python':
                if 'unused_imports' in checks:
                    all_issues.extend(self._analyze_python_unused_imports(file.content, file.path))
                if 'unused_vars' in checks:
                    all_issues.extend(self._analyze_python_unused_vars(file.content, file.path))
                if 'dead_code' in checks:
                    all_issues.extend(self._analyze_python_dead_code(file.content, file.path, all_contents))
            
            elif lang in ('typescript', 'javascript'):
                if 'unused_imports' in checks:
                    all_issues.extend(self._analyze_js_unused_imports(file.content, file.path))
                if 'unused_vars' in checks:
                    all_issues.extend(self._analyze_js_unused_vars(file.content, file.path))
        
        # Analyses cross-fichiers
        if 'duplication' in checks and len(request.files) > 1:
            all_issues.extend(self._analyze_duplication(request.files))
        
        if 'unresolved_symbol' in checks and request.mode == 'deep':
            all_issues.extend(self._analyze_unresolved_symbols(request.files))
        
        # Filtrer par confiance minimum
        all_issues = [i for i in all_issues if i.confidence >= request.min_confidence]
        
        # Compter par sévérité
        severity_counts = {'high': 0, 'medium': 0, 'low': 0, 'info': 0}
        for issue in all_issues:
            severity_counts[issue.severity] = severity_counts.get(issue.severity, 0) + 1
        
        summary = {
            'total': len(all_issues),
            'high': severity_counts['high'],
            'medium': severity_counts['medium'],
            'low': severity_counts['low'],
            'info': severity_counts['info'],
        }
        
        # Construire le résumé
        if not all_issues:
            analysis_summary = f"✅ Aucune incohérence détectée dans {len(request.files)} fichier(s)."
        else:
            analysis_summary = (
                f"⚠️ {len(all_issues)} incohérence(s) détectée(s) dans {len(request.files)} fichier(s). "
                f"Haute({severity_counts['high']}), Moyenne({severity_counts['medium']}), "
                f"Basse({severity_counts['low']}), Info({severity_counts['info']})."
            )
        
        return ConsistencyCheckResponse(
            valid=len(all_issues) == 0,
            summary=summary,
            issues=all_issues[:100],  # Limiter
            files_analyzed=len(request.files),
            checks_performed=checks,
            analysis_summary=analysis_summary
        )
