// AI Usage & Costs tab (Admins and Super Admins). All amounts are application
// estimates from API usage; the OpenAI invoice is final. Charts are plain inline
// SVG: one series each, so no legend; one bar colour; values in text tokens.

const AI_USAGE_POLL_MS = 60000;
const SVG_NS = 'http://www.w3.org/2000/svg';
const AI_USAGE_REASON_LABELS = {
  BUDGET_EXCEEDED: 'AI is paused: the AI budget limit has been reached. Conversations go to human agents.',
  BUDGET_UNAVAILABLE: 'AI is paused: the AI budget state cannot be read. Conversations go to human agents.'
};
const AI_PURPOSE_LABELS = {turn: 'Customer replies', escalation: 'Stronger model', repair: 'Reply repair', summary: 'History summaries', normalizer: 'Shipping understanding', transcription: 'Voice transcription', vision: 'Images', kb_build: 'Knowledge building (offline)', eval: 'Evaluation (offline)', test: 'Connection tests'};

function fmtUsd(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return '—';
  const number = Number(value);
  const digits = Math.abs(number) > 0 && Math.abs(number) < 1 ? 4 : 2;
  return '$' + number.toLocaleString('en-US', {minimumFractionDigits: digits, maximumFractionDigits: digits});
}

function fmtCount(value) {
  if (value === null || value === undefined) return '—';
  const number = Number(value);
  if (number >= 1e6) return (number / 1e6).toFixed(1).replace(/\.0$/, '') + 'M';
  if (number >= 1e4) return (number / 1e3).toFixed(1).replace(/\.0$/, '') + 'K';
  return number.toLocaleString('en-US');
}

function fmtPercent(value) {
  return value === null || value === undefined ? '—' : Number(value).toFixed(1).replace(/\.0$/, '') + '%';
}

function aiSvg(tag, attrs, text) {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [key, value] of Object.entries(attrs || {})) node.setAttribute(key, String(value));
  if (text !== undefined) node.textContent = text;
  return node;
}

function niceMax(value) {
  if (!(value > 0)) return 1;
  const exponent = Math.pow(10, Math.floor(Math.log10(value)));
  for (const step of [1, 2, 2.5, 5, 10]) {
    if (step * exponent >= value) return step * exponent;
  }
  return 10 * exponent;
}

// Column path: 4px rounded data-end, square at the baseline.
function columnPath(x, y, width, base) {
  const r = Math.min(4, width / 2, Math.max(0, base - y));
  return `M${x},${base}V${y + r}Q${x},${y} ${x + r},${y}H${x + width - r}Q${x + width},${y} ${x + width},${y + r}V${base}Z`;
}

function renderColumnChart(container, rows, {format, label, reference}) {
  container.replaceChildren();
  if (!rows.length || rows.every(row => !row.value)) {
    const empty = document.createElement('div');
    empty.className = 'empty';
    empty.textContent = 'No usage in this period.';
    container.append(empty);
    return;
  }
  const width = 640, height = 180, left = 52, right = 8, top = 16, bottom = 22;
  const plotWidth = width - left - right, plotHeight = height - top - bottom;
  const maxValue = niceMax(Math.max(...rows.map(row => row.value), reference ? reference.value : 0));
  const svg = aiSvg('svg', {viewBox: `0 0 ${width} ${height}`, role: 'img', 'aria-label': container.dataset.label || 'Chart'});
  for (let i = 0; i <= 4; i += 1) {
    const value = maxValue * i / 4;
    const y = top + plotHeight - plotHeight * i / 4;
    svg.append(aiSvg('line', {class: 'grid', x1: left, x2: width - right, y1: y, y2: y}));
    svg.append(aiSvg('text', {class: 'tick', x: left - 6, y: y + 4, 'text-anchor': 'end'}, format(value, true)));
  }
  const band = plotWidth / rows.length;
  const barWidth = Math.max(2, Math.min(24, band - 2));
  const labelEvery = Math.max(1, Math.ceil(rows.length / 8));
  const peak = rows.reduce((best, row, index) => (row.value > rows[best].value ? index : best), 0);
  rows.forEach((row, index) => {
    const x = left + band * index + (band - barWidth) / 2;
    const y = top + plotHeight - (row.value / maxValue) * plotHeight;
    const base = top + plotHeight;
    const group = aiSvg('g', {});
    const tip = label(row) + ': ' + format(row.value);
    // Hit target spans the whole band, larger than the bar itself.
    const hit = aiSvg('rect', {class: 'hit', x: left + band * index, y: top, width: band, height: plotHeight, fill: 'transparent', tabindex: 0});
    hit.append(aiSvg('title', {}, tip));
    group.append(hit);
    if (row.value > 0) group.append(aiSvg('path', {class: 'bar', d: columnPath(x, y, barWidth, base)}));
    if (index === peak && row.value > 0) {
      group.append(aiSvg('text', {class: 'bar-value', x: x + barWidth / 2, y: y - 4, 'text-anchor': 'middle'}, format(row.value)));
    }
    if (index % labelEvery === 0) {
      group.append(aiSvg('text', {class: 'tick', x: x + barWidth / 2, y: height - 6, 'text-anchor': 'middle'}, label(row, true)));
    }
    svg.append(group);
  });
  if (reference && reference.value > 0 && reference.value <= maxValue) {
    const y = top + plotHeight - (reference.value / maxValue) * plotHeight;
    svg.append(aiSvg('line', {class: 'reference', x1: left, x2: width - right, y1: y, y2: y}));
    svg.append(aiSvg('text', {class: 'tick', x: width - right, y: y - 4, 'text-anchor': 'end'}, reference.label));
  }
  container.append(svg);
}

