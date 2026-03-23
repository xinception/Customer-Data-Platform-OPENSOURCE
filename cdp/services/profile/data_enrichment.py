"""Data enrichment service -- compute derived scores and enrich profiles."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Awaitable

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from cdp.models.customer import Customer, CustomerAttribute, CustomerEvent

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enrichment step type
# ---------------------------------------------------------------------------

EnrichmentStep = Callable[[AsyncSession, Customer], Awaitable[dict[str, Any]]]


class DataEnrichmentService:
    """Enrich customer profiles with derived and third-party data.

    Responsibilities
    ----------------
    * Compute lifetime value, engagement score, recency score.
    * Derive customer preferences from behavioural events.
    * Provide placeholder hooks for social and firmographic enrichment.
    * Run an ordered enrichment pipeline across all (or selected) profiles.
    """

    def __init__(self) -> None:
        # Ordered list of enrichment steps executed by the pipeline.
        self._pipeline: list[tuple[str, EnrichmentStep]] = [
            ("lifetime_value", self._step_lifetime_value),
            ("engagement_score", self._step_engagement_score),
            ("recency_score", self._step_recency_score),
            ("preferences", self._step_preferences),
            ("social", self._step_social_enrichment),
            ("firmographic", self._step_firmographic_enrichment),
        ]

    # ------------------------------------------------------------------
    # Pipeline management
    # ------------------------------------------------------------------

    @property
    def pipeline(self) -> list[tuple[str, EnrichmentStep]]:
        """Return the current enrichment pipeline (name, callable) pairs."""
        return list(self._pipeline)

    def register_step(self, name: str, step: EnrichmentStep, position: int | None = None) -> None:
        """Add a custom enrichment step to the pipeline.

        Parameters
        ----------
        name:
            Human-readable label for logging.
        step:
            An async callable ``(db, customer) -> dict`` returning enrichment
            data to merge into the customer metadata.
        position:
            Insert position (0-based).  Appended at the end when ``None``.
        """
        entry = (name, step)
        if position is None:
            self._pipeline.append(entry)
        else:
            self._pipeline.insert(position, entry)
        logger.info("Registered enrichment step '%s' at position %s", name, position)

    def remove_step(self, name: str) -> bool:
        """Remove the first step matching *name*.  Returns ``True`` if found."""
        for idx, (n, _) in enumerate(self._pipeline):
            if n == name:
                self._pipeline.pop(idx)
                return True
        return False

    # ------------------------------------------------------------------
    # Individual enrichment methods
    # ------------------------------------------------------------------

    async def compute_lifetime_value(
        self,
        db: AsyncSession,
        customer_id: str | uuid.UUID,
    ) -> float:
        """Calculate and persist the customer lifetime value (CLV).

        CLV is approximated by summing ``revenue`` from purchase events.
        """
        cid = uuid.UUID(str(customer_id))
        stmt = (
            select(CustomerEvent)
            .where(
                CustomerEvent.customer_id == cid,
                CustomerEvent.event_type.in_(["purchase", "order", "transaction"]),
            )
        )
        result = await db.execute(stmt)
        events = result.scalars().all()

        ltv = sum(
            float(e.properties.get("revenue", 0) or 0)
            for e in events
            if e.properties
        )
        ltv = round(ltv, 2)

        await db.execute(
            update(Customer).where(Customer.id == cid).values(lifetime_value=ltv)
        )
        await db.flush()
        logger.info("Computed LTV for %s: %.2f", customer_id, ltv)
        return ltv

    async def compute_engagement_score(
        self,
        db: AsyncSession,
        customer_id: str | uuid.UUID,
    ) -> float:
        """Compute a 0--100 engagement score based on event frequency and recency.

        Scoring heuristic
        -----------------
        * Base: number of events in the last 90 days (capped at 50 contributing
          to 60 points).
        * Recency bonus: up to 20 points based on days since last event.
        * Variety bonus: up to 20 points based on distinct event types.
        """
        cid = uuid.UUID(str(customer_id))
        ninety_days_ago = datetime.now(timezone.utc) - timedelta(days=90)

        # Event count in last 90 days
        count_stmt = (
            select(func.count())
            .select_from(CustomerEvent)
            .where(
                CustomerEvent.customer_id == cid,
                CustomerEvent.timestamp >= ninety_days_ago,
            )
        )
        count_result = await db.execute(count_stmt)
        event_count = count_result.scalar() or 0

        # Most-recent event timestamp
        recent_stmt = (
            select(func.max(CustomerEvent.timestamp))
            .where(CustomerEvent.customer_id == cid)
        )
        recent_result = await db.execute(recent_stmt)
        last_event_ts = recent_result.scalar()

        # Distinct event types in last 90 days
        variety_stmt = (
            select(func.count(CustomerEvent.event_type.distinct()))
            .where(
                CustomerEvent.customer_id == cid,
                CustomerEvent.timestamp >= ninety_days_ago,
            )
        )
        variety_result = await db.execute(variety_stmt)
        event_type_count = variety_result.scalar() or 0

        # --- Score calculation ---
        frequency_score = min(event_count / 50.0, 1.0) * 60.0

        recency_score = 0.0
        if last_event_ts is not None:
            days_since = (datetime.now(timezone.utc) - last_event_ts.replace(tzinfo=timezone.utc)).days
            if days_since <= 7:
                recency_score = 20.0
            elif days_since <= 30:
                recency_score = 15.0
            elif days_since <= 90:
                recency_score = 8.0

        variety_score = min(event_type_count / 5.0, 1.0) * 20.0

        total = round(min(frequency_score + recency_score + variety_score, 100.0), 2)

        await db.execute(
            update(Customer).where(Customer.id == cid).values(engagement_score=total)
        )
        await db.flush()
        logger.info("Computed engagement score for %s: %.2f", customer_id, total)
        return total

    async def compute_recency_score(
        self,
        db: AsyncSession,
        customer_id: str | uuid.UUID,
    ) -> float:
        """Return a 0--1 recency score (1 = very recent activity).

        Uses an exponential decay with a half-life of 30 days.
        """
        import math

        cid = uuid.UUID(str(customer_id))
        stmt = (
            select(func.max(CustomerEvent.timestamp))
            .where(CustomerEvent.customer_id == cid)
        )
        result = await db.execute(stmt)
        last_ts = result.scalar()

        if last_ts is None:
            return 0.0

        days_since = max(
            (datetime.now(timezone.utc) - last_ts.replace(tzinfo=timezone.utc)).total_seconds() / 86400.0,
            0.0,
        )
        half_life = 30.0
        score = round(math.exp(-math.log(2) * days_since / half_life), 4)
        logger.debug("Recency score for %s: %.4f (days_since=%.1f)", customer_id, score, days_since)
        return score

    async def derive_preferences(
        self,
        db: AsyncSession,
        customer_id: str | uuid.UUID,
    ) -> dict[str, Any]:
        """Derive customer preferences from behavioural event properties.

        Aggregates ``category``, ``brand``, and ``tag`` values from event
        properties to build a frequency-ranked preference profile.
        """
        cid = uuid.UUID(str(customer_id))
        stmt = (
            select(CustomerEvent)
            .where(CustomerEvent.customer_id == cid)
            .order_by(CustomerEvent.timestamp.desc())
            .limit(500)
        )
        result = await db.execute(stmt)
        events = result.scalars().all()

        categories: dict[str, int] = {}
        brands: dict[str, int] = {}
        tags: dict[str, int] = {}

        for event in events:
            props = event.properties or {}
            if cat := props.get("category"):
                categories[cat] = categories.get(cat, 0) + 1
            if brand := props.get("brand"):
                brands[brand] = brands.get(brand, 0) + 1
            for tag in props.get("tags", []):
                if isinstance(tag, str):
                    tags[tag] = tags.get(tag, 0) + 1

        def _top_n(counter: dict[str, int], n: int = 10) -> list[dict[str, Any]]:
            return [
                {"name": k, "count": v}
                for k, v in sorted(counter.items(), key=lambda x: x[1], reverse=True)[:n]
            ]

        preferences = {
            "top_categories": _top_n(categories),
            "top_brands": _top_n(brands),
            "top_tags": _top_n(tags),
            "total_events_analysed": len(events),
        }

        # Persist preferences in metadata
        customer_stmt = select(Customer).where(Customer.id == cid)
        cust_result = await db.execute(customer_stmt)
        customer = cust_result.scalars().first()
        if customer:
            customer.metadata_ = {
                **(customer.metadata_ or {}),
                "preferences": preferences,
                "preferences_updated_at": datetime.now(timezone.utc).isoformat(),
            }
            await db.flush()

        logger.info("Derived preferences for %s: %d categories, %d brands", customer_id, len(categories), len(brands))
        return preferences

    # ------------------------------------------------------------------
    # Placeholder enrichment hooks
    # ------------------------------------------------------------------

    async def enrich_social_data(
        self,
        db: AsyncSession,
        customer_id: str | uuid.UUID,
    ) -> dict[str, Any]:
        """Placeholder for social media data enrichment.

        In a production system this would call external APIs (e.g. Clearbit,
        FullContact) to pull social profiles, follower counts, etc.
        """
        logger.info("Social enrichment called for %s (placeholder)", customer_id)
        return {
            "social_profiles": [],
            "enrichment_status": "not_configured",
            "message": "Social enrichment requires an external provider integration.",
        }

    async def enrich_firmographic_data(
        self,
        db: AsyncSession,
        customer_id: str | uuid.UUID,
    ) -> dict[str, Any]:
        """Placeholder for B2B firmographic enrichment.

        Would typically pull company size, industry, revenue, tech stack from
        providers like ZoomInfo, Clearbit, or Apollo.
        """
        logger.info("Firmographic enrichment called for %s (placeholder)", customer_id)
        return {
            "company": None,
            "industry": None,
            "employee_count": None,
            "annual_revenue": None,
            "enrichment_status": "not_configured",
            "message": "Firmographic enrichment requires an external provider integration.",
        }

    # ------------------------------------------------------------------
    # Pipeline step wrappers (used by the ordered pipeline)
    # ------------------------------------------------------------------

    async def _step_lifetime_value(self, db: AsyncSession, customer: Customer) -> dict[str, Any]:
        ltv = await self.compute_lifetime_value(db, customer.id)
        return {"lifetime_value": ltv}

    async def _step_engagement_score(self, db: AsyncSession, customer: Customer) -> dict[str, Any]:
        score = await self.compute_engagement_score(db, customer.id)
        return {"engagement_score": score}

    async def _step_recency_score(self, db: AsyncSession, customer: Customer) -> dict[str, Any]:
        score = await self.compute_recency_score(db, customer.id)
        return {"recency_score": score}

    async def _step_preferences(self, db: AsyncSession, customer: Customer) -> dict[str, Any]:
        prefs = await self.derive_preferences(db, customer.id)
        return {"preferences": prefs}

    async def _step_social_enrichment(self, db: AsyncSession, customer: Customer) -> dict[str, Any]:
        return await self.enrich_social_data(db, customer.id)

    async def _step_firmographic_enrichment(self, db: AsyncSession, customer: Customer) -> dict[str, Any]:
        return await self.enrich_firmographic_data(db, customer.id)

    # ------------------------------------------------------------------
    # Batch enrichment
    # ------------------------------------------------------------------

    async def enrich_all_profiles(
        self,
        db: AsyncSession,
        batch_size: int = 100,
        active_only: bool = True,
    ) -> dict[str, Any]:
        """Run the full enrichment pipeline across all customer profiles.

        Parameters
        ----------
        db:
            Active async database session.
        batch_size:
            Number of profiles to process per query batch.
        active_only:
            When ``True`` (default), skip inactive customers.

        Returns
        -------
        dict
            Summary with ``enriched``, ``errors``, and ``error_details``.
        """
        stmt = select(Customer)
        if active_only:
            stmt = stmt.where(Customer.is_active.is_(True))

        result = await db.execute(stmt)
        customers = result.scalars().all()

        enriched = 0
        error_count = 0
        error_details: list[dict[str, Any]] = []

        for customer in customers:
            try:
                await self._run_pipeline(db, customer)
                enriched += 1
            except Exception as exc:
                error_count += 1
                error_details.append({"customer_id": str(customer.id), "error": str(exc)})
                logger.warning("Enrichment failed for %s: %s", customer.id, exc)

        await db.flush()
        summary = {
            "enriched": enriched,
            "errors": error_count,
            "error_details": error_details,
            "pipeline_steps": [name for name, _ in self._pipeline],
        }
        logger.info("Batch enrichment complete: %d enriched, %d errors", enriched, error_count)
        return summary

    async def _run_pipeline(self, db: AsyncSession, customer: Customer) -> dict[str, Any]:
        """Execute every registered pipeline step for a single customer."""
        combined: dict[str, Any] = {}
        for step_name, step_fn in self._pipeline:
            try:
                result = await step_fn(db, customer)
                combined[step_name] = result
            except Exception:
                logger.exception("Pipeline step '%s' failed for customer %s", step_name, customer.id)
                combined[step_name] = {"error": True}

        # Store pipeline run metadata
        customer.metadata_ = {
            **(customer.metadata_ or {}),
            "last_enrichment_run": datetime.now(timezone.utc).isoformat(),
            "enrichment_results": {k: "ok" if "error" not in v else "error" for k, v in combined.items()},
        }
        return combined
