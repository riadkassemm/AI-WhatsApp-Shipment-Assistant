from __future__ import annotations

import base64
from contextlib import asynccontextmanager
import hashlib
import html
import json
import logging
import secrets
from datetime import datetime
from typing import Any
from urllib.parse import urlencode

import aiomysql
import httpx
from cryptography.fernet import Fernet, InvalidToken

from app.config import settings
from app.customer_directory import normalize_phone
from app.support_events import publish_support_event
from app.staff_repository import staff_repository

logger = logging.getLogger("shipment-bot")

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
GOOGLE_PEOPLE_CONNECTIONS_URL = "https://people.googleapis.com/v1/people/me/connections"
GOOGLE_REVOKE_URL = "https://oauth2.googleapis.com/revoke"
GOOGLE_SCOPES = (
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
    "https://www.googleapis.com/auth/contacts.readonly",
)
OAUTH_STATE_PREFIX = "support:google-contacts-oauth:"
OAUTH_STATE_TTL_SECONDS = 10 * 60

SYNC_LOCK_KEY = "support:google-contacts-sync-lock"
SYNC_LOCK_TTL_SECONDS = 180
PERSON_FIELDS = "names,phoneNumbers,emailAddresses,metadata"


class GoogleContactsError(Exception):
    pass


class GoogleContactsConfigurationError(GoogleContactsError):
    pass


class GoogleContactsSchemaError(GoogleContactsError):
    pass


class GoogleContactsProviderError(GoogleContactsError):
    pass


class GoogleContactsNotConnectedError(GoogleContactsError):
    pass


