from __future__ import annotations

import json
from datetime import datetime
from typing import Any

import aiomysql

from app.config import settings
from app.support_models import ACTIVE_TICKET_STATUSES, ALLOWED_TRANSITIONS


class SupportRepositoryError(Exception):
    pass


class SupportConflictError(Exception):
    pass


class SupportNotFoundError(Exception):
    pass


class SupportRepository:
    def __init__(self) -> None:
        self._pool: aiomysql.Pool | None = None

    async def connect(self) -> None:
        if self._pool is not None:
            return
        self._pool = await aiomysql.create_pool(
            host=settings.db_host,
            port=settings.db_port,
            user=settings.db_user,
            password=settings.db_password,
            db=settings.db_name,
            minsize=1,
            maxsize=10,
            autocommit=True,
            charset="utf8mb4",
        )

    async def close(self) -> None:
        if self._pool is not None:
            self._pool.close()
            await self._pool.wait_closed()
            self._pool = None

    async def _acquire(self):
        if self._pool is None:
            await self.connect()
        assert self._pool is not None
        return self._pool.acquire()

    async def get_ticket(self, ticket_id: int) -> dict[str, Any] | None:
        if self._pool is None:
            await self.connect()
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(
                    """
                    SELECT *
                    FROM support_tickets
                    WHERE id = %s
                    LIMIT 1
                    """,
                    (ticket_id,),
                )
                return await cur.fetchone()

    async def get_ticket_by_source_message(
        self,
        whatsapp_message_id: str,
    ) -> dict[str, Any] | None:
        if self._pool is None:
            await self.connect()
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(
                    """
                    SELECT *
                    FROM support_tickets
                    WHERE source_whatsapp_message_id = %s
                    LIMIT 1
                    """,
                    (whatsapp_message_id,),
                )
                return await cur.fetchone()

    async def find_active_ticket_by_phone(
        self,
        phone_number: str,
    ) -> dict[str, Any] | None:
        if self._pool is None:
            await self.connect()
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(
                    """
                    SELECT *
                    FROM support_tickets
                    WHERE active_key = %s
                      AND status IN (%s, %s, %s, %s, %s)
                    ORDER BY id DESC
                    LIMIT 1
                    """,
                    (phone_number, *ACTIVE_TICKET_STATUSES),
                )
                return await cur.fetchone()

    async def create_ticket(
        self,
        *,
        ticket_reference: str,
        phone_number: str,
        customer_id: str | None,
        customer_userid: str | None,
        customer_name: str | None,
        reason: str,
        ai_summary: str,
        tracking_reference: str | None,
        requested_action: str | None,
        current_language: str | None,
        communication_style: str | None,
        context_json: str,
        source_whatsapp_message_id: str | None,
    ) -> tuple[dict[str, Any], bool]:
        """Create once, or return an existing equivalent ticket.

        active_key has a UNIQUE index, so MariaDB is the final concurrency
        guard against two active tickets for the same WhatsApp conversation.
        source_whatsapp_message_id is also unique when present.
        """
        if self._pool is None:
            await self.connect()
        assert self._pool is not None

        if source_whatsapp_message_id:
            existing = await self.get_ticket_by_source_message(
                source_whatsapp_message_id
            )
            if existing:
                return existing, False

        existing = await self.find_active_ticket_by_phone(phone_number)
        if existing:
            return existing, False

        async with self._pool.acquire() as conn:
            await conn.begin()
            try:
                async with conn.cursor() as cur:
                    await cur.execute(
                        """
                        INSERT INTO support_tickets (
                            ticket_reference,
                            active_key,
                            whatsapp_phone,
                            customer_id,
                            customer_userid,
                            customer_name,
                            reason,
                            ai_summary,
                            tracking_reference,
                            requested_action,
                            current_language,
                            communication_style,
                            context_json,
                            source_whatsapp_message_id,
                            status,
                            created_at,
                            updated_at,
                            last_activity_at
                        ) VALUES (
                            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                            'NEW', UTC_TIMESTAMP(6), UTC_TIMESTAMP(6), UTC_TIMESTAMP(6)
                        )
                        """,
                        (
                            ticket_reference,
                            phone_number,
                            phone_number,
                            customer_id,
                            customer_userid,
                            customer_name,
                            reason,
                            ai_summary,
                            tracking_reference,
                            requested_action,
                            current_language,
                            communication_style,
                            context_json,
                            source_whatsapp_message_id,
                        ),
                    )
                    ticket_id = cur.lastrowid
                    await cur.execute(
                        """
                        INSERT INTO support_ticket_events (
                            ticket_id, event_type, actor_type, details_json, created_at
                        ) VALUES (%s, 'TICKET_CREATED', 'SYSTEM', %s, UTC_TIMESTAMP(6))
                        """,
                        (
                            ticket_id,
                            json.dumps(
                                {
                                    "reason": reason,
                                    "source_whatsapp_message_id": source_whatsapp_message_id,
                                },
                                ensure_ascii=False,
                            ),
                        ),
                    )
                await conn.commit()
            except aiomysql.IntegrityError:
                await conn.rollback()
                if source_whatsapp_message_id:
                    existing = await self.get_ticket_by_source_message(
                        source_whatsapp_message_id
                    )
                    if existing:
                        return existing, False
                existing = await self.find_active_ticket_by_phone(phone_number)
                if existing:
                    return existing, False
                raise
            except Exception:
                await conn.rollback()
                raise

        created = await self.get_ticket(int(ticket_id))
        if not created:
            raise SupportRepositoryError("Created support ticket could not be reloaded.")
        return created, True

    async def add_ticket_message(
        self,
        *,
        ticket_id: int,
        sender_type: str,
        direction: str,
        body: str,
        sender_id: str | None = None,
        sender_name: str | None = None,
        whatsapp_message_id: str | None = None,
        client_message_id: str | None = None,
        whatsapp_timestamp: str | None = None,
        message_type: str = "text",
        send_status: str | None = None,
        error_message: str | None = None,
        require_active: bool = False,
        current_language: str | None = None,
        communication_style: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        if self._pool is None:
            await self.connect()
        assert self._pool is not None

        if whatsapp_message_id:
            existing = await self.get_message_by_whatsapp_id(whatsapp_message_id)
            if existing:
                return existing, False
        if client_message_id:
            existing = await self.get_message_by_client_id(client_message_id)
            if existing:
                return existing, False

        async with self._pool.acquire() as conn:
            await conn.begin()
            try:
                async with conn.cursor(aiomysql.DictCursor) as cur:
                    if require_active:
                        await cur.execute(
                            """
                            SELECT id, status, active_key
                            FROM support_tickets
                            WHERE id = %s
                            FOR UPDATE
                            """,
                            (ticket_id,),
                        )
                        ticket_state = await cur.fetchone()
                        if not ticket_state:
                            raise SupportNotFoundError("Ticket not found.")
                        if (
                            ticket_state.get("active_key") is None
                            or ticket_state.get("status") not in ACTIVE_TICKET_STATUSES
                        ):
                            raise SupportConflictError("Ticket is no longer active.")

                    await cur.execute(
                        """
                        INSERT INTO support_ticket_messages (
                            ticket_id,
                            sender_type,
                            sender_id,
                            sender_name,
                            direction,
                            message_type,
                            body,
                            whatsapp_message_id,
                            client_message_id,
                            whatsapp_timestamp,
                            send_status,
                            error_message,
                            created_at,
                            updated_at
                        ) VALUES (
                            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                            UTC_TIMESTAMP(6), UTC_TIMESTAMP(6)
                        )
                        """,
                        (
                            ticket_id,
                            sender_type,
                            sender_id,
                            sender_name,
                            direction,
                            message_type,
                            body,
                            whatsapp_message_id,
                            client_message_id,
                            whatsapp_timestamp,
                            send_status,
                            error_message,
                        ),
                    )
                    message_id = cur.lastrowid
                    await cur.execute(
                        """
                        UPDATE support_tickets
                        SET last_activity_at = UTC_TIMESTAMP(6),
                            updated_at = UTC_TIMESTAMP(6),
                            current_language = COALESCE(%s, current_language),
                            communication_style = COALESCE(%s, communication_style)
                        WHERE id = %s
                        """,
                        (current_language, communication_style, ticket_id),
                    )
                await conn.commit()
            except aiomysql.IntegrityError:
                await conn.rollback()
                if whatsapp_message_id:
                    existing = await self.get_message_by_whatsapp_id(whatsapp_message_id)
                    if existing:
                        return existing, False
                if client_message_id:
                    existing = await self.get_message_by_client_id(client_message_id)
                    if existing:
                        return existing, False
                raise
            except Exception:
                await conn.rollback()
                raise

        message = await self.get_message(int(message_id))
        if not message:
            raise SupportRepositoryError("Created ticket message could not be reloaded.")
        return message, True

    async def get_message(self, message_id: int) -> dict[str, Any] | None:
        if self._pool is None:
            await self.connect()
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(
                    "SELECT * FROM support_ticket_messages WHERE id = %s LIMIT 1",
                    (message_id,),
                )
                return await cur.fetchone()

    async def get_message_by_whatsapp_id(self, message_id: str) -> dict[str, Any] | None:
        if self._pool is None:
            await self.connect()
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(
                    """
                    SELECT * FROM support_ticket_messages
                    WHERE whatsapp_message_id = %s LIMIT 1
                    """,
                    (message_id,),
                )
                return await cur.fetchone()

    async def get_message_by_client_id(self, client_message_id: str) -> dict[str, Any] | None:
        if self._pool is None:
            await self.connect()
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(
                    """
                    SELECT * FROM support_ticket_messages
                    WHERE client_message_id = %s LIMIT 1
                    """,
                    (client_message_id,),
                )
                return await cur.fetchone()

    async def update_message_send_state(
        self,
        message_id: int,
        *,
        send_status: str,
        whatsapp_message_id: str | None = None,
        error_message: str | None = None,
    ) -> None:
        if self._pool is None:
            await self.connect()
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    UPDATE support_ticket_messages
                    SET send_status = %s,
                        whatsapp_message_id = COALESCE(%s, whatsapp_message_id),
                        error_message = %s,
                        updated_at = UTC_TIMESTAMP(6)
                    WHERE id = %s
                    """,
                    (send_status, whatsapp_message_id, error_message, message_id),
                )

    async def list_messages(
        self,
        ticket_id: int,
        *,
        limit: int = 100,
        before_id: int | None = None,
    ) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 200))
        params: list[Any] = [ticket_id]
        extra = ""
        if before_id is not None:
            extra = " AND id < %s"
            params.append(before_id)
        params.append(limit)

        if self._pool is None:
            await self.connect()
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(
                    f"""
                    SELECT *
                    FROM support_ticket_messages
                    WHERE ticket_id = %s {extra}
                    ORDER BY id DESC
                    LIMIT %s
                    """,
                    tuple(params),
                )
                rows = await cur.fetchall()
        rows.reverse()
        return rows

    async def list_events(self, ticket_id: int, limit: int = 100) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 200))
        if self._pool is None:
            await self.connect()
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(
                    """
                    SELECT *
                    FROM support_ticket_events
                    WHERE ticket_id = %s
                    ORDER BY id ASC
                    LIMIT %s
                    """,
                    (ticket_id, limit),
                )
                return await cur.fetchall()

    async def add_event(
        self,
        ticket_id: int,
        *,
        event_type: str,
        actor_type: str,
        actor_id: str | None = None,
        actor_name: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        if self._pool is None:
            await self.connect()
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    INSERT INTO support_ticket_events (
                        ticket_id, event_type, actor_type, actor_id, actor_name,
                        details_json, created_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, UTC_TIMESTAMP(6))
                    """,
                    (
                        ticket_id,
                        event_type,
                        actor_type,
                        actor_id,
                        actor_name,
                        json.dumps(details or {}, ensure_ascii=False),
                    ),
                )


    async def mark_customer_replied(self, ticket_id: int) -> None:
        if self._pool is None:
            await self.connect()
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            await conn.begin()
            try:
                async with conn.cursor() as cur:
                    await cur.execute(
                        """
                        UPDATE support_tickets
                        SET status = 'IN_PROGRESS',
                            updated_at = UTC_TIMESTAMP(6),
                            last_activity_at = UTC_TIMESTAMP(6)
                        WHERE id = %s
                          AND status = 'WAITING_CUSTOMER'
                          AND active_key IS NOT NULL
                        """,
                        (ticket_id,),
                    )
                    changed = cur.rowcount == 1
                    if changed:
                        await cur.execute(
                            """
                            INSERT INTO support_ticket_events (
                                ticket_id, event_type, actor_type, details_json, created_at
                            ) VALUES (%s, 'CUSTOMER_REPLIED', 'CUSTOMER', '{}', UTC_TIMESTAMP(6))
                            """,
                            (ticket_id,),
                        )
                await conn.commit()
            except Exception:
                await conn.rollback()
                raise


    async def mark_agent_reply_started(
        self,
        ticket_id: int,
        *,
        agent_id: str,
        agent_name: str,
    ) -> None:
        """Move ASSIGNED -> IN_PROGRESS only if the ticket is still active."""
        if self._pool is None:
            await self.connect()
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            await conn.begin()
            try:
                async with conn.cursor() as cur:
                    await cur.execute(
                        """
                        UPDATE support_tickets
                        SET status = 'IN_PROGRESS',
                            updated_at = UTC_TIMESTAMP(6),
                            last_activity_at = UTC_TIMESTAMP(6)
                        WHERE id = %s
                          AND status = 'ASSIGNED'
                          AND active_key IS NOT NULL
                        """,
                        (ticket_id,),
                    )
                    if cur.rowcount == 1:
                        await cur.execute(
                            """
                            INSERT INTO support_ticket_events (
                                ticket_id, event_type, actor_type, actor_id, actor_name,
                                details_json, created_at
                            ) VALUES (%s, 'STATUS_CHANGED', 'AGENT', %s, %s, %s, UTC_TIMESTAMP(6))
                            """,
                            (
                                ticket_id,
                                agent_id,
                                agent_name,
                                json.dumps({"from": "ASSIGNED", "to": "IN_PROGRESS"}),
                            ),
                        )
                await conn.commit()
            except Exception:
                await conn.rollback()
                raise

    async def claim_ticket(
        self,
        ticket_id: int,
        *,
        agent_id: str,
        agent_name: str,
    ) -> tuple[dict[str, Any], bool]:
        """Atomically claim a ticket. Repeating the same agent claim is idempotent."""
        if self._pool is None:
            await self.connect()
        assert self._pool is not None
        changed = False
        async with self._pool.acquire() as conn:
            await conn.begin()
            try:
                async with conn.cursor(aiomysql.DictCursor) as cur:
                    await cur.execute(
                        """
                        SELECT id, status, current_assignee_id, active_key
                        FROM support_tickets
                        WHERE id = %s
                        FOR UPDATE
                        """,
                        (ticket_id,),
                    )
                    state = await cur.fetchone()
                    if not state:
                        raise SupportNotFoundError("Ticket not found.")

                    if (
                        state.get("status") == "NEW"
                        and state.get("current_assignee_id") is None
                        and state.get("active_key") is not None
                    ):
                        await cur.execute(
                            """
                            UPDATE support_tickets
                            SET current_assignee_id = %s,
                                current_assignee_name = %s,
                                status = 'ASSIGNED',
                                assigned_at = UTC_TIMESTAMP(6),
                                updated_at = UTC_TIMESTAMP(6),
                                last_activity_at = UTC_TIMESTAMP(6)
                            WHERE id = %s
                            """,
                            (agent_id, agent_name, ticket_id),
                        )
                        await cur.execute(
                            """
                            INSERT INTO support_ticket_events (
                                ticket_id, event_type, actor_type, actor_id, actor_name,
                                details_json, created_at
                            ) VALUES (%s, 'TICKET_CLAIMED', 'AGENT', %s, %s, '{}', UTC_TIMESTAMP(6))
                            """,
                            (ticket_id, agent_id, agent_name),
                        )
                        changed = True
                    elif (
                        state.get("active_key") is not None
                        and str(state.get("current_assignee_id") or "") == str(agent_id)
                        and state.get("status") in ACTIVE_TICKET_STATUSES
                    ):
                        # Same-agent retry after a lost HTTP response. Do not create a
                        # second claim event or change lifecycle state.
                        changed = False
                    else:
                        raise SupportConflictError(
                            "Ticket has already been claimed or is not claimable."
                        )
                await conn.commit()
            except Exception:
                await conn.rollback()
                raise

        ticket = await self.get_ticket(ticket_id)
        if not ticket:
            raise SupportNotFoundError("Ticket not found after claim.")
        return ticket, changed

    async def update_status(
        self,
        ticket_id: int,
        *,
        status: str,
        agent_id: str,
        agent_name: str,
    ) -> dict[str, Any]:
        if self._pool is None:
            await self.connect()
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            await conn.begin()
            try:
                async with conn.cursor(aiomysql.DictCursor) as cur:
                    await cur.execute(
                        "SELECT status FROM support_tickets WHERE id = %s FOR UPDATE",
                        (ticket_id,),
                    )
                    row = await cur.fetchone()
                    if not row:
                        raise SupportNotFoundError("Ticket not found.")
                    previous = row["status"]
                    if status not in ALLOWED_TRANSITIONS.get(str(previous), set()):
                        raise SupportConflictError(
                            f"Ticket is now {previous}; refresh before changing its status."
                        )
                    await cur.execute(
                        """
                        UPDATE support_tickets
                        SET status = %s,
                            updated_at = UTC_TIMESTAMP(6),
                            last_activity_at = UTC_TIMESTAMP(6),
                            closed_at = CASE WHEN %s = 'CLOSED' THEN UTC_TIMESTAMP(6) ELSE closed_at END
                        WHERE id = %s
                        """,
                        (status, status, ticket_id),
                    )
                    await cur.execute(
                        """
                        INSERT INTO support_ticket_events (
                            ticket_id, event_type, actor_type, actor_id, actor_name,
                            details_json, created_at
                        ) VALUES (%s, 'STATUS_CHANGED', 'AGENT', %s, %s, %s, UTC_TIMESTAMP(6))
                        """,
                        (
                            ticket_id,
                            agent_id,
                            agent_name,
                            json.dumps({"from": previous, "to": status}),
                        ),
                    )
                await conn.commit()
            except Exception:
                await conn.rollback()
                raise
        ticket = await self.get_ticket(ticket_id)
        if not ticket:
            raise SupportNotFoundError("Ticket not found.")
        return ticket

    async def resolve_ticket(
        self,
        ticket_id: int,
        *,
        agent_id: str,
        agent_name: str,
    ) -> tuple[dict[str, Any], bool]:
        """Resolve ticket atomically and release its active_key.

        Returns (ticket, changed). Repeated resolve calls are idempotent.
        """
        if self._pool is None:
            await self.connect()
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            await conn.begin()
            try:
                async with conn.cursor(aiomysql.DictCursor) as cur:
                    await cur.execute(
                        "SELECT status FROM support_tickets WHERE id = %s FOR UPDATE",
                        (ticket_id,),
                    )
                    row = await cur.fetchone()
                    if not row:
                        raise SupportNotFoundError("Ticket not found.")
                    if row["status"] in {"RESOLVED", "CLOSED"}:
                        await conn.rollback()
                        ticket = await self.get_ticket(ticket_id)
                        assert ticket is not None
                        return ticket, False

                    # Outbound send attempts are persisted as PENDING before the
                    # WhatsApp HTTP call. Refuse resolution while a recent send
                    # may still be in flight, preventing return-to-AI from racing
                    # an agent response. PENDING older than two minutes is treated
                    # as stale/uncertain (the HTTP client timeout is much shorter).
                    await cur.execute(
                        """
                        SELECT id
                        FROM support_ticket_messages
                        WHERE ticket_id = %s
                          AND direction = 'OUTBOUND'
                          AND send_status = 'PENDING'
                          AND created_at >= (UTC_TIMESTAMP(6) - INTERVAL 2 MINUTE)
                        ORDER BY id DESC
                        LIMIT 1
                        FOR UPDATE
                        """,
                        (ticket_id,),
                    )
                    if await cur.fetchone():
                        raise SupportConflictError(
                            "An outbound WhatsApp message is still being sent. Refresh and resolve after it finishes."
                        )

                    await cur.execute(
                        """
                        UPDATE support_tickets
                        SET status = 'RESOLVED',
                            active_key = NULL,
                            resolved_at = UTC_TIMESTAMP(6),
                            resolved_by_staff_id = %s,
                            updated_at = UTC_TIMESTAMP(6),
                            last_activity_at = UTC_TIMESTAMP(6)
                        WHERE id = %s
                        """,
                        (int(agent_id), ticket_id),
                    )
                    await cur.execute(
                        """
                        INSERT INTO support_ticket_events (
                            ticket_id, event_type, actor_type, actor_id, actor_name,
                            details_json, created_at
                        ) VALUES (%s, 'TICKET_RESOLVED', 'AGENT', %s, %s, '{}', UTC_TIMESTAMP(6))
                        """,
                        (ticket_id, agent_id, agent_name),
                    )
                    await cur.execute(
                        """
                        INSERT INTO support_ticket_events (
                            ticket_id, event_type, actor_type, actor_id, actor_name,
                            details_json, created_at
                        ) VALUES (%s, 'RETURNED_TO_AI', 'AGENT', %s, %s, '{}', UTC_TIMESTAMP(6))
                        """,
                        (ticket_id, agent_id, agent_name),
                    )
                await conn.commit()
            except Exception:
                await conn.rollback()
                raise

        ticket = await self.get_ticket(ticket_id)
        if not ticket:
            raise SupportNotFoundError("Ticket not found after resolve.")
        return ticket, True

    async def list_tickets(
        self,
        *,
        view: str,
        agent_id: str,
        page: int = 1,
        page_size: int = 30,
    ) -> dict[str, Any]:
        page = max(page, 1)
        page_size = max(1, min(page_size, 100))
        offset = (page - 1) * page_size

        where = "1=1"
        params: list[Any] = []
        if view == "unassigned":
            where = "status = 'NEW' AND current_assignee_id IS NULL AND active_key IS NOT NULL"
        elif view == "mine":
            where = "current_assignee_id = %s AND active_key IS NOT NULL"
            params.append(agent_id)
        elif view == "active":
            placeholders = ",".join(["%s"] * len(ACTIVE_TICKET_STATUSES))
            where = f"status IN ({placeholders}) AND active_key IS NOT NULL"
            params.extend(ACTIVE_TICKET_STATUSES)
        elif view == "waiting":
            where = "status IN ('WAITING_CUSTOMER', 'WAITING_INTERNAL') AND active_key IS NOT NULL"
        elif view == "history":
            where = "status IN ('RESOLVED', 'CLOSED')"
        else:
            raise ValueError("Unknown ticket view.")

        if self._pool is None:
            await self.connect()
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(
                    f"SELECT COUNT(*) AS total FROM support_tickets WHERE {where}",
                    tuple(params),
                )
                count_row = await cur.fetchone()
                total = int((count_row or {}).get("total", 0))

                list_params = [*params, page_size, offset]
                await cur.execute(
                    f"""
                    SELECT
                        id, ticket_reference, whatsapp_phone, customer_id,
                        customer_userid, customer_name,
                        LEFT(reason, 300) AS reason,
                        LEFT(ai_summary, 500) AS ai_summary,
                        tracking_reference, requested_action, current_language,
                        communication_style, status,
                        current_assignee_id, current_assignee_name,
                        created_at, updated_at, last_activity_at, assigned_at,
                        resolved_at, closed_at
                    FROM support_tickets
                    WHERE {where}
                    ORDER BY last_activity_at DESC, id DESC
                    LIMIT %s OFFSET %s
                    """,
                    tuple(list_params),
                )
                rows = await cur.fetchall()
        return {
            "items": rows,
            "page": page,
            "page_size": page_size,
            "total": total,
        }


support_repository = SupportRepository()
