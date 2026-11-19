"""LangChain @tool wrappers. These are the ONLY capabilities the LLM agents have.

Every tool validates its arguments and delegates to deterministic business logic. Nothing here
runs shell commands, sets prices freely, or touches payment state.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from langchain_core.tools import tool

from app.config import settings
from app.db.database import rows
from app.slicer.service import slice_model
from app.tools import config_rules, inventory_ops, pricing, scheduler_ops


def _j(obj) -> str:
    return json.dumps(obj, default=str)


def _safe_path(p: str) -> Path:
    """Agents may only read files inside DATA_DIR (uploads / generated G-code)."""
    path = Path(p).resolve()
    if settings.data_dir not in path.parents:
        raise ValueError("path outside allowed data directory")
    return path


# ------------------------------------------------------------------ QUOTING AGENT TOOLS
@tool
def recommend_configuration(goal: Literal["strength_cost_balance", "max_strength", "cheapest", "high_quality", "flexible"]) -> str:
    """Map a customer's optimisation goal to a recommended print configuration (material, infill, layer height, walls).
    This is only a SUGGESTION; the customer must confirm it. Goals: strength_cost_balance, max_strength,
    cheapest, high_quality, flexible."""
    try:
        return _j(config_rules.recommend(goal))
    except ValueError as e:
        return _j({"error": str(e)})


@tool
def get_material_prices() -> str:
    """List available materials with density (g/cm3) and price per gram (INR)."""
    return _j(rows("SELECT name, density_g_cm3, price_per_g FROM materials"))


@tool
def estimate_print(file_path: str, material: str, infill_percent: int, layer_height_mm: float, wall_count: int) -> str:
    """Slice (or estimate) the model with the given settings. Returns weight_g, print_minutes, layer_count,
    gcode_path and which slicer produced the numbers."""
    try:
        errs = config_rules.validate_configuration(
            dict(material=material, infill_percent=infill_percent, layer_height_mm=layer_height_mm,
                 wall_count=wall_count, color="x"))
        if errs:
            return _j({"error": "; ".join(errs)})
        return _j(slice_model(str(_safe_path(file_path)), material, infill_percent, layer_height_mm, wall_count))
    except Exception as e:
        return _j({"error": str(e)})


@tool
def calculate_price(material: str, weight_g: float, print_minutes: float, quantity: int = 1,
                    delivery: Literal["pickup", "standard", "express"] = "standard",
                    finishing: Literal["none", "sanding", "painting"] = "none") -> str:
    """Compute the final INR price with a transparent line-item breakdown. Deterministic - you cannot change the result.
    Use the weight_g and print_minutes returned by estimate_print."""
    try:
        return _j(pricing.calculate_price(material, weight_g, print_minutes, quantity, delivery, finishing))
    except Exception as e:
        return _j({"error": str(e)})


# ------------------------------------------------------------------ INVENTORY AGENT TOOLS
@tool
def check_stock(material: str, color: str, grams: float) -> str:
    """Check whether `grams` of filament (material+color) is available (on hand minus reserved)."""
    return _j(inventory_ops.check_stock(material, color, grams))


@tool
def suggest_alternative_material(material: str, color: str, grams: float) -> str:
    """When stock is short: list in-stock alternatives (other colors, then substitute materials).
    Alternatives are only proposals - the customer must approve any change."""
    return _j(inventory_ops.suggest_alternatives(material, color, grams))


@tool
def reserve_material(order_id: str, material: str, color: str, grams: float) -> str:
    """Reserve filament for a PAID order so concurrent orders cannot use it."""
    return _j(inventory_ops.reserve(order_id, material, color, grams))


@tool
def deduct_material(order_id: str) -> str:
    """Permanently deduct the reserved filament after the print finished successfully."""
    return _j(inventory_ops.deduct(order_id))


@tool
def release_reservation(order_id: str) -> str:
    """Release reserved filament (order cancelled or failed)."""
    return _j(inventory_ops.release(order_id))


@tool
def low_stock_report() -> str:
    """List filament stocks at or below their reorder level."""
    return _j(inventory_ops.low_stock_report())


# ------------------------------------------------------------------ SCHEDULER AGENT TOOLS
@tool
def list_printers() -> str:
    """List every printer with status, build volume, supported materials, loaded filament and queue length."""
    return _j(scheduler_ops.list_printers())


@tool
def score_printers(x_mm: float, y_mm: float, z_mm: float, material: str, color: str, print_minutes: float) -> str:
    """Rank printers that can physically print this job (fit, material, online) by estimated finish time.
    Returns `ranked` (best first) and `rejected` with reasons."""
    return _j(scheduler_ops.score_printers({"x": x_mm, "y": y_mm, "z": z_mm}, material, color, print_minutes))


@tool
def assign_job(order_id: str, printer_id: str, gcode_path: str, run_minutes: float, material: str, color: str) -> str:
    """Claim a printer for the order (atomic). Use the top-ranked printer from score_printers unless there is a good reason."""
    try:
        return _j(scheduler_ops.assign_job(order_id, printer_id, str(_safe_path(gcode_path)), run_minutes, material, color))
    except Exception as e:
        return _j({"ok": False, "error": str(e)})


@tool
def dispatch_gcode(job_id: int) -> str:
    """Send the assigned job's G-code to the printer so printing starts. Only call after assign_job succeeded."""
    return _j(scheduler_ops.dispatch_gcode(job_id))


QUOTING_TOOLS = [recommend_configuration, get_material_prices, estimate_print, calculate_price]
INVENTORY_TOOLS = [check_stock, suggest_alternative_material, reserve_material, deduct_material,
                   release_reservation, low_stock_report]
SCHEDULER_TOOLS = [list_printers, score_printers, assign_job, dispatch_gcode]
