"""Per-item forwards reuse scoped delivery ownership with real persistence."""
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from app.support_auth import SupportAgent
from app.support_media import SupportMediaStore
from app.support_service import support_service, SupportForbiddenError
import app.support_service as service


@pytest.mark.asyncio
async def test_multiple_forward_items_deduplicate_dispatch_and_preserve_access(infrastructure, monkeypatch, tmp_path):
    repo, sql, _ = infrastructure
    monkeypatch.setattr(service.settings, 'support_media_directory', str(tmp_path))
    store=SupportMediaStore(); monkeypatch.setattr(service, 'support_media_store', store)
    monkeypatch.setattr(service, 'publish_support_event', AsyncMock())
    await sql("UPDATE support_tickets SET current_assignee_id='7' WHERE id=43")
    calls=[]
    async def send(**kwargs):
        calls.append(kwargs);return {'messages':[{'id':f'ld:90005:FWD{len(calls)}'}]}
    monkeypatch.setattr(service,'whatsapp_client',SimpleNamespace(send_text=send,send_media=send,upload_media=AsyncMock(return_value='asset')))
    agent=SupportAgent(id='7',name='Agent',role='agent')
    originals=[]
    for kind in ['text','document','image']:
        row,_=await repo.add_ticket_message(ticket_id=42,sender_type='CUSTOMER',direction='INBOUND',body='example',message_type=kind)
        originals.append(row)
        if kind!='text':
            store.save(ticket_id=42,message_id=row['id'],content=b'%PDF-1.4' if kind=='document' else b'jpeg',
                       mime_type='application/pdf' if kind=='document' else 'image/jpeg',filename='file.pdf' if kind=='document' else 'photo.jpg')
    ids=[]
    for row in originals:
        result=await support_service.forward_message(source_ticket_id=42,source_message_id=row['id'],destination_ticket_id=43,
            agent=agent,client_message_id='batch-key-'+str(row['id']))
        duplicate=await support_service.forward_message(source_ticket_id=42,source_message_id=row['id'],destination_ticket_id=43,
            agent=agent,client_message_id='batch-key-'+str(row['id']))
        assert result['id']==duplicate['id'] and result['send_status']=='SENT'
        assert result['is_forwarded'] and result['forwarded_from_message_id']==row['id']
        ids.append(result['id'])
    assert len(set(ids))==3 and len(calls)==3
    assert len(await sql("SELECT id FROM support_ticket_events WHERE ticket_id=43 AND event_type='MESSAGE_FORWARDED'"))==3
    with pytest.raises(SupportForbiddenError):
        await support_service.forward_message(source_ticket_id=42,source_message_id=originals[0]['id'],destination_ticket_id=43,
            agent=SupportAgent(id='8',name='Other',role='supervisor'),client_message_id='batch-key-'+str(originals[0]['id']))
    assert len(calls)==3
