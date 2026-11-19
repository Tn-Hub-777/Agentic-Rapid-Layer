"""Command-line walkthrough of the whole multi-agent workflow (no UI needed).

    python scripts/run_demo.py [examples/cylinder.stl] ["strong but cheap"]
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.db.database import reset_db, rows  # noqa: E402
from app.service import PrintService  # noqa: E402


def show(title, v):
    print(f"\n=== {title}  [status: {v['status']}] ===")
    for m in v["state"].get("messages", [])[-2:]:
        print(f"  {m['agent']:>15}: {m['text'][:140]}")


reset_db()
svc = PrintService()
model = sys.argv[1] if len(sys.argv) > 1 else str(ROOT / "examples" / "cylinder.stl")
goal = sys.argv[2] if len(sys.argv) > 2 else "strong but cheap"
cfg = dict(material="PLA", color="white", infill_percent=20, layer_height_mm=0.2, wall_count=2, quantity=1, delivery="standard")

v = svc.start_order(model, cfg, goal)
show("1. Uploaded -> quoted -> stock checked (paused for YOUR approval)", v)
if v["pending"]:
    q = v["pending"]["quote"]
    for i in q["line_items"]:
        print(f"     {i['label']:<14} Rs {i['amount']:>8.2f}")
    print(f"     {'TOTAL':<14} Rs {q['total']:>8.2f}   ({q['weight_g']} g, {q['print_minutes']/60:.1f} h)")
    if v["pending"].get("suggestion"):
        s = v["pending"]["suggestion"]
        print(f"     AI suggestion (not applied): {s['material']} {s['infill_percent']}% {s['layer_height_mm']}mm - {s['reason']}")
    v = svc.respond_quote(v["order_id"], "approve")
    show("2. Approved -> paid (verified) -> filament reserved -> printer chosen -> G-code sent", v)
    print("     assignment:", json.dumps({k: v['state']['assignment'][k] for k in ('printer_name', 'remote_id')}))
    v = svc.print_finished(v["order_id"])
    show("3. Print finished -> filament deducted", v)
    print("     low stock alerts:", [(a["material"], a["color"]) for a in v["state"].get("inventory_alerts", [])])
else:
    print("errors:", v["state"].get("errors"))
print("\nLLM/tool calls recorded:", rows("SELECT COUNT(*) n FROM trace_events")[0]["n"])
