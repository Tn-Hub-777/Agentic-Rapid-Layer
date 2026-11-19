from __future__ import annotations

from typing import Annotated, TypedDict

from langgraph.graph.message import add_messages


class PrintJobState(TypedDict, total=False):
    """Shared state that every agent reads from / writes to. The orchestrator owns it."""
    messages: Annotated[list, add_messages]    # conversation + agent explanations
    order_id: str
    customer_id: str
    file_path: str
    goal: str                                  # free-text optimisation goal ("strong but cheap")
    config: dict                               # material, color, infill_percent, layer_height_mm, wall_count, quantity, delivery
    model_analysis: dict                       # deterministic mesh analysis
    quote: dict                                # price breakdown + weight/time + gcode_path
    suggestion: dict                           # AI configuration suggestion (never applied silently)
    stock_check: dict                          # availability + alternatives
    notice: str                                # why something changed (e.g. material substituted)
    substitutions: int
    payment: dict                              # provider order + verification status
    reservation: dict
    assignment: dict                           # printer/job chosen by the scheduler agent
    inventory_alerts: list
    status: str                                # order state machine value
    route: str                                 # last customer decision (approve / modify / cancel)
    errors: list[str]
