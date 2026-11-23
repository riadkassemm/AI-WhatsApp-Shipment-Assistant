"""Deterministic must-escalate safety net for change requests and complaints."""
from test_ai_ticket_turns import main  # dependency doubles
from test_agent_core import agent, response, reply

import asyncio

import pytest

from app.agent.escalation_rules import customer_burst, must_escalate_reason

CHANGES = [
    "Please change my delivery address", "I want to cancel my order", "can you switch my parcel to air?",
    "stop the shipment please", "return my parcel", "update my address please",
    "je veux changer l'adresse de livraison", "annulez ma commande",
    "بدي غير العنوان", "بدي الغي الطلب", "وقفوا الطرد",
    "baddi ghayyer l address", "kansel l talab please", "wa2ef l shi7na",
]
COMPLAINTS = [
    "my parcel arrived broken", "there are missing items", "you charged me too much", "I want a refund",
    "you lost my parcel", "le colis est arrivé cassé", "الطرد وصل مكسور", "الشحنة ناقصة",
    "الطرد وصل مكسوراً", "l shi7na wesle mkassra", "fi 2ase na2sa",
]
ORDINARY = [
    "Any update on my order?", "Is this the correct address?", "Where is my parcel?",
    "How much to ship 5kg to Beirut?", "Hello", "what are your opening hours", "kifak, wen sarit l shi7na?",
    "وين صارت الشحنة؟", "quand arrive mon colis ?", "Can you send me the tracking number",
    "I changed my mind, how much is sea shipping?", "شو سعر الشحن الجوي", "my order number is <ID_1>",
    "Can I pay by card?", "is there a delivery to Tripoli",
]


@pytest.mark.parametrize("text", CHANGES)
def test_change_requests_force_escalation(text):
    assert must_escalate_reason([text]) == "CHANGE_REQUIRES_HUMAN"


@pytest.mark.parametrize("text", COMPLAINTS)
def test_complaints_force_escalation(text):
    assert must_escalate_reason([text]) == "COMPLAINT_DISPUTE"


@pytest.mark.parametrize("text", ORDINARY)
def test_ordinary_questions_are_not_forced(text):
    assert must_escalate_reason([text]) is None


def test_only_the_latest_customer_burst_counts():
    messages = [{"role": "user", "text": "my parcel arrived broken"}, {"role": "assistant", "text": "Sorry, staff will help."},
                {"role": "user", "text": "hello"}, {"role": "user", "text": "where is my other parcel?"}]
    assert customer_burst(messages) == ["hello", "where is my other parcel?"]
    assert must_escalate_reason(customer_burst(messages)) is None


def test_model_reply_to_a_complaint_becomes_a_handoff(agent):
    agent.state["rows"] = [{"sender_type": "CUSTOMER", "body": "l shi7na wesle mkassra", "whatsapp_message_id": "in.1"}]
    outcome = asyncio.run(agent.run())
    assert outcome.reply is None and outcome.reason_code == "COMPLAINT_DISPUTE" and outcome.intent == "greeting"
    assert len(agent.calls) == 1


def test_tool_call_for_a_change_request_is_not_executed(agent, monkeypatch):
    from app.agent import core
    executed = []
    async def execute(*args, **kwargs):
        executed.append(args)
    monkeypatch.setattr(core, "execute_tool", execute)
    agent.state["rows"] = [{"sender_type": "CUSTOMER", "body": "please cancel my order", "whatsapp_message_id": "in.1"}]
    agent.state["responses"] = [response({"kind": "tool_call", "name": "get_customer_shipments", "arguments_json": "{}"})]
    outcome = asyncio.run(agent.run())
    assert outcome.reason_code == "CHANGE_REQUIRES_HUMAN" and not executed


def test_model_escalation_keeps_its_own_reason(agent):
    agent.state["rows"] = [{"sender_type": "CUSTOMER", "body": "cancel my order and talk to a human", "whatsapp_message_id": "in.1"}]
    agent.state["responses"] = [response({"kind": "escalate", "reason_code": "CUSTOMER_REQUESTED_HUMAN"})]
    assert asyncio.run(agent.run()).reason_code == "CUSTOMER_REQUESTED_HUMAN"


def test_ordinary_reply_is_unchanged(agent):
    outcome = asyncio.run(agent.run())
    assert outcome.reply is not None and outcome.reason_code is None
