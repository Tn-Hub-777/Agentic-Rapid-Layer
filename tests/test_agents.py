"""Agent-level tests: tool wiring, security restrictions, prompts, retry logic, and the config-agent eval set."""
import json
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage

from app.agents.factory import build_quoting_agent, build_scheduler_agent
from app.graph.orchestrator import AgentError, run_agent, tool_results
from app.llm import prompts
from app.tools import config_rules

CASES = json.loads((Path(__file__).parent / "evals" / "config_cases.json").read_text())


def test_scheduler_agent_cannot_run_shell_or_write_files():
    tools = set(build_scheduler_agent().nodes["tools"].bound.tools_by_name)
    assert "execute" not in tools and "write_file" not in tools and "edit_file" not in tools
    assert {"write_todos", "list_printers", "score_printers", "assign_job", "dispatch_gcode"} <= tools


def test_agents_only_get_their_own_tools():
    q = set(build_quoting_agent().nodes["tools"].bound.tools_by_name)
    assert q == {"recommend_configuration", "get_material_prices", "estimate_print", "calculate_price"}
    assert "dispatch_gcode" not in q and "reserve_material" not in q


def test_prompts_are_versioned_and_tagged():
    assert prompts.PROMPT_VERSION
    for p in (prompts.QUOTING_SYSTEM, prompts.INVENTORY_SYSTEM, prompts.SCHEDULER_SYSTEM):
        assert "[AGENT:" in p


def test_estimate_tool_rejects_paths_outside_data_dir():
    from app.tools.agent_tools import estimate_print
    out = json.loads(estimate_print.invoke({"file_path": "/etc/passwd", "material": "PLA", "infill_percent": 20,
                                            "layer_height_mm": 0.2, "wall_count": 2}))
    assert "outside" in out["error"]


def test_tools_validate_llm_arguments():
    from app.tools.agent_tools import calculate_price
    out = json.loads(calculate_price.invoke({"material": "PLA", "weight_g": -5, "print_minutes": 10}))
    assert "error" in out


@pytest.mark.parametrize("case", CASES, ids=[c["text"][:30] for c in CASES])
def test_config_agent_eval_cases(case, svc, example):
    """Evaluation set: free text -> goal -> deterministic configuration. Same dataset is used by scripts/run_eval.py."""
    staged = svc._stage_upload(example("small_box.stl"))
    req = dict(file_path=staged, material="PLA", infill_percent=20, layer_height_mm=0.2, wall_count=2,
               quantity=1, delivery="standard", goal=case["text"])
    result = build_quoting_agent().invoke({"messages": [{"role": "user", "content": json.dumps(req)}]})
    calls = [tc for m in result["messages"] if isinstance(m, AIMessage) for tc in m.tool_calls]
    rec_calls = [c for c in calls if c["name"] == "recommend_configuration"]
    assert rec_calls and rec_calls[0]["args"]["goal"] == case["expected_goal"]          # tool selection + argument
    rec = tool_results(result["messages"], "recommend_configuration")[0]
    assert config_rules.validate_configuration({**rec, "color": "white"}) == []         # structured output validity
    assert rec["material"] == case["expected_material"]
    assert rec["layer_height_mm"] <= case.get("max_layer_mm", 1)
    assert rec["infill_percent"] <= case.get("max_infill", 100) and rec["infill_percent"] >= case.get("min_infill", 0)
    # the suggestion must NOT silently change what the customer asked for
    assert tool_results(result["messages"], "estimate_print")[0]["weight_g"] > 0
    assert "PLA" in str(result["messages"][-1].content)


class _Msg:                                             # minimal stand-in for a LangChain message
    def __init__(self, name):
        from langchain_core.messages import ToolMessage
        self.m = ToolMessage(content=json.dumps({"ok": 1}), name=name, tool_call_id="1")


class FlakyAgent:
    """Skips the required tool on the first attempt (like a weak local model), obeys on the retry."""
    def __init__(self, obey_after):
        self.calls, self.obey_after = 0, obey_after

    def invoke(self, payload, config=None):
        self.calls += 1
        msgs = [AIMessage(content="done")]
        if self.calls > self.obey_after:
            msgs.insert(0, _Msg("check_stock").m)
        return {"messages": msgs}


def test_retry_when_agent_skips_required_tool():
    agent = FlakyAgent(obey_after=1)
    _, found = run_agent(agent, "inventory", {"action": "check"}, "O1", ["check_stock"], retries=1)
    assert agent.calls == 2 and found["check_stock"]


def test_agent_error_after_retries_exhausted():
    with pytest.raises(AgentError):
        run_agent(FlakyAgent(obey_after=99), "inventory", {"action": "check"}, "O1", ["check_stock"], retries=1)
