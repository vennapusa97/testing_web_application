__import__("pysqlite3")
import sys as _sys
_sys.modules["sqlite3"] = _sys.modules.pop("pysqlite3")

"""IDAMP Streamlit control surface.

Human-facing entry point for the Intent-Driven Agentic Medallion pipeline.
Unchanged from the original: five-phase HITL flow (upload -> Bronze STTM ->
Silver STTM -> Gold STTM -> report), driven by agents/orchestrator.py.

NEW in this version:
- Theme toggle: "Yellow_theme" (bronze/silver/gold thread-styled cards) vs
  "Kanban-Ocean" (blue/teal kanban board across Bronze/Silver/Gold/Report).
- Run history sidebar (core/run_history.py) — reopen a completed run's report
  and chat, or see status of an in-progress run, without losing old work.
- Gold-table chat (agents/chat_agent.py) on the report screen — fast Q&A
  against already-materialized Gold Parquet, independent of full report
  regeneration.
- Six results-screen features: View/Hide SQL per answer, agent-proposed
  follow-up chips, pin-to-memory (core/memory.py), export (report/Gold
  CSV/audit JSON), compare runs (headline metric side by side), thumbs
  up/down feedback logged to the audit trail.

Known scope limit (flagged, not hidden): "resume mid-pipeline from history"
only works today for runs that reached the report phase. Resuming an
in-progress run (e.g. one still awaiting Silver STTM approval) would need
uploaded_files captured in the pipeline_started audit event, which the
current orchestrator does not log. Noted as a follow-up, not implemented
here to avoid silently pretending resume works when it would resume with
missing input files.
"""

import sys
import json
import html
from textwrap import dedent
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import streamlit as st
import pandas as pd
from core.config import LANDING_DIR, STTM_DIR, REPORTS_DIR
from core.audit import AuditLogger
from core.run_history import list_runs, get_run, get_report_summary
from core.memory import store_insight, list_pinned_insights
from agents.chat_agent import GoldChatSession, log_feedback
from agents.orchestrator import (
    run_until_bronze_sttm,
    run_bronze_to_silver_sttm,
    run_silver_to_gold_sttm,
    run_gold_and_report,
)

st.set_page_config(
    page_title="IDAMP - Intent-Driven Agentic Medallion Pipeline",
    page_icon="\U0001F3D7\uFE0F",
    layout="wide",
)

SELECTION_COL = "_selected_for_approval"

PROGRESS_STEPS = [
    ("upload", "Upload & Intent"),
    ("bronze_sttm", "Bronze STTM Review"),
    ("bronze_load", "Bronze Layer Load"),
    ("silver_sttm", "Silver STTM Review"),
    ("silver_load", "Silver Layer Load"),
    ("gold_sttm", "Gold STTM Review"),
    ("gold_load", "Gold Layer Load"),
    ("report", "Executive Report"),
]

# ---------------------------------------------------------------------------
# Theme definitions
# ---------------------------------------------------------------------------

THEMES = {
    "yellow": {
        "label": "Yellow_theme",
        "canvas": "#1a1613", "canvas2": "#211c18", "panel": "#26201b", "panel2": "#2e2621",
        "hair": "rgba(214,190,158,0.14)", "hair_strong": "rgba(214,190,158,0.28)",
        "ink": "#f1e9de", "ink_dim": "#a89a89", "ink_faint": "#6f6459",
        "p1": "#c17a4f", "p1b": "#e39a6c", "p2": "#b9c2cc", "p2b": "#dde3e9",
        "p3": "#e3b04b", "p3b": "#f5cf7c", "accent": "#4fb8a8", "accent_ink": "#0d2b26",
    },
    "kanban": {
        "label": "Kanban-Ocean",
        "canvas": "#0c1620", "canvas2": "#131e29", "panel": "#121f2c", "panel2": "#182534",
        "hair": "rgba(150,190,220,0.13)", "hair_strong": "rgba(150,190,220,0.26)",
        "ink": "#e7f0f7", "ink_dim": "#93aec2", "ink_faint": "#5d7186",
        "p1": "#3d7ec9", "p1b": "#6ba3e6", "p2": "#6fd8c9", "p2b": "#9be8dd",
        "p3": "#f0a84c", "p3b": "#f5c078", "accent": "#3d7ec9", "accent_ink": "#eaf3fb",
    },
}


