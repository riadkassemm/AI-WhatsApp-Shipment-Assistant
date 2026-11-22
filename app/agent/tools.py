"""Closed read-only registry; authorization and ownership stay in the backend."""
from __future__ import annotations

from pydantic import Field, ValidationError

from app.agent.business_data import BusinessDataError, BusinessResult, business_data_source, fresh_timestamp
from app.agent.contracts import Reply, StrictModel, ToolCall
from app.client_chat_auth import redact_sensitive_text
from app.customer_ai_identity import AIIdentity
from app.customer_reply_guard import render_authoritative_tool_result, render_business_result, validate_customer_reply


class NoArguments(StrictModel):
    pass


class Reference(StrictModel):
    reference: str = Field(min_length=1, max_length=191)


class Route(StrictModel):
    origin: str = Field(min_length=1, max_length=150)
    destination: str = Field(min_length=1, max_length=150)
    shipping_method: str | None = Field(max_length=100)


class Duration(Route):
    goods_type: str | None = Field(max_length=191)


class Price(Duration):
    weight_kg: float = Field(gt=0, le=1000000, allow_inf_nan=False)


class PreviousTicket(StrictModel):
    reference: str = Field(min_length=1, max_length=100)


# Arguments never include an account/customer ID or SQL. Required nullable fields
# keep the schema compatible with strict Structured Outputs.
REGISTRY = {
    "get_customer_shipments": ("shipments", NoArguments, "List this customer's recent shipments; L1 required."),
    "track_shipment": ("tracking", Reference, "Track one owned shipment using an exact reference; L1 required."),
    "get_order_status": ("orders", Reference, "Read one owned order's status; L1 required."),
    "get_customer_balance": ("wallet", NoArguments, "Read this customer's wallet; L2 required."),
    "get_customer_transactions": ("payments", NoArguments, "Read this customer's recent transactions; L2 required."),
    "get_route_shipping_options": ("rates", Route, "Public route options from the current catalogue."),
    "get_shipping_price": ("rates", Price, "Public price calculated from current catalogue rates."),
    "get_delivery_duration": ("rates", Duration, "Public transit duration from the current catalogue."),
    "get_previous_ticket_context": ("account", PreviousTicket,
                                    "Only when the customer refers to an earlier issue: one prior ticket, exact reference or latest. L2 required."),
}
TOOL_SCHEMAS = tuple({"name": name, "description": description, "parameters": arguments.model_json_schema()}
                     for name, (_, arguments, description) in REGISTRY.items())


def render_result(result: BusinessResult, style: str) -> Reply:
    fresh_timestamp(result.as_of)
    text = render_authoritative_tool_result(result.tool, result.payload, style)
    if text is None:
        text = render_business_result(result.tool, result.payload, style)
    if not text or not validate_customer_reply(text, style).safe or redact_sensitive_text(text) != text:
        raise BusinessDataError()
    return Reply(kind="reply", text=text, language=style, facts_used=[],
                 awaiting_customer=result.tool == "get_customer_shipments")


async def execute_tool(action: ToolCall, identity: AIIdentity, *, ticket: dict, style: str,
                       current_text: str = "", repository=None) -> Reply:
    if action.name == "update_shipment_mode":
        raise BusinessDataError("CHANGE_REQUIRES_HUMAN")
    spec = REGISTRY.get(action.name)
    if spec is None:
        raise BusinessDataError("AI_TOOL_UNAVAILABLE")
    data_class, schema, _ = spec
    if not identity.allows(data_class):
        raise BusinessDataError("IDENTITY_VERIFICATION_REQUIRED")
    try:
        arguments = schema.model_validate_json(action.arguments_json).model_dump()
    except (ValueError, ValidationError):
        raise BusinessDataError("AI_TOOL_UNAVAILABLE") from None
    if data_class not in {"public", "rates"} and not identity.customer_id:
        raise BusinessDataError("IDENTITY_VERIFICATION_REQUIRED")
    if action.name == "get_previous_ticket_context":
        from app.agent.memory import previous_context
        if repository is None:
            from app.support_repository import support_repository
            repository = support_repository
        return await previous_context(repository, ticket=ticket, identity=identity, style=style,
                                      reference=arguments["reference"], current_text=current_text)
    result = await business_data_source.read(action.name, arguments, identity)
    if result.tool != action.name:
        raise BusinessDataError()
    return render_result(result, style)
