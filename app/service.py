"""Application service: the single entry point used by the Streamlit UI, the REST API and the tests.

It owns the compiled graph, the checkpointer and the long-term store, and translates between
"HTTP/UI-friendly" dicts and LangGraph's invoke / interrupt / resume protocol.
"""
from __future__ import annotations

import hashlib
import hmac
import shutil
import uuid
from pathlib import Path

from langgraph.types import Command

from app.config import settings
from app.db import database as db
from app.graph.orchestrator import build_graph
from app.memory.stores import make_checkpointer, make_store
from app.observability.tracing import TraceLogger
from app.tools.geometry import sanitize_filename
from app.tools.payments import MockPaymentProvider, get_payment_provider

DEFAULT_CONFIG = dict(material="PLA", color="white", infill_percent=20, layer_height_mm=0.20,
                      wall_count=2, quantity=1, delivery="standard")


class PrintService:
    def __init__(self):
        db.init_db()
        self.store = make_store()
        self.graph = build_graph(make_checkpointer(), self.store)
        self.tracer = TraceLogger()

    # ------------------------------------------------------------------ helpers
    def _cfg(self, order_id: str) -> dict:
        return {"configurable": {"thread_id": order_id}, "callbacks": [self.tracer],
                "metadata": {"order_id": order_id}, "recursion_limit": 60}

    def _stage_upload(self, path: str) -> str:
        """Copy the upload into DATA_DIR/uploads under a sanitised, unique name (agents may only read DATA_DIR)."""
        src = Path(path)
        if src.suffix.lower() not in settings.allowed_extensions:
            raise ValueError(f"Unsupported file type {src.suffix}")
        if src.stat().st_size > settings.max_upload_mb * 1_048_576:
            raise ValueError("File too large")
        dest = settings.data_dir / "uploads" / f"{uuid.uuid4().hex[:8]}_{sanitize_filename(src.name)}"
        dest.parent.mkdir(exist_ok=True)
        shutil.copy(src, dest)
        return str(dest)

    def view(self, order_id: str) -> dict:
        snap = self.graph.get_state({"configurable": {"thread_id": order_id}})
        vals = dict(snap.values or {})
        pending = None
        for t in snap.tasks:
            if t.interrupts:
                pending = t.interrupts[0].value
        if pending and pending.get("type") == "quote_approval":
            vals["status"] = "QUOTE_READY"          # node persisted this status; the checkpoint only commits on node exit
        vals["messages"] = [{"agent": getattr(m, "name", None) or m.type, "text": str(m.content)} for m in vals.get("messages", [])]
        return {"order_id": order_id, "status": vals.get("status", "DRAFT"), "pending": pending, "state": vals}

    # ------------------------------------------------------------------ customer actions
    def start_order(self, file_path: str, config: dict | None = None, goal: str | None = None,
                    customer_id: str = "demo-customer", auto_pay: bool = False) -> dict:
        order_id = "ORD-" + uuid.uuid4().hex[:8].upper()
        staged = self._stage_upload(file_path)
        init = {"order_id": order_id, "customer_id": customer_id, "file_path": staged, "goal": goal or "",
                "config": {**DEFAULT_CONFIG, **(config or {})} if config is not None else {}, "errors": []}
        if config is None:                                   # let long-term memory (saved prefs) fill the config
            init["config"] = {}
        self.graph.invoke(init, self._cfg(order_id))
        return self.view(order_id)

    def respond_quote(self, order_id: str, action: str, config_updates: dict | None = None, auto_pay: bool = True) -> dict:
        """action: approve | modify | cancel.  Approving a mock-payment order also simulates the customer paying."""
        self.graph.invoke(Command(resume={"action": action, "config": config_updates or {}}), self._cfg(order_id))
        v = self.view(order_id)
        if action == "approve" and auto_pay and v["pending"] and v["pending"].get("type") == "payment" \
                and isinstance(get_payment_provider(), MockPaymentProvider):
            return self.mock_customer_pays(order_id)
        return v

    def mock_customer_pays(self, order_id: str) -> dict:
        """DEMO ONLY: plays the role of the customer completing checkout at the gateway."""
        pending = self.view(order_id)["pending"]
        prov = MockPaymentProvider()
        pid = "pay_" + uuid.uuid4().hex[:10]
        return self.submit_payment(order_id, pending["provider_order_id"], pid, prov.sign(pending["provider_order_id"], pid))

    def submit_payment(self, order_id: str, provider_order_id: str, payment_id: str, signature: str) -> dict:
        """Resume the graph with gateway data. The graph VERIFIES the signature server-side before confirming."""
        self.graph.invoke(Command(resume={"payment_id": payment_id, "provider_order_id": provider_order_id,
                                          "signature": signature}), self._cfg(order_id))
        return self.view(order_id)

    def print_finished(self, order_id: str, success: bool = True) -> dict:
        self.graph.invoke(Command(resume={"result": "success" if success else "failed"}), self._cfg(order_id))
        return self.view(order_id)

    # ------------------------------------------------------------------ webhooks
    @staticmethod
    def verify_webhook(raw_body: bytes, signature: str) -> bool:
        exp = hmac.new(settings.payment_secret.encode(), raw_body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(exp, signature or "")
