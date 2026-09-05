"""Razorpay Payment Links: a real client (test mode) and an in-memory fake with the same interface.

The fake mirrors the documented v1 payment_links entity shape and emits webhook payloads signed
exactly like Razorpay does (HMAC-SHA256 over the raw body with the webhook secret, sent in the
`X-Razorpay-Signature` header), so the webhook handler is the same code in both modes.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, Protocol


class PaymentLinkClient(Protocol):
    mode: str

    def create(self, payload: dict[str, Any]) -> dict[str, Any]: ...
    def fetch(self, link_id: str) -> dict[str, Any]: ...
    def cancel(self, link_id: str) -> dict[str, Any]: ...
    def notify(self, link_id: str, medium: str) -> dict[str, Any]: ...


class RazorpayUnavailable(RuntimeError):
    """Raised when the gateway returns a transient failure (5xx/network)."""


def sign_webhook(body: bytes, secret: str) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def verify_webhook(body: bytes, signature: str, secret: str) -> bool:
    return hmac.compare_digest(sign_webhook(body, secret), signature)


# --------------------------------------------------------------------------------------------
class RealRazorpay:
    """Thin wrapper over the official SDK. Only ever used with test-mode keys (rzp_test_...)."""

    mode = "razorpay-test"

    def __init__(self, key_id: str | None = None, key_secret: str | None = None):
        import razorpay  # imported lazily so the fake path has no SDK dependency at runtime

        key_id = key_id or os.environ["RAZORPAY_KEY_ID"]
        key_secret = key_secret or os.environ["RAZORPAY_KEY_SECRET"]
        if not key_id.startswith("rzp_test_"):
            raise ValueError("Baaki refuses to run against live keys. Use rzp_test_* keys only.")
        self._client = razorpay.Client(auth=(key_id, key_secret))
        self._client.enable_retry(True)
        self._errors = razorpay.errors

    def _wrap(self, fn, *args):
        try:
            return fn(*args)
        except (self._errors.ServerError, self._errors.GatewayError) as e:
            raise RazorpayUnavailable(str(e)) from e
        except self._errors.BadRequestError as e:
            # The SDK reports HTTP 429 as a BadRequestError. A rate limit is the definition of
            # transient; everything else under this class (bad amount, duplicate reference) is
            # genuinely the caller's problem and must not be retried.
            if "too many requests" in str(e).lower():
                raise RazorpayUnavailable(str(e)) from e
            raise

    def create(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._wrap(self._client.payment_link.create, payload)

    def fetch(self, link_id: str) -> dict[str, Any]:
        return self._wrap(self._client.payment_link.fetch, link_id)

    def cancel(self, link_id: str) -> dict[str, Any]:
        return self._wrap(self._client.payment_link.cancel, link_id)

    def notify(self, link_id: str, medium: str) -> dict[str, Any]:
        return self._wrap(self._client.payment_link.notifyBy, link_id, medium)


# --------------------------------------------------------------------------------------------
@dataclass
class FakeRazorpay:
    """In-memory Razorpay. Deterministic ids, documented entity shape, signed webhooks."""

    webhook_secret: str = "baaki-test-webhook-secret"
    fail_next_creates: int = 0  # fault injection: raise RazorpayUnavailable N times
    mode: str = "simulated"
    links: dict[str, dict[str, Any]] = field(default_factory=dict)
    calls: list[dict[str, Any]] = field(default_factory=list)
    _seq: int = 0

    def _next(self, prefix: str) -> str:
        self._seq += 1
        return f"{prefix}_{self._seq:012d}"

    def create(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.calls.append({"op": "create", "payload": payload})
        if self.fail_next_creates > 0:
            self.fail_next_creates -= 1
            raise RazorpayUnavailable("503 Service Unavailable (injected)")
        if payload.get("amount", 0) <= 0:
            raise ValueError("BAD_REQUEST_ERROR: amount must be positive")
        now = int(time.time())
        link_id = self._next("plink")
        entity = {
            "id": link_id,
            "entity": "payment_link",
            "amount": payload["amount"],
            "amount_paid": 0,
            "currency": payload.get("currency", "INR"),
            "accept_partial": bool(payload.get("accept_partial", False)),
            "first_min_partial_amount": payload.get("first_min_partial_amount", 0),
            "expire_by": payload.get("expire_by", 0),
            "expired_at": 0,
            "status": "created",
            "reference_id": payload.get("reference_id"),
            "description": payload.get("description"),
            "customer": payload.get("customer", {}),
            "notify": payload.get("notify", {"sms": True, "email": True}),
            "reminder_enable": payload.get("reminder_enable", False),
            "notes": payload.get("notes", {}),
            "short_url": f"https://rzp.io/i/{link_id[-8:]}",
            "created_at": now,
            "updated_at": now,
            "cancelled_at": 0,
            "payments": [],
        }
        self.links[link_id] = entity
        return dict(entity)

    def fetch(self, link_id: str) -> dict[str, Any]:
        return dict(self.links[link_id])

    def cancel(self, link_id: str) -> dict[str, Any]:
        self.calls.append({"op": "cancel", "id": link_id})
        e = self.links[link_id]
        if e["status"] in ("paid", "cancelled"):
            raise ValueError(f"BAD_REQUEST_ERROR: cannot cancel a {e['status']} link")
        e["status"] = "cancelled"
        e["cancelled_at"] = int(time.time())
        return dict(e)

    def notify(self, link_id: str, medium: str) -> dict[str, Any]:
        self.calls.append({"op": "notify", "id": link_id, "medium": medium})
        return {"success": True}

    # ---- simulation hooks (not part of the real API) -------------------------------------
    def simulate_payment(self, link_id: str, amount_paise: int) -> tuple[bytes, str]:
        """Debtor pays on the link. Returns (webhook body, signature) exactly as Razorpay would POST."""
        e = self.links[link_id]
        if e["status"] in ("paid", "cancelled", "expired"):
            raise ValueError(f"link {link_id} is {e['status']}")
        remaining = e["amount"] - e["amount_paid"]
        amount_paise = min(amount_paise, remaining)
        if amount_paise < remaining and not e["accept_partial"]:
            raise ValueError("partial payment on a full-only link")
        pay_id = self._next("pay")
        e["amount_paid"] += amount_paise
        e["status"] = "paid" if e["amount_paid"] >= e["amount"] else "partially_paid"
        e["payments"].append({"payment_id": pay_id, "amount": amount_paise, "status": "captured"})
        event = "payment_link.paid" if e["status"] == "paid" else "payment_link.partially_paid"
        body = {
            "entity": "event",
            "account_id": "acc_baaki_sim",
            "event": event,
            "contains": ["payment_link", "payment"],
            "payload": {
                "payment_link": {"entity": dict(e)},
                "payment": {
                    "entity": {
                        "id": pay_id,
                        "entity": "payment",
                        "amount": amount_paise,
                        "currency": "INR",
                        "status": "captured",
                        "method": "upi",
                        "captured": True,
                        "notes": e["notes"],
                    }
                },
            },
            "created_at": int(time.time()),
        }
        raw = json.dumps(body, separators=(",", ":")).encode()
        return raw, sign_webhook(raw, self.webhook_secret)


def build_client() -> PaymentLinkClient:
    if os.environ.get("RAZORPAY_KEY_ID") and os.environ.get("RAZORPAY_KEY_SECRET"):
        return RealRazorpay()
    return FakeRazorpay(webhook_secret=os.environ.get("RAZORPAY_WEBHOOK_SECRET", "baaki-test-webhook-secret"))
