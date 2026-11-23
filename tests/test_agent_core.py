"""Agent core runs against fake providers only; no customer or provider payloads."""
from __future__ import annotations

# These install the same dependency doubles used by the inbound routing suite.
from test_ai_ticket_turns import main

import asyncio
import json
from types import SimpleNamespace

import pytest

from app.agent import core
from app.agent.context import masked_messages
from app.agent.contracts import Reply
from app.agent.guard import reply_violations
from app.ai_usage import AIUsageBlocked
from app.customer_ai_identity import AIIdentity
from test_ai_retry_side_effects import Harness, _incoming, _status


def reply(text="How can I help?", **kwargs):
    return Reply(kind="reply", text=text, language=kwargs.pop("language", "en"),
                 facts_used=kwargs.pop("facts_used", []), awaiting_customer=True, **kwargs)


def response(action=None, **kwargs):
    return SimpleNamespace(status=kwargs.pop("status", "completed"), output=[{
        "type": "message", "phase": kwargs.pop("phase", "final_answer"),
        "content": [{"type": "output_text", "text": json.dumps({"intent": "greeting", "action": action or reply().model_dump()})}],
    }], **kwargs)


@pytest.fixture
def agent(monkeypatch):
    calls, metered, options, reads, failures = [], [], [], [], []
    state = {"responses": [response()], "knowledge": "Standard parcels take 5 days.",
             "rows": [{"sender_type": "CUSTOMER", "body": "Hello", "whatsapp_message_id": "in.1"}]}

    async def create(**kwargs):
        calls.append(kwargs)
        result = state["responses"].pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    client = SimpleNamespace(responses=SimpleNamespace(create=create))

    def with_options(**kwargs):
        options.append(kwargs)
        return client

    client.with_options = with_options

    async def get_client(*args):
        return client

    async def report_failure(exc):
        failures.append(type(exc).__name__)
        return "provider_error"

    async def meter(**kwargs):
        metered.append(kwargs)
        if state.get("blocked"):
            raise AIUsageBlocked(state["blocked"], "blocked")
        return await kwargs["call"]()

    async def identity(phone):
        return AIIdentity(level="L0", reason="NO_MATCH", customer_id="private-id")

    async def knowledge():
        return state["knowledge"]

    async def summary(*args, **kwargs):
        return None

    async def messages(ticket_id, **kwargs):
        reads.append((ticket_id, kwargs))
        return state["rows"]

    monkeypatch.setattr(core.ai_provider_service, "get_client", get_client)
    monkeypatch.setattr(core.ai_provider_service, "report_failure", report_failure)
    monkeypatch.setattr(core.ai_usage_service, "metered", meter)
    monkeypatch.setattr(core.customer_ai_identity_service, "resolve_or_unknown", identity)
    monkeypatch.setattr(core.ai_knowledge_service, "prompt_text", knowledge)
    monkeypatch.setattr(core, "ticket_summary", summary)
    monkeypatch.setattr(core.support_repository, "list_ai_context_messages", messages)

    async def run(**kwargs):
        return await core.agent_service.run(ticket={"id": 47, "whatsapp_phone": "15550000000"},
                                           style="en", source_message_id="in.1", **kwargs)

    return SimpleNamespace(state=state, calls=calls, meters=metered, options=options,
                           reads=reads, failures=failures, run=run)


def test_single_structured_metered_call_with_private_identity_and_stable_prefix(agent):
    outcome = asyncio.run(agent.run())
    assert outcome.reply.text == "How can I help?"
    assert len(agent.calls) == len(agent.meters) == 1
    request = agent.calls[0]
    assert request["model"] == "gpt-6-luna" and request["reasoning"] == {"effort": "none"}
    assert request["store"] is False and request["text"]["format"]["strict"] is True
    assert agent.options == [{"max_retries": 0}]
    assert agent.reads == [(47, {"source_message_id": "in.1"})]
    assert "private-id" not in json.dumps(request) and "15550000000" not in json.dumps(request)
    assert request["instructions"] == core.cached_prefix(agent.state["knowledge"])
    assert agent.meters[0]["input_payload"] == request


