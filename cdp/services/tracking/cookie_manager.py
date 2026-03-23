"""Cookie management service with encryption, session handling, and first-party tracking."""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from starlette.requests import Request
from starlette.responses import Response

from cdp.config import settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Cookie name constants
# ---------------------------------------------------------------------------
VISITOR_COOKIE = "_cdp_vid"
SESSION_COOKIE = "_cdp_sid"
CONSENT_COOKIE = "_cdp_consent"

# Session timeout: 30 minutes
SESSION_TIMEOUT_SECONDS = 30 * 60


@dataclass
class CookieConfig:
    """Holds shared cookie parameters."""

    domain: str = field(default_factory=lambda: settings.COOKIE_DOMAIN)
    path: str = "/"
    max_age: int = field(default_factory=lambda: settings.COOKIE_MAX_AGE)
    secure: bool = True
    httponly: bool = True
    samesite: str = "Lax"


class CookieManager:
    """Manages visitor and session cookies with Fernet encryption.

    This service implements a first-party cookie strategy: all cookies are set
    on the platform's own domain, avoiding third-party cookie restrictions
    imposed by modern browsers.
    """

    def __init__(
        self,
        encryption_key: str | None = None,
        config: CookieConfig | None = None,
    ) -> None:
        self._config = config or CookieConfig()

        # Derive or use a provided Fernet key.  In production the key should
        # come from an env-var / secrets manager.  Here we fall back to a
        # deterministic derivation from SECRET_KEY so the service starts
        # without extra configuration.
        if encryption_key:
            self._fernet = Fernet(encryption_key.encode() if isinstance(encryption_key, str) else encryption_key)
        else:
            self._fernet = Fernet(self._derive_key(settings.SECRET_KEY))

        # In-memory session store.  In production swap for Redis.
        self._sessions: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------------
    # Encryption helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _derive_key(secret: str) -> bytes:
        """Derive a Fernet-compatible key from an arbitrary secret string."""
        import base64
        import hashlib

        digest = hashlib.sha256(secret.encode()).digest()
        return base64.urlsafe_b64encode(digest)

    def encrypt(self, value: str) -> str:
        """Encrypt a plaintext string and return a URL-safe token."""
        return self._fernet.encrypt(value.encode()).decode()

    def decrypt(self, token: str) -> str | None:
        """Decrypt a token.  Returns ``None`` on failure instead of raising."""
        try:
            return self._fernet.decrypt(token.encode()).decode()
        except (InvalidToken, Exception):
            logger.warning("Failed to decrypt cookie value")
            return None

    # ------------------------------------------------------------------
    # Cookie helpers (low-level)
    # ------------------------------------------------------------------

    def _set_cookie(
        self,
        response: Response,
        key: str,
        value: str,
        *,
        max_age: int | None = None,
        httponly: bool | None = None,
    ) -> None:
        """Set a cookie on *response* with the configured security defaults."""
        response.set_cookie(
            key=key,
            value=value,
            domain=self._config.domain,
            path=self._config.path,
            max_age=max_age if max_age is not None else self._config.max_age,
            secure=self._config.secure,
            httponly=httponly if httponly is not None else self._config.httponly,
            samesite=self._config.samesite,
        )

    @staticmethod
    def _get_cookie(request: Request, key: str) -> str | None:
        return request.cookies.get(key)

    def delete_cookie(self, response: Response, key: str) -> None:
        """Remove a cookie by setting max_age=0."""
        response.delete_cookie(
            key=key,
            domain=self._config.domain,
            path=self._config.path,
        )

    # ------------------------------------------------------------------
    # Visitor cookie (long-lived, identifies the browser)
    # ------------------------------------------------------------------

    def create_visitor_cookie(self, response: Response) -> str:
        """Generate a new visitor ID, encrypt it, and set the cookie.

        Returns the plaintext visitor UUID.
        """
        visitor_id = str(uuid.uuid4())
        payload = json.dumps({"vid": visitor_id, "ts": int(time.time())})
        encrypted = self.encrypt(payload)
        self._set_cookie(response, VISITOR_COOKIE, encrypted)
        logger.info("Created visitor cookie for %s", visitor_id)
        return visitor_id

    def get_visitor_id(self, request: Request) -> str | None:
        """Extract and decrypt the visitor ID from the request cookies."""
        raw = self._get_cookie(request, VISITOR_COOKIE)
        if raw is None:
            return None
        decrypted = self.decrypt(raw)
        if decrypted is None:
            return None
        try:
            data = json.loads(decrypted)
            return data.get("vid")
        except (json.JSONDecodeError, TypeError):
            logger.warning("Malformed visitor cookie payload")
            return None

    # ------------------------------------------------------------------
    # Session cookie (short-lived, scoped to a browsing session)
    # ------------------------------------------------------------------

    def create_session_cookie(self, response: Response) -> str:
        """Create a new session, encrypt its ID, and set the cookie.

        Returns the plaintext session ID.
        """
        session_id = str(uuid.uuid4())
        self._sessions[session_id] = {
            "created_at": time.time(),
            "last_active": time.time(),
        }
        encrypted = self.encrypt(session_id)
        self._set_cookie(
            response,
            SESSION_COOKIE,
            encrypted,
            max_age=SESSION_TIMEOUT_SECONDS,
        )
        logger.info("Created session %s", session_id)
        return session_id

    def get_session_id(self, request: Request) -> str | None:
        """Retrieve and validate the session ID from request cookies."""
        raw = self._get_cookie(request, SESSION_COOKIE)
        if raw is None:
            return None
        session_id = self.decrypt(raw)
        if session_id is None:
            return None
        if not self.is_session_valid(session_id):
            return None
        # Refresh last-active timestamp.
        if session_id in self._sessions:
            self._sessions[session_id]["last_active"] = time.time()
        return session_id

    def rotate_session(self, request: Request, response: Response) -> str:
        """Invalidate the current session and issue a new one.

        Useful after authentication events to mitigate session-fixation.
        """
        old_id = self.get_session_id(request)
        if old_id and old_id in self._sessions:
            del self._sessions[old_id]
        return self.create_session_cookie(response)

    def is_session_valid(self, session_id: str) -> bool:
        """Return ``True`` if the session exists and has not timed out."""
        session = self._sessions.get(session_id)
        if session is None:
            return False
        elapsed = time.time() - session["last_active"]
        if elapsed > SESSION_TIMEOUT_SECONDS:
            del self._sessions[session_id]
            logger.info("Session %s expired", session_id)
            return False
        return True
