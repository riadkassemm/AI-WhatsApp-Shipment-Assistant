"""Explicit Admin/Super Admin capability, real cookie/CSRF boundary, bounded inputs."""
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Response
from pydantic import BaseModel, ConfigDict, Field

from app.bridge_management_api import management_reader
from app.config import settings
from app.bridge_recovery_repository import RecoveryConflict
from app.bridge_recovery_service import bridge_recovery_service, REASONS, safe_code, serialize_operation
from app.support_auth import SupportAgent, require_support_csrf

router = APIRouter(prefix='/api/support/whatsapp-linked-device', tags=['bridge-recovery'])


class OperationRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    operation_id: UUID
    action: Literal['reconnect', 'pause', 'resume']
    expected_version: int = Field(ge=0, le=2**53-1, strict=True)
    expected_generation: int = Field(ge=0, le=2**53-1, strict=True)
    expected_epoch: UUID
    confirm_line_id: str = Field(pattern=r'^[0-9]{5,32}$')


async def recovery_writer(response: Response, agent: SupportAgent = Depends(require_support_csrf)):
    response.headers['Cache-Control'] = 'no-store'
    if not agent.can_recover_whatsapp:
        raise HTTPException(403, 'Only Admins and Super Admins can request safe WhatsApp recovery.')
    return agent


def failure(exc):
    if isinstance(exc, KeyError): return HTTPException(404, 'This operation was not found.')
    code = safe_code(exc.code) if isinstance(exc, RecoveryConflict) else 'RECOVERY_REVIEW_REQUIRED'
    return HTTPException(429 if code == 'ACTION_RATE_LIMITED' else 409 if isinstance(exc, RecoveryConflict) else 503,
                         detail={'code':code,'message':REASONS[code]})


@router.post('/lines/{line_id}/operations', status_code=202)
async def create_operation(body: OperationRequest, line_id: str = Path(pattern=r'^[0-9]{5,32}$'),
                           idempotency_key: UUID = Header(alias='Idempotency-Key'),
                           agent: SupportAgent = Depends(recovery_writer)):
    if body.confirm_line_id != line_id or idempotency_key != body.operation_id:
        raise HTTPException(422, 'Confirm the selected number and use the same operation ID as the idempotency key.')
    if not settings.whatsapp_admin_recovery_enabled:
        raise HTTPException(409, detail={'code':'PHASE_B_UNAVAILABLE','message':REASONS['PHASE_B_UNAVAILABLE']})
    request = {'id':str(body.operation_id),'action':body.action,'expected_version':body.expected_version,
               'expected_generation':body.expected_generation,'expected_epoch':str(body.expected_epoch)}
    try: return await bridge_recovery_service.create(line_id, agent, request)
    except Exception as exc: raise failure(exc) from None


@router.get('/operations/{operation_id}')
async def operation_status(operation_id: UUID, agent: SupportAgent = Depends(management_reader)):
    try: return await bridge_recovery_service.get(str(operation_id))
    except Exception as exc: raise failure(exc) from None


@router.get('/lines/{line_id}/operations')
async def operation_history(line_id: str = Path(pattern=r'^[0-9]{5,32}$'), agent: SupportAgent = Depends(management_reader)):
    try: return await bridge_recovery_service.history(line_id)
    except Exception as exc: raise failure(exc) from None


@router.post('/operations/{operation_id}/cancel')
async def cancel_operation(operation_id: UUID, agent: SupportAgent = Depends(recovery_writer)):
    try: return serialize_operation(await bridge_recovery_service.repository.cancel(str(operation_id), agent))
    except Exception as exc: raise failure(exc) from None
