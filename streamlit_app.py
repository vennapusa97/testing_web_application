__import__("pysqlite3")
import sys as _sys
_sys.modules["sqlite3"] = _sys.modules.pop("pysqlite3")

"""IDAMP Studio - full UI rebuild on the "restrained graphite + gold + teal"
design direction (mockups: idamp_studio_final.html / _upload.html /
_after_results.html).

Honesty notes on where real Streamlit diverges from the static mockup:
- No true hover dropdown for Export -- it's a toggle button that reveals three
  real st.download_button widgets below it. Streamlit has no native hover
  menu; faking one with pure CSS is fragile across Streamlit versions.
- STTM approval still uses st.data_editor (styled via CSS), not a custom
  toggle-switch table -- data_editor is what makes the checkboxes/edits
  actually work; a purely decorative HTML table would need to reimplement
  editing from scratch for no functional gain.
- "Fork this run" only forks from the Gold STTM checkpoint. Forking from
  Bronze would need the original uploaded CSV paths, which the orchestrator's
  pipeline_started audit event doesn't currently log (same limitation noted
  when run-history resume was first built). Forking from Gold is fully
  supported because silver_output_paths + sttm_gold_path are already stored
  per run.
- The native Streamlit header/menu/footer are hidden via CSS so the custom
  top bar is the only chrome visible.
"""

import sys
import re
import json
import html
import uuid
import shutil
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import streamlit as st
import pandas as pd
from core.config import LANDING_DIR, STTM_DIR
from core.audit import AuditLogger
from core.run_history import list_runs, get_run, get_report_summary
from core.memory import store_insight, list_pinned_insights, delete_insight
from agents.chat_agent import GoldChatSession, log_feedback
from agents.orchestrator import (
    run_until_bronze_sttm,
    run_bronze_to_silver_sttm,
    run_silver_to_gold_sttm,
    run_gold_and_report,
)

st.set_page_config(page_title="IDAMP Studio", page_icon="\u25C9", layout="wide")

SELECTION_COL = "_selected_for_approval"
TRACE_STEPS = ["Upload", "Bronze", "Silver", "Gold", "Report"]
PHASE_TO_TRACE_INDEX = {
    "upload": 0, "bronze_sttm": 1, "bronze_load": 1,
    "silver_sttm": 2, "silver_load": 2, "gold_sttm": 3, "gold_load": 3, "report": 4,
}

