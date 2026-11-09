"""Durable command submission. A timeout is an observation, never retry permission;
only the owning bridge reporting no record of the ID permits resubmitting the
identical command under the same ID."""
import asyncio
import json
import logging
from uuid import UUID

from app.bridge_recovery_repository import bridge_recovery_repository, RecoveryConflict, TERMINAL
from app.config import configured_linked_device_lines
from app.whatsapp_client import linked_device_client, WhatsAppSendError

REASONS = {
    'OPERATOR_REQUEST': 'An authorized administrator requested this operation.',
    'QUEUED': 'The operation is queued for processing.',
    'PROCESSING': 'The operation is still processing. Its result will update automatically.',
    'BRIDGE_UNAVAILABLE': 'The bridge is unavailable. The saved operation will be checked again under the same ID.',
    'CONNECTED': 'WhatsApp reconnected successfully.',
    'RESUMED': 'This number is resumed. Pending work can continue safely.',
    'ADMIN_PAUSED': 'This number is paused by an administrator.',
    'AUTHENTICATION_REVIEW_REQUIRED': 'Reconnect unavailable: authentication recovery is required.',
    'DIRECTORY_OWNERSHIP_UNPROVEN': 'Exclusive session ownership could not be verified.',
    'SESSION_OWNERSHIP_UNPROVEN': 'The saved session owner could not be verified.',
    'CONFLICTING_SESSION_OWNER': 'Another identity owns or is reserving this phone. Recovery is blocked.',
    'TRANSPORT_RELEASE_UNCONFIRMED': 'The previous connection has not confirmed it is closed.',
    'EVENT_SUBSCRIPTION_NOT_RELEASED': 'The previous connection is still finishing its work.',
    'WORK_NOT_SETTLED': 'Existing operations must finish before this action is safe.',
    'STORAGE_REVIEW_REQUIRED': 'Saved session storage needs review before recovery.',
    'SESSION_DISABLED': 'The saved session is disabled. Super Admin review is required.',
    'STALE_OBSERVATION': 'The connection changed. Refresh diagnostics before requesting another action.',
    'MANAGEMENT_OPERATION_ACTIVE': 'Another operation for this number is still processing.',
    'ACTION_RATE_LIMITED': 'Too many recovery requests. Wait a minute before trying again.',
    'OPERATOR_BUSY': 'Another request from this operator is being saved. Check its status.',
    'BRIDGE_NOT_READY': 'The bridge is not ready for recovery actions.',
    'BRIDGE_STOPPING': 'The operation was interrupted by a graceful bridge shutdown.',
    'BRIDGE_RESTARTED': 'The bridge restarted. The previous operation was not replayed.',
    'BRIDGE_STATE_LOST': 'The previous command outcome cannot be verified. Review diagnostics; it was not replayed.',
    'OUTCOME_UNCONFIRMED': 'The bridge has no confirmed result for this command. Further actions are blocked; escalate with the operation ID.',
    'CONNECTION_NOT_CONFIRMED': 'WhatsApp did not confirm a connection. Review the current diagnostics.',
    'CONNECTION_NOT_DISCONNECTED': 'Reconnect is available only for a disconnected session.',
    'TRANSPORT_TRANSITION_IN_PROGRESS': 'A connection transition is already in progress.',
    'ALREADY_PAUSED': 'This number is already paused.',
    'NOT_PAUSED': 'This number is not paused.',
    'APP_ROUTING_DISABLED': 'This number is disabled in the application.',
    'PHASE_B_UNAVAILABLE': 'Recovery requires the reviewed Phase B bridge and database release.',
    'RECOVERY_STATE_UNAVAILABLE': 'Saved administrative state is unavailable. Recovery actions are blocked.',
    'MANAGEMENT_STATE_INVALID': 'Saved administrative state requires integrity review. This number remains paused.',
    'CANCELLATION_UNSAFE': 'This operation has started and cannot be cancelled safely. Continue checking its status.',
    'CANCELLED_BEFORE_DISPATCH': 'The queued operation was cancelled before reaching the bridge.',
    'IDEMPOTENCY_CONFLICT': 'This operation ID belongs to a different request.',
    'RECOVERY_REVIEW_REQUIRED': 'Recovery could not complete safely. Review diagnostics and escalate with the operation ID.',
}


