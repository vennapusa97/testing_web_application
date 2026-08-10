
"""Lightweight chat agent — fast, ad hoc Q&A against already-materialized Gold tables.

Unlike agents/reporter.py, this agent does NOT generate charts or an HTML report.
It answers one question at a time and returns three things the UI needs for the
results-screen features: the direct answer, the exact SQL it ran (for the
"View SQL" transparency toggle), and 2-3 agent-proposed follow-up questions
(for the suggested-question chips).

Table registration mirrors reporter.py's _make_reporter_tools exactly (same
Parquet-stem-to-table-name rule) so a Gold file registers under the same table
name whether the Reporter or this chat agent loads it — important since a
person may ask a follow-up referencing a table name they saw in the report's
"Query Code" section.

I/O contract:
    GoldChatSession(gold_files, run_id).ask(question) -> dict
      {"answer": str, "sql": str, "follow_ups": list[str], "error": str|None, "duration": float}
"""

import json
import time
import duckdb
import pandas as pd
from pathlib import Path
from langchain_core.tools import tool
from langchain_core.messages import HumanMessage
from langchain.agents import create_agent
from core.config import ANTHROPIC_API_KEY, ANTHROPIC_MODEL, LLM_MAX_TOKENS
from core.llm_retry import invoke_with_retry
from core.audit import AuditLogger
from core.observability import AgentTrace


CHAT_AGENT_PROMPT = """You are a fast data Q&A assistant answering follow-up questions against
already-materialized Gold layer tables from a Medallion data pipeline. Unlike
a full executive report, you answer ONE question directly and quickly — no
chart generation, no HTML, no markdown fences.

## Sequence
1. Call load_gold_data_tool to get the table catalog (columns, dtypes, row counts).
2. Write SQL and call execute_query_tool(sql_query=<your_sql>) to get results.
3. Return ONLY a valid JSON object as your final answer:
{
  "answer": "Direct answer with specific numbers from the query results",
  "sql": "the exact SQL you ran to get this answer",
  "follow_ups": ["short specific next question 1", "question 2", "question 3"]
}

## Rules
- "highest/most/top" for a category or dimension means SUM(...) GROUP BY that
  dimension across ALL matching rows — never a single largest row — unless the
  question explicitly asks about one transaction/order.
- follow_ups must be 2-3 short, specific questions grounded in columns you
  actually saw in the table catalog — not generic filler questions.
- Use ACTUAL column names from the query result in "answer", not guessed ones.
- If execute_query_tool returns an error, fix the SQL and retry once.
- Write standard ANSI SQL compatible with DuckDB."""


# ---------------------------------------------------------------------------
# Tool factory — same load/execute pair and table-naming rule as reporter.py
# ---------------------------------------------------------------------------

def _make_chat_tools(gold_files: list[str], conn: duckdb.DuckDBPyConnection):
    scratchpad: dict = {}

    @tool
    def load_gold_data_tool(confirmation: str = "execute") -> str:
        """Load Gold Parquet files into DuckDB and return the full table catalog.

        Registers each Gold file as a DuckDB table and returns a catalog mapping
        table names to column names, types, row counts, and sample data. Call
        this before execute_query_tool.
        """
        catalog: dict = {}
        for fp in gold_files:
            df = pd.read_parquet(fp)
            stem = Path(fp).stem.replace("-", "_").replace(" ", "_")
            conn.register(stem, df)
            catalog[stem] = {
                "table_name": stem,
                "columns": list(df.columns),
                "dtypes": {c: str(t) for c, t in df.dtypes.items()},
                "sample": df.head(5).to_dict(orient="records"),
                "row_count": len(df),
            }
        scratchpad["catalog"] = catalog
        return json.dumps(catalog, default=str)

    @tool
    def execute_query_tool(sql_query: str) -> str:
        """Execute a SQL SELECT query against the loaded Gold tables in DuckDB.

        Call this after load_gold_data_tool. Returns the query result as a JSON
        array of records (up to 100 rows). On SQL error returns {"error": "..."}.
        """
        try:
            result_df = conn.execute(sql_query).fetchdf()
            scratchpad["result_df"] = result_df
            scratchpad["sql_query"] = sql_query
            return json.dumps(result_df.head(100).to_dict(orient="records"), default=str)
        except Exception as e:
            return json.dumps({"error": str(e)})

    return load_gold_data_tool, execute_query_tool, scratchpad


