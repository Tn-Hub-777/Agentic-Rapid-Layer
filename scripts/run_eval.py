"""Evaluate the Quoting/Configuration agent with whatever LLM is configured in .env (mock by default).

    python scripts/run_eval.py          # uses tests/evals/config_cases.json

Reports: tool-selection accuracy, structured-output validity, hallucinated-argument rate, latency, tokens.
Use it to compare models, e.g.  LLM_PROVIDER=ollama LLM_MODEL=qwen3:8b python scripts/run_eval.py
"""
import json
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from langchain_core.messages import AIMessage  # noqa: E402

from app.agents.factory import build_quoting_agent  # noqa: E402
from app.config import settings  # noqa: E402
from app.db.database import init_db  # noqa: E402
from app.graph.orchestrator import tool_results  # noqa: E402
from app.service import PrintService  # noqa: E402
from app.tools import config_rules  # noqa: E402

init_db()
cases = json.loads((ROOT / "tests" / "evals" / "config_cases.json").read_text())
staged = PrintService()._stage_upload(str(ROOT / "examples" / "small_box.stl"))
agent = build_quoting_agent()
rowsout, tin, tout = [], 0, 0
for c in cases:
    req = dict(file_path=staged, material="PLA", infill_percent=20, layer_height_mm=0.2, wall_count=2,
               quantity=1, delivery="standard", goal=c["text"])
    t0 = time.time()
    try:
        r = agent.invoke({"messages": [{"role": "user", "content": json.dumps(req)}]})
        err = None
    except Exception as e:  # a model that breaks the agent loop counts as a failure, not a crash
        r, err = {"messages": []}, str(e)[:80]
    dt = time.time() - t0
    calls = [tc for m in r["messages"] if isinstance(m, AIMessage) for tc in m.tool_calls]
    rec = [x for x in calls if x["name"] == "recommend_configuration"]
    got_goal = rec[0]["args"].get("goal") if rec else None
    res = (tool_results(r["messages"], "recommend_configuration") or [{}])[0]
    valid = bool(res) and "error" not in res and not config_rules.validate_configuration({**res, "color": "white"})
    for m in r["messages"]:
        um = getattr(m, "usage_metadata", None) or {}
        tin, tout = tin + um.get("input_tokens", 0), tout + um.get("output_tokens", 0)
    rowsout.append(dict(text=c["text"], expected=c["expected_goal"], got=got_goal, ok=got_goal == c["expected_goal"],
                        valid=valid, hallucinated=bool(rec) and got_goal not in config_rules.GOALS, seconds=round(dt, 2), error=err))

n = len(rowsout)
print(f"\nModel: {settings.llm_provider}/{settings.llm_model if settings.llm_provider != 'mock' else 'scripted'}")
for r in rowsout:
    print(f"  {'PASS' if r['ok'] else 'FAIL'}  {r['text'][:45]:<45} expected={r['expected']:<22} got={r['got']}  {r['seconds']}s")
print(f"\nTool-selection accuracy : {sum(r['ok'] for r in rowsout)}/{n}")
print(f"Valid structured output : {sum(r['valid'] for r in rowsout)}/{n}")
print(f"Hallucinated goals      : {sum(r['hallucinated'] for r in rowsout)}/{n}")
print(f"Avg latency             : {sum(r['seconds'] for r in rowsout)/n:.2f}s   tokens in/out: {tin}/{tout}")
