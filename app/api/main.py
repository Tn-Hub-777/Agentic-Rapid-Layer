"""REST API (FastAPI).  Run:  uvicorn app.api.main:app --reload   ->  docs at http://localhost:8000/docs"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field

from app.config import settings
from app.db.database import rows
from app.observability.tracing import summary
from app.service import PrintService
from app.tools.geometry import sanitize_filename
from app.tools.scheduler_ops import list_printers

app = FastAPI(title="Multi-Agent 3D Print Service", version="1.0")
_svc: PrintService | None = None


def svc() -> PrintService:
    global _svc
    if _svc is None:
        _svc = PrintService()
    return _svc


def admin(x_admin_token: str = Header(default="")):
    if x_admin_token != settings.admin_token:
        raise HTTPException(401, "admin token required")


class QuoteDecision(BaseModel):
    action: str = Field(pattern="^(approve|modify|cancel)$")
    config: dict = {}


class PaymentCallback(BaseModel):
    provider_order_id: str
    payment_id: str
    signature: str


@app.post("/api/orders")
async def create_order(file: UploadFile = File(...), material: str = Form("PLA"), color: str = Form("white"),
                       infill_percent: int = Form(20), layer_height_mm: float = Form(0.2), wall_count: int = Form(2),
                       quantity: int = Form(1), delivery: str = Form("standard"), goal: str = Form("")):
    """Upload a model and start the multi-agent workflow. Returns the quote awaiting customer approval."""
    name = sanitize_filename(file.filename or "model.stl")
    data = await file.read(settings.max_upload_mb * 1_048_576 + 1)
    if len(data) > settings.max_upload_mb * 1_048_576:
        raise HTTPException(413, "file too large")
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / name
        p.write_bytes(data)
        try:
            return svc().start_order(str(p), dict(material=material, color=color, infill_percent=infill_percent,
                                                  layer_height_mm=layer_height_mm, wall_count=wall_count,
                                                  quantity=quantity, delivery=delivery), goal or None)
        except ValueError as e:
            raise HTTPException(400, str(e))


@app.get("/api/orders/{order_id}")
def get_order(order_id: str):
    v = svc().view(order_id)
    if not v["state"].get("order_id"):
        raise HTTPException(404, "unknown order")
    return v


@app.post("/api/orders/{order_id}/decision")
def decide(order_id: str, body: QuoteDecision):
    """Customer approval checkpoint: approve | modify (with config changes) | cancel."""
    return svc().respond_quote(order_id, body.action, body.config, auto_pay=False)


@app.post("/api/orders/{order_id}/payment")
def payment_callback(order_id: str, body: PaymentCallback):
    """Gateway result. The signature is verified server-side inside the workflow; the frontend is never trusted."""
    return svc().submit_payment(order_id, body.provider_order_id, body.payment_id, body.signature)


@app.post("/api/payments/webhook")
async def payment_webhook(request: Request, x_signature: str = Header(default="")):
    raw = await request.body()
    if not PrintService.verify_webhook(raw, x_signature):
        raise HTTPException(401, "bad webhook signature")
    d = json.loads(raw)
    return svc().submit_payment(d["order_id"], d["provider_order_id"], d["payment_id"], d["signature"])


@app.post("/api/orders/{order_id}/print-finished", dependencies=[Depends(admin)])
def print_finished(order_id: str, success: bool = True):
    return svc().print_finished(order_id, success)


@app.get("/api/inventory")
def inventory():
    return rows("SELECT material,color,on_hand_g,reserved_g,on_hand_g-reserved_g AS available_g,reorder_level_g FROM stock")


@app.get("/api/printers")
def printers():
    return list_printers()


@app.get("/api/admin/traces", dependencies=[Depends(admin)])
def traces():
    return summary()
