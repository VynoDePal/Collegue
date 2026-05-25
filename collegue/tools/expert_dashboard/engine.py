"""
Moteur d'agrégation pour le tableau de bord des experts.
"""

import logging
from typing import Any, Dict, List, Optional

from .models import (
    DelegationActivity,
    ExpertStatus,
    ProjectHealth,
    Recommendation,
)

logger = logging.getLogger(__name__)

# Experts LLM connus
KNOWN_EXPERTS = [
    "code_review",
    "architecture_analysis",
    "performance_analysis",
    "code_refactoring",
    "test_generation",
    "code_documentation",
    "iac_guardrails_scan",
    "repo_consistency_check",
    "impact_analysis",
    "smart_orchestrator",
]

# Mapping expert → catégories
EXPERT_CATEGORIES = {
    "code_review": ["naming", "complexity", "security", "dry", "solid", "error_handling"],
    "architecture_analysis": ["dependencies", "coupling", "cohesion", "patterns", "debt"],
    "performance_analysis": ["cpu", "memory", "io", "algorithmic", "parallelism"],
    "code_refactoring": ["clean", "optimize", "extract", "rename"],
    "test_generation": ["unit", "integration", "coverage"],
    "code_documentation": ["docstring", "readme", "api"],
    "iac_guardrails_scan": ["security", "compliance", "best_practices"],
    "repo_consistency_check": ["structure", "conventions", "quality"],
    "impact_analysis": ["risk", "dependencies", "breaking_changes"],
    "smart_orchestrator": ["planning", "coordination"],
}


