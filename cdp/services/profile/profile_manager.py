"""Unified customer profile management -- the 360-degree view."""

from __future__ import annotations

import csv
import io
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from cdp.models.customer import (
    Customer,
    CustomerAttribute,
    CustomerEvent,
    CustomerIdentity,
)
from cdp.utils.helpers import generate_uuid, mask_pii, paginate, validate_email

logger = logging.getLogger(__name__)

# Fields that contribute to the completeness score and their weights.
_COMPLETENESS_FIELDS: dict[str, float] = {
    "email": 0.20,
    "phone": 0.10,
    "first_name": 0.15,
    "last_name": 0.15,
    "date_of_birth": 0.10,
    "gender": 0.05,
    "avatar_url": 0.05,
    "segment": 0.10,
    "lifetime_value": 0.10,
}


class ProfileManager:
    """Provides CRUD, search, enrichment, timeline, and bulk operations for customer profiles."""

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    async def create_profile(
        self,
        db: AsyncSession,
        customer_data: dict[str, Any],
    ) -> Customer:
        """Create a new customer profile.

        Parameters
        ----------
        db:
            Active async database session.
        customer_data:
            Dictionary of column values.  ``id`` is generated automatically
            when not provided.

        Returns
        -------
        Customer
            The newly-persisted customer ORM instance.
        """
        if "email" in customer_data and customer_data["email"]:
            if not validate_email(customer_data["email"]):
                raise ValueError(f"Invalid email: {customer_data['email']}")

        customer_data.setdefault("id", uuid.UUID(generate_uuid()))
        customer = Customer(**customer_data)
        db.add(customer)
        await db.flush()
        await db.refresh(customer)
        logger.info("Created profile %s", customer.id)
        return customer

    async def update_profile(
        self,
        db: AsyncSession,
        customer_id: str | uuid.UUID,
        updates: dict[str, Any],
    ) -> Customer | None:
        """Update an existing customer profile.

        Returns ``None`` when the customer does not exist.
        """
        cid = uuid.UUID(str(customer_id))
        stmt = (
            update(Customer)
            .where(Customer.id == cid)
            .values(**updates, updated_at=datetime.now(timezone.utc))
            .returning(Customer)
        )
        result = await db.execute(stmt)
        row = result.scalars().first()
        if row is None:
            logger.warning("Profile %s not found for update", customer_id)
            return None
        await db.flush()
        logger.info("Updated profile %s", customer_id)
        return row

    async def get_profile(
        self,
        db: AsyncSession,
        customer_id: str | uuid.UUID,
    ) -> Customer | None:
        """Fetch a single customer by primary key."""
        cid = uuid.UUID(str(customer_id))
        stmt = select(Customer).where(Customer.id == cid)
        result = await db.execute(stmt)
        return result.scalars().first()

    async def delete_profile(
        self,
        db: AsyncSession,
        customer_id: str | uuid.UUID,
    ) -> bool:
        """Hard-delete a customer and all related rows (cascade).

        Returns ``True`` when a row was actually deleted.
        """
        cid = uuid.UUID(str(customer_id))
        stmt = delete(Customer).where(Customer.id == cid)
        result = await db.execute(stmt)
        await db.flush()
        deleted = result.rowcount > 0  # type: ignore[union-attr]
        if deleted:
            logger.info("Deleted profile %s", customer_id)
        return deleted

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    async def search_profiles(
        self,
        db: AsyncSession,
        query: str | None = None,
        filters: dict[str, Any] | None = None,
        page: int = 1,
        size: int = 20,
    ) -> dict[str, Any]:
        """Search customer profiles with optional free-text query and filters.

        Parameters
        ----------
        query:
            Free-text search applied against ``email``, ``first_name``, and
            ``last_name`` using SQL ``ILIKE``.
        filters:
            Exact-match filters keyed by column name (e.g.
            ``{"segment": "vip", "is_active": True}``).
        page / size:
            Pagination controls.

        Returns
        -------
        dict
            ``{"items": [...], "total": int, "page": int, "size": int}``
        """
        stmt = select(Customer)
        count_stmt = select(func.count()).select_from(Customer)

        if query:
            pattern = f"%{query}%"
            text_filter = or_(
                Customer.email.ilike(pattern),
                Customer.first_name.ilike(pattern),
                Customer.last_name.ilike(pattern),
            )
            stmt = stmt.where(text_filter)
            count_stmt = count_stmt.where(text_filter)

        if filters:
            for col_name, value in filters.items():
                col = getattr(Customer, col_name, None)
                if col is not None:
                    stmt = stmt.where(col == value)
                    count_stmt = count_stmt.where(col == value)

        total_result = await db.execute(count_stmt)
        total = total_result.scalar() or 0

        stmt = stmt.order_by(Customer.updated_at.desc())
        stmt = paginate(stmt, page, size)

        result = await db.execute(stmt)
        items = list(result.scalars().all())

        return {"items": items, "total": total, "page": page, "size": size}

    # ------------------------------------------------------------------
    # Profile enrichment (pulls all sources into one view)
    # ------------------------------------------------------------------

    async def enrich_profile(
        self,
        db: AsyncSession,
        customer_id: str | uuid.UUID,
    ) -> dict[str, Any]:
        """Pull data from all related tables and compute derived scores.

        This is the lightweight, single-profile variant.  See
        :class:`~cdp.services.profile.data_enrichment.DataEnrichmentService`
        for the batch enrichment pipeline.
        """
        profile = await self.get_full_profile(db, customer_id)
        if profile is None:
            raise ValueError(f"Customer {customer_id} not found")

        # Compute a completeness score and persist it in metadata
        score = self.compute_completeness_score(profile["customer"])
        await self.update_profile(
            db,
            customer_id,
            {"metadata_": {**(profile["customer"].metadata_ or {}), "completeness_score": score}},
        )
        profile["completeness_score"] = score
        logger.info("Enriched profile %s (completeness=%0.2f)", customer_id, score)
        return profile

    # ------------------------------------------------------------------
    # Full profile (360-degree view)
    # ------------------------------------------------------------------

    async def get_full_profile(
        self,
        db: AsyncSession,
        customer_id: str | uuid.UUID,
    ) -> dict[str, Any] | None:
        """Return a 360-degree dictionary for *customer_id*.

        Includes:
        - customer (ORM object)
        - events, identities, attributes
        - segments, predictions, campaign_history (from metadata)
        - completeness_score
        """
        cid = uuid.UUID(str(customer_id))
        stmt = (
            select(Customer)
            .where(Customer.id == cid)
            .options(
                selectinload(Customer.events),
                selectinload(Customer.identities),
                selectinload(Customer.attributes),
            )
        )
        result = await db.execute(stmt)
        customer = result.scalars().first()
        if customer is None:
            return None

        meta = customer.metadata_ or {}
        return {
            "customer": customer,
            "events": list(customer.events),
            "identities": list(customer.identities),
            "attributes": list(customer.attributes),
            "segments": meta.get("segments", []),
            "predictions": meta.get("predictions", {}),
            "campaign_history": meta.get("campaign_history", []),
            "completeness_score": self.compute_completeness_score(customer),
        }

    # ------------------------------------------------------------------
    # Completeness
    # ------------------------------------------------------------------

    @staticmethod
    def compute_completeness_score(customer: Customer) -> float:
        """Return a 0-1 score indicating how complete the profile is.

        Each field defined in ``_COMPLETENESS_FIELDS`` contributes its
        weight when the value is non-null and non-empty.
        """
        score = 0.0
        for field, weight in _COMPLETENESS_FIELDS.items():
            value = getattr(customer, field, None)
            if value is not None and value != "" and value != 0:
                score += weight
        return round(min(score, 1.0), 4)

    # ------------------------------------------------------------------
    # Timeline
    # ------------------------------------------------------------------

    async def get_customer_timeline(
        self,
        db: AsyncSession,
        customer_id: str | uuid.UUID,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Return a reverse-chronological timeline of events.

        Each entry is a lightweight dict suitable for API serialisation.
        """
        cid = uuid.UUID(str(customer_id))
        stmt = (
            select(CustomerEvent)
            .where(CustomerEvent.customer_id == cid)
            .order_by(CustomerEvent.timestamp.desc())
            .limit(min(limit, 500))
        )
        result = await db.execute(stmt)
        events = result.scalars().all()
        return [
            {
                "id": str(e.id),
                "event_type": e.event_type,
                "event_name": e.event_name,
                "properties": e.properties,
                "source": e.source,
                "timestamp": e.timestamp.isoformat() if e.timestamp else None,
            }
            for e in events
        ]

    # ------------------------------------------------------------------
    # Bulk operations
    # ------------------------------------------------------------------

    async def bulk_import(
        self,
        db: AsyncSession,
        customers_data: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Import many customer records in a single transaction.

        Returns a summary dict with ``created``, ``errors`` counts and
        a list of ``error_details``.
        """
        created = 0
        errors = 0
        error_details: list[dict[str, Any]] = []

        for idx, row in enumerate(customers_data):
            try:
                await self.create_profile(db, row)
                created += 1
            except Exception as exc:
                errors += 1
                error_details.append({"index": idx, "error": str(exc)})
                logger.warning("Bulk import row %d failed: %s", idx, exc)

        await db.flush()
        logger.info("Bulk import complete: %d created, %d errors", created, errors)
        return {"created": created, "errors": errors, "error_details": error_details}

    async def bulk_export(
        self,
        db: AsyncSession,
        criteria: dict[str, Any] | None = None,
        format: str = "json",
    ) -> str:
        """Export customer profiles matching *criteria* as a string.

        Parameters
        ----------
        criteria:
            Column-level filters passed to :meth:`search_profiles`.
        format:
            ``"json"`` or ``"csv"``.
        """
        result = await self.search_profiles(db, filters=criteria, page=1, size=100)
        customers: list[Customer] = result["items"]

        rows = [
            {
                "id": str(c.id),
                "email": c.email,
                "phone": c.phone,
                "first_name": c.first_name,
                "last_name": c.last_name,
                "segment": c.segment,
                "lifetime_value": c.lifetime_value,
                "is_active": c.is_active,
                "created_at": c.created_at.isoformat() if c.created_at else None,
            }
            for c in customers
        ]

        if format == "csv":
            if not rows:
                return ""
            buf = io.StringIO()
            writer = csv.DictWriter(buf, fieldnames=rows[0].keys())
            writer.writeheader()
            writer.writerows(rows)
            return buf.getvalue()

        return json.dumps(rows, default=str, indent=2)
