from __future__ import annotations

import base64
from dataclasses import dataclass
import logging
import quopri
import re
from typing import Any

import aiomysql

from app.config import settings
from app.customer_directory import normalize_phone
from app.support_events import publish_support_event

logger = logging.getLogger("shipment-bot")

MAX_VCF_IMPORT_BYTES = 10 * 1024 * 1024
MAX_VCF_CARDS = 100_000


class VCFContactsError(Exception):
    pass


class VCFContactsValidationError(VCFContactsError):
    pass


class VCFContactsTooLargeError(VCFContactsValidationError):
    pass


class VCFContactsSchemaError(VCFContactsError):
    pass


@dataclass(frozen=True)
class ParsedVCFContact:
    normalized_phone: str
    display_name: str
    email: str | None = None
    phone_label: str | None = None


@dataclass(frozen=True)
class VCFParseResult:
    contacts: list[ParsedVCFContact]
    cards_seen: int
    duplicate_phones: int
    skipped_without_phone: int
    skipped_invalid_phone: int


def _unescape_vcard(value: str) -> str:
    # vCard escaping is deliberately decoded after transfer encoding so an
    # escaped comma/semicolon is not confused with the property structure.
    value = value.replace("\\n", "\n").replace("\\N", "\n")
    value = value.replace("\\,", ",").replace("\\;", ";")
    return value.replace("\\\\", "\\").strip()


def _decode_property_value(value: str, params: dict[str, str]) -> str:
    encoding = params.get("ENCODING", "").upper()
    charset = params.get("CHARSET", "utf-8") or "utf-8"
    if encoding == "QUOTED-PRINTABLE":
        try:
            decoded = quopri.decodestring(value.encode("utf-8"))
            return _unescape_vcard(decoded.decode(charset, errors="replace"))
        except (LookupError, UnicodeError):
            return _unescape_vcard(quopri.decodestring(value.encode("utf-8")).decode("utf-8", errors="replace"))
    if encoding in {"B", "BASE64"}:
        try:
            decoded = base64.b64decode(value, validate=False)
            return _unescape_vcard(decoded.decode(charset, errors="replace"))
        except Exception:
            return _unescape_vcard(value)
    return _unescape_vcard(value)


def _logical_lines(raw: bytes) -> list[str]:
    # Most exported address books are UTF-8. Replacement mode keeps malformed
    # legacy bytes from aborting a whole import; encoded vCard fields are then
    # decoded according to their explicit CHARSET/ENCODING parameters below.
    text = raw.decode("utf-8-sig", errors="replace").replace("\r\n", "\n").replace("\r", "\n")
    physical = text.split("\n")
    logical: list[str] = []
    for line in physical:
        if logical and line.startswith((" ", "\t")):
            logical[-1] += line[1:]
            continue
        if logical and logical[-1].endswith("=") and "ENCODING=QUOTED-PRINTABLE" in logical[-1].upper():
            logical[-1] = logical[-1][:-1] + line
            continue
        logical.append(line)
    return logical


def _property(line: str) -> tuple[str, dict[str, str], list[str], str] | None:
    if ":" not in line:
        return None
    left, value = line.split(":", 1)
    pieces = left.split(";")
    name = pieces[0].upper().strip()
    params: dict[str, str] = {}
    bare: list[str] = []
    for piece in pieces[1:]:
        piece = piece.strip()
        if not piece:
            continue
        if "=" in piece:
            key, param_value = piece.split("=", 1)
            params[key.upper()] = param_value.strip().strip('"')
        else:
            bare.append(piece)
    return name, params, bare, value


def _name_from_n(value: str) -> str:
    components = [_unescape_vcard(part).strip() for part in value.split(";")]
    family = components[0] if len(components) > 0 else ""
    given = components[1] if len(components) > 1 else ""
    additional = components[2] if len(components) > 2 else ""
    prefix = components[3] if len(components) > 3 else ""
    suffix = components[4] if len(components) > 4 else ""
    return " ".join(part for part in [prefix, given, additional, family, suffix] if part).strip()


