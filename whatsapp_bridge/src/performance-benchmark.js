// Synthetic, local-only benchmark. No Bridge/socket/auth initialization.
import {mkdtemp,writeFile,stat,rm} from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import {performance,monitorEventLoopDelay} from 'node:perf_hooks';
import {setTimeout as delay} from 'node:timers/promises';
import {FileStore} from './storage.js';
import {JournalStore} from './journal-store.js';
const directory=await mkdtemp(path.join(os.tmpdir(),'bridge-benchmark-'));
try {
  const messages={};
  for(let i=0;i<18000;i++) messages['fixture'+i]={text:'x'.repeat(1000),timestamp:i,phone:'synthetic',message:{key:{id:'fixture'+i}}};
  const fixture=JSON.stringify({messages,media:{},operations:{}});
  const results=[];
  for(const [name,Store] of [['before',FileStore],['after',JournalStore]]) {
    const filename=path.join(directory,name+'.json');await writeFile(filename,fixture);
    const store=await new Store(filename,()=>({})).load();
    const loop=monitorEventLoopDelay({resolution:1});loop.enable();await delay(10);
    const started=performance.now();let bytes=0;
    for(let i=0;i<5;i++){
      await store.update(d=>{d.messages['send'+i]={pending:true,text:'synthetic',timestamp:i}});
      await store.update(d=>{d.messages['send'+i].ack=true});
      await store.update(d=>{d.messages['send'+i].pending=false});
      if(name==='before')bytes+=(await stat(filename)).size*3;
    }
    const duration=performance.now()-started;await delay(10);loop.disable();
    if(name==='after')bytes=(await stat(filename+'.journal')).size;
    results.push({name,updates:15,duration_ms:Math.round(duration),bytes_written:bytes,
      event_loop_max_ms:Number((loop.max/1e6).toFixed(2)),event_loop_p99_ms:Number((loop.percentile(99)/1e6).toFixed(2))});
  }
  console.log(JSON.stringify({fixture_bytes:Buffer.byteLength(fixture),node:process.version,results},null,2));
} finally {await rm(directory,{recursive:true,force:true});}
