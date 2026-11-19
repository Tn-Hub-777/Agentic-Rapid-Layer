"""Scripted offline chat model so the whole system (and the tests) run with no LLM and no internet.

It behaves like a tool-calling model: it reads the system prompt's [AGENT:xxx] tag and the JSON request,
emits tool calls in the right order, then writes a final answer from the tool results. It exists to test the
INTEGRATION (graph, state, tools, checkpoints), not to demonstrate language understanding.
"""
from __future__ import annotations

import json
import re
import uuid
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult


def _goal_from_text(text: str) -> str:
    t = text.lower()
    if any(w in t for w in ("flex", "bend", "rubber")):
        return "flexible"
    if any(w in t for w in ("cheap", "afford", "budget", "low cost", "low-cost")) and any(w in t for w in ("strong", "durable", "tough")):
        return "strength_cost_balance"
    if any(w in t for w in ("cheap", "afford", "budget", "low cost", "lowest")):
        return "cheapest"
    if any(w in t for w in ("smooth", "quality", "detail", "miniature", "nice")):
        return "high_quality"
    if any(w in t for w in ("strong", "durable", "tough", "strength")):
        return "max_strength"
    return "strength_cost_balance"


class MockChatModel(BaseChatModel):
    @property
    def _llm_type(self) -> str:
        return "mock-scripted"

    def bind_tools(self, tools: Any, **kwargs: Any):  # tools are chosen by the script, so binding is a no-op
        return self

    # -- helpers
    @staticmethod
    def _tool_results(msgs: list, since: int) -> dict[str, list[dict]]:
        out: dict[str, list[dict]] = {}
        for m in msgs[since:]:
            if isinstance(m, ToolMessage):
                try:
                    out.setdefault(m.name or "", []).append(json.loads(m.content))
                except Exception:
                    out.setdefault(m.name or "", []).append({"raw": str(m.content)})
        return out

    @staticmethod
    def _call(name: str, **args) -> AIMessage:
        return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": "call_" + uuid.uuid4().hex[:8]}])

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        system = next((m.content for m in messages if isinstance(m, SystemMessage)), "")
        system = system if isinstance(system, str) else json.dumps(system)
        tag = (re.search(r"\[AGENT:(\w+)\]", system) or [None, ""])[1]
        last_h = max((i for i, m in enumerate(messages) if isinstance(m, HumanMessage)), default=0)
        try:
            req = json.loads(messages[last_h].content)
        except Exception:
            req = {}
        res = self._tool_results(messages, last_h + 1)
        reply = getattr(self, f"_{tag}", self._default)(req, res)
        reply.usage_metadata = {"input_tokens": 120 + 10 * len(messages), "output_tokens": 40,
                                "total_tokens": 160 + 10 * len(messages)}
        return ChatResult(generations=[ChatGeneration(message=reply)])

    # -- per-agent scripts
    def _default(self, req, res):
        return AIMessage(content="Mock model: no script for this agent.")

    def _quoting(self, req, res):
        if req.get("goal") and "recommend_configuration" not in res:
            return self._call("recommend_configuration", goal=_goal_from_text(str(req["goal"])))
        if "estimate_print" not in res:
            return self._call("estimate_print", file_path=req["file_path"], material=req["material"],
                              infill_percent=req["infill_percent"], layer_height_mm=req["layer_height_mm"],
                              wall_count=req["wall_count"])
        est = res["estimate_print"][-1]
        if "error" in est:
            return AIMessage(content=f"I could not estimate this print: {est['error']}")
        if "calculate_price" not in res:
            return self._call("calculate_price", material=req["material"], weight_g=est["weight_g"],
                              print_minutes=est["print_minutes"], quantity=req.get("quantity", 1),
                              delivery=req.get("delivery", "standard"))
        q = res["calculate_price"][-1]
        if "error" in q:
            return AIMessage(content=f"Pricing failed: {q['error']}")
        parts = ", ".join(f"{i['label']} ₹{i['amount']:.0f}" for i in q["line_items"])
        return AIMessage(content=f"Your {req['material']} print weighs about {est['weight_g']} g and takes "
                                 f"{est['print_minutes']/60:.1f} h. Breakdown: {parts}. Total ₹{q['total']:.0f}.")

    def _inventory(self, req, res):
        action = req.get("action")
        if action == "check":
            if "check_stock" not in res:
                return self._call("check_stock", material=req["material"], color=req["color"], grams=req["grams"])
            s = res["check_stock"][-1]
            if not s["ok"] and "suggest_alternative_material" not in res:
                return self._call("suggest_alternative_material", material=req["material"], color=req["color"], grams=req["grams"])
            if s["ok"]:
                return AIMessage(content=f"Stock OK: {s['available_g']} g of {s['material']} {s['color']} available.")
            alts = res["suggest_alternative_material"][-1]
            return AIMessage(content=f"Short by {s['shortfall_g']} g. Alternatives: {alts or 'none'}.")
        if action == "reserve":
            if "reserve_material" not in res:
                return self._call("reserve_material", order_id=req["order_id"], material=req["material"],
                                  color=req["color"], grams=req["grams"])
            r = res["reserve_material"][-1]
            return AIMessage(content="Reserved filament for the order." if r.get("ok") else f"Reservation failed: {r.get('error')}")
        if action == "deduct":
            if "deduct_material" not in res:
                return self._call("deduct_material", order_id=req["order_id"])
            if "low_stock_report" not in res:
                return self._call("low_stock_report")
            low = res["low_stock_report"][-1]
            return AIMessage(content="Deducted filament." + (f" Low stock: {[(l['material'], l['color']) for l in low]}." if low else ""))
        if action == "release":
            if "release_reservation" not in res:
                return self._call("release_reservation", order_id=req["order_id"])
            return AIMessage(content="Released reservation.")
        return AIMessage(content="Unknown inventory action.")

    def _scheduler(self, req, res):
        d = req["dims_mm"]
        if "write_todos" not in res and not getattr(self, "_skip_todos", False):
            return self._call("write_todos", todos=[
                {"content": "List printers", "status": "in_progress"},
                {"content": "Score eligible printers", "status": "pending"},
                {"content": "Assign job and dispatch G-code", "status": "pending"}])
        if "list_printers" not in res:
            return self._call("list_printers")
        if "score_printers" not in res:
            return self._call("score_printers", x_mm=d["x"], y_mm=d["y"], z_mm=d["z"], material=req["material"],
                              color=req["color"], print_minutes=req["print_minutes"])
        ranked = res["score_printers"][-1]["ranked"]
        if not ranked:
            why = res["score_printers"][-1]["rejected"]
            return AIMessage(content=f"No eligible printer. Reasons: {why}")
        best = ranked[0]
        if "assign_job" not in res:
            return self._call("assign_job", order_id=req["order_id"], printer_id=best["printer_id"],
                              gcode_path=req["gcode_path"], run_minutes=best["run_minutes"],
                              material=req["material"], color=req["color"])
        a = res["assign_job"][-1]
        if not a.get("ok"):
            return AIMessage(content=f"Assignment failed: {a.get('error')}")
        if "dispatch_gcode" not in res:
            return self._call("dispatch_gcode", job_id=a["job_id"])
        dsp = res["dispatch_gcode"][-1]
        if not dsp.get("ok"):
            return AIMessage(content=f"Dispatch failed: {dsp.get('error')}")
        return AIMessage(content=f"Chose {best['name']}: earliest finish in about {best['eta_minutes']:.0f} min "
                                 f"(queue {best['queue_minutes']:.0f} min, changeover {best['changeover_minutes']:.0f} min). "
                                 f"G-code sent, print started.")
