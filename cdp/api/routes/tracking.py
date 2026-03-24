"""Tracking and consent API endpoints.

These endpoints are called by the client-side JS tracker to record page views,
custom events, and manage cookie consent.
"""

from __future__ import annotations

import base64
import logging
import uuid
from typing import Any

from fastapi import APIRouter, Cookie, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from cdp.api.schemas import (
    ConsentCreate,
    ConsentResponse,
    EventBatchCreate,
    IdentifyPayload,
    PageViewPayload,
    SessionResponse,
    TrackingPayload,
)
from cdp.database import get_db

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Tracking"])

# 1x1 transparent GIF for image-pixel tracking
_PIXEL_GIF = base64.b64decode(
    "R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7"
)

# ---------------------------------------------------------------------------
# Service helpers
# ---------------------------------------------------------------------------


def _get_event_collector():
    try:
        from cdp.services.tracking.event_collector import EventCollector

        return EventCollector()
    except ImportError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Tracking service is not available yet.",
        )


def _get_consent_manager():
    try:
        from cdp.services.tracking.consent_manager import ConsentManager

        return ConsentManager()
    except ImportError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Consent service is not available yet.",
        )


def _get_cookie_manager():
    try:
        from cdp.services.tracking.cookie_manager import CookieManager

        return CookieManager()
    except ImportError:
        return None


# ---------------------------------------------------------------------------
# Tracking endpoints
# ---------------------------------------------------------------------------


@router.post(
    "/api/v1/track",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Track a single event",
    response_model=dict[str, str],
)
async def track_event(
    payload: TrackingPayload,
    request: Request,
    response: Response,
    cdp_visitor: str | None = Cookie(None),
    db: AsyncSession = Depends(get_db),
) -> Any:
    """
    Record a single tracking event from the JS tracker.

    Sets a first-party visitor cookie if one is not already present.
    """
    collector = _get_event_collector()

    # Resolve or create visitor id
    visitor_id = payload.visitor_id or cdp_visitor or str(uuid.uuid4())

    # Set visitor cookie
    cookie_mgr = _get_cookie_manager()
    if cookie_mgr:
        cookie_mgr.set_visitor_cookie(response, visitor_id)
    else:
        response.set_cookie(
            key="cdp_visitor",
            value=visitor_id,
            max_age=365 * 24 * 60 * 60,
            httponly=True,
            samesite="lax",
        )

    try:
        await collector.track(
            db,
            visitor_id=visitor_id,
            event=payload.event,
            properties=payload.properties,
            session_id=payload.session_id,
            page_url=payload.page_url,
            page_title=payload.page_title,
            referrer=payload.referrer,
            ip_address=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
            timestamp=payload.timestamp,
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Failed to track event")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        )

    return {"status": "accepted", "visitor_id": visitor_id}


@router.get(
    "/api/v1/track",
    summary="Track event via image pixel (GET)",
    response_class=Response,
)
async def track_event_pixel(
    request: Request,
    response: Response,
    e: str = Query("page_view", description="Event name"),
    v: str | None = Query(None, description="Visitor ID"),
    s: str | None = Query(None, description="Session ID"),
    u: str | None = Query(None, description="Page URL"),
    cdp_visitor: str | None = Cookie(None),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """
    Image-pixel tracking endpoint.

    Embed as ``<img src="/api/v1/track?e=page_view&u=..." />`` and the
    server returns a transparent 1x1 GIF while recording the event.
    """
    collector = _get_event_collector()
    visitor_id = v or cdp_visitor or str(uuid.uuid4())

    pixel_response = Response(content=_PIXEL_GIF, media_type="image/gif")

    cookie_mgr = _get_cookie_manager()
    if cookie_mgr:
        cookie_mgr.set_visitor_cookie(pixel_response, visitor_id)
    else:
        pixel_response.set_cookie(
            key="cdp_visitor",
            value=visitor_id,
            max_age=365 * 24 * 60 * 60,
            httponly=True,
            samesite="lax",
        )

    try:
        await collector.track(
            db,
            visitor_id=visitor_id,
            event=e,
            session_id=s,
            page_url=u,
            ip_address=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
        )
    except Exception:
        logger.exception("Pixel tracking failed (non-fatal)")

    return pixel_response


@router.post(
    "/api/v1/track/batch",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Batch track events",
    response_model=dict[str, Any],
)
async def track_batch(
    payload: EventBatchCreate,
    db: AsyncSession = Depends(get_db),
) -> Any:
    """Ingest a batch of events in a single request."""
    collector = _get_event_collector()
    try:
        count = await collector.track_batch(
            db, [ev.model_dump() for ev in payload.events]
        )
        return {"status": "accepted", "count": count}
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Batch tracking failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        )


@router.post(
    "/api/v1/track/identify",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Identify a visitor as a known customer",
    response_model=dict[str, str],
)
async def identify_user(
    payload: IdentifyPayload,
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> Any:
    """
    Link an anonymous visitor to a known customer profile.

    Merges the visitor's anonymous event history into the customer's timeline.
    """
    collector = _get_event_collector()

    # Set cookie with the (now-identified) visitor id
    cookie_mgr = _get_cookie_manager()
    if cookie_mgr:
        cookie_mgr.set_visitor_cookie(response, payload.visitor_id)
    else:
        response.set_cookie(
            key="cdp_visitor",
            value=payload.visitor_id,
            max_age=365 * 24 * 60 * 60,
            httponly=True,
            samesite="lax",
        )

    try:
        await collector.identify(
            db,
            visitor_id=payload.visitor_id,
            customer_id=payload.customer_id,
            email=payload.email,
            traits=payload.traits,
        )
        return {"status": "identified", "visitor_id": payload.visitor_id}
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Identify failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        )


@router.post(
    "/api/v1/track/page",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Track a page view",
    response_model=dict[str, str],
)
async def track_page_view(
    payload: PageViewPayload,
    request: Request,
    response: Response,
    cdp_visitor: str | None = Cookie(None),
    db: AsyncSession = Depends(get_db),
) -> Any:
    """Record a page-view event with optional scroll depth and time-on-page."""
    collector = _get_event_collector()
    visitor_id = payload.visitor_id or cdp_visitor or str(uuid.uuid4())

    cookie_mgr = _get_cookie_manager()
    if cookie_mgr:
        cookie_mgr.set_visitor_cookie(response, visitor_id)
    else:
        response.set_cookie(
            key="cdp_visitor",
            value=visitor_id,
            max_age=365 * 24 * 60 * 60,
            httponly=True,
            samesite="lax",
        )

    try:
        await collector.track(
            db,
            visitor_id=visitor_id,
            event="page_view",
            session_id=payload.session_id,
            page_url=payload.page_url,
            page_title=payload.page_title,
            referrer=payload.referrer,
            properties={
                "time_on_page": payload.time_on_page,
                "scroll_depth": payload.scroll_depth,
            },
            ip_address=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
        )
        return {"status": "accepted", "visitor_id": visitor_id}
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Page-view tracking failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        )


