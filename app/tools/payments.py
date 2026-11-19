"""Payment abstraction. Mock provider by default (free); Razorpay TEST mode is also free."""
from __future__ import annotations

import hashlib
import hmac
import uuid
from abc import ABC, abstractmethod

from app.config import settings


class PaymentProvider(ABC):
    @abstractmethod
    def create_order(self, amount_inr: float, order_id: str) -> dict: ...
    @abstractmethod
    def verify(self, provider_order_id: str, payment_id: str, signature: str) -> bool: ...


class MockPaymentProvider(PaymentProvider):
    def create_order(self, amount_inr, order_id):
        return {"provider_order_id": "mock_" + uuid.uuid4().hex[:10], "amount": amount_inr, "currency": "INR"}

    def sign(self, provider_order_id: str, payment_id: str) -> str:
        """What the 'payment gateway' would send back after the customer pays (demo only)."""
        return hmac.new(settings.payment_secret.encode(), f"{provider_order_id}|{payment_id}".encode(),
                        hashlib.sha256).hexdigest()

    def verify(self, provider_order_id, payment_id, signature):
        return hmac.compare_digest(self.sign(provider_order_id, payment_id), signature or "")


class RazorpayProvider(PaymentProvider):
    """Real Razorpay (use TEST keys, free). Signature scheme: HMAC_SHA256(order_id|payment_id, key_secret)."""

    def __init__(self):
        import os
        self.key_id, self.key_secret = os.getenv("RAZORPAY_KEY_ID", ""), os.getenv("RAZORPAY_KEY_SECRET", "")

    def create_order(self, amount_inr, order_id):
        import httpx
        r = httpx.post("https://api.razorpay.com/v1/orders", auth=(self.key_id, self.key_secret), timeout=30,
                       json={"amount": int(round(amount_inr * 100)), "currency": "INR", "receipt": order_id})
        r.raise_for_status()
        d = r.json()
        return {"provider_order_id": d["id"], "amount": amount_inr, "currency": "INR"}

    def verify(self, provider_order_id, payment_id, signature):
        exp = hmac.new(self.key_secret.encode(), f"{provider_order_id}|{payment_id}".encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(exp, signature or "")


def get_payment_provider() -> PaymentProvider:
    return RazorpayProvider() if settings.payment_provider == "razorpay" else MockPaymentProvider()