def inject_theme_css(theme_key: str) -> None:
    t = THEMES[theme_key]
    st.markdown(
        f"""
        <style>
        .stApp {{ background-color: {t['canvas']}; }}
        .idamp-card {{
            background: {t['panel']}; border: 1px solid {t['hair']};
            border-radius: 14px; padding: 16px 18px; margin-bottom: 12px;
        }}
        .idamp-card-strong {{ border-color: {t['hair_strong']}; }}
        .idamp-pill {{
            font-size: 10px; font-weight: 700; padding: 3px 9px; border-radius: 999px;
            text-transform: uppercase; letter-spacing: 0.03em; display: inline-block;
        }}
        .idamp-answer {{ color: {t['p3b']}; font-size: 13px; margin-bottom: 10px; }}
        .idamp-chip {{
            display: inline-block; background: {t['panel2']}; border: 1px solid {t['hair_strong']};
            border-radius: 999px; padding: 5px 12px; font-size: 11px; color: {t['ink_dim']};
            margin: 3px 4px 3px 0;
        }}
        .idamp-q-bubble {{
            background: {t['panel2']}; border-radius: 10px; padding: 9px 12px; font-size: 12.5px;
            margin-bottom: 6px; max-width: 82%; margin-left: auto; color: {t['ink']};
        }}
        .idamp-a-bubble {{
            background: rgba(79,184,168,0.08); border: 1px solid rgba(79,184,168,0.22);
            border-radius: 10px; padding: 9px 12px; font-size: 12.5px; margin-bottom: 4px;
            max-width: 86%; color: {t['ink']};
        }}
        .idamp-sql-box {{
            background: {t['canvas2']}; border: 1px solid {t['hair']}; border-radius: 8px;
            padding: 8px 10px; font-size: 10.5px; color: #8fbf9f; margin-top: 6px;
            font-family: 'JetBrains Mono', monospace; white-space: pre-wrap;
        }}
        .idamp-pin-chip {{
            display: inline-block; background: rgba(227,176,75,0.1); border: 1px solid rgba(227,176,75,0.3);
            border-radius: 999px; padding: 5px 12px; font-size: 10.5px; color: {t['p3b']};
            margin: 3px 4px 3px 0;
        }}
        .idamp-run-card {{
            padding: 9px 10px; border-radius: 10px; margin-bottom: 6px; border: 1px solid transparent;
            background: {t['panel']};
        }}
        .idamp-run-card.active {{ border-color: {t['hair_strong']}; }}
        .idamp-run-title {{ font-size: 12px; font-weight: 600; color: {t['ink']}; margin-bottom: 2px; }}
        .idamp-run-meta {{ font-size: 10px; color: {t['ink_faint']}; }}
        .idamp-kb-board {{ display: grid; grid-template-columns: repeat(4,1fr); gap: 12px; margin-bottom: 14px; }}
        .idamp-kb-col {{
            background: {t['panel']}; border: 1px solid {t['hair']}; border-radius: 12px; padding: 10px;
        }}
        .idamp-kb-head {{
            font-size: 10px; font-weight: 700; color: {t['ink_dim']}; text-transform: uppercase;
            letter-spacing: 0.04em; margin-bottom: 8px; padding-bottom: 6px; border-bottom: 2px solid {t['p1']};
        }}
        .idamp-kb-card {{
            background: {t['panel2']}; border: 1px solid {t['hair']}; border-radius: 8px;
            padding: 8px 9px; margin-bottom: 6px; font-size: 10.5px; color: {t['ink_dim']};
        }}
        </style>
        """,
        unsafe_allow_html=True,
    )
    return t


# ---------------------------------------------------------------------------
# Existing helpers (unchanged behaviour)
# ---------------------------------------------------------------------------

