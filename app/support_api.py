from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, Depends, File, Form, HTTPException, Path, Request, Response, Query, UploadFile, status
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from app.ai_availability import (
    AIControlConflictError,
    AIControlError,
    AIControlForbiddenError,
    AIControlValidationError,
    ai_availability_service,
)
from app.ai_usage import (
    AIUsageBlocked,
    AIUsageConflictError,
    AIUsageError,
    AIUsageForbiddenError,
    AIUsageValidationError,
    ai_usage_service,
)
from app.ai_provider import (
    AIProviderError,
    AIProviderForbiddenError,
    AIProviderRateLimitedError,
    AIProviderUnavailableError,
    AIProviderValidationError,
    AISecretsNotConfiguredError,
    ai_provider_service,
)
from app.config import settings
from app.google_contacts import (
    GoogleContactsConfigurationError,
    GoogleContactsError,
    GoogleContactsNotConnectedError,
    GoogleContactsProviderError,
    GoogleContactsSchemaError,
    google_contacts_service,
)
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
from app.support_events import event_stream
from app.support_statistics import router as statistics_router
from app.support_media import SupportMediaError, SupportMediaTooLargeError, is_spreadsheet_mime, max_upload_bytes
from app.support_repository import SupportConflictError, SupportNotFoundError, SupportSchemaError
from app.vcf_contacts import (
    MAX_VCF_IMPORT_BYTES,
    VCFContactsError,
    VCFContactsSchemaError,
    VCFContactsTooLargeError,
    VCFContactsValidationError,
    vcf_contacts_service,
)
from app.whatsapp_registry import (
    WhatsAppRegistryError,
    WhatsAppRegistryConflictError,
    WhatsAppRegistryForbiddenError,
    WhatsAppRegistryProviderError,
    WhatsAppRegistrySchemaError,
    WhatsAppRegistryValidationError,
    whatsapp_registry_service,
)
from app.whatsapp_outreach import OutreachLimitError, OutreachUnavailableError
from app.support_service import (
    SupportForbiddenError,
    SupportSendError,
    SupportValidationError,
    support_service,
)


logger = logging.getLogger("shipment-bot")

router = APIRouter(prefix="/api/support", tags=["support"])
# Statistics keeps its own focused API/repository while sharing normal auth.
router.include_router(statistics_router, prefix="")


class LoginRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=4096)


class StatusUpdateRequest(BaseModel):
    status: str


class AIIdentityVerifyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    userid: str = Field(min_length=1, max_length=100)
    confirmed: bool


class ReopenTicketRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # Required, nullable for legacy records. Guards against replaying an old
    # reopen request after a newer resolve/reopen cycle.
    resolved_at: datetime | None


class SendMessageRequest(BaseModel):
    body: str = Field(min_length=1, max_length=4096)
    client_message_id: str = Field(min_length=1, max_length=191)
    reply_to_message_id: int | None = Field(default=None, ge=1)


class EditMessageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    content: str = Field(min_length=1, max_length=4096)
    version: int = Field(ge=1)


class UncertainCopyRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    accept_duplicate_risk: bool = Field(strict=True)


class DeleteMessageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scope: Literal["me", "everyone"]


class StartChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    phone_number: str = Field(min_length=8, max_length=32)
    business_phone_number_id: str = Field(min_length=5, max_length=64)
    body: str = Field(min_length=1, max_length=4096)
    customer_name: str | None = Field(default=None, max_length=191)


class ForwardMessageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    destination_ticket_id: int = Field(ge=1)
    client_message_id: str | None = Field(default=None, min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")


class ForwardContactRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_message_id: str | None = Field(default=None, min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")
    phone_number: str = Field(min_length=8, max_length=32)
    customer_name: str | None = Field(default=None, max_length=191)


class SendLocationRequest(BaseModel):
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    name: str | None = Field(default=None, max_length=255)
    address: str | None = Field(default=None, max_length=1000)
    client_message_id: str = Field(min_length=1, max_length=191)
    reply_to_message_id: int | None = Field(default=None, ge=1)


class SendReactionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    emoji: str = Field(default="", max_length=64)
    client_reaction_id: str = Field(min_length=1, max_length=191)
    context_ticket_id: int | None = Field(default=None, ge=1)


class SaveContactRequest(BaseModel):
    name: str = Field(min_length=1, max_length=191)


class TransferTicketRequest(BaseModel):
    target_staff_id: int = Field(ge=1)


class CreateStaffRequest(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=12, max_length=4096)
    role: Literal["agent", "admin", "supervisor"]


class UpdateStaffRequest(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=120)
    email: str | None = Field(default=None, min_length=3, max_length=320)
    password: str | None = Field(default=None, min_length=12, max_length=4096)
    role: Literal["agent", "admin", "supervisor"] | None = None
    is_active: bool | None = None


class StaffMessagePreferencesUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    greeting_message: str = Field(default="", max_length=2000)
    resolution_message: str = Field(default="", max_length=2000)


class ContactEditRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contact_key: str = Field(min_length=3, max_length=64)
    display_name: str = Field(default="", max_length=1000)
    phone: str = Field(default="", max_length=64)


class QuickReplyLocation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    latitude: float | None = Field(default=None, ge=-90, le=90, allow_inf_nan=False)
    longitude: float | None = Field(default=None, ge=-180, le=180, allow_inf_nan=False)
    name: str = Field(default="", max_length=255)
    address: str = Field(default="", max_length=1000)
    maps_url: str = Field(default="", max_length=2048)


class QuickReplyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Generous transport limits; support_service applies the exact rules with clear messages.
    title: str = Field(default="", max_length=1000)
    body: str = Field(default="", max_length=20000)
    location: QuickReplyLocation | None = None


class QuickReplyAssignment(QuickReplyRequest):
    recipient_ids: list[int] = Field(default_factory=list, max_length=1000)
    all_staff: bool = False


class ShareQuickReplyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    recipient_ids: list[int] = Field(default_factory=list, max_length=1000)
    all_staff: bool = False


class AIScheduleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start_time: str = Field(pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    end_time: str = Field(pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    timezone: str = Field(min_length=1, max_length=128)


class AISettingsUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    control_mode: Literal["MANUAL", "SCHEDULED"]
    manual_ai_enabled: bool
    schedule: AIScheduleRequest
    version: int = Field(ge=1)


class WhatsAppAccountSyncRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    waba_id: str = Field(min_length=5, max_length=64, pattern=r"^[0-9]+$")


class WhatsAppNumberStateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool


class AIBudgetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    monthly_limit_usd: Decimal = Field(..., gt=0, le=100000)
    daily_limit_usd: Decimal | None = Field(None, gt=0, le=100000)
    warning_thresholds: list[int] = Field(..., min_length=1, max_length=6)
    enforcement_enabled: bool
    override_until: str | None = Field(None, max_length=40)
    version: int = Field(..., ge=1)


class AIPriceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model_prefix: str = Field(..., min_length=2, max_length=100)
    input_per_million: Decimal = Field(..., ge=0, le=1000)
    cached_input_per_million: Decimal = Field(..., ge=0, le=1000)
    output_per_million: Decimal = Field(..., ge=0, le=1000)
    audio_per_minute: Decimal | None = Field(None, ge=0, le=1000)
    batch_factor: Decimal = Field(Decimal("0.5"), gt=0, le=1)
    source: str | None = Field(None, max_length=255)


class OpenAIKeyRequest(BaseModel):
    # Never echo the submitted key back in a validation error.
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    api_key: SecretStr = Field(..., min_length=20, max_length=512)
    test_first: bool = True


class WhatsAppNumberAIRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = Field(..., strict=True)


class WhatsAppAllNumbersAIRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = Field(..., strict=True)
    scope: Literal["all"]


class WhatsAppNumberNameRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str | None = Field(..., max_length=80)


class WhatsAppNumberOutreachLimitsRequest(BaseModel):
    """Chat limits for one number; null restores the server default."""

    model_config = ConfigDict(extra="forbid")

    daily_limit: int | None = Field(..., ge=0, le=500, strict=True)
    new_number_daily_limit: int | None = Field(..., ge=0, le=500, strict=True)
    same_text_limit: int | None = Field(..., ge=0, le=500, strict=True)


def _translate_error(exc: Exception) -> HTTPException:
    if isinstance(exc, AIUsageForbiddenError):
        return HTTPException(status_code=403, detail=str(exc))
    if isinstance(exc, AIUsageValidationError):
        return HTTPException(status_code=422, detail=str(exc))
    if isinstance(exc, AIUsageConflictError):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, AIUsageBlocked):
        return HTTPException(status_code=503, detail=str(exc))
    if isinstance(exc, AIUsageError):
        return HTTPException(status_code=500, detail="AI usage data is temporarily unavailable.")
    if isinstance(exc, AIProviderForbiddenError):
        return HTTPException(status_code=403, detail=str(exc))
    if isinstance(exc, AIProviderValidationError):
        return HTTPException(status_code=422, detail=str(exc))
    if isinstance(exc, AIProviderRateLimitedError):
        return HTTPException(status_code=429, detail=str(exc))
    if isinstance(exc, (AISecretsNotConfiguredError, AIProviderUnavailableError)):
        return HTTPException(status_code=503, detail=str(exc))
    if isinstance(exc, AIProviderError):
        return HTTPException(status_code=500, detail="The OpenAI configuration is temporarily unavailable.")
    if isinstance(exc, OutreachLimitError):
        return HTTPException(status_code=429, detail=str(exc))
    if isinstance(exc, OutreachUnavailableError):
        return HTTPException(status_code=503, detail=str(exc))
    if isinstance(exc, AIControlForbiddenError):
        return HTTPException(status_code=403, detail=str(exc))
    if isinstance(exc, AIControlConflictError):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, AIControlValidationError):
        return HTTPException(status_code=422, detail=str(exc))
    if isinstance(exc, AIControlError):
        return HTTPException(status_code=500, detail="AI control settings are temporarily unavailable.")
    if isinstance(exc, WhatsAppRegistryForbiddenError):
        return HTTPException(status_code=403, detail=str(exc))
    if isinstance(exc, WhatsAppRegistryConflictError):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, WhatsAppRegistryValidationError):
        return HTTPException(status_code=422, detail=str(exc))
    if isinstance(exc, WhatsAppRegistrySchemaError):
        return HTTPException(status_code=503, detail=str(exc))
    if isinstance(exc, WhatsAppRegistryProviderError):
        return HTTPException(status_code=502, detail=str(exc))
    if isinstance(exc, WhatsAppRegistryError):
        return HTTPException(status_code=500, detail="WhatsApp number management is temporarily unavailable.")
    if isinstance(exc, GoogleContactsSchemaError):
        return HTTPException(status_code=503, detail=str(exc))
    if isinstance(exc, GoogleContactsConfigurationError):
        return HTTPException(status_code=503, detail=str(exc))
    if isinstance(exc, GoogleContactsNotConnectedError):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, GoogleContactsProviderError):
        return HTTPException(status_code=502, detail=str(exc))
    if isinstance(exc, GoogleContactsError):
        return HTTPException(status_code=500, detail="Google Contacts is temporarily unavailable.")
    if isinstance(exc, VCFContactsTooLargeError):
        return HTTPException(status_code=413, detail=str(exc))
    if isinstance(exc, VCFContactsValidationError):
        return HTTPException(status_code=422, detail=str(exc))
    if isinstance(exc, VCFContactsSchemaError):
        return HTTPException(status_code=503, detail=str(exc))
    if isinstance(exc, VCFContactsError):
        return HTTPException(status_code=500, detail="VCF contact import is temporarily unavailable.")
    if isinstance(exc, StaffManagementForbiddenError):
        return HTTPException(status_code=403, detail=str(exc))
    if isinstance(exc, StaffManagementNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, StaffManagementConflictError):
        return HTTPException(status_code=409, detail=exc.detail())
    if isinstance(exc, StaffManagementValidationError):
        return HTTPException(status_code=422, detail=str(exc))
    if isinstance(exc, SupportSchemaError):
        return HTTPException(status_code=503, detail=str(exc))
    if isinstance(exc, SupportNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, SupportForbiddenError):
        return HTTPException(status_code=403, detail=str(exc))
    if isinstance(exc, SupportConflictError):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, SupportValidationError):
        return HTTPException(status_code=422, detail=str(exc))
    if isinstance(exc, SupportMediaTooLargeError):
        return HTTPException(status_code=413, detail=str(exc))
    if isinstance(exc, SupportMediaError):
        return HTTPException(status_code=503 if exc.retryable else 404, detail=str(exc))
    if isinstance(exc, SupportSendError):
        return HTTPException(status_code=502, detail=str(exc))
    # Unexpected support-console failures must never be opaque in server logs.
    # Log only the exception type here; logger.exception adds the traceback while
    # avoiding message bodies/tokens in the structured summary line.
    logger.exception(
        "Unhandled support operation error: exception_type=%s",
        type(exc).__name__,
    )
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
        "can_manage_ai": login.agent.is_supervisor,
        "can_manage_whatsapp": login.agent.is_supervisor,
        "can_link_whatsapp": login.agent.is_staff,
        "linked_device_available": len(settings.whatsapp_linked_device_secret) >= 32,
        "bridge_restart_available": login.agent.is_supervisor and bool(settings.whatsapp_bridge_restart_enabled),
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
        "can_manage_ai": agent.is_supervisor,
        "can_manage_whatsapp": agent.is_supervisor,
        "can_link_whatsapp": agent.is_staff,
        "linked_device_available": len(settings.whatsapp_linked_device_secret) >= 32,
        "bridge_restart_available": agent.is_supervisor and bool(settings.whatsapp_bridge_restart_enabled),
    }