def safe_code(value):
    return value if isinstance(value, str) and value in REASONS else 'RECOVERY_REVIEW_REQUIRED'


def serialize_operation(row):
    from app.bridge_management_service import iso
    code = safe_code(row['reason_code'])
    return {key: row[key] for key in ('id','line_id','operator_id','operator_role','action','state')} | {
        'reason_code': code, 'message': REASONS[code], 'idempotency_key': row['id'],
        'created_at': iso(row['created_at']), 'updated_at': iso(row['updated_at']),
        'completed_at': iso(row['completed_at']) if row.get('completed_at') else None,
        'can_cancel': row['state'] == 'queued', 'terminal': row['state'] in TERMINAL,
    }


def recovery_actions(raw, control, *, enabled_in_app=True, available=True):
    """Allowlisted bridge decisions; never infer safe reuse from connection color."""
    raw = raw if isinstance(raw, dict) else {}
    def identifier(value):
        try: return str(UUID(value)) if isinstance(value, str) else None
        except ValueError: return None
    epoch = identifier(raw.get('epoch'))
    generation, version = raw.get('generation'), raw.get('version')
    active_value = (control or {}).get('active_operation_id') or raw.get('active_operation_id')
    valid = epoch and type(raw.get('paused')) is bool and (active_value is None or identifier(active_value)) and all(type(n) is int and 0 <= n <= 2**53-1 for n in (generation,version))
    supported = bool(available and valid and raw.get('supported') is True)
    paused = True if raw.get('paused') is True or bool((control or {}).get('paused')) else False if raw.get('paused') is False and available else None
    active = identifier(active_value)
    result = []
    for action, title in [('reconnect','Safe reconnect'),('pause','Pause'),('resume','Resume')]:
        decisions = raw.get('actions') if isinstance(raw.get('actions'), dict) else {}
        decision = decisions.get(action) if isinstance(decisions.get(action), dict) else {}
        reasons = decision.get('blockers')
        blockers = [safe_code(code) for code in reasons[:20]] if isinstance(reasons,list) else ['RECOVERY_REVIEW_REQUIRED']
        if not supported: blockers.insert(0, 'PHASE_B_UNAVAILABLE')
        if not enabled_in_app: blockers.insert(0, 'APP_ROUTING_DISABLED')
        if active: blockers.insert(0, 'MANAGEMENT_OPERATION_ACTIVE')
        if action == 'reconnect' and paused: blockers.insert(0, 'ADMIN_PAUSED')
        if action == 'pause' and paused: blockers.insert(0, 'ALREADY_PAUSED')
        if action == 'resume' and paused is False: blockers.insert(0, 'NOT_PAUSED')
        allowed = supported and decision.get('enabled') is True and not blockers
        result.append({'action':action,'label':title,'enabled':allowed,'blockers':list(dict.fromkeys(blockers)),
            'reason_code':blockers[0] if blockers else 'AVAILABLE' if allowed else 'RECOVERY_REVIEW_REQUIRED',
            'reason':REASONS[blockers[0]] if blockers else 'Ready for an authorized request.' if allowed else REASONS['RECOVERY_REVIEW_REQUIRED']})
    return {'paused':paused,'epoch':epoch if supported else None,
            'generation':generation if supported else None,'version':version if supported else None,
            'active_operation_id':active,'actions':result}


