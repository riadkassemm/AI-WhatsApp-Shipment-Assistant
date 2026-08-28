"""Narrow OpenAI function tools over authoritative application services.

The model never receives database credentials and never supplies the authenticated
customer identity. Protected tools receive the server-established users.id only from
the backend dispatcher.
"""
from __future__ import annotations

import logging
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from app.shipment_client import (
    ShipmentDBError,
    detect_shipping_method_in_text,
    shipment_client,
)


logger = logging.getLogger("shipment-bot")


def _strict_object(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "name": "get_route_shipping_options",
        "description": (
            "Public, authentication-free lookup of the goods categories, shipping "
            "methods, listed prices, and transit times currently present in the company "
            "rate catalog for a route. Use this for questions such as 'what can I ship', "
            "'شو فيني اشحن', 'ماذا يمكن شحنه', or 'qu'est-ce que je peux expédier', "
            "especially when no weight was supplied. Use it before considering human "
            "handoff. Returned categories are rate-catalog categories, not an exhaustive "
            "customs or prohibited-items policy."
        ),
        "parameters": _strict_object(
            {
                "origin": {
                    "type": "string",
                    "description": "Origin country/place in the customer's wording; backend normalizes aliases.",
                },
                "destination": {
                    "type": "string",
                    "description": "Destination country/place in the customer's wording; backend normalizes aliases.",
                },
                "shipping_method": {
                    "type": ["string", "null"],
                    "description": (
                        "Broad method only if the active customer message explicitly "
                        "specifies it; null otherwise. Never copy a method from a prior "
                        "tool result or assistant answer."
                    ),
                },
            },
            ["origin", "destination", "shipping_method"],
        ),
        "strict": True,
    },
    {
        "type": "function",
        "name": "get_shipping_price",
        "description": (
            "Public, authentication-free lookup of company-authoritative shipping rates "
            "for a route and weight. Always use it when a customer asks for a rate. The "
            "backend normalizes English, French, Arabic, Arabizi, and mixed route/method/"
            "goods wording and calculates totals from explicit per-kilogram rates."
        ),
        "parameters": _strict_object(
            {
                "origin": {"type": "string", "description": "Origin country/place in the customer's wording; backend normalizes aliases."},
                "destination": {"type": "string", "description": "Destination country/place in the customer's wording; backend normalizes aliases."},
                "weight_kg": {"type": "number", "description": "Package weight in kilograms."},
                "goods_type": {
                    "type": ["string", "null"],
                    "description": "Goods category in the customer's wording if known; null otherwise.",
                },
                "shipping_method": {
                    "type": ["string", "null"],
                    "description": (
                        "Broad method in the active customer's wording (air/sea/land "
                        "etc.) only when explicitly requested in that message; null "
                        "otherwise. Do not inherit a method from a previous result."
                    ),
                },
            },
            ["origin", "destination", "weight_kg", "goods_type", "shipping_method"],
        ),
        "strict": True,
    },
    {
        "type": "function",
        "name": "get_delivery_duration",
        "description": (
            "Public, authentication-free lookup of company-authoritative transit time "
            "for a route. Use the customer's own wording; backend normalizes aliases."
        ),
        "parameters": _strict_object(
            {
                "origin": {"type": "string"},
                "destination": {"type": "string"},
                "goods_type": {
                    "type": ["string", "null"],
                    "description": "Goods category in the customer's wording if known; null otherwise.",
                },
                "shipping_method": {
                    "type": ["string", "null"],
                    "description": (
                        "Broad method only if explicitly requested in the active "
                        "customer message; null otherwise."
                    ),
                },
            },
            ["origin", "destination", "goods_type", "shipping_method"],
        ),
        "strict": True,
    },
    {
        "type": "function",
        "name": "get_customer_shipments",
        "description": (
            "List recent shipments belonging only to the authenticated customer. "
            "Use when the customer says 'my shipment' without a unique reference or "
            "when several shipments may match. Customer identity is injected by backend."
        ),
        "parameters": _strict_object({}, []),
        "strict": True,
    },
    {
        "type": "function",
        "name": "track_shipment",
        "description": (
            "Get fresh authoritative details for one shipment belonging to the "
            "authenticated customer. The reference may be its tracking number, shipment "
            "ID, or order ID. Customer identity is injected by backend."
        ),
        "parameters": _strict_object(
            {
                "tracking_number": {
                    "type": "string",
                    "description": "Exact shipment/tracking/order reference from the customer or prior context.",
                }
            },
            ["tracking_number"],
        ),
        "strict": True,
    },
    {
        "type": "function",
        "name": "get_customer_balance",
        "description": (
            "Get the authenticated customer's authoritative wallet balance. "
            "Customer identity is injected by backend."
        ),
        "parameters": _strict_object({}, []),
        "strict": True,
    },
    {
        "type": "function",
        "name": "update_shipment_mode",
        "description": (
            "Change an authenticated customer's own shipment between pickup and delivery. "
            "Ownership is enforced by backend."
        ),
        "parameters": _strict_object(
            {
                "tracking_number": {"type": "string"},
                "mode": {"type": "string", "enum": ["pickup", "delivery"]},
            },
            ["tracking_number", "mode"],
        ),
        "strict": True,
    },
    {
        "type": "function",
        "name": "transfer_to_human",
        "description": (
            "Create a human-support handoff only for an explicit human request or a "
            "genuine issue the automated tools cannot safely complete."
        ),
        "parameters": _strict_object(
            {
                "reason": {"type": "string", "description": "Concise reason human support is required."},
                "summary": {
                    "type": "string",
                    "description": (
                        "Concise support summary with the request, relevant known facts, "
                        "what was attempted, and what remains unresolved. Never include secrets."
                    ),
                },
                "tracking_number": {
                    "type": ["string", "null"],
                    "description": "Related shipment reference if already known; null otherwise.",
                },
                "requested_action": {
                    "type": ["string", "null"],
                    "description": "Requested human action if known; null otherwise.",
                },
            },
            ["reason", "summary", "tracking_number", "requested_action"],
        ),
        "strict": True,
    },
]


