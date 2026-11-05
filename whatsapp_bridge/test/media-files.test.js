import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import crypto from 'node:crypto';
import { MediaFiles } from '../src/media-files.js';

async function fixture(t) {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), 'bridge-media-files-'));
  t.after(() => fs.rm(directory, {recursive: true, force: true}));
  return {directory, filename: path.join(directory, `${crypto.randomUUID()}.bin`)};
}

test('media restart cleanup removes only recognized unpublished files and syncs the directory', async t => {
  const {directory, filename} = await fixture(t);
  const temporary = `${filename}.${crypto.randomUUID()}.tmp`;
  for (const name of [filename, temporary, path.join(directory, 'unrelated.tmp')]) await fs.writeFile(name, 'retained');
  let synchronized = false;
  const writer = new MediaFiles({...fs, open: async (...args) => {
    const handle = await fs.open(...args);
    if (args[0] !== directory) return handle;
    return {close: () => handle.close(), sync: async () => {await handle.sync(); synchronized = true;}};
  }});
  await writer.prepare(directory);
  assert.equal(synchronized, true);
  assert.deepEqual((await fs.readdir(directory)).sort(), [path.basename(filename), 'unrelated.tmp'].sort());
  assert.equal(await fs.readFile(filename, 'utf8'), 'retained');
});

for (const stage of ['write', 'sync', 'rename']) test(`failed media ${stage} retries cannot accumulate temporary files`, async t => {
  const {directory, filename} = await fixture(t);
  let blocked = true, allocations = 0;
  const failure = () => Object.assign(new Error('EIO'), {code: 'EIO'});
  const writer = new MediaFiles({...fs,
    unlink: async (...args) => {if (blocked) throw failure(); return fs.unlink(...args);},
    rename: async (...args) => {if (blocked && stage === 'rename') throw failure(); return fs.rename(...args);},
    open: async (...args) => {
      const handle = await fs.open(...args);
      if (args[1] !== 'wx') return handle;
      allocations++;
      return {close: () => handle.close(), writeFile: async data => {
        await handle.writeFile(data); if (blocked && stage === 'write') throw failure();
      }, sync: async () => {if (blocked && stage === 'sync') throw failure(); await handle.sync();}};
    },
  });
  for (let attempt = 0; attempt < 4; attempt++) await assert.rejects(writer.write(filename, Buffer.from('synthetic media')), {code: 'EIO'});
  assert.equal(allocations, 1, 'cleanup must succeed before allocating another attempt');
  assert.equal((await fs.readdir(directory)).length, 1);
  blocked = false;
  await writer.write(filename, Buffer.from('recovered media'));
  assert.deepEqual(await fs.readdir(directory), [path.basename(filename)]);
  assert.equal(await fs.readFile(filename, 'utf8'), 'recovered media');
});

test('failed directory sync retains cleanup obligation after temporary unlink', async t => {
  const {directory, filename} = await fixture(t);
  let blocked = true, allocations = 0;
  const writer = new MediaFiles({...fs,
    rename: async (...args) => {if (blocked) throw Object.assign(new Error('EIO'), {code: 'EIO'}); return fs.rename(...args);},
    open: async (...args) => {
      const handle = await fs.open(...args);
      if (args[1] === 'wx') {allocations++; return handle;}
      return {close: () => handle.close(), sync: async () => {
        if (blocked) throw Object.assign(new Error('EIO'), {code: 'EIO'});
        await handle.sync();
      }};
    },
  });
  for (let attempt = 0; attempt < 3; attempt++) await assert.rejects(writer.write(filename, Buffer.from('bytes')), {code: 'EIO'});
  assert.equal(allocations, 1);
  assert.deepEqual(await fs.readdir(directory), []);
  blocked = false;
  await writer.write(filename, Buffer.from('durable'));
  assert.equal(await fs.readFile(filename, 'utf8'), 'durable');
});