def _contact_score(contact: ParsedVCFContact) -> tuple[int, int, int]:
    name = contact.display_name.strip()
    numeric_name = bool(re.fullmatch(r"\+?\d[\d\s().-]*", name))
    return (0 if numeric_name else 1, 1 if contact.email else 0, len(name))


def parse_vcf(raw: bytes) -> VCFParseResult:
    if not raw:
        raise VCFContactsValidationError("The VCF file is empty.")
    if len(raw) > MAX_VCF_IMPORT_BYTES:
        raise VCFContactsTooLargeError("The VCF file is larger than the 10 MB import limit.")

    cards: list[list[str]] = []
    current: list[str] | None = None
    for line in _logical_lines(raw):
        marker = line.strip().upper()
        if marker == "BEGIN:VCARD":
            if current is not None:
                # Recover gracefully from a malformed nested BEGIN by closing
                # the previous card rather than losing the remaining file.
                cards.append(current)
            current = []
            continue
        if marker == "END:VCARD":
            if current is not None:
                cards.append(current)
                current = None
                if len(cards) > MAX_VCF_CARDS:
                    raise VCFContactsValidationError("The VCF file contains too many contact cards.")
            continue
        if current is not None:
            current.append(line)
    if current:
        cards.append(current)
    if not cards:
        raise VCFContactsValidationError("No vCard contacts were found in the uploaded file.")

    by_phone: dict[str, ParsedVCFContact] = {}
    duplicate_phones = 0
    skipped_without_phone = 0
    skipped_invalid_phone = 0

    for card in cards:
        fn = ""
        fallback_n = ""
        org = ""
        emails: list[str] = []
        phones: list[tuple[str, str | None]] = []
        for line in card:
            parsed = _property(line)
            if not parsed:
                continue
            name, params, bare, raw_value = parsed
            value = _decode_property_value(raw_value, params)
            if name == "FN" and not fn:
                fn = value.strip()
            elif name == "N" and not fallback_n:
                fallback_n = _name_from_n(value)
            elif name == "ORG" and not org:
                org = value.replace(";", " ").strip()
            elif name == "EMAIL":
                email = value.strip()
                if email and len(email) <= 320:
                    emails.append(email)
            elif name == "TEL":
                normalized = normalize_phone(value)
                if not normalized:
                    skipped_invalid_phone += 1
                    continue
                if len(normalized) < 7 or len(normalized) > 32:
                    skipped_invalid_phone += 1
                    continue
                label = params.get("TYPE") or (bare[0] if bare else None)
                phones.append((normalized, label[:64] if label else None))

        if not phones:
            skipped_without_phone += 1
            continue
        display_name = (fn or fallback_n or org).strip()
        email = emails[0] if emails else None
        for normalized, label in phones:
            candidate = ParsedVCFContact(
                normalized_phone=normalized,
                display_name=(display_name or normalized)[:191],
                email=email,
                phone_label=label,
            )
            existing = by_phone.get(normalized)
            if existing is None:
                by_phone[normalized] = candidate
            else:
                duplicate_phones += 1
                if _contact_score(candidate) > _contact_score(existing):
                    by_phone[normalized] = candidate

    if not by_phone:
        raise VCFContactsValidationError("No usable phone contacts were found in the VCF file.")
    return VCFParseResult(
        contacts=list(by_phone.values()),
        cards_seen=len(cards),
        duplicate_phones=duplicate_phones,
        skipped_without_phone=skipped_without_phone,
        skipped_invalid_phone=skipped_invalid_phone,
    )


