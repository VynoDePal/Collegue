"""
Moteur d'analyse de performance.

Contient la logique métier pure : détection de patterns inefficaces,
estimation de complexité, identification de hotspots.
"""

import ast
import re
from typing import Any, Dict, List

from .config import INEFFICIENT_PATTERNS
from .models import PerformanceIssue


class PerformanceEngine:
    """Moteur d'analyse de performance de code."""

    def __init__(self, logger=None):
        self.logger = logger

    def detect_inefficient_patterns(self, code: str, language: str) -> List[PerformanceIssue]:
        """Détecte les patterns inefficaces connus."""
        issues = []
        lang = language.lower()
        patterns = INEFFICIENT_PATTERNS.get(lang, {})

        for name, info in patterns.items():
            matches = list(re.finditer(info["pattern"], code, re.MULTILINE))
            for match in matches:
                line_num = code[: match.start()].count("\n") + 1
                issues.append(
                    PerformanceIssue(
                        category=info["category"],
                        severity=info["severity"],
                        line=line_num,
                        title=name.replace("_", " ").title(),
                        description=info["description"],
                    )
                )

        return issues

    def analyze_algorithmic_complexity(self, code: str, language: str) -> List[PerformanceIssue]:
        """Analyse la complexité algorithmique."""
        issues = []
        lang = language.lower()

        if lang == "python":
            issues.extend(self._analyze_python_complexity(code))
        else:
            issues.extend(self._analyze_generic_complexity(code))

        return issues

    def _analyze_python_complexity(self, code: str) -> List[PerformanceIssue]:
        issues = []
        try:
            tree = ast.parse(code)
        except SyntaxError:
            return issues

        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                loop_depth = self._get_max_loop_depth(node)
                if loop_depth >= 3:
                    issues.append(
                        PerformanceIssue(
                            category="algorithmic",
                            severity="critical",
                            line=node.lineno,
                            title=f"Complexité O(n^{loop_depth}) dans '{node.name}'",
                            description=(
                                f"La fonction '{node.name}' contient {loop_depth} niveaux de boucles imbriquées, "
                                f"suggérant une complexité O(n^{loop_depth})."
                            ),
                            estimated_complexity=f"O(n^{loop_depth})",
                        )
                    )
                elif loop_depth == 2:
                    issues.append(
                        PerformanceIssue(
                            category="algorithmic",
                            severity="warning",
                            line=node.lineno,
                            title=f"Complexité O(n²) potentielle dans '{node.name}'",
                            description=(
                                f"La fonction '{node.name}' contient des boucles imbriquées. "
                                "Vérifiez si une structure de données (set, dict) peut réduire la complexité."
                            ),
                            estimated_complexity="O(n²)",
                        )
                    )

                # Vérifier les opérations 'in' sur des listes dans des boucles
                self._check_list_search_in_loop(node, issues)

        return issues

    def _get_max_loop_depth(self, node: ast.AST, current_depth: int = 0) -> int:
        """Calcule la profondeur maximale de boucles imbriquées."""
        max_depth = current_depth
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.For, ast.While)):
                child_depth = self._get_max_loop_depth(child, current_depth + 1)
                max_depth = max(max_depth, child_depth)
            else:
                child_depth = self._get_max_loop_depth(child, current_depth)
                max_depth = max(max_depth, child_depth)
        return max_depth

    def _check_list_search_in_loop(self, func_node: ast.FunctionDef, issues: List[PerformanceIssue]) -> None:
        """Détecte les recherches linéaires dans des boucles."""
        for node in ast.walk(func_node):
            if isinstance(node, (ast.For, ast.While)):
                for child in ast.walk(node):
                    if isinstance(child, ast.Compare):
                        for op in child.ops:
                            if isinstance(op, ast.In):
                                issues.append(
                                    PerformanceIssue(
                                        category="algorithmic",
                                        severity="warning",
                                        line=getattr(child, "lineno", func_node.lineno),
                                        title="Recherche linéaire dans une boucle",
                                        description=(
                                            "Opérateur 'in' dans une boucle — si la collection est une liste, "
                                            "la complexité est O(n²). Utiliser un set pour O(n)."
                                        ),
                                        estimated_complexity="O(n²) → O(n)",
                                        suggestion="Convertir la collection en set() avant la boucle.",
                                    )
                                )
                                return

    def _analyze_generic_complexity(self, code: str) -> List[PerformanceIssue]:
        issues = []
        lines = code.split("\n")

        loop_depth = 0
        max_depth = 0
        loop_start = 0

        for i, line in enumerate(lines, 1):
            stripped = line.strip()
            if re.match(r"(for|while)\s*[\(:]", stripped):
                if loop_depth == 0:
                    loop_start = i
                loop_depth += 1
                max_depth = max(max_depth, loop_depth)
            elif stripped in ("}", "end", "endfor", "endwhile"):
                loop_depth = max(0, loop_depth - 1)

        if max_depth >= 2:
            severity = "critical" if max_depth >= 3 else "warning"
            issues.append(
                PerformanceIssue(
                    category="algorithmic",
                    severity=severity,
                    line=loop_start,
                    title=f"Boucles imbriquées (profondeur {max_depth})",
                    description=f"Complexité potentielle O(n^{max_depth}).",
                    estimated_complexity=f"O(n^{max_depth})",
                )
            )

        return issues

    def detect_memory_issues(self, code: str, language: str) -> List[PerformanceIssue]:
        """Détecte les problèmes de mémoire."""
        issues = []
        lang = language.lower()

        if lang == "python":
            # Chargement de gros fichiers en mémoire
            if "readlines()" in code or ".read()" in code:
                for i, line in enumerate(code.split("\n"), 1):
                    if "readlines()" in line or ".read()" in line:
                        issues.append(
                            PerformanceIssue(
                                category="memory",
                                severity="warning",
                                line=i,
                                title="Chargement complet de fichier en mémoire",
                                description=(
                                    "readlines() ou read() charge tout le fichier en mémoire. "
                                    "Pour les gros fichiers, itérer ligne par ligne."
                                ),
                                suggestion="for line in open(file): ...",
                            )
                        )

            # Listes non bornées
            if "while True" in code and ".append(" in code:
                issues.append(
                    PerformanceIssue(
                        category="memory",
                        severity="warning",
                        title="Accumulation potentiellement non bornée",
                        description=(
                            "Une boucle infinie avec append() peut consommer toute la mémoire. Ajoutez une limite."
                        ),
                    )
                )

        elif lang in ("javascript", "typescript"):
            # Closures en boucle
            if re.search(r"for\s*\(.*\)\s*\{[\s\S]*?function\s*\(", code):
                issues.append(
                    PerformanceIssue(
                        category="memory",
                        severity="warning",
                        title="Closure dans une boucle",
                        description="Créer des closures dans une boucle peut causer des fuites mémoire.",
                        suggestion="Utiliser let au lieu de var, ou extraire la fonction.",
                    )
                )

        return issues

    def detect_io_issues(self, code: str, language: str) -> List[PerformanceIssue]:
        """Détecte les problèmes d'I/O."""
        issues = []
        lang = language.lower()

        if lang == "python":
            # Requêtes séquentielles
            lines = code.split("\n")
            request_count = sum(1 for line in lines if re.search(r"requests\.(get|post|put|delete)\s*\(", line))
            if request_count > 2:
                issues.append(
                    PerformanceIssue(
                        category="io",
                        severity="warning",
                        title=f"{request_count} requêtes HTTP séquentielles",
                        description=(
                            f"Détecté {request_count} appels HTTP séquentiels. "
                            "Utiliser asyncio/aiohttp ou threading pour paralléliser."
                        ),
                    )
                )

            # Open sans context manager
            for i, line in enumerate(lines, 1):
                if re.search(r"open\s*\(", line) and "with " not in line:
                    issues.append(
                        PerformanceIssue(
                            category="io",
                            severity="warning",
                            line=i,
                            title="open() sans context manager",
                            description="Utiliser 'with open(...)' pour garantir la fermeture du fichier.",
                        )
                    )

        return issues

    def identify_hotspots(self, code: str, language: str, issues: List[PerformanceIssue]) -> List[Dict[str, Any]]:
        """Identifie les points chauds (lignes les plus problématiques)."""
        line_scores: Dict[int, float] = {}
        severity_weights = {"info": 0.1, "warning": 0.3, "error": 0.6, "critical": 1.0}

        for issue in issues:
            if issue.line:
                line_scores[issue.line] = line_scores.get(issue.line, 0) + severity_weights.get(issue.severity, 0.1)

        sorted_lines = sorted(line_scores.items(), key=lambda x: x[1], reverse=True)[:5]
        lines = code.split("\n")

        hotspots = []
        for line_num, score in sorted_lines:
            code_line = lines[line_num - 1].strip() if line_num <= len(lines) else ""
            hotspots.append(
                {
                    "line": line_num,
                    "score": round(score, 2),
                    "code": code_line[:100],
                    "issues": [{"title": i.title, "severity": i.severity} for i in issues if i.line == line_num],
                }
            )

        return hotspots

    def calculate_performance_score(self, issues: List[PerformanceIssue], total_lines: int) -> float:
        """Calcule le score de performance global."""
        if total_lines == 0:
            return 1.0

        severity_weights = {"info": 0.03, "warning": 0.10, "error": 0.25, "critical": 0.40}
        penalty = sum(severity_weights.get(i.severity, 0.1) for i in issues)

        size_factor = max(1.0, total_lines / 50.0)
        normalized = penalty / size_factor

        return max(0.0, min(1.0, 1.0 - normalized))

    def calculate_category_scores(self, issues: List[PerformanceIssue], categories: List[str]) -> Dict[str, float]:
        """Score par catégorie."""
        scores = {}
        severity_weights = {"info": 0.05, "warning": 0.15, "error": 0.30, "critical": 0.50}

        for cat in categories:
            cat_issues = [i for i in issues if i.category == cat]
            if not cat_issues:
                scores[cat] = 1.0
            else:
                penalty = sum(severity_weights.get(i.severity, 0.1) for i in cat_issues)
                scores[cat] = max(0.0, min(1.0, 1.0 - penalty))

        return scores
