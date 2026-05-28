"""
Collegue MCP -- Streamlit Monitoring Dashboard.

Provides a real-time web interface (port 4125) to visualize:
- Expert system health (scores, activity)
- LLM conversations (inputs/outputs per expert)
- Metrics (latency, tokens, costs, errors)
- Delegation chains and inter-expert communication
- Project memory statistics and write history

All data is read from disk-backed stores so the dashboard
(separate process) sees updates from the MCP server in real-time.
"""

import sys
from pathlib import Path

import streamlit as st

# Ensure the project root is importable
_project_root = Path(__file__).resolve().parent.parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from collegue.dashboard.data import (
    get_activity_data,
    get_dashboard_data,
    get_delegation_data,
    get_memory_stats,
    get_metrics_data,
)

# -- Page config ---------------------------------------------------------------

st.set_page_config(
    page_title="Collegue MCP -- Dashboard",
    page_icon="",
    layout="wide",
    initial_sidebar_state="expanded",
)

# -- Custom CSS ----------------------------------------------------------------

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
    .llm-prompt { background: #f0f4ff; border-left: 3px solid #4a90d9;
                  padding: 8px 12px; margin: 4px 0; border-radius: 4px;
                  font-size: 0.85rem; }
    .llm-response { background: #f0fff0; border-left: 3px solid #4caf50;
                    padding: 8px 12px; margin: 4px 0; border-radius: 4px;
                    font-size: 0.85rem; }
    .delegation-event { background: #fff8f0; border-left: 3px solid #ff9800;
                        padding: 8px 12px; margin: 4px 0; border-radius: 4px; }
    .memory-event { background: #f8f0ff; border-left: 3px solid #9c27b0;
                    padding: 8px 12px; margin: 4px 0; border-radius: 4px; }
</style>
""",
    unsafe_allow_html=True,
)

# -- Sidebar -------------------------------------------------------------------

with st.sidebar:
    st.title("Collegue MCP")
    st.caption("Monitoring Dashboard")
    st.divider()

    auto_refresh = st.toggle("Auto-refresh (5s)", value=False)

    st.divider()
    st.markdown("**Serveur MCP**")
    st.code("http://localhost:4121/mcp/", language=None)
    st.markdown("**Dashboard**")
    st.code("http://localhost:4125", language=None)

# -- Helpers -------------------------------------------------------------------


def _escape_html(text: str) -> str:
    """Escape HTML special chars for safe display."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\n", "<br/>")


# -- Main Content --------------------------------------------------------------

st.title("Dashboard Monitoring")

# Tabs
tab_overview, tab_activity, tab_metrics, tab_delegation, tab_memory = st.tabs(
    ["Vue d'ensemble", "Activite LLM", "Metriques", "Delegation", "Memoire"]
)

# ==============================================================================
# TAB 1: Overview
# ==============================================================================

with tab_overview:
    dashboard = get_dashboard_data()

    # Health scores
    st.subheader("Sante du Projet")
    health = dashboard.get("project_health", {})

    col1, col2, col3, col4, col5 = st.columns(5)
    col1.metric("Global", f"{health.get('overall_score', 0):.0%}")
    col2.metric("Qualite", f"{health.get('quality_score', 0) or 0:.0%}")
    col3.metric("Architecture", f"{health.get('architecture_score', 0) or 0:.0%}")
    col4.metric("Performance", f"{health.get('performance_score', 0) or 0:.0%}")
    col5.metric("Securite", f"{health.get('security_score', 0) or 0:.0%}")

    st.divider()

    # Expert statuses
    st.subheader("Statut des Experts")
    statuses = dashboard.get("expert_statuses", [])

    if statuses:
        cols = st.columns(min(len(statuses), 4))
        for i, expert in enumerate(statuses):
            with cols[i % 4]:
                score = expert.get("last_score")
                score_str = f"{score:.0%}" if score is not None else "--"
                execs = expert.get("total_executions", 0)
                st.metric(
                    label=expert.get("name", "?"),
                    value=score_str,
                    delta=f"{execs} executions",
                )
    else:
        st.info("Aucune activite d'expert enregistree.")

    st.divider()

    # Recent expert results from activity log
    st.subheader("Resultats Recents")
    activity = get_activity_data()
    expert_results = activity.get("expert_results", [])
    if expert_results:
        for res in reversed(expert_results[-10:]):
            status_icon = "OK" if res.get("status") == "success" else "ERR"
            score_str = f" score={res['score']:.2f}" if res.get("score") is not None else ""
            iters_str = f" iter={res['iterations']}" if res.get("iterations") else ""
            findings_str = f" findings={res['findings_count']}" if res.get("findings_count") else ""
            st.text(
                f"[{res.get('time', '?')}] [{status_icon}] {res.get('expert', '?')} "
                f"({res.get('duration_s', 0):.1f}s){score_str}{iters_str}{findings_str}"
            )
            if res.get("summary"):
                with st.expander("Resume"):
                    st.write(res["summary"])
    else:
        st.info("Aucun resultat d'expert enregistre.")

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

