"""
Expert Dashboard — Tableau de bord du collectif d'experts IA.

Usage:
    from collegue.tools.expert_dashboard import ExpertDashboardTool
"""

from .models import DashboardRequest, DashboardResponse
from .tool import ExpertDashboardTool

__all__ = ["ExpertDashboardTool", "DashboardRequest", "DashboardResponse"]
