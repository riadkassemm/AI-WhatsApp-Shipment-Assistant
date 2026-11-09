// Administrative commands belong to the process holding the directory lock.
// HTTP/Redis timeouts never establish transport release or credential validity.
import crypto from 'node:crypto';
import {BridgeError} from './config.js';

const terminal = new Set(['succeeded', 'failed', 'interrupted']);
const actions = new Set(['reconnect', 'pause', 'resume']);
const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const fail = code => {const error = new BridgeError(409, code, 'This administrative action is unavailable. Refresh diagnostics.'); error.delivery_uncertain = false; return error;};
const control = line => line.store?.data?.administration ?? {version: 0, paused: false, operations: {}};
const reconnectConfirmMs = 120_000;

export class Management {
  constructor(bridge) { this.bridge = bridge; this.epoch = crypto.randomUUID(); this.tasks = new Map(); }
  paused(line) { return line.pauseAdmission === true || control(line).paused === true; }
  assertSendAllowed(line) { if (this.paused(line)) throw fail('ADMIN_PAUSED'); }
  assertLifecycleAvailable(line) { if (line.managementOperation || line.lifecycleBusy) throw fail('MANAGEMENT_OPERATION_ACTIVE'); }
  commonBlockers(line) {
    const b = this.bridge, blockers = [];
    if (!b.config.managementEnabled) blockers.push('PHASE_B_UNAVAILABLE');
    if (line.managementStateInvalid) blockers.push('MANAGEMENT_STATE_INVALID');
    if (line.connectPromise) blockers.push('TRANSPORT_TRANSITION_IN_PROGRESS');
    if (b.stopping || !b.initialized) blockers.push('BRIDGE_NOT_READY');
    try { b.assertExclusiveOwnership(); } catch { blockers.push('DIRECTORY_OWNERSHIP_UNPROVEN'); }
    if (line.quarantined || line.fencedAuth || line.isolating || line.auth?.store?.failed) blockers.push('AUTHENTICATION_REVIEW_REQUIRED');
    if (line.removing || line.managementOperation || line.lifecycleBusy) blockers.push('MANAGEMENT_OPERATION_ACTIVE');
    const health = b.storageMonitor?.status(line.line_id);
    if (!line.store || line.store.failed || !health || !Number.isFinite(health.checked_at) || Date.now() - health.checked_at < 0 || Date.now() - health.checked_at > 60000 || !['ok','warning'].includes(health.level)) blockers.push('STORAGE_REVIEW_REQUIRED');
    return blockers;
  }
  reusableBlockers(line, {transport = true} = {}) {
    const blockers = [];
    const proof = line.auth?.managementEvidence?.();
    const phone = line.store?.data?.bound_phone;
    const ownPhone = String(line.auth?.state?.creds?.me?.id ?? '').split('@')[0].split(':')[0];
    if (!/^\d{5,20}$/.test(phone ?? '') || ownPhone !== phone || (line.expected_phone && line.expected_phone !== phone)) blockers.push('SESSION_OWNERSHIP_UNPROVEN');
    const reserved = this.bridge.phoneReservations.get(phone);
    if ((reserved && reserved !== line.line_id) || [...this.bridge.lines.values()].some(other => other !== line &&
        (other.store?.data?.bound_phone === phone || String(other.auth?.state?.creds?.me?.id ?? '').split('@')[0].split(':')[0] === phone))) blockers.push('CONFLICTING_SESSION_OWNER');
    if (!proof?.current_owner) blockers.push('SESSION_OWNERSHIP_UNPROVEN');
    if (!proof?.paired || !proof?.healthy || line.store?.data?.auth_reset_required ||
        ['logged_out', 'authentication_failure', 'session_conflict', 'protocol_failure', 'storage_failure'].includes(line.recoveryCategory)) blockers.push('AUTHENTICATION_REVIEW_REQUIRED');
    if (!proof?.settled || line.sendPending || line.connectPromise) blockers.push('WORK_NOT_SETTLED');
    if (transport && line.generation > 0 && !proof?.transport_closed) blockers.push('TRANSPORT_RELEASE_UNCONFIRMED');
    if (transport && line.generation > 0 && !proof?.events_released) blockers.push('EVENT_SUBSCRIPTION_NOT_RELEASED');
    if (!line.store?.data?.enabled) blockers.push('SESSION_DISABLED');
    return blockers;
  }
  availability(line) {
    const base = this.commonBlockers(line), paused = this.paused(line);
    const disconnected = ['disconnected', 'error'].includes(line.status);
    const connected = line.status === 'connected' && line.session?.ready && !line.session?.closed;
    const reconnect = [...base, ...this.reusableBlockers(line), ...(paused ? ['ADMIN_PAUSED'] : []),
      ...(!disconnected ? ['CONNECTION_NOT_DISCONNECTED'] : [])];
    const resume = [...base, ...this.reusableBlockers(line, {transport: !connected}),
      ...(!connected && !disconnected ? ['TRANSPORT_TRANSITION_IN_PROGRESS'] : [])];
    const pause = [...base, ...(paused ? ['ALREADY_PAUSED'] : []),
      ...(!line.auth?.managementEvidence?.().paired ? ['AUTHENTICATION_REVIEW_REQUIRED'] : [])];
    return {supported: true, epoch: this.epoch, generation: line.generation, version: control(line).version,
      paused, active_operation_id: line.managementOperation ?? null,
      actions: Object.fromEntries([['reconnect', reconnect], ['pause', pause], ['resume', resume]]
        .map(([action, blockers]) => [action, {enabled: blockers.length === 0, blockers: [...new Set(blockers)]}]))};
  }
  get(line, id) {
    const operation = control(line).operations?.[id];
    if (!operation) throw new BridgeError(404, 'OPERATION_NOT_FOUND', 'No saved administrative operation has this ID.');
    return structuredClone(operation);
  }
  async initialize(line) {
    if (line.fencedAuth || line.quarantined || !Object.hasOwn(line.store?.data ?? {}, 'administration')) return;
    const value = line.store.data.administration;
    if (!value || typeof value !== 'object' || !Number.isSafeInteger(value.version) || value.version < 0 || typeof value.paused !== 'boolean' ||
        !value.operations || typeof value.operations !== 'object' || Array.isArray(value.operations) ||
        Object.entries(value.operations).some(([id, op]) => !uuid.test(id) || !op || op.id !== id ||
          !actions.has(op.action) || !['running',...terminal].includes(op.state) ||
          !/^[a-f0-9]{64}$/.test(op.fingerprint ?? '') || !Number.isSafeInteger(op.created_at))) {
      line.pauseAdmission = true; line.managementStateInvalid = true;
      throw fail('MANAGEMENT_STATE_INVALID');
    }
    const pending = Object.values(control(line).operations ?? {}).filter(op => !terminal.has(op.state));
    if (pending.length) await line.store.update(data => {
      for (const op of pending) {
        Object.assign(data.administration.operations[op.id],
          {state: 'interrupted', reason_code: 'BRIDGE_RESTARTED', completed_at: Date.now()});
        // A resume can persist paused=false before its post-save revalidation.
        // Interrupted, it never completed: fail safe as paused (as the API does).
        if (op.action === 'resume') data.administration.paused = true;
      }
    });
    // Never replay a command after restart. A durable pause remains effective.
  }
  submit(line, request) {
    if (!request || !uuid.test(request.id ?? '') || !actions.has(request.action) ||
        !['admin', 'supervisor'].includes(request.role) || !/^\d{1,20}$/.test(request.operator ?? '') ||
        request.reason !== 'OPERATOR_REQUEST' || !Number.isSafeInteger(request.expected_version) ||
        !Number.isSafeInteger(request.expected_generation) || !uuid.test(request.expected_epoch ?? '') ||
        Object.keys(request).some(k => !['id','action','role','operator','reason','expected_version','expected_generation','expected_epoch'].includes(k))) throw fail('INVALID_MANAGEMENT_REQUEST');
    const fingerprint = crypto.createHash('sha256').update(JSON.stringify([
      request.action, request.role, request.operator, request.reason,
      request.expected_version, request.expected_generation, request.expected_epoch])).digest('hex');
    const existing = control(line).operations?.[request.id];
    if (existing) {
      if (existing.fingerprint !== fingerprint) throw fail('IDEMPOTENCY_CONFLICT');
      return Promise.resolve(structuredClone(existing));
    }
    const inflight = this.tasks.get(request.id);
    if (inflight) {
      if (inflight.fingerprint !== fingerprint || inflight.line !== line) throw fail('IDEMPOTENCY_CONFLICT');
      return inflight.admitted;
    }
    const available = this.availability(line);
    if (request.expected_epoch !== this.epoch || request.expected_generation !== line.generation || request.expected_version !== available.version) throw fail('STALE_OBSERVATION');
    if (!available.actions[request.action].enabled) throw fail(available.actions[request.action].blockers[0]);
    const recent = Object.values(control(line).operations ?? {}).filter(op => op.created_at > Date.now() - 60000);
    if (recent.length >= 6 || recent.filter(op => op.action === request.action).length >= 3) throw fail('ACTION_RATE_LIMITED');
    // Admission is synchronous, before the first fsync yields to another request.
    line.managementOperation = request.id;
    if (request.action === 'pause' || request.action === 'resume') line.pauseAdmission = true;
    clearTimeout(line.reconnectTimer); line.reconnectTimer = null; this.bridge.reconnectSlots.delete(line.line_id);
    const op = {id: request.id, action: request.action, operator: request.operator, role: request.role,
      reason: request.reason, fingerprint, state: 'running', reason_code: 'PROCESSING', created_at: Date.now(), completed_at: null};
    const admitted = line.store.update(data => {
      data.administration ??= {version: 0, paused: false, operations: {}};
      const c = data.administration;
      c.version++;
      if (request.action === 'pause') c.paused = true;
      c.operations[op.id] = op;
      // SQL holds permanent audit history; versions prevent replay of pruned IDs.
      const old = Object.values(c.operations).filter(x => terminal.has(x.state)).sort((a,b) => a.created_at-b.created_at);
      for (const item of old.slice(0, Math.max(0, old.length - 127))) delete c.operations[item.id];
    }).then(() => this.get(line, op.id));
    const task = admitted.then(() => this.run(line, op)).catch(error => {
      // A failed commit cannot be treated as an unperformed pause. Fence admission
      // until the durable operation can be assessed; never clear authentication.
      this.bridge.onError(error);
    }).finally(() => {
      this.tasks.delete(op.id);
      if (!line.store.failed) { line.managementOperation = null; line.pauseAdmission = false; }
      if (!this.bridge.stopping && !this.paused(line)) this.bridge.scheduleReconnect(line);
    });
    this.tasks.set(op.id, {line, fingerprint, admitted, task});
    return admitted;
  }
  async run(line, op) {
    let state = 'succeeded', reason = op.action === 'pause' ? 'ADMIN_PAUSED' : op.action === 'resume' ? 'RESUMED' : 'CONNECTED';
    try {
      if (this.bridge.stopping) throw fail('BRIDGE_STOPPING');
      if (op.action !== 'pause') {
        const blockers = this.reusableBlockers(line, {transport: op.action === 'reconnect' || line.status !== 'connected'});
        if (blockers.length) throw fail(blockers[0]);
        this.bridge.assertExclusiveOwnership();
        this.bridge.storageMonitor.assertCanSubmit(line.line_id);
        if (op.action === 'resume') {
          await line.store.update(data => { data.administration.paused = false; });
          const after = this.reusableBlockers(line, {transport: line.status !== 'connected'});
          if (after.length) throw fail(after[0]);
          this.bridge.assertExclusiveOwnership();
          this.bridge.storageMonitor.assertCanSubmit(line.line_id);
        } else {
          line.safeReconnectOnly = true;
          line.connectPromise = this.bridge.openSocket(line, {pairedOnly: true}).finally(() => {line.connectPromise = null;});
          await line.connectPromise;
          // Socket construction is not connection success. Keep ownership and
          // the operation running until the provider reports a terminal result,
          // bounded so a silent transport cannot hold lifecycle admission forever.
          const deadline = Date.now() + reconnectConfirmMs;
          while (!this.bridge.stopping && line.status === 'connecting' && Date.now() < deadline) await new Promise(resolve => setTimeout(resolve, 250));
          if (line.status !== 'connected' || !line.session?.ready) throw fail('CONNECTION_NOT_CONFIRMED');
        }
      }
      if (this.bridge.stopping) throw fail('BRIDGE_STOPPING');
    } catch (error) {
      state = this.bridge.stopping ? 'interrupted' : 'failed';
      reason = /^[A-Z_]{1,64}$/.test(error.code ?? '') ? error.code : 'RECOVERY_REVIEW_REQUIRED';
      if (op.action === 'resume') await line.store.update(data => { data.administration.paused = true; });
    }
    await line.store.update(data => { Object.assign(data.administration.operations[op.id],
      {state, reason_code: reason, completed_at: Date.now()}); });
  }
  async drain() { await Promise.all([...this.tasks.values()].map(item => item.task)); }
}
