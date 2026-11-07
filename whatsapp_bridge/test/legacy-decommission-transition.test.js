import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import {atomicWrite} from '../src/storage.js';
import {validateLegacyExit,validateStoppedStores} from '../src/legacy-decommission-transition.js';
import {initAuthCreds} from '@whiskeysockets/baileys';

const proof={version:1,revision:'b5f8e35ceba5daba0ac6a24cc47332fad4cb149e',target:'99035',eligible:['99012']};
const after={MainPID:'0',ActiveState:'failed',Restart:'no',ExecMainCode:'1',ExecMainStatus:'1',Result:'exit-code'};
const errors=()=>Array.from({length:2},()=>({event:'shutdown_failed',code:'BRIDGE_SHUTDOWN_FAILED'}));
test('audited old binary may exit unsuccessfully only with the exact known aggregate shutdown result',()=>{
  assert.doesNotThrow(()=>validateLegacyExit(proof,after,errors()));
  for(const changed of [{MainPID:'55'},{Restart:'always'},{ExecMainCode:'2'},{ExecMainStatus:'9'},{Result:'timeout'},{ActiveState:'deactivating'}])
    assert.throws(()=>validateLegacyExit(proof,{...after,...changed},errors()),/LEGACY_NOT_NATURALLY_EXITED/);
  for(const event of [{event:'shutdown_deadline_exceeded'},{event:'operation_failed',persistence_failure:true},
    {event:'request_handler_failed'},{event:'checkpoint_failed'},{event:'storage_lock_release_failed'}])
    assert.throws(()=>validateLegacyExit(proof,after,[...errors(),event]),/ADDITIONAL_SHUTDOWN_FAILURE/);
  assert.throws(()=>validateLegacyExit(proof,after,[{event:'shutdown_failed',code:'HTTP_REQUEST_DRAIN_FAILED'}]),/UNEXPECTED/);
});

test('all healthy auth markers, shared journals, inventory and pending business sends are mandatory gates',async t=>{
  const dir=await fs.mkdtemp(path.join(os.tmpdir(),'legacy-transition-mock-'));
  t.after(()=>fs.rm(dir,{recursive:true,force:true}));
  const write=(name,value)=>atomicWrite(path.join(dir,name),value);
  await fs.mkdir(path.join(dir,'99012'),{mode:0o700});await fs.mkdir(path.join(dir,'99035'),{mode:0o700});
  await write('lines.json',{version:1,lines:{'99012':'','99035':''},requests:{}});
  await write('outbox.json',{entries:[{event:{line_id:'99035',event_id:'kept'}}],dead_letters:[]});
  const creds=initAuthCreds();creds.me={id:'96170000000:1@s.whatsapp.net'};
  const healthyState={bound_phone:'96170000000',messages:{},media:{},operations:{}};
  assert.equal(creds.registered,false,'QR pairing keeps the separate registration flag false');
  await write('99012/auth.json',{creds,keys:{}});
  await write('99012/state.json',healthyState);
  await write('99035/state.json',{messages:{uncertain:{pending_send:true,delivery_uncertain:true}},media:{},operations:{}});
  // Missing final credentials for the explicitly abandoned target are NOT a
  // recovery prerequisite. Any unfinished healthy owner remains a hard gate.
  await write('99035/auth.json.auth-intent',{version:1,operations:[{id:'unresolved'}]});
  assert.equal(Object.keys(await validateStoppedStores(dir,'99035',['99012'])).length,4);
  await write('99012/state.json',{...healthyState,bound_phone:'96179999999'});
  await assert.rejects(validateStoppedStores(dir,'99035',['99012']),/HEALTHY_SESSION_INCOMPLETE/);
  await write('99012/state.json',healthyState);
  await write('99012/auth.json',{creds:{...creds,noiseKey:null},keys:{}});
  await assert.rejects(validateStoppedStores(dir,'99035',['99012']),/HEALTHY_SESSION_INCOMPLETE/);
  await write('99012/auth.json',{creds,keys:{}});
  for(const suffix of ['auth-active','auth-intent','auth-failure']) {
    await write('99012/auth.json.'+suffix,{version:1});
    await assert.rejects(validateStoppedStores(dir,'99035',['99012']),/AUTH_RECOVERY_REQUIRED/);
    await fs.unlink(path.join(dir,'99012/auth.json.'+suffix));
  }
  await write('99012/state.json',{...healthyState,messages:{x:{pending_send:true}}});
  await assert.rejects(validateStoppedStores(dir,'99035',['99012']),/HEALTHY_SEND_UNRESOLVED/);
  await write('99012/state.json',healthyState);
  await fs.writeFile(path.join(dir,'outbox.json.journal'),'partial',{mode:0o600});
  await assert.rejects(validateStoppedStores(dir,'99035',['99012']),/BUSINESS_DURABILITY_UNPROVEN/);
  await fs.unlink(path.join(dir,'outbox.json.journal'));
  await fs.mkdir(path.join(dir,'99099'));
  await assert.rejects(validateStoppedStores(dir,'99035',['99012']),/UNACCOUNTED_IDENTITY_DIRECTORY/);
});
