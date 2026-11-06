// Reviewed lifecycle hooks for exactly @whiskeysockets/baileys 7.0.0-rc14.
// No wire-format or cryptography changes. Every accepted callback, including
// detached work, exposes its complete promise to the owning auth session.
import fs from 'node:fs/promises';
import path from 'node:path';
import crypto from 'node:crypto';
import { fileURLToPath } from 'node:url';

const marker = '/* bridge-storage-lifecycle-v1 */';
const hash = text => crypto.createHash('sha256').update(text).digest('hex');
const defaultRoot = fileURLToPath(new URL('../node_modules/@whiskeysockets/baileys/', import.meta.url));
const edit = (before, after) => ({ before, after });
const hook = factory => edit(`export const ${factory} = (config) => {`,
  `export const ${factory} = (config) => {\n    ${marker}\n    const bridgeTask = config.bridgeTask ?? (task => task);\n    const bridgeDetachedTask = config.bridgeDetachedTask ?? bridgeTask;`);
const named = (opening, ending, task = 'bridgeTask') => [
  edit(opening, opening.replace('= async ', `= ${task}(async `)),
  edit(ending, ending.replace('};', '});')),
];
const listener = (opening, ending, task = 'bridgeTask') => [
  edit(opening, opening.replace(', async ', `, ${task}(async `)),
  edit(ending, ending.replace('});', '}));')),
];

