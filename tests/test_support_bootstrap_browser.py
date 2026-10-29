"""Load the complete dashboard in Chrome, with native browser timers and fake APIs."""
import asyncio
import json
import os
from pathlib import Path
import shutil
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Event, Thread
from urllib.parse import urlsplit

import pytest


@pytest.mark.parametrize('authenticated', [False, True])
def test_complete_dashboard_session_and_scheduler_in_browser(tmp_path: Path, monkeypatch, authenticated):
    from app import support_ui
    monkeypatch.setattr(support_ui.settings, 'support_ui_enabled', True)
    browser = shutil.which(os.environ.get('SUPPORT_TEST_BROWSER', 'chromium'))
    assert browser, 'Install the configured Chrome/Chromium browser'
    page = asyncio.run(support_ui.support_dashboard()).body.decode()
    # Only transport is mocked. Scripts, DOM, timers, login cleanup, scheduler
    # and bootstrap all run as shipped; Node timers do not enforce Window receivers.
    setup = r'''<script>
window.browserErrors=[];
window.addEventListener('error',event=>browserErrors.push(event.message));
window.addEventListener('unhandledrejection',event=>browserErrors.push(String(event.reason)));
class TestEventSource { close() {} }
window.EventSource=TestEventSource;
</script>'''
    checks = r'''<script>
(async()=>{try{
  await bootstrap();
  const check=(condition,message)=>{if(!condition)throw Error(message)};
  check(browserErrors.length===0,browserErrors.join('; '));
  check($('sessionLoading').classList.contains('hidden'),'Session loading remained visible');
  if(AUTHENTICATED){
    check(!$('dashboard').classList.contains('hidden'),'Dashboard not visible');
    check(dashboardRefresh.started && dashboardRefresh.timer!==null,'Native timer was not armed');
    dashboardRefresh.request('queue','counts');
    document.dispatchEvent(new Event('visibilitychange'));
    check(dashboardRefresh.timer!==null,'Visibility change lost timer');
    try { await api('/api/support/me?expired=1'); throw Error('Expired session accepted'); }
    catch(error){check(error.status===401,'Expired-session cleanup failed: '+error.message)}
  }
  check(!$('loginPage').classList.contains('hidden'),'Sign-in form not visible');
  check($('dashboard').classList.contains('hidden'),'Signed-out dashboard remained visible');
  check(!dashboardRefresh.started && dashboardRefresh.timer===null,'Logout left polling active');
  check(!state.authenticated,'Expired session remained authenticated');
  check(browserErrors.length===0,browserErrors.join('; '));
  await fetch('/test-result',{method:'POST',body:JSON.stringify({passed:true})});
}catch(error){await fetch('/test-result',{method:'POST',body:JSON.stringify({error:String(error),stack:error.stack})})}})();
</script>'''.replace('AUTHENTICATED', 'true' if authenticated else 'false')
    page = page.replace('</head>', setup+'</head>').replace('</body>', checks+'</body>')
    finished, results = Event(), []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            path = urlsplit(self.path).path
            status, mime = 200, 'application/json'
            if path == '/support':
                content, mime = page.encode(), 'text/html'
            elif path.startswith('/support/assets/'):
                response = asyncio.run(support_ui.support_asset(path.rsplit('/', 1)[-1]))
                content, mime, status = response.body, response.headers['content-type'], response.status_code
            elif path == '/api/support/me':
                status = 200 if authenticated and 'expired=1' not in self.path else 401
                content = json.dumps({'id':'7','name':'Test staff','role':'agent'} if status == 200
                                     else {'detail':'Authentication required.'}).encode()
            elif path == '/api/support/tickets':
                content = b'{"items":[],"total":0,"page":1,"page_size":30}'
            elif path == '/api/support/queue-counts':
                content = b'{"counts":{"unassigned":0,"mine":0,"chats":0}}'
            else:
                content = b'{}'
            self.send_response(status)
            self.send_header('Content-Type', mime)
            self.end_headers()
            self.wfile.write(content)

        def do_POST(self):
            body = self.rfile.read(int(self.headers.get('Content-Length', 0)))
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(b'{}')
            if self.path == '/test-result':
                results.append(json.loads(body))
                finished.set()

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    process = subprocess.Popen([browser, '--headless', '--no-sandbox', '--disable-gpu',
        '--disable-dev-shm-usage', '--disable-background-networking', '--no-first-run',
        '--user-data-dir='+str(tmp_path/'profile'), f'http://127.0.0.1:{server.server_port}/support'],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        assert finished.wait(20), 'Dashboard did not complete bootstrap'
        assert results == [{'passed': True}], results
    finally:
        process.terminate()
        process.wait(5)
        server.shutdown()
        server.server_close()
        thread.join(5)