def render_progress_banner(current_phase: str, state: dict | None = None, report_complete: bool = False) -> None:
    phase_to_index = {phase: index for index, (phase, _) in enumerate(PROGRESS_STEPS)}
    current_index = phase_to_index.get(current_phase, 0)
    report_path = (state or {}).get("report_path", "")
    report_exists = bool(report_path) and Path(str(report_path)).exists()
    if current_phase == "report" and (report_complete or report_exists):
        current_index += 1

    parts = ["<div style='display:flex;gap:6px;margin-bottom:16px;flex-wrap:wrap;'>"]
    for index, (_, label) in enumerate(PROGRESS_STEPS):
        if index < current_index:
            marker, bg = "\u2713", "#1ec978"
        elif index == current_index:
            marker, bg = str(index + 1), "#f3b63e"
        else:
            marker, bg = str(index + 1), "#5b677a"
        parts.append(
            f"<div style='display:flex;align-items:center;gap:6px;font-size:11px;color:#c8d1df;'>"
            f"<span style='width:22px;height:22px;border-radius:50%;background:{bg};color:#1b1f27;"
            f"display:flex;align-items:center;justify-content:center;font-weight:700;font-size:10px;'>{marker}</span>"
            f"{label}</div>"
        )
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


def _current_audit_logs() -> list[dict]:
    run_id = st.session_state.get("current_run_id", "")
    if not run_id:
        return []
    return AuditLogger(run_id).get_logs()


def _reset_analysis_session() -> None:
    st.session_state.phase = "upload"
    st.session_state.pipeline_state = None
    st.session_state.current_run_id = ""
    st.session_state.chat_session = None
    st.session_state.chat_history = []


# ---------------------------------------------------------------------------
# NEW: Run history sidebar
# ---------------------------------------------------------------------------

def render_run_history_sidebar() -> None:
    st.sidebar.markdown("### Run history")
    if st.sidebar.button("+ New analysis", use_container_width=True):
        _reset_analysis_session()
        st.rerun()

    runs = list_runs(limit=20)
    if not runs:
        st.sidebar.caption("No past runs yet.")
        return

    current_run_id = st.session_state.get("current_run_id", "")

    for run in runs:
        is_active = run["run_id"] == current_run_id
        intent_preview = (run["business_intent"] or "(no intent captured)")[:60]
        status = run["status"]
        badge = {
            "completed": "\u2713 done",
            "failed": "\u2717 failed",
        }.get(status, status.replace("_", " "))

        with st.sidebar.container():
            st.markdown(
                f"<div class='idamp-run-card {'active' if is_active else ''}'>"
                f"<div class='idamp-run-title'>{html.escape(intent_preview)}</div>"
                f"<div class='idamp-run-meta'>{html.escape(badge)} \u00b7 {run['run_id'][:8]}</div>"
                f"</div>",
                unsafe_allow_html=True,
            )
            cols = st.sidebar.columns([1, 1])
            if status == "completed":
                if cols[0].button("Open", key=f"open_{run['run_id']}", use_container_width=True):
                    _open_completed_run(run)
                    st.rerun()
            else:
                cols[0].caption("in progress")
            if cols[1].button("Compare", key=f"cmp_{run['run_id']}", use_container_width=True):
                selected = st.session_state.get("compare_run_ids", [])
                if run["run_id"] not in selected:
                    selected.append(run["run_id"])
                st.session_state.compare_run_ids = selected[-2:]
                st.rerun()


def _open_completed_run(run: dict) -> None:
    """Reopen a completed run's report + chat. See module docstring for the
    scope limit on resuming runs that are still mid-pipeline."""
    st.session_state.pipeline_state = {
        "run_id": run["run_id"],
        "status": "completed",
        "business_intent": run["business_intent"],
        "gold_output_paths": run["gold_output_paths"],
        "report_path": run["report_path"],
    }
    st.session_state.current_run_id = run["run_id"]
    st.session_state.phase = "report"
    st.session_state.chat_session = None
    st.session_state.chat_history = []


# ---------------------------------------------------------------------------
# NEW: Kanban board (Kanban-Ocean theme structural element)
# ---------------------------------------------------------------------------

