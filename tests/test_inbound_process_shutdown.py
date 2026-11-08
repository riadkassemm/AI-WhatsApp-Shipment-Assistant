"""Real ASGI shutdown on a Unix socket; synthetic messages and no providers."""
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time

import httpx


def test_sigterm_drains_worker_after_uvicorn_cancels_request(tmp_path):
    source = '''
import asyncio
from pathlib import Path
from fastapi import FastAPI
from app import main
from app.conversation_store import InMemoryConversationStore
from app.whatsapp_client import IncomingWhatsAppMessage
root=Path(__file__).parent
main.settings.support_ui_enabled=False
main.settings.support_phone_app_idle_resolve_minutes=0
main.settings.whatsapp_message_burst_debounce_seconds=0
store=InMemoryConversationStore()
main.conversation_store=store
async def no_ticket(*args, **kwargs): return None
main.support_service.get_active_ticket=no_ticket
async def process(incoming):
    (root/'entered').touch()
    while not (root/'finish').exists(): await asyncio.sleep(.02)
    (root/'completed').touch()
    return main._json_response(200, {'status':'ok'})
main._process_incoming_message=process
app=FastAPI(lifespan=main._lifespan)
@app.get('/fixture')
async def fixture():
    message=IncomingWhatsAppMessage('15550000001','fixture','text','synthetic','1')
    token=await store.claim_incoming_message('fixture')
    await store.enqueue_incoming_work(message.from_number,main._queued_inbound_item(message,token))
    try: return await main._drive_inbound_queue(message.from_number)
    except asyncio.CancelledError:
        (root/'request-cancelled').touch()
        raise
'''
    (tmp_path / 'shutdown_fixture.py').write_text(source)
    sock = tmp_path / 'http.sock'
    env = {**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[1]), 'OPENAI_API_KEY': 'fixture-only'}
    with (tmp_path / 'process.log').open('w') as log:
        process = subprocess.Popen([sys.executable, '-m', 'uvicorn', 'shutdown_fixture:app',
                                    '--app-dir', str(tmp_path), '--uds', str(sock),
                                    '--timeout-graceful-shutdown', '0'], env=env, cwd=tmp_path, stdout=log, stderr=log)
        def wait_for(name):
            deadline = time.monotonic() + 20
            while not (tmp_path / name).exists():
                assert process.poll() is None, 'Synthetic API exited unexpectedly'
                assert time.monotonic() < deadline, 'Synthetic shutdown milestone timed out'
                time.sleep(.02)
        def request():
            try:
                with httpx.Client(transport=httpx.HTTPTransport(uds=str(sock)), timeout=15) as client:
                    client.get('http://fixture/fixture')
            except httpx.HTTPError:
                pass
        thread = None
        try:
            wait_for('http.sock')
            thread = threading.Thread(target=request)
            thread.start()
            wait_for('entered')
            process.send_signal(signal.SIGTERM)
            wait_for('request-cancelled')
            assert process.poll() is None
            (tmp_path / 'finish').touch()
            # Uvicorn re-raises its captured SIGTERM after a successful shutdown.
            assert process.wait(timeout=20) in (0, -signal.SIGTERM)
            assert (tmp_path / 'completed').exists()
            assert 'Application shutdown complete.' in (tmp_path / 'process.log').read_text()
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            if thread is not None:
                thread.join(timeout=20)