@router.get("/message-preferences")
async def get_message_preferences(
    agent: SupportAgent = Depends(get_current_agent),
) -> dict:
    try:
        return await support_service.get_message_preferences(agent)
    except Exception as exc:
        raise _translate_error(exc) from exc


@router.put("/message-preferences")
async def update_message_preferences(
    payload: StaffMessagePreferencesUpdateRequest,
    agent: SupportAgent = Depends(require_support_csrf),
) -> dict:
    try:
        return await support_service.update_message_preferences(
            agent,
            greeting_message=payload.greeting_message,
            resolution_message=payload.resolution_message,
        )
    except Exception as exc:
        raise _translate_error(exc) from exc


@router.get("/quick-replies")
async def list_quick_replies(
    agent: SupportAgent = Depends(get_current_agent),
) -> dict:
    try:
        return await support_service.list_quick_replies(agent)
    except Exception as exc:
        raise _translate_error(exc) from exc


@router.post("/quick-replies", status_code=status.HTTP_201_CREATED)
async def create_quick_reply(
    payload: QuickReplyRequest,
    agent: SupportAgent = Depends(require_support_csrf),
) -> dict:
    try:
        return await support_service.create_quick_reply(agent, **payload.model_dump())
    except Exception as exc:
        raise _translate_error(exc) from exc


@router.put("/quick-replies/{reply_id}")
async def update_quick_reply(
    payload: QuickReplyRequest,
    reply_id: int = Path(gt=0),
    agent: SupportAgent = Depends(require_support_csrf),
) -> dict:
    try:
        return await support_service.update_quick_reply(agent, reply_id, **payload.model_dump())
    except Exception as exc:
        raise _translate_error(exc) from exc


@router.delete("/quick-replies/{reply_id}")
async def delete_quick_reply(
    reply_id: int = Path(gt=0),
    agent: SupportAgent = Depends(require_support_csrf),
) -> dict:
    try:
        return await support_service.delete_quick_reply(agent, reply_id)
    except Exception as exc:
        raise _translate_error(exc) from exc


async def _save_quick_reply_pdf_upload(
    file: UploadFile, title: str, body: str, agent: SupportAgent, reply_id: int | None = None,
) -> dict:
    try:
        content = await file.read(max_upload_bytes("document") + 1)
        if len(content) > max_upload_bytes("document"):
            raise SupportValidationError("The PDF document is too large.")
        return await support_service.save_quick_reply_pdf(agent, title=title, body=body,
            content=content, filename=file.filename, reply_id=reply_id)
    except Exception as exc:
        raise _translate_error(exc) from exc
    finally:
        await file.close()


@router.post("/quick-replies/pdf", status_code=status.HTTP_201_CREATED)
async def create_quick_reply_pdf(
    file: UploadFile = File(...), title: str = Form(default="", max_length=80),
    body: str = Form(default="", max_length=1024),
    agent: SupportAgent = Depends(require_support_csrf),
) -> dict:
    return await _save_quick_reply_pdf_upload(file, title, body, agent)


@router.get("/quick-replies/recipients")
async def quick_reply_recipients(agent: SupportAgent = Depends(get_current_agent)) -> dict:
    try:
        return await support_service.quick_reply_recipients(agent)
    except Exception as exc:
        raise _translate_error(exc) from exc


@router.post("/quick-replies/assign", status_code=status.HTTP_201_CREATED)
async def assign_quick_reply(payload: QuickReplyAssignment, agent: SupportAgent = Depends(require_support_csrf)) -> dict:
    try:
        return await support_service.assign_quick_reply(agent, **payload.model_dump())
    except Exception as exc:
        raise _translate_error(exc) from exc


@router.post("/quick-replies/assign/pdf", status_code=status.HTTP_201_CREATED)
async def assign_quick_reply_pdf(
    file: UploadFile = File(...), assignment: str = Form(max_length=40000),
    agent: SupportAgent = Depends(require_support_csrf),
) -> dict:
    try:
        if not agent.is_supervisor:
            raise SupportForbiddenError("Only Admins and Super Admins can assign quick replies.")
        from pydantic import ValidationError
        try:
            payload = QuickReplyAssignment.model_validate_json(assignment)
        except ValidationError:
            raise SupportValidationError("Invalid quick reply or staff selection.") from None
        content = await file.read(max_upload_bytes("document") + 1)
        if len(content) > max_upload_bytes("document"):
            raise SupportValidationError("The PDF document is too large.")
        return await support_service.assign_quick_reply(agent, **payload.model_dump(), content=content, filename=file.filename)
    except Exception as exc:
        raise _translate_error(exc) from exc
    finally:
        await file.close()


@router.post("/quick-replies/{reply_id}/assign", status_code=status.HTTP_201_CREATED)
async def share_quick_reply(
    payload: ShareQuickReplyRequest, reply_id: int = Path(gt=0),
    agent: SupportAgent = Depends(require_support_csrf),
) -> dict:
    try:
        return await support_service.share_quick_reply(agent, reply_id, **payload.model_dump())
    except Exception as exc:
        raise _translate_error(exc) from exc


@router.put("/quick-replies/{reply_id}/pdf")
async def replace_quick_reply_pdf(
    reply_id: int = Path(gt=0), file: UploadFile = File(...),
    title: str = Form(default="", max_length=80), body: str = Form(default="", max_length=1024),
    agent: SupportAgent = Depends(require_support_csrf),
) -> dict:
    return await _save_quick_reply_pdf_upload(file, title, body, agent, reply_id)


@router.get("/quick-replies/{reply_id}/pdf")
async def quick_reply_pdf(reply_id: int = Path(gt=0), agent: SupportAgent = Depends(get_current_agent)) -> Response:
    from urllib.parse import quote
    try:
        content, filename = await support_service.get_quick_reply_pdf(agent, reply_id)
        return Response(content, media_type="application/pdf", headers={
            "Content-Disposition": "attachment; filename*=UTF-8''" + quote(filename, safe=''),
            "Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "sandbox",
        })
    except Exception as exc:
        raise _translate_error(exc) from exc


async def _save_quick_reply_image_upload(file, title, body, agent, reply_id=None):
    try:
        if file.content_type not in {"image/jpeg", "image/png"}:
            raise SupportValidationError("Choose a JPEG or PNG image.")
        content = await file.read(max_upload_bytes("image") + 1)
        return await support_service.save_quick_reply_pdf(agent, title=title, body=body,
            content=content, filename=file.filename, reply_id=reply_id, mime_type=file.content_type)
    except Exception as exc:
        raise _translate_error(exc) from exc
    finally:
        await file.close()


