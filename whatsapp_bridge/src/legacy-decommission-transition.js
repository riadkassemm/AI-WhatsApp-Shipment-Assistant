// One-time adapter for an audited old binary. It never signals a process.
// Prepare while ingress is closed; finalize only after that binary exits itself.
import fs from 'node:fs/promises';
import path from 'node:path';
import crypto from 'node:crypto';
import {execFileSync} from 'node:child_process';
import {pathToFileURL} from 'node:url';
import {acquireDataDirectory, atomicWrite, FileStore, assertAuthRecoveryClear} from './storage.js';
import {readJournal} from './journal-store.js';
import {readConfig, validateLineManifest} from './config.js';
import {publishQuarantine, removalResult} from './decommission.js';

const REVISION = 'b5f8e35ceba5daba0ac6a24cc47332fad4cb149e';
const HASHES = {
  'bridge.js':'983cf041c8dc995846f3b3aff17c7ddfc95fe39fa3a39feef49ad259239bb1cd',
  'runtime-lifecycle.js':'1488ea0741c897dca78369b4aa4eac333b71df089490641f3b67349cfa70cd57',
  'storage.js':'34e96c2c472397e0a0c3dc24de3ec2fb534a176e538c6592eac0a8ba8d2d7775',
  'journal-store.js':'01ce00e5fbdae854680998ebb011ce5d70a6bf30af3d8fc2c6d8014d2d788661',
  'index.js':'974f8c43766b8961f97627d91d27374e1dc97756c7a467661c9f4b4cc51408ed',
  'server.js':'0ebd4c0e84413a3671effbdbea38121aa942aed5a3be6ae266fc5f60c7e8fc9b',
};
const unit = () => Object.fromEntries(execFileSync('systemctl',['show','whatsapp-bridge.service',
  '-p','MainPID','-p','ActiveState','-p','SubState','-p','ExecMainCode','-p','ExecMainStatus','-p','Result','-p','Restart','-p','ControlGroup'],{encoding:'utf8'})
  .trim().split('\n').map(s=>{const i=s.indexOf('=');return [s.slice(0,i),s.slice(i+1)];}));
const fail = message => {throw new Error(message);};
async function verifySource(release) {
  if ((await fs.readFile(path.join(release,'REVISION'),'utf8')).trim() !== REVISION) fail('LEGACY_REVISION_MISMATCH');
  for (const [name,hash] of Object.entries(HASHES)) if (crypto.createHash('sha256').update(await fs.readFile(path.join(release,'whatsapp_bridge/src',name))).digest('hex') !== hash) fail('LEGACY_SOURCE_MISMATCH');
}
const boot = () => fs.readFile('/proc/sys/kernel/random/boot_id','utf8');
async function processStart(pid) {
  try {const value=await fs.readFile(`/proc/${pid}/stat`,'utf8');return value.slice(value.lastIndexOf(')')+2).split(' ')[19];}
  catch(error){if(error.code==='ENOENT')return null;throw error;}
}

export function validateLegacyExit(proof, after, events) {
  if (proof.version!==1 || proof.revision!==REVISION || !proof.target || !Array.isArray(proof.eligible) || !proof.eligible.length) fail('INVALID_TRANSITION_PROOF');
  if (after.MainPID!=='0' || !['inactive','failed'].includes(after.ActiveState) || after.Restart!=='no' ||
      after.ExecMainCode!=='1' || after.ExecMainStatus!=='1' || after.Result!=='exit-code') fail('LEGACY_NOT_NATURALLY_EXITED');
  const errors=events.filter(e=>e.event==='shutdown_failed');
  if (errors.length!==2 || errors.some(e=>e.code!=='BRIDGE_SHUTDOWN_FAILED')) fail('UNEXPECTED_SHUTDOWN_RESULT');
  for(const event of events) {
    if(event.event==='shutdown_failed')continue;
    if(event.persistence_failure || /failed|failure|deadline|corrupt|drain_timeout/i.test(event.event??'')) fail('ADDITIONAL_SHUTDOWN_FAILURE');
  }
}

