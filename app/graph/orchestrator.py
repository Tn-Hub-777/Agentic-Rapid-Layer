"""The orchestrator: a LangGraph StateGraph that wires the agents together.

Deterministic routing and approvals live HERE (plain code + interrupt()); the LLM agents only do the
work inside their own nodes. Human checkpoints: quote approval, payment, print completion.

  validate_file -> quote -> stock_check -(short)-> substitute -> quote
                                        \\(ok)-> approval -(approve)-> create_payment -> verify_payment
                                                    |  \\(modify)-> quote                    |
                                                    \\(cancel)-> cancel                      v
        deduct <- await_completion <- schedule <- reserve <---------------------------------+
"""
from __future__ import annotations

import functools
import json
import uuid

from langchain_core.messages import AIMessage, ToolMessage
from langgraph.errors import GraphBubbleUp
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from app.agents.factory import build_inventory_agent, build_quoting_agent, build_scheduler_agent
from app.db import database as db
from app.graph.order_status import transition
from app.graph.state import PrintJobState
from app.tools import config_rules, inventory_ops, scheduler_ops
from app.tools.geometry import analyze_model
from app.tools.payments import get_payment_provider

MAX_SUBSTITUTIONS = 2


# ----------------------------------------------------------------------------- helpers
class AgentError(Exception):
    pass


def tool_results(messages: list, name: str) -> list[dict]:
    out = []
    for m in messages:
        if isinstance(m, ToolMessage) and m.name == name:
            try:
                out.append(json.loads(m.content))
            except Exception:
                out.append({"error": str(m.content)})
    return out


def run_agent(agent, name: str, request: dict, order_id: str, expect: list[str], retries: int = 1):
    """Invoke an agent with one JSON request; retry once if it skipped a required tool (malformed/lazy model)."""
    messages = [{"role": "user", "content": json.dumps(request)}]
    for attempt in range(retries + 1):
        result = agent.invoke({"messages": messages},
                              config={"metadata": {"agent": name, "order_id": order_id, "attempt": attempt}})
        found = {n: tool_results(result["messages"], n) for n in expect}
        if all(found.values()):
            return result, found
        messages = messages + [{"role": "user", "content": json.dumps(
            {**request, "reminder": f"You MUST call these tools before answering: {expect}"})}]
    raise AgentError(f"{name} agent did not call required tools {expect}")


def final_text(result) -> str:
    return str(result["messages"][-1].content)


def safe(fn):
    """Turn unexpected exceptions into a FAILED order instead of crashing the graph (interrupts pass through)."""
    @functools.wraps(fn)
    def wrapper(state, *args, **kwargs):
        try:
            return fn(state, *args, **kwargs)
        except GraphBubbleUp:
            raise
        except Exception as exc:
            return {"status": "FAILED", "errors": state.get("errors", []) + [f"{fn.__name__}: {exc}"]}
    return wrapper


def _persist(state, status, summary=""):
    q = state.get("quote") or {}
    db.upsert_order(state["order_id"], state.get("customer_id", ""), status, q.get("total"), summary)


def _set(state, new, **extra):
    status = transition(state.get("status"), new)
    _persist({**state, **extra}, status, extra.get("notice", ""))
    return {"status": status, **extra}


def _msg(agent: str, text: str):
    return {"messages": [AIMessage(content=text, name=agent)]}


