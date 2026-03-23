"""Analytics engine providing cohort analysis, funnel analysis, customer
journey mapping, and real-time engagement metrics.

All public methods are async and operate through SQLAlchemy async sessions so
they integrate naturally with the CDP's request lifecycle.
"""

from __future__ import annotations

import logging
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

import numpy as np
import pandas as pd
from sqlalchemy import case, distinct, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from cdp.models.customer import Customer, CustomerEvent
from cdp.models.tracking import PageView, TrackingSession

logger = logging.getLogger(__name__)


class AnalyticsEngine:
    """Compute behavioural analytics from customer and event data.

    All methods accept (and require) an ``AsyncSession`` so they can be
    called from API handlers or background jobs.

    Usage::

        engine = AnalyticsEngine()
        cohorts = await engine.cohort_analysis(session, start, end)
        funnel  = await engine.funnel_analysis(session, steps, date_range)
    """

    # ------------------------------------------------------------------
    # Cohort analysis
    # ------------------------------------------------------------------

    async def cohort_analysis(
        self,
        db_session: AsyncSession,
        start_date: date,
        end_date: date,
        cohort_type: str = "monthly",
    ) -> dict[str, Any]:
        """Run a retention-style cohort analysis.

        Customers are grouped into cohorts by their signup period.  For each
        subsequent period the percentage of the cohort that generated at
        least one event is reported.

        Parameters
        ----------
        db_session:
            Active async session.
        start_date / end_date:
            Date window for the analysis.
        cohort_type:
            ``"weekly"`` or ``"monthly"`` (default).

        Returns
        -------
        dict
            ``cohorts`` (list of cohort dicts), ``summary`` statistics.
        """
        logger.info(
            "Running %s cohort analysis from %s to %s",
            cohort_type, start_date, end_date,
        )

        # Determine the SQL truncation
        trunc = "month" if cohort_type == "monthly" else "week"

        # Fetch customers created in the window with their first event date
        customers_stmt = (
            select(
                Customer.id.label("customer_id"),
                func.date_trunc(trunc, Customer.created_at).label("cohort_period"),
            )
            .where(
                Customer.created_at >= datetime.combine(start_date, datetime.min.time(), tzinfo=timezone.utc),
                Customer.created_at < datetime.combine(end_date, datetime.min.time(), tzinfo=timezone.utc),
            )
        )
        customers_result = await db_session.execute(customers_stmt)
        customers_rows = customers_result.all()

        if not customers_rows:
            return {"cohorts": [], "summary": {"total_customers": 0}}

        customers_df = pd.DataFrame(customers_rows, columns=["customer_id", "cohort_period"])
        customer_ids = list(customers_df["customer_id"])

        # Fetch events for those customers
        events_stmt = (
            select(
                CustomerEvent.customer_id,
                func.date_trunc(trunc, CustomerEvent.timestamp).label("activity_period"),
            )
            .where(
                CustomerEvent.customer_id.in_(customer_ids),
                CustomerEvent.timestamp >= datetime.combine(start_date, datetime.min.time(), tzinfo=timezone.utc),
                CustomerEvent.timestamp < datetime.combine(end_date, datetime.min.time(), tzinfo=timezone.utc),
            )
        )
        events_result = await db_session.execute(events_stmt)
        events_rows = events_result.all()

        if not events_rows:
            cohort_sizes = customers_df.groupby("cohort_period").size().to_dict()
            return {
                "cohorts": [
                    {"cohort": str(k), "size": int(v), "retention": {}}
                    for k, v in cohort_sizes.items()
                ],
                "summary": {"total_customers": len(customer_ids)},
            }

        events_df = pd.DataFrame(events_rows, columns=["customer_id", "activity_period"])

        # Merge
        merged = events_df.merge(customers_df, on="customer_id")

        # Compute period offset
        if cohort_type == "monthly":
            merged["period_offset"] = (
                (merged["activity_period"].dt.year - merged["cohort_period"].dt.year) * 12
                + (merged["activity_period"].dt.month - merged["cohort_period"].dt.month)
            )
        else:
            merged["period_offset"] = (
                (merged["activity_period"] - merged["cohort_period"]).dt.days // 7
            )

        cohort_sizes = customers_df.groupby("cohort_period")["customer_id"].nunique()

        cohort_data = (
            merged.groupby(["cohort_period", "period_offset"])["customer_id"]
            .nunique()
            .reset_index()
            .rename(columns={"customer_id": "active_users"})
        )

        cohorts: list[dict[str, Any]] = []
        for cohort_period, group in cohort_data.groupby("cohort_period"):
            size = int(cohort_sizes.get(cohort_period, 0))
            retention: dict[int, float] = {}
            for _, row in group.iterrows():
                offset = int(row["period_offset"])
                retention[offset] = round(float(row["active_users"]) / max(size, 1), 4)

            cohorts.append({
                "cohort": str(cohort_period),
                "size": size,
                "retention": retention,
            })

        return {
            "cohorts": cohorts,
            "summary": {
                "total_customers": len(customer_ids),
                "total_cohorts": len(cohorts),
                "date_range": {"start": str(start_date), "end": str(end_date)},
                "cohort_type": cohort_type,
            },
        }

    # ------------------------------------------------------------------
    # Funnel analysis
    # ------------------------------------------------------------------

    async def funnel_analysis(
        self,
        db_session: AsyncSession,
        funnel_steps: list[str],
        date_range: tuple[date, date],
    ) -> dict[str, Any]:
        """Analyse conversion through an ordered sequence of event types.

        Parameters
        ----------
        db_session:
            Active async session.
        funnel_steps:
            Ordered list of ``event_type`` values representing the funnel
            (e.g. ``["page_view", "add_to_cart", "purchase"]``).
        date_range:
            ``(start_date, end_date)`` tuple bounding the analysis window.

        Returns
        -------
        dict
            Per-step counts, conversion rates, and overall conversion.
        """
        if len(funnel_steps) < 2:
            raise ValueError("funnel_steps must contain at least two steps.")

        start_dt = datetime.combine(date_range[0], datetime.min.time(), tzinfo=timezone.utc)
        end_dt = datetime.combine(date_range[1], datetime.min.time(), tzinfo=timezone.utc)

        logger.info("Running funnel analysis for steps %s", funnel_steps)

        steps_result: list[dict[str, Any]] = []
        prev_customers: set[uuid.UUID] | None = None

        for idx, step in enumerate(funnel_steps):
            stmt = (
                select(func.count(distinct(CustomerEvent.customer_id)))
                .where(
                    CustomerEvent.event_type == step,
                    CustomerEvent.timestamp >= start_dt,
                    CustomerEvent.timestamp < end_dt,
                )
            )

            # After the first step only count customers from the previous step
            if prev_customers is not None:
                stmt = stmt.where(CustomerEvent.customer_id.in_(prev_customers))

            count_result = await db_session.execute(stmt)
            count = count_result.scalar() or 0

            # Also fetch the actual customer IDs for the next iteration
            ids_stmt = (
                select(distinct(CustomerEvent.customer_id))
                .where(
                    CustomerEvent.event_type == step,
                    CustomerEvent.timestamp >= start_dt,
                    CustomerEvent.timestamp < end_dt,
                )
            )
            if prev_customers is not None:
                ids_stmt = ids_stmt.where(CustomerEvent.customer_id.in_(prev_customers))

            ids_result = await db_session.execute(ids_stmt)
            current_customers = {row[0] for row in ids_result.all()}

            conversion_rate = (
                round(count / len(prev_customers), 4)
                if prev_customers and len(prev_customers) > 0
                else 1.0 if idx == 0
                else 0.0
            )

            steps_result.append({
                "step": idx + 1,
                "event_type": step,
                "users": count,
                "conversion_rate": conversion_rate,
                "drop_off": (
                    len(prev_customers) - count if prev_customers else 0
                ),
            })

            prev_customers = current_customers

        first_count = steps_result[0]["users"] if steps_result else 0
        last_count = steps_result[-1]["users"] if steps_result else 0
        overall_conversion = (
            round(last_count / max(first_count, 1), 4)
        )

        return {
            "steps": steps_result,
            "overall_conversion": overall_conversion,
            "date_range": {
                "start": str(date_range[0]),
                "end": str(date_range[1]),
            },
        }

    # ------------------------------------------------------------------
    # Customer journey mapping
    # ------------------------------------------------------------------

    async def customer_journey(
        self,
        db_session: AsyncSession,
        customer_id: uuid.UUID,
        limit: int = 500,
    ) -> dict[str, Any]:
        """Build an ordered timeline of touchpoints for a customer.

        Parameters
        ----------
        db_session:
            Active async session.
        customer_id:
            UUID of the target customer.
        limit:
            Maximum number of events to return.

        Returns
        -------
        dict
            ``customer_id``, ``journey`` list of event dicts, and summary
            statistics.
        """
        logger.info("Mapping journey for customer %s", customer_id)

        # Events
        events_stmt = (
            select(CustomerEvent)
            .where(CustomerEvent.customer_id == customer_id)
            .order_by(CustomerEvent.timestamp.asc())
            .limit(limit)
        )
        events_result = await db_session.execute(events_stmt)
        events = events_result.scalars().all()

        # Page views (via sessions)
        sessions_stmt = (
            select(TrackingSession)
            .where(TrackingSession.customer_id == customer_id)
            .order_by(TrackingSession.started_at.asc())
        )
        sessions_result = await db_session.execute(sessions_stmt)
        sessions = sessions_result.scalars().all()

        journey: list[dict[str, Any]] = []

        for event in events:
            journey.append({
                "type": "event",
                "event_type": event.event_type,
                "event_name": event.event_name,
                "timestamp": event.timestamp.isoformat() if event.timestamp else None,
                "properties": event.properties,
                "source": event.source,
                "channel": event.source,
            })

        for session in sessions:
            journey.append({
                "type": "session",
                "session_id": str(session.session_id),
                "started_at": session.started_at.isoformat() if session.started_at else None,
                "ended_at": session.ended_at.isoformat() if session.ended_at else None,
                "timestamp": session.started_at.isoformat() if session.started_at else None,
                "page_views": session.page_views,
                "device_type": session.device_type,
                "browser": session.browser,
                "country": session.country,
                "utm_source": session.utm_source,
                "utm_medium": session.utm_medium,
                "utm_campaign": session.utm_campaign,
            })

        # Sort combined journey by timestamp
        journey.sort(key=lambda t: t.get("timestamp") or "")

        # Summary
        event_types = [t["event_type"] for t in journey if t["type"] == "event"]
        unique_channels = list({t.get("channel") or t.get("utm_source") or "direct" for t in journey})

        first_touch = journey[0]["timestamp"] if journey else None
        last_touch = journey[-1]["timestamp"] if journey else None

        return {
            "customer_id": str(customer_id),
            "touchpoints": len(journey),
            "journey": journey,
            "summary": {
                "total_events": len(events),
                "total_sessions": len(sessions),
                "unique_event_types": len(set(event_types)),
                "channels": unique_channels,
                "first_touch": first_touch,
                "last_touch": last_touch,
            },
        }

    # ------------------------------------------------------------------
    # Engagement & health scores
    # ------------------------------------------------------------------

    async def compute_engagement_score(
        self,
        db_session: AsyncSession,
        customer_id: uuid.UUID,
        lookback_days: int = 90,
    ) -> dict[str, Any]:
        """Compute a 0-100 engagement score for a customer.

        The score is a weighted composite of:
        * Event frequency (40 %)
        * Session frequency (20 %)
        * Recency of last event (25 %)
        * Diversity of event types (15 %)

        Parameters
        ----------
        db_session:
            Active async session.
        customer_id:
            Target customer UUID.
        lookback_days:
            Window over which activity is measured.

        Returns
        -------
        dict
            ``engagement_score``, ``components``, ``updated_at``.
        """
        cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)

        # Event count
        event_count_result = await db_session.execute(
            select(func.count())
            .select_from(CustomerEvent)
            .where(
                CustomerEvent.customer_id == customer_id,
                CustomerEvent.timestamp >= cutoff,
            )
        )
        event_count = event_count_result.scalar() or 0

        # Session count
        session_count_result = await db_session.execute(
            select(func.count())
            .select_from(TrackingSession)
            .where(
                TrackingSession.customer_id == customer_id,
                TrackingSession.started_at >= cutoff,
            )
        )
        session_count = session_count_result.scalar() or 0

        # Recency (days since last event)
        last_event_result = await db_session.execute(
            select(func.max(CustomerEvent.timestamp))
            .where(CustomerEvent.customer_id == customer_id)
        )
        last_event_ts = last_event_result.scalar()
        if last_event_ts:
            recency_days = (datetime.now(timezone.utc) - last_event_ts.replace(tzinfo=timezone.utc)).days
        else:
            recency_days = lookback_days

        # Event type diversity
        diversity_result = await db_session.execute(
            select(func.count(distinct(CustomerEvent.event_type)))
            .where(
                CustomerEvent.customer_id == customer_id,
                CustomerEvent.timestamp >= cutoff,
            )
        )
        event_type_diversity = diversity_result.scalar() or 0

        # Normalise components to 0-100
        # Event frequency: cap at 100 events in the window
        event_score = min(event_count / 100.0, 1.0) * 100

        # Session frequency: cap at 50 sessions
        session_score = min(session_count / 50.0, 1.0) * 100

        # Recency: 0 days = 100, lookback_days = 0
        recency_score = max(1.0 - (recency_days / lookback_days), 0.0) * 100

        # Diversity: cap at 10 event types
        diversity_score = min(event_type_diversity / 10.0, 1.0) * 100

        engagement = round(
            event_score * 0.40
            + session_score * 0.20
            + recency_score * 0.25
            + diversity_score * 0.15,
            2,
        )

        # Persist to customer record
        customer_result = await db_session.execute(
            select(Customer).where(Customer.id == customer_id)
        )
        customer = customer_result.scalar_one_or_none()
        if customer:
            customer.engagement_score = engagement
            await db_session.flush()

        return {
            "customer_id": str(customer_id),
            "engagement_score": engagement,
            "components": {
                "event_frequency": round(event_score, 2),
                "session_frequency": round(session_score, 2),
                "recency": round(recency_score, 2),
                "diversity": round(diversity_score, 2),
            },
            "raw": {
                "event_count": event_count,
                "session_count": session_count,
                "recency_days": recency_days,
                "event_type_diversity": event_type_diversity,
            },
            "lookback_days": lookback_days,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }

    async def compute_health_score(
        self,
        db_session: AsyncSession,
        customer_id: uuid.UUID,
        lookback_days: int = 90,
    ) -> dict[str, Any]:
        """Compute a 0-100 customer health score.

        Combines engagement, purchase behaviour, and support interaction
        signals into a single indicator:
        * Engagement score (40 %)
        * Purchase recency (30 %)
        * Purchase frequency trend (20 %)
        * Support ticket absence bonus (10 %)

        Parameters
        ----------
        db_session:
            Active async session.
        customer_id:
            Target customer UUID.
        lookback_days:
            Analysis window.

        Returns
        -------
        dict
            ``health_score``, ``components``, ``risk_level``.
        """
        cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)

        # Re-use engagement score
        engagement = await self.compute_engagement_score(
            db_session, customer_id, lookback_days
        )
        engagement_score = engagement["engagement_score"]

        # Purchase recency
        last_purchase_result = await db_session.execute(
            select(func.max(CustomerEvent.timestamp))
            .where(
                CustomerEvent.customer_id == customer_id,
                CustomerEvent.event_type.in_(["purchase", "order", "transaction"]),
            )
        )
        last_purchase_ts = last_purchase_result.scalar()
        if last_purchase_ts:
            purchase_recency_days = (
                datetime.now(timezone.utc) - last_purchase_ts.replace(tzinfo=timezone.utc)
            ).days
        else:
            purchase_recency_days = lookback_days

        purchase_recency_score = max(1.0 - (purchase_recency_days / lookback_days), 0.0) * 100

        # Purchase frequency trend: compare last half-window to first half
        half = timedelta(days=lookback_days // 2)
        midpoint = datetime.now(timezone.utc) - half

        recent_count_result = await db_session.execute(
            select(func.count())
            .select_from(CustomerEvent)
            .where(
                CustomerEvent.customer_id == customer_id,
                CustomerEvent.event_type.in_(["purchase", "order", "transaction"]),
                CustomerEvent.timestamp >= midpoint,
            )
        )
        recent_purchases = recent_count_result.scalar() or 0

        earlier_count_result = await db_session.execute(
            select(func.count())
            .select_from(CustomerEvent)
            .where(
                CustomerEvent.customer_id == customer_id,
                CustomerEvent.event_type.in_(["purchase", "order", "transaction"]),
                CustomerEvent.timestamp >= cutoff,
                CustomerEvent.timestamp < midpoint,
            )
        )
        earlier_purchases = earlier_count_result.scalar() or 0

        if earlier_purchases > 0:
            trend_ratio = recent_purchases / earlier_purchases
            trend_score = min(trend_ratio, 2.0) / 2.0 * 100
        elif recent_purchases > 0:
            trend_score = 100.0
        else:
            trend_score = 0.0

        # Support ticket absence bonus (no negative events = bonus)
        support_count_result = await db_session.execute(
            select(func.count())
            .select_from(CustomerEvent)
            .where(
                CustomerEvent.customer_id == customer_id,
                CustomerEvent.event_type.in_(["support_ticket", "complaint", "refund"]),
                CustomerEvent.timestamp >= cutoff,
            )
        )
        support_count = support_count_result.scalar() or 0
        support_bonus = max(1.0 - (support_count / 5.0), 0.0) * 100

        health = round(
            engagement_score * 0.40
            + purchase_recency_score * 0.30
            + trend_score * 0.20
            + support_bonus * 0.10,
            2,
        )

        # Risk classification
        if health >= 75:
            risk_level = "low"
        elif health >= 45:
            risk_level = "medium"
        else:
            risk_level = "high"

        # Persist
        customer_result = await db_session.execute(
            select(Customer).where(Customer.id == customer_id)
        )
        customer = customer_result.scalar_one_or_none()
        if customer:
            customer.risk_score = round(100.0 - health, 2)
            await db_session.flush()

        return {
            "customer_id": str(customer_id),
            "health_score": health,
            "risk_level": risk_level,
            "components": {
                "engagement": round(engagement_score, 2),
                "purchase_recency": round(purchase_recency_score, 2),
                "purchase_trend": round(trend_score, 2),
                "support_bonus": round(support_bonus, 2),
            },
            "raw": {
                "recent_purchases": recent_purchases,
                "earlier_purchases": earlier_purchases,
                "purchase_recency_days": purchase_recency_days,
                "support_tickets": support_count,
            },
            "lookback_days": lookback_days,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }

    # ------------------------------------------------------------------
    # Real-time metrics
    # ------------------------------------------------------------------

    async def active_users(
        self,
        db_session: AsyncSession,
        window_minutes: int = 30,
    ) -> dict[str, Any]:
        """Count customers with activity in the last *window_minutes*.

        Returns
        -------
        dict
            ``active_users`` count, ``window_minutes``, and ``as_of``
            timestamp.
        """
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=window_minutes)

        result = await db_session.execute(
            select(func.count(distinct(CustomerEvent.customer_id)))
            .where(CustomerEvent.timestamp >= cutoff)
        )
        count = result.scalar() or 0

        session_result = await db_session.execute(
            select(func.count(distinct(TrackingSession.customer_id)))
            .where(
                TrackingSession.is_active.is_(True),
                TrackingSession.started_at >= cutoff,
            )
        )
        session_count = session_result.scalar() or 0

        return {
            "active_users_events": count,
            "active_users_sessions": session_count,
            "window_minutes": window_minutes,
            "as_of": datetime.now(timezone.utc).isoformat(),
        }

    async def event_counts(
        self,
        db_session: AsyncSession,
        window_minutes: int = 60,
        group_by: str = "event_type",
    ) -> dict[str, Any]:
        """Aggregate event counts over a recent time window.

        Parameters
        ----------
        db_session:
            Active async session.
        window_minutes:
            Lookback window.
        group_by:
            Column to group by (``"event_type"`` or ``"source"``).

        Returns
        -------
        dict
            ``total`` count, ``breakdown`` dict, and metadata.
        """
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=window_minutes)

        group_col = (
            CustomerEvent.event_type
            if group_by == "event_type"
            else CustomerEvent.source
        )

        stmt = (
            select(group_col, func.count().label("cnt"))
            .where(CustomerEvent.timestamp >= cutoff)
            .group_by(group_col)
            .order_by(func.count().desc())
        )

        result = await db_session.execute(stmt)
        rows = result.all()

        breakdown = {str(row[0]): int(row[1]) for row in rows}
        total = sum(breakdown.values())

        return {
            "total": total,
            "breakdown": breakdown,
            "group_by": group_by,
            "window_minutes": window_minutes,
            "as_of": datetime.now(timezone.utc).isoformat(),
        }

    async def conversion_rates(
        self,
        db_session: AsyncSession,
        conversion_event: str = "purchase",
        window_days: int = 30,
    ) -> dict[str, Any]:
        """Compute the conversion rate over a rolling window.

        Conversion rate = unique customers with *conversion_event* /
        total unique active customers.

        Parameters
        ----------
        db_session:
            Active async session.
        conversion_event:
            Event type that counts as a conversion.
        window_days:
            Lookback window in days.

        Returns
        -------
        dict
            ``conversion_rate``, ``converters``, ``total_active``.
        """
        cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)

        total_result = await db_session.execute(
            select(func.count(distinct(CustomerEvent.customer_id)))
            .where(CustomerEvent.timestamp >= cutoff)
        )
        total_active = total_result.scalar() or 0

        converters_result = await db_session.execute(
            select(func.count(distinct(CustomerEvent.customer_id)))
            .where(
                CustomerEvent.event_type == conversion_event,
                CustomerEvent.timestamp >= cutoff,
            )
        )
        converters = converters_result.scalar() or 0

        rate = round(converters / max(total_active, 1), 4)

        return {
            "conversion_rate": rate,
            "converters": converters,
            "total_active": total_active,
            "conversion_event": conversion_event,
            "window_days": window_days,
            "as_of": datetime.now(timezone.utc).isoformat(),
        }
