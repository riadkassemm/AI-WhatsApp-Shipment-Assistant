"""Recording lifecycle regressions exercise the shipped JavaScript with event barriers."""
from pathlib import Path
import subprocess


def recording_source():
    source = Path('app/static/support/media.js').read_text()
    return source[:source.index('function clearMediaDraft(')] + source[
        source.index('function recordingMimeType('):source.index('function showLocationError(')]


def test_recording_ownership_final_chunk_and_cancellation():
    harness = r'''
const assert=require('node:assert/strict');
const {File}=require('node:buffer');
const state={ticketId:42,agent:{id:'7'},composerEnabled:true,busy:false};
const errors=[],drafts=[],requests=[],recorders=[];
const $=()=>({classList:{add(){},remove(){}}});const text=()=>{},showError=m=>errors.push(m);
const clearMediaDraft=()=>{},updateComposerControls=()=>{},setAppStatus=()=>{};
const setPendingMedia=(kind,file)=>drafts.push(file);
const setInterval=()=>1,clearInterval=()=>{};
const navigator={mediaDevices:{getUserMedia:()=>new Promise((resolve,reject)=>requests.push({resolve,reject}))}};
const stream=()=>({stopped:false,getTracks(){return [{stop:()=>{this.stopped=true}}]}});
let supported=new Set(['audio/mp4','audio/webm;codecs=opus']);
class MediaRecorder {
 static isTypeSupported(type){return supported.has(type)}
 constructor(stream, options={}){this.mimeType=options.mimeType||'audio/mp4';this.state='inactive';this.stopCalls=0;recorders.push(this)}
 start(...args){assert.equal(args.length,0,'capture one complete file');this.state='recording'}
 stop(){this.stopCalls++;if(this.state==='inactive')throw Error('already stopped');this.state='inactive'}
 data(text){this.ondataavailable?.({data:new Blob([text],{type:this.mimeType})})}
 finish(){this.onstop?.()}
}
'''
    checks = r'''
(async()=>{
assert.equal(recordingMimeType(),'audio/webm;codecs=opus','prefer Opus over MP4');
supported=new Set(['audio/mp4']);assert.equal(recordingMimeType(),'audio/mp4','retain MP4-only browser support');
supported=new Set(['audio/ogg;codecs=opus','audio/mp4']);assert.equal(recordingMimeType(),'audio/ogg;codecs=opus');
const first=startVoiceRecording();await startVoiceRecording();assert.equal(requests.length,1,'permission wait cannot start two recorders');
const firstStream=stream();requests.shift().resolve(firstStream);await first;
const firstRecorder=recorders.at(-1);firstRecorder.data('header');toggleVoiceRecording();toggleVoiceRecording();
assert.equal(firstRecorder.stopCalls,1,'second stop cannot erase pending final bytes');
firstRecorder.data('final audio');firstRecorder.finish();
assert.equal(await drafts[0].text(),'headerfinal audio');assert(firstStream.stopped);assert.equal(state.voiceRecording,null);
assert.equal(firstRecorder.ondataavailable,null,'release callbacks');
const pending=startVoiceRecording();cancelVoiceRecording();
const current=startVoiceRecording();const cancelledStream=stream(),currentStream=stream();
requests.shift().resolve(cancelledStream);await pending;assert(cancelledStream.stopped);
assert(state.voiceRecording,'stale microphone result cannot clear new session');
requests.shift().resolve(currentStream);await current;
const currentRecorder=recorders.at(-1);const lateData=currentRecorder.ondataavailable,lateStop=currentRecorder.onstop;
currentRecorder.data('discarded');cancelVoiceRecording();
const next=startVoiceRecording();const nextStream=stream();requests.shift().resolve(nextStream);await next;
lateData({data:new Blob(['stale'])});lateStop();assert(!nextStream.stopped);
const nextRecorder=recorders.at(-1);nextRecorder.data('only new recording');toggleVoiceRecording();nextRecorder.finish();
assert.equal(await drafts[1].text(),'only new recording');assert.equal(drafts.length,2);
const navigation=startVoiceRecording();state.ticketId=43;
const navigated=stream();requests.shift().resolve(navigated);await navigation;
assert(navigated.stopped);assert.equal(state.voiceRecording,null);assert.equal(drafts.length,2);
const failed=startVoiceRecording();const failedStream=stream();requests.shift().resolve(failedStream);await failed;
const failedRecorder=recorders.at(-1);failedRecorder.data('partial');failedRecorder.onerror();failedRecorder.finish();
assert(failedStream.stopped);assert.equal(drafts.length,2);assert(errors.at(-1).includes('record it again'));
const empty=startVoiceRecording();requests.shift().resolve(stream());await empty;
const emptyRecorder=recorders.at(-1);toggleVoiceRecording();emptyRecorder.finish();
assert.equal(drafts.length,2);assert(errors.at(-1).includes('No audio'));
const denied=startVoiceRecording();requests.shift().reject(Object.assign(Error('denied'),{name:'NotAllowedError'}));await denied;
assert.equal(state.voiceRecording,null);assert(errors.at(-1).includes('permission'));
})().catch(error=>{console.error(error);process.exitCode=1});
'''
    result = subprocess.run(['node', '-e', harness + recording_source() + checks], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_browser_recordings_convert_to_mobile_voice_notes(tmp_path):
    """Real MediaRecorder + real FFmpeg, using synthetic audio, never a microphone."""
    import asyncio
    import base64
    import json
    import os
    import shutil
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from threading import Event, Thread
    from app.support_media import SupportMediaStore

    browser = shutil.which(os.environ.get('SUPPORT_TEST_BROWSER', 'chromium'))
    assert browser, 'Install Chrome/Chromium for recording regressions'
    setup = r'''
const state={ticketId:42,agent:{id:'7'},composerEnabled:true,busy:false};
const $=()=>({classList:{add(){},remove(){}}});const text=()=>{},updateComposerControls=()=>{},clearMediaDraft=()=>{},setAppStatus=()=>{};
let recorded,resolveRecording;
const showError=message=>{if(message)throw Error(message)};
const setPendingMedia=(kind,file)=>{recorded=file;resolveRecording()};
'''
    recording = r'''
(async()=>{
const results=[],supports=MediaRecorder.isTypeSupported.bind(MediaRecorder);
for(const mode of ['default','mp4']){
 if(mode==='mp4'&&!supports('audio/mp4'))continue;
 MediaRecorder.isTypeSupported=type=>supports(type)&&(mode!=='mp4'||type==='audio/mp4');
 const context=new AudioContext();await context.resume();
 const source=context.createOscillator(),destination=context.createMediaStreamDestination();source.connect(destination);
 Object.defineProperty(navigator.mediaDevices,'getUserMedia',{configurable:true,value:async()=>destination.stream});
 const completed=new Promise(resolve=>{resolveRecording=resolve});
 await startVoiceRecording();
 source.onended=()=>toggleVoiceRecording();source.start();source.stop(context.currentTime+0.6);
 await completed;
 const bytes=new Uint8Array(await recorded.arrayBuffer());
 results.push({mode,type:recorded.type,base64:btoa(Array.from(bytes,b=>String.fromCharCode(b)).join(''))});
 if(destination.stream.getTracks().some(track=>track.readyState!=='ended'))throw Error('microphone track leaked');
 if(state.voiceRecording||state.mediaRecorder)throw Error('recording session leaked');
 await context.close();
}
await fetch('/result',{method:'POST',body:JSON.stringify(results)});
})().catch(error=>fetch('/result',{method:'POST',body:JSON.stringify({error:String(error)})}));
'''
    page = '<script>' + setup + recording_source() + recording + '</script>'
    finished, payload = Event(), []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            self.send_response(200)
            self.send_header('Content-Type', 'text/html')
            self.end_headers()
            self.wfile.write(page.encode())

        def do_POST(self):
            payload.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
            self.send_response(200)
            self.end_headers()
            finished.set()

    server = HTTPServer(('127.0.0.1', 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    process = subprocess.Popen([browser, '--headless', '--no-sandbox', '--disable-gpu', '--disable-dev-shm-usage',
        '--disable-background-networking', '--autoplay-policy=no-user-gesture-required',
        '--user-data-dir=' + str(tmp_path/'profile'), f'http://127.0.0.1:{server.server_port}'],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        assert finished.wait(30), 'Browser did not complete recording'
        assert isinstance(payload[0], list), payload
        assert payload[0][0]['mode'] == 'default'
        for result in payload[0]:
            if result['mode'] == 'mp4':
                assert result['type'].startswith('audio/mp4')
            data, mime, filename, voice = asyncio.run(SupportMediaStore().prepare_voice_note(
                base64.b64decode(result['base64']), result['type']))
            assert mime == 'audio/ogg; codecs=opus' and voice
            assert filename == 'voice-note.opus'
            path = tmp_path / filename
            path.write_bytes(data)
            stream = json.loads(subprocess.check_output(['ffprobe', '-v', 'error', '-show_entries',
                'stream=codec_name,channels,sample_rate,duration', '-of', 'json', str(path)]))['streams'][0]
            assert stream['codec_name'] == 'opus' and stream['channels'] == 1
            assert stream['sample_rate'] == '48000' and float(stream['duration']) > 0.3
    finally:
        process.terminate()
        process.wait(5)
        server.shutdown()
        server.server_close()
        thread.join(5)