function renderBarList(container, rows) {
  container.replaceChildren();
  const used = rows.filter(row => row.cost_usd > 0);
  if (!used.length) {
    const empty = document.createElement('div');
    empty.className = 'empty';
    empty.textContent = 'No usage in this period.';
    container.append(empty);
    return;
  }
  const rowHeight = 28, left = 150, right = 70, width = 640;
  const height = used.length * rowHeight + 4;
  const maxValue = Math.max(...used.map(row => row.cost_usd));
  const svg = aiSvg('svg', {viewBox: `0 0 ${width} ${height}`, role: 'img', 'aria-label': 'Estimated spend by model'});
  used.forEach((row, index) => {
    const y = index * rowHeight + 4;
    const barLength = Math.max(2, (row.cost_usd / maxValue) * (width - left - right));
    const thickness = 16;
    const group = aiSvg('g', {});
    const hit = aiSvg('rect', {class: 'hit', x: 0, y, width, height: rowHeight, fill: 'transparent', tabindex: 0});
    hit.append(aiSvg('title', {}, `${row.name}: ${fmtUsd(row.cost_usd)} · ${fmtCount(row.requests)} requests`));
    group.append(hit);
    const r = Math.min(4, barLength / 2);
    const top = y + (rowHeight - thickness) / 2 - 2;
    group.append(aiSvg('path', {class: 'bar', d: `M${left},${top}H${left + barLength - r}Q${left + barLength},${top} ${left + barLength},${top + r}V${top + thickness - r}Q${left + barLength},${top + thickness} ${left + barLength - r},${top + thickness}H${left}Z`}));
    group.append(aiSvg('text', {class: 'bar-label', x: left - 8, y: top + thickness - 4, 'text-anchor': 'end'}, row.name));
    group.append(aiSvg('text', {class: 'bar-value', x: left + barLength + 6, y: top + thickness - 4}, fmtUsd(row.cost_usd)));
    svg.append(group);
  });
  container.append(svg);
}

function aiStat(label, value, hint) {
  const card = document.createElement('div');
  card.className = 'ai-stat';
  const name = document.createElement('span');
  name.textContent = label;
  const figure = document.createElement('strong');
  figure.textContent = value;
  card.append(name, figure);
  if (hint) {
    const note = document.createElement('small');
    note.textContent = hint;
    card.append(note);
  }
  return card;
}

