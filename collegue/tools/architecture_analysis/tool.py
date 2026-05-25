"""
Architecture Analysis — Expert IA en analyse architecturale.

Cet outil analyse l'architecture d'un projet, détecte la dette technique,
identifie les patterns et évalue le couplage/cohésion.
"""

from typing import Any, Dict, List

from ...core.shared import parse_llm_json_response
from ..agent_loop import AgentLoopConfig, AgentLoopMixin
from ..base import BaseTool
from .config import ANALYSIS_TYPES
from .engine import ArchitectureEngine
from .models import (
    ArchitecturalIssue,
    ArchitectureAnalysisRequest,
    ArchitectureAnalysisResponse,
)


class ArchitectureAnalysisTool(AgentLoopMixin, BaseTool):
    """
    Expert IA en analyse architecturale.

    Analyse les dépendances, le couplage, la cohésion, détecte les patterns
    architecturaux et la dette technique.
    """

    tool_name = "architecture_analysis"
    tool_description = (
        "Analyse l'architecture d'un projet : dépendances, couplage, cohésion, "
        "patterns et dette technique.\n\n"
        "PARAMÈTRES REQUIS:\n"
        "- code: Le code source à analyser.\n"
        "- language: Le langage de programmation (python, javascript, typescript, php).\n\n"
        "PARAMÈTRES OPTIONNELS:\n"
        "- analysis_types: Types d'analyse (dependencies, coupling, cohesion, patterns, debt).\n"
        "- context: Contexte additionnel sur le projet.\n\n"
        "RETOURNE:\n"
        "- architecture_score: Score global (0.0-1.0)\n"
        "- detected_patterns: Patterns architecturaux trouvés\n"
        "- dependencies: Graphe de dépendances\n"
        "- issues: Problèmes architecturaux avec sévérité\n"
        "- debt_score: Score de dette technique\n"
        "- recommendations: Recommandations architecturales"
    )
    tags = {"analysis", "architecture"}
    request_model = ArchitectureAnalysisRequest
    response_model = ArchitectureAnalysisResponse
    supported_languages = ["python", "javascript", "typescript", "php"]

    agent_config = AgentLoopConfig(
        max_iterations=3,
        initial_temperature=0.5,
        temperature_decay=0.1,
        min_temperature=0.2,
    )

    def __init__(self, config=None, app_state=None):
        super().__init__(config, app_state)
        self._engine = ArchitectureEngine(logger=self.logger)

    # --- AgentLoopMixin hooks ---

    async def validate_agent_output(self, output: str, context: Dict[str, Any]) -> List[str]:
        """Valide que le LLM a produit une analyse structurée."""
        errors = []
        if not output or len(output.strip()) < 30:
            errors.append("Analyse vide ou trop courte")
            return errors

        lower = output.lower()
        if "architecture" not in lower and "score" not in lower and "pattern" not in lower:
            errors.append("L'analyse ne contient pas de score ni de patterns")

        return errors

    async def assess_agent_quality(self, output: str, context: Dict[str, Any]) -> float:
        """Évalue la qualité de l'analyse."""
        if not output or len(output.strip()) < 30:
            return 0.0

        score = 0.3
        lower = output.lower()

        if "```json" in lower or '"issues"' in lower:
            score += 0.25
        if "pattern" in lower or "architecture" in lower:
            score += 0.2
        if "debt" in lower or "coupling" in lower or "cohesion" in lower:
            score += 0.15
        if "recommendation" in lower:
            score += 0.1

        return min(1.0, score)

    async def build_agent_feedback(
        self, output: str, errors: List[str], quality: float, context: Dict[str, Any]
    ) -> str:
        parts = []
        for error in errors:
            parts.append(f"PROBLÈME: {error}")

        if quality < 0.7:
            parts.append(
                "Améliore l'analyse en fournissant un JSON structuré avec: "
                "architecture_score, detected_patterns, issues, debt_score, recommendations."
            )

        return "\n".join(parts) if parts else "Affine l'analyse architecturale."

    def get_usage_description(self) -> str:
        return (
            "Expert en analyse architecturale. Évalue le couplage, la cohésion, "
            "détecte les patterns et la dette technique."
        )

    def get_examples(self) -> List[Dict[str, Any]]:
        return [
            {
                "title": "Analyse architecture Python",
                "request": {
                    "code": "from db import get_conn\nclass UserService:\n    pass",
                    "language": "python",
                    "analysis_types": ["dependencies", "patterns", "debt"],
                },
            }
        ]

    def get_capabilities(self) -> List[str]:
        return [
            "Analyse de dépendances et détection de cycles",
            "Évaluation du couplage et de la cohésion",
            "Détection de patterns architecturaux (MVC, Clean Architecture, etc.)",
            "Identification de dette technique (God Class, etc.)",
            "Métriques architecturales (LOC, fan-in/fan-out, profondeur d'héritage)",
            "Boucle agentique pour analyse approfondie",
        ]

    def _build_analysis_prompt(self, request: ArchitectureAnalysisRequest) -> str:
        """Construit le prompt pour l'analyse LLM."""
        types_desc = "\n".join(f"- {t}: {ANALYSIS_TYPES.get(t, t)}" for t in request.analysis_types)

        return f"""Effectue une analyse architecturale approfondie de ce code {request.language}.

Types d'analyse demandés:
{types_desc}

```{request.language}
{request.code}
```

{f"Contexte: {request.context}" if request.context else ""}

Réponds en JSON avec cette structure exacte:
{{
  "architecture_score": 0.65,
  "detected_patterns": ["Repository Pattern", "Service Layer"],
  "issues": [
    {{
      "category": "high_coupling",
      "severity": "warning",
      "title": "Titre",
      "description": "Description",
      "affected_modules": ["module1"],
      "recommendation": "Recommandation"
    }}
  ],
  "debt_score": 0.3,
  "recommendations": ["Recommandation 1"]
}}"""

    def _execute_core_logic(self, request: ArchitectureAnalysisRequest, **kwargs) -> ArchitectureAnalysisResponse:
        """Exécute l'analyse architecturale (synchrone)."""
        all_issues: List[ArchitecturalIssue] = []
        analysis = request.analysis_types

        # Dépendances
        dependencies = self._engine.extract_dependencies(request.code, request.language)

        if "circular_deps" in analysis or "dependencies" in analysis:
            circular_issues = self._engine.detect_circular_dependencies(dependencies)
            all_issues.extend(circular_issues)

        # Couplage
        coupling_score = 0.0
        if "coupling" in analysis:
            coupling_score, coupling_issues = self._engine.analyze_coupling(request.code, request.language)
            all_issues.extend(coupling_issues)

        # Cohésion
        cohesion_score = 0.5
        if "cohesion" in analysis:
            cohesion_score, cohesion_issues = self._engine.analyze_cohesion(request.code, request.language)
            all_issues.extend(cohesion_issues)

        # Patterns
        patterns = []
        if "patterns" in analysis:
            patterns = self._engine.detect_patterns(request.code, request.language)

        # Métriques
        metrics = {}
        if "metrics" in analysis:
            metrics = self._engine.calculate_metrics(request.code, request.language)

        # Scores
        debt_score = self._engine.calculate_debt_score(all_issues)
        arch_score = self._engine.calculate_architecture_score(coupling_score, cohesion_score, debt_score, all_issues)

        recommendations = []
        if debt_score > 0.5:
            recommendations.append("Refactoring recommandé pour réduire la dette technique")
        if coupling_score > 0.5:
            recommendations.append("Réduire le couplage en introduisant des abstractions")
        if cohesion_score < 0.4:
            recommendations.append("Améliorer la cohésion en regroupant les responsabilités")

        summary = (
            f"Analyse architecturale de {len(request.code.split(chr(10)))} lignes {request.language}. "
            f"Score: {arch_score:.2f}/1.0. "
            f"{len(all_issues)} problème(s), dette: {debt_score:.2f}. "
            f"Patterns: {', '.join(patterns) if patterns else 'aucun détecté'}."
        )

        return ArchitectureAnalysisResponse(
            architecture_score=arch_score,
            detected_patterns=patterns,
            dependencies=dependencies,
            issues=all_issues,
            metrics=metrics,
            debt_score=debt_score,
            recommendations=recommendations,
            summary=summary,
            language=request.language,
        )

    async def _execute_core_logic_async(
        self, request: ArchitectureAnalysisRequest, **kwargs
    ) -> ArchitectureAnalysisResponse:
        """Version asynchrone avec boucle agentique."""
        ctx = kwargs.get("ctx")

        if ctx:
            await ctx.info("Analyse statique de l'architecture...")

        import asyncio

        local_result = await asyncio.to_thread(self._execute_core_logic, request)

        if not ctx:
            return local_result

        try:
            if ctx:
                await ctx.info("Analyse architecturale agentique en cours...")

            prompt = self._build_analysis_prompt(request)
            sys_prompt = (
                f"Tu es un architecte logiciel senior expert en {request.language}. "
                "Tu analyses l'architecture, les patterns et la dette technique. "
                "Réponds UNIQUEMENT en JSON valide."
            )

            agent_result = await self.agent_execute(
                initial_prompt=prompt,
                system_prompt=sys_prompt,
                ctx=ctx,
                context={
                    "language": request.language,
                    "analysis_types": request.analysis_types,
                    "local_issues_count": len(local_result.issues),
                },
                max_tokens=3000,
            )

            llm_issues, llm_patterns, llm_debt, llm_recs = self._parse_llm_analysis(agent_result.best_output)

            # Fusionner
            merged_issues = list(local_result.issues)
            existing_titles = {i.title for i in merged_issues}
            for i in llm_issues:
                if i.title not in existing_titles:
                    merged_issues.append(i)
                    existing_titles.add(i.title)

            merged_patterns = list(set(local_result.detected_patterns + llm_patterns))
            merged_recs = list(set(local_result.recommendations + llm_recs))

            debt_score = max(local_result.debt_score, llm_debt)
            coupling_score, _ = self._engine.analyze_coupling(request.code, request.language)
            cohesion_score, _ = self._engine.analyze_cohesion(request.code, request.language)
            arch_score = self._engine.calculate_architecture_score(
                coupling_score, cohesion_score, debt_score, merged_issues
            )

            summary = (
                f"Analyse architecturale de {len(request.code.split(chr(10)))} lignes {request.language}. "
                f"Score: {arch_score:.2f}/1.0. "
                f"{len(merged_issues)} problème(s) "
                f"({len(local_result.issues)} statique + {len(llm_issues)} LLM). "
                f"Patterns: {', '.join(merged_patterns) if merged_patterns else 'aucun'}."
            )

            return ArchitectureAnalysisResponse(
                architecture_score=arch_score,
                detected_patterns=merged_patterns,
                dependencies=local_result.dependencies,
                issues=merged_issues,
                metrics=local_result.metrics,
                debt_score=debt_score,
                recommendations=merged_recs,
                summary=summary,
                language=request.language,
                agent_iterations=agent_result.total_iterations,
                agent_best_score=agent_result.best_score,
                agent_converged=agent_result.converged,
            )

        except Exception as e:
            self.logger.warning(f"Fallback analyse statique suite à erreur LLM: {e}")
            return local_result

    def _parse_llm_analysis(self, output: str) -> tuple[list[ArchitecturalIssue], list[str], float, list[str]]:
        """Parse le JSON de l'analyse LLM."""
        issues: list[ArchitecturalIssue] = []
        patterns: list[str] = []
        debt_score = 0.0
        recommendations: list[str] = []

        try:
            data = parse_llm_json_response(output)
            if not isinstance(data, dict):
                return issues, patterns, debt_score, recommendations

            debt_score = float(data.get("debt_score", 0.0))

            for i in data.get("issues", []):
                if isinstance(i, dict) and "title" in i:
                    issues.append(
                        ArchitecturalIssue(
                            category=i.get("category", "missing_abstraction"),
                            severity=i.get("severity", "info"),
                            title=i["title"],
                            description=i.get("description", ""),
                            affected_modules=i.get("affected_modules", []),
                            recommendation=i.get("recommendation"),
                        )
                    )

            patterns = data.get("detected_patterns", [])
            if not isinstance(patterns, list):
                patterns = []

            recommendations = data.get("recommendations", [])
            if not isinstance(recommendations, list):
                recommendations = []

        except Exception:
            pass

        return issues, patterns, debt_score, recommendations
