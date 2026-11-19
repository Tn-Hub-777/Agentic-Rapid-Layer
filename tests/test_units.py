import hashlib
import hmac
import os

import pytest

from app.db.database import connect, rows
from app.config import _load_dotenv
from app.graph.order_status import InvalidTransition, transition
from app.tools import config_rules, inventory_ops, pricing, scheduler_ops
from app.tools.geometry import analyze_model, sanitize_filename
from app.tools.payments import MockPaymentProvider
from app.slicer.service import EstimatorSlicer


# ---------------------------------------------------------------- geometry
def test_analyze_valid_stl(example):
    a = analyze_model(example("small_box.stl"))
    assert a["valid"] and a["format"] == "STL" and a["mesh_valid"]
    assert a["dimensions_mm"] == {"x": 40.0, "y": 30.0, "z": 20.0}
    assert a["volume_cm3"] == pytest.approx(24.0, rel=1e-3)


def test_analyze_rejects_bad_files(tmp_path):
    bad = tmp_path / "evil.stl"
    bad.write_text("this is not a mesh")
    assert not analyze_model(str(bad))["valid"]
    exe = tmp_path / "run.exe"
    exe.write_text("x")
    assert "Unsupported" in analyze_model(str(exe))["error"]


def test_sanitize_filename():
    assert sanitize_filename("../../etc/pass wd;rm.stl") == "pass_wd_rm.stl"


# ---------------------------------------------------------------- pricing (deterministic)
def test_pricing_breakdown_adds_up():
    q = pricing.calculate_price("PLA", 100, 120, quantity=2, delivery="standard")
    assert q["total"] == pytest.approx(sum(i["amount"] for i in q["line_items"]), abs=0.05)
    assert q["currency"] == "INR" and q["quote_id"].startswith("Q-")
    assert pricing.calculate_price("PLA", 100, 120)["total"] < pricing.calculate_price("TPU", 100, 120)["total"]


def test_pricing_rejects_bad_input():
    with pytest.raises(ValueError):
        pricing.calculate_price("UNOBTAINIUM", 10, 10)
    with pytest.raises(ValueError):
        pricing.calculate_price("PLA", -1, 10)


# ---------------------------------------------------------------- configuration rules
@pytest.mark.parametrize("value", ['"quoted-key"', "'quoted-key'", "quoted-key"])
def test_dotenv_loader_strips_surrounding_quotes(tmp_path, monkeypatch, value):
    env_file = tmp_path / ".env"
    env_file.write_text(f"GOOGLE_API_KEY= {value}\n")
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)

    _load_dotenv(str(env_file))

    assert os.environ["GOOGLE_API_KEY"] == "quoted-key"


def test_config_validation():
    ok = dict(material="PLA", color="white", infill_percent=20, layer_height_mm=0.2, wall_count=2, quantity=1)
    assert config_rules.validate_configuration(ok) == []
    bad = {**ok, "infill_percent": 33, "material": "WOOD", "quantity": 0}
    assert len(config_rules.validate_configuration(bad)) == 3


@pytest.mark.parametrize("goal,material", [("cheapest", "PLA"), ("max_strength", "PETG"), ("flexible", "TPU")])
def test_recommendations_are_valid_configs(goal, material):
    r = config_rules.recommend(goal)
    assert r["material"] == material
    assert config_rules.validate_configuration({**r, "color": "white"}) == []


def test_estimator_scales_with_infill(example, env):
    s = EstimatorSlicer()
    lo = s.slice(example("cylinder.stl"), "PLA", 10, 0.2, 2)
    hi = s.slice(example("cylinder.stl"), "PLA", 100, 0.2, 2)
    assert hi["weight_g"] > lo["weight_g"] and hi["print_minutes"] > lo["print_minutes"]


