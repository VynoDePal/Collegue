"""
Repo Consistency Check - Outil de détection d'incohérences dans le code.

Cet outil détecte les incohérences typiques générées par l'IA:
- Code mort (fonctions/classes jamais appelées)
- Variables inutilisées
- Imports non utilisés
- Duplication de code
- Mismatch paramètres/retours
- Symboles non résolus

Refactorisé: Le fichier original faisait 813 lignes, maintenant ~180 lignes.
"""

import asyncio
from typing import Any, Dict, List, Optional, Tuple

from ...core.llm_response_parser import LLMConsistencyResponse, parse_llm_response_strict
from ...core.shared import aggregate_severities, parse_llm_json_response
from ..agent_loop import AgentLoopConfig, AgentLoopMixin
from ..analyzers.javascript import JavaScriptAnalyzer
from ..analyzers.php import PHPAnalyzer
from ..analyzers.python import PythonAnalyzer
from ..base import BaseTool
from .config import ALL_CHECKS
from .engine import ConsistencyAnalysisEngine
from .models import (
    ConsistencyCheckRequest,
    ConsistencyCheckResponse,
    LLMInsight,
    SuggestedAction,
)


class RepoConsistencyCheckTool(AgentLoopMixin, BaseTool):
    """
    Outil de détection d'incohérences dans le code.

    Détecte les imports/variables inutilisés, code mort, duplication,
    et symboles non résolus dans Python, JavaScript/TypeScript et PHP.
    """

    tool_name = "repo_consistency_check"
    tool_description = (
        "Analyse le code source pour détecter les incohérences (imports/variables inutilisés, code mort, duplication, symboles non résolus).\n"
        "\n"
        "PARAMÈTRE REQUIS:\n"
        "- files: Liste d'objets représentant les fichiers à analyser. "
        "Format EXACT: [{'path': 'src/main.py', 'content': 'def foo():...'}, ...]. "
        "Il est crucial de fournir le contenu brut ('content') et le chemin ('path') pour chaque fichier.\n"
        "\n"
        "PARAMÈTRES OPTIONNELS UTILES:\n"
        "- language: Langage des fichiers ('python', 'typescript', 'javascript', 'php', 'auto'). Défaut: 'auto'.\n"
        "- checks: Liste des vérifications à lancer. Options: 'unused_imports', 'unused_vars', 'dead_code', 'duplication', 'unresolved_symbol'.\n"
        "- analysis_depth: 'fast' (heuristiques ultra-rapides) ou 'deep' (heuristiques + analyse LLM approfondie avec insights).\n"
        "- auto_chain: Booléen. Si True et que le score de refactoring est élevé, déclenche automatiquement l'outil 'code_refactoring'.\n"
        "\n"
        "UTILISATION:\n"
        'Idéal pour un audit rapide de la qualité du code après plusieurs modifications ou pour détecter des variables/dangers "hallucinés" par une IA.'
    )
    tags = {"analysis", "quality"}
    request_model = ConsistencyCheckRequest
    response_model = ConsistencyCheckResponse
    supported_languages = ["python", "typescript", "javascript", "php", "auto"]
    long_running = False

    agent_config = AgentLoopConfig(
        max_iterations=2,
        initial_temperature=0.5,
        temperature_decay=0.15,
        min_temperature=0.3,
    )

    def __init__(self, config=None, app_state=None):
        super().__init__(config, app_state)
        self._engine = ConsistencyAnalysisEngine(logger=self.logger)
        self._python_analyzer = PythonAnalyzer(logger=self.logger)
        self._js_analyzer = JavaScriptAnalyzer(logger=self.logger)
        self._php_analyzer = PHPAnalyzer(logger=self.logger)

    # --- AgentLoopMixin hooks ---

    async def validate_agent_output(self, output: str, context: Dict[str, Any]) -> List[str]:
        """Valide la réponse JSON du LLM pour le scoring deep."""
        errors = []
        try:
            data = parse_llm_json_response(output)
        except Exception:
            errors.append("Réponse non-JSON ou JSON invalide")
            return errors

        if "refactoring_score" not in data:
            errors.append("Champ 'refactoring_score' manquant")
        else:
            score = data["refactoring_score"]
            if not isinstance(score, (int, float)) or score < 0 or score > 1:
                errors.append(f"refactoring_score invalide: {score} (attendu 0.0-1.0)")

        insights = data.get("insights", [])
        if not insights:
            errors.append("Aucun insight fourni")
        else:
            for i, item in enumerate(insights[:10]):
                if not isinstance(item, dict) or "insight" not in item:
                    errors.append(f"insight[{i}] invalide: 'insight' manquant")

        return errors

    async def assess_agent_quality(self, output: str, context: Dict[str, Any]) -> float:
        """Évalue la qualité du scoring/insights LLM."""
        try:
            data = parse_llm_json_response(output)
        except Exception:
            return 0.0

        has_score = "refactoring_score" in data
        insights = data.get("insights", [])
        valid_insights = [i for i in insights if isinstance(i, dict) and "insight" in i]
        issues_count = context.get("issues_count", 1)
        insight_coverage = min(1.0, len(valid_insights) / max(issues_count / 3, 1))

        return (0.3 if has_score else 0.0) + insight_coverage * 0.7

    async def build_agent_feedback(
        self, output: str, errors: List[str], quality: float, context: Dict[str, Any]
    ) -> str:
        """Construit un feedback pour améliorer l'analyse deep."""
        parts = []
        for error in errors:
            if "non-JSON" in error:
                parts.append("Réponds UNIQUEMENT avec du JSON valide, sans markdown.")
            elif "manquant" in error:
                parts.append(f"ERREUR: {error}. Ajoute ce champ.")
            elif "invalide" in error:
                parts.append(f"ERREUR: {error}. Corrige la valeur.")
            elif "Aucun insight" in error:
                parts.append(
                    "Fournis au moins 2-3 insights sur les patterns, l'architecture ou la dette technique détectés."
                )
        return "\n".join(parts) if parts else "Améliore la précision du scoring et des insights."

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
                    "files": [
                        {
                            "path": "utils.py",
                            "content": "import os\nimport sys\nprint('hello')",
                        }
                    ],
                    "language": "python",
                },
            },
            {
                "title": "Mode deep avec checks spécifiques",
                "request": {
                    "files": [{"path": "app.ts", "content": "..."}],
                    "language": "typescript",
                    "mode": "deep",
                    "checks": ["unused_imports", "dead_code"],
                },
            },
        ]

    def get_capabilities(self) -> List[str]:
        return [
            "Détection d'imports non utilisés (Python, JS/TS, PHP)",
            "Détection de variables inutilisées",
            "Détection de code mort (fonctions non appelées)",
            "Détection de duplication de code",
            "Détection de mismatch signature/usage",
            "Support multi-fichiers avec analyse croisée",
        ]

    def _build_prompt(self, request: ConsistencyCheckRequest, issues: List) -> str:
        """Construit le prompt pour l'analyse LLM deep."""
        files_summary = []
        for f in request.files[:5]:
            preview = f.content[:400] + "..." if len(f.content) > 400 else f.content
            files_summary.append(f"### {f.path}\n```\n{preview}\n```")

        issues_summary = []
        for issue in issues[:15]:
            issues_summary.append(
                f"- [{issue.severity.upper()}] {issue.kind} @ {issue.path}:{issue.line or '?'}: {issue.message}"
            )

        files_block = "\n".join(files_summary)
        issues_block = "\n".join(issues_summary) if issues_summary else "Aucune issue détectée"

        return f"""Analyse les incohérences détectées dans ce code et fournis des insights.

## Fichiers analysés
{files_block}

## Issues détectées ({len(issues)} total)
{issues_block}

---

Fournis une analyse enrichie au format JSON strict:
{{
  "refactoring_score": 0.0-1.0,
  "insights": [
    {{
      "category": "pattern|architecture|debt|suggestion",
      "insight": "Description détaillée",
      "confidence": "low|medium|high",
      "affected_files": ["file1.py", "file2.ts"]
    }}
  ]
}}

Catégories d'insights:
- **pattern**: Anti-patterns détectés (god class, spaghetti code, etc.)
- **architecture**: Problèmes structurels (couplage, cohésion, responsabilités)
- **debt**: Dette technique (complexité, maintenabilité)
- **suggestion**: Recommandations d'amélioration

Le `refactoring_score` doit refléter l'urgence d'un refactoring:
- 0.0-0.3: Code acceptable, améliorations optionnelles
- 0.3-0.6: Refactoring suggéré pour améliorer la maintenabilité
- 0.6-0.8: Refactoring recommandé, risques de bugs
- 0.8-1.0: Refactoring critique, dette technique élevée

Réponds UNIQUEMENT avec le JSON, sans markdown ni explication."""

    async def _deep_analysis_with_llm(
        self, request: ConsistencyCheckRequest, issues: List, ctx=None
    ) -> Tuple[Optional[List[LLMInsight]], float, str]:
        """Effectue l'analyse approfondie avec boucle agentique."""
        if ctx is None:
            self.logger.warning("ctx non disponible pour analyse deep")
            score, priority = self._engine.calculate_refactoring_score(issues)
            return None, score, priority

        try:
            prompt = self._build_prompt(request, issues)

            agent_result = await self.agent_execute(
                initial_prompt=prompt,
                system_prompt=(
                    "Tu es un expert en qualité de code. Réponds UNIQUEMENT avec du JSON strict sans markdown."
                ),
                ctx=ctx,
                context={"issues_count": len(issues)},
                max_tokens=2000,
            )

            response = agent_result.best_output

            if not response:
                score, priority = self._engine.calculate_refactoring_score(issues)
                return None, score, priority

            parsed = parse_llm_response_strict(response, LLMConsistencyResponse)

            llm_score = parsed.refactoring_score

            heuristic_score, _ = self._engine.calculate_refactoring_score(issues)
            final_score = (llm_score * 0.6) + (heuristic_score * 0.4)

            if final_score >= 0.8:
                priority = "critical"
            elif final_score >= 0.6:
                priority = "recommended"
            elif final_score >= 0.3:
                priority = "suggested"
            else:
                priority = "none"

            insights = []
            for item in parsed.insights[:10]:
                insights.append(
                    LLMInsight(
                        category=item.category,
                        insight=item.insight,
                        confidence=item.confidence,
                        affected_files=item.affected_files,
                    )
                )

            self.logger.info(f"Analyse deep: {len(insights)} insights, score={final_score:.2f}")
            return insights, final_score, priority

        except Exception as e:
            self.logger.error(f"Erreur analyse deep: {e}")
            score, priority = self._engine.calculate_refactoring_score(issues)
            return None, score, priority

    async def _execute_auto_chain_refactoring(
        self,
        request: ConsistencyCheckRequest,
        issues: List,
        suggested_actions: List[SuggestedAction],
        ctx=None,
    ) -> Optional[Dict[str, Any]]:
        """Exécute le refactoring automatique si activé."""
        try:
            from ..refactoring import RefactoringRequest, RefactoringTool

            if not suggested_actions:
                return None

            best_action = max(suggested_actions, key=lambda a: a.score)
            if best_action.tool_name != "code_refactoring":
                return None

            params = best_action.params
            if not params.get("code"):
                file_with_issues = next(
                    (f for f in request.files if any(i.path == f.path for i in issues)),
                    request.files[0] if request.files else None,
                )
                if not file_with_issues:
                    return None
                params["code"] = file_with_issues.content[:5000]
                params["language"] = file_with_issues.language or self._engine.detect_language(file_with_issues.path)
                params["file_path"] = file_with_issues.path

            refactoring_request = RefactoringRequest(
                code=params.get("code", ""),
                language=params.get("language", "python"),
                refactoring_type=params.get("refactoring_type", "clean"),
                file_path=params.get("file_path"),
                parameters={"context": "auto-triggered from repo_consistency_check"},
            )

            refactoring_tool = RefactoringTool(app_state=self.app_state)
            if ctx is not None:
                result = await refactoring_tool.execute_async(refactoring_request, ctx=ctx)
            else:
                result = refactoring_tool.execute(refactoring_request)

            self.logger.info(f"Auto-refactoring exécuté sur {params.get('file_path', 'fichier')}")

            return {
                "file_path": params.get("file_path"),
                "refactoring_type": params.get("refactoring_type"),
                "original_code_preview": params.get("code", "")[:200] + "...",
                "refactored_code_preview": result.refactored_code[:200] + "..." if result.refactored_code else None,
                "changes_count": len(result.changes),
                "explanation": result.explanation,
            }

        except Exception as e:
            self.logger.error(f"Erreur auto-chain refactoring: {e}")
            return None

    def _execute_core_logic(self, request: ConsistencyCheckRequest, **kwargs) -> ConsistencyCheckResponse:
        """Exécute la vérification de cohérence (synchrone)."""
        self.logger.info(f"Vérification de cohérence sur {len(request.files)} fichier(s)")

        checks = request.checks or ALL_CHECKS
        all_issues = []
        all_contents = "\n".join(f.content for f in request.files)

        # Analyser chaque fichier
        for file in request.files:
            lang = file.language or (
                request.language if request.language != "auto" else self._engine.detect_language(file.path)
            )

            if lang == "python":
                if "unused_imports" in checks:
                    all_issues.extend(self._python_analyzer.analyze_unused_imports(file.content, file.path))
                if "unused_vars" in checks:
                    all_issues.extend(self._python_analyzer.analyze_unused_vars(file.content, file.path))
                if "dead_code" in checks:
                    all_issues.extend(self._python_analyzer.analyze_dead_code(file.content, file.path, all_contents))

            elif lang in ("typescript", "javascript"):
                if "unused_imports" in checks:
                    all_issues.extend(self._js_analyzer.analyze_unused_imports(file.content, file.path))
                if "unused_vars" in checks:
                    all_issues.extend(self._js_analyzer.analyze_unused_vars(file.content, file.path))

            elif lang == "php":
                if "unused_imports" in checks:
                    all_issues.extend(self._php_analyzer.analyze_unused_imports(file.content, file.path))
                if "unused_vars" in checks:
                    all_issues.extend(self._php_analyzer.analyze_unused_vars(file.content, file.path))
                if "dead_code" in checks:
                    all_issues.extend(self._php_analyzer.analyze_dead_code(file.content, file.path))

        # Analyses multi-fichiers
        if "duplication" in checks and len(request.files) > 1:
            all_issues.extend(self._engine.analyze_duplication(request.files))

        if "unresolved_symbol" in checks and request.mode == "deep":
            all_issues.extend(self._engine.analyze_unresolved_symbols(request.files))

        # Filtrer par confiance
        all_issues = [i for i in all_issues if i.confidence >= request.min_confidence]

        # Calculer les statistiques
        severity_counts = aggregate_severities(all_issues, default_levels=["high", "medium", "low", "info"])
        summary = {
            "total": len(all_issues),
            "high": severity_counts["high"],
            "medium": severity_counts["medium"],
            "low": severity_counts["low"],
            "info": severity_counts["info"],
        }

        # Scores et actions
        refactoring_score, refactoring_priority = self._engine.calculate_refactoring_score(all_issues)
        suggested_actions = self._engine.generate_suggested_actions(
            all_issues, request.files, refactoring_score, self._engine.detect_language
        )

        # Résumé
        analysis_summary = self._engine.build_analysis_summary(all_issues, len(request.files), severity_counts)

        return ConsistencyCheckResponse(
            valid=len(all_issues) == 0,
            summary=summary,
            issues=all_issues[:100],
            files_analyzed=len(request.files),
            checks_performed=checks,
            analysis_summary=analysis_summary,
            analysis_depth_used="fast",
            llm_insights=None,
            refactoring_score=refactoring_score,
            refactoring_priority=refactoring_priority,
            suggested_actions=suggested_actions,
            auto_refactoring_triggered=False,
            auto_refactoring_result=None,
        )

    async def _execute_core_logic_async(self, request: ConsistencyCheckRequest, **kwargs) -> ConsistencyCheckResponse:
        """Version asynchrone avec support deep analysis et auto-chain."""
        ctx = kwargs.get("ctx")

        # Exécution synchrone de base
        response = await asyncio.to_thread(self._execute_core_logic, request)

        if ctx is None:
            return response

        llm_insights = response.llm_insights
        refactoring_score = response.refactoring_score
        refactoring_priority = response.refactoring_priority
        auto_refactoring_triggered = False
        auto_refactoring_result = None

        # Deep analysis si demandé
        if request.analysis_depth == "deep":
            try:
                (
                    llm_insights,
                    refactoring_score,
                    refactoring_priority,
                ) = await self._deep_analysis_with_llm(request, response.issues, ctx=ctx)
            except Exception as e:
                self.logger.warning(f"Fallback mode fast suite à erreur deep: {e}")

        # Regénérer les actions avec le nouveau score
        suggested_actions = self._engine.generate_suggested_actions(
            response.issues,
            request.files,
            refactoring_score,
            self._engine.detect_language,
        )

        # Auto-chain si activé (legacy) ou délégation inter-experts
        delegation_triggered = False
        delegation_results_list = None

        if request.auto_chain and refactoring_score >= request.refactoring_threshold and suggested_actions:
            # Essayer d'abord la délégation inter-experts
            delegation_engine = kwargs.get("delegation_engine")
            if delegation_engine is not None:
                try:
                    result_dict = response.model_copy(
                        update={
                            "refactoring_score": refactoring_score,
                            "suggested_actions": [
                                a.model_dump() if hasattr(a, "model_dump") else a for a in suggested_actions
                            ],
                        }
                    ).model_dump()
                    tasks = await delegation_engine.evaluate_delegations(self.tool_name, result_dict)
                    if tasks:
                        tool_registry = kwargs.get("tool_registry", {})
                        tool_kw = {
                            "parser": kwargs.get("parser"),
                            "prompt_engine": kwargs.get("prompt_engine"),
                            "context_manager": kwargs.get("context_manager"),
                        }
                        del_results = await delegation_engine.execute_delegation_chain(
                            tasks, tool_registry, ctx=ctx, tool_kwargs=tool_kw
                        )
                        delegation_triggered = True
                        delegation_results_list = [r.model_dump() for r in del_results]
                except Exception as e:
                    self.logger.warning(f"Erreur délégation: {e}")

            # Fallback: auto-chain legacy si pas de delegation engine
            if not delegation_triggered:
                try:
                    auto_refactoring_result = await self._execute_auto_chain_refactoring(
                        request, response.issues, suggested_actions, ctx=ctx
                    )
                    if auto_refactoring_result:
                        auto_refactoring_triggered = True
                except Exception as e:
                    self.logger.warning(f"Erreur auto-chain: {e}")

        # Mettre à jour le résumé
        analysis_summary = self._engine.build_analysis_summary(
            response.issues,
            response.files_analyzed,
            response.summary,
            request.analysis_depth,
            refactoring_score,
            refactoring_priority,
            len(llm_insights) if llm_insights else 0,
            auto_refactoring_triggered,
        )

        return response.model_copy(
            update={
                "llm_insights": llm_insights,
                "refactoring_score": refactoring_score,
                "refactoring_priority": refactoring_priority,
                "suggested_actions": suggested_actions,
                "analysis_depth_used": request.analysis_depth,
                "analysis_summary": analysis_summary,
                "auto_refactoring_triggered": auto_refactoring_triggered,
                "auto_refactoring_result": auto_refactoring_result,
                "delegation_triggered": delegation_triggered,
                "delegation_results": delegation_results_list,
            }
        )
