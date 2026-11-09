"""Administrative reads only. Existing mutation routes retain their own guards."""
from fastapi import APIRouter, Depends, HTTPException, Path, Response

from app.bridge_management_service import bridge_management_service
from app.support_auth import SupportAgent, get_current_agent

router = APIRouter(prefix="/api/support/whatsapp-linked-device", tags=["bridge-management"])


async def management_reader(response: Response, agent: SupportAgent = Depends(get_current_agent)) -> SupportAgent:
    response.headers["Cache-Control"] = "no-store"
    if not agent.can_view_whatsapp_management:
        raise HTTPException(403, "Only Admins and Super Admins can view WhatsApp management.")
    return agent


@router.get("/management")
async def management(agent: SupportAgent = Depends(management_reader)) -> dict:
    return await bridge_management_service.management()


async def read_detail(kind: str, line_id: str) -> dict:
    try:
        return await getattr(bridge_management_service, kind)(line_id)
    except KeyError:
        raise HTTPException(404, "This WhatsApp identity is not available for management.") from None
    except Exception:
        raise HTTPException(503, detail={"code": "DIAGNOSTICS_UNAVAILABLE",
            "message": "This diagnostic is temporarily unavailable. No connection changes were made."}) from None


@router.get("/lines/{line_id}/diagnostics")
async def diagnostics(line_id: str = Path(pattern=r"^[0-9]{5,32}$"), agent: SupportAgent = Depends(management_reader)):
    return await read_detail("line", line_id)


@router.get("/lines/{line_id}/auth-recovery")
async def auth_recovery(line_id: str = Path(pattern=r"^[0-9]{5,32}$"), agent: SupportAgent = Depends(management_reader)):
    if not agent.can_assess_whatsapp_authentication:
        raise HTTPException(403, "Only Admins and Super Admins can view detailed authentication assessments.")
    return await read_detail("assessment", line_id)


@router.get("/lines/{line_id}/history")
async def history(line_id: str = Path(pattern=r"^[0-9]{5,32}$"), agent: SupportAgent = Depends(management_reader)):
    return await read_detail("history", line_id)
