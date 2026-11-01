// One observer owns mounting/disposal for both the timeline and composer previews.
// Rendering builds detached rows before reconciling them; only connected players
// load media, so discarded rows never start duplicate authenticated downloads.
const voiceMessagePlayers = new Set();
let voiceMessageObserver = null;
let activeVoiceMessage = null;

function syncVoiceMessagePlayers() {
  for (const entry of voiceMessagePlayers) {
    if (entry.player.isConnected) entry.mount();
    else entry.destroy();
  }
  if (!voiceMessagePlayers.size) {
    voiceMessageObserver?.disconnect();
    voiceMessageObserver = null;
  }
}

function voiceMessageTime(seconds) {
  if (!Number.isFinite(seconds) || seconds < 0) return '--:--';
  const whole = Math.floor(seconds);
  return Math.floor(whole / 60) + ':' + String(whole % 60).padStart(2, '0');
}

function voiceMessagePeaks(source, peaks, count = 40) {
  const samples = Array.from(peaks || []).map(Number).filter(value => Number.isFinite(value) && value >= 0);
  if (samples.length) {
    const maximum = samples.reduce((max, value) => Math.max(max, value), 0) || 1;
    return Array.from({length: count}, (_, index) => {
      const start = Math.floor(index * samples.length / count);
      const end = Math.max(start + 1, Math.floor((index + 1) * samples.length / count));
      let peak = 0;
      for (let offset = start; offset < end; offset++) peak = Math.max(peak, samples[offset]);
      return 8 + 86 * peak / maximum;
    });
  }
  // A stable visual fallback, not a claim about the recording's amplitude.
  let seed = 2166136261;
  for (const char of source) seed = Math.imul(seed ^ char.charCodeAt(0), 16777619) >>> 0;
  return Array.from({length: count}, (_, index) => {
    seed = (Math.imul(seed, 1664525) + 1013904223) >>> 0;
    const envelope = 0.45 + 0.55 * Math.sin(Math.PI * (index + 1) / (count + 1));
    return 12 + (seed / 4294967296) * 78 * envelope;
  });
}

