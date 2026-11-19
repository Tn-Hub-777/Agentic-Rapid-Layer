import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(env):
    import app.api.main as m
    m._svc = None
    return TestClient(m.app)


def upload(client, example, name="cylinder.stl"):
    with open(example(name), "rb") as fh:
        return client.post("/api/orders", files={"file": (name, fh)}, data={"material": "PLA", "color": "white"}).json()


def test_full_flow_over_http(client, example):
    from app.config import settings
    v = upload(client, example)
    assert v["status"] == "QUOTE_READY"
    oid = v["order_id"]
    v = client.post(f"/api/orders/{oid}/decision", json={"action": "approve"}).json()
    assert v["status"] == "PAYMENT_PENDING" and v["pending"]["type"] == "payment"

    po = v["pending"]["provider_order_id"]
    sig = hmac.new(settings.payment_secret.encode(), f"{po}|pay_1".encode(), hashlib.sha256).hexdigest()
    body = json.dumps({"order_id": oid, "provider_order_id": po, "payment_id": "pay_1", "signature": sig}).encode()
    wh = hmac.new(settings.payment_secret.encode(), body, hashlib.sha256).hexdigest()
    assert client.post("/api/payments/webhook", content=body, headers={"X-Signature": "nope"}).status_code == 401
    v = client.post("/api/payments/webhook", content=body, headers={"X-Signature": wh}).json()
    assert v["status"] == "PRINTING"

    assert client.post(f"/api/orders/{oid}/print-finished").status_code == 401            # operator only
    v = client.post(f"/api/orders/{oid}/print-finished", headers={"X-Admin-Token": settings.admin_token}).json()
    assert v["status"] == "COMPLETED"
    assert client.get("/api/inventory").status_code == 200 and len(client.get("/api/printers").json()) == 5


def test_rejects_bad_upload(client, tmp_path):
    r = client.post("/api/orders", files={"file": ("virus.exe", b"MZ...")}, data={})
    assert r.status_code == 400


def test_unknown_order_404(client):
    assert client.get("/api/orders/NOPE").status_code == 404
