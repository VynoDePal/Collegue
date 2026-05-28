"""
Code Review — Expert IA en revue de code automatique.

Cet outil effectue des revues de code automatiques avec standards configurables.
Il détecte les problèmes de naming, complexité, sécurité, performance,
duplication et gestion des erreurs.
"""

from typing import Any, Dict, List

from ...core.llm_response_parser import LLMCodeReviewResponse, parse_llm_response_strict
from ..agent_loop import AgentLoopConfig, AgentLoopMixin
from ..base import BaseTool
from .config import REVIEW_STANDARDS
from .engine import CodeReviewEngine
from .models import CodeReviewRequest, CodeReviewResponse, ReviewFinding


class CodeReviewTool(AgentLoopMixin, BaseTool):
    """
    Expert IA en revue de code automatique.

    Analyse le code selon des standards configurables (naming, complexity,
    security, DRY, SOLID, error_handling) et produit un score de qualité
    avec des recommandations actionnables.
    """

    tool_name = "code_review"
    tool_description = (
        "Effectue une revue de code automatique avec standards configurables.\n\n"
        "PARAMÈTRES REQUIS:\n"
        "- code: Le code source à reviewer.\n"
        "- language: Le langage de programmation (python, javascript, typescript, php).\n\n"
        "PARAMÈTRES OPTIONNELS:\n"
        "- review_standards: Standards à vérifier (naming, complexity, security, performance, dry, solid).\n"
        "- severity_threshold: Sévérité minimale à reporter (info, warning, error, critical).\n"
        "- context: Contexte additionnel (PR description, ticket, etc.).\n\n"
        "RETOURNE:\n"
        "- quality_score: Score global (0.0-1.0)\n"
        "- findings: Liste des problèmes détectés avec sévérité et suggestions\n"
        "- category_scores: Score détaillé par catégorie\n"
        "- strengths: Points forts du code\n"
        "- recommendations: Recommandations d'amélioration"
    )
    tags = {"analysis", "quality"}
    request_model = CodeReviewRequest
    response_model = CodeReviewResponse
    supported_languages = ["python", "javascript", "typescript", "php"]

    agent_config = AgentLoopConfig(
        max_iterations=3,
        initial_temperature=0.5,
        temperature_decay=0.1,
        min_temperature=0.2,
    )

    def __init__(self, config=None, app_state=None):
        super().__init__(config, app_state)
        self._engine = CodeReviewEngine(logger=self.logger)

    # --- AgentLoopMixin hooks ---

    async def validate_agent_output(self, output: str, context: Dict[str, Any]) -> List[str]:
        """Valide que le LLM a produit une revue structurée."""
        errors = []
        if not output or len(output.strip()) < 20:
            errors.append("Revue vide ou trop courte")
            return errors

        lower = output.lower()
        if "quality" not in lower and "score" not in lower and "finding" not in lower:
            errors.append("La revue ne contient pas de score ni de findings structurés")

        return errors

    async def assess_agent_quality(self, output: str, context: Dict[str, Any]) -> float:
        """Évalue la qualité de la revue produite."""
        if not output or len(output.strip()) < 20:
            return 0.0

        score = 0.3

        lower = output.lower()
        if "```json" in lower or '"findings"' in lower:
            score += 0.3
        if "score" in lower or "quality" in lower:
            score += 0.2
        if "recommendation" in lower or "suggestion" in lower:
            score += 0.2

        return min(1.0, score)

    async def build_agent_feedback(
        self, output: str, errors: List[str], quality: float, context: Dict[str, Any]
    ) -> str:
        """Construit un feedback pour améliorer la revue."""
        parts = []
        for error in errors:
            parts.append(f"PROBLÈME: {error}")

        if quality < 0.7:
            parts.append(
                "Améliore la revue en fournissant un JSON structuré avec: "
                "quality_score (0.0-1.0), findings (liste de problèmes avec "
                "category, severity, line, title, description, suggestion), "
                "strengths (points forts), recommendations (liste)."
            )

        return "\n".join(parts) if parts else "Affine la revue avec plus de détails."

    def get_usage_description(self) -> str:
        return (
            "Expert en revue de code automatique. Analyse le code selon des standards "
            "configurables et produit un score de qualité avec des recommandations."
        )

    def get_examples(self) -> List[Dict[str, Any]]:
        return [
            {
                "title": "Revue de code Python",
                "request": {
                    "code": "def calc(a,b):\n  x=a+b\n  return x",
                    "language": "python",
                    "review_standards": ["naming", "complexity"],
                },
            }
        ]

    def get_capabilities(self) -> List[str]:
        return [
            "Revue de code multi-langages (Python, JS/TS, PHP)",
            "Standards configurables: naming, complexity, security, DRY, SOLID",
            "Score de qualité global et par catégorie",
            "Détection de code smells et anti-patterns",
            "Suggestions de correction avec code proposé",
            "Boucle agentique pour trouver plus de problèmes",
        ]

    def _build_review_prompt(self, request: CodeReviewRequest) -> str:
        """Construit le prompt pour la revue LLM."""
        standards_desc = "\n".join(f"- {std}: {REVIEW_STANDARDS.get(std, std)}" for std in request.review_standards)

        prompt = f"""Effectue une revue de code approfondie de ce code {request.language}.

Standards à vérifier:
{standards_desc}

Sévérité minimale: {request.severity_threshold}

```{request.language}
{request.code}
```

{f"Contexte: {request.context}" if request.context else ""}

Réponds en JSON avec cette structure exacte:
{{
  "quality_score": 0.75,
  "findings": [
    {{
      "category": "naming",
      "severity": "warning",
      "line": 1,
      "title": "Titre court",
      "description": "Description détaillée",
      "suggestion": "Code corrigé proposé"
    }}
  ],
  "strengths": ["Point fort 1"],
  "recommendations": ["Recommandation 1"]
}}"""
        return prompt

    def _execute_core_logic(self, request: CodeReviewRequest, **kwargs) -> CodeReviewResponse:
        """Exécute la revue de code (synchrone)."""
        total_lines = len(request.code.split("\n"))

        # Analyse statique locale
        all_findings = []
        standards = request.review_standards

        if "naming" in standards:
            all_findings.extend(self._engine.analyze_naming(request.code, request.language))
        if "complexity" in standards:
            all_findings.extend(self._engine.analyze_complexity(request.code, request.language))
        if "security" in standards:
            all_findings.extend(self._engine.analyze_security(request.code, request.language))
        if "dry" in standards:
            all_findings.extend(self._engine.analyze_dry(request.code, request.language))
        if "error_handling" in standards:
            all_findings.extend(self._engine.analyze_error_handling(request.code, request.language))

        # Filtrer par sévérité
        severity_order = ["info", "warning", "error", "critical"]
        threshold_idx = severity_order.index(request.severity_threshold)
        all_findings = [f for f in all_findings if severity_order.index(f.severity) >= threshold_idx]

        quality_score = self._engine.calculate_quality_score(all_findings, total_lines)
        category_scores = self._engine.calculate_category_scores(all_findings, standards)
        strengths = self._engine.identify_strengths(request.code, request.language)

        recommendations = []
        if quality_score < 0.5:
            recommendations.append("Refactoring recommandé pour améliorer la qualité globale")
        critical_count = sum(1 for f in all_findings if f.severity == "critical")
        if critical_count > 0:
            recommendations.append(f"{critical_count} problème(s) critique(s) à corriger immédiatement")

        summary = (
            f"Revue de {total_lines} lignes de code {request.language}. "
            f"Score: {quality_score:.2f}/1.0. "
            f"{len(all_findings)} problème(s) détecté(s)."
        )

        response = CodeReviewResponse(
            quality_score=quality_score,
            findings=all_findings,
            summary=summary,
            category_scores=category_scores,
            strengths=strengths,
            recommendations=recommendations,
            language=request.language,
            lines_reviewed=total_lines,
        )

        # Stocker les résultats en mémoire
        self._store_to_memory(
            entry_type="expert_result",
            category="code_review",
            title=f"Revue: {total_lines} lignes, score {quality_score:.2f}",
            data={"findings_count": len(all_findings), "category_scores": category_scores},
            score=quality_score,
            language=request.language,
        )
        for finding in all_findings:
            if finding.severity in ("critical", "error"):
                self._store_to_memory(
                    entry_type="issue_found",
                    category=finding.category,
                    title=finding.title,
                    data={"severity": finding.severity, "description": finding.description},
                    language=request.language,
                )

        return response

    async def _execute_core_logic_async(self, request: CodeReviewRequest, **kwargs) -> CodeReviewResponse:
        """Version asynchrone avec boucle agentique."""
        ctx = kwargs.get("ctx")

        if ctx:
            await ctx.info("Analyse statique du code...")

        # Analyse statique locale d'abord
        import asyncio

        local_result = await asyncio.to_thread(self._execute_core_logic, request)

        if not ctx:
            return local_result

        # Enrichissement LLM via boucle agentique
        try:
            if ctx:
                await ctx.info("Revue agentique en cours...")

            # Enrichir le prompt avec le contexte mémoire
            memory_ctx = self._recall_from_memory(language=request.language)
            prompt = self._build_review_prompt(request)
            if memory_ctx:
                memory_info = "\n".join(f"- {k}: {v}" for k, v in memory_ctx.items())
                prompt += f"\n\nContexte historique du projet:\n{memory_info}"
            sys_prompt = (
                f"Tu es un expert senior en revue de code {request.language}. "
                "Tu effectues des revues rigoureuses et constructives. "
                "Réponds UNIQUEMENT en JSON valide."
            )

            agent_result = await self.agent_execute(
                initial_prompt=prompt,
                system_prompt=sys_prompt,
                ctx=ctx,
                context={
                    "language": request.language,
                    "standards": request.review_standards,
                    "local_findings_count": len(local_result.findings),
                },
                max_tokens=3000,
            )

            # Parser le résultat LLM
            llm_findings, llm_score, llm_strengths, llm_recs = self._parse_llm_review(agent_result.best_output)

            # Fusionner findings locaux et LLM (dédupliqués)
            merged_findings = list(local_result.findings)
            existing_titles = {f.title for f in merged_findings}
            for f in llm_findings:
                if f.title not in existing_titles:
                    merged_findings.append(f)
                    existing_titles.add(f.title)

            # Recalculer les scores avec tous les findings
            total_lines = len(request.code.split("\n"))
            quality_score = self._engine.calculate_quality_score(merged_findings, total_lines)
            category_scores = self._engine.calculate_category_scores(merged_findings, request.review_standards)

            merged_strengths = list(set(local_result.strengths + llm_strengths))
            merged_recs = list(set(local_result.recommendations + llm_recs))

            summary = (
                f"Revue de {total_lines} lignes de code {request.language}. "
                f"Score: {quality_score:.2f}/1.0. "
                f"{len(merged_findings)} problème(s) détecté(s) "
                f"({len(local_result.findings)} statique + {len(llm_findings)} LLM)."
            )

            response = CodeReviewResponse(
                quality_score=quality_score,
                findings=merged_findings,
                summary=summary,
                category_scores=category_scores,
                strengths=merged_strengths,
                recommendations=merged_recs,
                language=request.language,
                lines_reviewed=total_lines,
                agent_iterations=agent_result.total_iterations,
                agent_best_score=agent_result.best_score,
                agent_converged=agent_result.converged,
            )

            # Stocker le résultat final (enrichi LLM) en mémoire
            self._store_to_memory(
                entry_type="expert_result",
                category="code_review",
                title=f"Revue agentique: {total_lines} lignes, score {quality_score:.2f}",
                data={"findings_count": len(merged_findings), "category_scores": category_scores},
                score=quality_score,
                language=request.language,
            )
            for finding in merged_findings:
                if finding.severity in ("critical", "error"):
                    self._store_to_memory(
                        entry_type="issue_found",
                        category=finding.category,
                        title=finding.title,
                        data={"severity": finding.severity, "description": finding.description},
                        language=request.language,
                    )

            return response

        except Exception as e:
            self.logger.warning(f"Fallback revue statique suite à erreur LLM: {e}")
            return local_result

    def _parse_llm_review(self, output: str) -> tuple[list[ReviewFinding], float, list[str], list[str]]:
        """Parse le JSON de la revue LLM avec validation Pydantic stricte."""
        parsed = parse_llm_response_strict(output, LLMCodeReviewResponse)

        findings = []
        for f in parsed.findings:
            findings.append(
                ReviewFinding(
                    category=f.category,
                    severity=f.severity,
                    line=f.line,
                    title=f.title,
                    description=f.description,
                    suggestion=f.suggestion,
                )
            )

        return findings, parsed.quality_score, parsed.strengths, parsed.recommendations