class DashboardEngine:
    """Agrège les données pour le tableau de bord."""

    def __init__(self, logger_instance=None):
        self.logger = logger_instance or logger

    def build_expert_statuses(self, memory_entries: List[Dict[str, Any]]) -> List[ExpertStatus]:
        """Construit le statut de chaque expert depuis la mémoire."""
        statuses = []

        for expert in KNOWN_EXPERTS:
            expert_entries = [e for e in memory_entries if e.get("expert") == expert]

            # Dernier score
            result_entries = [e for e in expert_entries if e.get("entry_type") == "expert_result"]
            last_score = None
            if result_entries:
                latest = max(result_entries, key=lambda e: e.get("timestamp", 0))
                last_score = latest.get("score")

            statuses.append(
                ExpertStatus(
                    name=expert,
                    total_executions=len(result_entries),
                    last_score=last_score,
                    categories=EXPERT_CATEGORIES.get(expert, []),
                    recent_findings=len(expert_entries),
                )
            )

        return statuses

    def build_recommendations(self, memory_entries: List[Dict[str, Any]], limit: int = 10) -> List[Recommendation]:
        """Construit les recommandations depuis la mémoire."""
        recommendations = []

        # Issues trouvées = recommandations de correction
        issue_entries = [e for e in memory_entries if e.get("entry_type") == "issue_found"]

        for entry in issue_entries:
            recommendations.append(
                Recommendation(
                    expert=entry.get("expert", "unknown"),
                    priority=self._severity_to_priority(entry.get("data", {}).get("severity", "info")),
                    title=entry.get("title", ""),
                    description=entry.get("data", {}).get("description", ""),
                    category=entry.get("category", ""),
                    file_path=entry.get("file_path"),
                )
            )

        # Patterns appris = recommandations d'amélioration
        pattern_entries = [e for e in memory_entries if e.get("entry_type") == "pattern_learned"]
        for entry in pattern_entries:
            if entry.get("score", 0) < 0.5:
                recommendations.append(
                    Recommendation(
                        expert=entry.get("expert", "unknown"),
                        priority=3,
                        title=f"Améliorer: {entry.get('title', '')}",
                        description=f"Pattern détecté avec score bas ({entry.get('score', 0):.2f})",
                        category=entry.get("category", ""),
                    )
                )

        recommendations.sort(key=lambda r: r.priority, reverse=True)
        return recommendations[:limit]

    def build_project_health(self, memory_entries: List[Dict[str, Any]]) -> ProjectHealth:
        """Calcule la santé globale du projet depuis la mémoire."""
        scores: Dict[str, List[float]] = {
            "quality": [],
            "architecture": [],
            "performance": [],
            "security": [],
        }

        for entry in memory_entries:
            if entry.get("entry_type") != "expert_result":
                continue
            expert = entry.get("expert", "")
            score_val = entry.get("score")
            if score_val is None:
                continue

            if expert in ("code_review", "code_refactoring", "repo_consistency_check"):
                scores["quality"].append(score_val)
            elif expert == "architecture_analysis":
                scores["architecture"].append(score_val)
            elif expert == "performance_analysis":
                scores["performance"].append(score_val)
            elif expert == "iac_guardrails_scan":
                scores["security"].append(score_val)

        quality = self._avg(scores["quality"])
        architecture = self._avg(scores["architecture"])
        performance = self._avg(scores["performance"])
        security = self._avg(scores["security"])

        all_scores = [s for s in [quality, architecture, performance, security] if s is not None]
        overall = sum(all_scores) / len(all_scores) if all_scores else 0.0

        return ProjectHealth(
            overall_score=round(overall, 2),
            quality_score=round(quality, 2) if quality is not None else None,
            architecture_score=round(architecture, 2) if architecture is not None else None,
            performance_score=round(performance, 2) if performance is not None else None,
            security_score=round(security, 2) if security is not None else None,
        )

    def build_delegation_activity(self, delegation_engine=None) -> DelegationActivity:
        """Construit l'activité de délégation."""
        if delegation_engine is None:
            return DelegationActivity()

        rules = getattr(delegation_engine, "_rules", [])
        total_rules = len(rules)

        source_counts: Dict[str, int] = {}
        target_counts: Dict[str, int] = {}
        for rule in rules:
            src = getattr(rule, "source_tool", "")
            tgt = getattr(rule, "target_tool", "")
            source_counts[src] = source_counts.get(src, 0) + 1
            target_counts[tgt] = target_counts.get(tgt, 0) + 1

        most_source = max(source_counts, key=source_counts.get) if source_counts else None
        most_target = max(target_counts, key=target_counts.get) if target_counts else None

        return DelegationActivity(
            total_rules=total_rules,
            most_active_source=most_source,
            most_active_target=most_target,
        )

    def build_summary(
        self,
        health: ProjectHealth,
        statuses: List[ExpertStatus],
        recommendations: List[Recommendation],
    ) -> str:
        """Génère un résumé textuel."""
        parts = []

        parts.append(f"Score global du projet: {health.overall_score:.2f}/1.0")

        if health.quality_score is not None:
            parts.append(f"Qualité: {health.quality_score:.2f}")
        if health.architecture_score is not None:
            parts.append(f"Architecture: {health.architecture_score:.2f}")
        if health.performance_score is not None:
            parts.append(f"Performance: {health.performance_score:.2f}")
        if health.security_score is not None:
            parts.append(f"Sécurité: {health.security_score:.2f}")

        active = [s for s in statuses if s.total_executions > 0]
        if active:
            parts.append(f"{len(active)} experts actifs sur {len(statuses)}")

        if recommendations:
            critical = [r for r in recommendations if r.priority >= 8]
            if critical:
                parts.append(f"{len(critical)} recommandation(s) critique(s)")
            parts.append(f"{len(recommendations)} recommandation(s) au total")

        return ". ".join(parts) + "."

    @staticmethod
    def _severity_to_priority(severity: str) -> int:
        return {"critical": 10, "error": 8, "warning": 5, "info": 3}.get(severity, 3)

    @staticmethod
    def _avg(values: List[float]) -> Optional[float]:
        if not values:
            return None
        return sum(values) / len(values)
