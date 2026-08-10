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
.block-container { padding-top: 1.2rem; max-width: 900px; }

:root {
    --canvas: #0d0d0f; --panel: #16161a; --panel-2: #1c1c21; --raised: #202026;
    --hair: rgba(255,255,255,0.07); --hair-strong: rgba(255,255,255,0.12);
    --ink: #ececef; --ink-dim: #9a9aa3; --ink-faint: #5c5c66;
    --gold: #e8b34a; --gold-dim: rgba(232,179,74,0.14);
    --teal: #45c4a8; --teal-dim: rgba(69,196,168,0.12);
}
.stApp { background-color: var(--canvas); color: var(--ink); }

.studio-topbar {
    display: flex; align-items: center; gap: 10px; padding: 6px 0 18px 0;
    border-bottom: 1px solid var(--hair); margin-bottom: 18px;
}
.studio-mark { width: 18px; height: 18px; border-radius: 5px; background: var(--gold); }
.studio-title { font-size: 14px; font-weight: 600; }
.studio-run-pill { font-size: 10.5px; color: var(--ink-dim); background: var(--panel); border: 1px solid var(--hair); padding: 4px 10px; border-radius: 8px; font-family: monospace; }

.studio-trace { display: flex; gap: 6px; align-items: center; font-size: 11px; color: var(--ink-faint); margin-bottom: 20px; }
.studio-trace .step { color: var(--ink-dim); }
.studio-trace .step.current { color: var(--gold); font-weight: 600; }
.studio-trace .sep { opacity: 0.4; }

.studio-hero { background: var(--panel); border: 1px solid var(--hair-strong); border-radius: 16px; padding: 20px 22px; margin-bottom: 14px; }
.studio-hero-label { font-size: 10.5px; color: var(--ink-faint); text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 8px; }
.studio-hero-answer { font-size: 18px; font-weight: 500; line-height: 1.45; margin-bottom: 12px; }
.studio-confidence { display: inline-flex; align-items: center; gap: 6px; font-size: 11px; color: var(--teal); background: var(--teal-dim); padding: 5px 10px; border-radius: 999px; }
.studio-confidence-dot { width: 5px; height: 5px; border-radius: 50%; background: var(--teal); display: inline-block; }

.studio-metric-row { display: flex; gap: 10px; margin-bottom: 4px; }
.studio-metric { flex: 1; background: var(--panel); border: 1px solid var(--hair); border-radius: 12px; padding: 11px 13px; }
.studio-metric-label { font-size: 9.5px; color: var(--ink-faint); text-transform: uppercase; }
.studio-metric-val { font-size: 17px; font-weight: 600; margin-top: 2px; }

.studio-pin-chip {
    display: inline-flex; align-items: center; gap: 6px; background: var(--gold-dim);
    border: 1px solid rgba(232,179,74,0.3); border-radius: 999px; padding: 5px 11px; font-size: 10.5px;
    color: var(--gold); margin: 3px 4px 3px 0;
}
.studio-drawer { background: var(--panel-2); border: 1px solid var(--hair); border-radius: 12px; padding: 13px 15px; margin: 4px 0 14px; font-size: 11.5px; }
.studio-lineage-item { display: inline-block; background: var(--raised); border: 1px solid var(--hair-strong); border-radius: 6px; padding: 4px 9px; font-family: monospace; font-size: 10.5px; margin-right: 6px; }
.studio-sql-block { background: var(--canvas); border: 1px solid var(--hair); border-radius: 8px; padding: 10px 12px; font-family: monospace; font-size: 10.5px; color: var(--teal); white-space: pre-wrap; }
.studio-compare-card { background: var(--raised); border: 1px solid var(--hair-strong); border-radius: 10px; padding: 10px 12px; font-size: 11px; }

.studio-chat-q { background: var(--panel-2); border-radius: 10px; padding: 8px 12px; font-size: 12px; margin-bottom: 8px; margin-left: auto; max-width: 82%; }
.studio-chat-a { background: var(--teal-dim); border-radius: 10px; padding: 8px 12px; font-size: 12px; max-width: 88%; margin-bottom: 4px; }

.studio-end-panel { background: var(--panel); border: 1px solid var(--hair-strong); border-radius: 16px; padding: 18px 22px; text-align: center; margin-top: 18px; }

.stButton button {
    background: var(--panel) !important; border: 1px solid var(--hair) !important;
    color: var(--ink-dim) !important; border-radius: 8px !important; font-size: 12px !important;
}
.stButton button:hover { border-color: var(--hair-strong) !important; color: var(--ink) !important; }
.stButton button[kind="primary"] {
    background: var(--gold) !important; color: #2a1c04 !important; border: none !important; font-weight: 700 !important;
}

section[data-testid="stSidebar"] { background-color: var(--panel); border-right: 1px solid var(--hair); }
</style>
"""


def inject_studio_css():
    st.markdown(STUDIO_CSS, unsafe_allow_html=True)


def render_trace(current_phase: str):
    idx = PHASE_TO_TRACE_INDEX.get(current_phase, 0)
    parts = ["<div class='studio-trace'>"]
    for i, step in enumerate(TRACE_STEPS):
        cls = "current" if i == idx else "step"
        parts.append(f"<span class='step {cls}'>{step}</span>")
        if i < len(TRACE_STEPS) - 1:
            parts.append("<span class='sep'>/</span>")
    parts.append("</div>")
    st.markdown("".join(parts), unsafe_allow_html=True)


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


def render_results_screen(state: dict):
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
    st.caption("Upload your raw data and describe what you want to learn from it.")
    uploaded_files = st.file_uploader("CSV files", type=["csv"], accept_multiple_files=True)
    business_intent = st.text_area("Business intent", height=90, placeholder="Which product category had the highest sales decline in Q4?")

    if st.button("Start workflow ->", type="primary", disabled=not (uploaded_files and business_intent)):
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
