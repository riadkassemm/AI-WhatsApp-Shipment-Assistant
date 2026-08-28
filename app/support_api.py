from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response, Query, status
from pydantic import BaseModel, Field

from app.config import settings
from app.support_auth import (
    SupportAgent,
    authenticate_staff,
    get_current_agent,
    require_support_csrf,
    revoke_current_session,
)
from app.staff_management import (
    StaffManagementConflictError,
    StaffManagementForbiddenError,
    StaffManagementNotFoundError,
    StaffManagementValidationError,
    create_staff_account,
    delete_staff_account,
    list_staff_accounts,
    update_staff_account,
)
from app.support_repository import SupportConflictError, SupportNotFoundError
from app.support_service import (
    SupportForbiddenError,
    SupportSendError,
    SupportValidationError,
    support_service,
)


router = APIRouter(prefix="/api/support", tags=["support"])


class LoginRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=4096)


class StatusUpdateRequest(BaseModel):
    status: str


class SendMessageRequest(BaseModel):
    body: str = Field(min_length=1, max_length=4096)
    client_message_id: str = Field(min_length=1, max_length=191)


class CreateStaffRequest(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=12, max_length=4096)
    role: Literal["agent", "admin"]


class UpdateStaffRequest(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=120)
    email: str | None = Field(default=None, min_length=3, max_length=320)
    password: str | None = Field(default=None, min_length=12, max_length=4096)
    role: Literal["agent", "admin"] | None = None
    is_active: bool | None = None


def _translate_error(exc: Exception) -> HTTPException:
    if isinstance(exc, StaffManagementForbiddenError):
        return HTTPException(status_code=403, detail=str(exc))
    if isinstance(exc, StaffManagementNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, StaffManagementConflictError):
        return HTTPException(status_code=409, detail=exc.detail())
    if isinstance(exc, StaffManagementValidationError):
        return HTTPException(status_code=422, detail=str(exc))
    if isinstance(exc, SupportNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, SupportForbiddenError):
        return HTTPException(status_code=403, detail=str(exc))
    if isinstance(exc, SupportConflictError):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, SupportValidationError):
        return HTTPException(status_code=422, detail=str(exc))
    if isinstance(exc, SupportSendError):
        return HTTPException(status_code=502, detail=str(exc))
    return HTTPException(status_code=500, detail="Support operation failed.")


def _set_session_cookies(response: Response, *, session_token: str, csrf_token: str) -> None:
    max_age = max(60, int(settings.support_session_ttl_seconds))
    response.set_cookie(
        key=settings.support_cookie_name,
        value=session_token,
        max_age=max_age,
        httponly=True,
        secure=settings.support_cookie_secure,
        samesite=settings.support_cookie_samesite,
        path="/",
    )
    # Double-submit CSRF token. It is not an authentication credential and is
    # intentionally readable by same-origin JavaScript so it can be echoed in a
    # request header. The server validates its hash against the authenticated
    # session row.
    response.set_cookie(
        key=settings.support_csrf_cookie_name,
        value=csrf_token,
        max_age=max_age,
        httponly=False,
        secure=settings.support_cookie_secure,
        samesite=settings.support_cookie_samesite,
        path="/",
    )


def _clear_session_cookies(response: Response) -> None:
    response.delete_cookie(settings.support_cookie_name, path="/")
    response.delete_cookie(settings.support_csrf_cookie_name, path="/")


@router.post("/login")
async def support_login(payload: LoginRequest, response: Response) -> dict:
    try:
        login = await authenticate_staff(payload.email, payload.password)
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Support login is temporarily unavailable.") from exc

    if login is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password.",
        )

    _set_session_cookies(
        response,
        session_token=login.session_token,
        csrf_token=login.csrf_token,
    )
    return {
        "id": login.agent.id,
        "name": login.agent.name,
        "role": login.agent.role,
        "can_manage_staff": login.agent.is_supervisor,
        "expires_at": login.expires_at.isoformat(),
    }


@router.post("/logout")
async def support_logout(
    request: Request,
    response: Response,
    _agent: SupportAgent = Depends(require_support_csrf),
) -> dict:
    try:
        await revoke_current_session(request)
    finally:
        _clear_session_cookies(response)
    return {"logged_out": True}


