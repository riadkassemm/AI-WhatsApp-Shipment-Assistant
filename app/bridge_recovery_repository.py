"""Durable administrative coordination. No provider operation occurs in a transaction."""
from contextlib import asynccontextmanager
import hashlib
import json

import aiomysql

from app.whatsapp_registry import whatsapp_registry_repository

TERMINAL = {'succeeded', 'failed', 'interrupted', 'cancelled'}


class RecoveryConflict(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class BridgeRecoveryRepository:
    async def pool(self):
        await whatsapp_registry_repository.connect()
        return whatsapp_registry_repository._pool

    @asynccontextmanager
    async def coordinated(self, key):
        """Connection-scoped DB mutex, never a claim about WhatsApp ownership."""
        pool = await self.pool()
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                name = 'wa-admin:' + hashlib.sha256(key.encode()).hexdigest()[:48]
                await cur.execute('SELECT GET_LOCK(%s, 0)', (name,))
                acquired = (await cur.fetchone())[0] == 1
            try:
                yield conn if acquired else None
            finally:
                if acquired:
                    async with conn.cursor() as cur:
                        await cur.execute('SELECT RELEASE_LOCK(%s)', (name,))

    async def get(self, operation_id):
        return await whatsapp_registry_repository.fetch_one(
            'SELECT * FROM whatsapp_bridge_operations WHERE id=%s', (operation_id,))

    async def history(self, line_id):
        return await whatsapp_registry_repository.fetch_all(
            'SELECT * FROM whatsapp_bridge_operations WHERE line_id=%s ORDER BY created_at DESC LIMIT 30', (line_id,))

    async def audit(self, operation_id):
        return await whatsapp_registry_repository.fetch_all(
            'SELECT state,reason_code,actor_id,actor_role,created_at FROM whatsapp_bridge_operation_audit WHERE operation_id=%s ORDER BY id LIMIT 100', (operation_id,))

    async def controls(self):
        return await whatsapp_registry_repository.fetch_all(
            'SELECT line_id,paused,active_operation_id FROM whatsapp_bridge_controls')

    async def paused(self, line_id):
        row = await whatsapp_registry_repository.fetch_one(
            'SELECT paused FROM whatsapp_bridge_controls WHERE line_id=%s', (line_id,))
        return bool(row and row['paused'])

    async def create(self, line_id, agent, request):
        params = dict(request, operator=str(agent.id), role=agent.role, reason='OPERATOR_REQUEST')
        fingerprint = hashlib.sha256(json.dumps([line_id, params], sort_keys=True).encode()).hexdigest()
        async with self.coordinated('operator:' + str(agent.id)) as conn:
            if conn is None:
                raise RecoveryConflict('OPERATOR_BUSY')
            await conn.begin()
            try:
                async with conn.cursor(aiomysql.DictCursor) as cur:
                    await cur.execute('INSERT INTO whatsapp_bridge_controls (line_id,updated_at) VALUES (%s,UTC_TIMESTAMP(6)) ON DUPLICATE KEY UPDATE line_id=line_id', (line_id,))
                    await cur.execute('SELECT * FROM whatsapp_bridge_controls WHERE line_id=%s FOR UPDATE', (line_id,))
                    control = await cur.fetchone()
                    await cur.execute('SELECT * FROM whatsapp_bridge_operations WHERE id=%s', (request['id'],))
                    existing = await cur.fetchone()
                    if existing:
                        if existing['fingerprint'] != fingerprint:
                            raise RecoveryConflict('IDEMPOTENCY_CONFLICT')
                        await conn.commit()
                        return existing
                    if control['active_operation_id']:
                        raise RecoveryConflict('MANAGEMENT_OPERATION_ACTIVE')
                    await cur.execute("""SELECT COUNT(*) AS n FROM whatsapp_bridge_operations
                        WHERE operator_id=%s AND created_at>UTC_TIMESTAMP(6)-INTERVAL 1 MINUTE""", (str(agent.id),))
                    if (await cur.fetchone())['n'] >= 10:
                        raise RecoveryConflict('ACTION_RATE_LIMITED')
                    await cur.execute("""SELECT COUNT(*) AS n FROM whatsapp_bridge_operations WHERE line_id=%s AND action=%s
                        AND created_at>UTC_TIMESTAMP(6)-INTERVAL 1 MINUTE""", (line_id, request['action']))
                    if (await cur.fetchone())['n'] >= 3:
                        raise RecoveryConflict('ACTION_RATE_LIMITED')
                    await cur.execute("""INSERT INTO whatsapp_bridge_operations
                        (id,line_id,operator_id,operator_role,action,fingerprint,parameters_json,previous_paused,state,reason_code,created_at,updated_at)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'queued','QUEUED',UTC_TIMESTAMP(6),UTC_TIMESTAMP(6))""",
                        (request['id'],line_id,str(agent.id),agent.role,request['action'],fingerprint,json.dumps(params),control['paused']))
                    await cur.execute("""UPDATE whatsapp_bridge_controls SET active_operation_id=%s,
                        paused=IF(%s='pause',1,paused),updated_at=UTC_TIMESTAMP(6) WHERE line_id=%s""",
                        (request['id'],request['action'],line_id))
                    if request['action'] == 'pause':
                        # Preserve current retry windows and queued work while paused.
                        await cur.execute("""UPDATE support_ticket_messages m JOIN support_tickets t ON t.id=m.ticket_id
                            SET m.admin_hold=1 WHERE t.business_phone_number_id=%s AND m.direction='OUTBOUND'
                              AND (m.send_status='PENDING' OR (m.send_status='FAILED' AND m.delivery_retry_safe=1
                                   AND m.held_until>UTC_TIMESTAMP(6)))""", (line_id,))
                    await cur.execute("""INSERT INTO whatsapp_bridge_operation_audit (operation_id,actor_id,actor_role,state,reason_code,created_at)
                        VALUES (%s,%s,%s,'queued','OPERATOR_REQUEST',UTC_TIMESTAMP(6))""", (request['id'],str(agent.id),agent.role))
                await conn.commit()
            except BaseException:
                await conn.rollback()
                raise
        return await self.get(request['id'])

    async def update(self, conn, operation, state, reason, *, expected_state=None, actor=None, restore_pause=False):
        """``restore_pause`` only for a definitive rejection before bridge admission:
        the bridge never paused, so API admission returns to its previous state."""
        assert state in TERMINAL | {'queued', 'running', 'unconfirmed'}
        assert not restore_pause or state == 'failed'
        await conn.begin()
        try:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute('SELECT state,reason_code FROM whatsapp_bridge_operations WHERE id=%s FOR UPDATE', (operation['id'],))
                old = await cur.fetchone()
                if old['state'] in TERMINAL or (expected_state is not None and old['state'] != expected_state):
                    await conn.commit(); return False
                if old['state'] == state and old['reason_code'] == reason:
                    # Rotate bounded polling fairly without duplicate audit entries.
                    await cur.execute('UPDATE whatsapp_bridge_operations SET updated_at=UTC_TIMESTAMP(6) WHERE id=%s', (operation['id'],))
                    await conn.commit(); return True
                await cur.execute("""UPDATE whatsapp_bridge_operations SET state=%s,reason_code=%s,updated_at=UTC_TIMESTAMP(6),
                    completed_at=IF(%s,UTC_TIMESTAMP(6),NULL) WHERE id=%s""", (state,reason,state in TERMINAL,operation['id']))
                await cur.execute("""INSERT INTO whatsapp_bridge_operation_audit (operation_id,actor_id,actor_role,state,reason_code,created_at)
                    VALUES (%s,%s,%s,%s,%s,UTC_TIMESTAMP(6))""", (operation['id'],str(actor.id) if actor else operation['operator_id'],
                        actor.role if actor else operation['operator_role'],state,reason))
                if state in TERMINAL:
                    # A pause that failed/was interrupted after bridge admission
                    # remains blocked at API admission (the bridge persisted it).
                    # Resume is the only operation that can release that hold.
                    await cur.execute("""UPDATE whatsapp_bridge_controls SET active_operation_id=NULL,
                        paused=CASE WHEN %s THEN %s WHEN %s THEN 0 ELSE paused END,updated_at=UTC_TIMESTAMP(6)
                        WHERE line_id=%s AND active_operation_id=%s""",
                        (state=='cancelled' or restore_pause,operation['previous_paused'],
                         operation['action']=='resume' and state=='succeeded',operation['line_id'],operation['id']))
            await conn.commit()
            return True
        except BaseException:
            await conn.rollback(); raise

    async def pending(self):
        return await whatsapp_registry_repository.fetch_all(
            "SELECT * FROM whatsapp_bridge_operations WHERE state IN ('queued','running','unconfirmed') ORDER BY updated_at LIMIT 32")

    async def cancel(self, operation_id, actor):
        operation = await self.get(operation_id)
        if not operation:
            raise KeyError(operation_id)
        async with self.coordinated('operation:' + operation['line_id']) as conn:
            if conn is None:
                raise RecoveryConflict('CANCELLATION_UNSAFE')
            operation = await self.get(operation_id)
            if operation['state'] == 'cancelled':
                return operation
            if operation['state'] != 'queued':
                raise RecoveryConflict('CANCELLATION_UNSAFE')
            await self.update(conn, operation, 'cancelled', 'CANCELLED_BEFORE_DISPATCH', actor=actor)
        return await self.get(operation_id)


bridge_recovery_repository = BridgeRecoveryRepository()
