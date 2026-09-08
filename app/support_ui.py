from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from app.config import settings


router = APIRouter(tags=["support-ui"])


@router.get("/support", response_class=HTMLResponse)
async def support_dashboard() -> HTMLResponse:
    if not settings.support_ui_enabled:
        return HTMLResponse("Support UI is disabled.", status_code=404)
    html = _HTML.replace(
        "__POLL_MS__", str(max(1, settings.support_queue_poll_seconds) * 1000)
    ).replace("__CSRF_COOKIE__", settings.support_csrf_cookie_name)
    return HTMLResponse(html)


_HTML = r'''<!doctype html>
<html lang="en" data-theme="light">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover" />
<meta name="color-scheme" content="light dark" />
<meta name="theme-color" content="#f4f7fb" />
<title>Shipment Support</title>
<script>
(function () {
  try {
    const saved = localStorage.getItem('shipment-support-theme');
    const preferred = window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
    document.documentElement.dataset.theme = saved === 'dark' || saved === 'light' ? saved : preferred;
  } catch (_) {
    document.documentElement.dataset.theme = 'light';
  }
}());
</script>
<style>
:root {
  color-scheme: light;
  font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  --font-mono: "SFMono-Regular", Consolas, "Liberation Mono", monospace;
  --bg-app: #f3f6fb;
  --bg-subtle: #f7f9fc;
  --surface: #ffffff;
  --surface-raised: #ffffff;
  --surface-muted: #f0f4f9;
  --surface-hover: #edf4ff;
  --surface-selected: #e8f1ff;
  --surface-input: #ffffff;
  --text: #172033;
  --text-strong: #0d172a;
  --text-muted: #52637a;
  --text-subtle: #617187;
  --border: #dbe3ee;
  --border-strong: #c7d2e1;
  --primary: #1666d9;
  --primary-hover: #0f58c4;
  --primary-soft: #e7f0ff;
  --primary-contrast: #ffffff;
  --success: #16794a;
  --success-soft: #e7f7ef;
  --warning: #9a5b06;
  --warning-soft: #fff5df;
  --danger: #b42336;
  --danger-hover: #941b2b;
  --danger-soft: #ffebee;
  --info: #2563a9;
  --info-soft: #e9f3ff;
  --focus: #2f7ff2;
  --focus-ring: rgba(47, 127, 242, 0.28);
  --overlay: rgba(15, 23, 42, 0.56);
  --shadow-xs: 0 1px 2px rgba(15, 23, 42, 0.05);
  --shadow-sm: 0 8px 24px rgba(15, 23, 42, 0.08);
  --shadow-lg: 0 24px 70px rgba(15, 23, 42, 0.24);
  --radius-sm: 8px;
  --radius-md: 12px;
  --radius-lg: 16px;
  --radius-xl: 20px;
  --sidebar-width: 236px;
  --queue-width: 370px;
  --context-width: 330px;
  --header-height: 76px;
  --transition: 150ms ease;
}

:root[data-theme="dark"] {
  color-scheme: dark;
  --bg-app: #0b1220;
  --bg-subtle: #101927;
  --surface: #111b2b;
  --surface-raised: #152135;
  --surface-muted: #19263a;
  --surface-hover: #1a2d48;
  --surface-selected: #17365f;
  --surface-input: #0f1928;
  --text: #dce6f4;
  --text-strong: #f7f9fc;
  --text-muted: #9aacbf;
  --text-subtle: #74869c;
  --border: #26364b;
  --border-strong: #344861;
  --primary: #63a4ff;
  --primary-hover: #84b7ff;
  --primary-soft: #17365f;
  --primary-contrast: #07111f;
  --success: #5bd49a;
  --success-soft: #133928;
  --warning: #f0b95a;
  --warning-soft: #3a2b13;
  --danger: #ff7c8c;
  --danger-hover: #ff9aa7;
  --danger-soft: #431d27;
  --info: #77b4ff;
  --info-soft: #17334f;
  --focus: #7cb5ff;
  --focus-ring: rgba(124, 181, 255, 0.3);
  --overlay: rgba(2, 6, 23, 0.76);
  --shadow-xs: 0 1px 2px rgba(0, 0, 0, 0.26);
  --shadow-sm: 0 12px 30px rgba(0, 0, 0, 0.28);
  --shadow-lg: 0 28px 80px rgba(0, 0, 0, 0.48);
}

*, *::before, *::after { box-sizing: border-box; }
html, body { width: 100%; min-height: 100%; }
html { background: var(--bg-app); }
body {
  margin: 0;
  background: var(--bg-app);
  color: var(--text);
  font-size: 14px;
  line-height: 1.45;
  text-rendering: optimizeLegibility;
  -webkit-font-smoothing: antialiased;
}
body.modal-open { overflow: hidden; }
button, input, select, textarea { font: inherit; }
button, a, input, select, textarea { -webkit-tap-highlight-color: transparent; }
button { color: inherit; }
a { color: var(--primary); }
::selection { background: var(--primary-soft); color: var(--text-strong); }
.hidden { display: none !important; }
.sr-only {
  position: absolute !important;
  width: 1px !important;
  height: 1px !important;
  padding: 0 !important;
  margin: -1px !important;
  overflow: hidden !important;
  clip: rect(0, 0, 0, 0) !important;
  white-space: nowrap !important;
  border: 0 !important;
}
.muted { color: var(--text-muted); }
.subtle { color: var(--text-subtle); }
.icon { width: 18px; height: 18px; display: block; flex: 0 0 auto; fill: none; stroke: currentColor; stroke-width: 1.8; stroke-linecap: round; stroke-linejoin: round; }
.icon-sm { width: 15px; height: 15px; }
.icon-lg { width: 22px; height: 22px; }

::-webkit-scrollbar { width: 10px; height: 10px; }
::-webkit-scrollbar-track { background: transparent; }
::-webkit-scrollbar-thumb { background: color-mix(in srgb, var(--text-subtle) 38%, transparent); border: 3px solid transparent; border-radius: 999px; background-clip: padding-box; }
::-webkit-scrollbar-thumb:hover { background: color-mix(in srgb, var(--text-muted) 55%, transparent); border: 3px solid transparent; background-clip: padding-box; }

:where(button, input, select, textarea, summary, [tabindex]):focus-visible {
  outline: 3px solid var(--focus-ring);
  outline-offset: 2px;
  border-color: var(--focus);
}

.btn, .icon-btn, .queue-nav-btn, .ticket-row, .menu-summary {
  border: 0;
  cursor: pointer;
  transition: background-color var(--transition), border-color var(--transition), color var(--transition), box-shadow var(--transition), transform var(--transition), opacity var(--transition);
}
.btn {
  min-height: 38px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  padding: 8px 13px;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--surface);
  color: var(--text);
  font-weight: 650;
  font-size: 13px;
  white-space: nowrap;
  box-shadow: var(--shadow-xs);
}
.btn:hover:not(:disabled) { background: var(--surface-hover); border-color: var(--border-strong); }
.btn:active:not(:disabled) { transform: translateY(1px); }
.btn.primary { background: var(--primary); border-color: var(--primary); color: var(--primary-contrast); box-shadow: none; }
.btn.primary:hover:not(:disabled) { background: var(--primary-hover); border-color: var(--primary-hover); }
.btn.danger { background: transparent; color: var(--danger); border-color: color-mix(in srgb, var(--danger) 35%, var(--border)); }
.btn.danger:hover:not(:disabled) { background: var(--danger-soft); border-color: color-mix(in srgb, var(--danger) 55%, var(--border)); }
.btn.ghost { background: transparent; border-color: transparent; box-shadow: none; }
.btn.ghost:hover:not(:disabled) { background: var(--surface-muted); border-color: transparent; }
.btn.small { min-height: 32px; padding: 5px 9px; font-size: 12px; }
.btn:disabled, .icon-btn:disabled { opacity: 0.48; cursor: not-allowed; transform: none; }
.btn[aria-busy="true"] .icon { animation: spin 0.8s linear infinite; }
.icon-btn {
  position: relative;
  width: 38px;
  height: 38px;
  display: inline-grid;
  place-items: center;
  flex: 0 0 auto;
  padding: 0;
  border: 1px solid transparent;
  border-radius: var(--radius-sm);
  background: transparent;
  color: var(--text-muted);
}
.icon-btn:hover:not(:disabled) { color: var(--text-strong); background: var(--surface-muted); }
.icon-btn.primary { color: var(--primary-contrast); background: var(--primary); }
.icon-btn.danger { color: var(--danger); }
[data-tooltip]::after {
  content: attr(data-tooltip);
  position: absolute;
  z-index: 80;
  left: 50%;
  top: calc(100% + 8px);
  transform: translate(-50%, -3px);
  padding: 6px 8px;
  border-radius: 7px;
  background: var(--text-strong);
  color: var(--surface);
  font-size: 11px;
  font-weight: 600;
  line-height: 1.2;
  white-space: nowrap;
  pointer-events: none;
  opacity: 0;
  visibility: hidden;
  transition: opacity var(--transition), transform var(--transition), visibility var(--transition);
}
[data-tooltip]:hover::after, [data-tooltip]:focus-visible::after { opacity: 1; visibility: visible; transform: translate(-50%, 0); }

.field { display: grid; gap: 7px; margin: 0 0 16px; }
.field label, .field-label { color: var(--text-strong); font-size: 13px; font-weight: 700; }
.required-mark { color: var(--danger); }
.field-hint { margin-top: -1px; color: var(--text-muted); font-size: 12px; }
.field-error { color: var(--danger); font-size: 12px; font-weight: 600; }
.input, .field input, .field select, .field textarea, .composer-textarea {
  width: 100%;
  min-height: 42px;
  border: 1px solid var(--border-strong);
  border-radius: var(--radius-sm);
  background: var(--surface-input);
  color: var(--text-strong);
  padding: 9px 11px;
  box-shadow: inset 0 1px 2px rgba(15, 23, 42, 0.03);
  transition: border-color var(--transition), box-shadow var(--transition), background-color var(--transition);
}
.field input:hover:not(:disabled), .field select:hover:not(:disabled), .field textarea:hover:not(:disabled), .composer-textarea:hover:not(:disabled) { border-color: var(--text-subtle); }
.field input:disabled, .field select:disabled, .field textarea:disabled, .composer-textarea:disabled { background: var(--surface-muted); color: var(--text-muted); cursor: not-allowed; }
.field input[aria-invalid="true"], .field select[aria-invalid="true"], .field textarea[aria-invalid="true"] { border-color: var(--danger); box-shadow: 0 0 0 3px color-mix(in srgb, var(--danger) 16%, transparent); }
select { cursor: pointer; }

.login-page {
  min-height: 100vh;
  min-height: 100dvh;
  display: grid;
  place-items: center;
  padding: 28px;
  background:
    radial-gradient(circle at 18% 12%, color-mix(in srgb, var(--primary) 10%, transparent), transparent 30%),
    var(--bg-app);
}
.login-shell { width: min(440px, 100%); }
.login-theme-row { display: flex; justify-content: flex-end; margin-bottom: 12px; }
.login-card {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-xl);
  box-shadow: var(--shadow-sm);
  overflow: hidden;
}
.login-card-accent { height: 5px; background: var(--primary); }
.login-content { padding: 30px; }
.login-brand { display: flex; gap: 13px; align-items: center; margin-bottom: 28px; }
.brand-logo {
  width: 42px;
  height: 42px;
  display: grid;
  place-items: center;
  flex: 0 0 auto;
  border-radius: 12px;
  background: var(--primary);
  color: var(--primary-contrast);
  box-shadow: 0 8px 18px color-mix(in srgb, var(--primary) 24%, transparent);
}
.login-brand-copy strong { display: block; color: var(--text-strong); font-size: 15px; }
.login-brand-copy span { color: var(--text-muted); font-size: 12px; }
.login-card h1 { margin: 0; color: var(--text-strong); font-size: clamp(23px, 4vw, 28px); line-height: 1.2; letter-spacing: -0.025em; }
.login-intro { margin: 8px 0 24px; color: var(--text-muted); }
.login-card .btn[type="submit"] { width: 100%; min-height: 44px; margin-top: 4px; }
.login-error, .error-banner, .staff-feedback {
  border: 1px solid color-mix(in srgb, var(--danger) 26%, var(--border));
  border-radius: var(--radius-sm);
  background: var(--danger-soft);
  color: var(--danger);
  font-size: 13px;
  font-weight: 600;
}
.login-error { display: none; margin-top: 14px; padding: 10px 12px; }
.login-security { display: flex; gap: 8px; align-items: flex-start; margin-top: 20px; color: var(--text-muted); font-size: 12px; }

.app-shell {
  height: 100vh;
  height: 100dvh;
  display: grid;
  grid-template-columns: var(--sidebar-width) var(--queue-width) minmax(0, 1fr);
  grid-template-rows: minmax(0, 1fr);
  overflow: hidden;
  background: var(--bg-app);
}
.global-sidebar {
  min-width: 0;
  min-height: 0;
  display: flex;
  flex-direction: column;
  background: var(--surface);
  border-right: 1px solid var(--border);
  z-index: 4;
}
.brand-bar {
  min-height: var(--header-height);
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 15px 16px;
  border-bottom: 1px solid var(--border);
}
.brand-copy { min-width: 0; flex: 1; }
.brand-copy strong { display: block; color: var(--text-strong); font-size: 14px; letter-spacing: -0.01em; }
.brand-copy span { display: block; margin-top: 1px; color: var(--text-muted); font-size: 11px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.sidebar-label { padding: 18px 17px 8px; color: var(--text-subtle); font-size: 11px; font-weight: 800; letter-spacing: 0.08em; text-transform: uppercase; }
.queue-nav { display: grid; gap: 4px; padding: 0 10px; }
.queue-nav-btn {
  width: 100%;
  min-height: 42px;
  display: grid;
  grid-template-columns: 20px minmax(0, 1fr) auto;
  align-items: center;
  gap: 10px;
  padding: 8px 10px;
  border-radius: 9px;
  background: transparent;
  color: var(--text-muted);
  text-align: left;
  font-weight: 650;
}
.queue-nav-btn:hover { background: var(--surface-muted); color: var(--text-strong); }
.queue-nav-btn.active { background: var(--primary-soft); color: var(--primary); }
.queue-nav-btn.active::before { content: ""; position: absolute; }
.queue-nav-label { min-width: 0; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.queue-nav-count {
  min-width: 23px;
  height: 21px;
  display: inline-grid;
  place-items: center;
  padding: 0 6px;
  border-radius: 999px;
  background: var(--surface-muted);
  color: var(--text-muted);
  font-size: 11px;
  font-variant-numeric: tabular-nums;
}
.queue-nav-btn.active .queue-nav-count { background: color-mix(in srgb, var(--primary) 14%, var(--surface)); color: var(--primary); }
.queue-nav-count:empty { display: none; }
.nav-spacer { flex: 1; min-height: 14px; }
.agent-card { margin: 10px; padding: 10px; border: 1px solid var(--border); border-radius: var(--radius-md); background: var(--bg-subtle); }
.agent-main { display: flex; align-items: center; gap: 10px; min-width: 0; }
.avatar {
  width: 36px;
  height: 36px;
  display: grid;
  place-items: center;
  flex: 0 0 auto;
  border-radius: 50%;
  background: var(--primary-soft);
  color: var(--primary);
  font-size: 12px;
  font-weight: 800;
  text-transform: uppercase;
  user-select: none;
}
.avatar.large { width: 42px; height: 42px; font-size: 13px; }
.avatar.customer { background: var(--info-soft); color: var(--info); }
.avatar.ai { background: var(--surface-muted); color: var(--text-muted); }
.agent-copy { min-width: 0; flex: 1; }
.agent-copy strong { display: block; color: var(--text-strong); font-size: 13px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.agent-copy span { display: block; color: var(--text-muted); font-size: 11px; text-transform: capitalize; }
.staff-actions { display: flex; align-items: center; gap: 4px; margin-top: 10px; }
.staff-actions .btn { flex: 1; }

.inbox-panel {
  min-width: 0;
  min-height: 0;
  display: grid;
  grid-template-rows: auto auto minmax(0, 1fr) auto;
  background: var(--surface);
  border-right: 1px solid var(--border);
}
.inbox-header { min-height: var(--header-height); display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 13px 16px; border-bottom: 1px solid var(--border); }
.eyebrow { color: var(--text-subtle); font-size: 10px; font-weight: 800; letter-spacing: 0.09em; text-transform: uppercase; }
.inbox-title-row { display: flex; align-items: baseline; gap: 8px; }
.inbox-title-row h1 { margin: 1px 0 0; color: var(--text-strong); font-size: 18px; line-height: 1.2; letter-spacing: -0.02em; }
.queue-total { color: var(--text-muted); font-size: 12px; white-space: nowrap; }
.search-wrap { padding: 12px 14px; border-bottom: 1px solid var(--border); }
.search-field { position: relative; }
.search-field > .icon { position: absolute; z-index: 1; left: 11px; top: 50%; transform: translateY(-50%); color: var(--text-subtle); pointer-events: none; }
.search-field input { width: 100%; min-height: 40px; padding: 8px 42px 8px 36px; border: 1px solid var(--border); border-radius: var(--radius-sm); background: var(--bg-subtle); color: var(--text-strong); }
.search-field input::placeholder { color: var(--text-subtle); }
.search-clear { position: absolute; right: 4px; top: 50%; transform: translateY(-50%); width: 32px; height: 32px; }
.search-scope { margin-top: 7px; color: var(--text-subtle); font-size: 11px; }
.queue {
  min-height: 0;
  overflow: auto;
  overscroll-behavior: contain;
  background: var(--surface);
}
.ticket-row {
  width: 100%;
  position: relative;
  display: block;
  padding: 14px 15px;
  border-radius: 0;
  border-bottom: 1px solid var(--border);
  background: transparent;
  color: var(--text);
  text-align: left;
}
.ticket-row:hover { background: var(--surface-hover); }
.ticket-row.active { background: var(--surface-selected); box-shadow: inset 3px 0 0 var(--primary); }
.ticket-row:focus-visible { z-index: 1; outline-offset: -3px; }
.ticket-row-head { display: grid; grid-template-columns: auto minmax(0, 1fr) auto; gap: 10px; align-items: center; }
.ticket-row-copy { min-width: 0; }
.ticket-customer { color: var(--text-strong); font-weight: 750; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.ticket-reference { margin-top: 1px; color: var(--text-muted); font-family: var(--font-mono); font-size: 11px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.ticket-time { align-self: start; color: var(--text-subtle); font-size: 11px; font-variant-numeric: tabular-nums; white-space: nowrap; }
.ticket-summary { display: -webkit-box; margin: 10px 0 9px 46px; overflow: hidden; color: var(--text-muted); font-size: 12px; line-height: 1.45; -webkit-box-orient: vertical; -webkit-line-clamp: 2; overflow-wrap: anywhere; }
.ticket-row-meta { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; margin-left: 46px; }
.meta-chip, .status-badge, .role-badge, .channel-pill {
  max-width: 100%;
  display: inline-flex;
  align-items: center;
  gap: 5px;
  min-height: 23px;
  padding: 3px 7px;
  border-radius: 999px;
  font-size: 10px;
  font-weight: 750;
  line-height: 1;
  white-space: nowrap;
}
.meta-chip { background: var(--surface-muted); color: var(--text-muted); font-weight: 600; overflow: hidden; text-overflow: ellipsis; }
.channel-pill { background: var(--info-soft); color: var(--info); }
.status-badge { border: 1px solid transparent; text-transform: capitalize; }
.status-new { background: var(--info-soft); color: var(--info); }
.status-assigned, .status-in-progress { background: var(--primary-soft); color: var(--primary); }
.status-waiting-customer, .status-waiting-internal { background: var(--warning-soft); color: var(--warning); }
.status-resolved, .status-closed { background: var(--success-soft); color: var(--success); }
.status-default { background: var(--surface-muted); color: var(--text-muted); }
.queue-footer { min-height: 58px; display: flex; align-items: center; justify-content: space-between; gap: 8px; padding: 10px 14px; border-top: 1px solid var(--border); background: var(--surface); }
.page-summary { color: var(--text-muted); font-size: 11px; font-variant-numeric: tabular-nums; }
.pagination-actions { display: flex; gap: 5px; }

.loading-list { display: grid; gap: 0; }
.skeleton-row { padding: 16px; border-bottom: 1px solid var(--border); }
.skeleton { position: relative; overflow: hidden; border-radius: 7px; background: var(--surface-muted); }
.skeleton::after { content: ""; position: absolute; inset: 0; transform: translateX(-100%); background: linear-gradient(90deg, transparent, color-mix(in srgb, var(--surface) 62%, transparent), transparent); animation: shimmer 1.45s infinite; }
.skeleton-line { height: 11px; margin-bottom: 9px; }
.skeleton-line.short { width: 42%; }
.skeleton-line.medium { width: 68%; }
.skeleton-line.long { width: 92%; }
.empty-state { min-height: 220px; display: grid; place-items: center; padding: 32px 24px; text-align: center; }
.empty-state-inner { max-width: 280px; }
.empty-icon { width: 44px; height: 44px; display: grid; place-items: center; margin: 0 auto 13px; border-radius: 13px; background: var(--surface-muted); color: var(--text-muted); }
.empty-state h3 { margin: 0; color: var(--text-strong); font-size: 14px; }
.empty-state p { margin: 6px 0 0; color: var(--text-muted); font-size: 12px; }
.empty-state .btn { margin-top: 14px; }

.main {
  min-width: 0;
  min-height: 0;
  display: grid;
  grid-template-rows: auto minmax(0, 1fr) auto;
  background: var(--bg-app);
  position: relative;
}
.workspace-header {
  grid-row: 1;
  grid-column: 1;
  min-height: var(--header-height);
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 11px 16px;
  border-bottom: 1px solid var(--border);
  background: var(--surface);
  z-index: 3;
}
.mobile-back { display: none; }
.ticket-heading-avatar { display: none; }
.ticket-heading { min-width: 0; flex: 1; }
.ticket-heading-line { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; }
.ticket-heading h2 { max-width: 100%; margin: 0; color: var(--text-strong); font-size: 16px; line-height: 1.25; letter-spacing: -0.015em; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.ticket-subtitle { margin-top: 4px; color: var(--text-muted); font-size: 12px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.ticket-header-meta { display: flex; flex-wrap: wrap; gap: 5px; margin-top: 6px; }
.actions { display: flex; align-items: center; justify-content: flex-end; flex-wrap: wrap; gap: 7px; }
.context-toggle { display: none; }
.action-menu { position: relative; }
.action-menu[open] { z-index: 15; }
.menu-summary { list-style: none; min-height: 38px; display: inline-flex; align-items: center; gap: 7px; padding: 8px 12px; border: 1px solid var(--border); border-radius: var(--radius-sm); background: var(--surface); font-weight: 650; font-size: 13px; box-shadow: var(--shadow-xs); }
.menu-summary::-webkit-details-marker { display: none; }
.menu-summary:hover { background: var(--surface-hover); border-color: var(--border-strong); }
.action-menu-panel { position: absolute; top: calc(100% + 7px); right: 0; width: max-content; min-width: 205px; padding: 6px; border: 1px solid var(--border); border-radius: var(--radius-md); background: var(--surface-raised); box-shadow: var(--shadow-sm); }
.action-menu-panel button { width: 100%; min-height: 36px; justify-content: flex-start; border: 0; background: transparent; box-shadow: none; }
.workspace-loading { grid-row: 2; grid-column: 1; align-self: start; z-index: 6; width: 100%; height: 2px; overflow: hidden; background: transparent; }
.workspace-loading::after { content: ""; display: block; width: 35%; height: 100%; background: var(--primary); animation: indeterminate 1.1s ease-in-out infinite; }
.error-banner { grid-row: 2; grid-column: 1; align-self: start; z-index: 5; display: none; margin: 10px 14px 0; padding: 10px 12px; align-items: flex-start; gap: 9px; }
.error-banner.visible { display: flex; }
.error-banner-copy { flex: 1; }
.error-banner .icon-btn { width: 28px; height: 28px; margin: -4px -5px -4px 0; color: var(--danger); }
.workspace-content { grid-row: 2; grid-column: 1; min-height: 0; display: grid; grid-template-columns: minmax(0, 1fr) var(--context-width); }
.conversation-shell { min-width: 0; min-height: 0; position: relative; background: var(--bg-app); }
.conversation {
  height: 100%;
  overflow: auto;
  overscroll-behavior: contain;
  padding: 22px clamp(16px, 3vw, 36px) 28px;
  scroll-padding-bottom: 90px;
}
.conversation-inner { width: min(820px, 100%); margin: 0 auto; }
.conversation-empty { height: 100%; min-height: 260px; display: grid; place-items: center; }
.scroll-latest { position: absolute; z-index: 3; right: 18px; bottom: 16px; border-radius: 999px; box-shadow: var(--shadow-sm); }
.date-divider { display: flex; align-items: center; gap: 12px; margin: 22px 0 16px; color: var(--text-subtle); font-size: 10px; font-weight: 800; letter-spacing: 0.06em; text-transform: uppercase; }
.date-divider::before, .date-divider::after { content: ""; height: 1px; flex: 1; background: var(--border); }
.context-divider { display: flex; align-items: center; gap: 8px; margin: 0 auto 18px; width: max-content; max-width: 100%; padding: 6px 10px; border: 1px dashed var(--border-strong); border-radius: 999px; color: var(--text-muted); background: var(--surface); font-size: 11px; }
.message-row { display: grid; grid-template-columns: 34px minmax(0, 1fr); align-items: end; gap: 9px; margin: 11px 0; }
.message-row.outbound { grid-template-columns: minmax(0, 1fr) 34px; }
.message-row.outbound .message-avatar { grid-column: 2; }
.message-row.outbound .message-stack { grid-column: 1; grid-row: 1; justify-self: end; align-items: flex-end; }
.message-avatar { width: 32px; height: 32px; font-size: 10px; }
.message-stack { width: min(72%, 640px); min-width: 0; display: flex; flex-direction: column; align-items: flex-start; }
.message-bubble { width: fit-content; max-width: 100%; padding: 10px 12px; border: 1px solid var(--border); border-radius: 4px 14px 14px 14px; background: var(--surface); box-shadow: var(--shadow-xs); }
.message-row.outbound .message-bubble { border-radius: 14px 4px 14px 14px; border-color: color-mix(in srgb, var(--primary) 18%, var(--border)); background: var(--primary-soft); }
.message-row.ai .message-bubble { background: var(--surface-muted); }
.message-row.context-msg { opacity: 0.83; }
.message-row.context-msg .message-bubble { border-style: dashed; box-shadow: none; }
.message-sender { display: flex; align-items: center; flex-wrap: wrap; gap: 6px; margin-bottom: 5px; color: var(--text-strong); font-size: 11px; font-weight: 800; }
.role-badge { min-height: 18px; padding: 2px 6px; background: var(--surface-muted); color: var(--text-muted); font-size: 9px; letter-spacing: 0.035em; text-transform: uppercase; }
.message-body { color: var(--text-strong); white-space: pre-wrap; overflow-wrap: anywhere; word-break: break-word; }
.message-body code { font-family: var(--font-mono); font-size: 0.92em; }
.message-meta { display: flex; align-items: center; flex-wrap: wrap; gap: 5px; margin-top: 6px; padding: 0 3px; color: var(--text-subtle); font-size: 10px; }
.message-row.outbound .message-meta { justify-content: flex-end; }
.send-state { display: inline-flex; align-items: center; gap: 3px; font-weight: 700; }
.send-state.failed { color: var(--danger); }
.send-state.pending { color: var(--warning); }
.msg-media { margin: 0 0 8px; }
.media-open { max-width: 100%; display: block; padding: 0; border: 0; border-radius: 10px; background: transparent; cursor: zoom-in; }
.msg-media img { display: block; width: auto; max-width: min(440px, 100%); max-height: 420px; border-radius: 10px; object-fit: contain; background: var(--surface-muted); }
.msg-media audio { display: block; width: min(360px, 62vw); max-width: 100%; }
.media-unavailable { padding: 12px; border: 1px dashed var(--border-strong); border-radius: 9px; background: var(--surface-muted); color: var(--text-muted); font-size: 12px; }
.event { width: fit-content; max-width: min(90%, 620px); margin: 15px auto; padding: 6px 10px; border: 1px solid var(--border); border-radius: 999px; background: color-mix(in srgb, var(--surface) 82%, transparent); color: var(--text-muted); text-align: center; font-size: 10px; overflow-wrap: anywhere; }

.context-panel { min-width: 0; overflow: auto; border-left: 1px solid var(--border); background: var(--surface); }
.context-panel-head { position: sticky; top: 0; z-index: 2; min-height: 54px; display: flex; align-items: center; justify-content: space-between; gap: 10px; padding: 10px 15px; border-bottom: 1px solid var(--border); background: color-mix(in srgb, var(--surface) 94%, transparent); backdrop-filter: blur(10px); }
.context-panel-head strong { color: var(--text-strong); font-size: 13px; }
.context-close { display: none; }
.context { padding: 15px; }
.context-hero { display: flex; align-items: center; gap: 11px; padding-bottom: 15px; border-bottom: 1px solid var(--border); }
.context-hero-copy { min-width: 0; }
.context-hero-copy strong { display: block; color: var(--text-strong); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.context-hero-copy span { display: block; margin-top: 2px; color: var(--text-muted); font-size: 11px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.context-card { margin-top: 12px; border: 1px solid var(--border); border-radius: var(--radius-md); background: var(--surface); overflow: hidden; }
.context-card h3 { margin: 0; padding: 10px 12px; border-bottom: 1px solid var(--border); background: var(--bg-subtle); color: var(--text-muted); font-size: 10px; font-weight: 800; letter-spacing: 0.075em; text-transform: uppercase; }
.context-list { margin: 0; padding: 5px 12px; }
.context-item { display: grid; grid-template-columns: minmax(88px, 0.8fr) minmax(0, 1.2fr); gap: 10px; padding: 8px 0; border-bottom: 1px solid var(--border); }
.context-item:last-child { border-bottom: 0; }
.context-item dt { color: var(--text-muted); font-size: 11px; }
.context-item dd { min-width: 0; margin: 0; color: var(--text-strong); font-size: 11px; text-align: right; overflow-wrap: anywhere; white-space: pre-wrap; }
.context-item dd.code { font-family: var(--font-mono); font-size: 10px; }
.context-summary { padding: 11px 12px; color: var(--text); font-size: 12px; white-space: pre-wrap; overflow-wrap: anywhere; }
.context-empty { padding: 14px 12px; color: var(--text-muted); font-size: 11px; }
.context-backdrop { display: none; }

.composer-shell { grid-row: 3; grid-column: 1; position: relative; z-index: 3; border-top: 1px solid var(--border); background: var(--surface); }
.read-only-banner, .recording-bar, .media-draft { margin: 10px 14px 0; }
.read-only-banner { display: flex; align-items: flex-start; gap: 9px; padding: 10px 12px; border: 1px solid color-mix(in srgb, var(--warning) 32%, var(--border)); border-radius: var(--radius-sm); background: var(--warning-soft); color: var(--warning); font-size: 12px; }
.recording-bar { display: flex; align-items: center; gap: 9px; padding: 10px 12px; border: 1px solid color-mix(in srgb, var(--danger) 25%, var(--border)); border-radius: var(--radius-sm); background: var(--danger-soft); color: var(--danger); font-size: 12px; font-weight: 700; }
.recording-dot { width: 9px; height: 9px; flex: 0 0 auto; border-radius: 50%; background: var(--danger); animation: pulse 1s infinite; }
.recording-bar .btn { margin-left: auto; }
.media-draft { display: flex; align-items: center; gap: 12px; padding: 10px; border: 1px solid var(--border); border-radius: var(--radius-md); background: var(--bg-subtle); }
.media-draft-preview { width: 70px; height: 58px; display: grid; place-items: center; flex: 0 0 auto; overflow: hidden; border-radius: 9px; background: var(--surface-muted); }
.media-draft-preview img { width: 100%; height: 100%; object-fit: cover; }
.media-draft-preview audio { width: min(300px, 52vw); }
.media-draft.audio .media-draft-preview { width: auto; min-width: min(300px, 52vw); height: auto; background: transparent; }
.media-draft-info { min-width: 0; flex: 1; }
.media-draft-name { color: var(--text-strong); font-size: 12px; font-weight: 750; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.media-draft-size { margin-top: 2px; color: var(--text-muted); font-size: 11px; }
.media-draft-actions { display: flex; gap: 6px; }
.composer { padding: 11px 14px 12px; }
.composer-mode { display: flex; align-items: center; justify-content: space-between; gap: 10px; margin: 0 2px 7px; }
.reply-mode { display: inline-flex; align-items: center; gap: 6px; color: var(--success); font-size: 11px; font-weight: 750; }
.reply-mode-dot { width: 7px; height: 7px; border-radius: 50%; background: currentColor; }
.composer-shortcut { color: var(--text-subtle); font-size: 10px; }
.composer-row { display: grid; grid-template-columns: auto auto minmax(0, 1fr) auto; align-items: end; gap: 7px; }
.composer-input-wrap { min-width: 0; }
.composer-textarea { min-height: 42px; max-height: 154px; resize: none; line-height: 1.45; padding: 10px 11px; overflow-y: auto; }
.composer-meta { min-height: 18px; display: flex; justify-content: space-between; gap: 10px; margin: 4px 2px 0; color: var(--text-subtle); font-size: 10px; }
.composer-count { margin-left: auto; font-variant-numeric: tabular-nums; }
.send-btn { min-width: 82px; min-height: 42px; }

.modal-backdrop { position: fixed; inset: 0; z-index: 50; display: grid; place-items: center; padding: 22px; background: var(--overlay); }
.modal-card { width: min(820px, 100%); max-height: min(90vh, 900px); max-height: min(90dvh, 900px); display: flex; flex-direction: column; overflow: hidden; border: 1px solid var(--border-strong); border-radius: var(--radius-lg); background: var(--surface-raised); box-shadow: var(--shadow-lg); }
.modal-card.transfer-card { width: min(520px, 100%); }
.modal-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 18px; padding: 18px 20px 14px; border-bottom: 1px solid var(--border); }
.modal-head h2 { margin: 0; color: var(--text-strong); font-size: 18px; letter-spacing: -0.015em; }
.modal-subtitle { margin-top: 3px; color: var(--text-muted); font-size: 12px; }
.modal-close { width: 36px; height: 36px; }
.modal-body { min-height: 0; overflow: auto; padding: 18px 20px 20px; }
.staff-feedback { display: none; margin-bottom: 12px; padding: 10px 12px; }
.staff-feedback.success-box { border-color: color-mix(in srgb, var(--success) 28%, var(--border)); background: var(--success-soft); color: var(--success); }
.staff-view-heading, .section-title { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.staff-view-heading { margin-bottom: 16px; padding-bottom: 14px; border-bottom: 1px solid var(--border); }
.staff-view-heading strong, .section-title strong { color: var(--text-strong); }
.staff-view-heading .muted { margin-top: 2px; font-size: 12px; }
.staff-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 0 15px; }
.staff-grid .wide { grid-column: 1 / -1; }
.inline-check { min-height: 42px; display: flex; align-items: center; gap: 9px; margin: 0 0 16px; padding-top: 20px; color: var(--text-strong); font-weight: 650; }
.inline-check input { width: 17px; height: 17px; margin: 0; accent-color: var(--primary); }
.form-note { padding: 10px 12px; border-radius: var(--radius-sm); background: var(--surface-muted); color: var(--text-muted); font-size: 11px; }
.modal-actions { display: flex; justify-content: flex-end; gap: 8px; margin-top: 18px; }
.staff-actions-header { display: flex; gap: 7px; }
.staff-list { margin-top: 14px; border: 1px solid var(--border); border-radius: var(--radius-md); overflow: hidden; }
.staff-list-head, .staff-list-row { display: grid; grid-template-columns: minmax(150px, 1.2fr) minmax(180px, 1.4fr) 90px 90px minmax(128px, auto); gap: 12px; align-items: center; }
.staff-list-head { padding: 9px 12px; border-bottom: 1px solid var(--border); background: var(--bg-subtle); color: var(--text-subtle); font-size: 10px; font-weight: 800; letter-spacing: 0.06em; text-transform: uppercase; }
.staff-list-row { padding: 12px; border-bottom: 1px solid var(--border); }
.staff-list-row:last-child { border-bottom: 0; }
.staff-name { color: var(--text-strong); font-weight: 750; }
.staff-created { margin-top: 2px; color: var(--text-subtle); font-size: 10px; }
.staff-email { color: var(--text-muted); font-size: 12px; overflow-wrap: anywhere; }
.role-badge { width: fit-content; min-height: 22px; padding: 3px 7px; background: var(--primary-soft); color: var(--primary); font-size: 10px; text-transform: capitalize; }
.status-dot { width: fit-content; display: inline-flex; align-items: center; gap: 5px; padding: 3px 7px; border-radius: 999px; background: var(--success-soft); color: var(--success); font-size: 10px; font-weight: 750; }
.status-dot::before { content: ""; width: 6px; height: 6px; border-radius: 50%; background: currentColor; }
.status-dot.off { background: var(--surface-muted); color: var(--text-muted); }
.status-dot.conflict { margin-top: 5px; background: var(--warning-soft); color: var(--warning); }
.staff-row-actions { display: flex; justify-content: flex-end; gap: 5px; flex-wrap: wrap; }
.staff-row-actions .btn { min-height: 31px; padding: 5px 8px; font-size: 11px; }
.transfer-summary { margin: 0 0 16px; padding: 11px 12px; border-radius: var(--radius-sm); background: var(--info-soft); color: var(--info); font-size: 12px; }

.toast-region { position: fixed; z-index: 90; right: 18px; bottom: 18px; width: min(360px, calc(100vw - 36px)); display: grid; gap: 8px; pointer-events: none; }
.toast { display: flex; align-items: flex-start; gap: 9px; padding: 11px 12px; border: 1px solid var(--border-strong); border-radius: var(--radius-md); background: var(--surface-raised); color: var(--text); box-shadow: var(--shadow-sm); animation: toast-in 180ms ease-out; pointer-events: auto; }
.toast.success { border-color: color-mix(in srgb, var(--success) 32%, var(--border)); }
.toast.error { border-color: color-mix(in srgb, var(--danger) 32%, var(--border)); }
.toast-copy { flex: 1; font-size: 12px; }
.toast .icon-btn { width: 26px; height: 26px; margin: -4px -5px -4px 0; }

@keyframes spin { to { transform: rotate(360deg); } }
@keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.35; } }
@keyframes shimmer { 100% { transform: translateX(100%); } }
@keyframes indeterminate { 0% { transform: translateX(-120%); } 55% { transform: translateX(190%); } 100% { transform: translateX(190%); } }
@keyframes toast-in { from { opacity: 0; transform: translateY(8px); } to { opacity: 1; transform: translateY(0); } }

@media (max-width: 1280px) {
  :root { --sidebar-width: 82px; --queue-width: 350px; }
  .brand-bar { position: relative; justify-content: center; padding-inline: 10px; }
  .brand-copy, .sidebar-label, .agent-copy, .staff-actions .btn span, .queue-nav-label { display: none; }
  .brand-bar .brand-logo { width: 34px; height: 34px; position: absolute; top: 5px; }
  .brand-bar .icon-btn { width: 28px; height: 28px; display: inline-grid; position: absolute; bottom: 5px; }
  .queue-nav { padding: 8px; }
  .queue-nav-btn { position: relative; grid-template-columns: 1fr; justify-items: center; padding: 9px 6px; }
  .queue-nav-count { position: absolute; top: 4px; right: 4px; min-width: 18px; height: 18px; padding: 0 4px; font-size: 9px; }
  .agent-card { padding: 8px; }
  .agent-main { justify-content: center; }
  .staff-actions { display: grid; grid-template-columns: 1fr 1fr; }
  .staff-actions .btn { min-width: 0; padding: 7px; }
  .staff-actions .btn .icon { margin: 0; }
}

@media (max-width: 1100px) {
  :root { --queue-width: 330px; }
  .workspace-content { grid-template-columns: minmax(0, 1fr); }
  .context-panel { position: fixed; z-index: 42; top: 0; right: 0; bottom: 0; width: min(360px, 92vw); transform: translateX(104%); border-left: 1px solid var(--border-strong); box-shadow: var(--shadow-lg); transition: transform 190ms ease; }
  .context-panel.open { transform: translateX(0); }
  .context-close { display: inline-grid; }
  .context-backdrop { position: fixed; z-index: 41; inset: 0; display: block; background: var(--overlay); opacity: 0; visibility: hidden; transition: opacity 190ms ease, visibility 190ms ease; }
  .context-backdrop.visible { opacity: 1; visibility: visible; }
  .context-toggle { display: inline-grid; }
}

@media (max-width: 900px) {
  :root { --queue-width: 306px; }
  .actions .btn .optional-label { display: none; }
  .message-stack { width: min(82%, 640px); }
}

@media (max-width: 800px) {
  body { overflow: hidden; }
  .app-shell { display: flex; flex-direction: column; }
  .global-sidebar {
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto;
    grid-template-areas: "brand agent" "nav nav";
    border-right: 0;
    border-bottom: 1px solid var(--border);
  }
  .brand-bar { grid-area: brand; min-height: 58px; justify-content: flex-start; padding: 8px 12px; border-bottom: 0; }
  .brand-bar .brand-logo { width: 36px; height: 36px; position: static; border-radius: 10px; }
  .brand-bar .icon-btn { width: 36px; height: 36px; position: static; margin-left: auto; }
  .brand-copy { display: block; }
  .brand-copy span { display: none; }
  .queue-nav { grid-area: nav; display: flex; gap: 5px; overflow-x: auto; padding: 5px 10px 9px; scroll-snap-type: x proximity; }
  .queue-nav-btn { width: auto; min-width: max-content; min-height: 35px; display: inline-flex; justify-content: center; gap: 6px; padding: 7px 10px; scroll-snap-align: start; }
  .queue-nav-btn .icon { width: 16px; height: 16px; }
  .queue-nav-label { display: inline; }
  .queue-nav-count { position: static; min-width: 19px; height: 18px; }
  .nav-spacer, .sidebar-label { display: none; }
  .agent-card { grid-area: agent; align-self: center; display: flex; align-items: center; margin: 0 9px 0 0; padding: 0; border: 0; background: transparent; }
  .agent-main { display: none; }
  .staff-actions { display: flex; margin: 0; }
  .staff-actions .btn { width: 36px; height: 36px; min-height: 36px; flex: 0 0 auto; padding: 0; border-color: transparent; background: transparent; box-shadow: none; }
  .staff-actions .btn:hover { background: var(--surface-muted); }
  .inbox-panel { min-height: 0; flex: 1; border-right: 0; }
  .inbox-header { min-height: 62px; padding: 10px 14px; }
  .main { display: none; min-height: 0; flex: 1; }
  body.mobile-ticket-open .global-sidebar, body.mobile-ticket-open .inbox-panel { display: none; }
  body.mobile-ticket-open .main { display: grid; }
  .workspace-header { min-height: 64px; padding: 8px 10px; }
  .mobile-back { display: inline-grid; }
  .ticket-heading-avatar { display: grid; }
  .ticket-heading h2 { font-size: 14px; }
  .ticket-subtitle { max-width: 58vw; }
  .ticket-header-meta { display: none; }
  .actions { gap: 4px; }
  .actions .btn { min-width: 38px; padding: 8px 9px; }
  .actions > .btn .action-label, .menu-summary .action-label { display: none; }
  .menu-summary { width: 38px; padding: 8px; justify-content: center; }
  .action-menu-panel { right: 0; }
  .conversation { padding: 16px 12px 24px; }
  .message-stack { width: min(88%, 620px); }
  .message-row { grid-template-columns: 29px minmax(0, 1fr); gap: 6px; }
  .message-row.outbound { grid-template-columns: minmax(0, 1fr) 29px; }
  .message-avatar { width: 28px; height: 28px; font-size: 9px; }
  .composer { padding: 9px 10px 10px; }
  .composer-mode { margin-inline: 1px; }
  .composer-shortcut { display: none; }
  .composer-row { grid-template-columns: auto auto minmax(0, 1fr) auto; gap: 5px; }
  .composer-row .icon-btn { width: 40px; height: 40px; }
  .send-btn { min-width: 44px; width: 44px; padding: 0; }
  .send-btn .send-label { display: none; }
  .read-only-banner, .recording-bar, .media-draft { margin-inline: 10px; }
  .media-draft { align-items: flex-start; flex-wrap: wrap; }
  .media-draft-info { min-width: 120px; }
  .modal-backdrop { align-items: end; padding: 0; }
  .modal-card { width: 100%; max-height: 92dvh; border-radius: 18px 18px 0 0; border-bottom: 0; }
  .modal-card.transfer-card { width: 100%; }
  .modal-head { padding: 16px; }
  .modal-body { padding: 16px; }
  .staff-grid { grid-template-columns: 1fr; }
  .staff-grid .wide { grid-column: auto; }
  .inline-check { padding-top: 0; }
  .staff-list-head { display: none; }
  .staff-list-row { grid-template-columns: 1fr auto; gap: 8px 12px; }
  .staff-list-row .staff-email { grid-column: 1 / -1; }
  .staff-list-row .role-badge { grid-column: 1; }
  .staff-list-row .status-dot { grid-column: 2; grid-row: 3; }
  .staff-row-actions { grid-column: 1 / -1; justify-content: flex-start; padding-top: 5px; }
}

@media (max-width: 520px) {
  .login-page { padding: 16px; }
  .login-content { padding: 24px 20px; }
  .inbox-title-row h1 { font-size: 17px; }
  .queue-total { display: none; }
  .ticket-row { padding-inline: 12px; }
  .ticket-summary, .ticket-row-meta { margin-left: 43px; }
  .ticket-heading-avatar { display: none; }
  .ticket-subtitle { max-width: 50vw; }
  .message-stack { width: min(92%, 620px); }
  .message-sender { margin-bottom: 4px; }
  .message-bubble { padding: 9px 10px; }
  .composer-row { grid-template-columns: auto auto minmax(0, 1fr) auto; }
  .composer-row .icon-btn { width: 38px; height: 38px; }
  .composer-textarea { min-height: 40px; padding: 9px; }
  .send-btn { width: 40px; min-width: 40px; min-height: 40px; }
  .modal-actions { flex-direction: column-reverse; }
  .modal-actions .btn { width: 100%; }
  .staff-view-heading, .section-title { align-items: flex-start; flex-direction: column; }
  .staff-actions-header { width: 100%; }
  .staff-actions-header .btn { flex: 1; }
}

@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after { scroll-behavior: auto !important; animation-duration: 0.01ms !important; animation-iteration-count: 1 !important; transition-duration: 0.01ms !important; }
}
</style>
</head>
<body>
<svg class="sr-only" aria-hidden="true" focusable="false">
  <symbol id="icon-logo" viewBox="0 0 24 24"><path d="M4 7.5h11.5v8H4z"/><path d="M15.5 10h2.2l2.3 2.5v3h-4.5z"/><circle cx="7.5" cy="17.5" r="1.5"/><circle cx="17.5" cy="17.5" r="1.5"/><path d="M7 10.5h5M9.5 8v5"/></symbol>
  <symbol id="icon-inbox" viewBox="0 0 24 24"><path d="M4 5.5h16v13H4z"/><path d="M4 14h4l1.5 2h5l1.5-2h4"/></symbol>
  <symbol id="icon-user-check" viewBox="0 0 24 24"><circle cx="9" cy="8" r="3"/><path d="M3.5 19c.6-3.4 2.4-5.1 5.5-5.1 1.2 0 2.2.2 3 .7"/><path d="m15 17 2 2 4-5"/></symbol>
  <symbol id="icon-eye" viewBox="0 0 24 24"><path d="M2.8 12s3.2-5.5 9.2-5.5S21.2 12 21.2 12 18 17.5 12 17.5 2.8 12 2.8 12Z"/><circle cx="12" cy="12" r="2.5"/></symbol>
  <symbol id="icon-activity" viewBox="0 0 24 24"><path d="M3 12h4l2-6 4 12 2-6h6"/></symbol>
  <symbol id="icon-clock" viewBox="0 0 24 24"><circle cx="12" cy="12" r="8.5"/><path d="M12 7.5V12l3 2"/></symbol>
  <symbol id="icon-history" viewBox="0 0 24 24"><path d="M4 7v5h5"/><path d="M5.2 16.7A8 8 0 1 0 4 12"/><path d="M12 8v4l2.5 1.5"/></symbol>
  <symbol id="icon-users" viewBox="0 0 24 24"><circle cx="9" cy="8" r="3"/><path d="M3.5 19c.5-3.3 2.4-5 5.5-5s5 1.7 5.5 5"/><path d="M15.5 6.4a3 3 0 0 1 0 5.2M16.5 14.2c2.2.4 3.5 2 4 4.8"/></symbol>
  <symbol id="icon-log-out" viewBox="0 0 24 24"><path d="M10 5H5v14h5"/><path d="M13 8l4 4-4 4M8 12h9"/></symbol>
  <symbol id="icon-sun" viewBox="0 0 24 24"><circle cx="12" cy="12" r="3.5"/><path d="M12 2.5v2M12 19.5v2M2.5 12h2M19.5 12h2M5.3 5.3l1.4 1.4M17.3 17.3l1.4 1.4M18.7 5.3l-1.4 1.4M6.7 17.3l-1.4 1.4"/></symbol>
  <symbol id="icon-moon" viewBox="0 0 24 24"><path d="M20 15.2A8 8 0 0 1 8.8 4a8.2 8.2 0 1 0 11.2 11.2Z"/></symbol>
  <symbol id="icon-search" viewBox="0 0 24 24"><circle cx="10.5" cy="10.5" r="6.5"/><path d="m15.5 15.5 4 4"/></symbol>
  <symbol id="icon-x" viewBox="0 0 24 24"><path d="m6 6 12 12M18 6 6 18"/></symbol>
  <symbol id="icon-refresh" viewBox="0 0 24 24"><path d="M20 7v5h-5"/><path d="M18.6 16.8A8 8 0 1 1 20 12"/></symbol>
  <symbol id="icon-chevron-left" viewBox="0 0 24 24"><path d="m15 18-6-6 6-6"/></symbol>
  <symbol id="icon-chevron-right" viewBox="0 0 24 24"><path d="m9 18 6-6-6-6"/></symbol>
  <symbol id="icon-chevron-down" viewBox="0 0 24 24"><path d="m7 10 5 5 5-5"/></symbol>
  <symbol id="icon-arrow-left" viewBox="0 0 24 24"><path d="M19 12H5M11 6l-6 6 6 6"/></symbol>
  <symbol id="icon-panel-right" viewBox="0 0 24 24"><rect x="3" y="4" width="18" height="16" rx="1.5"/><path d="M15 4v16"/></symbol>
  <symbol id="icon-message" viewBox="0 0 24 24"><path d="M4 5h16v12H9l-5 3z"/></symbol>
  <symbol id="icon-phone" viewBox="0 0 24 24"><path d="M7 3.5 10 7 8.2 9.2a14.6 14.6 0 0 0 6.6 6.6L17 14l3.5 3c-1.1 2.4-2.8 3.4-5 2.6A18.4 18.4 0 0 1 4.4 8.5c-.8-2.2.2-3.9 2.6-5Z"/></symbol>
  <symbol id="icon-tag" viewBox="0 0 24 24"><path d="M4 4h7l9 9-7 7-9-9z"/><circle cx="8" cy="8" r="1"/></symbol>
  <symbol id="icon-user" viewBox="0 0 24 24"><circle cx="12" cy="8" r="3.5"/><path d="M5 20c.7-4 3-6 7-6s6.3 2 7 6"/></symbol>
  <symbol id="icon-transfer" viewBox="0 0 24 24"><path d="M4 8h12M13 5l3 3-3 3M20 16H8M11 13l-3 3 3 3"/></symbol>
  <symbol id="icon-check" viewBox="0 0 24 24"><path d="m5 12 4 4 10-10"/></symbol>
  <symbol id="icon-check-circle" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="m8 12 2.5 2.5L16.5 8"/></symbol>
  <symbol id="icon-alert" viewBox="0 0 24 24"><path d="M12 3 2.8 20h18.4z"/><path d="M12 9v4M12 16.5h.01"/></symbol>
  <symbol id="icon-info" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M12 10.5V17M12 7h.01"/></symbol>
  <symbol id="icon-image" viewBox="0 0 24 24"><rect x="3" y="4" width="18" height="16" rx="2"/><circle cx="8.5" cy="9" r="1.5"/><path d="m5 18 5-5 3 3 2-2 4 4"/></symbol>
  <symbol id="icon-mic" viewBox="0 0 24 24"><rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5.5 11.5a6.5 6.5 0 0 0 13 0M12 18v3M8.5 21h7"/></symbol>
  <symbol id="icon-square" viewBox="0 0 24 24"><rect x="6" y="6" width="12" height="12" rx="1"/></symbol>
  <symbol id="icon-send" viewBox="0 0 24 24"><path d="m3.5 4 17 8-17 8 3-8zM6.5 12h14"/></symbol>
  <symbol id="icon-shield" viewBox="0 0 24 24"><path d="M12 3 5 6v5c0 4.5 2.5 7.7 7 10 4.5-2.3 7-5.5 7-10V6z"/><path d="m9 12 2 2 4-4"/></symbol>
  <symbol id="icon-plus" viewBox="0 0 24 24"><path d="M12 5v14M5 12h14"/></symbol>
  <symbol id="icon-edit" viewBox="0 0 24 24"><path d="M4 20h4l11-11-4-4L4 16zM13.5 6.5l4 4"/></symbol>
  <symbol id="icon-trash" viewBox="0 0 24 24"><path d="M4 7h16M9 7V4h6v3M7 7l1 13h8l1-13M10 11v5M14 11v5"/></symbol>
  <symbol id="icon-lock" viewBox="0 0 24 24"><rect x="5" y="10" width="14" height="10" rx="2"/><path d="M8 10V7a4 4 0 0 1 8 0v3"/></symbol>
  <symbol id="icon-truck" viewBox="0 0 24 24"><path d="M3 6h11v10H3zM14 9h3l4 4v3h-7z"/><circle cx="7" cy="18" r="2"/><circle cx="17.5" cy="18" r="2"/></symbol>
  <symbol id="icon-arrow-down" viewBox="0 0 24 24"><path d="M12 4v14M6 12l6 6 6-6"/></symbol>
</svg>

<main class="login-page" id="loginPage">
  <div class="login-shell">
    <div class="login-theme-row">
      <button class="icon-btn" id="loginThemeBtn" type="button" aria-label="Switch to dark theme" data-tooltip="Change theme">
        <svg class="icon" aria-hidden="true"><use href="#icon-moon"></use></svg>
      </button>
    </div>
    <form class="login-card" id="loginForm" novalidate>
      <div class="login-card-accent"></div>
      <div class="login-content">
        <div class="login-brand">
          <span class="brand-logo" aria-hidden="true"><svg class="icon icon-lg"><use href="#icon-logo"></use></svg></span>
          <div class="login-brand-copy"><strong>Shipment Support</strong><span>Customer operations workspace</span></div>
        </div>
        <h1>Welcome back</h1>
        <p class="login-intro">Sign in with your staff account to manage customer conversations and shipment escalations.</p>
        <div class="field">
          <label for="email">Email address</label>
          <input id="email" type="email" inputmode="email" autocomplete="username" maxlength="320" required aria-describedby="emailHint" />
          <div class="field-hint" id="emailHint">Use the email assigned to your support account.</div>
        </div>
        <div class="field">
          <label for="password">Password</label>
          <input id="password" type="password" autocomplete="current-password" required />
        </div>
        <button class="btn primary" id="loginBtn" type="submit">
          <svg class="icon" aria-hidden="true"><use href="#icon-log-out"></use></svg>
          <span>Sign in</span>
        </button>
        <div class="login-error" id="loginError" role="alert" aria-live="assertive"></div>
        <div class="login-security"><svg class="icon icon-sm" aria-hidden="true"><use href="#icon-shield"></use></svg><span>Your support session and customer data are kept out of shared browser caches.</span></div>
      </div>
    </form>
  </div>
</main>

<div class="app-shell hidden" id="dashboard">
  <aside class="global-sidebar" aria-label="Support workspace navigation">
    <div class="brand-bar">
      <span class="brand-logo" aria-hidden="true"><svg class="icon icon-lg"><use href="#icon-logo"></use></svg></span>
      <div class="brand-copy"><strong>Shipment Support</strong><span>Customer operations</span></div>
      <button class="icon-btn" id="themeBtn" type="button" aria-label="Switch to dark theme" data-tooltip="Change theme">
        <svg class="icon" aria-hidden="true"><use href="#icon-moon"></use></svg>
      </button>
    </div>
    <div class="sidebar-label">Queues</div>
    <nav class="queue-nav" id="tabs" aria-label="Ticket queues">
      <button class="queue-nav-btn active" data-view="unassigned" type="button" aria-label="Unassigned tickets" aria-current="page" title="Unassigned">
        <svg class="icon" aria-hidden="true"><use href="#icon-inbox"></use></svg><span class="queue-nav-label">Unassigned</span><span class="queue-nav-count" data-count-for="unassigned"></span>
      </button>
      <button class="queue-nav-btn" data-view="mine" type="button" aria-label="My tickets" title="My tickets">
        <svg class="icon" aria-hidden="true"><use href="#icon-user-check"></use></svg><span class="queue-nav-label">My tickets</span><span class="queue-nav-count" data-count-for="mine"></span>
      </button>
      <button class="queue-nav-btn" data-view="watching" type="button" aria-label="Watching" title="Watching">
        <svg class="icon" aria-hidden="true"><use href="#icon-eye"></use></svg><span class="queue-nav-label">Watching</span><span class="queue-nav-count" data-count-for="watching"></span>
      </button>
      <button class="queue-nav-btn" data-view="active" type="button" aria-label="Active tickets" title="Active">
        <svg class="icon" aria-hidden="true"><use href="#icon-activity"></use></svg><span class="queue-nav-label">Active</span><span class="queue-nav-count" data-count-for="active"></span>
      </button>
      <button class="queue-nav-btn" data-view="waiting" type="button" aria-label="Waiting tickets" title="Waiting">
        <svg class="icon" aria-hidden="true"><use href="#icon-clock"></use></svg><span class="queue-nav-label">Waiting</span><span class="queue-nav-count" data-count-for="waiting"></span>
      </button>
      <button class="queue-nav-btn" data-view="history" type="button" aria-label="Ticket history" title="History">
        <svg class="icon" aria-hidden="true"><use href="#icon-history"></use></svg><span class="queue-nav-label">History</span><span class="queue-nav-count" data-count-for="history"></span>
      </button>
    </nav>
    <div class="nav-spacer"></div>
    <div class="agent-card">
      <div class="agent-main">
        <div class="avatar" id="agentAvatar" aria-hidden="true">—</div>
        <div class="agent-copy"><strong id="agentName">Support agent</strong><span id="agentLabel"></span></div>
      </div>
      <div class="staff-actions">
        <button class="btn ghost hidden" id="manageStaffBtn" type="button" aria-label="Staff accounts" title="Staff accounts">
          <svg class="icon" aria-hidden="true"><use href="#icon-users"></use></svg><span>Staff</span>
        </button>
        <button class="btn ghost" id="logoutBtn" type="button" aria-label="Sign out" title="Sign out">
          <svg class="icon" aria-hidden="true"><use href="#icon-log-out"></use></svg><span>Sign out</span>
        </button>
      </div>
    </div>
  </aside>

  <section class="inbox-panel" aria-labelledby="queueTitle">
    <header class="inbox-header">
      <div>
        <div class="eyebrow">Support queue</div>
        <div class="inbox-title-row"><h1 id="queueTitle">Unassigned</h1><span class="queue-total" id="queueTotal">—</span></div>
      </div>
      <button class="icon-btn" id="refreshQueueBtn" type="button" aria-label="Refresh queue" data-tooltip="Refresh queue">
        <svg class="icon" aria-hidden="true"><use href="#icon-refresh"></use></svg>
      </button>
    </header>
    <div class="search-wrap">
      <label class="sr-only" for="queueSearch">Search tickets on this page</label>
      <div class="search-field">
        <svg class="icon" aria-hidden="true"><use href="#icon-search"></use></svg>
        <input id="queueSearch" type="search" autocomplete="off" placeholder="Search this page" aria-describedby="searchScope" />
        <button class="icon-btn search-clear hidden" id="clearQueueSearchBtn" type="button" aria-label="Clear ticket search">
          <svg class="icon icon-sm" aria-hidden="true"><use href="#icon-x"></use></svg>
        </button>
      </div>
      <div class="search-scope" id="searchScope">Searches the loaded page by customer, ticket, shipment, status, or support line.</div>
    </div>
    <div class="queue" id="queue" role="listbox" aria-label="Support tickets" aria-busy="false">
      <div class="empty-state"><div class="empty-state-inner"><div class="empty-icon"><svg class="icon icon-lg" aria-hidden="true"><use href="#icon-inbox"></use></svg></div><h3>No tickets loaded</h3><p>Your selected queue will appear here after sign-in.</p></div></div>
    </div>
    <footer class="queue-footer" id="queueFooter">
      <span class="page-summary" id="pageSummary">Page 1</span>
      <div class="pagination-actions">
        <button class="icon-btn" id="previousPageBtn" type="button" aria-label="Previous ticket page" data-tooltip="Previous page" disabled><svg class="icon" aria-hidden="true"><use href="#icon-chevron-left"></use></svg></button>
        <button class="icon-btn" id="nextPageBtn" type="button" aria-label="Next ticket page" data-tooltip="Next page" disabled><svg class="icon" aria-hidden="true"><use href="#icon-chevron-right"></use></svg></button>
      </div>
    </footer>
  </section>

  <main class="main" aria-labelledby="ticketTitle">
    <header class="workspace-header">
      <button class="icon-btn mobile-back" id="mobileBackBtn" type="button" aria-label="Back to ticket list">
        <svg class="icon" aria-hidden="true"><use href="#icon-arrow-left"></use></svg>
      </button>
      <div class="avatar customer large ticket-heading-avatar" id="ticketAvatar" aria-hidden="true">—</div>
      <div class="ticket-heading">
        <div class="ticket-heading-line"><h2 id="ticketTitle">Select a ticket</h2><span id="ticketHeadingStatus"></span></div>
        <div class="ticket-subtitle" id="ticketSubtitle">Choose a conversation from the queue to begin.</div>
        <div class="ticket-header-meta" id="ticketHeaderMeta"></div>
      </div>
      <div class="actions" id="actions"></div>
      <button class="icon-btn context-toggle" id="contextToggleBtn" type="button" aria-label="Open customer details" aria-controls="contextPanel" aria-expanded="false" data-tooltip="Customer details">
        <svg class="icon" aria-hidden="true"><use href="#icon-panel-right"></use></svg>
      </button>
    </header>
    <div class="workspace-loading hidden" id="workspaceLoading" role="progressbar" aria-label="Loading ticket"></div>
    <div class="error-banner" id="error" role="alert" aria-live="assertive">
      <svg class="icon" aria-hidden="true"><use href="#icon-alert"></use></svg>
      <div class="error-banner-copy" id="errorText"></div>
      <button class="icon-btn" id="dismissErrorBtn" type="button" aria-label="Dismiss error"><svg class="icon icon-sm" aria-hidden="true"><use href="#icon-x"></use></svg></button>
    </div>
    <div class="workspace-content">
      <section class="conversation-shell" aria-label="Ticket conversation">
        <div class="conversation" id="conversation" role="log" aria-label="Conversation timeline" tabindex="0">
          <div class="conversation-empty"><div class="empty-state-inner"><div class="empty-icon"><svg class="icon icon-lg" aria-hidden="true"><use href="#icon-message"></use></svg></div><h3>Open a customer conversation</h3><p>Ticket conversation will appear here.</p></div></div>
        </div>
        <button class="btn small scroll-latest hidden" id="scrollToLatestBtn" type="button"><svg class="icon icon-sm" aria-hidden="true"><use href="#icon-arrow-down"></use></svg>Latest</button>
      </section>
      <aside class="context-panel" id="contextPanel" aria-label="Customer and ticket details">
        <div class="context-panel-head"><strong>Customer details</strong><button class="icon-btn context-close" id="closeContextBtn" type="button" aria-label="Close customer details"><svg class="icon" aria-hidden="true"><use href="#icon-x"></use></svg></button></div>
        <div class="context" id="context"><div class="empty-state"><div class="empty-state-inner"><div class="empty-icon"><svg class="icon icon-lg" aria-hidden="true"><use href="#icon-user"></use></svg></div><h3>No ticket selected</h3><p>Customer and shipment context will appear here.</p></div></div></div>
      </aside>
      <div class="context-backdrop" id="contextBackdrop" aria-hidden="true"></div>
    </div>
    <div class="composer-shell">
      <div class="read-only-banner hidden" id="readOnlyBanner" role="status"></div>
      <div class="recording-bar hidden" id="recordingBar" role="status" aria-live="polite"><span class="recording-dot" aria-hidden="true"></span><span id="recordingTime">Recording 0:00</span><button class="btn small" id="cancelRecordingBtn" type="button">Cancel recording</button></div>
      <div class="media-draft hidden" id="mediaDraft"></div>
      <div class="composer">
        <div class="composer-mode"><span class="reply-mode"><span class="reply-mode-dot" aria-hidden="true"></span>Public reply · WhatsApp</span><span class="composer-shortcut">Enter to send · Shift+Enter for a new line</span></div>
        <div class="composer-row">
          <input id="imageInput" type="file" accept="image/jpeg,image/png" class="hidden" />
          <button class="icon-btn" id="imageBtn" type="button" disabled aria-label="Attach an image" data-tooltip="Attach image">
            <svg class="icon" aria-hidden="true"><use href="#icon-image"></use></svg>
          </button>
          <button class="icon-btn" id="voiceBtn" type="button" disabled aria-label="Record a voice note" aria-pressed="false" data-tooltip="Record voice note">
            <svg class="icon" aria-hidden="true"><use href="#icon-mic"></use></svg>
          </button>
          <div class="composer-input-wrap">
            <label class="sr-only" for="message">Public reply to customer on WhatsApp</label>
            <textarea class="composer-textarea" id="message" dir="auto" rows="1" maxlength="4096" placeholder="Reply to customer on WhatsApp" disabled aria-describedby="composerHelp composerCount"></textarea>
            <div class="composer-meta"><span id="composerHelp">Messages are sent from the WhatsApp number that received this ticket.</span><span class="composer-count" id="composerCount">0 / 4096</span></div>
          </div>
          <button class="btn primary send-btn" id="sendBtn" type="button" disabled>
            <svg class="icon" aria-hidden="true"><use href="#icon-send"></use></svg><span class="send-label">Send</span>
          </button>
        </div>
      </div>
    </div>
  </main>
</div>

<section class="modal-backdrop hidden" id="staffModal" role="dialog" aria-modal="true" aria-hidden="true" aria-labelledby="staffModalTitle" aria-describedby="staffModalSubtitle">
  <div class="modal-card" role="document" tabindex="-1">
    <div class="modal-head">
      <div><h2 id="staffModalTitle">Staff accounts</h2><div class="modal-subtitle" id="staffModalSubtitle">Manage your support team from one place.</div></div>
      <button class="icon-btn modal-close" id="closeStaffBtn" type="button" aria-label="Close staff accounts" data-tooltip="Close"><svg class="icon" aria-hidden="true"><use href="#icon-x"></use></svg></button>
    </div>
    <div class="modal-body">
      <div class="staff-feedback error-box" id="staffError" role="alert" aria-live="assertive"></div>
      <button class="btn hidden" id="editConflictStaffBtn" type="button"><svg class="icon" aria-hidden="true"><use href="#icon-edit"></use></svg>Edit the existing account</button>
      <div class="staff-feedback success-box" id="staffSuccess" role="status" aria-live="polite"></div>

      <div class="staff-view" id="staffListView">
        <div class="section-title">
          <div><strong>Existing staff</strong><div class="muted">Active and inactive support accounts.</div></div>
          <div class="staff-actions-header"><button class="btn primary" id="createStaffBtn" type="button"><svg class="icon" aria-hidden="true"><use href="#icon-plus"></use></svg>Create staff</button><button class="btn" id="refreshStaffBtn" type="button"><svg class="icon" aria-hidden="true"><use href="#icon-refresh"></use></svg>Refresh</button></div>
        </div>
        <div class="staff-list" role="table" aria-label="Support staff accounts">
          <div class="staff-list-head" role="row"><span role="columnheader">Staff member</span><span role="columnheader">Email</span><span role="columnheader">Role</span><span role="columnheader">Status</span><span role="columnheader">Actions</span></div>
          <div id="staffList" role="rowgroup"><div class="empty-state"><div class="empty-state-inner"><div class="empty-icon"><svg class="icon icon-lg" aria-hidden="true"><use href="#icon-users"></use></svg></div><h3>Loading staff</h3><p>Retrieving support accounts.</p></div></div></div>
        </div>
      </div>

      <div class="staff-view hidden" id="staffCreateView">
        <div class="staff-view-heading"><div><strong>Create staff account</strong><div class="muted">Add a new agent or administrator.</div></div><button class="btn" id="backFromCreateBtn" type="button"><svg class="icon" aria-hidden="true"><use href="#icon-arrow-left"></use></svg>Back to staff</button></div>
        <form id="createStaffForm" novalidate>
          <div class="staff-grid">
            <div class="field"><label for="createStaffName">Name <span class="required-mark" aria-hidden="true">*</span></label><input id="createStaffName" type="text" minlength="2" maxlength="120" autocomplete="off" required /></div>
            <div class="field"><label for="createStaffEmail">Email <span class="required-mark" aria-hidden="true">*</span></label><input id="createStaffEmail" type="email" maxlength="320" autocomplete="off" required /></div>
            <div class="field"><label for="createStaffRole">Role <span class="required-mark" aria-hidden="true">*</span></label><select id="createStaffRole" required><option value="agent">Agent</option><option value="admin">Admin</option></select></div>
            <div></div>
            <div class="field"><label for="createStaffPassword">Initial password <span class="required-mark" aria-hidden="true">*</span></label><input id="createStaffPassword" type="password" minlength="12" maxlength="72" autocomplete="new-password" required aria-describedby="createPasswordHint" /><div class="field-hint" id="createPasswordHint">12 characters minimum; 72 UTF-8 bytes maximum.</div></div>
            <div class="field"><label for="createStaffPasswordConfirm">Confirm password <span class="required-mark" aria-hidden="true">*</span></label><input id="createStaffPasswordConfirm" type="password" minlength="12" maxlength="72" autocomplete="new-password" required aria-describedby="createPasswordMatchError" /><div class="field-error hidden" id="createPasswordMatchError" role="alert"></div></div>
          </div>
          <div class="form-note"><strong>Credential safety:</strong> Passwords are never displayed or returned by the API. New staff must sign in with the initial password you provide.</div>
          <div class="modal-actions"><button class="btn" id="cancelCreateStaffBtn" type="button">Cancel</button><button class="btn primary" id="createStaffSubmitBtn" type="submit"><svg class="icon" aria-hidden="true"><use href="#icon-plus"></use></svg>Create account</button></div>
        </form>
      </div>

      <div class="staff-view hidden" id="staffEditView">
        <div class="staff-view-heading"><div><strong id="editStaffHeading">Edit staff account</strong><div class="muted">Update account details, status, role, or password.</div></div><button class="btn" id="backFromEditBtn" type="button"><svg class="icon" aria-hidden="true"><use href="#icon-arrow-left"></use></svg>Back to staff</button></div>
        <form id="editStaffForm" novalidate>
          <div class="staff-grid">
            <div class="field"><label for="editStaffName">Name <span class="required-mark" aria-hidden="true">*</span></label><input id="editStaffName" type="text" minlength="2" maxlength="120" autocomplete="off" required /></div>
            <div class="field"><label for="editStaffEmail">Email <span class="required-mark" aria-hidden="true">*</span></label><input id="editStaffEmail" type="email" maxlength="320" autocomplete="off" required /></div>
            <div class="field"><label for="editStaffRole">Role <span class="required-mark" aria-hidden="true">*</span></label><select id="editStaffRole" required><option value="agent">Agent</option><option value="admin">Admin</option></select></div>
            <label class="inline-check"><input id="editStaffActive" type="checkbox" checked /> Account active</label>
            <div class="field"><label for="editStaffPassword">New password <span class="muted">(optional)</span></label><input id="editStaffPassword" type="password" minlength="12" maxlength="72" autocomplete="new-password" aria-describedby="editPasswordHint" /><div class="field-hint" id="editPasswordHint">Leave blank to keep the current password.</div></div>
            <div class="field"><label for="editStaffPasswordConfirm">Confirm new password</label><input id="editStaffPasswordConfirm" type="password" minlength="12" maxlength="72" autocomplete="new-password" aria-describedby="editPasswordMatchError" /><div class="field-error hidden" id="editPasswordMatchError" role="alert"></div></div>
          </div>
          <div class="form-note">Credential, role, or activation changes revoke the affected account’s current sessions.</div>
          <div class="modal-actions"><button class="btn" id="cancelEditStaffBtn" type="button">Cancel</button><button class="btn primary" id="editStaffSubmitBtn" type="submit"><svg class="icon" aria-hidden="true"><use href="#icon-check"></use></svg>Save changes</button></div>
        </form>
      </div>
    </div>
  </div>
</section>

<section class="modal-backdrop hidden" id="transferModal" role="dialog" aria-modal="true" aria-hidden="true" aria-labelledby="transferModalTitle" aria-describedby="transferModalSubtitle">
  <div class="modal-card transfer-card" role="document" tabindex="-1">
    <div class="modal-head">
      <div><h2 id="transferModalTitle">Transfer ticket</h2><div class="modal-subtitle" id="transferModalSubtitle">Assign this conversation to another staff member.</div></div>
      <button class="icon-btn modal-close" id="closeTransferBtn" type="button" aria-label="Close transfer window" data-tooltip="Close"><svg class="icon" aria-hidden="true"><use href="#icon-x"></use></svg></button>
    </div>
    <div class="modal-body">
      <div class="staff-feedback error-box" id="transferError" role="alert" aria-live="assertive"></div>
      <div class="transfer-summary">After transfer, you can keep watching the conversation, but only the new assignee can send messages.</div>
      <div class="field"><label for="transferTarget">Transfer to</label><select id="transferTarget"></select></div>
      <div class="form-note">The new assignee may transfer the ticket again or resolve it and return the customer to AI.</div>
      <div class="modal-actions"><button class="btn" id="cancelTransferBtn" type="button">Cancel</button><button class="btn primary" id="confirmTransferBtn" type="button"><svg class="icon" aria-hidden="true"><use href="#icon-transfer"></use></svg>Transfer ticket</button></div>
    </div>
  </div>
</section>

<div class="toast-region" id="toastRegion" aria-live="polite" aria-atomic="false"></div>
<div class="sr-only" id="appStatus" role="status" aria-live="polite" aria-atomic="true"></div>
<script>
const QUEUE_CONFIG = {
  unassigned: {label: 'Unassigned', emptyTitle: 'No unassigned tickets', emptyBody: 'New customer escalations will appear here when they need an owner.', icon: 'inbox'},
  mine: {label: 'My tickets', emptyTitle: 'No tickets assigned to you', emptyBody: 'Claim an unassigned ticket or wait for a transfer from another agent.', icon: 'user-check'},
  watching: {label: 'Watching', emptyTitle: 'No watched conversations', emptyBody: 'Tickets you previously transferred remain here while they are active.', icon: 'eye'},
  active: {label: 'Active', emptyTitle: 'No active tickets', emptyBody: 'There are no active conversations available in this view.', icon: 'activity'},
  waiting: {label: 'Waiting', emptyTitle: 'Nothing is waiting', emptyBody: 'Tickets waiting on a customer or internal follow-up will appear here.', icon: 'clock'},
  history: {label: 'History', emptyTitle: 'No ticket history', emptyBody: 'Resolved and closed conversations will appear here.', icon: 'history'}
};
const STATUS_LABELS = {
  NEW: 'New',
  ASSIGNED: 'Assigned',
  IN_PROGRESS: 'In progress',
  WAITING_CUSTOMER: 'Waiting for customer',
  WAITING_INTERNAL: 'Waiting internally',
  RESOLVED: 'Resolved',
  CLOSED: 'Closed'
};
const MESSAGE_ROLE_LABELS = {
  CUSTOMER: 'Customer',
  AGENT: 'Support agent',
  AI: 'AI assistant',
  SYSTEM: 'System'
};
const state = {
  view: 'unassigned',
  page: 1,
  pageSize: 30,
  total: 0,
  queueItems: [],
  queueTotals: {},
  query: '',
  queueRequest: 0,
  ticketRequest: 0,
  ticketId: null,
  currentTicketData: null,
  timelineSignature: '',
  agent: null,
  busy: false,
  authenticated: false,
  staffBusy: false,
  staffItems: [],
  staffView: 'list',
  editingStaffId: null,
  editingStaffOriginal: null,
  conflictStaff: null,
  composerEnabled: false,
  pendingMedia: null,
  mediaRecorder: null,
  mediaStream: null,
  recordingChunks: [],
  recordingTimer: null,
  recordingStartedAt: 0,
  discardRecording: false,
  transferBusy: false,
  transferTargets: [],
  activeModal: null,
  lastFocused: null,
  contextOpen: false
};

const $ = id => document.getElementById(id);

function createIcon(name, className = 'icon') {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('class', className);
  svg.setAttribute('aria-hidden', 'true');
  const use = document.createElementNS('http://www.w3.org/2000/svg', 'use');
  use.setAttribute('href', '#icon-' + name);
  svg.append(use);
  return svg;
}

function setIcon(button, name) {
  const use = button && button.querySelector('use');
  if (use) use.setAttribute('href', '#icon-' + name);
}

function cookie(name) {
  return document.cookie.split(';').map(value => value.trim()).find(value => value.startsWith(name + '='))?.slice(name.length + 1) || '';
}

function csrf() {
  return decodeURIComponent(cookie('__CSRF_COOKIE__'));
}

function text(element, value) {
  if (element) element.textContent = value ?? '';
}

function initials(value) {
  const words = String(value || '').trim().split(/\s+/).filter(Boolean);
  if (!words.length) return '—';
  if (words.length === 1) return words[0].slice(0, 2).toUpperCase();
  return (words[0][0] + words[words.length - 1][0]).toUpperCase();
}

function fmt(value) {
  if (!value) return '—';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return new Intl.DateTimeFormat(undefined, {dateStyle: 'medium', timeStyle: 'short'}).format(date);
}

function relativeTime(value) {
  if (!value) return '—';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '—';
  const seconds = Math.round((date.getTime() - Date.now()) / 1000);
  const absolute = Math.abs(seconds);
  const formatter = new Intl.RelativeTimeFormat(undefined, {numeric: 'auto', style: 'narrow'});
  if (absolute < 60) return formatter.format(seconds, 'second');
  if (absolute < 3600) return formatter.format(Math.round(seconds / 60), 'minute');
  if (absolute < 86400) return formatter.format(Math.round(seconds / 3600), 'hour');
  if (absolute < 604800) return formatter.format(Math.round(seconds / 86400), 'day');
  return new Intl.DateTimeFormat(undefined, {month: 'short', day: 'numeric'}).format(date);
}

function formatStatus(value) {
  return STATUS_LABELS[String(value || '').toUpperCase()] || String(value || 'Unknown').replaceAll('_', ' ').toLowerCase().replace(/^./, char => char.toUpperCase());
}

function statusClass(value) {
  return 'status-' + String(value || 'default').toLowerCase().replaceAll('_', '-');
}

function statusBadge(value) {
  const badge = document.createElement('span');
  badge.className = 'status-badge ' + statusClass(value);
  badge.textContent = formatStatus(value);
  return badge;
}

function formatBytes(bytes) {
  const value = Number(bytes || 0);
  if (value < 1024) return value + ' B';
  if (value < 1024 * 1024) return (value / 1024).toFixed(1) + ' KB';
  return (value / (1024 * 1024)).toFixed(1) + ' MB';
}

function setAppStatus(message) {
  const element = $('appStatus');
  element.textContent = '';
  window.setTimeout(() => { element.textContent = message || ''; }, 20);
}

function showLoginError(message) {
  const element = $('loginError');
  element.textContent = message || '';
  element.style.display = message ? 'block' : 'none';
}

function showError(message) {
  const banner = $('error');
  text($('errorText'), message || '');
  banner.classList.toggle('visible', Boolean(message));
}

function notify(message, type = 'success') {
  if (!message) return;
  const toast = document.createElement('div');
  toast.className = 'toast ' + type;
  toast.setAttribute('role', type === 'error' ? 'alert' : 'status');
  toast.append(createIcon(type === 'error' ? 'alert' : 'check-circle'));
  const copy = document.createElement('div');
  copy.className = 'toast-copy';
  copy.textContent = message;
  const close = document.createElement('button');
  close.type = 'button';
  close.className = 'icon-btn';
  close.setAttribute('aria-label', 'Dismiss notification');
  close.append(createIcon('x', 'icon icon-sm'));
  close.onclick = () => toast.remove();
  toast.append(copy, close);
  $('toastRegion').append(toast);
  window.setTimeout(() => toast.remove(), 5200);
}

function setButtonBusy(button, busy, busyLabel = 'Working…') {
  if (!button) return;
  if (busy) {
    if (!button.dataset.originalHtml) button.dataset.originalHtml = button.innerHTML;
    button.disabled = true;
    button.setAttribute('aria-busy', 'true');
    button.replaceChildren(createIcon('refresh'), document.createTextNode(busyLabel));
  } else {
    button.disabled = false;
    button.removeAttribute('aria-busy');
    if (button.dataset.originalHtml) {
      button.innerHTML = button.dataset.originalHtml;
      delete button.dataset.originalHtml;
    }
  }
}

function currentTheme() {
  return document.documentElement.dataset.theme === 'dark' ? 'dark' : 'light';
}

function syncThemeControls() {
  const dark = currentTheme() === 'dark';
  const nextLabel = dark ? 'Switch to light theme' : 'Switch to dark theme';
  for (const id of ['themeBtn', 'loginThemeBtn']) {
    const button = $(id);
    if (!button) continue;
    button.setAttribute('aria-label', nextLabel);
    setIcon(button, dark ? 'sun' : 'moon');
  }
  const themeMeta = document.querySelector('meta[name="theme-color"]');
  if (themeMeta) themeMeta.setAttribute('content', dark ? '#0b1220' : '#f4f7fb');
}

function toggleTheme() {
  const next = currentTheme() === 'dark' ? 'light' : 'dark';
  document.documentElement.dataset.theme = next;
  try { localStorage.setItem('shipment-support-theme', next); } catch (_) {}
  syncThemeControls();
  setAppStatus(next === 'dark' ? 'Dark theme enabled.' : 'Light theme enabled.');
}

function focusableElements(container) {
  return [...container.querySelectorAll('button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])')].filter(element => !element.closest('.hidden') && element.offsetParent !== null);
}

function openDialog(id, focusId) {
  const modal = $(id);
  if (!modal) return;
  state.lastFocused = document.activeElement;
  state.activeModal = id;
  modal.classList.remove('hidden');
  modal.setAttribute('aria-hidden', 'false');
  document.body.classList.add('modal-open');
  window.setTimeout(() => {
    const target = focusId ? $(focusId) : null;
    const fallback = focusableElements(modal)[0] || modal.querySelector('.modal-card');
    (target || fallback)?.focus();
  }, 20);
}

function closeDialog(id, restoreFocus = true) {
  const modal = $(id);
  if (!modal) return;
  modal.classList.add('hidden');
  modal.setAttribute('aria-hidden', 'true');
  if (state.activeModal === id) state.activeModal = null;
  if (!state.activeModal) document.body.classList.remove('modal-open');
  if (restoreFocus && state.lastFocused && typeof state.lastFocused.focus === 'function') {
    const target = state.lastFocused;
    state.lastFocused = null;
    window.setTimeout(() => target.focus(), 10);
  }
}

function openContextPanel() {
  state.contextOpen = true;
  $('contextPanel').classList.add('open');
  $('contextBackdrop').classList.add('visible');
  $('contextToggleBtn').setAttribute('aria-expanded', 'true');
  window.setTimeout(() => $('closeContextBtn').focus(), 20);
}

function closeContextPanel(restoreFocus = true) {
  state.contextOpen = false;
  $('contextPanel').classList.remove('open');
  $('contextBackdrop').classList.remove('visible');
  $('contextToggleBtn').setAttribute('aria-expanded', 'false');
  if (restoreFocus && window.innerWidth <= 1100) $('contextToggleBtn').focus();
}

function handleGlobalKeydown(event) {
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'k' && state.authenticated && !state.activeModal) {
    event.preventDefault();
    $('queueSearch').focus();
    return;
  }
  if (event.key === 'Escape') {
    if (state.contextOpen) {
      event.preventDefault();
      closeContextPanel();
      return;
    }
    if (state.activeModal === 'transferModal') {
      event.preventDefault();
      closeTransferModal();
      return;
    }
    if (state.activeModal === 'staffModal') {
      event.preventDefault();
      closeStaffModal();
      return;
    }
  }
  if (event.key === 'Tab' && state.activeModal) {
    const modal = $(state.activeModal);
    const items = focusableElements(modal);
    if (!items.length) return;
    const first = items[0];
    const last = items[items.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  }
}

async function api(path, options = {}) {
  const headers = {...(options.headers || {})};
  if (options.body && !(options.body instanceof FormData)) headers['Content-Type'] = 'application/json';
  if (options.method && options.method !== 'GET') headers['X-CSRF-Token'] = csrf();
  const response = await fetch(path, {credentials: 'same-origin', ...options, cache:'no-store', headers});
  let data = {};
  try { data = await response.json(); } catch (_) {}
  if (response.status === 401 && path !== '/api/support/login') {
    showLogin();
    throw new Error('Your staff session expired. Please sign in again.');
  }
  if (!response.ok) {
    const detail = data.detail;
    const message = typeof detail === 'string' ? detail : (detail && detail.message) || ('Request failed (' + response.status + ')');
    const error = new Error(message);
    error.detail = detail;
    error.status = response.status;
    throw error;
  }
  return data;
}

function setAgentIdentity(agent) {
  const name = agent?.name || 'Support agent';
  text($('agentName'), name);
  text($('agentLabel'), agent?.role || 'agent');
  text($('agentAvatar'), initials(name));
}

function canManageStaff() {
  return Boolean(state.agent && (state.agent.can_manage_staff || ['supervisor', 'admin'].includes(state.agent.role)));
}

function configureStaffAccess() {
  $('manageStaffBtn').classList.toggle('hidden', !canManageStaff());
}

function emptyMarkup(title, body, iconName = 'inbox') {
  const outer = document.createElement('div');
  outer.className = 'empty-state';
  const inner = document.createElement('div');
  inner.className = 'empty-state-inner';
  const icon = document.createElement('div');
  icon.className = 'empty-icon';
  icon.append(createIcon(iconName, 'icon icon-lg'));
  const heading = document.createElement('h3');
  heading.textContent = title;
  const paragraph = document.createElement('p');
  paragraph.textContent = body;
  inner.append(icon, heading, paragraph);
  outer.append(inner);
  return {outer, inner};
}

function renderQueueSkeleton() {
  const container = document.createElement('div');
  container.className = 'loading-list';
  for (let index = 0; index < 6; index += 1) {
    const row = document.createElement('div');
    row.className = 'skeleton-row';
    for (const size of ['medium', 'long', 'short']) {
      const line = document.createElement('div');
      line.className = 'skeleton skeleton-line ' + size;
      row.append(line);
    }
    container.append(row);
  }
  $('queue').replaceChildren(container);
}

function clearTicketWorkspace() {
  state.ticketId = null;
  state.currentTicketData = null;
  state.timelineSignature = '';
  state.composerEnabled = false;
  state.queueItems = [];
  state.total = 0;
  $('queue').replaceChildren(emptyMarkup('No tickets loaded', 'Your selected queue will appear here after sign-in.', 'inbox').outer);
  text($('ticketTitle'), 'Select a ticket');
  text($('ticketSubtitle'), 'Choose a conversation from the queue to begin.');
  $('ticketHeadingStatus').replaceChildren();
  $('ticketHeaderMeta').replaceChildren();
  $('conversation').innerHTML = '<div class="conversation-empty"><div class="empty-state-inner"><div class="empty-icon"><svg class="icon icon-lg" aria-hidden="true"><use href="#icon-message"></use></svg></div><h3>Open a customer conversation</h3><p>Ticket history, messages, and activity will appear here.</p></div></div>';
  $('context').innerHTML = '<div class="empty-state"><div class="empty-state-inner"><div class="empty-icon"><svg class="icon icon-lg" aria-hidden="true"><use href="#icon-user"></use></svg></div><h3>No ticket selected</h3><p>Customer and shipment context will appear here.</p></div></div>';
  $('actions').replaceChildren();
  $('readOnlyBanner').textContent = '';
  $('readOnlyBanner').classList.add('hidden');
  $('message').value = '';
  text($('ticketAvatar'), '—');
  showError('');
  updateQueueSummary();
  setComposerEnabled(false);
  closeContextPanel(false);
  setMobileTicketOpen(false);
}

function showLogin() {
  state.authenticated = false;
  state.agent = null;
  state.staffItems = [];
  state.editingStaffId = null;
  state.composerEnabled = false;
  state.queueRequest += 1;
  state.ticketRequest += 1;
  if (state.mediaRecorder) cancelVoiceRecording();
  clearMediaDraft();
  clearTicketWorkspace();$('dashboard').classList.add('hidden');
  $('loginPage').classList.remove('hidden');
  closeDialog('staffModal', false);
  closeDialog('transferModal', false);
  $('manageStaffBtn').classList.add('hidden');
  $('password').value = '';
  showLoginError('');
  window.setTimeout(() => $('email').focus(), 20);
}

function showDashboard() {
  state.authenticated = true;
  $('loginPage').classList.add('hidden');
  $('dashboard').classList.remove('hidden');
  configureStaffAccess();
}

async function login(event) {
  event.preventDefault();
  const form = $('loginForm');
  if (state.busy || !form.reportValidity()) return;
  state.busy = true;
  setButtonBusy($('loginBtn'), true, 'Signing in…');
  showLoginError('');
  try {
    state.agent = await api('/api/support/login', {method:'POST', body: JSON.stringify({email: $('email').value.trim(), password: $('password').value})});
    setAgentIdentity(state.agent);
    showDashboard();
    await loadQueue();
    setAppStatus('Signed in to Shipment Support.');
  } catch (error) {
    showLoginError(error.message);
    $('password').select();
  } finally {
    state.busy = false;
    setButtonBusy($('loginBtn'), false);
  }
}

async function bootstrap() {
  syncThemeControls();
  try {
    state.agent = await api('/api/support/me');
    setAgentIdentity(state.agent);
    showDashboard();
    await loadQueue();
  } catch (_) {
    showLogin();
  }
}

async function logout() {
  if (state.busy) return;
  state.busy = true;
  const button = $('logoutBtn');
  button.disabled = true;
  try {
    await api('/api/support/logout', {method:'POST'});
  } catch (_) {
  } finally {
    state.busy = false;
    button.disabled = false;
    showLogin();
  }
}

function updateQueueCount(view, total) {
  state.queueTotals[view] = Number(total || 0);
  const badge = document.querySelector('[data-count-for="' + view + '"]');
  if (badge) badge.textContent = state.queueTotals[view] > 0 ? String(state.queueTotals[view]) : '';
}

function updateQueueSummary(visibleCount = null) {
  const config = QUEUE_CONFIG[state.view] || QUEUE_CONFIG.unassigned;
  text($('queueTitle'), config.label);
  const total = Number(state.total || 0);
  const first = total ? ((state.page - 1) * state.pageSize) + 1 : 0;
  const last = total ? Math.min(state.page * state.pageSize, total) : 0;
  const searchCount = visibleCount === null ? state.queueItems.length : visibleCount;
  if (state.query) {
    text($('queueTotal'), searchCount + ' match' + (searchCount === 1 ? '' : 'es'));
  } else {
    text($('queueTotal'), total + ' ticket' + (total === 1 ? '' : 's'));
  }
  const pages = Math.max(1, Math.ceil(total / state.pageSize));
  text($('pageSummary'), total ? ('Showing ' + first + '–' + last + ' of ' + total + ' · Page ' + state.page + ' of ' + pages) : 'Page 1');
  $('previousPageBtn').disabled = state.page <= 1;
  $('nextPageBtn').disabled = state.page >= pages || total === 0;
}

function queueSearchText(ticket) {
  return [
    ticket.ticket_reference,
    ticket.customer_name,
    ticket.customer_userid,
    ticket.whatsapp_phone,
    ticket.ai_summary,
    ticket.reason,
    ticket.status,
    ticket.tracking_reference,
    ticket.requested_action,
    ticket.current_assignee_name,
    ticket.business_display_phone_number,
    ticket.business_phone_number_id
  ].filter(Boolean).join(' ').toLocaleLowerCase();
}

function supportNumberLabel(ticket) {
  if (ticket && ticket.business_display_phone_number) return ticket.business_display_phone_number;
  if (ticket && ticket.business_phone_number_id) return 'WhatsApp number ID ' + ticket.business_phone_number_id;
  return 'Default WhatsApp number';
}

function createMetaChip(label, iconName, className = 'meta-chip') {
  const chip = document.createElement('span');
  chip.className = className;
  if (iconName) chip.append(createIcon(iconName, 'icon icon-sm'));
  const copy = document.createElement('span');
  copy.textContent = label;
  chip.append(copy);
  chip.title = label;
  return chip;
}

function createTicketRow(ticket) {
  const selected = String(state.ticketId || '') === String(ticket.id || '');
  const row = document.createElement('button');
  row.type = 'button';
  row.className = 'ticket-row' + (selected ? ' active' : '');
  row.setAttribute('role', 'option');
  row.setAttribute('aria-selected', selected ? 'true' : 'false');
  row.dataset.ticketId = String(ticket.id);

  const head = document.createElement('div');
  head.className = 'ticket-row-head';
  const avatar = document.createElement('div');
  avatar.className = 'avatar customer';
  avatar.setAttribute('aria-hidden', 'true');
  const customer = ticket.customer_name || ticket.customer_userid || ticket.whatsapp_phone || 'Unknown customer';
  avatar.textContent = initials(customer);

  const copy = document.createElement('div');
  copy.className = 'ticket-row-copy';
  const name = document.createElement('div');
  name.className = 'ticket-customer';
  name.dir = 'auto';
  name.textContent = customer;
  const reference = document.createElement('div');
  reference.className = 'ticket-reference';
  reference.textContent = ticket.ticket_reference || ('Ticket #' + ticket.id);
  copy.append(name, reference);

  const time = document.createElement('time');
  time.className = 'ticket-time';
  time.dateTime = ticket.last_activity_at || '';
  time.title = fmt(ticket.last_activity_at);
  time.textContent = relativeTime(ticket.last_activity_at);
  head.append(avatar, copy, time);

  const summary = document.createElement('div');
  summary.className = 'ticket-summary';
  summary.dir = 'auto';
  summary.textContent = ticket.ai_summary || ticket.reason || ticket.requested_action || 'No summary available.';

  const meta = document.createElement('div');
  meta.className = 'ticket-row-meta';
  meta.append(statusBadge(ticket.status));
  meta.append(createMetaChip(supportNumberLabel(ticket), 'message', 'channel-pill'));
  if (ticket.current_assignee_name) meta.append(createMetaChip(ticket.current_assignee_name, 'user'));
  if (ticket.tracking_reference) meta.append(createMetaChip(ticket.tracking_reference, 'truck'));

  row.append(head, summary, meta);
  row.onclick = () => openTicket(ticket.id);
  return row;
}

function renderQueue() {
  const queue = $('queue');
  const query = state.query.trim().toLocaleLowerCase();
  const items = query ? state.queueItems.filter(ticket => queueSearchText(ticket).includes(query)) : state.queueItems;
  queue.replaceChildren();

  if (!state.queueItems.length) {
    const config = QUEUE_CONFIG[state.view] || QUEUE_CONFIG.unassigned;
    queue.append(emptyMarkup(config.emptyTitle, config.emptyBody, config.icon).outer);
  } else if (!items.length) {
    const empty = emptyMarkup('No tickets match this search', 'Try a customer name, ticket reference, shipment number, status, or support line.', 'search');
    const clear = document.createElement('button');
    clear.type = 'button';
    clear.className = 'btn';
    clear.append(createIcon('x'), document.createTextNode('Clear search'));
    clear.onclick = clearQueueSearch;
    empty.inner.append(clear);
    queue.append(empty.outer);
  } else {
    const fragment = document.createDocumentFragment();
    for (const ticket of items) fragment.append(createTicketRow(ticket));
    queue.append(fragment);
  }

  updateQueueSummary(items.length);
  $('clearQueueSearchBtn').classList.toggle('hidden', !state.query);
}

function renderQueueError(message) {
  const empty = emptyMarkup('Could not load this queue', message || 'Check your connection and try again.', 'alert');
  const retry = document.createElement('button');
  retry.type = 'button';
  retry.className = 'btn primary';
  retry.append(createIcon('refresh'), document.createTextNode('Retry'));
  retry.onclick = () => loadQueue();
  empty.inner.append(retry);
  $('queue').replaceChildren(empty.outer);
}

async function loadQueue({silent = false} = {}) {
  if (!state.authenticated) return;
  const requestId = ++state.queueRequest;
  const queue = $('queue');
  const previousScrollTop = queue.scrollTop;
  queue.setAttribute('aria-busy', 'true');
  $('refreshQueueBtn').disabled = true;
  if (!silent) renderQueueSkeleton();
  try {
    const path = '/api/support/tickets?view=' + encodeURIComponent(state.view) + '&page=' + state.page + '&page_size=' + state.pageSize;
    const data = await api(path);
    if (requestId !== state.queueRequest) return;
    const total = Number(data.total || 0);
    const pageCount = Math.max(1, Math.ceil(total / state.pageSize));
    if (state.page > pageCount) {
      state.page = pageCount;
      await loadQueue({silent: false});
      return;
    }
    state.queueItems = Array.isArray(data.items) ? data.items : [];
    state.total = total;
    updateQueueCount(state.view, total);
    renderQueue();
    if (silent) queue.scrollTop = previousScrollTop;
  } catch (error) {
    if (requestId !== state.queueRequest) return;
    renderQueueError(error.message);
    text($('queueTotal'), 'Unavailable');
  } finally {
    if (requestId === state.queueRequest) {
      queue.setAttribute('aria-busy', 'false');
      $('refreshQueueBtn').disabled = false;
    }
  }
}

function setQueueView(view) {
  if (!QUEUE_CONFIG[view] || state.view === view) return;
  state.view = view;
  state.page = 1;
  state.query = '';
  $('queueSearch').value = '';
  for (const button of $('tabs').querySelectorAll('button[data-view]')) {
    const active = button.dataset.view === view;
    button.classList.toggle('active', active);
    button.setAttribute('aria-current', active ? 'page' : 'false');
  }
  updateQueueSummary();
  loadQueue();
}

function clearQueueSearch() {
  state.query = '';
  $('queueSearch').value = '';
  renderQueue();
  $('queueSearch').focus();
}

function changePage(direction) {
  const nextPage = state.page + direction;
  const pages = Math.max(1, Math.ceil(state.total / state.pageSize));
  if (nextPage < 1 || nextPage > pages) return;
  state.page = nextPage;
  state.query = '';
  $('queueSearch').value = '';
  loadQueue().then(() => $('queue').focus({preventScroll: true}));
}

function handleQueueKeydown(event) {
  const current = event.target.closest('.ticket-row');
  if (!current || !['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) return;
  const rows = [...$('queue').querySelectorAll('.ticket-row')];
  const index = rows.indexOf(current);
  let target = null;
  if (event.key === 'ArrowDown') target = rows[Math.min(rows.length - 1, index + 1)];
  if (event.key === 'ArrowUp') target = rows[Math.max(0, index - 1)];
  if (event.key === 'Home') target = rows[0];
  if (event.key === 'End') target = rows[rows.length - 1];
  if (target) {
    event.preventDefault();
    target.focus();
  }
}

function setMobileTicketOpen(open) {
  document.body.classList.toggle('mobile-ticket-open', Boolean(open));
}

function closeMobileTicket() {
  setMobileTicketOpen(false);
  window.setTimeout(() => {
    const selected = $('queue').querySelector('.ticket-row.active');
    (selected || $('queueSearch')).focus();
  }, 20);
}

function messageActor(message) {
  const type = String(message.sender_type || message.role || '').toUpperCase();
  if (message.sender_name) return message.sender_name;
  return MESSAGE_ROLE_LABELS[type] || (type ? formatStatus(type) : 'Message');
}

function messageRole(message) {
  const type = String(message.sender_type || message.role || '').toUpperCase();
  if (type === 'CUSTOMER') return 'Customer';
  if (type === 'AI') return 'AI assistant';
  if (type === 'AGENT') return 'Agent reply';
  return type ? formatStatus(type) : 'Message';
}

function messageClass(message) {
  const type = String(message.sender_type || message.role || '').toUpperCase();
  if (type === 'CUSTOMER') return 'customer';
  if (type === 'AI') return 'ai';
  if (type === 'AGENT') return 'agent';
  return 'system';
}

function addMessage(container, m, extraClass = '') {
  const outbound = m.direction === 'OUTBOUND';
  const row = document.createElement('article');
  row.className = 'message-row ' + (outbound ? 'outbound ' : 'inbound ') + messageClass(m) + (extraClass ? ' ' + extraClass : '');

  const avatar = document.createElement('div');
  avatar.className = 'avatar message-avatar ' + (messageClass(m) === 'customer' ? 'customer' : messageClass(m) === 'ai' ? 'ai' : '');
  avatar.setAttribute('aria-hidden', 'true');
  avatar.textContent = initials(messageActor(m));

  const stack = document.createElement('div');
  stack.className = 'message-stack';
  const bubble = document.createElement('div');
  bubble.className = 'message-bubble';

  const sender = document.createElement('div');
  sender.className = 'message-sender';
  sender.append(document.createTextNode(messageActor(m)));
  const role = document.createElement('span');
  role.className = 'role-badge';
  role.textContent = messageRole(m);
  sender.append(role);
  bubble.append(sender);

  const kind = m.media_kind || m.message_type || '';
  if (['image', 'audio'].includes(kind)) {
    const media = document.createElement('div');
    media.className = 'msg-media';
    if (m.media_url) {
      if (kind === 'image') {
        const open = document.createElement('button');
        open.type = 'button';
        open.className = 'media-open';
        open.setAttribute('aria-label', 'Open image from ' + messageActor(m) + ' in a new tab');
        const img = document.createElement('img');
        img.src = m.media_url;
        img.alt = 'Image from ' + messageActor(m);
        img.loading = 'lazy';
        img.onerror = () => {
          const unavailable = document.createElement('div');
          unavailable.className = 'media-unavailable';
          unavailable.textContent = 'This image could not be loaded.';
          open.replaceWith(unavailable);
        };
        open.onclick = () => window.open(m.media_url, '_blank', 'noopener,noreferrer');
        open.append(img);
        media.append(open);
      } else {
        const audio = document.createElement('audio');
        audio.controls = true;
        audio.preload = 'metadata';
        audio.src = m.media_url;
        audio.setAttribute('aria-label', 'Voice note from ' + messageActor(m));
        audio.onerror = () => {
          const unavailable = document.createElement('div');
          unavailable.className = 'media-unavailable';
          unavailable.textContent = 'This voice note could not be loaded.';
          audio.replaceWith(unavailable);
        };
        media.append(audio);
      }
    } else {
      const unavailable = document.createElement('div');
      unavailable.className = 'media-unavailable';
      unavailable.textContent = kind === 'image' ? 'Image unavailable' : 'Voice note unavailable';
      media.append(unavailable);
    }
    bubble.append(media);
  }

  const bodyText = m.body || m.content || '';
  const defaultMediaLabel = ['Image', 'Voice note', 'Audio message'].includes(bodyText);
  if (bodyText && !(m.media_url && defaultMediaLabel)) {
    const body = document.createElement('div');
    body.className = 'message-body';
    body.dir = 'auto';
    body.textContent = bodyText;
    bubble.append(body);
  }

  const meta = document.createElement('div');
  meta.className = 'message-meta';
  const time = document.createElement('time');
  time.dateTime = m.created_at || '';
  time.title = fmt(m.created_at);
  time.textContent = m.created_at ? fmt(m.created_at) : (extraClass ? 'Earlier conversation context' : 'Time unavailable');
  meta.append(time);
  if (m.send_status === 'FAILED') {
    const failed = document.createElement('span');
    failed.className = 'send-state failed';
    failed.append(createIcon('alert', 'icon icon-sm'), document.createTextNode('Send failed' + (m.error_message ? ': ' + m.error_message : '')));
    meta.append(failed);
  } else if (m.send_status === 'PENDING') {
    const pending = document.createElement('span');
    pending.className = 'send-state pending';
    pending.append(createIcon('clock', 'icon icon-sm'), document.createTextNode('Sending'));
    meta.append(pending);
  } else if (outbound && m.send_status) {
    const sent = document.createElement('span');
    sent.className = 'send-state';
    sent.append(createIcon('check', 'icon icon-sm'), document.createTextNode(formatStatus(m.send_status)));
    meta.append(sent);
  }

  stack.append(bubble, meta);
  if (outbound) row.append(stack, avatar);
  else row.append(avatar, stack);
  container.append(row);
}

function addEvent(container, event) {
  const element = document.createElement('div');
  element.className = 'event';
  let label = String(event.event_type || 'EVENT').replaceAll('_', ' ');
  if (event.event_type === 'TICKET_TRANSFERRED') {
    try {
      const details = typeof event.details_json === 'string' ? JSON.parse(event.details_json) : (event.details_json || {});
      if (details.from_agent_name && details.to_agent_name) label = 'Transferred: ' + details.from_agent_name + ' → ' + details.to_agent_name;
    } catch (_) {}
  }
  const actor = event.actor_name || event.actor_type || 'System';
  element.textContent = formatStatus(label) + ' · ' + actor + ' · ' + fmt(event.created_at);
  container.append(element);
}

function dayKey(value) {
  if (!value) return '';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '';
  return date.getFullYear() + '-' + date.getMonth() + '-' + date.getDate();
}

function dayLabel(value) {
  if (!value) return '';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '';
  const today = new Date();
  const yesterday = new Date();
  yesterday.setDate(today.getDate() - 1);
  if (dayKey(date) === dayKey(today)) return 'Today';
  if (dayKey(date) === dayKey(yesterday)) return 'Yesterday';
  return new Intl.DateTimeFormat(undefined, {weekday: 'short', month: 'short', day: 'numeric', year: date.getFullYear() === today.getFullYear() ? undefined : 'numeric'}).format(date);
}

function addDateDivider(container, value) {
  const divider = document.createElement('div');
  divider.className = 'date-divider';
  divider.textContent = dayLabel(value);
  container.append(divider);
}

function timelineSignature(data) {
  const messages = (data.messages || []).map(message => [message.id, message.updated_at, message.send_status, message.error_message, message.media_available, message.body]);
  const events = (data.events || []).map(event => [event.id, event.event_type, event.created_at, event.details_json]);
  const recent = ((data.context || {}).recent_messages || []).map(message => [message.role, message.content]);
  return JSON.stringify([data.ticket?.id, messages, events, recent]);
}

function renderConversation(data, {forceScroll = false} = {}) {
  const conversation = $('conversation');
  const nearBottom = conversation.scrollHeight - conversation.scrollTop - conversation.clientHeight < 140;
  const previousScrollTop = conversation.scrollTop;
  const inner = document.createElement('div');
  inner.className = 'conversation-inner';

  const recent = ((data.context || {}).recent_messages || []).filter(message => ['user', 'assistant'].includes(message.role) && message.content);
  const firstInbound = (data.messages || []).find(message => message.sender_type === 'CUSTOMER' && message.direction === 'INBOUND');
  if (firstInbound && recent.length && recent[recent.length - 1].role === 'user' && recent[recent.length - 1].content === firstInbound.body) recent.pop();
  if (recent.length) {
    const divider = document.createElement('div');
    divider.className = 'context-divider';
    divider.append(createIcon('history', 'icon icon-sm'), document.createTextNode('Earlier AI conversation context'));
    inner.append(divider);
    for (const message of recent) {
      addMessage(inner, {body: message.content, sender_type: message.role === 'user' ? 'CUSTOMER' : 'AI', direction: message.role === 'user' ? 'INBOUND' : 'OUTBOUND'}, 'context-msg');
    }
  }

  const timeline = [
    ...(data.messages || []).map(item => ({kind: 'message', at: item.created_at, data: item})),
    ...(data.events || []).map(item => ({kind: 'event', at: item.created_at, data: item}))
  ].sort((left, right) => String(left.at || '').localeCompare(String(right.at || '')));

  let currentDay = '';
  for (const item of timeline) {
    const itemDay = dayKey(item.at);
    if (itemDay && itemDay !== currentDay) {
      currentDay = itemDay;
      addDateDivider(inner, item.at);
    }
    if (item.kind === 'message') addMessage(inner, item.data);
    else addEvent(inner, item.data);
  }

  if (!recent.length && !timeline.length) {
    const empty = emptyMarkup('No messages yet', 'The customer conversation has no message history to display.', 'message');
    empty.outer.classList.add('conversation-empty');
    conversation.replaceChildren(empty.outer);
  } else {
    conversation.replaceChildren(inner);
  }

  if (forceScroll || nearBottom) conversation.scrollTop = conversation.scrollHeight;
  else conversation.scrollTop = previousScrollTop;
  updateScrollLatestButton();
}

function updateScrollLatestButton() {
  const conversation = $('conversation');
  const away = conversation.scrollHeight - conversation.scrollTop - conversation.clientHeight > 220;
  $('scrollToLatestBtn').classList.toggle('hidden', !away);
}

function contextValue(value) {
  if (value === true) return 'Yes';
  if (value === false) return 'No';
  if (value === null || value === undefined || value === '') return '—';
  return String(value);
}

function appendContextCard(container, title, entries, {summary = false} = {}) {
  const card = document.createElement('section');
  card.className = 'context-card';
  const heading = document.createElement('h3');
  heading.textContent = title;
  card.append(heading);
  const available = entries.filter(([, value]) => value !== null && value !== undefined && value !== '');
  if (!available.length) {
    const empty = document.createElement('div');
    empty.className = 'context-empty';
    empty.textContent = 'No information available.';
    card.append(empty);
  } else if (summary) {
    const copy = document.createElement('div');
    copy.className = 'context-summary';
    copy.dir = 'auto';
    copy.textContent = contextValue(available[0][1]);
    card.append(copy);
  } else {
    const list = document.createElement('dl');
    list.className = 'context-list';
    for (const [label, value, kind] of available) {
      const item = document.createElement('div');
      item.className = 'context-item';
      const term = document.createElement('dt');
      term.textContent = label;
      const description = document.createElement('dd');
      description.dir = 'auto';
      description.textContent = contextValue(value);
      if (kind === 'code') description.classList.add('code');
      item.append(term, description);
      list.append(item);
    }
    card.append(list);
  }
  container.append(card);
}

function renderContext(ticket, context, shipment) {
  const container = $('context');
  const previousScrollTop = $('contextPanel').scrollTop;
  container.replaceChildren();
  const customer = ticket.customer_name || ticket.customer_userid || ticket.whatsapp_phone || 'Unknown customer';
  const hero = document.createElement('div');
  hero.className = 'context-hero';
  const avatar = document.createElement('div');
  avatar.className = 'avatar customer large';
  avatar.setAttribute('aria-hidden', 'true');
  avatar.textContent = initials(customer);
  const copy = document.createElement('div');
  copy.className = 'context-hero-copy';
  const name = document.createElement('strong');
  name.dir = 'auto';
  name.textContent = customer;
  const detail = document.createElement('span');
  detail.dir = 'auto';
  detail.textContent = ticket.whatsapp_phone || ticket.customer_userid || 'Customer identity unavailable';
  copy.append(name, detail);
  hero.append(avatar, copy);
  container.append(hero);

  appendContextCard(container, 'Customer', [
    ['Name', ticket.customer_name],
    ['User ID', ticket.customer_userid, 'code'],
    ['Customer ID', ticket.customer_id, 'code'],
    ['WhatsApp', ticket.whatsapp_phone],
    ['Authenticated', context.authenticated],
    ['Language', ticket.current_language || context.current_language],
    ['Communication style', ticket.communication_style || context.communication_style]
  ]);

  appendContextCard(container, 'Escalation', [
    ['Reason', ticket.reason],
    ['Requested action', ticket.requested_action],
    ['Tracking reference', ticket.tracking_reference, 'code']
  ]);
  if (ticket.ai_summary) appendContextCard(container, 'AI summary', [['Summary', ticket.ai_summary]], {summary: true});

  appendContextCard(container, 'Shipment', [
    ['Status', shipment && shipment.status],
    ['Mode', shipment && shipment.mode],
    ['Description', shipment && shipment.description],
    ['Order ID', shipment && shipment.order_id, 'code'],
    ['Shipment ID', shipment && shipment.shipment_id, 'code']
  ]);

  appendContextCard(container, 'Ticket', [
    ['Status', formatStatus(ticket.status)],
    ['Assignee', ticket.current_assignee_name || 'Unassigned'],
    ['Received on', supportNumberLabel(ticket)],
    ['Meta phone number ID', ticket.business_phone_number_id, 'code'],
    ['Created', fmt(ticket.created_at)],
    ['Last activity', fmt(ticket.last_activity_at)]
  ]);
  $('contextPanel').scrollTop = previousScrollTop;
}

function renderTicketHeader(ticket) {
  const customer = ticket.customer_name || ticket.customer_userid || ticket.whatsapp_phone || 'Unknown customer';
  text($('ticketTitle'), ticket.ticket_reference || ('Ticket #' + ticket.id));
  text($('ticketSubtitle'), customer + ' · Received on ' + supportNumberLabel(ticket));
  text($('ticketAvatar'), initials(customer));
  $('ticketHeadingStatus').replaceChildren(statusBadge(ticket.status));
  const meta = $('ticketHeaderMeta');
  meta.replaceChildren();
  if (ticket.current_assignee_name) meta.append(createMetaChip('Assigned to ' + ticket.current_assignee_name, 'user'));
  else meta.append(createMetaChip('Unassigned', 'user'));
  if (ticket.tracking_reference) meta.append(createMetaChip(ticket.tracking_reference, 'truck'));
  meta.append(createMetaChip('Updated ' + relativeTime(ticket.last_activity_at), 'clock'));
}

function renderReadOnlyState(data) {
  const banner = $('readOnlyBanner');
  const ticket = data.ticket;
  const permissions = data.permissions;
  banner.replaceChildren();
  if (permissions.is_read_only_observer && ticket.active_key) {
    banner.append(createIcon('eye'), document.createTextNode('Watching only — this ticket is assigned to ' + (ticket.current_assignee_name || 'another agent') + '. You can follow the live conversation, but only the current assignee can send messages.'));
    banner.classList.remove('hidden');
  } else {
    banner.classList.add('hidden');
  }
}

function actionButton(label, handler, className = '', iconName = null) {
  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'btn ' + className;
  button.dataset.ticketAction = 'true';
  button.disabled = state.busy;
  if (iconName) button.append(createIcon(iconName));
  const copy = document.createElement('span');
  copy.className = 'action-label';
  copy.textContent = label;
  button.append(copy);
  button.title = label;
  button.setAttribute('aria-label', label);
  button.onclick = handler;
  return button;
}

function renderActions(data) {
  const container = $('actions');
  container.replaceChildren();
  const ticket = data.ticket;
  const permissions = data.permissions;
  if (permissions.can_claim) container.append(actionButton('Claim ticket', () => doAction('/claim', 'Ticket claimed.'), 'primary', 'user-check'));
  if (permissions.can_transfer && ticket.active_key) container.append(actionButton('Transfer', openTransferModal, '', 'transfer'));
  if (permissions.can_operate && ticket.active_key) {
    const availableStatuses = ['IN_PROGRESS', 'WAITING_CUSTOMER', 'WAITING_INTERNAL'].filter(status => status !== ticket.status);
    if (availableStatuses.length) {
      const menu = document.createElement('details');
      menu.className = 'action-menu';
      const summary = document.createElement('summary');
      summary.className = 'menu-summary';
      summary.title = 'Change ticket status';
      summary.setAttribute('aria-label', 'Change ticket status');
      summary.append(createIcon('activity'), (() => { const span = document.createElement('span'); span.className = 'action-label'; span.textContent = 'Change status'; return span; })(), createIcon('chevron-down', 'icon icon-sm'));
      const panel = document.createElement('div');
      panel.className = 'action-menu-panel';
      panel.setAttribute('role', 'menu');
      for (const status of availableStatuses) {
        const button = actionButton(formatStatus(status), () => {
          menu.open = false;
          setStatus(status);
        }, 'ghost', status === 'IN_PROGRESS' ? 'activity' : 'clock');
        button.setAttribute('role', 'menuitem');
        panel.append(button);
      }
      menu.append(summary, panel);
      container.append(menu);
    }
    container.append(actionButton('Resolve & return to AI', resolveTicket, 'danger', 'check-circle'));
  }
}

function setWorkspaceBusy(busy) {
  for (const control of document.querySelectorAll('[data-ticket-action="true"]')) control.disabled = busy;
  updateComposerControls();
}

async function openTicket(id, {silent = false} = {}) {
  const isNewSelection = String(state.ticketId || '') !== String(id || '');
  if (isNewSelection) {
    if (state.mediaRecorder) cancelVoiceRecording();
    clearMediaDraft();
    $('message').value = '';
    updateComposerCount();
    state.timelineSignature = '';
  }
  state.ticketId = id;
  setMobileTicketOpen(true);
  closeContextPanel(false);
  renderQueue();
  showError('');
  const requestId = ++state.ticketRequest;
  if (isNewSelection || !silent) $('workspaceLoading').classList.remove('hidden');
  try {
    const data = await api('/api/support/tickets/' + id);
    if (requestId !== state.ticketRequest || String(state.ticketId) !== String(id)) return;
    state.currentTicketData = data;
    const ticket = data.ticket;
    renderTicketHeader(ticket);
    renderContext(ticket, data.context || {}, data.shipment || null);
    renderReadOnlyState(data);
    renderActions(data);
    const signature = timelineSignature(data);
    if (signature !== state.timelineSignature || isNewSelection) {
      renderConversation(data, {forceScroll: isNewSelection});
      state.timelineSignature = signature;
    }
    setComposerEnabled(Boolean(data.permissions.can_reply && ticket.active_key));
    await loadQueue({silent: true});
    if (isNewSelection) setAppStatus('Opened ' + (ticket.ticket_reference || 'ticket') + ' for ' + (ticket.customer_name || ticket.customer_userid || ticket.whatsapp_phone || 'customer') + '.');
  } catch (e) {
    if (requestId !== state.ticketRequest) return;
    if(e.status===403) {
      state.currentTicketData = null;
      state.ticketId = null;
      state.timelineSignature = '';
      setComposerEnabled(false);
      clearMediaDraft();
      text($('ticketTitle'), 'Private ticket');
      text($('ticketSubtitle'), 'Conversation access is restricted to participating agents.');
      $('ticketHeadingStatus').replaceChildren();
      $('ticketHeaderMeta').replaceChildren();
      $('conversation').innerHTML = '<div class="conversation-empty"><div class="empty-state-inner"><div class="empty-icon"><svg class="icon icon-lg" aria-hidden="true"><use href="#icon-lock"></use></svg></div><h3>Ticket access restricted</h3><p>You can only open tickets you have claimed or worked on.</p></div></div>';
      $('context').replaceChildren();
      $('actions').replaceChildren();
      $('readOnlyBanner').classList.add('hidden');
      await loadQueue({silent: true});
    }
    showError(e.message);
  } finally {
    if (requestId === state.ticketRequest) $('workspaceLoading').classList.add('hidden');
  }
}

async function doAction(suffix, successMessage = 'Ticket updated.') {
  if (state.busy || !state.ticketId) return;
  state.busy = true;
  setWorkspaceBusy(true);
  showError('');
  let succeeded = false;
  try {
    await api('/api/support/tickets/' + state.ticketId + suffix, {method:'POST'});
    succeeded = true;
    notify(successMessage);
  } catch (error) {
    showError(error.message);
    notify(error.message, 'error');
  } finally {
    state.busy = false;
    setWorkspaceBusy(false);
  }
  if (succeeded) await openTicket(state.ticketId, {silent: true});
}

async function setStatus(status) {
  if (state.busy || !state.ticketId) return;
  state.busy = true;
  setWorkspaceBusy(true);
  showError('');
  let succeeded = false;
  try {
    await api('/api/support/tickets/' + state.ticketId + '/status', {method:'POST', body: JSON.stringify({status})});
    succeeded = true;
    notify('Ticket status changed to ' + formatStatus(status) + '.');
  } catch (error) {
    showError(error.message);
    notify(error.message, 'error');
  } finally {
    state.busy = false;
    setWorkspaceBusy(false);
  }
  if (succeeded) await openTicket(state.ticketId, {silent: true});
}

async function resolveTicket() {
  if (!confirm('Resolve this ticket and return the WhatsApp conversation to AI?')) return;
  await doAction('/resolve', 'Ticket resolved and returned to AI.');
}

function stopMediaTracks() {
  if (!state.mediaStream) return;
  for (const track of state.mediaStream.getTracks()) track.stop();
  state.mediaStream = null;
}

function resetRecordingUi() {
  if (state.recordingTimer) {
    clearInterval(state.recordingTimer);
    state.recordingTimer = null;
  }
  state.mediaRecorder = null;
  state.recordingChunks = [];
  state.recordingStartedAt = 0;
  $('recordingBar').classList.add('hidden');
  text($('recordingTime'), 'Recording 0:00');
  stopMediaTracks();
  updateComposerControls();
}

function cancelVoiceRecording() {
  if (!state.mediaRecorder) return;
  state.discardRecording = true;
  try { state.mediaRecorder.stop(); } catch (_) { resetRecordingUi(); }
}

function clearMediaDraft() {
  if (state.pendingMedia && state.pendingMedia.url) URL.revokeObjectURL(state.pendingMedia.url);
  state.pendingMedia = null;
  $('mediaDraft').classList.add('hidden');
  $('mediaDraft').replaceChildren();
  $('imageInput').value = '';
  $('message').maxLength = 4096;
  $('message').placeholder = 'Reply to customer on WhatsApp';
  text($('composerHelp'), 'Messages are sent from the WhatsApp number that received this ticket.');
  updateComposerCount();
  updateComposerControls();
}

function renderMediaDraft() {
  const container = $('mediaDraft');
  container.replaceChildren();
  const draft = state.pendingMedia;
  if (!draft) {
    container.classList.add('hidden');
    return;
  }
  container.className = 'media-draft ' + draft.kind;
  const preview = document.createElement('div');
  preview.className = 'media-draft-preview';
  if (draft.kind === 'image') {
    const img = document.createElement('img');
    img.src = draft.url;
    img.alt = 'Selected image preview';
    preview.append(img);
  } else {
    const audio = document.createElement('audio');
    audio.controls = true;
    audio.preload = 'metadata';
    audio.src = draft.url;
    audio.setAttribute('aria-label', 'Voice note preview');
    preview.append(audio);
  }
  const info = document.createElement('div');
  info.className = 'media-draft-info';
  const name = document.createElement('div');
  name.className = 'media-draft-name';
  name.textContent = draft.kind === 'image' ? (draft.file.name || 'Selected image') : 'Voice note ready';
  const size = document.createElement('div');
  size.className = 'media-draft-size';
  size.textContent = formatBytes(draft.file.size);
  info.append(name, size);
  const actions = document.createElement('div');
  actions.className = 'media-draft-actions';
  const remove = document.createElement('button');
  remove.type = 'button';
  remove.className = 'btn small';
  remove.append(createIcon('trash', 'icon icon-sm'), document.createTextNode('Remove'));
  remove.onclick = clearMediaDraft;
  actions.append(remove);
  container.append(preview, info, actions);

  if (draft.kind === 'image') {
    $('message').maxLength = 1024;
    $('message').placeholder = 'Add an image caption (optional)';
    text($('composerHelp'), 'The image and optional caption will be sent as a public WhatsApp reply.');
  } else {
    $('message').value = '';
    $('message').placeholder = 'Voice note ready to send';
    text($('composerHelp'), 'Review the recording, then send it as a public WhatsApp voice note.');
  }
  updateComposerCount();
  updateComposerControls();
}

function setPendingMedia(kind, file) {
  clearMediaDraft();
  const url = URL.createObjectURL(file);
  state.pendingMedia = {kind, file, url};
  renderMediaDraft();
}

function setComposerEnabled(enabled) {
  state.composerEnabled = Boolean(enabled);
  updateComposerControls();
}

function updateComposerCount() {
  const maximum = Number($('message').maxLength > 0 ? $('message').maxLength : 4096);
  const length = $('message').value.length;
  text($('composerCount'), length + ' / ' + maximum);
  $('composerCount').style.color = length >= maximum ? 'var(--danger)' : '';
}

function resizeComposer() {
  const textarea = $('message');
  textarea.style.height = 'auto';
  textarea.style.height = Math.min(textarea.scrollHeight, 154) + 'px';
}

function updateComposerControls() {
  const enabled = state.composerEnabled && !state.busy;
  const recording = Boolean(state.mediaRecorder);
  const draft = state.pendingMedia;
  const hasBody = Boolean($('message').value.trim());
  const canSend = enabled && !recording && (Boolean(draft) || hasBody);

  $('imageBtn').disabled = !enabled || recording;
  $('voiceBtn').disabled = !enabled;
  $('voiceBtn').setAttribute('aria-pressed', recording ? 'true' : 'false');
  $('voiceBtn').setAttribute('aria-label', recording ? 'Stop voice recording' : 'Record a voice note');
  $('voiceBtn').setAttribute('data-tooltip', recording ? 'Stop recording' : 'Record voice note');
  setIcon($('voiceBtn'), recording ? 'square' : 'mic');
  $('message').disabled = !enabled || recording || Boolean(draft && draft.kind === 'audio');
  $('sendBtn').disabled = !canSend;
  const label = $('sendBtn').querySelector('.send-label');
  if (label) label.textContent = draft ? (draft.kind === 'image' ? 'Send image' : 'Send voice') : 'Send';
}

function recordingMimeType() {
  if (typeof MediaRecorder === 'undefined') return '';
  for (const type of ['audio/ogg;codecs=opus', 'audio/mp4', 'audio/webm;codecs=opus', 'audio/webm']) {
    if (!MediaRecorder.isTypeSupported || MediaRecorder.isTypeSupported(type)) return type;
  }
  return '';
}

async function startVoiceRecording() {
  if (!state.composerEnabled || state.busy || state.mediaRecorder) return;
  showError('');
  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia || typeof MediaRecorder === 'undefined') {
    showError('Voice recording is not supported by this browser.');
    return;
  }
  clearMediaDraft();
  try {
    const stream = await navigator.mediaDevices.getUserMedia({audio: true});
    state.mediaStream = stream;
    const mime = recordingMimeType();
    const recorder = mime ? new MediaRecorder(stream, {mimeType: mime}) : new MediaRecorder(stream);
    state.recordingChunks = [];
    state.discardRecording = false;
    recorder.ondataavailable = event => {
      if (event.data && event.data.size) state.recordingChunks.push(event.data);
    };
    recorder.onerror = () => {
      showError('The voice recording failed.');
      state.discardRecording = true;
    };
    recorder.onstop = () => {
      const discard = state.discardRecording;
      const chunks = state.recordingChunks.slice();
      const type = recorder.mimeType || mime || 'audio/webm';
      resetRecordingUi();
      state.discardRecording = false;
      if (discard || !chunks.length) return;
      const blob = new Blob(chunks, {type});
      const extension = type.includes('ogg') ? 'ogg' : type.includes('mp4') ? 'm4a' : 'webm';
      const file = new File([blob], 'voice-note.' + extension, {type});
      setPendingMedia('audio', file);
      setAppStatus('Voice note recorded and ready to review.');
    };
    state.mediaRecorder = recorder;
    state.recordingStartedAt = Date.now();
    $('recordingBar').classList.remove('hidden');
    state.recordingTimer = setInterval(() => {
      const seconds = Math.max(0, Math.floor((Date.now() - state.recordingStartedAt) / 1000));
      text($('recordingTime'), 'Recording ' + Math.floor(seconds / 60) + ':' + String(seconds % 60).padStart(2, '0'));
    }, 250);
    recorder.start(250);
    updateComposerControls();
    setAppStatus('Voice recording started.');
  } catch (error) {
    stopMediaTracks();
    resetRecordingUi();
    showError(error && error.name === 'NotAllowedError' ? 'Microphone permission was denied.' : 'Could not start the microphone.');
  }
}

function toggleVoiceRecording() {
  if (state.mediaRecorder) {
    state.discardRecording = false;
    try { state.mediaRecorder.stop(); } catch (_) { resetRecordingUi(); }
  } else {
    startVoiceRecording();
  }
}

function chooseImage() {
  if (state.composerEnabled && !state.busy && !state.mediaRecorder) $('imageInput').click();
}

function imageSelected() {
  const file = $('imageInput').files && $('imageInput').files[0];
  if (!file) return;
  if (!['image/jpeg', 'image/png'].includes(file.type)) {
    showError('Use a JPEG or PNG image.');
    $('imageInput').value = '';
    return;
  }
  setPendingMedia('image', file);
  $('message').focus();
  setAppStatus('Image attached. You may add a caption before sending.');
}

async function sendMediaMessage() {
  const draft = state.pendingMedia;
  if (!draft || !state.ticketId || state.busy) return;
  state.busy = true;
  setWorkspaceBusy(true);
  setButtonBusy($('sendBtn'), true, draft.kind === 'image' ? 'Sending image…' : 'Sending voice…');
  showError('');
  const form = new FormData();
  form.append('media_type', draft.kind);
  form.append('client_message_id', crypto.randomUUID());
  if (draft.kind === 'image') form.append('caption', $('message').value.trim());
  form.append('file', draft.file, draft.file.name || (draft.kind === 'image' ? 'image' : 'voice-note'));
  let refresh = false;
  try {
    await api('/api/support/tickets/' + state.ticketId + '/media', {method:'POST', body: form});
    $('message').value = '';
    clearMediaDraft();
    notify(draft.kind === 'image' ? 'Image sent to the customer.' : 'Voice note sent to the customer.');
    refresh = true;
  } catch (error) {
    showError(error.message);
    notify(error.message, 'error');
    refresh = true;
  } finally {
    state.busy = false;
    setButtonBusy($('sendBtn'), false);
    setWorkspaceBusy(false);
    updateComposerCount();
  }
  if (refresh) await openTicket(state.ticketId, {silent: true});
}

async function sendMessage() {
  if (state.pendingMedia) return sendMediaMessage();
  const body = $('message').value.trim();
  if (!body || state.busy || !state.ticketId) return;
  state.busy = true;
  setWorkspaceBusy(true);
  setButtonBusy($('sendBtn'), true, 'Sending…');
  showError('');
  const clientMessageId = crypto.randomUUID();
  let refresh = false;
  try {
    await api('/api/support/tickets/' + state.ticketId + '/messages', {method:'POST', body: JSON.stringify({body, client_message_id: clientMessageId})});
    $('message').value = '';
    resizeComposer();
    updateComposerCount();
    notify('Reply sent to the customer.');
    refresh = true;
  } catch (error) {
    showError(error.message);
    notify(error.message, 'error');
    refresh = true;
  } finally {
    state.busy = false;
    setButtonBusy($('sendBtn'), false);
    setWorkspaceBusy(false);
  }
  if (refresh) await openTicket(state.ticketId, {silent: true});
}

function showStaffFeedback(id, message) {
  const element = $(id);
  element.textContent = message || '';
  element.style.display = message ? 'block' : 'none';
}

function setFieldError(inputId, errorId, message) {
  const input = $(inputId);
  const error = $(errorId);
  input.setAttribute('aria-invalid', message ? 'true' : 'false');
  error.textContent = message || '';
  error.classList.toggle('hidden', !message);
}

function clearStaffFeedback() {
  showStaffFeedback('staffError', '');
  showStaffFeedback('staffSuccess', '');
  $('editConflictStaffBtn').classList.add('hidden');
  state.conflictStaff = null;
  setFieldError('createStaffPasswordConfirm', 'createPasswordMatchError', '');
  setFieldError('editStaffPasswordConfirm', 'editPasswordMatchError', '');
}

function setStaffView(view) {
  state.staffView = view;
  for (const [name, id] of Object.entries({list: 'staffListView', create: 'staffCreateView', edit: 'staffEditView'})) {
    $(id).classList.toggle('hidden', name !== view);
  }
  const copy = {
    list: ['Staff accounts', 'Manage your support team from one place.'],
    create: ['Create staff', 'Create a new support staff account.'],
    edit: ['Edit staff', 'Update an existing support staff account.']
  }[view] || ['Staff accounts', 'Manage your support team from one place.'];
  text($('staffModalTitle'), copy[0]);
  text($('staffModalSubtitle'), copy[1]);
}

function resetCreateStaffForm() {
  $('createStaffForm').reset();
  $('createStaffRole').value = 'agent';
  setFieldError('createStaffPasswordConfirm', 'createPasswordMatchError', '');
}

function resetEditStaffForm() {
  state.editingStaffId = null;
  state.editingStaffOriginal = null;
  $('editStaffForm').reset();
  $('editStaffRole').value = 'agent';
  $('editStaffActive').checked = true;
  text($('editStaffHeading'), 'Edit staff account');
  setFieldError('editStaffPasswordConfirm', 'editPasswordMatchError', '');
}

function staffFormHasChanges() {
  if (state.staffView === 'create') {
    return Boolean(
      $('createStaffName').value ||
      $('createStaffEmail').value ||
      $('createStaffPassword').value ||
      $('createStaffPasswordConfirm').value ||
      $('createStaffRole').value !== 'agent'
    );
  }
  if (state.staffView === 'edit' && state.editingStaffOriginal) {
    const original = state.editingStaffOriginal;
    return (
      $('editStaffName').value.trim() !== (original.name || '') ||
      $('editStaffEmail').value.trim() !== (original.email || '') ||
      $('editStaffRole').value !== (original.role || 'agent') ||
      $('editStaffActive').checked !== Boolean(original.is_active) ||
      Boolean($('editStaffPassword').value || $('editStaffPasswordConfirm').value)
    );
  }
  return false;
}

function confirmDiscardStaffChanges() {
  return !staffFormHasChanges() || confirm('Discard the unsaved staff account changes?');
}

function closeStaffModal() {
  if (state.staffBusy || !confirmDiscardStaffChanges()) return;
  closeDialog('staffModal');
  clearStaffFeedback();
  resetCreateStaffForm();
  resetEditStaffForm();
  setStaffView('list');
}

async function openStaffModal() {
  if (!canManageStaff()) return;
  clearStaffFeedback();
  resetCreateStaffForm();
  resetEditStaffForm();
  setStaffView('list');
  openDialog('staffModal', 'createStaffBtn');
  await loadStaffAccounts();
}

function openCreateStaff() {
  if (!canManageStaff()) return;
  clearStaffFeedback();
  resetCreateStaffForm();
  setStaffView('create');
  $('createStaffName').focus();
}

function backToStaffList() {
  if (state.staffBusy || !confirmDiscardStaffChanges()) return;
  clearStaffFeedback();
  resetCreateStaffForm();
  resetEditStaffForm();
  setStaffView('list');
  $('createStaffBtn').focus();
}

function beginStaffEdit(item) {
  if (!item || item.role === 'supervisor') return;
  clearStaffFeedback();
  resetEditStaffForm();
  state.editingStaffId = String(item.id);
  state.editingStaffOriginal = {...item};
  $('editStaffName').value = item.name || '';
  $('editStaffEmail').value = item.email || '';
  $('editStaffRole').value = item.role === 'admin' ? 'admin' : 'agent';
  $('editStaffActive').checked = Boolean(item.is_active);
  $('editStaffPassword').value = '';
  $('editStaffPasswordConfirm').value = '';
  text($('editStaffHeading'), 'Edit ' + (item.name || item.email || 'staff account'));
  setStaffView('edit');
  $('editStaffName').focus();
}

function staffButton(label, handler, className = '', iconName = null) {
  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'btn ' + className;
  if (iconName) button.append(createIcon(iconName, 'icon icon-sm'));
  button.append(document.createTextNode(label));
  button.onclick = handler;
  return button;
}

function renderStaffAccounts(items) {
  state.staffItems = items || [];
  const container = $('staffList');
  container.replaceChildren();
  if (!state.staffItems.length) {
    container.append(emptyMarkup('No staff accounts found', 'Create an agent or administrator to add them to the support team.', 'users').outer);
    return;
  }
  const fragment = document.createDocumentFragment();
  for (const item of state.staffItems) {
    const row = document.createElement('div');
    row.className = 'staff-list-row';
    row.setAttribute('role', 'row');

    const identity = document.createElement('div');
    identity.setAttribute('role', 'cell');
    const name = document.createElement('div');
    name.className = 'staff-name';
    name.textContent = item.name || 'Unnamed';
    const created = document.createElement('div');
    created.className = 'staff-created';
    created.textContent = 'Created ' + fmt(item.created_at);
    identity.append(name, created);
    if (item.email_conflict) {
      const conflict = document.createElement('span');
      conflict.className = 'status-dot conflict';
      conflict.textContent = 'Duplicate email';
      identity.append(conflict);
    }

    const email = document.createElement('div');
    email.className = 'staff-email';
    email.setAttribute('role', 'cell');
    email.textContent = item.email || '—';

    const role = document.createElement('span');
    role.className = 'role-badge';
    role.setAttribute('role', 'cell');
    role.textContent = item.role || '—';

    const active = document.createElement('span');
    active.className = 'status-dot' + (item.is_active ? '' : ' off');
    active.setAttribute('role', 'cell');
    active.textContent = item.is_active ? 'Active' : 'Inactive';

    const actions = document.createElement('div');
    actions.className = 'staff-row-actions';
    actions.setAttribute('role', 'cell');
    if (item.role !== 'supervisor') {
      actions.append(staffButton(item.is_active ? 'Edit' : 'Edit / reactivate', () => beginStaffEdit(item), '', 'edit'));
      if (item.is_active) actions.append(staffButton('Delete', () => deleteStaffAccount(item), 'danger', 'trash'));
    } else {
      const protectedLabel = document.createElement('span');
      protectedLabel.className = 'muted';
      protectedLabel.textContent = 'Protected';
      actions.append(protectedLabel);
    }
    row.append(identity, email, role, active, actions);
    fragment.append(row);
  }
  container.append(fragment);
}

function renderStaffLoading() {
  const container = document.createElement('div');
  container.className = 'loading-list';
  for (let index = 0; index < 4; index += 1) {
    const row = document.createElement('div');
    row.className = 'skeleton-row';
    const first = document.createElement('div');
    first.className = 'skeleton skeleton-line medium';
    const second = document.createElement('div');
    second.className = 'skeleton skeleton-line long';
    row.append(first, second);
    container.append(row);
  }
  $('staffList').replaceChildren(container);
}

async function loadStaffAccounts() {
  if (!canManageStaff()) return;
  renderStaffLoading();
  $('refreshStaffBtn').disabled = true;
  try {
    const data = await api('/api/support/staff');
    renderStaffAccounts(data.items || []);
  } catch (error) {
    $('staffList').replaceChildren(emptyMarkup('Could not load staff accounts', 'Try refreshing the staff directory.', 'alert').outer);
    showStaffFeedback('staffError', error.message);
  } finally {
    $('refreshStaffBtn').disabled = false;
  }
}

function handleStaffConflict(error) {
  const detail = error && error.detail;
  if (detail && detail.code === 'email_conflict' && detail.existing_staff) {
    state.conflictStaff = detail.existing_staff;
    showStaffFeedback('staffError', detail.message || error.message);
    $('editConflictStaffBtn').classList.remove('hidden');
    return true;
  }
  return false;
}

function passwordByteLength(value) {
  return new TextEncoder().encode(value || '').length;
}

function validatePasswordPair(passwordId, confirmationId, errorId, required) {
  const password = $(passwordId).value;
  const confirmation = $(confirmationId).value;
  let message = '';
  if (required && !password) message = 'A password is required.';
  else if (password && password.length < 12) message = 'Use at least 12 characters.';
  else if (password && passwordByteLength(password) > 72) message = 'Use a password no longer than 72 UTF-8 bytes.';
  else if (password !== confirmation) message = 'Passwords do not match.';
  setFieldError(confirmationId, errorId, message);
  return !message;
}

async function createStaffAccount(event) {
  event.preventDefault();
  if (state.staffBusy || !canManageStaff()) return;
  const form = $('createStaffForm');
  clearStaffFeedback();
  if (!form.reportValidity() || !validatePasswordPair('createStaffPassword', 'createStaffPasswordConfirm', 'createPasswordMatchError', true)) return;
  const payload = {
    name: $('createStaffName').value.trim(),
    email: $('createStaffEmail').value.trim(),
    role: $('createStaffRole').value,
    password: $('createStaffPassword').value
  };
  state.staffBusy = true;
  setButtonBusy($('createStaffSubmitBtn'), true, 'Creating…');
  try {
    const data = await api('/api/support/staff', {method:'POST', body: JSON.stringify(payload)});
    resetCreateStaffForm();
    setStaffView('list');
    showStaffFeedback('staffSuccess', 'Created ' + data.staff.name + ' (' + data.staff.role + ').');
    notify('Staff account created.');
    await loadStaffAccounts();
  } catch (error) {
    if (!handleStaffConflict(error)) showStaffFeedback('staffError', error.message);
  } finally {
    state.staffBusy = false;
    setButtonBusy($('createStaffSubmitBtn'), false);
    $('createStaffPassword').value = '';
    $('createStaffPasswordConfirm').value = '';
  }
}

async function updateStaffAccount(event) {
  event.preventDefault();
  if (state.staffBusy || !canManageStaff() || !state.editingStaffId) return;
  const form = $('editStaffForm');
  clearStaffFeedback();
  if (!form.reportValidity() || !validatePasswordPair('editStaffPassword', 'editStaffPasswordConfirm', 'editPasswordMatchError', false)) return;
  const original = state.editingStaffOriginal || {};
  const name = $('editStaffName').value.trim();
  const email = $('editStaffEmail').value.trim();
  const role = $('editStaffRole').value;
  const active = $('editStaffActive').checked;
  const password = $('editStaffPassword').value;
  const payload = {};
  if (name !== (original.name || '')) payload.name = name;
  if (email !== (original.email || '')) payload.email = email;
  if (role !== (original.role || '')) payload.role = role;
  if (active !== Boolean(original.is_active)) payload.is_active = active;
  if (password) payload.password = password;
  if (!Object.keys(payload).length) {
    showStaffFeedback('staffError', 'No changes to save.');
    return;
  }
  state.staffBusy = true;
  setButtonBusy($('editStaffSubmitBtn'), true, 'Saving…');
  try {
    const data = await api('/api/support/staff/' + encodeURIComponent(state.editingStaffId), {method:'PATCH', body: JSON.stringify(payload)});
    const sessionRevoked = Boolean(data.staff.current_session_revoked);
    resetEditStaffForm();
    setStaffView('list');
    showStaffFeedback('staffSuccess', 'Updated ' + data.staff.name + ' (' + data.staff.role + ').');
    notify('Staff account updated.');
    await loadStaffAccounts();
    if (sessionRevoked) {
      alert('Your credentials changed, so this staff session was revoked. Please sign in again.');
      showLogin();
    }
  } catch (error) {
    if (!handleStaffConflict(error)) showStaffFeedback('staffError', error.message);
  } finally {
    state.staffBusy = false;
    setButtonBusy($('editStaffSubmitBtn'), false);
    $('editStaffPassword').value = '';
    $('editStaffPasswordConfirm').value = '';
  }
}

async function deleteStaffAccount(item) {
  if (state.staffBusy || !item) return;
  if (String(item.id) === String(state.agent && state.agent.id)) {
    showStaffFeedback('staffError', 'You cannot delete your own active account.');
    return;
  }
  if (!confirm('Delete this staff account? It will be deactivated and all of its sessions will be revoked.')) return;
  state.staffBusy = true;
  try {
    await api('/api/support/staff/' + encodeURIComponent(item.id), {method:'DELETE'});
    showStaffFeedback('staffSuccess', 'Deleted ' + (item.name || item.email) + '.');
    notify('Staff account deactivated.');
    await loadStaffAccounts();
  } catch (error) {
    showStaffFeedback('staffError', error.message);
  } finally {
    state.staffBusy = false;
  }
}

function showTransferError(message) {
  const element = $('transferError');
  element.textContent = message || '';
  element.style.display = message ? 'block' : 'none';
}

function closeTransferModal() {
  if (state.transferBusy) return;
  closeDialog('transferModal');
  showTransferError('');
  $('transferTarget').replaceChildren();
  state.transferTargets = [];
}

async function openTransferModal() {
  const data = state.currentTicketData;
  if (!data || !data.permissions.can_transfer || state.transferBusy) return;
  showTransferError('');
  $('transferTarget').replaceChildren(new Option('Loading staff…', ''));
  $('confirmTransferBtn').disabled = true;
  openDialog('transferModal', 'transferTarget');
  try {
    const directory = await api('/api/support/transfer-targets');
    const currentId = String(data.ticket.current_assignee_id || '');
    state.transferTargets = (directory.items || []).filter(item => String(item.id) !== currentId);
    const select = $('transferTarget');
    select.replaceChildren();
    if (!state.transferTargets.length) {
      select.append(new Option('No other active staff available', ''));
      showTransferError('There is no other active staff member available for transfer.');
      return;
    }
    for (const item of state.transferTargets) select.append(new Option(item.name + ' · ' + item.role, item.id));
    $('confirmTransferBtn').disabled = false;
    select.focus();
  } catch (error) {
    showTransferError(error.message);
  }
}

async function confirmTransfer() {
  if (state.transferBusy || !state.ticketId) return;
  const targetId = Number($('transferTarget').value);
  if (!targetId) {
    showTransferError('Choose a staff member.');
    return;
  }
  const target = state.transferTargets.find(item => Number(item.id) === targetId);
  if (!target) return;
  if (!confirm('Transfer this ticket to ' + target.name + '? You will become read-only unless it is transferred back to you.')) return;
  state.transferBusy = true;
  setButtonBusy($('confirmTransferBtn'), true, 'Transferring…');
  showTransferError('');
  try {
    await api('/api/support/tickets/' + state.ticketId + '/transfer', {method:'POST', body: JSON.stringify({target_staff_id: targetId})});
    closeDialog('transferModal');
    state.transferTargets = [];
    notify('Ticket transferred to ' + target.name + '.');
    await openTicket(state.ticketId, {silent: true});
  } catch (error) {
    showTransferError(error.message);
  } finally {
    state.transferBusy = false;
    setButtonBusy($('confirmTransferBtn'), false);
  }
}

$('loginForm').addEventListener('submit', login);
$('loginThemeBtn').addEventListener('click', toggleTheme);
$('themeBtn').addEventListener('click', toggleTheme);
$('logoutBtn').addEventListener('click', logout);
$('manageStaffBtn').addEventListener('click', openStaffModal);
$('refreshQueueBtn').addEventListener('click', () => loadQueue());
$('previousPageBtn').addEventListener('click', () => changePage(-1));
$('nextPageBtn').addEventListener('click', () => changePage(1));
$('queue').addEventListener('keydown', handleQueueKeydown);
$('queueSearch').addEventListener('input', event => {
  state.query = event.target.value;
  renderQueue();
});
$('queueSearch').addEventListener('keydown', event => {
  if (event.key === 'Escape' && state.query) {
    event.preventDefault();
    clearQueueSearch();
  }
});
$('clearQueueSearchBtn').addEventListener('click', clearQueueSearch);
$('tabs').addEventListener('click', event => {
  const button = event.target.closest('button[data-view]');
  if (button) setQueueView(button.dataset.view);
});
$('mobileBackBtn').addEventListener('click', closeMobileTicket);
$('contextToggleBtn').addEventListener('click', openContextPanel);
$('closeContextBtn').addEventListener('click', () => closeContextPanel());
$('contextBackdrop').addEventListener('click', () => closeContextPanel());
$('dismissErrorBtn').addEventListener('click', () => showError(''));
$('scrollToLatestBtn').addEventListener('click', () => {
  $('conversation').scrollTo({top: $('conversation').scrollHeight, behavior: 'smooth'});
});
$('conversation').addEventListener('scroll', updateScrollLatestButton, {passive: true});

$('imageBtn').addEventListener('click', chooseImage);
$('voiceBtn').addEventListener('click', toggleVoiceRecording);
$('cancelRecordingBtn').addEventListener('click', cancelVoiceRecording);
$('imageInput').addEventListener('change', imageSelected);
$('sendBtn').addEventListener('click', sendMessage);
$('message').addEventListener('input', () => {
  updateComposerCount();
  resizeComposer();
  updateComposerControls();
});
$('message').addEventListener('keydown', event => {
  if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    sendMessage();
  }
});

$('closeStaffBtn').addEventListener('click', closeStaffModal);
$('createStaffBtn').addEventListener('click', openCreateStaff);
$('refreshStaffBtn').addEventListener('click', loadStaffAccounts);
$('backFromCreateBtn').addEventListener('click', backToStaffList);
$('backFromEditBtn').addEventListener('click', backToStaffList);
$('cancelCreateStaffBtn').addEventListener('click', backToStaffList);
$('cancelEditStaffBtn').addEventListener('click', backToStaffList);
$('editConflictStaffBtn').addEventListener('click', () => {
  if (state.conflictStaff) beginStaffEdit(state.conflictStaff);
});
$('createStaffForm').addEventListener('submit', createStaffAccount);
$('editStaffForm').addEventListener('submit', updateStaffAccount);
$('createStaffPassword').addEventListener('input', () => setFieldError('createStaffPasswordConfirm', 'createPasswordMatchError', ''));
$('createStaffPasswordConfirm').addEventListener('input', () => validatePasswordPair('createStaffPassword', 'createStaffPasswordConfirm', 'createPasswordMatchError', true));
$('editStaffPassword').addEventListener('input', () => setFieldError('editStaffPasswordConfirm', 'editPasswordMatchError', ''));
$('editStaffPasswordConfirm').addEventListener('input', () => validatePasswordPair('editStaffPassword', 'editStaffPasswordConfirm', 'editPasswordMatchError', false));

$('closeTransferBtn').addEventListener('click', closeTransferModal);
$('cancelTransferBtn').addEventListener('click', closeTransferModal);
$('confirmTransferBtn').addEventListener('click', confirmTransfer);

document.addEventListener('keydown', handleGlobalKeydown);
document.addEventListener('click', event => {
  for (const menu of document.querySelectorAll('.action-menu[open]')) {
    if (!menu.contains(event.target)) menu.open = false;
  }
});
window.addEventListener('resize', () => {
  if (window.innerWidth > 1100 && state.contextOpen) closeContextPanel(false);
});
window.addEventListener('beforeunload', stopMediaTracks);

document.addEventListener('visibilitychange', () => {
  if (!document.hidden && state.authenticated && !state.busy && !state.activeModal) {
    if (state.ticketId) openTicket(state.ticketId, {silent: true});
    else loadQueue({silent: true});
  }
});

updateComposerCount();
updateComposerControls();
updateQueueSummary();
bootstrap();
setInterval(() => {
  if (!state.authenticated || state.busy || state.mediaRecorder || state.activeModal || document.hidden) return;
  const audioPlaying = [...document.querySelectorAll('#conversation audio')].some(audio => !audio.paused && !audio.ended);
  if (state.ticketId && !audioPlaying) openTicket(state.ticketId, {silent: true});
  else if (!state.ticketId) loadQueue({silent: true});
}, __POLL_MS__);
</script>
</body>
</html>'''
