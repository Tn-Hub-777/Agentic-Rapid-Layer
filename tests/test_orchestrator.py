"""Integration tests: the full multi-agent workflow through the LangGraph orchestrator (mock LLM, no network)."""
from app.db.database import connect, rows
from app.service import PrintService
from app.tools.payments import MockPaymentProvider

CFG = dict(material="PLA", color="white", infill_percent=20, layer_height_mm=0.2, wall_count=2, quantity=1, delivery="standard")


def stock(material="PLA", color="white"):
    return rows("SELECT on_hand_g, reserved_g FROM stock WHERE material=? AND color=?", (material, color))[0]


def test_happy_path_upload_to_completed(svc, example):
    v = svc.start_order(example("cylinder.stl"), CFG, goal="strong but cheap")
    assert v["status"] == "QUOTE_READY" and v["pending"]["type"] == "quote_approval"
    quote = v["pending"]["quote"]
    assert quote["total"] > 0 and quote["line_items"] and quote["quote_id"].startswith("Q-")
    assert v["pending"]["suggestion"]["material"] == "PETG"                       # suggested, NOT applied
    assert v["state"]["config"]["material"] == "PLA"
    assert stock()["reserved_g"] == 0                                           # nothing reserved before payment
    assert rows("SELECT * FROM jobs") == []                                     # nothing dispatched before approval

    v = svc.respond_quote(v["order_id"], "approve")                             # approval + (mock) payment + verify
    assert v["status"] == "PRINTING" and v["state"]["payment"]["status"] == "PAID"
    assert v["state"]["assignment"]["printer_id"] == "P1"
    job = rows("SELECT * FROM jobs")[0]
    assert job["status"] == "PRINTING" and job["remote_id"].startswith("sim-")
    assert stock()["reserved_g"] == quote["weight_g"]                           # reserved while printing

    before = stock()["on_hand_g"]
    v = svc.print_finished(v["order_id"])
    assert v["status"] == "COMPLETED"
    assert stock() == {"on_hand_g": round(before - quote["weight_g"], 2), "reserved_g": 0}
    assert rows("SELECT status FROM jobs")[0]["status"] == "DONE"
    assert rows("SELECT status FROM orders WHERE id=?", (v["order_id"],))[0]["status"] == "COMPLETED"


def test_customer_can_modify_configuration_and_get_new_quote(svc, example):
    v = svc.start_order(example("cylinder.stl"), CFG)
    first = v["pending"]["quote"]
    v = svc.respond_quote(v["order_id"], "modify", {"infill_percent": 75})
    assert v["status"] == "QUOTE_READY"
    assert v["pending"]["quote"]["total"] > first["total"] and v["pending"]["quote"]["quote_id"] != first["quote_id"]


def test_cancel_charges_nothing(svc, example):
    v = svc.start_order(example("cylinder.stl"), CFG)
    v = svc.respond_quote(v["order_id"], "cancel")
    assert v["status"] == "CANCELLED" and stock()["reserved_g"] == 0 and rows("SELECT * FROM jobs") == []


def test_out_of_stock_triggers_substitute_and_requires_new_approval(svc, example):
    with connect() as c:
        c.execute("UPDATE stock SET on_hand_g=5 WHERE material='PLA' AND color='white'")
    v = svc.start_order(example("cylinder.stl"), CFG)
    assert v["status"] == "QUOTE_READY"                                         # loop: stock_check -> substitute -> quote
    cfg = v["state"]["config"]
    assert (cfg["material"], cfg["color"]) != ("PLA", "white") and "out of stock" in v["pending"]["notice"]
    assert v["state"]["stock_check"]["ok"]
    assert rows("SELECT * FROM jobs") == []                                     # customer must still approve


def test_no_stock_and_no_alternative_fails_cleanly(svc, example):
    with connect() as c:
        c.execute("UPDATE stock SET on_hand_g=1")
    v = svc.start_order(example("cylinder.stl"), CFG)
    assert v["status"] == "FAILED" and v["pending"] is None


def test_model_too_big_fails_before_quote(svc, example):
    v = svc.start_order(example("too_big.stl"), CFG)
    assert v["status"] == "FAILED" and "build volume" in v["state"]["errors"][0] and not v["state"].get("quote")


def test_large_model_goes_to_the_only_printer_that_fits(svc, example):
    v = svc.start_order(example("large_bracket.stl"), CFG)
    v = svc.respond_quote(v["order_id"], "approve")
    assert v["state"]["assignment"]["printer_id"] == "P3"


