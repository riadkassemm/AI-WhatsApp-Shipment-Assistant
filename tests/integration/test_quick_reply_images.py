"""Real persistence and private image endpoints; providers never receive messages."""
import base64
import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from app.config import settings
from app.support_api import router
from app.support_auth import SupportAgent
import app.support_auth as auth
import app.support_service as service
from tests.integration.test_quick_reply_pdfs import create_sessions

PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jGQAAAABJRU5ErkJggg==')


@pytest.mark.asyncio
async def test_images_private_crud_distribution_and_repeatable_migration(infrastructure, monkeypatch, tmp_path):
    repo, sql, _ = infrastructure
    await create_sessions(sql)
    monkeypatch.setattr(settings, 'support_media_directory', str(tmp_path))
    monkeypatch.setattr(auth, '_schedule_session_touch', lambda _: None)
    app = FastAPI(); app.include_router(router)
    asgi = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=httpx.MockTransport(asgi.handle_async_request), base_url='http://test') as client:
        path = '/api/support/quick-replies'
        files = {'file': ('Map.png', PNG, 'image/png')}
        assert (await client.post(path+'/image', files=files)).status_code == 401
        client.cookies.set(settings.support_cookie_name, 'token-7')
        assert (await client.post(path+'/image', files=files)).status_code == 403
        client.headers['X-CSRF-Token'] = 'csrf'
        created = await client.post(path+'/image', files=files, data={'title': 'Map', 'body': 'Find us here'})
        assert created.status_code == 201, created.text
        row = created.json(); ident = row['id']
        assert row['attachment_mime_type'] == 'image/png' and 'attachment_key' not in row
        response = await client.get(f'{path}/{ident}/image')
        assert response.content == PNG and response.headers['content-type'] == 'image/png'
        assert 'no-store' in response.headers['cache-control']
        assert (await client.get(f'{path}/{ident}/pdf')).status_code == 404
        assignment = {'title': 'Map', 'body': 'Find us here', 'all_staff': True, 'recipient_ids': []}
        assert (await client.post(path+'/assign/image', files=files, data={'assignment': json.dumps(assignment)})).status_code == 403
        client.cookies.set(settings.support_cookie_name, 'token-8')
        assert (await client.get(f'{path}/{ident}/image')).status_code == 404
        assert (await client.put(f'{path}/{ident}/image', files=files)).status_code == 404
        for expected in [(1, 1), (0, 2)]:
            distributed = await client.post(path+'/assign/image', files=files, data={'assignment': json.dumps(assignment)})
            assert distributed.status_code == 201, distributed.text
            assert (distributed.json()['saved_count'], distributed.json()['skipped_count']) == expected
        own = distributed.json()['item']['id']
        shared = await client.post(f'{path}/{own}/assign', json={'recipient_ids': [7]})
        assert shared.status_code == 201 and shared.json()['skipped_count'] == 1
        replacement = await client.put(f'{path}/{own}/image', files=files, data={'title': 'Replacement'})
        assert replacement.status_code == 200
        client.cookies.set(settings.support_cookie_name, 'token-7')
        assert (await client.get(f'{path}/{ident}/image')).content == PNG
        assert (await client.delete(f'{path}/{ident}')).status_code == 200
        assert (await client.get(f'{path}/{ident}/image')).status_code == 404
        assert (await client.post(path+'/image', files={'file': ('fake.png', b'not an image', 'image/png')})).status_code == 422
        assert (await client.post(path+'/image', files={'file': ('x.svg', b'<svg/>', 'image/svg+xml')})).status_code == 422
        monkeypatch.setattr(settings, 'support_image_max_bytes', 10)
        assert (await client.post(path+'/image', files=files)).status_code == 422
    from pathlib import Path
    await sql(Path('migrations/20261027_quick_reply_images.sql').read_text())
    assert (await repo.get_quick_reply(8, own))['attachment_mime_type'] == 'image/png'
