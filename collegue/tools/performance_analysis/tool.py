"""
Performance Analysis — Expert IA en analyse de performance.

Cet outil détecte les problèmes de performance, analyse la complexité
algorithmique et propose des optimisations.
"""

from typing import Any, Dict, List

from ...core.shared import parse_llm_json_response
from ..agent_loop import AgentLoopConfig, AgentLoopMixin
from ..base import BaseTool
from .config import ANALYSIS_CATEGORIES
from .engine import PerformanceEngine
from .models import PerformanceAnalysisRequest, PerformanceAnalysisResponse, PerformanceIssue


class PerformanceAnalysisTool(AgentLoopMixin, BaseTool):
    """
    Expert IA en analyse de performance.

    Détecte les patterns inefficaces, analyse la complexité algorithmique,
    identifie les problèmes de mémoire et d'I/O, et propose des optimisations.
    """

    tool_name = "performance_analysis"
    tool_description = (
        "Analyse les performances du code : complexité algorithmique, patterns "
        "inefficaces, problèmes de mémoire et d'I/O.\n\n"
        "PARAMÈTRES REQUIS:\n"
        "- code: Le code source à analyser.\n"
        "- language: Le langage de programmation (python, javascript, typescript).\n\n"
        "PARAMÈTRES OPTIONNELS:\n"
        "- analysis_categories: Catégories (cpu, memory, io, algorithmic, parallelism).\n"
        "- context: Contexte (taille des données, fréquence d'appel, etc.).\n\n"
        "RETOURNE:\n"
        "- performance_score: Score global (0.0-1.0)\n"
        "- issues: Problèmes détectés avec complexité estimée\n"
        "- hotspots: Points chauds du code\n"
        "- optimizations: Suggestions d'optimisation"
    )
    tags = {"analysis", "performance"}
    request_model = PerformanceAnalysisRequest
    response_model = PerformanceAnalysisResponse
    supported_languages = ["python", "javascript", "typescript"]

    agent_config = AgentLoopConfig(
        max_iterations=3,
        initial_temperature=0.5,
        temperature_decay=0.1,
        min_temperature=0.2,
    )

    def __init__(self, config=None, app_state=None):
        super().__init__(config, app_state)
        self._engine = PerformanceEngine(logger=self.logger)

    # --- AgentLoopMixin hooks ---

    async def validate_agent_output(self, output: str, context: Dict[str, Any]) -> List[str]:
        errors = []
        if not output or len(output.strip()) < 20:
            errors.append("Analyse vide ou trop courte")
            return errors

        lower = output.lower()
        if "performance" not in lower and "score" not in lower and "issue" not in lower:
            errors.append("L'analyse ne contient pas de score ni de problèmes structurés")

        return errors

    async def assess_agent_quality(self, output: str, context: Dict[str, Any]) -> float:
        if not output or len(output.strip()) < 20:
            return 0.0

        score = 0.3
        lower = output.lower()

        if "```json" in lower or '"issues"' in lower:
            score += 0.3
        if "complexity" in lower or "o(n" in lower:
            score += 0.2
        if "optimization" in lower or "suggestion" in lower:
            score += 0.2

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
                "performance_score, issues (avec category, severity, line, title, "
                "description, estimated_complexity, suggestion), "
                "hotspots, optimizations."
            )

        return "\n".join(parts) if parts else "Affine l'analyse de performance."

    def get_usage_description(self) -> str:
        return (
            "Expert en analyse de performance. Détecte les patterns inefficaces, "
            "analyse la complexité algorithmique et propose des optimisations."
        )

    def get_examples(self) -> List[Dict[str, Any]]:
        return [
            {
                "title": "Analyse performance Python",
                "request": {
                    "code": "for i in items:\n    for j in items:\n        if i == j:\n            pass",
                    "language": "python",
                    "analysis_categories": ["algorithmic", "cpu"],
                },
            }
        ]

    def get_capabilities(self) -> List[str]:
        return [
            "Détection de patterns inefficaces (O(n²), allocations, I/O bloquant)",
            "Analyse de complexité algorithmique (Big-O)",
            "Détection de fuites mémoire potentielles",
            "Identification de hotspots de performance",
            "Suggestions d'optimisation avec code proposé",
            "Boucle agentique pour analyse approfondie",
        ]

    def _build_performance_prompt(self, request: PerformanceAnalysisRequest) -> str:
        """Construit le prompt pour l'analyse LLM."""
        cats_desc = "\n".join(f"- {cat}: {ANALYSIS_CATEGORIES.get(cat, cat)}" for cat in request.analysis_categories)

        return f"""Effectue une analyse de performance approfondie de ce code {request.language}.

Catégories d'analyse:
{cats_desc}

```{request.language}
{request.code}
```

{f"Contexte: {request.context}" if request.context else ""}

Réponds en JSON avec cette structure exacte:
{{
  "performance_score": 0.6,
  "issues": [
    {{
      "category": "algorithmic",
      "severity": "warning",
      "line": 5,
      "title": "Titre",
      "description": "Description",
      "estimated_complexity": "O(n²)",
      "suggestion": "Code optimisé"
    }}
  ],
  "hotspots": [{{"line": 5, "score": 0.8, "reason": "Boucle imbriquée"}}],
  "optimizations": ["Optimisation 1"]
}}"""

    def _execute_core_logic(self, request: PerformanceAnalysisRequest, **kwargs) -> PerformanceAnalysisResponse:
        """Exécute l'analyse de performance (synchrone)."""
        total_lines = len(request.code.split("\n"))
        all_issues: List[PerformanceIssue] = []
        categories = request.analysis_categories

        # Détection de patterns inefficaces
        all_issues.extend(self._engine.detect_inefficient_patterns(request.code, request.language))

        # Complexité algorithmique
        if "algorithmic" in categories:
            all_issues.extend(self._engine.analyze_algorithmic_complexity(request.code, request.language))

        # Problèmes mémoire
        if "memory" in categories:
            all_issues.extend(self._engine.detect_memory_issues(request.code, request.language))

        # Problèmes I/O
        if "io" in categories:
            all_issues.extend(self._engine.detect_io_issues(request.code, request.language))

        # Filtrer par catégorie demandée
        all_issues = [i for i in all_issues if i.category in categories]

        perf_score = self._engine.calculate_performance_score(all_issues, total_lines)
        cat_scores = self._engine.calculate_category_scores(all_issues, categories)
        hotspots = self._engine.identify_hotspots(request.code, request.language, all_issues)

        optimizations = []
        for issue in all_issues:
            if issue.suggestion:
                optimizations.append(f"{issue.title}: {issue.suggestion}")
        if perf_score < 0.5:
            optimizations.append("Refactoring recommandé pour améliorer les performances")

        summary = (
            f"Analyse de performance de {total_lines} lignes {request.language}. "
            f"Score: {perf_score:.2f}/1.0. "
            f"{len(all_issues)} problème(s), {len(hotspots)} hotspot(s)."
        )

        response = PerformanceAnalysisResponse(
            performance_score=perf_score,
            issues=all_issues,
            category_scores=cat_scores,
            hotspots=hotspots,
            optimizations=optimizations,
            summary=summary,
            language=request.language,
            lines_analyzed=total_lines,
        )

        self._store_to_memory(
            entry_type="expert_result",
            category="performance",
            title=f"Performance: score {perf_score:.2f}, {len(all_issues)} issues",
            data={"issues_count": len(all_issues), "hotspots_count": len(hotspots)},
            score=perf_score,
            language=request.language,
        )
        for issue in all_issues:
            if issue.severity in ("critical", "error"):
                self._store_to_memory(
                    entry_type="issue_found",
                    category=issue.category,
                    title=issue.title,
                    data={"severity": issue.severity, "complexity": issue.estimated_complexity},
                    language=request.language,
                )

        return response

    async def _execute_core_logic_async(
        self, request: PerformanceAnalysisRequest, **kwargs
    ) -> PerformanceAnalysisResponse:
        """Version asynchrone avec boucle agentique."""
        ctx = kwargs.get("ctx")

        if ctx:
            await ctx.info("Analyse statique des performances...")

        import asyncio

        local_result = await asyncio.to_thread(self._execute_core_logic, request)

        if not ctx:
            return local_result

        try:
            if ctx:
                await ctx.info("Analyse de performance agentique en cours...")

            memory_ctx = self._recall_from_memory(language=request.language)
            prompt = self._build_performance_prompt(request)
            if memory_ctx:
                memory_info = "\n".join(f"- {k}: {v}" for k, v in memory_ctx.items())
                prompt += f"\n\nContexte historique du projet:\n{memory_info}"

            sys_prompt = (
                f"Tu es un expert senior en optimisation de performance {request.language}. "
                "Tu détectes les bottlenecks, analyses la complexité Big-O et proposes des optimisations. "
                "Réponds UNIQUEMENT en JSON valide."
            )

            agent_result = await self.agent_execute(
                initial_prompt=prompt,
                system_prompt=sys_prompt,
                ctx=ctx,
                context={
                    "language": request.language,
                    "categories": request.analysis_categories,
                    "local_issues_count": len(local_result.issues),
                },
                max_tokens=3000,
            )

            llm_issues, llm_hotspots, llm_opts = self._parse_llm_performance(agent_result.best_output)

            # Fusionner
            merged_issues = list(local_result.issues)
            existing_titles = {i.title for i in merged_issues}
            for i in llm_issues:
                if i.title not in existing_titles:
                    merged_issues.append(i)
                    existing_titles.add(i.title)

            total_lines = len(request.code.split("\n"))
            perf_score = self._engine.calculate_performance_score(merged_issues, total_lines)
            cat_scores = self._engine.calculate_category_scores(merged_issues, request.analysis_categories)
            hotspots = self._engine.identify_hotspots(request.code, request.language, merged_issues)
            merged_opts = list(set(local_result.optimizations + llm_opts))

            summary = (
                f"Analyse de performance de {total_lines} lignes {request.language}. "
                f"Score: {perf_score:.2f}/1.0. "
                f"{len(merged_issues)} problème(s) "
                f"({len(local_result.issues)} statique + {len(llm_issues)} LLM)."
            )

            response = PerformanceAnalysisResponse(
                performance_score=perf_score,
                issues=merged_issues,
                category_scores=cat_scores,
                hotspots=hotspots,
                optimizations=merged_opts,
                summary=summary,
                language=request.language,
                lines_analyzed=total_lines,
                agent_iterations=agent_result.total_iterations,
                agent_best_score=agent_result.best_score,
                agent_converged=agent_result.converged,
            )

            # Stocker le résultat final (enrichi LLM) en mémoire
            self._store_to_memory(
                entry_type="expert_result",
                category="performance",
                title=f"Analyse perf agentique: score {perf_score:.2f}",
                data={"issues_count": len(merged_issues), "category_scores": cat_scores},
                score=perf_score,
                language=request.language,
            )
            for issue in merged_issues:
                if issue.severity in ("critical", "error"):
                    self._store_to_memory(
                        entry_type="issue_found",
                        category=issue.category,
                        title=issue.title,
                        data={"severity": issue.severity, "description": issue.description},
                        language=request.language,
                    )

            return response

        except Exception as e:
            self.logger.warning(f"Fallback analyse statique suite à erreur LLM: {e}")
            return local_result

    def _parse_llm_performance(self, output: str) -> tuple[list[PerformanceIssue], list[dict], list[str]]:
        """Parse le JSON de l'analyse LLM."""
        issues: list[PerformanceIssue] = []
        hotspots: list[dict] = []
        optimizations: list[str] = []

        try:
            data = parse_llm_json_response(output)
            if not isinstance(data, dict):
                return issues, hotspots, optimizations

            for i in data.get("issues", []):
                if isinstance(i, dict) and "title" in i:
                    issues.append(
                        PerformanceIssue(
                            category=i.get("category", "cpu"),
                            severity=i.get("severity", "info"),
                            line=i.get("line"),
                            title=i["title"],
                            description=i.get("description", ""),
                            estimated_complexity=i.get("estimated_complexity"),
                            suggestion=i.get("suggestion"),
                        )
                    )

            hotspots = data.get("hotspots", [])
            if not isinstance(hotspots, list):
                hotspots = []

            optimizations = data.get("optimizations", [])
            if not isinstance(optimizations, list):
                optimizations = []

        except Exception:
            pass

        return issues, hotspots, optimizations