function renderAiUsageCards(report) {
  const s = report.summary || {};
  const ticketHint = s.ai_tickets_handled === null ? 'No AI tickets in this period' : null;
  const opened = Number(s.ai_tickets_opened || 0);
  const outcomeHint = opened
    ? 'Of ' + fmtCount(opened) + ' AI ticket' + (opened === 1 ? '' : 's') + ' opened' + (s.ai_tickets_open ? ' (' + fmtCount(s.ai_tickets_open) + ' still open)' : '')
    : 'No AI tickets opened in this period';
  $('aiUsageCards').replaceChildren(
    aiStat("Today's cost", fmtUsd(s.today_cost_usd), 'All numbers and models'),
    aiStat('Monthly cost', fmtUsd(s.month_cost_usd), 'All numbers and models'),
    aiStat('Monthly budget', fmtUsd(s.monthly_budget_usd)),
    aiStat('Budget remaining', fmtUsd(s.budget_remaining_usd)),
    aiStat('AI tickets handled', fmtCount(s.ai_tickets_handled), ticketHint),
    aiStat('Average cost per ticket', fmtUsd(s.average_cost_per_ticket_usd), ticketHint),
    aiStat('API requests', fmtCount(s.api_requests), 'In the selected period'),
    aiStat('Selected period cost', fmtUsd(s.range_cost_usd), 'Includes offline work'),
    aiStat('Offline cost', fmtUsd(s.offline_cost_usd), 'Knowledge building and evaluation'),
    aiStat('Handed to staff', fmtPercent(s.escalation_rate), outcomeHint),
    aiStat('Resolved by AI', fmtPercent(s.resolution_rate), outcomeHint)
  );
}

function renderAiBudgetMeter(report) {
  const container = $('aiBudgetMeter');
  container.replaceChildren();
  const percent = Number(report.summary?.budget_used_percent || 0);
  const thresholds = report.budget?.warning_thresholds || [];
  const track = document.createElement('div');
  track.className = 'ai-meter-track';
  track.setAttribute('role', 'meter');
  track.setAttribute('aria-valuemin', '0');
  track.setAttribute('aria-valuemax', '100');
  track.setAttribute('aria-valuenow', String(Math.min(100, percent)));
  track.setAttribute('aria-label', 'Monthly AI budget used');
  const fill = document.createElement('div');
  const top = thresholds.length ? Math.max(...thresholds) : 95;
  fill.className = 'ai-meter-fill' + (percent >= 100 || percent >= top ? ' danger' : thresholds.some(t => percent >= t) ? ' warning' : '');
  fill.style.width = Math.min(100, percent) + '%';
  track.append(fill);
  for (const threshold of thresholds) {
    const mark = document.createElement('i');
    mark.className = 'ai-meter-mark';
    mark.style.left = threshold + '%';
    mark.title = 'Warning at ' + threshold + '%';
    track.append(mark);
  }
  const copy = document.createElement('div');
  copy.className = 'ai-meter-copy';
  const used = document.createElement('span');
  used.textContent = fmtPercent(percent) + ' of the monthly budget used (estimate)';
  const mode = document.createElement('span');
  mode.textContent = report.budget?.enforcement_enabled ? 'Automatic stop at the limit: ON' : 'Automatic stop at the limit: OFF';
  if (report.budget?.override_until && new Date(report.budget.override_until) > new Date()) {
    mode.textContent = 'Limit override until ' + new Date(report.budget.override_until).toLocaleString();
  }
  copy.append(used, mode);
  container.append(track, copy);
}

function aiUsageTable(title, rows, nameLabel) {
  const wrap = document.createElement('div');
  wrap.className = 'table-wrap';
  const heading = document.createElement('h4');
  heading.textContent = title;
  const table = document.createElement('table');
  table.className = 'ai-usage-table';
  const head = table.createTHead().insertRow();
  for (const label of [nameLabel, 'Requests', 'Input tokens', 'Cached input', 'Output tokens', 'Reasoning', 'Estimated cost']) {
    const th = document.createElement('th');
    th.scope = 'col';
    th.textContent = label;
    head.append(th);
  }
  const body = table.createTBody();
  for (const row of rows) {
    const tr = body.insertRow();
    for (const value of [row.label || row.name, fmtCount(row.requests), fmtCount(row.input_tokens), fmtCount(row.cached_input_tokens), fmtCount(row.output_tokens), fmtCount(row.reasoning_tokens), fmtUsd(row.cost_usd)]) {
      tr.insertCell().textContent = value;
    }
  }
  if (!rows.length) body.insertRow().insertCell().textContent = 'No usage in this period.';
  wrap.append(heading, table);
  return wrap;
}