NEEDS_VERIFIED_IDENTITY = {
    "get_customer_shipments",
    "track_shipment",
    "get_customer_balance",
    "update_shipment_mode",
}

NOT_VERIFIED_MESSAGE = (
    "The customer is not authenticated. Follow the strict two-step login flow: ask "
    "only for the customer's User ID in this message, and wait for their reply. Do "
    "not ask for the password yet and do not mention it in this message — it will be "
    "requested separately, in its own message, only after the User ID is received. "
    "Never ask for the User ID and password together, and never guess or assume how "
    "the customer will format either credential."
)


def json_safe(value: Any) -> Any:
    """Convert DB-native values to deterministic JSON-compatible values.

    Decimal is rendered as a string so currency/price precision is not rounded by a
    binary float conversion. Date/time values use ISO-8601 strings.
    """
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(v) for v in value]
    return value


def _text_arg(arguments: dict[str, Any], name: str, *, max_len: int = 191) -> str:
    value = str(arguments.get(name) or "").strip()
    if not value or len(value) > max_len:
        raise ValueError(f"Invalid {name}")
    return value


def _shipping_method_arg(
    arguments: dict[str, Any],
    *,
    current_user_message: str | None,
) -> str | None:
    """Honor a method only when the active customer turn explicitly mentions one.

    A previous authoritative answer may say that a route is by air/land/sea. That is a
    result, not necessarily a customer constraint. Without this boundary, a follow-up
    such as "and from Saudi?" can accidentally reuse ``air`` from the UAE result and
    hide the valid KSA land rate.

    ``current_user_message=None`` keeps backwards compatibility for direct backend
    callers and tests that deliberately supply a method argument.
    """
    if current_user_message is not None:
        return detect_shipping_method_in_text(current_user_message)
    supplied = arguments.get("shipping_method")
    return str(supplied).strip()[:191] if supplied else None