@router.get(
    "/api/v1/track/session/{session_id}",
    response_model=SessionResponse,
    summary="Get session data",
)
async def get_session(
    session_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> Any:
    """Retrieve session data by session ID."""
    collector = _get_event_collector()
    try:
        session = await collector.get_session(db, session_id)
        if session is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Session {session_id} not found",
            )
        return session
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Failed to get session %s", session_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        )


# ---------------------------------------------------------------------------
# Consent endpoints
# ---------------------------------------------------------------------------


@router.post(
    "/api/v1/consent",
    response_model=ConsentResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Grant or update consent",
)
async def grant_consent(
    payload: ConsentCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> Any:
    """Record that a visitor has granted (or updated) tracking consent."""
    mgr = _get_consent_manager()
    try:
        consent = await mgr.grant_consent(
            db,
            visitor_id=payload.visitor_id,
            consent_given=payload.consent_given,
            consent_categories=payload.consent_categories,
            ip_address=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
        )
        return consent
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Failed to record consent")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        )


@router.get(
    "/api/v1/consent/{visitor_id}",
    response_model=ConsentResponse,
    summary="Get consent status for a visitor",
)
async def get_consent(
    visitor_id: str,
    db: AsyncSession = Depends(get_db),
) -> Any:
    """Retrieve the current consent status for a visitor."""
    mgr = _get_consent_manager()
    try:
        consent = await mgr.get_consent(db, visitor_id=visitor_id)
        if consent is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"No consent record for visitor {visitor_id}",
            )
        return consent
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Failed to get consent for %s", visitor_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        )


@router.delete(
    "/api/v1/consent/{visitor_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Revoke consent for a visitor",
)
async def revoke_consent(
    visitor_id: str,
    db: AsyncSession = Depends(get_db),
) -> None:
    """Revoke all tracking consent for a visitor."""
    mgr = _get_consent_manager()
    try:
        revoked = await mgr.revoke_consent(db, visitor_id=visitor_id)
        if not revoked:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"No consent record for visitor {visitor_id}",
            )
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Failed to revoke consent for %s", visitor_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        )
