"""Customer-related SQLAlchemy models."""

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
from sqlalchemy.orm import Mapped, mapped_column, relationship

from cdp.database import Base


class IdentityType(str, enum.Enum):
    """Supported identity types for customer resolution."""

    EMAIL = "email"
    PHONE = "phone"
    COOKIE = "cookie"
    DEVICE = "device"
    SOCIAL = "social"


class Customer(Base):
    """Core customer profile aggregated from multiple sources."""

    __tablename__ = "customers"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    external_id: Mapped[str | None] = mapped_column(
        String(255), unique=True, index=True
    )
    email: Mapped[str | None] = mapped_column(String(320), index=True)
    phone: Mapped[str | None] = mapped_column(String(50), index=True)
    first_name: Mapped[str | None] = mapped_column(String(255))
    last_name: Mapped[str | None] = mapped_column(String(255))
    date_of_birth: Mapped[datetime | None] = mapped_column(DateTime)
    gender: Mapped[str | None] = mapped_column(String(50))
    avatar_url: Mapped[str | None] = mapped_column(String(2048))
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSON, default=dict)
    tags: Mapped[list | None] = mapped_column(JSON, default=list)
    source: Mapped[str | None] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    lifetime_value: Mapped[float | None] = mapped_column(Float, default=0.0)
    segment: Mapped[str | None] = mapped_column(String(255), index=True)
    risk_score: Mapped[float | None] = mapped_column(Float)
    engagement_score: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    # Relationships
    identities: Mapped[list["CustomerIdentity"]] = relationship(
        back_populates="customer", cascade="all, delete-orphan"
    )
    events: Mapped[list["CustomerEvent"]] = relationship(
        back_populates="customer", cascade="all, delete-orphan"
    )
    attributes: Mapped[list["CustomerAttribute"]] = relationship(
        back_populates="customer", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("ix_customers_email_active", "email", "is_active"),
        Index("ix_customers_segment_active", "segment", "is_active"),
    )

    def __repr__(self) -> str:
        return f"<Customer {self.id} email={self.email}>"


class CustomerIdentity(Base):
    """Resolved identity linking various identifiers to a single customer."""

    __tablename__ = "customer_identities"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    customer_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("customers.id", ondelete="CASCADE"), index=True
    )
    identity_type: Mapped[str] = mapped_column(String(50))
    identity_value: Mapped[str] = mapped_column(String(1024))
    confidence_score: Mapped[float | None] = mapped_column(Float, default=1.0)
    verified: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    customer: Mapped["Customer"] = relationship(back_populates="identities")

    __table_args__ = (
        Index(
            "ix_identity_type_value", "identity_type", "identity_value", unique=True
        ),
    )

    def __repr__(self) -> str:
        return f"<CustomerIdentity {self.identity_type}={self.identity_value}>"


class CustomerEvent(Base):
    """Behavioural event captured for a customer."""

    __tablename__ = "customer_events"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    customer_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("customers.id", ondelete="CASCADE"), index=True
    )
    event_type: Mapped[str] = mapped_column(String(255), index=True)
    event_name: Mapped[str] = mapped_column(String(255))
    properties: Mapped[dict | None] = mapped_column(JSON, default=dict)
    source: Mapped[str | None] = mapped_column(String(255))
    session_id: Mapped[str | None] = mapped_column(String(255), index=True)
    page_url: Mapped[str | None] = mapped_column(String(2048))
    referrer: Mapped[str | None] = mapped_column(String(2048))
    user_agent: Mapped[str | None] = mapped_column(Text)
    ip_address: Mapped[str | None] = mapped_column(String(45))
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    processed: Mapped[bool] = mapped_column(Boolean, default=False, index=True)

    customer: Mapped["Customer"] = relationship(back_populates="events")

    __table_args__ = (
        Index("ix_events_customer_timestamp", "customer_id", "timestamp"),
        Index("ix_events_type_timestamp", "event_type", "timestamp"),
    )

    def __repr__(self) -> str:
        return f"<CustomerEvent {self.event_type}:{self.event_name}>"


class CustomerAttribute(Base):
    """Key-value attribute attached to a customer profile."""

    __tablename__ = "customer_attributes"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    customer_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("customers.id", ondelete="CASCADE"), index=True
    )
    attribute_key: Mapped[str] = mapped_column(String(255))
    attribute_value: Mapped[str | None] = mapped_column(Text)
    attribute_type: Mapped[str | None] = mapped_column(String(50))
    source: Mapped[str | None] = mapped_column(String(255))
    confidence: Mapped[float | None] = mapped_column(Float, default=1.0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    customer: Mapped["Customer"] = relationship(back_populates="attributes")

    __table_args__ = (
        Index(
            "ix_attr_customer_key",
            "customer_id",
            "attribute_key",
            unique=True,
        ),
    )

    def __repr__(self) -> str:
        return f"<CustomerAttribute {self.attribute_key}>"
