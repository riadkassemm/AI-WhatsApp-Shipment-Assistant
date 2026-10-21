import asyncio
import re
from pathlib import Path

from app import support_ui


def test_dashboard_loads_allowlisted_assets_in_order(monkeypatch):
    monkeypatch.setattr(support_ui.settings, 'support_ui_enabled', True)
    html = asyncio.run(support_ui.support_dashboard()).body.decode()
    urls = re.findall(r'(?:src|href)="(/support/assets/[^\"]+)"', html)
    assert len(urls) == len(support_ui._ASSETS)
    assert [url.split('/')[-1].split('?')[0] for url in urls] == [
        'theme.js', 'support.css', *support_ui._SCRIPT_NAMES]
    for url in urls:
        name = url.split('/')[-1].split('?')[0]
        response = asyncio.run(support_ui.support_asset(name))
        assert response.status_code == 200
        assert '__UI_VERSION__' not in response.body.decode()
        assert '__POLL_MS__' not in response.body.decode()
        assert response.headers['cache-control'] == 'no-store'
        assert response.headers['x-content-type-options'] == 'nosniff'
    assert '<style>' not in html and '<script>\nconst ' not in html
    assert '__SUPPORT_' not in html
    assert asyncio.run(support_ui.support_asset('../config.py')).status_code == 404
    monkeypatch.setattr(support_ui.settings, 'support_ui_enabled', False)
    assert asyncio.run(support_ui.support_asset('state.js')).status_code == 404
    assert asyncio.run(support_ui.support_dashboard()).status_code == 404


def test_source_view_uses_all_external_files():
    root = Path(support_ui.__file__).parent / 'static' / 'support'
    for name in support_ui._SCRIPT_NAMES:
        assert (root / name).read_text() in support_ui._HTML
