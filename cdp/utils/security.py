"""Security utilities: JWT, password hashing, API keys, rate limiting, and input sanitisation."""

from __future__ import annotations

import functools
import hashlib
import html
import re
import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from jose import JWTError, jwt
from passlib.context import CryptContext

from cdp.config import settings

import logging

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Password hashing (bcrypt via passlib)
# ---------------------------------------------------------------------------

_pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(plain: str) -> str:
    """Return the bcrypt hash of *plain*."""
    return _pwd_context.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    """Return ``True`` if *plain* matches the bcrypt *hashed* value."""
    return _pwd_context.verify(plain, hashed)


# ---------------------------------------------------------------------------
# JWT helpers
# ---------------------------------------------------------------------------


def create_access_token(
    data: dict[str, Any],
    expires_delta: timedelta | None = None,
) -> str:
    """Create a signed JWT access token.

    Parameters
    ----------
    data:
        Claims to embed in the token (``sub`` is conventional for user id).
    expires_delta:
        Custom expiry.  Defaults to ``settings.ACCESS_TOKEN_EXPIRE_MINUTES``.
    """
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + (
        expires_delta
        if expires_delta is not None
        else timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    )
    to_encode.update({"exp": expire, "iat": datetime.now(timezone.utc)})
    return jwt.encode(to_encode, settings.SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def decode_access_token(token: str) -> dict[str, Any] | None:
    """Decode and verify a JWT token.

    Returns the payload dict on success, or ``None`` if the token is
    invalid or expired.
    """
    try:
        payload = jwt.decode(
            token,
            settings.SECRET_KEY,
            algorithms=[settings.JWT_ALGORITHM],
        )
        return payload
    except JWTError:
        logger.warning("JWT decode failed")
        return None


def create_refresh_token(
    data: dict[str, Any],
    expires_days: int = 30,
) -> str:
    """Create a long-lived refresh token."""
    return create_access_token(data, expires_delta=timedelta(days=expires_days))


# ---------------------------------------------------------------------------
# API key generation / validation
# ---------------------------------------------------------------------------

_API_KEY_PREFIX = "cdp_"


def generate_api_key() -> tuple[str, str]:
    """Generate an API key and its hash.

    Returns
    -------
    tuple[str, str]
        ``(plain_key, hashed_key)`` -- store the hash, give the plain key
        to the user once.
    """
    raw = secrets.token_urlsafe(48)
    plain = f"{_API_KEY_PREFIX}{raw}"
    hashed = hashlib.sha256(plain.encode()).hexdigest()
    return plain, hashed


def validate_api_key(plain_key: str, stored_hash: str) -> bool:
    """Return ``True`` when the SHA-256 of *plain_key* matches *stored_hash*."""
    return hashlib.sha256(plain_key.encode()).hexdigest() == stored_hash


# ---------------------------------------------------------------------------
# Rate limiting (in-memory, per-process -- use Redis for distributed)
# ---------------------------------------------------------------------------

_rate_limit_store: dict[str, list[float]] = {}


def rate_limit(
    max_calls: int = 60,
    period_seconds: int = 60,
    key_func: Callable[..., str] | None = None,
) -> Callable:
    """Decorator that enforces a simple in-memory rate limit.

    Parameters
    ----------
    max_calls:
        Maximum allowed calls within *period_seconds*.
    period_seconds:
        Sliding window duration in seconds.
    key_func:
        Optional callable that receives the same args as the wrapped
        function and returns a string key.  When ``None`` the function
        name is used (global limit).

    Raises
    ------
    RuntimeError
        When the rate limit is exceeded.
    """

    def decorator(fn: Callable) -> Callable:
        @functools.wraps(fn)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            _check_rate(fn, args, kwargs, max_calls, period_seconds, key_func)
            return await fn(*args, **kwargs)

        @functools.wraps(fn)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            _check_rate(fn, args, kwargs, max_calls, period_seconds, key_func)
            return fn(*args, **kwargs)

        import asyncio

        return async_wrapper if asyncio.iscoroutinefunction(fn) else sync_wrapper

    return decorator


def _check_rate(
    fn: Callable,
    args: tuple,
    kwargs: dict,
    max_calls: int,
    period_seconds: int,
    key_func: Callable[..., str] | None,
) -> None:
    key = key_func(*args, **kwargs) if key_func else fn.__qualname__
    now = time.monotonic()
    window = _rate_limit_store.setdefault(key, [])
    # Prune expired entries
    cutoff = now - period_seconds
    _rate_limit_store[key] = [t for t in window if t > cutoff]
    window = _rate_limit_store[key]
    if len(window) >= max_calls:
        raise RuntimeError(
            f"Rate limit exceeded: {max_calls} calls per {period_seconds}s for '{key}'"
        )
    window.append(now)


# ---------------------------------------------------------------------------
# CORS configuration helper
# ---------------------------------------------------------------------------


def get_cors_config(
    allowed_origins: list[str] | None = None,
) -> dict[str, Any]:
    """Return a dict suitable for ``CORSMiddleware`` kwargs.

    Parameters
    ----------
    allowed_origins:
        Explicit origin list.  Defaults to ``["*"]`` in debug mode,
        otherwise an empty list (you must set origins in production).
    """
    if allowed_origins is None:
        allowed_origins = ["*"] if settings.DEBUG else []

    return {
        "allow_origins": allowed_origins,
        "allow_credentials": True,
        "allow_methods": ["*"],
        "allow_headers": ["*"],
        "expose_headers": ["X-Request-Id", "X-RateLimit-Remaining"],
    }


# ---------------------------------------------------------------------------
# Input sanitisation
# ---------------------------------------------------------------------------

# Tags that are never allowed even after sanitisation
_DANGEROUS_PATTERN = re.compile(
    r"<\s*\/?\s*(script|iframe|object|embed|form|input|svg|math|link|style)\b",
    re.IGNORECASE,
)


def sanitise_string(value: str) -> str:
    """Strip dangerous HTML/script content from *value*.

    Applies HTML entity escaping and removes known-dangerous tags.
    """
    cleaned = html.escape(value, quote=True)
    cleaned = _DANGEROUS_PATTERN.sub("", cleaned)
    return cleaned.strip()


def sanitise_dict(data: dict[str, Any]) -> dict[str, Any]:
    """Recursively sanitise all string values in a dictionary."""
    sanitised: dict[str, Any] = {}
    for key, value in data.items():
        if isinstance(value, str):
            sanitised[key] = sanitise_string(value)
        elif isinstance(value, dict):
            sanitised[key] = sanitise_dict(value)
        elif isinstance(value, list):
            sanitised[key] = [
                sanitise_string(v) if isinstance(v, str) else v for v in value
            ]
        else:
            sanitised[key] = value
    return sanitised
