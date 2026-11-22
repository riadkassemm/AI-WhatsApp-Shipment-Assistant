"""AI knowledge base (Step 4c) lifecycle against real MariaDB."""
import pytest

from app.ai_knowledge import (
    AIKnowledgeConflictError,
    AIKnowledgeForbiddenError,
    AIKnowledgeRepository,
    AIKnowledgeService,
    AIKnowledgeValidationError,
)
from app.support_auth import SupportAgent

ADMIN = SupportAgent(id='8', name='Other', role='supervisor')
AGENT = SupportAgent(id='7', name='Agent', role='agent')
ARTICLE = {
    'intent_key': 'lost_parcel', 'title': 'Lost parcel',
    'approved_policy': 'Apologise, ask for the tracking number and open a claim with staff.',
    'data_needed': 'Tracking number', 'escalate_when': 'Always after collecting the tracking number.',
    'examples': [{'style': 'leb_arabizi', 'customer': 'el parcel da3', 'reply': 'Sorry! Ba3etle el tracking number?'}],
    'observed_practice': 'Agents sometimes promised refunds.',
}


@pytest.fixture
def knowledge(infrastructure):
    repo, sql, _ = infrastructure
    storage = AIKnowledgeRepository()
    storage._pool = repo._pool
    return AIKnowledgeService(storage), sql


@pytest.mark.asyncio
async def test_draft_review_and_approval(knowledge):
    service, sql = knowledge
    with pytest.raises(AIKnowledgeForbiddenError):
        await service.overview(AGENT)
    draft = await service.create(ADMIN, ARTICLE)
    assert (draft['status'], draft['version'], draft['source']) == ('DRAFT', 1, 'STAFF')
    with pytest.raises(AIKnowledgeConflictError, match='already has a draft'):
        await service.create(ADMIN, ARTICLE)
    # Drafts are invisible to the AI.
    assert await service.prompt_text() == ''

    edited = await service.update(ADMIN, draft['id'], {**ARTICLE, 'title': 'Lost or missing parcel',
                                                       'row_version': draft['row_version']})
    with pytest.raises(AIKnowledgeConflictError, match='changed this article'):
        await service.update(ADMIN, draft['id'], {**ARTICLE, 'row_version': draft['row_version']})
    approved = await service.approve(ADMIN, draft['id'], edited['row_version'])
    assert approved['status'] == 'APPROVED' and approved['approved_by_staff_name'] == 'Other'
    prompt = await service.prompt_text()
    assert 'Lost or missing parcel [lost_parcel]' in prompt and 'Tracking number' in prompt
    assert 'promised refunds' not in prompt  # observed practice never reaches the AI
    assert [entry['action'] for entry in approved['audit']] == ['DRAFT_CREATED', 'DRAFT_EDITED', 'APPROVED']

    # Approved articles are immutable; a revision is a new version that replaces it on approval.
    with pytest.raises(AIKnowledgeConflictError, match='Only drafts'):
        await service.update(ADMIN, draft['id'], {**ARTICLE, 'row_version': approved['row_version']})
    revision = await service.revise(ADMIN, draft['id'])
    assert (revision['status'], revision['version']) == ('DRAFT', 2)
    revised = await service.update(ADMIN, revision['id'], {**ARTICLE, 'approved_policy': 'New policy text.',
                                                           'row_version': revision['row_version']})
    await service.approve(ADMIN, revision['id'], revised['row_version'])
    statuses = {row['version']: row['status'] for row in (await service.overview(ADMIN))['articles']}
    assert statuses == {1: 'RETIRED', 2: 'APPROVED'}
    assert 'New policy text.' in await service.prompt_text()

    overview = await service.overview(ADMIN)
    assert overview['approved_topics'] == 1 and overview['over_budget'] is False


@pytest.mark.asyncio
async def test_tool_drafts_with_placeholders_cannot_be_approved(knowledge):
    service, sql = knowledge
    article_id = await service.create_tool_draft(
        {**ARTICLE, 'intent_key': 'refund', 'title': 'Refunds',
         'approved_policy': 'Refunds above <AMOUNT_1> need a manager.'},
        source_ticket_count=42, source_ticket_ids=[1, 2, 3],
    )
    draft = await service.article(ADMIN, article_id)
    assert draft['source'] == 'AI_DRAFT' and draft['source_ticket_count'] == 42 and draft['status'] == 'DRAFT'
    with pytest.raises(AIKnowledgeValidationError, match='placeholders'):
        await service.approve(ADMIN, article_id, draft['row_version'])
    retired = await service.retire(ADMIN, article_id, draft['row_version'])
    assert retired['status'] == 'RETIRED'


@pytest.mark.asyncio
async def test_invalid_articles_are_rejected(knowledge):
    service, _ = knowledge
    for change, message in (({'intent_key': 'Bad Key'}, 'Topic keys'),
                            ({'approved_policy': ''}, 'required'),
                            ({'examples': [{'style': 'klingon', 'customer': 'a', 'reply': 'b'}]}, 'style')):
        with pytest.raises(AIKnowledgeValidationError, match=message):
            await service.create(ADMIN, {**ARTICLE, **change})