def test_one_repair_then_escalation_with_no_echo_of_unsafe_draft(agent):
    agent.state["responses"] = [response(reply("It costs $999.").model_dump())] * 2
    outcome = asyncio.run(agent.run())
    assert outcome.reason_code == "AI_REPLY_VALIDATION_FAILED"
    assert len(agent.calls) == 2
    assert [meter["purpose"] for meter in agent.meters] == ["turn", "repair"]
    assert agent.calls[0]["instructions"] == agent.calls[1]["instructions"]
    assert "999" not in json.dumps(agent.calls[1])


def test_successful_repair(agent):
    agent.state["responses"] = [response(reply("It takes 66 days.").model_dump()), response()]
    assert asyncio.run(agent.run()).reply is not None
    assert len(agent.calls) == 2


@pytest.mark.parametrize("action,reason", [
    ({"kind": "escalate", "reason_code": "CHANGE_REQUIRES_HUMAN"}, "CHANGE_REQUIRES_HUMAN"),
    ({"kind": "tool_call", "name": "update_shipment_mode", "arguments_json": "{}"}, "CHANGE_REQUIRES_HUMAN"),
])
def test_closed_decisions_never_execute_legacy_tools(agent, action, reason):
    agent.state["responses"] = [response(action)]
    assert asyncio.run(agent.run()).reason_code == reason
    assert len(agent.calls) == 1


@pytest.mark.parametrize("reason", ["AI_MODEL_PRICING_UNCONFIGURED", "AI_BUDGET_EXCEEDED", "AI_BUDGET_UNAVAILABLE"])
def test_budget_and_unverified_pricing_block_before_call(agent, reason):
    agent.state["blocked"] = reason
    assert asyncio.run(agent.run()).reason_code == reason
    assert agent.calls == []
    assert agent.failures == []


def test_provider_failure_is_never_retried_or_logged(agent, caplog):
    agent.state["responses"] = [RuntimeError("private-provider-body")]
    assert asyncio.run(agent.run()).reason_code == "AI_AGENT_UNAVAILABLE"
    assert len(agent.calls) == 1 and "private-provider-body" not in caplog.text
    assert agent.failures == ["RuntimeError"]


@pytest.mark.parametrize("bad", [
    response(status="incomplete"), response(phase="commentary"),
    response({"kind": "escalate", "reason_code": "raw customer secret"}),
    response({**reply().model_dump(), "unexpected": "field"}),
    response({**reply().model_dump(), "awaiting_customer": "false"}),
    SimpleNamespace(status="completed", output=[{"type": "message", "content": [{"type": "refusal"}]}]),
    SimpleNamespace(status="completed", output_text='{"action":{}}', output=[]),
])
def test_invalid_or_refused_output_never_becomes_a_reply(agent, bad):
    agent.state["responses"] = [bad, bad]
    assert asyncio.run(agent.run()).reason_code == "AI_REPLY_VALIDATION_FAILED"
    assert len(agent.calls) == 2


def test_context_unavailable_never_calls_provider(agent):
    agent.state["rows"] = []
    assert asyncio.run(agent.run()).reason_code == "AI_CONTEXT_UNAVAILABLE"
    assert agent.calls == []


def test_current_ticket_context_is_masked_and_bounded(agent):
    agent.state["rows"] = [
        {"sender_type": "CUSTOMER", "body": f"old message {i}"} for i in range(12)
    ] + [{"sender_type": "CUSTOMER", "body": "password: private-secret", "whatsapp_message_id": "in.1"}]
    asyncio.run(agent.run())
    payload = json.loads(agent.calls[0]["input"][0]["content"])
    assert len(payload["messages"]) == 12
    assert "private-secret" not in json.dumps(payload)
    assert payload["messages"][0]["text"] == "old message 1"


def test_password_prompt_just_outside_window_masks_unlabelled_answer():
    rows = [{"sender_type": "AI", "body": "Please send your password"},
            {"sender_type": "CUSTOMER", "body": "baresecret"}]
    rows += [{"sender_type": "CUSTOMER", "body": "hello"}] * 11
    masked = masked_messages(rows, source_message_id=None)
    assert masked[0]["text"] == "[credential withheld]" and len(masked) == 12


