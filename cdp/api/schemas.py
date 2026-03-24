"""Pydantic schemas for the Customer Data Platform API."""

from __future__ import annotations

import math
import uuid
from datetime import datetime
from enum import Enum
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, EmailStr, Field, HttpUrl, field_validator

T = TypeVar("T")


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class CampaignTypeEnum(str, Enum):
    email = "email"
    sms = "sms"
    push = "push"
    in_app = "in_app"
    webhook = "webhook"


class CampaignStatusEnum(str, Enum):
    draft = "draft"
    scheduled = "scheduled"
    active = "active"
    paused = "paused"
    completed = "completed"


class TriggerTypeEnum(str, Enum):
    event = "event"
    time = "time"
    segment = "segment"
    api = "api"


class ConsentChannelEnum(str, Enum):
    email = "email"
    sms = "sms"
    push = "push"


class PredictionTypeEnum(str, Enum):
    churn = "churn"
    ltv = "ltv"
    next_purchase = "next_purchase"
    segment = "segment"


# ---------------------------------------------------------------------------
# Paginated wrapper
# ---------------------------------------------------------------------------

class PaginatedResponse(BaseModel, Generic[T]):
    """Generic paginated response wrapper."""

    items: list[T]
    total: int = Field(..., description="Total number of records", ge=0)
    page: int = Field(..., description="Current page number", ge=1)
    size: int = Field(..., description="Items per page", ge=1)
    pages: int = Field(..., description="Total number of pages", ge=0)

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "items": [],
                "total": 100,
                "page": 1,
                "size": 20,
                "pages": 5,
            }
        }
    )

    @classmethod
    def create(
        cls,
        items: list[T],
        total: int,
        page: int,
        size: int,
    ) -> "PaginatedResponse[T]":
        pages = math.ceil(total / size) if size else 0
        return cls(items=items, total=total, page=page, size=size, pages=pages)


# ---------------------------------------------------------------------------
# Auth / Token
# ---------------------------------------------------------------------------

class UserLogin(BaseModel):
    """Credentials for JWT login."""

    username: str = Field(..., min_length=1, max_length=255)
    password: str = Field(..., min_length=1)

    model_config = ConfigDict(
        json_schema_extra={
            "example": {"username": "admin@cdp.io", "password": "s3cret"}
        }
    )


class Token(BaseModel):
    """JWT access + refresh token pair."""

    access_token: str
    refresh_token: str | None = None
    token_type: str = "bearer"

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "access_token": "eyJhbGciOiJIUzI1NiIs...",
                "refresh_token": "eyJhbGciOiJIUzI1NiIs...",
                "token_type": "bearer",
            }
        }
    )


class TokenPayload(BaseModel):
    """Decoded JWT payload."""

    sub: str
    exp: int | None = None
    scopes: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Customer
# ---------------------------------------------------------------------------

class CustomerCreate(BaseModel):
    """Payload for creating a new customer."""

    external_id: str | None = Field(None, max_length=255)
    email: EmailStr | None = None
    phone: str | None = Field(None, max_length=50)
    first_name: str | None = Field(None, max_length=255)
    last_name: str | None = Field(None, max_length=255)
    date_of_birth: datetime | None = None
    gender: str | None = Field(None, max_length=50)
    avatar_url: str | None = Field(None, max_length=2048)
    metadata: dict[str, Any] | None = Field(default_factory=dict)
    tags: list[str] | None = Field(default_factory=list)
    source: str | None = Field(None, max_length=255)

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "email": "alice@example.com",
                "first_name": "Alice",
                "last_name": "Smith",
                "phone": "+15551234567",
                "tags": ["vip", "early-adopter"],
                "source": "website",
            }
        }
    )


class CustomerUpdate(BaseModel):
    """Payload for partial customer update."""

    email: EmailStr | None = None
    phone: str | None = Field(None, max_length=50)
    first_name: str | None = Field(None, max_length=255)
    last_name: str | None = Field(None, max_length=255)
    date_of_birth: datetime | None = None
    gender: str | None = Field(None, max_length=50)
    avatar_url: str | None = Field(None, max_length=2048)
    metadata: dict[str, Any] | None = None
    tags: list[str] | None = None
    is_active: bool | None = None

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "first_name": "Alice",
                "tags": ["vip", "loyal"],
            }
        }
    )