def test_invalid_configuration_is_rejected(svc, example):
    v = svc.start_order(example("cylinder.stl"), {**CFG, "infill_percent": 33})
    assert v["status"] == "FAILED" and "infill" in v["state"]["errors"][0]


def test_forged_payment_never_confirms_order(svc, example):
    v = svc.start_order(example("cylinder.stl"), CFG)
    v = svc.respond_quote(v["order_id"], "approve", auto_pay=False)
    assert v["pending"]["type"] == "payment" and v["status"] == "PAYMENT_PENDING"
    v = svc.submit_payment(v["order_id"], v["pending"]["provider_order_id"], "pay_x", "forged-signature")
    assert v["status"] == "FAILED" and v["state"]["payment"]["status"] == "VERIFICATION_FAILED"
    assert stock()["reserved_g"] == 0 and rows("SELECT * FROM jobs") == []


def test_no_printer_available_releases_stock_and_flags_refund(svc, example):
    with connect() as c:
        c.execute("UPDATE printers SET status='offline'")
    v = svc.start_order(example("cylinder.stl"), CFG)
    v = svc.respond_quote(v["order_id"], "approve")
    assert v["status"] == "FAILED" and v["state"]["payment"]["status"] == "REFUND_REQUIRED"
    assert stock()["reserved_g"] == 0


def test_failed_print_releases_reservation(svc, example):
    v = svc.start_order(example("cylinder.stl"), CFG)
    v = svc.respond_quote(v["order_id"], "approve")
    v = svc.print_finished(v["order_id"], success=False)
    assert v["status"] == "FAILED" and stock()["reserved_g"] == 0


def test_concurrent_orders_never_double_use_filament(svc, example):
    with connect() as c:
        c.execute("UPDATE stock SET on_hand_g=60 WHERE material='PLA' AND color='white'")   # enough for ONE 41 g print
    a = svc.start_order(example("cylinder.stl"), CFG)
    b = svc.start_order(example("cylinder.stl"), CFG)
    a = svc.respond_quote(a["order_id"], "approve")
    assert a["status"] == "PRINTING"
    b = svc.respond_quote(b["order_id"], "approve")                             # stock was reserved by A meanwhile
    assert b["status"] == "FAILED" and b["state"]["payment"]["status"] == "REFUND_REQUIRED"
    assert stock()["reserved_g"] == a["state"]["quote"]["weight_g"]


def test_second_job_prefers_printer_with_shorter_queue(svc, example):
    first = svc.respond_quote(svc.start_order(example("cylinder.stl"), CFG)["order_id"], "approve")
    second = svc.respond_quote(svc.start_order(example("cylinder.stl"), CFG)["order_id"], "approve")
    assert first["state"]["assignment"]["printer_id"] == "P1"
    assert second["state"]["assignment"]["printer_id"] != "P1"                  # P1 now has a queue


def test_order_survives_service_restart(svc, example):
    """Checkpointer: pause at approval, throw the whole service away, resume from disk."""
    v = svc.start_order(example("cylinder.stl"), CFG)
    oid = v["order_id"]
    del svc
    svc2 = PrintService()                                                       # new process, same DATA_DIR
    assert svc2.view(oid)["pending"]["type"] == "quote_approval"
    assert svc2.respond_quote(oid, "approve")["status"] == "PRINTING"


def test_long_term_memory_reuses_preferences(svc, example):
    v = svc.start_order(example("cylinder.stl"), {**CFG, "material": "PETG", "color": "black"})
    v = svc.print_finished(svc.respond_quote(v["order_id"], "approve")["order_id"])
    assert v["status"] == "COMPLETED"
    v2 = svc.start_order(example("small_box.stl"), None)                        # no config -> saved preferences are used
    assert v2["state"]["config"]["material"] == "PETG" and v2["state"]["config"]["color"] == "black"
    assert svc.store.get(("inventory", "usage"), "PETG").value["grams"] > 0


def test_traces_are_recorded(svc, example):
    v = svc.start_order(example("cylinder.stl"), CFG)
    svc.respond_quote(v["order_id"], "approve")
    from app.observability.tracing import summary
    s = summary()
    assert {r["agent"] for r in s["llm_by_agent"]} >= {"quoting", "inventory", "scheduler"}
    assert all(r["tin"] > 0 for r in s["llm_by_agent"])


def test_webhook_signature():
    import hashlib, hmac
    from app.config import settings
    body = b'{"order_id":"X"}'
    good = hmac.new(settings.payment_secret.encode(), body, hashlib.sha256).hexdigest()
    assert PrintService.verify_webhook(body, good) and not PrintService.verify_webhook(body, "bad")
