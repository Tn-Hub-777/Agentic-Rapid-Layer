"""Deterministic pricing engine. The LLM may EXPLAIN a quote but can never set a price."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from app.db.database import rows

RATES = dict(
    machine_per_hour=40.0,      # INR/h machine depreciation + maintenance
    power_kw=0.15, power_inr_per_kwh=8.0,
    labor_flat=50.0,            # handling per order
    service_fee_pct=0.10,       # platform fee on subtotal
    delivery={"pickup": 0.0, "standard": 80.0, "express": 180.0},
    finishing={"none": 0.0, "sanding": 60.0, "painting": 150.0},
    quote_valid_hours=24,
)


def calculate_price(material: str, weight_g: float, print_minutes: float, quantity: int = 1,
                    delivery: str = "standard", finishing: str = "none") -> dict:
    r = rows("SELECT price_per_g FROM materials WHERE name=?", (material,))
    if not r:
        raise ValueError(f"unknown material {material}")
    if weight_g < 0 or print_minutes < 0 or quantity < 1:
        raise ValueError("invalid weight/time/quantity")
    hours = print_minutes / 60.0
    material_cost = weight_g * r[0]["price_per_g"] * quantity
    machine_cost = hours * RATES["machine_per_hour"] * quantity
    electricity = hours * RATES["power_kw"] * RATES["power_inr_per_kwh"] * quantity
    labor = RATES["labor_flat"]
    finishing_cost = RATES["finishing"][finishing] * quantity
    subtotal = material_cost + machine_cost + electricity + labor + finishing_cost
    service_fee = subtotal * RATES["service_fee_pct"]
    delivery_fee = RATES["delivery"][delivery]
    total = subtotal + service_fee + delivery_fee
    items = [("Material", material_cost), ("Machine time", machine_cost), ("Electricity", electricity),
             ("Handling", labor), ("Finishing", finishing_cost), ("Service fee", service_fee),
             ("Delivery", delivery_fee)]
    return {
        "quote_id": "Q-" + uuid.uuid4().hex[:8].upper(),
        "currency": "INR",
        "line_items": [{"label": k, "amount": round(v, 2)} for k, v in items if v > 0],
        "total": round(total, 2),
        "weight_g": round(weight_g, 2), "print_minutes": round(print_minutes, 1), "quantity": quantity,
        "expires_at": (datetime.now(timezone.utc) + timedelta(hours=RATES["quote_valid_hours"])).isoformat(),
    }
