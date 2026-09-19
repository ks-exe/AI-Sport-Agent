from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any


ORDERS = {
    "ORD-001": {
        "order_id": "ORD-001",
        "customer_id": "CUST-001",
        "status": "In transit",
        "carrier": "UPS",
        "tracking_number": "1Z999AA10123456784",
        "estimated_delivery": "2026-09-16",
        "items": [{"sku": "RUN-001", "name": "AeroRun Performance Shoes", "quantity": 1}],
        "order_total": 129.99,
    },
    "ORD-002": {
        "order_id": "ORD-002",
        "customer_id": "CUST-001",
        "status": "Delivered",
        "carrier": "FedEx",
        "tracking_number": "61299991234567891234",
        "estimated_delivery": "2026-09-10",
        "items": [{"sku": "BAG-014", "name": "TrailFlex Hydration Pack", "quantity": 1}],
        "order_total": 89.5,
    },
    "ORD-003": {
        "order_id": "ORD-003",
        "customer_id": "CUST-002",
        "status": "Processing",
        "carrier": "Pending fulfillment",
        "tracking_number": "Pending",
        "estimated_delivery": "2026-09-20",
        "items": [{"sku": "YOG-220", "name": "BalancePro Yoga Mat", "quantity": 2}],
        "order_total": 78.0,
    },
}

CUSTOMERS = {
    "CUST-001": {
        "customer_id": "CUST-001",
        "name": "Maya Johnson",
        "email": "maya.johnson@example.com",
        "loyalty_tier": "Gold",
        "loyalty_points": 4250,
    },
    "CUST-002": {
        "customer_id": "CUST-002",
        "name": "Ravi Patel",
        "email": "ravi.patel@example.com",
        "loyalty_tier": "Silver",
        "loyalty_points": 1800,
    },
}


def _response(status_code: int, body: dict[str, Any]) -> dict[str, Any]:
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
        },
        "body": json.dumps(body),
    }


def _path_parameters(event: dict[str, Any]) -> dict[str, str]:
    raw = event.get("pathParameters", {})
    return raw if isinstance(raw, dict) else {}


def _get_order(order_id: str) -> dict[str, Any]:
    order = ORDERS.get(order_id)
    if not order:
        return {"found": False, "message": f"Order {order_id} was not found."}
    return {"found": True, "order": order}


def _get_customer_orders(customer_id: str) -> dict[str, Any]:
    orders = [order for order in ORDERS.values() if order["customer_id"] == customer_id]
    return {"customer_id": customer_id, "orders": orders, "count": len(orders)}


def _get_customer(customer_id: str) -> dict[str, Any]:
    customer = CUSTOMERS.get(customer_id)
    if not customer:
        return {"found": False, "message": f"Customer {customer_id} was not found."}
    return {"found": True, "customer": customer}


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    params = _path_parameters(event)
    route_key = f"{event.get('httpMethod', 'GET')} {event.get('resource', event.get('path', ''))}"

    if route_key == "GET /orders/{order_id}":
        return _response(200, _get_order(params.get("order_id", "")))
    if route_key == "GET /customers/{customer_id}/orders":
        return _response(200, _get_customer_orders(params.get("customer_id", "")))
    if route_key == "GET /customers/{customer_id}":
        return _response(200, _get_customer(params.get("customer_id", "")))

    return _response(
        404,
        {
            "message": "Unsupported route",
            "route": route_key,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        },
    )
