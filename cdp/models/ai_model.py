"""AI / ML prediction and segmentation SQLAlchemy models."""

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column

from cdp.database import Base


class PredictionType(str, enum.Enum):
    CHURN = "churn"
    LTV = "ltv"
    NEXT_PURCHASE = "next_purchase"
    SEGMENT = "segment"


class AISegment(Base):
    """AI-driven customer segment definition and metadata."""

    __tablename__ = "ai_segments"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(255), unique=True)
    description: Mapped[str | None] = mapped_column(Text)
    criteria: Mapped[dict | None] = mapped_column(JSON, default=dict)
    model_type: Mapped[str | None] = mapped_column(String(255))
    model_params: Mapped[dict | None] = mapped_column(JSON, default=dict)
    customer_count: Mapped[int | None] = mapped_column(default=0)
    last_computed: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    def __repr__(self) -> str:
        return f"<AISegment {self.name}>"


class PredictionResult(Base):
    """Stored prediction output for a customer."""

    __tablename__ = "prediction_results"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    customer_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("customers.id", ondelete="CASCADE"),
        index=True,
    )
    prediction_type: Mapped[str] = mapped_column(String(50), index=True)
    prediction_value: Mapped[float | None] = mapped_column(Float)
    confidence: Mapped[float | None] = mapped_column(Float)
    features_used: Mapped[dict | None] = mapped_column(JSON, default=dict)
    model_version: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )

    __table_args__ = (
        Index(
            "ix_predictions_customer_type",
            "customer_id",
            "prediction_type",
        ),
        Index(
            "ix_predictions_type_created",
            "prediction_type",
            "created_at",
        ),
    )

    def __repr__(self) -> str:
        return f"<PredictionResult {self.prediction_type}={self.prediction_value}>"


class Recommendation(Base):
    """Personalized recommendation generated for a customer."""

    __tablename__ = "recommendations"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    customer_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("customers.id", ondelete="CASCADE"),
        index=True,
    )
    recommendation_type: Mapped[str] = mapped_column(String(255), index=True)
    item_id: Mapped[str | None] = mapped_column(String(255))
    item_name: Mapped[str | None] = mapped_column(String(1024))
    score: Mapped[float | None] = mapped_column(Float)
    reason: Mapped[str | None] = mapped_column(Text)
    context: Mapped[dict | None] = mapped_column(JSON, default=dict)
    shown: Mapped[bool] = mapped_column(Boolean, default=False)
    clicked: Mapped[bool] = mapped_column(Boolean, default=False)
    converted: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )

    __table_args__ = (
        Index("ix_recs_customer_type", "customer_id", "recommendation_type"),
    )

    def __repr__(self) -> str:
        return f"<Recommendation {self.recommendation_type} item={self.item_id}>"