@router.post("/quick-replies/image", status_code=201)
async def create_quick_reply_image(file: UploadFile = File(...), title: str = Form(default="", max_length=80),
    body: str = Form(default="", max_length=1024), agent: SupportAgent = Depends(require_support_csrf)) -> dict:
    return await _save_quick_reply_image_upload(file, title, body, agent)


@router.put("/quick-replies/{reply_id}/image")
async def replace_quick_reply_image(reply_id: int = Path(gt=0), file: UploadFile = File(...),
    title: str = Form(default="", max_length=80), body: str = Form(default="", max_length=1024),
    agent: SupportAgent = Depends(require_support_csrf)) -> dict:
    return await _save_quick_reply_image_upload(file, title, body, agent, reply_id)


@router.post("/quick-replies/assign/image", status_code=201)
async def assign_quick_reply_image(file: UploadFile = File(...), assignment: str = Form(max_length=40000),
    agent: SupportAgent = Depends(require_support_csrf)) -> dict:
    try:
        if not agent.is_supervisor:
            raise SupportForbiddenError("Only Admins and Super Admins can assign quick replies.")
        if file.content_type not in {"image/jpeg", "image/png"}:
            raise SupportValidationError("Choose a JPEG or PNG image.")
        from pydantic import ValidationError
        try:
            payload = QuickReplyAssignment.model_validate_json(assignment)
        except ValidationError:
            raise SupportValidationError("Invalid quick reply or staff selection.") from None
        content = await file.read(max_upload_bytes("image") + 1)
        return await support_service.assign_quick_reply(agent, **payload.model_dump(), content=content,
            filename=file.filename, mime_type=file.content_type)
    except Exception as exc:
        raise _translate_error(exc) from exc
    finally:
        await file.close()


@router.get("/quick-replies/{reply_id}/image")
async def quick_reply_image(reply_id: int = Path(gt=0), agent: SupportAgent = Depends(get_current_agent)) -> Response:
    from urllib.parse import quote
    try:
        content, filename = await support_service.get_quick_reply_pdf(agent, reply_id, image=True)
        mime = "image/png" if content.startswith(b"\x89PNG") else "image/jpeg"
        return Response(content, media_type=mime, headers={
            "Content-Disposition": "inline; filename*=UTF-8''" + quote(filename, safe=''),
            "Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff", "Content-Security-Policy": "sandbox",
        })
    except Exception as exc:
        raise _translate_error(exc) from exc


@router.get("/ai-settings")
async def get_ai_settings(
    _agent: SupportAgent = Depends(get_current_agent),
) -> dict:
    try:
        return await ai_availability_service.get_settings_payload()
    except Exception as exc:
        raise _translate_error(exc) from exc


@router.put("/ai-settings")
async def update_ai_settings(
    payload: AISettingsUpdateRequest,
    agent: SupportAgent = Depends(require_support_csrf),
) -> dict:
    try:
        return await ai_availability_service.update_settings(
            actor=agent,
            expected_version=payload.version,
            control_mode=payload.control_mode,
            manual_ai_enabled=payload.manual_ai_enabled,
            schedule_start_time=payload.schedule.start_time,
            schedule_end_time=payload.schedule.end_time,
            schedule_timezone=payload.schedule.timezone,
        )
    except Exception as exc:
        raise _translate_error(exc) from exc


@router.get("/ai-usage")
async def get_ai_usage(
    date_from: str | None = Query(None, alias="from", max_length=10),
    date_to: str | None = Query(None, alias="to", max_length=10),
    line: str | None = Query(None, max_length=64),
    model: str | None = Query(None, max_length=100),
    agent: SupportAgent = Depends(get_current_agent),
) -> dict:
    try:
        return await ai_usage_service.usage_report(agent, start=date_from, end=date_to, line=line, model=model)
    except Exception as exc:
        raise _translate_error(exc) from exc


@router.get("/ai-budget")
async def get_ai_budget(agent: SupportAgent = Depends(get_current_agent)) -> dict:
    try:
        return await ai_usage_service.budget_payload(agent)
    except Exception as exc:
        raise _translate_error(exc) from exc


@router.put("/ai-budget")
async def update_ai_budget(
    payload: AIBudgetRequest,
    agent: SupportAgent = Depends(require_support_csrf),
) -> dict:
    try:
        return await ai_usage_service.update_settings(agent, payload.model_dump())
    except Exception as exc:
        raise _translate_error(exc) from exc


@router.get("/ai-prices")
async def get_ai_prices(agent: SupportAgent = Depends(get_current_agent)) -> dict:
    try:
        return await ai_usage_service.price_list(agent)
    except Exception as exc:
        raise _translate_error(exc) from exc


@router.post("/ai-prices")
async def add_ai_price(
    payload: AIPriceRequest,
    agent: SupportAgent = Depends(require_support_csrf),
) -> dict:
    try:
        return await ai_usage_service.add_price(agent, payload.model_dump())
    except Exception as exc:
        raise _translate_error(exc) from exc


@router.post("/ai-prices/{price_id}/verify")
async def verify_ai_price(
    price_id: int,
    agent: SupportAgent = Depends(require_support_csrf),
) -> dict:
    try:
        return await ai_usage_service.verify_price(agent, price_id)
    except Exception as exc:
        raise _translate_error(exc) from exc


@router.post("/ai-prices/{price_id}/retire")
async def retire_ai_price(
    price_id: int,
    agent: SupportAgent = Depends(require_support_csrf),
) -> dict:
    try:
        return await ai_usage_service.retire_price(agent, price_id)
    except Exception as exc:
        raise _translate_error(exc) from exc


@router.get("/ai-settings/openai")
async def get_openai_configuration(
    agent: SupportAgent = Depends(get_current_agent),
) -> dict:
    try:
        return await ai_provider_service.status_payload(agent)
    except Exception as exc:
        raise _translate_error(exc) from exc


@router.put("/ai-settings/openai")
async def save_openai_key(
    payload: OpenAIKeyRequest,
    agent: SupportAgent = Depends(require_support_csrf),
) -> dict:
    try:
        return await ai_provider_service.save_key(
            agent, payload.api_key.get_secret_value(), test_first=payload.test_first,
        )
    except Exception as exc:
        raise _translate_error(exc) from exc


@router.post("/ai-settings/openai/test")
async def test_openai_connection(
    agent: SupportAgent = Depends(require_support_csrf),
) -> dict:
    try:
        return await ai_provider_service.test_connection(agent)
    except Exception as exc:
        raise _translate_error(exc) from exc


@router.delete("/ai-settings/openai")
async def delete_openai_key(
    agent: SupportAgent = Depends(require_support_csrf),
) -> dict:
    try:
        return await ai_provider_service.delete_key(agent)
    except Exception as exc:
        raise _translate_error(exc) from exc


