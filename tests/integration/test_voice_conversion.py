"""Malformed browser audio fails before persistence or provider submission."""
from types import SimpleNamespace
import subprocess

import pytest

from app.support_auth import SupportAgent
from app.support_repository import SupportConflictError
from app.support_media import SupportMediaStore
from app.support_service import SupportValidationError, support_service
import app.support_service as service


@pytest.mark.asyncio
async def test_invalid_mp4_then_fresh_recording_is_safe_and_idempotent(infrastructure, monkeypatch, tmp_path, caplog):
    _, sql, _ = infrastructure
    source = tmp_path/'source.m4a'
    subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','sine=frequency=440:duration=0.2',
                    '-c:a','aac','-movflags','frag_keyframe+empty_moov',str(source)], check=True, capture_output=True)
    valid = source.read_bytes()
    assert b'tfhd' in valid
    # Remove required fragment headers to reproduce the exact deployed decoder error.
    invalid = valid.replace(b'tfhd', b'free')
    calls = []
    async def upload(**kwargs):
        calls.append(('upload', kwargs))
        assert kwargs['content'].startswith(b'OggS')
        assert kwargs['filename'] == 'voice-note.opus'
        assert kwargs['mime_type'] == 'audio/ogg; codecs=opus'
        return 'asset'
    async def send(**kwargs):
        calls.append(('send', kwargs))
        assert kwargs['voice'] is True
        return {'messages':[{'id':'ld:90005:voice-confirmed'}]}
    monkeypatch.setattr(service, 'whatsapp_client', SimpleNamespace(upload_media=upload,send_media=send))
    monkeypatch.setattr(service.settings, 'support_media_directory', str(tmp_path/'stored'))
    monkeypatch.setattr(service, 'support_media_store', SupportMediaStore())
    async def request(data):
        return await support_service.send_agent_media(42, agent=SupportAgent(id='7',name='Agent',role='agent'),
            media_type='audio',content=data,content_type='audio/mp4',filename='voice.m4a',caption=None,
            client_message_id='voice-client-key')
    for _ in range(2):
        with pytest.raises(SupportValidationError, match='record it again'):
            await request(invalid)
    assert 'no tfhd was found' in caplog.text
    assert 'ticket_id=42 agent_id=7 mime=audio/mp4' in caplog.text
    assert not calls
    assert not await sql('SELECT id FROM support_ticket_messages WHERE ticket_id=42')
    result = await request(valid)
    assert result['send_status'] == 'SENT'
    stored = service.support_media_store.get(42, result['id'])
    assert stored.path.suffix == '.opus' and stored.filename == 'voice-note.opus'
    # Same logical request returns its result; key reuse for changed bytes is rejected.
    assert (await request(valid))['id'] == result['id']
    with pytest.raises(SupportConflictError):
        await request(invalid)
    assert [kind for kind, _ in calls] == ['upload','send']
    assert len(await sql('SELECT id FROM support_ticket_messages WHERE ticket_id=42')) == 1