export const patchManifest = Object.freeze({
  'lib/Socket/socket.js': {
    sha256: 'ff8b19ff02491fa080ee371f066d49c94acb903207dd0d9fdb5548e5a594fb4a',
    edits: [
      hook('makeSocket'),
      // Passive, bounded diagnostics only: no extra pings, deadline changes,
      // payload logging or dependency on a successful observer callback.
      edit('    const bridgeDetachedTask = config.bridgeDetachedTask ?? bridgeTask;',
        `    const bridgeDetachedTask = config.bridgeDetachedTask ?? bridgeTask;
    const bridgeDiagnostic = sample => { try { config.bridgeConnectionDiagnostics?.(sample); } catch {} };`),
      edit('            lastDateRecv = new Date();\n            let anyTriggered = false;',
        "            lastDateRecv = new Date();\n            bridgeDiagnostic({event: 'decoded_frame'});\n            let anyTriggered = false;"),
      edit('    const startKeepAliveRequest = () => (keepAliveReq = setInterval(() => {',
        `    const startKeepAliveRequest = () => {
        let bridgeLastTick = performance.now();
        return (keepAliveReq = setInterval(() => {
        const bridgeTick = performance.now();
        const bridgeTimerDelay = Math.max(0, bridgeTick - bridgeLastTick - keepAliveIntervalMs);
        bridgeLastTick = bridgeTick;`),
      edit('        const diff = Date.now() - lastDateRecv.getTime();',
        "        const diff = Date.now() - lastDateRecv.getTime();\n        bridgeDiagnostic({event: 'heartbeat_tick', frame_age_ms: diff, timer_delay_ms: bridgeTimerDelay});"),
      edit('        if (diff > keepAliveIntervalMs + 5000) {',
        "        if (diff > keepAliveIntervalMs + 5000) {\n            bridgeDiagnostic({event: 'heartbeat_timeout'});"),
      edit("                content: [{ tag: 'ping', attrs: {} }]\n            }).catch(err => {",
        "                content: [{ tag: 'ping', attrs: {} }]\n            }).then(() => {\n                bridgeDiagnostic({event: 'heartbeat_result', rtt_ms: performance.now() - bridgeTick});\n            }).catch(err => {\n                bridgeDiagnostic({event: 'heartbeat_error'});"),
      edit('    }, keepAliveIntervalMs));', '    }, keepAliveIntervalMs));\n    };'),
      ...named('    const rotateSignedPreKey = async () => {', '    };\n    const executeUSyncQuery = async (usyncQuery) => {'),
      ...named('    const requestPairingCode = async (phoneNumber, customPairingCode) => {', '    };\n    async function generatePairingKey() {'),
      // Track the actual upload/retry operation, even if its timeout race wins.
      edit('            uploadLogic(0),', '            bridgeDetachedTask(() => uploadLogic(0))(),'),
      ...listener("    ws.on('open', async () => {", "    });\n    ws.on('error', mapWebSocketError(end));"),
      ...listener("    ws.on('CB:iq,type:set,pair-device', async (stanza) => {", '    });\n    // device paired for the first time'),
      ...listener("    ws.on('CB:iq,,pair-success', async (stanza) => {", '    });\n    // login complete'),
      ...listener("    ws.on('CB:success', async (node) => {", "    });\n    ws.on('CB:stream:error', (node) => {"),
      // Admission occurs now; execution still starts on nextTick as upstream.
      edit('            process.nextTick(async () => {',
        '            void bridgeDetachedTask(async () => {\n                await new Promise(resolve => process.nextTick(resolve));'),
      edit("                    logger.error({ error, lid: myLID }, 'Failed to create own LID session');\n                }\n            });",
        "                    logger.error({ error, lid: myLID }, 'Failed to create own LID session');\n                }\n            })();"),
      // Edge routing changes credentials synchronously, so cover its complete
      // callback without delaying native query-response/TAG event handlers.
      edit("    ws.on('CB:ib,,edge_routing', (node) => {", "    ws.on('CB:ib,,edge_routing', bridgeTask((node) => {"),
      edit('    });\n    let didStartBuffer = false;', '    }));\n    let didStartBuffer = false;'),
      edit("        type: 'md',\n        ws,", "        type: 'md',\n        bridgeTaskVersion: 1,\n        bridgeConnectionDiagnosticsVersion: 1,\n        ws,"),
    ],
  },
  'lib/Socket/chats.js': {
    sha256: '1e70aea7b1164d92757a63ee7f375cf537c7c9126478c6e7bbf1daf930abb691',
    edits: [
      hook('makeChatsSocket'),
      // Own resync's post-transaction credential events and the full buffered
      // upsert callback; wrapping just Signal transactions is insufficient.
      edit('    const resyncAppState = ev.createBufferedFunction(async (collections, isInitialSync) => {',
        '    const resyncAppState = bridgeTask(ev.createBufferedFunction(async (collections, isInitialSync) => {'),
      edit('    });\n    /**\n     * fetch the profile picture of a user/group', '    }));\n    /**\n     * fetch the profile picture of a user/group'),
      ...named('    const executeInitQueries = async () => {', '    };\n    const upsertMessage = ev.createBufferedFunction(async (msg, type) => {', 'bridgeDetachedTask'),
      edit('    const upsertMessage = ev.createBufferedFunction(async (msg, type) => {',
        '    const upsertMessage = bridgeTask(ev.createBufferedFunction(async (msg, type) => {'),
      edit("    });\n    ws.on('CB:presence', handlePresenceUpdate);", "    }));\n    ws.on('CB:presence', handlePresenceUpdate);"),
      ...listener("    ws.on('CB:ib,,dirty', async (node) => {", "    });\n    ev.on('connection.update', ({ connection, receivedPendingNotifications }) => {"),
      ...listener("    ev.on('lid-mapping.update', async ({ lid, pn }) => {", '    });\n    registerSocketEndHandler(() => {', 'bridgeDetachedTask'),
      edit("        resyncAppState(collections, false).catch(error => onUnexpectedError(error, 'blocked collections resync'));",
        "        bridgeDetachedTask(() => resyncAppState(collections, false))().catch(error => onUnexpectedError(error, 'blocked collections resync'));"),
      // This timer's body can mutate credentials. A timer that has already
      // fired becomes owned work; retirement cancels timers not yet fired.
      edit('        awaitingSyncTimeout = setTimeout(() => {', '        awaitingSyncTimeout = setTimeout(bridgeTask(() => {'),
      edit('        }, 20000);', '        }), 20000);'),
      edit('    return {\n        ...sock,', `    return {\n        ...sock,\n        bridgePrepareRetirement: () => {\n            sock.bridgePrepareRetirement?.();\n            clearTimeout(awaitingSyncTimeout);\n            awaitingSyncTimeout = undefined;\n            clearTimeout(historySyncPausedTimeout);\n            historySyncPausedTimeout = undefined;\n        },`),
    ],
  },
  'lib/Socket/messages-send.js': {
    sha256: '81387f00b457ea28b481b1320bfdbf5ada766305f6f2bc7033931f49811519dc',
    edits: [
      hook('makeMessagesSocket'),
      edit('                resolveIssuanceJid(destinationJid, sock.serverProps.lidTrustedTokenIssueToLid, getLIDForPN, getPNForLID)',
        '                bridgeDetachedTask(() => resolveIssuanceJid(destinationJid, sock.serverProps.lidTrustedTokenIssueToLid, getLIDForPN, getPNForLID)'),
      edit('                    inFlightTcTokenIssuance.delete(tcTokenJid);\n                });',
        '                    inFlightTcTokenIssuance.delete(tcTokenJid);\n                }))();'),
      edit('                    process.nextTick(async () => {\n                        await messageMutex.mutex(() => upsertMessage(fullMsg, \'append\'));\n                    });',
        '                    void bridgeDetachedTask(async () => {\n                        await new Promise(resolve => process.nextTick(resolve));\n                        await messageMutex.mutex(() => upsertMessage(fullMsg, \'append\'));\n                    })();'),
    ],
  },
  'lib/Socket/messages-recv.js': {
    sha256: '850445e6f9e076b1beb98034cbc5cf79562dfda26a44f5a874a84e3609bfd710',
    edits: [
      hook('makeMessagesRecvSocket'),
      edit('        void (async () => {\n            const normalizedJid = jidNormalizedUser(from);',
        '        void bridgeDetachedTask(async () => {\n            const normalizedJid = jidNormalizedUser(from);'),
      edit('                    void (async () => {\n                        try {', '                    void bridgeDetachedTask(async () => {\n                        try {'),
      // Preserve the complete delayed placeholder result, including its update.
      edit('                        requestPlaceholderResend(cleanKey, msgData)', '                        bridgeDetachedTask(() => requestPlaceholderResend(cleanKey, msgData)'),
      edit("                            logger.warn({ err, msgId: msg.key.id }, 'failed to request placeholder resend for unavailable message');\n                        });",
        "                            logger.warn({ err, msgId: msg.key.id }, 'failed to request placeholder resend for unavailable message');\n                        }))();"),
      edit('    async function flushTcTokenIndex() {', '    const flushTcTokenIndex = bridgeDetachedTask(async () => {'),
      edit("        return authState.keys.set({ tctoken: write });\n    }\n    function scheduleTcTokenIndexSave() {",
        "        return authState.keys.set({ tctoken: write });\n    });\n    function scheduleTcTokenIndexSave() {"),
      edit('    async function pruneExpiredTcTokens() {', '    const pruneExpiredTcTokens = bridgeDetachedTask(async () => {'),
      edit("            logger.warn({ err: err?.message }, 'failed to prune expired tctokens');\n        }\n    }\n    return {",
        "            logger.warn({ err: err?.message }, 'failed to prune expired tctokens');\n        }\n    });\n    return {"),
      edit('            offlineNodeProcessor.enqueue(type, node);', '            await offlineNodeProcessor.enqueue(type, node);'),
      // A native transport close can reject a receipt after decryption. Keep
      // the authenticated plaintext on its existing upsert path; never retry a
      // send here. Unconfigured consumers retain upstream error behavior.
      edit('                        await sendReceipt(msg.key.remoteJid, participant, [msg.key.id], type);',
        "                        await sendReceipt(msg.key.remoteJid, participant, [msg.key.id], type).catch(error => {\n                            if (!config.bridgeTask) throw error;\n                            onUnexpectedError(error, 'receipt after decoded message');\n                        });"),
      edit("                            await sendReceipt(jid, undefined, [msg.key.id], 'hist_sync'); // TODO: investigate",
        "                            await sendReceipt(jid, undefined, [msg.key.id], 'hist_sync').catch(error => {\n                                if (!config.bridgeTask) throw error;\n                                onUnexpectedError(error, 'history receipt after decoded message');\n                            }); // TODO: investigate"),
      edit("                        await sendMessageAck(node);\n                        logger.debug({ key: msg.key }, 'processed newsletter message without receipts');",
        "                        await sendMessageAck(node).catch(error => {\n                            if (!config.bridgeTask) throw error;\n                            onUnexpectedError(error, 'ack after decoded newsletter message');\n                        });\n                        logger.debug({ key: msg.key }, 'processed newsletter message without receipts');"),
      edit("                        await sendMessageAck(node);\n                        // Don't return — fall through to upsertMessage so the stub is emitted",
        "                        await sendMessageAck(node).catch(error => {\n                            if (!config.bridgeTask) throw error;\n                            onUnexpectedError(error, 'ack before unavailable message stub');\n                        });\n                        // Don't return — fall through to upsertMessage so the stub is emitted"),
      edit('                            await sendMessageAck(node, NACK_REASONS.UnhandledError);\n                        });',
        "                            await sendMessageAck(node, NACK_REASONS.UnhandledError).catch(error => {\n                                if (!config.bridgeTask) throw error;\n                                onUnexpectedError(error, 'nack before failed-decryption message stub');\n                            });\n                        });"),
      ...listener("    ws.on('CB:message', async (node) => {", "    });\n    ws.on('CB:call', async (node) => {"),
      ...listener("    ws.on('CB:call', async (node) => {", "    });\n    ws.on('CB:receipt', async (node) => {"),
      ...listener("    ws.on('CB:receipt', async (node) => {", "    });\n    ws.on('CB:notification', async (node) => {"),
      ...listener("    ws.on('CB:notification', async (node) => {", "    });\n    ws.on('CB:ack,class:message', (node) => {"),
      edit("    ws.on('CB:ack,class:message', (node) => {\n        handleBadAck(node).catch(error => onUnexpectedError(error, 'handling bad ack'));\n    });",
        "    ws.on('CB:ack,class:message', bridgeTask((node) => {\n        return handleBadAck(node).catch(error => onUnexpectedError(error, 'handling bad ack'));\n    }));"),
      ...listener("    ev.on('call', async ([call]) => {", '    });\n    /** timestamp of last tctoken prune run', 'bridgeDetachedTask'),
      edit('    return {\n        ...sock,', `    return {\n        ...sock,\n        bridgePrepareRetirement: () => {\n            sock.bridgePrepareRetirement?.();\n            if (tcTokenIndexTimer) return flushTcTokenIndex();\n        },`),
    ],
  },
  'lib/Utils/offline-node-processor.js': {
    sha256: '7c50dea48a0f2be6284b588ea44f656b9903c9b5112b104d8791cd1411cc7840',
    edits: [
      edit('    let isProcessing = false;', `    ${marker}\n    let isProcessing = false;\n    let processingPromise;`),
      edit('        if (isProcessing) {\n            return;\n        }\n        isProcessing = true;',
        '        if (isProcessing) {\n            return processingPromise;\n        }\n        isProcessing = true;\n        let complete;\n        const activePromise = processingPromise = new Promise(resolve => { complete = resolve; });'),
      edit('            let processedInBatch = 0;', '            let processedInBatch = 0;\n            try {'),
      edit('            isProcessing = false;\n        };',
        '            } finally {\n                isProcessing = false;\n                complete();\n            }\n        };'),
      edit("        promise().catch(error => deps.onUnexpectedError(error, 'processing offline nodes'));",
        "        promise().catch(error => deps.onUnexpectedError(error, 'processing offline nodes'));\n        return activePromise;"),
    ],
  },
  'lib/Utils/auth-utils.js': {
    sha256: 'd85b07863507235407a3a1430fbb1cfae108c4023f746c50393d9933578caf15',
    edits: [
      edit('        isInTransaction,\n        transaction: async (work, key) => {',
        `        isInTransaction,\n        ${marker}\n        // Detached socket jobs must not append to a transaction that has\n        // already committed while their network request was in flight.\n        bridgeRunOutsideTransaction: work => txStorage.run(undefined, work),\n        transaction: async (work, key) => {`),
    ],
  },
});