def _b64url_sha256(value: str) -> str:
    digest = hashlib.sha256(value.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def _oauth_callback_html(*, ok: bool, message: str) -> str:
    safe_message = html.escape(message)
    event = "google_contacts_connected" if ok else "google_contacts_error"
    payload = json.dumps({"type": event, "message": message}, ensure_ascii=False)
    return f"""<!doctype html>
<html><head><meta charset=\"utf-8\"><title>Google Contacts</title></head>
<body style=\"font-family:system-ui,sans-serif;padding:28px\">
<h2>{'Google Contacts connected' if ok else 'Google Contacts connection failed'}</h2>
<p>{safe_message}</p>
<script>
try {{ if (window.opener) window.opener.postMessage({payload}, window.location.origin); }} catch (_) {{}}
setTimeout(() => window.close(), 450);
</script>
</body></html>"""


class GoogleContactsService:
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
                maxsize=6,
                autocommit=True,
                charset="utf8mb4",
            )
        if not self._schema_checked:
            await self._validate_schema(self._pool)
            self._schema_checked = True
        return self._pool

    async def _validate_schema(self, pool: aiomysql.Pool) -> None:
        required = {"support_google_accounts", "support_google_contacts", "support_vcf_contacts"}
        async with pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(
                    """
                    SELECT TABLE_NAME AS table_name
                    FROM INFORMATION_SCHEMA.TABLES
                    WHERE TABLE_SCHEMA = %s
                      AND TABLE_NAME IN ('support_google_accounts', 'support_google_contacts', 'support_vcf_contacts')
                    """,
                    (settings.db_name,),
                )
                rows = await cur.fetchall()
        found = {str(row.get("table_name") or "") for row in rows}
        if not required.issubset(found):
            raise GoogleContactsSchemaError(
                "Contacts database schema is out of date. Apply "
                "migrations/20260923_google_contacts.sql and "
                "migrations/20260925_vcf_contacts.sql before using the Contacts section."
            )

    @staticmethod
    def _require_configured() -> None:
        missing = []
        if not settings.google_contacts_client_id.strip():
            missing.append("GOOGLE_CONTACTS_CLIENT_ID")
        if not settings.google_contacts_client_secret.strip():
            missing.append("GOOGLE_CONTACTS_CLIENT_SECRET")
        if not settings.google_contacts_redirect_uri.strip():
            missing.append("GOOGLE_CONTACTS_REDIRECT_URI")
        if not settings.google_contacts_token_encryption_key.strip():
            missing.append("GOOGLE_CONTACTS_TOKEN_ENCRYPTION_KEY")
        if missing:
            raise GoogleContactsConfigurationError(
                "Google Contacts is not configured. Missing: " + ", ".join(missing)
            )

    @staticmethod
    def _fernet() -> Fernet:
        GoogleContactsService._require_configured()
        try:
            return Fernet(settings.google_contacts_token_encryption_key.encode("ascii"))
        except Exception as exc:
            raise GoogleContactsConfigurationError(
                "GOOGLE_CONTACTS_TOKEN_ENCRYPTION_KEY must be a valid Fernet key."
            ) from exc

    async def _account(self) -> dict[str, Any] | None:
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(
                    """
                    SELECT id, connected_by_staff_id, google_subject, google_email, google_display_name,
                           refresh_token_encrypted, sync_token, last_synced_at,
                           created_at, updated_at
                    FROM support_google_accounts
                    WHERE id = 1
                    LIMIT 1
                    """
                )
                return await cur.fetchone()

    async def status(self) -> dict[str, Any]:
        try:
            account = await self._account()
        except GoogleContactsSchemaError:
            raise
        configured = all(
            [
                settings.google_contacts_client_id.strip(),
                settings.google_contacts_client_secret.strip(),
                settings.google_contacts_redirect_uri.strip(),
                settings.google_contacts_token_encryption_key.strip(),
            ]
        )
        if not account:
            return {"configured": configured, "connected": False, "account": None}
        return {
            "configured": configured,
            "connected": True,
            "account": {
                "email": account.get("google_email"),
                "name": account.get("google_display_name"),
                "last_synced_at": account.get("last_synced_at"),
            },
        }

    async def begin_connect(self, *, staff_id: str, session_id: int | None) -> str:
        self._require_configured()
        await self._ensure_pool()
        if session_id is None:
            raise GoogleContactsError("A valid support session is required.")
        state = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(64)
        challenge = _b64url_sha256(verifier)
        payload = json.dumps(
            {"staff_id": str(staff_id), "session_id": int(session_id), "verifier": verifier},
            separators=(",", ":"),
        )
        import redis.asyncio as redis

        client = redis.from_url(settings.redis_url, decode_responses=True)
        try:
            await client.setex(OAUTH_STATE_PREFIX + state, OAUTH_STATE_TTL_SECONDS, payload)
        finally:
            await client.aclose()

        params = {
            "client_id": settings.google_contacts_client_id,
            "redirect_uri": settings.google_contacts_redirect_uri,
            "response_type": "code",
            "scope": " ".join(GOOGLE_SCOPES),
            "access_type": "offline",
            "include_granted_scopes": "true",
            "prompt": "consent",
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        return GOOGLE_AUTH_URL + "?" + urlencode(params)

    async def _consume_state(self, state: str) -> dict[str, Any]:
        if not state or len(state) > 256:
            raise GoogleContactsProviderError("Invalid OAuth state.")
        import redis.asyncio as redis

        client = redis.from_url(settings.redis_url, decode_responses=True)
        try:
            raw = await client.getdel(OAUTH_STATE_PREFIX + state)
        finally:
            await client.aclose()
        if not raw:
            raise GoogleContactsProviderError("Google authorization expired or was already used.")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise GoogleContactsProviderError("Invalid OAuth state.") from exc
        if not payload.get("staff_id") or not payload.get("verifier"):
            raise GoogleContactsProviderError("Invalid OAuth state.")
        return payload

    async def oauth_callback(self, *, state: str, code: str | None, error: str | None) -> str:
        self._require_configured()
        state_data = await self._consume_state(state)
        if error:
            return _oauth_callback_html(ok=False, message="Google authorization was cancelled or denied.")
        if not code:
            return _oauth_callback_html(ok=False, message="Google did not return an authorization code.")

        async with httpx.AsyncClient(timeout=20.0) as client:
            token_response = await client.post(
                GOOGLE_TOKEN_URL,
                data={
                    "code": code,
                    "client_id": settings.google_contacts_client_id,
                    "client_secret": settings.google_contacts_client_secret,
                    "redirect_uri": settings.google_contacts_redirect_uri,
                    "grant_type": "authorization_code",
                    "code_verifier": state_data["verifier"],
                },
            )
            if token_response.status_code >= 400:
                logger.warning("Google OAuth token exchange failed: status=%s", token_response.status_code)
                raise GoogleContactsProviderError("Google authorization could not be completed.")
            token_data = token_response.json()
            access_token = str(token_data.get("access_token") or "")
            refresh_token = str(token_data.get("refresh_token") or "")
            if not access_token:
                raise GoogleContactsProviderError("Google did not return an access token.")

            user_response = await client.get(
                GOOGLE_USERINFO_URL,
                headers={"Authorization": f"Bearer {access_token}"},
            )
            if user_response.status_code >= 400:
                raise GoogleContactsProviderError("Google account information could not be loaded.")
            profile = user_response.json()

        staff_id = str(state_data["staff_id"])
        session = await staff_repository.get_active_session_by_id(int(state_data.get("session_id") or 0))
        if (
            not session
            or str(session.get("staff_id")) != staff_id
            or str(session.get("role") or "") not in {"supervisor", "admin"}
        ):
            raise GoogleContactsProviderError(
                "The support session that started Google authorization is no longer authorized."
            )

        existing = await self._account()
        if not refresh_token and existing:
            try:
                refresh_token = self._fernet().decrypt(
                    str(existing.get("refresh_token_encrypted") or "").encode("ascii")
                ).decode("utf-8")
            except (InvalidToken, ValueError, TypeError):
                refresh_token = ""
        if not refresh_token:
            raise GoogleContactsProviderError(
                "Google did not provide offline access. Disconnect the Google account and connect it again."
            )

        encrypted = self._fernet().encrypt(refresh_token.encode("utf-8")).decode("ascii")
        await self._upsert_account(
            staff_id=staff_id,
            google_subject=str(profile.get("sub") or profile.get("email") or ""),
            google_email=str(profile.get("email") or ""),
            google_display_name=str(profile.get("name") or "") or None,
            refresh_token_encrypted=encrypted,
        )
        try:
            result = await self.sync()
            message = f"Connected {profile.get('email') or 'Google account'} and synced {result['contact_count']} contacts."
        except Exception:
            logger.exception("Google Contacts connected but initial sync failed: staff_id=%s", staff_id)
            message = f"Connected {profile.get('email') or 'Google account'}. Use Sync contacts to retry."
        return _oauth_callback_html(ok=True, message=message)

    async def _upsert_account(
        self,
        *,
        staff_id: str,
        google_subject: str,
        google_email: str,
        google_display_name: str | None,
        refresh_token_encrypted: str,
    ) -> None:
        if not google_subject or not google_email:
            raise GoogleContactsProviderError("Google account identity was incomplete.")
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    INSERT INTO support_google_accounts (
                        id, connected_by_staff_id, google_subject, google_email, google_display_name,
                        refresh_token_encrypted, sync_token, created_at, updated_at
                    ) VALUES (1, %s, %s, %s, %s, %s, NULL, UTC_TIMESTAMP(6), UTC_TIMESTAMP(6))
                    ON DUPLICATE KEY UPDATE
                        connected_by_staff_id = VALUES(connected_by_staff_id),
                        google_subject = VALUES(google_subject),
                        google_email = VALUES(google_email),
                        google_display_name = VALUES(google_display_name),
                        refresh_token_encrypted = VALUES(refresh_token_encrypted),
                        sync_token = NULL,
                        updated_at = UTC_TIMESTAMP(6)
                    """,
                    (
                        int(staff_id),
                        google_subject[:255],
                        google_email[:320],
                        (google_display_name or "")[:191] or None,
                        refresh_token_encrypted,
                    ),
                )

    @asynccontextmanager
    async def _sync_lock(self):
        import redis.asyncio as redis

        token = secrets.token_urlsafe(24)
        client = redis.from_url(settings.redis_url, decode_responses=True)
        acquired = False
        try:
            acquired = bool(await client.set(SYNC_LOCK_KEY, token, nx=True, ex=SYNC_LOCK_TTL_SECONDS))
            if not acquired:
                raise GoogleContactsError("A Google Contacts sync is already running.")
            yield
        finally:
            if acquired:
                try:
                    await client.eval(
                        "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) else return 0 end",
                        1,
                        SYNC_LOCK_KEY,
                        token,
                    )
                except Exception:
                    logger.warning("Could not release Google Contacts sync lock cleanly")
            await client.aclose()

    async def _access_token(self, account: dict[str, Any]) -> str:
        try:
            refresh_token = self._fernet().decrypt(
                str(account.get("refresh_token_encrypted") or "").encode("ascii")
            ).decode("utf-8")
        except (InvalidToken, ValueError, TypeError) as exc:
            raise GoogleContactsConfigurationError(
                "Stored Google credentials cannot be decrypted. Verify GOOGLE_CONTACTS_TOKEN_ENCRYPTION_KEY."
            ) from exc
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.post(
                GOOGLE_TOKEN_URL,
                data={
                    "client_id": settings.google_contacts_client_id,
                    "client_secret": settings.google_contacts_client_secret,
                    "refresh_token": refresh_token,
                    "grant_type": "refresh_token",
                },
            )
        if response.status_code >= 400:
            logger.warning("Google token refresh failed: status=%s", response.status_code)
            raise GoogleContactsProviderError(
                "Google access expired or was revoked. Reconnect the Google account."
            )
        access_token = str(response.json().get("access_token") or "")
        if not access_token:
            raise GoogleContactsProviderError("Google did not return an access token.")
        return access_token

    @staticmethod
    def _contact_rows(person: dict[str, Any]) -> list[dict[str, Any]]:
        if (person.get("metadata") or {}).get("deleted"):
            return []
        names = person.get("names") or []
        display_name = ""
        if names:
            display_name = str(names[0].get("displayName") or "").strip()
        emails = person.get("emailAddresses") or []
        email = str(emails[0].get("value") or "").strip() if emails else ""
        rows = []
        seen: set[str] = set()
        for phone in person.get("phoneNumbers") or []:
            normalized = normalize_phone(str(phone.get("canonicalForm") or phone.get("value") or ""))
            if len(normalized) < 8 or normalized in seen:
                continue
            seen.add(normalized)
            rows.append(
                {
                    "resource_name": str(person.get("resourceName") or "")[:255],
                    "normalized_phone": normalized[:32],
                    "display_name": (display_name or str(phone.get("value") or normalized))[:191],
                    "email": email[:320] or None,
                    "phone_label": str(phone.get("formattedType") or phone.get("type") or "")[:64] or None,
                    "etag": str(person.get("etag") or "")[:255] or None,
                }
            )
        return rows

    async def _fetch_people(self, access_token: str, sync_token: str | None) -> tuple[list[dict[str, Any]], str | None, bool]:
        async def run(token: str | None) -> tuple[list[dict[str, Any]], str | None]:
            page_token: str | None = None
            people: list[dict[str, Any]] = []
            next_sync: str | None = None
            async with httpx.AsyncClient(timeout=30.0) as client:
                while True:
                    params: dict[str, Any] = {
                        "pageSize": 500,
                        "personFields": PERSON_FIELDS,
                        "sortOrder": "LAST_MODIFIED_ASCENDING",
                    }
                    if token:
                        params["syncToken"] = token
                    else:
                        params["requestSyncToken"] = "true"
                    if page_token:
                        params["pageToken"] = page_token
                    response = await client.get(
                        GOOGLE_PEOPLE_CONNECTIONS_URL,
                        params=params,
                        headers={"Authorization": f"Bearer {access_token}"},
                    )
                    if response.status_code == 410 and token:
                        raise RuntimeError("SYNC_TOKEN_EXPIRED")
                    if response.status_code >= 400:
                        logger.warning("Google People API failed: status=%s", response.status_code)
                        raise GoogleContactsProviderError("Google contacts could not be loaded.")
                    data = response.json()
                    people.extend(data.get("connections") or [])
                    page_token = data.get("nextPageToken")
                    if not page_token:
                        next_sync = data.get("nextSyncToken")
                        break
            return people, next_sync

        if sync_token:
            try:
                people, next_sync = await run(sync_token)
                return people, next_sync, False
            except RuntimeError as exc:
                if str(exc) != "SYNC_TOKEN_EXPIRED":
                    raise
        people, next_sync = await run(None)
        return people, next_sync, True

    async def sync(self) -> dict[str, Any]:
        async with self._sync_lock():
            return await self._sync_unlocked()

    async def _sync_unlocked(self) -> dict[str, Any]:
        self._require_configured()
        account = await self._account()
        if not account:
            raise GoogleContactsNotConnectedError("Connect a Google account first.")
        access_token = await self._access_token(account)
        people, next_sync, full_sync = await self._fetch_people(
            access_token, str(account.get("sync_token") or "") or None
        )
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            await conn.begin()
            try:
                async with conn.cursor(aiomysql.DictCursor) as cur:
                    account_id = int(account["id"])
                    if full_sync:
                        await cur.execute(
                            "DELETE FROM support_google_contacts WHERE account_id = %s",
                            (account_id,),
                        )
                    for person in people:
                        resource_name = str(person.get("resourceName") or "")[:255]
                        previous_names = [str(v)[:255] for v in ((person.get("metadata") or {}).get("previousResourceNames") or [])]
                        if resource_name:
                            await cur.execute(
                                "DELETE FROM support_google_contacts WHERE account_id = %s AND resource_name = %s",
                                (account_id, resource_name),
                            )
                        for previous in previous_names:
                            await cur.execute(
                                "DELETE FROM support_google_contacts WHERE account_id = %s AND resource_name = %s",
                                (account_id, previous),
                            )
                        for row in self._contact_rows(person):
                            await cur.execute(
                                """
                                INSERT INTO support_google_contacts (
                                    account_id, resource_name, normalized_phone, display_name,
                                    email, phone_label, etag, created_at, updated_at
                                ) VALUES (%s, %s, %s, %s, %s, %s, %s, UTC_TIMESTAMP(6), UTC_TIMESTAMP(6))
                                ON DUPLICATE KEY UPDATE
                                    display_name = VALUES(display_name), email = VALUES(email),
                                    phone_label = VALUES(phone_label), etag = VALUES(etag),
                                    updated_at = UTC_TIMESTAMP(6)
                                """,
                                (
                                    account_id,
                                    row["resource_name"],
                                    row["normalized_phone"],
                                    row["display_name"],
                                    row["email"],
                                    row["phone_label"],
                                    row["etag"],
                                ),
                            )
                    await cur.execute(
                        """
                        UPDATE support_google_accounts
                        SET sync_token = %s, last_synced_at = UTC_TIMESTAMP(6), updated_at = UTC_TIMESTAMP(6)
                        WHERE id = %s
                        """,
                        (next_sync, account_id),
                    )
                await conn.commit()
            except Exception:
                await conn.rollback()
                raise

        count = await self.count_contacts()
        await publish_support_event("contacts_synced", contact_count=count)
        return {"contact_count": count, "full_sync": full_sync, "synced_at": datetime.utcnow().isoformat() + "Z"}

    async def disconnect(self) -> None:
        async with self._sync_lock():
            await self._disconnect_unlocked()

    async def _disconnect_unlocked(self) -> None:
        account = await self._account()
        if not account:
            return
        try:
            refresh_token = self._fernet().decrypt(
                str(account.get("refresh_token_encrypted") or "").encode("ascii")
            ).decode("utf-8")
            async with httpx.AsyncClient(timeout=10.0) as client:
                await client.post(GOOGLE_REVOKE_URL, data={"token": refresh_token})
        except Exception:
            logger.info("Google token revoke skipped/failed during disconnect")
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("DELETE FROM support_google_accounts WHERE id = 1")
        await publish_support_event("contacts_synced", contact_count=await self.count_contacts())

    async def list_contacts(
        self,
        *,
        query: str = "",
        page: int = 1,
        page_size: int = 50,
    ) -> dict[str, Any]:
        pool = await self._ensure_pool()
        query = str(query or "").strip()
        page = max(1, int(page))
        page_size = max(1, min(int(page_size), 100))
        offset = (page - 1) * page_size
        like = f"%{query}%"
        normalized_query = normalize_phone(query)
        phone_like = f"%{normalized_query}%" if normalized_query else like

        where_manual = ""
        where_google = ""
        where_vcf = ""
        params_manual: list[Any] = []
        params_google: list[Any] = []
        params_vcf: list[Any] = []
        if query:
            where_manual = "WHERE display_name LIKE %s OR normalized_phone LIKE %s"
            params_manual.extend([like, phone_like])
            where_google = "AND (gc.display_name LIKE %s OR gc.normalized_phone LIKE %s OR COALESCE(gc.email, '') LIKE %s)"
            params_google.extend([like, phone_like, like])
            where_vcf = "AND (vc.display_name LIKE %s OR vc.normalized_phone LIKE %s OR COALESCE(vc.email, '') LIKE %s)"
            params_vcf.extend([like, phone_like, like])

        union_sql = f"""
            SELECT contact_key, source, display_name, normalized_phone, email, phone_label,
                   google_account_email, updated_at
            FROM (
                SELECT CONCAT('manual:', sc.id) AS contact_key,
                       'manual' AS source,
                       sc.display_name,
                       sc.normalized_phone,
                       NULL AS email,
                       NULL AS phone_label,
                       NULL AS google_account_email,
                       sc.updated_at
                FROM support_contacts sc
                {where_manual}
                UNION ALL
                SELECT CONCAT('google:', gc.id) AS contact_key,
                       'google' AS source,
                       gc.display_name,
                       gc.normalized_phone,
                       gc.email,
                       gc.phone_label,
                       ga.google_email AS google_account_email,
                       gc.updated_at
                FROM support_google_contacts gc
                JOIN support_google_accounts ga ON ga.id = gc.account_id
                WHERE ga.id = 1
                  AND NOT EXISTS (
                      SELECT 1 FROM support_contacts sc2
                      WHERE sc2.normalized_phone = gc.normalized_phone
                  )
                {where_google}
                UNION ALL
                SELECT CONCAT('vcf:', vc.id) AS contact_key,
                       'vcf' AS source,
                       vc.display_name,
                       vc.normalized_phone,
                       vc.email,
                       vc.phone_label,
                       NULL AS google_account_email,
                       vc.updated_at
                FROM support_vcf_contacts vc
                WHERE NOT EXISTS (
                          SELECT 1 FROM support_contacts sc3
                          WHERE sc3.normalized_phone = vc.normalized_phone
                      )
                  AND NOT EXISTS (
                          SELECT 1
                          FROM support_google_contacts gc2
                          JOIN support_google_accounts ga2 ON ga2.id = gc2.account_id
                          WHERE ga2.id = 1
                            AND gc2.normalized_phone = vc.normalized_phone
                      )
                {where_vcf}
            ) combined
        """
        params = [*params_manual, *params_google, *params_vcf]
        async with pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(f"SELECT COUNT(*) AS total FROM ({union_sql}) counted", tuple(params))
                count_row = await cur.fetchone()
                total = int((count_row or {}).get("total") or 0)
                await cur.execute(
                    union_sql + " ORDER BY display_name ASC, normalized_phone ASC LIMIT %s OFFSET %s",
                    tuple([*params, page_size, offset]),
                )
                rows = list(await cur.fetchall())
        return {"items": rows, "page": page, "page_size": page_size, "total": total}

    async def count_contacts(self) -> int:
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(
                    """
                    SELECT
                        (SELECT COUNT(*) FROM support_contacts)
                        +
                        (SELECT COUNT(*)
                         FROM support_google_contacts gc
                         JOIN support_google_accounts ga ON ga.id = gc.account_id
                         WHERE ga.id = 1
                           AND NOT EXISTS (
                               SELECT 1 FROM support_contacts sc2
                               WHERE sc2.normalized_phone = gc.normalized_phone
                           ))
                        +
                        (SELECT COUNT(*)
                         FROM support_vcf_contacts vc
                         WHERE NOT EXISTS (
                                   SELECT 1 FROM support_contacts sc3
                                   WHERE sc3.normalized_phone = vc.normalized_phone
                               )
                           AND NOT EXISTS (
                                   SELECT 1
                                   FROM support_google_contacts gc2
                                   JOIN support_google_accounts ga2 ON ga2.id = gc2.account_id
                                   WHERE ga2.id = 1
                                     AND gc2.normalized_phone = vc.normalized_phone
                               )) AS total
                    """
                )
                row = await cur.fetchone()
        return int((row or {}).get("total") or 0)


google_contacts_service = GoogleContactsService()
