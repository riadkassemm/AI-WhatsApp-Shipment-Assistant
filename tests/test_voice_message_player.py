"""Voice playback regressions with the shipped component, CSS and native DOM.

Media events are controlled for races and errors; a separate smoke test plays a
short synthetic WAV through the browser's real decoder. No customer APIs run.
"""

import io
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import shutil
import subprocess
from threading import Event, Thread
from urllib.parse import urlsplit
import wave

import pytest

from app.support_ui import _ASSETS


_HARNESS = r"""
const check = (condition, message) => {if (!condition) throw Error(message)};
const settle = async () => {for (let i = 0; i < 4; i++) await Promise.resolve()};
const fixture = document.getElementById('fixture');
function note(source = '/synthetic.wav', options = {}, attach = true, label = 'Voice note from Customer') {
  const result = buildVoiceMessagePlayer(source, label, options);
  const {player, audio} = result;
  for (const bar of player.querySelectorAll('.voice-wave-bar')) bar.style.transition = 'none';
  const media = {duration: NaN, currentTime: 0, paused: true, ended: false, readyState: 0, error: null,
    loads: 0, plays: 0, pauses: 0, mode: 'success'};
  for (const property of ['duration', 'currentTime', 'paused', 'ended', 'readyState', 'error']) {
    Object.defineProperty(audio, property, {configurable: true,
      get: () => media[property], set: value => {media[property] = value}});
  }
  const emit = name => audio.dispatchEvent(new Event(name));
  // Synthetic PointerEvents do not establish a browser-owned pointer capture.
  const captured = new Set();
  const waveform = player.querySelector('.voice-waveform');
  waveform.setPointerCapture = id => captured.add(id);
  waveform.hasPointerCapture = id => captured.has(id);
  waveform.releasePointerCapture = id => captured.delete(id);
  audio.load = () => {media.loads++};
  audio.pause = () => {media.pauses++; if (!media.paused) {media.paused = true; emit('pause')}};
  audio.play = () => {
    media.plays++;
    if (media.mode === 'reject') return Promise.reject(new DOMException('Synthetic failure', 'NotAllowedError'));
    if (media.mode === 'pending') return new Promise(resolve => {
      media.finishPlay = () => {media.paused = false; emit('play'); emit('playing'); resolve()};
    });
    media.paused = false; media.ended = false; emit('play'); emit('playing');
    return Promise.resolve();
  };
  const metadata = duration => {media.duration = duration; media.readyState = 4; emit('loadedmetadata'); emit('durationchange')};
  if (attach) fixture.append(player);
  return {...result, media, emit, metadata, toggle: player.querySelector('.voice-toggle-btn'),
    speed: player.querySelector('.voice-speed-btn'), waveform};
}
function key(target, name) {
  const event = new KeyboardEvent('keydown', {key: name, bubbles: true, cancelable: true});
  target.dispatchEvent(event);
  return event;
}
function pointer(target, name, fraction, pointerId = 1) {
  const bounds = target.getBoundingClientRect();
  target.dispatchEvent(new PointerEvent(name, {pointerId, pointerType: 'mouse', button: 0,
    buttons: name === 'pointerup' ? 0 : 1, clientX: bounds.left + fraction * bounds.width,
    clientY: bounds.top + bounds.height / 2, bubbles: true, cancelable: true}));
}
"""