STUDIO_CSS = """
<style>
#MainMenu, header[data-testid="stHeader"], footer { visibility: hidden; height: 0; }
.block-container { padding-top: 1.2rem; max-width: 920px; }

:root {
    --ink: #f4f6ff; --ink-dim: #b9c0e0; --ink-faint: #7a80a8;
    --panel: rgba(30, 27, 60, 0.55); --panel-2: rgba(40, 35, 75, 0.65); --raised: rgba(50, 44, 92, 0.75);
    --hair: rgba(180,190,255,0.14); --hair-strong: rgba(180,190,255,0.28);
    --violet: #8b5cf6; --violet-b: #a78bfa;
    --pink: #ec4899; --pink-b: #f472b6;
    --cyan: #22d3ee; --cyan-b: #67e8f9;
    --lime: #a3e635;
}

/* Animated aurora background -- real motion, not a static gradient image */
@keyframes auroraShift {
    0%   { background-position: 0% 50%; }
    50%  { background-position: 100% 50%; }
    100% { background-position: 0% 50%; }
}
.stApp {
    background: linear-gradient(-45deg, #0f0c29, #24123f, #1a0f3d, #2d1b4e, #150a2e);
    background-size: 400% 400%;
    animation: auroraShift 18s ease infinite;
    color: var(--ink);
}

/* Sticky live status bar -- pinned to top, always visible, never needs scrolling to check */
.studio-livebar {
    position: sticky; top: 0; z-index: 999; backdrop-filter: blur(14px);
    background: rgba(15,12,41,0.75); border-bottom: 1px solid var(--hair);
    padding: 12px 4px; margin: -1.2rem -1rem 18px -1rem;
}
.studio-topbar { display: flex; align-items: center; gap: 10px; padding: 0 16px 10px 16px; }
.studio-mark {
    width: 20px; height: 20px; border-radius: 6px;
    background: linear-gradient(135deg, var(--pink-b), var(--violet)); box-shadow: 0 0 16px rgba(236,72,153,0.5);
}
.studio-title { font-size: 14px; font-weight: 700; }
.studio-run-pill { font-size: 10.5px; color: var(--ink-dim); background: var(--panel); border: 1px solid var(--hair); padding: 4px 10px; border-radius: 8px; font-family: monospace; }

.studio-trace { display: flex; gap: 0; align-items: center; padding: 0 16px; }
.studio-trace-step {
    flex: 1; text-align: center; font-size: 11px; color: var(--ink-faint); font-weight: 600;
    padding: 8px 4px; border-bottom: 3px solid var(--hair); position: relative; transition: all 0.3s ease;
}
.studio-trace-step.done { color: var(--cyan-b); border-bottom-color: var(--cyan); }
.studio-trace-step.current { color: var(--pink-b); border-bottom-color: var(--pink); }
.studio-trace-step.current .live-pulse {
    display: inline-block; width: 7px; height: 7px; border-radius: 50%; background: var(--pink);
    margin-right: 5px; box-shadow: 0 0 0 0 rgba(236,72,153,0.7);
    animation: pulseGlow 1.4s ease-out infinite;
}
@keyframes pulseGlow {
    0%   { box-shadow: 0 0 0 0 rgba(236,72,153,0.7); }
    70%  { box-shadow: 0 0 0 8px rgba(236,72,153,0); }
    100% { box-shadow: 0 0 0 0 rgba(236,72,153,0); }
}

.studio-hero {
    background: var(--panel); border: 1px solid var(--hair-strong); border-radius: 18px;
    padding: 22px 24px; margin-bottom: 14px; backdrop-filter: blur(8px);
    box-shadow: 0 8px 32px rgba(139,92,246,0.15);
}
.studio-hero-label { font-size: 10.5px; color: var(--cyan-b); text-transform: uppercase; letter-spacing: 0.06em; margin-bottom: 8px; font-weight: 700; }
.studio-hero-answer { font-size: 19px; font-weight: 600; line-height: 1.45; margin-bottom: 12px; }
.studio-confidence { display: inline-flex; align-items: center; gap: 6px; font-size: 11px; color: var(--lime); background: rgba(163,230,53,0.12); padding: 5px 10px; border-radius: 999px; }
.studio-confidence-dot { width: 5px; height: 5px; border-radius: 50%; background: var(--lime); display: inline-block; }

.studio-metric-row { display: flex; gap: 10px; margin-bottom: 4px; }
.studio-metric {
    flex: 1; background: var(--panel); border: 1px solid var(--hair); border-radius: 14px; padding: 12px 14px;
    transition: transform 0.2s ease, border-color 0.2s ease;
}
.studio-metric:hover { transform: translateY(-2px); border-color: var(--violet-b); }
.studio-metric-label { font-size: 9.5px; color: var(--ink-faint); text-transform: uppercase; }
.studio-metric-val { font-size: 18px; font-weight: 700; margin-top: 2px; background: linear-gradient(90deg, var(--cyan-b), var(--violet-b)); -webkit-background-clip: text; -webkit-text-fill-color: transparent; }

.studio-pin-chip {
    display: inline-flex; align-items: center; gap: 6px; background: rgba(236,72,153,0.12);
    border: 1px solid rgba(236,72,153,0.35); border-radius: 999px; padding: 5px 11px; font-size: 10.5px;
    color: var(--pink-b); margin: 3px 4px 3px 0;
}
.studio-drawer { background: var(--panel-2); border: 1px solid var(--hair); border-radius: 14px; padding: 13px 15px; margin: 4px 0 14px; font-size: 11.5px; }
.studio-lineage-item { display: inline-block; background: var(--raised); border: 1px solid var(--hair-strong); border-radius: 6px; padding: 4px 9px; font-family: monospace; font-size: 10.5px; margin-right: 6px; color: var(--cyan-b); }
.studio-sql-block { background: rgba(0,0,0,0.35); border: 1px solid var(--hair); border-radius: 8px; padding: 10px 12px; font-family: monospace; font-size: 10.5px; color: var(--lime); white-space: pre-wrap; }
.studio-compare-card { background: var(--raised); border: 1px solid var(--hair-strong); border-radius: 12px; padding: 10px 12px; font-size: 11px; }

.studio-chat-q { background: var(--panel-2); border-radius: 12px; padding: 8px 12px; font-size: 12px; margin-bottom: 8px; margin-left: auto; max-width: 82%; }
.studio-chat-a { background: rgba(34,211,238,0.1); border: 1px solid rgba(34,211,238,0.25); border-radius: 12px; padding: 8px 12px; font-size: 12px; max-width: 88%; margin-bottom: 4px; }

.studio-end-panel {
    background: var(--panel); border: 1px solid var(--hair-strong); border-radius: 18px;
    padding: 18px 22px; text-align: center; margin-top: 18px;
    box-shadow: 0 8px 32px rgba(34,211,238,0.1);
}

/* Tactile button feedback -- visible press-down the instant you click,
   before the Streamlit rerun even completes. */
.stButton button {
    background: var(--panel) !important; border: 1px solid var(--hair-strong) !important;
    color: var(--ink-dim) !important; border-radius: 10px !important; font-size: 12px !important;
    transition: transform 0.08s ease, box-shadow 0.15s ease, border-color 0.15s ease !important;
}
.stButton button:hover { border-color: var(--violet-b) !important; color: var(--ink) !important; box-shadow: 0 0 12px rgba(139,92,246,0.3) !important; }
.stButton button:active {
    transform: scale(0.95) !important; box-shadow: 0 0 4px rgba(139,92,246,0.6) inset !important;
}
.stButton button[kind="primary"] {
    background: linear-gradient(135deg, var(--pink-b), var(--violet)) !important; color: #fff !important;
    border: none !important; font-weight: 700 !important; box-shadow: 0 4px 20px rgba(236,72,153,0.35) !important;
}
.stButton button[kind="primary"]:active { transform: scale(0.95) !important; box-shadow: 0 0 6px rgba(236,72,153,0.7) inset !important; }

.studio-upload-header {
    background: var(--panel); border: 1.5px dashed var(--hair-strong); border-radius: 16px;
    padding: 10px 16px; margin-bottom: -6px; display: flex; align-items: center; gap: 10px;
}
.studio-upload-icon {
    width: 32px; height: 32px; border-radius: 10px; background: rgba(139,92,246,0.18); color: var(--violet-b);
    display: flex; align-items: center; justify-content: center; font-size: 15px; flex-shrink: 0;
}
.studio-upload-title { font-size: 12.5px; font-weight: 600; }
.studio-upload-sub { font-size: 10.5px; color: var(--ink-faint); }

div[data-testid="stFileUploaderDropzone"] {
    background: var(--panel-2) !important; border: 1.5px dashed var(--hair-strong) !important;
    border-radius: 0 0 16px 16px !important; border-top: none !important;
}
div[data-testid="stFileUploaderDropzoneInstructions"] { color: var(--ink-dim) !important; }
section[data-testid="stFileUploadDropzone"] {
    background: var(--panel-2) !important; border: 1.5px dashed var(--hair-strong) !important;
}

.stTextArea textarea {
    background: var(--panel) !important; border: 1px solid var(--hair-strong) !important;
    color: var(--ink) !important; border-radius: 14px !important;
}
.stSelectbox div[data-baseweb="select"] { background: var(--panel) !important; border-radius: 10px !important; }

section[data-testid="stSidebar"] { background: rgba(15,12,41,0.85); border-right: 1px solid var(--hair); }
</style>
"""


