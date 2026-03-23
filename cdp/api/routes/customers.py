"""Customer management API endpoints."""

from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from sqlalchemy.ext.asyncio import AsyncSession

from cdp.api.schemas import (
    BulkImportResult,
    CustomerCreate,
    CustomerListResponse,
    CustomerResponse,
    CustomerSearchQuery,
    CustomerUpdate,
    EventResponse,
    PaginatedResponse,
    PredictionResponse,
    RecommendationResponse,
)
from cdp.database import get_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/customers", tags=["Customers"])

# ---------------------------------------------------------------------------
# Service helpers – imported lazily so the API layer can start even if
# service modules are still being developed.
# ---------------------------------------------------------------------------


def _get_profile_service():
    """Return the profile manager service, or raise 503 if unavailable."""
    try:
        from cdp.services.profile.profile_manager import ProfileManager

        return ProfileManager()
    except ImportError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Profile service is not available yet.",
        )


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


@router.post(
    "",
    response_model=CustomerResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new customer",
)
async def create_customer(
    payload: CustomerCreate,
    db: AsyncSession = Depends(get_db),
) -> Any:
    """Create a new customer profile in the CDP."""
    try:
        svc = _get_profile_service()
        customer = await svc.create_customer(db, payload.model_dump(exclude_unset=True))
        return customer
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Failed to create customer")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        )


@router.get(
    "",
    response_model=CustomerListResponse,
    summary="List customers (paginated)",
)
async def list_customers(
    page: int = Query(1, ge=1, description="Page number"),
    size: int = Query(20, ge=1, le=100, description="Items per page"),
    is_active: bool | None = Query(None, description="Filter by active status"),
    segment: str | None = Query(None, description="Filter by segment"),
    tag: str | None = Query(None, description="Filter by tag"),
    source: str | None = Query(None, description="Filter by source"),
    db: AsyncSession = Depends(get_db),
) -> Any:
    """Return a paginated, optionally filtered list of customers."""
    try:
        svc = _get_profile_service()
        filters = {}
        if is_active is not None:
            filters["is_active"] = is_active
        if segment:
            filters["segment"] = segment
        if tag:
            filters["tag"] = tag
        if source:
            filters["source"] = source
        result = await svc.list_customers(db, page=page, size=size, filters=filters)
        return result
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Failed to list customers")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        )


@router.get(
    "/{customer_id}",
    response_model=CustomerResponse,
    summary="Get full customer profile (360-degree view)",
)
async def get_customer(
    customer_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> Any:
    """Retrieve the full 360-degree customer profile by ID."""
    try:
        svc = _get_profile_service()
        customer = await svc.get_customer(db, customer_id)
        if customer is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Customer {customer_id} not found",
            )
        return customer
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Failed to retrieve customer %s", customer_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        )


@router.put(
    "/{customer_id}",
    response_model=CustomerResponse,
    summary="Update a customer",
)
async def update_customer(
    customer_id: uuid.UUID,
    payload: CustomerUpdate,
    db: AsyncSession = Depends(get_db),
) -> Any:
    """Partially update a customer profile."""
    try:
        svc = _get_profile_service()
        customer = await svc.update_customer(
            db, customer_id, payload.model_dump(exclude_unset=True)
        )
        if customer is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Customer {customer_id} not found",
            )
        return customer
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Failed to update customer %s", customer_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        )


@router.delete(
    "/{customer_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a customer (GDPR right-to-erasure)",
)
async def delete_customer(
    customer_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> None:
    """Permanently delete a customer and all related data (GDPR compliance)."""
    try:
        svc = _get_profile_service()
        deleted = await svc.delete_customer(db, customer_id)
        if not deleted:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Customer {customer_id} not found",
            )
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Failed to delete customer %s", customer_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        )


# ---------------------------------------------------------------------------
# Timeline, Predictions, Recommendations
# ---------------------------------------------------------------------------


@router.get(
    "/{customer_id}/timeline",
    response_model=PaginatedResponse[EventResponse],
    summary="Get customer event timeline",
)
async def get_customer_timeline(
    customer_id: uuid.UUID,
    page: int = Query(1, ge=1),
    size: int = Query(50, ge=1, le=200),
    event_type: str | None = Query(None, description="Filter by event type"),
    db: AsyncSession = Depends(get_db),
) -> Any:
    """Return the chronological event timeline for a customer."""
    try:
        svc = _get_profile_service()
        result = await svc.get_customer_timeline(
            db, customer_id, page=page, size=size, event_type=event_type
        )
        return result
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Failed to get timeline for customer %s", customer_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        )


@router.get(
    "/{customer_id}/predictions",
    response_model=list[PredictionResponse],
    summary="Get AI predictions for a customer",
)
async def get_customer_predictions(
    customer_id: uuid.UUID,
    prediction_type: str | None = Query(None, description="Filter by prediction type"),
    db: AsyncSession = Depends(get_db),
) -> Any:
    """Retrieve all AI predictions for the specified customer."""
    try:
        svc = _get_profile_service()
        predictions = await svc.get_customer_predictions(
            db, customer_id, prediction_type=prediction_type
        )
        return predictions
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Failed to get predictions for customer %s", customer_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        )


@router.get(
    "/{customer_id}/recommendations",
    response_model=list[RecommendationResponse],
    summary="Get recommendations for a customer",
)
async def get_customer_recommendations(
    customer_id: uuid.UUID,
    recommendation_type: str | None = Query(None, description="Filter by type"),
    limit: int = Query(10, ge=1, le=50),
    db: AsyncSession = Depends(get_db),
) -> Any:
    """Retrieve personalised recommendations for the specified customer."""
    try:
        svc = _get_profile_service()
        recs = await svc.get_customer_recommendations(
            db, customer_id, recommendation_type=recommendation_type, limit=limit
        )
        return recs
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Failed to get recommendations for customer %s", customer_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        )


# ---------------------------------------------------------------------------
# Search & Bulk Import
# ---------------------------------------------------------------------------


@router.post(
    "/search",
    response_model=CustomerListResponse,
    summary="Advanced customer search",
)
async def search_customers(
    query: CustomerSearchQuery,
    db: AsyncSession = Depends(get_db),
) -> Any:
    """Perform an advanced search across customer profiles."""
    try:
        svc = _get_profile_service()
        result = await svc.search_customers(db, query.model_dump(exclude_unset=True))
        return result
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Customer search failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        )


@router.post(
    "/bulk-import",
    response_model=BulkImportResult,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Bulk import customers from CSV/JSON",
)
async def bulk_import_customers(
    file: UploadFile = File(..., description="CSV or JSON file with customer records"),
    db: AsyncSession = Depends(get_db),
) -> Any:
    """
    Accept a CSV or JSON file and import customer records in bulk.

    Returns a summary with counts of created, updated, and failed records.
    """
    allowed_types = {
        "text/csv",
        "application/json",
        "application/vnd.ms-excel",
        "application/octet-stream",
    }
    if file.content_type and file.content_type not in allowed_types:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=f"Unsupported file type: {file.content_type}. Use CSV or JSON.",
        )
    try:
        svc = _get_profile_service()
        content = await file.read()
        result = await svc.bulk_import(db, content, filename=file.filename)
        return result
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Bulk import failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        )
