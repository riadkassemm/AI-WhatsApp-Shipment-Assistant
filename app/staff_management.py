"""Supervisor/admin staff-account lifecycle management for the support dashboard."""
from __future__ import annotations

import asyncio
import logging
import re
import unicodedata
from datetime import datetime, timezone
from typing import Any

import bcrypt

from app.staff_repository import (
    StaffAlreadyExistsError,
    StaffNotFoundError,
    staff_repository,
)
from app.support_auth import SupportAgent, normalize_staff_email


logger = logging.getLogger("shipment-bot")

MANAGEABLE_STAFF_ROLES = {"agent", "admin"}
_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


class StaffManagementError(Exception):
    pass


class StaffManagementForbiddenError(StaffManagementError):
    pass


class StaffManagementValidationError(StaffManagementError):
    pass


class StaffManagementNotFoundError(StaffManagementError):
    pass


class StaffManagementConflictError(StaffManagementError):
    def __init__(
        self,
        message: str,
        *,
        code: str = "staff_conflict",
        existing_staff: dict[str, Any] | None = None,
        requested_role: str | None = None,
        suggested_action: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.existing_staff = existing_staff
        self.requested_role = requested_role
        self.suggested_action = suggested_action

    def detail(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"code": self.code, "message": str(self)}
        if self.existing_staff is not None:
            payload["existing_staff"] = self.existing_staff
        if self.requested_role is not None:
            payload["requested_role"] = self.requested_role
        if self.suggested_action is not None:
            payload["suggested_action"] = self.suggested_action
        return payload


def _contains_control(value: str) -> bool:
    return any(unicodedata.category(char) == "Cc" for char in value)


def _normalize_public_row(row: dict[str, Any]) -> dict[str, Any]:
    safe: dict[str, Any] = {}
    for key in (
        "id",
        "name",
        "email",
        "role",
        "is_active",
        "last_login_at",
        "created_at",
        "updated_at",
        "email_conflict",
    ):
        value = row.get(key)
        if isinstance(value, datetime):
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            value = value.isoformat()
        safe[key] = value
    if safe.get("id") is not None:
        safe["id"] = str(safe["id"])
    safe["is_active"] = bool(safe.get("is_active"))
    safe["email_conflict"] = bool(safe.get("email_conflict"))
    return safe


def _require_manager(actor: SupportAgent) -> None:
    if not actor.is_supervisor:
        raise StaffManagementForbiddenError(
            "Only a supervisor or administrator can manage staff accounts."
        )


def _validate_name(name: str) -> str:
    rendered = str(name or "").strip()
    if len(rendered) < 2 or len(rendered) > 120 or _contains_control(rendered):
        raise StaffManagementValidationError(
            "Staff name must be between 2 and 120 characters."
        )
    return rendered


def _validate_email(email: str) -> tuple[str, str]:
    rendered = str(email or "").strip()
    normalized = normalize_staff_email(rendered)
    if (
        len(rendered) < 3
        or len(rendered) > 320
        or _contains_control(rendered)
        or not _EMAIL_RE.fullmatch(normalized)
    ):
        raise StaffManagementValidationError("A valid staff email is required.")
    return rendered, normalized


def _validate_password(password: str) -> bytes:
    rendered = str(password or "")
    encoded = rendered.encode("utf-8")
    if len(rendered) < 12:
        raise StaffManagementValidationError(
            "Staff passwords must be at least 12 characters."
        )
    if len(encoded) > 72:
        raise StaffManagementValidationError(
            "Staff passwords must be at most 72 UTF-8 bytes."
        )
    if _contains_control(rendered):
        raise StaffManagementValidationError(
            "The password contains unsupported control characters."
        )
    return encoded


def _validate_role(role: str) -> str:
    normalized = str(role or "").strip().casefold()
    if normalized not in MANAGEABLE_STAFF_ROLES:
        raise StaffManagementValidationError("Role must be either agent or admin.")
    return normalized


async def _hash_password(password: str) -> str:
    password_bytes = _validate_password(password)
    password_hash = await asyncio.to_thread(
        bcrypt.hashpw,
        password_bytes,
        bcrypt.gensalt(rounds=12),
    )
    return password_hash.decode("utf-8")


def _email_conflict(
    existing: dict[str, Any] | None,
    *,
    requested_role: str | None,
) -> StaffManagementConflictError:
    public = _normalize_public_row(existing or {}) if existing else None
    existing_role = str((existing or {}).get("role") or "")
    suggestion = (
        "change_role"
        if requested_role and existing_role != requested_role
        else "edit_existing"
    )
    message = "A staff account with that email already exists."
    if public and requested_role and existing_role != requested_role:
        message += (
            f" It currently has role {existing_role}; edit that account to change "
            f"its role to {requested_role} instead of creating a duplicate."
        )
    return StaffManagementConflictError(
        message,
        code="email_conflict",
        existing_staff=public,
        requested_role=requested_role,
        suggested_action=suggestion,
    )


def _authorize_target_change(
    actor: SupportAgent,
    target: dict[str, Any],
    *,
    requested_role: str | None,
    requested_active: bool | None,
    deleting: bool,
) -> None:
    target_id = str(target.get("id") or "")
    target_role = str(target.get("role") or "")
    is_self = target_id == str(actor.id)

    if deleting and is_self:
        raise StaffManagementConflictError(
            "You cannot delete your own active staff account.",
            code="self_delete_forbidden",
        )
    if is_self and requested_role is not None and requested_role != target_role:
        raise StaffManagementConflictError(
            "You cannot change your own role while using that account.",
            code="self_role_change_forbidden",
        )
    if is_self and requested_active is False:
        raise StaffManagementConflictError(
            "You cannot deactivate your own active staff account.",
            code="self_deactivation_forbidden",
        )

    # Bootstrap supervisor accounts remain protected from web structural changes.
    if target_role == "supervisor":
        structural = deleting or requested_role is not None or requested_active is not None
        if not is_self or structural:
            raise StaffManagementForbiddenError(
                "Supervisor accounts are protected from role, activation, and deletion changes in the web dashboard."
            )


async def list_staff_accounts(actor: SupportAgent) -> list[dict[str, Any]]:
    _require_manager(actor)
    rows = await staff_repository.list_public_staff(limit=500)

    counts: dict[str, int] = {}
    for row in rows:
        normalized = normalize_staff_email(str(row.get("email") or ""))
        if normalized:
            counts[normalized] = counts.get(normalized, 0) + 1

    result: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        normalized = normalize_staff_email(str(item.get("email") or ""))
        item["email_conflict"] = bool(normalized and counts.get(normalized, 0) > 1)
        result.append(_normalize_public_row(item))
    return result


async def create_staff_account(
    actor: SupportAgent,
    *,
    name: str,
    email: str,
    password: str,
    role: str,
) -> dict[str, Any]:
    _require_manager(actor)

    clean_name = _validate_name(name)
    clean_email, normalized_email = _validate_email(email)
    clean_role = _validate_role(role)

    # Fast conflict before bcrypt. The repository repeats it under a MariaDB lock and
    # the unique index remains the final invariant.
    existing = await staff_repository.get_staff_by_email(normalized_email)
    if existing:
        raise _email_conflict(existing, requested_role=clean_role)

    password_hash = await _hash_password(password)
    try:
        staff_id = await staff_repository.create_staff(
            name=clean_name,
            email=clean_email,
            email_normalized=normalized_email,
            password_hash=password_hash,
            role=clean_role,
        )
    except StaffAlreadyExistsError as exc:
        raise _email_conflict(exc.existing, requested_role=clean_role) from exc

    created = await staff_repository.get_public_staff_by_id(staff_id)
    if created is None:
        raise StaffManagementError(
            "The staff account was created but could not be reloaded."
        )

    logger.info(
        "Support staff account created: actor_staff_id=%s created_staff_id=%s role=%s",
        actor.id,
        staff_id,
        clean_role,
    )
    return _normalize_public_row(created)


async def update_staff_account(
    actor: SupportAgent,
    staff_id: int,
    *,
    name: str | None = None,
    email: str | None = None,
    password: str | None = None,
    role: str | None = None,
    is_active: bool | None = None,
) -> dict[str, Any]:
    _require_manager(actor)
    target = await staff_repository.get_staff_by_id(int(staff_id))
    if target is None:
        raise StaffManagementNotFoundError("Staff account not found.")

    clean_name = _validate_name(name) if name is not None else None
    clean_email: str | None = None
    normalized_email: str | None = None
    if email is not None:
        clean_email, normalized_email = _validate_email(email)
    clean_role = _validate_role(role) if role is not None else None
    password_hash = await _hash_password(password) if password is not None else None

    _authorize_target_change(
        actor,
        target,
        requested_role=clean_role,
        requested_active=is_active,
        deleting=False,
    )

    if not any(
        value is not None
        for value in (clean_name, clean_email, password_hash, clean_role, is_active)
    ):
        raise StaffManagementValidationError(
            "Provide at least one staff field to update."
        )

    revoke_sessions = any(
        value is not None
        for value in (clean_email, password_hash, clean_role, is_active)
    )
    try:
        updated = await staff_repository.update_staff(
            int(staff_id),
            name=clean_name,
            email=clean_email,
            email_normalized=normalized_email,
            password_hash=password_hash,
            role=clean_role,
            is_active=is_active,
            revoke_sessions=revoke_sessions,
        )
    except StaffAlreadyExistsError as exc:
        raise _email_conflict(
            exc.existing,
            requested_role=clean_role or str(target.get("role") or ""),
        ) from exc
    except StaffNotFoundError as exc:
        raise StaffManagementNotFoundError(str(exc)) from exc

    changed_fields = [
        key
        for key, value in (
            ("name", clean_name),
            ("email", clean_email),
            ("password", password_hash),
            ("role", clean_role),
            ("is_active", is_active),
        )
        if value is not None
    ]
    logger.info(
        "Support staff account updated: actor_staff_id=%s target_staff_id=%s fields=%s",
        actor.id,
        staff_id,
        ",".join(changed_fields),
    )
    result = _normalize_public_row(updated)
    result["sessions_revoked"] = revoke_sessions
    result["current_session_revoked"] = (
        revoke_sessions and str(staff_id) == str(actor.id)
    )
    return result


async def delete_staff_account(actor: SupportAgent, staff_id: int) -> dict[str, Any]:
    """Soft-delete by deactivating the account and revoking all sessions."""
    _require_manager(actor)
    target = await staff_repository.get_staff_by_id(int(staff_id))
    if target is None:
        raise StaffManagementNotFoundError("Staff account not found.")

    _authorize_target_change(
        actor,
        target,
        requested_role=None,
        requested_active=False,
        deleting=True,
    )

    if not bool(target.get("is_active")):
        result = _normalize_public_row(target)
        result["deleted"] = True
        result["already_inactive"] = True
        return result

    try:
        deleted = await staff_repository.soft_delete_staff(int(staff_id))
    except StaffNotFoundError as exc:
        raise StaffManagementNotFoundError(str(exc)) from exc

    logger.info(
        "Support staff account soft-deleted: actor_staff_id=%s target_staff_id=%s role=%s",
        actor.id,
        staff_id,
        target.get("role"),
    )
    result = _normalize_public_row(deleted)
    result["deleted"] = True
    result["already_inactive"] = False
    return result
