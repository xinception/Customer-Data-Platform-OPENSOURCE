"""Authentication API endpoints (JWT-based)."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm

from cdp.api.schemas import Token, TokenPayload, UserLogin
from cdp.config import settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/auth", tags=["Authentication"])

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/login")

# ---------------------------------------------------------------------------
# JWT helpers – thin wrappers so the route file is self-contained even when
# a dedicated auth service module does not yet exist.
# ---------------------------------------------------------------------------


def _create_access_token(subject: str, expires_delta: timedelta | None = None) -> str:
    """Create a signed JWT access token."""
    try:
        from jose import jwt as jose_jwt
    except ImportError:
        try:
            import jwt as pyjwt

            expire = datetime.now(timezone.utc) + (
                expires_delta or timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
            )
            payload = {"sub": subject, "exp": expire, "type": "access"}
            return pyjwt.encode(payload, settings.SECRET_KEY, algorithm=settings.JWT_ALGORITHM)
        except ImportError:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="JWT library (python-jose or PyJWT) is not installed.",
            )

    expire = datetime.now(timezone.utc) + (
        expires_delta or timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    )
    payload = {"sub": subject, "exp": expire, "type": "access"}
    return jose_jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def _create_refresh_token(subject: str) -> str:
    """Create a signed JWT refresh token (longer-lived)."""
    return _create_access_token(
        subject,
        expires_delta=timedelta(days=7),
    )


def _decode_token(token: str) -> TokenPayload:
    """Decode and validate a JWT token, returning its payload."""
    try:
        from jose import JWTError
        from jose import jwt as jose_jwt

        payload = jose_jwt.decode(
            token, settings.SECRET_KEY, algorithms=[settings.JWT_ALGORITHM]
        )
    except ImportError:
        try:
            import jwt as pyjwt

            payload = pyjwt.decode(
                token, settings.SECRET_KEY, algorithms=[settings.JWT_ALGORITHM]
            )
        except ImportError:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="JWT library is not installed.",
            )
        except pyjwt.PyJWTError:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or expired token",
                headers={"WWW-Authenticate": "Bearer"},
            )
    except JWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    sub: str | None = payload.get("sub")
    if sub is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token payload missing subject",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return TokenPayload(sub=sub, exp=payload.get("exp"), scopes=payload.get("scopes", []))


async def get_current_user(token: str = Depends(oauth2_scheme)) -> TokenPayload:
    """FastAPI dependency that extracts and validates the current user from the JWT."""
    return _decode_token(token)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post(
    "/login",
    response_model=Token,
    summary="Obtain JWT access and refresh tokens",
)
async def login(form_data: OAuth2PasswordRequestForm = Depends()) -> Any:
    """
    Authenticate with username/password and receive JWT tokens.

    In a production deployment this would verify credentials against a user
    store.  For now a simple check is used as a placeholder.
    """
    # ---- Placeholder authentication logic ----
    # Replace with real user lookup + password hash verification.
    if form_data.username == "" or form_data.password == "":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # In production: look up user in DB, verify hashed password.
    # For now we accept any non-empty credentials to unblock frontend work.
    logger.info("User '%s' logged in", form_data.username)

    access_token = _create_access_token(form_data.username)
    refresh_token = _create_refresh_token(form_data.username)

    return Token(
        access_token=access_token,
        refresh_token=refresh_token,
        token_type="bearer",
    )


@router.post(
    "/refresh",
    response_model=Token,
    summary="Refresh an access token",
)
async def refresh_token(token: str = Depends(oauth2_scheme)) -> Any:
    """Exchange a valid refresh token for a new access/refresh token pair."""
    payload = _decode_token(token)
    new_access = _create_access_token(payload.sub)
    new_refresh = _create_refresh_token(payload.sub)
    return Token(
        access_token=new_access,
        refresh_token=new_refresh,
        token_type="bearer",
    )


@router.get(
    "/me",
    response_model=TokenPayload,
    summary="Get current authenticated user",
)
async def me(current_user: TokenPayload = Depends(get_current_user)) -> Any:
    """Return the identity of the currently authenticated user."""
    return current_user