class CustomerResponse(BaseModel):
    """Full customer profile returned by the API."""

    id: uuid.UUID
    external_id: str | None = None
    email: str | None = None
    phone: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    date_of_birth: datetime | None = None
    gender: str | None = None
    avatar_url: str | None = None
    metadata: dict[str, Any] | None = None
    tags: list[str] | None = None
    source: str | None = None
    is_active: bool = True
    lifetime_value: float | None = None
    segment: str | None = None
    risk_score: float | None = None
    engagement_score: float | None = None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class CustomerListResponse(PaginatedResponse[CustomerResponse]):
    """Paginated list of customers."""

    pass


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------

class EventCreate(BaseModel):
    """Payload for recording a single customer event."""

    customer_id: uuid.UUID
    event_type: str = Field(..., max_length=255)
    event_name: str = Field(..., max_length=255)
    properties: dict[str, Any] | None = Field(default_factory=dict)
    source: str | None = Field(None, max_length=255)
    session_id: str | None = Field(None, max_length=255)
    page_url: str | None = Field(None, max_length=2048)
    referrer: str | None = Field(None, max_length=2048)
    timestamp: datetime | None = None

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "customer_id": "550e8400-e29b-41d4-a716-446655440000",
                "event_type": "purchase",
                "event_name": "order_completed",
                "properties": {"order_id": "ORD-123", "total": 99.99},
                "source": "website",
            }
        }
    )


class EventBatchCreate(BaseModel):
    """Batch of events to ingest at once."""

    events: list[EventCreate] = Field(..., min_length=1, max_length=1000)

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "events": [
                    {
                        "customer_id": "550e8400-e29b-41d4-a716-446655440000",
                        "event_type": "page_view",
                        "event_name": "viewed_product",
                        "properties": {"product_id": "SKU-42"},
                    }
                ]
            }
        }
    )


class EventResponse(BaseModel):
    """Persisted event returned by the API."""

    id: uuid.UUID
    customer_id: uuid.UUID
    event_type: str
    event_name: str
    properties: dict[str, Any] | None = None
    source: str | None = None
    session_id: str | None = None
    page_url: str | None = None
    referrer: str | None = None
    timestamp: datetime
    processed: bool = False

    model_config = ConfigDict(from_attributes=True)


# ---------------------------------------------------------------------------
# Tracking (JS tracker payloads)
# ---------------------------------------------------------------------------

class TrackingPayload(BaseModel):
    """Payload sent by the client-side JS tracker."""

    event: str = Field(..., max_length=255, description="Event name")
    properties: dict[str, Any] | None = Field(default_factory=dict)
    visitor_id: str | None = Field(None, max_length=255)
    session_id: str | None = Field(None, max_length=255)
    page_url: str | None = Field(None, max_length=2048)
    page_title: str | None = Field(None, max_length=1024)
    referrer: str | None = Field(None, max_length=2048)
    screen_width: int | None = None
    screen_height: int | None = None
    timestamp: datetime | None = None

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "event": "page_view",
                "visitor_id": "v_abc123",
                "session_id": "s_xyz789",
                "page_url": "https://shop.example.com/products/42",
                "page_title": "Super Widget",
                "referrer": "https://google.com",
                "properties": {"category": "widgets"},
            }
        }
    )


class IdentifyPayload(BaseModel):
    """Payload to link a visitor to a known customer."""

    visitor_id: str = Field(..., max_length=255)
    customer_id: uuid.UUID | None = None
    email: EmailStr | None = None
    traits: dict[str, Any] | None = Field(default_factory=dict)

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "visitor_id": "v_abc123",
                "email": "alice@example.com",
                "traits": {"plan": "premium"},
            }
        }
    )


class PageViewPayload(BaseModel):
    """Dedicated page-view tracking payload."""

    visitor_id: str | None = Field(None, max_length=255)
    session_id: str | None = Field(None, max_length=255)
    page_url: str = Field(..., max_length=2048)
    page_title: str | None = Field(None, max_length=1024)
    referrer: str | None = Field(None, max_length=2048)
    time_on_page: float | None = None
    scroll_depth: float | None = Field(None, ge=0, le=100)

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "visitor_id": "v_abc123",
                "page_url": "https://shop.example.com/checkout",
                "page_title": "Checkout",
                "referrer": "https://shop.example.com/cart",
            }
        }
    )