def inject_studio_css():
    st.markdown(STUDIO_CSS, unsafe_allow_html=True)


def render_trace(current_phase: str):
    """Sticky, always-visible live status strip. The current step gets a
    pulsing dot (real CSS animation, genuinely 'live') so progress is
    readable at a glance without scrolling anywhere to find it."""
    idx = PHASE_TO_TRACE_INDEX.get(current_phase, 0)
    steps_html = []
    for i, step in enumerate(TRACE_STEPS):
        if i < idx:
            cls, pulse = "done", ""
        elif i == idx:
            cls, pulse = "current", "<span class='live-pulse'></span>"
        else:
            cls, pulse = "", ""
        steps_html.append(f"<div class='studio-trace-step {cls}'>{pulse}{step}</div>")

    st.markdown(
        f"""
        <div class='studio-livebar'>
            <div class='studio-topbar'>
                <div class='studio-mark'></div>
                <div class='studio-title'>IDAMP Studio</div>
                <div class='studio-run-pill'>{html.escape((st.session_state.get('current_run_id') or 'no run')[:12])}</div>
            </div>
            <div class='studio-trace'>{''.join(steps_html)}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def _prepare_sttm_editor_df(df: pd.DataFrame) -> pd.DataFrame:
    editor_df = df.copy()
    if SELECTION_COL not in editor_df.columns:
        editor_df.insert(0, SELECTION_COL, True)
    editor_df[SELECTION_COL] = editor_df[SELECTION_COL].fillna(True).astype(bool)
    for col in editor_df.columns:
        if col == SELECTION_COL:
            continue
        if editor_df[col].dtype == object:
            editor_df[col] = editor_df[col].fillna("").astype(str)
        elif pd.api.types.is_float_dtype(editor_df[col]) and editor_df[col].isna().all():
            editor_df[col] = editor_df[col].fillna("").astype(str)
    return editor_df


def _extract_selected_rows(edited_df: pd.DataFrame) -> pd.DataFrame:
    if SELECTION_COL not in edited_df.columns:
        return edited_df.copy()
    return edited_df[edited_df[SELECTION_COL]].drop(columns=[SELECTION_COL], errors="ignore")


def _reset_analysis_session():
    st.session_state.phase = "upload"
    st.session_state.pipeline_state = None
    st.session_state.current_run_id = ""
    st.session_state.chat_session = None
    st.session_state.chat_history = []
    st.session_state.open_drawer = None


def extract_query_sql(report_path: str) -> str:
    try:
        html_text = Path(report_path).read_text(encoding="utf-8")
        match = re.search(r'<pre class="code-block"><code>(.*?)</code></pre>', html_text, re.DOTALL)
        if match:
            return html.unescape(match.group(1)).strip()
    except Exception:
        pass
    return ""


def compute_confidence(sql: str, row_count: int) -> str:
    if not sql:
        return f"Based on {row_count} rows -- query not captured for this run"
    has_filter = bool(re.search(r"\bwhere\b", sql, re.IGNORECASE))
    if has_filter:
        return f"Filtered subset -- based on {row_count} rows matching a WHERE condition"
    return f"High confidence -- based on all {row_count} rows, no filtering applied"


def trace_lineage(state: dict, gold_column: str) -> list:
    chain = [f"gold:{gold_column}"]
    try:
        gold_sttm = pd.read_csv(state.get("sttm_gold_path", "")).fillna("")
        gold_row = gold_sttm[gold_sttm["target_column"] == gold_column]
        if gold_row.empty:
            return chain
        silver_col = str(gold_row.iloc[0].get("source_column", ""))
        if silver_col:
            chain.insert(0, f"silver:{silver_col}")
            silver_sttm = pd.read_csv(state.get("sttm_silver_path", "")).fillna("")
            silver_row = silver_sttm[silver_sttm["target_column"] == silver_col]
            if not silver_row.empty:
                bronze_col = str(silver_row.iloc[0].get("source_column", ""))
                if bronze_col:
                    chain.insert(0, f"bronze:{bronze_col}")
                    bronze_sttm = pd.read_csv(state.get("sttm_bronze_path", "")).fillna("")
                    bronze_row = bronze_sttm[bronze_sttm["target_column"] == bronze_col]
                    if not bronze_row.empty:
                        source_col = str(bronze_row.iloc[0].get("source_column", ""))
                        if source_col:
                            chain.insert(0, f"source:{source_col}")
    except Exception:
        pass
    return chain


def fork_run_from_gold(source_run_id: str) -> dict:
    source = get_run(source_run_id)
    if not source or not source.get("silver_output_paths") or not source.get("sttm_gold_path"):
        return {}

    new_run_id = str(uuid.uuid4())
    new_sttm_path = str(STTM_DIR / f"sttm_gold_fork_{new_run_id[:8]}.csv")
    shutil.copy(source["sttm_gold_path"], new_sttm_path)

    AuditLogger(new_run_id).log(
        "orchestrator", "pipeline_started",
        intent=source["business_intent"], status="started", phase="upload",
        rationale=f"Forked from run {source_run_id[:8]} at Gold STTM checkpoint.",
        forked_from=source_run_id,
    )

    return {
        "run_id": new_run_id,
        "status": "awaiting_gold_sttm_approval",
        "business_intent": source["business_intent"],
        "silver_output_paths": source["silver_output_paths"],
        "sttm_gold_path": new_sttm_path,
        "sttm_bronze_path": source.get("sttm_bronze_path", ""),
        "sttm_silver_path": source.get("sttm_silver_path", ""),
        "bronze_output_paths": source.get("bronze_output_paths", []),
        "error": "",
    }


def render_run_rail():
    st.sidebar.markdown("<div class='studio-hero-label' style='padding:0 4px;'>Runs</div>", unsafe_allow_html=True)
    if st.sidebar.button("+ New analysis", use_container_width=True):
        _reset_analysis_session()
        st.rerun()

    runs = list_runs(limit=20)
    current_run_id = st.session_state.get("current_run_id", "")

    for run in runs:
        is_active = run["run_id"] == current_run_id
        label = (run["business_intent"] or "(untitled)")[:34]
        dot = "\U0001F7E1" if run["status"] == "completed" else "\U0001F7E2"
        cols = st.sidebar.columns([5, 2])
        style = "font-weight:600;" if is_active else ""
        cols[0].markdown(f"<div style='font-size:12px;{style}padding-top:6px;'>{dot} {html.escape(label)}</div>", unsafe_allow_html=True)
        if run["status"] == "completed":
            if cols[1].button("Open", key=f"open_{run['run_id']}"):
                st.session_state.pipeline_state = {
                    "run_id": run["run_id"], "status": "completed",
                    "business_intent": run["business_intent"],
                    "gold_output_paths": run["gold_output_paths"],
                    "report_path": run["report_path"],
                    "sttm_bronze_path": run.get("sttm_bronze_path", ""),
                    "sttm_silver_path": run.get("sttm_silver_path", ""),
                    "sttm_gold_path": run.get("sttm_gold_path", ""),
                    "silver_output_paths": run.get("silver_output_paths", []),
                    "bronze_output_paths": run.get("bronze_output_paths", []),
                }
                st.session_state.current_run_id = run["run_id"]
                st.session_state.phase = "report"
                st.session_state.chat_session = None
                st.session_state.chat_history = []
                st.session_state.open_drawer = None
                st.rerun()


def render_live_chart_picker(state: dict):
    """Genuinely new functionality, not a restyle: lets the person pick a
    chart type and columns and see it rendered live against the actual Gold
    table, independent of whatever chart(s) the report agent baked in.
    Streamlit reruns the script on every widget change, so changing the
    selectbox redraws the chart on the next rerun automatically -- this is
    real, live interactivity, not a static pre-rendered image.
    """
    gold_paths = state.get("gold_output_paths", [])
    if not gold_paths:
        return

    st.markdown("**Explore this data \u2014 pick a chart type**")
    table_names = [Path(p).stem for p in gold_paths]
    chosen_table = st.selectbox("Gold table", table_names, key="livechart_table")
    df = pd.read_parquet(gold_paths[table_names.index(chosen_table)])

    numeric_cols = [c for c in df.columns if pd.api.types.is_numeric_dtype(df[c])]
    all_cols = list(df.columns)
    if not all_cols:
        return

    c1, c2, c3 = st.columns(3)
    chart_type = c1.selectbox("Chart type", ["Bar", "Line", "Area", "Scatter", "Pie"], key="livechart_type")
    x_col = c2.selectbox("X axis", all_cols, key="livechart_x")
    y_default_idx = numeric_cols.index(numeric_cols[0]) if numeric_cols else 0
    y_options = numeric_cols if numeric_cols else all_cols
    y_col = c3.selectbox("Y axis / value", y_options, index=y_default_idx if numeric_cols else 0, key="livechart_y")

    try:
        import plotly.express as px
        plot_df = df.copy()
        # Live chart config only accepts vibrant, saturated colors matching
        # the new palette -- avoids the chart looking mismatched against the
        # animated background.
        color_seq = ["#8b5cf6", "#ec4899", "#22d3ee", "#a3e635", "#f472b6"]

        if chart_type == "Bar":
            fig = px.bar(plot_df, x=x_col, y=y_col, color_discrete_sequence=color_seq)
        elif chart_type == "Line":
            fig = px.line(plot_df, x=x_col, y=y_col, markers=True, color_discrete_sequence=color_seq)
        elif chart_type == "Area":
            fig = px.area(plot_df, x=x_col, y=y_col, color_discrete_sequence=color_seq)
        elif chart_type == "Scatter":
            fig = px.scatter(plot_df, x=x_col, y=y_col, color_discrete_sequence=color_seq)
        else:  # Pie
            fig = px.pie(plot_df, names=x_col, values=y_col, color_discrete_sequence=color_seq)

        fig.update_layout(
            plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
            font_color="#f4f6ff", margin=dict(l=10, r=10, t=30, b=10), height=380,
        )
        st.plotly_chart(fig, use_container_width=True, key="livechart_plot")
    except Exception as e:
        st.warning(f"Couldn't render that combination: {e}")



    run_id = state.get("run_id", "")
    report_path = state.get("report_path", "")
    if not (report_path and Path(report_path).exists()):
        st.error("Report file not found.")
        return

    summary = get_report_summary(run_id) or {}
    direct_answer = summary.get("direct_answer", {}).get("answer", "")
    sql = extract_query_sql(report_path)

    st.markdown(
        f"<div class='studio-hero'>"
        f"<div class='studio-hero-label'>{html.escape((state.get('business_intent') or 'Executive report')[:80])}</div>"
        f"<div class='studio-hero-answer'>{html.escape(direct_answer or 'See full report below.')}</div>"
        f"</div>",
        unsafe_allow_html=True,
    )

    insights = list_pinned_insights(run_id=run_id, n_results=10)
    if insights:
        chips = "".join(
            f"<span class='studio-pin-chip'>\U0001F4CC {html.escape(i['metadata'].get('answer','')[:60])}</span>"
            for i in insights
        )
        st.markdown(chips, unsafe_allow_html=True)

    # Full generated report (charts, query results table, detailed analysis)
    # lives in the report HTML file, NOT in the hero card above — the hero
    # only shows the short direct_answer text pulled from the JSON sidecar.
    # This embed is what actually renders the Plotly charts.
    with st.expander("View full report (charts, data table, detailed analysis)", expanded=True):
        with open(report_path, "r", encoding="utf-8") as f:
            report_html_content = f.read()
        st.components.v1.html(report_html_content, height=1200, scrolling=True)

    render_live_chart_picker(state)

    if "open_drawer" not in st.session_state:
        st.session_state.open_drawer = None

    tcols = st.columns(6)
    labels = ["Lineage", "SQL", "Pin", "Compare", "Fork", "Export"]
    for i, label in enumerate(labels):
        if tcols[i].button(label, key=f"tool_{label}", use_container_width=True):
            st.session_state.open_drawer = None if st.session_state.open_drawer == label else label

    drawer = st.session_state.open_drawer

    if drawer == "Lineage":
        gold_paths = state.get("gold_output_paths", [])
        col_choice = None
        if gold_paths:
            try:
                cols_available = list(pd.read_parquet(gold_paths[0]).columns)
                col_choice = st.selectbox("Trace a Gold column back to its source", cols_available, key="lineage_col")
            except Exception:
                pass
        if col_choice and state.get("sttm_gold_path"):
            chain = trace_lineage(state, col_choice)
            chain_html = " -&gt; ".join(f"<span class='studio-lineage-item'>{html.escape(c)}</span>" for c in chain)
            st.markdown(f"<div class='studio-drawer'>{chain_html}</div>", unsafe_allow_html=True)
        else:
            st.markdown("<div class='studio-drawer'>No Gold table or STTM available to trace for this run.</div>", unsafe_allow_html=True)

    elif drawer == "SQL":
        st.markdown(
            f"<div class='studio-drawer'><div class='studio-sql-block'>{html.escape(sql or 'No SQL captured for this run.')}</div></div>",
            unsafe_allow_html=True,
        )

    elif drawer == "Pin":
        if st.button("\U0001F4CC Pin this answer", key="pin_hero"):
            store_insight(run_id, state.get("business_intent", "Executive summary"), direct_answer or "See full report", sql)
            st.toast("Pinned to memory")
            st.rerun()

    elif drawer == "Compare":
        other_runs = [r for r in list_runs(limit=20) if r["run_id"] != run_id and r["status"] == "completed"]
        options = {f"{(r['business_intent'] or r['run_id'][:8])[:40]}": r["run_id"] for r in other_runs}
        chosen_label = st.selectbox("Compare against", ["--"] + list(options.keys()), key="compare_pick")
        if chosen_label != "--":
            other_id = options[chosen_label]
            other_summary = get_report_summary(other_id) or {}
            other_answer = other_summary.get("direct_answer", {}).get("answer", "")
            c1, c2 = st.columns(2)
            c1.markdown(f"<div class='studio-compare-card'><b>This run</b><br>{html.escape(direct_answer or '')}</div>", unsafe_allow_html=True)
            c2.markdown(f"<div class='studio-compare-card'><b>{html.escape(chosen_label)}</b><br>{html.escape(other_answer or 'No report data')}</div>", unsafe_allow_html=True)

    elif drawer == "Fork":
        st.markdown(
            "<div class='studio-drawer'>Forks this run from its Gold STTM checkpoint -- "
            "reuses the same Silver data, lets you edit Gold rules fresh, and produces a "
            "brand-new run/report without touching the original.</div>",
            unsafe_allow_html=True,
        )
        if st.button("Fork from Gold STTM", key="fork_btn"):
            forked_state = fork_run_from_gold(run_id)
            if forked_state:
                st.session_state.pipeline_state = forked_state
                st.session_state.current_run_id = forked_state["run_id"]
                st.session_state.phase = "gold_sttm"
                st.session_state.chat_session = None
                st.session_state.chat_history = []
                st.session_state.open_drawer = None
                st.rerun()
            else:
                st.error("Can't fork -- this run is missing Silver output or Gold STTM data.")

    elif drawer == "Export":
        e1, e2, e3 = st.columns(3)
        with e1:
            with open(report_path, "rb") as f:
                st.download_button("Report (.html)", f.read(), file_name=Path(report_path).name, mime="text/html", use_container_width=True)
        with e2:
            gold_paths = state.get("gold_output_paths", [])
            if gold_paths:
                try:
                    df = pd.read_parquet(gold_paths[0])
                    st.download_button("Gold table (.csv)", df.to_csv(index=False).encode("utf-8"),
                                        file_name=f"{Path(gold_paths[0]).stem}.csv", mime="text/csv", use_container_width=True)
                except Exception:
                    pass
        with e3:
            logs = AuditLogger(run_id).get_logs()
            st.download_button("Audit trail (.json)", json.dumps(logs, indent=2),
                                file_name=f"audit_{run_id[:8]}.json", mime="application/json", use_container_width=True)

    st.markdown("<div style='height:8px'></div>", unsafe_allow_html=True)
    render_chat_panel(state)

    st.markdown(
        "<div class='studio-end-panel'><b>Analysis complete.</b><br>"
        "<span style='color:var(--ink-dim);font-size:11.5px;'>Keep chatting with this run's Gold tables any time from the run list, or start fresh.</span></div>",
        unsafe_allow_html=True,
    )
    if st.button("+ Start new analysis", key="end_new"):
        _reset_analysis_session()
        st.rerun()


def render_chat_panel(state: dict):
    run_id = state.get("run_id", "")
    gold_paths = state.get("gold_output_paths", [])
    if not gold_paths:
        return

    if st.session_state.get("chat_session") is None:
        st.session_state.chat_session = GoldChatSession(gold_paths, run_id)
    if "chat_history" not in st.session_state:
        st.session_state.chat_history = []

    st.markdown("**Ask the Gold tables**")

    for i, turn in enumerate(st.session_state.chat_history):
        st.markdown(f"<div class='studio-chat-q'>{html.escape(turn['question'])}</div>", unsafe_allow_html=True)
        st.markdown(f"<div class='studio-chat-a'>{html.escape(turn['answer'] or turn.get('error') or 'No answer.')}</div>", unsafe_allow_html=True)
        cols = st.columns([1, 1, 1, 6])
        show_key = f"chat_sql_{i}"
        if cols[0].button("SQL", key=f"chat_sql_btn_{i}"):
            st.session_state[show_key] = not st.session_state.get(show_key, False)
        if cols[1].button("\U0001F44D", key=f"chat_up_{i}"):
            log_feedback(run_id, turn["question"], "up")
            st.toast("Feedback recorded")
        if cols[2].button("\U0001F4CC", key=f"chat_pin_{i}"):
            store_insight(run_id, turn["question"], turn["answer"], turn.get("sql", ""))
            st.toast("Pinned")
        if st.session_state.get(show_key):
            st.markdown(f"<div class='studio-sql-block'>{html.escape(turn.get('sql') or 'No SQL captured.')}</div>", unsafe_allow_html=True)

        if turn.get("follow_ups"):
            fcols = st.columns(len(turn["follow_ups"]))
            for ci, fu in enumerate(turn["follow_ups"]):
                if fcols[ci].button(fu, key=f"chat_chip_{i}_{ci}", use_container_width=True):
                    _ask_chat(fu)
                    st.rerun()

    q = st.text_input("Ask another question about this Gold data...", key="chat_q_input")
    if st.button("Ask ->", key="chat_ask_btn") and q.strip():
        _ask_chat(q.strip())
        st.rerun()


def _ask_chat(question: str):
    session = st.session_state.chat_session
    with st.spinner("Querying Gold tables..."):
        result = session.ask(question)
    st.session_state.chat_history.append({
        "question": question, "answer": result.get("answer", ""),
        "sql": result.get("sql", ""), "follow_ups": result.get("follow_ups", []),
        "error": result.get("error"),
    })


inject_studio_css()

for key, default in [
    ("pipeline_state", None), ("phase", "upload"), ("current_run_id", ""),
    ("compare_run_ids", []), ("open_drawer", None),
]:
    if key not in st.session_state:
        st.session_state[key] = default

st.markdown(
    "<div class='studio-topbar'><div class='studio-mark'></div>"
    "<div class='studio-title'>IDAMP Studio</div>"
    f"<div class='studio-run-pill'>{html.escape((st.session_state.current_run_id or 'no run')[:12])}</div></div>",
    unsafe_allow_html=True,
)

render_run_rail()

if st.session_state.pipeline_state and st.session_state.pipeline_state.get("run_id"):
    st.session_state.current_run_id = st.session_state.pipeline_state["run_id"]

render_trace(st.session_state.phase)

if st.session_state.phase == "upload":
    st.markdown("### Start a new analysis")
    st.caption("Upload your raw data and describe what you want to learn from it. The pipeline handles profiling, cleansing, and materialisation \u2014 you approve each step.")

    st.markdown(
        "<div class='studio-upload-header'>"
        "<div class='studio-upload-icon'>\u2913</div>"
        "<div><div class='studio-upload-title'>Drag CSV files here</div>"
        "<div class='studio-upload-sub'>or use Browse files below \u00b7 multiple files supported</div></div>"
        "</div>",
        unsafe_allow_html=True,
    )
    uploaded_files = st.file_uploader("CSV files", type=["csv"], accept_multiple_files=True, label_visibility="collapsed")

    st.markdown("<div style='height:8px'></div>", unsafe_allow_html=True)
    st.markdown("**Business intent**")
    business_intent = st.text_area(
        "Business intent", height=90,
        placeholder="Which product category had the highest sales decline in Q4?",
        label_visibility="collapsed",
    )
    st.caption("Be specific \u2014 this drives every downstream STTM rule and the final report.")

    if st.button("Start workflow \u2192", type="primary", disabled=not (uploaded_files and business_intent)):
        saved_paths = []
        for uf in uploaded_files:
            save_path = str(LANDING_DIR / uf.name)
            with open(save_path, "wb") as f:
                f.write(uf.getbuffer())
            saved_paths.append(save_path)
        with st.spinner("Profiling data and generating Bronze STTM..."):
            result = run_until_bronze_sttm(saved_paths, business_intent)
            st.session_state.pipeline_state = result
            st.session_state.current_run_id = result.get("run_id", "")
            if result.get("error"):
                st.error(f"Error: {result['error']}")
            else:
                st.session_state.phase = "bronze_sttm"
                st.rerun()

elif st.session_state.phase == "bronze_sttm":
    st.markdown("### Review Bronze layer STTM")
    state = st.session_state.pipeline_state
    sttm_path = state.get("sttm_bronze_path", "")
    if sttm_path and Path(sttm_path).exists():
        df = pd.read_csv(sttm_path)
        edited_df = st.data_editor(_prepare_sttm_editor_df(df), use_container_width=True, num_rows="fixed", hide_index=True, key="bronze_editor", height=380)
        selected_df = _extract_selected_rows(edited_df)
        if st.button("Approve & continue", type="primary", disabled=selected_df.empty):
            selected_df.to_csv(sttm_path, index=False)
            with st.spinner("Executing Bronze layer and generating Silver STTM..."):
                result = run_bronze_to_silver_sttm(state)
                st.session_state.pipeline_state = result
                if result.get("error"):
                    st.error(f"Error: {result['error']}")
                else:
                    st.session_state.phase = "silver_sttm"
                    st.rerun()
    else:
        st.error("Bronze STTM file not found.")

elif st.session_state.phase == "silver_sttm":
    st.markdown("### Review Silver layer STTM")
    state = st.session_state.pipeline_state
    sttm_path = state.get("sttm_silver_path", "")
    if sttm_path and Path(sttm_path).exists():
        df = pd.read_csv(sttm_path)
        edited_df = st.data_editor(_prepare_sttm_editor_df(df), use_container_width=True, num_rows="fixed", hide_index=True, key="silver_editor", height=380)
        selected_df = _extract_selected_rows(edited_df)
        if st.button("Approve & continue", type="primary", disabled=selected_df.empty):
            selected_df.to_csv(sttm_path, index=False)
            with st.spinner("Executing Silver layer and generating Gold STTM..."):
                result = run_silver_to_gold_sttm(state)
                st.session_state.pipeline_state = result
                if result.get("error"):
                    st.error(f"Error: {result['error']}")
                else:
                    st.session_state.phase = "gold_sttm"
                    st.rerun()
    else:
        st.error("Silver STTM file not found.")

elif st.session_state.phase == "gold_sttm":
    st.markdown("### Review Gold layer STTM")
    state = st.session_state.pipeline_state
    sttm_path = state.get("sttm_gold_path", "")
    if sttm_path and Path(sttm_path).exists():
        df = pd.read_csv(sttm_path)
        edited_df = st.data_editor(_prepare_sttm_editor_df(df), use_container_width=True, num_rows="fixed", hide_index=True, key="gold_editor", height=380)
        selected_df = _extract_selected_rows(edited_df)
        if st.button("Approve & execute", type="primary", disabled=selected_df.empty):
            selected_df.to_csv(sttm_path, index=False)
            with st.spinner("Executing Gold layer and generating report..."):
                result = run_gold_and_report(state)
                st.session_state.pipeline_state = result
                if result.get("error"):
                    st.error(f"Error: {result['error']}")
                else:
                    st.session_state.phase = "report"
                    st.rerun()
    else:
        st.error("Gold STTM file not found.")

elif st.session_state.phase == "report":
    render_results_screen(st.session_state.pipeline_state)
