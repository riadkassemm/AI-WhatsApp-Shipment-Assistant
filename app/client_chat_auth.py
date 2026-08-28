"""Conversational client-auth intent detection and credential redaction.

This module only interprets customer text. It never authenticates a customer and never
changes authorization state. Credential verification remains in ``customer_auth``.

The parser is deliberately deterministic so obvious credential-bearing messages are
intercepted before they can enter normal AI context. It combines multilingual labels,
conversation-state context, Unicode decimal-digit normalization, and conservative
heuristics for unlabeled ``login <user> <password>`` forms.

STRICT TWO-STEP AUTHENTICATION: ``extract_credentials`` never returns both a username
and a password from the same inbound message, regardless of how the customer formatted
or labeled them. Login is always User ID first, then password, on two separate turns.
This is deliberate: guessing which token in a combined message is the username versus
the password is exactly the kind of format-prediction this module avoids.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
import unicodedata
from typing import Literal


AuthState = Literal[
    "guest",
    "awaiting_credentials",
    "awaiting_username",
    "awaiting_password",
    "authenticating",
    "authenticated",
    "authentication_failed",
]


# Labels are concepts, not whole fixed sentences. They intentionally cover common
# English/French/Arabic/Lebanese/Arabizi vocabulary while extraction remains based on
# label position and conversational state rather than an exact phrase list.
_USERNAME_LABEL_PATTERN = r"""
(?:
    (?:(?:l|el)\s+|(?:ال|وال)\s*)?user(?:\s*name|\s*id)?
  | userid
  | user\s+id
  | client\s+id
  | account(?:\s+(?:id|number|no))?
  | identifiant
  | utilisateur(?:\s*/\s*client)?
  | nom\s+d['’]utilisateur
  | اسم\s+المستخدم
  | رقم\s+المستخدم
  | (?:و)?(?:ال)?يوزر
  | (?:و)?(?:ال)?اكاونت
  | (?:و)?الحساب
)
"""

_PASSWORD_LABEL_PATTERN = r"""
(?:
    (?:(?:l|el)\s+|(?:ال|وال)\s*)?password
  | (?:(?:l|el)\s+|(?:ال|وال)\s*)?pass(?:word)?
  | passwd
  | pwd
  | mot\s+de\s+passe
  | كلمة\s+السر
  | كلمة\s+المرور
  | (?:و)?(?:ال)?باسوورد
  | (?:و)?(?:ال)?باسورد
  | (?:و)?(?:ال)?باس
)
"""

_USERNAME_LABEL_RE = re.compile(_USERNAME_LABEL_PATTERN, re.IGNORECASE | re.VERBOSE)
_PASSWORD_LABEL_RE = re.compile(_PASSWORD_LABEL_PATTERN, re.IGNORECASE | re.VERBOSE)
_ANY_LABEL_RE = re.compile(
    rf"(?P<username>{_USERNAME_LABEL_PATTERN})|(?P<password>{_PASSWORD_LABEL_PATTERN})",
    re.IGNORECASE | re.VERBOSE,
)

_AUTH_INTENT_RE = re.compile(
    r"(?:\blog\s*in\b|\blogin\b|\bsign\s*in\b|\bconnect(?:er|e|ez)?\b|"
    r"\bcredentials?\b|\blogin\s+details?\b|\bidentifiants?\b|"
    r"\bbade\s+fout\b|\bbaddi\s+fout\b|\bbadde\s+fout\b|\bfout\b|"
    r"(?:بدي|بدّي|اريد|أريد)\s+(?:فوت|ادخل|أدخل)|(?:تسجيل\s+الدخول|بيانات\s+الدخول|معلومات\s+الدخول|دخول))",
    re.IGNORECASE,
)

_LOGOUT_RE = re.compile(
    r"(?:\blog\s*out\b|\blogout\b|\bsign\s*out\b|\bd[eé]connect(?:er|e|ez)?\b|"
    r"\bbade\s+(?:logout|etla3)\b|\bbaddi\s+(?:logout|etla3)\b|"
    r"(?:طلعني|طلّعني|سجلني\s+خروج|تسجيل\s+الخروج|بدي\s+اطلع|بدي\s+أطلع))",
    re.IGNORECASE,
)

# Words that may naturally sit between a credential label and its actual value.
_LEADING_FILLERS = {
    "is", "est", "هو", "هي", "هوي", "هويي", "تبعي", "تبعى", "تبعِي", "taba3e", "taba3i",
    "taba3eh", "howe", "houwe", "huwe", "huwwe", "ya3ne", "يعني", "actually", "basically",
    "mon", "ma", "mes", "my", "the", "le", "la", "l", "el", "ال", "رقم", "number",
    "value", "valeur", "code", "هوّي", "هوية", "الحالي", "current",
}
_TRAILING_FILLERS = {
    "and", "w", "we", "wel", "wella", "et", "avec", "le", "la", "l", "el", "و", "وال",
    "then", "please", "pls", "plz", "svp", "لو", "سمحت", "my", "mon", "ma", "taba3e", "taba3i",
}
_NON_VALUE_AFTER_PASSWORD = {
    "to", "for", "pour", "la", "le", "l", "afin", "للتابعة", "للمتابعة", "نكمل", "نكمل.",
    "continue", "continuer", "login", "authenticate", "authentication",
}
_LOGIN_FILLERS = {
    "login", "log", "in", "signin", "sign", "credentials", "credential", "details",
    "bade", "baddi", "badde", "fout", "taba3e", "taba3i", "l", "el", "w", "wel",
    "please", "pls", "plz", "svp", "bonjour", "salut", "hello", "hi", "marhaba", "مرحبا",
    "بدي", "بدّي", "فوت", "دخول", "تسجيل", "الدخول", "بيانات", "معلومات",
}

_TOKEN_RE = re.compile(r"[^\s,;،:=]+")
_QUOTED_RE = re.compile(r"^\s*([\"'])(.*?)\1", re.DOTALL)


@dataclass(frozen=True)
class CredentialExtraction:
    is_authentication_attempt: bool
    username: str | None = None
    password: str | None = None
    username_present: bool = False
    password_present: bool = False
    ambiguous: bool = False
    secret_detected: bool = False
    redacted_text: str = ""


def normalize_decimal_digits(value: str) -> str:
    """Normalize Unicode decimal digits to ASCII without changing other characters."""
    out: list[str] = []
    for char in str(value):
        if unicodedata.category(char) == "Nd":
            try:
                out.append(str(unicodedata.digit(char)))
                continue
            except (TypeError, ValueError):
                pass
        out.append(char)
    return "".join(out)


def normalize_username(value: str) -> str:
    """Apply only safe normalization supported by the existing users.userid flow."""
    candidate = str(value).strip()
    if candidate and all(unicodedata.category(ch) == "Nd" for ch in candidate):
        return normalize_decimal_digits(candidate)
    return candidate


def normalize_password(value: str) -> str:
    """Preserve password semantics; normalize only an all-decimal-digit password.

    Existing customer credentials are compared exactly by the backend. We therefore do
    not case-fold, transliterate, normalize general Unicode, or alter spaces. Numeric
    passwords entered using Arabic-Indic/Persian decimal digits are canonicalized to
    ASCII because those characters represent the same decimal digits used by the
    existing numeric credential convention.
    """
    candidate = str(value)
    if candidate and all(unicodedata.category(ch) == "Nd" for ch in candidate):
        return normalize_decimal_digits(candidate)
    return candidate


def is_logout_intent(text: str | None) -> bool:
    return bool(_LOGOUT_RE.search(str(text or "").strip()))


def _labels(text: str) -> list[tuple[str, int, int]]:
    found: list[tuple[str, int, int]] = []
    for match in _ANY_LABEL_RE.finditer(text):
        kind = "username" if match.group("username") is not None else "password"
        found.append((kind, match.start(), match.end()))
    return found


def _strip_edges(segment: str) -> str:
    value = segment.strip(" \t\r\n:=,;،-|/")
    # Repeatedly strip natural filler words from either edge while preserving the
    # credential token itself exactly.
    for _ in range(4):
        tokens = list(_TOKEN_RE.finditer(value))
        if not tokens:
            return ""
        first = tokens[0].group(0).casefold().strip(".?!")
        if first in _LEADING_FILLERS:
            value = value[tokens[0].end():].lstrip(" \t:=,;،-|")
            continue
        break
    for _ in range(4):
        tokens = list(_TOKEN_RE.finditer(value))
        if not tokens:
            return ""
        last = tokens[-1].group(0).casefold().strip(".?!")
        if last in _TRAILING_FILLERS:
            value = value[:tokens[-1].start()].rstrip(" \t:=,;،-|")
            continue
        break
    return value.strip()


def _candidate_tokens(segment: str) -> list[str]:
    value = _strip_edges(segment)
    if not value:
        return []
    quoted = _QUOTED_RE.match(value)
    if quoted:
        remainder = value[quoted.end():].strip()
        if not remainder or remainder.casefold().strip(".?!") in _TRAILING_FILLERS:
            return [quoted.group(2)]
    candidates: list[str] = []
    for match in _TOKEN_RE.finditer(value):
        token = match.group(0).strip(".?!()[]{}\"'|/\\")
        if not token or not any(ch.isalnum() for ch in token):
            continue
        folded = token.casefold()
        if folded in _LEADING_FILLERS or folded in _TRAILING_FILLERS:
            continue
        candidates.append(token)
    return candidates


def _prefer_credential_shaped_tokens(candidates: list[str]) -> list[str]:
    """Drop surrounding prose when one obvious credential token is present.

    Natural messages often contain extra words between a label and value, e.g.
    ``my username is actually 55501 and my password is 987654``. We do not guess
    when multiple credential-looking values are present; that remains ambiguous.
    """
    if len(candidates) <= 1:
        return candidates

    with_digits = [token for token in candidates if any(ch.isdigit() for ch in token)]
    if len(with_digits) == 1:
        return with_digits
    if len(with_digits) > 1:
        return with_digits
    return candidates


def _extract_labeled_values(text: str) -> tuple[list[str], list[str], bool, bool]:
    labels = _labels(text)
    usernames: list[str] = []
    passwords: list[str] = []
    saw_username = False
    saw_password = False

    for index, (kind, _start, end) in enumerate(labels):
        next_start = labels[index + 1][1] if index + 1 < len(labels) else len(text)
        segment = text[end:next_start]
        candidates = _prefer_credential_shaped_tokens(_candidate_tokens(segment))
        if kind == "username":
            saw_username = True
            usernames.extend(candidates)
        else:
            saw_password = True
            passwords.extend(candidates)

    return usernames, passwords, saw_username, saw_password


def _mask_exact_secret(text: str, raw_secret: str | None) -> str:
    if not raw_secret:
        return text
    idx = text.rfind(raw_secret)
    if idx < 0:
        return text
    return f"{text[:idx]}********{text[idx + len(raw_secret):]}"


def redact_sensitive_text(text: str | None) -> str:
    """Redact labeled password values without treating ordinary password prompts as secrets.

    This is intentionally reusable at persistence, staff-API, logging-context, and AI
    context boundaries. It also protects historical messages written before this
    conversational-auth implementation.
    """
    value = str(text or "")
    labels = _labels(value)
    if not labels:
        return value

    replacements: list[tuple[int, int, str]] = []
    for index, (kind, _start, end) in enumerate(labels):
        if kind != "password":
            continue
        next_start = labels[index + 1][1] if index + 1 < len(labels) else len(value)
        segment = value[end:next_start]
        stripped = segment.lstrip(" \t:=")
        if not stripped:
            continue
        # Ordinary prompts such as "send your password to continue" contain no
        # credential value and must remain readable.
        first_match = _TOKEN_RE.search(stripped)
        if not first_match:
            continue
        first = first_match.group(0).casefold().strip(".?!")
        if first in _NON_VALUE_AFTER_PASSWORD:
            continue

        # Stop at a sentence/list delimiter where possible; otherwise redact the
        # whole value segment so multi-token secrets cannot leak.
        relative_end = len(segment)
        delimiter = re.search(r"[,;،\n\r]", segment)
        if delimiter:
            relative_end = delimiter.start()
        secret_segment = segment[:relative_end]
        leading_len = len(secret_segment) - len(secret_segment.lstrip(" \t:="))
        secret_start = end + leading_len
        secret_end = end + relative_end
        if secret_end > secret_start:
            replacement = "********"
            if secret_segment and secret_segment[-1].isspace():
                replacement += " "
            replacements.append((secret_start, secret_end, replacement))

    redacted = value
    for start, end, replacement in reversed(replacements):
        redacted = redacted[:start] + replacement + redacted[end:]
    return redacted


def extract_credentials(
    text: str | None,
    *,
    auth_state: AuthState = "guest",
    pending_username: str | None = None,
    has_pending_password: bool = False,
) -> CredentialExtraction:
    """Detect authentication intent and extract at most one credential value.

    A single inbound message can yield a username OR a password, never both — see the
    module-level STRICT TWO-STEP AUTHENTICATION note. When values are ambiguous,
    ``ambiguous`` is returned instead of trying multiple combinations. In active
    partial-auth states, an unlabeled next message can represent exactly the missing
    credential.
    """
    raw = str(text or "")
    stripped = raw.strip()
    if not stripped:
        return CredentialExtraction(False, redacted_text=raw)

    usernames, passwords, saw_username, saw_password = _extract_labeled_values(raw)
    auth_hint = bool(_AUTH_INTENT_RE.search(raw))
    state_is_auth = auth_state in {
        "awaiting_credentials",
        "awaiting_username",
        "awaiting_password",
        "authentication_failed",
    }

    raw_username: str | None = None
    raw_password: str | None = None
    ambiguous = len(usernames) > 1 or len(passwords) > 1

    if len(usernames) == 1:
        raw_username = usernames[0]
    if len(passwords) == 1:
        raw_password = passwords[0]

    # Contextual single-field messages. A password may intentionally contain spaces,
    # so when the backend explicitly awaits it, preserve the whole message exactly.
    if not saw_username and not saw_password:
        if auth_state == "awaiting_password" and pending_username:
            raw_password = stripped
            saw_password = True
        elif auth_state == "awaiting_username" and has_pending_password:
            tokens = _candidate_tokens(stripped)
            if len(tokens) == 1:
                raw_username = tokens[0]
                saw_username = True
            else:
                ambiguous = True
        elif auth_hint or auth_state in {
            "awaiting_credentials",
            "awaiting_username",
            "authentication_failed",
        }:
            candidates = [
                token for token in _candidate_tokens(stripped)
                if token.casefold() not in _LOGIN_FILLERS
            ]
            # A single bare token is accepted as the User ID whenever we reached this
            # branch — either because the message itself signaled login intent, or
            # because we are explicitly in a waiting-for-credentials state (i.e. the
            # bot just asked "Please provide your User ID." and this is simply the
            # customer's answer to that question; no extra keyword should be required).
            if len(candidates) == 1:
                raw_username = candidates[0]
                saw_username = True
            elif len(candidates) > 1:
                # Do not guess a User ID/password pairing from an unlabeled multi-token
                # reply (see STRICT TWO-STEP AUTHENTICATION above) — ask for
                # clarification instead of predicting the format.
                ambiguous = True

    is_authentication_attempt = bool(
        saw_username
        or saw_password
        or auth_hint
        or (state_is_auth and (pending_username or has_pending_password))
    )

    username = normalize_username(raw_username) if raw_username is not None else None
    password = normalize_password(raw_password) if raw_password is not None else None

    # STRICT SEQUENTIAL AUTHENTICATION: never accept a User ID and a password from the
    # same inbound message, no matter how the customer wrote or labeled them. The
    # chatbot must ask for the User ID first, wait for a reply, then ask for the
    # password separately. If a single message appears to contain both, only the User
    # ID is honored here; the apparent password value is still masked out of stored/
    # logged text below (using raw_password), but it is discarded rather than used to
    # authenticate, and the customer is asked for the password on the next turn instead
    # of us guessing which token they intended as which credential.
    if username is not None and password is not None:
        password = None

    redacted = redact_sensitive_text(raw)
    secret_detected = raw_password is not None or saw_password
    if raw_password is not None:
        # Handles unlabeled/password-only forms that the generic label sanitizer cannot
        # identify. For a password-only turn the full secret may contain spaces.
        if auth_state == "awaiting_password" and not _PASSWORD_LABEL_RE.search(raw):
            redacted = "********"
        else:
            redacted = _mask_exact_secret(redacted, raw_password)
            if raw_password in redacted:
                redacted = _mask_exact_secret(raw, raw_password)

    return CredentialExtraction(
        is_authentication_attempt=is_authentication_attempt,
        username=username,
        password=password,
        username_present=username is not None,
        password_present=password is not None,
        ambiguous=ambiguous,
        secret_detected=secret_detected,
        redacted_text=redacted,
    )