class SessionResponse(BaseModel):
    """Session data returned by the API."""

    id: uuid.UUID
    visitor_id: uuid.UUID
    customer_id: uuid.UUID | None = None
    session_id: uuid.UUID
    started_at: datetime
    ended_at: datetime | None = None
    page_views: int = 0
    events_count: int = 0
    is_active: bool = True
    device_type: str | None = None
    browser: str | None = None
    os: str | None = None
    country: str | None = None
    city: str | None = None
    utm_source: str | None = None
    utm_medium: str | None = None
    utm_campaign: str | None = None

    model_config = ConfigDict(from_attributes=True)


# ---------------------------------------------------------------------------
# Consent
# ---------------------------------------------------------------------------

class ConsentCreate(BaseModel):
    """Grant or update cookie / tracking consent."""

    visitor_id: str = Field(..., max_length=255)
    consent_given: bool
    consent_categories: dict[str, bool] | None = Field(default_factory=dict)

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "visitor_id": "v_abc123",
                "consent_given": True,
                "consent_categories": {
                    "analytics": True,
                    "marketing": False,
                    "personalization": True,
                },
            }
        }
    )


class ConsentResponse(BaseModel):
    """Consent record returned by the API."""

    id: uuid.UUID
    visitor_id: str
    consent_given: bool
    consent_categories: dict[str, bool] | None = None
    ip_address: str | None = None
    granted_at: datetime | None = None
    expires_at: datetime | None = None
    revoked_at: datetime | None = None

    model_config = ConfigDict(from_attributes=True)


# ---------------------------------------------------------------------------
# Campaigns
# ---------------------------------------------------------------------------

class CampaignCreate(BaseModel):
    """Payload for creating a marketing campaign."""

    name: str = Field(..., min_length=1, max_length=255)
    description: str | None = None
    campaign_type: CampaignTypeEnum
    segment_criteria: dict[str, Any] | None = Field(default_factory=dict)
    content_template: str | None = None
    subject_line: str | None = Field(None, max_length=1024)
    sender_name: str | None = Field(None, max_length=255)
    sender_email: EmailStr | None = None
    scheduled_at: datetime | None = None

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "name": "Summer Sale 2026",
                "campaign_type": "email",
                "subject_line": "Don't miss our summer deals!",
                "sender_name": "CDP Marketing",
                "sender_email": "marketing@cdp.io",
                "segment_criteria": {"tags": ["vip"]},
            }
        }
    )


class CampaignUpdate(BaseModel):
    """Partial update for a campaign."""

    name: str | None = Field(None, max_length=255)
    description: str | None = None
    segment_criteria: dict[str, Any] | None = None
    content_template: str | None = None
    subject_line: str | None = Field(None, max_length=1024)
    sender_name: str | None = Field(None, max_length=255)
    sender_email: EmailStr | None = None
    scheduled_at: datetime | None = None

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "subject_line": "Updated: Summer Sale 2026!",
            }
        }
    )


class CampaignResponse(BaseModel):
    """Campaign record returned by the API."""

    id: uuid.UUID
    name: str
    description: str | None = None
    campaign_type: str
    status: str
    segment_criteria: dict[str, Any] | None = None
    content_template: str | None = None
    subject_line: str | None = None
    sender_name: str | None = None
    sender_email: str | None = None
    scheduled_at: datetime | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    created_by: str | None = None
    total_recipients: int = 0
    sent_count: int = 0
    opened_count: int = 0
    clicked_count: int = 0
    converted_count: int = 0
    revenue_generated: float = 0.0
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class CampaignStats(BaseModel):
    """Aggregated campaign performance statistics."""

    campaign_id: uuid.UUID
    total_recipients: int = 0
    sent_count: int = 0
    opened_count: int = 0
    clicked_count: int = 0
    converted_count: int = 0
    open_rate: float = 0.0
    click_rate: float = 0.0
    conversion_rate: float = 0.0
    revenue_generated: float = 0.0

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "campaign_id": "550e8400-e29b-41d4-a716-446655440000",
                "total_recipients": 5000,
                "sent_count": 4950,
                "opened_count": 1200,
                "clicked_count": 350,
                "converted_count": 80,
                "open_rate": 24.24,
                "click_rate": 7.07,
                "conversion_rate": 1.62,
                "revenue_generated": 12500.00,
            }
        }
    )