@router.get("/whatsapp-numbers")
async def get_whatsapp_numbers(
    agent: SupportAgent = Depends(get_current_agent),
) -> dict:
    try:
        return await whatsapp_registry_service.list_payload(agent)
    except Exception as exc:
        raise _translate_error(exc) from exc


@router.post("/whatsapp-accounts/sync")
async def sync_whatsapp_account(
    payload: WhatsAppAccountSyncRequest,
    agent: SupportAgent = Depends(require_support_csrf),
) -> dict:
    try:
        return await whatsapp_registry_service.sync_waba(agent, payload.waba_id)
    except Exception as exc:
        raise _translate_error(exc) from exc


# Registered before /whatsapp-numbers/{phone_number_id} so "ai" is not taken as an ID.
@router.put("/whatsapp-numbers/ai")
async def update_all_whatsapp_numbers_ai(
    payload: WhatsAppAllNumbersAIRequest,
    agent: SupportAgent = Depends(require_support_csrf),
) -> dict:
    try:
        return await whatsapp_registry_service.set_all_numbers_ai_enabled(agent, payload.enabled)
    except Exception as exc:
        raise _translate_error(exc) from exc


@router.put("/whatsapp-numbers/{phone_number_id}")
async def update_whatsapp_number_state(
    phone_number_id: str,
    payload: WhatsAppNumberStateRequest,
    agent: SupportAgent = Depends(require_support_csrf),
) -> dict:
    try:
        return await whatsapp_registry_service.set_number_enabled(
            agent, phone_number_id, payload.enabled
        )
    except Exception as exc:
        raise _translate_error(exc) from exc


@router.put("/whatsapp-numbers/{phone_number_id}/ai")
async def update_whatsapp_number_ai(
    phone_number_id: str,
    payload: WhatsAppNumberAIRequest,
    agent: SupportAgent = Depends(require_support_csrf),
) -> dict:
    try:
        return await whatsapp_registry_service.set_number_ai_enabled(agent, phone_number_id, payload.enabled)
    except Exception as exc:
        raise _translate_error(exc) from exc


@router.put("/whatsapp-numbers/{phone_number_id}/name")
async def update_whatsapp_number_name(
    phone_number_id: str,
    payload: WhatsAppNumberNameRequest,
    agent: SupportAgent = Depends(require_support_csrf),
) -> dict:
    try:
        return await whatsapp_registry_service.set_number_display_name(
            agent, phone_number_id, payload.display_name
        )
    except Exception as exc:
        raise _translate_error(exc) from exc


@router.put("/whatsapp-numbers/{phone_number_id}/outreach-limits")
async def update_whatsapp_number_outreach_limits(
    phone_number_id: str,
    payload: WhatsAppNumberOutreachLimitsRequest,
    agent: SupportAgent = Depends(require_support_csrf),
) -> dict:
    try:
        return await whatsapp_registry_service.set_outreach_limits(
            agent, phone_number_id, payload.model_dump()
        )
    except Exception as exc:
        raise _translate_error(exc) from exc


@router.delete("/whatsapp-numbers/{phone_number_id}")
async def remove_saved_whatsapp_number(
    phone_number_id: str,
    agent: SupportAgent = Depends(require_support_csrf),
) -> dict:
    try:
        return await whatsapp_registry_service.remove_saved_number(agent, phone_number_id)
    except Exception as exc:
        raise _translate_error(exc) from exc


@router.get("/staff")
async def list_support_staff(
    agent: SupportAgent = Depends(get_current_agent),
) -> dict:
    try:
        return {"items": await list_staff_accounts(agent)}
    except Exception as exc:
        raise _translate_error(exc) from exc


@router.get("/transfer-targets")
async def list_transfer_targets(
    agent: SupportAgent = Depends(get_current_agent),
) -> dict:
    try:
        return {"items": await support_service.transfer_targets(agent)}
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


@router.get("/events")
async def support_events(
    agent: SupportAgent = Depends(get_current_agent),
) -> StreamingResponse:
    from app.support_auth import revalidate_support_agent

    current_agent = agent
    notification_since = datetime.now(timezone.utc).timestamp()

    async def validate() -> None:
        nonlocal current_agent
        current_agent = await revalidate_support_agent(agent)

    async def authorize(event: dict) -> bool:
        return await support_service.can_receive_support_event(event, current_agent, notification_since=notification_since)

    return StreamingResponse(
        event_stream(authorize, validate=validate),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-store",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@router.get("/queue-counts")
async def queue_counts(
    agent: SupportAgent = Depends(get_current_agent),
) -> dict:
    try:
        return {"counts": await support_service.queue_counts(agent)}
    except Exception as exc:
        raise _translate_error(exc) from exc


@router.get("/contacts")
async def list_contacts(
    q: str = Query(default="", max_length=191),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=500),
    agent: SupportAgent = Depends(get_current_agent),
) -> dict:
    try:
        return await google_contacts_service.list_contacts(
            query=q, page=page, page_size=page_size
        )
    except Exception as exc:
        raise _translate_error(exc) from exc


@router.put("/contacts")
async def edit_contact(
    payload: ContactEditRequest,
    agent: SupportAgent = Depends(require_support_csrf),
) -> dict:
    try:
        return await support_service.edit_contact(
            agent=agent,
            contact_key=payload.contact_key,
            display_name=payload.display_name,
            phone=payload.phone,
        )
    except Exception as exc:
        raise _translate_error(exc) from exc


@router.post("/contacts/import-vcf")
async def import_vcf_contacts(
    file: UploadFile = File(...),
    agent: SupportAgent = Depends(require_support_csrf),
) -> dict:
    try:
        filename = str(file.filename or "").strip()
        if not filename.lower().endswith(".vcf"):
            raise VCFContactsValidationError("Choose a .vcf contacts file.")
        raw = await file.read(MAX_VCF_IMPORT_BYTES + 1)
        if len(raw) > MAX_VCF_IMPORT_BYTES:
            raise VCFContactsTooLargeError("The VCF file is larger than the 10 MB import limit.")
        result = await vcf_contacts_service.import_bytes(
            raw=raw,
            filename=filename,
            staff_id=agent.id,
        )
        result["contact_count"] = await google_contacts_service.count_contacts()
        return result
    except Exception as exc:
        raise _translate_error(exc) from exc
    finally:
        await file.close()


@router.get("/google-contacts/status")
async def google_contacts_status(
    agent: SupportAgent = Depends(get_current_agent),
) -> dict:
    try:
        return await google_contacts_service.status()
    except Exception as exc:
        raise _translate_error(exc) from exc


@router.post("/google-contacts/connect")
async def google_contacts_connect(
    agent: SupportAgent = Depends(require_support_csrf),
) -> dict:
    try:
        if not agent.is_supervisor:
            raise SupportForbiddenError("Only Super Admins and Admins can connect the shared Google Contacts account.")
        return {"authorization_url": await google_contacts_service.begin_connect(
            staff_id=agent.id, session_id=agent.session_id
        )}
    except Exception as exc:
        raise _translate_error(exc) from exc


