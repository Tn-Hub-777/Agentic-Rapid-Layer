"""Deterministic configuration validation + recommendation rules.

The LLM only maps free text ("strong but cheap") onto a goal enum; the numbers come from here.
"""
from __future__ import annotations

from app.db.database import rows

ALLOWED_INFILL = (10, 20, 30, 50, 75, 100)
ALLOWED_LAYER_MM = (0.12, 0.16, 0.20, 0.28)
ALLOWED_DELIVERY = ("pickup", "standard", "express")
GOALS = ("strength_cost_balance", "max_strength", "cheapest", "high_quality", "flexible")

RECOMMENDATIONS = {
    "strength_cost_balance": dict(material="PETG", infill_percent=30, layer_height_mm=0.20, wall_count=3,
                                  reason="PETG is tough and affordable; 30% infill + 3 walls adds strength without much extra material."),
    "max_strength": dict(material="PETG", infill_percent=50, layer_height_mm=0.20, wall_count=4,
                         reason="Higher infill and more walls maximise strength; PETG resists impact better than PLA."),
    "cheapest": dict(material="PLA", infill_percent=10, layer_height_mm=0.28, wall_count=2,
                     reason="PLA is the cheapest filament; thick layers and low infill minimise time and material."),
    "high_quality": dict(material="PLA", infill_percent=20, layer_height_mm=0.12, wall_count=2,
                         reason="A 0.12 mm layer height gives smooth surfaces; PLA holds fine detail well."),
    "flexible": dict(material="TPU", infill_percent=20, layer_height_mm=0.20, wall_count=2,
                     reason="TPU is the flexible material; moderate infill keeps it springy."),
}


def recommend(goal: str) -> dict:
    if goal not in RECOMMENDATIONS:
        raise ValueError(f"Unknown goal '{goal}'. Valid goals: {', '.join(GOALS)}")
    return {"goal": goal, **RECOMMENDATIONS[goal]}


def validate_configuration(cfg: dict) -> list[str]:
    """Return a list of problems (empty = valid)."""
    errs: list[str] = []
    mats = {r["name"] for r in rows("SELECT name FROM materials")}
    if cfg.get("material") not in mats:
        errs.append(f"material must be one of {sorted(mats)}")
    if cfg.get("infill_percent") not in ALLOWED_INFILL:
        errs.append(f"infill_percent must be one of {ALLOWED_INFILL}")
    if cfg.get("layer_height_mm") not in ALLOWED_LAYER_MM:
        errs.append(f"layer_height_mm must be one of {ALLOWED_LAYER_MM}")
    wc = cfg.get("wall_count", 2)
    if not isinstance(wc, int) or not 1 <= wc <= 6:
        errs.append("wall_count must be an integer 1-6")
    q = cfg.get("quantity", 1)
    if not isinstance(q, int) or not 1 <= q <= 100:
        errs.append("quantity must be an integer 1-100")
    if cfg.get("delivery", "standard") not in ALLOWED_DELIVERY:
        errs.append(f"delivery must be one of {ALLOWED_DELIVERY}")
    if not cfg.get("color"):
        errs.append("color is required")
    return errs