@pytest.mark.parametrize("text,evidence", [
    ("Order AB123 is ready", "Order AB1234 is ready"),
    ("It costs $90", "It costs $900"),
    ("It costs $50", "It weighs 50 kg"),
    ("It costs €50", "It costs $50"),
    ("Your balance is -50", "Your balance is 50"),
    ("Tracking: SECRET", "Tracking: OTHER"),
    ("It arrives 2026-12-05", "It arrives 2026-12-06"),
    ("It arrives tomorrow", "Please ask staff"),
    ("It costs twenty dollars", "Please ask staff"),
    ("password: supersecret", "password: supersecret"),
    ("Please send your password", "Please send your password"),
])
def test_invented_ids_amounts_dates_and_passwords_are_rejected(text, evidence):
    assert reply_violations(reply(text), style="en", evidence=(evidence,))


@pytest.mark.parametrize("text,evidence", [
    ("It arrives today", "Please ask staff"),
    ("It arrives on May 5", "Please ask staff"),
    ("Delivery is on 5 March", "Please ask staff"),
    ("It weighs 5kg", "It weighs 50kg"),
    ("Pick it up on the 3rd", "Please ask staff"),
    ("We open at 7pm", "Please ask staff"),
    ("Your code is LB3A4", "Please ask staff"),
    ("Order: AB3CD is ready", "Please ask staff"),
    ("l shi7na btousal bokra 12345", "Please ask staff"),
    ("It is 5m away", "Please ask staff"),
    ("Ready in 7h", "Please ask staff"),
    ("رح توصل بكرا", "Please ask staff"),
])
def test_narrowed_date_and_arabizi_rules_keep_real_values_strict(text, evidence):
    assert "ungrounded_value" in reply_violations(reply(text), style="en", evidence=(evidence,))


@pytest.mark.parametrize("style,text", [
    ("en", "Hello! How can I help you today?"),
    ("en", "How can I help you with that today?"),
    ("en", "You may send your tracking number here."),
    ("fr", "Bonjour ! Comment puis-je vous aider aujourd'hui ?"),
    ("ar", "مرحبا، كيف بقدر ساعدك اليوم؟"),
    ("leb_arabizi", "ahla! ba3den b2oullak, 3tine ra2em l shi7na"),
    ("leb_arabizi", "mni7, shu fiyye sa3dak?"),
    ("leb_arabizi", "eh, b2oullak 3a l ra2em"),
])
def test_greetings_modal_may_and_arabizi_spelling_are_not_facts(style, text):
    assert reply_violations(reply(text, language=style), style=style, evidence=()) == ()


def test_model_fact_claims_cannot_become_evidence():
    assert "unsupported_fact_reference" in reply_violations(
        reply("It costs $90", facts_used=["It costs $90"]), style="en", evidence=("Ask staff",))


@pytest.mark.parametrize("style,text", [("en", "Hello"), ("fr", "Bonjour"), ("ar", "مرحبا"),
                                        ("leb_ar", "أهلا"), ("leb_arabizi", "mar7aba"), ("mixed", "Hello merci")])
def test_supported_styles(style, text):
    assert reply_violations(reply(text, language=style), style=style, evidence=()) == ()


def test_context_keeps_arabizi_greeting():
    assert masked_messages([{"sender_type": "CUSTOMER", "body": "mar7aba"}], source_message_id=None) == [
        {"role": "user", "text": "mar7aba"}]


def test_authoritative_values_pass():
    body = "Order: ABC123. The price is $50 and the date is 2026-12-05."
    assert reply_violations(reply(body, facts_used=[body]), style="en", evidence=(body,)) == ()


def test_guard_applies_script_and_structured_language():
    assert reply_violations(reply("مرحبا", language="leb_arabizi"), style="leb_arabizi", evidence=())
    assert reply_violations(reply("Hello", language="en"), style="fr", evidence=()) == ("language_mismatch",)


@pytest.fixture
def routing(monkeypatch, fake_ai_ticket):
    h = Harness(monkeypatch)
    monkeypatch.setattr(main.settings, "ai_agent_v2_enabled", True)
    h.agent_calls = []
    h.escalations = []
    h.outcome = core.AgentOutcome(reply=reply())

    async def run(**kwargs):
        h.agent_calls.append(kwargs)
        return h.outcome

    async def escalate(ticket, **kwargs):
        h.escalations.append(kwargs)
        return {**ticket, "handling_mode": "HUMAN"}, True

    monkeypatch.setattr(core.agent_service, "run", run)
    monkeypatch.setattr(main.support_service, "escalate_ai_ticket", escalate)
    return h