export async function validateStoppedStores(directory, target, eligible, minimumSequences = {}) {
  const manifest = validateLineManifest(JSON.parse(await fs.readFile(path.join(directory,'lines.json'),'utf8')));
  if (!manifest.lines[target] && !Object.hasOwn(manifest.lines,target) && !manifest.quarantined_lines[target]) fail('TARGET_NOT_REGISTERED');
  const active=Object.keys(manifest.lines).filter(id=>id!==target).sort();
  if(JSON.stringify(active)!==JSON.stringify([...eligible].sort()))fail('IDENTITY_INVENTORY_CHANGED');
  const numeric=(await fs.readdir(directory,{withFileTypes:true})).filter(e=>e.isDirectory()&&/^\d+$/.test(e.name)).map(e=>e.name).sort();
  if(JSON.stringify(numeric)!==JSON.stringify([...eligible,target].sort()))fail('UNACCOUNTED_IDENTITY_DIRECTORY');
  const summaries={};
  for(const name of ['outbox.json',...eligible.flatMap(id=>[`${id}/auth.json`,`${id}/state.json`]),`${target}/state.json`]) {
    if(name.endsWith('/auth.json'))await assertAuthRecoveryClear(path.join(directory,name));
    const journal=await readJournal(path.join(directory,name),()=>{throw new Error('STORE_MISSING');},{repairTail:false});
    if(journal.incompleteTailBytes || journal.seq < (minimumSequences[name]??0))fail('BUSINESS_DURABILITY_UNPROVEN');
    if(name.endsWith('/auth.json') && (journal.data.creds?.registered!==true || typeof journal.data.creds?.me?.id!=='string'))fail('HEALTHY_SESSION_INCOMPLETE');
    if(name.endsWith('/state.json') && !name.startsWith(target+'/') && Object.values(journal.data.messages??{}).some(m=>m.pending_send && !m.delivery_uncertain))fail('HEALTHY_SEND_UNRESOLVED');
    let health;
    try{health=JSON.parse(await fs.readFile(path.join(directory,name+'.health.json'),'utf8'));}
    catch(error){if(error.code!=='ENOENT')throw error;}
    if(health && (health.history_unavailable || health.last_compaction_error || health.recovery_errors>0))fail('STORE_HEALTH_UNPROVEN');
    summaries[name]={sequence:journal.seq,messages:Object.keys(journal.data.messages??{}).length};
  }
  return summaries;
}

async function prepare(config,target,recordFile,release) {
  await verifySource(release);
  try{await fs.lstat(recordFile);fail('TRANSITION_RECORD_EXISTS');}catch(error){if(error.code!=='ENOENT')throw error;}
  const before=unit();if(before.ActiveState!=='active'||before.Restart!=='no'||!Number(before.MainPID))fail('TRANSITION_STOP_POLICY_NOT_READY');
  const response=await fetch(`http://${config.host}:${config.port}/v1/status`,{headers:{Authorization:`Bearer ${config.secret}`},signal:AbortSignal.timeout(10000)});
  if(!response.ok)fail('BRIDGE_STATUS_UNAVAILABLE');const status=await response.json();
  const failed=status.lines.filter(line=>line.line_id===target);
  const eligible=status.lines.filter(line=>line.line_id!==target);
  if(failed.length!==1 || failed[0].status!=='error' || failed[0].diagnostics?.transport?.ready_state!==3 ||
      eligible.some(line=>line.status!=='connected'||!line.is_paired||line.qr))fail('IDENTITY_PREFLIGHT_FAILED');
  const stores=status.storage?.stores;
  if(!Array.isArray(stores) || stores.filter(s=>s.failed).length!==1 ||
      !stores.some(s=>s.failed&&s.line_id===target&&s.kind==='auth'))fail('FAILURE_NOT_IDENTITY_SCOPED');
  if(status.storage.issues.some(i=>i.level==='critical'&&!(i.line_id===target&&i.kind==='auth'&&i.code==='STORE_UNAVAILABLE')))fail('SHARED_STORAGE_UNSAFE');
  const proof={version:1,revision:REVISION,release,target,eligible:eligible.map(l=>l.line_id).sort(),
    pid:Number(before.MainPID),process_start:await processStart(before.MainPID),boot:await boot(),prepared_at:Date.now(),
    minimum_sequences:Object.fromEntries(stores.filter(s=>!(s.line_id===target&&s.kind==='auth')).map(s=>[s.line_id?`${s.line_id}/${s.kind}.json`:'outbox.json',s.sequence]))};
  if(!proof.process_start)fail('OWNER_IDENTITY_UNPROVEN');
  await atomicWrite(recordFile,proof);
  return {prepared:true,target,eligible:proof.eligible,pid:proof.pid};
}

