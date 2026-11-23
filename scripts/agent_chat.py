#!/usr/bin/env python3
"""Chat with the v2 agent core in a terminal, for manual testing on a disposable database.

Uses the real agent core, reply guard, tool registry and metered OpenAI gateway
(purpose ``eval``), with a synthetic identity, no approved knowledge by default
and unavailable business data, like the ``unavailable-v1`` benchmark profile.
Nothing is written to tickets and nothing is sent to WhatsApp.

Every turn is a paid OpenAI call. The script refuses to start unless the
configured database is local, holds no support tickets (so it is disposable),
the inbound v2 switch is off and the model's price is verified there.

Start a line with "+" to queue a customer message without a bot turn, like
several WhatsApp messages sent in a row; the next normal line triggers one turn.
Commands: /reset starts a new conversation, /cost shows spend, /quit exits.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
import uuid
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

LOCAL_HOSTS = {"127.0.0.1", "localhost"}
MAX_TURNS = 200


class ChatRefused(RuntimeError):
    pass


async def preflight(model: str) -> None:
    from app.ai_usage import ai_usage_service
    from app.config import settings

    if settings.db_host not in LOCAL_HOSTS:
        raise ChatRefused("DB_HOST must be a local disposable database (127.0.0.1 or localhost).")
    if settings.ai_agent_v2_enabled:
        raise ChatRefused("AI_AGENT_V2_ENABLED must stay false for manual testing.")
    rows = await ai_usage_service.repository._fetch("SELECT COUNT(*) AS n FROM support_tickets", one=True)
    if int((rows or {}).get("n") or 0):
        raise ChatRefused("The configured database has support tickets; use an empty disposable database.")
    if await ai_usage_service.price_for(model) is None:
        raise ChatRefused(f"Verify the price of {model} in the disposable database first.")


async def turn_cost(batch_id: str) -> tuple[int, bool]:
    from app.ai_usage import ai_usage_service

    events = await ai_usage_service.repository._fetch(
        "SELECT outcome, cost_basis, cost_micros FROM ai_usage_events WHERE purpose = 'eval' AND batch_id = %s",
        (batch_id,))
    complete = bool(events) and all(e["outcome"] == "success" and e["cost_basis"] == "computed" for e in events)
    return sum(int(e["cost_micros"] or 0) for e in events), complete


async def run_turn(history: list[dict], *, identity_level: str, knowledge: str, options: dict) -> tuple:
    from app.agent import core, tools
    from app.agent.business_data import BusinessDataError
    from app.ai_usage import usage_scope
    from app.customer_ai_identity import AIIdentity
    from app.language import detect_communication_style

    rows = [{"id": i + 1, "sender_type": m["sender_type"], "body": m["body"],
             "whatsapp_message_id": f"chat-{i}"} for i, m in enumerate(history)]
    style = "en"
    for message in history:
        if message["sender_type"] == "CUSTOMER":
            style = detect_communication_style(message["body"], previous_style=style).style

    async def context(*args, **kwargs):
        return rows

    async def snapshot(*args, **kwargs):
        return None, rows

    async def identity(_):
        return AIIdentity(level=identity_level, reason="LOCAL_CHAT_FIXTURE", customer_id="local-chat-only")

    async def approved_knowledge():
        return knowledge

    async def unavailable(*args, **kwargs):
        raise BusinessDataError("BUSINESS_DATA_UNAVAILABLE")

    attempted = []
    execute = tools.execute_tool

    async def capture(action, *args, **kwargs):
        attempted.append(action.name)
        return await execute(action, *args, **kwargs)

    batch_id = uuid.uuid4().hex
    token = usage_scope.set({"batch_id": batch_id})
    started = time.monotonic()
    try:
        with ExitStack() as stack:
            for owner, name, value in (
                (core, "support_repository", SimpleNamespace(list_ai_context_messages=context, ai_memory_snapshot=snapshot)),
                (core, "customer_ai_identity_service", SimpleNamespace(resolve_or_unknown=identity)),
                (core, "ai_knowledge_service", SimpleNamespace(prompt_text=approved_knowledge)),
                (core, "execute_tool", capture),
                (tools, "business_data_source", SimpleNamespace(read=unavailable)),
            ):
                stack.enter_context(patch.object(owner, name, value))
            outcome = await core.AgentService(**options, usage_purpose="eval").run(
                ticket={"id": 1, "whatsapp_phone": "local-chat-only"}, style=style,
                source_message_id=f"chat-{len(history) - 1}")
    finally:
        usage_scope.reset(token)
    seconds = time.monotonic() - started
    cost, complete = await turn_cost(batch_id)
    return outcome, attempted, style, seconds, cost, complete


async def chat(args) -> int:
    await preflight(args.model)
    knowledge = args.knowledge.read_text(encoding="utf-8") if args.knowledge else ""
    options = {"model": args.model, "reasoning_effort": args.reasoning, "step_up": False}
    print(f"Local agent chat: {args.model} ({args.reasoning}), identity {args.identity}, "
          f"knowledge {'from file' if knowledge else 'empty'}, business data unavailable.")
    print('Each turn is a paid OpenAI call. "+text" queues a message without a turn. Commands: /reset, /cost, /quit')
    history: list[dict] = []
    total = turns = 0
    while turns < MAX_TURNS:
        try:
            text = input("\nyou> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not text:
            continue
        if text == "/quit":
            break
        if text == "/reset":
            history = []
            print("(new conversation)")
            continue
        if text == "/cost":
            print(f"(spent ${total / 1_000_000:.4f} over {turns} turns)")
            continue
        if text.startswith("+"):
            if text[1:].strip():
                history.append({"sender_type": "CUSTOMER", "body": text[1:].strip()})
                print("(queued, no bot turn yet)")
            continue
        history.append({"sender_type": "CUSTOMER", "body": text})
        outcome, attempted, style, seconds, cost, complete = await run_turn(
            history, identity_level=args.identity, knowledge=knowledge, options=options)
        turns += 1
        total += cost
        if outcome.reply is not None:
            history.append({"sender_type": "AI", "body": outcome.reply.text})
            print(f"bot> {outcome.reply.text}")
        else:
            print(f"bot> [ESCALATED to staff: {outcome.reason_code}]")
        print(f"     intent={outcome.intent} tools={attempted or '-'} style={style} "
              f"{seconds:.1f}s ${cost / 1_000_000:.5f}{'' if complete else ' (usage incomplete)'}")
        if not complete:
            print("Stopping: usage is incomplete; check ai_usage_events before continuing.")
            break
    print(f"\nTotal: ${total / 1_000_000:.4f} over {turns} turns.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--identity", choices=("L0", "L1", "L2"), default="L1")
    parser.add_argument("--model", default="gpt-6-luna")
    parser.add_argument("--reasoning", choices=("none", "low"), default="none")
    parser.add_argument("--knowledge", type=Path, help="optional text file used as approved knowledge")
    args = parser.parse_args()
    # SDK/HTTP logs could contain message bodies; the terminal output above is the only output.
    previous = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        return asyncio.run(chat(args))
    except ChatRefused as exc:
        print(f"Refused: {exc}", file=sys.stderr)
        return 2
    except Exception:
        print("Chat stopped. Check the disposable database, Redis, key and prices; no exception body logged.",
              file=sys.stderr)
        return 1
    finally:
        logging.disable(previous)


if __name__ == "__main__":
    raise SystemExit(main())
