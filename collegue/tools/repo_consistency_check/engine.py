"""
Moteur d'analyse de cohérence pour le Repo Consistency Check.

Contient la logique métier pure : détection de duplication, analyse de symboles,
calcul de scores, etc.
"""
import re
import ast
import hashlib
from typing import List, Dict, Any, Tuple, Set
from ...core.shared import ConsistencyIssue, detect_language_from_extension
from .config import BUILTINS, REFACTORING_WEIGHTS, REFACTORING_THRESHOLDS, DUPLICATION_MIN_LINES


class ConsistencyAnalysisEngine:
    """Moteur d'analyse de cohérence du code."""
    
    def __init__(self, logger=None):
        self.logger = logger
    
    def detect_language(self, filepath: str) -> str:
        """Détecte le langage à partir de l'extension du fichier."""
        return detect_language_from_extension(filepath)
    
    def normalize_line(self, line: str) -> str:
        """Normalise une ligne de code pour la comparaison."""
        line = line.strip()
        line = re.sub(r'//.*$', '', line)
        line = re.sub(r'#.*$', '', line)
        return line
    
    def get_code_blocks(self, content: str, block_size: int = DUPLICATION_MIN_LINES) -> Dict[str, Tuple[int, str]]:
        """Extrait les blocs de code avec leur hash."""
        lines = content.split('\n')
        blocks = {}
        
        for i in range(len(lines) - block_size + 1):
            block_lines = [self.normalize_line(l) for l in lines[i:i+block_size]]
            
            # Ignorer les blocs trop courts ou vides
            if all(len(l) < 3 for l in block_lines):
                continue
            
            block_hash = hashlib.md5('\n'.join(block_lines).encode()).hexdigest()
            if block_hash not in blocks:
                blocks[block_hash] = (i + 1, '\n'.join(lines[i:i+block_size]))
        
        return blocks
    
    def analyze_duplication(self, files: List, min_lines: int = DUPLICATION_MIN_LINES) -> List[ConsistencyIssue]:
        """Détecte la duplication de code entre fichiers."""
        issues = []
        file_blocks = {}
        
        # Extraire les blocs de chaque fichier
        for file in files:
            file_blocks[file.path] = self.get_code_blocks(file.content, min_lines)
        
        # Chercher les doublons
        seen_duplicates = set()
        for path1, blocks1 in file_blocks.items():
            for path2, blocks2 in file_blocks.items():
                if path1 >= path2:
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
    
    def extract_defined_symbols(self, file) -> Set[str]:
        """Extrait les symboles définis dans un fichier."""
        defined = set()
        lang = file.language or self.detect_language(file.path)
        
        if lang == 'python':
            try:
                tree = ast.parse(file.content)
                for node in ast.walk(tree):
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        defined.add(node.name)
                    elif isinstance(node, ast.ClassDef):
                        defined.add(node.name)
                    elif isinstance(node, ast.Import):
                        for alias in node.names:
                            defined.add(alias.asname or alias.name.split('.')[0])
                    elif isinstance(node, ast.ImportFrom):
                        for alias in node.names:
                            defined.add(alias.asname or alias.name)
                    elif isinstance(node, ast.Assign):
                        for target in node.targets:
                            if isinstance(target, ast.Name):
                                defined.add(target.id)
            except SyntaxError:
                pass
        
        elif lang in ('typescript', 'javascript', 'php'):
            patterns = [
                r"(?:function|class)\s+(\w+)",
                r"(?:const|let|var)\s+(\w+)",
                r"import\s+(?:\{[^}]*\}|\*\s+as\s+)?(\w+)",
                r"export\s+(?:default\s+)?(?:function|class|const|let|var)\s+(\w+)",
            ]
            
            if lang == 'php':
                patterns = [
                    r"(?:function|class|trait|interface)\s+(\w+)",
                    r"(\$[a-zA-Z0-9_]+)\s*=",
                    r"use\s+(?:[a-zA-Z0-9_\\]+\\)*(\w+)(?:\s+as\s+(\w+))?;",
                ]
            
            for pattern in patterns:
                matches = re.findall(pattern, file.content)
                for m in matches:
                    if isinstance(m, tuple):
                        for g in m:
                            if g:
                                defined.add(g)
                    else:
                        defined.add(m)
        
        return defined
    
    def analyze_unresolved_symbols(self, files: List) -> List[ConsistencyIssue]:
        """Détecte les symboles utilisés mais non définis."""
        issues = []
        
        # Collecter tous les symboles définis + builtins
        defined_symbols = set()
        for builtins_set in BUILTINS.values():
            defined_symbols.update(builtins_set)
        
        for file in files:
            defined_symbols.update(self.extract_defined_symbols(file))
        
        # Chercher les symboles non résolus
        for file in files:
            lang = file.language or self.detect_language(file.path)
            
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
    
    def calculate_refactoring_score(self, issues: List[ConsistencyIssue]) -> Tuple[float, str]:
        """Calcule le score et la priorité de refactoring."""
        if not issues:
            return 0.0, "none"
        
        total_weight = sum(REFACTORING_WEIGHTS.get(i.severity, 0.1) for i in issues)
        score = min(1.0, total_weight / 4.0)
        
        if score >= REFACTORING_THRESHOLDS['critical']:
            priority = "critical"
        elif score >= REFACTORING_THRESHOLDS['recommended']:
            priority = "recommended"
        elif score >= REFACTORING_THRESHOLDS['suggested']:
            priority = "suggested"
        else:
            priority = "none"
        
        return score, priority
    
    def generate_suggested_actions(
        self,
        issues: List[ConsistencyIssue],
        files: List,
        score: float,
        detect_language_fn
    ) -> List:
        """Génère les actions suggérées pour corriger les problèmes."""
        from .models import SuggestedAction
        actions = []
        
        # Grouper les issues par type
        issue_types = {}
        for issue in issues:
            issue_types.setdefault(issue.kind, []).append(issue)
        
        # Action pour le fichier avec le plus de problèmes
        if len(issues) >= 5:
            file_issues = {}
            for issue in issues:
                file_issues.setdefault(issue.path, []).append(issue)
            
            worst_file = max(file_issues.items(), key=lambda x: len(x[1]))
            file_content = next((f.content for f in files if f.path == worst_file[0]), "")
            
            actions.append(SuggestedAction(
                tool_name="code_refactoring",
                action_type="cleanup",
                rationale=f"Fichier '{worst_file[0]}' a {len(worst_file[1])} problèmes de cohérence",
                priority="high" if len(worst_file[1]) >= 5 else "medium",
                params={
                    "code": file_content[:5000],
                    "language": detect_language_fn(worst_file[0]),
                    "refactoring_type": "clean",
                    "file_path": worst_file[0]
                },
                score=score
            ))
        
        # Action pour code mort
        if 'dead_code' in issue_types and len(issue_types['dead_code']) >= 2:
            actions.append(SuggestedAction(
                tool_name="code_refactoring",
                action_type="cleanup",
                rationale=f"{len(issue_types['dead_code'])} fonctions/classes mortes détectées",
                priority="medium",
                params={"refactoring_type": "clean"},
                score=min(1.0, len(issue_types['dead_code']) * 0.2)
            ))
        
        # Action pour duplication
        if 'duplication' in issue_types:
            actions.append(SuggestedAction(
                tool_name="code_refactoring",
                action_type="restructure",
                rationale=f"{len(issue_types['duplication'])} bloc(s) de code dupliqué(s)",
                priority="medium",
                params={"refactoring_type": "extract"},
                score=min(1.0, len(issue_types['duplication']) * 0.25)
            ))
        
        return actions[:5]
    
    def build_analysis_summary(
        self,
        issues: List[ConsistencyIssue],
        files_count: int,
        severity_counts: Dict[str, int],
        analysis_depth: str = "fast",
        refactoring_score: float = 0.0,
        refactoring_priority: str = "none",
        llm_insights_count: int = 0,
        auto_refactoring: bool = False
    ) -> str:
        """Construit le résumé textuel de l'analyse."""
        if not issues:
            summary = f"✅ Aucune incohérence détectée dans {files_count} fichier(s)."
        else:
            summary = (
                f"⚠️ {len(issues)} incohérence(s) détectée(s) dans {files_count} fichier(s). "
                f"Haute({severity_counts['high']}), Moyenne({severity_counts['medium']}), "
                f"Basse({severity_counts['low']}), Info({severity_counts['info']})."
            )
        
        if analysis_depth == 'deep':
            summary += f" 🤖 Score refactoring: {refactoring_score:.0%} ({refactoring_priority})."
            if llm_insights_count > 0:
                summary += f" {llm_insights_count} insight(s) IA."
        
        if auto_refactoring:
            summary += " 🔧 Refactoring auto-déclenché."
        
        return summary
