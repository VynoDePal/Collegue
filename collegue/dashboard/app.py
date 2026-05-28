"""
Collègue MCP — Streamlit Monitoring Dashboard.

Provides a real-time web interface (port 4125) to visualize:
- Expert system health (scores, activity)
- Metrics (latency, tokens, costs, errors)
- Delegation chains and activity
- Project memory statistics

Connects to the running MCP server's internal state via shared singletons.
"""

import sys
import time
from pathlib import Path

import streamlit as st

# Ensure the project root is importable
_project_root = Path(__file__).resolve().parent.parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from collegue.dashboard.data import (
    get_dashboard_data,
    get_delegation_data,
    get_memory_stats,
    get_metrics_data,
)

# ─── Page config ────────────────────────────────────────────────────────────

st.set_page_config(
    page_title="Collègue MCP — Dashboard",
    page_icon="🤖",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ─── Custom CSS ─────────────────────────────────────────────────────────────

st.markdown(
    """
<style>
    .block-container { padding-top: 1.5rem; }
    [data-testid="stMetric"] {
        background-color: #f8f9fa;
        border: 1px solid #e9ecef;
        border-radius: 8px;
        padding: 12px 16px;
    }
    [data-testid="stMetric"] label { font-size: 0.85rem; color: #6c757d; }
    [data-testid="stMetric"] [data-testid="stMetricValue"] { font-size: 1.5rem; }
    .stTabs [data-baseweb="tab-list"] { gap: 8px; }
    .stTabs [data-baseweb="tab"] {
        padding: 8px 20px;
        border-radius: 6px;
    }
</style>
""",
    unsafe_allow_html=True,
)

# ─── Sidebar ────────────────────────────────────────────────────────────────

with st.sidebar:
    st.title("Collègue MCP")
    st.caption("Monitoring Dashboard")
    st.divider()

    auto_refresh = st.toggle("Auto-refresh (10s)", value=False)
    if auto_refresh:
        time.sleep(0.1)
        st.rerun()

    st.divider()
    st.markdown("**Serveur MCP**")
    st.code("http://localhost:4121/mcp/", language=None)
    st.markdown("**Dashboard**")
    st.code("http://localhost:4125", language=None)

# ─── Main Content ───────────────────────────────────────────────────────────

st.title("Dashboard Monitoring")

# Tabs
tab_overview, tab_metrics, tab_delegation, tab_memory = st.tabs(
    ["Vue d'ensemble", "Métriques", "Délégation", "Mémoire"]
)

# ═══════════════════════════════════════════════════════════════════════════════
# TAB 1: Overview
# ═══════════════════════════════════════════════════════════════════════════════

with tab_overview:
    dashboard = get_dashboard_data()

    # Health scores
    st.subheader("Santé du Projet")
    health = dashboard.get("project_health", {})

    col1, col2, col3, col4, col5 = st.columns(5)
    col1.metric("Global", f"{health.get('overall_score', 0):.0%}")
    col2.metric("Qualité", f"{health.get('quality_score', 0) or 0:.0%}")
    col3.metric("Architecture", f"{health.get('architecture_score', 0) or 0:.0%}")
    col4.metric("Performance", f"{health.get('performance_score', 0) or 0:.0%}")
    col5.metric("Sécurité", f"{health.get('security_score', 0) or 0:.0%}")

    st.divider()

    # Expert statuses
    st.subheader("Statut des Experts")
    statuses = dashboard.get("expert_statuses", [])

    if statuses:
        cols = st.columns(min(len(statuses), 4))
        for i, expert in enumerate(statuses):
            with cols[i % 4]:
                score = expert.get("last_score")
                score_str = f"{score:.0%}" if score is not None else "—"
                execs = expert.get("total_executions", 0)
                st.metric(
                    label=expert.get("name", "?"),
                    value=score_str,
                    delta=f"{execs} exécutions",
                )
    else:
        st.info("Aucune activité d'expert enregistrée. Lancez une analyse pour voir les résultats ici.")

    st.divider()

    # Recommendations
    st.subheader("Recommandations Prioritaires")
    recommendations = dashboard.get("recommendations", [])

    if recommendations:
        for rec in recommendations[:5]:
            priority = rec.get("priority", 5)
            icon = "🔴" if priority >= 8 else "🟡" if priority >= 5 else "🟢"
            with st.expander(f"{icon} [{rec.get('expert', '?')}] {rec.get('title', '?')}"):
                st.write(rec.get("description", ""))
                if rec.get("file_path"):
                    st.code(rec["file_path"], language=None)
    else:
        st.success("Aucune recommandation en attente.")

# ═══════════════════════════════════════════════════════════════════════════════
# TAB 2: Metrics
# ═══════════════════════════════════════════════════════════════════════════════

with tab_metrics:
    metrics = get_metrics_data()

    summary = metrics.get("summary", {})
    experts_metrics = metrics.get("experts", {})

    # Global metrics
    st.subheader("Métriques Globales")
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Exécutions totales", summary.get("total_executions", 0))
    col2.metric("Coût total", f"${summary.get('total_cost_usd', 0):.4f}")
    col3.metric("Erreurs", summary.get("total_errors", 0))
    col4.metric("Latence moy.", f"{summary.get('avg_latency_ms', 0):.0f} ms")

    st.divider()

    # Per-expert metrics table
    st.subheader("Métriques par Expert")

    if experts_metrics:
        import pandas as pd

        rows = []
        for name, data in experts_metrics.items():
            rows.append(
                {
                    "Expert": name,
                    "Exécutions": data.get("total_executions", 0),
                    "Succès": f"{data.get('success_rate', 0):.0%}",
                    "Latence moy.": f"{data.get('avg_latency_ms', 0):.0f} ms",
                    "P95": f"{data.get('p95_latency_ms', 0):.0f} ms",
                    "Tokens (in)": data.get("total_input_tokens", 0),
                    "Tokens (out)": data.get("total_output_tokens", 0),
                    "Coût": f"${data.get('total_cost_usd', 0):.5f}",
                    "Erreurs": data.get("failed_executions", 0),
                }
            )
        df = pd.DataFrame(rows)
        st.dataframe(df, use_container_width=True, hide_index=True)

        # Latency chart
        st.subheader("Latence par Expert")
        latency_data = {name: data.get("avg_latency_ms", 0) for name, data in experts_metrics.items()}
        st.bar_chart(latency_data)

        # Cost breakdown
        st.subheader("Répartition des Coûts")
        cost_data = {name: data.get("total_cost_usd", 0) for name, data in experts_metrics.items()}
        if any(v > 0 for v in cost_data.values()):
            st.bar_chart(cost_data)
        else:
            st.info("Aucun coût enregistré pour le moment.")

        # Error breakdown
        st.subheader("Erreurs par Type")
        all_errors = {}
        for name, data in experts_metrics.items():
            errors_by_type = data.get("errors_by_type", {})
            for err_type, count in errors_by_type.items():
                all_errors[f"{name}/{err_type}"] = all_errors.get(f"{name}/{err_type}", 0) + count
        if all_errors:
            st.bar_chart(all_errors)
        else:
            st.success("Aucune erreur enregistrée.")
    else:
        st.info("Aucune métrique disponible. Les métriques s'afficheront après les premières exécutions d'experts.")

# ═══════════════════════════════════════════════════════════════════════════════
# TAB 3: Delegation
# ═══════════════════════════════════════════════════════════════════════════════

with tab_delegation:
    delegation = get_delegation_data()

    st.subheader("Activité de Délégation")
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Chaînes exécutées", delegation.get("total_chains", 0))
    col2.metric("Règles actives", delegation.get("total_rules", 0))
    col3.metric("Source la + active", delegation.get("most_active_source", "—"))
    col4.metric("Cible la + active", delegation.get("most_active_target", "—"))

    st.divider()

    # Rules table
    st.subheader("Règles de Délégation")
    rules = delegation.get("rules", [])
    if rules:
        import pandas as pd

        rules_df = pd.DataFrame(rules)
        st.dataframe(rules_df, use_container_width=True, hide_index=True)
    else:
        st.info("Aucune règle de délégation chargée.")

    # Chain history
    st.subheader("Historique des Chaînes")
    chain_history = delegation.get("chain_history", [])
    if chain_history:
        for chain in chain_history[-10:]:
            st.write(f"`{chain.get('source', '?')}` → `{chain.get('target', '?')}` ({chain.get('status', '?')})")
    else:
        st.info("Aucune chaîne exécutée pour le moment.")

# ═══════════════════════════════════════════════════════════════════════════════
# TAB 4: Memory
# ═══════════════════════════════════════════════════════════════════════════════

with tab_memory:
    memory = get_memory_stats()

    st.subheader("Statistiques de la Mémoire Projet")

    stats = memory.get("stats", {})
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Entrées totales", stats.get("total_entries", 0))
    col2.metric("Experts actifs", stats.get("experts_count", 0))
    col3.metric("Catégories", stats.get("categories_count", 0))
    col4.metric("Langages", stats.get("languages_count", 0))

    st.divider()

    # Entries by expert
    st.subheader("Entrées par Expert")
    by_expert = stats.get("entries_by_expert", {})
    if by_expert:
        st.bar_chart(by_expert)
    else:
        st.info("Aucune entrée en mémoire.")

    # Entries by type
    st.subheader("Entrées par Type")
    by_type = stats.get("entries_by_type", {})
    if by_type:
        st.bar_chart(by_type)

    # Recent entries
    st.subheader("Entrées Récentes")
    recent = memory.get("recent_entries", [])
    if recent:
        for entry in recent[:10]:
            st.write(f"**{entry.get('expert', '?')}** — {entry.get('entry_type', '?')} ({entry.get('language', '?')})")
            if entry.get("data"):
                with st.expander("Détails"):
                    st.json(entry["data"])
    else:
        st.info("Aucune entrée récente en mémoire.")
