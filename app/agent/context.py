"""Build provider context from this ticket only, never Redis conversation history."""
from __future__ import annotations

import re

from app.client_chat_auth import redact_sensitive_text

# Legacy tickets can contain unlabelled password answers. Mask the answer to a
# password prompt, and ambiguous standalone credential-like strings even when
# the prompt fell outside the bounded context window. Staff storage is untouched.
_PASSWORD = re.compile(r"password|passwd|\bpwd\b|mot de passe|كلمة\s*(?:السر|المرور)|باسورد", re.I)
_BARE_SECRET = re.compile(r"(?=\S{6,}$)(?=.*[A-Za-z])(?:(?=.*[@#$%*!_])|(?=(?:\D*\d){3}))[\S]+$")


def masked_messages(rows: list[dict], *, source_message_id: str | None,
                    redacted_current_text: str | None = None) -> list[dict[str, str]]:
    messages = []
    password_prompt = False
    for row in rows:
        role = "user" if row["sender_type"] == "CUSTOMER" else "assistant"
        raw = str(row.get("body") or "")
        if row.get("ai_media_text"):
            raw += "\n[Untrusted customer media]\n" + str(row["ai_media_text"])
        if (redacted_current_text is not None and source_message_id
                and row.get("whatsapp_message_id") == source_message_id):
            body = redacted_current_text
        elif role == "user" and (password_prompt or _BARE_SECRET.fullmatch(raw.strip())):
            body = "[credential withheld]"
        else:
            body = raw
        body = redact_sensitive_text(body)
        password_prompt = role == "assistant" and bool(_PASSWORD.search(raw))
        messages.append({"role": role, "text": body[:4096]})
    return messages[-12:]
