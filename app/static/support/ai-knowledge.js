// AI knowledge base review (Admins and Super Admins). The AI uses APPROVED
// articles only; approving a new version retires the previous one.
const AI_KNOWLEDGE_STATUS_LABELS = {DRAFT: 'Draft', APPROVED: 'Approved', RETIRED: 'Retired'};
let aiKnowledgeArticle = null;
let aiKnowledgeRequest = 0;

async function loadAiKnowledge() {
  if (!canManageAiSettings()) return;
  const request = ++aiKnowledgeRequest;
  const status = $('aiKnowledgeStatus').value;
  try {
    const data = await api('/api/support/ai-knowledge' + (status ? '?status=' + encodeURIComponent(status) : ''));
    if (request !== aiKnowledgeRequest) return;
    renderAiKnowledge(data);
  } catch (error) {
    if (request === aiKnowledgeRequest) text($('aiKnowledgeSummary'), error.message);
  }
}

function renderAiKnowledge(data) {
  const tokens = Number(data.prompt_tokens_estimate || 0);
  text($('aiKnowledgeSummary'), (data.approved_topics || 0) + ' approved topic' + (data.approved_topics === 1 ? '' : 's') +
    ' · about ' + tokens.toLocaleString('en-US') + ' of ' + Number(data.prompt_token_budget || 0).toLocaleString('en-US') +
    ' tokens' + (data.over_budget ? ' — over the budget, shorten or retire articles' : ''));
  const list = $('aiKnowledgeList');
  list.replaceChildren();
  const articles = data.articles || [];
  if (!articles.length) {
    const empty = document.createElement('div');
    empty.className = 'muted';
    empty.textContent = 'No articles yet. Create a topic, or run the drafting tool to add drafts for review.';
    list.append(empty);
    return;
  }
  for (const article of articles) {
    const item = document.createElement('button');
    item.type = 'button';
    item.className = 'ai-knowledge-item';
    const copy = document.createElement('span');
    const title = document.createElement('strong');
    title.textContent = article.title;
    const meta = document.createElement('small');
    meta.textContent = article.intent_key + ' · version ' + article.version + (article.source === 'AI_DRAFT' ? ' · drafted from tickets' : '');
    copy.append(title, meta);
    const badge = document.createElement('span');
    badge.className = 'ai-status-pill' + (article.status === 'APPROVED' ? '' : ' off');
    badge.textContent = AI_KNOWLEDGE_STATUS_LABELS[article.status] || article.status;
    item.append(copy, badge);
    item.onclick = () => openAiKnowledgeArticle(article.id);
    list.append(item);
  }
}

function aiKnowledgeExamplesText(examples) {
  return (examples || []).map(example => [example.style, example.customer, example.reply].join(' | ')).join('\n');
}

function parseAiKnowledgeExamples(value) {
  return String(value || '').split('\n').map(line => line.trim()).filter(Boolean).map(line => {
    const parts = line.split('|').map(part => part.trim());
    if (parts.length < 3) throw new Error('Each example line needs: style | customer message | reply');
    return {style: parts[0], customer: parts[1], reply: parts.slice(2).join(' | ')};
  });
}

function fillAiKnowledgeForm(article) {
  aiKnowledgeArticle = article;
  const draft = !article || article.status === 'DRAFT';
  $('aiKnowledgeForm').classList.remove('hidden');
  text($('aiKnowledgeFormTitle'), article ? article.title : 'New topic');
  const meta = [];
  if (article) {
    meta.push((AI_KNOWLEDGE_STATUS_LABELS[article.status] || article.status) + ' · version ' + article.version);
    if (article.source === 'AI_DRAFT') meta.push('drafted from ' + (article.source_ticket_count || 0) + ' de-identified tickets');
    if (article.approved_at) meta.push('approved ' + fmt(article.approved_at) + (article.approved_by_staff_name ? ' by ' + article.approved_by_staff_name : ''));
  }
  text($('aiKnowledgeMeta'), meta.join(' · '));
  $('aiKnowledgeIntent').value = article?.intent_key || '';
  $('aiKnowledgeIntent').disabled = Boolean(article);
  $('aiKnowledgeTitle').value = article?.title || '';
  $('aiKnowledgePolicy').value = article?.approved_policy || '';
  $('aiKnowledgeData').value = article?.data_needed || '';
  $('aiKnowledgeEscalate').value = article?.escalate_when || '';
  $('aiKnowledgeExamples').value = aiKnowledgeExamplesText(article?.examples);
  $('aiKnowledgeObserved').value = article?.observed_practice || '';
  for (const id of ['aiKnowledgeTitle', 'aiKnowledgePolicy', 'aiKnowledgeData', 'aiKnowledgeEscalate', 'aiKnowledgeExamples', 'aiKnowledgeObserved']) {
    $(id).readOnly = !draft;
  }
  $('aiKnowledgeSaveBtn').classList.toggle('hidden', !draft);
  $('aiKnowledgeApproveBtn').classList.toggle('hidden', !article || article.status !== 'DRAFT');
  $('aiKnowledgeReviseBtn').classList.toggle('hidden', !article || article.status === 'DRAFT');
  $('aiKnowledgeRetireBtn').classList.toggle('hidden', !article || article.status === 'RETIRED');
}