@router.get("/me")
async def support_me(agent: SupportAgent = Depends(get_current_agent)) -> dict:
    return {
        "id": agent.id,
        "name": agent.name,
        "role": agent.role,
        "can_manage_staff": agent.is_supervisor,
    }


@router.get("/staff")
async def list_support_staff(
    agent: SupportAgent = Depends(get_current_agent),
) -> dict:
    try:
        return {"items": await list_staff_accounts(agent)}
    except Exception as exc:
        raise _translate_error(exc) from exc


@router.post("/staff", status_code=status.HTTP_201_CREATED)
async def create_support_staff(
    payload: CreateStaffRequest,
    agent: SupportAgent = Depends(require_support_csrf),
) -> dict:
    try:
        return {
            "staff": await create_staff_account(
                agent,
                name=payload.name,
                email=payload.email,
                password=payload.password,
                role=payload.role,
            )
        }
    except Exception as exc:
        raise _translate_error(exc) from exc


@router.patch("/staff/{staff_id}")
async def update_support_staff(
    staff_id: int,
    payload: UpdateStaffRequest,
    agent: SupportAgent = Depends(require_support_csrf),
) -> dict:
    try:
        return {
            "staff": await update_staff_account(
                agent,
                staff_id,
                name=payload.name,
                email=payload.email,
                password=payload.password,
                role=payload.role,
                is_active=payload.is_active,
            )
        }
    except Exception as exc:
        raise _translate_error(exc) from exc


@router.delete("/staff/{staff_id}")
async def delete_support_staff(
    staff_id: int,
    agent: SupportAgent = Depends(require_support_csrf),
) -> dict:
    try:
        return {"staff": await delete_staff_account(agent, staff_id)}
    except Exception as exc:
        raise _translate_error(exc) from exc


@router.get("/tickets")
async def list_tickets(
    view: str = Query(default="unassigned", pattern="^(unassigned|mine|active|waiting|history)$"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=30, ge=1, le=100),
    agent: SupportAgent = Depends(get_current_agent),
) -> dict:
    try:
        return await support_service.list_tickets(
            view=view,
            agent=agent,
            page=page,
            page_size=page_size,
        )
    except Exception as exc:
        raise _translate_error(exc) from exc


@router.get("/tickets/{ticket_id}")
async def ticket_detail(
    ticket_id: int,
    agent: SupportAgent = Depends(get_current_agent),
) -> dict:
    try:
        return await support_service.ticket_detail(ticket_id, agent)
    except Exception as exc:
        raise _translate_error(exc) from exc


@router.post("/tickets/{ticket_id}/claim")
async def claim_ticket(
    ticket_id: int,
    agent: SupportAgent = Depends(require_support_csrf),
) -> dict:
    try:
        return await support_service.claim(ticket_id, agent)
    except Exception as exc:
        raise _translate_error(exc) from exc


@router.post("/tickets/{ticket_id}/status")
async def update_ticket_status(
    ticket_id: int,
    request: StatusUpdateRequest,
    agent: SupportAgent = Depends(require_support_csrf),
) -> dict:
    try:
        return {
            "ticket": await support_service.update_status(
                ticket_id,
                new_status=request.status,
                agent=agent,
            )
        }
    except Exception as exc:
        raise _translate_error(exc) from exc


@router.post("/tickets/{ticket_id}/messages")
async def send_ticket_message(
    ticket_id: int,
    request: SendMessageRequest,
    agent: SupportAgent = Depends(require_support_csrf),
) -> dict:
    try:
        return {
            "message": await support_service.send_agent_message(
                ticket_id,
                agent=agent,
                body=request.body,
                client_message_id=request.client_message_id,
            )
        }
    except Exception as exc:
        raise _translate_error(exc) from exc


@router.post("/tickets/{ticket_id}/resolve")
async def resolve_ticket(
    ticket_id: int,
    agent: SupportAgent = Depends(require_support_csrf),
) -> dict:
    try:
        return await support_service.resolve_and_return_to_ai(ticket_id, agent=agent)
    except Exception as exc:
        raise _translate_error(exc) from exc