# ==============================================================================
# TAB 2: LLM Activity (Conversations)
# ==============================================================================

with tab_activity:
    activity = get_activity_data()
    llm_calls = activity.get("llm_calls", [])
    experts_seen = activity.get("experts_seen", [])

    st.subheader("Conversations LLM")

    # Summary metrics
    col1, col2, col3 = st.columns(3)
    col1.metric("Appels LLM", activity.get("total_llm_calls", 0))
    col2.metric("Resultats experts", activity.get("total_results", 0))
    col3.metric("Delegations", activity.get("total_delegations", 0))

    st.divider()

    # Filter by expert
    filter_expert = st.selectbox(
        "Filtrer par expert",
        ["Tous"] + experts_seen,
        key="llm_filter",
    )

    filtered_calls = llm_calls
    if filter_expert != "Tous":
        filtered_calls = [c for c in llm_calls if c.get("expert") == filter_expert]

    if filtered_calls:
        for call in reversed(filtered_calls[-20:]):
            expert_name = call.get("expert", "?")
            duration = call.get("duration_s", 0)
            iteration = call.get("iteration", 1)
            time_str = call.get("time", "")

            st.markdown(
                f"**{expert_name}** -- iteration {iteration} "
                f"({duration:.1f}s) [{time_str}]"
            )

            # Prompt
            prompt = call.get("prompt", "")
            if prompt:
                st.markdown(
                    f'<div class="llm-prompt"><strong>Prompt:</strong><br/>'
                    f"{_escape_html(prompt[:300])}{'...' if len(prompt) > 300 else ''}</div>",
                    unsafe_allow_html=True,
                )

            # Response
            response = call.get("response", "")
            if response:
                st.markdown(
                    f'<div class="llm-response"><strong>Reponse LLM:</strong><br/>'
                    f"{_escape_html(response[:500])}{'...' if len(response) > 500 else ''}</div>",
                    unsafe_allow_html=True,
                )

            # Full details expander
            with st.expander("Voir tout"):
                st.text(f"Prompt complet:\n{prompt}")
                st.divider()
                st.text(f"Reponse complete:\n{response}")

            st.divider()
    else:
        st.info("Aucun appel LLM enregistre. Lancez une analyse pour voir les conversations ici.")

    # Delegation messages
    delegations = activity.get("delegations", [])
    if delegations:
        st.subheader("Communications Inter-Experts")
        for deleg in reversed(delegations[-10:]):
            src = deleg.get("source", "?")
            tgt = deleg.get("target", "?")
            reason = deleg.get("reason", "")
            time_str = deleg.get("time", "")
            st.markdown(
                f'<div class="delegation-event">'
                f"<strong>{src}</strong> → <strong>{tgt}</strong> [{time_str}]<br/>"
                f"Raison: {_escape_html(reason)}</div>",
                unsafe_allow_html=True,
            )
            params = deleg.get("params", {})
            if params:
                with st.expander("Parametres"):
                    st.json(params)

# ==============================================================================
# TAB 3: Metrics
# ==============================================================================

