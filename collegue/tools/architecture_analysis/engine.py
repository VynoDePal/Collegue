"""
Moteur d'analyse architecturale.

Contient la logique métier pure : extraction de dépendances, détection de
patterns, calcul de métriques, identification de dette technique.
"""

import ast
import re
from collections import defaultdict
from typing import Any, Dict, List, Set, Tuple

from .config import DEBT_INDICATORS, IMPORT_PATTERNS
from .models import ArchitecturalIssue, DependencyInfo


class ArchitectureEngine:
    """Moteur d'analyse architecturale de code."""

    def __init__(self, logger=None):
        self.logger = logger

    def extract_dependencies(self, code: str, language: str) -> List[DependencyInfo]:
        """Extrait les dépendances (imports) du code."""
        deps = []
        lang = language.lower()
        pattern = IMPORT_PATTERNS.get(lang)
        if not pattern:
            return deps

        for line in code.split("\n"):
            match = re.search(pattern, line.strip())
            if match:
                groups = match.groups()
                target = next((g for g in groups if g), None)
                if target:
                    deps.append(
                        DependencyInfo(
                            source="current_module",
                            target=target,
                            import_type="direct",
                        )
                    )

        return deps

    def detect_circular_dependencies(self, dependencies: List[DependencyInfo]) -> List[ArchitecturalIssue]:
        """Détecte les dépendances circulaires dans le graphe."""
        issues = []

        graph: Dict[str, Set[str]] = defaultdict(set)
        for dep in dependencies:
            graph[dep.source].add(dep.target)

        visited: Set[str] = set()
        rec_stack: Set[str] = set()
        cycles: List[List[str]] = []

        def _dfs(node: str, path: List[str]) -> None:
            visited.add(node)
            rec_stack.add(node)
            path.append(node)

            for neighbor in graph.get(node, set()):
                if neighbor not in visited:
                    _dfs(neighbor, path)
                elif neighbor in rec_stack:
                    cycle_start = path.index(neighbor) if neighbor in path else 0
                    cycle = path[cycle_start:] + [neighbor]
                    cycles.append(cycle)

            path.pop()
            rec_stack.discard(node)

        for node in graph:
            if node not in visited:
                _dfs(node, [])

        for cycle in cycles:
            issues.append(
                ArchitecturalIssue(
                    category="circular_dependency",
                    severity="critical",
                    title=f"Dépendance circulaire: {' → '.join(cycle)}",
                    description=(
                        f"Les modules {', '.join(cycle[:-1])} forment une dépendance circulaire. "
                        "Cela rend le code difficile à tester et à maintenir."
                    ),
                    affected_modules=cycle[:-1],
                    recommendation="Extraire une interface ou un module intermédiaire.",
                )
            )

        return issues

    def analyze_coupling(self, code: str, language: str) -> Tuple[float, List[ArchitecturalIssue]]:
        """Analyse le couplage du code. Retourne (score_couplage, issues)."""
        issues = []
        deps = self.extract_dependencies(code, language)

        fan_out = len(deps)
        threshold = DEBT_INDICATORS["high_fan_out"]["threshold"]

        if fan_out > threshold:
            issues.append(
                ArchitecturalIssue(
                    category="high_coupling",
                    severity="warning",
                    title=f"Couplage élevé: {fan_out} dépendances",
                    description=(
                        f"Le module a {fan_out} dépendances directes (seuil: {threshold}). "
                        "Un couplage élevé rend le code fragile aux changements."
                    ),
                    recommendation="Appliquer le principe d'inversion de dépendance (DIP).",
                )
            )

        coupling_score = min(1.0, fan_out / 20.0)
        return coupling_score, issues

    def analyze_cohesion(self, code: str, language: str) -> Tuple[float, List[ArchitecturalIssue]]:
        """Analyse la cohésion du code. Retourne (score_cohésion, issues)."""
        issues = []
        lang = language.lower()

        if lang == "python":
            return self._analyze_python_cohesion(code)

        lines = code.split("\n")
        func_count = sum(1 for line in lines if "function " in line or line.strip().startswith("def "))
        total_lines = len([l for l in lines if l.strip()])

        if func_count > 0:
            avg_func_size = total_lines / func_count
            cohesion = min(1.0, 30.0 / max(1, avg_func_size))
        else:
            cohesion = 0.3 if total_lines > 50 else 0.7

        if cohesion < 0.4:
            issues.append(
                ArchitecturalIssue(
                    category="low_cohesion",
                    severity="warning",
                    title="Cohésion faible",
                    description="Les fonctions semblent trop longues ou peu reliées.",
                    recommendation="Découper en fonctions plus petites et focalisées.",
                )
            )

        return cohesion, issues

    def _analyze_python_cohesion(self, code: str) -> Tuple[float, List[ArchitecturalIssue]]:
        issues = []
        try:
            tree = ast.parse(code)
        except SyntaxError:
            return 0.5, issues

        classes = [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]
        functions = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]

        for cls in classes:
            methods = [n for n in ast.walk(cls) if isinstance(n, ast.FunctionDef)]
            method_count = len(methods)
            cls_lines = getattr(cls, "end_lineno", 0) - cls.lineno + 1

            god_threshold_lines = DEBT_INDICATORS["god_class"]["threshold_lines"]
            god_threshold_methods = DEBT_INDICATORS["god_class"]["threshold_methods"]

            if cls_lines > god_threshold_lines or method_count > god_threshold_methods:
                issues.append(
                    ArchitecturalIssue(
                        category="god_class",
                        severity="error",
                        title=f"God Class: '{cls.name}' ({cls_lines} lignes, {method_count} méthodes)",
                        description=(
                            f"La classe '{cls.name}' est trop grande. "
                            f"Seuils: {god_threshold_lines} lignes, {god_threshold_methods} méthodes."
                        ),
                        affected_modules=[cls.name],
                        recommendation="Extraire des sous-classes ou utiliser la composition.",
                    )
                )

        total_funcs = len(functions)
        if total_funcs == 0:
            return 0.5, issues

        total_lines = len([l for l in code.split("\n") if l.strip()])
        avg_size = total_lines / total_funcs
        cohesion = min(1.0, 25.0 / max(1, avg_size))

        return cohesion, issues

    def detect_patterns(self, code: str, language: str) -> List[str]:
        """Détecte les patterns architecturaux utilisés dans le code."""
        patterns = []

        if "class" in code:
            if "Repository" in code or "repository" in code:
                patterns.append("Repository Pattern")
            if "Factory" in code or "factory" in code:
                patterns.append("Factory Pattern")
            if "Service" in code or "service" in code:
                patterns.append("Service Layer")
            if "@singleton" in code or "Singleton" in code or "_instance" in code:
                patterns.append("Singleton")
            if "Observer" in code or "listener" in code or "on_event" in code:
                patterns.append("Observer")
            if "Strategy" in code or "strategy" in code:
                patterns.append("Strategy")

        if "models" in code.lower() and "views" in code.lower():
            patterns.append("MVC (Model-View-Controller)")
        if "controller" in code.lower() or "handler" in code.lower():
            patterns.append("Layered Architecture")

        if "ABC" in code or "abstractmethod" in code or "abstract" in code.lower():
            patterns.append("Clean Architecture (abstractions)")

        return patterns

    def calculate_metrics(self, code: str, language: str) -> Dict[str, Any]:
        """Calcule les métriques architecturales."""
        lines = code.split("\n")
        non_empty = [l for l in lines if l.strip()]

        deps = self.extract_dependencies(code, language)

        metrics = {
            "total_lines": len(lines),
            "code_lines": len(non_empty),
            "modules_imported": len(deps),
            "fan_out": len(deps),
            "fan_in": 0,
        }

        if language.lower() == "python":
            try:
                tree = ast.parse(code)
                metrics["class_count"] = sum(1 for n in ast.walk(tree) if isinstance(n, ast.ClassDef))
                metrics["function_count"] = sum(1 for n in ast.walk(tree) if isinstance(n, ast.FunctionDef))

                max_depth = 0
                for node in ast.walk(tree):
                    if isinstance(node, ast.ClassDef):
                        depth = len(node.bases)
                        max_depth = max(max_depth, depth)
                metrics["max_inheritance_depth"] = max_depth

            except SyntaxError:
                metrics["class_count"] = sum(1 for l in non_empty if l.strip().startswith("class "))
                metrics["function_count"] = sum(1 for l in non_empty if l.strip().startswith("def "))
        else:
            metrics["class_count"] = sum(1 for l in non_empty if "class " in l)
            metrics["function_count"] = sum(1 for l in non_empty if "function " in l or l.strip().startswith("def "))

        return metrics

    def calculate_debt_score(self, issues: List[ArchitecturalIssue]) -> float:
        """Calcule le score de dette technique."""
        if not issues:
            return 0.0

        severity_weights = {
            "info": 0.05,
            "warning": 0.15,
            "error": 0.30,
            "critical": 0.50,
        }

        total = sum(severity_weights.get(i.severity, 0.1) for i in issues)
        return min(1.0, total)

    def calculate_architecture_score(
        self,
        coupling_score: float,
        cohesion_score: float,
        debt_score: float,
        issues: List[ArchitecturalIssue],
    ) -> float:
        """Calcule le score architectural global."""
        coupling_component = max(0.0, 1.0 - coupling_score) * 0.3
        cohesion_component = cohesion_score * 0.3
        debt_component = max(0.0, 1.0 - debt_score) * 0.2

        critical_count = sum(1 for i in issues if i.severity == "critical")
        issue_penalty = min(0.2, critical_count * 0.05)
        issue_component = 0.2 - issue_penalty

        return max(0.0, min(1.0, coupling_component + cohesion_component + debt_component + issue_component))
