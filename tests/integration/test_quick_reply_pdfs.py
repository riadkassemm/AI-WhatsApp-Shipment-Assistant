import asyncio
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

import app.support_auth as auth
import app.support_service as service
from app.config import settings
from app.support_api import router
from app.support_auth import SupportAgent
from app.support_media import SupportMediaStore
from app.support_repository import SupportNotFoundError

PDF = b'%PDF-1.4\n1 0 obj << /Type /Catalog >> endobj\n%%EOF\n'
AGENT = SupportAgent(id='7',name='Agent',role='agent')


async def create_sessions(sql):
    for ident in (7,8):
        await sql('''INSERT INTO staff_sessions(id,staff_id,token_hash,csrf_hash,expires_at,created_at,last_seen_at)
            VALUES (%s,%s,%s,%s,UTC_TIMESTAMP(6)+INTERVAL 1 DAY,UTC_TIMESTAMP(6),UTC_TIMESTAMP(6))''',
            (ident,ident,auth._sha256(f'token-{ident}'),auth._sha256('csrf')))


@pytest.mark.asyncio
async def test_pdf_api_private_crud_and_send_uses_existing_delivery(infrastructure,monkeypatch,tmp_path):
    repo,sql,_ = infrastructure
    await create_sessions(sql)
    monkeypatch.setattr(settings,'support_media_directory',str(tmp_path))
    media = SupportMediaStore()
    monkeypatch.setattr(service,'support_media_store',media)
    monkeypatch.setattr(auth,'_schedule_session_touch',lambda _:None)
    calls=[]
    async def upload(**kw): return 'uploaded-test-file'
    async def send(**kw):
        calls.append(kw)
        return {'messages':[{'id':'ld:90005:pdf-test'}]}
    monkeypatch.setattr(service,'whatsapp_client',SimpleNamespace(upload_media=upload,send_media=send))
    app=FastAPI();app.include_router(router)
    asgi=httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=httpx.MockTransport(asgi.handle_async_request),base_url='http://test') as client:
        path='/api/support/quick-replies'
        assert (await client.post(path+'/pdf',files={'file':('x.pdf',PDF,'application/pdf')})).status_code==401
        client.cookies.set(settings.support_cookie_name,'token-7')
        assert (await client.post(path+'/pdf',files={'file':('x.pdf',PDF,'application/pdf')})).status_code==403
        client.headers['X-CSRF-Token']='csrf'
        saved=await client.post(path+'/pdf',files={'file':('Guide.pdf',PDF,'application/pdf')},data={'title':'Guide'})
        assert saved.status_code==201,saved.text
        row=saved.json();ident=row['id']
        assert row['body']=='' and row['attachment_filename']=='Guide.pdf'
        assert row['attachment_size']==len(PDF) and 'attachment_key' not in row and 'staff_id' not in row
        download=await client.get(f'{path}/{ident}/pdf')
        assert download.content==PDF and download.headers['content-type']=='application/pdf'
        assert 'attachment;' in download.headers['content-disposition'] and 'no-store' in download.headers['cache-control']
        client.cookies.set(settings.support_cookie_name,'token-8')
        assert (await client.get(path)).json()=={'items':[]}
        assert (await client.get(f'{path}/{ident}/pdf')).status_code==404
        assert (await client.put(f'{path}/{ident}/pdf',files={'file':('other.pdf',PDF,'application/pdf')})).status_code==404
        assert (await client.put(f'{path}/{ident}',json={'body':'intrusion'})).status_code==404
        assert (await client.delete(f'{path}/{ident}')).status_code==404
        assert len(list((tmp_path/'quick-replies').glob('*.pdf')))==1
        client.cookies.set(settings.support_cookie_name,'token-7')
        assert (await client.put(f'{path}/{ident}',json={'body':'caption','title':'New title'})).status_code==200
        assert (await client.put(f'{path}/{ident}',json={'body':'x'*1025})).status_code==422
        old_file=next((tmp_path/'quick-replies').glob('*.pdf'))
        updated=await client.put(f'{path}/{ident}/pdf',files={'file':('New.pdf',PDF+b'\n','application/pdf')},data={'body':'New caption'})
        assert updated.status_code==200,updated.text
        assert not old_file.exists() and len(list((tmp_path/'quick-replies').glob('*.pdf')))==1
        for _ in range(2):
            sent=await client.post('/api/support/tickets/42/media',files={'file':('New.pdf',PDF+b'\n','application/pdf')},
                data={'media_type':'document','caption':'New caption','client_message_id':'saved-pdf-send'})
            assert sent.status_code==200,sent.text
        assert len(calls)==1
        message=sent.json()['message']
        assert message['sender_id']=='7' and message['send_status']=='SENT'
        assert (await client.delete(f'{path}/{ident}')).status_code==200
        assert not list((tmp_path/'quick-replies').glob('*.pdf'))
        assert media.get(42,message['id']).path.read_bytes()==PDF+b'\n'
        assert (await client.get(f'{path}/{ident}/pdf')).status_code==404