def test_flag_routes_to_agent_before_legacy_auth_and_ai(routing):
    response_ = asyncio.run(routing.process(_incoming()))
    assert _status(response_) == "agent_replied"
    assert len(routing.agent_calls) == 1 and routing.ai_calls == 0
    assert routing.quota.calls == routing.provider_sends == 1


def test_off_flag_preserves_legacy_path(routing, monkeypatch):
    monkeypatch.setattr(main.settings, "ai_agent_v2_enabled", False)
    assert _status(asyncio.run(routing.process(_incoming()))) == "handled"
    assert routing.agent_calls == [] and routing.ai_calls == 1


def test_structured_escalation_moves_ticket_to_staff(routing):
    routing.outcome = core.AgentOutcome(reason_code="CHANGE_REQUIRES_HUMAN")
    assert _status(asyncio.run(routing.process(_incoming()))) == "agent_escalated"
    assert routing.escalations[0]["reason_code"] == "CHANGE_REQUIRES_HUMAN"
    assert routing.provider_sends == 0


def test_credentials_escalate_without_model_or_password_login(routing):
    assert _status(asyncio.run(routing.process(_incoming(text="password: secret123")))) == "agent_escalated"
    assert routing.agent_calls == [] and routing.ai_calls == 0
    assert routing.escalations[0]["reason_code"] == "IDENTITY_VERIFICATION_REQUIRED"


def test_quota_refusal_hands_off_without_model(routing, monkeypatch):
    from app.ai_rate_limit import AIQuotaDecision

    async def refused(_phone):
        return AIQuotaDecision(False, scope="day")

    monkeypatch.setattr(routing.quota, "consume", refused)
    assert _status(asyncio.run(routing.process(_incoming()))) == "agent_escalated"
    assert routing.escalations[0]["reason_code"] == "AI_RATE_LIMITED" and routing.agent_calls == []


def test_disable_during_call_suppresses_send(routing, monkeypatch):
    async def run(**kwargs):
        async def disabled(**kwargs):
            return SimpleNamespace(effective_ai_enabled=False)
        monkeypatch.setattr(main.ai_availability_service, "get_effective_ai_availability", disabled)
        return routing.outcome

    monkeypatch.setattr(core.agent_service, "run", run)
    assert _status(asyncio.run(routing.process(_incoming()))) == "handed_off_after_failed_turn"
    assert routing.provider_sends == 0 and routing.handoffs == ["AI_DISABLED_DURING_TURN"]


def test_failed_send_is_not_replayed(routing):
    from app.whatsapp_client import WhatsAppSendError
    routing.send_error = WhatsAppSendError("unknown outcome", delivery_uncertain=True)

    async def run():
        await routing.process(_incoming())
        await routing.process(_incoming())

    asyncio.run(run())
    assert len(routing.agent_calls) == routing.provider_sends == 1
    assert "AI_REPLY_SEND_FAILED" in routing.handoffs


def test_tool_result_is_rendered_without_another_model_call(agent, monkeypatch):
    from app.agent import tools, business_data
    from datetime import datetime, timezone

    async def identity(phone):
        return AIIdentity(level="L1", reason="PHONE_MATCH", customer_id="9")

    async def read(name, arguments, owner):
        assert owner.customer_id == "9"
        return business_data.BusinessResult(name, datetime.now(timezone.utc), "test", {
            "found": True, "count": 1, "shipments": [{"order_id": "O-42", "status": "Transit"}],
        })

    monkeypatch.setattr(core.customer_ai_identity_service, "resolve_or_unknown", identity)
    monkeypatch.setattr(tools, "business_data_source", SimpleNamespace(read=read))
    agent.state["responses"] = [response({"kind": "tool_call", "name": "get_customer_shipments", "arguments_json": "{}"})]
    outcome = asyncio.run(agent.run())
    assert "O-42" in outcome.reply.text and len(agent.calls) == 1


def test_identity_is_rechecked_after_provider_call(agent, monkeypatch):
    levels = ["L2", "L0"]

    async def identity(phone):
        return AIIdentity(level=levels.pop(0), reason="TEST", customer_id="9")

    monkeypatch.setattr(core.customer_ai_identity_service, "resolve_or_unknown", identity)
    agent.state["responses"] = [response({"kind": "tool_call", "name": "get_customer_balance", "arguments_json": "{}"})]
    assert asyncio.run(agent.run()).reason_code == "IDENTITY_VERIFICATION_REQUIRED"
    assert len(agent.calls) == 1 and levels == []