@router.get("/google-contacts/callback", response_class=HTMLResponse)
async def google_contacts_callback(
    state: str = Query(min_length=8, max_length=256),
    code: str | None = Query(default=None, max_length=4096),
    error: str | None = Query(default=None, max_length=512),
) -> HTMLResponse:
    try:
        body = await google_contacts_service.oauth_callback(state=state, code=code, error=error)
        return HTMLResponse(body, headers={"Cache-Control": "no-store"})
    except Exception as exc:
        logger.exception("Google Contacts OAuth callback failed: exception_type=%s", type(exc).__name__)
        body = (
            "<!doctype html><html><body style='font-family:system-ui,sans-serif;padding:28px'>"
            "<h2>Google Contacts connection failed</h2>"
            "<p>Return to the support application and try connecting again.</p>"
            "<script>try{if(window.opener)window.opener.postMessage({type:'google_contacts_error',message:'Google Contacts connection failed.'},window.location.origin);}catch(_){}</script>"
            "</body></html>"
        )
        return HTMLResponse(body, status_code=400, headers={"Cache-Control": "no-store"})


@router.post("/google-contacts/sync")
async def sync_google_contacts(
    agent: SupportAgent = Depends(require_support_csrf),
) -> dict:
    try:
        # The Google account is shared across the support workspace. Any authenticated
        # staff member may refresh its contacts, while connect/disconnect remain
        # restricted to Supervisors/Admins below and in the connect endpoint.
        return await google_contacts_service.sync()
    except Exception as exc:
        raise _translate_error(exc) from exc


@router.delete("/google-contacts")
async def disconnect_google_contacts(
    agent: SupportAgent = Depends(require_support_csrf),
) -> dict:
    try:
        if not agent.is_supervisor:
            raise SupportForbiddenError("Only Super Admins and Admins can disconnect the shared Google Contacts account.")
        await google_contacts_service.disconnect()
        return {"disconnected": True}
    except Exception as exc:
        raise _translate_error(exc) from exc


@router.get("/chat-lines")
async def chat_lines(
    agent: SupportAgent = Depends(get_current_agent),
) -> dict:
    try:
        return {"items": await support_service.list_chat_lines(agent)}
    except Exception as exc:
        raise _translate_error(exc) from exc


@router.get("/chats")
async def list_chats(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=30, ge=1, le=500),
    staff_id: str | None = Query(default=None, max_length=64),
    support_number: str | None = Query(default=None, max_length=64),
    chat_status: str = Query(default="all", pattern="^(all|open|closed)$"),
    q: str = Query(default="", max_length=200),
    unread: bool = Query(default=False),
    keep_ticket_id: int | None = Query(default=None, ge=1),
    agent: SupportAgent = Depends(get_current_agent),
) -> dict:
    try:
        return await support_service.list_tickets(
            view="chats", agent=agent, page=page, page_size=page_size,
            staff_id=staff_id, support_number=support_number, chat_status=chat_status, query=q,
            unread=unread, keep_ticket_id=keep_ticket_id,
        )
    except Exception as exc:
        raise _translate_error(exc) from exc


@router.post("/chats")
async def start_chat(
    request: StartChatRequest,
    agent: SupportAgent = Depends(require_support_csrf),
) -> dict:
    try:
        return await support_service.start_chat(
            agent=agent,
            phone_number=request.phone_number,
            business_phone_number_id=request.business_phone_number_id,
            body=request.body,
            customer_name=request.customer_name,
        )
    except Exception as exc:
        raise _translate_error(exc) from exc


@router.post("/chats/{chat_id}/close")
async def close_chat(
    chat_id: int,
    agent: SupportAgent = Depends(require_support_csrf),
) -> dict:
    try:
        return await support_service.close_staff_chat(chat_id, agent=agent)
    except Exception as exc:
        raise _translate_error(exc) from exc


@router.post("/tickets/{ticket_id}/messages/{message_id}/forward")
async def forward_ticket_message(
    ticket_id: int,
    message_id: int,
    request: ForwardMessageRequest,
    agent: SupportAgent = Depends(require_support_csrf),
) -> dict:
    try:
        return {"message": await support_service.forward_message(
            source_ticket_id=ticket_id,
            source_message_id=message_id,
            destination_ticket_id=request.destination_ticket_id, client_message_id=request.client_message_id,
            agent=agent,
        )}
    except Exception as exc:
        raise _translate_error(exc) from exc


@router.post("/tickets/{ticket_id}/messages/{message_id}/forward-contact")
async def forward_ticket_message_to_contact(
    ticket_id: int,
    message_id: int,
    request: ForwardContactRequest,
    agent: SupportAgent = Depends(require_support_csrf),
) -> dict:
    try:
        return await support_service.forward_message_to_contact(
            source_ticket_id=ticket_id,
            source_message_id=message_id,
            phone_number=request.phone_number,
            customer_name=request.customer_name,
            agent=agent, client_message_id=request.client_message_id,
        )
    except Exception as exc:
        raise _translate_error(exc) from exc


@router.get("/tickets")
async def list_tickets(
    view: str = Query(
        default="unassigned",
        pattern="^(ai|unassigned|mine|watching|active|waiting|chats|history)$",
    ),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=30, ge=1, le=500),
    staff_id: str | None = Query(default=None, max_length=64),
    support_number: str | None = Query(default=None, max_length=64),
    chat_status: str = Query(default="all", pattern="^(all|open|closed)$"),
    q: str = Query(default="", max_length=200),
    unread: bool = Query(default=False),
    keep_ticket_id: int | None = Query(default=None, ge=1),
    sort: str | None = Query(default=None, pattern="^(oldest|newest)$"),
    agent: SupportAgent = Depends(get_current_agent),
) -> dict:
    try:
        return await support_service.list_tickets(
            view=view,
            agent=agent,
            page=page,
            page_size=page_size,
            staff_id=staff_id, support_number=support_number, chat_status=chat_status, query=q,
            unread=unread, keep_ticket_id=keep_ticket_id,
            sort=sort,
        )
    except Exception as exc:
        raise _translate_error(exc) from exc


@router.get("/search/messages")
async def search_support_messages(
    q: str = Query(min_length=2, max_length=200),
    ticket_id: int | None = Query(default=None, ge=1),
    limit: int = Query(default=80, ge=1, le=100),
    agent: SupportAgent = Depends(get_current_agent),
) -> dict:
    try:
        return await support_service.search_messages(
            query=q, agent=agent, ticket_id=ticket_id, limit=limit
        )
    except Exception as exc:
        raise _translate_error(exc) from exc


