"""
Customer authentication for the WhatsApp shipment assistant.

Authentication model:

    customer provides userid + password
            |
            v
    backend queries users table
            |
            v
    users.userid == supplied userid
    users.password == supplied password
            |
       +----+----+
       |         |
     MATCH     NO MATCH
       |         |
       v         v
 authenticated  rejected

The LLM is NOT involved in authentication decisions.

Passwords are handled only by the backend and are never stored in
conversation state or sent to OpenAI.
"""

from __future__ import annotations

from dataclasses import dataclass
import asyncio
import logging

import bcrypt

import aiomysql

from app.config import settings
from app.security import safe_log_identifier


logger = logging.getLogger("shipment-bot")

class CustomerAuthenticationUnavailable(RuntimeError):
    """Raised when the credential store cannot be checked reliably.

    Invalid credentials still return ``None``. Infrastructure failures use this
    exception so callers do not misreport an outage as a bad username/password.
    """


@dataclass(frozen=True)
class AuthenticatedCustomer:
    """
    Represents a customer whose userid and password were successfully
    verified against the users table.
    """

    customer_id: str
    userid: str
    name: str | None = None


async def _get_db_connection():
    """
    Create a MariaDB connection using application configuration.
    """

    return await aiomysql.connect(
        host=settings.db_host,
        port=settings.db_port,
        user=settings.db_user,
        password=settings.db_password,
        db=settings.db_name,
        autocommit=True,
        charset="utf8mb4",
    )


async def authenticate_customer(
    userid: str,
    password: str,
) -> AuthenticatedCustomer | None:
    """
    Authenticate a customer using:

        users.userid
        users.password

    The password is compared by the backend.

    IMPORTANT:
    The password is never returned, logged, stored in Conversation state,
    or sent to OpenAI.

    Returns None if authentication fails.
    """

    userid = str(userid).strip()

    if not userid:
        logger.info("Customer authentication rejected: missing credentials")
        return None

    if not password:
        logger.info("Customer authentication rejected: missing credentials")
        return None

    connection = None

    try:
        connection = await _get_db_connection()

        async with connection.cursor(aiomysql.DictCursor) as cursor:

            await cursor.execute(
                """
                SELECT
                    id,
                    userid,
                    password,
                    name,
                    status
                FROM users
                WHERE userid = %s
                LIMIT 1
                """,
                (userid,),
            )

            user = await cursor.fetchone()

        if not user:
            logger.info(
                "Customer authentication rejected: user_ref=%s",
                safe_log_identifier(userid, "user"),
            )
            return None

        # Respect disabled accounts.
        status = str(
            user.get("status") or "1"
        ).strip()

        if status not in (
            "1",
            "active",
            "ACTIVE",
        ):
            logger.info(
                "Customer authentication rejected: user_ref=%s",
                safe_log_identifier(userid, "user"),
            )
            return None

        stored_password = user.get("password")

        if stored_password is None:
            logger.info(
                "Customer authentication rejected: user_ref=%s",
                safe_log_identifier(userid, "user"),
            )
            return None

        stored_password = str(stored_password)

        
        # ---------------------------------------------------------
        # PASSWORD VERIFICATION
        # ---------------------------------------------------------
        #
        # The database currently contains both:
        #
        #   1. Plaintext passwords
        #   2. bcrypt hashes
        #
        # Detect bcrypt by its standard prefix and verify accordingly.
        #
        # The supplied plaintext password is never logged or stored.
        # ---------------------------------------------------------

        if stored_password.startswith(("$2a$", "$2b$", "$2y$")):
            bcrypt_hash = stored_password

            # Python bcrypt implementations commonly use $2b$.
            # PHP's password_hash() commonly produces $2y$.
            # $2y$ and $2b$ use the same bcrypt algorithm.
            if bcrypt_hash.startswith("$2y$"):
                bcrypt_hash = "$2b$" + bcrypt_hash[4:]

            try:
                password_matches = bcrypt.checkpw(
                    password.encode("utf-8"),
                    bcrypt_hash.encode("utf-8"),
                )

            except (ValueError, TypeError):
                logger.warning("Customer authentication failed: invalid stored bcrypt hash")
                return None

            if not password_matches:
                logger.info(
                    "Customer authentication rejected: user_ref=%s",
                    safe_log_identifier(userid, "user"),
                )
                return None
        else:
            # Backwards-compatible plaintext account. On successful login, migrate
            # that row to bcrypt immediately so plaintext storage disappears over
            # time without changing the existing customer login flow.
            if password != stored_password:
                logger.info(
                    "Customer authentication rejected: user_ref=%s",
                    safe_log_identifier(userid, "user"),
                )
                return None
            try:
                migrated_hash = await asyncio.to_thread(
                    bcrypt.hashpw,
                    password.encode("utf-8"),
                    bcrypt.gensalt(rounds=12),
                )
                async with connection.cursor() as cursor:
                    await cursor.execute(
                        "UPDATE users SET password = %s WHERE id = %s AND password = %s",
                        (migrated_hash.decode("utf-8"), user["id"], stored_password),
                    )
            except Exception:
                # Do not attach a DB exception traceback here: some drivers can include
                # query parameter representations, and this UPDATE contains plaintext
                # legacy credentials. Login remains compatible while operators still get
                # a remediation signal.
                logger.error("Could not migrate legacy customer password to bcrypt")

        customer = AuthenticatedCustomer(
            customer_id=str(user["id"]),
            userid=str(user["userid"]),
            name=user.get("name"),
        )

        logger.info(
            "Customer authenticated successfully: user_ref=%s",
            safe_log_identifier(userid, "user"),
        )

        return customer

    except CustomerAuthenticationUnavailable:
        raise
    except Exception:
        # Authentication failures must never put credential-bearing DB exception
        # details into logs or error traces. Do not collapse an infrastructure
        # problem into "invalid credentials"; the chat layer can now respond
        # with a temporary-service message without exposing backend details.
        logger.error("Database error while authenticating customer")
        raise CustomerAuthenticationUnavailable(
            "Customer credential verification is temporarily unavailable"
        ) from None

    finally:
        if connection is not None:
            connection.close()


async def resolve_customer(
    userid: str | None = None,
    password: str | None = None,
) -> AuthenticatedCustomer | None:
    """
    Compatibility wrapper.

    Authentication requires both userid and password.
    """

    if not userid or not password:
        return None

    return await authenticate_customer(
        userid=userid,
        password=password,
    )