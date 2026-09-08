import httpx

from app.config import settings


async def notify_human_agents(
    customer_id: str,
    reason: str,
    ticket_reference: str | None = None,
) -> None:
    """Send an optional secondary alert after a durable ticket exists."""
    if not settings.human_handoff_webhook_url:
        return

    prefix = f"Support ticket {ticket_reference}: " if ticket_reference else ""
    payload = {
        "text": f"🔁 {prefix}handoff requested for customer {customer_id}: {reason}"
    }
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            await client.post(settings.human_handoff_webhook_url, json=payload)
        except httpx.RequestError:
            pass