async function finalize(config,recordFile) {
  const proof=JSON.parse(await fs.readFile(recordFile,'utf8'));
  await verifySource(proof.release);
  if(await boot()!==proof.boot || await processStart(proof.pid)===proof.process_start)fail('OLD_OWNER_NOT_GONE');
  const after=unit();
  if(after.ControlGroup) {
    try {if((await fs.readFile('/sys/fs/cgroup'+after.ControlGroup+'/cgroup.procs','utf8')).trim())fail('OLD_CGROUP_NOT_EMPTY');}
    catch(error){if(error.code!=='ENOENT')throw error;}
  }
  const logs=execFileSync('journalctl',['-u','whatsapp-bridge.service','--since',`@${Math.floor(proof.prepared_at/1000)}`,'-o','json','--no-pager'],{encoding:'utf8',maxBuffer:8*1024*1024});
  const events=[];
  for(const raw of logs.trim().split('\n').filter(Boolean)) {
    const row=JSON.parse(raw);if(String(row._PID)!==String(proof.pid))continue;
    try{const e=JSON.parse(row.MESSAGE);if(e&&typeof e==='object')events.push(e);}catch{
      if(/error|exception|rejection|fatal/i.test(row.MESSAGE))fail('UNCLASSIFIED_SHUTDOWN_ERROR');
    }
  }
  validateLegacyExit(proof,after,events);
  const release=await acquireDataDirectory(config.dataDir);
  try {
    const summaries=await validateStoppedStores(config.dataDir,proof.target,proof.eligible,proof.minimum_sequences);
    const manifest=await new FileStore(path.join(config.dataDir,'lines.json'),()=>{throw new Error('MANIFEST_MISSING');}).load();
    const record=await publishQuarantine({manifest,id:proof.target,directory:path.join(config.dataDir,proof.target),actor:'approved-production-transition',
      proof:{kind:'process_exit',previous_pid:proof.pid,previous_process_start:proof.process_start,previous_revision:proof.revision},
      assertStopped:release.assertHeld});
    return {...removalResult(record),eligible:proof.eligible,validated_stores:Object.keys(summaries).length,stores:summaries};
  } finally {await release();}
}

if(import.meta.url===pathToFileURL(process.argv[1]??'').href) {
  const [command,target,recordFile,release]=process.argv.slice(2);
  const config=readConfig();
  const task=command==='prepare'&&/^\d{5,32}$/.test(target??'')&&recordFile&&release ? prepare(config,target,recordFile,release) :
    command==='finalize'&&recordFile ? finalize(config,recordFile) : Promise.reject(new Error('INVALID_TRANSITION_COMMAND'));
  task.then(result=>console.log(JSON.stringify(result))).catch(error=>{console.error(JSON.stringify({ok:false,code:/^[A-Z_]+$/.test(error.message)?error.message:'TRANSITION_BLOCKED'}));process.exitCode=1;});
}