class VCFContactsService:
    def __init__(self) -> None:
        self._pool: aiomysql.Pool | None = None
        self._schema_checked = False

    async def _ensure_pool(self) -> aiomysql.Pool:
        if self._pool is None:
            self._pool = await aiomysql.create_pool(
                host=settings.db_host,
                port=settings.db_port,
                user=settings.db_user,
                password=settings.db_password,
                db=settings.db_name,
                minsize=1,
                maxsize=4,
                autocommit=True,
                charset="utf8mb4",
            )
        if not self._schema_checked:
            async with self._pool.acquire() as conn:
                async with conn.cursor(aiomysql.DictCursor) as cur:
                    await cur.execute(
                        """
                        SELECT TABLE_NAME AS table_name
                        FROM INFORMATION_SCHEMA.TABLES
                        WHERE TABLE_SCHEMA = %s AND TABLE_NAME = 'support_vcf_contacts'
                        """,
                        (settings.db_name,),
                    )
                    row = await cur.fetchone()
            if not row:
                raise VCFContactsSchemaError(
                    "VCF contacts database schema is out of date. Apply "
                    "migrations/20260925_vcf_contacts.sql before importing contacts."
                )
            self._schema_checked = True
        return self._pool

    async def import_bytes(
        self,
        *,
        raw: bytes,
        filename: str,
        staff_id: str,
    ) -> dict[str, Any]:
        parsed = parse_vcf(raw)
        pool = await self._ensure_pool()
        clean_filename = str(filename or "contacts.vcf").strip().rsplit("/", 1)[-1].rsplit("\\", 1)[-1][:255]

        phones = [contact.normalized_phone for contact in parsed.contacts]
        existing: set[str] = set()
        # Fetch existing imported phones in chunks so a large address book does
        # not exceed MariaDB's placeholder/packet limits.
        async with pool.acquire() as conn:
            await conn.begin()
            try:
                async with conn.cursor(aiomysql.DictCursor) as cur:
                    for start in range(0, len(phones), 500):
                        chunk = phones[start:start + 500]
                        placeholders = ",".join(["%s"] * len(chunk))
                        await cur.execute(
                            f"SELECT normalized_phone FROM support_vcf_contacts WHERE normalized_phone IN ({placeholders})",
                            tuple(chunk),
                        )
                        existing.update(str(row["normalized_phone"]) for row in await cur.fetchall())

                    for contact in parsed.contacts:
                        await cur.execute(
                            """
                            INSERT INTO support_vcf_contacts (
                                normalized_phone, display_name, email, phone_label,
                                source_filename, imported_by_staff_id, created_at, updated_at
                            ) VALUES (%s, %s, %s, %s, %s, %s, UTC_TIMESTAMP(6), UTC_TIMESTAMP(6))
                            ON DUPLICATE KEY UPDATE
                                display_name = VALUES(display_name),
                                email = VALUES(email),
                                phone_label = VALUES(phone_label),
                                source_filename = VALUES(source_filename),
                                imported_by_staff_id = VALUES(imported_by_staff_id),
                                updated_at = UTC_TIMESTAMP(6)
                            """,
                            (
                                contact.normalized_phone,
                                contact.display_name,
                                contact.email,
                                contact.phone_label,
                                clean_filename,
                                staff_id,
                            ),
                        )
                await conn.commit()
            except Exception:
                await conn.rollback()
                raise

        imported = sum(1 for phone in phones if phone not in existing)
        updated = len(phones) - imported
        await publish_support_event(
            "contacts_synced",
            source="vcf",
            imported=imported,
            updated=updated,
            staff_id=staff_id,
        )
        logger.info(
            "VCF contacts imported: staff_id=%s cards=%s unique=%s imported=%s updated=%s duplicates=%s",
            staff_id,
            parsed.cards_seen,
            len(parsed.contacts),
            imported,
            updated,
            parsed.duplicate_phones,
        )
        return {
            "cards_seen": parsed.cards_seen,
            "unique_contacts": len(parsed.contacts),
            "imported": imported,
            "updated": updated,
            "duplicates_removed": parsed.duplicate_phones,
            "skipped_without_phone": parsed.skipped_without_phone,
            "skipped_invalid_phone": parsed.skipped_invalid_phone,
        }


vcf_contacts_service = VCFContactsService()
