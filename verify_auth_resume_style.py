from __future__ import annotations

import re

from app.conversation_store import Conversation
from app.language import (
    auth_prompt,
    detect_communication_style,
    login_confirmation,
    password_prompt,
)


def main() -> None:
    cases = {
        "bade tshefle order": "leb_arabizi",
        "shefle el order": "leb_arabizi",
        "bade check my order": "leb_arabizi",
        "check my order": "en",
    }
    for text, expected in cases.items():
        actual = detect_communication_style(text).style
        if actual != expected:
            raise SystemExit(
                f"Style verification failed for {text!r}: expected {expected}, got {actual}"
            )

    arabic_script = re.compile(r"[\u0600-\u06FF]")
    templates = [
        auth_prompt("leb_arabizi"),
        password_prompt("leb_arabizi"),
        login_confirmation("leb_arabizi"),
    ]
    if any(arabic_script.search(value) for value in templates):
        raise SystemExit("Arabizi authentication template contains Arabic-script characters.")

    conversation = Conversation(
        phone_number="test",
        pending_request="bade tshefle order",
        pending_tool_name="get_customer_shipments",
        pending_tool_arguments={},
    )
    restored = Conversation.from_json(conversation.to_json())
    if restored.pending_request != "bade tshefle order":
        raise SystemExit("pending_request did not survive conversation serialization.")
    if restored.pending_tool_name != "get_customer_shipments":
        raise SystemExit("pending_tool_name did not survive conversation serialization.")
    if restored.pending_tool_arguments != {}:
        raise SystemExit("pending_tool_arguments did not survive conversation serialization.")

    print("OK: pending-request resume state and Arabizi authentication style are configured.")


if __name__ == "__main__":
    main()