async function openAiKnowledgeArticle(id) {
  try {
    fillAiKnowledgeForm(await api('/api/support/ai-knowledge/' + id));
  } catch (error) {
    notify(error.message, 'error');
  }
}

function aiKnowledgePayload() {
  return {
    intent_key: $('aiKnowledgeIntent').value.trim(),
    title: $('aiKnowledgeTitle').value.trim(),
    approved_policy: $('aiKnowledgePolicy').value.trim(),
    data_needed: $('aiKnowledgeData').value.trim() || null,
    escalate_when: $('aiKnowledgeEscalate').value.trim() || null,
    examples: parseAiKnowledgeExamples($('aiKnowledgeExamples').value),
    observed_practice: $('aiKnowledgeObserved').value.trim() || null,
    row_version: aiKnowledgeArticle?.row_version ?? null
  };
}

async function aiKnowledgeAction(action) {
  try {
    let article;
    if (action === 'save') {
      const payload = aiKnowledgePayload();
      article = aiKnowledgeArticle
        ? await api('/api/support/ai-knowledge/' + aiKnowledgeArticle.id, {method: 'PUT', body: JSON.stringify(payload)})
        : await api('/api/support/ai-knowledge', {method: 'POST', body: JSON.stringify(payload)});
      notify('Draft saved.');
    } else if (action === 'revise') {
      article = await api('/api/support/ai-knowledge/' + aiKnowledgeArticle.id + '/revise', {method: 'POST'});
      notify('New draft version opened.');
    } else {
      const ok = await confirmDialog(action === 'approve'
        ? {title: 'Approve this answer?', message: 'The AI will use it for this topic. Any earlier approved version is retired.', confirmLabel: 'Approve', tone: 'success'}
        : {title: 'Retire this article?', message: 'The AI stops using it.', confirmLabel: 'Retire', tone: 'danger'});
      if (!ok) return;
      article = await api('/api/support/ai-knowledge/' + aiKnowledgeArticle.id + '/' + action, {
        method: 'POST', body: JSON.stringify({row_version: aiKnowledgeArticle.row_version})
      });
      notify(action === 'approve' ? 'Answer approved.' : 'Article retired.');
    }
    fillAiKnowledgeForm(article);
    await loadAiKnowledge();
  } catch (error) {
    notify(error.message, 'error');
  }
}

$('aiKnowledgeStatus').addEventListener('change', () => loadAiKnowledge());
$('aiKnowledgeNewBtn').addEventListener('click', () => { fillAiKnowledgeForm(null); $('aiKnowledgeIntent').focus(); });
$('aiKnowledgeCancelBtn').addEventListener('click', () => { aiKnowledgeArticle = null; $('aiKnowledgeForm').classList.add('hidden'); });
$('aiKnowledgeForm').addEventListener('submit', event => { event.preventDefault(); aiKnowledgeAction('save'); });
$('aiKnowledgeApproveBtn').addEventListener('click', () => aiKnowledgeAction('approve'));
$('aiKnowledgeReviseBtn').addEventListener('click', () => aiKnowledgeAction('revise'));
$('aiKnowledgeRetireBtn').addEventListener('click', () => aiKnowledgeAction('retire'));
