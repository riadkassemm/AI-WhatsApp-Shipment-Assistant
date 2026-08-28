"""
Webhook authenticity check + a minimal per-sender rate limiter.

Both of these were missing from the initial scaffold and are called out
explicitly in the integration proposal's security section.
"""
import hashlib
import hmac
import time
from collections import defaultdict, deque

from app.config import settings


def verify_meta_signature(raw_body: bytes, signature_header: str | None) -> bool:
    """
    Meta signs every webhook POST with your app secret and sends the result
    in the X-Hub-Signature-256 header as `sha256=<hex digest>`.

    Recomputing and comparing this is the only way to be sure a request
    actually came from Meta and not from someone who guessed/found your
    webhook URL and is POSTing crafted payloads (fake tracking numbers,
    fake balance-check triggers, etc).
    """
    if not signature_header or not signature_header.startswith("sha256="):
        return False

    expected = hmac.new(
        key=settings.whatsapp_app_secret.encode("utf-8"),
        msg=raw_body,
        digestmod=hashlib.sha256,
    ).hexdigest()
    provided = signature_header.removeprefix("sha256=")

    # constant-time comparison to avoid timing attacks
    return hmac.compare_digest(expected, provided)


def safe_log_identifier(value: str, prefix: str = "id") -> str:
    """Return a stable non-reversible identifier suitable for application logs."""
    digest = hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()
    return f"{prefix}_{digest[:12]}"


class SlidingWindowRateLimiter:
    """In-memory rate limiter. Fine for a single process; move to Redis
    (INCR + EXPIRE, or a token-bucket) once you scale past one worker."""

    def __init__(self, max_events: int, window_seconds: int) -> None:
        self._max_events = max_events
        self._window_seconds = window_seconds
        self._hits: dict[str, deque] = defaultdict(deque)

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        window = self._hits[key]
        while window and now - window[0] > self._window_seconds:
            window.popleft()
        if len(window) >= self._max_events:
            return False
        window.append(now)
        return True


rate_limiter = SlidingWindowRateLimiter(
    max_events=settings.rate_limit_max_messages,
    window_seconds=settings.rate_limit_window_seconds,
)

client_auth_rate_limiter = SlidingWindowRateLimiter(
    max_events=settings.client_auth_rate_limit_max_attempts,
    window_seconds=settings.client_auth_rate_limit_window_seconds,
)
