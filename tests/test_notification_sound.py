"""Verify the shipped sound controls and Web Audio/desktop-notification behavior."""
import os
from pathlib import Path
import re
import shutil
import subprocess


def test_notification_mute_in_browser(tmp_path):
    from app.support_ui import _HTML
    browser = shutil.which(os.environ.get('SUPPORT_TEST_BROWSER', 'chromium'))
    assert browser, 'Install the configured test browser'
    functions = _HTML[_HTML.index('function readNotificationSoundPreference('):_HTML.index('function clearAttentionIndicator(')]
    button = re.search(r'<button[^>]*id="notificationSoundBtn".*?</button>', _HTML, re.S).group()
    icons = re.search(r'<svg class="sr-only".*?</svg>', _HTML, re.S).group()
    script = r'''
const $=id=>document.getElementById(id);
const setIcon=(button,name)=>button.querySelector('use').setAttribute('href','#icon-'+name);
localStorage.removeItem('shipment-support-sounds-muted');
let tones=0, resumed;
const notices=[];
window.Notification=class {static permission='granted'; constructor(title,options){notices.push(options)}};
const context={state:'running',currentTime:0,destination:{},
  createGain(){return {gain:{value:1,setValueAtTime(){},exponentialRampToValueAtTime(){}},connect(){}}},
  createOscillator(){return {frequency:{},connect(){},start(){tones++},stop(){}}},
  resume(){return new Promise(resolve=>{resumed=resolve})}};
const state={notificationSoundsMuted:false,notificationUnlocked:true,notificationAudioContext:context,
 notificationSoundGain:null,authenticated:true,notificationPermissionRequested:true};
''' + functions + r'''
(async()=>{try{
  const button=$('notificationSoundBtn');
  button.addEventListener('click',toggleNotificationSounds);
  window.addEventListener('storage',syncNotificationSoundStorage);
  syncNotificationSoundControls();
  if(readNotificationSoundPreference() || button.getAttribute('aria-pressed')!=='false') throw Error('Wrong default');
  notifyNewCustomerActivity('New ticket');
  if(tones!==3 || notices[0].silent!==false) throw Error('Enabled sound failed');
  button.click();
  if(!readNotificationSoundPreference() || button.getAttribute('aria-pressed')!=='true' || state.notificationSoundGain.gain.value!==0) throw Error('Mute did not persist/silence scheduled tones');
  if(button.textContent.trim()!=='Sound muted') throw Error('Muted label missing');
  notifyNewCustomerActivity('New message');
  if(tones!==3 || !notices.at(-1).silent || !document.title.startsWith('●')) throw Error('Mute lost visual alerts or played sound');
  // A refresh initializes the preference from the same persisted value.
  state.notificationSoundsMuted=readNotificationSoundPreference();
  playNewConversationSound();
  if(tones!==3) throw Error('Restored muted preference played sound');
  button.click();
  if(readNotificationSoundPreference() || state.notificationSoundGain.gain.value!==1) throw Error('Unmute failed');
  playNewConversationSound();
  if(tones!==6) throw Error('Unmuted audio did not resume');
  context.state='suspended';
  playNewConversationSound();
  button.click();
  resumed();await Promise.resolve();
  if(tones!==6) throw Error('Delayed resume ignored mute');
  context.state='running';
  localStorage.setItem('shipment-support-sounds-muted','false');
  window.dispatchEvent(new StorageEvent('storage',{key:'shipment-support-sounds-muted'}));
  if(state.notificationSoundsMuted) throw Error('Other tab not synchronized');
  localStorage.setItem('shipment-support-sounds-muted','true');
  window.dispatchEvent(new StorageEvent('storage',{key:'unrelated-preference'}));
  if(state.notificationSoundsMuted) throw Error('Unrelated storage event changed sound');
  window.dispatchEvent(new StorageEvent('storage',{key:'shipment-support-sounds-muted'}));
  if(!state.notificationSoundsMuted) throw Error('Other tab mute missing');
  // Private/blocked storage still permits toggling during this page session.
  Object.defineProperty(window,'localStorage',{configurable:true,get(){throw Error('blocked')}});
  button.click();
  if(state.notificationSoundsMuted) throw Error('Blocked storage broke toggle');
  button.click();
  if(!state.notificationSoundsMuted) throw Error('Blocked storage broke mute');
  document.body.dataset.result='passed';
}catch(error){document.body.dataset.result='failed: '+error.message}})();
'''
    css = (Path(__file__).resolve().parents[1] / 'app/static/support/support.css').read_text()
    page = tmp_path / 'notification-sound.html'
    page.write_text('<html><head><style>'+css+'body{padding:32px;overflow:auto}.preview{width:280px;padding:20px;background:var(--surface);border:1px solid var(--border);border-radius:12px}</style></head><body>'+icons+'<section class="preview"><h2>Notification sounds</h2><div class="message-preferences-slot">'+button+'</div><p>New tickets and messages are silent. Visual alerts stay available.</p></section><script>'+script+'</script></body></html>')
    result = subprocess.run([browser,'--headless','--no-sandbox','--disable-gpu','--disable-dev-shm-usage',
        '--no-first-run','--no-default-browser-check','--disable-background-networking','--user-data-dir='+str(tmp_path/'profile'),
        '--virtual-time-budget=1000','--screenshot='+str(tmp_path/'notification-sound.png'),'--window-size=650,400','--dump-dom',page.as_uri()],
        capture_output=True,text=True,timeout=20)
    assert result.returncode == 0, result.stderr
    assert 'data-result="passed"' in result.stdout, result.stdout[-2500:]
    assert "$('notificationSoundBtn').addEventListener('click', toggleNotificationSounds);" in _HTML