# ---------------------------------------------------------------------------
# Workflows
# ---------------------------------------------------------------------------

class WorkflowCreate(BaseModel):
    """Payload for creating an automation workflow."""

    name: str = Field(..., min_length=1, max_length=255)
    description: str | None = None
    trigger_type: TriggerTypeEnum
    trigger_config: dict[str, Any] | None = Field(default_factory=dict)
    steps: list[dict[str, Any]] | None = Field(default_factory=list)

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "name": "Welcome Series",
                "trigger_type": "event",
                "trigger_config": {"event_type": "signup"},
                "steps": [
                    {"action": "send_email", "template": "welcome", "delay": "0m"},
                    {"action": "send_email", "template": "onboarding", "delay": "24h"},
                ],
            }
        }
    )


class WorkflowResponse(BaseModel):
    """Workflow record returned by the API."""

    id: uuid.UUID
    name: str
    description: str | None = None
    trigger_type: str
    trigger_config: dict[str, Any] | None = None
    steps: list[dict[str, Any]] | None = None
    status: str
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class WorkflowExecutionResponse(BaseModel):
    """Single workflow execution record."""

    id: uuid.UUID
    workflow_id: uuid.UUID
    customer_id: uuid.UUID
    current_step: int | None = 0
    status: str
    started_at: datetime
    completed_at: datetime | None = None
    context: dict[str, Any] | None = None

    model_config = ConfigDict(from_attributes=True)


# ---------------------------------------------------------------------------
# Segments
# ---------------------------------------------------------------------------

class SegmentCreate(BaseModel):
    """Payload for creating an AI segment."""

    name: str = Field(..., min_length=1, max_length=255)
    description: str | None = None
    criteria: dict[str, Any] | None = Field(default_factory=dict)
    model_type: str | None = Field(None, max_length=255)
    model_params: dict[str, Any] | None = Field(default_factory=dict)

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "name": "High-Value Churners",
                "description": "Customers with high LTV showing churn signals",
                "criteria": {"ltv_min": 500, "churn_score_min": 0.7},
                "model_type": "kmeans",
            }
        }
    )


class SegmentResponse(BaseModel):
    """AI segment record returned by the API."""

    id: uuid.UUID
    name: str
    description: str | None = None
    criteria: dict[str, Any] | None = None
    model_type: str | None = None
    model_params: dict[str, Any] | None = None
    customer_count: int | None = 0
    last_computed: datetime | None = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


# ---------------------------------------------------------------------------
# Predictions & Recommendations
# ---------------------------------------------------------------------------

class PredictionResponse(BaseModel):
    """Prediction result returned by the API."""

    id: uuid.UUID
    customer_id: uuid.UUID
    prediction_type: str
    prediction_value: float | None = None
    confidence: float | None = None
    features_used: dict[str, Any] | None = None
    model_version: str | None = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class RecommendationResponse(BaseModel):
    """Recommendation returned by the API."""

    id: uuid.UUID
    customer_id: uuid.UUID
    recommendation_type: str
    item_id: str | None = None
    item_name: str | None = None
    score: float | None = None
    reason: str | None = None
    context: dict[str, Any] | None = None
    shown: bool = False
    clicked: bool = False
    converted: bool = False
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


# ---------------------------------------------------------------------------
# Audience
# ---------------------------------------------------------------------------

class AudienceQuery(BaseModel):
    """Query to define or estimate an audience."""

    filters: dict[str, Any] = Field(
        ..., description="Filter criteria for audience selection"
    )
    include_tags: list[str] | None = None
    exclude_tags: list[str] | None = None
    min_engagement_score: float | None = Field(None, ge=0, le=100)
    max_risk_score: float | None = Field(None, ge=0, le=100)

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "filters": {"segment": "high_value", "is_active": True},
                "include_tags": ["vip"],
                "exclude_tags": ["unsubscribed"],
                "min_engagement_score": 30,
            }
        }
    )


