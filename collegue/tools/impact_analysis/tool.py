"""
Impact Analysis - Outil d'analyse d'impact des changements de code.

Cet outil analyse l'impact potentiel d'un changement de code avant son implémentation:
- Identifie les fichiers impactés par un changement
- Détecte les risques (breaking changes, sécurité, migration)
- Génère des requêtes de recherche pour l'IDE
- Recommande les tests à exécuter

Refactorisé: Le fichier original faisait 680 lignes, maintenant ~200 lignes.
"""

from typing import List, Dict, Any, Optional
from ..base import BaseTool, ToolValidationError
from ...core.shared import run_async_from_sync, parse_llm_json_response
from .models import (
    ImpactAnalysisRequest,
    ImpactAnalysisResponse,
    ImpactedFile,
    RiskNote,
    SearchQuery,
    TestRecommendation,
    FollowupAction,
    LLMInsight,
)
from .engine import ImpactAnalysisEngine
from .config import CONFIDENCE_THRESHOLDS


class ImpactAnalysisTool(BaseTool):
    """
    Outil d'analyse d'impact des changements de code.

    Identifie les fichiers impactés, détecte les risques, recommande les tests
    et guide la stratégie avant de coder pour réduire les breaking changes.
    """

    tool_name = "impact_analysis"
    tool_description = (
        "Analyse l'impact potentiel d'un changement de code avant son implémentation.\n"
        "ATTENTION: Cet outil aide à la planification et ne modifie aucun fichier.\n"
        "\n"
        "PARAMÈTRES REQUIS:\n"
        "- change_intent: La description du changement prévu (ex: 'renommer UserService en AuthService').\n"
        "- files: La liste des fichiers impactés initiaux. Doit respecter le format: [{'path': '...', 'content': '...', 'language': '...'}].\n"
        "\n"
        "PARAMÈTRES OPTIONNELS:\n"
        "- diff: Diff unifié des changements si disponibles.\n"
        "- entry_points: Les points d'entrée du projet à considérer (ex: ['main.py', 'api/router.ts']).\n"
        "- assumptions: Hypothèses et contraintes spécifiques du projet.\n"
        "- confidence_mode: Niveau de confiance pour la détection ('balanced' par défaut, 'conservative', ou 'aggressive').\n"
        "- analysis_depth: Profondeur de l'analyse ('fast' pour des heuristiques rapides, 'deep' pour une analyse LLM avancée).\n"
        "\n"
        "UTILISATION:\n"
        "Idéal pour préparer un plan de refactoring complexe, anticiper les 'breaking changes', ou obtenir des recommandations de tests."
    )
    tags = {"analysis", "planning"}
    request_model = ImpactAnalysisRequest
    response_model = ImpactAnalysisResponse
    supported_languages = [
        "python",
        "javascript",
        "typescript",
        "java",
        "c#",
        "php",
        "go",
        "rust",
    ]
    long_running = False

    def __init__(self, config=None, app_state=None):
        super().__init__(config, app_state)
        self._engine = ImpactAnalysisEngine(logger=self.logger)

    def get_usage_description(self) -> str:
        return (
            "Analyse l'impact d'un changement de code en identifiant les fichiers affectés, "
            "les risques potentiels (breaking changes, sécurité), et en recommandant les tests "
            "à exécuter. Guide la stratégie de refactoring avant d'implémenter pour éviter "
            "les régressions."
        )

    def get_examples(self) -> List[Dict[str, Any]]:
        return [
            {
                "title": "Renommer un service",
                "description": "Analyser l'impact du renommage d'un service",
                "request": {
                    "change_intent": "Renommer UserService en AuthService",
                    "files": [
                        {
                            "path": "services/user_service.py",
                            "content": "class UserService:...",
                        },
                        {
                            "path": "api/auth.py",
                            "content": "from services.user_service import UserService",
                        },
                    ],
                },
            },
            {
                "title": "Modifier une API",
                "description": "Analyser l'impact d'un changement d'API",
                "request": {
                    "change_intent": "Modifier l'endpoint /api/users pour retourner plus de champs",
                    "files": [
                        {
                            "path": "api/users.py",
                            "content": "@app.get('/api/users')...",
                        },
                        {"path": "models/user.py", "content": "class User:..."},
                    ],
                    "entry_points": ["api/users.py"],
                },
            },
        ]

    def get_capabilities(self) -> List[str]:
        return [
            "Identification des fichiers impactés par un changement",
            "Détection des breaking changes potentiels",
            "Identification des risques de sécurité",
            "Détection des besoins de migration de données",
            "Génération de requêtes de recherche pour l'IDE",
            "Recommandation des tests à exécuter",
            "3 modes de confiance: conservative, balanced, aggressive",
            "Analyse heuristique rapide ou enrichie par IA",
        ]

    def validate_request(self, request) -> bool:
        """Valide la requête d'analyse d'impact."""
        super().validate_request(request)

        if not request.files:
            raise ToolValidationError("Au moins un fichier est requis pour l'analyse")

        return True

    def _build_deep_analysis_prompt(
        self,
        request: ImpactAnalysisRequest,
        impacted_files: List[Dict],
        risk_notes: List[Dict],
    ) -> str:
        """Construit le prompt pour l'analyse deep avec LLM."""
        prompt_parts = [
            f"Analyse en profondeur l'impact du changement suivant:",
            f"",
            f"Intent: {request.change_intent}",
            f"",
            f"Fichiers impactés ({len(impacted_files)}):",
        ]

        for f in impacted_files[:10]:
            prompt_parts.append(f"  - {f['path']} ({f.get('impact_type', 'unknown')})")

        if risk_notes:
            prompt_parts.extend([f"", f"Risques identifiés:"])
            for r in risk_notes[:5]:
                prompt_parts.append(f"  - [{r['category']}] {r['note']}")

        prompt_parts.extend(
            [
                f"",
                f"Fournis une analyse structurée au format JSON:",
                f"{{",
                f'  "semantic_summary": "Résumé sémantique du changement",',
                f'  "insights": [',
                f'    {{"category": "semantic|architectural|business", "insight": "...", "confidence": "high|medium|low"}}',
                f"  ]",
                f"}}",
            ]
        )

        return "\n".join(prompt_parts)

    def _execute_core_logic(
        self, request: ImpactAnalysisRequest, **kwargs
    ) -> ImpactAnalysisResponse:
        """Exécute l'analyse d'impact (synchrone)."""
        ctx = kwargs.get("ctx")

        # 1. Extraire les identifiants
        identifiers = self._engine.extract_identifiers(request.change_intent)

        if self.logger:
            self.logger.debug(f"Identifiants extraits: {identifiers}")

        # 2. Analyser chaque fichier
        confidence_threshold = CONFIDENCE_THRESHOLDS.get(request.confidence_mode, 0.6)
        all_impacts = []

        for file in request.files:
            impacts = self._engine.analyze_single_file(
                file, identifiers, confidence_threshold, request.entry_points
            )
            all_impacts.extend(impacts)

        # 3. Dédoublonner et filtrer
        seen_paths = set()
        unique_impacts = []
        for impact in all_impacts:
            key = (impact["path"], impact.get("identifier", ""))
            if key not in seen_paths:
                seen_paths.add(key)
                unique_impacts.append(impact)

        # 4. Filtrer par confiance
        filtered_impacts = self._engine.filter_by_confidence(
            unique_impacts, request.confidence_mode
        )

        # 5. Analyser les risques
        risk_notes = self._engine.analyze_risks(request.change_intent, identifiers)

        # 6. Générer les requêtes de recherche
        search_queries = self._engine.generate_search_queries(
            identifiers, request.change_intent
        )

        # 7. Recommander les tests
        tests_to_run = self._engine.recommend_tests(filtered_impacts, "python")

        # 8. Générer les actions de suivi
        followups = self._engine.generate_followup_actions(filtered_impacts, risk_notes)

        # 9. Construire le résumé
        analysis_summary = self._engine.build_analysis_summary(
            request.change_intent, filtered_impacts, risk_notes
        )

        # Convertir en modèles Pydantic
        impacted_files = [ImpactedFile(**impact) for impact in filtered_impacts[:50]]
        risk_notes_models = [RiskNote(**risk) for risk in risk_notes]
        search_queries_models = [SearchQuery(**q) for q in search_queries]
        tests_models = [TestRecommendation(**t) for t in tests_to_run]
        followups_models = [FollowupAction(**a) for a in followups]

        return ImpactAnalysisResponse(
            change_summary=f"Analyse d'impact: {request.change_intent}",
            impacted_files=impacted_files,
            risk_notes=risk_notes_models,
            search_queries=search_queries_models,
            tests_to_run=tests_models,
            followups=followups_models,
            analysis_summary=analysis_summary,
            analysis_depth_used="fast",
        )

    async def _execute_core_logic_async(
        self, request: ImpactAnalysisRequest, **kwargs
    ) -> ImpactAnalysisResponse:
        """Version asynchrone avec support deep analysis."""
        ctx = kwargs.get("ctx")

        # Exécution de base
        response = await self._run_async_from_sync(self._execute_core_logic, request)

        if ctx and request.analysis_depth == "deep":
            try:
                if ctx:
                    await ctx.info("Analyse sémantique en cours...")

                prompt = self._build_deep_analysis_prompt(
                    request,
                    [f.model_dump() for f in response.impacted_files],
                    [r.model_dump() for r in response.risk_notes],
                )

                result = await ctx.sample(
                    messages=prompt, temperature=0.5, max_tokens=1500
                )

                # Parser la réponse JSON
                try:
                    data = parse_llm_json_response(result.text)

                    insights = []
                    for item in data.get("insights", []):
                        insights.append(LLMInsight(**item))

                    response = response.model_copy(
                        update={
                            "llm_insights": insights,
                            "semantic_summary": data.get("semantic_summary"),
                            "analysis_depth_used": "deep",
                        }
                    )

                except Exception as e:
                    if self.logger:
                        self.logger.warning(f"Erreur parsing insights LLM: {e}")

            except Exception as e:
                if self.logger:
                    self.logger.warning(f"Erreur analyse deep: {e}")

        return response

    async def _run_async_from_sync(self, func, *args, **kwargs):
        """Helper pour exécuter une fonction synchrone de manière asynchrone."""
        import asyncio

        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, lambda: func(*args, **kwargs))