function replaceOnce(source, before, after, filename) {
  const first = source.indexOf(before);
  if (first < 0 || source.indexOf(before, first + before.length) !== -1) {
    throw new Error(`BAILEYS_PATCH_ANCHOR_MISMATCH:${filename}`);
  }
  return source.slice(0, first) + after + source.slice(first + before.length);
}

export function transformProviderSource(filename, source, { reverse = false } = {}) {
  const spec = patchManifest[filename];
  if (!spec) throw new Error('BAILEYS_PATCH_UNKNOWN_FILE');
  const changes = reverse ? [...spec.edits].reverse() : spec.edits;
  for (const { before, after } of changes) {
    source = replaceOnce(source, reverse ? after : before, reverse ? before : after, filename);
  }
  return source;
}

export async function patchBaileys(root = defaultRoot, { verifyOnly = false } = {}) {
  root = path.resolve(root);
  // A private test checkout must never patch a shared/deployed dependency tree
  // through a node_modules symlink. Verification itself remains read-only.
  if (!verifyOnly && await fs.realpath(root) !== root) throw new Error('BAILEYS_PATCH_ROOT_SYMLINK');
  const manifest = JSON.parse(await fs.readFile(path.join(root, 'package.json'), 'utf8'));
  if (manifest.version !== '7.0.0-rc14') throw new Error('BAILEYS_PATCH_VERSION_MISMATCH');
  const pending = [];
  // Validate all dependencies before mutating any. A partially completed prior
  // installation is safe to resume; runtime verification rejects partial sets.
  for (const [relative, spec] of Object.entries(patchManifest)) {
    const filename = path.join(root, relative);
    if (!verifyOnly && await fs.realpath(filename) !== filename) throw new Error('BAILEYS_PATCH_FILE_SYMLINK');
    const source = await fs.readFile(filename, 'utf8');
    if (hash(source) === spec.sha256) {
      if (verifyOnly) throw new Error(`BAILEYS_LIFECYCLE_PATCH_REQUIRED:${relative}`);
      const updated = transformProviderSource(relative, source);
      if (transformProviderSource(relative, updated, { reverse: true }) !== source) throw new Error('BAILEYS_PATCH_ROUNDTRIP_FAILED');
      pending.push({ filename, source: updated });
      continue;
    }
    let original;
    try { original = transformProviderSource(relative, source, { reverse: true }); }
    catch { throw new Error(`BAILEYS_PATCH_SOURCE_MISMATCH:${relative}`); }
    if (hash(original) !== spec.sha256 || transformProviderSource(relative, original) !== source) {
      throw new Error(`BAILEYS_PATCH_SOURCE_MISMATCH:${relative}`);
    }
  }
  for (const { filename, source } of pending) {
    const temporary = `${filename}.bridge-patch-${crypto.randomUUID()}.tmp`;
    try {
      const info = await fs.stat(filename);
      const handle = await fs.open(temporary, 'wx', info.mode & 0o777);
      try { await handle.writeFile(source); await handle.sync(); } finally { await handle.close(); }
      await fs.rename(temporary, filename);
      const directory = await fs.open(path.dirname(filename), 'r');
      try { await directory.sync(); } finally { await directory.close(); }
    } finally { await fs.unlink(temporary).catch(error => { if (error.code !== 'ENOENT') throw error; }); }
  }
  return { version: 1, files: Object.keys(patchManifest).length, patched: pending.length };
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const args = process.argv.slice(2);
  let root = defaultRoot, verifyOnly = false;
  for (let index = 0; index < args.length; index++) {
    if (args[index] === '--verify') verifyOnly = true;
    else if (args[index] === '--root' && args[index + 1]) root = path.resolve(args[++index]);
    else throw new Error('BAILEYS_PATCH_INVALID_ARGUMENT');
  }
  await patchBaileys(root, { verifyOnly });
}