function syncAiUsageFilterOptions(report) {
  const lineSelect = $('aiUsageLine');
  const currentLine = report.filters?.line || lineSelect.value;
  const numbers = state.aiNumbers || [];
  lineSelect.replaceChildren(new Option('All numbers', ''));
  for (const number of numbers) lineSelect.append(new Option(aiNumberLabel(number), number.phone_number_id));
  lineSelect.value = currentLine || '';
  const modelSelect = $('aiUsageModel');
  const currentModel = report.filters?.model || modelSelect.value;
  const models = new Set([...(state.aiUsageModels || []), ...(report.by_model || []).map(row => row.name).filter(Boolean)]);
  if (currentModel) models.add(currentModel);
  modelSelect.replaceChildren(new Option('All models', ''));
  for (const model of [...models].sort()) modelSelect.append(new Option(model, model));
  modelSelect.value = currentModel || '';
  state.aiUsageModels = [...models];
}

function renderAiUsage(report) {
  state.aiUsage = report;
  const banner = $('aiUsageBanner');
  const reason = report.status?.within_budget === false ? report.status.reason : null;
  banner.classList.toggle('hidden', !reason);
  text(banner, reason ? (AI_USAGE_REASON_LABELS[reason] || reason) : '');
  $('aiUsageFrom').value = report.filters?.from || '';
  $('aiUsageTo').value = report.filters?.to || '';
  syncAiUsageFilterOptions(report);
  renderAiUsageCards(report);
  renderAiBudgetMeter(report);
  text($('aiUsageNote'), report.note || '');
  const daily = (report.daily || []).map(row => ({day: row.day, value: Number(row.cost_usd), requests: Number(row.requests)}));
  const dayLabel = (row, short) => (short ? row.day.slice(5) : row.day);
  const dailyLimit = report.budget?.daily_limit_usd;
  $('aiChartSpend').dataset.label = 'Estimated spend per day';
  renderColumnChart($('aiChartSpend'), daily, {
    format: (value, axis) => (axis && value >= 1 ? '$' + Math.round(value) : fmtUsd(value)),
    label: dayLabel,
    reference: dailyLimit ? {value: Number(dailyLimit), label: 'Daily limit ' + fmtUsd(dailyLimit)} : null
  });
  $('aiChartRequests').dataset.label = 'OpenAI requests per day';
  renderColumnChart($('aiChartRequests'), daily.map(row => ({day: row.day, value: row.requests})), {
    format: value => fmtCount(Math.round(value)), label: dayLabel, reference: null
  });
  renderBarList($('aiChartModels'), report.by_model || []);
  $('aiUsageTablesBody').replaceChildren(
    aiUsageTable('By model', report.by_model || [], 'Model'),
    aiUsageTable('By purpose', (report.by_purpose || []).map(row => ({...row, label: AI_PURPOSE_LABELS[row.name] || row.name})), 'Purpose'),
    aiUsageTable('By model tier', report.by_tier || [], 'Tier')
  );
  renderAiBudgetForm(report.budget || {});
}

function renderAiBudgetForm(budget) {
  if (state.aiBudgetDirty) return;
  state.aiBudget = budget;
  $('aiBudgetMonthly').value = budget.monthly_limit_usd ?? '';
  $('aiBudgetDaily').value = budget.daily_limit_usd ?? '';
  $('aiBudgetThresholds').value = (budget.warning_thresholds || []).join(', ');
  $('aiBudgetEnforce').checked = Boolean(budget.enforcement_enabled);
  $('aiBudgetOverride').value = budget.override_until ? new Date(budget.override_until).toISOString().slice(0, 16) : '';
}

function aiUsageQuery() {
  const params = new URLSearchParams();
  for (const [key, id] of [['from', 'aiUsageFrom'], ['to', 'aiUsageTo'], ['line', 'aiUsageLine'], ['model', 'aiUsageModel']]) {
    if ($(id).value) params.set(key, $(id).value);
  }
  return params.toString();
}

