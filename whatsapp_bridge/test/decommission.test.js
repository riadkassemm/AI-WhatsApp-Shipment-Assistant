import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import {EventEmitter} from 'node:events';
import {Bridge} from '../src/bridge.js';
import {FileStore, atomicWrite, acquireDataDirectory} from '../src/storage.js';
import {inventory} from '../src/fenced-auth.js';
import {publishQuarantine, loadQuarantine, removalResult} from '../src/decommission.js';
import {storageTool} from '../src/storage-tool.js';

const ID = '99035', HEALTHY = ['99012','99013','99015','99017','99024','99026'];
const disk = async () => ({bsize:4096,blocks:10_000_000,bavail:5_000_000,files:1_000_000,ffree:900_000});
async function fixture(t) {
  const dataDir = await fs.mkdtemp(path.join(os.tmpdir(),'decommission-mock-'));
  const release = await acquireDataDirectory(dataDir);
  const directory = path.join(dataDir,ID); await fs.mkdir(directory,{mode:0o700});
  await atomicWrite(path.join(directory,'auth.json'),{creds:{registered:true, synthetic:'never-reuse'},keys:{}});
  await atomicWrite(path.join(directory,'auth.json.auth-active'),{version:1,id:'abandoned-owner'});
  await atomicWrite(path.join(directory,'auth.json.auth-failure'),{version:1,failures:[{operation:'drain_timeout'}]});
  await atomicWrite(path.join(directory,'auth.json.auth-intent'),{version:1,operations:Array.from({length:477},(_,i)=>({id:`mock-${i}`,mutations:i<3?[{synthetic:true}]:[]}))});
  await atomicWrite(path.join(directory,'state.json'),{enabled:true,messages:{kept:{pending_send:true,delivery_uncertain:true}},operations:{kept:{status:'uncertain'}},media:{},send_requests:{}});
  await fs.mkdir(path.join(directory,'media'),{mode:0o700});
  await fs.writeFile(path.join(directory,'media','attachment'),'retained image',{mode:0o600});
  const manifest = await new FileStore(path.join(dataDir,'lines.json'),()=>({version:1,lines:{[ID]:''},requests:{}})).load();
  const before = await inventory(directory);
  const sockets=[];
  const config={dataDir,lines:HEALTHY.map(line_id=>({line_id,expected_phone:''})),fallbackLineId:ID,storage:{maintenance:{enabled:false}},ackTimeoutMs:30};
  const deps={assertExclusiveOwnership:release.assertHeld,storageFilesystem:disk,log(){},onError(){},
    fetch:async()=>{throw new Error('Unexpected callback');},makeSocket:()=>{
      const ws=new EventEmitter();ws.isOpen=true;let end;
      const s={ws,user:{id:'96170000000:1@s.whatsapp.net'},bridgeTransportClosed:new Promise(resolve=>{end=resolve;}),
        bridgePrepareRetirement(){},ev:{process(handler){s.handler=handler;return ()=>{};}},
        end(){ws.isOpen=false;end();ws.emit('close');},sendMessage(){throw new Error('Duplicate or unexpected send');}};
      sockets.push(s);return s;
    }};
  let released=false;
  const releaseOnce=async()=>{if(!released){await release();released=true;}};
  releaseOnce.assertHeld=release.assertHeld;
  t.after(async()=>{await releaseOnce();await fs.rm(dataDir,{recursive:true,force:true});});
  const publish=options=>publishQuarantine({manifest,id:ID,directory,actor:'mock-admin',proof:{kind:'process_exit',pid:99999},assertStopped:release.assertHeld,...options});
  return {dataDir,directory,manifest,before,config,deps,sockets,publish,release:releaseOnce};
}

test('permanent abandonment accepts missing credential outcome and preserves all evidence without purging',async t=>{
  const f=await fixture(t), record=await f.publish();
  assert.equal(record.credential_outcome,'incomplete_abandoned');
  assert.equal(record.remote_unlinked,false);assert.equal(record.reuse_allowed,false);
  assert.match(removalResult(record).message,/requires action/);
  assert.deepEqual(await inventory(f.directory),f.before);
  assert.equal(f.manifest.data.removed_line_ids,undefined);
  assert.equal(f.manifest.data.lines[ID],undefined);
  assert.deepEqual(await f.publish(),record,'repeated request is idempotent');
});

