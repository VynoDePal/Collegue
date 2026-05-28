"""
Data access layer for the Streamlit dashboard.

Reads from the same singletons used by the MCP server:
- MetricsCollector (latency, tokens, costs, errors)
- ProjectMemory (stored analysis results)
- ExpertDelegation (rules, chain history)
- ExpertDashboard (aggregated health scores)
"""

import logging
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


def get_metrics_data() -> Dict[str, Any]:
    """Get metrics from the MetricsCollector singleton."""
    try:
        from collegue.monitoring.metrics import get_metrics_collector

        collector = get_metrics_collector()
        summary = collector.get_summary()
        all_metrics = collector.get_all_metrics()

        return {
            "summary": summary.to_dict(),
            "experts": all_metrics,
        }
    except Exception as exc:
        logger.warning("Cannot load metrics: %s", exc)
        return {"summary": {}, "experts": {}}


def get_dashboard_data() -> Dict[str, Any]:
    """Get dashboard data (health scores, expert statuses, recommendations)."""
    try:
        from collegue.core.project_memory import get_project_memory
        from collegue.tools.expert_dashboard.engine import DashboardEngine

        memory = get_project_memory()
        entries = memory.recall(limit=500)
        entries_raw = [e.to_dict() for e in entries]

        engine = DashboardEngine()
        statuses = engine.build_expert_statuses(entries_raw)
        health = engine.build_project_health(entries_raw)
        recommendations = engine.build_recommendations(entries_raw, limit=10)

        # Metrics from collector
        metrics_data: Dict[str, Any] = {}
        try:
            from collegue.monitoring.metrics import get_metrics_collector

            metrics_data = get_metrics_collector().get_summary().to_dict()
        except Exception:
            pass

        return {
            "project_health": health.model_dump(),
            "expert_statuses": [s.model_dump() for s in statuses],
            "recommendations": [r.model_dump() for r in recommendations],
            "metrics": metrics_data,
        }
    except Exception as exc:
        logger.warning("Cannot load dashboard data: %s", exc)
        return {
            "project_health": {
                "overall_score": 0,
                "quality_score": None,
                "architecture_score": None,
                "performance_score": None,
                "security_score": None,
            },
            "expert_statuses": [],
            "recommendations": [],
            "metrics": {},
        }


def get_delegation_data() -> Dict[str, Any]:
    """Get delegation engine data (rules, history)."""
    try:
        from collegue.core.expert_delegation import create_default_delegation_engine

        engine = create_default_delegation_engine()

        # Get rules (access internal _rules list)
        rules: List[Dict[str, Any]] = []
        for rule in engine._rules:
            rules.append(
                {
                    "Source": rule.source_tool,
                    "Cible": rule.target_tool,
                    "Condition": rule.condition_name,
                    "Priorité": rule.priority,
                }
            )

        # Get chain history (returns List[DelegationResult])
        chain_results = engine.get_chain_history()
        chain_history: List[Dict[str, Any]] = []
        for result in chain_results:
            chain_history.append(
                {
                    "source": result.source_tool,
                    "target": result.target_tool,
                    "status": "success" if result.success else "failed",
                    "duration": f"{result.execution_time:.1f}s",
                }
            )

        # Activity summary
        activity = {
            "total_chains": len(chain_history),
            "total_rules": len(rules),
            "most_active_source": None,
            "most_active_target": None,
            "rules": rules,
            "chain_history": chain_history,
        }

        # Find most active
        if chain_history:
            sources: Dict[str, int] = {}
            targets: Dict[str, int] = {}
            for chain in chain_history:
                src = chain.get("source", "")
                tgt = chain.get("target", "")
                sources[src] = sources.get(src, 0) + 1
                targets[tgt] = targets.get(tgt, 0) + 1
            if sources:
                activity["most_active_source"] = max(sources, key=sources.get)
            if targets:
                activity["most_active_target"] = max(targets, key=targets.get)

        return activity
    except Exception as exc:
        logger.warning("Cannot load delegation data: %s", exc)
        return {
            "total_chains": 0,
            "total_rules": 0,
            "most_active_source": None,
            "most_active_target": None,
            "rules": [],
            "chain_history": [],
        }


def get_memory_stats() -> Dict[str, Any]:
    """Get project memory statistics."""
    try:
        from collegue.core.project_memory import get_project_memory

        memory = get_project_memory()
        raw_stats = memory.export_stats()
        entries = memory.recall(limit=20)
        recent = [e.to_dict() for e in entries]

        # Transform stats to match app.py expectations
        by_expert = raw_stats.get("by_expert", {})
        by_type = raw_stats.get("by_type", {})

        # Compute languages from recent entries
        languages = {e.language for e in memory.recall(limit=500) if e.language}

        stats = {
            "total_entries": raw_stats.get("total_entries", 0),
            "experts_count": len(by_expert),
            "categories_count": len(by_type),
            "languages_count": len(languages),
            "entries_by_expert": by_expert,
            "entries_by_type": by_type,
        }

        return {
            "stats": stats,
            "recent_entries": recent,
        }
    except Exception as exc:
        logger.warning("Cannot load memory stats: %s", exc)
        return {
            "stats": {},
            "recent_entries": [],
        }
