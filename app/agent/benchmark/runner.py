"""DEV-only paid replay, isolated from customer repositories and all transports.

Imported only by the explicit submit path. Shared pricing, budget reservations,
provider credentials, and durable metering remain active. No real ticket writes.
"""
from __future__ import annotations

import json
import socket
import time
import uuid
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.agent.benchmark.files import write_private
from app.agent.benchmark.scoring import digest, observation_digest


def require_paid_approval(approval: dict, *, config: dict, bundle_sha256: str,
                          hostname: str, db_host: str, ai_enabled: bool) -> None:
    if (approval.get("balance_confirmed") is not True or approval.get("environment") != "DEV"
            or approval.get("dev_hostname") != hostname or not hostname
            or approval.get("bundle_sha256") != bundle_sha256
            or approval.get("config_sha256") != digest(config)
            or approval.get("deployment_approved") is not True
            or approval.get("privacy_reviewed") is not True
            or ai_enabled or db_host not in {"127.0.0.1", "localhost", "158.220.113.93"}):
        raise ValueError("Paid benchmark prerequisites are not satisfied")


async def replay_case(case: dict, options: dict, *, configuration: str, run_id: str,
                      output: Path, bundle_sha256: str) -> dict:
    # This function runs sequentially in a dedicated CLI process. Patches are
    # module references, never live application objects in a serving process.
    from app.agent import core, tools
    from app.agent.business_data import BusinessDataError
    from app.ai_usage import ai_usage_service, usage_scope
    from app.customer_ai_identity import AIIdentity
    from app.language import detect_communication_style

    observation_id = uuid.uuid4().hex
    path = output / f"{observation_id}.json"
    started = {"status": "started", "case_id": case["case_id"], "configuration": configuration,
               "run_id": run_id, "batch_id": observation_id, "bundle_sha256": bundle_sha256}
    # Durable before the first provider call. Never silently resume/replay a
    # started case after a crash; its spend may already have been incurred.
    write_private(path, started, exclusive=True)
    rows = [{"id": i + 1, "sender_type": "CUSTOMER" if m["role"] == "user" else "AI",
             "body": m["text"], "whatsapp_message_id": f"prior-{i}"} for i, m in enumerate(case["context"])]
    rows.append({"id": len(rows) + 1, "sender_type": "CUSTOMER", "body": case["text"],
                 "whatsapp_message_id": "benchmark-current"})

    async def identity(_):
        return AIIdentity(level=case["identity_level"], reason="BENCHMARK_FIXTURE", customer_id="benchmark-only")

    async def knowledge():
        return case["approved_knowledge"]

    async def context(*args, **kwargs):
        return rows

    async def snapshot(*args, **kwargs):
        return None, rows

    async def unavailable(*args, **kwargs):
        raise BusinessDataError("BUSINESS_DATA_UNAVAILABLE")

    attempted_tools = []
    execute = tools.execute_tool

    async def capture(action, *args, **kwargs):
        try:
            spec = tools.REGISTRY.get(action.name)
            arguments = spec[1].model_validate_json(action.arguments_json).model_dump() if spec else json.loads(action.arguments_json)
        except ValueError:
            arguments = {"invalid_arguments": True}
        attempted_tools.append({"name": action.name, "arguments": arguments})
        return await execute(action, *args, **kwargs)

    service = core.AgentService(**options, usage_purpose="eval")
    style = "en"
    for message in case["context"] + [{"role": "user", "text": case["text"]}]:
        if message["role"] == "user":
            style = detect_communication_style(message["text"], previous_style=style).style
    token = usage_scope.set({"batch_id": observation_id})
    timer = time.monotonic()
    try:
        with ExitStack() as stack:
            for owner, name, value in (
                (core, "support_repository", SimpleNamespace(list_ai_context_messages=context, ai_memory_snapshot=snapshot)),
                (core, "customer_ai_identity_service", SimpleNamespace(resolve_or_unknown=identity)),
                (core, "ai_knowledge_service", SimpleNamespace(prompt_text=knowledge)),
                (core, "execute_tool", capture),
                (tools, "business_data_source", SimpleNamespace(read=unavailable)),
            ):
                stack.enter_context(patch.object(owner, name, value))
            outcome = await service.run(ticket={"id": 1, "whatsapp_phone": "benchmark-only"},
                                        style=style, source_message_id="benchmark-current")
    finally:
        usage_scope.reset(token)
    latency = time.monotonic() - timer
    events = await ai_usage_service.repository._fetch(
        "SELECT reservation_id, requested_model, model, outcome, cost_basis, cost_micros, "
        "input_tokens, cached_input_tokens, output_tokens, price_id FROM ai_usage_events "
        "WHERE purpose = 'eval' AND batch_id = %s ORDER BY created_at, reservation_id", (observation_id,))
    usage_complete = bool(events) and all(e["outcome"] == "success" and e["cost_basis"] == "computed"
        and all(e.get(k) is not None for k in ("input_tokens", "cached_input_tokens", "output_tokens")) for e in events)
    row = {**started, "status": "completed", "split": case["split"], "input_sha256": digest(case),
           "intent": outcome.intent, "tools": attempted_tools, "escalated": outcome.reply is None,
           "reason_code": outcome.reason_code, "reply": outcome.reply.text if outcome.reply else "",
           "detected_style": style, "latency_seconds": latency,
           "usage_complete": usage_complete, "cost_micros": sum(int(e["cost_micros"]) for e in events) if events else None,
           "usage": [dict(e) for e in events]}
    row["review"] = {"reviewed": False, "reviewer": "", "observation_sha256": observation_digest(row),
                     "language_style_match": None, "unsafe_disclosures": None, "factual_inconsistencies": None}
    write_private(path, row)
    return row


async def run_paid(cases: list[dict], config: dict, approval: dict, *, output: Path,
                   bundle_sha256: str, configurations: list[str]) -> None:
    from app.ai_usage import ai_usage_service
    from app.config import settings

    require_paid_approval(approval, config=config, bundle_sha256=bundle_sha256,
                          hostname=socket.gethostname(), db_host=settings.db_host,
                          ai_enabled=settings.ai_agent_v2_enabled)
    models = {options[key] for name, options in config["configurations"].items() if name in configurations
              for key in ("model", "step_up_model") if options.get(key)}
    # Preflight all prices before the first call; metered() rechecks each call.
    for model in models:
        if await ai_usage_service.price_for(model) is None:
            raise ValueError("Verify every selected model price in Usage & Costs before submitting")
    if any(output.iterdir()):
        raise ValueError("Use a new empty run directory; replay after a crash requires explicit reconciliation")
    run_id = uuid.uuid4().hex
    write_private(output / "run.json", {"run_id": run_id, "bundle_sha256": bundle_sha256,
        "config_sha256": digest(config), "configurations": configurations, "cases": len(cases),
        "configuration_options": {name: config["configurations"][name] for name in configurations}}, exclusive=True)
    for configuration in configurations:
        for case in cases:
            row = await replay_case(case, config["configurations"][configuration], configuration=configuration,
                                    run_id=run_id, output=output, bundle_sha256=bundle_sha256)
            if not row["usage_complete"]:
                raise ValueError("Run stopped: usage is incomplete; reconcile durable reservations before another run")
