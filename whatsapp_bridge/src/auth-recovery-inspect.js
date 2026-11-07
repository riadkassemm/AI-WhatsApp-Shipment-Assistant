// Read-only inspection of an already preserved, private identity copy. Never
// loads a JournalStore, acquires an owner, repairs a tail or creates credentials.
import fs from 'node:fs/promises';
import { constants } from 'node:fs';
import crypto from 'node:crypto';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import { decode, readJournal } from './journal-store.js';
import { assessAuthRecovery } from './auth-recovery.js';

async function privateFile(filename) {
  try {
    const handle = await fs.open(filename, constants.O_RDONLY | constants.O_NOFOLLOW);
    try {
      const s = await handle.stat();
      if (!s.isFile() || (s.mode & 0o077) || s.uid !== process.getuid()) throw new Error('PRIVATE_EVIDENCE_REQUIRED');
      const hash = crypto.createHash('sha256');
      for await (const chunk of handle.createReadStream({autoClose: false})) hash.update(chunk);
      return hash.digest('hex');
    } finally { await handle.close(); }
  } catch (error) { if (error.code === 'ENOENT') return null; throw error; }
}

export async function inspectPrivateAuthCopy(directory) {
  directory = path.resolve(directory);
  const s = await fs.lstat(directory);
  if (!s.isDirectory() || s.isSymbolicLink() || (s.mode & 0o077) || s.uid !== process.getuid()) throw new Error('PRIVATE_EVIDENCE_REQUIRED');
  const base = path.join(directory, 'auth.json');
  const names = ['', '.snapshot', '.journal', '.auth-active', '.auth-intent', '.auth-failure'];
  const before = await Promise.all(names.map(suffix => privateFile(base + suffix)));
  const optional = async suffix => {
    const index = names.indexOf(suffix);
    return before[index] === null ? null : decode(await fs.readFile(base + suffix, 'utf8'));
  };
  const [failure, active, intents, journal] = await Promise.all([
    optional('.auth-failure'), optional('.auth-active'), optional('.auth-intent'),
    readJournal(base, () => { throw new Error('AUTH_BASE_MISSING'); }, {repairTail: false}),
  ]);
  const after = await Promise.all(names.map(suffix => privateFile(base + suffix)));
  if (before.some((hash, i) => hash !== after[i])) throw new Error('EVIDENCE_CHANGED_DURING_READ');
  return assessAuthRecovery({failure, active, intents: intents ?? {operations: []}, journal});
}

if (import.meta.url === pathToFileURL(process.argv[1] ?? '').href) {
  const [flag, directory, extra] = process.argv.slice(2);
  if (flag !== '--private-copy' || !directory || extra) {
    console.error('Usage: node src/auth-recovery-inspect.js --private-copy PRIVATE_IDENTITY_COPY');
    process.exitCode = 2;
  } else {
    try {
      const assessment = await inspectPrivateAuthCopy(directory);
      console.log(JSON.stringify(assessment));
      if (!assessment.authentication_evidence_complete) process.exitCode = 2;
    } catch {
      // Even parser/filesystem errors can contain private file data or names.
      console.log(JSON.stringify({status: 'BLOCKED', recovery_allowed: false,
        authentication_evidence_complete: false, blockers: ['PRIVATE_EVIDENCE_VALIDATION_FAILED']}));
      process.exitCode = 2;
    }
  }
}
