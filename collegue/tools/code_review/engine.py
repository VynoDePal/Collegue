"""
Moteur d'analyse pour l'outil Code Review.

Contient la logique métier pure : détection de problèmes, calcul de scores,
analyse de patterns.
"""

import ast
import re
from typing import Dict, List

from .config import (
    COMPLEXITY_KEYWORDS,
    SECURITY_PATTERNS,
    SEVERITY_WEIGHTS,
)
from .models import ReviewFinding


class CodeReviewEngine:
    """Moteur d'analyse de code pour les revues."""

    def __init__(self, logger=None):
        self.logger = logger

    def analyze_naming(self, code: str, language: str) -> List[ReviewFinding]:
        """Analyse les conventions de nommage."""
        findings = []
        lang = language.lower()

        if lang == "python":
            findings.extend(self._check_python_naming(code))
        elif lang in ("javascript", "typescript"):
            findings.extend(self._check_js_naming(code))

        return findings

    def _check_python_naming(self, code: str) -> List[ReviewFinding]:
        findings = []
        try:
            tree = ast.parse(code)
        except SyntaxError:
            return findings

        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                if not re.match(r"^[a-z_][a-z0-9_]*$", node.name) and not node.name.startswith("_"):
                    findings.append(
                        ReviewFinding(
                            category="naming",
                            severity="warning",
                            line=node.lineno,
                            title=f"Nom de fonction non snake_case: '{node.name}'",
                            description=f"La fonction '{node.name}' ne suit pas la convention snake_case (PEP 8).",
                            suggestion=f"Renommer en '{self._to_snake_case(node.name)}'",
                        )
                    )
            elif isinstance(node, ast.ClassDef):
                if not re.match(r"^[A-Z][a-zA-Z0-9]*$", node.name):
                    findings.append(
                        ReviewFinding(
                            category="naming",
                            severity="warning",
                            line=node.lineno,
                            title=f"Nom de classe non PascalCase: '{node.name}'",
                            description=f"La classe '{node.name}' ne suit pas la convention PascalCase (PEP 8).",
                        )
                    )

        return findings

    def _check_js_naming(self, code: str) -> List[ReviewFinding]:
        findings = []
        for i, line in enumerate(code.split("\n"), 1):
            match = re.match(r"\s*(?:function|const|let|var)\s+([a-zA-Z_$][a-zA-Z0-9_$]*)", line)
            if match:
                name = match.group(1)
                if re.match(r"^[A-Z]", name) and "class" not in line:
                    if not re.match(r"^[A-Z_]+$", name):
                        findings.append(
                            ReviewFinding(
                                category="naming",
                                severity="info",
                                line=i,
                                title=f"Variable/fonction avec majuscule initiale: '{name}'",
                                description=f"'{name}' commence par une majuscule mais n'est pas une classe.",
                            )
                        )
        return findings

    def analyze_complexity(self, code: str, language: str) -> List[ReviewFinding]:
        """Analyse la complexité du code."""
        findings = []
        lang = language.lower()
        keywords = COMPLEXITY_KEYWORDS.get(lang, COMPLEXITY_KEYWORDS.get("python", []))

        lines = code.split("\n")
        func_name = None
        func_start = 0
        func_complexity = 0

        for i, line in enumerate(lines, 1):
            stripped = line.strip()

            if lang == "python" and stripped.startswith("def "):
                if func_name and func_complexity > 10:
                    findings.append(
                        ReviewFinding(
                            category="complexity",
                            severity="error" if func_complexity > 15 else "warning",
                            line=func_start,
                            title=f"Complexité élevée: '{func_name}' (score={func_complexity})",
                            description=(
                                f"La fonction '{func_name}' a une complexité cyclomatique de {func_complexity}. "
                                "Recommandé: < 10."
                            ),
                        )
                    )
                match = re.match(r"\s*def\s+(\w+)", stripped)
                func_name = match.group(1) if match else "unknown"
                func_start = i
                func_complexity = 1
                continue

            for kw in keywords:
                if kw in stripped.lower():
                    func_complexity += 1

            # Vérifier l'imbrication excessive
            indent = len(line) - len(line.lstrip())
            indent_level = indent // 4
            if indent_level >= 5 and stripped:
                findings.append(
                    ReviewFinding(
                        category="complexity",
                        severity="warning",
                        line=i,
                        title=f"Imbrication excessive (niveau {indent_level})",
                        description=(
                            f"L'imbrication de {indent_level} niveaux rend le code difficile à lire. "
                            "Recommandé: < 4 niveaux."
                        ),
                    )
                )

        # Dernière fonction
        if func_name and func_complexity > 10:
            findings.append(
                ReviewFinding(
                    category="complexity",
                    severity="error" if func_complexity > 15 else "warning",
                    line=func_start,
                    title=f"Complexité élevée: '{func_name}' (score={func_complexity})",
                    description=(
                        f"La fonction '{func_name}' a une complexité cyclomatique de {func_complexity}. "
                        "Recommandé: < 10."
                    ),
                )
            )

        return findings

    def analyze_security(self, code: str, language: str) -> List[ReviewFinding]:
        """Détecte les problèmes de sécurité."""
        findings = []
        lang = language.lower()
        patterns = SECURITY_PATTERNS.get(lang, [])

        for pattern in patterns:
            for i, line in enumerate(code.split("\n"), 1):
                if re.search(pattern, line):
                    findings.append(
                        ReviewFinding(
                            category="security",
                            severity="critical" if "password" in pattern or "secret" in pattern else "error",
                            line=i,
                            title="Problème de sécurité détecté",
                            description=f"Pattern dangereux détecté: '{line.strip()}'",
                        )
                    )

        return findings

    def analyze_dry(self, code: str, language: str) -> List[ReviewFinding]:
        """Détecte la duplication de code."""
        findings = []
        lines = [line.strip() for line in code.split("\n") if line.strip()]

        seen = {}
        for i, line in enumerate(lines):
            if len(line) > 20 and not line.startswith("#") and not line.startswith("//"):
                if line in seen:
                    findings.append(
                        ReviewFinding(
                            category="dry",
                            severity="warning",
                            line=i + 1,
                            title="Code dupliqué",
                            description=(
                                f"Ligne identique trouvée aux lignes {seen[line]} et {i + 1}: '{line[:60]}...'"
                            ),
                        )
                    )
                else:
                    seen[line] = i + 1

        return findings

    def analyze_error_handling(self, code: str, language: str) -> List[ReviewFinding]:
        """Analyse la gestion des erreurs."""
        findings = []
        lang = language.lower()

        if lang == "python":
            for i, line in enumerate(code.split("\n"), 1):
                stripped = line.strip()
                if stripped == "except:" or stripped == "except Exception:":
                    findings.append(
                        ReviewFinding(
                            category="error_handling",
                            severity="warning",
                            line=i,
                            title="Except trop large",
                            description="Catch trop générique. Attrapez des exceptions spécifiques.",
                        )
                    )
                if "pass" == stripped and i > 1:
                    prev_line = code.split("\n")[i - 2].strip()
                    if prev_line.startswith("except"):
                        findings.append(
                            ReviewFinding(
                                category="error_handling",
                                severity="error",
                                line=i,
                                title="Exception silencieuse",
                                description="Exception attrapée et ignorée (pass). Loggez ou re-lancez l'erreur.",
                            )
                        )

        return findings

    def calculate_quality_score(self, findings: List[ReviewFinding], total_lines: int) -> float:
        """Calcule le score de qualité global."""
        if total_lines == 0:
            return 1.0

        penalty = 0.0
        for f in findings:
            weight = SEVERITY_WEIGHTS.get(f.severity, 0.1)
            penalty += weight

        # Normaliser par la taille du code (codes plus grands tolèrent plus de findings)
        size_factor = max(1.0, total_lines / 50.0)
        normalized_penalty = penalty / size_factor

        return max(0.0, min(1.0, 1.0 - normalized_penalty))

    def calculate_category_scores(self, findings: List[ReviewFinding], standards: List[str]) -> Dict[str, float]:
        """Calcule un score par catégorie."""
        scores = {}
        for std in standards:
            category_findings = [f for f in findings if f.category == std]
            if not category_findings:
                scores[std] = 1.0
            else:
                penalty = sum(SEVERITY_WEIGHTS.get(f.severity, 0.1) for f in category_findings)
                scores[std] = max(0.0, min(1.0, 1.0 - penalty))
        return scores

    def identify_strengths(self, code: str, language: str) -> List[str]:
        """Identifie les points forts du code."""
        strengths = []
        lines = code.split("\n")

        if language.lower() == "python":
            has_type_hints = any(":" in line and "->" in line for line in lines if "def " in line)
            if has_type_hints:
                strengths.append("Utilisation de type hints")

            has_docstrings = '"""' in code or "'''" in code
            if has_docstrings:
                strengths.append("Documentation avec docstrings")

        has_error_handling = any("try" in line for line in lines)
        if has_error_handling:
            strengths.append("Gestion des erreurs présente")

        func_count = sum(1 for line in lines if line.strip().startswith("def ") or "function " in line)
        if func_count > 0:
            avg_lines = len(lines) / func_count
            if avg_lines < 30:
                strengths.append("Fonctions courtes et focalisées")

        return strengths

    @staticmethod
    def _to_snake_case(name: str) -> str:
        s = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", name)
        s = re.sub(r"([a-z\d])([A-Z])", r"\1_\2", s)
        return s.lower()
