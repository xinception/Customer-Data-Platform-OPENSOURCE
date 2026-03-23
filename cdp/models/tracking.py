"""Tracking and consent SQLAlchemy models."""

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


class TrackingSession(Base):
    """Browser / app session for a visitor or identified customer."""

    __tablename__ = "tracking_sessions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    visitor_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), index=True
    )
    customer_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("customers.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), unique=True, default=uuid.uuid4
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    page_views: Mapped[int] = mapped_column(Integer, default=0)
    events_count: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    device_type: Mapped[str | None] = mapped_column(String(50))
    browser: Mapped[str | None] = mapped_column(String(255))
    os: Mapped[str | None] = mapped_column(String(255))
    country: Mapped[str | None] = mapped_column(String(100))
    city: Mapped[str | None] = mapped_column(String(255))
    utm_source: Mapped[str | None] = mapped_column(String(255))
    utm_medium: Mapped[str | None] = mapped_column(String(255))
    utm_campaign: Mapped[str | None] = mapped_column(String(255))

    # Relationships
    page_view_records: Mapped[list["PageView"]] = relationship(
        back_populates="session", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("ix_sessions_visitor_started", "visitor_id", "started_at"),
        Index("ix_sessions_customer_started", "customer_id", "started_at"),
    )

    def __repr__(self) -> str:
        return f"<TrackingSession {self.session_id}>"


class PageView(Base):
    """Individual page view within a tracking session."""

    __tablename__ = "page_views"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tracking_sessions.id", ondelete="CASCADE"),
        index=True,
    )
    customer_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("customers.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    page_url: Mapped[str] = mapped_column(String(2048))
    page_title: Mapped[str | None] = mapped_column(String(1024))
    referrer: Mapped[str | None] = mapped_column(String(2048))
    time_on_page: Mapped[float | None] = mapped_column(Float)
    scroll_depth: Mapped[float | None] = mapped_column(Float)
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )

    session: Mapped["TrackingSession"] = relationship(
        back_populates="page_view_records"
    )

    __table_args__ = (
        Index("ix_pageviews_session_timestamp", "session_id", "timestamp"),
    )

    def __repr__(self) -> str:
        return f"<PageView {self.page_url}>"


class CookieConsent(Base):
    """Record of cookie / tracking consent given by a visitor."""

    __tablename__ = "cookie_consents"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    visitor_id: Mapped[str] = mapped_column(String(255), index=True)
    consent_given: Mapped[bool] = mapped_column(Boolean, default=False)
    consent_categories: Mapped[dict | None] = mapped_column(JSON, default=dict)
    ip_address: Mapped[str | None] = mapped_column(String(45))
    user_agent: Mapped[str | None] = mapped_column(Text)
    granted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index("ix_consent_visitor_granted", "visitor_id", "granted_at"),
    )

    def __repr__(self) -> str:
        return f"<CookieConsent visitor={self.visitor_id} given={self.consent_given}>"