with tab_metrics:
    metrics = get_metrics_data()

    summary = metrics.get("summary", {})
    experts_metrics = metrics.get("experts", {})

    # Global metrics
    st.subheader("Metriques Globales")
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Executions totales", summary.get("total_executions", 0))
    col2.metric("Cout total", f"${summary.get('total_cost_usd', 0):.4f}")
    col3.metric("Erreurs", summary.get("total_errors", 0))
    col4.metric("Latence moy.", f"{summary.get('avg_latency_ms', 0):.0f} ms")

    st.divider()

    # Per-expert metrics table
    st.subheader("Metriques par Expert")

    if experts_metrics:
        import pandas as pd

        rows = []
        for name, data in experts_metrics.items():
            rows.append(
                {
                    "Expert": name,
                    "Executions": data.get("total_executions", 0),
                    "Succes": f"{data.get('success_rate', 0):.0%}",
                    "Latence moy.": f"{data.get('avg_latency_ms', 0):.0f} ms",
                    "P95": f"{data.get('p95_latency_ms', 0):.0f} ms",
                    "Tokens (in)": data.get("total_input_tokens", 0),
                    "Tokens (out)": data.get("total_output_tokens", 0),
                    "Cout": f"${data.get('total_cost_usd', 0):.5f}",
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
        st.subheader("Repartition des Couts")
        cost_data = {name: data.get("total_cost_usd", 0) for name, data in experts_metrics.items()}
        if any(v > 0 for v in cost_data.values()):
            st.bar_chart(cost_data)
        else:
            st.info("Aucun cout enregistre pour le moment.")

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
            st.success("Aucune erreur enregistree.")
    else:
        st.info("Aucune metrique disponible. Les metriques s'afficheront apres les premieres executions d'experts.")

# ==============================================================================
# TAB 4: Delegation
# ==============================================================================

with tab_delegation:
    delegation = get_delegation_data()

    st.subheader("Activite de Delegation")
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Chaines executees", delegation.get("total_chains", 0))
    col2.metric("Regles actives", delegation.get("total_rules", 0))
    col3.metric("Source la + active", delegation.get("most_active_source") or "--")
    col4.metric("Cible la + active", delegation.get("most_active_target") or "--")

    st.divider()

    # Rules table
    st.subheader("Regles de Delegation")
    rules = delegation.get("rules", [])
    if rules:
        import pandas as pd

        rules_df = pd.DataFrame(rules)
        st.dataframe(rules_df, use_container_width=True, hide_index=True)
    else:
        st.info("Aucune regle de delegation chargee.")

    # Chain history (now includes activity log events)
    st.subheader("Historique des Delegations")
    chain_history = delegation.get("chain_history", [])
    if chain_history:
        for chain in reversed(chain_history[-15:]):
            time_str = chain.get("time", "")
            reason = chain.get("reason", "")
            st.markdown(
                f"`{chain.get('source', '?')}` → `{chain.get('target', '?')}` "
                f"({chain.get('status', '?')}) {f'[{time_str}]' if time_str else ''}"
            )
            if reason:
                st.caption(f"Raison: {reason}")
            params = chain.get("params", {})
            if params:
                with st.expander("Parametres envoyes"):
                    st.json(params)
    else:
        st.info("Aucune delegation executee pour le moment.")

# ==============================================================================
# TAB 5: Memory
# ==============================================================================

with tab_memory:
    memory = get_memory_stats()

    st.subheader("Statistiques de la Memoire Projet")

    stats = memory.get("stats", {})
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Entrees totales", stats.get("total_entries", 0))
    col2.metric("Experts actifs", stats.get("experts_count", 0))
    col3.metric("Categories", stats.get("categories_count", 0))
    col4.metric("Langages", stats.get("languages_count", 0))

    st.divider()

    # Entries by expert
    st.subheader("Entrees par Expert")
    by_expert = stats.get("entries_by_expert", {})
    if by_expert:
        st.bar_chart(by_expert)
    else:
        st.info("Aucune entree en memoire.")

    # Entries by type
    st.subheader("Entrees par Type")
    by_type = stats.get("entries_by_type", {})
    if by_type:
        st.bar_chart(by_type)

    st.divider()

    # Memory write events (who added what, when)
    st.subheader("Ajouts Recents en Memoire")
    memory_writes = memory.get("memory_writes", [])
    if memory_writes:
        for mw in reversed(memory_writes[-15:]):
            expert_name = mw.get("expert", "?")
            entry_type = mw.get("entry_type", "?")
            title = mw.get("title", "?")
            category = mw.get("category", "")
            score = mw.get("score", 0)
            time_str = mw.get("time", "")
            st.markdown(
                f'<div class="memory-event">'
                f"<strong>{expert_name}</strong> a ajoute: "
                f"<em>{title}</em> (type={entry_type}, cat={category}"
                f"{f', score={score:.2f}' if score else ''}) [{time_str}]</div>",
                unsafe_allow_html=True,
            )
    else:
        st.info("Aucun ajout en memoire enregistre dans le log d'activite.")

    st.divider()

    # Recent entries (from ProjectMemory)
    st.subheader("Contenu de la Memoire")
    recent = memory.get("recent_entries", [])
    if recent:
        for entry in recent[:10]:
            st.write(f"**{entry.get('expert', '?')}** -- {entry.get('entry_type', '?')} ({entry.get('language', '?')})")
            if entry.get("data"):
                with st.expander("Details"):
                    st.json(entry["data"])
    else:
        st.info("Aucune entree recente en memoire.")


# -- Auto-refresh at the end (after all data is loaded) ------------------------

if auto_refresh:
    import time as _time

    _time.sleep(5)
    st.rerun()