@router.delete("/chats/{chat_id}")
async def delete_staff_chat(
    chat_id: int,
    agent: SupportAgent = Depends(require_support_csrf),
) -> dict:
    try:
        return await support_service.delete_staff_chat(chat_id, agent=agent)
    except Exception as exc:
        raise _translate_error(exc) from exc


@router.get("/chats/{chat_id}")
async def chat_detail(
    chat_id: int,
    around_message_id: int | None = Query(default=None, ge=1),
    include_previous_history: bool = Query(default=False),
    agent: SupportAgent = Depends(get_current_agent),
) -> dict:
    try:
        data = await support_service.ticket_detail(
            chat_id, agent, around_message_id=around_message_id,
            include_previous_history=include_previous_history,
        )
        if str((data.get("ticket") or {}).get("conversation_origin") or "") != "STAFF":
            raise SupportNotFoundError("Chat not found.")
        return data
    except Exception as exc:
        raise _translate_error(exc) from exc


@router.get("/tickets/{ticket_id}")
async def ticket_detail(
    ticket_id: int,
    around_message_id: int | None = Query(default=None, ge=1),
    include_previous_history: bool = Query(default=False),
    agent: SupportAgent = Depends(get_current_agent),
) -> dict:
    try:
        return await support_service.ticket_detail(
            ticket_id,
            agent,
            around_message_id=around_message_id,
            include_previous_history=include_previous_history,
        )
    except Exception as exc:
        raise _translate_error(exc) from exc


@router.get("/tickets/{ticket_id}/history")
@router.get("/chats/{ticket_id}/history")
async def conversation_history_page(
    ticket_id: int,
    before_ticket_id: int | None = Query(default=None, ge=1),
    limit: int = Query(default=5, ge=1, le=20),
    agent: SupportAgent = Depends(get_current_agent),
) -> dict:
    try:
        return await support_service.history_page(
            ticket_id, agent, before_ticket_id=before_ticket_id, limit=limit,
        )
    except Exception as exc:
        raise _translate_error(exc) from exc


@router.get("/tickets/{ticket_id}/history/{history_ticket_id}/messages")
@router.get("/chats/{ticket_id}/history/{history_ticket_id}/messages")
async def conversation_history_messages_page(
    ticket_id: int,
    history_ticket_id: int,
    before_message_id: int | None = Query(default=None, ge=1),
    limit: int = Query(default=100, ge=1, le=100),
    agent: SupportAgent = Depends(get_current_agent),
) -> dict:
    try:
        return await support_service.history_messages_page(
            ticket_id, history_ticket_id, agent,
            before_message_id=before_message_id, limit=limit,
        )
    except Exception as exc:
        raise _translate_error(exc) from exc


@router.get("/tickets/{ticket_id}/messages/{message_id}/reply")
@router.get("/chats/{ticket_id}/messages/{message_id}/reply")
async def conversation_reply_destination(
    ticket_id: int,
    message_id: int,
    agent: SupportAgent = Depends(get_current_agent),
) -> dict:
    try:
        return await support_service.reply_destination(ticket_id, message_id, agent)
    except Exception as exc:
        raise _translate_error(exc) from exc


@router.get("/tickets/{ticket_id}/messages")
@router.get("/chats/{ticket_id}/messages")
async def conversation_messages_page(
    ticket_id: int,
    before_message_id: int | None = Query(default=None, ge=1),
    limit: int = Query(default=150, ge=1, le=150),
    agent: SupportAgent = Depends(get_current_agent),
) -> dict:
    try:
        return await support_service.messages_page(
            ticket_id, agent, before_message_id=before_message_id, limit=limit,
        )
    except Exception as exc:
        raise _translate_error(exc) from exc


@router.put("/tickets/{ticket_id}/contact")
async def save_ticket_contact(
    ticket_id: int,
    payload: SaveContactRequest,
    agent: SupportAgent = Depends(require_support_csrf),
) -> dict:
    try:
        return {
            "contact": await support_service.save_contact(
                ticket_id, agent=agent, display_name=payload.name
            )
        }
    except Exception as exc:
        raise _translate_error(exc) from exc


@router.delete("/tickets/{ticket_id}")
async def delete_history_ticket(
    ticket_id: int,
    agent: SupportAgent = Depends(require_support_csrf),
) -> dict:
    try:
        return await support_service.delete_history_ticket(ticket_id, agent=agent)
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


@router.get("/tickets/{ticket_id}/ai-identity")
async def get_ai_identity(
    ticket_id: int,
    agent: SupportAgent = Depends(get_current_agent),
) -> dict:
    """The customer's AI identity level: L0 unknown, L1 phone-matched, L2 staff-verified."""
    try:
        return await support_service.ai_identity(ticket_id, agent)
    except Exception as exc:
        raise _translate_error(exc) from exc


@router.post("/tickets/{ticket_id}/ai-identity/verify")
async def verify_ai_identity(
    ticket_id: int,
    request: AIIdentityVerifyRequest,
    agent: SupportAgent = Depends(require_support_csrf),
) -> dict:
    try:
        return await support_service.verify_ai_identity(
            ticket_id, agent, userid=request.userid, confirmed=request.confirmed,
        )
    except Exception as exc:
        raise _translate_error(exc) from exc


@router.post("/tickets/{ticket_id}/ai-identity/revoke")
async def revoke_ai_identity(
    ticket_id: int,
    agent: SupportAgent = Depends(require_support_csrf),
) -> dict:
    try:
        return await support_service.revoke_ai_identity(ticket_id, agent)
    except Exception as exc:
        raise _translate_error(exc) from exc


@router.post("/tickets/{ticket_id}/take-over")
async def take_over_ai_ticket(
    ticket_id: int,
    agent: SupportAgent = Depends(require_support_csrf),
) -> dict:
    """Take an AI-handled conversation over: the AI stops and the caller owns it."""
    try:
        return await support_service.take_over(ticket_id, agent)
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


@router.post("/tickets/{ticket_id}/transfer")
async def transfer_ticket(
    ticket_id: int,
    request: TransferTicketRequest,
    agent: SupportAgent = Depends(require_support_csrf),
) -> dict:
    try:
        return {
            "ticket": await support_service.transfer(
                ticket_id,
                target_staff_id=request.target_staff_id,
                agent=agent,
            )
        }
    except Exception as exc:
        raise _translate_error(exc) from exc


@router.get("/tickets/{ticket_id}/messages/by-client/{client_message_id}")
async def lookup_ticket_message(ticket_id: int, client_message_id: str,
                                agent: SupportAgent = Depends(get_current_agent)) -> dict:
    try:
        return {"message": await support_service.lookup_agent_message(ticket_id, client_message_id, agent=agent)}
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
                reply_to_message_id=request.reply_to_message_id,
            )
        }
    except Exception as exc:
        raise _translate_error(exc) from exc


