"""Inventory business logic. Stock = on_hand - reserved. All writes are atomic (BEGIN IMMEDIATE)."""
from __future__ import annotations

from app.db.database import connect, rows

# materials a customer could reasonably accept instead (needs explicit customer approval)
SUBSTITUTES = {"PLA": ["PETG"], "PETG": ["PLA", "ABS"], "ABS": ["PETG"], "TPU": []}


def check_stock(material: str, color: str, grams: float) -> dict:
    r = rows("SELECT on_hand_g, reserved_g FROM stock WHERE material=? AND color=?", (material, color))
    available = (r[0]["on_hand_g"] - r[0]["reserved_g"]) if r else 0.0
    return {"material": material, "color": color, "needed_g": round(grams, 2),
            "available_g": round(available, 2), "ok": available >= grams,
            "shortfall_g": round(max(0.0, grams - available), 2)}


def suggest_alternatives(material: str, color: str, grams: float) -> list[dict]:
    """Same material in other colors first, then substitute materials (same color first)."""
    out = []
    cands = rows("SELECT material,color,on_hand_g-reserved_g AS avail FROM stock WHERE avail>=? ORDER BY avail DESC", (grams,))
    for c in cands:
        if c["material"] == material and c["color"] != color:
            out.append({"material": c["material"], "color": c["color"], "available_g": c["avail"], "kind": "other_color"})
    for sub in SUBSTITUTES.get(material, []):
        for c in sorted([c for c in cands if c["material"] == sub], key=lambda c: c["color"] != color):
            out.append({"material": c["material"], "color": c["color"], "available_g": c["avail"], "kind": "other_material"})
    return out


def reserve(order_id: str, material: str, color: str, grams: float) -> dict:
    with connect() as c:
        c.execute("BEGIN IMMEDIATE")
        r = c.execute("SELECT on_hand_g, reserved_g FROM stock WHERE material=? AND color=?", (material, color)).fetchone()
        if not r or r["on_hand_g"] - r["reserved_g"] < grams:
            c.execute("ROLLBACK")
            return {"ok": False, "error": "insufficient stock", "order_id": order_id}
        c.execute("UPDATE stock SET reserved_g=reserved_g+? WHERE material=? AND color=?", (grams, material, color))
        cur = c.execute("INSERT INTO reservations(order_id,material,color,grams,status) VALUES(?,?,?,?,'RESERVED')",
                        (order_id, material, color, grams))
        c.execute("COMMIT")
    return {"ok": True, "reservation_id": cur.lastrowid, "order_id": order_id, "grams": grams,
            "material": material, "color": color}


def deduct(order_id: str) -> dict:
    """Consume reserved material after a successful print."""
    with connect() as c:
        c.execute("BEGIN IMMEDIATE")
        res = c.execute("SELECT * FROM reservations WHERE order_id=? AND status='RESERVED'", (order_id,)).fetchall()
        if not res:
            c.execute("ROLLBACK")
            return {"ok": False, "error": "no active reservation"}
        for r in res:
            c.execute("UPDATE stock SET on_hand_g=on_hand_g-?, reserved_g=reserved_g-? WHERE material=? AND color=?",
                      (r["grams"], r["grams"], r["material"], r["color"]))
            c.execute("UPDATE reservations SET status='CONSUMED' WHERE id=?", (r["id"],))
        c.execute("COMMIT")
    return {"ok": True, "order_id": order_id, "consumed_g": sum(r["grams"] for r in res)}


def release(order_id: str) -> dict:
    """Free a reservation (order cancelled / failed)."""
    with connect() as c:
        c.execute("BEGIN IMMEDIATE")
        res = c.execute("SELECT * FROM reservations WHERE order_id=? AND status='RESERVED'", (order_id,)).fetchall()
        for r in res:
            c.execute("UPDATE stock SET reserved_g=reserved_g-? WHERE material=? AND color=?",
                      (r["grams"], r["material"], r["color"]))
            c.execute("UPDATE reservations SET status='RELEASED' WHERE id=?", (r["id"],))
        c.execute("COMMIT")
    return {"ok": True, "released": len(res)}


def low_stock_report() -> list[dict]:
    return rows("SELECT material,color,on_hand_g,reserved_g,on_hand_g-reserved_g AS available_g,reorder_level_g "
                "FROM stock WHERE on_hand_g-reserved_g <= reorder_level_g ORDER BY available_g")
