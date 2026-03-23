"""General-purpose utility functions for the Customer Data Platform."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import uuid as _uuid
from datetime import datetime, timezone
from typing import Any

from cryptography.fernet import Fernet

# ---------------------------------------------------------------------------
# ID helpers
# ---------------------------------------------------------------------------


def generate_uuid() -> str:
    """Return a new UUID4 as a string."""
    return str(_uuid.uuid4())


# ---------------------------------------------------------------------------
# PII helpers
# ---------------------------------------------------------------------------

_EMAIL_RE = re.compile(
    r"^[a-zA-Z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-zA-Z0-9-]+(?:\.[a-zA-Z0-9-]+)*$"
)
_PHONE_RE = re.compile(r"^\+?[1-9]\d{1,14}$")  # E.164-ish


def hash_email(email: str) -> str:
    """Return a SHA-256 hex digest of the lower-cased, stripped email."""
    normalised = email.strip().lower()
    return hashlib.sha256(normalised.encode()).hexdigest()


def mask_pii(data: dict[str, Any], fields: tuple[str, ...] = ("email", "phone", "ssn")) -> dict[str, Any]:
    """Return a shallow copy of *data* with sensitive fields masked.

    Only top-level keys whose names appear in *fields* are masked.
    """
    masked = dict(data)
    for field in fields:
        if field in masked and masked[field]:
            value = str(masked[field])
            if len(value) <= 4:
                masked[field] = "****"
            else:
                masked[field] = value[:2] + "*" * (len(value) - 4) + value[-2:]
    return masked


def validate_email(email: str) -> bool:
    """Return ``True`` when *email* looks like a valid email address."""
    return bool(_EMAIL_RE.match(email.strip()))


def validate_phone(phone: str) -> bool:
    """Return ``True`` when *phone* matches a basic E.164 pattern."""
    return bool(_PHONE_RE.match(phone.strip()))


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------


def paginate(query: Any, page: int = 1, size: int = 20) -> Any:
    """Apply LIMIT/OFFSET pagination to a SQLAlchemy *query*.

    Parameters
    ----------
    query:
        A SQLAlchemy ``Select`` statement.
    page:
        1-based page number.
    size:
        Number of rows per page (capped at 100).
    """
    page = max(1, page)
    size = max(1, min(size, 100))
    return query.offset((page - 1) * size).limit(size)


# ---------------------------------------------------------------------------
# Date / time helpers
# ---------------------------------------------------------------------------


def to_utc(dt: datetime) -> datetime:
    """Convert a datetime to UTC.  Naive datetimes are assumed UTC."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def format_datetime(dt: datetime, fmt: str = "%Y-%m-%dT%H:%M:%SZ") -> str:
    """Format a datetime as an ISO-8601 string in UTC."""
    return to_utc(dt).strftime(fmt)


def date_range(
    start: datetime, end: datetime, freq_days: int = 1
) -> list[datetime]:
    """Return a list of datetimes from *start* to *end* (inclusive) stepped by *freq_days*."""
    from datetime import timedelta

    dates: list[datetime] = []
    current = to_utc(start)
    end_utc = to_utc(end)
    step = timedelta(days=freq_days)
    while current <= end_utc:
        dates.append(current)
        current += step
    return dates


# ---------------------------------------------------------------------------
# JSON helpers
# ---------------------------------------------------------------------------


def safe_json_loads(s: str | bytes | None, default: Any = None) -> Any:
    """Parse a JSON string, returning *default* on failure instead of raising."""
    if s is None:
        return default
    try:
        return json.loads(s)
    except (json.JSONDecodeError, TypeError):
        return default


def deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge *override* into *base*, returning a new dict.

    For conflicting non-dict values the *override* wins.
    """
    merged = dict(base)
    for key, value in override.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


# ---------------------------------------------------------------------------
# Encryption helpers (Fernet symmetric encryption)
# ---------------------------------------------------------------------------


def _derive_fernet_key(key: str) -> bytes:
    """Derive a URL-safe 32-byte key from an arbitrary string."""
    digest = hashlib.sha256(key.encode()).digest()
    return base64.urlsafe_b64encode(digest)


def encrypt_field(value: str, key: str) -> str:
    """Encrypt *value* using Fernet with the given *key* string.

    Returns a URL-safe base64-encoded ciphertext.
    """
    fernet = Fernet(_derive_fernet_key(key))
    return fernet.encrypt(value.encode()).decode()


def decrypt_field(token: str, key: str) -> str:
    """Decrypt a Fernet *token* that was encrypted with *key*."""
    fernet = Fernet(_derive_fernet_key(key))
    return fernet.decrypt(token.encode()).decode()
