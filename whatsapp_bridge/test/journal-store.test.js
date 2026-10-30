import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtemp, readFile, writeFile, appendFile, rm, stat} from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import {JournalStore} from '../src/journal-store.js';
import {storageTool} from '../src/storage-tool.js';
const gate = () => { let resolve; const promise = new Promise(r => {resolve = r}); return {promise,resolve}; };
async function setup(t, options) {
 const dir = await mkdtemp(path.join(os.tmpdir(), 'bridge-journal-'));
 t.after(()=>rm(dir,{recursive:true,force:true}));
 const store=await new JournalStore(path.join(dir,'state.json'),()=>({messages:{},media:{},operations:{},enabled:true}),options).load();
 return {dir,store};
}

test('18 MiB history: only changed fields appended, binary references survive reload',async t=>{
 const {store}=await setup(t);const history={messages:{},media:{},operations:{}};
 for(let i=0;i<18000;i++)history.messages['m'+i]={body:'synthetic'.repeat(120),timestamp:i};
 await writeFile(store.filename,JSON.stringify(history));await store.load();
 const original=await readFile(store.filename);
 assert(original.length>17.8*1024*1024);
 Object.defineProperty(store.data.messages, 'unrelated', {enumerable:true,configurable:true,get(){throw Error('Whole-history traversal on update')}});
 await store.update(data=>{data.messages.send={pending:true,key:Buffer.from([1,2,3])}});
 delete store.data.messages.unrelated;
 await store.update(data=>{data.messages.send.pending=false;data.messages.send.ack=true});
 assert((await stat(store.filename+'.journal')).size<1500);
 assert.deepEqual(await readFile(store.filename),original,'legacy snapshot not rewritten');
 const recovered=await new JournalStore(store.filename,()=>({})).load();
 assert.equal(Object.keys(recovered.data.messages).length,18001);
 assert.deepEqual(recovered.data.messages.send,{pending:false,key:Buffer.from([1,2,3]),ack:true});
});

test('concurrent changes are atomic and one blocked account does not stall another',async t=>{
 const entered=gate(),release=gate();let blocked=true;
 const {store:a}=await setup(t); const append=a.append;
 a.append=async bytes=>{if(blocked){blocked=false;entered.resolve();await release.promise;}await append(bytes)};
 const {store:b}=await setup(t);
 const tasks=Array.from({length:20},(_,i)=>a.update(d=>{d.messages[i]={id:i}}));
 await entered.promise;await b.update(d=>{d.messages.independent={ack:true}});
 assert(b.data.messages.independent.ack);assert.equal(Object.keys(a.data.messages).length,0);
 release.resolve();await Promise.all(tasks);assert.equal(Object.keys(a.data.messages).length,20);
});

test('bounded queue, fsync failure, incomplete crash tail, and corruption fail closed',async t=>{
 const entered=gate(),release=gate();const {store}=await setup(t,{maxPending:1});const append=store.append;
 store.append=async bytes=>{entered.resolve();await release.promise;await append(bytes)};
 const pending=store.update(d=>{d.enabled=false});await entered.promise;
 await assert.rejects(store.update(d=>{d.enabled=true}),/QUEUE_FULL/);release.resolve();await pending;
 store.append=async()=>{throw Error('synthetic disk failure')};
 await assert.rejects(store.update(d=>{d.enabled=true}));assert.equal(store.data.enabled,false);
 await assert.rejects(store.update(d=>{d.enabled=true}),/RESTART_REQUIRED/);
 await appendFile(store.filename+'.journal','{"partial":');
 const recovered=await new JournalStore(store.filename,()=>({})).load();assert.equal(recovered.data.enabled,false);
 await recovered.update(d=>{d.enabled=true});
 await appendFile(store.filename+'.journal','{"record":"bad","checksum":"wrong"}\n');
 await assert.rejects(new JournalStore(store.filename,()=>({})).load(),/CHECKSUM/);
});

test('offline validation/import/compaction/export preserve post-cutover records and array updates',async t=>{
 const {dir}=await setup(t);const filename=path.join(dir,'outbox.json');
 const store=await new JournalStore(filename,()=>({entries:[],dead_letters:[]})).load();
 const original=await readFile(filename);
 await store.update(d=>{d.entries.push({id:1},{id:2})});
 await store.update(d=>{d.entries.find(e=>e.id===2).attempts=1;d.entries=d.entries.filter(e=>e.id!==1)});
 assert.deepEqual((await new JournalStore(filename,()=>({})).load()).data.entries,[{id:2,attempts:1}]);
 assert.equal((await storageTool('validate',dir)).stores,1);
 await storageTool('import',dir);await storageTool('import',dir);
 await storageTool('compact',dir);await storageTool('compact',dir);
 const current=await new JournalStore(filename,()=>({})).load();
 await current.update(d=>{d.entries.push({id:3})});
 const destination=dir+'-rollback';t.after(()=>rm(destination,{recursive:true,force:true}));
 await storageTool('export',dir,destination);
 assert.deepEqual(JSON.parse(await readFile(path.join(destination,'outbox.json'),'utf8')).entries,[{id:2,attempts:1},{id:3}]);
 assert.deepEqual(await readFile(filename),original);
});

test('snapshot commit before rotation and committed record before lost response both recover',async t=>{
 const {dir,store}=await setup(t);
 await store.update(d=>{const record=d.messages.a={id:'a'};record.ack=true});
 assert.equal(store.data.messages.a.ack,true,'mutations through a newly assigned object persist');
 const originalAppend=store.append;
 store.append=async bytes=>{await originalAppend(bytes);throw Error('crash after sync before publish')};
 await assert.rejects(store.update(d=>{d.messages.b={pending:true}}));
 assert.equal(store.data.messages.b,undefined);
 const recovered=await new JournalStore(store.filename,()=>({})).load();
 assert.equal(recovered.data.messages.b.pending,true);
 const {atomicWrite}=await import('../src/storage.js');
 await atomicWrite(store.filename+'.snapshot',{version:1,seq:recovered.seq,data:recovered.data});
 const next=await new JournalStore(store.filename,()=>({})).load();
 await next.update(d=>{d.messages.b.ack=true});
 const again=await new JournalStore(store.filename,()=>({})).load();
 assert.equal(again.data.messages.a.ack,true);assert.equal(again.data.messages.b.ack,true);
 assert.equal((await readFile(store.filename+'.journal','utf8')).split('\n').filter(Boolean).length,3);
});

test('invalid storage errors do not expose JSON contents',async t=>{
 const {store}=await setup(t);
 await writeFile(store.filename, '{"secret":"synthetic-private-value" broken');
 await assert.rejects(new JournalStore(store.filename,()=>({})).load(), error=>
   error.message==='INVALID_STORAGE_JSON' && !error.message.includes('private'));
});
