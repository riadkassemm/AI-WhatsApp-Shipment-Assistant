// Own process resources before awaiting them. Signals close the same owner even
// while imports, the directory lock, journal replay, or listen are in flight.
export function createRuntime({prepare, process: processOwner = process, logger = console,
  shutdownTimeoutMs = 30_000}) {
  let bridge, server, release, startPromise, serverClose;
  let stopping = false, failed = false, deadline, finish;
  const done = new Promise(resolve => { finish = resolve; });
  const report = (event, error) => {
    const candidate = error?.code ?? error?.message;
    const code = typeof candidate === 'string' && /^[A-Z][A-Z0-9_]{1,80}$/.test(candidate) ? candidate : undefined;
    try { logger.error(JSON.stringify({component: 'whatsapp_bridge', event, ...(code ? {code} : {})})); }
    catch (_) { /* Logging cannot change shutdown ownership or its result. */ }
  };
  const closeServer = () => {
    if (!server) return Promise.resolve();
    serverClose ??= new Promise((resolve, reject) => {
      server.close(error => error && error.code !== 'ERR_SERVER_NOT_RUNNING' ? reject(error) : resolve());
      server.closeIdleConnections?.();
    });
    return serverClose;
  };
  const closeBridge = () => {
    try { return Promise.resolve(bridge?.close()); }
    catch (error) { return Promise.reject(error); }
  };
  const stop = () => {
    if (stopping) return done;
    stopping = true;
    // The diagnostic deadline leaves five seconds before DEV's systemd limit.
    // It never releases a live owner's lock or reports incomplete cleanup as
    // success. Stalled kernel I/O may still require systemd's forced stop.
    deadline = setTimeout(() => {
      failed = true;
      processOwner.exitCode = 1;
      report('shutdown_deadline_exceeded');
    }, shutdownTimeoutMs);
    // Defer only the waiter: close() is invoked immediately to stop admission,
    // retire sockets and abort initialization before another await resumes.
    const earlyClose = closeBridge();
    const earlyServerClose = closeServer();
    earlyClose.catch(() => {}); earlyServerClose.catch(() => {});
    void (async () => {
      let clean = true;
      // A lock acquisition or resource constructor already admitted by start
      // must settle before deciding what this owner has to release.
      await startPromise?.catch(() => {});
      const results = await Promise.allSettled([earlyClose, closeBridge()]);
      // Accepted bridge operations have now drained. An unfinished HTTP body
      // must not consume the remaining stop budget: it cannot submit new work
      // after the bridge's admission fence. A disconnected handler can still
      // be running, so separately await its application promise below.
      try { server?.closeAllConnections?.(); }
      catch (error) { results.push({status: 'rejected', reason: error}); }
      results.push(...await Promise.allSettled([earlyServerClose, closeServer()]));
      // No more request events can be admitted once connection closure settles.
      results.push(...await Promise.allSettled([Promise.resolve().then(() => server?.drainRequests?.())]));
      for (const result of results) if (result.status === 'rejected') {
        clean = false; failed = true; report('shutdown_failed', result.reason);
      }
      if (clean && release) {
        try { await release(); release = null; }
        catch (error) { failed = true; report('storage_lock_release_failed', error); }
      }
      // On cleanup failure, keep the lock for the remaining process lifetime.
      // Authentication markers and journals remain available for recovery.
      clearTimeout(deadline);
      processOwner.exitCode = failed ? 1 : 0;
      const result = {exitCode: processOwner.exitCode};
      finish(result);
    })().catch(error => {
      failed = true; processOwner.exitCode = 1;
      clearTimeout(deadline);
      report('shutdown_failed', error);
      finish({exitCode: 1});
    });
    return done;
  };
  // Keep handlers installed through termination. A second signal must neither
  // bypass the drain nor restore Node's default abrupt signal termination.
  processOwner.on('SIGTERM', stop);
  processOwner.on('SIGINT', stop);
  const start = () => {
    if (startPromise) return startPromise;
    if (stopping) return Promise.resolve();
    startPromise = (async () => {
      const dependencies = await prepare();
      if (stopping) return;
      const {config, acquireDataDirectory, createBridge, createServer} = dependencies;
      release = await acquireDataDirectory(config.dataDir);
      if (stopping) return;
      bridge = createBridge(config);
      if (stopping) return;
      // Bind health before replay. The HTTP layer rejects operational routes
      // until initialize() finishes; a live process is not a ready sender.
      server = createServer(bridge, config);
      // Keep ownership on errors after listen has completed too. An unhandled
      // EventEmitter error would otherwise bypass the same durable drain as a
      // signal. The startup promise additionally observes its own bind error.
      server.on('error', error => {
        failed = true; report('server_failed', error); void stop();
      });
      if (stopping) return;
      await new Promise((resolve, reject) => {
        const cleanup = () => {
          server.removeListener('error', onError);
          server.removeListener('close', onClose);
          server.removeListener('listening', onListening);
        };
        const onError = error => { cleanup(); reject(error); };
        const onClose = () => {
          cleanup();
          if (stopping) resolve();
          else reject(new Error('SERVER_CLOSED_DURING_STARTUP'));
        };
        const onListening = () => { cleanup(); resolve(); };
        server.once('error', onError);
        server.once('close', onClose);
        server.once('listening', onListening);
        try { server.listen(config.port, config.host); }
        catch (error) { onError(error); }
      });
      if (stopping) return;
      await bridge.initialize();
      if (stopping) return;
      bridge.startWorkers();
      logger.info(`WhatsApp linked-device bridge listening on ${config.host}:${config.port}`);
    })();
    // Do not await stop from inside startup: stop owns startup's settlement.
    startPromise.catch(error => {
      const cancelled = stopping && (error?.code === 'STOPPING' || error?.name === 'AbortError');
      if (!cancelled) { failed = true; report('startup_failed', error); }
      void stop();
    });
    return startPromise;
  };
  return {start, stop, done, get stopping() { return stopping; }};
}
