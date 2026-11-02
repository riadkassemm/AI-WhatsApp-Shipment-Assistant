"""Authenticated creation-date sorting across the full filtered ticket queue."""
from datetime import datetime

import httpx
import pytest
from fastapi import FastAPI

from app.config import settings
from app.support_api import router
from tests.integration.test_agent_statistics import sessions


@pytest.mark.asyncio
async def test_unassigned_creation_sort_filters_pages_and_other_queue_order(infrastructure):
    _, sql, _ = infrastructure
    await sessions(sql)
    # ID, creation time and latest activity deliberately have different orders.
    # A timestamp tie straddles each page boundary to exercise the ID tiebreaker.
    for ticket_id, created_day, activity_day, line in [
        (51, 3, 1, '90005'), (52, 1, 6, '90005'),
        (53, 2, 4, '90006'), (54, 1, 5, '90005'),
        (55, 4, 2, '90005'), (56, 2, 3, '90005'),
    ]:
        await sql("""
            INSERT INTO support_tickets (
                id,ticket_reference,active_key,whatsapp_phone,customer_name,reason,ai_summary,
                status,conversation_origin,business_phone_number_id,created_at,updated_at,last_activity_at
            ) VALUES (%s,%s,%s,%s,'Sorted customer','test','test','NEW','CUSTOMER',%s,%s,%s,%s)
        """, (
            ticket_id, f'SORT-{ticket_id}', f'{line}:{ticket_id}', f'1555000{ticket_id}', line,
            datetime(2026, 1, created_day), datetime(2026, 1, activity_day), datetime(2026, 1, activity_day),
        ))
    app = FastAPI()
    app.include_router(router)
    transport = httpx.MockTransport(httpx.ASGITransport(app=app).handle_async_request)
    async with httpx.AsyncClient(transport=transport, base_url='http://test') as client:
        client.cookies.set(settings.support_cookie_name, 'token-1')

        async def ordered_ids(**params):
            response = await client.get('/api/support/tickets', params={
                'view': 'unassigned', 'q': 'Sorted customer', **params,
            })
            assert response.status_code == 200, response.text
            data = response.json()
            return [row['id'] for row in data['items']], data['total']

        for sort, expected in [
            ('oldest', [52, 54, 53, 56, 51, 55]),
            ('newest', [55, 51, 56, 53, 54, 52]),
        ]:
            actual = []
            for page in range(1, 3):
                items, total = await ordered_ids(sort=sort, page=page, page_size=3)
                assert total == 6
                actual.extend(items)
            assert actual == expected
            items, total = await ordered_ids(sort=sort, support_number='90005')
            assert total == 5
            assert items == [ticket_id for ticket_id in expected if ticket_id != 53]

        # New activity must not move a ticket in either creation-date ordering.
        await sql("UPDATE support_tickets SET last_activity_at='2026-02-01' WHERE id=51")
        assert await ordered_ids(sort='oldest') == ([52, 54, 53, 56, 51, 55], 6)
        assert await ordered_ids(sort='newest') == ([55, 51, 56, 53, 54, 52], 6)
        assert await ordered_ids() == ([51, 52, 54, 53, 56, 55], 6)

        # Sorting cannot bypass queue membership; other queues keep activity order.
        await sql("UPDATE support_tickets SET status='ASSIGNED',current_assignee_id='7' WHERE id>=51")
        assert await ordered_ids(sort='oldest') == ([], 0)
        for sort in ('oldest', 'newest'):
            assert await ordered_ids(view='mine', sort=sort) == ([51, 52, 54, 53, 56, 55], 6)
        await sql("UPDATE support_tickets SET current_assignee_id='8' WHERE id=52")
        assert await ordered_ids(view='mine', sort='oldest') == ([51, 54, 53, 56, 55], 5)

        response = await client.get('/api/support/tickets', params={'sort': 'invalid'})
        assert response.status_code == 422