@pytest.mark.parametrize('reason', ['AI_UNCERTAIN', 'AI_LOW_CONFIDENCE', 'MULTIPLE_ISSUES'])
def test_defined_review_triggers_use_one_stronger_model(agent, reason):
    agent.state['responses'] = [response({'kind': 'escalate', 'reason_code': reason}), response()]
    assert asyncio.run(agent.run()).reply
    assert [call['model'] for call in agent.calls] == ['gpt-6-luna', 'gpt-6.2-sol']
    assert agent.calls[1]['reasoning'] == {'effort': 'low'}
    assert [meter['tier'] for meter in agent.meters] == ['primary', 'step_up']


def test_stronger_model_uncertainty_never_causes_third_call(agent):
    agent.state['responses'] = [response({'kind': 'escalate', 'reason_code': 'AI_LOW_CONFIDENCE'})] * 2
    assert asyncio.run(agent.run()).reason_code == 'AI_LOW_CONFIDENCE'
    assert len(agent.calls) == 2


def test_complaint_always_goes_to_human(agent):
    agent.state['responses'] = [response({'kind': 'escalate', 'reason_code': 'COMPLAINT_DISPUTE'})]
    assert asyncio.run(agent.run()).reason_code == 'COMPLAINT_DISPUTE'
    assert len(agent.calls) == 1


def test_image_in_same_call_and_notes_cached_once(agent, monkeypatch):
    from app.agent.media import PreparedMedia
    saved = []

    async def prepare(*args):
        return PreparedMedia(101, 'data:image/png;base64,c3ludGhldGlj')

    async def save(ticket, media, notes, repository):
        saved.append((media.message_id, notes))

    monkeypatch.setattr(core, 'prepare_media', prepare)
    monkeypatch.setattr(core, 'save_image_notes', save)
    def image_response(action):
        return SimpleNamespace(status='completed', output=[{'type': 'message', 'content': [{
            'type': 'output_text', 'text': json.dumps({'intent': 'greeting', 'action': action, 'media_notes': 'A parcel box.'}),
        }]}])
    agent.state['responses'] = [image_response(reply('It costs $999.').model_dump()), image_response(reply().model_dump())]
    assert asyncio.run(agent.run(media_type='image')).reply
    assert saved == [(101, 'A parcel box.')]
    assert len(agent.calls) == 2
    for request, meter in zip(agent.calls, agent.meters):
        content = request['input'][0]['content']
        assert content[1]['type'] == 'input_image'
        assert request['text']['format']['schema'] == core.IMAGE_DECISION_SCHEMA
        assert meter['images'] == 1 and 'base64' not in json.dumps(meter['input_payload'])


def test_single_model_benchmark_has_one_call_and_explicit_intent(agent):
    agent.state['responses'] = [response({'kind': 'escalate', 'reason_code': 'AI_LOW_CONFIDENCE'})]
    service = core.AgentService(model='gpt-6.1-terra', reasoning_effort='low', step_up=False, usage_purpose='eval')
    outcome = asyncio.run(service.run(ticket={'id': 47, 'whatsapp_phone': 'benchmark'},
                                      style='en', source_message_id='in.1'))
    assert outcome.intent == 'greeting' and outcome.reason_code == 'AI_LOW_CONFIDENCE'
    assert len(agent.calls) == 1
    assert agent.calls[0]['model'] == 'gpt-6.1-terra'
    assert agent.calls[0]['reasoning']['effort'] == 'low'
    assert agent.meters[0]['purpose'] == 'eval'


def test_single_model_benchmark_does_not_repair_rejected_reply(agent):
    agent.state['responses'] = [response(reply('Your balance is $999').model_dump())]
    service = core.AgentService(step_up=False)
    outcome = asyncio.run(service.run(ticket={'id': 47, 'whatsapp_phone': 'benchmark'},
                                      style='en', source_message_id='in.1'))
    assert outcome.reason_code == 'AI_REPLY_VALIDATION_FAILED' and len(agent.calls) == 1
