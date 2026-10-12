const sessionPrefixes = [
  'Closing session:', 'Opening session:', 'Removing old closed session:',
  'Session already closed', 'V1 session storage migration error: registrationId',
  'Unhandled bucket type (for naming):',
];

// libsignal 6.0.0 bypasses Baileys's pino logger and sends complete Signal
// sessions/key buffers straight to console. Restrict its structured dumps at
// the console boundary, without inspecting or stringifying secret arguments.
// Keep its harmless plain diagnostics and the bridge's own logs available.
export function installLibsignalConsoleFilter(target = console) {
  const originals = new Map();
  const wrappers = new Map();
  for (const method of ['log', 'info', 'warn', 'error', 'debug', 'dir', 'table', 'trace']) {
    const original = target[method];
    if (typeof original !== 'function') continue;
    function filteredConsole(...args) {
      const prefix = typeof args[0] === 'string' ? args[0] : '';
      const sensitivePrefix = sessionPrefixes.some(value => prefix.startsWith(value));
      const structured = args.some(value => value !== null && ['object', 'function'].includes(typeof value));
      if (sensitivePrefix || structured) {
        const trace = {};
        Error.captureStackTrace(trace, filteredConsole);
        const caller = String(trace.stack).split('\n')[1] ?? '';
        if (/[\\/]node_modules[\\/]libsignal[\\/]/.test(caller)) return;
      }
      return original.apply(target, args);
    }
    originals.set(method, original);
    wrappers.set(method, filteredConsole);
    target[method] = filteredConsole;
  }
  return () => {
    for (const [method, original] of originals) if (target[method] === wrappers.get(method)) target[method] = original;
  };
}