function buildVoiceMessagePlayer(src, label, {peaks = []} = {}) {
  const player = document.createElement('div');
  player.className = 'voice-note-player';
  player.setAttribute('role', 'group');
  player.setAttribute('aria-label', label);
  const audio = document.createElement('audio');
  audio.preload = 'metadata';
  audio.hidden = true;
  audio.dataset.mediaUrl = src;
  const toggle = document.createElement('button');
  toggle.type = 'button';
  toggle.className = 'voice-toggle-btn';
  const icon = document.createElement('span');
  icon.className = 'voice-toggle-icon';
  icon.setAttribute('aria-hidden', 'true');
  toggle.append(icon);
  const track = document.createElement('div');
  track.className = 'voice-track';
  const waveform = document.createElement('div');
  waveform.className = 'voice-waveform';
  waveform.tabIndex = 0;
  waveform.setAttribute('role', 'slider');
  waveform.setAttribute('aria-label', 'Seek ' + label.toLowerCase());
  waveform.setAttribute('aria-orientation', 'horizontal');
  waveform.setAttribute('aria-valuemin', '0');
  waveform.title = 'Seek with the arrow keys, Home or End';
  const bars = voiceMessagePeaks(src, peaks).map(height => {
    const bar = document.createElement('span');
    bar.className = 'voice-wave-bar';
    bar.style.height = height + '%';
    bar.setAttribute('aria-hidden', 'true');
    waveform.append(bar);
    return bar;
  });
  const time = document.createElement('div');
  time.className = 'voice-time';
  const current = document.createElement('span');
  current.className = 'voice-current-time';
  const total = document.createElement('span');
  total.className = 'voice-duration';
  time.append(current, total);
  track.append(waveform, time);
  const speed = document.createElement('button');
  speed.type = 'button';
  speed.className = 'voice-speed-btn';
  const status = document.createElement('span');
  status.className = 'voice-status';
  status.setAttribute('role', 'status');
  status.setAttribute('aria-atomic', 'true');
  player.append(audio, toggle, track, speed, status);

  const listeners = new AbortController();
  const on = (target, event, callback) => target.addEventListener(event, callback, {signal: listeners.signal});
  let mounted = false;
  let disposed = false;
  let requested = false;
  let needsReload = false;
  let generation = 0;
  let frame = null;
  let pointerId = null;
  let finalDuration = NaN;
  let completed = false;
  const rates = [1, 1.5, 2];
  const duration = () => Number.isFinite(audio.duration) && audio.duration > 0 ? audio.duration : finalDuration;
  const setText = (node, value) => { if (node.textContent !== value) node.textContent = value; };
  function setState(value, message = '') {
    player.dataset.state = value;
    const playing = requested || !audio.paused;
    toggle.setAttribute('aria-label', (playing ? 'Pause ' : needsReload ? 'Retry ' : 'Play ') + label.toLowerCase());
    toggle.title = playing ? 'Pause' : needsReload ? 'Retry playback' : 'Play';
    toggle.setAttribute('aria-pressed', String(playing));
    setText(status, message);
    status.hidden = !message;
  }
  function progress() {
    const length = duration();
    const known = Number.isFinite(length) && length > 0;
    const elapsed = completed && known ? length : Math.max(0, audio.currentTime || 0);
    setText(current, voiceMessageTime(elapsed));
    setText(total, voiceMessageTime(length));
    current.setAttribute('aria-label', 'Elapsed ' + voiceMessageTime(elapsed));
    total.setAttribute('aria-label', known ? 'Duration ' + voiceMessageTime(length) : 'Duration unavailable');
    waveform.setAttribute('aria-disabled', String(!known || needsReload));
    waveform.setAttribute('aria-valuemax', String(known ? length : 0));
    waveform.setAttribute('aria-valuenow', String(known ? Math.min(elapsed, length) : 0));
    waveform.setAttribute('aria-valuetext', voiceMessageTime(elapsed) + (known ? ' of ' + voiceMessageTime(length) : ', duration unavailable'));
    const played = known ? Math.round(Math.min(1, elapsed / length) * bars.length) : 0;
    bars.forEach((bar, index) => bar.classList.toggle('played', index < played));
  }
  function stopFrames() {
    if (frame !== null) cancelAnimationFrame(frame);
    frame = null;
  }
  function animate() {
    frame = null;
    if (disposed || audio.paused || audio.ended) return;
    progress();
    frame = requestAnimationFrame(animate);
  }
  function pause() {
    requested = false;
    generation++;
    audio.pause();
    stopFrames();
    if (activeVoiceMessage === entry) activeVoiceMessage = null;
    if (!disposed) setState('paused');
  }
  function fail(message, reload = true) {
    pause();
    needsReload = reload;
    setState('error', message);
    progress();
  }
  function mount() {
    if (mounted || disposed) return;
    mounted = true;
    audio.src = src;
  }
  async function play() {
    if (disposed) return;
    mount();
    if (activeVoiceMessage && activeVoiceMessage !== entry) activeVoiceMessage.pause();
    activeVoiceMessage = entry;
    if (needsReload) {
      needsReload = false;
      finalDuration = NaN;
      // Retain the existing server-side media recovery/cache-busting behavior.
      audio.src = src.startsWith('blob:') || src.startsWith('data:') ? src
        : src + (src.includes('?') ? '&' : '?') + 'retry=' + Date.now();
      audio.load();
    }
    if (completed || audio.ended) audio.currentTime = 0;
    completed = false;
    requested = true;
    const attempt = ++generation;
    setState('loading', 'Loading audio…');
    try {
      await audio.play();
      if (disposed || generation !== attempt) {
        if (disposed || !requested) audio.pause();
        return;
      }
      setState(audio.paused ? 'paused' : 'playing');
    } catch (error) {
      if (disposed || generation !== attempt) return;
      if (error.name === 'AbortError') { pause(); return; }
      fail(error.name === 'NotAllowedError' ? 'Press play to allow audio playback.' : 'Audio unavailable. Press play to retry.', error.name !== 'NotAllowedError');
    }
  }
  function seek(value) {
    const length = duration();
    if (!Number.isFinite(length) || length <= 0 || needsReload || disposed) return;
    try {
      audio.currentTime = Math.max(0, Math.min(length, value));
      completed = false;
      progress();
    } catch (_) { /* Media may become unavailable between metadata and seeking. */ }
  }
  function seekPointer(event) {
    const bounds = waveform.getBoundingClientRect();
    if (bounds.width) seek((event.clientX - bounds.left) / bounds.width * duration());
  }
  function updateSpeed() {
    setText(speed, audio.playbackRate + '×');
    const next = rates[(rates.indexOf(audio.playbackRate) + 1) % rates.length];
    speed.setAttribute('aria-label', 'Playback speed ' + audio.playbackRate + ' times. Change to ' + next + ' times');
    speed.title = 'Change playback speed';
  }
  function destroy() {
    if (disposed) return;
    disposed = true;
    listeners.abort();
    pause();
    if (pointerId !== null && waveform.hasPointerCapture(pointerId)) waveform.releasePointerCapture(pointerId);
    pointerId = null;
    audio.removeAttribute('src');
    if (mounted) audio.load();
    voiceMessagePlayers.delete(entry);
    if (!voiceMessagePlayers.size) {
      voiceMessageObserver?.disconnect();
      voiceMessageObserver = null;
    }
  }
  const entry = {player, audio, mount, pause, destroy};
  on(toggle, 'click', () => requested || !audio.paused ? pause() : play());
  on(speed, 'click', () => {
    audio.playbackRate = rates[(rates.indexOf(audio.playbackRate) + 1) % rates.length];
    audio.defaultPlaybackRate = audio.playbackRate;
    updateSpeed();
  });
  on(audio, 'ratechange', updateSpeed);
  on(audio, 'play', () => {
    if (!requested || activeVoiceMessage !== entry) { audio.pause(); return; }
    completed = false;
    setState('loading', 'Loading audio…');
  });
  on(audio, 'playing', () => {
    if (!requested || activeVoiceMessage !== entry) { audio.pause(); return; }
    needsReload = false;
    requested = true;
    setState('playing');
    stopFrames();
    animate();
  });
  on(audio, 'pause', () => {
    // A queued pause from a previous attempt must not cancel a newer play().
    if (!audio.paused) return;
    requested = false;
    stopFrames();
    if (activeVoiceMessage === entry) activeVoiceMessage = null;
    if (player.dataset.state !== 'error') setState('paused');
  });
  on(audio, 'ended', () => {
    if (!Number.isFinite(duration()) && audio.currentTime > 0) finalDuration = audio.currentTime;
    completed = true;
    pause();
    progress();
  });
  for (const event of ['loadedmetadata', 'durationchange', 'canplay']) on(audio, event, () => {
    progress();
    if (!requested && !needsReload) setState('ready');
  });
  for (const event of ['timeupdate', 'seeked']) on(audio, event, progress);
  for (const event of ['waiting', 'stalled']) on(audio, event, () => {
    if (requested || !audio.paused) setState('buffering', 'Buffering…');
  });
  on(audio, 'error', () => fail('Audio unavailable. Press play to retry.'));
  on(waveform, 'pointerdown', event => {
    if (event.button !== 0 || waveform.getAttribute('aria-disabled') === 'true') return;
    pointerId = event.pointerId;
    waveform.setPointerCapture(pointerId);
    waveform.focus({preventScroll: true});
    seekPointer(event);
    event.preventDefault();
  });
  on(waveform, 'pointermove', event => { if (event.pointerId === pointerId) seekPointer(event); });
  on(waveform, 'pointerup', event => {
    if (event.pointerId !== pointerId) return;
    seekPointer(event);
    if (waveform.hasPointerCapture(pointerId)) waveform.releasePointerCapture(pointerId);
    pointerId = null;
  });
  for (const event of ['pointercancel', 'lostpointercapture']) on(waveform, event, () => { pointerId = null; });
  on(waveform, 'keydown', event => {
    const length = duration();
    const page = Math.max(10, (length || 0) / 10);
    const target = {ArrowLeft: audio.currentTime - 5, ArrowDown: audio.currentTime - 5,
      ArrowRight: audio.currentTime + 5, ArrowUp: audio.currentTime + 5,
      Home: 0, End: length, PageDown: audio.currentTime - page, PageUp: audio.currentTime + page};
    if (Object.hasOwn(target, event.key)) { event.preventDefault(); seek(target[event.key]); }
  });
  updateSpeed();
  progress();
  setState('loading', 'Loading audio…');
  voiceMessagePlayers.add(entry);
  if (!voiceMessageObserver) {
    voiceMessageObserver = new MutationObserver(syncVoiceMessagePlayers);
    voiceMessageObserver.observe(document.body, {childList: true, subtree: true});
  }
  queueMicrotask(syncVoiceMessagePlayers);
  return {player, audio, destroy};
}

window.addEventListener('pagehide', () => activeVoiceMessage?.pause());