test('six healthy identities restart independently with quarantine; no reuse, sends, cleanup or callback loss',async t=>{
  const f=await fixture(t);await f.publish();
  const bridge=await new Bridge(f.config,f.deps).initialize({autoConnect:false});
  await bridge.outbox.update(data=>data.entries.push({event:{line_id:ID,event_id:'retained-callback'},next_try:0}));
  for(const id of HEALTHY) await bridge.connect(id);
  assert.equal(f.sockets.length,6);
  assert.equal(bridge.runtimeHealth().eligible_lines,6);
  assert.equal(bridge.runtimeHealth().ready,true);
  assert.equal(bridge.getLine(ID).status,'decommissioned');
  for(const action of [()=>bridge.connect(ID),()=>bridge.disconnect(ID),()=>bridge.send(ID,{}),()=>bridge.uploadMedia(ID,{})]) await assert.rejects(action,{code:'IDENTITY_DECOMMISSIONED'});
  await bridge.recoverPending();await bridge.deliverCallbacks();
  assert.equal(bridge.outbox.data.entries.length,1);
  assert.equal((await bridge.removeLine(ID)).unlinked,false);
  assert.equal((await bridge.decommissionLine(ID)).quarantined,true);
  await assert.rejects(bridge.purgeRemovedLine(ID),/QUARANTINE_PURGE_FORBIDDEN/);
  await bridge.close();
  const restarted=await new Bridge({...f.config,lines:[...f.config.lines,{line_id:ID,expected_phone:''}]},f.deps).initialize();
  assert.equal(f.sockets.length,12,'fallback/env cannot revive abandoned credentials');
  assert.equal(restarted.outbox.data.entries.length,1);
  assert.equal(restarted.getLine(ID).store.data.messages.kept.pending_send,true);
  assert.equal(restarted.getLine(ID).store.data.operations.kept.status,'uncertain');
  assert.deepEqual(await inventory(f.directory),f.before);
  assert.equal(await fs.readFile(path.join(f.directory,'media','attachment'),'utf8'),'retained image');
  const added=await restarted.addLine({request_id:'11111111-2222-3333-4444-555555555555',expected_phone:''});
  assert.notEqual(added.line_id,ID);assert.notEqual(restarted.getLine(added.line_id).auth.state.creds.synthetic,'never-reuse');
  await restarted.close();
});

test('live writer or unproven owner prevents publishing any quarantine record',async t=>{
  const f=await fixture(t);
  await assert.rejects(f.publish({assertStopped:()=>{throw new Error('QUARANTINE_WRITER_ACTIVE');}}),/WRITER_ACTIVE/);
  assert.equal(f.manifest.data.quarantined_lines,undefined);assert.deepEqual(await inventory(f.directory),f.before);
});

test('interrupted publication before commit retains all files and allows an explicit retry',async t=>{
  const f=await fixture(t), update=f.manifest.update.bind(f.manifest);
  f.manifest.update=async()=>{throw new Error('synthetic-fsync-failure');};
  await assert.rejects(f.publish(),/fsync/);assert.equal(f.manifest.data.quarantined_lines,undefined);
  assert.deepEqual(await inventory(f.directory),f.before);
  f.manifest.update=update;await f.publish();
  const b=await new Bridge(f.config,f.deps).initialize({autoConnect:false});
  await b.close();assert.deepEqual(await inventory(f.directory),f.before);
});

test('changed evidence blocks startup and activates no healthy or quarantined transports',async t=>{
  const f=await fixture(t);await f.publish();
  await atomicWrite(path.join(f.directory,'auth.json.auth-failure'),{version:1,unexpected:true});
  const b=await new Bridge(f.config,f.deps).initialize();
  assert.equal(b.runtimeHealth().ready,false);assert.equal(f.sockets.length,0);
  await assert.rejects(b.close());
  // Dispose only synthetic handles; production must preserve the rejected gate.
  b.lines.delete(ID);b.startupIsolationBlocked=false;await b.close();
});

