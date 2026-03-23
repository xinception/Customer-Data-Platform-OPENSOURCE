"""Campaign lifecycle management with A/B testing, audience selection, and performance tracking.

Provides create, schedule, launch, pause, stop operations on campaigns, plus
async bulk execution and real-time performance metrics.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from cdp.models.customer import Customer
from cdp.models.marketing import Campaign, CampaignStatus, CampaignType, MarketingConsent
from cdp.services.marketing.audience_builder import AudienceBuilder
from cdp.services.marketing.channel_sender import ChannelSender, ChannelType

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# A/B testing helpers
# ---------------------------------------------------------------------------

class CampaignVariant:
    """Single variant inside an A/B test."""

    def __init__(
        self,
        variant_id: str,
        name: str,
        subject_line: str | None = None,
        content_template: str | None = None,
        weight: float = 0.5,
    ) -> None:
        self.variant_id = variant_id
        self.name = name
        self.subject_line = subject_line
        self.content_template = content_template
        self.weight = weight
        # Running metrics for the variant
        self.sent_count: int = 0
        self.opened_count: int = 0
        self.clicked_count: int = 0
        self.converted_count: int = 0
        self.revenue: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "variant_id": self.variant_id,
            "name": self.name,
            "subject_line": self.subject_line,
            "content_template": self.content_template,
            "weight": self.weight,
            "sent_count": self.sent_count,
            "opened_count": self.opened_count,
            "clicked_count": self.clicked_count,
            "converted_count": self.converted_count,
            "revenue": self.revenue,
        }


# ---------------------------------------------------------------------------
# CampaignManager
# ---------------------------------------------------------------------------

class CampaignManager:
    """Manages the full campaign lifecycle including A/B testing and execution.

    Parameters
    ----------
    audience_builder:
        Audience builder instance for resolving segment criteria.
    channel_sender:
        Channel sender instance for multi-channel delivery.
    """

    def __init__(
        self,
        audience_builder: AudienceBuilder | None = None,
        channel_sender: ChannelSender | None = None,
    ) -> None:
        self._audience_builder = audience_builder or AudienceBuilder()
        self._channel_sender = channel_sender or ChannelSender()
        # In-memory A/B variant store keyed by campaign_id
        self._variants: dict[uuid.UUID, list[CampaignVariant]] = {}

    # ------------------------------------------------------------------
    # Campaign CRUD
    # ------------------------------------------------------------------

    async def create_campaign(
        self,
        db_session: AsyncSession,
        campaign_data: dict[str, Any],
    ) -> Campaign:
        """Create a new campaign in DRAFT status.

        Parameters
        ----------
        db_session:
            Active async database session.
        campaign_data:
            Dictionary of :class:`Campaign` column values.  ``id`` is
            auto-generated when not supplied.

        Returns
        -------
        Campaign
            The newly-persisted campaign.
        """
        campaign_data.setdefault("id", uuid.uuid4())
        campaign_data.setdefault("status", CampaignStatus.DRAFT.value)

        campaign = Campaign(**campaign_data)
        db_session.add(campaign)
        await db_session.flush()
        await db_session.refresh(campaign)
        logger.info("Created campaign '%s' (%s)", campaign.name, campaign.id)
        return campaign

    async def get_campaign(
        self,
        db_session: AsyncSession,
        campaign_id: uuid.UUID,
    ) -> Campaign | None:
        """Fetch a single campaign by primary key."""
        stmt = select(Campaign).where(Campaign.id == campaign_id)
        result = await db_session.execute(stmt)
        return result.scalars().first()

    # ------------------------------------------------------------------
    # Lifecycle transitions
    # ------------------------------------------------------------------

    async def schedule_campaign(
        self,
        db_session: AsyncSession,
        campaign_id: uuid.UUID,
        scheduled_at: datetime,
    ) -> Campaign:
        """Schedule a DRAFT campaign for future launch.

        Raises
        ------
        ValueError
            If the campaign is not in DRAFT status or ``scheduled_at`` is in
            the past.
        """
        campaign = await self._get_or_raise(db_session, campaign_id)

        if campaign.status != CampaignStatus.DRAFT.value:
            raise ValueError(
                f"Cannot schedule campaign in '{campaign.status}' status; must be DRAFT"
            )

        if scheduled_at.tzinfo is None:
            scheduled_at = scheduled_at.replace(tzinfo=timezone.utc)
        if scheduled_at <= datetime.now(timezone.utc):
            raise ValueError("scheduled_at must be in the future")

        campaign.status = CampaignStatus.SCHEDULED.value
        campaign.scheduled_at = scheduled_at
        await db_session.flush()
        logger.info("Scheduled campaign %s for %s", campaign_id, scheduled_at.isoformat())
        return campaign

    async def launch_campaign(
        self,
        db_session: AsyncSession,
        campaign_id: uuid.UUID,
    ) -> Campaign:
        """Immediately activate a DRAFT or SCHEDULED campaign.

        Raises
        ------
        ValueError
            If the campaign is not in a launchable state.
        """
        campaign = await self._get_or_raise(db_session, campaign_id)

        launchable = {CampaignStatus.DRAFT.value, CampaignStatus.SCHEDULED.value}
        if campaign.status not in launchable:
            raise ValueError(
                f"Cannot launch campaign in '{campaign.status}' status; "
                "must be DRAFT or SCHEDULED"
            )

        campaign.status = CampaignStatus.ACTIVE.value
        campaign.started_at = datetime.now(timezone.utc)
        await db_session.flush()
        logger.info("Launched campaign %s", campaign_id)
        return campaign

    async def pause_campaign(
        self,
        db_session: AsyncSession,
        campaign_id: uuid.UUID,
    ) -> Campaign:
        """Pause an ACTIVE campaign.

        Raises
        ------
        ValueError
            If the campaign is not ACTIVE.
        """
        campaign = await self._get_or_raise(db_session, campaign_id)

        if campaign.status != CampaignStatus.ACTIVE.value:
            raise ValueError(
                f"Cannot pause campaign in '{campaign.status}' status; must be ACTIVE"
            )

        campaign.status = CampaignStatus.PAUSED.value
        await db_session.flush()
        logger.info("Paused campaign %s", campaign_id)
        return campaign

    async def stop_campaign(
        self,
        db_session: AsyncSession,
        campaign_id: uuid.UUID,
    ) -> Campaign:
        """Stop (complete) an ACTIVE or PAUSED campaign.

        Raises
        ------
        ValueError
            If the campaign is not in a stoppable state.
        """
        campaign = await self._get_or_raise(db_session, campaign_id)

        stoppable = {CampaignStatus.ACTIVE.value, CampaignStatus.PAUSED.value}
        if campaign.status not in stoppable:
            raise ValueError(
                f"Cannot stop campaign in '{campaign.status}' status; "
                "must be ACTIVE or PAUSED"
            )

        campaign.status = CampaignStatus.COMPLETED.value
        campaign.completed_at = datetime.now(timezone.utc)
        await db_session.flush()
        logger.info("Stopped campaign %s", campaign_id)
        return campaign

    # ------------------------------------------------------------------
    # A/B testing
    # ------------------------------------------------------------------

    def add_variant(
        self,
        campaign_id: uuid.UUID,
        variant: CampaignVariant,
    ) -> None:
        """Register an A/B test variant for *campaign_id*."""
        variants = self._variants.setdefault(campaign_id, [])
        variants.append(variant)
        # Re-normalise weights so they sum to 1.0
        total = sum(v.weight for v in variants)
        if total > 0:
            for v in variants:
                v.weight = v.weight / total
        logger.info(
            "Added variant '%s' to campaign %s (%d total)",
            variant.name,
            campaign_id,
            len(variants),
        )

    def get_variants(self, campaign_id: uuid.UUID) -> list[CampaignVariant]:
        """Return all registered variants for a campaign."""
        return list(self._variants.get(campaign_id, []))

    def remove_variant(self, campaign_id: uuid.UUID, variant_id: str) -> bool:
        """Remove a variant by its ID.  Returns ``True`` when found."""
        variants = self._variants.get(campaign_id, [])
        for idx, v in enumerate(variants):
            if v.variant_id == variant_id:
                variants.pop(idx)
                logger.info("Removed variant '%s' from campaign %s", variant_id, campaign_id)
                return True
        return False

    def _select_variant(self, campaign_id: uuid.UUID, index: int) -> CampaignVariant | None:
        """Deterministically assign a variant based on recipient index and weights."""
        variants = self._variants.get(campaign_id)
        if not variants:
            return None
        # Weighted round-robin: map the normalised index position to a variant
        position = (index % 1000) / 1000.0
        cumulative = 0.0
        for variant in variants:
            cumulative += variant.weight
            if position < cumulative:
                return variant
        return variants[-1]

    # ------------------------------------------------------------------
    # Campaign execution
    # ------------------------------------------------------------------

    async def execute_campaign(
        self,
        db_session: AsyncSession,
        campaign_id: uuid.UUID,
    ) -> dict[str, Any]:
        """Execute a campaign by sending to all matched recipients.

        The campaign must be ACTIVE.  For each recipient the appropriate
        channel sender method is called.  When A/B variants are registered
        the content is varied accordingly.

        Returns a summary dict with send counts and errors.
        """
        campaign = await self._get_or_raise(db_session, campaign_id)

        if campaign.status != CampaignStatus.ACTIVE.value:
            raise ValueError(
                f"Cannot execute campaign in '{campaign.status}' status; must be ACTIVE"
            )

        # Resolve audience from segment criteria
        criteria = campaign.segment_criteria or {}
        if criteria:
            customers = await self._audience_builder.evaluate_audience(db_session, criteria)
        else:
            # No criteria means send to all active customers
            stmt = select(Customer).where(Customer.is_active.is_(True))
            result = await db_session.execute(stmt)
            customers = list(result.scalars().all())

        # Filter by consent for the campaign channel
        consented_customers = await self._filter_by_consent(
            db_session, customers, campaign.campaign_type
        )

        campaign.total_recipients = len(consented_customers)
        sent = 0
        errors = 0
        error_details: list[dict[str, Any]] = []

        for idx, customer in enumerate(consented_customers):
            try:
                variant = self._select_variant(campaign_id, idx)
                subject = (variant.subject_line if variant else None) or campaign.subject_line or ""
                body = (variant.content_template if variant else None) or campaign.content_template or ""
                template_vars = self._build_template_vars(customer, campaign)

                await self._send_to_customer(
                    customer=customer,
                    campaign=campaign,
                    subject=subject,
                    body=body,
                    template_vars=template_vars,
                )
                sent += 1
                if variant:
                    variant.sent_count += 1
            except Exception as exc:
                errors += 1
                error_details.append({
                    "customer_id": str(customer.id),
                    "error": str(exc),
                })
                logger.warning(
                    "Failed to send campaign %s to customer %s: %s",
                    campaign_id,
                    customer.id,
                    exc,
                )

        campaign.sent_count = sent
        await db_session.flush()

        summary = {
            "campaign_id": str(campaign_id),
            "total_recipients": campaign.total_recipients,
            "sent": sent,
            "errors": errors,
            "error_details": error_details,
        }
        logger.info(
            "Campaign %s executed: %d sent, %d errors out of %d recipients",
            campaign_id,
            sent,
            errors,
            campaign.total_recipients,
        )
        return summary

    # ------------------------------------------------------------------
    # Performance metrics
    # ------------------------------------------------------------------

    async def get_campaign_stats(
        self,
        db_session: AsyncSession,
        campaign_id: uuid.UUID,
    ) -> dict[str, Any]:
        """Return performance metrics for a campaign.

        Metrics include open_rate, click_rate, conversion_rate, and revenue.
        When A/B variants are registered their individual metrics are
        included under a ``variants`` key.
        """
        campaign = await self._get_or_raise(db_session, campaign_id)

        sent = campaign.sent_count or 0
        stats: dict[str, Any] = {
            "campaign_id": str(campaign_id),
            "name": campaign.name,
            "status": campaign.status,
            "total_recipients": campaign.total_recipients,
            "sent_count": sent,
            "opened_count": campaign.opened_count,
            "clicked_count": campaign.clicked_count,
            "converted_count": campaign.converted_count,
            "revenue_generated": campaign.revenue_generated,
            "open_rate": (campaign.opened_count / sent) if sent > 0 else 0.0,
            "click_rate": (campaign.clicked_count / sent) if sent > 0 else 0.0,
            "conversion_rate": (campaign.converted_count / sent) if sent > 0 else 0.0,
            "revenue_per_recipient": (
                campaign.revenue_generated / sent if sent > 0 else 0.0
            ),
            "started_at": campaign.started_at.isoformat() if campaign.started_at else None,
            "completed_at": campaign.completed_at.isoformat() if campaign.completed_at else None,
        }

        # Include A/B variant breakdown when available
        variants = self.get_variants(campaign_id)
        if variants:
            stats["variants"] = [v.to_dict() for v in variants]

        return stats

    async def record_event(
        self,
        db_session: AsyncSession,
        campaign_id: uuid.UUID,
        event_type: str,
        *,
        revenue: float = 0.0,
        variant_id: str | None = None,
    ) -> None:
        """Record an open, click, or conversion event against a campaign.

        Parameters
        ----------
        event_type:
            One of ``"open"``, ``"click"``, ``"conversion"``.
        revenue:
            Revenue amount to attribute (only meaningful for conversions).
        variant_id:
            Optional variant ID if A/B testing is active.
        """
        campaign = await self._get_or_raise(db_session, campaign_id)

        if event_type == "open":
            campaign.opened_count = (campaign.opened_count or 0) + 1
        elif event_type == "click":
            campaign.clicked_count = (campaign.clicked_count or 0) + 1
        elif event_type == "conversion":
            campaign.converted_count = (campaign.converted_count or 0) + 1
            campaign.revenue_generated = (campaign.revenue_generated or 0.0) + revenue
        else:
            raise ValueError(f"Unknown campaign event type: {event_type}")

        # Update variant metrics when applicable
        if variant_id:
            for variant in self._variants.get(campaign_id, []):
                if variant.variant_id == variant_id:
                    if event_type == "open":
                        variant.opened_count += 1
                    elif event_type == "click":
                        variant.clicked_count += 1
                    elif event_type == "conversion":
                        variant.converted_count += 1
                        variant.revenue += revenue
                    break

        await db_session.flush()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _get_or_raise(
        self,
        db_session: AsyncSession,
        campaign_id: uuid.UUID,
    ) -> Campaign:
        """Return a campaign or raise ``ValueError``."""
        campaign = await self.get_campaign(db_session, campaign_id)
        if campaign is None:
            raise ValueError(f"Campaign {campaign_id} not found")
        return campaign

    async def _filter_by_consent(
        self,
        db_session: AsyncSession,
        customers: list[Customer],
        campaign_type: str,
    ) -> list[Customer]:
        """Filter customers to only those who have opted in to the campaign channel."""
        channel_map: dict[str, str] = {
            CampaignType.EMAIL.value: "email",
            CampaignType.SMS.value: "sms",
            CampaignType.PUSH.value: "push",
        }
        channel = channel_map.get(campaign_type)
        if channel is None:
            # Webhooks / in-app do not require marketing consent
            return customers

        customer_ids = [c.id for c in customers]
        if not customer_ids:
            return []

        stmt = (
            select(MarketingConsent.customer_id)
            .where(
                MarketingConsent.customer_id.in_(customer_ids),
                MarketingConsent.channel == channel,
                MarketingConsent.opted_in.is_(True),
            )
        )
        result = await db_session.execute(stmt)
        consented_ids = {row[0] for row in result.fetchall()}

        consented = [c for c in customers if c.id in consented_ids]
        filtered_out = len(customers) - len(consented)
        if filtered_out:
            logger.info(
                "Filtered out %d customers without %s consent",
                filtered_out,
                channel,
            )
        return consented

    @staticmethod
    def _build_template_vars(customer: Customer, campaign: Campaign) -> dict[str, Any]:
        """Build template variables from customer and campaign data."""
        return {
            "customer_id": str(customer.id),
            "email": customer.email or "",
            "first_name": customer.first_name or "",
            "last_name": customer.last_name or "",
            "full_name": f"{customer.first_name or ''} {customer.last_name or ''}".strip(),
            "campaign_name": campaign.name,
            "sender_name": campaign.sender_name or "",
        }

    async def _send_to_customer(
        self,
        customer: Customer,
        campaign: Campaign,
        subject: str,
        body: str,
        template_vars: dict[str, Any],
    ) -> None:
        """Dispatch a message to a single customer via the appropriate channel."""
        campaign_type = campaign.campaign_type

        if campaign_type == CampaignType.EMAIL.value:
            if not customer.email:
                raise ValueError(f"Customer {customer.id} has no email address")
            await self._channel_sender.send_email(
                to=customer.email,
                subject=subject,
                body=body,
                template_vars=template_vars,
            )

        elif campaign_type == CampaignType.SMS.value:
            if not customer.phone:
                raise ValueError(f"Customer {customer.id} has no phone number")
            await self._channel_sender.send_sms(
                to=customer.phone,
                message=body,
                template_vars=template_vars,
            )

        elif campaign_type == CampaignType.PUSH.value:
            # Device token expected in metadata
            meta = customer.metadata_ or {}
            device_token = meta.get("device_token")
            if not device_token:
                raise ValueError(f"Customer {customer.id} has no device token")
            await self._channel_sender.send_push(
                device_token=device_token,
                title=subject,
                body=body,
                template_vars=template_vars,
            )

        elif campaign_type == CampaignType.WEBHOOK.value:
            meta = customer.metadata_ or {}
            webhook_url = meta.get("webhook_url")
            if not webhook_url:
                raise ValueError(f"Customer {customer.id} has no webhook URL")
            await self._channel_sender.send_webhook(
                url=webhook_url,
                payload={
                    "campaign_id": str(campaign.id),
                    "customer_id": str(customer.id),
                    "subject": subject,
                    "body": body,
                    **template_vars,
                },
            )

        else:
            raise ValueError(f"Unsupported campaign type: {campaign_type}")
