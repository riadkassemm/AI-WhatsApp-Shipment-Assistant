"""AI identity levels for WhatsApp customers (plan B4), without OTP.

What the AI may disclose depends on how sure the platform is about who is
writing from a WhatsApp number:

- ``L0`` unknown: public information only (rates, policies).
- ``L1`` phone-matched: the number exactly matches the mobile of exactly one
  active account, and no other account uses it. Orders, tracking and the
  shipments list of that account. Computed live on every lookup.
- ``L2`` staff-verified: a staff member confirmed the customer's identity with
  the usual process and clicked "Verify customer for AI". Wallet, payments and
  account details as well. Stored in ``customer_ai_verifications``; it expires
  after ``AI_IDENTITY_VERIFICATION_DAYS`` and is revoked automatically when the
  account's mobile changes, the account becomes inactive or disappears, or
  another account starts using the number.

The AI never asks for a password and never accepts a claimed ID or order number
as proof of ownership. Lookups that cannot be completed count as L0.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

import aiomysql

from app.config import settings
from app.customer_directory import CustomerDirectory, normalize_phone
from app.customer_users import customer_identity_id, customer_identity_matches_source, customer_users

logger = logging.getLogger("shipment-bot")

LEVELS = ("L0", "L1", "L2")
_RANK = {level: rank for rank, level in enumerate(LEVELS)}

# The lowest level that may see each kind of business data.
DATA_CLASS_LEVELS = {
    "public": "L0",
    "rates": "L0",
    "policies": "L0",
    "orders": "L1",
    "tracking": "L1",
    "shipments": "L1",
    "wallet": "L2",
    "payments": "L2",
    "account": "L2",
}

ACTIVE_ACCOUNT_STATUSES = {"1", "active", "ACTIVE"}


class AIIdentityError(Exception):
    """A verification request was refused; the message is safe to show staff."""


class AIIdentityUnavailable(Exception):
    """The customer accounts could not be read."""


def is_active_account(row: dict[str, Any]) -> bool:
    # Same rule as password login (customer_auth): a missing status is active.
    return str(row.get("status") or "1").strip() in ACTIVE_ACCOUNT_STATUSES


def _utc(value: Any) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class AIIdentity:
    level: str
    reason: str
    customer_id: str | None = None
    customer_userid: str | None = None
    customer_name: str | None = None
    verification: dict[str, Any] | None = None

    def allows(self, data_class: str) -> bool:
        """True when this level may see ``data_class``. Unknown classes need L2."""
        required = DATA_CLASS_LEVELS.get(data_class, "L2")
        return _RANK[self.level] >= _RANK[required]

    def to_dict(self) -> dict[str, Any]:
        return {
            "level": self.level,
            "reason": self.reason,
            "customer_id": self.customer_id,
            "customer_userid": self.customer_userid,
            "customer_name": self.customer_name,
            "verification": self.verification,
        }


class CustomerAIIdentityRepository:
    """Verification rows and their audit trail in the support database."""

    def __init__(self) -> None:
        self._pool: aiomysql.Pool | None = None

    async def connect(self) -> None:
        if self._pool is not None:
            return
        self._pool = await aiomysql.create_pool(
            host=settings.db_host, port=settings.db_port, user=settings.db_user,
            password=settings.db_password, db=settings.db_name, minsize=1, maxsize=3,
            autocommit=True, charset="utf8mb4",
        )

    async def active_verification(self, phone: str) -> dict[str, Any] | None:
        if self._pool is None:
            await self.connect()
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(
                    "SELECT * FROM customer_ai_verifications WHERE active_key = %s LIMIT 1", (phone,),
                )
                return await cur.fetchone()

    @staticmethod
    async def _audit(cur: Any, *, verification_id: int | None, phone: str, customer_id: str | None,
                     action: str, reason: str | None, actor_type: str, staff_id: int | None,
                     staff_name: str | None, ticket_id: int | None, details: dict[str, Any] | None) -> None:
        await cur.execute(
            """
            INSERT INTO customer_ai_verification_audit (
                verification_id, whatsapp_phone, customer_id, action, reason, actor_type,
                actor_staff_id, actor_staff_name, ticket_id, details_json, created_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, UTC_TIMESTAMP(6))
            """,
            (verification_id, phone, customer_id, action, reason, actor_type, staff_id, staff_name,
             ticket_id, json.dumps(details or {}, ensure_ascii=False)),
        )

    async def create(self, *, phone: str, customer_id: str, customer_userid: str | None, bound_mobile: str,
                     staff_id: int, staff_name: str, ticket_id: int | None, days: int,
                     details: dict[str, Any]) -> dict[str, Any]:
        """Replace any active verification for the number with a new one."""
        if self._pool is None:
            await self.connect()
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            await conn.begin()
            try:
                async with conn.cursor(aiomysql.DictCursor) as cur:
                    await cur.execute(
                        "SELECT * FROM customer_ai_verifications WHERE active_key = %s FOR UPDATE", (phone,),
                    )
                    previous = await cur.fetchone()
                    if previous:
                        await cur.execute(
                            """
                            UPDATE customer_ai_verifications
                            SET active_key = NULL, revoked_at = UTC_TIMESTAMP(6),
                                revoked_reason = 'REPLACED', revoked_by_staff_id = %s
                            WHERE id = %s
                            """,
                            (staff_id, previous["id"]),
                        )
                        await self._audit(cur, verification_id=int(previous["id"]), phone=phone,
                                          customer_id=previous["customer_id"], action="REVOKED", reason="REPLACED",
                                          actor_type="AGENT", staff_id=staff_id, staff_name=staff_name,
                                          ticket_id=ticket_id, details=None)
                    await cur.execute(
                        """
                        INSERT INTO customer_ai_verifications (
                            whatsapp_phone, active_key, customer_id, customer_userid, bound_mobile,
                            verified_by_staff_id, verified_by_staff_name, ticket_id, verified_at, expires_at
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, UTC_TIMESTAMP(6),
                                  UTC_TIMESTAMP(6) + INTERVAL %s DAY)
                        """,
                        (phone, phone, customer_id, customer_userid, bound_mobile, staff_id, staff_name,
                         ticket_id, days),
                    )
                    verification_id = int(cur.lastrowid)
                    await self._audit(cur, verification_id=verification_id, phone=phone, customer_id=customer_id,
                                      action="VERIFIED", reason=None, actor_type="AGENT", staff_id=staff_id,
                                      staff_name=staff_name, ticket_id=ticket_id,
                                      details={**details, "valid_days": days})
                    await cur.execute("SELECT * FROM customer_ai_verifications WHERE id = %s", (verification_id,))
                    row = await cur.fetchone()
                await conn.commit()
            except Exception:
                await conn.rollback()
                raise
        assert row is not None
        return row

    async def revoke(self, verification_id: int, *, reason: str, actor_type: str, staff_id: int | None = None,
                     staff_name: str | None = None, ticket_id: int | None = None) -> bool:
        """End one verification if it is still active. Returns False when it already ended."""
        if self._pool is None:
            await self.connect()
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            await conn.begin()
            try:
                async with conn.cursor(aiomysql.DictCursor) as cur:
                    await cur.execute(
                        "SELECT * FROM customer_ai_verifications WHERE id = %s AND active_key IS NOT NULL FOR UPDATE",
                        (verification_id,),
                    )
                    row = await cur.fetchone()
                    if not row:
                        await conn.rollback()
                        return False
                    await cur.execute(
                        """
                        UPDATE customer_ai_verifications
                        SET active_key = NULL, revoked_at = UTC_TIMESTAMP(6), revoked_reason = %s,
                            revoked_by_staff_id = %s
                        WHERE id = %s
                        """,
                        (reason, staff_id, verification_id),
                    )
                    await self._audit(cur, verification_id=verification_id, phone=row["whatsapp_phone"],
                                      customer_id=row["customer_id"],
                                      action="EXPIRED" if reason == "EXPIRED" else "REVOKED", reason=reason,
                                      actor_type=actor_type, staff_id=staff_id, staff_name=staff_name,
                                      ticket_id=ticket_id, details=None)
                await conn.commit()
            except Exception:
                await conn.rollback()
                raise
        return True


class CustomerAIIdentityService:
    def __init__(
        self,
        repository: CustomerAIIdentityRepository | None = None,
        fetch_users: Callable[[str, tuple[Any, ...]], Awaitable[list[dict[str, Any]]]] | None = None,
    ) -> None:
        self.repository = repository or CustomerAIIdentityRepository()
        # Read-only customer accounts (the live replica when configured).
        self._fetch_users = fetch_users

    async def _users(self, query: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
        fetch = self._fetch_users or customer_users.fetch_all
        try:
            return list(await fetch(query, params))
        except Exception as exc:
            raise AIIdentityUnavailable("Customer accounts could not be read.") from exc

    async def accounts_for_phone(self, phone: str) -> list[dict[str, Any]]:
        """Accounts whose mobile is exactly this number (no suffix matching)."""
        normalized = normalize_phone(phone)
        if len(normalized) < 8:
            return []
        expr = CustomerDirectory._mobile_sql()
        return await self._users(
            f"""
            SELECT id, userid, name, mobile, status FROM users
            WHERE mobile IS NOT NULL AND mobile <> '' AND ({expr} = %s OR {expr} = %s)
            LIMIT 5
            """,
            (normalized, f"00{normalized}"),
        )

    async def account_by_userid(self, userid: str) -> dict[str, Any] | None:
        rows = await self._users(
            "SELECT id, userid, name, mobile, status FROM users WHERE userid = %s LIMIT 2", (userid,),
        )
        return rows[0] if len(rows) == 1 else None

    async def account_by_customer_id(self, customer_id: str) -> dict[str, Any] | None:
        if not customer_identity_matches_source(customer_id):
            return None
        database_id = customer_id[8:] if customer_id.startswith("replica:") else customer_id
        rows = await self._users(
            "SELECT id, userid, name, mobile, status FROM users WHERE id = %s LIMIT 1", (database_id,),
        )
        return rows[0] if rows else None

    @staticmethod
    def _verification_payload(row: dict[str, Any]) -> dict[str, Any]:
        verified_at, expires_at = _utc(row.get("verified_at")), _utc(row.get("expires_at"))
        return {
            "id": int(row["id"]),
            "verified_at": verified_at.isoformat() if verified_at else None,
            "expires_at": expires_at.isoformat() if expires_at else None,
            "verified_by_staff_name": row.get("verified_by_staff_name"),
        }

    async def _staff_verified(self, phone: str) -> AIIdentity | None:
        row = await self.repository.active_verification(phone)
        if not row:
            return None
        reason = None
        expires_at = _utc(row.get("expires_at"))
        account = None
        if expires_at is None or expires_at <= datetime.now(timezone.utc):
            reason = "EXPIRED"
        else:
            account = await self.account_by_customer_id(str(row["customer_id"]))
            if account is None:
                reason = "ACCOUNT_MISSING"
            elif not is_active_account(account):
                reason = "ACCOUNT_INACTIVE"
            elif normalize_phone(str(account.get("mobile") or "")) != str(row["bound_mobile"]):
                reason = "MOBILE_CHANGED"
            elif any(customer_identity_id(other["id"]) != str(row["customer_id"])
                     for other in await self.accounts_for_phone(phone)):
                reason = "NUMBER_SHARED"
        if reason is not None:
            if await self.repository.revoke(int(row["id"]), reason=reason, actor_type="SYSTEM"):
                logger.info("AI identity verification ended automatically: verification_id=%s reason=%s",
                            row["id"], reason)
            return None
        assert account is not None
        return AIIdentity(
            level="L2", reason="STAFF_VERIFIED", customer_id=str(row["customer_id"]),
            customer_userid=account.get("userid"), customer_name=account.get("name"),
            verification=self._verification_payload(row),
        )

    async def resolve(self, phone: str) -> AIIdentity:
        """The AI identity level of a WhatsApp number. Raises AIIdentityUnavailable."""
        normalized = normalize_phone(phone)
        if not normalized:
            return AIIdentity(level="L0", reason="NO_MATCH")
        verified = await self._staff_verified(normalized)
        if verified is not None:
            return verified
        accounts = await self.accounts_for_phone(normalized)
        if not accounts:
            return AIIdentity(level="L0", reason="NO_MATCH")
        if len(accounts) > 1:
            return AIIdentity(level="L0", reason="SHARED_NUMBER")
        account = accounts[0]
        if not is_active_account(account):
            return AIIdentity(level="L0", reason="INACTIVE_ACCOUNT")
        return AIIdentity(
            level="L1", reason="PHONE_MATCH", customer_id=customer_identity_id(account["id"]),
            customer_userid=account.get("userid"), customer_name=account.get("name"),
        )

    async def resolve_or_unknown(self, phone: str) -> AIIdentity:
        """Like resolve(), but an unreadable account store counts as L0 (fail closed)."""
        try:
            return await self.resolve(phone)
        except Exception:
            logger.warning("AI identity lookup unavailable; treating the customer as unknown")
            return AIIdentity(level="L0", reason="LOOKUP_UNAVAILABLE")

    async def verify(self, *, phone: str, userid: str, staff_id: int, staff_name: str,
                     ticket_id: int | None) -> AIIdentity:
        """Record that staff verified the customer writing from ``phone`` as account ``userid``."""
        normalized = normalize_phone(phone)
        if len(normalized) < 8:
            raise AIIdentityError("This conversation has no WhatsApp number that can be verified.")
        userid = str(userid or "").strip()
        if not userid or len(userid) > 100:
            raise AIIdentityError("Enter the customer's User ID.")
        account = await self.account_by_userid(userid)
        if account is None:
            raise AIIdentityError("No customer account has that User ID.")
        if not is_active_account(account):
            raise AIIdentityError("That customer account is not active.")
        customer_id = customer_identity_id(account["id"])
        others = [row for row in await self.accounts_for_phone(normalized)
                  if customer_identity_id(row["id"]) != customer_id]
        if others:
            raise AIIdentityError("This WhatsApp number is registered on another customer account.")
        bound_mobile = normalize_phone(str(account.get("mobile") or ""))
        row = await self.repository.create(
            phone=normalized, customer_id=customer_id, customer_userid=account.get("userid"),
            bound_mobile=bound_mobile, staff_id=staff_id, staff_name=staff_name, ticket_id=ticket_id,
            days=max(1, int(settings.ai_identity_verification_days)),
            details={"mobile_matches_whatsapp": bound_mobile == normalized},
        )
        return AIIdentity(
            level="L2", reason="STAFF_VERIFIED", customer_id=customer_id, customer_userid=account.get("userid"),
            customer_name=account.get("name"), verification=self._verification_payload(row),
        )

    async def revoke(self, *, phone: str, staff_id: int, staff_name: str, ticket_id: int | None) -> bool:
        row = await self.repository.active_verification(normalize_phone(phone))
        if not row:
            return False
        return await self.repository.revoke(int(row["id"]), reason="REVOKED_BY_STAFF", actor_type="AGENT",
                                            staff_id=staff_id, staff_name=staff_name, ticket_id=ticket_id)


customer_ai_identity_service = CustomerAIIdentityService()
