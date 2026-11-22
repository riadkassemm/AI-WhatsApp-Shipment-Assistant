"""De-identification of historical transcripts (Step 4b) is deterministic and model-free."""
from __future__ import annotations

import sys
import types

for _name in ("aiomysql", "bcrypt", "openai"):
    sys.modules.setdefault(_name, types.ModuleType(_name))

from app.knowledge.deidentify import Deidentifier, deidentify_transcript  # noqa: E402


def test_personal_data_becomes_stable_placeholders() -> None:
    tool = Deidentifier(names=["Rami Haddad"])
    first = tool.text("Hi, I'm Rami Haddad, call me on +961 70 123 456 or rami@example.com")
    assert "Rami" not in first and "70 123" not in first and "example.com" not in first
    assert "<NAME_1>" in first and "<PHONE_1>" in first and "<EMAIL_1>" in first
    # The same number later in the conversation keeps the same placeholder.
    assert tool.text("again: 0096170123456") == "again: <PHONE_1>"
    assert "<PHONE_2>" in tool.text("my brother 03 555 777")


def test_passwords_ids_and_amounts_are_removed() -> None:
    tool = Deidentifier()
    text = tool.text("user id AB12345 password: hunter22, order 556677 costs $45.50 or 4,000,000 LBP")
    for secret in ("hunter22", "AB12345", "556677", "45.50", "4,000,000"):
        assert secret not in text
    assert "<SECRET>" in text and "<ID_1>" in text and "<AMOUNT_1>" in text and "<AMOUNT_2>" in text


def test_ordinary_text_media_and_arabic_are_kept() -> None:
    tool = Deidentifier()
    assert tool.text("[image]") == "[image]"
    assert tool.text("Where is my parcel? It was sent 3 days ago") == "Where is my parcel? It was sent 3 days ago"
    assert tool.text("وين الطرد تبعي؟") == "وين الطرد تبعي؟"
    assert tool.text("kifak, ba3atet el parcel?") == "kifak, ba3atet el parcel?"


def test_transcripts_keep_roles_and_order() -> None:
    rows = deidentify_transcript(
        [{"role": "customer", "text": "Tracking TRK998877?"}, {"role": "agent", "text": "TRK998877 is in Beirut"}],
    )
    assert [row["role"] for row in rows] == ["customer", "agent"]
    assert rows[0]["text"] == "Tracking <ID_1>?" and rows[1]["text"] == "<ID_1> is in Beirut"