def render_kanban_board(phase: str, state: dict) -> None:
    def col_status(done: bool, current: bool) -> str:
        if done:
            return "\u2713 done"
        if current:
            return "in progress"
        return "pending"

    phase_order = ["upload", "bronze_sttm", "bronze_load", "silver_sttm", "silver_load", "gold_sttm", "gold_load", "report"]
    idx = phase_order.index(phase) if phase in phase_order else 0

    bronze_done = idx > 2
    silver_done = idx > 4
    gold_done = idx > 6
    report_done = phase == "report" and bool(state.get("report_path"))

    cols_html = ["<div class='idamp-kb-board'>"]
    for label, done, current in [
        ("Bronze", bronze_done, idx in (1, 2)),
        ("Silver", silver_done, idx in (3, 4)),
        ("Gold", gold_done, idx in (5, 6)),
        ("Report", report_done, idx == 7),
    ]:
        status = col_status(done, current)
        cols_html.append(
            f"<div class='idamp-kb-col'><div class='idamp-kb-head'>{label}</div>"
            f"<div class='idamp-kb-card'>{status}</div></div>"
        )
    cols_html.append("</div>")
    st.markdown("".join(cols_html), unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# NEW: Results-screen features (shared by both themes)
# ---------------------------------------------------------------------------

def render_compare_strip(theme: dict, current_run_id: str) -> None:
    compare_ids = st.session_state.get("compare_run_ids", [])
    if not compare_ids:
        return
    st.markdown("**Compare runs**")
    cols = st.columns(len(compare_ids))
    for i, rid in enumerate(compare_ids):
        summary = get_report_summary(rid)
        answer = ""
        if summary:
            answer = summary.get("direct_answer", {}).get("answer", "")
        run_meta = get_run(rid) or {}
        with cols[i]:
            st.markdown(
                f"<div class='idamp-card'><div style='font-size:11px;color:{theme['ink_dim']};margin-bottom:4px;'>"
                f"{html.escape((run_meta.get('business_intent') or rid[:8])[:40])}</div>"
                f"<div style='font-size:12.5px;color:{theme['ink']};'>{html.escape(answer[:140] or 'No report data')}</div></div>",
                unsafe_allow_html=True,
            )
    if st.button("Clear comparison"):
        st.session_state.compare_run_ids = []
        st.rerun()


def render_export_row(state: dict) -> None:
    report_path = state.get("report_path", "")
    gold_paths = state.get("gold_output_paths", [])
    run_id = state.get("run_id", "")

    cols = st.columns(3)
    with cols[0]:
        if report_path and Path(report_path).exists():
            with open(report_path, "rb") as f:
                st.download_button("Export report (.html)", f.read(), file_name=Path(report_path).name,
                                    mime="text/html", use_container_width=True)
    with cols[1]:
        if gold_paths:
            table_names = [Path(p).stem for p in gold_paths]
            chosen = st.selectbox("Gold table to export", table_names, key="export_gold_select",
                                   label_visibility="collapsed")
            chosen_path = gold_paths[table_names.index(chosen)]
            if Path(chosen_path).exists():
                df = pd.read_parquet(chosen_path)
                st.download_button("Export Gold CSV", df.to_csv(index=False).encode("utf-8"),
                                    file_name=f"{chosen}.csv", mime="text/csv", use_container_width=True)
    with cols[2]:
        logs = AuditLogger(run_id).get_logs() if run_id else []
        if logs:
            st.download_button("Export audit trail (.json)", json.dumps(logs, indent=2),
                                file_name=f"audit_{run_id[:8]}.json", mime="application/json",
                                use_container_width=True)


def render_pinned_strip(theme: dict, run_id: str) -> None:
    insights = list_pinned_insights(run_id=run_id, n_results=10)
    if not insights:
        return
    chips = "".join(
        f"<span class='idamp-pin-chip'>\U0001F4CC {html.escape(i['metadata'].get('answer', '')[:60])}</span>"
        for i in insights
    )
    st.markdown(chips, unsafe_allow_html=True)


def render_gold_chat(theme: dict, state: dict) -> None:
    run_id = state.get("run_id", "")
    gold_paths = state.get("gold_output_paths", [])
    if not gold_paths:
        st.info("No Gold tables available to chat with for this run.")
        return

    if "chat_session" not in st.session_state or st.session_state.chat_session is None:
        st.session_state.chat_session = GoldChatSession(gold_paths, run_id)
    if "chat_history" not in st.session_state:
        st.session_state.chat_history = []

    st.markdown("**Ask the Gold tables**")
    st.caption(", ".join(Path(p).stem for p in gold_paths))

    for i, turn in enumerate(st.session_state.chat_history):
        st.markdown(f"<div class='idamp-q-bubble'>{html.escape(turn['question'])}</div>", unsafe_allow_html=True)
        st.markdown(
            f"<div class='idamp-a-bubble'>{html.escape(turn['answer'] or turn.get('error') or 'No answer.')}</div>",
            unsafe_allow_html=True,
        )
        show_key = f"show_sql_{i}"
        cols = st.columns([1, 1, 1, 1, 4])
        if cols[0].button("SQL", key=f"sql_btn_{i}"):
            st.session_state[show_key] = not st.session_state.get(show_key, False)
        if cols[1].button("\U0001F44D", key=f"up_{i}"):
            log_feedback(run_id, turn["question"], "up")
            st.toast("Feedback recorded")
        if cols[2].button("\U0001F44E", key=f"down_{i}"):
            log_feedback(run_id, turn["question"], "down")
            st.toast("Feedback recorded")
        if cols[3].button("\U0001F4CC", key=f"pin_{i}"):
            store_insight(run_id, turn["question"], turn["answer"], turn.get("sql", ""))
            st.toast("Pinned to memory")
        if st.session_state.get(show_key):
            st.markdown(f"<div class='idamp-sql-box'>{html.escape(turn.get('sql') or 'No SQL captured.')}</div>",
                        unsafe_allow_html=True)

        if turn.get("follow_ups"):
            chip_cols = st.columns(len(turn["follow_ups"]))
            for ci, fu in enumerate(turn["follow_ups"]):
                if chip_cols[ci].button(fu, key=f"chip_{i}_{ci}", use_container_width=True):
                    _ask_chat(fu)
                    st.rerun()

    question = st.text_input("Ask another question about this Gold data...", key="chat_input")
    if st.button("Send \u2192", key="chat_send") and question.strip():
        _ask_chat(question.strip())
        st.rerun()


def _ask_chat(question: str) -> None:
    session: GoldChatSession = st.session_state.chat_session
    with st.spinner("Querying Gold tables..."):
        result = session.ask(question)
    st.session_state.chat_history.append({
        "question": question,
        "answer": result.get("answer", ""),
        "sql": result.get("sql", ""),
        "follow_ups": result.get("follow_ups", []),
        "error": result.get("error"),
    })


# ---------------------------------------------------------------------------
# Main app
# ---------------------------------------------------------------------------

if "theme" not in st.session_state:
    st.session_state.theme = "yellow"
if "pipeline_state" not in st.session_state:
    st.session_state.pipeline_state = None
if "phase" not in st.session_state:
    st.session_state.phase = "upload"
if "current_run_id" not in st.session_state:
    st.session_state.current_run_id = ""
if "report_complete" not in st.session_state:
    st.session_state.report_complete = False
if "compare_run_ids" not in st.session_state:
    st.session_state.compare_run_ids = []

theme = inject_theme_css(st.session_state.theme)

top_cols = st.columns([1, 1, 6])
if top_cols[0].button("Yellow_theme", use_container_width=True,
                       type="primary" if st.session_state.theme == "yellow" else "secondary"):
    st.session_state.theme = "yellow"
    st.rerun()
if top_cols[1].button("Kanban-Ocean", use_container_width=True,
                       type="primary" if st.session_state.theme == "kanban" else "secondary"):
    st.session_state.theme = "kanban"
    st.rerun()

st.title("Intent-Driven Agentic Medallion Workflow")

render_run_history_sidebar()

if st.session_state.pipeline_state and st.session_state.pipeline_state.get("run_id"):
    st.session_state.current_run_id = st.session_state.pipeline_state["run_id"]

if st.session_state.theme == "kanban":
    render_kanban_board(st.session_state.phase, st.session_state.pipeline_state or {})
else:
    render_progress_banner(st.session_state.phase, st.session_state.pipeline_state,
                            st.session_state.get("report_complete", False))

main_col, audit_col = st.columns([3.4, 1.4], gap="large")

with audit_col:
    st.markdown(
        f"""
        <style>
        .idamp-audit-sticky {{
            position: sticky;
            top: 1rem;
        }}
        .idamp-audit-scroll {{
            max-height: 65vh;
            overflow-y: auto;
            padding-right: 4px;
        }}
        .idamp-audit-scroll .idamp-card {{ margin-bottom: 8px; }}
        </style>
        """,
        unsafe_allow_html=True,
    )
    st.markdown("<div class='idamp-audit-sticky'>", unsafe_allow_html=True)
    st.markdown("### Audit trail")
    logs = _current_audit_logs()
    if not logs:
        st.info("No audit events yet.")
    else:
        # Newest first, all entries joined into ONE html block so the max-height
        # + overflow-y:auto container actually scrolls internally instead of
        # each st.markdown call rendering as a separate unbounded element that
        # forces the whole page to scroll to see older entries.
        cards = "".join(
            f"<div class='idamp-card'><div style='font-size:10px;color:{theme['ink_faint']};'>"
            f"{html.escape(str(entry.get('timestamp',''))[11:19])}</div>"
            f"<div style='font-size:12px;color:{theme['ink']};font-weight:600;'>"
            f"{html.escape(str(entry.get('agent','')))} | {html.escape(str(entry.get('action','')))}</div></div>"
            for entry in reversed(logs)
        )
        st.markdown(f"<div class='idamp-audit-scroll'>{cards}</div>", unsafe_allow_html=True)
    st.markdown("</div>", unsafe_allow_html=True)

with main_col:
    if st.session_state.phase == "upload":
        st.header("Phase 1: Upload data & define intent")
        uploaded_files = st.file_uploader("Upload CSV files", type=["csv"], accept_multiple_files=True)
        business_intent = st.text_area("Business intent / question", height=100)

        if st.button("Start workflow", disabled=not (uploaded_files and business_intent)):
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
        st.header("Phase 2: Review Bronze layer STTM")
        state = st.session_state.pipeline_state
        sttm_path = state.get("sttm_bronze_path", "")
        if sttm_path and Path(sttm_path).exists():
            df = pd.read_csv(sttm_path)
            editor_df = _prepare_sttm_editor_df(df)
            edited_df = st.data_editor(editor_df, use_container_width=True, num_rows="fixed",
                                        hide_index=True, key="bronze_sttm_editor", height=400)
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
        st.header("Phase 3: Review Silver layer STTM")
        state = st.session_state.pipeline_state
        sttm_path = state.get("sttm_silver_path", "")
        if sttm_path and Path(sttm_path).exists():
            df = pd.read_csv(sttm_path)
            editor_df = _prepare_sttm_editor_df(df)
            edited_df = st.data_editor(editor_df, use_container_width=True, num_rows="fixed",
                                        hide_index=True, key="silver_sttm_editor", height=400)
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
        st.header("Phase 4: Review Gold layer STTM")
        state = st.session_state.pipeline_state
        sttm_path = state.get("sttm_gold_path", "")
        if sttm_path and Path(sttm_path).exists():
            df = pd.read_csv(sttm_path)
            editor_df = _prepare_sttm_editor_df(df)
            edited_df = st.data_editor(editor_df, use_container_width=True, num_rows="fixed",
                                        hide_index=True, key="gold_sttm_editor", height=400)
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
        st.header("Phase 5: Executive report")
        state = st.session_state.pipeline_state
        st.session_state.report_complete = True
        run_id = state.get("run_id", "")

        report_path = state.get("report_path", "")
        if report_path and Path(report_path).exists():
            summary = get_report_summary(run_id) or {}
            direct_answer = summary.get("direct_answer", {}).get("answer", "")

            action_cols = st.columns([1, 1, 1, 3])
            if action_cols[0].button("\U0001F4CC Pin insight"):
                store_insight(run_id, state.get("business_intent", "Executive summary"),
                              direct_answer or "See full report", "")
                st.toast("Pinned to memory")

            if direct_answer:
                st.markdown(f"<div class='idamp-card idamp-card-strong'><div class='idamp-answer'>"
                            f"{html.escape(direct_answer)}</div></div>", unsafe_allow_html=True)

            with open(report_path, "r", encoding="utf-8") as f:
                report_html = f.read()
            st.components.v1.html(report_html, height=1400, scrolling=True)

            render_pinned_strip(theme, run_id)
            render_compare_strip(theme, run_id)
            st.markdown("---")
            render_export_row(state)
            st.markdown("---")
            render_gold_chat(theme, state)

            if st.button("Start new analysis"):
                st.session_state.report_complete = False
                _reset_analysis_session()
                st.rerun()
        else:
            st.error("Report file not found.")
