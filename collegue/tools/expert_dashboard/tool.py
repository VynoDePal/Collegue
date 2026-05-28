"""
Expert Dashboard — Tableau de bord du collectif d'experts IA.

Fournit une vue agrégée de l'activité des experts, la santé du projet,
les recommandations prioritaires, et les statistiques de mémoire.
"""

from typing import Any, Dict, List

from ..base import BaseTool
from .engine import DashboardEngine
from .models import DashboardRequest, DashboardResponse


class ExpertDashboardTool(BaseTool):
    """
    Tableau de bord du collectif d'experts IA.

    Agrège les scores, recommandations et activités de tous les experts
    pour fournir une vue unifiée de la santé du projet.
    """

    tool_name = "expert_dashboard"
    tool_description = (
        "Tableau de bord du collectif d'experts IA. "
        "Affiche la santé du projet, les recommandations prioritaires, "
        "l'activité des experts et les statistiques de mémoire.\n\n"
        "PARAMÈTRES OPTIONNELS:\n"
        "- include_memory: Inclure les données mémoire (défaut: true)\n"
        "- include_recommendations: Inclure les recommandations (défaut: true)\n"
        "- top_recommendations: Nombre max de recommandations (défaut: 10)\n"
        "- language_filter: Filtrer par langage\n\n"
        "RETOURNE:\n"
        "- project_health: Scores agrégés (qualité, architecture, performance, sécurité)\n"
        "- expert_statuses: Statut de chaque expert\n"
        "- recommendations: Actions prioritaires\n"
        "- delegation_activity: Activité de délégation inter-experts\n"
        "- memory_stats: Statistiques de la mémoire projet"
    )
    tags = {"dashboard", "monitoring"}
    request_model = DashboardRequest
    response_model = DashboardResponse

    def __init__(self, config=None, app_state=None):
        super().__init__(config, app_state)
        self._engine = DashboardEngine(logger_instance=self.logger)

    def get_usage_description(self) -> str:
        return "Tableau de bord du collectif d'experts IA. Vue d'ensemble de la santé du projet et des recommandations."

    def get_examples(self) -> List[Dict[str, Any]]:
        return [
            {
                "title": "Dashboard complet",
                "request": {"include_memory": True, "include_recommendations": True},
            }
        ]

    def get_capabilities(self) -> List[str]:
        return [
            "Agrégation des scores de tous les experts",
            "Recommandations prioritaires",
            "Historique d'activité des experts",
            "Statistiques de mémoire projet",
            "Activité de délégation inter-experts",
        ]

    def _execute_core_logic(self, request: DashboardRequest, **kwargs) -> DashboardResponse:
        """Génère le tableau de bord."""
        memory_entries_raw: List[Dict[str, Any]] = []
        memory_stats: Dict[str, Any] = {}
        monitor_stats: Dict[str, Any] = {}

        # Charger la mémoire projet
        if request.include_memory:
            try:
                from ...core.project_memory import get_project_memory

                memory = get_project_memory()

                if request.language_filter:
                    entries = memory.recall(language=request.language_filter, limit=500)
                else:
                    entries = memory.recall(limit=500)

                memory_entries_raw = [e.to_dict() for e in entries]
                memory_stats = memory.export_stats()
            except Exception as exc:
                self.logger.warning("Impossible de charger la mémoire: %s", exc)

        # Charger les stats du moniteur proactif
        try:
            from ...autonomous.proactive_monitor import get_proactive_monitor

            monitor = get_proactive_monitor()
            monitor_stats = monitor.get_stats()
        except Exception:
            pass

        # Construire le dashboard
        statuses = self._engine.build_expert_statuses(memory_entries_raw)
        health = self._engine.build_project_health(memory_entries_raw)

        recommendations = []
        if request.include_recommendations:
            recommendations = self._engine.build_recommendations(memory_entries_raw, limit=request.top_recommendations)

        # Délégation
        delegation = self._engine.build_delegation_activity()
        try:
            from ...core.expert_delegation import create_default_delegation_engine

            delegation = self._engine.build_delegation_activity(create_default_delegation_engine())
        except Exception:
            pass

        # Métriques de performance
        metrics_data: Dict[str, Any] = {}
        try:
            from ...monitoring.metrics import get_metrics_collector

            metrics_data = get_metrics_collector().get_summary().to_dict()
        except Exception:
            pass

        summary = self._engine.build_summary(health, statuses, recommendations)

        return DashboardResponse(
            project_health=health,
            expert_statuses=statuses,
            recommendations=recommendations,
            delegation_activity=delegation,
            memory_stats=memory_stats,
            monitor_stats=monitor_stats,
            metrics=metrics_data,
            summary=summary,
        )

    async def _execute_core_logic_async(self, request: DashboardRequest, **kwargs) -> DashboardResponse:
        """Version asynchrone."""
        import asyncio

        return await asyncio.to_thread(self._execute_core_logic, request)
