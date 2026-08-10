
"""Run history — indexes past pipeline runs from existing audit logs.

No new storage. Every run already writes a JSONL audit trail to
`audit_logs/{run_id}.jsonl` via AuditLogger (see core/audit.py) and
orchestrator.py's phase-completion events. This module is purely read-side:
it scans those files and reduces each run's event stream into a single
summary dict the UI can list, filter, and click into.

Used by the run-history sidebar/rail in streamlit_app.py, and by the
"Compare runs" feature (via get_report_summary) and the chat agent (which
needs a run's gold_output_paths to know which Parquet files to load).
"""

import json
from pathlib import Path
from core.config import AUDIT_DIR, REPORTS_DIR

# Orchestrator phase-completion action -> human-readable pipeline status.
# Keys match the `action` field logged in orchestrator.py's audit.log() calls.
_PHASE_STATUS = {
    "phase1_supervisor_completed": "awaiting_bronze_sttm_approval",
    "phase2_supervisor_completed": "awaiting_silver_sttm_approval",
    "phase3_supervisor_completed": "awaiting_gold_sttm_approval",
    "phase4_supervisor_completed": "completed",
}
_FAILURE_ACTIONS = {
    "phase1_supervisor_failed",
    "phase2_supervisor_failed",
    "phase3_supervisor_failed",
    "phase4_supervisor_failed",
}


def _read_jsonl(path: Path) -> list[dict]:
    """Read a JSONL audit file, skipping any unparseable lines rather than failing
    the whole run's history over one corrupted line (e.g. a crash mid-write)."""
    entries: list[dict] = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except (json.JSONDecodeError, ValueError):
                    continue
    except Exception:
        return []
    return entries


def summarize_run(run_id: str, entries: list[dict]) -> dict:
    """Reduce one run's audit entries into a single summary dict.

    Walks events in chronological order and keeps the latest value seen for
    each field, so a run's summary always reflects its most recent state
    even if it's still mid-pipeline (e.g. only phase1 has completed so far).
    """
    summary = {
        "run_id": run_id,
        "business_intent": "",
        "status": "unknown",
        "started_at": "",
        "updated_at": "",
        "report_path": "",
        "gold_output_paths": [],
        "silver_output_paths": [],
        "bronze_output_paths": [],
        "sttm_bronze_path": "",
        "sttm_silver_path": "",
        "sttm_gold_path": "",
        "failed": False,
        "error": "",
    }
    if not entries:
        return summary

    entries = sorted(entries, key=lambda e: e.get("timestamp", ""))
    summary["started_at"] = entries[0].get("timestamp", "")
    summary["updated_at"] = entries[-1].get("timestamp", "")

    for entry in entries:
        action = entry.get("action", "")

        if action == "pipeline_started":
            summary["business_intent"] = entry.get("intent", "") or summary["business_intent"]

        if action in _PHASE_STATUS:
            summary["status"] = _PHASE_STATUS[action]

        if action in _FAILURE_ACTIONS:
            summary["failed"] = True
            summary["status"] = "failed"
            summary["error"] = entry.get("detail", "") or summary["error"]

        if action == "phase1_supervisor_completed":
            summary["sttm_bronze_path"] = entry.get("sttm_bronze_path") or summary["sttm_bronze_path"]

        if action == "phase2_supervisor_completed":
            summary["bronze_output_paths"] = entry.get("bronze_output_paths") or summary["bronze_output_paths"]
            summary["sttm_silver_path"] = entry.get("sttm_silver_path") or summary["sttm_silver_path"]

        if action == "phase3_supervisor_completed":
            summary["silver_output_paths"] = entry.get("silver_output_paths") or summary["silver_output_paths"]
            summary["sttm_gold_path"] = entry.get("sttm_gold_path") or summary["sttm_gold_path"]

        if action == "phase4_supervisor_completed":
            summary["gold_output_paths"] = entry.get("gold_output_paths") or summary["gold_output_paths"]
            summary["report_path"] = entry.get("report_path") or summary["report_path"]

    return summary


def list_runs(limit: int | None = None) -> list[dict]:
    """Return a summary dict per run found in AUDIT_DIR, most recently updated first.

    Skips any audit log that never logged a "pipeline_started" event. Without
    this, an audit_logs/*.jsonl file left behind by a partial/crashed run, or
    by an agent-level test/debug call that instantiates AuditLogger directly
    (e.g. calling gold_agent's _apply_gold_rules in isolation, as the test
    suite and manual smoke tests do), shows up in the sidebar as a blank
    "unknown" entry with no business intent — confirmed by actually running
    this against a log file that only had gold_agent events, no
    pipeline_started. A real run always logs pipeline_started first
    (see orchestrator.run_until_bronze_sttm), so requiring a non-empty
    business_intent is a reliable filter for "is this an actual pipeline run".
    """
    runs = []
    for path in AUDIT_DIR.glob("*.jsonl"):
        entries = _read_jsonl(path)
        if not entries:
            continue
        summary = summarize_run(path.stem, entries)
        if not summary["business_intent"]:
            continue
        runs.append(summary)

    runs.sort(key=lambda r: r.get("updated_at", ""), reverse=True)
    if limit:
        runs = runs[:limit]
    return runs


def get_run(run_id: str) -> dict | None:
    """Return the summary for a single run_id, or None if no audit log exists for it."""
    path = AUDIT_DIR / f"{run_id}.jsonl"
    if not path.exists():
        return None
    return summarize_run(run_id, _read_jsonl(path))


def get_report_summary(run_id: str) -> dict | None:
    """Read a completed run's report_{run_id[:8]}.json for the headline metric,
    used by the 'Compare runs' feature to show two runs' direct answers side by
    side without re-running anything. Returns None if no report JSON exists yet
    (run isn't complete, or predates the JSON-sidecar being written)."""
    json_path = REPORTS_DIR / f"report_{run_id[:8]}.json"
    if not json_path.exists():
        return None
    try:
        with open(json_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, ValueError, OSError):
        return None