# ----------------------------------------------------------------------------- the graph
def build_graph(checkpointer, store):
    quoting, inventory, scheduler = build_quoting_agent(), build_inventory_agent(), build_scheduler_agent()

    @safe
    def validate_file(state: PrintJobState, *, store):
        s = _set(state, "ANALYZING")
        analysis = analyze_model(state["file_path"])
        if not analysis["valid"]:
            return {**s, "model_analysis": analysis, "status": "FAILED", "errors": [analysis.get("error", "invalid model")]}
        if not scheduler_ops.fits_any_printer(analysis["dimensions_mm"]):
            return {**s, "model_analysis": analysis, "status": "FAILED",
                    "errors": ["Model is larger than the build volume of every printer."]}
        cfg, notice = dict(state.get("config") or {}), ""
        saved = store.get(("customers", state.get("customer_id", "anon")), "prefs")     # long-term memory
        if saved and not cfg.get("material"):
            cfg = {**saved.value, **cfg}
            notice = "Started from your previous preferences."
        return {**s, "model_analysis": analysis, "config": cfg, "notice": notice, "substitutions": 0,
                **_msg("orchestrator", f"Model OK: {analysis['dimensions_mm']} mm, {analysis['volume_cm3']} cm3, "
                                       f"printability {analysis['printability']}.")}

    @safe
    def quote(state: PrintJobState):
        cfg = state["config"]
        errs = config_rules.validate_configuration(cfg)
        if errs:
            return {"status": "FAILED", "errors": errs}
        s = _set(state, "QUOTING")
        req = dict(file_path=state["file_path"], material=cfg["material"], color=cfg["color"],
                   infill_percent=cfg["infill_percent"], layer_height_mm=cfg["layer_height_mm"],
                   wall_count=cfg.get("wall_count", 2), quantity=cfg.get("quantity", 1),
                   delivery=cfg.get("delivery", "standard"))
        if state.get("goal"):
            req["goal"] = state["goal"]
        result, found = run_agent(quoting, "quoting", req, state["order_id"], ["estimate_print", "calculate_price"])
        est, price = found["estimate_print"][-1], found["calculate_price"][-1]
        if "error" in est or "error" in price:
            return {**s, "status": "FAILED", "errors": [est.get("error") or price.get("error")]}
        suggestion = (tool_results(result["messages"], "recommend_configuration") or [None])[-1]
        q = {**price, "gcode_path": est["gcode_path"], "layer_count": est["layer_count"],
             "slicer": est["source"], "slicer_warnings": est.get("warnings", []), "explanation": final_text(result),
             "config": dict(cfg)}
        return {**s, "quote": q, "suggestion": suggestion if suggestion and "error" not in suggestion else None,
                **_msg("quoting_agent", final_text(result))}

    @safe
    def stock_check(state: PrintJobState):
        s = _set(state, "STOCK_CHECK")
        cfg, q = state["config"], state["quote"]
        grams = q["weight_g"] * cfg.get("quantity", 1)
        result, found = run_agent(inventory, "inventory",
                                  dict(action="check", material=cfg["material"], color=cfg["color"], grams=grams),
                                  state["order_id"], ["check_stock"])
        stock = found["check_stock"][-1]
        alts = (tool_results(result["messages"], "suggest_alternative_material") or [[]])[-1]
        return {**s, "stock_check": {**stock, "alternatives": alts}, **_msg("inventory_agent", final_text(result))}

    def route_stock(state):
        sc = state["stock_check"]
        if sc["ok"]:
            return "approval"
        if sc["alternatives"] and state.get("substitutions", 0) < MAX_SUBSTITUTIONS:
            return "substitute"
        return "fail"

    @safe
    def substitute(state: PrintJobState):
        alt = state["stock_check"]["alternatives"][0]
        old = state["config"]
        cfg = {**old, "material": alt["material"], "color": alt["color"]}
        notice = (f"{old['material']} {old['color']} is out of stock. Proposed substitute: {alt['material']} "
                  f"{alt['color']} - please review the new price before approving.")
        return {"config": cfg, "notice": notice, "substitutions": state.get("substitutions", 0) + 1, "status": "QUOTING"}

    @safe
    def approval(state: PrintJobState):
        s = _set(state, "QUOTE_READY")
        decision = interrupt({"type": "quote_approval", "quote": state["quote"], "notice": state.get("notice", ""),
                              "suggestion": state.get("suggestion"), "stock": state.get("stock_check")})
        action = (decision or {}).get("action", "cancel")
        out = {**s, "notice": ""}
        if action == "modify":
            out["config"] = {**state["config"], **decision.get("config", {})}
        out["route"] = action
        return out

    def route_approval(state):
        if state.get("status") == "FAILED":
            return "fail"
        return {"approve": "create_payment", "modify": "quote"}.get(state.get("route"), "cancel")

    @safe
    def create_payment(state: PrintJobState):
        s = _set(state, "PAYMENT_PENDING")
        po = get_payment_provider().create_order(state["quote"]["total"], state["order_id"])
        return {**s, "payment": {**po, "status": "PENDING"}}

    @safe
    def verify_payment(state: PrintJobState):
        po = state["payment"]
        info = interrupt({"type": "payment", "provider_order_id": po["provider_order_id"], "amount": po["amount"]})
        ok = get_payment_provider().verify(po["provider_order_id"], (info or {}).get("payment_id", ""),
                                           (info or {}).get("signature", ""))
        if not ok:
            return {"status": "FAILED", "payment": {**po, "status": "VERIFICATION_FAILED"},
                    "errors": ["Payment verification failed - the order was NOT confirmed."]}
        s = _set(state, "PAID")
        return {**s, "payment": {**po, "status": "PAID", "payment_id": info["payment_id"]}}

    @safe
    def reserve(state: PrintJobState):
        cfg, q = state["config"], state["quote"]
        grams = q["weight_g"] * cfg.get("quantity", 1)
        result, found = run_agent(inventory, "inventory", dict(action="reserve", order_id=state["order_id"],
                                  material=cfg["material"], color=cfg["color"], grams=grams),
                                  state["order_id"], ["reserve_material"])
        r = found["reserve_material"][-1]
        if not r.get("ok"):
            return {"status": "FAILED", "errors": [f"Could not reserve filament: {r.get('error')} (refund required)"],
                    "payment": {**state["payment"], "status": "REFUND_REQUIRED"}}
        s = _set(state, "RESERVED")
        return {**s, "reservation": r, **_msg("inventory_agent", final_text(result))}

    @safe
    def schedule(state: PrintJobState):
        cfg, q, dims = state["config"], state["quote"], state["model_analysis"]["dimensions_mm"]
        req = dict(order_id=state["order_id"], gcode_path=q["gcode_path"], dims_mm=dims, material=cfg["material"],
                   color=cfg["color"], print_minutes=q["print_minutes"] * cfg.get("quantity", 1))
        result, found = run_agent(scheduler, "scheduler", req, state["order_id"], ["score_printers"])
        if not found["score_printers"][-1]["ranked"]:
            return {"status": "FAILED", "errors": ["No printer can currently run this job: "
                                                   + json.dumps(found["score_printers"][-1]["rejected"])]}
        dispatched = tool_results(result["messages"], "dispatch_gcode")
        assigned = tool_results(result["messages"], "assign_job")
        if not (assigned and assigned[-1].get("ok") and dispatched and dispatched[-1].get("ok")):
            return {"status": "FAILED", "errors": ["Scheduler could not dispatch the job: " + final_text(result)]}
        s = _set(state, "SCHEDULED")
        s = {"status": transition(s["status"], "PRINTING")}
        _persist({**state}, "PRINTING")
        return {**s, "assignment": {**dispatched[-1], "explanation": final_text(result)},
                **_msg("scheduler_agent", final_text(result))}

    @safe
    def await_completion(state: PrintJobState):
        outcome = interrupt({"type": "print_in_progress", "assignment": state["assignment"]})   # printer webhook / operator
        if (outcome or {}).get("result") != "success":
            return {"status": "FAILED", "errors": ["Print failed - reservation released, customer to be refunded."]}
        scheduler_ops.complete_job(state["order_id"])
        return {"status": "PRINTING"}

    @safe
    def deduct(state: PrintJobState, *, store):
        result, found = run_agent(inventory, "inventory", dict(action="deduct", order_id=state["order_id"]),
                                  state["order_id"], ["deduct_material", "low_stock_report"])
        cfg, cid = state["config"], state.get("customer_id", "anon")
        # long-term memory: remember preferences + running material usage for restock insight
        store.put(("customers", cid), "prefs", {k: cfg[k] for k in
                  ("material", "color", "infill_percent", "layer_height_mm", "wall_count") if k in cfg})
        used = store.get(("inventory", "usage"), cfg["material"])
        total = (used.value["grams"] if used else 0) + found["deduct_material"][-1].get("consumed_g", 0)
        store.put(("inventory", "usage"), cfg["material"], {"grams": round(total, 2)})
        s = _set(state, "COMPLETED")
        return {**s, "inventory_alerts": found["low_stock_report"][-1], **_msg("inventory_agent", final_text(result))}

    @safe
    def cancel(state: PrintJobState):
        s = _set(state, "CANCELLED")
        return {**s, **_msg("orchestrator", "Order cancelled by the customer. Nothing was charged.")}

    def fail(state: PrintJobState):
        """Single failure sink: release any reservation, record the order as FAILED."""
        if state.get("reservation"):
            inventory_ops.release(state["order_id"])
        status = transition(state.get("status"), "FAILED") if state.get("status") not in ("FAILED",) else "FAILED"
        _persist(state, "FAILED", "; ".join(state.get("errors", [])))
        out = {"status": status, **_msg("orchestrator", "Order failed: " + "; ".join(state.get("errors", ["unknown error"])))}
        if (state.get("payment") or {}).get("status") == "PAID":          # money was taken but we cannot deliver
            out["payment"] = {**state["payment"], "status": "REFUND_REQUIRED"}
        return out

    def ok_or_fail(nxt):
        return lambda state: "fail" if state.get("status") == "FAILED" else nxt

    g = StateGraph(PrintJobState)
    for name, fn in [("validate_file", validate_file), ("quote", quote), ("stock_check", stock_check),
                     ("substitute", substitute), ("approval", approval), ("create_payment", create_payment),
                     ("verify_payment", verify_payment), ("reserve", reserve), ("schedule", schedule),
                     ("await_completion", await_completion), ("deduct", deduct), ("cancel", cancel), ("fail", fail)]:
        g.add_node(name, fn)
    g.add_edge(START, "validate_file")
    g.add_conditional_edges("validate_file", ok_or_fail("quote"), ["quote", "fail"])
    g.add_conditional_edges("quote", ok_or_fail("stock_check"), ["stock_check", "fail"])
    g.add_conditional_edges("stock_check", lambda s: "fail" if s.get("status") == "FAILED" else route_stock(s),
                            ["approval", "substitute", "fail"])
    g.add_edge("substitute", "quote")
    g.add_conditional_edges("approval", route_approval, ["create_payment", "quote", "cancel", "fail"])
    g.add_edge("create_payment", "verify_payment")
    g.add_conditional_edges("verify_payment", ok_or_fail("reserve"), ["reserve", "fail"])
    g.add_conditional_edges("reserve", ok_or_fail("schedule"), ["schedule", "fail"])
    g.add_conditional_edges("schedule", ok_or_fail("await_completion"), ["await_completion", "fail"])
    g.add_conditional_edges("await_completion", ok_or_fail("deduct"), ["deduct", "fail"])
    g.add_edge("deduct", END)
    g.add_edge("cancel", END)
    g.add_edge("fail", END)
    return g.compile(checkpointer=checkpointer, store=store)
