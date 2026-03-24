"""Analytics and dashboard API endpoints."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from cdp.api.schemas import (
    AnalyticsResponse,
    AudienceQuery,
    AudienceResponse,
    CohortAnalysis,
    DashboardStats,
    FunnelAnalysis,
    PaginatedResponse,
    PredictionResponse,
    SegmentCreate,
    SegmentResponse,
)
from cdp.database import get_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/analytics", tags=["Analytics"])

# ---------------------------------------------------------------------------
# Service helpers
# ---------------------------------------------------------------------------


def _get_segmentation_service():
    try:
        from cdp.services.ai.segmentation import SegmentationService

        return SegmentationService()
    except ImportError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Segmentation service is not available yet.",
        )


def _get_audience_service():
    try:
        from cdp.services.marketing.audience_builder import AudienceBuilder

        return AudienceBuilder()
    except ImportError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Audience service is not available yet.",
        )


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------


@router.get(
    "/dashboard",
    response_model=DashboardStats,
    summary="Get dashboard statistics",
)
async def get_dashboard_stats(
    db: AsyncSession = Depends(get_db),
) -> Any:
    """Return high-level KPI metrics for the main dashboard."""
    try:
        svc = _get_segmentation_service()
        stats = await svc.get_dashboard_stats(db)
        return stats
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Failed to get dashboard stats")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        )


# ---------------------------------------------------------------------------
# Cohort & Funnel
# ---------------------------------------------------------------------------


@router.get(
    "/cohort",
    response_model=CohortAnalysis,
    summary="Run cohort analysis",
)
async def cohort_analysis(
    period: str = Query("monthly", description="Cohort period: weekly, monthly, quarterly"),
    metric: str = Query("retention", description="Metric to compute"),
    start_date: datetime | None = Query(None, description="Analysis start date"),
    end_date: datetime | None = Query(None, description="Analysis end date"),
    db: AsyncSession = Depends(get_db),
) -> Any:
    """Compute cohort analysis over the selected period and metric."""
    try:
        svc = _get_segmentation_service()
        result = await svc.cohort_analysis(
            db, period=period, metric=metric, start_date=start_date, end_date=end_date
        )
        return result
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Cohort analysis failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        )


@router.get(
    "/funnel",
    response_model=FunnelAnalysis,
    summary="Run funnel analysis",
)
async def funnel_analysis(
    steps: str = Query(
        ...,
        description="Comma-separated event names defining funnel steps",
    ),
    start_date: datetime | None = Query(None),
    end_date: datetime | None = Query(None),
    db: AsyncSession = Depends(get_db),
) -> Any:
    """Compute a conversion funnel from the given ordered event steps."""
    step_list = [s.strip() for s in steps.split(",") if s.strip()]
    if len(step_list) < 2:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="At least two funnel steps are required.",
        )
    try:
        svc = _get_segmentation_service()
        result = await svc.funnel_analysis(
            db, steps=step_list, start_date=start_date, end_date=end_date
        )
        return result
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Funnel analysis failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        )


# ---------------------------------------------------------------------------
# Segments
# ---------------------------------------------------------------------------


@router.get(
    "/segments",
    response_model=PaginatedResponse[SegmentResponse],
    summary="List AI segments",
)
async def list_segments(
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
) -> Any:
    """Return an overview of all AI-driven customer segments."""
    try:
        svc = _get_segmentation_service()
        result = await svc.list_segments(db, page=page, size=size)
        return result
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Failed to list segments")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        )


@router.post(
    "/segments/compute",
    response_model=SegmentResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Trigger AI segmentation computation",
)
async def compute_segments(
    payload: SegmentCreate,
    db: AsyncSession = Depends(get_db),
) -> Any:
    """Create or recompute an AI segment based on provided criteria."""
    try:
        svc = _get_segmentation_service()
        segment = await svc.compute_segment(db, payload.model_dump(exclude_unset=True))
        return segment
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Segment computation failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        )


# ---------------------------------------------------------------------------
# Predictions
# ---------------------------------------------------------------------------


@router.get(
    "/predictions",
    response_model=AnalyticsResponse,
    summary="Predictions overview",
)
async def predictions_overview(
    prediction_type: str | None = Query(None, description="Filter by prediction type"),
    db: AsyncSession = Depends(get_db),
) -> Any:
    """Return aggregate prediction statistics across all customers."""
    try:
        svc = _get_segmentation_service()
        result = await svc.predictions_overview(db, prediction_type=prediction_type)
        return result
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Failed to get predictions overview")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        )


# ---------------------------------------------------------------------------
# Audience estimation
# ---------------------------------------------------------------------------


@router.post(
    "/audience/estimate",
    response_model=AudienceResponse,
    summary="Estimate audience size",
)
async def estimate_audience(
    query: AudienceQuery,
    db: AsyncSession = Depends(get_db),
) -> Any:
    """Estimate the number of customers matching the audience criteria."""
    try:
        svc = _get_audience_service()
        result = await svc.estimate_audience(db, query.model_dump(exclude_unset=True))
        return result
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Audience estimation failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        )