def _make_llm():
    from langchain_anthropic import ChatAnthropic
    return ChatAnthropic(api_key=ANTHROPIC_API_KEY, model=ANTHROPIC_MODEL, temperature=0, max_tokens=LLM_MAX_TOKENS)


def _extract_chat_answer(result: dict) -> dict:
    """Scan agent messages in reverse for a JSON object with an 'answer' key.
    Same fence/brace-scanning approach as reporter.py's _extract_analysis."""
    for msg in reversed(result.get("messages", [])):
        content = getattr(msg, "content", "")
        if isinstance(content, list):
            content = " ".join(
                block.get("text", "") if isinstance(block, dict) else str(block)
                for block in content
                if not isinstance(block, dict) or block.get("type") == "text"
            )
        if not isinstance(content, str) or not content.strip():
            continue
        text = content
        if "```json" in text:
            text = text.split("```json")[1].split("```")[0]
        elif "```" in text:
            text = text.split("```")[1].split("```")[0]
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end == -1:
            continue
        try:
            parsed = json.loads(text[start:end + 1])
            if isinstance(parsed, dict) and "answer" in parsed:
                return parsed
        except (json.JSONDecodeError, ValueError):
            continue
    return {}


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

class GoldChatSession:
    """One DuckDB connection per run, reused across multiple questions in the
    same UI session so a multi-turn chat doesn't re-register Gold Parquet
    files on every message. Call close() when the person leaves the run/tab.
    """

    def __init__(self, gold_files: list[str], run_id: str):
        self.gold_files = gold_files
        self.run_id = run_id
        self.conn = duckdb.connect(":memory:")

    def ask(self, question: str) -> dict:
        """Ask one question against this session's Gold tables.

        Returns {"answer": str, "sql": str, "follow_ups": list[str],
                  "error": str|None, "duration": float}.
        """
        trace = AgentTrace("chat_agent", self.run_id)
        trace.set_input(question=question, gold_files=self.gold_files)
        audit = AuditLogger(self.run_id)
        audit.log("chat_agent", "question_asked", question=question)

        load_tool, query_tool, scratchpad = _make_chat_tools(self.gold_files, self.conn)
        llm = _make_llm()
        agent = create_agent(llm, [load_tool, query_tool], system_prompt=CHAT_AGENT_PROMPT)

        start = time.time()
        try:
            result = invoke_with_retry(agent, {"messages": [HumanMessage(content=question)]})
        except Exception as e:
            trace.fail(str(e))
            audit.log("chat_agent", "question_failed", question=question, detail=str(e))
            return {"answer": "", "sql": "", "follow_ups": [], "error": str(e), "duration": 0.0}

        messages = result.get("messages", [])
        trace.extract_from_messages(messages)
        parsed = _extract_chat_answer(result)

        answer = parsed.get("answer", "")
        sql = parsed.get("sql") or scratchpad.get("sql_query", "")
        follow_ups = parsed.get("follow_ups", [])
        duration = round(time.time() - start, 2)

        trace.set_output(answer=answer, sql=sql, duration=duration).complete()
        audit.log(
            "chat_agent", "question_answered",
            question=question, answer=answer, sql=sql, duration_seconds=duration,
        )

        return {
            "answer": answer,
            "sql": sql,
            "follow_ups": follow_ups,
            "error": None,
            "duration": duration,
        }

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:
            pass


def log_feedback(run_id: str, question: str, rating: str) -> None:
    """Record a thumbs up/down on a chat answer into the run's audit trail.

    rating: "up" or "down". This is the real signal behind the results
    screen's feedback icons — nothing fancier than an audit.log() call using
    the tool that already exists.
    """
    AuditLogger(run_id).log("chat_agent", "feedback", question=question, rating=rating)