class BridgeRecoveryService:
    def __init__(self, repository=None, client=None):
        self.repository = repository or bridge_recovery_repository
        self.client = client or linked_device_client

    async def create(self, line_id, agent, request):
        from app.bridge_management_service import bridge_management_service
        # Resolve an HTTP timeout by ID before any new eligibility checks.
        existing = await self.repository.get(request['id'])
        if existing:
            return serialize_operation(await self.repository.create(line_id, agent, request))
        if line_id not in configured_linked_device_lines():
            raise RecoveryConflict('APP_ROUTING_DISABLED')
        snapshot = await bridge_management_service.line(line_id)
        line = snapshot.get('line', snapshot)
        action = next((a for a in line.get('actions', []) if a['action'] == request['action']), None)
        if not action or not action['enabled']:
            raise RecoveryConflict(safe_code((action or {}).get('reason_code')))
        return serialize_operation(await self.repository.create(line_id, agent, request))

    async def get(self, operation_id):
        row = await self.repository.get(operation_id)
        if not row: raise KeyError(operation_id)
        result = serialize_operation(row)
        from app.bridge_management_service import iso
        result['audit'] = [{'state':item['state'],'reason_code':safe_code(item['reason_code']),
                           'operator_id':item['actor_id'],'operator_role':item['actor_role'],
                           'created_at':iso(item['created_at'])} for item in await self.repository.audit(operation_id)]
        return result

    async def history(self, line_id):
        return {'operations':[serialize_operation(row) for row in await self.repository.history(line_id)]}

    async def process(self, row):
        async with self.repository.coordinated('operation:' + row['line_id']) as conn:
            if conn is None: return
            row = await self.repository.get(row['id'])
            if row['state'] in TERMINAL: return
            path = f"/v1/lines/{row['line_id']}/management-operations"
            submitting = False
            try:
                try:
                    result = await self.client._json_request('GET', path + '/' + row['id'], timeout_seconds=5)
                except WhatsAppSendError as exc:
                    if exc.http_status != 404 or getattr(exc, 'bridge_code', None) not in {None, 'OPERATION_NOT_FOUND'}:
                        raise
                    # The owning bridge has no admission record for this ID: an
                    # earlier POST (if any) never reached it. Persist dispatch
                    # intent, then submit the IDENTICAL command again. The bridge
                    # deduplicates by operation ID (durable record and in-flight
                    # admission) and rejects a changed epoch/generation/version,
                    # so a resubmission can neither run twice nor run late.
                    if row['state'] == 'queued':
                        if not await self.repository.update(conn, row, 'running', 'PROCESSING', expected_state='queued'):
                            return
                        row = {**row, 'state':'running'}
                    submitting = True
                    result = await self.client._json_request('POST', path, payload=json.loads(row['parameters_json']),
                                                             mutating=True, timeout_seconds=5)
                state = result.get('state')
                if state not in {'running','succeeded','failed','interrupted'} or result.get('id') != row['id']:
                    raise RuntimeError('INVALID_OPERATION_RESPONSE')
                await self.repository.update(conn, row, state, safe_code(result.get('reason_code')))
            except WhatsAppSendError as exc:
                if submitting and exc.http_status in {400,403,404,409,422,429} and not exc.delivery_uncertain:
                    # A 4xx precedes bridge admission: nothing was executed.
                    await self.repository.update(conn, row, 'failed', safe_code(exc.bridge_code), restore_pause=True)
                else:
                    # The saved ID is queried (and, if unknown, resubmitted
                    # unchanged) again; never under a fresh ID.
                    await self.repository.update(conn, row, row['state'], 'BRIDGE_UNAVAILABLE')

    async def run(self, stopping):
        while not stopping.is_set():
            try:
                for row in await self.repository.pending():
                    if stopping.is_set(): break
                    await self.process(row)
                if not stopping.is_set():
                    from app.bridge_paused_replies import paused_replies
                    await paused_replies.process()
            except asyncio.CancelledError:
                raise
            except Exception:
                logging.getLogger('shipment-bot').warning('Administrative recovery worker unavailable; durable operations retained')
            try: await asyncio.wait_for(stopping.wait(), 2)
            except asyncio.TimeoutError: pass


bridge_recovery_service = BridgeRecoveryService()
