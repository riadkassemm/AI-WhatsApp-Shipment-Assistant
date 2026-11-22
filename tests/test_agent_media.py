from test_ai_ticket_turns import main  # dependency doubles

import asyncio
from types import SimpleNamespace

import pytest

from app.agent import media
from app.agent.business_data import BusinessDataError


@pytest.fixture
def env(monkeypatch, tmp_path):
    path = tmp_path / 'synthetic.ogg'
    path.write_bytes(b'synthetic audio')
    state = SimpleNamespace(row={'id': 7, 'ticket_id': 41, 'sender_type': 'CUSTOMER', 'direction': 'INBOUND',
                                 'message_type': 'audio'}, calls=[], saves=[], meters=[], options=[], failures=[])
    state.asset = SimpleNamespace(path=path, size_bytes=path.stat().st_size, mime_type='audio/ogg')

    async def get_message(_):
        return state.row

    async def save(*args, **kwargs):
        state.saves.append((args, kwargs))
        state.row.update(ai_media_processed_at='saved', ai_media_text=kwargs['text'])
        return True

    async def duration(path):
        return 31

    async def create(**kwargs):
        state.calls.append(kwargs)
        if getattr(state, 'failure', None):
            raise state.failure
        return SimpleNamespace(text='Bonjour, où est mon colis ?')

    client = SimpleNamespace(audio=SimpleNamespace(transcriptions=SimpleNamespace(create=create)))
    def options(**kwargs):
        state.options.append(kwargs)
        return client
    client.with_options = options

    async def get_client(_):
        return client

    async def meter(**kwargs):
        state.meters.append(kwargs)
        return await kwargs['call']()

    async def report(exc):
        state.failures.append(type(exc).__name__)

    monkeypatch.setattr(media.support_media_store, 'get', lambda *args: state.asset)
    monkeypatch.setattr(media, 'audio_duration', duration)
    monkeypatch.setattr(media.ai_provider_service, 'get_client', get_client)
    monkeypatch.setattr(media.ai_provider_service, 'report_failure', report)
    monkeypatch.setattr(media.ai_usage_service, 'metered', meter)
    state.repo = SimpleNamespace(get_message_by_whatsapp_id=get_message, save_ai_media_annotation=save)
    state.run = lambda: asyncio.run(media.prepare_media({'id': 41}, 'wamid.synthetic', state.repo))
    return state


def test_transcription_is_metered_and_reused(env):
    assert env.run().image_url is None
    env.run()
    assert len(env.calls) == len(env.saves) == len(env.meters) == 1
    assert env.calls[0]['model'] == 'gpt-transcribe'
    assert env.meters[0]['audio_seconds'] == 31 and env.meters[0]['modalities'] == 'audio'
    assert env.options == [{'max_retries': 0}]


@pytest.mark.parametrize('change', [{'ticket_id': 99}, {'deleted_for_everyone_at': 'deleted'}, {'sender_type': 'NOTE'}, {'direction': 'OUTBOUND'}])
def test_wrong_ticket_deleted_and_non_customer_media_blocked(env, change):
    env.row.update(change)
    with pytest.raises(BusinessDataError):
        env.run()
    assert env.calls == env.saves == []


def test_oversized_media_is_not_uploaded(env, monkeypatch):
    monkeypatch.setattr(media.settings, 'ai_agent_media_max_bytes', 2)
    with pytest.raises(BusinessDataError):
        env.run()
    assert env.calls == []


def test_image_preparation_never_makes_separate_provider_call(env):
    env.row['message_type'] = 'image'
    env.asset.mime_type = 'image/png'
    assert env.run().image_url.startswith('data:image/png;base64,')
    assert env.calls == env.saves == []


def test_provider_failure_has_no_retry_or_raw_logs(env, caplog):
    env.failure = RuntimeError('private-provider-body')
    with pytest.raises(RuntimeError):
        env.run()
    assert len(env.calls) == 1 and env.failures == ['RuntimeError']
    assert 'private-provider-body' not in caplog.text


def test_real_audio_duration_and_limit(tmp_path, monkeypatch):
    import wave
    path = tmp_path / 'synthetic.wav'
    with wave.open(str(path), 'wb') as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(8000)
        output.writeframes(b'\x00\x00' * 16000)
    assert asyncio.run(media.audio_duration(path)) == 2
    monkeypatch.setattr(media.settings, 'ai_agent_audio_max_seconds', 1)
    with pytest.raises(BusinessDataError):
        asyncio.run(media.audio_duration(path))
