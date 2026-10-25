"""The Chats list must expose the same lifecycle state as the stored conversation."""
import json
from pathlib import Path
import subprocess

from fastapi import FastAPI
import httpx
import pytest

import app.support_auth as auth
from app.config import settings
from app.support_api import router
from tests.integration.test_agent_statistics import sessions


@pytest.mark.asyncio
@pytest.mark.parametrize('role', ['agent', 'supervisor', 'admin'])
async def test_chat_list_badges_follow_open_closed_and_reopened_state(infrastructure, monkeypatch, role):
    repo, sql, _ = infrastructure
    await sessions(sql)
    await sql("UPDATE support_tickets SET conversation_origin='STAFF' WHERE id IN (42,43)")
    await sql("INSERT INTO whatsapp_phone_numbers(phone_number_id,is_enabled,provider_present,source) VALUES ('90005',1,1,'META_SYNC')")
    if role != 'agent':
        await sql('UPDATE staff SET role=%s WHERE id=8', (role,))
    monkeypatch.setattr(auth, '_schedule_session_touch', lambda _: None)
    app = FastAPI()
    app.include_router(router)
    transport = httpx.MockTransport(httpx.ASGITransport(app=app).handle_async_request)
    rendered = []
    async with httpx.AsyncClient(transport=transport, base_url='http://test') as client:
        path = '/api/support/tickets?view=chats'
        assert (await client.get(path)).status_code == 401
        client.cookies.set(settings.support_cookie_name, 'token-1' if role == 'agent' else 'token-3')

        async def check_list(expected_open):
            response = await client.get(path)
            assert response.status_code == 200, response.text
            items = {item['id']: item for item in response.json()['items']}
            assert (43 in items) == (role != 'agent')  # No new access to another employee's active chat.
            assert 41 not in items  # Customer tickets remain outside Chats.
            item = items[42]
            stored = await repo.get_ticket(42)
            assert 'active_key' in item, 'Missing state makes the chat badge incorrectly show Closed'
            assert item['active_key'] == stored['active_key']
            assert bool(item['active_key']) is expected_open
            assert item['status'] == stored['status']
            rendered.append({'ticket': item, 'label': 'Open' if expected_open else 'Closed'})

        for status in ('ASSIGNED', 'IN_PROGRESS', 'WAITING_CUSTOMER', 'WAITING_INTERNAL'):
            await sql('UPDATE support_tickets SET status=%s WHERE id=42', (status,))
            await check_list(True)
        _, changed = await repo.close_staff_chat(42, agent_id='7', agent_name='Agent', expected_assignee_id='7')
        assert changed
        await check_list(False)
        _, changed = await repo.reopen_staff_chat(42, agent_id='7', agent_name='Agent')
        assert changed
        await check_list(True)

    # Exercise the shipped badge renderer with actual serialized API responses.
    source = Path('app/static/support/rendering.js').read_text()
    helpers = source[source.index('function isStaffChat('):source.index('function createMetaChip(')]
    script = "const assert=require('node:assert/strict'); const document={createElement:()=>({})};\n" + helpers + """
for (const {ticket,label} of JSON.parse(require('node:fs').readFileSync(0,'utf8'))) {
  assert.equal(conversationStatusBadge(ticket).textContent,label);
}
"""
    result = subprocess.run(['node', '-e', script], input=json.dumps(rendered), text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
