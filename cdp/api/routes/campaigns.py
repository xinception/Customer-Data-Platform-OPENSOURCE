"""Campaign management API endpoints."""

from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from cdp.api.schemas import (
    CampaignCreate,
    CampaignResponse,
    CampaignStats,
    CampaignUpdate,
    CustomerResponse,
    PaginatedResponse,
)
from cdp.database import get_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/campaigns", tags=["Campaigns"])

# ---------------------------------------------------------------------------
# Service helper
# ---------------------------------------------------------------------------


def _get_campaign_service():
    try:
        from cdp.services.marketing.channel_sender import ChannelSender

        return ChannelSender()
    except ImportError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Campaign service is not available yet.",
        )


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


@router.post(
    "",
    response_model=CampaignResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new campaign",
)
async def create_campaign(
    payload: CampaignCreate,
    db: AsyncSession = Depends(get_db),
) -> Any:
    """Create a new marketing campaign."""
    try:
        svc = _get_campaign_service()
        campaign = await svc.create_campaign(db, payload.model_dump(exclude_unset=True))
        return campaign
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Failed to create campaign")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        )


@router.get(
    "",
    response_model=PaginatedResponse[CampaignResponse],
    summary="List campaigns (paginated)",
)
async def list_campaigns(
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    status_filter: str | None = Query(None, alias="status", description="Filter by status"),
    campaign_type: str | None = Query(None, description="Filter by type"),
    db: AsyncSession = Depends(get_db),
) -> Any:
    """Return a paginated list of campaigns."""
    try:
        svc = _get_campaign_service()
        filters: dict[str, Any] = {}
        if status_filter:
            filters["status"] = status_filter
        if campaign_type:
            filters["campaign_type"] = campaign_type
        result = await svc.list_campaigns(db, page=page, size=size, filters=filters)
        return result
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Failed to list campaigns")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        )


@router.get(
    "/{campaign_id}",
    response_model=CampaignResponse,
    summary="Get a campaign by ID",
)
async def get_campaign(
    campaign_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> Any:
    """Retrieve a single campaign by ID."""
    try:
        svc = _get_campaign_service()
        campaign = await svc.get_campaign(db, campaign_id)
        if campaign is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Campaign {campaign_id} not found",
            )
        return campaign
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Failed to get campaign %s", campaign_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        )


@router.put(
    "/{campaign_id}",
    response_model=CampaignResponse,
    summary="Update a campaign",
)
async def update_campaign(
    campaign_id: uuid.UUID,
    payload: CampaignUpdate,
    db: AsyncSession = Depends(get_db),
) -> Any:
    """Partially update a campaign. Only modifiable while in draft/paused state."""
    try:
        svc = _get_campaign_service()
        campaign = await svc.update_campaign(
            db, campaign_id, payload.model_dump(exclude_unset=True)
        )
        if campaign is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Campaign {campaign_id} not found",
            )
        return campaign
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Failed to update campaign %s", campaign_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        )


@router.delete(
    "/{campaign_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a campaign",
)
async def delete_campaign(
    campaign_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> None:
    """Delete a campaign. Only allowed if the campaign is in draft state."""
    try:
        svc = _get_campaign_service()
        deleted = await svc.delete_campaign(db, campaign_id)
        if not deleted:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Campaign {campaign_id} not found",
            )
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Failed to delete campaign %s", campaign_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        )


# ---------------------------------------------------------------------------
# Lifecycle actions
# ---------------------------------------------------------------------------


@router.post(
    "/{campaign_id}/launch",
    response_model=CampaignResponse,
    summary="Launch a campaign",
)
async def launch_campaign(
    campaign_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> Any:
    """Transition a campaign from draft/scheduled to active and begin delivery."""
    try:
        svc = _get_campaign_service()
        campaign = await svc.launch_campaign(db, campaign_id)
        if campaign is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Campaign {campaign_id} not found",
            )
        return campaign
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Failed to launch campaign %s", campaign_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        )


@router.post(
    "/{campaign_id}/pause",
    response_model=CampaignResponse,
    summary="Pause a running campaign",
)
async def pause_campaign(
    campaign_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> Any:
    """Pause an active campaign. Delivery is stopped until resumed."""
    try:
        svc = _get_campaign_service()
        campaign = await svc.pause_campaign(db, campaign_id)
        if campaign is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Campaign {campaign_id} not found",
            )
        return campaign
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Failed to pause campaign %s", campaign_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        )


# ---------------------------------------------------------------------------
# Stats & Recipients
# ---------------------------------------------------------------------------


@router.get(
    "/{campaign_id}/stats",
    response_model=CampaignStats,
    summary="Get campaign performance statistics",
)
async def get_campaign_stats(
    campaign_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> Any:
    """Return aggregated campaign performance metrics."""
    try:
        svc = _get_campaign_service()
        stats = await svc.get_campaign_stats(db, campaign_id)
        if stats is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Campaign {campaign_id} not found",
            )
        return stats
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Failed to get stats for campaign %s", campaign_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        )


@router.get(
    "/{campaign_id}/recipients",
    response_model=PaginatedResponse[CustomerResponse],
    summary="List campaign recipients",
)
async def get_campaign_recipients(
    campaign_id: uuid.UUID,
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
) -> Any:
    """Return a paginated list of customers targeted by the campaign."""
    try:
        svc = _get_campaign_service()
        result = await svc.get_campaign_recipients(
            db, campaign_id, page=page, size=size
        )
        if result is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Campaign {campaign_id} not found",
            )
        return result
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Failed to get recipients for campaign %s", campaign_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        )
