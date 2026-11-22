"""Closed structured decisions; model output is never an executable command."""
from __future__ import annotations

from typing import Literal, Union

from pydantic import BaseModel, ConfigDict, Field

Style = Literal["en", "fr", "ar", "leb_ar", "leb_arabizi", "mixed"]
Reason = Literal[
    "CHANGE_REQUIRES_HUMAN", "IDENTITY_VERIFICATION_REQUIRED",
    "BUSINESS_DATA_UNAVAILABLE", "BUSINESS_DATA_STALE", "KNOWLEDGE_UNAVAILABLE",
    "CUSTOMER_REQUESTED_HUMAN", "AI_UNCERTAIN", "AI_TOOL_UNAVAILABLE",
    "AI_LOW_CONFIDENCE", "MULTIPLE_ISSUES", "COMPLAINT_DISPUTE",
    "AI_MEDIA_UNAVAILABLE",
]

Intent = Literal[
    "tracking_status", "shipping_rates", "account_access", "address_validation",
    "general_service_information", "account_registration", "branch_hours", "warehouse_address",
    "route_availability", "app_tracking_visibility", "local_delivery", "delivery_time",
    "parcel_pickup", "branch_contact", "payment_confirmation", "payment_instructions",
    "restricted_goods", "billing_weight_dispute", "delivery_mode_change", "supplier_payment",
    "complaint_followup", "shipping_mode_change", "customs_clearance", "split_shipments",
    "wallet_balance", "app_usage", "branch_location", "missing_or_wrong_items",
    "return_cancellation", "refund_request", "lost_parcel", "greeting", "unclear",
    "media_unavailable", "change_request", "human_request", "previous_ticket", "conversation_summary",
]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Reply(StrictModel):
    kind: Literal["reply"]
    text: str = Field(max_length=4096)
    language: Style
    facts_used: list[str] = Field(max_length=30)
    awaiting_customer: bool


class ToolCall(StrictModel):
    kind: Literal["tool_call"]
    name: str = Field(max_length=80)
    arguments_json: str = Field(max_length=4000)


class Escalate(StrictModel):
    kind: Literal["escalate"]
    reason_code: Reason


class Decision(StrictModel):
    # A root object with a nested anyOf is supported by strict Structured Outputs.
    intent: Intent
    action: Union[Reply, ToolCall, Escalate]


DECISION_SCHEMA = Decision.model_json_schema()


class ImageDecision(Decision):
    media_notes: str = Field(min_length=1, max_length=2000)


IMAGE_DECISION_SCHEMA = ImageDecision.model_json_schema()
