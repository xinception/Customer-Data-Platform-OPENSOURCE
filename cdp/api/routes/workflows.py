"""Automation workflow API endpoints."""

from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from cdp.api.schemas import (
    PaginatedResponse,
    WorkflowCreate,
    WorkflowExecutionResponse,
    WorkflowResponse,
)
from cdp.database import get_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/workflows", tags=["Workflows"])

# ---------------------------------------------------------------------------
# Service helper
# ---------------------------------------------------------------------------


def _get_workflow_service():
    try:
        from cdp.services.marketing.channel_sender import ChannelSender

        return ChannelSender()
    except ImportError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Workflow service is not available yet.",
        )


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


@router.post(
    "",
    response_model=WorkflowResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new automation workflow",
)
async def create_workflow(
    payload: WorkflowCreate,
    db: AsyncSession = Depends(get_db),
) -> Any:
    """Define a new automation workflow with trigger and steps."""
    try:
        svc = _get_workflow_service()
        workflow = await svc.create_workflow(db, payload.model_dump(exclude_unset=True))
        return workflow
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Failed to create workflow")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        )


@router.get(
    "",
    response_model=PaginatedResponse[WorkflowResponse],
    summary="List workflows (paginated)",
)
async def list_workflows(
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    status_filter: str | None = Query(None, alias="status", description="Filter by status"),
    db: AsyncSession = Depends(get_db),
) -> Any:
    """Return a paginated list of automation workflows."""
    try:
        svc = _get_workflow_service()
        filters: dict[str, Any] = {}
        if status_filter:
            filters["status"] = status_filter
        result = await svc.list_workflows(db, page=page, size=size, filters=filters)
        return result
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Failed to list workflows")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        )


@router.get(
    "/{workflow_id}",
    response_model=WorkflowResponse,
    summary="Get a workflow by ID",
)
async def get_workflow(
    workflow_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> Any:
    """Retrieve a single workflow definition."""
    try:
        svc = _get_workflow_service()
        workflow = await svc.get_workflow(db, workflow_id)
        if workflow is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Workflow {workflow_id} not found",
            )
        return workflow
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Failed to get workflow %s", workflow_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        )


@router.put(
    "/{workflow_id}",
    response_model=WorkflowResponse,
    summary="Update a workflow",
)
async def update_workflow(
    workflow_id: uuid.UUID,
    payload: WorkflowCreate,
    db: AsyncSession = Depends(get_db),
) -> Any:
    """Update an existing workflow definition. Only allowed when inactive/draft."""
    try:
        svc = _get_workflow_service()
        workflow = await svc.update_workflow(
            db, workflow_id, payload.model_dump(exclude_unset=True)
        )
        if workflow is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Workflow {workflow_id} not found",
            )
        return workflow
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Failed to update workflow %s", workflow_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        )


@router.delete(
    "/{workflow_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a workflow",
)
async def delete_workflow(
    workflow_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> None:
    """Delete a workflow. Only allowed if not currently active."""
    try:
        svc = _get_workflow_service()
        deleted = await svc.delete_workflow(db, workflow_id)
        if not deleted:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Workflow {workflow_id} not found",
            )
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Failed to delete workflow %s", workflow_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        )


# ---------------------------------------------------------------------------
# Lifecycle actions
# ---------------------------------------------------------------------------


@router.post(
    "/{workflow_id}/activate",
    response_model=WorkflowResponse,
    summary="Activate a workflow",
)
async def activate_workflow(
    workflow_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> Any:
    """Activate a workflow so it starts processing triggers."""
    try:
        svc = _get_workflow_service()
        workflow = await svc.activate_workflow(db, workflow_id)
        if workflow is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Workflow {workflow_id} not found",
            )
        return workflow
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Failed to activate workflow %s", workflow_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        )


@router.post(
    "/{workflow_id}/deactivate",
    response_model=WorkflowResponse,
    summary="Deactivate a workflow",
)
async def deactivate_workflow(
    workflow_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> Any:
    """Deactivate a workflow. Existing executions will complete, but no new ones start."""
    try:
        svc = _get_workflow_service()
        workflow = await svc.deactivate_workflow(db, workflow_id)
        if workflow is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Workflow {workflow_id} not found",
            )
        return workflow
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Failed to deactivate workflow %s", workflow_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        )


# ---------------------------------------------------------------------------
# Executions
# ---------------------------------------------------------------------------


@router.get(
    "/{workflow_id}/executions",
    response_model=PaginatedResponse[WorkflowExecutionResponse],
    summary="List workflow executions",
)
async def list_executions(
    workflow_id: uuid.UUID,
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    status_filter: str | None = Query(None, alias="status", description="Filter by execution status"),
    db: AsyncSession = Depends(get_db),
) -> Any:
    """Return a paginated list of executions for the given workflow."""
    try:
        svc = _get_workflow_service()
        filters: dict[str, Any] = {}
        if status_filter:
            filters["status"] = status_filter
        result = await svc.list_executions(
            db, workflow_id, page=page, size=size, filters=filters
        )
        return result
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Failed to list executions for workflow %s", workflow_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        )
