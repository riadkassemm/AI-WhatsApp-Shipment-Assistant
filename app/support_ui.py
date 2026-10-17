from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, Response

from app.config import settings


router = APIRouter(tags=["support-ui"])


@router.get("/support", response_class=HTMLResponse)
async def support_dashboard() -> HTMLResponse:
    if not settings.support_ui_enabled:
        return HTMLResponse("Support UI is disabled.", status_code=404)
    html = _HTML.replace(
        "__POLL_MS__", str(max(1, settings.support_queue_poll_seconds) * 1000)
    ).replace("__CSRF_COOKIE__", settings.support_csrf_cookie_name)
    return HTMLResponse(
        html,
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
        },
    )


@router.get("/support/image-editor.js")
async def support_image_editor_script() -> Response:
    if not settings.support_ui_enabled:
        return Response(status_code=404)
    script = Path(__file__).with_name("support_image_editor.js").read_text(encoding="utf-8")
    script = script.replace("__IMAGE_MAX_BYTES__", str(max(1, settings.support_image_max_bytes)))
    return Response(script, media_type="text/javascript", headers={"Cache-Control": "no-store"})


_HTML = r'''<!doctype html>
<html lang="en" data-theme="light">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover" />
<meta name="color-scheme" content="light dark" />
<meta name="theme-color" content="#f4f6f8" />
<title>Shipment Support</title>
<script src="/support/image-editor.js"></script>
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
/* ==========================================================================
   Shipment Support design system: "calm teal workspace"
   Color roles: teal = interactive/WhatsApp, green = success, violet = AI,
   blue = info, amber = waiting, red = destructive. Status never relies on
   color alone; chips always carry text and an icon or dot.
   ========================================================================== */
:root {
  color-scheme: light;
  font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, "Noto Sans", "Noto Sans Arabic", sans-serif;
  --font-mono: ui-monospace, "SFMono-Regular", "Cascadia Mono", Consolas, "Liberation Mono", monospace;

  --bg-app: #f4f6f8;
  --bg-subtle: #f8fafb;
  --surface: #ffffff;
  --surface-raised: #ffffff;
  --surface-muted: #eef2f5;
  --surface-hover: #f1f5f7;
  --surface-selected: #e4f3f1;
  --surface-input: #ffffff;
  --text: #1f2933;
  --text-strong: #0f1720;
  --text-muted: #4b5866;
  --text-subtle: #5c6977;
  --text-disabled: #8a96a2;
  --border: #e1e6eb;
  --border-strong: #c5ced7;

  --primary: #0b7a75;
  --primary-hover: #08625e;
  --primary-soft: #e4f3f1;
  --primary-soft-strong: #c9e9e5;
  --primary-text: #076560;
  --primary-contrast: #ffffff;
  --success: #2b7a2c;
  --success-hover: #22621f;
  --success-soft: #eaf5e6;
  --success-contrast: #ffffff;
  --ai: #6a4bd3;
  --ai-soft: #f0ecff;
  --info: #235fa3;
  --info-soft: #e8f1fb;
  --warning: #925505;
  --warning-soft: #fff3da;
  --danger: #b42336;
  --danger-hover: #941b2b;
  --danger-soft: #fdecee;

  --bubble-in: #ffffff;
  --bubble-out: #e4f3f1;
  --bubble-out-border: #bfe2dd;
  --bubble-ai: #f5f2ff;
  --bubble-ai-border: #ddd4fb;

  --focus: #0b7a75;
  --focus-ring: rgba(11, 122, 117, 0.32);
  --overlay: rgba(15, 23, 32, 0.52);
  --shadow-xs: 0 1px 2px rgba(15, 23, 32, 0.06);
  --shadow-sm: 0 1px 2px rgba(15, 23, 32, 0.05), 0 4px 12px rgba(15, 23, 32, 0.06);
  --shadow-md: 0 2px 6px rgba(15, 23, 32, 0.06), 0 10px 28px rgba(15, 23, 32, 0.10);
  --shadow-lg: 0 8px 20px rgba(15, 23, 32, 0.08), 0 28px 64px rgba(15, 23, 32, 0.20);

  --space-1: 4px; --space-2: 8px; --space-3: 12px; --space-4: 16px; --space-5: 20px; --space-6: 24px; --space-7: 32px; --space-8: 40px;
  --text-xs: 11px; --text-sm: 12px; --text-md: 13px; --text-base: 14px; --text-lg: 16px; --text-xl: 18px; --text-2xl: 24px;
  --radius-sm: 6px;
  --radius-md: 10px;
  --radius-lg: 14px;
  --radius-xl: 18px;
  --radius-full: 999px;
  --duration-fast: 120ms;
  --duration-base: 180ms;
  --ease-out: cubic-bezier(0.2, 0.8, 0.2, 1);
  --transition: var(--duration-fast) ease;
  --z-sticky: 5; --z-resizer: 18; --z-popover: 30; --z-drawer: 42; --z-modal: 50; --z-tooltip: 80; --z-toast: 90;

  --sidebar-width: 244px;
  --queue-width: 344px;
  --context-width: 304px;
  --header-height: 64px;
}

:root[data-theme="dark"] {
  color-scheme: dark;
  --bg-app: #0b1115;
  --bg-subtle: #0f161b;
  --surface: #121a20;
  --surface-raised: #172129;
  --surface-muted: #1a252d;
  --surface-hover: #1b2730;
  --surface-selected: #0f3331;
  --surface-input: #0e161b;
  --text: #d9e2e8;
  --text-strong: #f3f7f9;
  --text-muted: #a0afba;
  --text-subtle: #8696a3;
  --text-disabled: #5d6b76;
  --border: #24323c;
  --border-strong: #344652;

  --primary: #3fc9c1;
  --primary-hover: #64d7d0;
  --primary-soft: #0f3331;
  --primary-soft-strong: #164744;
  --primary-text: #5fd5ce;
  --primary-contrast: #03201e;
  --success: #a3d977;
  --success-hover: #b8e394;
  --success-soft: #1d2f14;
  --success-contrast: #132009;
  --ai: #b7a6ff;
  --ai-soft: #272148;
  --info: #7ab6ff;
  --info-soft: #14304c;
  --warning: #f0b95a;
  --warning-soft: #36290f;
  --danger: #ff8a97;
  --danger-hover: #ffa9b2;
  --danger-soft: #3f1c24;

  --bubble-in: #172129;
  --bubble-out: #10302e;
  --bubble-out-border: #1d4a46;
  --bubble-ai: #1f1b38;
  --bubble-ai-border: #362d63;

  --focus: #5fd5ce;
  --focus-ring: rgba(95, 213, 206, 0.36);
  --overlay: rgba(2, 6, 10, 0.72);
  --shadow-xs: 0 1px 2px rgba(0, 0, 0, 0.3);
  --shadow-sm: 0 1px 2px rgba(0, 0, 0, 0.3), 0 4px 12px rgba(0, 0, 0, 0.24);
  --shadow-md: 0 2px 6px rgba(0, 0, 0, 0.3), 0 12px 30px rgba(0, 0, 0, 0.36);
  --shadow-lg: 0 8px 20px rgba(0, 0, 0, 0.32), 0 28px 72px rgba(0, 0, 0, 0.56);
}

/* ---------- Base ---------- */
*, *::before, *::after { box-sizing: border-box; }
html, body { width: 100%; min-height: 100%; }
html { background: var(--bg-app); }
body {
  margin: 0;
  background: var(--bg-app);
  color: var(--text);
  font-size: var(--text-base);
  line-height: 1.45;
  text-rendering: optimizeLegibility;
  -webkit-font-smoothing: antialiased;
  -moz-osx-font-smoothing: grayscale;
}
body.modal-open { overflow: hidden; }
button, input, select, textarea { font: inherit; }
button, a, input, select, textarea { -webkit-tap-highlight-color: transparent; }
button { color: inherit; }
a { color: var(--primary-text); }
::selection { background: var(--primary); color: var(--primary-contrast); }
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
kbd {
  display: inline-flex;
  align-items: center;
  min-width: 20px;
  height: 20px;
  padding: 0 5px;
  border: 1px solid var(--border-strong);
  border-bottom-width: 2px;
  border-radius: 5px;
  background: var(--surface);
  color: var(--text-muted);
  font: 600 var(--text-xs)/1 var(--font-mono);
  justify-content: center;
}

::-webkit-scrollbar { width: 10px; height: 10px; }
::-webkit-scrollbar-track { background: transparent; }
::-webkit-scrollbar-thumb { background: color-mix(in srgb, var(--text-subtle) 34%, transparent); border: 3px solid transparent; border-radius: 999px; background-clip: padding-box; }
::-webkit-scrollbar-thumb:hover { background: color-mix(in srgb, var(--text-muted) 55%, transparent); border: 3px solid transparent; background-clip: padding-box; }

:where(button, a, input, select, textarea, summary, [tabindex]):focus-visible {
  outline: 2px solid var(--focus);
  outline-offset: 2px;
  box-shadow: 0 0 0 4px var(--focus-ring);
}
:where(input, select, textarea):focus-visible { outline-offset: 0; }
:where(input[type="radio"], input[type="checkbox"]):focus-visible { outline-offset: 2px; box-shadow: none; }
.ai-mode-option input:focus-visible, .check-field input:focus-visible { outline: 0; }

/* ---------- Buttons ---------- */
.btn, .icon-btn, .queue-nav-btn, .ticket-row, .menu-summary {
  border: 0;
  cursor: pointer;
  transition: background-color var(--transition), border-color var(--transition), color var(--transition), box-shadow var(--transition), transform var(--transition), opacity var(--transition);
}
.btn {
  min-height: 36px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 7px;
  padding: 7px 13px;
  border: 1px solid var(--border-strong);
  border-radius: var(--radius-sm);
  background: var(--surface);
  color: var(--text-strong);
  font-weight: 600;
  font-size: var(--text-md);
  line-height: 1.2;
  white-space: nowrap;
  text-decoration: none;
  box-shadow: var(--shadow-xs);
}
.btn .icon { width: 16px; height: 16px; }
.btn:hover:not(:disabled) { background: var(--surface-hover); border-color: color-mix(in srgb, var(--text-subtle) 45%, var(--border-strong)); }
.btn:active:not(:disabled) { transform: translateY(1px); }
.btn.primary { background: var(--primary); border-color: var(--primary); color: var(--primary-contrast); box-shadow: 0 1px 2px color-mix(in srgb, var(--primary) 30%, transparent); }
.btn.primary:hover:not(:disabled) { background: var(--primary-hover); border-color: var(--primary-hover); }
.btn.success-action { background: var(--success); border-color: var(--success); color: var(--success-contrast); box-shadow: 0 1px 2px color-mix(in srgb, var(--success) 30%, transparent); }
.btn.success-action:hover:not(:disabled) { background: var(--success-hover); border-color: var(--success-hover); }
.btn.danger { background: var(--surface); color: var(--danger); border-color: color-mix(in srgb, var(--danger) 38%, var(--border)); }
.btn.danger:hover:not(:disabled) { background: var(--danger-soft); border-color: color-mix(in srgb, var(--danger) 60%, var(--border)); }
.btn.ghost { background: transparent; border-color: transparent; box-shadow: none; color: var(--text-muted); }
.btn.ghost:hover:not(:disabled) { background: var(--surface-muted); border-color: transparent; color: var(--text-strong); }
.btn.small { min-height: 30px; padding: 4px 10px; font-size: var(--text-sm); gap: 5px; }
.btn.small .icon { width: 14px; height: 14px; }
.btn:disabled, .icon-btn:disabled {
  cursor: not-allowed;
  transform: none;
  color: var(--text-disabled);
  background: var(--surface-muted);
  border-color: var(--border);
  box-shadow: none;
}
.icon-btn:disabled { background: transparent; border-color: transparent; }
.btn[aria-busy="true"] { color: var(--text-muted); }
.btn[aria-busy="true"] .icon { animation: spin 0.8s linear infinite; }
.icon-btn {
  position: relative;
  width: 36px;
  height: 36px;
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
.icon-btn[aria-pressed="true"] { color: var(--primary-text); background: var(--primary-soft); }
.icon-btn.primary { color: var(--primary-contrast); background: var(--primary); }
.icon-btn.danger { color: var(--danger); }

/* ---------- Tooltips (placement-aware) ---------- */
:where([data-tooltip]) { position: relative; }
[data-tooltip]::after {
  content: attr(data-tooltip);
  position: absolute;
  z-index: var(--z-tooltip);
  left: 50%;
  top: calc(100% + 6px);
  transform: translate(-50%, -2px);
  padding: 5px 8px;
  border-radius: var(--radius-sm);
  background: var(--text-strong);
  color: var(--surface);
  font-size: var(--text-xs);
  font-weight: 600;
  line-height: 1.25;
  letter-spacing: 0;
  white-space: nowrap;
  pointer-events: none;
  opacity: 0;
  visibility: hidden;
  transition: opacity var(--duration-fast) ease, transform var(--duration-fast) ease, visibility var(--duration-fast) ease;
}
[data-tooltip-placement="top"]::after { top: auto; bottom: calc(100% + 6px); transform: translate(-50%, 2px); }
[data-tooltip-placement="start"]::after { left: auto; right: 0; transform: translate(0, -2px); }
[data-tooltip]:hover::after, [data-tooltip]:focus-visible::after { opacity: 1; visibility: visible; transform: translate(-50%, 0); transition-delay: 350ms; }
[data-tooltip-placement="start"]:hover::after, [data-tooltip-placement="start"]:focus-visible::after { transform: translate(0, 0); }
@media (hover: none) { [data-tooltip]::after { display: none; } }

/* ---------- Fields ---------- */
.field { display: grid; align-content: start; gap: 6px; margin: 0 0 16px; }
.field label, .field-label { color: var(--text-strong); font-size: var(--text-sm); font-weight: 600; }
.required-mark { color: var(--danger); }
.field-hint { color: var(--text-muted); font-size: var(--text-sm); }
.field-error { color: var(--danger); font-size: var(--text-sm); font-weight: 600; }
.input, .field input, .field select, .field textarea {
  width: 100%;
  min-height: 38px;
  border: 1px solid var(--border-strong);
  border-radius: var(--radius-sm);
  background: var(--surface-input);
  color: var(--text-strong);
  padding: 8px 11px;
  box-shadow: var(--shadow-xs);
  transition: border-color var(--transition), box-shadow var(--transition), background-color var(--transition);
}
.field textarea { line-height: 1.45; }
.field input::placeholder, .field textarea::placeholder { color: var(--text-subtle); }
.field input:hover:not(:disabled), .field select:hover:not(:disabled), .field textarea:hover:not(:disabled) { border-color: color-mix(in srgb, var(--text-subtle) 60%, var(--border-strong)); }
.field input:focus-visible, .field select:focus-visible, .field textarea:focus-visible { border-color: var(--focus); }
.field input:disabled, .field select:disabled, .field textarea:disabled { background: var(--surface-muted); color: var(--text-muted); cursor: not-allowed; }
.field input[aria-invalid="true"], .field select[aria-invalid="true"], .field textarea[aria-invalid="true"] { border-color: var(--danger); box-shadow: 0 0 0 3px color-mix(in srgb, var(--danger) 16%, transparent); }
select { cursor: pointer; }
.password-field { position: relative; }
.password-field input { padding-right: 44px; }
.password-toggle { position: absolute; top: 50%; right: 3px; transform: translateY(-50%); width: 34px; height: 34px; }

/* ---------- Chips / badges (one family) ---------- */
.meta-chip, .status-badge, .role-badge, .channel-pill, .contact-source, .status-dot, .ai-status-pill {
  max-width: 100%;
  width: fit-content;
  min-height: 22px;
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 2px 8px;
  border: 1px solid transparent;
  border-radius: var(--radius-full);
  font-size: var(--text-xs);
  font-weight: 600;
  line-height: 1.2;
  letter-spacing: 0;
  text-transform: none;
  white-space: nowrap;
}
.meta-chip .icon, .channel-pill .icon { width: 13px; height: 13px; }
.meta-chip { background: var(--surface-muted); color: var(--text-muted); overflow: hidden; text-overflow: ellipsis; }
.meta-chip > span { min-width: 0; overflow: hidden; text-overflow: ellipsis; }
.channel-pill { background: var(--info-soft); color: var(--info); }
.status-badge::before, .status-dot::before, .ai-status-pill::before { content: ""; width: 6px; height: 6px; flex: 0 0 auto; border-radius: 50%; background: currentColor; }
.status-new { background: var(--info-soft); color: var(--info); }
.status-assigned, .status-in-progress { background: var(--primary-soft); color: var(--primary-text); }
.status-waiting-customer, .status-waiting-internal { background: var(--warning-soft); color: var(--warning); }
.status-resolved, .status-closed { background: var(--success-soft); color: var(--success); }
.status-default { background: var(--surface-muted); color: var(--text-muted); }
.role-badge { background: var(--surface-muted); color: var(--text-muted); text-transform: capitalize; }
.contact-source { background: var(--surface-muted); color: var(--text-muted); }
.contact-source.google { background: var(--info-soft); color: var(--info); }
.contact-source.vcf { background: var(--primary-soft); color: var(--primary-text); }
.status-dot { background: var(--success-soft); color: var(--success); }
.status-dot.off { background: var(--surface-muted); color: var(--text-muted); }
.status-dot.conflict { margin-top: 5px; background: var(--warning-soft); color: var(--warning); }
.ai-status-pill { min-height: 28px; padding: 4px 11px; font-size: var(--text-sm); background: var(--ai-soft); color: var(--ai); font-weight: 700; }
.ai-status-pill.off { background: var(--surface-muted); color: var(--text-muted); }

/* ---------- Feedback banners ---------- */
.login-error, .error-banner, .staff-feedback {
  border: 1px solid color-mix(in srgb, var(--danger) 28%, var(--border));
  border-left-width: 3px;
  border-left-color: var(--danger);
  border-radius: var(--radius-sm);
  background: var(--danger-soft);
  color: var(--danger);
  font-size: var(--text-md);
  font-weight: 500;
}
.staff-feedback { display: none; margin-bottom: 14px; padding: 10px 12px; }
.staff-feedback.success-box { border-color: color-mix(in srgb, var(--success) 30%, var(--border)); border-left-color: var(--success); background: var(--success-soft); color: var(--success); }
.form-note { padding: 10px 12px; border: 1px solid var(--border); border-radius: var(--radius-sm); background: var(--bg-subtle); color: var(--text-muted); font-size: var(--text-sm); line-height: 1.5; }
.form-note strong { color: var(--text-strong); }

/* ---------- Session + login ---------- */
.login-page {
  min-height: 100vh;
  min-height: 100dvh;
  display: grid;
  place-items: center;
  padding: 28px;
  background:
    radial-gradient(1200px 520px at 50% -10%, color-mix(in srgb, var(--primary) 10%, transparent), transparent 70%),
    var(--bg-app);
}
.session-loading-content { display: grid; justify-items: center; gap: 12px; text-align: center; }
.session-loading-content strong { color: var(--text-strong); font-size: var(--text-lg); font-weight: 650; }
.session-loading-content p { margin: 0; color: var(--text-muted); }
.session-loading-content .btn { margin-top: 4px; }
.session-spinner { width: 22px; height: 22px; border: 2.5px solid var(--primary-soft-strong); border-top-color: var(--primary); border-radius: 50%; animation: spin 0.8s linear infinite; }
#sessionRetryBtn:not(.hidden) ~ .session-spinner, .session-loading-content:has(#sessionRetryBtn:not(.hidden)) .session-spinner { display: none; }
.login-shell { width: min(420px, 100%); }
.login-theme-row { display: flex; justify-content: flex-end; margin-bottom: 10px; }
.login-card {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-xl);
  box-shadow: var(--shadow-md);
  overflow: hidden;
}
.login-card-accent { display: none; }
.login-content { padding: 32px; }
.login-brand { display: flex; gap: 12px; align-items: center; margin-bottom: 28px; }
.brand-logo {
  width: 40px;
  height: 40px;
  display: grid;
  place-items: center;
  flex: 0 0 auto;
  border-radius: 11px;
  background: linear-gradient(145deg, color-mix(in srgb, var(--primary) 82%, #ffffff), var(--primary));
  color: var(--primary-contrast);
  box-shadow: 0 6px 16px color-mix(in srgb, var(--primary) 26%, transparent), inset 0 1px 0 rgba(255, 255, 255, 0.18);
}
.login-brand-copy strong { display: block; color: var(--text-strong); font-size: var(--text-base); font-weight: 650; }
.login-brand-copy span { color: var(--text-muted); font-size: var(--text-sm); }
.login-card h1 { margin: 0; color: var(--text-strong); font-size: var(--text-2xl); font-weight: 700; line-height: 1.2; letter-spacing: -0.02em; }
.login-intro { margin: 8px 0 24px; color: var(--text-muted); font-size: var(--text-md); line-height: 1.55; }
.login-card .field input { min-height: 42px; }
.login-card .btn[type="submit"] { width: 100%; min-height: 42px; margin-top: 4px; font-size: var(--text-base); }
.login-error { display: none; margin-top: 14px; padding: 10px 12px; }
.login-security { display: flex; gap: 8px; align-items: flex-start; margin-top: 22px; padding-top: 18px; border-top: 1px solid var(--border); color: var(--text-muted); font-size: var(--text-sm); }
.login-security .icon { color: var(--primary-text); }

/* ---------- App shell ---------- */
.app-shell {
  height: 100vh;
  height: 100dvh;
  display: grid;
  grid-template-columns: var(--sidebar-width) var(--queue-width) minmax(0, 1fr);
  grid-template-rows: minmax(0, 1fr);
  overflow: hidden;
  background: var(--bg-app);
}

/* ---------- Sidebar ---------- */
.global-sidebar {
  min-width: 0;
  min-height: 0;
  display: flex;
  flex-direction: column;
  overflow-y: auto;
  overflow-x: hidden;
  background: var(--bg-subtle);
  border-right: 1px solid var(--border);
  z-index: 4;
}
.brand-bar {
  min-height: var(--header-height);
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 12px 12px 12px 16px;
  border-bottom: 1px solid var(--border);
}
.brand-bar .brand-logo { width: 34px; height: 34px; border-radius: 9px; }
.brand-bar .brand-logo .icon { width: 19px; height: 19px; }
.brand-copy { min-width: 0; flex: 1; }
.brand-copy strong { display: block; color: var(--text-strong); font-size: var(--text-base); font-weight: 650; letter-spacing: -0.01em; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.brand-copy span { display: block; color: var(--text-subtle); font-size: var(--text-xs); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.sidebar-label, .nav-group-label { padding: 16px 10px 6px; color: var(--text-subtle); font-size: var(--text-xs); font-weight: 600; letter-spacing: 0.02em; }
.nav-group-label:first-child { padding-top: 12px; }
.queue-nav { display: flex; flex-direction: column; gap: 2px; padding: 0 8px; }
.queue-nav-btn {
  position: relative;
  width: 100%;
  min-height: 36px;
  display: grid;
  grid-template-columns: 18px minmax(0, 1fr) auto;
  align-items: center;
  gap: 10px;
  padding: 6px 10px;
  border-radius: var(--radius-sm);
  background: transparent;
  color: var(--text-muted);
  text-align: left;
  font-size: var(--text-md);
  font-weight: 500;
}
.queue-nav-btn .icon { width: 17px; height: 17px; }
.queue-nav-btn:hover { background: var(--surface-muted); color: var(--text-strong); }
.queue-nav-btn.active { background: var(--surface); color: var(--text-strong); font-weight: 600; box-shadow: var(--shadow-xs), inset 0 0 0 1px var(--border); }
.queue-nav-btn.active::before { content: ""; position: absolute; left: -8px; top: 8px; bottom: 8px; width: 3px; border-radius: 0 3px 3px 0; background: var(--primary); }
.queue-nav-btn.active .icon { color: var(--primary-text); }
.queue-nav-label { min-width: 0; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.queue-nav-count {
  min-width: 22px;
  height: 20px;
  display: inline-grid;
  place-items: center;
  padding: 0 6px;
  border-radius: var(--radius-full);
  background: var(--surface-muted);
  color: var(--text-muted);
  font-size: var(--text-xs);
  font-weight: 600;
  font-variant-numeric: tabular-nums;
}
.queue-nav-btn.active .queue-nav-count { background: var(--primary-soft); color: var(--primary-text); }
.queue-nav-btn[data-view="unassigned"] .queue-nav-count:not(:empty):not([data-zero]) { background: var(--primary); color: var(--primary-contrast); }
.queue-nav-count:empty { display: none; }
.nav-spacer { flex: 1; min-height: 14px; }
.account-zone { flex: 0 0 auto; display: grid; gap: 2px; padding: 8px 8px 10px; border-top: 1px solid var(--border); }
.message-preferences-slot, .whatsapp-manage-slot { margin: 0; }
.message-preferences-slot .btn, .whatsapp-manage-slot .btn, .staff-actions .btn { width: 100%; min-height: 34px; justify-content: flex-start; padding: 6px 10px; font-weight: 500; }
.agent-card { margin-top: 6px; padding: 10px; border: 1px solid var(--border); border-radius: var(--radius-md); background: var(--surface); box-shadow: var(--shadow-xs); }
.agent-main { display: flex; align-items: center; gap: 10px; min-width: 0; }
.avatar {
  width: 34px;
  height: 34px;
  display: grid;
  place-items: center;
  flex: 0 0 auto;
  border-radius: 50%;
  background: var(--primary-soft);
  color: var(--primary-text);
  font-size: var(--text-xs);
  font-weight: 700;
  letter-spacing: 0.02em;
  text-transform: uppercase;
  user-select: none;
}
.avatar.large { width: 40px; height: 40px; font-size: var(--text-sm); }
.avatar.customer { background: var(--surface-muted); color: var(--text-muted); box-shadow: inset 0 0 0 1px var(--border); }
.avatar.ai { background: var(--ai-soft); color: var(--ai); }
.agent-copy { min-width: 0; flex: 1; }
.agent-copy strong { display: block; color: var(--text-strong); font-size: var(--text-md); font-weight: 600; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.agent-copy span { display: block; color: var(--text-muted); font-size: var(--text-xs); text-transform: capitalize; }
.staff-actions { display: grid; grid-template-columns: 1fr 1fr; gap: 4px; margin-top: 8px; padding-top: 8px; border-top: 1px solid var(--border); }
.staff-actions:has(#manageStaffBtn.hidden) { grid-template-columns: 1fr; }
.staff-actions .btn { min-height: 32px; justify-content: center; font-size: var(--text-sm); }

/* ---------- Queue panel ---------- */
.inbox-panel {
  position: relative;
  min-width: 0;
  min-height: 0;
  display: grid;
  grid-template-columns: minmax(0, 1fr);
  grid-template-rows: auto auto auto minmax(0, 1fr) auto;
  background: var(--surface);
  border-right: 1px solid var(--border);
  container-type: inline-size;
  container-name: queue;
}
.inbox-header { grid-row: 1; min-width: 0; min-height: var(--header-height); display: flex; align-items: center; justify-content: space-between; gap: 10px; padding: 10px 12px 10px 16px; border-bottom: 1px solid var(--border); }
.inbox-header > div:first-child { min-width: 0; }
.inbox-header-actions { display: flex; align-items: center; gap: 6px; flex: 0 0 auto; }
.eyebrow { color: var(--text-subtle); font-size: var(--text-xs); font-weight: 600; letter-spacing: 0.02em; }
.inbox-title-row { display: flex; flex-wrap: wrap; align-items: baseline; column-gap: 8px; min-width: 0; }
.inbox-title-row h1 { flex: 0 1 auto; min-width: 0; margin: 0; color: var(--text-strong); font-size: var(--text-xl); font-weight: 700; line-height: 1.25; letter-spacing: -0.015em; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.queue-total { color: var(--text-muted); font-size: var(--text-sm); white-space: nowrap; font-variant-numeric: tabular-nums; }
#startChatBtn { min-width: 30px; }
@container queue (max-width: 379px) {
  #startChatBtn { width: 32px; padding: 0; }
  #startChatBtn .optional-label { display: none; }
}
@container queue (max-width: 299px) {
  .ai-header-control .ai-header-copy { display: none; }
}
.ai-header-control {
  min-height: 30px;
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 4px 10px;
  border: 1px solid var(--border);
  border-radius: var(--radius-full);
  background: var(--surface);
  color: var(--text-muted);
  font-size: var(--text-xs);
  font-weight: 600;
  cursor: pointer;
  transition: background-color var(--transition), border-color var(--transition);
}
.ai-header-control:hover { border-color: var(--border-strong); background: var(--surface-hover); }
.ai-header-control .ai-header-state { color: var(--text-strong); font-weight: 700; }
.ai-header-control:has(.ai-status-dot.on) { border-color: color-mix(in srgb, var(--ai) 34%, var(--border)); background: var(--ai-soft); color: var(--ai); }
.ai-header-control:has(.ai-status-dot.on) .ai-header-state { color: var(--ai); }
.ai-status-dot { width: 7px; height: 7px; flex: 0 0 auto; border-radius: 50%; background: var(--text-subtle); }
.ai-status-dot.on { background: var(--ai); box-shadow: 0 0 0 3px color-mix(in srgb, var(--ai) 20%, transparent); }
.ai-status-dot.off { background: transparent; box-shadow: inset 0 0 0 1.5px var(--text-subtle); }
.search-wrap { grid-row: 2; padding: 10px 12px; border-bottom: 1px solid var(--border); }
.search-field { position: relative; }
.search-field > .icon { position: absolute; z-index: 1; left: 11px; top: 50%; width: 16px; height: 16px; transform: translateY(-50%); color: var(--text-subtle); pointer-events: none; }
.search-field input { width: 100%; min-height: 36px; padding: 7px 64px 7px 34px; border: 1px solid var(--border); border-radius: var(--radius-sm); background: var(--bg-subtle); color: var(--text-strong); font-size: var(--text-md); transition: border-color var(--transition), background-color var(--transition); }
.search-field input:hover { border-color: var(--border-strong); }
.search-field input:focus-visible { background: var(--surface-input); border-color: var(--focus); }
.search-field input::placeholder { color: var(--text-subtle); }
.search-field input::-webkit-search-cancel-button { display: none; }
.search-kbd { position: absolute; right: 8px; top: 50%; transform: translateY(-50%); display: inline-flex; gap: 3px; pointer-events: none; }
.search-field input:not(:placeholder-shown) ~ .search-kbd, .search-field input:focus ~ .search-kbd { display: none; }
.search-clear { position: absolute; right: 3px; top: 50%; transform: translateY(-50%); width: 30px; height: 30px; }
.search-scope { margin-top: 6px; color: var(--text-subtle); font-size: var(--text-xs); line-height: 1.4; display: -webkit-box; -webkit-box-orient: vertical; -webkit-line-clamp: 2; overflow: hidden; }
.queue {
  grid-row: 4;
  min-height: 0;
  overflow: auto;
  overscroll-behavior: contain;
  background: var(--surface);
}
.ticket-row {
  width: 100%;
  position: relative;
  display: block;
  padding: 11px 14px 11px 16px;
  border-radius: 0;
  border-bottom: 1px solid var(--border);
  background: transparent;
  color: var(--text);
  text-align: left;
}
.ticket-row:hover { background: var(--surface-hover); }
.ticket-row.active { background: var(--surface-selected); }
.ticket-row.active::before { content: ""; position: absolute; left: 0; top: 0; bottom: 0; width: 3px; background: var(--primary); }
.ticket-row:focus-visible { z-index: 1; outline-offset: -2px; box-shadow: none; }
.ticket-row-head { display: grid; grid-template-columns: auto minmax(0, 1fr) auto; gap: 10px; align-items: center; }
.ticket-row .avatar { width: 32px; height: 32px; }
.ticket-row-copy { min-width: 0; }
.ticket-customer { color: var(--text-strong); font-size: var(--text-md); font-weight: 600; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.ticket-reference { margin-top: 1px; color: var(--text-subtle); font-family: var(--font-mono); font-size: var(--text-xs); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.ticket-time { align-self: start; display: inline-flex; align-items: center; gap: 3px; padding-top: 1px; color: var(--text-subtle); font-size: var(--text-xs); font-variant-numeric: tabular-nums; white-space: nowrap; }
.ticket-time .icon { width: 12px; height: 12px; }
.ticket-time.age-warn { color: var(--warning); font-weight: 600; }
.ticket-time.age-alert { padding: 1px 6px; border-radius: var(--radius-full); background: var(--danger-soft); color: var(--danger); font-weight: 600; }
.ticket-summary { display: -webkit-box; margin: 6px 0 8px 42px; overflow: hidden; color: var(--text-muted); font-size: var(--text-sm); line-height: 1.45; -webkit-box-orient: vertical; -webkit-line-clamp: 2; overflow-wrap: anywhere; }
.ticket-row-meta { display: flex; flex-wrap: wrap; align-items: center; gap: 4px; margin-left: 42px; }
.contact-row { width: 100%; display: grid; grid-template-columns: 34px minmax(0, 1fr) auto; gap: 10px; align-items: center; padding: 10px 14px 10px 16px; border: 0; border-bottom: 1px solid var(--border); background: transparent; color: var(--text); text-align: left; cursor: pointer; transition: background-color var(--transition); }
.contact-row:hover { background: var(--surface-hover); }
.contact-row:focus-visible { outline-offset: -2px; box-shadow: none; }
.contact-row-copy { min-width: 0; }
.contact-row-name { color: var(--text-strong); font-size: var(--text-md); font-weight: 600; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.contact-row-phone { margin-top: 1px; color: var(--text-muted); font-size: var(--text-xs); font-family: var(--font-mono); }
.contact-row-meta { margin-top: 5px; display: flex; gap: 6px; flex-wrap: wrap; align-items: center; color: var(--text-subtle); font-size: var(--text-xs); }
.contact-row .btn.small, .contact-chat-chip { min-height: 28px; display: inline-flex; align-items: center; gap: 5px; padding: 3px 10px; border: 1px solid var(--border-strong); border-radius: var(--radius-full); background: var(--surface); color: var(--primary-text); font-size: var(--text-sm); font-weight: 600; }
.contact-row:hover .contact-chat-chip { border-color: var(--primary); background: var(--primary-soft); }
.contacts-toolbar { grid-row: 3; display: flex; align-items: center; gap: 8px; flex-wrap: wrap; padding: 10px 12px; border-bottom: 1px solid var(--border); background: var(--bg-subtle); }
.contacts-google-copy { min-width: 140px; flex: 1; }
.contacts-google-copy strong { display: block; color: var(--text-strong); font-size: var(--text-sm); font-weight: 600; }
.contacts-google-copy span { display: block; margin-top: 1px; color: var(--text-muted); font-size: var(--text-xs); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.contacts-toolbar-actions { display: flex; gap: 5px; flex-wrap: wrap; justify-content: flex-end; }
.contact-picker-list { max-height: 260px; overflow: auto; border: 1px solid var(--border); border-radius: var(--radius-md); background: var(--surface); }
.contact-picker-row { position: relative; width: 100%; display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 8px; align-items: center; padding: 9px 12px; border: 0; border-bottom: 1px solid var(--border); background: transparent; color: var(--text); text-align: left; cursor: pointer; transition: background-color var(--transition); }
.contact-picker-row:last-child { border-bottom: 0; }
.contact-picker-row:hover { background: var(--surface-hover); }
.contact-picker-row.selected { background: var(--surface-selected); box-shadow: inset 3px 0 0 var(--primary); }
.contact-picker-row strong { display: block; color: var(--text-strong); font-size: var(--text-md); font-weight: 600; }
.contact-picker-row span { display: block; margin-top: 1px; color: var(--text-muted); font-size: var(--text-xs); }
.contact-picker-row .contact-source { display: inline-flex; margin-top: 0; }
.contact-picker-row.selected .contact-source { background: var(--primary); color: var(--primary-contrast); }
.contact-picker-section { margin-top: 14px; }
.contact-picker-section-title { margin: 0 0 6px; color: var(--text-subtle); font-size: var(--text-xs); font-weight: 600; letter-spacing: 0.02em; }
.queue-footer { grid-row: 5; min-height: 48px; display: flex; align-items: center; justify-content: space-between; gap: 8px; padding: 6px 8px 6px 16px; border-top: 1px solid var(--border); background: var(--surface); }
.page-summary { min-width: 0; color: var(--text-subtle); font-size: var(--text-xs); font-variant-numeric: tabular-nums; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.pagination-actions { display: flex; gap: 2px; flex: 0 0 auto; }
.pagination-actions .icon-btn { width: 32px; height: 32px; }

/* ---------- Loading / empty states ---------- */
.loading-list { display: grid; gap: 0; }
.skeleton-row { padding: 14px 16px; border-bottom: 1px solid var(--border); }
.skeleton { position: relative; overflow: hidden; border-radius: var(--radius-sm); background: var(--surface-muted); }
.skeleton::after { content: ""; position: absolute; inset: 0; transform: translateX(-100%); background: linear-gradient(90deg, transparent, color-mix(in srgb, var(--surface) 64%, transparent), transparent); animation: shimmer 1.45s infinite; }
.skeleton-line { height: 10px; margin-bottom: 9px; }
.skeleton-line:last-child { margin-bottom: 0; }
.skeleton-line.short { width: 38%; }
.skeleton-line.medium { width: 62%; height: 12px; }
.skeleton-line.long { width: 90%; }
.empty-state { min-height: 220px; display: grid; place-items: center; padding: 32px 24px; text-align: center; }
.empty-state-inner { max-width: 290px; }
.empty-icon { width: 44px; height: 44px; display: grid; place-items: center; margin: 0 auto 12px; border-radius: 12px; background: var(--surface-muted); color: var(--text-subtle); box-shadow: inset 0 0 0 1px var(--border); }
.empty-state h3, .conversation-empty h3 { margin: 0; color: var(--text-strong); font-size: var(--text-base); font-weight: 650; }
.empty-state p, .conversation-empty p { margin: 6px 0 0; color: var(--text-muted); font-size: var(--text-md); line-height: 1.5; }
.empty-state .btn { margin-top: 14px; }

/* ---------- Workspace ---------- */
.main {
  min-width: 0;
  min-height: 0;
  display: grid;
  grid-template-rows: auto minmax(0, 1fr) auto;
  grid-template-columns: minmax(0, 1fr);
  background: var(--bg-app);
  position: relative;
}
.workspace-header {
  grid-row: 1;
  grid-column: 1;
  min-width: 0;
  min-height: var(--header-height);
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px 12px 10px 20px;
  border-bottom: 1px solid var(--border);
  background: var(--surface);
  z-index: 3;
  container-type: inline-size;
  container-name: workspace-header;
}
.mobile-back { display: none; }
.ticket-heading-avatar { display: none; }
.ticket-heading { min-width: 0; flex: 1 1 auto; }
.ticket-heading-line { display: flex; align-items: center; flex-wrap: wrap; gap: 6px 8px; min-width: 0; }
.ticket-heading h2 { max-width: 100%; min-width: 0; margin: 0; color: var(--text-strong); font-size: var(--text-lg); font-weight: 650; line-height: 1.3; letter-spacing: -0.01em; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.ticket-subtitle { margin-top: 2px; color: var(--text-muted); font-size: var(--text-sm); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.ticket-history-shortcut { max-width: 100%; min-height: 24px; display: inline-flex; align-items: center; gap: 5px; margin-top: 6px; margin-right: 4px; padding: 2px 9px; border: 1px solid color-mix(in srgb, var(--primary) 28%, var(--border)); border-radius: var(--radius-full); background: var(--primary-soft); color: var(--primary-text); font-size: var(--text-xs); font-weight: 600; white-space: nowrap; cursor: pointer; transition: border-color var(--transition), background-color var(--transition); }
.ticket-history-shortcut:hover, .ticket-history-shortcut:focus-visible { border-color: var(--primary); background: var(--primary-soft-strong); }
.ticket-history-shortcut .icon { width: 13px; height: 13px; }
.ticket-history-shortcut-compact { display: none; }
.ticket-header-meta { display: inline-flex; flex-wrap: wrap; gap: 4px; margin-top: 6px; vertical-align: top; }
.ticket-header-meta:empty { display: none; }
.actions { display: flex; align-items: center; justify-content: flex-end; flex-wrap: nowrap; gap: 6px; flex: 0 0 auto; }
@container workspace-header (max-width: 900px) {
  .actions > .btn:not(.primary):not(.success-action) .action-label, .actions .menu-summary .action-label { display: none; }
  .actions > .btn:not(.primary):not(.success-action), .actions .menu-summary { min-width: 36px; padding-inline: 9px; }
}
@container workspace-header (max-width: 640px) {
  .ticket-history-shortcut-full { display: none; }
  .ticket-history-shortcut .ticket-history-shortcut-compact { display: inline; }
  .ticket-header-meta { display: none; }
}
@container workspace-header (max-width: 470px) {
  .actions > .btn .action-label { display: none; }
  .actions > .btn { min-width: 36px; padding-inline: 9px; }
}
.actions > .btn.danger { margin-left: 4px; }
.actions > .btn.success-action + .btn.danger, .actions > .action-menu + .btn.danger { margin-left: 4px; }
.header-tools { display: flex; align-items: center; gap: 2px; padding-left: 6px; margin-left: 2px; border-left: 1px solid var(--border); }
.context-toggle { display: inline-grid; }
.action-menu { position: relative; }
.action-menu[open] { z-index: var(--z-popover); }
.menu-summary { list-style: none; min-height: 36px; display: inline-flex; align-items: center; gap: 7px; padding: 7px 11px; border: 1px solid var(--border-strong); border-radius: var(--radius-sm); background: var(--surface); color: var(--text-strong); font-weight: 600; font-size: var(--text-md); box-shadow: var(--shadow-xs); }
.menu-summary .icon { width: 16px; height: 16px; }
.menu-summary::-webkit-details-marker { display: none; }
.menu-summary:hover { background: var(--surface-hover); }
.action-menu[open] > .menu-summary { background: var(--surface-muted); }
.action-menu-panel { position: absolute; top: calc(100% + 6px); right: 0; width: max-content; min-width: 210px; padding: 5px; border: 1px solid var(--border); border-radius: var(--radius-md); background: var(--surface-raised); box-shadow: var(--shadow-md); animation: pop-in var(--duration-fast) var(--ease-out); }
.action-menu-panel button { width: 100%; min-height: 34px; justify-content: flex-start; border: 0; background: transparent; box-shadow: none; color: var(--text-strong); font-weight: 500; }
.action-menu-panel button:hover:not(:disabled) { background: var(--surface-muted); }
.workspace-loading { grid-row: 2; grid-column: 1; align-self: start; z-index: 6; width: 100%; height: 2px; overflow: hidden; background: transparent; }
.workspace-loading::after { content: ""; display: block; width: 35%; height: 100%; background: var(--primary); animation: indeterminate 1.1s ease-in-out infinite; }
.error-banner { grid-row: 2; grid-column: 1; align-self: start; z-index: 5; display: none; margin: 12px 16px 0; padding: 10px 12px; align-items: flex-start; gap: 9px; box-shadow: var(--shadow-sm); }
.error-banner.visible { display: flex; animation: pop-in var(--duration-base) var(--ease-out); }
.error-banner-copy { flex: 1; }
.error-banner .icon-btn { width: 26px; height: 26px; margin: -3px -4px -3px 0; color: var(--danger); }
.workspace-content { grid-row: 2; grid-column: 1; min-height: 0; display: grid; grid-template-columns: minmax(0, 1fr) var(--context-width); transition: grid-template-columns var(--duration-base) ease; }
.conversation-shell { min-width: 0; min-height: 0; position: relative; background: var(--bg-app); }
.layout-resizer { position: absolute; z-index: var(--z-resizer); top: 0; bottom: 0; width: 9px; touch-action: none; cursor: col-resize; outline: none; }
.layout-resizer::after { content: ""; position: absolute; top: 0; bottom: 0; left: 4px; width: 1px; background: transparent; transition: background-color var(--transition), width var(--transition), left var(--transition); }
.layout-resizer:hover::after, .layout-resizer:focus-visible::after, body.column-resizing .layout-resizer.active::after { left: 3px; width: 3px; background: var(--primary); }
.layout-resizer:focus-visible { box-shadow: none; }
.queue-resizer { right: -5px; }
.context-resizer { left: -5px; }
body.column-resizing { cursor: col-resize !important; user-select: none !important; }
body.column-resizing * { cursor: col-resize !important; }

/* ---------- Conversation timeline ---------- */
.conversation {
  height: 100%;
  overflow: auto;
  overscroll-behavior: contain;
  padding: 20px clamp(14px, 3vw, 32px) 28px;
  scroll-padding-bottom: 90px;
}
.conversation:focus-visible { outline-offset: -2px; box-shadow: none; }
.conversation-inner { width: min(920px, 100%); margin: 0 auto; }
.conversation-empty { height: 100%; min-height: 260px; display: grid; place-items: center; text-align: center; }
.conversation-empty .empty-icon { width: 52px; height: 52px; border-radius: 14px; background: var(--surface); }
.scroll-latest { position: absolute; z-index: 3; right: 20px; bottom: 16px; min-height: 32px; border-radius: var(--radius-full); border-color: var(--primary); background: var(--primary); color: var(--primary-contrast); box-shadow: var(--shadow-md); animation: pop-in var(--duration-base) var(--ease-out); }
.scroll-latest:hover:not(:disabled) { background: var(--primary-hover); border-color: var(--primary-hover); }
.date-divider { position: relative; width: fit-content; margin: 20px auto 10px; padding: 3px 11px; border: 1px solid var(--border); border-radius: var(--radius-full); background: var(--surface); color: var(--text-muted); font-size: var(--text-xs); font-weight: 600; line-height: 1.5; white-space: nowrap; pointer-events: none; -webkit-user-select: none; user-select: none; }
.context-divider { display: flex; align-items: center; gap: 7px; margin: 4px auto 16px; width: max-content; max-width: 100%; padding: 5px 11px; border: 1px dashed var(--border-strong); border-radius: var(--radius-full); color: var(--text-muted); background: var(--surface); font-size: var(--text-xs); font-weight: 600; }
.context-divider .icon { width: 13px; height: 13px; }
.previous-ticket-divider { display: flex; flex-wrap: wrap; align-items: baseline; gap: 4px 8px; margin: 22px 0 12px; padding: 9px 12px; border: 1px solid var(--border); border-radius: var(--radius-md); background: var(--surface-muted); color: var(--text-muted); font-size: var(--text-sm); }
.previous-ticket-divider strong { color: var(--text-strong); font-weight: 650; }
.previous-ticket-divider > span { margin-left: 0; }
.support-number-label { display: inline-block; max-width: 100%; vertical-align: bottom; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.support-number-label.has-alias { display: inline-grid; cursor: help; }
.support-number-label.has-alias > span { grid-area: 1 / 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; }
.support-number-label .support-number-raw { visibility: hidden; }
.support-number-label.has-alias:is(:hover, :focus) .support-number-alias,
.ticket-row:focus-visible .support-number-label.has-alias .support-number-alias { visibility: hidden; }
.support-number-label.has-alias:is(:hover, :focus) .support-number-raw,
.ticket-row:focus-visible .support-number-label.has-alias .support-number-raw { visibility: visible; }
.support-number-label:focus-visible { outline: 2px solid var(--focus); outline-offset: 2px; border-radius: 2px; box-shadow: none; }
.history-load-row { display: flex; justify-content: center; margin: 10px 0 16px; }
.history-load-row .btn { white-space: normal; text-align: center; border-radius: var(--radius-full); background: var(--surface); color: var(--primary-text); }
.message-row.previous-ticket-msg { opacity: 0.88; }
.message-row.previous-ticket-msg .message-bubble { box-shadow: none; }
.message-row { display: grid; grid-template-columns: 30px minmax(0, 1fr); align-items: end; gap: 8px; margin: 14px 0 0; }
.message-row.inbound + .message-row.inbound, .message-row.outbound + .message-row.outbound { margin-top: 6px; }
.message-row.outbound { grid-template-columns: minmax(0, 1fr) 30px; }
.message-row.outbound .message-avatar { grid-column: 2; }
.message-row.outbound .message-stack { grid-column: 1; grid-row: 1; justify-self: end; align-items: flex-end; }
.message-avatar { width: 30px; height: 30px; font-size: 10px; margin-bottom: 22px; }
.message-stack { position: relative; width: min(74%, 640px); min-width: 0; display: flex; flex-direction: column; align-items: flex-start; }
.message-bubble { position: relative; width: fit-content; max-width: 100%; padding: 8px 12px 9px; border: 1px solid var(--border); border-radius: 4px 14px 14px 14px; background: var(--bubble-in); box-shadow: var(--shadow-xs); user-select: text; -webkit-user-select: text; }
.message-row.outbound .message-bubble { border-radius: 14px 4px 14px 14px; border-color: var(--bubble-out-border); background: var(--bubble-out); }
.message-row.ai .message-bubble { border-color: var(--bubble-ai-border); background: var(--bubble-ai); }
.message-row.ai .message-sender { color: var(--ai); }
.message-row.system .message-bubble { background: var(--surface-muted); }
.message-row.context-msg { opacity: 0.84; }
.message-row.context-msg .message-bubble { border-style: dashed; box-shadow: none; }
.message-sender { display: flex; align-items: center; flex-wrap: wrap; gap: 6px; margin-bottom: 3px; color: var(--text-strong); font-size: var(--text-sm); font-weight: 650; }
.message-row.outbound .message-sender { color: var(--primary-text); }
.message-quote { width: min(100%, 520px); margin: 2px 0 7px; padding: 6px 10px; border-left: 3px solid var(--primary); border-radius: var(--radius-sm); background: color-mix(in srgb, var(--text-strong) 5%, transparent); color: var(--text-muted); font-size: var(--text-sm); }
.message-quote strong { display: block; margin-bottom: 1px; color: var(--primary-text); font-size: var(--text-xs); font-weight: 650; }
.message-quote span { display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.message-body {
  color: var(--text-strong);
  font-size: var(--text-base);
  line-height: 1.5;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  word-break: break-word;
  user-select: text !important;
  -webkit-user-select: text !important;
  cursor: text;
}
.message-body a { color: var(--primary-text); text-decoration: underline; text-decoration-thickness: 1px; text-underline-offset: 2px; overflow-wrap: anywhere; }
.message-body a:hover { color: var(--primary-hover); }
.message-body code { font-family: var(--font-mono); font-size: 0.92em; }
.message-search-highlight { background: color-mix(in srgb, var(--warning) 34%, transparent); color: inherit; border-radius: 3px; padding: 0 1px; }
.message-row.search-target .message-bubble { outline: 2px solid var(--primary); outline-offset: 3px; }
.conversation-search-toolbar { position: absolute; z-index: 12; top: 10px; right: 16px; width: min(440px, calc(100% - 32px)); display: flex; align-items: center; gap: 4px; padding: 5px 6px 5px 12px; border: 1px solid var(--border-strong); border-radius: var(--radius-md); background: var(--surface-raised); box-shadow: var(--shadow-md); animation: pop-in var(--duration-fast) var(--ease-out); }
.conversation-search-toolbar > .icon { color: var(--text-subtle); }
.conversation-search-toolbar .icon-btn { width: 30px; height: 30px; }
.conversation-search-toolbar.hidden { display: none; }
.conversation-search-input { min-width: 0; flex: 1; height: 32px; border: 0; outline: 0; background: transparent; color: var(--text-strong); font-size: var(--text-md); }
.conversation-search-input:focus-visible { box-shadow: none; outline: 0; }
.conversation-search-input::-webkit-search-cancel-button { display: none; }
.conversation-search-count { min-width: 48px; color: var(--text-muted); font-size: var(--text-xs); text-align: center; font-variant-numeric: tabular-nums; }
.search-result-row { width: 100%; display: block; padding: 11px 14px 11px 16px; border: 0; border-bottom: 1px solid var(--border); background: transparent; color: var(--text); text-align: left; cursor: pointer; transition: background-color var(--transition); }
.search-result-row:hover { background: var(--surface-hover); }
.search-result-row:focus-visible { outline-offset: -2px; box-shadow: none; }
.search-result-head { display: flex; align-items: center; justify-content: space-between; gap: 10px; }
.search-result-customer { min-width: 0; color: var(--text-strong); font-size: var(--text-md); font-weight: 600; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.search-result-time { flex: 0 0 auto; color: var(--text-subtle); font-size: var(--text-xs); }
.search-result-ref { margin-top: 1px; color: var(--text-subtle); font-size: var(--text-xs); font-family: var(--font-mono); }
.search-result-preview { margin-top: 6px; color: var(--text-muted); font-size: var(--text-sm); line-height: 1.45; overflow-wrap: anywhere; display: -webkit-box; -webkit-box-orient: vertical; -webkit-line-clamp: 3; overflow: hidden; }
.message-meta { min-height: 22px; display: flex; align-items: center; flex-wrap: wrap; gap: 4px 6px; margin-top: 4px; padding: 0 2px; color: var(--text-subtle); font-size: var(--text-xs); font-variant-numeric: tabular-nums; }
.message-row.outbound .message-meta { justify-content: flex-end; }

/* Compact message toolbar: buttons keep their text as the accessible name;
   the glyph is drawn with a CSS mask so the DOM stays unchanged. */
.message-actions {
  display: inline-flex;
  align-items: center;
  gap: 1px;
  padding: 2px;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--surface-raised);
  box-shadow: var(--shadow-sm);
  opacity: 0;
  visibility: hidden;
  transform: translateY(2px);
  transition: opacity var(--duration-fast) ease, transform var(--duration-fast) ease, visibility var(--duration-fast) ease;
}
.message-row.outbound .message-actions { order: -1; }
.message-row:hover .message-actions, .message-row:focus-within .message-actions, .message-actions:has(details[open]) { opacity: 1; visibility: visible; transform: none; }
.message-action-btn {
  position: relative;
  width: 26px;
  min-width: 26px;
  height: 26px;
  min-height: 26px;
  display: inline-grid;
  place-items: center;
  padding: 0;
  border: 0;
  border-radius: 5px;
  background: transparent;
  color: var(--text-muted);
  font-size: 0;
  line-height: 0;
  box-shadow: none;
  transition: background-color var(--transition), color var(--transition);
}
.message-action-btn > .icon { display: none; }
.message-action-btn::before {
  content: "";
  width: 15px;
  height: 15px;
  background: currentColor;
  -webkit-mask: var(--glyph) center / contain no-repeat;
  mask: var(--glyph) center / contain no-repeat;
}
.message-action-btn:hover:not(:disabled) { background: var(--surface-muted); color: var(--text-strong); }
.message-action-btn[data-action="delete"]:hover:not(:disabled) { background: var(--danger-soft); color: var(--danger); }
.message-action-btn:disabled { background: transparent; }
.message-action-btn[data-action="edit"] { --glyph: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='black' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='M4 20h4l11-11-4-4L4 16zM13.5 6.5l4 4'/%3E%3C/svg%3E"); }
.message-action-btn[data-action="copy"] { --glyph: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='black' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'%3E%3Crect x='8' y='8' width='12' height='12' rx='2'/%3E%3Cpath d='M16 8V5.5A1.5 1.5 0 0 0 14.5 4h-9A1.5 1.5 0 0 0 4 5.5v9A1.5 1.5 0 0 0 5.5 16H8'/%3E%3C/svg%3E"); }
.message-action-btn[data-action="delete"] { --glyph: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='black' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='M4 7h16M9 7V4h6v3M7 7l1 13h8l1-13M10 11v5M14 11v5'/%3E%3C/svg%3E"); }
.message-action-btn[data-action="dismiss"] { --glyph: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='black' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='m6 6 12 12M18 6 6 18'/%3E%3C/svg%3E"); }
.message-action-btn[data-action="reply"] { --glyph: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='black' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='m10 7-6 5 6 5v-3h3.5c3.3 0 5.6 1.2 7 3.5-.3-5.3-2.8-8-7.5-8H10z'/%3E%3C/svg%3E"); }
.message-action-btn[data-action="react"] { --glyph: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='black' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'%3E%3Ccircle cx='12' cy='12' r='8.5'/%3E%3Cpath d='M8.5 14.5c.9 1.3 2 2 3.5 2s2.6-.7 3.5-2M9 9.5h.01M15 9.5h.01'/%3E%3C/svg%3E"); }
.message-action-btn[data-action="forward"] { --glyph: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='black' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='m14 7 6 5-6 5v-3h-3.5c-3.3 0-5.6 1.2-7 3.5.3-5.3 2.8-8 7.5-8H14z'/%3E%3C/svg%3E"); }
.message-action-btn:not([data-action])::before { display: none; }
.message-action-btn:not([data-action]) { width: auto; padding: 0 7px; font-size: var(--text-xs); line-height: 1; }
@media (hover: none) {
  .message-actions { opacity: 1; visibility: visible; transform: none; padding: 0; border-color: transparent; background: transparent; box-shadow: none; }
  .message-action-btn { width: 36px; min-width: 36px; height: 36px; min-height: 36px; color: var(--text-subtle); }
}
@media (pointer: coarse) { .media-viewer-tools .btn.small, .image-editor-toolbar .btn.small { min-height: 40px; } }
.message-reactions { display: flex; flex-wrap: wrap; gap: 4px; margin: -6px 8px 0; position: relative; z-index: 1; }
.message-row.outbound .message-reactions { justify-content: flex-end; }
.reaction-chip { min-height: 22px; display: inline-flex; align-items: center; gap: 3px; padding: 1px 7px; border: 1px solid var(--border); border-radius: var(--radius-full); background: var(--surface-raised); color: var(--text); font-size: 13px; box-shadow: var(--shadow-xs); }
.reaction-chip span { color: var(--text-muted); font-size: var(--text-xs); font-weight: 600; }
.message-reaction-menu { position: relative; display: inline-flex; }
.message-reaction-menu > summary { list-style: none; }
.message-reaction-menu > summary::-webkit-details-marker { display: none; }
.message-reaction-menu.disabled > summary { pointer-events: none; opacity: 0.4; }
.message-reaction-picker { position: absolute; z-index: var(--z-popover); bottom: calc(100% + 8px); left: 50%; transform: translateX(-50%); display: flex; gap: 2px; padding: 4px; border: 1px solid var(--border-strong); border-radius: var(--radius-full); background: var(--surface-raised); box-shadow: var(--shadow-md); animation: pop-in var(--duration-fast) var(--ease-out); }
.reaction-option { width: 32px; height: 32px; display: grid; place-items: center; padding: 0; border: 0; border-radius: 50%; background: transparent; font-size: 18px; cursor: pointer; transition: transform var(--duration-fast) var(--ease-out), background-color var(--transition); }
.reaction-option:hover, .reaction-option:focus-visible { background: var(--surface-muted); transform: scale(1.12); }
.reaction-option.active { background: var(--primary-soft); box-shadow: inset 0 0 0 1.5px var(--primary); }
.forwarded-label { display: inline-flex; align-items: center; gap: 4px; margin-bottom: 3px; color: var(--text-subtle); font-size: var(--text-xs); font-style: italic; font-weight: 500; }
.forwarded-label .icon { width: 13px; height: 13px; }
.send-state { display: inline-flex; align-items: center; gap: 3px; color: var(--text-subtle); font-weight: 600; }
.send-state .icon { width: 13px; height: 13px; }
.send-state.failed { padding: 1px 7px 1px 5px; border-radius: var(--radius-full); background: var(--danger-soft); color: var(--danger); }
.send-state.pending { color: var(--warning); }
.msg-media { margin: 2px 0 6px; }
.msg-media:last-child { margin-bottom: 0; }
.media-open { max-width: 100%; display: block; padding: 0; border: 0; border-radius: var(--radius-md); background: transparent; cursor: zoom-in; overflow: hidden; }
.media-open:focus-visible { outline-offset: 2px; }
.msg-media img { display: block; width: auto; max-width: min(420px, 100%); max-height: 400px; border-radius: var(--radius-md); object-fit: contain; background: var(--surface-muted); transition: filter var(--duration-fast) ease; }
.media-open:hover img { filter: brightness(0.94); }
.msg-media.sticker img { max-width: min(180px, 50vw); max-height: 180px; background: transparent; }
.msg-media audio { display: block; width: min(340px, 60vw); max-width: 100%; height: 40px; }
.voice-note-player { display: flex; align-items: center; gap: 6px; max-width: 100%; }
.voice-note-player audio { min-width: 0; flex: 1 1 auto; }
.voice-speed-btn { flex: 0 0 auto; min-width: 40px; min-height: 28px; padding: 2px 8px; border: 1px solid var(--border-strong); border-radius: var(--radius-full); background: var(--surface); color: var(--text-strong); font-size: var(--text-xs); font-weight: 700; font-variant-numeric: tabular-nums; cursor: pointer; transition: background-color var(--transition), border-color var(--transition); }
.voice-speed-btn:hover, .voice-speed-btn:focus-visible { background: var(--primary-soft); border-color: var(--primary); }
.document-card, .location-card { width: min(400px, 100%); display: flex; align-items: center; gap: 10px; padding: 10px 10px 10px 12px; border: 1px solid var(--border); border-radius: var(--radius-md); background: var(--surface); }
.message-row.outbound .document-card, .message-row.outbound .location-card { background: color-mix(in srgb, var(--surface) 70%, transparent); }
.document-card > .icon, .location-card > .icon { width: 34px; height: 34px; padding: 7px; flex: 0 0 auto; border-radius: var(--radius-sm); background: var(--primary-soft); color: var(--primary-text); }
.document-card-copy, .location-card-copy { min-width: 0; flex: 1; }
.document-card-copy strong, .location-card-copy strong { display: block; color: var(--text-strong); font-size: var(--text-md); font-weight: 600; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.document-card-copy span, .location-card-copy span { display: block; margin-top: 1px; color: var(--text-muted); font-size: var(--text-xs); overflow-wrap: anywhere; }
.media-unavailable { padding: 10px 12px; border: 1px dashed var(--border-strong); border-radius: var(--radius-md); background: var(--surface-muted); color: var(--text-muted); font-size: var(--text-sm); display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.media-unavailable > span { flex: 1 1 200px; }
.event { width: fit-content; max-width: min(90%, 620px); margin: 14px auto; padding: 3px 11px; border-radius: var(--radius-full); background: var(--surface-muted); color: var(--text-muted); text-align: center; font-size: var(--text-xs); line-height: 1.5; overflow-wrap: anywhere; }

/* ---------- Context panel ---------- */
.context-panel { position: relative; min-width: 0; overflow: auto; border-left: 1px solid var(--border); background: var(--surface); }
.context-panel-head { position: sticky; top: 0; z-index: 2; min-height: 48px; display: flex; align-items: center; justify-content: space-between; gap: 10px; padding: 8px 10px 8px 16px; border-bottom: 1px solid var(--border); background: color-mix(in srgb, var(--surface) 92%, transparent); backdrop-filter: blur(10px); }
.context-panel-head strong { color: var(--text-strong); font-size: var(--text-md); font-weight: 650; }
.context-panel-head .icon-btn { width: 32px; height: 32px; }
.context-close { display: inline-grid; }
.context { padding: 16px; }
.context-hero { display: flex; align-items: center; gap: 11px; padding: 2px 0 16px; border-bottom: 1px solid var(--border); }
.context-hero .avatar.large { width: 44px; height: 44px; font-size: var(--text-md); background: var(--primary-soft); color: var(--primary-text); box-shadow: none; }
.context-hero-copy { min-width: 0; flex: 1; }
.context-hero-copy strong { display: block; color: var(--text-strong); font-size: var(--text-base); font-weight: 650; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.context-hero-copy span { display: block; margin-top: 1px; color: var(--text-muted); font-size: var(--text-sm); font-family: var(--font-mono); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.context-card { margin-top: 14px; border: 1px solid var(--border); border-radius: var(--radius-md); background: var(--surface); overflow: hidden; }
.context-card h3 { display: flex; align-items: center; gap: 7px; margin: 0; padding: 9px 12px; border-bottom: 1px solid var(--border); background: var(--bg-subtle); color: var(--text-strong); font-size: var(--text-sm); font-weight: 650; letter-spacing: 0; text-transform: none; }
.context-card h3 .icon { width: 15px; height: 15px; color: var(--text-subtle); }
.context-card.ai { border-color: var(--bubble-ai-border); }
.context-card.ai h3 { background: var(--ai-soft); color: var(--ai); border-bottom-color: var(--bubble-ai-border); }
.context-card.ai h3 .icon { color: var(--ai); }
.context-list { margin: 0; padding: 2px 12px; }
.context-item { display: grid; grid-template-columns: minmax(84px, 0.8fr) minmax(0, 1.2fr); gap: 10px; padding: 7px 0; border-bottom: 1px solid var(--border); }
.context-item:last-child { border-bottom: 0; }
.context-item dt { color: var(--text-muted); font-size: var(--text-sm); }
.context-item dd { min-width: 0; margin: 0; color: var(--text-strong); font-size: var(--text-sm); font-weight: 500; text-align: right; overflow-wrap: anywhere; white-space: pre-wrap; }
.context-item dd.code { font-family: var(--font-mono); font-size: var(--text-xs); }
.context-summary { padding: 11px 12px; color: var(--text); font-size: var(--text-md); line-height: 1.55; white-space: pre-wrap; overflow-wrap: anywhere; }
.context-empty { padding: 12px; color: var(--text-muted); font-size: var(--text-sm); }
.context-backdrop { display: none; }
.context-contact-action { margin-left: auto; flex: 0 0 auto; }

/* ---------- Composer ---------- */
.composer-shell { grid-row: 3; grid-column: 1; position: relative; z-index: 3; border-top: 1px solid var(--border); background: var(--surface); }
.read-only-banner, .recording-bar, .media-draft, .reply-draft { margin: 10px 16px 0; }
.read-only-banner { display: flex; align-items: flex-start; gap: 9px; padding: 10px 12px; border: 1px solid color-mix(in srgb, var(--warning) 30%, var(--border)); border-left: 3px solid var(--warning); border-radius: var(--radius-sm); background: var(--warning-soft); color: var(--warning); font-size: var(--text-sm); font-weight: 500; line-height: 1.5; }
.read-only-banner .icon { margin-top: 1px; }
.reply-draft { display: flex; align-items: center; gap: 10px; padding: 7px 8px 7px 12px; border-left: 3px solid var(--primary); border-radius: var(--radius-sm); background: var(--primary-soft); color: var(--primary-text); animation: pop-in var(--duration-fast) var(--ease-out); }
.reply-draft-copy { min-width: 0; flex: 1; }
.reply-draft-copy strong { display: block; color: var(--primary-text); font-size: var(--text-sm); font-weight: 650; }
.reply-draft-copy span { display: block; margin-top: 1px; color: var(--text-muted); font-size: var(--text-sm); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.reply-draft .icon-btn { width: 30px; height: 30px; }
.recording-bar { display: flex; align-items: center; gap: 9px; padding: 8px 8px 8px 12px; border: 1px solid color-mix(in srgb, var(--danger) 26%, var(--border)); border-radius: var(--radius-sm); background: var(--danger-soft); color: var(--danger); font-size: var(--text-sm); font-weight: 600; font-variant-numeric: tabular-nums; }
.recording-dot { width: 9px; height: 9px; flex: 0 0 auto; border-radius: 50%; background: var(--danger); animation: pulse 1s infinite; }
.recording-bar .btn { margin-left: auto; }
.media-draft { display: flex; align-items: center; gap: 12px; padding: 8px; border: 1px solid var(--border); border-radius: var(--radius-md); background: var(--bg-subtle); animation: pop-in var(--duration-fast) var(--ease-out); }
.media-draft-preview { width: 64px; height: 52px; display: grid; place-items: center; flex: 0 0 auto; overflow: hidden; border-radius: var(--radius-sm); background: var(--surface-muted); }
.media-draft-preview img { width: 100%; height: 100%; object-fit: cover; }
.media-draft-preview audio { width: min(300px, 52vw); }
.media-draft.audio .media-draft-preview { width: auto; min-width: min(300px, 52vw); height: auto; background: transparent; }
.media-draft.document .media-draft-preview { color: var(--primary-text); }
.media-draft.document .media-draft-preview .icon { width: 28px; height: 28px; }
.media-draft-info { min-width: 0; flex: 1; }
.media-draft-name { color: var(--text-strong); font-size: var(--text-md); font-weight: 600; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.media-draft-size { margin-top: 1px; color: var(--text-muted); font-size: var(--text-xs); }
.media-draft-actions { display: flex; gap: 6px; }
.composer { padding: 10px 16px 10px; }
.composer-box {
  border: 1px solid var(--border-strong);
  border-radius: var(--radius-lg);
  background: var(--surface-input);
  box-shadow: var(--shadow-xs);
  transition: border-color var(--transition), box-shadow var(--transition);
}
.composer-box:focus-within { border-color: var(--focus); box-shadow: 0 0 0 3px var(--focus-ring); }
.composer-box:has(.composer-textarea:disabled) { background: var(--bg-subtle); border-color: var(--border); box-shadow: none; }
.composer-input-wrap { min-width: 0; }
.composer-textarea {
  display: block;
  width: 100%;
  min-height: 44px;
  max-height: 154px;
  padding: 12px 14px 4px;
  border: 0;
  border-radius: var(--radius-lg) var(--radius-lg) 0 0;
  background: transparent;
  color: var(--text-strong);
  font-size: var(--text-base);
  line-height: 1.45;
  resize: none;
  overflow-y: auto;
}
.composer-textarea::placeholder { color: var(--text-subtle); }
.composer-textarea:focus-visible { outline: 0; box-shadow: none; }
.composer-textarea:disabled { cursor: not-allowed; color: var(--text-muted); }
.composer-toolbar { display: flex; align-items: center; gap: 4px; padding: 4px 6px 6px 6px; }
.composer-tools { display: flex; align-items: center; gap: 2px; }
.composer-tools .icon-btn { width: 34px; height: 34px; }
.composer-count { margin-left: auto; color: var(--text-subtle); font-size: var(--text-xs); font-variant-numeric: tabular-nums; }
.send-btn { min-width: 84px; min-height: 34px; margin-left: 6px; border-radius: var(--radius-md); }
.composer-meta { min-height: 18px; display: flex; flex-wrap: wrap; align-items: center; gap: 2px 10px; margin: 6px 4px 0; color: var(--text-subtle); font-size: var(--text-xs); }
.composer-meta > span:not(.reply-mode) { min-width: 0; }
.reply-mode { display: inline-flex; align-items: center; gap: 5px; color: var(--primary-text); font-weight: 600; }
.reply-mode-dot { width: 6px; height: 6px; border-radius: 50%; background: currentColor; }
.composer-shortcut { margin-left: auto; display: inline-flex; align-items: center; gap: 4px; }
.composer-shortcut kbd { height: 17px; min-width: 17px; font-size: 10px; }
.shortcut-link { padding: 0; border: 0; background: none; color: var(--primary-text); font-size: var(--text-xs); font-weight: 600; text-decoration: underline; text-underline-offset: 2px; cursor: pointer; }

/* ---------- Modals ---------- */
.modal-backdrop { position: fixed; inset: 0; z-index: var(--z-modal); display: grid; place-items: center; padding: 24px; background: var(--overlay); animation: fade-in var(--duration-base) ease; }
.modal-card { width: min(820px, 100%); max-height: min(90vh, 900px); max-height: min(90dvh, 900px); display: flex; flex-direction: column; overflow: hidden; border: 1px solid var(--border); border-radius: var(--radius-lg); background: var(--surface-raised); box-shadow: var(--shadow-lg); animation: modal-in var(--duration-base) var(--ease-out); }
.modal-card:focus-visible { outline: 0; box-shadow: var(--shadow-lg); }
.modal-card.transfer-card { width: min(520px, 100%); }
.modal-card.confirm-card { width: min(460px, 100%); }
.modal-card.message-preferences-card { width: min(620px, 100%); }
.message-preference-textarea { min-height: 112px !important; resize: vertical; line-height: 1.45; }
.message-preference-hint { display: flex; justify-content: space-between; gap: 10px; }
.message-preference-hint span:last-child { font-variant-numeric: tabular-nums; white-space: nowrap; }
.modal-card.ai-settings-card { width: min(640px, 100%); }
.modal-card.whatsapp-card { width: min(760px, 100%); }
.modal-card.location-card-modal { width: min(560px, 100%); }
.modal-card.shortcuts-card { width: min(520px, 100%); }
.modal-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; padding: 18px 16px 14px 22px; border-bottom: 1px solid var(--border); }
.modal-head h2 { margin: 0; color: var(--text-strong); font-size: var(--text-xl); font-weight: 650; line-height: 1.3; letter-spacing: -0.01em; }
.modal-subtitle { margin-top: 3px; color: var(--text-muted); font-size: var(--text-md); line-height: 1.5; }
.modal-close { width: 34px; height: 34px; margin-top: -4px; }
.modal-body { min-height: 0; overflow: auto; padding: 20px 22px 22px; }
.modal-actions { display: flex; justify-content: flex-end; flex-wrap: wrap; gap: 8px; margin-top: 20px; }
.location-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 0 12px; }
.location-grid .wide { grid-column: 1 / -1; }
#useCurrentLocationBtn { margin-right: auto; }
.confirm-body { display: flex; gap: 14px; align-items: flex-start; }
.confirm-icon { width: 40px; height: 40px; flex: 0 0 auto; display: grid; place-items: center; border-radius: 50%; background: var(--primary-soft); color: var(--primary-text); }
.confirm-card[data-tone="danger"] .confirm-icon { background: var(--danger-soft); color: var(--danger); }
.confirm-card[data-tone="success"] .confirm-icon { background: var(--success-soft); color: var(--success); }
.confirm-card[data-tone="warning"] .confirm-icon { background: var(--warning-soft); color: var(--warning); }
.confirm-copy { min-width: 0; flex: 1; }
.confirm-copy h2 { margin: 2px 0 0; color: var(--text-strong); font-size: var(--text-lg); font-weight: 650; line-height: 1.35; }
.confirm-message { margin: 6px 0 0; color: var(--text-muted); font-size: var(--text-md); line-height: 1.55; white-space: pre-line; overflow-wrap: anywhere; }
.confirm-copy .field { margin: 14px 0 0; }
.confirm-card .modal-actions { margin-top: 22px; }
.shortcut-list { display: grid; gap: 0; margin: 0; }
.shortcut-row { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 9px 0; border-bottom: 1px solid var(--border); }
.shortcut-row:last-child { border-bottom: 0; }
.shortcut-row dt { color: var(--text-strong); font-size: var(--text-md); }
.shortcut-row dd { display: flex; gap: 4px; margin: 0; }
.whatsapp-account-list, .whatsapp-number-list { display: grid; gap: 8px; margin-top: 12px; }
.whatsapp-account-row, .whatsapp-number-row { display: grid; gap: 10px; align-items: center; padding: 12px 14px; border: 1px solid var(--border); border-radius: var(--radius-md); background: var(--surface); }
.whatsapp-account-row, .whatsapp-number-row { grid-template-columns: minmax(0, 1fr) auto; }
.whatsapp-row-title { color: var(--text-strong); font-size: var(--text-md); font-weight: 650; }
.whatsapp-row-subtitle { margin-top: 2px; color: var(--text-muted); font-size: var(--text-xs); overflow-wrap: anywhere; }
.whatsapp-row-meta { display: flex; flex-wrap: wrap; gap: 4px; margin-top: 7px; }
.whatsapp-number-actions { display: flex; flex-wrap: wrap; justify-content: flex-end; gap: 6px; }
.whatsapp-name-editor { grid-column: 1 / -1; display: flex; align-items: flex-end; flex-wrap: wrap; gap: 8px; padding-top: 4px; }
.whatsapp-name-editor .field { flex: 1 1 220px; min-width: 0; margin: 0; }
.whatsapp-name-editor .btn { min-height: 38px; }
.whatsapp-sync-form { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 8px; align-items: start; margin-top: 14px; }
.whatsapp-sync-form .field { margin: 0; }
.whatsapp-sync-form > .btn { margin-top: 24px; min-height: 38px; }
.whatsapp-sync-form + .form-note { margin-top: 14px; }
.linked-device-section { padding: 16px; margin-bottom: 22px; border: 1px solid var(--border); border-radius: var(--radius-lg); background: var(--bg-subtle); }
.linked-device-section .form-note { margin-top: 12px; background: var(--surface); }
.linked-device-qr { display: block; width: 200px; max-width: 100%; height: auto; margin: 12px 0; padding: 10px; border: 1px solid var(--border); border-radius: var(--radius-md); background: #ffffff; image-rendering: pixelated; box-shadow: var(--shadow-sm); }
.linked-device-line { display: grid; grid-template-columns: minmax(0, 1fr) auto auto; align-items: center; gap: 2px 6px; padding: 12px 0; border-bottom: 1px solid var(--border); }
.linked-device-line > strong { grid-column: 1; grid-row: 1; min-width: 0; color: var(--text-strong); font-size: var(--text-md); }
.linked-device-line > .muted { grid-column: 1; grid-row: 2; min-width: 0; font-size: var(--text-sm); }
.linked-device-line > .btn { grid-row: 1 / span 2; }
.linked-device-line:not(:has(> .btn + .btn)) > .btn { grid-column: 2 / -1; justify-self: end; }
.linked-device-line > .linked-device-qr { grid-column: 1; grid-row: 3; }
.linked-device-line:last-child { border-bottom: 0; }
.section-spaced { margin-top: 24px; }
.whatsapp-card .section-title { padding-top: 2px; }
.modal-card.media-viewer-card { width: min(1200px, 96vw); height: min(92vh, 960px); }
.media-viewer-card .modal-head { align-items: center; flex-wrap: wrap; row-gap: 10px; padding-block: 12px; }
.media-viewer-card .modal-head > div:first-child { min-width: 0; flex: 1 1 200px; }
.media-viewer-card .modal-close { order: 2; margin-top: 0; }
.media-viewer-body { flex: 1 1 0; min-width: 0; min-height: 0; position: relative; display: flex; align-items: center; justify-content: center; overflow: hidden; padding: 14px; background: color-mix(in srgb, var(--bg-app) 70%, #000000 30%); }
:root[data-theme="dark"] .media-viewer-body { background: #05080a; }
.media-viewer-body.image-mode { touch-action: none; }
.media-viewer-body img { display: block; position: absolute; max-width: none; max-height: none; border-radius: var(--radius-sm); transform-origin: center center; user-select: none; -webkit-user-drag: none; }
.media-viewer-body.can-pan { cursor: grab; }
.media-viewer-body.panning { cursor: grabbing; }
.media-viewer-body.editing { padding: 10px; }
.media-viewer-body canvas { display: block; position: absolute; max-width: none; max-height: none; transform-origin: center center; touch-action: none; box-shadow: var(--shadow-md); }
.media-viewer-tools { display: flex; align-items: center; gap: 6px; margin-left: auto; flex-wrap: wrap; order: 1; }
@media (max-width: 640px) { .media-viewer-tools { order: 3; flex: 1 1 100%; margin-left: 0; } }
.image-editor-toolbar { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; padding: 10px 14px; border-top: 1px solid var(--border); background: var(--surface-raised); }
.image-editor-toolbar label { display: flex; align-items: center; gap: 5px; color: var(--text-muted); font-size: var(--text-sm); }
.image-editor-toolbar input[type="text"] { flex: 1 1 150px; min-width: 110px; min-height: 30px; padding: 4px 9px; border: 1px solid var(--border-strong); border-radius: var(--radius-sm); background: var(--surface-input); color: var(--text-strong); }
.image-editor-toolbar input[type="range"] { width: 90px; accent-color: var(--primary); }
.image-editor-toolbar input[type="color"] { width: 30px; height: 26px; padding: 0; border: 1px solid var(--border-strong); border-radius: var(--radius-sm); background: none; cursor: pointer; }
.image-editor-toolbar button[aria-pressed="true"] { background: var(--primary-soft); border-color: var(--primary); color: var(--primary-text); }
.media-viewer-zoom { min-width: 48px; text-align: center; color: var(--text-muted); font-size: var(--text-xs); font-variant-numeric: tabular-nums; }
.media-viewer-body iframe { width: 100%; height: 100%; min-height: 62vh; border: 0; border-radius: var(--radius-sm); background: white; }
.ai-status-card { display: flex; align-items: center; justify-content: space-between; gap: 16px; margin-bottom: 20px; padding: 14px 16px; border: 1px solid var(--border); border-radius: var(--radius-md); background: var(--bg-subtle); }
.ai-status-card:has(.ai-status-pill:not(.off)) { border-color: var(--bubble-ai-border); background: color-mix(in srgb, var(--ai-soft) 60%, var(--surface)); }
.ai-status-copy strong { display: block; color: var(--text-strong); font-size: var(--text-md); font-weight: 650; }
.ai-status-copy span { display: block; margin-top: 2px; color: var(--text-muted); font-size: var(--text-sm); }
.ai-mode-options { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin: 0 0 18px; padding: 0; border: 0; }
.ai-mode-options legend { grid-column: 1 / -1; margin-bottom: 6px; padding: 0; color: var(--text-strong); font-size: var(--text-sm); font-weight: 600; }
.ai-mode-option { display: flex; align-items: flex-start; gap: 10px; padding: 12px 14px; border: 1px solid var(--border-strong); border-radius: var(--radius-md); background: var(--surface); cursor: pointer; transition: border-color var(--transition), background-color var(--transition), box-shadow var(--transition); }
.ai-mode-option:hover { border-color: color-mix(in srgb, var(--primary) 40%, var(--border-strong)); }
.ai-mode-option:has(input:checked) { border-color: var(--primary); background: var(--primary-soft); box-shadow: inset 0 0 0 1px var(--primary); }
.ai-mode-option:has(input:focus-visible) { box-shadow: 0 0 0 3px var(--focus-ring); }
.ai-mode-option input { margin-top: 2px; accent-color: var(--primary); }
.ai-mode-option strong { display: block; color: var(--text-strong); font-size: var(--text-md); font-weight: 650; }
.ai-mode-option span { display: block; margin-top: 2px; color: var(--text-muted); font-size: var(--text-sm); line-height: 1.45; }
.ai-schedule-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 0 14px; }
.ai-schedule-grid .wide { grid-column: 1 / -1; }
.ai-next-change { margin-top: -4px; margin-bottom: 16px; color: var(--text-muted); font-size: var(--text-sm); }
.staff-view-heading, .section-title { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.staff-view-heading { margin-bottom: 18px; padding-bottom: 14px; border-bottom: 1px solid var(--border); }
.staff-view-heading strong, .section-title strong { color: var(--text-strong); font-size: var(--text-base); font-weight: 650; }
.staff-view-heading .muted, .section-title .muted { margin-top: 2px; font-size: var(--text-sm); }
.staff-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 0 16px; }
.staff-grid .wide { grid-column: 1 / -1; }
.check-field { min-height: 38px; display: flex; align-items: center; gap: 9px; padding: 0 12px; border: 1px solid var(--border-strong); border-radius: var(--radius-sm); background: var(--surface); color: var(--text-strong); font-size: var(--text-md); font-weight: 500; cursor: pointer; transition: border-color var(--transition), background-color var(--transition); }
.field .check-field { font-size: var(--text-md); font-weight: 500; }
.check-field:hover:not(.static) { border-color: color-mix(in srgb, var(--text-subtle) 60%, var(--border-strong)); }
.check-field:has(input:focus-visible) { border-color: var(--focus); box-shadow: 0 0 0 3px var(--focus-ring); }
.check-field:has(input:disabled) { background: var(--surface-muted); color: var(--text-muted); cursor: not-allowed; }
.check-field input { width: 17px; height: 17px; margin: 0; accent-color: var(--primary); }
.check-field.static { background: var(--bg-subtle); color: var(--text-muted); cursor: default; }
.check-field.static .icon { color: var(--success); }
.staff-actions-header { display: flex; gap: 8px; }
.staff-list { margin-top: 14px; border: 1px solid var(--border); border-radius: var(--radius-md); overflow: hidden; }
.staff-list-head, .staff-list-row { display: grid; grid-template-columns: minmax(0, 1.25fr) minmax(0, 1.5fr) 104px 100px 184px; gap: 16px; align-items: center; }
.staff-list-head > :last-child { padding-left: 2px; }
.staff-list-row > * { min-width: 0; }
.staff-list-row .role-badge, .staff-list-row .status-dot { justify-self: start; }
.staff-list-head { position: sticky; top: 0; z-index: 1; padding: 8px 14px; border-bottom: 1px solid var(--border); background: var(--bg-subtle); color: var(--text-muted); font-size: var(--text-xs); font-weight: 600; letter-spacing: 0.02em; }
.staff-list-row { padding: 11px 14px; border-bottom: 1px solid var(--border); transition: background-color var(--transition); }
.staff-list-row:hover { background: var(--surface-hover); }
.staff-list-row:last-child { border-bottom: 0; }
.staff-name { display: flex; align-items: center; gap: 6px; min-width: 0; color: var(--text-strong); font-size: var(--text-md); font-weight: 600; }
.staff-name-text { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.staff-you { flex: 0 0 auto; padding: 1px 6px; border-radius: var(--radius-full); background: var(--primary-soft); color: var(--primary-text); font-size: var(--text-xs); font-weight: 600; }
.staff-created { margin-top: 1px; color: var(--text-subtle); font-size: var(--text-xs); }
.staff-email { overflow: hidden; color: var(--text-muted); font-size: var(--text-sm); text-overflow: ellipsis; white-space: nowrap; }
.staff-list-row .role-badge { background: var(--primary-soft); color: var(--primary-text); }
.staff-row-actions { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; }
.staff-row-actions .btn { width: 100%; min-height: 32px; padding: 4px 10px; justify-content: center; font-size: var(--text-sm); }
.transfer-summary { display: flex; gap: 9px; margin: 0 0 18px; padding: 11px 12px; border: 1px solid color-mix(in srgb, var(--info) 24%, var(--border)); border-radius: var(--radius-sm); background: var(--info-soft); color: var(--info); font-size: var(--text-sm); line-height: 1.5; }

/* ---------- Toasts ---------- */
.toast-region { position: fixed; z-index: var(--z-toast); right: 20px; bottom: 20px; width: min(380px, calc(100vw - 32px)); display: grid; gap: 8px; pointer-events: none; }
.toast { position: relative; display: flex; align-items: flex-start; gap: 10px; padding: 11px 10px 11px 14px; border: 1px solid var(--border); border-radius: var(--radius-md); background: var(--surface-raised); color: var(--text-strong); box-shadow: var(--shadow-md); animation: toast-in var(--duration-base) var(--ease-out); pointer-events: auto; overflow: hidden; }
.toast::before { content: ""; position: absolute; left: 0; top: 0; bottom: 0; width: 3px; background: var(--primary); }
.toast > .icon { color: var(--primary-text); margin-top: 1px; }
.toast.success::before { background: var(--success); }
.toast.success > .icon { color: var(--success); }
.toast.error::before { background: var(--danger); }
.toast.error > .icon { color: var(--danger); }
.toast-copy { flex: 1; font-size: var(--text-md); line-height: 1.45; }
.toast .icon-btn { width: 26px; height: 26px; margin: -4px -2px -4px 0; }

@keyframes spin { to { transform: rotate(360deg); } }
@keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.35; } }
@keyframes shimmer { 100% { transform: translateX(100%); } }
@keyframes indeterminate { 0% { transform: translateX(-120%); } 55% { transform: translateX(190%); } 100% { transform: translateX(190%); } }
@keyframes toast-in { from { opacity: 0; transform: translateY(8px) scale(0.98); } to { opacity: 1; transform: none; } }
@keyframes fade-in { from { opacity: 0; } to { opacity: 1; } }
@keyframes modal-in { from { opacity: 0; transform: translateY(8px) scale(0.985); } to { opacity: 1; transform: none; } }
@keyframes pop-in { from { opacity: 0; transform: translateY(-3px); } to { opacity: 1; transform: none; } }

/* ---------- Responsive ---------- */
@media (min-width: 1101px) {
  .workspace-content.context-collapsed { grid-template-columns: minmax(0, 1fr) 0; }
  .workspace-content.context-collapsed .context-panel { width: 0; min-width: 0; visibility: hidden; pointer-events: none; overflow: hidden; border-left: 0; }
  .workspace-content.context-collapsed .context-resizer { display: none; }
}

@media (max-width: 1280px) {
  :root { --sidebar-width: 68px; --queue-width: 336px; }
  .brand-bar { position: relative; justify-content: center; flex-direction: column; gap: 6px; padding: 10px 6px; }
  .brand-copy, .sidebar-label, .nav-group-label, .agent-copy, .staff-actions .btn span, .message-preferences-slot .btn span, .whatsapp-manage-slot .btn span, .queue-nav-label { display: none; }
  .brand-bar .icon-btn { width: 30px; height: 30px; }
  .queue-nav { padding: 10px 8px; gap: 4px; }
  .nav-group-label + .queue-nav-btn:not(:first-of-type) { margin-top: 8px; }
  .queue-nav-btn { grid-template-columns: 1fr; justify-items: center; min-height: 40px; padding: 8px 4px; }
  .queue-nav-btn .icon { width: 19px; height: 19px; }
  .queue-nav-btn.active::before { left: -8px; }
  .queue-nav-count { position: absolute; top: 2px; right: 2px; min-width: 17px; height: 17px; padding: 0 4px; font-size: 10px; }
  .account-zone { padding: 8px 6px 10px; justify-items: center; }
  .message-preferences-slot .btn, .whatsapp-manage-slot .btn { width: 40px; height: 36px; padding: 0; justify-content: center; }
  .agent-card { padding: 6px 0 0; border: 0; background: transparent; box-shadow: none; }
  .agent-main { justify-content: center; }
  .staff-actions { grid-template-columns: 1fr; padding-top: 6px; }
  .staff-actions .btn { width: 40px; height: 34px; min-width: 0; padding: 0; justify-content: center; }
  .staff-actions .btn .icon { margin: 0; }
}

@media (max-width: 1100px) {
  :root { --queue-width: 320px; }
  .workspace-content { grid-template-columns: minmax(0, 1fr); }
  .context-panel { position: fixed; z-index: var(--z-drawer); top: 0; right: 0; bottom: 0; width: min(360px, 92vw); transform: translateX(104%); border-left: 1px solid var(--border-strong); box-shadow: var(--shadow-lg); transition: transform var(--duration-base) var(--ease-out); }
  .context-panel.open { transform: translateX(0); }
  .context-close { display: inline-grid; }
  .context-backdrop { position: fixed; z-index: calc(var(--z-drawer) - 1); inset: 0; display: block; background: var(--overlay); opacity: 0; visibility: hidden; transition: opacity var(--duration-base) ease, visibility var(--duration-base) ease; }
  .context-backdrop.visible { opacity: 1; visibility: visible; }
  .context-toggle { display: inline-grid; }
  .context-resizer { display: none; }
}

@media (max-width: 960px) {
  .message-stack { width: min(84%, 640px); }
}

@media (max-width: 800px) {
  body { overflow: hidden; }
  .app-shell { display: flex; flex-direction: column; }
  .global-sidebar {
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto;
    grid-template-areas: "brand agent" "nav nav";
    overflow: visible;
    border-right: 0;
    border-bottom: 1px solid var(--border);
    background: var(--surface);
  }
  .brand-bar { grid-area: brand; min-height: 54px; flex-direction: row; justify-content: flex-start; gap: 10px; padding: 8px 12px; border-bottom: 0; }
  .brand-bar .brand-logo { width: 32px; height: 32px; position: static; }
  .brand-bar .icon-btn { width: 36px; height: 36px; position: static; margin-left: auto; }
  .brand-copy { display: block; }
  .brand-copy span { display: none; }
  .queue-nav { grid-area: nav; flex-direction: row; gap: 4px; overflow-x: auto; padding: 0 10px 8px; scroll-snap-type: x proximity; scrollbar-width: none; }
  .queue-nav::-webkit-scrollbar { display: none; }
  .queue-nav-btn { width: auto; min-width: max-content; min-height: 34px; display: inline-flex; justify-content: center; gap: 6px; padding: 6px 11px; border: 1px solid var(--border); border-radius: var(--radius-full); scroll-snap-align: start; }
  .queue-nav-btn.active { border-color: var(--primary); background: var(--primary-soft); color: var(--primary-text); box-shadow: none; }
  .queue-nav-btn.active::before { display: none; }
  .queue-nav-btn .icon { width: 16px; height: 16px; }
  .queue-nav-label { display: inline; }
  .queue-nav-count { position: static; min-width: 18px; height: 18px; font-size: 10px; }
  .nav-spacer, .sidebar-label, .nav-group-label { display: none; }
  .account-zone { grid-area: agent; align-self: center; display: flex; align-items: center; gap: 2px; margin: 0 8px 0 0; padding: 0; border: 0; }
  .message-preferences-slot .btn, .whatsapp-manage-slot .btn { width: 36px; height: 36px; min-height: 36px; padding: 0; justify-content: center; border-color: transparent; background: transparent; box-shadow: none; }
  .agent-card { align-self: center; display: flex; align-items: center; margin: 0; padding: 0; border: 0; background: transparent; box-shadow: none; }
  .agent-main { display: none; }
  .staff-actions { display: flex; margin: 0; padding: 0; border: 0; }
  .staff-actions .btn { width: 36px; height: 36px; min-height: 36px; flex: 0 0 auto; padding: 0; border-color: transparent; background: transparent; box-shadow: none; }
  .inbox-panel { min-height: 0; flex: 1; border-right: 0; }
  .queue-resizer { display: none; }
  .inbox-header { min-height: 56px; padding: 8px 10px 8px 14px; }
  .eyebrow { display: none; }
  .main { display: none; min-height: 0; flex: 1; }
  body.mobile-ticket-open .global-sidebar, body.mobile-ticket-open .inbox-panel { display: none; }
  body.mobile-ticket-open .main { display: grid; }
  .workspace-header { min-height: 56px; flex-wrap: wrap; gap: 6px 8px; padding: 8px 8px 8px 6px; }
  .mobile-back { display: inline-grid; }
  .ticket-heading-avatar { display: none; }
  .ticket-heading { flex: 1 1 0; }
  .ticket-heading h2 { font-size: var(--text-base); }
  .ticket-subtitle { font-size: var(--text-xs); }
  .ticket-history-shortcut-full { display: none; }
  .ticket-history-shortcut-compact { display: inline; }
  .ticket-history-shortcut { margin-top: 4px; }
  .ticket-header-meta { display: none; }
  .actions { order: 3; flex: 1 1 100%; justify-content: flex-start; gap: 6px; padding: 2px 0 0 42px; }
  .actions:empty { display: none; }
  .actions > .btn, .menu-summary { min-height: 34px; }
  .actions > .btn .action-label, .menu-summary .action-label { display: none; }
  .actions > .btn.primary .action-label, .actions > .btn.success-action .action-label { display: inline; }
  .header-tools { border-left: 0; padding-left: 0; margin-left: 0; }
  .action-menu-panel { right: auto; left: 0; }
  .conversation { padding: 14px 10px 20px; }
  .message-stack { width: min(88%, 620px); }
  .message-row { grid-template-columns: 26px minmax(0, 1fr); gap: 6px; }
  .message-row.outbound { grid-template-columns: minmax(0, 1fr) 26px; }
  .message-avatar { width: 26px; height: 26px; font-size: 9px; }
  .composer { padding: 8px 10px calc(8px + env(safe-area-inset-bottom)); }
  .composer-shortcut { display: none; }
  .composer-meta { margin-top: 4px; }
  .send-btn { min-width: 40px; width: 40px; padding: 0; }
  .send-btn .send-label { display: none; }
  .read-only-banner, .recording-bar, .media-draft, .reply-draft { margin-inline: 10px; }
  .media-draft { align-items: flex-start; flex-wrap: wrap; }
  .media-draft-info { min-width: 120px; }
  .modal-backdrop { align-items: end; padding: 0; }
  .modal-card { width: 100%; max-height: 92dvh; border-radius: var(--radius-xl) var(--radius-xl) 0 0; border-bottom: 0; animation: sheet-in var(--duration-base) var(--ease-out); }
  .modal-card.transfer-card, .modal-card.confirm-card, .modal-card.shortcuts-card { width: 100%; }
  .modal-head { padding: 16px 12px 12px 18px; }
  .modal-body { padding: 16px 18px calc(18px + env(safe-area-inset-bottom)); }
  .staff-grid { grid-template-columns: 1fr; }
  .staff-grid .wide { grid-column: auto; }
  .staff-list-head { display: none; }
  .staff-list-row { grid-template-columns: minmax(0, 1fr) auto; gap: 8px 12px; }
  .staff-list-row .staff-email { grid-column: 1 / -1; }
  .staff-list-row .role-badge { grid-column: 1; }
  .staff-list-row .status-dot { grid-column: 2; grid-row: 3; justify-self: end; }
  .staff-row-actions { grid-column: 1 / -1; padding-top: 4px; }
  .toast-region { right: 12px; left: 12px; bottom: 12px; width: auto; }
}
@keyframes sheet-in { from { transform: translateY(24px); opacity: 0.6; } to { transform: none; opacity: 1; } }

@media (max-width: 520px) {
  .login-page { padding: 16px; }
  .login-content { padding: 26px 20px; }
  .inbox-title-row h1 { font-size: var(--text-lg); }
  .ticket-row { padding-inline: 12px; }
  .ticket-summary, .ticket-row-meta { margin-left: 42px; }
  .message-stack { width: min(90%, 620px); }
  .message-bubble { padding: 8px 10px; }
  .composer-tools .icon-btn { width: 36px; height: 36px; }
  .modal-actions { flex-direction: column-reverse; }
  .modal-actions .btn { width: 100%; }
  #useCurrentLocationBtn { margin-right: 0; }
  .location-grid, .ai-schedule-grid, .ai-mode-options { grid-template-columns: 1fr; }
  .staff-view-heading, .section-title { align-items: flex-start; flex-direction: column; }
  .staff-actions-header { width: 100%; }
  .staff-actions-header .btn { flex: 1; }
  .whatsapp-sync-form { grid-template-columns: 1fr; }
  .whatsapp-sync-form > .btn { margin-top: 0; }
  .whatsapp-number-actions { grid-column: 1 / -1; justify-content: flex-start; }
  .linked-device-line { grid-template-columns: repeat(2, minmax(0, 1fr)); row-gap: 6px; }
  .linked-device-line > strong, .linked-device-line > .muted, .linked-device-line > .linked-device-qr { grid-column: 1 / -1; }
  .linked-device-line > .btn { grid-row: auto; }
  .linked-device-line:not(:has(> .btn + .btn)) > .btn { grid-column: 1 / -1; justify-self: stretch; }
  .whatsapp-name-editor .btn { flex: 1 1 auto; }
}

@media (pointer: coarse) {
  .icon-btn { min-width: 40px; min-height: 40px; }
  .btn.small { min-height: 36px; }
  .queue-nav-btn { min-height: 40px; }
}

@media (forced-colors: active) {
  .meta-chip, .status-badge, .role-badge, .channel-pill, .contact-source, .status-dot, .ai-status-pill, .queue-nav-count, .message-bubble, .composer-box, .message-actions, .toast, .ticket-time.age-alert { border: 1px solid CanvasText; }
  .ticket-row.active, .queue-nav-btn.active { outline: 2px solid Highlight; outline-offset: -2px; }
  .message-action-btn::before { background: ButtonText; }
}

@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after { scroll-behavior: auto !important; animation-duration: 0.01ms !important; animation-iteration-count: 1 !important; transition-duration: 0.01ms !important; transition-delay: 0ms !important; }
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
  <symbol id="icon-file" viewBox="0 0 24 24"><path d="M6 3h8l4 4v14H6z"/><path d="M14 3v5h5M8.5 13h7M8.5 16h7"/></symbol>
  <symbol id="icon-paperclip" viewBox="0 0 24 24"><path d="m8.5 12.5 6.2-6.2a3 3 0 1 1 4.2 4.2l-8.1 8.1a5 5 0 0 1-7.1-7.1l8-8"/></symbol>
  <symbol id="icon-reply" viewBox="0 0 24 24"><path d="m10 7-6 5 6 5v-3h3.5c3.3 0 5.6 1.2 7 3.5-.3-5.3-2.8-8-7.5-8H10z"/></symbol>
  <symbol id="icon-map-pin" viewBox="0 0 24 24"><path d="M12 21s6-5.3 6-11a6 6 0 1 0-12 0c0 5.7 6 11 6 11Z"/><circle cx="12" cy="10" r="2"/></symbol>
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

<main class="login-page" id="sessionLoading">
  <div class="session-loading-content">
    <span class="brand-logo" aria-hidden="true"><svg class="icon icon-lg"><use href="#icon-logo"></use></svg></span>
    <strong>Shipment Support</strong>
    <span class="session-spinner" aria-hidden="true"></span>
    <p id="sessionLoadingStatus" role="status" aria-live="polite">Checking your session…</p>
    <button class="btn primary hidden" id="sessionRetryBtn" type="button">Try again</button>
  </div>
</main>

<main class="login-page hidden" id="loginPage">
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
          <div class="password-field">
            <input id="password" type="password" autocomplete="current-password" required />
            <button class="icon-btn password-toggle" id="passwordToggleBtn" type="button" aria-label="Show password" aria-pressed="false" aria-controls="password" data-tooltip="Show password">
              <svg class="icon icon-sm" aria-hidden="true"><use href="#icon-eye"></use></svg>
            </button>
          </div>
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
    <nav class="queue-nav" id="tabs" aria-label="Ticket queues">
      <div class="nav-group-label" aria-hidden="true">Work</div>
      <button class="queue-nav-btn active" data-view="unassigned" type="button" aria-label="Unassigned tickets" aria-current="page" title="Unassigned">
        <svg class="icon" aria-hidden="true"><use href="#icon-inbox"></use></svg><span class="queue-nav-label">Unassigned</span><span class="queue-nav-count" data-count-for="unassigned"></span>
      </button>
      <button class="queue-nav-btn" data-view="mine" type="button" aria-label="My tickets" title="My tickets">
        <svg class="icon" aria-hidden="true"><use href="#icon-user-check"></use></svg><span class="queue-nav-label">My tickets</span><span class="queue-nav-count" data-count-for="mine"></span>
      </button>
      <button class="queue-nav-btn" data-view="chats" type="button" aria-label="Staff initiated chats" title="Chats">
        <svg class="icon" aria-hidden="true"><use href="#icon-message"></use></svg><span class="queue-nav-label">Chats</span><span class="queue-nav-count" data-count-for="chats"></span>
      </button>
      <button class="queue-nav-btn" data-view="watching" type="button" aria-label="Watching" title="Watching">
        <svg class="icon" aria-hidden="true"><use href="#icon-eye"></use></svg><span class="queue-nav-label">Watching</span><span class="queue-nav-count" data-count-for="watching"></span>
      </button>
      <div class="nav-group-label" aria-hidden="true">Browse</div>
      <button class="queue-nav-btn" data-view="active" type="button" aria-label="Active tickets" title="Active">
        <svg class="icon" aria-hidden="true"><use href="#icon-activity"></use></svg><span class="queue-nav-label">Active</span><span class="queue-nav-count" data-count-for="active"></span>
      </button>
      <button class="queue-nav-btn" data-view="waiting" type="button" aria-label="Waiting tickets" title="Waiting">
        <svg class="icon" aria-hidden="true"><use href="#icon-clock"></use></svg><span class="queue-nav-label">Waiting</span><span class="queue-nav-count" data-count-for="waiting"></span>
      </button>
      <button class="queue-nav-btn" data-view="history" type="button" aria-label="Ticket history" title="History">
        <svg class="icon" aria-hidden="true"><use href="#icon-history"></use></svg><span class="queue-nav-label">History</span><span class="queue-nav-count" data-count-for="history"></span>
      </button>
      <button class="queue-nav-btn" data-view="contacts" type="button" aria-label="Contacts" title="Contacts">
        <svg class="icon" aria-hidden="true"><use href="#icon-users"></use></svg><span class="queue-nav-label">Contacts</span><span class="queue-nav-count" data-count-for="contacts"></span>
      </button>
    </nav>
    <div class="nav-spacer"></div>
    <div class="account-zone">
      <div class="message-preferences-slot">
        <button class="btn ghost" id="messagePreferencesBtn" type="button" aria-label="My automatic messages" title="My automatic messages">
          <svg class="icon" aria-hidden="true"><use href="#icon-message"></use></svg><span>Messages</span>
        </button>
      </div>
      <div class="whatsapp-manage-slot">
        <button class="btn ghost hidden" id="manageWhatsAppBtn" type="button" aria-label="WhatsApp numbers" title="WhatsApp numbers">
          <svg class="icon" aria-hidden="true"><use href="#icon-phone"></use></svg><span>WhatsApp</span>
        </button>
      </div>
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
    </div>
  </aside>

  <section class="inbox-panel" aria-labelledby="queueTitle">
    <header class="inbox-header">
      <div>
        <div class="eyebrow">Support queue</div>
        <div class="inbox-title-row"><h1 id="queueTitle">Unassigned</h1><span class="queue-total" id="queueTotal">—</span></div>
      </div>
      <div class="inbox-header-actions">
        <button class="btn small primary" id="startChatBtn" type="button" aria-label="Start WhatsApp chat" title="Start WhatsApp chat">
          <svg class="icon icon-sm" aria-hidden="true"><use href="#icon-plus"></use></svg><span class="optional-label">New chat</span>
        </button>
        <button class="ai-header-control" id="aiHeaderBtn" type="button" aria-label="AI assistant status" title="AI assistant status">
          <i class="ai-status-dot" id="aiHeaderDot" aria-hidden="true"></i><span class="ai-header-copy">AI</span><span class="ai-header-state" id="aiHeaderStatus">—</span>
        </button>
        <button class="icon-btn" id="refreshQueueBtn" type="button" aria-label="Refresh queue" data-tooltip="Refresh queue">
          <svg class="icon" aria-hidden="true"><use href="#icon-refresh"></use></svg>
        </button>
      </div>
    </header>
    <div class="search-wrap">
      <label class="sr-only" for="queueSearch">Search conversations and messages</label>
      <div class="search-field">
        <svg class="icon" aria-hidden="true"><use href="#icon-search"></use></svg>
        <input id="queueSearch" type="search" autocomplete="off" placeholder="Search conversations and messages" aria-describedby="searchScope" aria-keyshortcuts="Control+K /" />
        <span class="search-kbd" aria-hidden="true"><kbd>/</kbd></span>
        <button class="icon-btn search-clear hidden" id="clearQueueSearchBtn" type="button" aria-label="Clear ticket search">
          <svg class="icon icon-sm" aria-hidden="true"><use href="#icon-x"></use></svg>
        </button>
      </div>
      <div class="search-scope" id="searchScope">Searches message history you are allowed to access. Customer/ticket filtering remains available for short searches.</div>
    </div>
    <div class="contacts-toolbar hidden" id="contactsToolbar">
      <div class="contacts-google-copy"><strong id="googleContactsTitle">Google Contacts</strong><span id="googleContactsStatus">Not connected</span></div>
      <div class="contacts-toolbar-actions">
        <button class="btn small" id="googleContactsConnectBtn" type="button">Sign in with Google</button>
        <input class="hidden" id="vcfContactsInput" type="file" accept=".vcf,text/vcard,text/x-vcard,text/directory" />
        <button class="btn small" id="importVcfContactsBtn" type="button"><svg class="icon icon-sm"><use href="#icon-plus"></use></svg>Import VCF</button>
        <button class="btn small hidden" id="googleContactsSyncBtn" type="button"><svg class="icon icon-sm"><use href="#icon-refresh"></use></svg>Sync</button>
        <button class="btn small danger hidden" id="googleContactsDisconnectBtn" type="button">Disconnect</button>
      </div>
    </div>
    <div class="queue" id="queue" role="listbox" aria-label="Support tickets" aria-busy="false">
      <div class="empty-state"><div class="empty-state-inner"><div class="empty-icon"><svg class="icon icon-lg" aria-hidden="true"><use href="#icon-inbox"></use></svg></div><h3>No tickets loaded</h3><p>Your selected queue will appear here after sign-in.</p></div></div>
    </div>
    <footer class="queue-footer" id="queueFooter">
      <span class="page-summary" id="pageSummary">Page 1</span>
      <div class="pagination-actions">
        <button class="icon-btn" id="previousPageBtn" type="button" aria-label="Previous ticket page" data-tooltip="Previous page" data-tooltip-placement="top" disabled><svg class="icon" aria-hidden="true"><use href="#icon-chevron-left"></use></svg></button>
        <button class="icon-btn" id="nextPageBtn" type="button" aria-label="Next ticket page" data-tooltip="Next page" data-tooltip-placement="top" disabled><svg class="icon" aria-hidden="true"><use href="#icon-chevron-right"></use></svg></button>
      </div>
    </footer>
    <div class="layout-resizer queue-resizer" id="queueResizer" role="separator" aria-orientation="vertical" aria-label="Resize ticket list" aria-valuemin="260" aria-valuemax="520" tabindex="0"></div>
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
        <button class="ticket-history-shortcut hidden" id="viewSharedHistoryBtn" type="button" aria-controls="conversation" aria-label="View shared customer history"><svg class="icon icon-sm" aria-hidden="true"><use href="#icon-history"></use></svg><span class="ticket-history-shortcut-full">View shared history</span><span class="ticket-history-shortcut-compact">History</span><span id="sharedHistoryCount"></span></button>
        <div class="ticket-header-meta" id="ticketHeaderMeta"></div>
      </div>
      <div class="actions" id="actions"></div>
      <div class="header-tools">
        <button class="icon-btn" id="conversationSearchBtn" type="button" aria-label="Search this conversation" data-tooltip="Search conversation">
          <svg class="icon" aria-hidden="true"><use href="#icon-search"></use></svg>
        </button>
        <button class="icon-btn context-toggle" id="contextToggleBtn" type="button" aria-label="Open customer details" aria-controls="contextPanel" aria-expanded="false" data-tooltip="Customer details" data-tooltip-placement="start">
          <svg class="icon" aria-hidden="true"><use href="#icon-panel-right"></use></svg>
        </button>
      </div>
    </header>
    <div class="workspace-loading hidden" id="workspaceLoading" role="progressbar" aria-label="Loading ticket"></div>
    <div class="error-banner" id="error" role="alert" aria-live="assertive">
      <svg class="icon" aria-hidden="true"><use href="#icon-alert"></use></svg>
      <div class="error-banner-copy" id="errorText"></div>
      <button class="icon-btn" id="dismissErrorBtn" type="button" aria-label="Dismiss error"><svg class="icon icon-sm" aria-hidden="true"><use href="#icon-x"></use></svg></button>
    </div>
    <div class="workspace-content" id="workspaceContent">
      <section class="conversation-shell" aria-label="Ticket conversation">
        <div class="conversation-search-toolbar hidden" id="conversationSearchToolbar">
          <svg class="icon icon-sm" aria-hidden="true"><use href="#icon-search"></use></svg>
          <input class="conversation-search-input" id="conversationSearchInput" type="search" autocomplete="off" placeholder="Search this conversation" aria-label="Search this conversation" />
          <span class="conversation-search-count" id="conversationSearchCount">—</span>
          <button class="icon-btn" id="conversationSearchPrev" type="button" aria-label="Previous match" data-tooltip="Previous match"><svg class="icon icon-sm"><use href="#icon-chevron-left"></use></svg></button>
          <button class="icon-btn" id="conversationSearchNext" type="button" aria-label="Next match" data-tooltip="Next match"><svg class="icon icon-sm"><use href="#icon-chevron-right"></use></svg></button>
          <button class="icon-btn" id="conversationSearchClose" type="button" aria-label="Close conversation search"><svg class="icon icon-sm"><use href="#icon-x"></use></svg></button>
        </div>
        <div class="conversation" id="conversation" role="log" aria-label="Conversation timeline" tabindex="0">
          <div class="conversation-empty"><div class="empty-state-inner"><div class="empty-icon"><svg class="icon icon-lg" aria-hidden="true"><use href="#icon-message"></use></svg></div><h3>Open a customer conversation</h3><p>Ticket conversation will appear here.</p></div></div>
        </div>
        <button class="btn small scroll-latest hidden" id="scrollToLatestBtn" type="button"><svg class="icon icon-sm" aria-hidden="true"><use href="#icon-arrow-down"></use></svg><span id="scrollToLatestLabel">Latest</span></button>
      </section>
      <aside class="context-panel" id="contextPanel" aria-label="Customer and ticket details">
        <div class="layout-resizer context-resizer" id="contextResizer" role="separator" aria-orientation="vertical" aria-label="Resize customer details" aria-valuemin="260" aria-valuemax="520" tabindex="0"></div>
        <div class="context-panel-head"><strong>Customer details</strong><button class="icon-btn context-close" id="closeContextBtn" type="button" aria-label="Close customer details"><svg class="icon" aria-hidden="true"><use href="#icon-x"></use></svg></button></div>
        <div class="context" id="context"><div class="empty-state"><div class="empty-state-inner"><div class="empty-icon"><svg class="icon icon-lg" aria-hidden="true"><use href="#icon-user"></use></svg></div><h3>No ticket selected</h3><p>Customer and shipment context will appear here.</p></div></div></div>
      </aside>
      <div class="context-backdrop" id="contextBackdrop" aria-hidden="true"></div>
    </div>
    <div class="composer-shell">
      <div class="read-only-banner hidden" id="readOnlyBanner" role="status"></div>
      <div class="reply-draft hidden" id="replyDraft" role="status">
        <svg class="icon icon-sm" aria-hidden="true"><use href="#icon-reply"></use></svg>
        <div class="reply-draft-copy"><strong id="replyDraftTitle">Replying to message</strong><span id="replyDraftBody"></span></div>
        <button class="icon-btn" id="cancelReplyBtn" type="button" aria-label="Cancel reply"><svg class="icon icon-sm" aria-hidden="true"><use href="#icon-x"></use></svg></button>
      </div>
      <div class="reply-draft hidden" id="editDraft" role="status"><div class="reply-draft-copy"><strong>Editing WhatsApp message</strong><span>Your changes will update the message for the customer.</span></div><button class="btn small" id="cancelEditBtn" type="button">Cancel</button></div>
      <div class="recording-bar hidden" id="recordingBar" role="status" aria-live="polite"><span class="recording-dot" aria-hidden="true"></span><span id="recordingTime">Recording 0:00</span><button class="btn small" id="cancelRecordingBtn" type="button">Cancel recording</button></div>
      <div class="media-draft hidden" id="mediaDraft"></div>
      <div class="composer">
        <div class="composer-box">
          <div class="composer-input-wrap">
            <label class="sr-only" for="message">Public reply to customer on WhatsApp</label>
            <textarea class="composer-textarea" id="message" dir="auto" rows="1" maxlength="4096" placeholder="Reply to customer on WhatsApp" disabled aria-describedby="composerHelp composerCount" aria-keyshortcuts="R"></textarea>
          </div>
          <div class="composer-toolbar">
            <div class="composer-tools">
              <input id="imageInput" type="file" accept="image/jpeg,image/png,image/webp,application/pdf" class="hidden" />
              <button class="icon-btn" id="imageBtn" type="button" disabled aria-label="Attach an image, sticker, or PDF" data-tooltip="Attach image, sticker, or PDF" data-tooltip-placement="top">
                <svg class="icon" aria-hidden="true"><use href="#icon-paperclip"></use></svg>
              </button>
              <button class="icon-btn" id="voiceBtn" type="button" disabled aria-label="Record a voice note" aria-pressed="false" data-tooltip="Record voice note" data-tooltip-placement="top">
                <svg class="icon" aria-hidden="true"><use href="#icon-mic"></use></svg>
              </button>
              <button class="icon-btn" id="locationBtn" type="button" disabled aria-label="Share a location" data-tooltip="Share location" data-tooltip-placement="top">
                <svg class="icon" aria-hidden="true"><use href="#icon-map-pin"></use></svg>
              </button>
            </div>
            <span class="composer-count" id="composerCount">0 / 4096</span>
            <button class="btn primary send-btn" id="sendBtn" type="button" disabled>
              <svg class="icon" aria-hidden="true"><use href="#icon-send"></use></svg><span class="send-label">Send</span>
            </button>
          </div>
        </div>
        <div class="composer-meta"><span class="reply-mode"><span class="reply-mode-dot" aria-hidden="true"></span>Public reply · WhatsApp</span><span id="composerHelp">Messages are sent from the WhatsApp number for this conversation.</span><span class="composer-shortcut"><span class="sr-only">Enter to send · Shift+Enter for a new line</span><span aria-hidden="true"><kbd>Enter</kbd> send · <kbd>Shift</kbd>+<kbd>Enter</kbd> new line ·</span><button class="shortcut-link" id="shortcutsHelpBtn" type="button">Shortcuts</button></span></div>
      </div>
    </div>
  </main>
</div>

<section class="modal-backdrop hidden" id="aiSettingsModal" role="dialog" aria-modal="true" aria-hidden="true" aria-labelledby="aiSettingsModalTitle" aria-describedby="aiSettingsModalSubtitle">
  <div class="modal-card ai-settings-card" role="document" tabindex="-1">
    <div class="modal-head">
      <div><h2 id="aiSettingsModalTitle">AI Assistant</h2><div class="modal-subtitle" id="aiSettingsModalSubtitle">Control global AI availability for new inbound WhatsApp messages.</div></div>
      <button class="icon-btn modal-close" id="closeAiSettingsBtn" type="button" aria-label="Close AI settings" data-tooltip="Close"><svg class="icon" aria-hidden="true"><use href="#icon-x"></use></svg></button>
    </div>
    <div class="modal-body">
      <div class="staff-feedback error-box" id="aiSettingsError" role="alert" aria-live="assertive"></div>
      <div class="ai-status-card">
        <div class="ai-status-copy"><strong>Current effective status</strong><span id="aiEffectiveReason">Loading current settings…</span></div>
        <span class="ai-status-pill off" id="aiEffectiveStatus">—</span>
      </div>
      <form id="aiSettingsForm" novalidate>
        <fieldset class="ai-mode-options">
          <legend>Control mode</legend>
          <label class="ai-mode-option"><input id="aiModeManual" type="radio" name="aiControlMode" value="MANUAL" /><span><strong>Manual</strong><span>Use the saved ON/OFF switch until a manager changes it.</span></span></label>
          <label class="ai-mode-option"><input id="aiModeScheduled" type="radio" name="aiControlMode" value="SCHEDULED" /><span><strong>Automatic schedule</strong><span>Calculate availability from a daily time window and IANA timezone.</span></span></label>
        </fieldset>
        <div id="aiManualFields">
          <div class="field"><label for="aiManualEnabled">AI mode</label><select id="aiManualEnabled"><option value="true">ON</option><option value="false">OFF</option></select></div>
        </div>
        <div class="hidden" id="aiScheduledFields">
          <div class="ai-schedule-grid">
            <div class="field"><label for="aiScheduleStart">AI active from</label><input id="aiScheduleStart" type="time" step="60" required /></div>
            <div class="field"><label for="aiScheduleEnd">AI active until</label><input id="aiScheduleEnd" type="time" step="60" required /></div>
            <div class="field wide"><label for="aiScheduleTimezone">Timezone</label><input id="aiScheduleTimezone" type="text" maxlength="128" placeholder="Asia/Beirut" autocomplete="off" required /><div class="field-hint">Use an IANA timezone such as Asia/Beirut, Europe/London, or UTC.</div></div>
          </div>
        </div>
        <div class="ai-next-change" id="aiNextChange">Next automatic change: —</div>
        <div class="form-note" id="aiUpdatedMeta">Saved configuration is authoritative across backend instances.</div>
        <div class="modal-actions"><button class="btn" id="cancelAiSettingsBtn" type="button">Cancel</button><button class="btn primary" id="saveAiSettingsBtn" type="submit"><svg class="icon" aria-hidden="true"><use href="#icon-check"></use></svg>Save changes</button></div>
      </form>
    </div>
  </div>
</section>

<section class="modal-backdrop hidden" id="whatsappModal" role="dialog" aria-modal="true" aria-hidden="true" aria-labelledby="whatsappModalTitle" aria-describedby="whatsappModalSubtitle">
  <div class="modal-card whatsapp-card" role="document" tabindex="-1">
    <div class="modal-head">
      <div><h2 id="whatsappModalTitle">WhatsApp numbers</h2><div class="modal-subtitle" id="whatsappModalSubtitle">Link WhatsApp numbers or sync them from Meta, then choose which numbers are active in the app.</div></div>
      <button class="icon-btn modal-close" id="closeWhatsAppBtn" type="button" aria-label="Close WhatsApp numbers"><svg class="icon" aria-hidden="true"><use href="#icon-x"></use></svg></button>
    </div>
    <div class="modal-body">
      <div class="staff-feedback error-box" id="whatsappError" role="alert" aria-live="assertive"></div>
      <div class="staff-feedback success-box" id="whatsappSuccess" role="status" aria-live="polite"></div>
      <section class="linked-device-section hidden" id="linkedDeviceSection" aria-labelledby="linkedDeviceTitle">
        <div class="section-title"><div><strong id="linkedDeviceTitle">WhatsApp linked devices</strong><div class="muted">Link each number using its phone's WhatsApp → Settings → Linked devices. Add it to the app after pairing.</div></div><button class="btn primary" id="addLinkedDeviceBtn" type="button">Link another number</button></div>
        <div id="linkedDeviceLines"></div>
        <div class="form-note" id="linkedDeviceStatus" role="status"></div>
      </section>
      <div class="section-title">
        <div><strong>Meta accounts</strong><div class="muted">The access token remains server-side. Add a WABA ID once, then Sync whenever numbers change in Meta.</div></div>
        <button class="btn" id="refreshWhatsAppBtn" type="button"><svg class="icon" aria-hidden="true"><use href="#icon-refresh"></use></svg>Refresh</button>
      </div>
      <form class="whatsapp-sync-form" id="wabaSyncForm" novalidate>
        <div class="field"><label for="wabaIdInput">WhatsApp Business Account (WABA) ID</label><input id="wabaIdInput" type="text" inputmode="numeric" pattern="[0-9]+" maxlength="64" placeholder="Enter WABA ID" required /><div class="field-hint">Found in Meta Business Manager / WhatsApp Manager. This is not a secret.</div></div>
        <button class="btn primary" id="syncWabaBtn" type="submit"><svg class="icon" aria-hidden="true"><use href="#icon-refresh"></use></svg>Connect & sync</button>
      </form>
      <div class="form-note">Newly discovered numbers are disabled by default. Select <strong>Add to app</strong> to allow customer messages on that line. Removing a line preserves its historical tickets.</div>
      <div class="whatsapp-account-list" id="whatsappAccounts"></div>
      <div class="section-title section-spaced"><div><strong>Phone numbers</strong><div class="muted">Only enabled numbers are accepted for new inbound conversations.</div></div></div>
      <div class="whatsapp-number-list" id="whatsappNumbers"></div>
    </div>
  </div>
</section>

<section class="modal-backdrop hidden" id="locationModal" role="dialog" aria-modal="true" aria-hidden="true" aria-labelledby="locationModalTitle">
  <div class="modal-card location-card-modal" role="document" tabindex="-1">
    <div class="modal-head">
      <div><h2 id="locationModalTitle">Share location</h2><div class="modal-subtitle">Send a WhatsApp location to the customer.</div></div>
      <button class="icon-btn modal-close" id="closeLocationBtn" type="button" aria-label="Close location"><svg class="icon" aria-hidden="true"><use href="#icon-x"></use></svg></button>
    </div>
    <div class="modal-body">
      <div class="staff-feedback error-box" id="locationError" role="alert"></div>
      <form id="locationForm" novalidate>
        <div class="location-grid">
          <div class="field"><label for="locationLatitude">Latitude</label><input id="locationLatitude" type="number" step="any" min="-90" max="90" required /></div>
          <div class="field"><label for="locationLongitude">Longitude</label><input id="locationLongitude" type="number" step="any" min="-180" max="180" required /></div>
          <div class="field wide"><label for="locationName">Location name <span class="muted">(optional)</span></label><input id="locationName" type="text" maxlength="255" placeholder="Office, warehouse, branch…" /></div>
          <div class="field wide"><label for="locationAddress">Address <span class="muted">(optional)</span></label><input id="locationAddress" type="text" maxlength="1000" placeholder="Street, city, country" /></div>
        </div>
        <div class="modal-actions"><button class="btn" id="useCurrentLocationBtn" type="button"><svg class="icon" aria-hidden="true"><use href="#icon-map-pin"></use></svg>Use my current location</button><button class="btn" id="cancelLocationBtn" type="button">Cancel</button><button class="btn primary" id="sendLocationBtn" type="submit"><svg class="icon" aria-hidden="true"><use href="#icon-send"></use></svg>Send location</button></div>
      </form>
    </div>
  </div>
</section>

<section class="modal-backdrop hidden" id="messagePreferencesModal" role="dialog" aria-modal="true" aria-hidden="true" aria-labelledby="messagePreferencesTitle" aria-describedby="messagePreferencesSubtitle">
  <div class="modal-card message-preferences-card" role="document" tabindex="-1">
    <div class="modal-head">
      <div><h2 id="messagePreferencesTitle">My automatic messages</h2><div class="modal-subtitle" id="messagePreferencesSubtitle">Customize the messages sent when you claim and resolve a customer ticket.</div></div>
      <button class="icon-btn modal-close" id="closeMessagePreferencesBtn" type="button" aria-label="Close automatic messages"><svg class="icon" aria-hidden="true"><use href="#icon-x"></use></svg></button>
    </div>
    <div class="modal-body">
      <div class="staff-feedback error-box" id="messagePreferencesError" role="alert" aria-live="assertive"></div>
      <form id="messagePreferencesForm" novalidate>
        <div class="field">
          <label for="customGreetingMessage">Greeting when I claim a ticket</label>
          <textarea id="customGreetingMessage" class="message-preference-textarea" rows="4" maxlength="2000" placeholder="Leave empty to use the default greeting"></textarea>
          <div class="field-hint message-preference-hint"><span>Sent automatically when you claim a ticket. Empty = existing language-aware default.</span><span id="customGreetingCount">0 / 2000</span></div>
        </div>
        <div class="field">
          <label for="customResolutionMessage">Resolution message</label>
          <textarea id="customResolutionMessage" class="message-preference-textarea" rows="4" maxlength="2000" placeholder="Leave empty to use the default resolution message"></textarea>
          <div class="field-hint message-preference-hint"><span>Sent automatically when you resolve a ticket and return it to AI. Empty = existing language-aware default.</span><span id="customResolutionCount">0 / 2000</span></div>
        </div>
        <div class="form-note">These settings belong only to your staff account. Other agents keep their own messages or the defaults.</div>
        <div class="modal-actions"><button class="btn" id="cancelMessagePreferencesBtn" type="button">Cancel</button><button class="btn primary" id="saveMessagePreferencesBtn" type="submit"><svg class="icon" aria-hidden="true"><use href="#icon-check"></use></svg>Save messages</button></div>
      </form>
    </div>
  </div>
</section>

<section class="modal-backdrop hidden" id="mediaViewerModal" role="dialog" aria-modal="true" aria-hidden="true" aria-labelledby="mediaViewerTitle">
  <div class="modal-card media-viewer-card" role="document" tabindex="-1">
    <div class="modal-head">
      <div><h2 id="mediaViewerTitle">Attachment</h2><div class="modal-subtitle" id="mediaViewerSubtitle">Customer attachment</div></div>
      <div class="media-viewer-tools hidden" id="mediaViewerTools">
        <button class="btn small" id="editMediaBtn" type="button">Edit</button>
        <a class="btn small" id="downloadMediaBtn" href="#" download>Download</a>
        <button class="btn small" id="zoomOutBtn" type="button" aria-label="Zoom out">−</button>
        <span class="media-viewer-zoom" id="mediaViewerZoom">100%</span>
        <button class="btn small" id="zoomInBtn" type="button" aria-label="Zoom in">+</button>
        <button class="btn small" id="zoomResetBtn" type="button">Reset</button>
      </div>
      <button class="icon-btn modal-close" id="closeMediaViewerBtn" type="button" aria-label="Close attachment viewer"><svg class="icon" aria-hidden="true"><use href="#icon-x"></use></svg></button>
    </div>
    <div class="media-viewer-body" id="mediaViewerBody"></div>
    <div class="image-editor-toolbar hidden" id="imageEditorToolbar">
      <button class="btn small" id="editorSelectBtn" type="button" aria-pressed="false">Select</button>
      <button class="btn small" id="editorDrawBtn" type="button" aria-pressed="true">Draw</button>
      <button class="btn small" id="editorTextBtn" type="button" aria-pressed="false">Text</button>
      <label>Color <input id="editorColor" type="color" value="#ff3b30" /></label>
      <label>Size <input id="editorSize" type="range" min="2" max="40" value="8" /></label>
      <button class="btn small" id="editorUndoBtn" type="button">Undo</button>
      <button class="btn small" id="editorRedoBtn" type="button">Redo</button>
      <button class="btn small" id="editorEditTextBtn" type="button">Edit selected text</button>
      <button class="btn small" id="editorDeleteBtn" type="button">Remove selected</button>
      <input id="editorCaption" type="text" maxlength="1024" placeholder="Caption (optional)" aria-label="Image caption" />
      <button class="btn primary small" id="editorSendBtn" type="button">Send image</button>
    </div>
  </div>
</section>

<section class="modal-backdrop hidden" id="messageDeleteModal" role="dialog" aria-modal="true" aria-hidden="true" aria-labelledby="messageDeleteTitle">
  <div class="modal-card transfer-card" role="document" tabindex="-1">
    <div class="modal-head"><div><h2 id="messageDeleteTitle">Delete message</h2><div class="modal-subtitle" id="messageDeleteSubtitle">Delete for me removes the message from your support view.</div></div><button class="icon-btn modal-close" id="cancelMessageDeleteBtn" type="button" aria-label="Cancel deletion"><svg class="icon" aria-hidden="true"><use href="#icon-x"></use></svg></button></div>
    <div class="modal-body"><div class="modal-actions"><button class="btn danger" id="deleteMeBtn" type="button">Delete for me</button><button class="btn danger hidden" id="deleteEveryoneBtn" type="button">Delete for everyone</button><button class="btn" id="cancelMessageDeleteActionBtn" type="button">Cancel</button></div></div>
  </div>
</section>

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
          <div><strong>Existing staff</strong><div class="muted">Active and deactivated support accounts. Deleted accounts are removed from this list.</div></div>
          <div class="staff-actions-header"><button class="btn primary" id="createStaffBtn" type="button"><svg class="icon" aria-hidden="true"><use href="#icon-plus"></use></svg>Create staff</button><button class="btn" id="refreshStaffBtn" type="button"><svg class="icon" aria-hidden="true"><use href="#icon-refresh"></use></svg>Refresh</button></div>
        </div>
        <div class="staff-list" role="table" aria-label="Support staff accounts">
          <div class="staff-list-head" role="row"><span role="columnheader">Staff member</span><span role="columnheader">Email</span><span role="columnheader">Role</span><span role="columnheader">Status</span><span role="columnheader">Actions</span></div>
          <div id="staffList" role="rowgroup"><div class="empty-state"><div class="empty-state-inner"><div class="empty-icon"><svg class="icon icon-lg" aria-hidden="true"><use href="#icon-users"></use></svg></div><h3>Loading staff</h3><p>Retrieving support accounts.</p></div></div></div>
        </div>
      </div>

      <div class="staff-view hidden" id="staffCreateView">
        <div class="staff-view-heading"><div><strong>Create staff account</strong><div class="muted">Add a new agent, administrator, or supervisor.</div></div><button class="btn" id="backFromCreateBtn" type="button"><svg class="icon" aria-hidden="true"><use href="#icon-arrow-left"></use></svg>Back to staff</button></div>
        <form id="createStaffForm" novalidate>
          <div class="staff-grid">
            <div class="field"><label for="createStaffName">Name <span class="required-mark" aria-hidden="true">*</span></label><input id="createStaffName" type="text" minlength="2" maxlength="120" autocomplete="off" required /></div>
            <div class="field"><label for="createStaffEmail">Email <span class="required-mark" aria-hidden="true">*</span></label><input id="createStaffEmail" type="email" maxlength="320" autocomplete="off" required /></div>
            <div class="field"><label for="createStaffRole">Role <span class="required-mark" aria-hidden="true">*</span></label><select id="createStaffRole" required><option value="agent">Agent</option><option value="admin">Admin</option><option value="supervisor">Supervisor</option></select></div>
            <div class="field"><span class="field-label">Status</span><div class="check-field static"><svg class="icon icon-sm" aria-hidden="true"><use href="#icon-check-circle"></use></svg>Active after creation</div></div>
            <div class="field"><label for="createStaffPassword">Initial password <span class="required-mark" aria-hidden="true">*</span></label><input id="createStaffPassword" type="password" minlength="12" maxlength="72" autocomplete="new-password" required aria-describedby="createPasswordHint" /><div class="field-hint" id="createPasswordHint">12 characters minimum; 72 UTF-8 bytes maximum.</div></div>
            <div class="field"><label for="createStaffPasswordConfirm">Confirm password <span class="required-mark" aria-hidden="true">*</span></label><input id="createStaffPasswordConfirm" type="password" minlength="12" maxlength="72" autocomplete="new-password" required aria-describedby="createPasswordConfirmHint createPasswordMatchError" /><div class="field-hint" id="createPasswordConfirmHint">Enter the same password again.</div><div class="field-error hidden" id="createPasswordMatchError" role="alert"></div></div>
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
            <div class="field"><label for="editStaffRole">Role <span class="required-mark" aria-hidden="true">*</span></label><select id="editStaffRole" required><option value="agent">Agent</option><option value="admin">Admin</option><option value="supervisor">Supervisor</option></select></div>
            <div class="field"><span class="field-label">Status</span><label class="check-field"><input id="editStaffActive" type="checkbox" checked /> Account active</label></div>
            <div class="field"><label for="editStaffPassword">New password <span class="muted">(optional)</span></label><input id="editStaffPassword" type="password" minlength="12" maxlength="72" autocomplete="new-password" aria-describedby="editPasswordHint" /><div class="field-hint" id="editPasswordHint">Leave blank to keep the current password.</div></div>
            <div class="field"><label for="editStaffPasswordConfirm">Confirm new password</label><input id="editStaffPasswordConfirm" type="password" minlength="12" maxlength="72" autocomplete="new-password" aria-describedby="editPasswordConfirmHint editPasswordMatchError" /><div class="field-hint" id="editPasswordConfirmHint">Required only when setting a new password.</div><div class="field-error hidden" id="editPasswordMatchError" role="alert"></div></div>
          </div>
          <div class="form-note">Credential, role, or activation changes revoke the affected account’s current sessions.</div>
          <div class="modal-actions"><button class="btn" id="cancelEditStaffBtn" type="button">Cancel</button><button class="btn primary" id="editStaffSubmitBtn" type="submit"><svg class="icon" aria-hidden="true"><use href="#icon-check"></use></svg>Save changes</button></div>
        </form>
      </div>
    </div>
  </div>
</section>

<section class="modal-backdrop hidden" id="startChatModal" role="dialog" aria-modal="true" aria-hidden="true" aria-labelledby="startChatTitle">
  <div class="modal-card transfer-card" role="document" tabindex="-1">
    <div class="modal-head">
      <div><h2 id="startChatTitle">Start WhatsApp chat</h2><div class="modal-subtitle">Start a staff-owned conversation with a customer.</div></div>
      <button class="icon-btn modal-close" id="closeStartChatBtn" type="button" aria-label="Close"><svg class="icon"><use href="#icon-x"></use></svg></button>
    </div>
    <div class="modal-body">
      <div class="staff-feedback error-box" id="startChatError" role="alert"></div>
      <form id="startChatForm" novalidate>
        <div class="field"><label for="startChatLine">Send from</label><select id="startChatLine" required></select><div class="field-hint" id="startChatLinePreview"></div></div>
        <div class="field"><label for="startChatPhone">Customer WhatsApp number</label><input id="startChatPhone" type="tel" inputmode="tel" placeholder="96170123456" maxlength="32" required /><div class="field-hint">Use the international number, with or without +.</div></div>
        <div class="field"><label for="startChatName">Customer name <span class="muted">(optional)</span></label><input id="startChatName" type="text" maxlength="191" /></div>
        <div class="field"><label for="startChatBody">First message</label><textarea id="startChatBody" rows="5" maxlength="4096" required placeholder="Write the first WhatsApp message"></textarea></div>
        <div class="form-note">If WhatsApp's customer-service window is closed, Meta may require an approved template instead of a free-form first message.</div>
        <div class="modal-actions"><button class="btn" id="cancelStartChatBtn" type="button">Cancel</button><button class="btn primary" id="confirmStartChatBtn" type="submit"><svg class="icon"><use href="#icon-send"></use></svg>Start chat</button></div>
      </form>
    </div>
  </div>
</section>

<section class="modal-backdrop hidden" id="forwardModal" role="dialog" aria-modal="true" aria-hidden="true" aria-labelledby="forwardTitle">
  <div class="modal-card transfer-card" role="document" tabindex="-1">
    <div class="modal-head">
      <div><h2 id="forwardTitle">Forward message</h2><div class="modal-subtitle" id="forwardSubtitle">Forward a message from any conversation you can view.</div></div>
      <button class="icon-btn modal-close" id="closeForwardBtn" type="button" aria-label="Close"><svg class="icon"><use href="#icon-x"></use></svg></button>
    </div>
    <div class="modal-body">
      <div class="staff-feedback error-box" id="forwardError" role="alert"></div>
      <div class="field"><label for="forwardSearch">Search contact or conversation</label><input id="forwardSearch" type="search" autocomplete="off" placeholder="Search name or phone number" /></div>
      <div class="contact-picker-section"><div class="contact-picker-section-title">Contacts</div><div class="contact-picker-list" id="forwardContactsList"></div></div>
      <div class="contact-picker-section"><div class="contact-picker-section-title">Active conversations</div><div class="contact-picker-list" id="forwardConversationsList"></div></div>
      <div class="form-note">Selecting a contact forwards through the source WhatsApp line and opens/reopens a staff chat when needed. Media is re-uploaded securely.</div>
      <div class="modal-actions"><button class="btn" id="cancelForwardBtn" type="button">Cancel</button><button class="btn primary" id="confirmForwardBtn" type="button"><svg class="icon"><use href="#icon-reply"></use></svg>Forward</button></div>
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

<section class="modal-backdrop hidden" id="confirmModal" role="alertdialog" aria-modal="true" aria-hidden="true" aria-labelledby="confirmTitle" aria-describedby="confirmMessage">
  <div class="modal-card confirm-card" role="document" tabindex="-1" data-tone="default">
    <form class="modal-body" id="confirmForm" novalidate>
      <div class="confirm-body">
        <span class="confirm-icon" aria-hidden="true"><svg class="icon"><use id="confirmIconUse" href="#icon-info"></use></svg></span>
        <div class="confirm-copy">
          <h2 id="confirmTitle">Are you sure?</h2>
          <p class="confirm-message" id="confirmMessage"></p>
          <div class="field hidden" id="confirmInputField"><label for="confirmInput" id="confirmInputLabel">Value</label><input id="confirmInput" type="text" maxlength="191" autocomplete="off" /></div>
        </div>
      </div>
      <div class="modal-actions"><button class="btn" id="confirmCancelBtn" type="button">Cancel</button><button class="btn primary" id="confirmAcceptBtn" type="submit">Confirm</button></div>
    </form>
  </div>
</section>

<section class="modal-backdrop hidden" id="shortcutsModal" role="dialog" aria-modal="true" aria-hidden="true" aria-labelledby="shortcutsTitle">
  <div class="modal-card shortcuts-card" role="document" tabindex="-1">
    <div class="modal-head">
      <div><h2 id="shortcutsTitle">Keyboard shortcuts</h2><div class="modal-subtitle">Shortcuts work when you are not typing in a field.</div></div>
      <button class="icon-btn modal-close" id="closeShortcutsBtn" type="button" aria-label="Close keyboard shortcuts"><svg class="icon" aria-hidden="true"><use href="#icon-x"></use></svg></button>
    </div>
    <div class="modal-body">
      <dl class="shortcut-list">
        <div class="shortcut-row"><dt>Next conversation in the list</dt><dd><kbd>J</kbd></dd></div>
        <div class="shortcut-row"><dt>Previous conversation in the list</dt><dd><kbd>K</kbd></dd></div>
        <div class="shortcut-row"><dt>Reply to the open conversation</dt><dd><kbd>R</kbd></dd></div>
        <div class="shortcut-row"><dt>Search conversations and messages</dt><dd><kbd>/</kbd> or <kbd>Ctrl</kbd><kbd>K</kbd></dd></div>
        <div class="shortcut-row"><dt>Send the reply</dt><dd><kbd>Enter</kbd></dd></div>
        <div class="shortcut-row"><dt>New line in the reply</dt><dd><kbd>Shift</kbd><kbd>Enter</kbd></dd></div>
        <div class="shortcut-row"><dt>Close a panel, dialog, or edit</dt><dd><kbd>Esc</kbd></dd></div>
        <div class="shortcut-row"><dt>Show this list</dt><dd><kbd>?</kbd></dd></div>
      </dl>
    </div>
  </div>
</section>

<div class="toast-region" id="toastRegion" aria-live="polite" aria-atomic="false"></div>
<div class="sr-only" id="appStatus" role="status" aria-live="polite" aria-atomic="true"></div>
<script>
const QUEUE_CONFIG = {
  unassigned: {label: 'Unassigned', emptyTitle: 'No unassigned tickets', emptyBody: 'New customer escalations will appear here when they need an owner.', icon: 'inbox'},
  mine: {label: 'My tickets', emptyTitle: 'No tickets assigned to you', emptyBody: 'Claim an unassigned ticket or wait for a transfer from another agent.', icon: 'user-check'},
  chats: {label: 'Chats', emptyTitle: 'No staff chats yet', emptyBody: 'Use New chat to start a WhatsApp conversation with a customer.', icon: 'message'},
  contacts: {label: 'Contacts', emptyTitle: 'No contacts yet', emptyBody: 'Save a customer contact, sync Google Contacts, or import a VCF address book.', icon: 'users'},
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
const REACTION_EMOJIS = ['👍', '❤️', '😂', '😮', '😢', '🙏'];
// Queue rows in these views tint their time as a customer keeps waiting.
const WAITING_AGE_VIEWS = ['unassigned', 'waiting'];
const WAITING_AGE_WARN_MINUTES = 5;
const WAITING_AGE_ALERT_MINUTES = 15;
const state = {
  view: 'unassigned',
  page: 1,
  pageSize: 30,
  total: 0,
  queueItems: [],
  queueTotals: {},
  queueCountsBusy: false,
  queueCountsPending: false,
  query: '',
  globalSearchResults: [],
  globalSearchBusy: false,
  globalSearchTimer: null,
  globalSearchRequest: 0,
  conversationSearchOpen: false,
  conversationSearchQuery: '',
  conversationSearchResults: [],
  conversationSearchIndex: -1,
  conversationSearchTimer: null,
  searchTargetMessageId: null,
  queueRequest: 0,
  ticketRequest: 0,
  ticketId: null,
  currentTicketData: null,
  currentConversationOrigin: null,
  previousTicketHistory: [],
  previousTicketHistoryHasMore: false,
  previousHistoryExpanded: false,
  previousHistoryLoaded: false,
  previousHistoryPending: false,
  historyJumpActive: false,
  historyJumpReachedTop: false,
  cachedCurrentMessages: [],
  currentMessagesHasMore: false,
  currentMessagesExpanded: false,
  historyLoadBusy: false,
  historyLoadRequest: 0,
  historySelectionEpoch: 0,
  timelineSignature: '',
  pendingConversationRender: null,
  conversationSelectionPointer: null,
  agent: null,
  busy: false,
  authenticated: false,
  pageLeaving: false,
  staffBusy: false,
  messagePreferencesBusy: false,
  aiSettingsBusy: false,
  aiSettings: null,
  whatsappBusy: false,
  whatsappRegistry: null,
  linkedDeviceStatus: null,
  linkedDeviceBusy: false,
  linkedDeviceRequest: 0,
  linkedDeviceAdditionId: null,
  chatBusy: false,
  chatLines: [],
  forwardTarget: null,
  forwardBusy: false,
  forwardSelection: null,
  forwardContacts: [],
  forwardConversations: [],
  forwardSearchTimer: null,
  forwardChoiceRequest: 0,
  googleContactsStatus: null,
  googleContactsBusy: false,
  vcfImportBusy: false,
  staffItems: [],
  staffView: 'list',
  editingStaffId: null,
  editingStaffOriginal: null,
  conflictStaff: null,
  composerEnabled: false,
  pendingMedia: null,
  replyTarget: null,
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
  contextOpen: false,
  notificationAudioContext: null,
  notificationUnlocked: false,
  notificationPermissionRequested: false,
  supportEventSource: null,
  mediaZoom: 1,
  mediaTranslateX: 0,
  mediaTranslateY: 0,
  mediaSource: null,
  editTarget: null,
  deleteTarget: null,
  imageEditor: null,
  unassignedSnapshot: null,
  lastInboundByTicket: {},
  localMessages: new Map(),
  realtimeMessages: new Map(),
  textSendChain: Promise.resolve()
};

const SUPPORT_SELECTION_STORAGE_PREFIX = 'shipment-support-workspace-v1:';

function supportSelectionStorageKey() {
  const staffId = String(state.agent?.id || '').trim();
  return staffId ? SUPPORT_SELECTION_STORAGE_PREFIX + encodeURIComponent(staffId) : null;
}

function saveSupportSelection() {
  if (!state.authenticated) return;
  const key = supportSelectionStorageKey();
  if (!key) return;
  const ticketId = Number(state.ticketId);
  const origin = String(state.currentConversationOrigin || '').toUpperCase();
  const selected = Number.isSafeInteger(ticketId) && ticketId > 0 && ['STAFF', 'CUSTOMER'].includes(origin);
  try {
    sessionStorage.setItem(key, JSON.stringify({
      view: Object.prototype.hasOwnProperty.call(QUEUE_CONFIG, state.view) ? state.view : 'unassigned',
      page: Number.isSafeInteger(state.page) && state.page > 0 ? state.page : 1,
      ticketId: selected ? ticketId : null,
      conversationOrigin: selected ? origin : null
    }));
  } catch (_) {}
}

function readSupportSelection() {
  if (!state.authenticated) return null;
  const key = supportSelectionStorageKey();
  if (!key) return null;
  try {
    const raw = sessionStorage.getItem(key);
    if (!raw) return null;
    const saved = JSON.parse(raw);
    if (!saved || !Object.prototype.hasOwnProperty.call(QUEUE_CONFIG, saved.view)) throw new Error('Invalid view');
    const ticketId = Number.isSafeInteger(saved.ticketId) && saved.ticketId > 0 ? saved.ticketId : null;
    const origin = ['STAFF', 'CUSTOMER'].includes(saved.conversationOrigin) ? saved.conversationOrigin : null;
    return {
      view: saved.view,
      page: Number.isSafeInteger(saved.page) && saved.page > 0 && saved.page <= 10000 ? saved.page : 1,
      ticketId: ticketId && origin ? ticketId : null,
      conversationOrigin: ticketId && origin ? origin : null
    };
  } catch (_) {
    try { sessionStorage.removeItem(key); } catch (_) {}
    return null;
  }
}

function clearSupportSelections() {
  try {
    for (let index = sessionStorage.length - 1; index >= 0; index -= 1) {
      const key = sessionStorage.key(index);
      if (key?.startsWith(SUPPORT_SELECTION_STORAGE_PREFIX)) sessionStorage.removeItem(key);
    }
  } catch (_) {}
}

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

function messageOccurredAt(message) {
  const numeric = Number(message?.whatsapp_timestamp);
  if (Number.isFinite(numeric) && numeric > 0) {
    const milliseconds = numeric < 100_000_000_000 ? numeric * 1000 : numeric;
    const occurred = new Date(milliseconds);
    if (!Number.isNaN(occurred.getTime()) && milliseconds <= Date.now() + 300_000) {
      return occurred.toISOString();
    }
  }
  return message?.created_at || null;
}

function fmt(value) {
  if (!value) return '—';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return new Intl.DateTimeFormat(undefined, {dateStyle: 'medium', timeStyle: 'short'}).format(date);
}

function fmtInTimezone(value, timezone) {
  if (!value) return '—';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  try {
    return new Intl.DateTimeFormat(undefined, {
      year: 'numeric',
      month: 'short',
      day: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
      timeZone: timezone || 'UTC',
      timeZoneName: 'short'
    }).format(date);
  } catch (_) {
    return fmt(value);
  }
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
  if (state.pageLeaving) return;
  const banner = $('error');
  text($('errorText'), message || '');
  banner.classList.toggle('visible', Boolean(message));
}

function markPageLeaving() {
  if (state.pageLeaving) return;
  state.pageLeaving = true;
  state.queueRequest += 1;
  state.ticketRequest += 1;
}

function notify(message, type = 'success') {
  if (!message || state.pageLeaving) return;
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

function unlockNotificationAudio() {
  try {
    const AudioContextClass = window.AudioContext || window.webkitAudioContext;
    if (AudioContextClass && !state.notificationAudioContext) state.notificationAudioContext = new AudioContextClass();
    if (state.notificationAudioContext && state.notificationAudioContext.state === 'suspended') state.notificationAudioContext.resume().catch(() => {});
    state.notificationUnlocked = Boolean(state.notificationAudioContext);
  } catch (_) {}
  if (state.authenticated && !state.notificationPermissionRequested && 'Notification' in window && Notification.permission === 'default') {
    state.notificationPermissionRequested = true;
    try { Notification.requestPermission().catch(() => {}); } catch (_) {}
  }
}

function playNewConversationSound() {
  const context = state.notificationAudioContext;
  if (!state.notificationUnlocked || !context || context.state === 'closed') return;
  const play = () => {
    try {
      const start = context.currentTime + 0.01;
      for (const [offset, frequency] of [[0, 740], [0.16, 980], [0.32, 820]]) {
        const oscillator = context.createOscillator();
        const gain = context.createGain();
        oscillator.type = 'sine';
        oscillator.frequency.value = frequency;
        gain.gain.setValueAtTime(0.0001, start + offset);
        gain.gain.exponentialRampToValueAtTime(0.22, start + offset + 0.02);
        gain.gain.exponentialRampToValueAtTime(0.0001, start + offset + 0.14);
        oscillator.connect(gain);
        gain.connect(context.destination);
        oscillator.start(start + offset);
        oscillator.stop(start + offset + 0.15);
      }
    } catch (_) {}
  };
  if (context.state === 'suspended') context.resume().then(play).catch(() => {});
  else play();
}

function notifyNewCustomerActivity(message) {
  const body = message || 'New customer conversation requires attention.';
  playNewConversationSound();
  notify(body);
  document.title = '● Shipment Support';
  if ('setAppBadge' in navigator) navigator.setAppBadge(1).catch(() => {});
  if ('Notification' in window && Notification.permission === 'granted') {
    try { new Notification('Shipment Support', {body, tag: 'shipment-support', renotify: true}); } catch (_) {}
  }
}

function clearAttentionIndicator() {
  document.title = 'Shipment Support';
  if ('clearAppBadge' in navigator) navigator.clearAppBadge().catch(() => {});
}

function disconnectSupportEvents() {
  if (state.supportEventSource) {
    state.supportEventSource.close();
    state.supportEventSource = null;
  }
}

function connectSupportEvents() {
  disconnectSupportEvents();
  if (!state.authenticated || typeof EventSource === 'undefined') return;
  const source = new EventSource('/api/support/events', {withCredentials: true});
  state.supportEventSource = source;
  source.onmessage = event => {
    let payload = {};
    try { payload = JSON.parse(event.data || '{}'); } catch (_) { return; }
    const type = String(payload.type || '');
    if (type === 'outbound_message' || type === 'customer_message' || type === 'message_media_ready') {
      if (payload.authorized !== true) return;
      if (payload.message && state.ticketId && String(state.ticketId) === String(payload.ticket_id || '')) {
        upsertTimelineMessage(payload.message, payload.ticket_id);
      } else if (state.ticketId && String(state.ticketId) === String(payload.ticket_id || '')) {
        openTicket(state.ticketId, {silent: true}).catch(() => {});
      } else if (type === 'message_media_ready' && state.previousTicketHistory.some(thread => String(thread.ticket?.id) === String(payload.ticket_id))) {
        if (payload.message) {
          changeCachedMessage(payload.ticket_id, payload.message.id, payload.message);
          if (state.currentTicketData) {
            state.timelineSignature = timelineSignature(state.currentTicketData);
            renderConversation(state.currentTicketData);
          }
        } else if (state.ticketId) openTicket(state.ticketId, {silent: true, refreshHistory: true}).catch(() => {});
      }
      if (type === 'outbound_message') {
        // Messages sent from the linked WhatsApp app can change a ticket's
        // preview and place in the queue even when another ticket is open.
        // Refreshing only the queue leaves any playing voice note untouched.
        refreshAllQueueCounts().catch(() => {});
        if (state.view !== 'contacts') loadQueue({silent: true}).catch(() => {});
        return;
      }
      if (type !== 'customer_message') return;
    }
    refreshAllQueueCounts().catch(() => {});
    if (type === 'ticket_updated') {
      if (payload.authorized !== true) return;
      if (state.ticketId && String(state.ticketId) === String(payload.ticket_id || '')) {
        openTicket(state.ticketId, {silent: true}).catch(() => {});
      }
      loadQueue({silent: true}).catch(() => {});
      return;
    }
    if (type === 'contacts_synced' || type === 'contact_saved') {
      if (!payload.staff_id || String(payload.staff_id) === String(state.agent?.id || '')) {
        if (state.view === 'contacts') loadQueue({silent: true}).catch(() => {});
        loadGoogleContactsStatus().catch(() => {});
      }
      return;
    }
    if (['message_updated', 'message_deleted_for_everyone', 'message_deleted_for_me'].includes(type)) {
      if (payload.authorized === true) {
        const changedId = Number(payload.message_id);
        state.realtimeMessages.delete(String(payload.ticket_id) + ':' + changedId);
        if (type === 'message_deleted_for_everyone' && state.editTarget?.id === changedId && String(state.editTarget.ticketId) === String(payload.ticket_id)) {
          cancelMessageEdit();
          notify('The message you were editing was deleted on WhatsApp.', 'error');
        }
        if (type === 'message_deleted_for_me') {
          state.realtimeMessages.delete(String(payload.ticket_id) + ':' + changedId);
          changeCachedMessage(payload.ticket_id, changedId, null, true);
        }
        if (type !== 'message_updated' && state.mediaSource && state.mediaSource.includes('/messages/' + changedId + '/media')) {
          if (state.imageEditor) notify('The original image was deleted. Your edits remain open, but it cannot be sent.', 'error');
          else closeMediaViewer();
        }
        refreshMessageSearchAfterChange(changedId);
        if (state.ticketId && String(state.ticketId) === String(payload.ticket_id || '')) {
          if (payload.message && type !== 'message_deleted_for_me') {
            upsertTimelineMessage(payload.message, payload.ticket_id);
            if (type === 'message_deleted_for_everyone' && state.replyTarget?.id === changedId) clearReplyTarget();
          } else openTicket(state.ticketId, {silent: true}).catch(() => {});
        } else if (state.previousTicketHistory.some(thread => String(thread.ticket?.id) === String(payload.ticket_id))) {
          if (payload.message && type !== 'message_deleted_for_me') changeCachedMessage(payload.ticket_id, changedId, payload.message);
          openTicket(state.ticketId, {silent: true, refreshHistory: true}).catch(() => {});
        }
      }
      return;
    }
    if (!['ticket_created', 'customer_message', 'message_reaction'].includes(type)) return;
    // Ticket/chat events are only actionable when the per-session backend SSE
    // authorizer approved them. This prevents future event types from accidentally
    // reintroducing client-side-only notification filtering.
    if (payload.authorized !== true) return;
    const customer = payload.customer_name || payload.customer_phone || 'customer';
    const notificationText = type === 'ticket_created'
      ? (payload.phone_initiated === true
          ? 'New unassigned WhatsApp app conversation with ' + customer + '.'
          : 'New unassigned conversation from ' + customer + '.')
      : type === 'message_reaction'
        ? 'New WhatsApp reaction from ' + customer + '.'
        : 'New customer message from ' + customer + '.';
    notifyNewCustomerActivity(notificationText);
    state.unassignedSnapshot = null;
    if (state.view === 'unassigned') loadQueue({silent: true}).catch(() => {});
    else pollUnassignedActivity().catch(() => {});
    if (type !== 'customer_message' && state.ticketId && String(state.ticketId) === String(payload.ticket_id || '')) openTicket(state.ticketId, {silent: true}).catch(() => {});
  };
  source.onerror = () => {
    // EventSource retries automatically. Polling remains active as a fallback.
  };
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
  if (themeMeta) themeMeta.setAttribute('content', dark ? '#0b1115' : '#f4f6f8');
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

// In-app replacement for window.confirm/prompt/alert. It can open on top of
// another dialog and restores that dialog's focus trap when it settles.
const CONFIRM_TONE_ICONS = {default: 'info', danger: 'alert', success: 'check-circle', warning: 'alert'};
let pendingConfirmDialog = null;

function settleConfirmDialog(result) {
  const pending = pendingConfirmDialog;
  if (!pending) return;
  pendingConfirmDialog = null;
  closeDialog('confirmModal', false);
  state.activeModal = pending.previousModal;
  state.lastFocused = pending.previousLastFocused;
  if (state.activeModal) document.body.classList.add('modal-open');
  const focusTarget = pending.returnFocus;
  if (focusTarget && typeof focusTarget.focus === 'function') window.setTimeout(() => focusTarget.focus(), 10);
  pending.resolve(pending.kind === 'prompt' ? (result ? $('confirmInput').value : null) : Boolean(result));
}

function showConfirmDialog(kind, {title, message = '', confirmLabel = 'Confirm', cancelLabel = 'Cancel', tone = 'default', label = '', value = ''} = {}) {
  if (pendingConfirmDialog) settleConfirmDialog(false);
  const card = $('confirmModal').querySelector('.confirm-card');
  card.dataset.tone = tone;
  $('confirmIconUse').setAttribute('href', '#icon-' + (CONFIRM_TONE_ICONS[tone] || 'info'));
  text($('confirmTitle'), title || (kind === 'alert' ? 'Notice' : 'Are you sure?'));
  text($('confirmMessage'), message);
  $('confirmMessage').classList.toggle('hidden', !message);
  $('confirmInputField').classList.toggle('hidden', kind !== 'prompt');
  text($('confirmInputLabel'), label || title || 'Value');
  $('confirmInput').value = value || '';
  const accept = $('confirmAcceptBtn');
  accept.className = 'btn ' + (tone === 'danger' ? 'danger' : tone === 'success' ? 'success-action' : 'primary');
  text(accept, confirmLabel);
  text($('confirmCancelBtn'), cancelLabel);
  $('confirmCancelBtn').classList.toggle('hidden', kind === 'alert');
  const previousModal = state.activeModal;
  const previousLastFocused = state.lastFocused;
  const returnFocus = document.activeElement;
  const focusId = kind === 'prompt' ? 'confirmInput' : tone === 'danger' ? 'confirmCancelBtn' : 'confirmAcceptBtn';
  return new Promise(resolve => {
    pendingConfirmDialog = {kind, resolve, previousModal, previousLastFocused, returnFocus};
    openDialog('confirmModal', focusId);
    if (kind === 'prompt') window.setTimeout(() => $('confirmInput').select(), 30);
  });
}

function confirmDialog(options) {
  return showConfirmDialog('confirm', options);
}

function promptDialog(options) {
  return showConfirmDialog('prompt', options);
}

function alertDialog(options) {
  return showConfirmDialog('alert', {confirmLabel: 'OK', ...options});
}

function openShortcutsHelp() {
  if (!state.authenticated || state.activeModal) return;
  openDialog('shortcutsModal', 'closeShortcutsBtn');
}

function closeShortcutsHelp() {
  closeDialog('shortcutsModal');
}

const LAYOUT_STORAGE = {
  queueWidth: 'shipment-support-queue-width',
  contextWidth: 'shipment-support-context-width',
  contextCollapsed: 'shipment-support-context-collapsed'
};

function clampLayoutValue(value, minimum, maximum) {
  return Math.min(maximum, Math.max(minimum, Number(value) || minimum));
}

function readLayoutWidth(variableName, fallback) {
  const raw = getComputedStyle(document.documentElement).getPropertyValue(variableName);
  const parsed = Number.parseFloat(raw);
  return Number.isFinite(parsed) ? parsed : fallback;
}

function setLayoutWidth(variableName, storageKey, value, minimum, maximum, handle = null, persist = true) {
  const width = Math.round(clampLayoutValue(value, minimum, maximum));
  document.documentElement.style.setProperty(variableName, width + 'px');
  if (handle) handle.setAttribute('aria-valuenow', String(width));
  if (persist) {
    try { localStorage.setItem(storageKey, String(width)); } catch (_) {}
  }
  return width;
}

function restoreLayoutPreferences() {
  try {
    const queueWidth = Number(localStorage.getItem(LAYOUT_STORAGE.queueWidth));
    if (Number.isFinite(queueWidth) && queueWidth > 0) setLayoutWidth('--queue-width', LAYOUT_STORAGE.queueWidth, queueWidth, 260, 520, $('queueResizer'), false);
    const contextWidth = Number(localStorage.getItem(LAYOUT_STORAGE.contextWidth));
    if (Number.isFinite(contextWidth) && contextWidth > 0) setLayoutWidth('--context-width', LAYOUT_STORAGE.contextWidth, contextWidth, 260, 520, $('contextResizer'), false);
    const collapsed = localStorage.getItem(LAYOUT_STORAGE.contextCollapsed);
    state.contextOpen = collapsed === null ? false : collapsed !== 'true';
  } catch (_) {
    state.contextOpen = false;
  }
  $('queueResizer').setAttribute('aria-valuenow', String(Math.round(readLayoutWidth('--queue-width', 340))));
  $('contextResizer').setAttribute('aria-valuenow', String(Math.round(readLayoutWidth('--context-width', 300))));
  syncContextLayout();
}

function syncContextLayout() {
  const desktop = window.innerWidth > 1100;
  const workspace = $('workspaceContent');
  const panel = $('contextPanel');
  const backdrop = $('contextBackdrop');
  const toggle = $('contextToggleBtn');
  if (desktop) {
    workspace.classList.toggle('context-collapsed', !state.contextOpen);
    panel.classList.remove('open');
    backdrop.classList.remove('visible');
  } else {
    workspace.classList.remove('context-collapsed');
    panel.classList.toggle('open', state.contextOpen);
    backdrop.classList.toggle('visible', state.contextOpen);
  }
  toggle.setAttribute('aria-expanded', state.contextOpen ? 'true' : 'false');
  toggle.setAttribute('aria-label', state.contextOpen ? 'Close customer details' : 'Open customer details');
  toggle.setAttribute('data-tooltip', state.contextOpen ? 'Hide customer details' : 'Customer details');
}

function rememberContextPreference() {
  try { localStorage.setItem(LAYOUT_STORAGE.contextCollapsed, state.contextOpen ? 'false' : 'true'); } catch (_) {}
}

function openContextPanel() {
  state.contextOpen = true;
  rememberContextPreference();
  syncContextLayout();
  if (window.innerWidth <= 1100) window.setTimeout(() => $('closeContextBtn').focus(), 20);
}

function closeContextPanel(restoreFocus = true) {
  state.contextOpen = false;
  rememberContextPreference();
  syncContextLayout();
  if (restoreFocus) $('contextToggleBtn').focus();
}

function toggleContextPanel() {
  if (state.contextOpen) closeContextPanel(false);
  else openContextPanel();
}

function installColumnResizer({id, variableName, storageKey, minimum, maximum, direction = 1, enabled}) {
  const handle = $(id);
  if (!handle) return;
  let startX = 0;
  let startWidth = 0;
  let activePointer = null;

  const setWidth = (value, persist = true) => setLayoutWidth(variableName, storageKey, value, minimum, maximum, handle, persist);
  const stop = () => {
    if (activePointer === null) return;
    activePointer = null;
    handle.classList.remove('active');
    document.body.classList.remove('column-resizing');
  };

  handle.addEventListener('pointerdown', event => {
    if (enabled && !enabled()) return;
    if (event.button !== 0) return;
    activePointer = event.pointerId;
    startX = event.clientX;
    startWidth = readLayoutWidth(variableName, minimum);
    handle.classList.add('active');
    document.body.classList.add('column-resizing');
    handle.setPointerCapture?.(event.pointerId);
    event.preventDefault();
  });
  handle.addEventListener('pointermove', event => {
    if (activePointer !== event.pointerId) return;
    setWidth(startWidth + ((event.clientX - startX) * direction), false);
  });
  handle.addEventListener('pointerup', event => {
    if (activePointer !== event.pointerId) return;
    setWidth(readLayoutWidth(variableName, minimum), true);
    stop();
  });
  handle.addEventListener('pointercancel', stop);
  handle.addEventListener('lostpointercapture', stop);
  window.addEventListener('blur', stop);
  handle.addEventListener('keydown', event => {
    if (enabled && !enabled()) return;
    if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
    event.preventDefault();
    if (event.key === 'Home') return void setWidth(minimum);
    if (event.key === 'End') return void setWidth(maximum);
    const step = event.shiftKey ? 32 : 16;
    const current = readLayoutWidth(variableName, minimum);
    const delta = event.key === 'ArrowRight' ? step : -step;
    setWidth(current + (delta * direction));
  });
  handle.addEventListener('dblclick', () => {
    const defaultWidth = variableName === '--queue-width' ? 340 : 300;
    setWidth(defaultWidth);
  });
}

function isTypingTarget(target) {
  if (!target || !(target instanceof Element)) return false;
  return Boolean(target.closest('input, textarea, select, [contenteditable=""], [contenteditable="true"]'));
}

function moveQueueSelection(delta) {
  const rows = [...$('queue').querySelectorAll('.ticket-row, .search-result-row')];
  if (!rows.length) return;
  const current = rows.findIndex(row => row.classList.contains('active') || row === document.activeElement);
  const index = current < 0 ? (delta > 0 ? 0 : rows.length - 1) : Math.max(0, Math.min(rows.length - 1, current + delta));
  const target = rows[index];
  if (!target || (target === rows[current] && target.classList.contains('active'))) return;
  for (const row of rows) row.tabIndex = row === target ? 0 : -1;
  target.focus();
  target.scrollIntoView({block: 'nearest'});
  target.click();
}

function handleAgentShortcut(event) {
  if (!state.authenticated || state.activeModal || event.ctrlKey || event.metaKey || event.altKey || event.isComposing) return false;
  if (isTypingTarget(event.target)) return false;
  const key = event.key;
  if (key === '?') { openShortcutsHelp(); return true; }
  if (key === '/') {
    if ($('queueSearch').offsetParent === null) return false;
    $('queueSearch').focus();
    $('queueSearch').select();
    return true;
  }
  if (event.shiftKey) return false;
  const lower = key.toLowerCase();
  if (lower === 'j' || lower === 'k') {
    if (state.view === 'contacts') return false;
    if (window.innerWidth <= 800 && document.body.classList.contains('mobile-ticket-open')) return false;
    moveQueueSelection(lower === 'j' ? 1 : -1);
    return true;
  }
  if (lower === 'r' && state.composerEnabled && !state.busy && !$('message').disabled) {
    const textarea = $('message');
    textarea.focus({preventScroll: true});
    textarea.setSelectionRange(textarea.value.length, textarea.value.length);
    return true;
  }
  return false;
}

function handleGlobalKeydown(event) {
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'k' && state.authenticated && !state.activeModal) {
    event.preventDefault();
    $('queueSearch').focus();
    return;
  }
  if (event.key !== 'Escape' && event.key !== 'Tab' && handleAgentShortcut(event)) {
    event.preventDefault();
    return;
  }
  if (event.key === 'Escape') {
    if (state.activeModal === 'confirmModal') {
      event.preventDefault();
      settleConfirmDialog(false);
      return;
    }
    if (state.activeModal === 'shortcutsModal') {
      event.preventDefault();
      closeShortcutsHelp();
      return;
    }
    if (state.contextOpen) {
      event.preventDefault();
      closeContextPanel();
      return;
    }
    if (state.activeModal === 'startChatModal') {
      event.preventDefault();
      closeStartChatModal();
      return;
    }
    if (state.activeModal === 'forwardModal') {
      event.preventDefault();
      closeForwardModal();
      return;
    }
    if (state.activeModal === 'transferModal') {
      event.preventDefault();
      closeTransferModal();
      return;
    }
    if (state.activeModal === 'messagePreferencesModal') {
      event.preventDefault();
      closeMessagePreferencesModal();
      return;
    }
    if (state.activeModal === 'aiSettingsModal') {
      event.preventDefault();
      closeAiSettingsModal();
      return;
    }
    if (state.activeModal === 'whatsappModal') {
      event.preventDefault();
      closeWhatsAppModal();
      return;
    }
    if (state.activeModal === 'locationModal') {
      event.preventDefault();
      closeLocationModal();
      return;
    }
    if (state.activeModal === 'mediaViewerModal') {
      event.preventDefault();
      closeMediaViewer();
      return;
    }
    if (state.activeModal === 'messageDeleteModal') {
      event.preventDefault();
      closeMessageDelete();
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
    if (!state.pageLeaving) showLogin();
    const error = new Error('Your staff session expired. Please sign in again.');
    error.status = 401;
    throw error;
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

function canManageAiSettings() {
  return Boolean(
    state.agent &&
    (state.agent.can_manage_ai || state.agent.can_manage_staff || ['supervisor', 'admin'].includes(state.agent.role))
  );
}

function canManageGoogleContacts() {
  return Boolean(state.agent && ['supervisor', 'admin'].includes(state.agent.role));
}

function canManageWhatsApp() {
  return Boolean(
    state.agent &&
    (state.agent.can_manage_whatsapp || state.agent.can_manage_staff || ['supervisor', 'admin'].includes(state.agent.role))
  );
}

function configureStaffAccess() {
  $('manageStaffBtn').classList.toggle('hidden', !canManageStaff());
  $('manageWhatsAppBtn').classList.toggle('hidden', !canManageWhatsApp());
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
  cancelMessageEdit(false);
  state.ticketId = null;
  state.currentTicketData = null;
  state.currentConversationOrigin = null;
  state.previousTicketHistory = [];
  state.previousTicketHistoryHasMore = false;
  state.previousHistoryExpanded = false;
  state.previousHistoryLoaded = false;
  state.previousHistoryPending = false;
  state.historyJumpActive = false;
  state.historyJumpReachedTop = false;
  updateSharedHistoryShortcut();
  $('scrollToLatestBtn').classList.add('hidden');
  state.cachedCurrentMessages = [];
  state.currentMessagesHasMore = false;
  state.currentMessagesExpanded = false;
  state.historyLoadBusy = false;
  state.historyLoadRequest += 1;
  state.historySelectionEpoch += 1;
  state.timelineSignature = '';
  state.pendingConversationRender = null;
  state.composerEnabled = false;
  closeConversationSearch(false);
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
  clearReplyTarget();
  text($('ticketAvatar'), '—');
  showError('');
  updateQueueSummary();
  setComposerEnabled(false);
  closeContextPanel(false);
  setMobileTicketOpen(false);
  saveSupportSelection();
}

function showLogin() {
  $('sessionLoading').classList.add('hidden');
  if (state.imageEditor) { state.imageEditor.dispose(); state.imageEditor = null; }
  $('mediaViewerModal').classList.add('hidden');
  $('imageEditorToolbar').classList.add('hidden');
  $('messageDeleteModal').classList.add('hidden');
  state.deleteTarget = null;
  clearSupportSelections();
  state.authenticated = false;
  disconnectSupportEvents();
  saveLinkedDeviceAddition(null);
  state.agent = null;
  state.staffItems = [];
  state.editingStaffId = null;
  state.composerEnabled = false;
  state.queueRequest += 1;
  state.ticketRequest += 1;
  if (state.mediaRecorder) cancelVoiceRecording();
  clearMediaDraft();
  clearTicketWorkspace();$('dashboard').classList.add('hidden');
  state.page = 1;
  state.query = '';
  state.globalSearchResults = [];
  state.globalSearchRequest += 1;
  $('queueSearch').value = '';
  setQueueView('unassigned', {load: false, persist: false});
  $('loginPage').classList.remove('hidden');
  closeDialog('messagePreferencesModal', false);
  closeDialog('aiSettingsModal', false);
  closeDialog('whatsappModal', false);
  closeDialog('staffModal', false);
  closeDialog('transferModal', false);
  closeDialog('startChatModal', false);
  closeDialog('forwardModal', false);
  closeDialog('shortcutsModal', false);
  if (typeof settleConfirmDialog === 'function') settleConfirmDialog(false);
  text($('aiHeaderStatus'), '—');
  $('aiHeaderDot').className = 'ai-status-dot';
  $('aiHeaderBtn').setAttribute('title', 'AI assistant status');
  $('manageStaffBtn').classList.add('hidden');
  $('manageWhatsAppBtn').classList.add('hidden');
  state.aiSettings = null;
  state.whatsappRegistry = null;
  state.linkedDeviceRequest++;
  state.linkedDeviceStatus = null;
  state.linkedDeviceAdditionId = null;
  $('linkedDeviceLines').replaceChildren();
  $('linkedDeviceSection').classList.add('hidden');
  state.unassignedSnapshot = null;
  state.lastInboundByTicket = {};
  $('password').value = '';
  $('password').type = 'password';
  $('passwordToggleBtn').setAttribute('aria-pressed', 'false');
  $('passwordToggleBtn').setAttribute('aria-label', 'Show password');
  $('passwordToggleBtn').setAttribute('data-tooltip', 'Show password');
  showLoginError('');
  window.setTimeout(() => $('email').focus(), 20);
}

function showDashboard() {
  state.authenticated = true;
  $('sessionLoading').classList.add('hidden');
  $('loginPage').classList.add('hidden');
  $('dashboard').classList.remove('hidden');
  configureStaffAccess();
  connectSupportEvents();
}

async function restoreSupportWorkspace() {
  const saved = readSupportSelection();
  if (saved) {
    setQueueView(saved.view, {load: false, persist: false});
    state.page = saved.page;
  }
  await loadQueue();
  if (saved?.ticketId) {
    await openTicket(saved.ticketId, {
      silent: true, conversationOrigin: saved.conversationOrigin, restoring: true
    });
  } else saveSupportSelection();
}

async function login(event) {
  event.preventDefault();
  unlockNotificationAudio();
  const form = $('loginForm');
  if (state.busy || !form.reportValidity()) return;
  state.busy = true;
  setButtonBusy($('loginBtn'), true, 'Signing in…');
  showLoginError('');
  try {
    state.agent = await api('/api/support/login', {method:'POST', body: JSON.stringify({email: $('email').value.trim(), password: $('password').value})});
    setAgentIdentity(state.agent);
    showDashboard();
    try { await loadAiSettings(); } catch (error) { renderAiSettingsUnavailable(error.message); }
    await restoreSupportWorkspace();
    await refreshAllQueueCounts();
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
  text($('sessionLoadingStatus'), 'Checking your session…');
  $('sessionRetryBtn').classList.add('hidden');
  try {
    state.agent = await api('/api/support/me');
  } catch (error) {
    if (state.pageLeaving || error.status === 401) return;
    text($('sessionLoadingStatus'), 'Unable to check your session. Check your connection and try again.');
    $('sessionRetryBtn').classList.remove('hidden');
    return;
  }
  setAgentIdentity(state.agent);
  showDashboard();
  try {
    try { await loadAiSettings(); } catch (error) { renderAiSettingsUnavailable(error.message); }
    await restoreSupportWorkspace();
    await refreshAllQueueCounts();
  } catch (error) {
    if (state.authenticated && !state.pageLeaving) showError(error.message);
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
  if (badge) {
    badge.textContent = String(state.queueTotals[view]);
    badge.toggleAttribute('data-zero', state.queueTotals[view] === 0);
  }
}

async function refreshAllQueueCounts() {
  if (!state.authenticated) return;
  if (state.queueCountsBusy) { state.queueCountsPending = true; return; }
  state.queueCountsBusy = true;
  try {
    const data = await api('/api/support/queue-counts');
    for (const [view, total] of Object.entries(data.counts || {})) updateQueueCount(view, total);
  } catch (_) {
    // Individual queue loading remains authoritative; count refresh is best-effort.
  } finally {
    state.queueCountsBusy = false;
    if (state.queueCountsPending) {
      state.queueCountsPending = false;
      window.setTimeout(() => refreshAllQueueCounts(), 100);
    }
  }
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
    const noun = state.view === 'chats' ? 'chat' : state.view === 'contacts' ? 'contact' : 'ticket';
    text($('queueTotal'), total + ' ' + noun + (total === 1 ? '' : 's'));
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
    ticket.display_name,
    ticket.business_display_phone_number,
    ticket.business_phone_number_id
  ].filter(Boolean).join(' ').toLocaleLowerCase();
}

function supportNumberAlias(item) {
  if (!item) return '';
  const direct = String(item.display_name || '').trim();
  if (direct) return direct;
  const id = item.business_phone_number_id || item.phone_number_id || item.line_id;
  const registered = (state.whatsappRegistry?.numbers || []).find(number => String(number.phone_number_id) === String(id));
  return String(registered?.display_name || '').trim();
}

function supportNumberRaw(item) {
  if (item && (item.business_display_phone_number || item.display_phone_number)) {
    return String(item.business_display_phone_number || item.display_phone_number);
  }
  const id = item?.business_phone_number_id || item?.phone_number_id || item?.line_id;
  if (id) return 'WhatsApp number ID ' + id;
  return 'Default WhatsApp number';
}

function supportNumberLabel(item) {
  return supportNumberAlias(item) || supportNumberRaw(item);
}

function createSupportNumberLabel(item, {focusable = true} = {}) {
  const alias = supportNumberAlias(item);
  const raw = supportNumberRaw(item);
  const label = document.createElement('span');
  label.className = 'support-number-label' + (alias && alias !== raw ? ' has-alias' : '');
  label.title = raw;
  if (alias && alias !== raw) {
    const name = document.createElement('span');
    name.className = 'support-number-alias';
    name.textContent = alias;
    const number = document.createElement('span');
    number.className = 'support-number-raw';
    number.textContent = raw;
    label.append(name, number);
    label.setAttribute('aria-label', alias + ', WhatsApp number ' + raw);
    if (focusable) {
      label.tabIndex = 0;
      label.onpointerdown = event => { if (event.pointerType === 'touch') label.focus(); };
    }
  } else {
    label.textContent = raw;
  }
  return label;
}

function isStaffChat(ticket) {
  return String(ticket?.conversation_origin || '').toUpperCase() === 'STAFF';
}

function conversationStatusBadge(ticket) {
  if (!isStaffChat(ticket)) return statusBadge(ticket?.status);
  const badge = document.createElement('span');
  const open = Boolean(ticket?.active_key);
  badge.className = 'status-badge ' + (open ? 'status-assigned' : 'status-resolved');
  badge.textContent = open ? 'Open' : 'Closed';
  return badge;
}

function createMetaChip(label, iconName, className = 'meta-chip') {
  const chip = document.createElement('span');
  chip.className = className;
  if (iconName) chip.append(createIcon(iconName, 'icon icon-sm'));
  const copy = document.createElement('span');
  if (typeof label === 'string') copy.textContent = label;
  else copy.append(label);
  chip.append(copy);
  chip.title = typeof label === 'string' ? label : label.title;
  return chip;
}

function createContactRow(contact) {
  const row = document.createElement('button');
  row.type = 'button';
  row.className = 'contact-row';
  row.setAttribute('role', 'option');
  const avatar = document.createElement('div');
  avatar.className = 'avatar customer';
  avatar.setAttribute('aria-hidden', 'true');
  avatar.textContent = initials(contact.display_name || contact.normalized_phone || 'Contact');
  const copy = document.createElement('div');
  copy.className = 'contact-row-copy';
  const name = document.createElement('div');
  name.className = 'contact-row-name';
  name.dir = 'auto';
  name.textContent = contact.display_name || contact.normalized_phone || 'Unnamed contact';
  const phone = document.createElement('div');
  phone.className = 'contact-row-phone';
  phone.textContent = '+' + String(contact.normalized_phone || '').replace(/^\+/, '');
  const meta = document.createElement('div');
  meta.className = 'contact-row-meta';
  const source = document.createElement('span');
  source.className = 'contact-source' + (contact.source === 'google' ? ' google' : contact.source === 'vcf' ? ' vcf' : '');
  source.textContent = contact.source === 'google' ? 'Google' : contact.source === 'vcf' ? 'VCF' : 'Saved';
  meta.append(source);
  if (contact.email) meta.append(document.createTextNode(contact.email));
  copy.append(name, phone, meta);
  const chat = document.createElement('span');
  chat.className = 'contact-chat-chip';
  chat.setAttribute('aria-hidden', 'true');
  chat.append(createIcon('message', 'icon icon-sm'), document.createTextNode('Chat'));
  row.setAttribute('aria-label', 'Start chat with ' + name.textContent);
  row.tabIndex = -1;
  row.append(avatar, copy, chat);
  row.onclick = () => openStartChatModal(contact);
  return row;
}

function createTicketRow(ticket) {
  const selected = String(state.ticketId || '') === String(ticket.id || '');
  const row = document.createElement('button');
  row.type = 'button';
  row.className = 'ticket-row' + (selected ? ' active' : '');
  row.setAttribute('role', 'option');
  row.setAttribute('aria-selected', selected ? 'true' : 'false');
  row.tabIndex = selected ? 0 : -1;
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
  reference.textContent = isStaffChat(ticket) ? ('Chat · ' + (ticket.whatsapp_phone || ticket.id)) : (ticket.ticket_reference || ('Ticket #' + ticket.id));
  copy.append(name, reference);

  const time = document.createElement('time');
  time.className = 'ticket-time';
  time.dateTime = ticket.last_activity_at || '';
  time.title = fmt(ticket.last_activity_at);
  time.textContent = relativeTime(ticket.last_activity_at);
  const waitedMinutes = (Date.now() - new Date(ticket.last_activity_at || '').getTime()) / 60000;
  if (WAITING_AGE_VIEWS.includes(state.view) && Number.isFinite(waitedMinutes) && waitedMinutes >= WAITING_AGE_WARN_MINUTES) {
    const overdue = waitedMinutes >= WAITING_AGE_ALERT_MINUTES;
    time.classList.add(overdue ? 'age-alert' : 'age-warn');
    if (overdue) time.prepend(createIcon('alert', 'icon icon-sm'));
    time.title = 'Waiting ' + Math.floor(waitedMinutes) + ' min · last activity ' + fmt(ticket.last_activity_at);
    time.setAttribute('aria-label', 'Waiting ' + Math.floor(waitedMinutes) + ' minutes');
  }
  head.append(avatar, copy, time);

  const summary = document.createElement('div');
  summary.className = 'ticket-summary';
  summary.dir = 'auto';
  summary.textContent = isStaffChat(ticket) ? ('WhatsApp conversation' + (ticket.current_assignee_name ? ' · ' + ticket.current_assignee_name : '')) : (ticket.ai_summary || ticket.reason || ticket.requested_action || 'No summary available.');

  const meta = document.createElement('div');
  meta.className = 'ticket-row-meta';
  meta.append(conversationStatusBadge(ticket));
  meta.append(createMetaChip(createSupportNumberLabel(ticket, {focusable: false}), 'message', 'channel-pill'));
  if (ticket.current_assignee_name) meta.append(createMetaChip(ticket.current_assignee_name, 'user'));
  if (ticket.tracking_reference) meta.append(createMetaChip(ticket.tracking_reference, 'truck'));

  row.append(head, summary, meta);
  row.onclick = () => openTicket(ticket.id, {conversationOrigin: ticket.conversation_origin});
  return row;
}

function createSearchResultRow(item) {
  const row = document.createElement('button');
  row.type = 'button';
  row.className = 'search-result-row';
  row.setAttribute('role', 'option');
  row.setAttribute('aria-selected', 'false');
  row.tabIndex = -1;
  const head = document.createElement('div');
  head.className = 'search-result-head';
  const customer = document.createElement('div');
  customer.className = 'search-result-customer';
  customer.dir = 'auto';
  customer.textContent = item.customer_name || item.customer_userid || item.whatsapp_phone || 'Unknown customer';
  const time = document.createElement('time');
  time.className = 'search-result-time';
  time.dateTime = item.created_at || '';
  time.textContent = item.created_at ? relativeTime(item.created_at) : '';
  head.append(customer, time);
  const ref = document.createElement('div');
  ref.className = 'search-result-ref';
  ref.textContent = (item.conversation_origin === 'STAFF' ? 'Chat · ' : 'Ticket · ') + (item.ticket_reference || ('#' + item.ticket_id));
  const preview = document.createElement('div');
  preview.className = 'search-result-preview';
  preview.dir = 'auto';
  preview.textContent = item.preview || item.message_type || 'Message';
  highlightSearchTerm(preview, state.query);
  row.append(head, ref, preview);
  row.onclick = () => openSearchResult(item, state.query);
  return row;
}

async function runGlobalMessageSearch() {
  const query = state.query.trim();
  const requestId = ++state.globalSearchRequest;
  if (query.length < 2) {
    state.globalSearchResults = [];
    state.globalSearchBusy = false;
    renderQueue();
    return;
  }
  state.globalSearchBusy = true;
  $('queue').setAttribute('aria-busy', 'true');
  text($('searchScope'), 'Searching accessible ticket and chat message history…');
  try {
    const data = await api('/api/support/search/messages?q=' + encodeURIComponent(query) + '&limit=80');
    if (requestId !== state.globalSearchRequest || query !== state.query.trim()) return;
    state.globalSearchResults = Array.isArray(data.items) ? data.items : [];
    renderQueue();
  } catch (error) {
    if (requestId !== state.globalSearchRequest) return;
    state.globalSearchResults = [];
    renderQueueError(error.message);
  } finally {
    if (requestId === state.globalSearchRequest) {
      state.globalSearchBusy = false;
      $('queue').setAttribute('aria-busy', 'false');
      text($('searchScope'), query.length >= 2 ? 'Searches message history you are allowed to access.' : 'Searches the loaded page by customer, conversation, shipment, status, or support line.');
    }
  }
}

function refreshMessageSearchAfterChange(messageId) {
  state.globalSearchResults = state.globalSearchResults.filter(item => Number(item.message_id) !== Number(messageId));
  state.conversationSearchResults = state.conversationSearchResults.filter(item => Number(item.message_id) !== Number(messageId));
  if (state.query.trim().length >= 2) runGlobalMessageSearch().catch(() => {});
  if (state.conversationSearchOpen && state.conversationSearchQuery.length >= 2) searchCurrentConversation({preserveQuery: true}).catch(() => {});
}

function scheduleGlobalMessageSearch() {
  if (state.globalSearchTimer) clearTimeout(state.globalSearchTimer);
  state.globalSearchTimer = setTimeout(runGlobalMessageSearch, 250);
}

async function openSearchResult(item, query) {
  if (!item || !item.ticket_id || !item.message_id) return;
  state.conversationSearchOpen = true;
  state.conversationSearchQuery = query || '';
  $('conversationSearchInput').value = state.conversationSearchQuery;
  $('conversationSearchToolbar').classList.remove('hidden');
  await openTicket(Number(item.ticket_id), {silent: false, aroundMessageId: Number(item.message_id), preserveSearch: true, conversationOrigin: item.conversation_origin});
  await searchCurrentConversation({focusMessageId: Number(item.message_id), preserveQuery: true});
}

// Only one queue row is a Tab stop; arrows, Home/End, and J/K move between rows.
function syncQueueRovingFocus(restoreFocus = false) {
  const rows = [...$('queue').querySelectorAll('.ticket-row, .contact-row, .search-result-row')];
  if (!rows.length) return;
  const current = rows.find(row => row.classList.contains('active')) || rows[0];
  for (const row of rows) row.tabIndex = row === current ? 0 : -1;
  if (restoreFocus && !$('queue').contains(document.activeElement)) current.focus({preventScroll: true});
}

function renderQueue() {
  const queue = $('queue');
  const queueHadFocus = queue.contains(document.activeElement);
  const rawQuery = state.query.trim();
  const query = rawQuery.toLocaleLowerCase();
  const globalSearch = state.view !== 'contacts' && rawQuery.length >= 2;
  const items = state.view === 'contacts' ? state.queueItems : (query && !globalSearch ? state.queueItems.filter(ticket => queueSearchText(ticket).includes(query)) : state.queueItems);
  queue.replaceChildren();

  if (state.view === 'contacts') {
    if (!state.queueItems.length) {
      const config = QUEUE_CONFIG.contacts;
      queue.append(emptyMarkup(config.emptyTitle, rawQuery ? 'No contact matches this search.' : config.emptyBody, config.icon).outer);
    } else {
      const fragment = document.createDocumentFragment();
      for (const contact of state.queueItems) fragment.append(createContactRow(contact));
      queue.append(fragment);
    }
    updateQueueSummary(state.queueItems.length);
    $('clearQueueSearchBtn').classList.toggle('hidden', !state.query);
    syncQueueRovingFocus(queueHadFocus);
    return;
  }

  if (globalSearch) {
    if (state.globalSearchBusy) {
      renderQueueSkeleton();
    } else if (!state.globalSearchResults.length) {
      queue.append(emptyMarkup('No message matches', 'No accessible ticket or chat contains this search text.', 'search').outer);
    } else {
      const fragment = document.createDocumentFragment();
      for (const item of state.globalSearchResults) fragment.append(createSearchResultRow(item));
      queue.append(fragment);
    }
    text($('queueTotal'), state.globalSearchResults.length + ' message match' + (state.globalSearchResults.length === 1 ? '' : 'es'));
    text($('pageSummary'), 'Message history search');
    $('previousPageBtn').disabled = true;
    $('nextPageBtn').disabled = true;
    $('clearQueueSearchBtn').classList.toggle('hidden', !state.query);
    syncQueueRovingFocus(queueHadFocus);
    return;
  }

  if (!state.queueItems.length) {
    const config = QUEUE_CONFIG[state.view] || QUEUE_CONFIG.unassigned;
    queue.append(emptyMarkup(config.emptyTitle, config.emptyBody, config.icon).outer);
  } else if (!items.length) {
    const empty = emptyMarkup(state.view === 'chats' ? 'No chats match this search' : 'No tickets match this search', state.view === 'chats' ? 'Try a customer name, phone number, owner, or support line.' : 'Try a customer name, ticket reference, shipment number, status, or support line.', 'search');
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
  syncQueueRovingFocus(queueHadFocus);
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
    const path = state.view === 'contacts'
      ? '/api/support/contacts?page=' + state.page + '&page_size=' + state.pageSize + '&q=' + encodeURIComponent(state.query.trim())
      : state.view === 'chats'
        ? '/api/support/chats?page=' + state.page + '&page_size=' + state.pageSize
        : '/api/support/tickets?view=' + encodeURIComponent(state.view) + '&page=' + state.page + '&page_size=' + state.pageSize;
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
    if (state.view === 'unassigned' && !(state.unassignedSnapshot instanceof Map)) {
      state.unassignedSnapshot = new Map(
        state.queueItems.map(item => [String(item.id), String(item.last_activity_at || '')])
      );
    }
    renderQueue();
    if (silent) queue.scrollTop = previousScrollTop;
  } catch (error) {
    if (requestId !== state.queueRequest) return;
    if (state.pageLeaving) return;
    renderQueueError(error.message);
    text($('queueTotal'), 'Unavailable');
  } finally {
    if (requestId === state.queueRequest) {
      queue.setAttribute('aria-busy', 'false');
      $('refreshQueueBtn').disabled = false;
    }
  }
}

async function pollUnassignedActivity() {
  if (!state.authenticated) return;
  try {
    const data = await api('/api/support/tickets?view=unassigned&page=1&page_size=30');
    const items = Array.isArray(data.items) ? data.items : [];
    updateQueueCount('unassigned', Number(data.total || 0));
    const snapshot = new Map(items.map(item => [String(item.id), String(item.last_activity_at || '')]));
    if (state.unassignedSnapshot instanceof Map) {
      let newCount = 0;
      let updatedCount = 0;
      for (const [id, activity] of snapshot.entries()) {
        if (!state.unassignedSnapshot.has(id)) newCount += 1;
        else if (state.unassignedSnapshot.get(id) !== activity) updatedCount += 1;
      }
      if (newCount || updatedCount) {
        const message = newCount
          ? (newCount === 1 ? 'New unassigned customer conversation.' : newCount + ' new unassigned customer conversations.')
          : 'New customer message in the Unassigned queue.';
        notifyNewCustomerActivity(message);
        if (state.view === 'unassigned' && state.page === 1) {
          state.queueItems = items;
          state.total = Number(data.total || 0);
          renderQueue();
        }
      }
    }
    state.unassignedSnapshot = snapshot;
  } catch (_) {
    // The normal queue error handling remains authoritative; notification polling is best-effort.
  }
}

function setQueueView(view, {load = true, persist = true} = {}) {
  if (!Object.prototype.hasOwnProperty.call(QUEUE_CONFIG, view) || state.view === view) return;
  state.view = view;
  state.page = 1;
  state.query = '';
  state.globalSearchResults = [];
  state.globalSearchRequest += 1;
  $('queueSearch').value = '';
  for (const button of $('tabs').querySelectorAll('button[data-view]')) {
    const active = button.dataset.view === view;
    button.classList.toggle('active', active);
    button.setAttribute('aria-current', active ? 'page' : 'false');
  }
  const contacts = view === 'contacts';
  $('contactsToolbar').classList.toggle('hidden', !contacts);
  $('queueSearch').placeholder = contacts ? 'Search contacts' : 'Search conversations and messages';
  text($('searchScope'), contacts ? 'Search saved, Google-synced, and VCF-imported contacts by name, phone, or email.' : 'Searches message history you are allowed to access. Customer/ticket filtering remains available for short searches.');
  if (contacts) loadGoogleContactsStatus().catch(() => {});
  updateQueueSummary();
  if (persist) saveSupportSelection();
  if (load) loadQueue();
}

function clearQueueSearch() {
  state.query = '';
  state.globalSearchResults = [];
  state.globalSearchRequest += 1;
  if (state.globalSearchTimer) { clearTimeout(state.globalSearchTimer); state.globalSearchTimer = null; }
  $('queueSearch').value = '';
  text($('searchScope'), state.view === 'contacts' ? 'Search saved, Google-synced, and VCF-imported contacts by name, phone, or email.' : 'Searches message history you are allowed to access. Customer/ticket filtering remains available for short searches.');
  if (state.view === 'contacts') loadQueue({silent: true});
  else renderQueue();
  $('queueSearch').focus();
}

function changePage(direction) {
  const nextPage = state.page + direction;
  const pages = Math.max(1, Math.ceil(state.total / state.pageSize));
  if (nextPage < 1 || nextPage > pages) return;
  state.page = nextPage;
  state.query = '';
  $('queueSearch').value = '';
  saveSupportSelection();
  loadQueue().then(() => $('queue').focus({preventScroll: true}));
}

function handleQueueKeydown(event) {
  const current = event.target.closest('.ticket-row, .contact-row, .search-result-row');
  if (!current || !['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) return;
  const rows = [...$('queue').querySelectorAll('.ticket-row, .contact-row, .search-result-row')];
  const index = rows.indexOf(current);
  let target = null;
  if (event.key === 'ArrowDown') target = rows[Math.min(rows.length - 1, index + 1)];
  if (event.key === 'ArrowUp') target = rows[Math.max(0, index - 1)];
  if (event.key === 'Home') target = rows[0];
  if (event.key === 'End') target = rows[rows.length - 1];
  if (target) {
    event.preventDefault();
    for (const row of rows) row.tabIndex = row === target ? 0 : -1;
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

function messagePreview(message) {
  if (message.deleted_for_everyone_at) return 'Original message deleted';
  const kind = String(message.media_kind || message.message_type || '').toLowerCase();
  if (kind === 'image') return 'Image';
  if (kind === 'sticker') return 'Sticker';
  if (kind === 'audio') return 'Voice note';
  if (kind === 'document') return message.media_filename || 'PDF document';
  if (kind === 'location') return message.location_name || message.location_address || 'Location';
  const body = String(message.body || message.content || '').trim();
  return body || 'Message';
}

function messageTextForCopy(message) {
  if (!message || message.deleted_for_everyone_at) return '';
  const body = String(message.body || message.content || '');
  if (!body.trim()) return '';
  const kind = String(message.media_kind || message.message_type || '').toLowerCase();
  const defaultMediaLabel = ['Image', 'Sticker', 'Voice note', 'Audio message', 'PDF document', 'Location'].includes(body);
  return (message.media_url || kind === 'location') && defaultMediaLabel ? '' : body;
}

async function copyMessageText(value) {
  if (!value) return;
  try {
    if (navigator.clipboard?.writeText) {
      try {
        await navigator.clipboard.writeText(value);
        notify('Message copied.');
        return;
      } catch (_) {
        // Some browsers deny the Clipboard API while still allowing a user-initiated copy.
      }
    }
    const field = document.createElement('textarea');
    field.value = value;
    field.readOnly = true;
    field.style.position = 'fixed';
    field.style.left = '-9999px';
    document.body.append(field);
    try {
      field.select();
      if (!document.execCommand?.('copy')) throw new Error('Copy failed');
    } finally {
      field.remove();
    }
    notify('Message copied.');
  } catch (_) {
    notify('Could not copy the message. Try selecting its text.', 'error');
  }
}

function renderReplyDraft() {
  const container = $('replyDraft');
  const target = state.replyTarget;
  if (!target) {
    container.classList.add('hidden');
    text($('replyDraftTitle'), 'Replying to message');
    text($('replyDraftBody'), '');
    return;
  }
  text($('replyDraftTitle'), 'Replying to ' + target.actor);
  text($('replyDraftBody'), target.preview);
  container.classList.remove('hidden');
}

function clearReplyTarget() {
  state.replyTarget = null;
  renderReplyDraft();
}

function startReplyToMessage(message) {
  if (!state.composerEnabled || !message || !message.id || !message.whatsapp_message_id) return;
  state.replyTarget = {
    id: Number(message.id),
    actor: messageActor(message),
    preview: messagePreview(message)
  };
  renderReplyDraft();
  $('message').focus();
  setAppStatus('Replying to ' + state.replyTarget.actor + '.');
}

function canChangeWhatsAppMessage(message, action) {
  if (!message || message.deleted_for_everyone_at || !message.id) return false;
  if (String(message.sender_type || '').toUpperCase() !== 'AGENT' || String(message.sender_id) !== String(state.agent?.id)) return false;
  return action === 'edit' ? message.can_edit === true : message.can_delete_for_everyone === true;
}

function messageTicketId(message) {
  return Number(message?.ticket_id || state.ticketId);
}

function startMessageEdit(message) {
  if (!canChangeWhatsAppMessage(message, 'edit')) return;
  if (state.busy || state.pendingMedia || state.mediaRecorder || state.editTarget) return;
  state.editTarget = {
    id: Number(message.id), ticketId: messageTicketId(message), version: Number(message.version || 1),
    draft: $('message').value, reply: state.replyTarget
  };
  clearReplyTarget();
  $('message').value = message.body || '';
  $('editDraft').classList.remove('hidden');
  updateComposerCount(); resizeComposer(); updateComposerControls(); focusMessageComposer();
}

function cancelMessageEdit(restore = true) {
  const target = state.editTarget;
  if (!target) return;
  state.editTarget = null;
  $('editDraft').classList.add('hidden');
  $('message').value = restore ? target.draft : '';
  state.replyTarget = restore ? target.reply : null;
  renderReplyDraft(); updateComposerCount(); resizeComposer(); updateComposerControls();
}

function openMessageDelete(message) {
  const canDeleteEveryone = canChangeWhatsAppMessage(message, 'everyone');
  state.deleteTarget = {id: Number(message.id), ticketId: messageTicketId(message), canDeleteEveryone};
  $('deleteEveryoneBtn').classList.toggle('hidden', !canDeleteEveryone);
  text($('messageDeleteSubtitle'), canDeleteEveryone
    ? 'Delete for everyone removes the message content from your support view and the customer’s WhatsApp chat. WhatsApp will show a deleted-message placeholder.'
    : 'Delete for everyone is unavailable for this message on WhatsApp. Delete for me removes it from your support view; the customer’s copy remains unchanged.');
  openDialog('messageDeleteModal', 'deleteMeBtn');
}

function closeMessageDelete() {
  state.deleteTarget = null;
  closeDialog('messageDeleteModal');
}

async function deleteMessage(scope) {
  const target = state.deleteTarget;
  if (!target || state.busy || !state.ticketId) return;
  if (scope === 'everyone' && !target.canDeleteEveryone) return;
  const visibleTicketId = state.ticketId;
  state.busy = true;
  $('deleteMeBtn').disabled = true; $('deleteEveryoneBtn').disabled = true;
  try {
    const result = await api('/api/support/tickets/' + target.ticketId + '/messages/' + target.id + '/delete', {
      method: 'POST', body: JSON.stringify({scope})
    });
    changeCachedMessage(target.ticketId, target.id, result.message, scope === 'me');
    state.realtimeMessages.delete(String(target.ticketId) + ':' + target.id);
    if (state.editTarget?.id === target.id) cancelMessageEdit();
    refreshMessageSearchAfterChange(target.id);
    closeMessageDelete();
    if (String(state.ticketId) === String(visibleTicketId)) {
      await openTicket(visibleTicketId, {silent: true, refreshHistory: target.ticketId !== Number(visibleTicketId)});
    }
    notify(scope === 'me' ? 'Message removed from your support history.' : 'Message deleted for everyone on WhatsApp.');
  } catch (error) {
    notify(error.message, 'error');
  } finally {
    state.busy = false;
    $('deleteMeBtn').disabled = false; $('deleteEveryoneBtn').disabled = false;
  }
}

async function sendMessageReaction(message, selectedEmoji, menu = null) {
  if (!state.composerEnabled || state.busy || !state.ticketId || !message?.id || !message.whatsapp_message_id) return;
  const ownReaction = (Array.isArray(message.reactions) ? message.reactions : []).find(item => String(item.reactor_type || '').toUpperCase() === 'AGENT');
  const emoji = ownReaction && ownReaction.emoji === selectedEmoji ? '' : selectedEmoji;
  if (menu) menu.open = false;
  state.busy = true;
  setWorkspaceBusy(true);
  showError('');
  let succeeded = false;
  try {
    await api('/api/support/tickets/' + state.ticketId + '/messages/' + message.id + '/reaction', {
      method: 'POST',
      body: JSON.stringify({emoji, client_reaction_id: crypto.randomUUID()})
    });
    notify(emoji ? 'Reaction sent.' : 'Reaction removed.');
    succeeded = true;
    await openTicket(state.ticketId, {silent: true, refreshQueue: true});
  } catch (error) {
    showError(error.message);
    notify(error.message, 'error');
  } finally {
    state.busy = false;
    setWorkspaceBusy(false);
  }
  if (succeeded) focusMessageComposer();
}

function messageReactionBar(message) {
  const reactions = Array.isArray(message.reactions) ? message.reactions.filter(item => item && item.emoji) : [];
  if (!reactions.length) return null;
  const grouped = new Map();
  for (const reaction of reactions) {
    const emoji = String(reaction.emoji || '');
    if (!grouped.has(emoji)) grouped.set(emoji, []);
    grouped.get(emoji).push(reaction);
  }
  const bar = document.createElement('div');
  bar.className = 'message-reactions';
  bar.setAttribute('aria-label', 'Message reactions');
  for (const [emoji, items] of grouped.entries()) {
    const chip = document.createElement('div');
    chip.className = 'reaction-chip';
    const names = items.map(item => item.reactor_name || (String(item.reactor_type || '').toUpperCase() === 'CUSTOMER' ? 'Customer' : 'Support agent'));
    chip.title = names.join(', ');
    chip.append(document.createTextNode(emoji));
    if (items.length > 1) {
      const count = document.createElement('span');
      count.textContent = String(items.length);
      chip.append(count);
    }
    bar.append(chip);
  }
  return bar;
}

function renderReplyQuote(parent, reply) {
  if (!reply) return;
  const quote = document.createElement('div');
  quote.className = 'message-quote';
  const actor = document.createElement('strong');
  actor.textContent = reply.sender_name || MESSAGE_ROLE_LABELS[String(reply.sender_type || '').toUpperCase()] || 'Message';
  const preview = document.createElement('span');
  preview.dir = 'auto';
  preview.textContent = messagePreview(reply);
  quote.append(actor, preview);
  parent.append(quote);
}

function applyMediaZoom() {
  const surface = state.imageEditor?.canvas || $('mediaViewerBody').querySelector('img');
  if (!surface) return;
  const body = $('mediaViewerBody');
  const width = surface.naturalWidth || surface.width;
  const height = surface.naturalHeight || surface.height;
  const fit = Math.min((body.clientWidth - 28) / width, (body.clientHeight - 28) / height, 1);
  if (!Number.isFinite(fit) || fit <= 0) return;
  surface.style.width = Math.round(width * fit) + 'px';
  surface.style.height = Math.round(height * fit) + 'px';
  surface.style.left = '50%'; surface.style.top = '50%';
  surface.style.transform = 'translate(-50%, -50%) translate(' + state.mediaTranslateX + 'px, ' + state.mediaTranslateY + 'px) scale(' + state.mediaZoom + ')';
  body.classList.toggle('can-pan', state.mediaZoom > 1);
  text($('mediaViewerZoom'), Math.round(state.mediaZoom * 100) + '%');
}

function zoomMediaAt(next, clientX, clientY) {
  const body = $('mediaViewerBody');
  const rect = body.getBoundingClientRect();
  const x = clientX - rect.left - rect.width / 2;
  const y = clientY - rect.top - rect.height / 2;
  const old = state.mediaZoom;
  const zoom = Math.max(1, Math.min(8, next));
  state.mediaTranslateX = x - (x - state.mediaTranslateX) * zoom / old;
  state.mediaTranslateY = y - (y - state.mediaTranslateY) * zoom / old;
  state.mediaZoom = zoom;
  if (zoom === 1) { state.mediaTranslateX = 0; state.mediaTranslateY = 0; }
  applyMediaZoom();
}

function changeMediaZoom(delta) {
  const rect = $('mediaViewerBody').getBoundingClientRect();
  zoomMediaAt(state.mediaZoom + delta, rect.left + rect.width / 2, rect.top + rect.height / 2);
}

function resetMediaZoom() {
  state.mediaZoom = 1; state.mediaTranslateX = 0; state.mediaTranslateY = 0;
  applyMediaZoom();
}

function openMediaViewer({kind, src, title, subtitle}) {
  if (!src) return;
  text($('mediaViewerTitle'), title || (kind === 'document' ? 'PDF document' : 'Image'));
  text($('mediaViewerSubtitle'), subtitle || 'WhatsApp attachment');
  const body = $('mediaViewerBody');
  body.replaceChildren();
  body.classList.toggle('image-mode', kind === 'image');
  state.mediaSource = kind === 'image' ? src : null;
  resetMediaZoom();
  $('mediaViewerTools').classList.toggle('hidden', kind !== 'image');
  $('editMediaBtn').classList.toggle('hidden', kind !== 'image' || !state.composerEnabled);
  $('downloadMediaBtn').classList.toggle('hidden', kind !== 'image');
  if (kind === 'image') $('downloadMediaBtn').href = src;
  if (kind === 'document') {
    const frame = document.createElement('iframe');
    frame.src = src;
    frame.title = title || 'PDF document';
    body.append(frame);
  } else {
    const img = document.createElement('img');
    img.src = src;
    img.alt = title || 'Image attachment';
    img.onload = applyMediaZoom;
    img.onerror = () => notify('Image could not be loaded.', 'error');
    body.append(img);
  }
  text($('mediaViewerZoom'), '100%');
  openDialog('mediaViewerModal', 'closeMediaViewerBtn');
}

async function closeMediaViewer() {
  if (state.imageEditor?.sending) return;
  if (state.imageEditor?.dirty && !(await confirmDialog({title: 'Discard image edits?', message: 'Discard your changes?', confirmLabel: 'Discard changes', cancelLabel: 'Keep editing', tone: 'danger'}))) return;
  if (state.imageEditor) { state.imageEditor.dispose(); state.imageEditor = null; }
  $('imageEditorToolbar').classList.add('hidden');
  $('mediaViewerBody').classList.remove('editing', 'image-mode', 'can-pan', 'panning');
  mediaPointers.clear();
  state.mediaSource = null;
  resetMediaZoom();
  $('mediaViewerTools').classList.add('hidden');
  $('mediaViewerBody').replaceChildren();
  closeDialog('mediaViewerModal');
}

async function enterImageEditor() {
  if (!state.mediaSource || !state.composerEnabled || state.imageEditor) return;
  if (!window.ChatImageEditor) { notify('Image editor is unavailable. Reload and try again.', 'error'); return; }
  const source = new URL(state.mediaSource, location.href);
  if (source.origin !== location.origin || !source.pathname.startsWith('/api/support/tickets/')) return;
  $('editMediaBtn').disabled = true;
  let bitmap = null;
  try {
    const response = await fetch(source.href, {credentials: 'same-origin', cache: 'no-store'});
    if (!response.ok) throw new Error('Image could not be loaded for editing.');
    const blob = await response.blob();
    if (!['image/jpeg', 'image/png', 'image/webp'].includes(blob.type)) throw new Error('This image format cannot be edited.');
    bitmap = await createImageBitmap(blob, {imageOrientation: 'from-image'});
    const editor = new window.ChatImageEditor(bitmap, blob.type);
    const body = $('mediaViewerBody');
    body.replaceChildren(); body.classList.remove('can-pan', 'panning'); body.classList.add('editing');
    state.mediaZoom = 1; state.mediaTranslateX = 0; state.mediaTranslateY = 0;
    $('editMediaBtn').classList.add('hidden');
    $('downloadMediaBtn').classList.add('hidden');
    $('imageEditorToolbar').classList.remove('hidden');
    $('editorCaption').value = '';
    $('editorSelectBtn').setAttribute('aria-pressed', 'false');
    $('editorDrawBtn').setAttribute('aria-pressed', 'true');
    $('editorTextBtn').setAttribute('aria-pressed', 'false');
    state.imageEditor = editor;
    body.append(editor.canvas);
    editor.resize();
    applyMediaZoom();
    editor.canvas.focus();
  } catch (error) {
    if (bitmap && !state.imageEditor) bitmap.close?.();
    notify(error.message || 'Image could not be edited.', 'error');
  }
  finally { $('editMediaBtn').disabled = false; }
}

async function sendEditedImage() {
  const editor = state.imageEditor;
  if (!editor || editor.sending || !state.ticketId) return;
  editor.sending = true;
  setButtonBusy($('editorSendBtn'), true, 'Sending…');
  try {
    const sourceCheck = await fetch(state.mediaSource, {credentials: 'same-origin', cache: 'no-store'});
    await sourceCheck.body?.cancel();
    if (!sourceCheck.ok) throw new Error('The original image is no longer available. Your edits are still here.');
    const blob = await editor.blob();
    const mime = blob.type;
    const form = new FormData();
    form.append('media_type', 'image');
    form.append('client_message_id', editor.sendId);
    form.append('caption', $('editorCaption').value.trim());
    form.append('file', new File([blob], mime === 'image/png' ? 'edited-image.png' : 'edited-image.jpg', {type: mime}));
    await api('/api/support/tickets/' + state.ticketId + '/media', {method: 'POST', body: form});
    editor.dispose(); state.imageEditor = null;
    $('imageEditorToolbar').classList.add('hidden');
    $('mediaViewerBody').classList.remove('editing');
    closeMediaViewer();
    await openTicket(state.ticketId, {silent: true, refreshQueue: true});
    notify('Edited image sent. The original remains unchanged.');
  } catch (error) {
    if (error.status === 502) editor.sendId = crypto.randomUUID();
    notify(error.message || 'Image upload failed. Your edits are still here.', 'error');
  }
  finally { editor.sending = false; setButtonBusy($('editorSendBtn'), false); }
}

function appendMessageTextWithLinks(container, value) {
  const source = String(value ?? '');
  const pattern = /(?:https?:\/\/|www\.)[^\s<>"']+/gi;
  let cursor = 0;

  for (const match of source.matchAll(pattern)) {
    const start = match.index ?? 0;
    const raw = match[0];
    if (start > cursor) container.append(document.createTextNode(source.slice(cursor, start)));

    let linkText = raw;
    let trailing = '';
    while (/[.,!?;:]$/.test(linkText)) {
      trailing = linkText.slice(-1) + trailing;
      linkText = linkText.slice(0, -1);
    }
    for (const [closing, opening] of [[')', '('], [']', '['], ['}', '{']]) {
      while (linkText.endsWith(closing)) {
        const closes = linkText.split(closing).length - 1;
        const opens = linkText.split(opening).length - 1;
        if (closes <= opens) break;
        trailing = closing + trailing;
        linkText = linkText.slice(0, -1);
      }
    }

    const href = /^www\./i.test(linkText) ? 'https://' + linkText : linkText;
    let safeHref = '';
    try {
      const parsed = new URL(href);
      if (!['http:', 'https:'].includes(parsed.protocol)) throw new Error('Unsupported URL protocol');
      safeHref = parsed.href;
    } catch (_) {
      container.append(document.createTextNode(raw));
      cursor = start + raw.length;
      continue;
    }

    const link = document.createElement('a');
    link.href = safeHref;
    link.target = '_blank';
    link.rel = 'noopener noreferrer';
    link.textContent = linkText;
    link.title = 'Open link in a new tab';
    container.append(link);
    if (trailing) container.append(document.createTextNode(trailing));
    cursor = start + raw.length;
  }

  if (cursor < source.length) container.append(document.createTextNode(source.slice(cursor)));
}

function highlightSearchTerm(root, term) {
  const query = String(term || '').trim();
  if (!root || !query) return;
  const lower = query.toLocaleLowerCase();
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  const nodes = [];
  while (walker.nextNode()) nodes.push(walker.currentNode);
  for (const node of nodes) {
    const value = node.nodeValue || '';
    const valueLower = value.toLocaleLowerCase();
    let start = 0;
    let index = valueLower.indexOf(lower, start);
    if (index < 0) continue;
    const fragment = document.createDocumentFragment();
    while (index >= 0) {
      if (index > start) fragment.append(document.createTextNode(value.slice(start, index)));
      const mark = document.createElement('mark');
      mark.className = 'message-search-highlight';
      mark.textContent = value.slice(index, index + query.length);
      fragment.append(mark);
      start = index + query.length;
      index = valueLower.indexOf(lower, start);
    }
    if (start < value.length) fragment.append(document.createTextNode(value.slice(start)));
    node.replaceWith(fragment);
  }
}

function createVoiceNotePlayer(src, label) {
  const player = document.createElement('div');
  player.className = 'voice-note-player';
  const audio = document.createElement('audio');
  audio.controls = true;
  audio.preload = 'metadata';
  audio.src = src;
  audio.dataset.mediaUrl = src;
  audio.setAttribute('aria-label', label);
  const speed = document.createElement('button');
  speed.type = 'button';
  speed.className = 'voice-speed-btn';
  const rates = [1, 1.5, 2];
  let rateIndex = 0;
  const updateSpeed = () => {
    speed.textContent = rates[rateIndex] + 'x';
    speed.setAttribute('aria-label', 'Playback speed ' + rates[rateIndex] + 'x. Click to change speed.');
    speed.title = 'Playback speed ' + rates[rateIndex] + 'x';
  };
  speed.onclick = () => {
    rateIndex = (rateIndex + 1) % rates.length;
    audio.playbackRate = rates[rateIndex];
    updateSpeed();
  };
  updateSpeed();
  player.append(audio, speed);
  return {player, audio};
}

function addMessage(container, m, extraClass = '') {
  const outbound = m.direction === 'OUTBOUND';
  const deleted = Boolean(m.deleted_for_everyone_at);
  const row = document.createElement('article');
  row.className = 'message-row ' + (outbound ? 'outbound ' : 'inbound ') + messageClass(m) + (extraClass ? ' ' + extraClass : '');
  if (m.id) row.dataset.messageId = String(m.id);

  const avatar = document.createElement('div');
  avatar.className = 'avatar message-avatar ' + (messageClass(m) === 'customer' ? 'customer' : messageClass(m) === 'ai' ? 'ai' : '');
  avatar.setAttribute('aria-hidden', 'true');
  avatar.textContent = initials(messageActor(m));

  const stack = document.createElement('div');
  stack.className = 'message-stack';
  const bubble = document.createElement('div');
  bubble.className = 'message-bubble';

  if (m.is_forwarded && !deleted) {
    const forwarded = document.createElement('div');
    forwarded.className = 'forwarded-label';
    forwarded.append(createIcon('transfer', 'icon icon-sm'), document.createTextNode('Forwarded'));
    bubble.append(forwarded);
  }

  const sender = document.createElement('div');
  sender.className = 'message-sender';
  sender.append(document.createTextNode(messageActor(m)));
  bubble.append(sender);
  if (!deleted) renderReplyQuote(bubble, m.reply_to);

  const kind = String(m.media_kind || m.message_type || '').toLowerCase();
  if (deleted) {
    const tombstone = document.createElement('div');
    tombstone.className = 'message-body';
    tombstone.textContent = 'This message was deleted';
    bubble.append(tombstone);
  } else if (['image', 'audio', 'document', 'sticker'].includes(kind)) {
    const media = document.createElement('div');
    media.className = 'msg-media' + (kind === 'sticker' ? ' sticker' : '');
    if (m.media_url) {
      if (kind === 'image' || kind === 'sticker') {
        const open = document.createElement('button');
        open.type = 'button';
        open.className = 'media-open';
        const mediaLabel = kind === 'sticker' ? 'sticker' : 'image';
        open.setAttribute('aria-label', 'View ' + mediaLabel + ' from ' + messageActor(m));
        const img = document.createElement('img');
        img.src = m.media_url;
        img.alt = (kind === 'sticker' ? 'Sticker' : 'Image') + ' from ' + messageActor(m);
        img.loading = 'lazy';
        const recovery = document.createElement('div');
        recovery.className = 'media-unavailable hidden';
        const recoveryText = document.createElement('span');
        recoveryText.textContent = (kind === 'sticker' ? 'Sticker' : 'Image') + ' loading failed. The server can retry the WhatsApp media download.';
        const retry = document.createElement('button');
        retry.type = 'button';
        retry.className = 'btn small';
        retry.textContent = kind === 'sticker' ? 'Retry sticker' : 'Retry image';
        retry.onclick = event => {
          event.stopPropagation();
          recovery.classList.add('hidden');
          img.src = m.media_url + (m.media_url.includes('?') ? '&' : '?') + 'retry=' + Date.now();
        };
        recovery.append(recoveryText, retry);
        img.onerror = () => recovery.classList.remove('hidden');
        img.onload = () => recovery.classList.add('hidden');
        open.onclick = () => openMediaViewer({kind: 'image', src: m.media_url, title: (kind === 'sticker' ? 'Sticker' : 'Image') + ' from ' + messageActor(m), subtitle: fmt(messageOccurredAt(m))});
        open.append(img);
        media.append(open, recovery);
      } else if (kind === 'document') {
        const card = document.createElement('div');
        card.className = 'document-card';
        card.append(createIcon('file'));
        const copy = document.createElement('div');
        copy.className = 'document-card-copy';
        const name = document.createElement('strong');
        name.textContent = m.media_filename || 'PDF document';
        const description = document.createElement('span');
        description.textContent = 'PDF attachment';
        copy.append(name, description);
        const view = document.createElement('button');
        view.type = 'button';
        view.className = 'btn small';
        view.textContent = 'View PDF';
        view.onclick = () => openMediaViewer({kind: 'document', src: m.media_url, title: m.media_filename || 'PDF document', subtitle: 'WhatsApp document'});
        card.append(copy, view);
        media.append(card);
      } else {
        const {player, audio} = createVoiceNotePlayer(m.media_url, 'Voice note from ' + messageActor(m));
        const recovery = document.createElement('div');
        recovery.className = 'media-unavailable hidden';
        const recoveryText = document.createElement('span');
        recoveryText.textContent = 'Playback failed. Retry the WhatsApp media download.';
        const retry = document.createElement('button');
        retry.type = 'button';
        retry.className = 'btn small';
        retry.textContent = 'Retry voice note';
        retry.onclick = () => {
          recovery.classList.add('hidden');
          audio.src = m.media_url + (m.media_url.includes('?') ? '&' : '?') + 'retry=' + Date.now();
          audio.load();
        };
        recovery.append(recoveryText, retry);
        audio.onerror = () => recovery.classList.remove('hidden');
        audio.oncanplay = () => recovery.classList.add('hidden');
        media.append(player, recovery);
      }
    } else {
      const unavailable = document.createElement('div');
      unavailable.className = 'media-unavailable';
      unavailable.textContent = kind === 'image'
        ? 'Image is not available in support storage.'
        : kind === 'sticker'
          ? 'Sticker is not available in support storage.'
        : kind === 'document'
          ? 'PDF is not available in support storage.'
          : 'Voice note is not available in support storage.';
      media.append(unavailable);
    }
    bubble.append(media);
  } else if (kind === 'location') {
    const card = document.createElement('div');
    card.className = 'location-card';
    card.append(createIcon('map-pin'));
    const copy = document.createElement('div');
    copy.className = 'location-card-copy';
    const name = document.createElement('strong');
    name.dir = 'auto';
    name.textContent = m.location_name || 'Shared location';
    const detail = document.createElement('span');
    detail.dir = 'auto';
    const coordinates = [m.location_latitude, m.location_longitude].every(value => value !== null && value !== undefined)
      ? Number(m.location_latitude).toFixed(6) + ', ' + Number(m.location_longitude).toFixed(6)
      : '';
    detail.textContent = m.location_address || coordinates || 'Location shared on WhatsApp';
    copy.append(name, detail);
    card.append(copy);
    if (coordinates) {
      const map = document.createElement('a');
      map.className = 'btn small';
      map.href = 'https://www.google.com/maps?q=' + encodeURIComponent(String(m.location_latitude) + ',' + String(m.location_longitude));
      map.target = '_blank';
      map.rel = 'noopener noreferrer';
      map.textContent = 'Open map';
      card.append(map);
    }
    bubble.append(card);
  }

  const bodyText = messageTextForCopy(m);
  if (bodyText) {
    const body = document.createElement('div');
    body.className = 'message-body';
    body.dir = 'auto';
    appendMessageTextWithLinks(body, bodyText);
    if (state.conversationSearchOpen && state.conversationSearchQuery) highlightSearchTerm(body, state.conversationSearchQuery);
    bubble.append(body);
  }

  const meta = document.createElement('div');
  meta.className = 'message-meta';
  const time = document.createElement('time');
  time.dateTime = m.created_at || '';
  const occurredAt = messageOccurredAt(m);
  time.title = fmt(occurredAt);
  time.textContent = occurredAt ? fmt(occurredAt) : (extraClass ? 'Earlier context' : 'Time unavailable');
  meta.append(time);
  if (m.edited_at && !deleted) {
    const edited = document.createElement('span');
    edited.textContent = 'edited';
    edited.title = 'Edited ' + fmt(m.edited_at);
    meta.append(edited);
  }
  const actions = document.createElement('div');
  actions.className = 'message-actions';
  actions.setAttribute('role', 'group');
  actions.setAttribute('aria-label', 'Message actions');
  if (canChangeWhatsAppMessage(m, 'edit')) {
    const edit = document.createElement('button');
    edit.type = 'button'; edit.className = 'btn message-action-btn'; edit.textContent = 'Edit';
    labelMessageAction(edit, 'edit', 'Edit message');
    edit.onclick = () => startMessageEdit(m);
    actions.append(edit);
  }
  if (bodyText) {
    const copy = document.createElement('button');
    copy.type = 'button'; copy.className = 'btn message-action-btn'; copy.textContent = 'Copy';
    copy.setAttribute('aria-label', 'Copy message text');
    labelMessageAction(copy, 'copy', 'Copy text');
    copy.onclick = () => copyMessageText(bodyText);
    actions.append(copy);
  }
  if (m.id && (!extraClass || canChangeWhatsAppMessage(m, 'everyone'))) {
    const remove = document.createElement('button');
    remove.type = 'button'; remove.className = 'btn message-action-btn'; remove.textContent = 'Delete';
    labelMessageAction(remove, 'delete', 'Delete message');
    remove.onclick = () => openMessageDelete(m);
    actions.append(remove);
  }
  if (!extraClass && !m.id && m.client_message_id && ['FAILED', 'UNKNOWN'].includes(m.send_status)) {
    const dismiss = document.createElement('button');
    dismiss.type = 'button'; dismiss.className = 'btn message-action-btn'; dismiss.textContent = 'Dismiss';
    dismiss.onclick = () => {
      state.localMessages.delete(String(m.client_message_id));
      state.currentTicketData.messages = (state.currentTicketData.messages || []).filter(item => item !== m);
      state.timelineSignature = timelineSignature(state.currentTicketData);
      renderConversation(state.currentTicketData);
    };
    labelMessageAction(dismiss, 'dismiss', 'Dismiss failed message');
    actions.append(dismiss);
  }
  if (!extraClass && m.id && m.whatsapp_message_id && !deleted) {
    const reply = document.createElement('button');
    reply.type = 'button';
    reply.className = 'btn message-action-btn message-reply-btn';
    reply.append(createIcon('reply', 'icon icon-sm'), document.createTextNode('Reply'));
    labelMessageAction(reply, 'reply', 'Reply');
    reply.onclick = () => startReplyToMessage(m);
    actions.append(reply);
  }
  if (!extraClass && m.id && m.whatsapp_message_id && !deleted) {
    const reactionMenu = document.createElement('details');
    reactionMenu.className = 'message-reaction-menu';
    const summary = document.createElement('summary');
    summary.className = 'btn message-action-btn message-reaction-btn';
    summary.append(document.createTextNode('React'));
    labelMessageAction(summary, 'react', 'React');
    const picker = document.createElement('div');
    picker.className = 'message-reaction-picker';
    const ownReaction = (Array.isArray(m.reactions) ? m.reactions : []).find(item => String(item.reactor_type || '').toUpperCase() === 'AGENT');
    for (const emoji of REACTION_EMOJIS) {
      const option = document.createElement('button');
      option.type = 'button';
      option.className = 'reaction-option' + (ownReaction?.emoji === emoji ? ' active' : '');
      option.textContent = emoji;
      option.title = ownReaction?.emoji === emoji ? 'Remove ' + emoji + ' reaction' : 'React with ' + emoji;
      option.setAttribute('aria-label', option.title);
      option.onclick = event => {
        event.preventDefault();
        event.stopPropagation();
        sendMessageReaction(m, emoji, reactionMenu);
      };
      picker.append(option);
    }
    reactionMenu.append(summary, picker);
    actions.append(reactionMenu);
  }
  if (!extraClass && m.id && !deleted && state.currentTicketData?.permissions?.can_forward !== false) {
    const forward = document.createElement('button');
    forward.type = 'button';
    forward.className = 'btn message-action-btn message-forward-btn';
    forward.append(createIcon('transfer', 'icon icon-sm'), document.createTextNode('Forward'));
    labelMessageAction(forward, 'forward', 'Forward');
    forward.onclick = () => openForwardModal(m);
    actions.append(forward);
  }
  if (actions.children.length) meta.append(actions);
  if (m.send_status === 'FAILED') {
    const failed = document.createElement('span');
    failed.className = 'send-state failed';
    failed.append(createIcon('alert', 'icon icon-sm'), document.createTextNode('Send failed' + (m.error_message ? ': ' + m.error_message : '')));
    meta.append(failed);
  } else if (m.send_status === 'UNKNOWN') {
    const unknown = document.createElement('span');
    unknown.className = 'send-state pending';
    unknown.append(createIcon('alert', 'icon icon-sm'), document.createTextNode('Send status unknown. Check WhatsApp before sending again.'));
    meta.append(unknown);
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

  const reactionBar = messageReactionBar(m);
  if (reactionBar) stack.append(bubble, reactionBar, meta);
  else stack.append(bubble, meta);
  if (outbound) row.append(stack, avatar);
  else row.append(avatar, stack);
  container.append(row);
}
// Compact message toolbar: the visible label stays in textContent (accessible
// name); CSS draws the glyph from data-action and shows data-tooltip.
function labelMessageAction(button, action, label) {
  button.dataset.action = action;
  button.dataset.tooltip = label;
  button.dataset.tooltipPlacement = 'top';
  if (action !== 'copy') button.setAttribute('aria-label', label);
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
  const messageSignature = message => [
    message.id, message.updated_at, message.send_status, message.error_message,
    message.media_available, message.body, message.version, message.edited_at,
    message.deleted_for_everyone_at, message.can_edit, message.can_delete_for_everyone,
    (message.reactions || []).map(reaction => [reaction.id, reaction.emoji, reaction.reactor_type, reaction.reactor_name, reaction.updated_at])
  ];
  const messages = (data.messages || []).map(messageSignature);
  const events = (data.events || []).map(event => [event.id, event.event_type, event.created_at, event.details_json]);
  const recent = ((data.context || {}).recent_messages || []).map(message => [message.role, message.content]);
  const previous = (data.previous_ticket_history || []).map(thread => [
    thread.ticket?.id,
    thread.ticket?.updated_at,
    thread.ticket?.display_name,
    thread.messages_has_more,
    ((thread.context || {}).recent_messages || []).map(message => [message.role, message.content]),
    (thread.messages || []).map(messageSignature)
  ]);
  return JSON.stringify([data.ticket?.id, data.ticket?.updated_at, data.ticket?.display_name, data.messages_has_more,
    data.previous_ticket_history_has_more, messages, events, recent, previous]);
}

function mergeTimelineMessages(loaded, incoming) {
  const byId = new Map();
  for (const message of [...(loaded || []), ...(incoming || [])]) {
    if (message?.id) byId.set(String(message.id), message);
  }
  return [...byId.values()].sort((left, right) => Number(left.id) - Number(right.id));
}

function mergePreviousHistoryThreads(loaded, fresh) {
  const existing = new Map((loaded || []).map(thread => [String(thread.ticket?.id), thread]));
  const freshIds = new Set((fresh || []).map(thread => String(thread.ticket?.id)));
  return [
    ...(loaded || []).filter(thread => !freshIds.has(String(thread.ticket?.id))),
    ...(fresh || []).map(thread => {
      const old = existing.get(String(thread.ticket?.id));
      if (!old) return thread;
      return {
        ...thread,
        messages: mergeTimelineMessages(old.messages, thread.messages),
        messages_has_more: old._messagesExpanded ? old.messages_has_more : thread.messages_has_more,
        _messagesExpanded: Boolean(old._messagesExpanded)
      };
    })
  ];
}

function changeCachedMessage(ticketId, messageId, updated = null, hidden = false) {
  const change = messages => (messages || []).filter(message => !hidden || Number(message.id) !== Number(messageId))
    .map(message => updated && Number(message.id) === Number(messageId) ? updated : message);
  if (String(ticketId) === String(state.ticketId)) {
    state.cachedCurrentMessages = change(state.cachedCurrentMessages);
    if (state.currentTicketData) state.currentTicketData.messages = change(state.currentTicketData.messages);
  } else {
    const thread = state.previousTicketHistory.find(item => String(item.ticket?.id) === String(ticketId));
    if (thread) thread.messages = change(thread.messages);
  }
}

function upsertTimelineMessage(message, ticketId) {
  if (!message || !state.currentTicketData || String(state.ticketId) !== String(ticketId)) return;
  if (state.currentTicketData.ticket?.id && String(state.currentTicketData.ticket.id) !== String(ticketId)) return;
  const messages = state.currentTicketData.messages || (state.currentTicketData.messages = []);
  const clientId = String(message.client_message_id || '');
  const index = messages.findIndex(item =>
    (message.id && String(item.id) === String(message.id)) ||
    (clientId && String(item.client_message_id || '') === clientId));
  if (index >= 0) messages[index] = message;
  else messages.push(message);
  if (message.id) {
    const cacheIndex = state.cachedCurrentMessages.findIndex(item => String(item.id) === String(message.id));
    if (cacheIndex >= 0) state.cachedCurrentMessages[cacheIndex] = message;
    else state.cachedCurrentMessages.push(message);
  }
  if (clientId && message.id) state.localMessages.delete(clientId);
  if (message.id) state.realtimeMessages.set(String(ticketId) + ':' + message.id, message);
  state.timelineSignature = timelineSignature(state.currentTicketData);
  renderConversation(state.currentTicketData);
  if (message.direction === 'INBOUND' && message.id) {
    const key = String(ticketId);
    state.lastInboundByTicket[key] = Math.max(Number(state.lastInboundByTicket[key] || 0), Number(message.id));
  }
}

function mergeLiveMessages(data, ticketId) {
  if (!Array.isArray(data.messages)) data.messages = [];
  const seen = new Set((data.messages || []).map(message => String(message.id)));
  for (const [key, message] of state.realtimeMessages) {
    if (!key.startsWith(String(ticketId) + ':')) continue;
    if (seen.has(String(message.id))) { state.realtimeMessages.delete(key); continue; }
    data.messages.push(message);
    seen.add(String(message.id));
  }
  const confirmed = new Set(data.messages.map(message => String(message.client_message_id || '')));
  for (const [clientId, item] of state.localMessages) {
    if (String(item.ticketId) !== String(ticketId)) continue;
    if (confirmed.has(clientId)) { state.localMessages.delete(clientId); continue; }
    data.messages.push(item.message);
  }
}

function updateAudioMessageRow(existing, rendered) {
  const existingStack = existing.querySelector('.message-stack');
  const renderedStack = rendered.querySelector('.message-stack');
  const existingBubble = existingStack.querySelector('.message-bubble');
  const renderedBubble = renderedStack.querySelector('.message-bubble');
  const existingMedia = existingBubble.querySelector('.msg-media');
  const renderedMedia = renderedBubble.querySelector('.msg-media');

  // Keep the media element and all of its ancestors connected while updating
  // the sender, caption, reactions, and delivery status around it.
  function updateAround(parent, fresh, kept, freshKept) {
    const children = [...fresh.children];
    const split = children.indexOf(freshKept);
    for (const child of children.slice(0, split)) parent.insertBefore(child, kept);
    for (const child of children.slice(split + 1)) parent.append(child);
    for (const child of [...parent.children]) {
      if (child !== kept && !children.includes(child)) child.remove();
    }
  }

  updateAround(existingBubble, renderedBubble, existingMedia, renderedMedia);
  updateAround(existingStack, renderedStack, existingBubble, renderedBubble);
  existing.querySelector('.message-avatar').replaceWith(rendered.querySelector('.message-avatar'));
  existing.className = rendered.className;
}

function updateConversationKeepingAudio(conversation, rendered) {
  const existing = conversation.firstElementChild;
  if (!existing?.classList.contains('conversation-inner')) return false;
  const audioRows = new Map();
  for (const row of existing.querySelectorAll('.message-row[data-message-id]')) {
    const audio = row.querySelector('.voice-note-player audio[data-media-url]');
    if (audio) audioRows.set(row.dataset.messageId, row);
  }
  if (!audioRows.size) return false;

  let reused = 0;
  const desired = [...rendered.children].map(row => {
    const previous = row.dataset.messageId && audioRows.get(row.dataset.messageId);
    if (!previous) return row;
    const oldAudio = previous.querySelector('.voice-note-player audio[data-media-url]');
    const newAudio = row.querySelector('.voice-note-player audio[data-media-url]');
    if (!newAudio || oldAudio.dataset.mediaUrl !== newAudio.dataset.mediaUrl) return row;
    audioRows.delete(row.dataset.messageId);
    updateAudioMessageRow(previous, row);
    reused++;
    return previous;
  });
  if (!reused) return false;

  const wanted = new Set(desired);
  let cursor = existing.firstElementChild;
  for (const row of desired) {
    if (row === cursor) {
      cursor = cursor.nextElementSibling;
      continue;
    }
    while (cursor && cursor !== row && !wanted.has(cursor)) {
      const next = cursor.nextElementSibling;
      cursor.remove();
      cursor = next;
    }
    if (row === cursor) cursor = cursor.nextElementSibling;
    else existing.insertBefore(row, cursor);
  }
  while (cursor) {
    const next = cursor.nextElementSibling;
    cursor.remove();
    cursor = next;
  }
  return true;
}

function historyLoadButton(label, handler, ariaLabel = label) {
  const row = document.createElement('div');
  row.className = 'history-load-row';
  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'btn small';
  button.textContent = label;
  button.setAttribute('aria-label', ariaLabel);
  button.disabled = state.historyLoadBusy;
  button.onclick = () => handler(button);
  row.append(button);
  return row;
}

function selectedConversationText() {
  if (typeof window === 'undefined' || typeof window.getSelection !== 'function') return '';
  const selection = window.getSelection();
  const conversation = $('conversation');
  if (!selection || selection.isCollapsed || !conversation?.contains ||
      !conversation.contains(selection.anchorNode) || !conversation.contains(selection.focusNode)) return '';
  return selection.toString();
}

function beginConversationSelection(event) {
  if (event.button === 0 && event.target.closest?.('.message-body, .message-quote, .message-sender')) {
    state.conversationSelectionPointer = event.pointerId;
  }
}

function finishConversationSelection(event) {
  if (state.conversationSelectionPointer !== event.pointerId) return;
  state.conversationSelectionPointer = null;
  window.setTimeout(flushPendingConversationRender, 0);
}

function handleConversationCopy(event) {
  const selected = selectedConversationText();
  if (!selected || !event.clipboardData?.setData) return;
  event.clipboardData.setData('text/plain', selected);
  event.preventDefault();
}

function flushPendingConversationRender() {
  const pending = state.pendingConversationRender;
  if (!pending || state.conversationSelectionPointer !== null || selectedConversationText()) return;
  state.pendingConversationRender = null;
  if (String(pending.data?.ticket?.id) !== String(state.ticketId)) return;
  const data = state.currentTicketData || pending.data;
  renderConversation(data, pending.options);
  state.timelineSignature = timelineSignature(data);
}

function renderConversation(data, {forceScroll = false, preserveAudio = true, preserveScrollAnchor = null} = {}) {
  if (!forceScroll && (state.conversationSelectionPointer !== null || selectedConversationText())) {
    state.pendingConversationRender = {data, options: {forceScroll, preserveAudio, preserveScrollAnchor}};
    return;
  }
  state.pendingConversationRender = null;
  const conversation = $('conversation');
  const nearBottom = conversation.scrollHeight - conversation.scrollTop - conversation.clientHeight < 140;
  const previousScrollTop = conversation.scrollTop;
  const previousHeight = conversation.scrollHeight;
  const anchorTop = preserveScrollAnchor ? (conversation.querySelector(preserveScrollAnchor)?.getBoundingClientRect().top ?? null) : null;
  const inner = document.createElement('div');
  inner.className = 'conversation-inner';

  const previousThreads = Array.isArray(data.previous_ticket_history) ? data.previous_ticket_history : [];
  if (previousThreads.length || data.previous_ticket_history_has_more) {
    const historyHeading = document.createElement('div');
    historyHeading.className = 'context-divider';
    historyHeading.append(createIcon('history', 'icon icon-sm'), document.createTextNode('Shared customer history'));
    inner.append(historyHeading);
  }
  if (data.previous_ticket_history_has_more && !state.conversationSearchOpen) {
    inner.append(historyLoadButton('Load earlier conversations', loadEarlierConversations));
  }
  for (const thread of previousThreads) {
    const historicalTicket = thread.ticket || {};
    const divider = document.createElement('div');
    divider.className = 'previous-ticket-divider';
    divider.dataset.historyTicketId = String(historicalTicket.id || '');
    const title = document.createElement('strong');
    title.textContent = (isStaffChat(historicalTicket) ? 'Chat · ' : 'Ticket · ') +
      (historicalTicket.ticket_reference || ('#' + historicalTicket.id));
    const details = document.createElement('span');
    const closedAt = historicalTicket.closed_at || historicalTicket.resolved_at || historicalTicket.last_activity_at;
    details.append(document.createTextNode((closedAt ? fmt(closedAt) + ' · ' : '') +
      formatStatus(historicalTicket.status || 'Resolved') + ' · '), createSupportNumberLabel(historicalTicket));
    divider.append(title, details);
    inner.append(divider);
    const historicalMessages = Array.isArray(thread.messages) ? thread.messages : [];
    const historicalContext = ((thread.context || {}).recent_messages || []).filter(message => ['user', 'assistant'].includes(message.role) && message.content);
    const firstHistoricalInbound = historicalMessages.find(message => message.sender_type === 'CUSTOMER' && message.direction === 'INBOUND');
    if (firstHistoricalInbound && historicalContext.length && historicalContext[historicalContext.length - 1].role === 'user' && historicalContext[historicalContext.length - 1].content === firstHistoricalInbound.body) historicalContext.pop();
    if (historicalContext.length) {
      const contextLabel = document.createElement('div');
      contextLabel.className = 'context-divider';
      contextLabel.append(createIcon('history', 'icon icon-sm'), document.createTextNode('AI context before this handoff'));
      inner.append(contextLabel);
      for (const message of historicalContext) {
        addMessage(inner, {body: message.content, sender_type: message.role === 'user' ? 'CUSTOMER' : 'AI', direction: message.role === 'user' ? 'INBOUND' : 'OUTBOUND'}, 'context-msg previous-ticket-msg');
      }
    }
    if (thread.messages_has_more && !state.conversationSearchOpen) {
      inner.append(historyLoadButton('Load older messages', button => loadOlderConversationMessages(historicalTicket.id, true, button),
        'Load older messages in ' + (historicalTicket.ticket_reference || ('conversation #' + historicalTicket.id))));
    }
    for (const message of [...historicalMessages].sort((left, right) => String(messageOccurredAt(left) || '').localeCompare(String(messageOccurredAt(right) || '')))) {
      addMessage(inner, message, 'previous-ticket-msg');
    }
  }
  if (previousThreads.length) {
    const currentDivider = document.createElement('div');
    currentDivider.className = 'context-divider';
    currentDivider.append(createIcon('message', 'icon icon-sm'), document.createTextNode(
      (isStaffChat(data.ticket) ? 'Current chat · ' : 'Current ticket · ') +
      (data.ticket?.ticket_reference || ('#' + data.ticket?.id))));
    inner.append(currentDivider);
  }

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
  if (data.messages_has_more && !state.conversationSearchOpen) {
    inner.append(historyLoadButton('Load older messages', button => loadOlderConversationMessages(data.ticket?.id, false, button),
      'Load older messages in this conversation'));
  }

  const timeline = [
    ...(data.messages || []).map(item => ({kind: 'message', at: messageOccurredAt(item), data: item})),
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

  if (!previousThreads.length && !recent.length && !timeline.length) {
    const empty = emptyMarkup('No messages yet', 'The customer conversation has no message history to display.', 'message');
    empty.outer.classList.add('conversation-empty');
    conversation.replaceChildren(empty.outer);
  } else {
    if (!preserveAudio || !updateConversationKeepingAudio(conversation, inner)) conversation.replaceChildren(inner);
  }

  if (preserveScrollAnchor) {
    const newAnchor = conversation.querySelector(preserveScrollAnchor);
    conversation.scrollTop = previousScrollTop + (anchorTop !== null && newAnchor
      ? newAnchor.getBoundingClientRect().top - anchorTop
      : conversation.scrollHeight - previousHeight);
  } else if (forceScroll || nearBottom) conversation.scrollTop = conversation.scrollHeight;
  else conversation.scrollTop = previousScrollTop;
  updateScrollLatestButton();
}

function beginHistoryLoad(button) {
  if (state.historyLoadBusy || !state.authenticated || !state.ticketId || !state.currentTicketData) return null;
  state.historyLoadBusy = true;
  const request = {
    ticketId: state.ticketId,
    selectionEpoch: state.historySelectionEpoch,
    requestId: ++state.historyLoadRequest,
    button,
    label: button?.textContent || ''
  };
  if (button) { button.disabled = true; button.textContent = 'Loading…'; }
  return request;
}

function historyLoadIsCurrent(request) {
  return request.requestId === state.historyLoadRequest &&
    request.selectionEpoch === state.historySelectionEpoch &&
    String(request.ticketId) === String(state.ticketId) && state.authenticated;
}

function finishHistoryLoad(request) {
  if (request.requestId !== state.historyLoadRequest) return;
  state.historyLoadBusy = false;
  if (request.button?.isConnected) {
    request.button.disabled = false;
    request.button.textContent = request.label;
  }
}

function historyDetailBase() {
  const origin = state.currentTicketData?.ticket?.conversation_origin || state.currentConversationOrigin;
  return '/api/support/' + (String(origin).toUpperCase() === 'STAFF' ? 'chats/' : 'tickets/') + state.ticketId;
}

async function loadEarlierConversations(button = null) {
  const oldest = state.previousTicketHistory[0];
  const beforeTicketId = Number(oldest?.ticket?.id);
  if (!state.previousTicketHistoryHasMore || !beforeTicketId) return;
  const request = beginHistoryLoad(button);
  if (!request) return;
  const anchor = '[data-history-ticket-id="' + beforeTicketId + '"]';
  try {
    const result = await api(historyDetailBase() + '/history?before_ticket_id=' + beforeTicketId + '&limit=5');
    if (!historyLoadIsCurrent(request)) return;
    if (!Array.isArray(result.previous_ticket_history)) throw new Error('Earlier conversation history could not be loaded.');
    const existing = new Set(state.previousTicketHistory.map(thread => String(thread.ticket?.id)));
    const older = result.previous_ticket_history.filter(thread => thread.ticket?.id && !existing.has(String(thread.ticket.id)));
    state.previousTicketHistory = [...older, ...state.previousTicketHistory];
    state.previousTicketHistoryHasMore = older.length > 0 && Boolean(result.previous_ticket_history_has_more);
    state.previousHistoryExpanded = true;
    updateSharedHistoryShortcut();
    const data = state.currentTicketData;
    data.previous_ticket_history = state.previousTicketHistory;
    data.previous_ticket_history_has_more = state.previousTicketHistoryHasMore;
    state.historyLoadBusy = false;
    renderConversation(data, {preserveScrollAnchor: anchor});
    state.timelineSignature = timelineSignature(data);
  } catch (error) {
    if (historyLoadIsCurrent(request) && !state.pageLeaving) {
      state.historyLoadBusy = false;
      renderConversation(state.currentTicketData, {preserveScrollAnchor: anchor});
      showError(error.message);
    }
  } finally {
    finishHistoryLoad(request);
  }
}

async function loadOlderConversationMessages(ticketId, previous = false, button = null) {
  const targetId = Number(ticketId);
  const thread = previous ? state.previousTicketHistory.find(item => Number(item.ticket?.id) === targetId) : null;
  const messages = previous ? thread?.messages : state.cachedCurrentMessages;
  const hasMore = previous ? thread?.messages_has_more : state.currentMessagesHasMore;
  const oldestMessageId = Number((messages || []).find(message => message?.id)?.id);
  if (!targetId || !hasMore || !oldestMessageId || (previous && !thread)) return;
  const request = beginHistoryLoad(button);
  if (!request) return;
  const anchor = '[data-message-id="' + oldestMessageId + '"]';
  const path = historyDetailBase() + (previous ? '/history/' + targetId : '') +
    '/messages?before_message_id=' + oldestMessageId + '&limit=' + (previous ? '100' : '150');
  try {
    const result = await api(path);
    if (!historyLoadIsCurrent(request)) return;
    if (!Array.isArray(result.messages)) throw new Error('Older messages could not be loaded.');
    const data = state.currentTicketData;
    if (previous) {
      const currentThread = state.previousTicketHistory.find(item => Number(item.ticket?.id) === targetId);
      if (!currentThread) return;
      const oldCount = (currentThread.messages || []).length;
      currentThread.messages = mergeTimelineMessages(result.messages, currentThread.messages);
      currentThread.messages_has_more = currentThread.messages.length > oldCount && Boolean(result.messages_has_more);
      currentThread._messagesExpanded = true;
      data.previous_ticket_history = state.previousTicketHistory;
    } else {
      const oldCount = state.cachedCurrentMessages.length;
      state.cachedCurrentMessages = mergeTimelineMessages(result.messages, state.cachedCurrentMessages);
      state.currentMessagesHasMore = state.cachedCurrentMessages.length > oldCount && Boolean(result.messages_has_more);
      state.currentMessagesExpanded = true;
      data.messages = state.cachedCurrentMessages.slice();
      data.messages_has_more = state.currentMessagesHasMore;
      mergeLiveMessages(data, state.ticketId);
    }
    state.historyLoadBusy = false;
    renderConversation(data, {preserveScrollAnchor: anchor});
    state.timelineSignature = timelineSignature(data);
  } catch (error) {
    if (historyLoadIsCurrent(request) && !state.pageLeaving) {
      state.historyLoadBusy = false;
      renderConversation(state.currentTicketData, {preserveScrollAnchor: anchor});
      showError(error.message);
    }
  } finally {
    finishHistoryLoad(request);
  }
}

function updateSharedHistoryShortcut() {
  const button = $('viewSharedHistoryBtn');
  const count = state.previousTicketHistory.length;
  const hasHistory = count > 0 || state.previousTicketHistoryHasMore;
  button.classList.toggle('hidden', !hasHistory);
  text($('sharedHistoryCount'), hasHistory ? '(' + count + (state.previousTicketHistoryHasMore ? '+' : '') + ')' : '');
  button.setAttribute('aria-label', hasHistory
    ? 'View shared customer history, ' + count + (state.previousTicketHistoryHasMore ? ' or more' : '') + ' conversations'
    : 'View shared customer history');
  if (!hasHistory) {
    state.historyJumpActive = false;
    state.historyJumpReachedTop = false;
  }
}

function jumpToSharedHistory() {
  if (!state.currentTicketData || $('viewSharedHistoryBtn').classList.contains('hidden')) return;
  state.historyJumpActive = true;
  state.historyJumpReachedTop = false;
  $('conversation').scrollTo({top: 0, behavior: 'smooth'});
  updateScrollLatestButton();
}

function jumpToCurrentConversation() {
  state.historyJumpActive = false;
  state.historyJumpReachedTop = false;
  const conversation = $('conversation');
  conversation.scrollTo({top: conversation.scrollHeight, behavior: 'smooth'});
  updateScrollLatestButton();
}

function updateScrollLatestButton() {
  const conversation = $('conversation');
  const away = conversation.scrollHeight - conversation.scrollTop - conversation.clientHeight > 220;
  if (state.historyJumpActive) {
    if (conversation.scrollTop <= 20) state.historyJumpReachedTop = true;
    else if (state.historyJumpReachedTop && !away) {
      state.historyJumpActive = false;
      state.historyJumpReachedTop = false;
    }
  }
  const button = $('scrollToLatestBtn');
  button.classList.toggle('hidden', !away && !state.historyJumpActive);
  const label = state.historyJumpActive ? 'Back to current' : 'Latest';
  text($('scrollToLatestLabel'), label);
  button.setAttribute('aria-label', state.historyJumpActive ? 'Back to current conversation' : 'Scroll to latest message');
}

function contextValue(value) {
  if (value === true) return 'Yes';
  if (value === false) return 'No';
  if (value === null || value === undefined || value === '') return '—';
  return String(value);
}

const CONTEXT_CARD_ICONS = {Customer: 'user', Escalation: 'alert', 'AI summary': 'activity', Shipment: 'truck', Ticket: 'tag', Chat: 'message'};

function appendContextCard(container, title, entries, {summary = false} = {}) {
  const card = document.createElement('section');
  card.className = 'context-card' + (title === 'AI summary' ? ' ai' : '');
  const heading = document.createElement('h3');
  if (CONTEXT_CARD_ICONS[title]) heading.append(createIcon(CONTEXT_CARD_ICONS[title], 'icon icon-sm'));
  heading.append(document.createTextNode(title));
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
      if (value && typeof value === 'object' && typeof value.append === 'function') description.append(value);
      else description.textContent = contextValue(value);
      if (kind === 'code') description.classList.add('code');
      item.append(term, description);
      list.append(item);
    }
    card.append(list);
  }
  container.append(card);
}

async function saveCurrentContact(ticket, identity) {
  if (!ticket || !state.ticketId || state.busy) return;
  const current = identity && identity.saved_contact ? (identity.name || ticket.customer_name || '') : '';
  const name = await promptDialog({
    title: identity && identity.saved_contact ? 'Edit contact name' : 'Save this guest as a contact',
    label: 'Contact name',
    value: current,
    confirmLabel: identity && identity.saved_contact ? 'Save name' : 'Save contact'
  });
  if (name === null) return;
  const trimmed = name.trim();
  if (!trimmed) { notify('Contact name is required.', 'error'); return; }
  state.busy = true;
  setWorkspaceBusy(true);
  try {
    await api('/api/support/tickets/' + state.ticketId + '/contact', {method:'PUT', body: JSON.stringify({name: trimmed})});
    notify('Contact saved.');
    await openTicket(state.ticketId, {silent: true, refreshQueue: true});
  } catch (error) {
    showError(error.message);
    notify(error.message, 'error');
  } finally {
    state.busy = false;
    setWorkspaceBusy(false);
  }
}

function renderContext(ticket, context, shipment, identity = null, permissions = null) {
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
  if (identity && !identity.registered_user && permissions && permissions.can_operate) {
    const save = document.createElement('button');
    save.type = 'button';
    save.className = 'btn small context-contact-action';
    save.append(createIcon('user', 'icon icon-sm'), document.createTextNode(identity.saved_contact ? 'Edit contact' : 'Save contact'));
    save.onclick = () => saveCurrentContact(ticket, identity);
    hero.append(save);
  }
  container.append(hero);

  appendContextCard(container, 'Customer', [
    ['Name', ticket.customer_name],
    ['User ID', ticket.customer_userid, 'code'],
    ['WhatsApp', ticket.whatsapp_phone],
    ['Authenticated', context.authenticated],
    ['Identity', identity?.registered_user ? 'Registered customer' : identity?.saved_contact ? 'Saved contact' : 'Guest']
  ]);

  if (!isStaffChat(ticket)) {
    appendContextCard(container, 'Escalation', [
      ['Reason', ticket.reason],
      ['Requested action', ticket.requested_action],
      ['Tracking reference', ticket.tracking_reference, 'code']
    ]);
    if (ticket.ai_summary) appendContextCard(container, 'AI summary', [['Summary', ticket.ai_summary]], {summary: true});
  }

  if (shipment) {
    appendContextCard(container, 'Shipment', [
      ['Status', shipment.status],
      ['Mode', shipment.mode],
      ['Description', shipment.description],
      ['Order ID', shipment.order_id, 'code'],
      ['Shipment ID', shipment.shipment_id, 'code']
    ]);
  }

  if (isStaffChat(ticket)) {
    appendContextCard(container, 'Chat', [
      ['Status', ticket.active_key ? 'Open' : 'Closed'],
      ['Owner', ticket.current_assignee_name || 'Unassigned'],
      ['WhatsApp line', createSupportNumberLabel(ticket)],
      ['Started', fmt(ticket.created_at)]
    ]);
  } else {
    appendContextCard(container, 'Ticket', [
      ['Status', formatStatus(ticket.status)],
      ['Assignee', ticket.current_assignee_name || 'Unassigned'],
      ['Received on', createSupportNumberLabel(ticket)],
      ['Created', fmt(ticket.created_at)]
    ]);
  }
  $('contextPanel').scrollTop = previousScrollTop;
}

function renderTicketHeader(ticket) {
  const customer = ticket.customer_name || ticket.customer_userid || ticket.whatsapp_phone || 'Unknown customer';
  text($('ticketTitle'), isStaffChat(ticket) ? customer : (ticket.ticket_reference || ('Ticket #' + ticket.id)));
  $('ticketSubtitle').replaceChildren(
    document.createTextNode(isStaffChat(ticket) ? ('Chat · ' + ticket.whatsapp_phone + ' · ') : (customer + ' · Received on ')),
    createSupportNumberLabel(ticket)
  );
  text($('ticketAvatar'), initials(customer));
  $('ticketHeadingStatus').replaceChildren(conversationStatusBadge(ticket));
  const meta = $('ticketHeaderMeta');
  meta.replaceChildren();
  if (ticket.current_assignee_name) meta.append(createMetaChip('Assigned to ' + ticket.current_assignee_name, 'user'));
  else meta.append(createMetaChip('Unassigned', 'user'));
  if (ticket.tracking_reference) meta.append(createMetaChip(ticket.tracking_reference, 'truck'));
}

function renderReadOnlyState(data) {
  const banner = $('readOnlyBanner');
  const ticket = data.ticket;
  const permissions = data.permissions;
  banner.replaceChildren();
  if (permissions.is_read_only_observer && ticket.active_key) {
    banner.append(createIcon('eye'), document.createTextNode('Watching only — this conversation is assigned to ' + (ticket.current_assignee_name || 'another staff member') + '. You can follow it live, but only the current assignee can send messages.'));
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

  if (isStaffChat(ticket)) {
    if (permissions.can_transfer && ticket.active_key) container.append(actionButton('Transfer chat', openTransferModal, '', 'transfer'));
    if (permissions.can_operate && ticket.active_key) {
      container.append(actionButton('Close chat', closeStaffChat, 'danger', 'check-circle'));
    }
    return;
  }

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
    container.append(actionButton('Resolve & return to AI', resolveTicket, 'success-action', 'check-circle'));
  }
  if (permissions.can_delete_history) {
    container.append(actionButton('Delete ticket', deleteHistoryTicket, 'danger', 'trash'));
  }
}

function setWorkspaceBusy(busy) {
  for (const control of document.querySelectorAll('[data-ticket-action="true"]')) control.disabled = busy;
  updateComposerControls();
}

function latestInboundMessageId(data) {
  const messages = Array.isArray(data && data.messages) ? data.messages : [];
  let latest = 0;
  for (const message of messages) {
    if (String(message.direction || '').toUpperCase() !== 'INBOUND') continue;
    const id = Number(message.id || 0);
    if (id > latest) latest = id;
  }
  return latest;
}

function syncConversationSearchControls() {
  const total = state.conversationSearchResults.length;
  const index = state.conversationSearchIndex;
  text($('conversationSearchCount'), total && index >= 0 ? (index + 1) + ' / ' + total : (state.conversationSearchQuery ? '0 / 0' : '—'));
  $('conversationSearchPrev').disabled = !total || index <= 0;
  $('conversationSearchNext').disabled = !total || index < 0 || index >= total - 1;
}

function openConversationSearch() {
  if (!state.ticketId) return;
  state.conversationSearchOpen = true;
  $('conversationSearchToolbar').classList.remove('hidden');
  $('conversationSearchInput').focus();
  $('conversationSearchInput').select();
  syncConversationSearchControls();
}

function closeConversationSearch(restoreFocus = true) {
  state.conversationSearchOpen = false;
  state.conversationSearchQuery = '';
  state.conversationSearchResults = [];
  state.conversationSearchIndex = -1;
  state.searchTargetMessageId = null;
  if (state.conversationSearchTimer) { clearTimeout(state.conversationSearchTimer); state.conversationSearchTimer = null; }
  const toolbar = $('conversationSearchToolbar');
  if (toolbar) toolbar.classList.add('hidden');
  if ($('conversationSearchInput')) $('conversationSearchInput').value = '';
  syncConversationSearchControls();
  const activeTicketId = state.ticketId;
  if (restoreFocus) {
    $('conversationSearchBtn')?.focus();
    if (activeTicketId) openTicket(activeTicketId, {silent: true});
  }
}

async function searchCurrentConversation({focusMessageId = null, preserveQuery = false} = {}) {
  if (!state.ticketId) return;
  const query = preserveQuery ? state.conversationSearchQuery : $('conversationSearchInput').value.trim();
  state.conversationSearchQuery = query;
  if (query.length < 2) {
    state.conversationSearchResults = [];
    state.conversationSearchIndex = -1;
    syncConversationSearchControls();
    if (state.currentTicketData) renderConversation(state.currentTicketData, {forceScroll: false});
    return;
  }
  try {
    const data = await api('/api/support/search/messages?q=' + encodeURIComponent(query) + '&ticket_id=' + encodeURIComponent(state.ticketId) + '&limit=100');
    const chronological = (Array.isArray(data.items) ? data.items : []).slice().reverse();
    state.conversationSearchResults = chronological;
    let index = chronological.length ? chronological.length - 1 : -1;
    if (focusMessageId) {
      const found = chronological.findIndex(item => Number(item.message_id) === Number(focusMessageId));
      if (found >= 0) index = found;
    }
    state.conversationSearchIndex = index;
    syncConversationSearchControls();
    if (index >= 0) await jumpToConversationSearchResult(index);
  } catch (error) {
    showError(error.message);
  }
}

function scheduleConversationSearch() {
  if (state.conversationSearchTimer) clearTimeout(state.conversationSearchTimer);
  state.conversationSearchTimer = setTimeout(() => searchCurrentConversation(), 220);
}

async function jumpToConversationSearchResult(index) {
  const item = state.conversationSearchResults[index];
  if (!item || !state.ticketId) return;
  state.conversationSearchIndex = index;
  state.searchTargetMessageId = Number(item.message_id);
  syncConversationSearchControls();
  await openTicket(state.ticketId, {silent: true, aroundMessageId: state.searchTargetMessageId, preserveSearch: true});
  const target = $('conversation').querySelector('[data-message-id="' + state.searchTargetMessageId + '"]');
  if (target) {
    target.classList.add('search-target');
    target.scrollIntoView({block: 'center', behavior: 'smooth'});
  }
}

function moveConversationSearch(delta) {
  const next = state.conversationSearchIndex + delta;
  if (next < 0 || next >= state.conversationSearchResults.length) return;
  jumpToConversationSearchResult(next);
}

async function openTicket(id, {silent = false, refreshQueue = false, refreshHistory = false, aroundMessageId = null, preserveSearch = false, conversationOrigin = null, restoring = false} = {}) {
  const isNewSelection = String(state.ticketId || '') !== String(id || '');
  if (!isNewSelection && silent && !refreshHistory && state.previousHistoryPending) return;
  if (isNewSelection) {
    state.pendingConversationRender = null;
    state.currentTicketData = null;
    setComposerEnabled(false);
    cancelMessageEdit(false);
    if (state.mediaRecorder) cancelVoiceRecording();
    clearMediaDraft();
    clearReplyTarget();
    $('message').value = '';
    updateComposerCount();
    state.timelineSignature = '';
    state.previousTicketHistory = [];
    state.previousTicketHistoryHasMore = false;
    state.previousHistoryExpanded = false;
    state.previousHistoryLoaded = false;
    state.previousHistoryPending = false;
    state.historyJumpActive = false;
    state.historyJumpReachedTop = false;
    updateSharedHistoryShortcut();
    $('scrollToLatestBtn').classList.add('hidden');
    state.cachedCurrentMessages = [];
    state.currentMessagesHasMore = false;
    state.currentMessagesExpanded = false;
    state.historyLoadBusy = false;
    state.historyLoadRequest += 1;
    state.historySelectionEpoch += 1;
    if (!preserveSearch) closeConversationSearch(false);
  }
  state.ticketId = id;
  if (conversationOrigin) state.currentConversationOrigin = conversationOrigin;
  else if (isNewSelection) state.currentConversationOrigin = state.view === 'chats' ? 'STAFF' : 'CUSTOMER';
  setMobileTicketOpen(true);
  // Keep the desktop Customer Details panel in the state chosen by the agent.
  // Background polling reuses openTicket(), so closing it unconditionally here
  // caused the panel to collapse every few seconds. On compact/mobile layouts
  // it remains an overlay, so close it only when switching to another ticket.
  if (isNewSelection && window.innerWidth <= 1100) closeContextPanel(false);
  renderQueue();
  showError('');
  const requestId = ++state.ticketRequest;
  const needPreviousHistory = isNewSelection || refreshHistory || !state.previousHistoryLoaded || state.previousHistoryPending;
  if (needPreviousHistory) state.previousHistoryPending = true;
  if (isNewSelection || !silent) $('workspaceLoading').classList.remove('hidden');
  try {
    const chatRequest = String(state.currentConversationOrigin || '').toUpperCase() === 'STAFF';
    const detailBase = chatRequest ? '/api/support/chats/' : '/api/support/tickets/';
    const detailParams = new URLSearchParams();
    if (aroundMessageId) detailParams.set('around_message_id', String(aroundMessageId));
    if (needPreviousHistory) detailParams.set('include_previous_history', 'true');
    const detailPath = detailBase + id + (detailParams.toString() ? ('?' + detailParams.toString()) : '');
    const data = await api(detailPath);
    if (requestId !== state.ticketRequest || String(state.ticketId) !== String(id)) return;
    if (needPreviousHistory && Array.isArray(data.previous_ticket_history)) {
      state.previousTicketHistory = state.previousHistoryExpanded
        ? mergePreviousHistoryThreads(state.previousTicketHistory, data.previous_ticket_history)
        : data.previous_ticket_history;
      if (!state.previousHistoryExpanded) state.previousTicketHistoryHasMore = Boolean(data.previous_ticket_history_has_more);
      state.previousHistoryLoaded = true;
    }
    data.previous_ticket_history = state.previousTicketHistory;
    data.previous_ticket_history_has_more = state.previousTicketHistoryHasMore;
    updateSharedHistoryShortcut();
    if (!aroundMessageId) {
      if (isNewSelection || !state.cachedCurrentMessages.length) {
        state.cachedCurrentMessages = Array.isArray(data.messages) ? data.messages.slice() : [];
        state.currentMessagesHasMore = Boolean(data.messages_has_more);
      } else {
        state.cachedCurrentMessages = mergeTimelineMessages(state.cachedCurrentMessages, data.messages);
        if (!state.currentMessagesExpanded) state.currentMessagesHasMore = Boolean(data.messages_has_more);
      }
      data.messages = state.cachedCurrentMessages.slice();
      data.messages_has_more = state.currentMessagesHasMore;
    }
    mergeLiveMessages(data, id);
    state.currentTicketData = data;
    if (state.replyTarget && (data.messages || []).some(item => Number(item.id) === state.replyTarget.id && item.deleted_for_everyone_at)) {
      clearReplyTarget();
      notify('The replied-to message was deleted.', 'error');
    }
    const ticket = data.ticket;
    state.currentConversationOrigin = ticket?.conversation_origin || state.currentConversationOrigin;
    saveSupportSelection();
    const inboundId = latestInboundMessageId(data);
    const previousInboundId = Number(state.lastInboundByTicket[String(id)] || 0);
    if (!isNewSelection && previousInboundId && inboundId > previousInboundId && ticket.current_assignee_id) {
      notifyNewCustomerActivity('New customer message from ' + (ticket.customer_name || ticket.customer_userid || ticket.whatsapp_phone || 'customer') + '.');
    }
    if (inboundId) state.lastInboundByTicket[String(id)] = inboundId;
    renderTicketHeader(ticket);
    renderContext(ticket, data.context || {}, data.shipment || null, data.customer_identity || null, data.permissions || null);
    renderReadOnlyState(data);
    renderActions(data);
    const signature = timelineSignature(data);
    if (signature !== state.timelineSignature || isNewSelection || aroundMessageId) {
      renderConversation(data, {forceScroll: isNewSelection && !aroundMessageId, preserveAudio: !isNewSelection});
      state.timelineSignature = signature;
    }
    if (aroundMessageId) {
      const target = $('conversation').querySelector('[data-message-id="' + aroundMessageId + '"]');
      if (target) {
        target.classList.add('search-target');
        target.scrollIntoView({block: 'center', behavior: 'smooth'});
      }
    }
    setComposerEnabled(Boolean(data.permissions.can_reply));
    if (data.permissions.can_reply && isStaffChat(ticket) && !ticket.active_key) {
      text($('composerHelp'), 'Sending will reopen this chat and preserve its full history.');
    }
    if ((isNewSelection && !restoring) || !silent || refreshQueue) await loadQueue({silent: true});
    if (isNewSelection) setAppStatus(isStaffChat(ticket) ? ('Opened chat with ' + (ticket.customer_name || ticket.whatsapp_phone || 'customer') + '.') : ('Opened ' + (ticket.ticket_reference || 'ticket') + ' for ' + (ticket.customer_name || ticket.customer_userid || ticket.whatsapp_phone || 'customer') + '.'));
  } catch (e) {
    if (state.pageLeaving || requestId !== state.ticketRequest) return;
    if (restoring && [403, 404].includes(e.status)) {
      clearTicketWorkspace();
      await loadQueue({silent: true});
      showError('The saved conversation is no longer available. Your queue is still open.');
      return;
    }
    if(e.status===403) {
      state.currentTicketData = null;
      state.ticketId = null;
      state.previousTicketHistory = [];
      state.previousTicketHistoryHasMore = false;
      state.historyJumpActive = false;
      state.historyJumpReachedTop = false;
      updateSharedHistoryShortcut();
      $('scrollToLatestBtn').classList.add('hidden');
      state.timelineSignature = '';
      state.currentConversationOrigin = null;
      saveSupportSelection();
      setComposerEnabled(false);
      clearMediaDraft();
      text($('ticketTitle'), 'Private conversation');
      text($('ticketSubtitle'), 'Conversation access is restricted to participating staff.');
      $('ticketHeadingStatus').replaceChildren();
      $('ticketHeaderMeta').replaceChildren();
      $('conversation').innerHTML = '<div class="conversation-empty"><div class="empty-state-inner"><div class="empty-icon"><svg class="icon icon-lg" aria-hidden="true"><use href="#icon-lock"></use></svg></div><h3>Conversation access restricted</h3><p>You can only open conversations you are allowed to access.</p></div></div>';
      $('context').replaceChildren();
      $('actions').replaceChildren();
      $('readOnlyBanner').classList.add('hidden');
      await loadQueue({silent: true});
    } else if (e.status === 404) {
      clearTicketWorkspace();
      await loadQueue({silent: true});
    }
    showError(e.message);
  } finally {
    if (requestId === state.ticketRequest) {
      state.previousHistoryPending = false;
      $('workspaceLoading').classList.add('hidden');
    }
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
  if (!succeeded) return;
  // A claimed ticket now belongs to this agent: follow it into My tickets so the
  // queue shows where it went. openTicket's refreshQueue loads the new view.
  if (suffix === '/claim') setQueueView('mine', {load: false});
  await openTicket(state.ticketId, {silent: true, refreshQueue: true});
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
  if (succeeded) await openTicket(state.ticketId, {silent: true, refreshQueue: true});
}

async function resolveTicket() {
  if (!(await confirmDialog({title: 'Resolve ticket?', message: 'Resolve this ticket and return the WhatsApp conversation to AI?', confirmLabel: 'Resolve & return to AI', tone: 'success'}))) return;
  await doAction('/resolve', 'Ticket resolved and returned to AI.');
}

async function closeStaffChat() {
  const data = state.currentTicketData;
  if (state.busy || !state.ticketId || !isStaffChat(data?.ticket)) return;
  if (!(await confirmDialog({title: 'Close chat?', message: 'Close this chat? Its message history will stay in Chats and can be reopened later.', confirmLabel: 'Close chat', tone: 'danger'}))) return;
  state.busy = true;
  setWorkspaceBusy(true);
  showError('');
  try {
    await api('/api/support/chats/' + state.ticketId + '/close', {method:'POST'});
    notify('Chat closed. Its history is preserved and will be reused if you start this chat again.');
    await openTicket(state.ticketId, {silent: true, refreshQueue: true, conversationOrigin: 'STAFF'});
  } catch (error) {
    showError(error.message);
    notify(error.message, 'error');
  } finally {
    state.busy = false;
    setWorkspaceBusy(false);
  }
}

async function deleteHistoryTicket() {
  const data = state.currentTicketData;
  if (state.busy || !state.ticketId || !data?.permissions?.can_delete_history) return;
  const reference = data.ticket?.ticket_reference || ('Ticket #' + state.ticketId);
  if (!(await confirmDialog({title: 'Delete from History?', message: 'Permanently delete ' + reference + ' from History?\n\nThis deletes its saved messages, events, and locally stored attachments. This action cannot be undone.', confirmLabel: 'Delete permanently', tone: 'danger'}))) return;

  const ticketId = state.ticketId;
  state.busy = true;
  setWorkspaceBusy(true);
  showError('');
  try {
    await api('/api/support/tickets/' + ticketId, {method:'DELETE'});
    notify(reference + ' deleted from History.');
    clearTicketWorkspace();
    await loadQueue();
  } catch (error) {
    showError(error.message);
    notify(error.message, 'error');
  } finally {
    state.busy = false;
    setWorkspaceBusy(false);
  }
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
  text($('composerHelp'), 'Messages are sent from the WhatsApp number for this conversation.');
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
  if (draft.kind === 'image' || draft.kind === 'sticker') {
    const img = document.createElement('img');
    img.src = draft.url;
    img.alt = draft.kind === 'sticker' ? 'Selected sticker preview' : 'Selected image preview';
    preview.append(img);
  } else if (draft.kind === 'audio') {
    preview.append(createVoiceNotePlayer(draft.url, 'Voice note preview').player);
  } else {
    preview.append(createIcon('file', 'icon icon-lg'));
  }
  const info = document.createElement('div');
  info.className = 'media-draft-info';
  const name = document.createElement('div');
  name.className = 'media-draft-name';
  name.textContent = draft.kind === 'image'
    ? (draft.file.name || 'Selected image')
    : draft.kind === 'sticker'
      ? (draft.file.name || 'WhatsApp sticker')
    : draft.kind === 'document'
      ? (draft.file.name || 'PDF document')
      : 'Voice note ready';
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

  if (draft.kind === 'image' || draft.kind === 'document') {
    $('message').maxLength = 1024;
    $('message').placeholder = draft.kind === 'image' ? 'Add an image caption (optional)' : 'Add a PDF caption (optional)';
    text($('composerHelp'), draft.kind === 'image'
      ? 'The image and optional caption will be sent as a public WhatsApp reply.'
      : 'The PDF and optional caption will be sent as a public WhatsApp reply.');
  } else if (draft.kind === 'audio') {
    $('message').value = '';
    $('message').placeholder = 'Voice note ready to send';
    text($('composerHelp'), 'Review the recording, then send it as a public WhatsApp voice note.');
  } else {
    $('message').value = '';
    $('message').placeholder = 'Sticker ready to send';
    text($('composerHelp'), 'The WebP sticker will be sent as a public WhatsApp reply.');
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

function focusMessageComposer() {
  window.requestAnimationFrame(() => {
    const textarea = $('message');
    if ((!state.composerEnabled && !state.editTarget) || state.busy || !textarea || textarea.disabled) return;
    try { textarea.focus({preventScroll: true}); } catch (_) { textarea.focus(); }
    const end = textarea.value.length;
    try { textarea.setSelectionRange(end, end); } catch (_) {}
  });
}

function updateComposerControls() {
  const enabled = (state.composerEnabled || Boolean(state.editTarget)) && !state.busy;
  const recording = Boolean(state.mediaRecorder);
  const draft = state.pendingMedia;
  const hasBody = Boolean($('message').value.trim());
  const canSend = enabled && !recording && (Boolean(draft) || hasBody);

  $('imageBtn').disabled = !enabled || recording || Boolean(state.editTarget);
  $('voiceBtn').disabled = !enabled || Boolean(state.editTarget);
  $('locationBtn').disabled = !enabled || recording || Boolean(state.editTarget);
  $('voiceBtn').setAttribute('aria-pressed', recording ? 'true' : 'false');
  $('voiceBtn').setAttribute('aria-label', recording ? 'Stop voice recording' : 'Record a voice note');
  $('voiceBtn').setAttribute('data-tooltip', recording ? 'Stop recording' : 'Record voice note');
  setIcon($('voiceBtn'), recording ? 'square' : 'mic');
  $('message').disabled = !enabled || recording || Boolean(draft && draft.kind === 'audio');
  $('sendBtn').disabled = !canSend;
  for (const replyButton of document.querySelectorAll('.message-reply-btn')) {
    replyButton.disabled = !enabled;
  }
  for (const reactionMenu of document.querySelectorAll('.message-reaction-menu')) {
    reactionMenu.classList.toggle('disabled', !enabled);
    const summary = reactionMenu.querySelector('summary');
    if (summary) summary.setAttribute('aria-disabled', enabled ? 'false' : 'true');
    if (!enabled) reactionMenu.open = false;
  }
  for (const forwardButton of document.querySelectorAll('.message-forward-btn')) {
    // Forwarding is independent from reply ownership. A resolved/history
    // conversation is read-only but its messages can still be forwarded.
    forwardButton.disabled = state.busy || state.forwardBusy;
  }
  const label = $('sendBtn').querySelector('.send-label');
  if (label) label.textContent = state.editTarget ? 'Save' : draft
    ? (draft.kind === 'image' ? 'Send image' : draft.kind === 'sticker' ? 'Send sticker' : draft.kind === 'document' ? 'Send PDF' : 'Send voice')
    : 'Send';
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

function showLocationError(message) {
  const element = $('locationError');
  element.textContent = message || '';
  element.style.display = message ? 'block' : 'none';
}

function openLocationModal() {
  if (!state.composerEnabled || state.busy || state.mediaRecorder) return;
  showLocationError('');
  $('locationForm').reset();
  openDialog('locationModal', 'useCurrentLocationBtn');
}

function closeLocationModal() {
  if (state.busy) return;
  showLocationError('');
  closeDialog('locationModal');
}

function useCurrentLocation() {
  showLocationError('');
  if (!navigator.geolocation) { showLocationError('Location access is not supported by this browser.'); return; }
  const button = $('useCurrentLocationBtn');
  setButtonBusy(button, true, 'Locating…');
  navigator.geolocation.getCurrentPosition(
    position => {
      $('locationLatitude').value = Number(position.coords.latitude).toFixed(7);
      $('locationLongitude').value = Number(position.coords.longitude).toFixed(7);
      setButtonBusy(button, false);
    },
    error => {
      showLocationError(error && error.code === 1 ? 'Location permission was denied.' : 'Could not determine your current location.');
      setButtonBusy(button, false);
    },
    {enableHighAccuracy: true, timeout: 12000, maximumAge: 30000}
  );
}

async function sendLocation(event) {
  event.preventDefault();
  if (state.busy || !state.ticketId || !$('locationForm').reportValidity()) return;
  const latitude = Number($('locationLatitude').value);
  const longitude = Number($('locationLongitude').value);
  if (!Number.isFinite(latitude) || latitude < -90 || latitude > 90 || !Number.isFinite(longitude) || longitude < -180 || longitude > 180) {
    showLocationError('Enter valid latitude and longitude coordinates.');
    return;
  }
  state.busy = true;
  setButtonBusy($('sendLocationBtn'), true, 'Sending…');
  showLocationError('');
  let succeeded = false;
  try {
    await api('/api/support/tickets/' + state.ticketId + '/location', {method:'POST', body: JSON.stringify({
      latitude, longitude,
      name: $('locationName').value.trim() || null,
      address: $('locationAddress').value.trim() || null,
      client_message_id: crypto.randomUUID(),
      reply_to_message_id: state.replyTarget ? state.replyTarget.id : null
    })});
    clearReplyTarget();
    closeDialog('locationModal');
    notify('Location sent to the customer.');
    succeeded = true;
    await openTicket(state.ticketId, {silent: true, refreshQueue: true});
  } catch (error) {
    showLocationError(error.message);
  } finally {
    state.busy = false;
    setButtonBusy($('sendLocationBtn'), false);
    setWorkspaceBusy(false);
  }
  if (succeeded) focusMessageComposer();
}

function chooseImage() {
  if (state.composerEnabled && !state.busy && !state.mediaRecorder) $('imageInput').click();
}

function imageSelected() {
  const file = $('imageInput').files && $('imageInput').files[0];
  if (!file) return;
  if (!['image/jpeg', 'image/png', 'image/webp', 'application/pdf'].includes(file.type)) {
    showError('Use a JPEG/PNG image, WebP sticker, or PDF document.');
    $('imageInput').value = '';
    return;
  }
  const kind = file.type === 'application/pdf' ? 'document' : file.type === 'image/webp' ? 'sticker' : 'image';
  setPendingMedia(kind, file);
  $('message').focus();
  setAppStatus(kind === 'document' ? 'PDF attached. You may add a caption before sending.' : kind === 'sticker' ? 'Sticker attached and ready to send.' : 'Image attached. You may add a caption before sending.');
}

async function sendMediaMessage() {
  const draft = state.pendingMedia;
  if (!draft || !state.ticketId || state.busy) return;
  state.busy = true;
  setWorkspaceBusy(true);
  setButtonBusy($('sendBtn'), true, draft.kind === 'image' ? 'Sending image…' : draft.kind === 'sticker' ? 'Sending sticker…' : draft.kind === 'document' ? 'Sending PDF…' : 'Sending voice…');
  showError('');
  const form = new FormData();
  form.append('media_type', draft.kind);
  form.append('client_message_id', crypto.randomUUID());
  if (draft.kind === 'image' || draft.kind === 'document') form.append('caption', $('message').value.trim());
  if (state.replyTarget) form.append('reply_to_message_id', String(state.replyTarget.id));
  form.append('file', draft.file, draft.file.name || (draft.kind === 'image' ? 'image' : draft.kind === 'sticker' ? 'sticker.webp' : draft.kind === 'document' ? 'document.pdf' : 'voice-note'));
  let refresh = false;
  let succeeded = false;
  try {
    await api('/api/support/tickets/' + state.ticketId + '/media', {method:'POST', body: form});
    $('message').value = '';
    clearMediaDraft();
    clearReplyTarget();
    notify(draft.kind === 'image' ? 'Image sent to the customer.' : draft.kind === 'sticker' ? 'Sticker sent to the customer.' : draft.kind === 'document' ? 'PDF sent to the customer.' : 'Voice note sent to the customer.');
    refresh = true;
    succeeded = true;
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
  if (refresh) await openTicket(state.ticketId, {silent: true, refreshQueue: true});
  if (succeeded) focusMessageComposer();
}

async function sendMessage() {
  if (state.editTarget) {
    const target = state.editTarget;
    const content = $('message').value.trim();
    if (!content || state.busy || !state.ticketId) return;
    const visibleTicketId = state.ticketId;
    state.busy = true; setButtonBusy($('sendBtn'), true, 'Saving…'); showError('');
    try {
      const result = await api('/api/support/tickets/' + target.ticketId + '/messages/' + target.id, {
        method: 'PATCH', body: JSON.stringify({content, version: target.version})
      });
      changeCachedMessage(target.ticketId, target.id, result.message);
      cancelMessageEdit(false);
      refreshMessageSearchAfterChange(target.id);
      if (String(state.ticketId) === String(visibleTicketId)) {
        await openTicket(visibleTicketId, {silent: true, refreshHistory: target.ticketId !== Number(visibleTicketId)});
      }
      notify('Message updated for everyone on WhatsApp.');
    } catch (error) {
      showError(error.message);
      notify(error.message, 'error');
    } finally {
      state.busy = false; setButtonBusy($('sendBtn'), false); updateComposerControls();
    }
    return;
  }
  if (state.pendingMedia) return sendMediaMessage();
  const body = $('message').value.trim();
  if (!body || state.busy || !state.ticketId || !state.composerEnabled) return;
  const ticketId = state.ticketId;
  const reply = state.replyTarget ? {...state.replyTarget} : null;
  const clientMessageId = crypto.randomUUID();
  const optimistic = {
    client_message_id: clientMessageId,
    ticket_id: ticketId,
    sender_type: 'AGENT', sender_id: state.agent?.id,
    sender_name: state.agent?.name || 'Support agent', direction: 'OUTBOUND',
    message_type: 'text', body, send_status: 'PENDING',
    created_at: new Date().toISOString(),
    reply_to: reply ? {id: reply.id, sender_name: reply.actor, body: reply.preview} : null
  };
  state.localMessages.set(clientMessageId, {ticketId, message: optimistic});
  upsertTimelineMessage(optimistic, ticketId);
  $('message').value = '';
  clearReplyTarget();
  resizeComposer();
  updateComposerCount();
  updateComposerControls();
  showError('');
  focusMessageComposer();
  const send = async () => {
    try {
      const result = await api('/api/support/tickets/' + ticketId + '/messages', {
        method: 'POST',
        body: JSON.stringify({body, client_message_id: clientMessageId, reply_to_message_id: reply?.id || null})
      });
      state.localMessages.delete(clientMessageId);
      if (result.message) upsertTimelineMessage(result.message, ticketId);
      loadQueue({silent: true}).catch(() => {});
    } catch (error) {
      optimistic.send_status = (!error.status || /WhatsApp accepted/i.test(error.message || '')) ? 'UNKNOWN' : 'FAILED';
      optimistic.error_message = error.message || 'Message could not be sent.';
      if (state.localMessages.has(clientMessageId)) {
        upsertTimelineMessage(optimistic, ticketId);
      }
      if (String(state.ticketId) === String(ticketId)) showError(optimistic.error_message);
      notify(optimistic.error_message, 'error');
    }
  };
  state.textSendChain = state.textSendChain.then(send, send);
}

function showMessagePreferencesError(message) {
  const element = $('messagePreferencesError');
  element.textContent = message || '';
  element.style.display = message ? 'block' : 'none';
}

function updateMessagePreferenceCounts() {
  text($('customGreetingCount'), $('customGreetingMessage').value.length + ' / 2000');
  text($('customResolutionCount'), $('customResolutionMessage').value.length + ' / 2000');
}

async function loadMessagePreferences() {
  const data = await api('/api/support/message-preferences');
  $('customGreetingMessage').value = data.greeting_message || '';
  $('customResolutionMessage').value = data.resolution_message || '';
  updateMessagePreferenceCounts();
  return data;
}

async function openMessagePreferencesModal() {
  if (!state.authenticated || state.messagePreferencesBusy) return;
  showMessagePreferencesError('');
  openDialog('messagePreferencesModal', 'customGreetingMessage');
  state.messagePreferencesBusy = true;
  setButtonBusy($('saveMessagePreferencesBtn'), true, 'Loading…');
  try {
    await loadMessagePreferences();
  } catch (error) {
    showMessagePreferencesError(error.message);
  } finally {
    state.messagePreferencesBusy = false;
    setButtonBusy($('saveMessagePreferencesBtn'), false);
  }
}

function closeMessagePreferencesModal() {
  if (state.messagePreferencesBusy) return;
  showMessagePreferencesError('');
  closeDialog('messagePreferencesModal');
}

async function saveMessagePreferences(event) {
  event.preventDefault();
  if (!state.authenticated || state.messagePreferencesBusy) return;
  const form = $('messagePreferencesForm');
  if (!form.reportValidity()) return;
  state.messagePreferencesBusy = true;
  setButtonBusy($('saveMessagePreferencesBtn'), true, 'Saving…');
  showMessagePreferencesError('');
  try {
    const data = await api('/api/support/message-preferences', {
      method: 'PUT',
      body: JSON.stringify({
        greeting_message: $('customGreetingMessage').value,
        resolution_message: $('customResolutionMessage').value
      })
    });
    $('customGreetingMessage').value = data.greeting_message || '';
    $('customResolutionMessage').value = data.resolution_message || '';
    updateMessagePreferenceCounts();
    notify('Automatic messages saved.');
    closeDialog('messagePreferencesModal');
  } catch (error) {
    showMessagePreferencesError(error.message);
  } finally {
    state.messagePreferencesBusy = false;
    setButtonBusy($('saveMessagePreferencesBtn'), false);
  }
}

const AI_REASON_LABELS = {
  MANUAL_ON: 'Manual control is ON',
  MANUAL_OFF: 'Manual control is OFF',
  WITHIN_SCHEDULE: 'Within scheduled AI hours',
  OUTSIDE_SCHEDULE: 'Outside scheduled AI hours'
};

function showAiSettingsError(message) {
  const element = $('aiSettingsError');
  element.textContent = message || '';
  element.style.display = message ? 'block' : 'none';
}

function selectedAiControlMode() {
  return document.querySelector('input[name="aiControlMode"]:checked')?.value || 'MANUAL';
}

function syncAiSettingsFields() {
  const scheduled = selectedAiControlMode() === 'SCHEDULED';
  $('aiManualFields').classList.toggle('hidden', scheduled);
  $('aiScheduledFields').classList.toggle('hidden', !scheduled);
  $('aiNextChange').classList.toggle('hidden', !scheduled);
}

function validIanaTimezone(value) {
  try {
    new Intl.DateTimeFormat('en-US', {timeZone: value}).format(new Date());
    return true;
  } catch (_) {
    return false;
  }
}

function validateAiSettingsForm() {
  showAiSettingsError('');
  for (const id of ['aiScheduleStart', 'aiScheduleEnd', 'aiScheduleTimezone']) {
    $(id).setAttribute('aria-invalid', 'false');
  }
  if (selectedAiControlMode() !== 'SCHEDULED') return true;

  const start = $('aiScheduleStart').value.trim();
  const end = $('aiScheduleEnd').value.trim();
  const timezone = $('aiScheduleTimezone').value.trim();
  const hhmm = /^(?:[01]\d|2[0-3]):[0-5]\d$/;
  if (!hhmm.test(start)) {
    $('aiScheduleStart').setAttribute('aria-invalid', 'true');
    showAiSettingsError('Choose a valid AI start time in HH:mm format.');
    return false;
  }
  if (!hhmm.test(end)) {
    $('aiScheduleEnd').setAttribute('aria-invalid', 'true');
    showAiSettingsError('Choose a valid AI end time in HH:mm format.');
    return false;
  }
  if (start === end) {
    $('aiScheduleStart').setAttribute('aria-invalid', 'true');
    $('aiScheduleEnd').setAttribute('aria-invalid', 'true');
    showAiSettingsError('AI start and end times must be different.');
    return false;
  }
  if (!timezone || !validIanaTimezone(timezone)) {
    $('aiScheduleTimezone').setAttribute('aria-invalid', 'true');
    showAiSettingsError('Enter a valid IANA timezone, for example Asia/Beirut or UTC.');
    return false;
  }
  return true;
}

function renderAiSettingsUnavailable(message) {
  state.aiSettings = null;
  text($('aiHeaderStatus'), '?');
  $('aiHeaderDot').className = 'ai-status-dot';
  $('aiHeaderBtn').setAttribute('aria-label', 'AI assistant status unavailable');
  $('aiHeaderBtn').setAttribute('title', message || 'AI assistant status unavailable');
}

function syncAiSettingsEditability() {
  const editable = canManageAiSettings();
  for (const id of ['aiModeManual', 'aiModeScheduled', 'aiManualEnabled', 'aiScheduleStart', 'aiScheduleEnd', 'aiScheduleTimezone']) {
    $(id).disabled = !editable;
  }
  $('saveAiSettingsBtn').classList.toggle('hidden', !editable);
  text($('cancelAiSettingsBtn'), editable ? 'Cancel' : 'Close');
  text(
    $('aiSettingsModalSubtitle'),
    editable
      ? 'Control global AI availability for new inbound WhatsApp messages.'
      : 'View the current global AI availability. Only Supervisors and Admins can change it.'
  );
}

function renderAiSettings(settings) {
  state.aiSettings = settings;
  const enabled = Boolean(settings?.effective_ai_enabled);
  text($('aiHeaderStatus'), enabled ? 'ON' : 'OFF');
  $('aiHeaderDot').className = 'ai-status-dot ' + (enabled ? 'on' : 'off');
  $('aiHeaderBtn').setAttribute('aria-label', 'AI assistant is ' + (enabled ? 'on' : 'off'));
  $('aiHeaderBtn').setAttribute('title', 'AI assistant: ' + (enabled ? 'ON' : 'OFF') + '. Select to view settings.');
  const status = $('aiEffectiveStatus');
  status.textContent = enabled ? 'ON' : 'OFF';
  status.classList.toggle('off', !enabled);
  text($('aiEffectiveReason'), AI_REASON_LABELS[settings?.effective_reason] || settings?.effective_reason || 'Current status unavailable');

  const mode = settings?.control_mode === 'SCHEDULED' ? 'SCHEDULED' : 'MANUAL';
  $('aiModeManual').checked = mode === 'MANUAL';
  $('aiModeScheduled').checked = mode === 'SCHEDULED';
  $('aiManualEnabled').value = settings?.manual_ai_enabled ? 'true' : 'false';
  $('aiScheduleStart').value = settings?.schedule?.start_time || '';
  $('aiScheduleEnd').value = settings?.schedule?.end_time || '';
  $('aiScheduleTimezone').value = settings?.schedule?.timezone || '';
  syncAiSettingsFields();
  syncAiSettingsEditability();

  text(
    $('aiNextChange'),
    settings?.next_transition_at
      ? 'Next automatic change: ' + fmtInTimezone(settings.next_transition_at, settings?.schedule?.timezone)
      : 'Next automatic change: —'
  );
  const updater = settings?.updated_by?.name ? ' by ' + settings.updated_by.name : '';
  text(
    $('aiUpdatedMeta'),
    settings?.updated_at
      ? 'Last saved ' + fmt(settings.updated_at) + updater + '. Settings are authoritative across backend instances.'
      : 'Saved configuration is authoritative across backend instances.'
  );
}

async function loadAiSettings() {
  if (!state.authenticated) return;
  const settings = await api('/api/support/ai-settings');
  renderAiSettings(settings);
}

async function openAiSettingsModal() {
  if (!state.authenticated) return;
  showAiSettingsError('');
  syncAiSettingsEditability();
  openDialog('aiSettingsModal', 'aiModeManual');
  state.aiSettingsBusy = true;
  setButtonBusy($('saveAiSettingsBtn'), true, 'Loading…');
  try {
    await loadAiSettings();
  } catch (error) {
    renderAiSettingsUnavailable(error.message);
    showAiSettingsError(error.message);
    syncAiSettingsEditability();
  } finally {
    state.aiSettingsBusy = false;
    setButtonBusy($('saveAiSettingsBtn'), false);
  }
}

function closeAiSettingsModal() {
  if (state.aiSettingsBusy) return;
  showAiSettingsError('');
  closeDialog('aiSettingsModal');
}

async function saveAiSettings(event) {
  event.preventDefault();
  if (state.aiSettingsBusy || !canManageAiSettings() || !state.aiSettings) return;
  if (!validateAiSettingsForm()) return;

  const payload = {
    control_mode: selectedAiControlMode(),
    manual_ai_enabled: $('aiManualEnabled').value === 'true',
    schedule: {
      start_time: $('aiScheduleStart').value.trim(),
      end_time: $('aiScheduleEnd').value.trim(),
      timezone: $('aiScheduleTimezone').value.trim()
    },
    version: Number(state.aiSettings.version)
  };

  state.aiSettingsBusy = true;
  setButtonBusy($('saveAiSettingsBtn'), true, 'Saving…');
  showAiSettingsError('');
  try {
    await api('/api/support/ai-settings', {method:'PUT', body: JSON.stringify(payload)});
    // Re-fetch the authoritative effective state instead of assuming the local form
    // reflects timezone boundaries or a concurrent update correctly.
    await loadAiSettings();
    notify('AI assistant settings saved.');
  } catch (error) {
    showAiSettingsError(error.status === 409 ? error.message + ' Reload the settings and try again.' : error.message);
    if (error.status === 409) {
      try { await loadAiSettings(); } catch (_) {}
    }
  } finally {
    state.aiSettingsBusy = false;
    setButtonBusy($('saveAiSettingsBtn'), false);
  }
}

function showWhatsAppFeedback(id, message) {
  const element = $(id);
  element.textContent = message || '';
  element.style.display = message ? 'block' : 'none';
}

function clearWhatsAppFeedback() {
  showWhatsAppFeedback('whatsappError', '');
  showWhatsAppFeedback('whatsappSuccess', '');
}

function renderWhatsAppRegistry(payload) {
  state.whatsappRegistry = payload || {accounts: [], numbers: []};
  const accounts = Array.isArray(state.whatsappRegistry.accounts) ? state.whatsappRegistry.accounts : [];
  const numbers = Array.isArray(state.whatsappRegistry.numbers) ? state.whatsappRegistry.numbers : [];
  const accountContainer = $('whatsappAccounts');
  const numberContainer = $('whatsappNumbers');
  accountContainer.replaceChildren();
  numberContainer.replaceChildren();

  if (!accounts.length) {
    accountContainer.append(emptyMarkup('No WABA synced yet', 'Enter a WhatsApp Business Account ID above, or receive a signed webhook so the account can be discovered.', 'phone').outer);
  } else {
    for (const account of accounts) {
      const row = document.createElement('div');
      row.className = 'whatsapp-account-row';
      const copy = document.createElement('div');
      const title = document.createElement('div');
      title.className = 'whatsapp-row-title';
      title.textContent = account.name || 'WhatsApp Business Account';
      const subtitle = document.createElement('div');
      subtitle.className = 'whatsapp-row-subtitle';
      subtitle.textContent = 'WABA ' + account.waba_id + (account.last_synced_at ? ' · synced ' + fmt(account.last_synced_at) : ' · not synced yet');
      copy.append(title, subtitle);
      const sync = document.createElement('button');
      sync.type = 'button';
      sync.className = 'btn small';
      sync.append(createIcon('refresh', 'icon icon-sm'), document.createTextNode('Sync'));
      sync.onclick = () => syncWaba(account.waba_id);
      row.append(copy, sync);
      accountContainer.append(row);
    }
  }

  if (!numbers.length) {
    numberContainer.append(emptyMarkup('No phone numbers found', 'Sync a WABA to fetch its registered WhatsApp phone numbers from Meta.', 'phone').outer);
    return;
  }

  for (const item of numbers) {
    const row = document.createElement('div');
    row.className = 'whatsapp-number-row';
    const copy = document.createElement('div');
    const title = document.createElement('div');
    title.className = 'whatsapp-row-title';
    title.append(createSupportNumberLabel(item));
    const subtitle = document.createElement('div');
    subtitle.className = 'whatsapp-row-subtitle';
    subtitle.textContent = (item.verified_name ? item.verified_name + ' · ' : '') + 'ID ' + item.phone_number_id + (item.source === 'LINKED_DEVICE' ? ' · Linked device' : item.waba_id ? ' · WABA ' + item.waba_id : ' · Cloud API');
    const meta = document.createElement('div');
    meta.className = 'whatsapp-row-meta';
    meta.append(createMetaChip(item.is_enabled ? 'Active in app' : 'Not active', item.is_enabled ? 'check-circle' : 'clock'));
    if (item.quality_rating) meta.append(createMetaChip('Quality ' + item.quality_rating, 'activity'));
    if (!item.provider_present) meta.append(createMetaChip(item.source === 'LINKED_DEVICE' ? 'Connection not configured' : 'Missing from latest Meta sync', 'alert'));
    copy.append(title, subtitle, meta);

    const actions = document.createElement('div');
    actions.className = 'whatsapp-number-actions';
    const rename = document.createElement('button');
    rename.type = 'button';
    rename.className = 'btn small';
    rename.disabled = state.whatsappBusy || state.linkedDeviceBusy;
    rename.textContent = item.display_name ? 'Edit name' : 'Rename';
    rename.setAttribute('aria-label', (item.display_name ? 'Edit name for ' : 'Rename ') + supportNumberRaw(item));
    rename.onclick = () => openWhatsAppNumberNameEditor(item, row, rename);
    actions.append(rename);

    const action = document.createElement('button');
    action.type = 'button';
    action.className = 'btn small' + (item.is_enabled ? ' danger' : ' primary');
    action.disabled = state.whatsappBusy || state.linkedDeviceBusy || (!item.provider_present && !item.is_enabled);
    action.textContent = item.is_enabled ? (item.source === 'LINKED_DEVICE' ? 'Deactivate' : 'Remove from app') : 'Add to app';
    action.onclick = () => setWhatsAppNumberState(item.phone_number_id, !item.is_enabled);
    actions.append(action);
    if (item.source === 'LINKED_DEVICE') {
      const remove = document.createElement('button');
      remove.type = 'button'; remove.className = 'btn small danger';
      remove.textContent = 'Remove and unlink';
      remove.disabled = state.whatsappBusy || state.linkedDeviceBusy;
      remove.onclick = () => removeLinkedDeviceNumber(item.phone_number_id);
      actions.append(remove);
    }
    row.append(copy, actions);
    numberContainer.append(row);
  }
  if (state.linkedDeviceStatus) renderLinkedDeviceStatus(state.linkedDeviceStatus);
}

function openWhatsAppNumberNameEditor(item, row, button) {
  if (!canManageWhatsApp() || state.whatsappBusy || state.linkedDeviceBusy || row.querySelector('.whatsapp-name-editor')) return;
  const editor = document.createElement('form');
  editor.className = 'whatsapp-name-editor';
  const field = document.createElement('label');
  field.className = 'field';
  const caption = document.createElement('span');
  caption.textContent = 'Custom name for ' + supportNumberRaw(item);
  const input = document.createElement('input');
  input.type = 'text';
  input.maxLength = 80;
  input.placeholder = 'For example, TestNumber';
  input.value = item.display_name || '';
  input.autocomplete = 'off';
  const hint = document.createElement('span');
  hint.className = 'field-hint';
  hint.textContent = 'Leave blank to show the phone number.';
  field.append(caption, input, hint);
  const save = document.createElement('button');
  save.type = 'submit'; save.className = 'btn small primary'; save.textContent = 'Save name';
  const cancel = document.createElement('button');
  cancel.type = 'button'; cancel.className = 'btn small'; cancel.textContent = 'Cancel';
  cancel.onclick = () => { editor.remove(); button.disabled = false; button.focus(); };
  editor.onsubmit = event => { event.preventDefault(); renameWhatsAppNumber(item.phone_number_id, input.value, save); };
  editor.append(field, save, cancel);
  row.append(editor);
  button.disabled = true;
  input.focus();
}

function refreshVisibleSupportNumberName(phoneNumberId, displayName) {
  const matches = ticket => ticket && String(ticket.business_phone_number_id) === String(phoneNumberId);
  for (const ticket of state.queueItems || []) if (matches(ticket)) ticket.display_name = displayName;
  for (const item of state.globalSearchResults || []) if (matches(item)) item.display_name = displayName;
  for (const line of state.chatLines || []) if (String(line.phone_number_id) === String(phoneNumberId)) line.display_name = displayName;
  const scrollTop = $('queue').scrollTop;
  renderQueue();
  $('queue').scrollTop = scrollTop;
  const data = state.currentTicketData;
  if (!data) return;
  const currentChanged = matches(data.ticket);
  if (currentChanged) data.ticket.display_name = displayName;
  let historyChanged = false;
  for (const thread of state.previousTicketHistory || []) {
    if (matches(thread.ticket)) { thread.ticket.display_name = displayName; historyChanged = true; }
  }
  if (currentChanged) {
    renderTicketHeader(data.ticket);
    renderContext(data.ticket, data.context || {}, data.shipment || null,
      data.customer_identity || null, data.permissions || null);
  }
  if (historyChanged) renderConversation(data, {preserveAudio: true});
  if (currentChanged || historyChanged) state.timelineSignature = timelineSignature(data);
}

async function renameWhatsAppNumber(phoneNumberId, value, button) {
  if (!canManageWhatsApp() || state.whatsappBusy || state.linkedDeviceBusy) return;
  const displayName = String(value || '').trim() || null;
  state.whatsappBusy = true;
  clearWhatsAppFeedback();
  setButtonBusy(button, true, 'Saving…');
  let saved = false;
  try {
    const payload = await api('/api/support/whatsapp-numbers/' + encodeURIComponent(phoneNumberId) + '/name',
      {method: 'PUT', body: JSON.stringify({display_name: displayName})});
    renderWhatsAppRegistry(payload);
    refreshVisibleSupportNumberName(phoneNumberId, displayName);
    saved = true;
    showWhatsAppFeedback('whatsappSuccess', displayName ? 'WhatsApp number renamed.' : 'Custom name removed.');
    await Promise.allSettled([
      loadQueue({silent: true}),
      state.ticketId ? openTicket(state.ticketId, {silent: true, refreshHistory: true}) : Promise.resolve()
    ]);
  } catch (error) {
    showWhatsAppFeedback('whatsappError', error.message);
  } finally {
    state.whatsappBusy = false;
    if (button.isConnected) setButtonBusy(button, false);
    if (saved && state.whatsappRegistry) renderWhatsAppRegistry(state.whatsappRegistry);
  }
}

function renderLinkedDeviceStatus(payload) {
  state.linkedDeviceStatus = payload;
  const section = $('linkedDeviceSection');
  section.classList.toggle('hidden', !payload?.configured);
  const container = $('linkedDeviceLines');
  container.replaceChildren();
  $('addLinkedDeviceBtn').disabled = state.linkedDeviceBusy || state.whatsappBusy;
  $('addLinkedDeviceBtn').textContent = pendingLinkedDeviceAddition() ? 'Retry linking' : 'Link another number';
  if (!payload?.configured) return;
  for (const line of payload.lines || []) {
    const row = document.createElement('div');
    row.className = 'linked-device-line';
    const heading = document.createElement('strong');
    const registryNumber = (state.whatsappRegistry?.numbers || []).find(item => String(item.phone_number_id) === String(line.line_id));
    heading.append(createSupportNumberLabel({line_id: line.line_id,
      display_phone_number: line.display_phone_number || registryNumber?.display_phone_number,
      display_name: registryNumber?.display_name}));
    const status = document.createElement('div');
    status.className = 'muted';
    const labels = {connected: 'Connected', connecting: 'Connecting…', qr: 'Scan this code with the phone you want to connect', logged_out: 'Not paired', disconnected: 'Disconnected', error: 'Connection failed'};
    status.textContent = labels[line.status] || 'Not paired';
    if (line.error) status.textContent += ' · ' + line.error;
    if (line.pending_callbacks) status.textContent += ' · ' + line.pending_callbacks + ' updates pending';
    if (line.failed_callbacks) status.textContent += ' · ' + line.failed_callbacks + ' failed updates need administrator attention';
    row.append(heading, status);
    if (line.status === 'qr' && typeof line.qr_data_url === 'string' && line.qr_data_url.startsWith('data:image/png;base64,')) {
      const qr = document.createElement('img');
      qr.className = 'linked-device-qr';
      qr.src = line.qr_data_url;
      qr.alt = 'Scan to pair WhatsApp line ' + line.line_id;
      row.append(qr);
    }
    const connected = line.status === 'connected';
    if (!connected) {
      const pair = document.createElement('button');
      pair.type = 'button'; pair.className = 'btn small';
      pair.textContent = line.status === 'qr' ? 'Refresh pairing' : 'Pair WhatsApp';
      pair.disabled = state.linkedDeviceBusy || state.whatsappBusy || line.status === 'connecting';
      pair.onclick = () => changeLinkedDeviceConnection(line.line_id, 'connect');
      row.append(pair);
    }
    const remove = document.createElement('button');
    remove.type = 'button'; remove.className = 'btn small danger';
    remove.textContent = 'Remove and unlink';
    remove.disabled = state.linkedDeviceBusy || state.whatsappBusy;
    remove.onclick = () => removeLinkedDeviceNumber(line.line_id);
    row.append(remove);
    container.append(row);
  }
  text($('linkedDeviceStatus'), 'Only paired lines added to the app can send or receive messages here.');
}

async function loadLinkedDeviceStatus() {
  if (!canManageWhatsApp() || !state.agent?.linked_device_available || state.linkedDeviceBusy) return;
  const request = ++state.linkedDeviceRequest;
  try {
    const payload = await api('/api/support/whatsapp-linked-device/status');
    if (request === state.linkedDeviceRequest && state.activeModal === 'whatsappModal') renderLinkedDeviceStatus(payload);
  } catch (error) {
    if (request !== state.linkedDeviceRequest || state.activeModal !== 'whatsappModal') return;
    $('linkedDeviceSection').classList.remove('hidden');
    $('linkedDeviceLines').replaceChildren();
    text($('linkedDeviceStatus'), error.message || 'The WhatsApp connection service is unavailable.');
    $('addLinkedDeviceBtn').disabled = state.linkedDeviceBusy || state.whatsappBusy;
  }
}

function linkedDeviceAdditionStorageKey() {
  return 'shipment-support-linked-device-addition:' + (state.agent?.id || '');
}

function pendingLinkedDeviceAddition() {
  if (state.linkedDeviceAdditionId) return state.linkedDeviceAdditionId;
  try {
    const pending = sessionStorage.getItem(linkedDeviceAdditionStorageKey());
    if (/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(pending || '')) state.linkedDeviceAdditionId = pending;
  } catch (_) {}
  return state.linkedDeviceAdditionId;
}

function saveLinkedDeviceAddition(requestId) {
  state.linkedDeviceAdditionId = requestId;
  try {
    if (requestId) sessionStorage.setItem(linkedDeviceAdditionStorageKey(), requestId);
    else sessionStorage.removeItem(linkedDeviceAdditionStorageKey());
  } catch (_) {}
}

async function addLinkedDeviceNumber() {
  if (!canManageWhatsApp() || !state.agent?.linked_device_available || state.linkedDeviceBusy || state.whatsappBusy) return;
  // Keep the same request ID after a lost response so retrying resumes its line.
  saveLinkedDeviceAddition(pendingLinkedDeviceAddition() || crypto.randomUUID());
  state.linkedDeviceBusy = true;
  state.linkedDeviceRequest++;
  clearWhatsAppFeedback();
  setButtonBusy($('addLinkedDeviceBtn'), true, 'Linking…');
  try {
    const payload = await api('/api/support/whatsapp-linked-device/lines', {
      method: 'POST', body: JSON.stringify({request_id: state.linkedDeviceAdditionId}),
    });
    saveLinkedDeviceAddition(null);
    if (payload.registry) renderWhatsAppRegistry(payload.registry);
    renderLinkedDeviceStatus(payload);
    const added = (payload.lines || []).find(line => line.line_id === payload.added_line_id);
    showWhatsAppFeedback('whatsappSuccess', added?.status === 'connected'
      ? 'WhatsApp number connected. Choose Add to app to activate it.'
      : 'Scan the new pairing code using the number you want to link, then choose Add to app.');
  } catch (error) {
    if (error.status === 409) {
      saveLinkedDeviceAddition(null);
      showWhatsAppFeedback('whatsappError', error.message + ' Choose Link another number to start a new pairing.');
    } else showWhatsAppFeedback('whatsappError', error.message + ' Retry linking to resume this request.');
  } finally {
    state.linkedDeviceBusy = false;
    setButtonBusy($('addLinkedDeviceBtn'), false);
    if (state.linkedDeviceStatus) renderLinkedDeviceStatus(state.linkedDeviceStatus);
    else {
      $('addLinkedDeviceBtn').disabled = false;
      $('addLinkedDeviceBtn').textContent = state.linkedDeviceAdditionId ? 'Retry linking' : 'Link another number';
    }
    if (state.linkedDeviceAdditionId) {
      try { await loadWhatsAppRegistry(); }
      catch (_) { await loadLinkedDeviceStatus(); }
    } else await loadLinkedDeviceStatus();
  }
}

async function removeLinkedDeviceNumber(lineId) {
  if (!canManageWhatsApp() || state.linkedDeviceBusy || state.whatsappBusy) return;
  if (!(await confirmDialog({title: 'Remove and unlink number?', message: 'Remove and unlink this WhatsApp number completely? It will be signed out and removed from the app. Active conversations on this number will be closed; their chat history will be kept.', confirmLabel: 'Remove and unlink', tone: 'danger'}))) return;
  state.linkedDeviceBusy = true;
  state.linkedDeviceRequest++;
  clearWhatsAppFeedback();
  if (state.linkedDeviceStatus) renderLinkedDeviceStatus(state.linkedDeviceStatus);
  if (state.whatsappRegistry) renderWhatsAppRegistry(state.whatsappRegistry);
  try {
    const payload = await api('/api/support/whatsapp-linked-device/lines/' + encodeURIComponent(lineId), {
      method: 'DELETE', body: JSON.stringify({close_active_tickets: true}),
    });
    saveLinkedDeviceAddition(null);
    if (payload.registry) renderWhatsAppRegistry(payload.registry);
    renderLinkedDeviceStatus(payload);
    showWhatsAppFeedback('whatsappSuccess', 'WhatsApp number removed and unlinked. Its chat history remains available.');
  } catch (error) {
    showWhatsAppFeedback('whatsappError', error.message);
  } finally {
    state.linkedDeviceBusy = false;
    try { await loadWhatsAppRegistry(); }
    catch (_) {
      if (state.whatsappRegistry) renderWhatsAppRegistry(state.whatsappRegistry);
      if (state.linkedDeviceStatus) renderLinkedDeviceStatus(state.linkedDeviceStatus);
    }
  }
}

async function changeLinkedDeviceConnection(lineId, action) {
  if (!canManageWhatsApp() || state.linkedDeviceBusy) return;
  if (action === 'disconnect' && !(await confirmDialog({title: 'Unlink device?', message: 'Unlink this WhatsApp device? You will need to pair it again to use this connection.', confirmLabel: 'Unlink device', tone: 'danger'}))) return;
  state.linkedDeviceBusy = true;
  state.linkedDeviceRequest++;
  clearWhatsAppFeedback();
  try {
    const payload = await api('/api/support/whatsapp-linked-device/lines/' + encodeURIComponent(lineId) + '/' + action, {method: 'POST'});
    renderLinkedDeviceStatus(payload);
    if (action === 'disconnect') showWhatsAppFeedback('whatsappSuccess', 'WhatsApp device unlinked.');
  } catch (error) {
    showWhatsAppFeedback('whatsappError', error.message);
  } finally {
    state.linkedDeviceBusy = false;
    if (state.linkedDeviceStatus) renderLinkedDeviceStatus(state.linkedDeviceStatus);
  }
}

async function loadWhatsAppRegistry() {
  if (!canManageWhatsApp()) return;
  const payload = await api('/api/support/whatsapp-numbers');
  renderWhatsAppRegistry(payload);
  await loadLinkedDeviceStatus();
}

async function openWhatsAppModal() {
  if (!canManageWhatsApp() || state.whatsappBusy) return;
  clearWhatsAppFeedback();
  openDialog('whatsappModal', 'wabaIdInput');
  state.whatsappBusy = true;
  $('refreshWhatsAppBtn').disabled = true;
  try {
    await loadWhatsAppRegistry();
  } catch (error) {
    showWhatsAppFeedback('whatsappError', error.message);
  } finally {
    state.whatsappBusy = false;
    $('refreshWhatsAppBtn').disabled = false;
    if (state.whatsappRegistry) renderWhatsAppRegistry(state.whatsappRegistry);
  }
}

function closeWhatsAppModal() {
  if (state.whatsappBusy || state.linkedDeviceBusy) return;
  state.linkedDeviceRequest++;
  $('linkedDeviceLines').replaceChildren();
  state.linkedDeviceStatus = null;
  clearWhatsAppFeedback();
  closeDialog('whatsappModal');
}

async function syncWaba(wabaId) {
  if (!canManageWhatsApp() || state.whatsappBusy) return;
  const value = String(wabaId || '').trim();
  if (!/^[0-9]{5,64}$/.test(value)) {
    showWhatsAppFeedback('whatsappError', 'Enter a valid numeric WABA ID.');
    return;
  }
  state.whatsappBusy = true;
  clearWhatsAppFeedback();
  setButtonBusy($('syncWabaBtn'), true, 'Syncing…');
  try {
    const payload = await api('/api/support/whatsapp-accounts/sync', {method:'POST', body: JSON.stringify({waba_id: value})});
    renderWhatsAppRegistry(payload);
    $('wabaIdInput').value = '';
    showWhatsAppFeedback('whatsappSuccess', 'Meta account synced. New phone numbers remain disabled until you add them to the app.');
  } catch (error) {
    showWhatsAppFeedback('whatsappError', error.message);
  } finally {
    state.whatsappBusy = false;
    setButtonBusy($('syncWabaBtn'), false);
    if (state.whatsappRegistry) renderWhatsAppRegistry(state.whatsappRegistry);
  }
}

async function syncWabaForm(event) {
  event.preventDefault();
  if (!$('wabaSyncForm').reportValidity()) return;
  await syncWaba($('wabaIdInput').value);
}

async function setWhatsAppNumberState(phoneNumberId, enabled) {
  if (!canManageWhatsApp() || state.whatsappBusy || state.linkedDeviceBusy) return;
  if (!enabled && !(await confirmDialog({title: 'Remove number from app?', message: 'Remove this WhatsApp number from the chatbot and support app? Historical tickets will remain available.', confirmLabel: 'Remove from app', tone: 'danger'}))) return;
  state.whatsappBusy = true;
  clearWhatsAppFeedback();
  try {
    const payload = await api('/api/support/whatsapp-numbers/' + encodeURIComponent(phoneNumberId), {method:'PUT', body: JSON.stringify({enabled: Boolean(enabled)})});
    renderWhatsAppRegistry(payload);
    showWhatsAppFeedback('whatsappSuccess', enabled ? 'WhatsApp number added to the app.' : 'WhatsApp number removed from the app.');
  } catch (error) {
    showWhatsAppFeedback('whatsappError', error.message);
  } finally {
    state.whatsappBusy = false;
    if (state.whatsappRegistry) renderWhatsAppRegistry(state.whatsappRegistry);
  }
}

async function refreshWhatsAppRegistry() {
  if (!canManageWhatsApp() || state.whatsappBusy) return;
  state.whatsappBusy = true;
  clearWhatsAppFeedback();
  setButtonBusy($('refreshWhatsAppBtn'), true, 'Refreshing…');
  try {
    await loadWhatsAppRegistry();
  } catch (error) {
    showWhatsAppFeedback('whatsappError', error.message);
  } finally {
    state.whatsappBusy = false;
    setButtonBusy($('refreshWhatsAppBtn'), false);
    if (state.whatsappRegistry) renderWhatsAppRegistry(state.whatsappRegistry);
  }
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
  $('editStaffRole').disabled = false;
  $('editStaffActive').checked = true;
  $('editStaffActive').disabled = false;
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

async function confirmDiscardStaffChanges() {
  return !staffFormHasChanges() || await confirmDialog({title: 'Discard changes?', message: 'Discard the unsaved staff account changes?', confirmLabel: 'Discard changes', cancelLabel: 'Keep editing', tone: 'danger'});
}

async function closeStaffModal() {
  if (state.staffBusy || !(await confirmDiscardStaffChanges())) return;
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

async function backToStaffList() {
  if (state.staffBusy || !(await confirmDiscardStaffChanges())) return;
  clearStaffFeedback();
  resetCreateStaffForm();
  resetEditStaffForm();
  setStaffView('list');
  $('createStaffBtn').focus();
}

function beginStaffEdit(item) {
  if (!item) return;
  clearStaffFeedback();
  resetEditStaffForm();
  state.editingStaffId = String(item.id);
  state.editingStaffOriginal = {...item};
  $('editStaffName').value = item.name || '';
  $('editStaffEmail').value = item.email || '';
  $('editStaffRole').value = ['agent', 'admin', 'supervisor'].includes(item.role) ? item.role : 'agent';
  $('editStaffActive').checked = Boolean(item.is_active);
  const isSelf = String(item.id) === String(state.agent?.id);
  $('editStaffRole').disabled = isSelf;
  $('editStaffActive').disabled = isSelf;
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
    const isSelf = String(item.id) === String(state.agent?.id);
    const name = document.createElement('div');
    name.className = 'staff-name';
    const nameText = document.createElement('span');
    nameText.className = 'staff-name-text';
    nameText.textContent = item.name || 'Unnamed';
    nameText.title = item.name || '';
    name.append(nameText);
    if (isSelf) {
      const you = document.createElement('span');
      you.className = 'staff-you';
      you.textContent = 'You';
      name.append(you);
    }
    const created = document.createElement('div');
    created.className = 'staff-created';
    const createdAt = new Date(item.created_at);
    created.textContent = 'Created ' + (Number.isNaN(createdAt.getTime()) ? fmt(item.created_at) : new Intl.DateTimeFormat(undefined, {dateStyle: 'medium'}).format(createdAt));
    created.title = fmt(item.created_at);
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
    email.title = item.email || '';

    const role = document.createElement('span');
    role.className = 'role-badge';
    role.setAttribute('role', 'cell');
    role.textContent = item.role || '—';

    const active = document.createElement('span');
    active.className = 'status-dot' + (item.is_active ? '' : ' off');
    active.setAttribute('role', 'cell');
    active.textContent = item.is_active ? 'Active' : 'Deactivated';

    const actions = document.createElement('div');
    actions.className = 'staff-row-actions';
    actions.setAttribute('role', 'cell');
    const label = item.name || item.email || 'staff account';
    const edit = staffButton('Edit', () => beginStaffEdit(item), '', 'edit');
    edit.setAttribute('aria-label', (item.is_active ? 'Edit ' : 'Edit or reactivate ') + label);
    actions.append(edit);
    if (!isSelf) {
      const remove = staffButton('Delete', () => deleteStaffAccount(item), 'danger', 'trash');
      remove.setAttribute('aria-label', 'Delete ' + label);
      actions.append(remove);
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
    if (sessionRevoked) {
      await alertDialog({title: 'Sign in again', message: 'Your credentials changed, so this staff session was revoked. Please sign in again.', confirmLabel: 'Go to sign in', tone: 'warning'});
      showLogin();
    } else {
      await loadStaffAccounts();
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
  if (!(await confirmDialog({title: 'Delete staff account?', message: 'Delete ' + (item.name || item.email || 'this staff account') + '? The account is removed from the staff list, signed out everywhere, and cannot be reactivated. Past conversations keep its name.', confirmLabel: 'Delete account', tone: 'danger'}))) return;
  state.staffBusy = true;
  try {
    await api('/api/support/staff/' + encodeURIComponent(item.id), {method:'DELETE'});
    showStaffFeedback('staffSuccess', 'Deleted ' + (item.name || item.email) + '.');
    notify('Staff account deleted.');
    await loadStaffAccounts();
  } catch (error) {
    showStaffFeedback('staffError', error.message);
  } finally {
    state.staffBusy = false;
  }
}

function showStartChatError(message) {
  const element = $('startChatError');
  element.textContent = message || '';
  element.style.display = message ? 'block' : 'none';
}

async function openStartChatModal(contact = null) {
  if (!state.authenticated || state.chatBusy) return;
  showStartChatError('');
  $('startChatForm').reset();
  if (contact) {
    $('startChatPhone').value = contact.normalized_phone || contact.phone || '';
    $('startChatName').value = contact.display_name || contact.name || '';
  }
  $('startChatLine').replaceChildren(new Option('Loading WhatsApp lines…', ''));
  $('startChatLinePreview').replaceChildren();
  openDialog('startChatModal', contact ? 'startChatBody' : 'startChatPhone');
  try {
    const data = await api('/api/support/chat-lines');
    state.chatLines = Array.isArray(data.items) ? data.items : [];
    const select = $('startChatLine');
    select.replaceChildren();
    if (!state.chatLines.length) {
      select.append(new Option('No enabled WhatsApp lines', ''));
      $('startChatLinePreview').replaceChildren();
      showStartChatError('No WhatsApp line is enabled for outbound chats.');
      $('confirmStartChatBtn').disabled = true;
      return;
    }
    for (const line of state.chatLines) {
      const option = new Option(supportNumberLabel(line), line.phone_number_id);
      option.title = supportNumberRaw(line);
      select.append(option);
    }
    renderStartChatLinePreview();
    $('confirmStartChatBtn').disabled = false;
  } catch (error) {
    showStartChatError(error.message);
    $('confirmStartChatBtn').disabled = true;
  }
}

function renderStartChatLinePreview() {
  const select = $('startChatLine');
  const preview = $('startChatLinePreview');
  const line = (state.chatLines || []).find(item => String(item.phone_number_id) === String(select.value));
  if (!line) { preview.replaceChildren(); select.title = ''; return; }
  select.title = supportNumberRaw(line);
  preview.replaceChildren(document.createTextNode('Selected line: '), createSupportNumberLabel(line));
}

function closeStartChatModal() {
  if (state.chatBusy) return;
  showStartChatError('');
  closeDialog('startChatModal');
}

async function submitStartChat(event) {
  event.preventDefault();
  if (state.chatBusy || !$('startChatForm').reportValidity()) return;
  state.chatBusy = true;
  setButtonBusy($('confirmStartChatBtn'), true, 'Starting…');
  showStartChatError('');
  try {
    const data = await api('/api/support/chats', {method:'POST', body: JSON.stringify({
      phone_number: $('startChatPhone').value.trim(),
      business_phone_number_id: $('startChatLine').value,
      customer_name: $('startChatName').value.trim() || null,
      body: $('startChatBody').value.trim()
    })});
    closeDialog('startChatModal');
    setQueueView('chats', {load: false});
    state.page = 1;
    saveSupportSelection();
    await loadQueue();
    const chat = data.chat || data.ticket;
    if (chat && chat.id) await openTicket(chat.id, {conversationOrigin: 'STAFF'});
    notify(data.reopened ? 'Existing WhatsApp chat reopened with its full history.' : 'WhatsApp chat started.');
  } catch (error) {
    showStartChatError(error.message);
  } finally {
    state.chatBusy = false;
    setButtonBusy($('confirmStartChatBtn'), false);
  }
}

function renderGoogleContactsStatus(status) {
  state.googleContactsStatus = status || null;
  const connected = Boolean(status && status.connected);
  const configured = Boolean(status && status.configured);
  const account = status && status.account;
  text($('googleContactsTitle'), connected ? (account?.name || 'Google Contacts') : 'Google Contacts');
  text($('googleContactsStatus'), connected
    ? ((account?.email || 'Connected Google account') + (account?.last_synced_at ? ' · synced ' + relativeTime(account.last_synced_at) : ' · not synced yet'))
    : (configured ? 'Not connected' : 'Google OAuth is not configured on this server'));
  const manager = canManageGoogleContacts();
  $('googleContactsConnectBtn').classList.toggle('hidden', connected || !manager);
  $('googleContactsConnectBtn').disabled = state.googleContactsBusy || !configured;
  // Syncing only refreshes the already-connected shared account, so every
  // authenticated staff member may do it. Account ownership changes remain
  // Supervisor/Admin-only.
  $('googleContactsSyncBtn').classList.toggle('hidden', !connected);
  $('googleContactsDisconnectBtn').classList.toggle('hidden', !connected || !manager);
  $('googleContactsSyncBtn').disabled = state.googleContactsBusy;
  $('googleContactsDisconnectBtn').disabled = state.googleContactsBusy;
}

async function loadGoogleContactsStatus() {
  if (!state.authenticated) return;
  const status = await api('/api/support/google-contacts/status');
  renderGoogleContactsStatus(status);
}

async function connectGoogleContacts() {
  if (state.googleContactsBusy) return;
  state.googleContactsBusy = true;
  renderGoogleContactsStatus(state.googleContactsStatus || {configured: true, connected: false});
  try {
    const data = await api('/api/support/google-contacts/connect', {method:'POST'});
    const popup = window.open(data.authorization_url, 'googleContactsOAuth', 'popup=yes,width=560,height=720,resizable=yes,scrollbars=yes');
    if (!popup) throw new Error('Allow pop-ups for this support site to connect Google Contacts.');
    popup.focus();
  } catch (error) {
    notify(error.message, 'error');
  } finally {
    state.googleContactsBusy = false;
    renderGoogleContactsStatus(state.googleContactsStatus || {configured: true, connected: false});
  }
}

async function syncGoogleContacts() {
  if (state.googleContactsBusy) return;
  state.googleContactsBusy = true;
  renderGoogleContactsStatus(state.googleContactsStatus);
  try {
    const data = await api('/api/support/google-contacts/sync', {method:'POST'});
    notify('Google Contacts synced · ' + Number(data.contact_count || 0) + ' contacts available.');
    await Promise.all([loadGoogleContactsStatus(), refreshAllQueueCounts()]);
    if (state.view === 'contacts') await loadQueue({silent: true});
  } catch (error) {
    notify(error.message, 'error');
  } finally {
    state.googleContactsBusy = false;
    renderGoogleContactsStatus(state.googleContactsStatus);
  }
}

function chooseVcfContactsFile() {
  if (state.vcfImportBusy) return;
  $('vcfContactsInput').click();
}

async function importSelectedVcfContacts() {
  const input = $('vcfContactsInput');
  const file = input.files && input.files[0];
  if (!file || state.vcfImportBusy) return;
  if (!file.name.toLocaleLowerCase().endsWith('.vcf')) {
    input.value = '';
    notify('Choose a .vcf contacts file.', 'error');
    return;
  }
  if (file.size > 10 * 1024 * 1024) {
    input.value = '';
    notify('The VCF file is larger than the 10 MB import limit.', 'error');
    return;
  }

  state.vcfImportBusy = true;
  const button = $('importVcfContactsBtn');
  setButtonBusy(button, true, 'Importing…');
  try {
    const form = new FormData();
    form.append('file', file, file.name);
    const data = await api('/api/support/contacts/import-vcf', {method:'POST', body: form});
    const imported = Number(data.imported || 0);
    const updated = Number(data.updated || 0);
    const duplicates = Number(data.duplicates_removed || 0);
    const skipped = Number(data.skipped_without_phone || 0) + Number(data.skipped_invalid_phone || 0);
    let message = 'VCF imported · ' + imported + ' new contact' + (imported === 1 ? '' : 's');
    if (updated) message += ' · ' + updated + ' updated';
    if (duplicates) message += ' · ' + duplicates + ' duplicate' + (duplicates === 1 ? '' : 's') + ' merged';
    if (skipped) message += ' · ' + skipped + ' skipped';
    notify(message);
    await refreshAllQueueCounts();
    if (state.view === 'contacts') await loadQueue({silent: true});
  } catch (error) {
    notify(error.message, 'error');
  } finally {
    input.value = '';
    state.vcfImportBusy = false;
    setButtonBusy(button, false);
  }
}

async function disconnectGoogleContacts() {
  if (state.googleContactsBusy || !(await confirmDialog({title: 'Disconnect Google Contacts?', message: 'Disconnect Google Contacts and remove its synced contacts from this support account? Manually saved contacts will remain.', confirmLabel: 'Disconnect', tone: 'danger'}))) return;
  state.googleContactsBusy = true;
  renderGoogleContactsStatus(state.googleContactsStatus);
  try {
    await api('/api/support/google-contacts', {method:'DELETE'});
    notify('Google Contacts disconnected.');
    await Promise.all([loadGoogleContactsStatus(), refreshAllQueueCounts()]);
    if (state.view === 'contacts') await loadQueue({silent: true});
  } catch (error) {
    notify(error.message, 'error');
  } finally {
    state.googleContactsBusy = false;
    renderGoogleContactsStatus(state.googleContactsStatus);
  }
}

function forwardChoiceRow({kind, id, name, phone, subtitle, badgeLabel = null}) {
  const button = document.createElement('button');
  button.type = 'button';
  const key = kind + ':' + id;
  button.className = 'contact-picker-row' + (state.forwardSelection?.key === key ? ' selected' : '');
  const copy = document.createElement('div');
  const strong = document.createElement('strong');
  strong.dir = 'auto';
  strong.textContent = name || phone || 'Contact';
  const detail = document.createElement('span');
  detail.textContent = subtitle || phone || '';
  copy.append(strong, detail);
  const badge = document.createElement('span');
  badge.className = 'contact-source' + (badgeLabel === 'Google' ? ' google' : badgeLabel === 'VCF' ? ' vcf' : '');
  badge.textContent = badgeLabel || (kind === 'contact' ? 'Contact' : 'Conversation');
  button.append(copy, badge);
  button.onclick = () => {
    state.forwardSelection = {key, kind, id, name, phone};
    renderForwardChoices($('forwardSearch').value.trim());
    $('confirmForwardBtn').disabled = false;
  };
  return button;
}

function renderForwardChoices(query = '') {
  const normalized = String(query || '').trim().toLocaleLowerCase();
  const contactsContainer = $('forwardContactsList');
  const conversationsContainer = $('forwardConversationsList');
  contactsContainer.replaceChildren();
  conversationsContainer.replaceChildren();
  const contacts = (state.forwardContacts || []).filter(item => {
    if (!normalized) return true;
    return [item.display_name, item.normalized_phone, item.email].filter(Boolean).join(' ').toLocaleLowerCase().includes(normalized);
  });
  const conversations = (state.forwardConversations || []).filter(item => {
    if (!normalized) return true;
    return [item.customer_name, item.whatsapp_phone, item.ticket_reference].filter(Boolean).join(' ').toLocaleLowerCase().includes(normalized);
  });
  if (!contacts.length) contactsContainer.append(emptyMarkup('No matching contacts', 'Save a contact, sync Google Contacts, or import a VCF file.', 'users').outer);
  else for (const item of contacts) contactsContainer.append(forwardChoiceRow({kind:'contact', id:item.contact_key, name:item.display_name, phone:item.normalized_phone, subtitle:(item.email ? item.normalized_phone + ' · ' + item.email : item.normalized_phone), badgeLabel:item.source === 'google' ? 'Google' : item.source === 'vcf' ? 'VCF' : 'Saved'}));
  if (!conversations.length) conversationsContainer.append(emptyMarkup('No matching active conversations', 'You can still forward directly to a contact.', 'message').outer);
  else for (const item of conversations) conversationsContainer.append(forwardChoiceRow({kind:'conversation', id:Number(item.id), name:item.customer_name || item.whatsapp_phone, phone:item.whatsapp_phone, subtitle:item.ticket_reference || ('Conversation #' + item.id)}));
}

async function loadForwardChoices(query = '') {
  const requestId = ++state.forwardChoiceRequest;
  const [contacts, mine, chats] = await Promise.all([
    api('/api/support/contacts?page=1&page_size=100&q=' + encodeURIComponent(query)),
    api('/api/support/tickets?view=mine&page=1&page_size=100'),
    api('/api/support/tickets?view=chats&page=1&page_size=100')
  ]);
  if (requestId !== state.forwardChoiceRequest) return;
  state.forwardContacts = Array.isArray(contacts.items) ? contacts.items : [];
  const all = [...(mine.items || []), ...(chats.items || [])];
  const seen = new Set();
  state.forwardConversations = all.filter(item => {
    if (!item || seen.has(String(item.id))) return false;
    seen.add(String(item.id));
    return String(item.id) !== String(state.ticketId)
      && String(item.current_assignee_id || '') === String(state.agent && state.agent.id || '')
      && !['RESOLVED', 'CLOSED'].includes(String(item.status || ''))
      && Boolean(item.active_key);
  });
  renderForwardChoices(query);
}

function showForwardError(message) {
  const element = $('forwardError');
  element.textContent = message || '';
  element.style.display = message ? 'block' : 'none';
}

async function openForwardModal(message) {
  if (!message || !message.id || state.forwardBusy) return;
  state.forwardTarget = {ticketId: Number(state.ticketId), messageId: Number(message.id), preview: messagePreview(message)};
  state.forwardSelection = null;
  state.forwardContacts = [];
  state.forwardConversations = [];
  text($('forwardSubtitle'), 'Forward: ' + state.forwardTarget.preview);
  showForwardError('');
  $('forwardSearch').value = '';
  $('forwardContactsList').replaceChildren(emptyMarkup('Loading contacts', 'Loading saved and Google contacts…', 'users').outer);
  $('forwardConversationsList').replaceChildren(emptyMarkup('Loading conversations', 'Loading active conversations…', 'message').outer);
  $('confirmForwardBtn').disabled = true;
  openDialog('forwardModal', 'forwardSearch');
  try {
    await loadForwardChoices('');
  } catch (error) {
    showForwardError(error.message);
  }
}

function closeForwardModal() {
  if (state.forwardBusy) return;
  state.forwardTarget = null;
  state.forwardSelection = null;
  state.forwardChoiceRequest += 1;
  state.forwardContacts = [];
  state.forwardConversations = [];
  showForwardError('');
  closeDialog('forwardModal');
}

async function confirmForward() {
  if (state.forwardBusy || !state.forwardTarget || !state.forwardSelection) return;
  state.forwardBusy = true;
  setButtonBusy($('confirmForwardBtn'), true, 'Forwarding…');
  showForwardError('');
  try {
    if (state.forwardSelection.kind === 'conversation') {
      await api('/api/support/tickets/' + state.forwardTarget.ticketId + '/messages/' + state.forwardTarget.messageId + '/forward', {
        method:'POST', body: JSON.stringify({destination_ticket_id: Number(state.forwardSelection.id)})
      });
    } else {
      await api('/api/support/tickets/' + state.forwardTarget.ticketId + '/messages/' + state.forwardTarget.messageId + '/forward-contact', {
        method:'POST', body: JSON.stringify({phone_number: state.forwardSelection.phone, customer_name: state.forwardSelection.name || null})
      });
    }
    closeDialog('forwardModal');
    state.forwardTarget = null;
    state.forwardSelection = null;
    notify('Message forwarded.');
    refreshAllQueueCounts().catch(() => {});
  } catch (error) {
    showForwardError(error.message);
  } finally {
    state.forwardBusy = false;
    setButtonBusy($('confirmForwardBtn'), false);
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
  const chat = isStaffChat(data.ticket);
  text($('transferModalTitle'), chat ? 'Transfer chat' : 'Transfer ticket');
  text($('transferModalSubtitle'), chat ? 'Assign this chat to another staff member.' : 'Assign this conversation to another staff member.');
  $('confirmTransferBtn').lastChild.textContent = chat ? 'Transfer chat' : 'Transfer ticket';
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
  const chat = isStaffChat(state.currentTicketData?.ticket);
  if (!(await confirmDialog({title: 'Transfer ' + (chat ? 'chat' : 'ticket') + '?', message: 'Transfer this ' + (chat ? 'chat' : 'ticket') + ' to ' + target.name + '? You will become read-only unless it is transferred back to you.', confirmLabel: 'Transfer', tone: 'default'}))) return;
  state.transferBusy = true;
  setButtonBusy($('confirmTransferBtn'), true, 'Transferring…');
  showTransferError('');
  try {
    await api('/api/support/tickets/' + state.ticketId + '/transfer', {method:'POST', body: JSON.stringify({target_staff_id: targetId})});
    closeDialog('transferModal');
    state.transferTargets = [];
    notify((chat ? 'Chat' : 'Ticket') + ' transferred to ' + target.name + '.');
    await openTicket(state.ticketId, {silent: true, refreshQueue: true});
  } catch (error) {
    showTransferError(error.message);
  } finally {
    state.transferBusy = false;
    setButtonBusy($('confirmTransferBtn'), false);
  }
}

$('loginForm').addEventListener('submit', login);
$('sessionRetryBtn').addEventListener('click', bootstrap);
$('loginThemeBtn').addEventListener('click', toggleTheme);
$('themeBtn').addEventListener('click', toggleTheme);
$('logoutBtn').addEventListener('click', logout);
$('startChatBtn').addEventListener('click', () => openStartChatModal());
$('messagePreferencesBtn').addEventListener('click', openMessagePreferencesModal);
$('aiHeaderBtn').addEventListener('click', openAiSettingsModal);
$('manageStaffBtn').addEventListener('click', openStaffModal);
$('manageWhatsAppBtn').addEventListener('click', openWhatsAppModal);
$('googleContactsConnectBtn').addEventListener('click', connectGoogleContacts);
$('importVcfContactsBtn').addEventListener('click', chooseVcfContactsFile);
$('vcfContactsInput').addEventListener('change', importSelectedVcfContacts);
$('googleContactsSyncBtn').addEventListener('click', syncGoogleContacts);
$('googleContactsDisconnectBtn').addEventListener('click', disconnectGoogleContacts);
$('refreshQueueBtn').addEventListener('click', () => loadQueue());
$('previousPageBtn').addEventListener('click', () => changePage(-1));
$('nextPageBtn').addEventListener('click', () => changePage(1));
$('queue').addEventListener('keydown', handleQueueKeydown);
$('queueSearch').addEventListener('input', event => {
  state.query = event.target.value;
  if (state.view === 'contacts') {
    if (state.globalSearchTimer) clearTimeout(state.globalSearchTimer);
    state.globalSearchTimer = setTimeout(() => loadQueue({silent: true}), 220);
    return;
  }
  if (state.query.trim().length >= 2) {
    state.globalSearchBusy = true;
    renderQueue();
    scheduleGlobalMessageSearch();
  } else {
    state.globalSearchResults = [];
    state.globalSearchRequest += 1;
    if (state.globalSearchTimer) { clearTimeout(state.globalSearchTimer); state.globalSearchTimer = null; }
    renderQueue();
  }
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
$('contextToggleBtn').addEventListener('click', toggleContextPanel);
$('conversationSearchBtn').addEventListener('click', openConversationSearch);
$('conversationSearchClose').addEventListener('click', () => closeConversationSearch());
$('conversationSearchPrev').addEventListener('click', () => moveConversationSearch(-1));
$('conversationSearchNext').addEventListener('click', () => moveConversationSearch(1));
$('conversationSearchInput').addEventListener('input', scheduleConversationSearch);
$('conversationSearchInput').addEventListener('keydown', event => {
  if (event.key === 'Escape') {
    event.preventDefault();
    closeConversationSearch();
  } else if (event.key === 'Enter') {
    event.preventDefault();
    if (event.shiftKey) moveConversationSearch(-1);
    else moveConversationSearch(1);
  }
});
$('closeContextBtn').addEventListener('click', () => closeContextPanel());
$('contextBackdrop').addEventListener('click', () => closeContextPanel());
$('dismissErrorBtn').addEventListener('click', () => showError(''));
$('viewSharedHistoryBtn').addEventListener('click', jumpToSharedHistory);
$('scrollToLatestBtn').addEventListener('click', jumpToCurrentConversation);
$('conversation').addEventListener('scroll', updateScrollLatestButton, {passive: true});

$('imageBtn').addEventListener('click', chooseImage);
$('voiceBtn').addEventListener('click', toggleVoiceRecording);
$('locationBtn').addEventListener('click', openLocationModal);
$('cancelRecordingBtn').addEventListener('click', cancelVoiceRecording);
$('cancelReplyBtn').addEventListener('click', clearReplyTarget);
$('cancelEditBtn').addEventListener('click', () => cancelMessageEdit());
$('cancelMessageDeleteBtn').addEventListener('click', closeMessageDelete);
$('cancelMessageDeleteActionBtn').addEventListener('click', closeMessageDelete);
$('deleteMeBtn').addEventListener('click', () => deleteMessage('me'));
$('deleteEveryoneBtn').addEventListener('click', () => deleteMessage('everyone'));
$('imageInput').addEventListener('change', imageSelected);
$('sendBtn').addEventListener('click', sendMessage);
$('message').addEventListener('input', () => {
  updateComposerCount();
  resizeComposer();
  updateComposerControls();
});
$('message').addEventListener('keydown', event => {
  if (event.key === 'Escape' && state.editTarget) { event.preventDefault(); cancelMessageEdit(); return; }
  if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    sendMessage();
  }
});

$('closeMessagePreferencesBtn').addEventListener('click', closeMessagePreferencesModal);
$('cancelMessagePreferencesBtn').addEventListener('click', closeMessagePreferencesModal);
$('messagePreferencesForm').addEventListener('submit', saveMessagePreferences);
$('customGreetingMessage').addEventListener('input', updateMessagePreferenceCounts);
$('customResolutionMessage').addEventListener('input', updateMessagePreferenceCounts);

$('closeAiSettingsBtn').addEventListener('click', closeAiSettingsModal);
$('cancelAiSettingsBtn').addEventListener('click', closeAiSettingsModal);
$('aiSettingsForm').addEventListener('submit', saveAiSettings);
$('aiModeManual').addEventListener('change', syncAiSettingsFields);
$('aiModeScheduled').addEventListener('change', syncAiSettingsFields);

$('closeWhatsAppBtn').addEventListener('click', closeWhatsAppModal);
$('addLinkedDeviceBtn').addEventListener('click', addLinkedDeviceNumber);
$('refreshWhatsAppBtn').addEventListener('click', refreshWhatsAppRegistry);
$('wabaSyncForm').addEventListener('submit', syncWabaForm);
$('startChatLine').addEventListener('change', renderStartChatLinePreview);
$('closeLocationBtn').addEventListener('click', closeLocationModal);
$('cancelLocationBtn').addEventListener('click', closeLocationModal);
$('useCurrentLocationBtn').addEventListener('click', useCurrentLocation);
$('locationForm').addEventListener('submit', sendLocation);
$('closeMediaViewerBtn').addEventListener('click', closeMediaViewer);
$('editMediaBtn').addEventListener('click', enterImageEditor);
$('editorSendBtn').addEventListener('click', sendEditedImage);
$('editorSelectBtn').addEventListener('click', () => {
  if (!state.imageEditor) return;
  state.imageEditor.mode = 'select';
  $('editorSelectBtn').setAttribute('aria-pressed', 'true');
  $('editorDrawBtn').setAttribute('aria-pressed', 'false');
  $('editorTextBtn').setAttribute('aria-pressed', 'false');
});
$('editorDrawBtn').addEventListener('click', () => {
  if (!state.imageEditor) return;
  state.imageEditor.mode = 'draw';
  $('editorSelectBtn').setAttribute('aria-pressed', 'false');
  $('editorDrawBtn').setAttribute('aria-pressed', 'true');
  $('editorTextBtn').setAttribute('aria-pressed', 'false');
});
$('editorTextBtn').addEventListener('click', () => {
  if (!state.imageEditor) return;
  state.imageEditor.mode = 'text';
  $('editorSelectBtn').setAttribute('aria-pressed', 'false');
  $('editorDrawBtn').setAttribute('aria-pressed', 'false');
  $('editorTextBtn').setAttribute('aria-pressed', 'true');
});
$('editorUndoBtn').addEventListener('click', () => state.imageEditor?.undo());
$('editorRedoBtn').addEventListener('click', () => state.imageEditor?.redo());
$('editorEditTextBtn').addEventListener('click', () => state.imageEditor?.editText());
$('editorDeleteBtn').addEventListener('click', () => state.imageEditor?.removeSelected());
$('editorColor').addEventListener('change', () => state.imageEditor?.setStyle());
$('editorSize').addEventListener('change', () => state.imageEditor?.setStyle());
$('zoomOutBtn').addEventListener('click', () => changeMediaZoom(-0.25));
$('zoomInBtn').addEventListener('click', () => changeMediaZoom(0.25));
$('zoomResetBtn').addEventListener('click', resetMediaZoom);
$('mediaViewerModal').addEventListener('keydown', event => {
  if (event.key !== 'Enter' || event.shiftKey || event.isComposing || event.repeat ||
      !state.imageEditor || event.target.closest('button, textarea, select')) return;
  event.preventDefault();
  sendEditedImage();
});
document.addEventListener('keydown', event => {
  if (event.code !== 'Space' || !state.imageEditor ||
      event.target.closest('input, textarea, button, select')) return;
  window.mediaEditorSpaceDown = true;
  event.preventDefault();
});
window.addEventListener('keyup', event => {
  if (event.code === 'Space') window.mediaEditorSpaceDown = false;
});
window.addEventListener('blur', () => { window.mediaEditorSpaceDown = false; });
$('mediaViewerBody').addEventListener('wheel', event => {
  if (state.activeModal !== 'mediaViewerModal' || $('mediaViewerTools').classList.contains('hidden')) return;
  event.preventDefault();
  zoomMediaAt(state.mediaZoom * Math.exp(-event.deltaY * 0.0015), event.clientX, event.clientY);
}, {passive: false});
const mediaPointers = new Map();
let lastPinch = null;
const viewerBody = $('mediaViewerBody');
viewerBody.addEventListener('pointerdown', event => {
  if (!state.mediaSource) return;
  const editor = state.imageEditor;
  const navigating = editor && (event.button === 1 || window.mediaEditorSpaceDown ||
    (editor.mode === 'select' && !editor.pointer && state.mediaZoom > 1));
  if (editor && !navigating && event.pointerType !== 'touch') return;
  mediaPointers.set(event.pointerId, {x: event.clientX, y: event.clientY});
  if (mediaPointers.size === 2 && editor) {
    editor.cancelActiveStroke();
    for (const pointerId of mediaPointers.keys()) viewerBody.setPointerCapture(pointerId);
  } else if (navigating || !editor) viewerBody.setPointerCapture(event.pointerId);
  if (mediaPointers.size === 2) lastPinch = null;
  if (state.mediaZoom > 1) viewerBody.classList.add('panning');
  if (navigating || mediaPointers.size > 1 || !editor) event.preventDefault();
});
viewerBody.addEventListener('pointermove', event => {
  if (!mediaPointers.has(event.pointerId)) return;
  if (state.imageEditor?.pointer && mediaPointers.size === 1) return;
  const previous = mediaPointers.get(event.pointerId);
  mediaPointers.set(event.pointerId, {x: event.clientX, y: event.clientY});
  if (mediaPointers.size === 1 && state.mediaZoom > 1) {
    state.mediaTranslateX += event.clientX - previous.x;
    state.mediaTranslateY += event.clientY - previous.y;
    applyMediaZoom();
  } else if (mediaPointers.size === 2) {
    const [a, b] = [...mediaPointers.values()];
    const now = {distance: Math.hypot(a.x - b.x, a.y - b.y), x: (a.x + b.x) / 2, y: (a.y + b.y) / 2};
    if (lastPinch) {
      zoomMediaAt(state.mediaZoom * now.distance / Math.max(1, lastPinch.distance), lastPinch.x, lastPinch.y);
      state.mediaTranslateX += now.x - lastPinch.x;
      state.mediaTranslateY += now.y - lastPinch.y;
      applyMediaZoom();
    }
    lastPinch = now;
  }
  event.preventDefault();
});
for (const name of ['pointerup', 'pointercancel', 'lostpointercapture']) viewerBody.addEventListener(name, event => {
  mediaPointers.delete(event.pointerId);
  lastPinch = null;
  viewerBody.classList.remove('panning');
});
viewerBody.addEventListener('dblclick', event => {
  if (!state.mediaSource || state.imageEditor?.selected >= 0) return;
  zoomMediaAt(state.mediaZoom > 1 ? 1 : 2, event.clientX, event.clientY);
});
window.addEventListener('resize', () => {
  state.imageEditor?.resize();
  applyMediaZoom();
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

$('closeStartChatBtn').addEventListener('click', closeStartChatModal);
$('cancelStartChatBtn').addEventListener('click', closeStartChatModal);
$('startChatForm').addEventListener('submit', submitStartChat);
$('closeForwardBtn').addEventListener('click', closeForwardModal);
$('cancelForwardBtn').addEventListener('click', closeForwardModal);
$('confirmForwardBtn').addEventListener('click', confirmForward);
$('forwardSearch').addEventListener('input', event => {
  const query = event.target.value.trim();
  if (state.forwardSearchTimer) clearTimeout(state.forwardSearchTimer);
  renderForwardChoices(query);
  state.forwardSearchTimer = setTimeout(() => {
    loadForwardChoices(query).catch(error => showForwardError(error.message));
  }, 220);
});

$('closeTransferBtn').addEventListener('click', closeTransferModal);
$('cancelTransferBtn').addEventListener('click', closeTransferModal);
$('confirmTransferBtn').addEventListener('click', confirmTransfer);
$('confirmForm').addEventListener('submit', event => { event.preventDefault(); settleConfirmDialog(true); });
$('confirmCancelBtn').addEventListener('click', () => settleConfirmDialog(false));
$('shortcutsHelpBtn').addEventListener('click', openShortcutsHelp);
$('closeShortcutsBtn').addEventListener('click', closeShortcutsHelp);
$('passwordToggleBtn').addEventListener('click', () => {
  const input = $('password');
  const show = input.type === 'password';
  input.type = show ? 'text' : 'password';
  const button = $('passwordToggleBtn');
  const label = show ? 'Hide password' : 'Show password';
  button.setAttribute('aria-pressed', show ? 'true' : 'false');
  button.setAttribute('aria-label', label);
  button.dataset.tooltip = label;
  input.focus();
});

document.addEventListener('keydown', handleGlobalKeydown);
document.addEventListener('copy', handleConversationCopy);
document.addEventListener('selectionchange', flushPendingConversationRender);
$('conversation').addEventListener('pointerdown', beginConversationSelection);
window.addEventListener('pointerup', finishConversationSelection);
window.addEventListener('pointercancel', finishConversationSelection);
window.addEventListener('blur', () => {
  state.conversationSelectionPointer = null;
  flushPendingConversationRender();
});
document.addEventListener('pointerdown', unlockNotificationAudio);
document.addEventListener('click', event => {
  for (const menu of document.querySelectorAll('.action-menu[open]')) {
    if (!menu.contains(event.target)) menu.open = false;
  }
});
window.addEventListener('message', event => {
  if (event.origin !== window.location.origin || !event.data || typeof event.data !== 'object') return;
  if (event.data.type === 'google_contacts_connected') {
    notify(event.data.message || 'Google Contacts connected.');
    Promise.all([loadGoogleContactsStatus(), refreshAllQueueCounts()]).then(() => {
      if (state.view === 'contacts') loadQueue({silent: true});
    }).catch(() => {});
  } else if (event.data.type === 'google_contacts_error') {
    notify(event.data.message || 'Google Contacts connection failed.', 'error');
  }
});
window.addEventListener('resize', syncContextLayout);
window.addEventListener('beforeunload', () => {
  markPageLeaving();
  stopMediaTracks();
});
window.addEventListener('pagehide', markPageLeaving);
window.addEventListener('pageshow', event => {
  state.pageLeaving = false;
  if (event.persisted && state.authenticated) {
    if (state.ticketId) openTicket(state.ticketId, {silent: true}).catch(() => {});
    else loadQueue({silent: true}).catch(() => {});
  }
});

document.addEventListener('visibilitychange', () => {
  if (!document.hidden) clearAttentionIndicator();
  if (!document.hidden && state.authenticated && !state.busy && !state.activeModal) {
    if (state.ticketId && !state.conversationSearchOpen) openTicket(state.ticketId, {silent: true});
    else if (!state.ticketId) loadQueue({silent: true});
  }
});

restoreLayoutPreferences();
installColumnResizer({id: 'queueResizer', variableName: '--queue-width', storageKey: LAYOUT_STORAGE.queueWidth, minimum: 260, maximum: 520, direction: 1, enabled: () => window.innerWidth > 800});
installColumnResizer({id: 'contextResizer', variableName: '--context-width', storageKey: LAYOUT_STORAGE.contextWidth, minimum: 260, maximum: 520, direction: -1, enabled: () => window.innerWidth > 1100 && state.contextOpen});
updateComposerCount();
updateMessagePreferenceCounts();
updateComposerControls();
updateQueueSummary();
bootstrap();
setInterval(() => {
  if (!state.authenticated || state.busy || state.mediaRecorder || state.activeModal || state.conversationSearchOpen) return;
  const audioPlaying = [...document.querySelectorAll('#conversation audio')].some(audio => !audio.paused && !audio.ended);
  if (state.ticketId && !audioPlaying) openTicket(state.ticketId, {silent: true});
  else if (!state.ticketId) loadQueue({silent: true});
}, Math.max(3000, __POLL_MS__));
setInterval(() => {
  if (state.authenticated && state.activeModal === 'whatsappModal' && !state.whatsappBusy && !state.linkedDeviceBusy) loadLinkedDeviceStatus();
}, 2000);
setInterval(() => {
  if (!state.authenticated || state.busy || state.activeModal) return;
  pollUnassignedActivity();
}, Math.max(2000, __POLL_MS__));
setInterval(() => {
  if (!state.authenticated) return;
  refreshAllQueueCounts();
}, Math.max(5000, __POLL_MS__));
setInterval(() => {
  if (!state.authenticated || state.aiSettingsBusy || document.hidden) return;
  loadAiSettings().catch(error => renderAiSettingsUnavailable(error.message));
}, Math.max(30000, __POLL_MS__));
</script>
</body>
</html>'''
