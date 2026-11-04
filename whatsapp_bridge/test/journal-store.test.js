import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtemp, readFile, writeFile, appendFile, rm, stat} from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import crypto from 'node:crypto';
import {JournalStore, readJournal} from '../src/journal-store.js';
import {storageTool} from '../src/storage-tool.js';
const gate = () => { let resolve; const promise = new Promise(r => {resolve = r}); return {promise,resolve}; };
const journalLine = (seq, ops) => {
 const record=JSON.stringify({version:1,seq,ops});
 return JSON.stringify({record,checksum:crypto.createHash('sha256').update(record).digest('hex')})+'\n';
};
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

test('streamed journal replay preserves large records and UTF-8 across read chunks',async t=>{
 const {store}=await setup(t);
 const body='Synthetic \u{1f9a6} \u0628\u064a\u0631\u0648\u062a\n'.repeat(12000);
 await store.update(data=>{data.messages.large={body,key:Buffer.from([0,128,255])}});
 await store.update(data=>{data.messages.large.ack=true;data.enabled=false});
 await store.update(data=>{data.messages.small={body:'after the large record'}});
 assert((await stat(store.filename+'.journal')).size>4*64*1024);
 const recovered=await readJournal(store.filename,()=>({}));
 assert.equal(recovered.seq,3);
 assert.equal(recovered.incompleteTailBytes,0);
 assert.deepEqual(recovered.data,store.data);
});

test('replay never converts the whole journal and handles exact chunk boundaries and blank lines',async t=>{
 const {store}=await setup(t);
 const messageLine=(seq,name,body)=>journalLine(seq,[{path:['messages',name],value:{body}}]);
 const emoji=Buffer.from('\u{1f9a6}');
 const emojiOffset=Buffer.from(messageLine(2,'first','\u{1f9a6}')).indexOf(emoji);
 const body='a'.repeat(64*1024-1-emojiOffset)+'\u{1f9a6}'.repeat(20000);
 const firstMessage=Buffer.from(messageLine(2,'first',body));
 assert.equal(firstMessage[64*1024-1],emoji[0]);
 assert.equal(firstMessage[64*1024],emoji[1],'UTF-8 character must cross a stream chunk');
 const boundaryLine=journalLine(1,[{path:['enabled'],value:false}]).trimEnd().padEnd(64*1024-1,' ')+'\n';
 const journal=boundaryLine+firstMessage.toString('utf8')+'\n'+messageLine(3,'second',body)+'\n';
 const maxStringBytes=256*1024;
 assert(Buffer.byteLength(journal)>maxStringBytes);
 assert(firstMessage.length<maxStringBytes);
 await writeFile(store.filename+'.journal',journal);
 // Model V8's whole-string ceiling cheaply: individual transactions fit,
 // but converting the complete journal must fail with the old reader.
 const toString=Buffer.prototype.toString;
 t.mock.method(Buffer.prototype,'toString',function(...args){
   assert(this.length<=maxStringBytes,'reader converted the whole journal into one string');
   return toString.apply(this,args);
 });
 const recovered=await readJournal(store.filename,()=>({}));
 assert.equal(recovered.seq,3);
 assert.equal(recovered.incompleteTailBytes,0);
 assert.equal(recovered.data.enabled,false);
 assert.deepEqual(recovered.data.messages,{first:{body},second:{body}});
});

test('streamed replay reports and repairs an incomplete tail spanning read chunks',async t=>{
 const {store}=await setup(t);
 await store.update(data=>{data.messages.saved={body:'committed'}});
 const committedSize=(await stat(store.filename+'.journal')).size;
 const tail=Buffer.from('{"record":"'+ 'synthetic-incomplete-\u{1f9a6}'.repeat(20000));
 await appendFile(store.filename+'.journal',tail);
 const inspected=await readJournal(store.filename,()=>({}));
 assert.equal(inspected.incompleteTailBytes,tail.length);
 assert.deepEqual(inspected.data,store.data);
 assert.equal((await stat(store.filename+'.journal')).size,committedSize+tail.length,'inspection must remain read-only');
 const repaired=await new JournalStore(store.filename,()=>({})).load();
 assert.equal(repaired.incompleteTailBytes,tail.length);
 assert.equal((await stat(store.filename+'.journal')).size,committedSize);
 await repaired.update(data=>{data.messages.next={body:'after repair'}});
 assert.deepEqual((await readJournal(store.filename,()=>({}))).data,repaired.data);
});

test('streamed replay keeps checksum and sequence failures after large records fail closed',async t=>{
 const {store}=await setup(t);
 await store.update(data=>{data.messages.large={body:'synthetic'.repeat(30000)}});
 const committed=await readFile(store.filename+'.journal');
 const record=JSON.stringify({version:1,seq:3,ops:[{path:['enabled'],value:false}]});
 const checksum=crypto.createHash('sha256').update(record).digest('hex');
 await appendFile(store.filename+'.journal',JSON.stringify({record,checksum})+'\n');
 await assert.rejects(readJournal(store.filename,()=>({})),/JOURNAL_SEQUENCE_GAP/);
 await writeFile(store.filename+'.journal',committed);
 await appendFile(store.filename+'.journal',JSON.stringify({record,checksum:'wrong'})+'\n');
 await appendFile(store.filename+'.journal','{"partial":');
 const corruptSize=(await stat(store.filename+'.journal')).size;
 await assert.rejects(readJournal(store.filename,()=>({}),{repairTail:true}),/JOURNAL_CHECKSUM_FAILED/);
 assert.equal((await stat(store.filename+'.journal')).size,corruptSize,'a corrupt complete record must not allow tail repair');
});
