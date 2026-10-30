"""Durable request identity across the API and repository, with real MariaDB."""
import asyncio
from types import SimpleNamespace

import pytest

from app.support_auth import SupportAgent
from app.support_idempotency import send_fingerprint
from app.support_repository import SupportConflictError
from app.support_service import support_service
import app.support_service as service


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['text', 'location', 'image', 'document', 'audio', 'video', 'sticker'])
async def test_payload_key_conflicts_never_dispatch_again(infrastructure, monkeypatch, tmp_path, kind):
    repo, sql, _ = infrastructure
    calls = []
    audio_bytes = b''
    if kind == 'audio':
        import subprocess
        audio_path = tmp_path / 'voice.mp3'
        subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'sine=frequency=440:duration=0.1',
                        str(audio_path)], check=True, capture_output=True)
        audio_bytes = audio_path.read_bytes()
    async def send(**kwargs):
        calls.append(kwargs)
        return {'messages': [{'id': 'ld:90005:' + kwargs['client_message_id']}]}
    async def upload(**kwargs):
        return 'asset'
    from app.support_media import SupportMediaStore
    monkeypatch.setattr(service.settings, 'support_media_directory', str(tmp_path))
    monkeypatch.setattr(service, 'support_media_store', SupportMediaStore())
    monkeypatch.setattr(service, 'whatsapp_client', SimpleNamespace(send_text=send, send_location=send, send_media=send, upload_media=upload))
    async def request(value, key='logical-send'):
        common = dict(agent=SupportAgent(id='7', name='Agent', role='agent'), client_message_id=key)
        if kind == 'text':
            return await support_service.send_agent_message(42, body=value, **common)
        if kind == 'location':
            return await support_service.send_agent_location(42, latitude=1, longitude=2, name=value, address=None, **common)
        if kind == 'audio':
            # Exercise the real voice conversion with generated, non-customer audio.
            return await support_service.send_agent_media(42, media_type=kind, content=audio_bytes if value == 'synthetic' else value.encode(), content_type='audio/mpeg', filename='test.mp3', caption='Audio attachment', **common)
        return await support_service.send_agent_media(42, media_type=kind, content=value.encode(),
            content_type={'image':'image/png','document':'application/pdf','video':'video/mp4','sticker':'image/webp'}[kind],
            filename='test.' + {'image':'png','document':'pdf','video':'mp4','sticker':'webp'}[kind], caption=None, **common)
    original = await request('synthetic')
    assert (await request('synthetic'))['id'] == original['id']
    with pytest.raises(SupportConflictError):
        await request('changed')
    assert len(calls) == 1
    # Identical content with a new key is an intentional second send.
    second = await request('synthetic', 'separate-send')
    assert second['id'] != original['id'] and len(calls) == 2
    # Nullable legacy rows remain readable; replay never guesses a fingerprint.
    await sql('UPDATE support_ticket_messages SET request_fingerprint=NULL WHERE id=%s', (original['id'],))
    with pytest.raises(SupportConflictError):
        await request('synthetic')
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_concurrent_different_payloads_share_no_row_and_migration_is_repeatable(infrastructure):
    from pathlib import Path
    repo, sql, _ = infrastructure
    gate = asyncio.Event()
    async def insert(body):
        await gate.wait()
        return await repo.add_ticket_message(ticket_id=42, sender_type='AGENT', sender_id='7', direction='OUTBOUND',
            body=body, client_message_id='same-key', request_fingerprint=send_fingerprint('text', body=body))
    tasks = [asyncio.create_task(insert(value)) for value in ['first', 'second']]
    gate.set()
    results = await asyncio.gather(*tasks, return_exceptions=True)
    assert sum(isinstance(result, SupportConflictError) for result in results) == 1
    rows = await sql("SELECT * FROM support_ticket_messages WHERE client_message_id='same-key'")
    assert len(rows) == 1 and rows[0]['request_fingerprint'] == send_fingerprint('text', body=rows[0]['body'])
    for _ in range(2):
        await sql(Path('migrations/20261029_support_send_fingerprints.sql').read_text())
    assert (await repo.get_message(rows[0]['id']))['request_fingerprint'] == rows[0]['request_fingerprint']