async function loadAiUsage() {
  if (!canManageAiSettings()) return;
  const query = aiUsageQuery();
  const request = state.aiUsageRequest = (state.aiUsageRequest || 0) + 1;
  try {
    const report = await api('/api/support/ai-usage' + (query ? '?' + query : ''));
    if (request !== state.aiUsageRequest || query !== aiUsageQuery() || !canManageAiSettings()) return;
    showAiSettingsError('');
    renderAiUsage(report);
  } catch (error) {
    if (request === state.aiUsageRequest && error.name !== 'AbortError') showAiSettingsError(error.message);
  }
  if (request === state.aiUsageRequest) await loadAiPrices();
}

function startAiUsagePolling() {
  stopAiUsagePolling();
  loadAiUsage();
  state.aiUsageTimer = setInterval(() => {
    if (document.visibilityState === 'visible') loadAiUsage();
  }, AI_USAGE_POLL_MS);
}

function stopAiUsagePolling() {
  if (state.aiUsageTimer) clearInterval(state.aiUsageTimer);
  state.aiUsageTimer = null;
  state.aiUsageRequest = (state.aiUsageRequest || 0) + 1;
  state.aiPricesRequest = (state.aiPricesRequest || 0) + 1;
}

async function saveAiBudget(event) {
  event.preventDefault();
  if (!canManageAiSettings() || state.aiBudgetBusy || !state.aiBudget) return;
  const thresholds = $('aiBudgetThresholds').value.split(',').map(part => Number(part.trim()));
  const override = $('aiBudgetOverride').value;
  const payload = {
    monthly_limit_usd: $('aiBudgetMonthly').value,
    daily_limit_usd: $('aiBudgetDaily').value || null,
    warning_thresholds: thresholds,
    enforcement_enabled: $('aiBudgetEnforce').checked,
    override_until: override ? new Date(override + 'Z').toISOString() : null,
    version: Number(state.aiBudget.version)
  };
  state.aiBudgetBusy = true;
  setButtonBusy($('aiBudgetSaveBtn'), true, 'Saving…');
  try {
    await api('/api/support/ai-budget', {method: 'PUT', body: JSON.stringify(payload)});
    state.aiBudgetDirty = false;
    notify('AI budget saved.');
    await loadAiUsage();
    try { await loadAiSettings(); } catch (_) {}
  } catch (error) {
    showAiSettingsError(error.message);
    if (error.status === 409) $('aiBudgetReloadBtn').classList.remove('hidden');
  } finally {
    state.aiBudgetBusy = false;
    setButtonBusy($('aiBudgetSaveBtn'), false);
  }
}

function renderAiPrices(payload) {
  const container = $('aiPricesTable');
  container.replaceChildren();
  if (payload.blocked_models?.length) {
    const warning = document.createElement('p');
    warning.className = 'ai-usage-banner';
    warning.textContent = 'Models awaiting verified prices: ' + payload.blocked_models.join(', ');
    container.append(warning);
  }
  const wrap = document.createElement('div');
  wrap.className = 'table-wrap';
  const table = document.createElement('table');
  table.className = 'ai-usage-table';
  const head = table.createTHead().insertRow();
  for (const label of ['Model / revision', 'Input', 'Cached', 'Output', 'Audio/min', 'Batch factor', 'Status', '']) {
    const th = document.createElement('th');
    th.scope = 'col';
    th.textContent = label;
    head.append(th);
  }
  const body = table.createTBody();
  const blocked = new Set(payload.blocked_models || []);
  for (const price of payload.prices || []) {
    if (price.retired) continue;
    const tr = body.insertRow();
    const modelCell = tr.insertCell();
    modelCell.textContent = price.model_prefix + ' · #' + price.id + (blocked.has(price.model_prefix) ? ' (blocked)' : '');
    modelCell.title = 'Effective: ' + (price.effective_from || '—') + '\nSource: ' + (price.source || '—');
    for (const value of [price.input_per_million, price.cached_input_per_million, price.output_per_million, price.audio_per_minute]) {
      tr.insertCell().textContent = value === null ? '—' : '$' + value;
    }
    tr.insertCell().textContent = price.batch_factor;
    tr.insertCell().textContent = price.verified
      ? 'Verified' + (price.verified_by ? ' by ' + price.verified_by : '')
      : 'Unverified revision — not used';
    const actions = tr.insertCell();
    const revise = document.createElement('button');
    revise.type = 'button';
    revise.className = 'btn';
    revise.textContent = 'Revise';
    revise.addEventListener('click', () => {
      for (const [id, key] of [['aiPriceModel', 'model_prefix'], ['aiPriceInput', 'input_per_million'], ['aiPriceCached', 'cached_input_per_million'], ['aiPriceOutput', 'output_per_million'], ['aiPriceAudio', 'audio_per_minute'], ['aiPriceBatch', 'batch_factor'], ['aiPriceSource', 'source']]) $(id).value = price[key] ?? '';
      $('aiPriceInput').focus();
    });
    actions.append(revise);
    if (!price.verified) {
      const verify = document.createElement('button');
      verify.type = 'button';
      verify.className = 'btn';
      verify.textContent = 'Verify';
      verify.addEventListener('click', () => changeAiPrice(price, 'verify'));
      actions.append(verify);
    }
    const retire = document.createElement('button');
    retire.type = 'button';
    retire.className = 'btn';
    retire.textContent = 'Retire';
    retire.addEventListener('click', () => changeAiPrice(price, 'retire'));
    actions.append(retire);
  }
  wrap.append(table);
  container.append(wrap);
}

