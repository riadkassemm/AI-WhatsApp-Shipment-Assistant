"""Payload-free durable turn traces, linked to pending usage before provider calls."""
from __future__ import annotations

import contextvars
import hashlib
import logging
import re
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from app.ai_usage import usage_scope

logger = logging.getLogger('shipment-bot')


@dataclass
class TraceState:
    tools: list[str] = field(default_factory=list)
    guards: list[dict] = field(default_factory=list)
    outcome: str = 'started'
    reason_code: str | None = None


active_trace: contextvars.ContextVar[TraceState | None] = contextvars.ContextVar('agent_trace', default=None)
_GUARDS = frozenset({
    'pass', 'invalid_structured_output', 'language_mismatch', 'credential_disclosure',
    'password_reference', 'unsupported_fact_reference', 'ungrounded_value', 'ungrounded_amount',
    'empty', 'too_long', 'control_character', 'internal_note_leak', 'arabic_script_in_arabizi',
    'must_escalate_rule',
})


def record_guard(stage: str, verdicts) -> None:
    state = active_trace.get()
    if state is not None:
        state.guards.append({'stage': stage if stage in {'reply', 'structured', 'tool', 'summary'} else 'unknown',
                             'verdicts': [value if value in _GUARDS else 'rejected' for value in (verdicts or ('pass',))]})


def record_tool(name: str, allowed) -> None:
    state = active_trace.get()
    if state is not None:
        state.tools.append(name if name in allowed else 'unknown')


def record_outcome(outcome: str, reason: str | None = None) -> None:
    state = active_trace.get()
    if state is not None:
        state.outcome = outcome if outcome in {'replied', 'escalated', 'suppressed', 'failed'} else 'failed'
        state.reason_code = reason if reason and re.fullmatch(r'[A-Z_]{1,80}', reason) else None


@asynccontextmanager
async def trace_turn(repository, *, ticket_id: int, source_message_id: str | None):
    if not source_message_id:
        raise RuntimeError('Trace requires a source message identifier')
    turn_id = hashlib.sha256(f'{ticket_id}:{source_message_id}'.encode()).hexdigest()
    if not await repository.begin_agent_trace(turn_id, ticket_id):
        # Never repeat a turn that has a durable trace, even after Redis loss.
        raise RuntimeError('Agent turn already recorded')
    state = TraceState()
    token = active_trace.set(state)
    usage_token = usage_scope.set({**(usage_scope.get() or {}), 'ticket_id': ticket_id, 'agent_turn_id': turn_id})
    started = time.monotonic()
    try:
        yield state
    except BaseException:
        record_outcome('failed', 'AI_TURN_FAILED')
        raise
    finally:
        try:
            await repository.finish_agent_trace(turn_id, outcome=state.outcome, reason_code=state.reason_code,
                tools=state.tools, guards=state.guards, latency_ms=max(0, int((time.monotonic() - started) * 1000)))
        except Exception:
            # The start row and usage links survive. Never turn a sent reply into
            # a resend attempt just because final trace aggregation is unavailable.
            logger.warning('AI trace finalization unavailable; durable start and usage links retained')
        finally:
            usage_scope.reset(usage_token)
            active_trace.reset(token)