@pytest.mark.asyncio
async def test_pdf_validation_limits_and_repeatable_migration(infrastructure,monkeypatch,tmp_path):
    repo,sql,_=infrastructure
    monkeypatch.setattr(settings,'support_media_directory',str(tmp_path))
    for content in (b'',b'<html>not a PDF</html>'):
        with pytest.raises(service.SupportValidationError,match='valid PDF'):
            await service.support_service.save_quick_reply_pdf(AGENT,title='',body='',content=content,filename='x.pdf')
    monkeypatch.setattr(settings,'support_document_max_bytes',10)
    with pytest.raises(service.SupportValidationError,match='too large'):
        await service.support_service.save_quick_reply_pdf(AGENT,title='',body='',content=PDF,filename='x.pdf')
    monkeypatch.setattr(settings,'support_document_max_bytes',1024)
    with pytest.raises(service.SupportValidationError,match='1024'):
        await service.support_service.save_quick_reply_pdf(AGENT,title='',body='x'*1025,content=PDF,filename='x.pdf')
    import app.support_repository as repository_module
    monkeypatch.setattr(repository_module,'MAX_QUICK_REPLIES_PER_STAFF',1)
    row=await service.support_service.save_quick_reply_pdf(AGENT,title='',body='',content=PDF,filename='../../folder\\Guide.pdf')
    assert row['attachment_filename']=='Guide.pdf'
    with pytest.raises(repository_module.SupportConflictError):
        await service.support_service.save_quick_reply_pdf(AGENT,title='',body='',content=PDF,filename='x.pdf')
    assert len(list((tmp_path/'quick-replies').glob('*.pdf')))==1
    await sql(Path('migrations/20261023_quick_reply_pdfs.sql').read_text())
    assert (await service.support_service.get_quick_reply_pdf(AGENT,row['id']))[0]==PDF


@pytest.mark.asyncio
async def test_delete_wins_during_pdf_replacement(infrastructure,monkeypatch,tmp_path):
    repo,sql,_=infrastructure
    monkeypatch.setattr(settings,'support_media_directory',str(tmp_path))
    row=await service.support_service.save_quick_reply_pdf(AGENT,title='',body='',content=PDF,filename='a.pdf')
    original=repo.replace_quick_reply_pdf
    entered,release=asyncio.Event(),asyncio.Event()
    async def pause(*args,**kwargs):
        entered.set();await release.wait()
        return await original(*args,**kwargs)
    monkeypatch.setattr(repo,'replace_quick_reply_pdf',pause)
    task=asyncio.create_task(service.support_service.save_quick_reply_pdf(AGENT,reply_id=row['id'],title='',body='',content=PDF,filename='b.pdf'))
    await asyncio.wait_for(entered.wait(),5)
    await service.support_service.delete_quick_reply(AGENT,row['id'])
    release.set()
    with pytest.raises(SupportNotFoundError): await task
    assert not list((tmp_path/'quick-replies').glob('*.pdf'))
    assert not await sql('SELECT * FROM support_quick_replies')


@pytest.mark.asyncio
async def test_ambiguous_save_keeps_committed_pdf_and_caption_guard(infrastructure,monkeypatch,tmp_path):
    repo,sql,_=infrastructure
    monkeypatch.setattr(settings,'support_media_directory',str(tmp_path))
    create=repo.create_quick_reply
    async def committed_but_lost_response(*args,**kwargs):
        await create(*args,**kwargs)
        raise OSError('lost acknowledgement')
    monkeypatch.setattr(repo,'create_quick_reply',committed_but_lost_response)
    with pytest.raises(OSError):
        await service.support_service.save_quick_reply_pdf(AGENT,title='',body='',content=PDF,filename='a.pdf')
    rows=await repo.list_quick_replies(7)
    assert len(rows)==1
    assert (await service.support_service.get_quick_reply_pdf(AGENT,rows[0]['id']))[0]==PDF
    # A stale text editor cannot bypass the PDF caption limit after replacement.
    from app.support_repository import SupportConflictError
    with pytest.raises(SupportConflictError,match='1024'):
        await repo.update_quick_reply(7,rows[0]['id'],title='',body='x'*1025)
    assert (await repo.get_quick_reply(7,rows[0]['id']))['body']==''