async function loadAiPrices() {
  if (!canManageAiSettings()) return;
  const request = state.aiPricesRequest = (state.aiPricesRequest || 0) + 1;
  try {
    const prices = await api('/api/support/ai-prices');
    if (request === state.aiPricesRequest && canManageAiSettings()) renderAiPrices(prices);
  } catch (error) {
    if (request === state.aiPricesRequest && error.name !== 'AbortError') showAiSettingsError(error.message);
  }
}

async function changeAiPrice(price, action) {
  if (!canManageAiSettings()) return;
  const verifying = action === 'verify';
  const confirmed = await confirmDialog({
    title: verifying ? 'Verify ' + price.model_prefix + ' price?' : 'Retire ' + price.model_prefix + ' price?',
    message: verifying
      ? 'Confirm these prices match OpenAI’s pricing page today: input $' + price.input_per_million + ', cached $' + price.cached_input_per_million + ', output $' + price.output_per_million + ' per 1M tokens; audio/min ' + (price.audio_per_minute ?? 'not configured') + '; batch factor ' + price.batch_factor + '. AI may then call this model.'
      : 'AI will stop calling this model unless another verified price covers it.',
    confirmLabel: verifying ? 'Verify price' : 'Retire price',
    tone: verifying ? 'success' : 'danger'
  });
  if (!confirmed) return;
  try {
    renderAiPrices(await api('/api/support/ai-prices/' + encodeURIComponent(price.id) + '/' + action, {method: 'POST'}));
    notify(verifying ? 'Price verified.' : 'Price retired.');
  } catch (error) {
    showAiSettingsError(error.message);
  }
}

async function addAiPrice(event) {
  event.preventDefault();
  if (!canManageAiSettings() || state.aiPriceBusy) return;
  const payload = {
    model_prefix: $('aiPriceModel').value.trim(),
    input_per_million: $('aiPriceInput').value,
    cached_input_per_million: $('aiPriceCached').value,
    output_per_million: $('aiPriceOutput').value,
    audio_per_minute: $('aiPriceAudio').value || null,
    batch_factor: $('aiPriceBatch').value,
    source: $('aiPriceSource').value.trim() || null
  };
  state.aiPriceBusy = true;
  setButtonBusy($('aiPriceAddBtn'), true, 'Adding…');
  try {
    renderAiPrices(await api('/api/support/ai-prices', {method: 'POST', body: JSON.stringify(payload)}));
    $('aiPriceForm').reset();
    notify('Price added. Verify it to let AI use the model.');
  } catch (error) {
    showAiSettingsError(error.message);
  } finally {
    state.aiPriceBusy = false;
    setButtonBusy($('aiPriceAddBtn'), false);
  }
}

$('aiUsageFilters').addEventListener('submit', event => { event.preventDefault(); loadAiUsage(); });
$('aiBudgetForm').addEventListener('submit', saveAiBudget);
$('aiBudgetForm').addEventListener('input', () => { state.aiBudgetDirty = true; });
$('aiPriceForm').addEventListener('submit', addAiPrice);
$('aiBudgetReloadBtn').addEventListener('click', () => {
  state.aiBudgetDirty = false;
  $('aiBudgetReloadBtn').classList.add('hidden');
  loadAiUsage();
});
