"""Callback handler that records every LLM call and tool call (agent, duration, tokens, errors) to SQLite.

LangSmith tracing is separate and optional: set LANGSMITH_TRACING=true + LANGSMITH_API_KEY and LangChain
sends traces automatically - nothing here needs to change.
"""
from __future__ import annotations

import time

from langchain_core.callbacks import BaseCallbackHandler

from app.db.database import connect


class TraceLogger(BaseCallbackHandler):
    raise_error = False

    def __init__(self):
        self._runs: dict = {}

    def _start(self, run_id, kind, name, metadata):
        md = metadata or {}
        self._runs[run_id] = dict(t=time.time(), kind=kind, name=name,
                                  agent=md.get("agent", ""), order_id=md.get("order_id", ""))

    def _finish(self, run_id, tokens=(0, 0), error=None):
        r = self._runs.pop(run_id, None)
        if not r:
            return
        with connect() as c:
            c.execute("INSERT INTO trace_events(order_id,agent,kind,name,duration_ms,input_tokens,output_tokens,error) "
                      "VALUES(?,?,?,?,?,?,?,?)",
                      (r["order_id"], r["agent"], r["kind"], r["name"], round((time.time() - r["t"]) * 1000, 1),
                       tokens[0], tokens[1], error))

    def on_chat_model_start(self, serialized, messages, *, run_id, metadata=None, **kw):
        self._start(run_id, "llm", (serialized or {}).get("name") or (metadata or {}).get("ls_model_name", "llm"), metadata)

    def on_llm_end(self, response, *, run_id, **kw):
        tin = tout = 0
        try:
            um = response.generations[0][0].message.usage_metadata or {}
            tin, tout = um.get("input_tokens", 0), um.get("output_tokens", 0)
        except Exception:
            pass
        self._finish(run_id, (tin, tout))

    def on_llm_error(self, error, *, run_id, **kw):
        self._finish(run_id, error=str(error)[:300])

    def on_tool_start(self, serialized, input_str, *, run_id, metadata=None, **kw):
        self._start(run_id, "tool", (serialized or {}).get("name", "tool"), metadata)

    def on_tool_end(self, output, *, run_id, **kw):
        self._finish(run_id)

    def on_tool_error(self, error, *, run_id, **kw):
        self._finish(run_id, error=str(error)[:300])


def summary() -> dict:
    from app.db.database import rows
    llm = rows("SELECT agent, COUNT(*) calls, SUM(input_tokens) tin, SUM(output_tokens) tout, "
               "ROUND(AVG(duration_ms),1) avg_ms FROM trace_events WHERE kind='llm' GROUP BY agent")
    tools = rows("SELECT agent, name, COUNT(*) calls, SUM(error IS NOT NULL) errors, ROUND(AVG(duration_ms),1) avg_ms "
                 "FROM trace_events WHERE kind='tool' GROUP BY agent, name ORDER BY agent")
    return {"llm_by_agent": llm, "tools": tools}
