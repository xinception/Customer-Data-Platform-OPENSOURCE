"""Marketing campaign and automation SQLAlchemy models."""

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from cdp.database import Base


class CampaignType(str, enum.Enum):
    EMAIL = "email"
    SMS = "sms"
    PUSH = "push"
    IN_APP = "in_app"
    WEBHOOK = "webhook"


class CampaignStatus(str, enum.Enum):
    DRAFT = "draft"
    SCHEDULED = "scheduled"
    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETED = "completed"


class TriggerType(str, enum.Enum):
    EVENT = "event"
    TIME = "time"
    SEGMENT = "segment"
    API = "api"


class WorkflowExecutionStatus(str, enum.Enum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    PAUSED = "paused"


class ConsentChannel(str, enum.Enum):
    EMAIL = "email"
    SMS = "sms"
    PUSH = "push"


class Campaign(Base):
    """Marketing campaign definition and aggregate metrics."""

    __tablename__ = "campaigns"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(Text)
    campaign_type: Mapped[str] = mapped_column(String(50), index=True)
    status: Mapped[str] = mapped_column(
        String(50), default=CampaignStatus.DRAFT.value, index=True
    )
    segment_criteria: Mapped[dict | None] = mapped_column(JSON, default=dict)
    content_template: Mapped[str | None] = mapped_column(Text)
    subject_line: Mapped[str | None] = mapped_column(String(1024))
    sender_name: Mapped[str | None] = mapped_column(String(255))
    sender_email: Mapped[str | None] = mapped_column(String(320))
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[str | None] = mapped_column(String(255))
    total_recipients: Mapped[int] = mapped_column(Integer, default=0)
    sent_count: Mapped[int] = mapped_column(Integer, default=0)
    opened_count: Mapped[int] = mapped_column(Integer, default=0)
    clicked_count: Mapped[int] = mapped_column(Integer, default=0)
    converted_count: Mapped[int] = mapped_column(Integer, default=0)
    revenue_generated: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        Index("ix_campaigns_status_type", "status", "campaign_type"),
    )

    def __repr__(self) -> str:
        return f"<Campaign {self.name} ({self.status})>"


class AutomationWorkflow(Base):
    """Reusable automation workflow definition."""

    __tablename__ = "automation_workflows"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(Text)
    trigger_type: Mapped[str] = mapped_column(String(50))
    trigger_config: Mapped[dict | None] = mapped_column(JSON, default=dict)
    steps: Mapped[dict | None] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(50), default="draft", index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    executions: Mapped[list["WorkflowExecution"]] = relationship(
        back_populates="workflow", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<AutomationWorkflow {self.name}>"


class WorkflowExecution(Base):
    """Single run of an automation workflow for a specific customer."""

    __tablename__ = "workflow_executions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    workflow_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("automation_workflows.id", ondelete="CASCADE"),
        index=True,
    )
    customer_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("customers.id", ondelete="CASCADE"),
        index=True,
    )
    current_step: Mapped[int | None] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(
        String(50), default=WorkflowExecutionStatus.RUNNING.value, index=True
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    context: Mapped[dict | None] = mapped_column(JSON, default=dict)

    workflow: Mapped["AutomationWorkflow"] = relationship(
        back_populates="executions"
    )

    __table_args__ = (
        Index("ix_executions_workflow_status", "workflow_id", "status"),
        Index("ix_executions_customer_status", "customer_id", "status"),
    )

    def __repr__(self) -> str:
        return f"<WorkflowExecution {self.id} status={self.status}>"


class MarketingConsent(Base):
    """Per-channel marketing consent for a customer."""

    __tablename__ = "marketing_consents"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    customer_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("customers.id", ondelete="CASCADE"),
        index=True,
    )
    channel: Mapped[str] = mapped_column(String(50))
    opted_in: Mapped[bool] = mapped_column(Boolean, default=False)
    opted_in_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    opted_out_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source: Mapped[str | None] = mapped_column(String(255))

    __table_args__ = (
        Index(
            "ix_consent_customer_channel",
            "customer_id",
            "channel",
            unique=True,
        ),
    )

    def __repr__(self) -> str:
        return f"<MarketingConsent customer={self.customer_id} channel={self.channel}>"
