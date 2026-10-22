from __future__ import annotations

import hashlib
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, Response

from app.config import settings

router = APIRouter(tags=["support-ui"])
_ROOT = Path(__file__).parent
_ASSET_ROOT = _ROOT / "static" / "support"
_SCRIPT_NAMES = ("state.js", "events.js", "dialogs.js", "api.js", "statistics.js", "rendering.js", "media.js", "actions.js")
_ASSETS = {name: (_ASSET_ROOT / name).read_text(encoding="utf-8")
           for name in ("theme.js", "support.css", *_SCRIPT_NAMES)}
_TEMPLATE = (_ROOT / "templates" / "support.html").read_text(encoding="utf-8")
_EDITOR = (_ROOT / "support_image_editor.js").read_text(encoding="utf-8")

# Include every shipped asset so the existing deploy/reload handshake remains valid.
SUPPORT_UI_VERSION = hashlib.sha256(
    (_TEMPLATE + "".join(_ASSETS.values()) + _EDITOR).encode("utf-8")
).hexdigest()[:16]


def _configure(source: str) -> str:
    return (source.replace("__POLL_MS__", str(max(1, settings.support_queue_poll_seconds) * 1000))
            .replace("__CSRF_COOKIE__", settings.support_csrf_cookie_name)
            .replace("__VIDEO_MAX_BYTES__", str(max(1, settings.support_video_max_bytes)))
            .replace("__IMAGE_MAX_BYTES__", str(max(1, settings.support_image_max_bytes)))
            .replace("__DOCUMENT_MAX_BYTES__", str(max(1, settings.support_document_max_bytes)))
            .replace("__STICKER_MAX_BYTES__", str(max(1, settings.support_sticker_max_bytes)))
            .replace("__UI_VERSION__", SUPPORT_UI_VERSION))


def _asset_url(name: str) -> str:
    return f"/support/assets/{name}?v={SUPPORT_UI_VERSION}"


@router.get("/support", response_class=HTMLResponse)
async def support_dashboard() -> HTMLResponse:
    if not settings.support_ui_enabled:
        return HTMLResponse("Support UI is disabled.", status_code=404)
    html = (_TEMPLATE.replace("__SUPPORT_STYLE__", f'<link rel="stylesheet" href="{_asset_url("support.css")}" />')
            .replace("__SUPPORT_THEME__", f'<script src="{_asset_url("theme.js")}"></script>')
            .replace("__SUPPORT_SCRIPTS__", "\n".join(
                f'<script src="{_asset_url(name)}"></script>' for name in _SCRIPT_NAMES)))
    return HTMLResponse(_configure(html), headers={
        "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0", "Pragma": "no-cache",
    })


@router.get("/support/assets/{asset_name}")
async def support_asset(asset_name: str) -> Response:
    # An explicit allowlist prevents filesystem traversal and accidental exposure.
    if not settings.support_ui_enabled or asset_name not in _ASSETS:
        return Response(status_code=404)
    mime = "text/css" if asset_name.endswith(".css") else "text/javascript"
    return Response(_configure(_ASSETS[asset_name]), media_type=mime,
                    headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})


@router.get("/support/image-editor.js")
async def support_image_editor_script() -> Response:
    if not settings.support_ui_enabled:
        return Response(status_code=404)
    script = _EDITOR.replace("__IMAGE_MAX_BYTES__", str(max(1, settings.support_image_max_bytes)))
    return Response(script, media_type="text/javascript", headers={"Cache-Control": "no-store"})


# Source view retained for existing DOM regression tests and tooling. The browser
# receives the template and external assets; this is assembled from the same files.
_HTML = (_TEMPLATE.replace("__SUPPORT_STYLE__", "<style>\n" + _ASSETS["support.css"] + "</style>")
         .replace("__SUPPORT_THEME__", "<script>\n" + _ASSETS["theme.js"] + "</script>")
         .replace("__SUPPORT_SCRIPTS__", "<script>\n" + "\n".join(_ASSETS[name] for name in _SCRIPT_NAMES) + "</script>"))
