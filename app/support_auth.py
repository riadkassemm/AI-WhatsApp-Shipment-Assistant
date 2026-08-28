from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import bcrypt
from fastapi import Depends, HTTPException, Request, status

from app.config import settings
from app.staff_repository import staff_repository


logger = logging.getLogger("shipment-bot")

_ALLOWED_ROLES = {"agent", "supervisor", "admin"}
# Fixed bcrypt hash used only to make unknown-email login attempts pay roughly the
# same password-verification cost as known accounts. It is not a credential.
_DUMMY_BCRYPT_HASH = b"$2b$12$C6UzMDM.H6dfI/f/IKcEe.9N5qg3wY.xBmQmH0GmTKp6WZL6f8BqK"


@dataclass(frozen=True)
class SupportAgent:
    id: str
    name: str
    role: str
    session_id: int | None = None
    csrf_hash: str | None = None

    @property
    def is_supervisor(self) -> bool:
        return self.role in {"supervisor", "admin"}


@dataclass(frozen=True)
class StaffLoginSession:
    agent: SupportAgent
    session_token: str
    csrf_token: str
    expires_at: datetime


def normalize_staff_email(email: str) -> str:
    return str(email or "").strip().casefold()


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


async def _check_password(password: str, password_hash: str) -> bool:
    try:
        return await asyncio.to_thread(
            bcrypt.checkpw,
            password.encode("utf-8"),
            password_hash.encode("utf-8"),
        )
    except (ValueError, TypeError):
        return False


async def authenticate_staff(email: str, password: str) -> StaffLoginSession | None:
    normalized = normalize_staff_email(email)
    if not normalized or not password or len(password) > 4096:
        # Still do a bcrypt operation for obviously invalid/unknown inputs.
        await asyncio.to_thread(bcrypt.checkpw, b"invalid", _DUMMY_BCRYPT_HASH)
        logger.info("Staff login failed")
        return None

    staff = await staff_repository.get_staff_by_email(normalized)
    if not staff:
        await asyncio.to_thread(bcrypt.checkpw, password.encode("utf-8"), _DUMMY_BCRYPT_HASH)
        logger.info("Staff login failed")
        return None

    staff_id = int(staff["id"])
    password_ok = await _check_password(password, str(staff.get("password_hash") or ""))

    locked_until = staff.get("locked_until")
    if isinstance(locked_until, datetime):
        if locked_until.tzinfo is None:
            locked_until = locked_until.replace(tzinfo=timezone.utc)
        is_locked = locked_until > datetime.now(timezone.utc)
    else:
        is_locked = False

    if not password_ok or not bool(staff.get("is_active")) or is_locked:
        if not password_ok and bool(staff.get("is_active")) and not is_locked:
            await staff_repository.record_failed_login(staff_id)
        logger.info("Staff login failed: staff_id=%s", staff_id)
        return None

    role = str(staff.get("role") or "")
    if role not in _ALLOWED_ROLES:
        logger.warning("Staff login rejected due to invalid role: staff_id=%s", staff_id)
        return None

    session_token = secrets.token_urlsafe(48)
    csrf_token = secrets.token_urlsafe(32)
    expires_at = datetime.now(timezone.utc) + timedelta(
        seconds=max(60, int(settings.support_session_ttl_seconds))
    )
    await staff_repository.create_session_and_mark_login(
        staff_id=staff_id,
        token_hash=_sha256(session_token),
        csrf_hash=_sha256(csrf_token),
        expires_at=expires_at,
    )

    agent = SupportAgent(
        id=str(staff_id),
        name=str(staff.get("name") or "Support Agent"),
        role=role,
    )
    logger.info("Staff login succeeded: staff_id=%s", staff_id)
    return StaffLoginSession(
        agent=agent,
        session_token=session_token,
        csrf_token=csrf_token,
        expires_at=expires_at,
    )


async def get_current_agent(request: Request) -> SupportAgent:
    raw_token = request.cookies.get(settings.support_cookie_name, "")
    if not raw_token or len(raw_token) > 1024:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required.",
        )

    session = await staff_repository.get_active_session(_sha256(raw_token))
    if not session:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required.",
        )

    role = str(session.get("role") or "")
    if role not in _ALLOWED_ROLES:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Staff account is not authorized for support access.",
        )

    session_id = int(session["session_id"])
    try:
        await staff_repository.touch_session(session_id)
    except Exception:
        # Session validity was already established. A last-seen telemetry failure
        # should not turn a valid request into an outage.
        logger.exception("Could not update staff session last_seen_at: session_id=%s", session_id)

    return SupportAgent(
        id=str(session["staff_id"]),
        name=str(session.get("name") or "Support Agent"),
        role=role,
        session_id=session_id,
        csrf_hash=str(session.get("csrf_hash") or ""),
    )


async def require_support_csrf(
    request: Request,
    agent: SupportAgent = Depends(get_current_agent),
) -> SupportAgent:
    supplied = request.headers.get("X-CSRF-Token", "")
    if not supplied or not agent.csrf_hash:
        raise HTTPException(status_code=403, detail="Invalid CSRF token.")
    supplied_hash = _sha256(supplied)
    if not hmac.compare_digest(supplied_hash, agent.csrf_hash):
        raise HTTPException(status_code=403, detail="Invalid CSRF token.")
    return agent


async def revoke_current_session(request: Request) -> None:
    raw_token = request.cookies.get(settings.support_cookie_name, "")
    if raw_token:
        await staff_repository.revoke_session(_sha256(raw_token))