class AudienceResponse(BaseModel):
    """Result of an audience estimation or query."""

    estimated_size: int
    filters_applied: dict[str, Any]
    sample_customer_ids: list[uuid.UUID] | None = None

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "estimated_size": 12345,
                "filters_applied": {"segment": "high_value"},
                "sample_customer_ids": [
                    "550e8400-e29b-41d4-a716-446655440000"
                ],
            }
        }
    )


# ---------------------------------------------------------------------------
# Analytics / Dashboard
# ---------------------------------------------------------------------------

class DashboardStats(BaseModel):
    """High-level dashboard statistics."""

    total_customers: int = 0
    active_customers: int = 0
    total_events_today: int = 0
    total_events_week: int = 0
    active_campaigns: int = 0
    active_workflows: int = 0
    avg_engagement_score: float = 0.0
    avg_lifetime_value: float = 0.0
    churn_risk_high: int = 0
    new_customers_today: int = 0
    new_customers_week: int = 0
    revenue_week: float = 0.0

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "total_customers": 50000,
                "active_customers": 42000,
                "total_events_today": 125000,
                "total_events_week": 870000,
                "active_campaigns": 5,
                "active_workflows": 12,
                "avg_engagement_score": 62.5,
                "avg_lifetime_value": 345.80,
                "churn_risk_high": 1200,
                "new_customers_today": 150,
                "new_customers_week": 950,
                "revenue_week": 128500.00,
            }
        }
    )


class CohortAnalysis(BaseModel):
    """Cohort analysis result."""

    cohort_period: str
    cohorts: list[dict[str, Any]]

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "cohort_period": "monthly",
                "cohorts": [
                    {
                        "cohort": "2026-01",
                        "size": 500,
                        "retention": [100, 72, 58, 45],
                    }
                ],
            }
        }
    )


class FunnelStep(BaseModel):
    """Single step in a funnel analysis."""

    step_name: str
    count: int
    conversion_rate: float


class FunnelAnalysis(BaseModel):
    """Funnel analysis result."""

    funnel_name: str
    total_entered: int
    total_converted: int
    overall_conversion_rate: float
    steps: list[FunnelStep]

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "funnel_name": "Purchase Funnel",
                "total_entered": 10000,
                "total_converted": 350,
                "overall_conversion_rate": 3.5,
                "steps": [
                    {"step_name": "page_view", "count": 10000, "conversion_rate": 100.0},
                    {"step_name": "add_to_cart", "count": 2500, "conversion_rate": 25.0},
                    {"step_name": "checkout", "count": 800, "conversion_rate": 8.0},
                    {"step_name": "purchase", "count": 350, "conversion_rate": 3.5},
                ],
            }
        }
    )


class AnalyticsResponse(BaseModel):
    """Generic analytics response wrapper."""

    metric: str
    period: str | None = None
    data: list[dict[str, Any]] = Field(default_factory=list)
    generated_at: datetime | None = None

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "metric": "daily_active_users",
                "period": "last_30_days",
                "data": [
                    {"date": "2026-03-01", "value": 4200},
                    {"date": "2026-03-02", "value": 4350},
                ],
                "generated_at": "2026-03-23T12:00:00Z",
            }
        }
    )


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

class CustomerSearchQuery(BaseModel):
    """Advanced customer search query."""

    query: str | None = Field(None, max_length=1024)
    email: str | None = None
    phone: str | None = None
    tags: list[str] | None = None
    segment: str | None = None
    is_active: bool | None = None
    min_lifetime_value: float | None = None
    max_lifetime_value: float | None = None
    created_after: datetime | None = None
    created_before: datetime | None = None
    page: int = Field(1, ge=1)
    size: int = Field(20, ge=1, le=100)

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "query": "alice",
                "tags": ["vip"],
                "is_active": True,
                "min_lifetime_value": 100,
                "page": 1,
                "size": 20,
            }
        }
    )


# ---------------------------------------------------------------------------
# Bulk Import
# ---------------------------------------------------------------------------

class BulkImportResult(BaseModel):
    """Result of a bulk customer import."""

    total_submitted: int
    created: int
    updated: int
    failed: int
    errors: list[dict[str, Any]] = Field(default_factory=list)

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "total_submitted": 500,
                "created": 480,
                "updated": 15,
                "failed": 5,
                "errors": [
                    {"row": 12, "error": "Invalid email format"},
                ],
            }
        }
    )