test('incomplete business journal blocks abandonment without rewriting its tail',async t=>{
  const f=await fixture(t);await fs.writeFile(path.join(f.directory,'state.json.journal'),'incomplete',{mode:0o600});
  const before=await inventory(f.directory);
  await assert.rejects(f.publish(),/STATE_INTEGRITY_UNPROVEN/);
  assert.deepEqual(await inventory(f.directory),before);assert.equal(f.manifest.data.quarantined_lines,undefined);
});

test('explicit quarantine facade revalidates cessation and refuses every mutation',async t=>{
  const f=await fixture(t),record=await f.publish();let stopped=true;
  const retained=await loadQuarantine(f.directory,record,()=>{f.release.assertHeld();if(!stopped)throw new Error('owner-live');});
  await assert.rejects(retained.stateStore.update(()=>{}),/IDENTITY_DECOMMISSIONED/);
  stopped=false;await assert.rejects(retained.verify(),/owner-live/);assert.equal(retained.isolation_safe,false);
});

test('offline validation preserves abandoned auth bytes and rejects transforming tools',async t=>{
  const f=await fixture(t);await f.publish();await f.release();
  const result=await storageTool('validate-quarantined',f.dataDir);
  assert.deepEqual(result.quarantined_lines,[ID]);
  assert.equal(result.abandoned_auth_stores,1);assert.equal(result.journal_validated_stores,1);
  assert.equal(result.authentication_reuse_allowed,false);
  for(const command of ['validate','validate-retained','compact','import','export'])
    await assert.rejects(storageTool(command,f.dataDir,path.join(f.dataDir,'../unused-export')),/QUARANTINE_TRANSFORM_FORBIDDEN/);
  assert.deepEqual(await inventory(f.directory),f.before);
});

test('a media download completing after quarantine cannot publish files',async t=>{
  const f=await fixture(t);await f.publish();
  const b=await new Bridge(f.config,f.deps).initialize({autoConnect:false});
  let mutations=0;
  assert.throws(()=>b.mediaStorage(b.getLine(ID),async()=>{mutations++;}),{code:'IDENTITY_DECOMMISSIONED'});
  assert.equal(mutations,0);await b.close();
  assert.deepEqual(await inventory(f.directory),f.before);
});

test('live decommission rejects executing work, then abandons settled credentials without auth flush; concurrent requests are idempotent',async t=>{
  const f=await fixture(t);await f.publish();
  const b=await new Bridge(f.config,f.deps).initialize({autoConnect:false});
  await b.connect(HEALTHY[0]);const line=b.getLine(HEALTHY[0]),auth=line.auth;
  let entered,finish;
  const admitted=new Promise(resolve=>{entered=resolve;}),wait=new Promise(resolve=>{finish=resolve;});
  const work=auth.trackProviderTask(async()=>{entered();await wait;throw new Error('synthetic-provider-failure');});
  const settled=Promise.allSettled([work]);await admitted;
  auth.retire();await assert.rejects(auth.drain({timeoutMs:5}),/AUTH_TRANSACTION_DRAIN_TIMEOUT/);
  await assert.rejects(b.decommissionLine(line.line_id),{code:'DECOMMISSION_BLOCKED'});
  assert.equal(b.manifest.data.quarantined_lines[line.line_id],undefined);
  finish();await settled;await auth.assessRecovery();
  // Synthetic missing historical final evidence. Reuse stays forbidden; only
  // the live cessation proof can authorize permanent abandonment.
  await atomicWrite(path.join(line.directory,'auth.json.auth-failure'),{version:1,failures:[{operation:'drain_timeout'}]});
  const before=await inventory(line.directory);
  const flush=t.mock.method(auth.store,'flush',()=>{throw new Error('Failed auth must not flush');});
  const close=t.mock.method(auth.store,'close',()=>{throw new Error('Failed auth must not close');});
  const [a,c]=await Promise.all([b.decommissionLine(line.line_id),b.decommissionLine(line.line_id)]);
  assert.deepEqual(a,c);assert.equal(a.unlinked,false);
  await assert.rejects(auth.saveCreds(),/AUTH_SESSION_RETIRED/);
  await b.close();
  assert.equal(flush.mock.callCount(),0);assert.equal(close.mock.callCount(),0);
  assert.deepEqual(await inventory(line.directory),before);
});