@router.post("/tickets/{ticket_id}/messages/{message_id}/resend")
async def resend_ticket_message(
    ticket_id: int,
    message_id: int,
    agent: SupportAgent = Depends(require_support_csrf),
) -> dict:
    try:
        return {"message": await support_service.resend_failed_message(ticket_id, message_id, agent=agent)}
    except Exception as exc:
        raise _translate_error(exc) from exc


@router.post('/tickets/{ticket_id}/messages/{message_id}/check-delivery')
async def check_ticket_message_delivery(ticket_id: int, message_id: int,
                                        agent: SupportAgent = Depends(require_support_csrf)) -> dict:
    try:
        return await support_service.check_message_delivery(ticket_id, message_id, agent=agent)
    except Exception as exc:
        raise _translate_error(exc) from exc


@router.post('/tickets/{ticket_id}/messages/{message_id}/new-copy')
async def copy_uncertain_ticket_message(ticket_id: int, message_id: int, request: UncertainCopyRequest,
                                        agent: SupportAgent = Depends(require_support_csrf)) -> dict:
    try:
        return {'message': await support_service.send_uncertain_copy(ticket_id, message_id, agent=agent,
            accept_duplicate_risk=request.accept_duplicate_risk)}
    except Exception as exc:
        raise _translate_error(exc) from exc


@router.patch("/tickets/{ticket_id}/messages/{message_id}")
async def edit_ticket_message(
    ticket_id: int, message_id: int, request: EditMessageRequest,
    agent: SupportAgent = Depends(require_support_csrf),
) -> dict:
    try:
        return {"message": await support_service.change_message(
            ticket_id, message_id, agent=agent, action="edit",
            content=request.content, version=request.version,
        )}
    except Exception as exc:
        raise _translate_error(exc) from exc


@router.post("/tickets/{ticket_id}/messages/{message_id}/delete")
async def delete_ticket_message(
    ticket_id: int, message_id: int, request: DeleteMessageRequest,
    agent: SupportAgent = Depends(require_support_csrf),
) -> dict:
    try:
        return {"message": await support_service.change_message(
            ticket_id, message_id, agent=agent, action=request.scope,
        )}
    except Exception as exc:
        raise _translate_error(exc) from exc


@router.post("/tickets/{ticket_id}/messages/{message_id}/reaction")
async def react_to_ticket_message(
    ticket_id: int,
    message_id: int,
    request: SendReactionRequest,
    agent: SupportAgent = Depends(require_support_csrf),
) -> dict:
    try:
        return {
            "reaction": await support_service.send_agent_reaction(
                ticket_id,
                message_id,
                agent=agent,
                emoji=request.emoji,
                client_reaction_id=request.client_reaction_id,
                context_ticket_id=request.context_ticket_id,
            )
        }
    except Exception as exc:
        raise _translate_error(exc) from exc


@router.post("/tickets/{ticket_id}/location")
async def send_ticket_location(
    ticket_id: int,
    request: SendLocationRequest,
    agent: SupportAgent = Depends(require_support_csrf),
) -> dict:
    try:
        return {
            "message": await support_service.send_agent_location(
                ticket_id,
                agent=agent,
                latitude=request.latitude,
                longitude=request.longitude,
                name=request.name,
                address=request.address,
                client_message_id=request.client_message_id,
                reply_to_message_id=request.reply_to_message_id,
            )
        }
    except Exception as exc:
        raise _translate_error(exc) from exc


@router.post("/tickets/{ticket_id}/media")
async def send_ticket_media(
    ticket_id: int,
    media_type: Literal["image", "audio", "document", "sticker", "video"] = Form(...),
    client_message_id: str = Form(..., min_length=1, max_length=191),
    caption: str = Form(default="", max_length=1024),
    reply_to_message_id: int | None = Form(default=None, ge=1),
    file: UploadFile = File(...),
    agent: SupportAgent = Depends(require_support_csrf),
) -> dict:
    limit = max_upload_bytes(media_type)
    try:
        content = await file.read(max(1, limit) + 1)
        if len(content) > limit:
            if media_type == "sticker":
                raise SupportValidationError(
                    f"The sticker is too large. Maximum size is {limit // 1024} KB."
                )
            max_mb = max(1, limit // (1024 * 1024))
            raise SupportValidationError(
                f"The {media_type} is too large. Maximum size is {max_mb} MB."
            )
        return {
            "message": await support_service.send_agent_media(
                ticket_id,
                agent=agent,
                media_type=media_type,
                content=content,
                content_type=file.content_type,
                filename=file.filename,
                caption=caption,
                client_message_id=client_message_id,
                reply_to_message_id=reply_to_message_id,
            )
        }
    except Exception as exc:
        raise _translate_error(exc) from exc
    finally:
        await file.close()


@router.get("/tickets/{ticket_id}/messages/{message_id}/media")
async def ticket_message_media(
    ticket_id: int,
    message_id: int,
    context_ticket_id: int | None = Query(default=None, ge=1),
    agent: SupportAgent = Depends(get_current_agent),
) -> FileResponse:
    try:
        asset = await support_service.get_ticket_media(
            ticket_id, message_id, agent, context_ticket_id=context_ticket_id
        )
    except Exception as exc:
        raise _translate_error(exc) from exc
    # Browsers cannot preview a workbook, so it is always downloaded under its
    # own name. PDFs and other media keep rendering inline in the dashboard.
    download = is_spreadsheet_mime(asset.mime_type)
    return FileResponse(
        path=asset.path,
        media_type=asset.mime_type,
        filename=asset.filename if download else None,
        content_disposition_type="attachment",
        headers={
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.post("/tickets/{ticket_id}/resolve")
async def resolve_ticket(
    ticket_id: int,
    agent: SupportAgent = Depends(require_support_csrf),
) -> dict:
    try:
        return await support_service.resolve_and_return_to_ai(ticket_id, agent=agent)
    except Exception as exc:
        raise _translate_error(exc) from exc


@router.post("/tickets/{ticket_id}/reopen")
async def reopen_ticket(
    ticket_id: int,
    payload: ReopenTicketRequest,
    agent: SupportAgent = Depends(require_support_csrf),
) -> dict:
    try:
        return await support_service.reopen_ticket(ticket_id, agent=agent, resolved_at=payload.resolved_at)
    except Exception as exc:
        raise _translate_error(exc) from exc


class DashboardReadRequest(BaseModel):
    message_id: int = Field(gt=0)


@router.post("/tickets/{ticket_id}/dashboard-read")
async def mark_dashboard_read(
    ticket_id: int, payload: DashboardReadRequest,
    agent: SupportAgent = Depends(require_support_csrf),
) -> dict:
    try:
        return await support_service.mark_dashboard_read(ticket_id, payload.message_id, agent)
    except Exception as exc:
        raise _translate_error(exc) from exc
