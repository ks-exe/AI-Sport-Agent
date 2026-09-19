from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any


REFUNDABLE_ORDERS = {
    "ORD-002": {
        "customer_id": "CUST-001",
        "order_total": 89.5,
        "status": "Delivered",
        "delivered_on": "2026-09-10",
        "refund_window_days": 30,
    },
    "ORD-003": {
        "customer_id": "CUST-002",
        "order_total": 78.0,
        "status": "Processing",
        "delivered_on": "",
        "refund_window_days": 30,
    },
}


def _api_response(status_code: int, body: dict[str, Any]) -> dict[str, Any]:
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
        },
        "body": json.dumps(body),
    }


def _payload(event: dict[str, Any]) -> dict[str, Any]:
    if "body" in event and event["body"]:
        body = event["body"]
        return json.loads(body) if isinstance(body, str) else body
    return event


def _refund_id(order_id: str, reason: str) -> str:
    digest = hashlib.sha256(f"{order_id}:{reason}".encode("utf-8")).hexdigest()[:10].upper()
    return f"RFND-{digest}"


def _process_refund(order_id: str, customer_id: str, reason: str) -> dict[str, Any]:
    order = REFUNDABLE_ORDERS.get(order_id)
    if not order:
        return {
            "approved": False,
            "order_id": order_id,
            "message": "The order is not eligible for automated refund processing.",
        }
    if customer_id and order["customer_id"] != customer_id:
        return {
            "approved": False,
            "order_id": order_id,
            "message": "The order does not belong to the supplied customer.",
        }
    if order["status"] != "Delivered":
        return {
            "approved": False,
            "order_id": order_id,
            "message": "Only delivered orders can be refunded automatically.",
        }

    return {
        "approved": True,
        "refund_id": _refund_id(order_id, reason),
        "order_id": order_id,
        "customer_id": order["customer_id"],
        "refund_amount": order["order_total"],
        "status": "Refund initiated",
        "estimated_posting_days": 5,
        "processed_at": datetime.now(timezone.utc).isoformat(),
    }


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    payload = _payload(event)
    order_id = str(payload.get("order_id", "")).strip()
    customer_id = str(payload.get("customer_id", "")).strip()
    reason = str(payload.get("reason", "Customer requested refund")).strip()

    if not order_id:
        return _api_response(400, {"approved": False, "message": "order_id is required."})

    return _api_response(200, _process_refund(order_id, customer_id, reason))