async def dispatch_tool_call(
    name: str,
    arguments: dict[str, Any],
    verified_customer_id: str | None,
    current_user_message: str | None = None,
) -> dict[str, Any]:
    # Do not log raw arguments: shipment/customer references can be sensitive.
    logger.info("AI tool dispatch: tool=%s authenticated=%s", name, bool(verified_customer_id))

    if name in NEEDS_VERIFIED_IDENTITY and not verified_customer_id:
        return {"error": "not_authenticated", "message": NOT_VERIFIED_MESSAGE}

    try:
        if name == "get_route_shipping_options":
            result = await shipment_client.get_route_shipping_options(
                origin=_text_arg(arguments, "origin", max_len=150),
                destination=_text_arg(arguments, "destination", max_len=150),
                shipping_method=_shipping_method_arg(
                    arguments,
                    current_user_message=current_user_message,
                ),
            )
            logger.info(
                "Shipping-options tool result: found=%s matched=%s category_count=%d",
                result.get("found"),
                result.get("matched_requested_filters"),
                len(result.get("rate_catalog_categories") or []),
            )
            return json_safe(result)

        if name == "get_shipping_price":
            weight = float(arguments["weight_kg"])
            if weight <= 0 or weight > 1_000_000:
                raise ValueError("Invalid weight")
            result = await shipment_client.get_shipping_price(
                origin=_text_arg(arguments, "origin", max_len=150),
                destination=_text_arg(arguments, "destination", max_len=150),
                weight_kg=weight,
                goods_type=(
                    str(arguments["goods_type"]).strip()[:191]
                    if arguments.get("goods_type")
                    else None
                ),
                shipping_method=_shipping_method_arg(
                    arguments,
                    current_user_message=current_user_message,
                ),
            )
            logger.info(
                "Shipping-rate tool result: found=%s matched=%s option_count=%d",
                result.get("found"),
                result.get("matched_requested_filters"),
                len(result.get("available_rates") or result.get("calculation", {}).get("options") or []),
            )
            return json_safe(result)

        if name == "get_delivery_duration":
            result = await shipment_client.get_delivery_duration(
                origin=_text_arg(arguments, "origin", max_len=150),
                destination=_text_arg(arguments, "destination", max_len=150),
                goods_type=(
                    str(arguments["goods_type"]).strip()[:191]
                    if arguments.get("goods_type")
                    else None
                ),
                shipping_method=_shipping_method_arg(
                    arguments,
                    current_user_message=current_user_message,
                ),
            )
            return json_safe(result)

        if name == "get_customer_shipments":
            assert verified_customer_id is not None
            result = await shipment_client.get_customer_shipments(
                customer_id=verified_customer_id,
                limit=10,
            )
            return json_safe(result)

        if name == "track_shipment":
            assert verified_customer_id is not None
            result = await shipment_client.track_shipment(
                customer_id=verified_customer_id,
                tracking_number=_text_arg(arguments, "tracking_number"),
            )
            return json_safe(result)

        if name == "get_customer_balance":
            assert verified_customer_id is not None
            result = await shipment_client.get_customer_balance(customer_id=verified_customer_id)
            return json_safe(result)

        if name == "update_shipment_mode":
            assert verified_customer_id is not None
            mode = str(arguments.get("mode") or "").strip().lower()
            if mode not in {"pickup", "delivery"}:
                raise ValueError("Invalid mode")
            result = await shipment_client.update_shipment_mode(
                customer_id=verified_customer_id,
                tracking_number=_text_arg(arguments, "tracking_number"),
                mode=mode,
            )
            return json_safe(result)

        if name == "transfer_to_human":
            reason = _text_arg(arguments, "reason", max_len=2000)
            summary = _text_arg(arguments, "summary", max_len=6000)
            tracking = arguments.get("tracking_number")
            action = arguments.get("requested_action")
            return {
                "handoff": True,
                "reason": reason,
                "summary": summary,
                "tracking_number": str(tracking).strip()[:191] if tracking else None,
                "requested_action": str(action).strip()[:500] if action else None,
            }

        return {"error": "unknown_tool", "message": "Unknown application function."}

    except ShipmentDBError:
        logger.exception("Shipment database operation failed for tool=%s", name)
        return {"error": "data_source_unavailable", "message": "Authoritative shipment data is temporarily unavailable."}
    except (KeyError, TypeError, ValueError):
        logger.info("Invalid arguments for tool=%s", name)
        return {"error": "invalid_arguments", "message": f"Invalid arguments for {name}."}
    except Exception:
        logger.exception("Unexpected tool failure: tool=%s", name)
        return {"error": "operation_failed", "message": "The requested operation could not be completed."}