def _browser_check(tmp_path: Path, checks: str, *, width: int = 1280, screenshot: bool = False) -> None:
    configured = os.environ.get('SUPPORT_TEST_BROWSER')
    browser = shutil.which(configured) if configured else next(
        (path for name in ('google-chrome', 'google-chrome-stable', 'chromium')
         if (path := shutil.which(name))), None,
    )
    if configured:
        assert browser, f'Configured SUPPORT_TEST_BROWSER is unavailable: {configured}'
    if not browser:
        pytest.skip('Chrome/Chromium is unavailable')
    content = io.BytesIO()
    with wave.open(content, 'wb') as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(8000)
        wav.writeframes(b'\0\0' * 8000)
    synthetic_audio = content.getvalue()
    script = _ASSETS['voice-player.js'] + _HARNESS + """
(async () => {try {
""" + checks + """
  await fetch('/test-result', {method: 'POST', body: JSON.stringify({passed: true})});
} catch (error) {
  await fetch('/test-result', {method: 'POST', body: JSON.stringify({error: String(error), stack: error.stack})});
}})();
"""
    html = ("<!doctype html><html data-theme='dark'><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width, initial-scale=1'><style>"
            + _ASSETS['support.css']
            + "#fixture {width: calc(100% - 32px); max-width: " + str(width - 32) + "px; margin: 16px auto;}"
            + "</style></head><body><main id='fixture'></main><script>" + script + "</script></body></html>")
    finished, results = Event(), []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            is_audio = urlsplit(self.path).path == '/synthetic.wav'
            self.send_response(200)
            self.send_header('Content-Type', 'audio/wav' if is_audio else 'text/html; charset=utf-8')
            body = synthetic_audio if is_audio else html.encode()
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            results.append(json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0)))))
            self.send_response(200)
            self.end_headers()
            finished.set()

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.daemon_threads = True
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    command = [browser, '--headless', '--no-sandbox', '--disable-gpu', '--disable-dev-shm-usage',
               '--no-first-run', '--disable-background-networking', '--autoplay-policy=no-user-gesture-required',
               '--mute-audio', f'--window-size={width},900', '--user-data-dir=' + str(tmp_path / 'profile')]
    screenshot_path = Path(os.environ['SUPPORT_VOICE_SCREENSHOT']) if screenshot and os.environ.get('SUPPORT_VOICE_SCREENSHOT') else None
    if screenshot_path:
        command.append('--screenshot=' + str(screenshot_path))
    process = subprocess.Popen(
        [*command, f'http://127.0.0.1:{server.server_port}/'],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        assert finished.wait(20), 'Voice message browser checks did not finish'
        assert results == [{'passed': True}], results
        if screenshot_path:
            # Headless Chrome writes its screenshot after the page's checks report.
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass
            assert screenshot_path.is_file(), 'Browser did not save the requested screenshot'
    finally:
        process.terminate()
        process.wait(5)
        server.shutdown()
        server.server_close()
        thread.join(5)


def test_voice_playback_seeking_duration_and_speed(tmp_path: Path) -> None:
    _browser_check(tmp_path, r"""
const first = note();
await settle();
check(first.audio.getAttribute('src') === '/synthetic.wav', 'Original media URL changed');
check(!first.audio.controls && getComputedStyle(first.audio).display === 'none', 'Native controls remain visible');
check(first.waveform.getAttribute('role') === 'slider', 'Waveform lacks slider semantics');
check(first.waveform.getAttribute('aria-disabled') === 'true', 'Unknown duration permits seeking');
check(!first.player.textContent.includes('NaN') && !first.player.textContent.includes('Infinity'), 'Unknown duration leaked');
const shapes = entry => [...entry.player.querySelectorAll('.voice-wave-bar')].map(bar => bar.style.height);
const originalShape = shapes(first);
check(originalShape.length === 40 && new Set(originalShape).size > 5, 'Waveform lacks variation');
const duplicate = note();
check(JSON.stringify(shapes(duplicate)) === JSON.stringify(originalShape), 'Waveform changes on rerender');
const peaks = note('/synthetic.wav?peaks', {peaks: Array.from({length: 40}, (_, i) => i / 39)});
check(parseFloat(shapes(peaks)[0]) < parseFloat(shapes(peaks)[39]), 'Available waveform peaks ignored');

first.metadata(125.9);
check(first.player.querySelector('.voice-duration').textContent === '2:05', 'Duration format');
check(first.player.querySelector('.voice-current-time').textContent === '0:00', 'Initial time');
check(first.waveform.getAttribute('aria-disabled') !== 'true', 'Known duration remains unavailable');
check(Number(first.waveform.getAttribute('aria-valuemax')) === 125.9, 'Accessible duration is inaccurate');
first.toggle.click(); await settle();
check(!first.audio.paused && first.player.dataset.state === 'playing', 'Play did not start');
check(/pause/i.test(first.toggle.getAttribute('aria-label')), 'Playing control label');
first.media.currentTime = 65; first.emit('timeupdate');
check(first.player.querySelector('.voice-current-time').textContent === '1:05', 'Playback time not updated');
check(Number(first.waveform.getAttribute('aria-valuenow')) === 65, 'Accessible progress not updated');
check(/1:05/.test(first.waveform.getAttribute('aria-valuetext')), 'Accessible time is missing');
const played = first.player.querySelectorAll('.voice-wave-bar.played');
check(played.length > 0 && played.length < 40, 'Waveform progress not colored');
check(getComputedStyle(played[0]).backgroundColor !== getComputedStyle(first.player.querySelector('.voice-wave-bar:not(.played)')).backgroundColor,
  'Played waveform is indistinguishable from unplayed audio');
check(JSON.stringify(shapes(first)) === JSON.stringify(originalShape), 'Playback changed waveform heights');
first.toggle.click();
check(first.audio.paused && /play/i.test(first.toggle.getAttribute('aria-label')), 'Pause did not update');
for (const rate of [1.5, 2, 1]) {
  first.speed.click();
  check(first.audio.playbackRate === rate && first.speed.textContent === rate + '×', 'Speed did not cycle');
  check(/speed/i.test(first.speed.getAttribute('aria-label')), 'Speed label is missing');
}
first.waveform.focus();
check(document.activeElement === first.waveform, 'Waveform cannot receive keyboard focus');
key(first.waveform, 'Home'); check(first.audio.currentTime === 0, 'Home seek');
check(key(first.waveform, 'ArrowRight').defaultPrevented && first.audio.currentTime === 5, 'Right seek');
key(first.waveform, 'ArrowLeft'); check(first.audio.currentTime === 0, 'Left seek');
key(first.waveform, 'End'); check(first.audio.currentTime === 125.9, 'End seek');
key(first.waveform, 'ArrowRight'); check(first.audio.currentTime === 125.9, 'Seek exceeded duration');
key(first.waveform, 'PageDown'); check(first.audio.currentTime < 125.9, 'PageDown seek');
key(first.waveform, 'PageUp'); check(first.audio.currentTime === 125.9, 'PageUp seek');
pointer(first.waveform, 'pointerdown', 0.25);
pointer(first.waveform, 'pointermove', 0.75);
pointer(first.waveform, 'pointerup', 0.75);
check(Math.abs(first.audio.currentTime - 125.9 * 0.75) < 1, 'Waveform drag did not seek');
first.media.currentTime = 125.9; first.media.ended = true; first.media.paused = true; first.emit('ended');
check(/play/i.test(first.toggle.getAttribute('aria-label')), 'Completion left the pause button');
first.toggle.click(); await settle();
check(first.audio.currentTime === 0 && !first.audio.paused, 'Completed audio did not replay from start');
first.metadata(0.7); key(first.waveform, 'End');
check(first.audio.currentTime === 0.7 && first.player.querySelector('.voice-duration').textContent === '0:00', 'Subsecond audio');
first.metadata(3661); check(first.player.querySelector('.voice-duration').textContent === '61:01', 'Long audio format');
first.metadata(Infinity);
check(first.waveform.getAttribute('aria-disabled') === 'true', 'Infinite duration allows invalid seeking');
check(!first.player.textContent.includes('Infinity'), 'Infinite duration displayed');
""")


def test_voice_loading_retry_exclusivity_and_disposal(tmp_path: Path) -> None:
    _browser_check(tmp_path, r"""
const first = note(); const second = note('/synthetic.wav?second');
await settle(); first.metadata(60); second.metadata(60);
first.toggle.click(); await settle();
second.toggle.click(); await settle();
check(first.audio.paused && !second.audio.paused, 'Multiple voice notes play together');
second.emit('waiting');
check(second.player.dataset.state === 'buffering', 'Waiting event lacks buffering state');
second.emit('playing');
check(second.player.dataset.state === 'playing', 'Buffering did not recover');
second.toggle.click(); second.media.mode = 'reject'; second.toggle.click(); await settle();
check(second.player.dataset.state === 'error', 'Rejected playback lacks a recoverable error');
check(second.player.querySelector('.voice-status').textContent.trim(), 'Failure has no descriptive status');
check(!second.toggle.disabled, 'Playback failure disabled retry');
second.media.mode = 'success'; second.toggle.click(); await settle();
check(!second.audio.paused && second.player.dataset.state === 'playing', 'Playback retry failed');
second.media.error = {code: 3}; second.emit('error');
check(second.player.dataset.state === 'error', 'Decoder error not shown');
second.media.error = null; second.toggle.click(); await settle();
check(second.player.dataset.state === 'playing', 'Decoder error cannot be retried');

const interrupted = note('/synthetic.wav?interrupted'); await settle(); interrupted.metadata(30);
interrupted.media.mode = 'pending'; interrupted.toggle.click();
check(second.audio.paused, 'Pending playback did not pause the previous player');
second.toggle.click(); await settle();
check(!second.audio.paused, 'Switching away from a pending player did not start playback');
interrupted.media.finishPlay(); await settle();
check(interrupted.audio.paused && !second.audio.paused, 'Late pending playback interrupted the newer voice note');
check(second.player.dataset.state === 'playing', 'Late media events changed the active player state');
interrupted.destroy();

const discarded = note('/synthetic.wav?discarded', {}, false);
await settle();
check(!discarded.audio.hasAttribute('src'), 'Discarded render fetched protected media');
const pending = note('/synthetic.wav?pending'); await settle(); pending.metadata(30);
pending.media.mode = 'pending'; pending.toggle.click();
check(pending.player.dataset.state === 'loading', 'Pending play lacks loading state');
pending.player.remove(); await settle();
check(!pending.audio.hasAttribute('src'), 'Removed player retained media resource');
pending.media.finishPlay(); await settle();
check(pending.audio.paused, 'Late play resolution revived a disposed player');

second.player.remove(); fixture.append(second.player); await settle();
check(second.audio.hasAttribute('src'), 'Synchronous DOM move destroyed the retained player');
second.player.remove(); await settle();
check(second.audio.paused && !second.audio.hasAttribute('src'), 'Conversation removal left playback/resources active');
const stateAfterRemoval = second.player.dataset.state;
second.emit('waiting'); second.emit('error'); second.emit('timeupdate');
check(second.player.dataset.state === stateAfterRemoval, 'Disposed media event listeners remain active');
const playsAfterRemoval = second.media.plays;
second.toggle.click();
check(second.media.plays === playsAfterRemoval, 'Disposed controls still start playback');
first.destroy(); first.destroy();
check(!first.audio.hasAttribute('src') && first.audio.paused, 'Explicit destroy is not safe or complete');
""")


@pytest.mark.parametrize('width', [390, 1280])
def test_voice_layout_preserves_chat_direction_and_theme(tmp_path: Path, width: int) -> None:
    _browser_check(tmp_path, r"""
for (const direction of ['inbound', 'outbound']) {
  const sender = direction === 'inbound' ? 'Customer' : 'Agent';
  const item = note('/synthetic.wav?' + direction, {}, true, 'Voice note from ' + sender);
  const row = document.createElement('article'); row.className = 'message-row ' + direction;
  row.innerHTML = '<div class="message-avatar">' + sender[0] + '</div><div class="message-stack"><div class="message-bubble voice-message">' +
    '<div class="message-sender">' + sender + '</div><div class="msg-media"></div></div><div class="message-meta">14:23</div></div>';
  row.querySelector('.msg-media').append(item.player); fixture.append(row); await settle(); item.metadata(125);
  if (direction === 'inbound') {item.media.currentTime = 42; item.emit('timeupdate')}
  const bounds = item.player.getBoundingClientRect();
  const waveform = item.waveform.getBoundingClientRect();
  const toggle = item.toggle.getBoundingClientRect();
  const speed = item.speed.getBoundingClientRect();
  check(bounds.width > 170 && bounds.width <= 340 && bounds.height < 100, 'Voice bubble is not compact');
  check(bounds.left >= 0 && bounds.right <= innerWidth, 'Voice bubble overflows viewport');
  const container = fixture.getBoundingClientRect();
  check(bounds.left >= container.left && bounds.right <= container.right, 'Voice bubble overflows the narrow chat container');
  check(waveform.width > 60 && waveform.left >= toggle.right && waveform.right <= speed.left, 'Controls overlap');
  check(row.querySelector('.message-sender').textContent === sender && row.textContent.includes('14:23'), 'Message metadata lost');
  const bubble = row.querySelector('.message-bubble');
  const expected = getComputedStyle(document.documentElement).getPropertyValue('--bubble-' + (direction === 'inbound' ? 'in' : 'out'));
  const sample = document.createElement('div'); sample.style.backgroundColor = expected; fixture.append(sample);
  check(getComputedStyle(bubble).backgroundColor === getComputedStyle(sample).backgroundColor, 'Chat direction color changed');
  check(getComputedStyle(item.player).boxShadow === 'none', 'Player adds a heavy shadow');
  check(item.toggle.getAttribute('aria-label') && item.speed.getAttribute('aria-label') && item.waveform.getAttribute('aria-label'), 'Controls lack names');
  check(item.toggle.tabIndex === 0 && item.speed.tabIndex === 0 && item.waveform.tabIndex === 0, 'Controls are not keyboard accessible');
}
check(document.documentElement.scrollWidth <= innerWidth, 'Voice message causes horizontal page scrolling');
""", width=width, screenshot=width == 1280)


def test_voice_plays_and_completes_native_synthetic_audio(tmp_path: Path) -> None:
    _browser_check(tmp_path, r"""
const {player, audio, destroy} = buildVoiceMessagePlayer('/synthetic.wav', 'Synthetic voice note');
fixture.append(player);
const event = name => new Promise((resolve, reject) => {
  audio.addEventListener(name, resolve, {once: true});
  audio.addEventListener('error', () => reject(Error('Native audio failed: ' + audio.error?.code)), {once: true});
});
await event('loadedmetadata');
check(audio.duration === 1, 'Native metadata duration');
check(player.querySelector('.voice-duration').textContent === '0:01', 'Native duration not displayed');
const ended = event('ended');
player.querySelector('.voice-toggle-btn').click();
await ended;
check(audio.ended && audio.paused, 'Synthetic audio did not finish');
check(/play/i.test(player.querySelector('.voice-toggle-btn').getAttribute('aria-label')), 'Native completion control state');
check(player.querySelector('.voice-current-time').textContent === '0:01', 'Native progress did not reach duration');
destroy(); check(!audio.hasAttribute('src'), 'Native resource not released');
""")