# ---------------------------------------------------------------- inventory
def test_reserve_deduct_release():
    start = rows("SELECT on_hand_g FROM stock WHERE material='PLA' AND color='white'")[0]["on_hand_g"]
    assert inventory_ops.reserve("A", "PLA", "white", 300)["ok"]
    assert inventory_ops.check_stock("PLA", "white", start - 300)["ok"]
    assert not inventory_ops.check_stock("PLA", "white", start - 299)["ok"]
    assert inventory_ops.deduct("A")["consumed_g"] == 300
    assert rows("SELECT on_hand_g, reserved_g FROM stock WHERE material='PLA' AND color='white'")[0] == \
        {"on_hand_g": start - 300, "reserved_g": 0}
    assert inventory_ops.reserve("B", "PLA", "white", 100)["ok"]
    inventory_ops.release("B")
    assert rows("SELECT reserved_g FROM stock WHERE material='PLA' AND color='white'")[0]["reserved_g"] == 0


def test_stock_can_never_go_negative():
    assert inventory_ops.reserve("A", "PETG", "blue", 50)["ok"]          # only 60 g exist
    assert not inventory_ops.reserve("B", "PETG", "blue", 50)["ok"]      # 2nd order must be refused


def test_alternatives_and_low_stock():
    alts = inventory_ops.suggest_alternatives("PETG", "blue", 100)
    assert alts and all(a["available_g"] >= 100 for a in alts)
    assert any(r["color"] == "blue" for r in inventory_ops.low_stock_report())


# ---------------------------------------------------------------- scheduler
def test_scheduler_filters_and_ranks():
    r = scheduler_ops.score_printers({"x": 300, "y": 120, "z": 80}, "PLA", "white", 100)
    assert [p["printer_id"] for p in r["ranked"]] == ["P3"]                     # only the Voron fits
    reasons = {p["printer_id"]: p["reasons"] for p in r["rejected"]}
    assert "model exceeds build volume" in reasons["P1"] and "printer offline" in reasons["P5"]

    small = scheduler_ops.score_printers({"x": 40, "y": 30, "z": 20}, "PLA", "white", 100)
    assert small["ranked"][0]["printer_id"] == "P1"                             # idle, white PLA already loaded
    tpu = scheduler_ops.score_printers({"x": 40, "y": 30, "z": 20}, "TPU", "black", 100)
    assert {p["printer_id"] for p in tpu["ranked"]} == {"P3", "P4"}             # only TPU-capable printers


def test_changeover_penalty_matters():
    r = scheduler_ops.score_printers({"x": 40, "y": 30, "z": 20}, "ABS", "white", 100)
    assert r["ranked"][0]["printer_id"] in ("P1", "P3")
    p3 = next(p for p in r["ranked"] if p["printer_id"] == "P3")
    assert p3["changeover_minutes"] == 0                                        # ABS white already loaded


def test_assign_is_refused_for_offline_printer(tmp_path):
    g = tmp_path / "x.gcode"
    g.write_text(";")
    assert not scheduler_ops.assign_job("O", "P5", str(g), 10, "PLA", "red")["ok"]


# ---------------------------------------------------------------- order state machine
def test_state_machine():
    assert transition("QUOTE_READY", "PAYMENT_PENDING") == "PAYMENT_PENDING"
    assert transition("PAYMENT_PENDING", "PAID") == "PAID"
    for cur, new in [("QUOTE_READY", "PRINTING"), ("QUOTE_READY", "PAID"), ("COMPLETED", "PRINTING"), ("CANCELLED", "QUOTING")]:
        with pytest.raises(InvalidTransition):
            transition(cur, new)


# ---------------------------------------------------------------- payments
def test_payment_signature_verification():
    p = MockPaymentProvider()
    sig = p.sign("order_1", "pay_1")
    assert p.verify("order_1", "pay_1", sig)
    assert not p.verify("order_1", "pay_2", sig)
    assert not p.verify("order_1", "pay_1", "forged")
    assert not p.verify("order_1", "pay_1", "")
