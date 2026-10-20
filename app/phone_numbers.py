"""Turn staff-entered phone numbers into WhatsApp's international digits.

Contacts imported from phones and Google often store local numbers such as
``03 512 345``. Sent as-is, WhatsApp treats those digits as a full international
number, so the message never reaches the customer (or reaches someone else).
"""
from __future__ import annotations

import phonenumbers


class PhoneNumberError(ValueError):
    pass


INVALID_NUMBER_MESSAGE = (
    "Enter a valid WhatsApp number with its country code, for example +961 3 123 456."
)


def region_for_number(value: str | None) -> str | None:
    """Two-letter region of an international number, e.g. 'LB' for +961 1 234 567."""
    digits = "".join(char for char in str(value or "") if char.isdigit())
    if not digits:
        return None
    try:
        region = phonenumbers.region_code_for_number(phonenumbers.parse("+" + digits))
    except phonenumbers.NumberParseException:
        return None
    return region if region and region != "ZZ" else None


def _valid_digits(raw: str, region: str | None) -> str | None:
    try:
        number = phonenumbers.parse(raw, region)
    except phonenumbers.NumberParseException:
        return None
    if not phonenumbers.is_valid_number(number):
        return None
    return phonenumbers.format_number(number, phonenumbers.PhoneNumberFormat.E164).lstrip("+")


def to_whatsapp_digits(value: str | None, *, default_region: str | None = None) -> str:
    """Return the number as international digits (no '+'), or raise PhoneNumberError.

    Numbers written with '+' or '00' are international. Otherwise a number that
    is valid as a local number in ``default_region`` (the WhatsApp line's
    country) is read as local first, then as international digits without '+'.
    """
    raw = str(value or "").strip()
    digits = "".join(char for char in raw if char.isdigit())
    if not digits or len(digits) > 17:
        raise PhoneNumberError(INVALID_NUMBER_MESSAGE)
    if raw.startswith("+") or digits.startswith("00"):
        international = digits[2:] if digits.startswith("00") and not raw.startswith("+") else digits
        result = _valid_digits("+" + international, None)
    else:
        result = (_valid_digits(raw, default_region) if default_region else None) or _valid_digits("+" + digits, None)
    if not result:
        raise PhoneNumberError(INVALID_NUMBER_MESSAGE)
    return result
