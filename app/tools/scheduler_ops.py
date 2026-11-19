"""Printer fleet logic: hard eligibility filter -> deterministic scoring -> atomic assignment -> dispatch."""
from __future__ import annotations

import json

from app.db.database import connect, rows
from app.drivers.printers import get_driver

MATERIAL_CHANGE_PENALTY_MIN = 20     # purge + reload filament
COLOR_CHANGE_PENALTY_MIN = 10


def list_printers() -> list[dict]:
    out = rows("SELECT * FROM printers ORDER BY id")
    for p in out:
        p["materials"] = json.loads(p["materials"])
    return out


def check_fit_and_material(printer: dict, dims: dict, material: str) -> list[str]:
    """Reasons a printer is NOT eligible (empty list = eligible). Tries any rotation of the part on the bed."""
    why = []
    if printer["status"] == "offline":
        why.append("printer offline")
    if material not in printer["materials"]:
        why.append(f"does not support {material}")
    bed = sorted([printer["bed_x"], printer["bed_y"], printer["bed_z"]])
    part = sorted([dims["x"], dims["y"], dims["z"]])
    if any(p > b for p, b in zip(part, bed)):
        why.append("model exceeds build volume")
    return why


def fits_any_printer(dims: dict) -> bool:
    """True if at least one printer's build volume can hold the part (any rotation), ignoring status/material."""
    part = sorted([dims["x"], dims["y"], dims["z"]])
    return any(all(p_ <= b for p_, b in zip(part, sorted([p["bed_x"], p["bed_y"], p["bed_z"]]))) for p in list_printers())


def score_printers(dims: dict, material: str, color: str, print_minutes: float) -> dict:
    """Rank eligible printers by estimated finish time (lower = better)."""
    ranked, rejected = [], []
    for p in list_printers():
        why = check_fit_and_material(p, dims, material)
        if why:
            rejected.append({"printer_id": p["id"], "name": p["name"], "reasons": why})
            continue
        penalty = 0.0
        if p["loaded_material"] != material:
            penalty += MATERIAL_CHANGE_PENALTY_MIN
        elif p["loaded_color"] != color:
            penalty += COLOR_CHANGE_PENALTY_MIN
        run = print_minutes / p["speed_factor"]
        eta = p["queue_minutes"] + penalty + run
        ranked.append({"printer_id": p["id"], "name": p["name"], "eta_minutes": round(eta, 1),
                       "queue_minutes": p["queue_minutes"], "changeover_minutes": penalty,
                       "run_minutes": round(run, 1), "status": p["status"]})
    ranked.sort(key=lambda r: (r["eta_minutes"], r["queue_minutes"]))
    return {"ranked": ranked, "rejected": rejected}


def assign_job(order_id: str, printer_id: str, gcode_path: str, run_minutes: float, material: str, color: str) -> dict:
    """Atomically claim the printer (re-checks it is still online) and create the job row."""
    with connect() as c:
        c.execute("BEGIN IMMEDIATE")
        p = c.execute("SELECT * FROM printers WHERE id=?", (printer_id,)).fetchone()
        if not p or p["status"] == "offline":
            c.execute("ROLLBACK")
            return {"ok": False, "error": "printer unavailable"}
        cur = c.execute("INSERT INTO jobs(order_id,printer_id,gcode_path,est_minutes,status) VALUES(?,?,?,?,'ASSIGNED')",
                        (order_id, printer_id, gcode_path, run_minutes))
        c.execute("UPDATE printers SET queue_minutes=queue_minutes+?, status='busy', loaded_material=?, loaded_color=? WHERE id=?",
                  (run_minutes, material, color, printer_id))
        c.execute("COMMIT")
    return {"ok": True, "job_id": cur.lastrowid, "printer_id": printer_id, "order_id": order_id}


def dispatch_gcode(job_id: int) -> dict:
    """Send the G-code to the assigned printer through its driver."""
    j = rows("SELECT * FROM jobs WHERE id=?", (job_id,))
    if not j:
        return {"ok": False, "error": "unknown job"}
    job = j[0]
    printer = rows("SELECT * FROM printers WHERE id=?", (job["printer_id"],))[0]
    try:
        remote = get_driver(printer).send_job(printer, job["gcode_path"])
    except Exception as exc:
        with connect() as c:
            c.execute("UPDATE jobs SET status='DISPATCH_FAILED' WHERE id=?", (job_id,))
        return {"ok": False, "error": str(exc)}
    with connect() as c:
        c.execute("UPDATE jobs SET status='PRINTING', remote_id=? WHERE id=?", (remote, job_id))
    return {"ok": True, "job_id": job_id, "printer_id": printer["id"], "printer_name": printer["name"], "remote_id": remote}


def complete_job(order_id: str) -> dict:
    """Called when the printer reports success: frees queue time."""
    j = rows("SELECT * FROM jobs WHERE order_id=? AND status='PRINTING'", (order_id,))
    if not j:
        return {"ok": False, "error": "no printing job"}
    job = j[0]
    with connect() as c:
        c.execute("UPDATE jobs SET status='DONE' WHERE id=?", (job["id"],))
        c.execute("UPDATE printers SET queue_minutes=MAX(0,queue_minutes-?), "
                  "status=CASE WHEN MAX(0,queue_minutes-?)=0 THEN 'idle' ELSE status END WHERE id=?",
                  (job["est_minutes"], job["est_minutes"], job["printer_id"]))
    return {"ok": True, "job_id": job["id"], "printer_id": job["printer_id"]}
