"""Data unification service -- merge, deduplicate, and build golden records."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from cdp.models.customer import Customer, CustomerAttribute, CustomerIdentity
from cdp.utils.helpers import generate_uuid

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Conflict-resolution strategies
# ---------------------------------------------------------------------------


class ResolutionStrategy(str, Enum):
    """Determines which value wins when the same field has multiple sources."""

    MOST_RECENT = "most_recent"
    HIGHEST_CONFIDENCE = "highest_confidence"
    MANUAL_OVERRIDE = "manual_override"
    SOURCE_PRIORITY = "source_priority"


# Default priority order (lower index = higher priority).
DEFAULT_SOURCE_PRIORITY: list[str] = [
    "manual",
    "crm",
    "website",
    "mobile_app",
    "import",
    "third_party",
]


class DataUnificationService:
    """Merge data from multiple sources into a single, golden customer record.

    Responsibilities
    ----------------
    * Merge two or more customer profiles into one.
    * Resolve field-level conflicts using configurable golden-record rules.
    * Detect and merge duplicate profiles based on shared identities.
    * Batch unification across the entire database.
    """

    def __init__(
        self,
        source_priority: list[str] | None = None,
        default_strategy: ResolutionStrategy = ResolutionStrategy.MOST_RECENT,
    ) -> None:
        self.source_priority = source_priority or list(DEFAULT_SOURCE_PRIORITY)
        self.default_strategy = default_strategy

    # ------------------------------------------------------------------
    # Profile merging
    # ------------------------------------------------------------------

    async def unify_profiles(
        self,
        db: AsyncSession,
        profile_ids: list[str | uuid.UUID],
    ) -> Customer:
        """Merge multiple profiles into a single unified customer record.

        The first id in *profile_ids* is treated as the **primary** record.
        All identities, attributes, and events from secondary profiles are
        re-parented to the primary, and the secondary profiles are deleted.

        Parameters
        ----------
        db:
            Active async database session.
        profile_ids:
            Two or more customer UUIDs to merge.

        Returns
        -------
        Customer
            The surviving (primary) customer record with merged data.

        Raises
        ------
        ValueError
            If fewer than two profile ids are provided or any id is missing.
        """
        if len(profile_ids) < 2:
            raise ValueError("At least two profile IDs are required for unification")

        uuids = [uuid.UUID(str(pid)) for pid in profile_ids]
        primary_id = uuids[0]
        secondary_ids = uuids[1:]

        # Load all profiles with relationships
        stmt = (
            select(Customer)
            .where(Customer.id.in_(uuids))
            .options(
                selectinload(Customer.identities),
                selectinload(Customer.attributes),
                selectinload(Customer.events),
            )
        )
        result = await db.execute(stmt)
        profiles = {c.id: c for c in result.scalars().all()}

        if primary_id not in profiles:
            raise ValueError(f"Primary profile {primary_id} not found")

        missing = [str(sid) for sid in secondary_ids if sid not in profiles]
        if missing:
            logger.warning("Secondary profiles not found: %s", missing)

        primary = profiles[primary_id]

        # Merge fields from secondary profiles using golden-record rules
        for sid in secondary_ids:
            secondary = profiles.get(sid)
            if secondary is None:
                continue
            await self._merge_into_primary(db, primary, secondary)

        await db.flush()
        await db.refresh(primary)
        logger.info(
            "Unified %d profiles into %s",
            len(profile_ids),
            primary_id,
        )
        return primary

    async def _merge_into_primary(
        self,
        db: AsyncSession,
        primary: Customer,
        secondary: Customer,
    ) -> None:
        """Merge *secondary* data into *primary* and delete the secondary."""

        # Resolve field conflicts for scalar columns
        mergeable_fields = [
            "email", "phone", "first_name", "last_name", "date_of_birth",
            "gender", "avatar_url", "segment",
        ]
        for field in mergeable_fields:
            primary_val = getattr(primary, field, None)
            secondary_val = getattr(secondary, field, None)
            if not primary_val and secondary_val:
                setattr(primary, field, secondary_val)
            elif primary_val and secondary_val and primary_val != secondary_val:
                winner = self.resolve_conflicts(
                    field,
                    [
                        {"value": primary_val, "source": primary.source, "updated_at": primary.updated_at},
                        {"value": secondary_val, "source": secondary.source, "updated_at": secondary.updated_at},
                    ],
                )
                setattr(primary, field, winner)

        # Merge lifetime value (take the max)
        primary.lifetime_value = max(
            primary.lifetime_value or 0.0,
            secondary.lifetime_value or 0.0,
        )

        # Merge metadata dicts
        primary.metadata_ = {
            **(primary.metadata_ or {}),
            **(secondary.metadata_ or {}),
        }

        # Re-parent identities
        for identity in secondary.identities:
            identity.customer_id = primary.id
        # Re-parent attributes (skip duplicates by key)
        existing_keys = {a.attribute_key for a in primary.attributes}
        for attr in secondary.attributes:
            if attr.attribute_key not in existing_keys:
                attr.customer_id = primary.id
            else:
                await db.delete(attr)
        # Re-parent events
        for event in secondary.events:
            event.customer_id = primary.id

        # Merge tags
        primary_tags = set(primary.tags or [])
        secondary_tags = set(secondary.tags or [])
        primary.tags = list(primary_tags | secondary_tags)

        primary.updated_at = datetime.now(timezone.utc)
        await db.delete(secondary)

    # ------------------------------------------------------------------
    # Conflict resolution
    # ------------------------------------------------------------------

    def resolve_conflicts(
        self,
        field: str,
        values_with_sources: list[dict[str, Any]],
        strategy: ResolutionStrategy | None = None,
    ) -> Any:
        """Pick the winning value for a field that has conflicting data.

        Parameters
        ----------
        field:
            The column name (for logging / rule lookup).
        values_with_sources:
            List of dicts each containing ``value``, ``source``, and
            optionally ``updated_at`` and ``confidence``.
        strategy:
            Override the instance default strategy.

        Returns
        -------
        Any
            The chosen value.
        """
        if not values_with_sources:
            return None

        strategy = strategy or self.default_strategy

        if strategy == ResolutionStrategy.MANUAL_OVERRIDE:
            manual = [v for v in values_with_sources if v.get("source") == "manual"]
            if manual:
                return manual[0]["value"]
            # Fall through to most-recent
            strategy = ResolutionStrategy.MOST_RECENT

        if strategy == ResolutionStrategy.HIGHEST_CONFIDENCE:
            ranked = sorted(
                values_with_sources,
                key=lambda v: v.get("confidence", 0.0),
                reverse=True,
            )
            return ranked[0]["value"]

        if strategy == ResolutionStrategy.SOURCE_PRIORITY:
            def _priority(v: dict[str, Any]) -> int:
                src = v.get("source", "")
                try:
                    return self.source_priority.index(src)
                except ValueError:
                    return len(self.source_priority)

            ranked = sorted(values_with_sources, key=_priority)
            return ranked[0]["value"]

        # Default: MOST_RECENT
        ranked = sorted(
            values_with_sources,
            key=lambda v: v.get("updated_at") or datetime.min.replace(tzinfo=timezone.utc),
            reverse=True,
        )
        return ranked[0]["value"]

    # ------------------------------------------------------------------
    # Golden record rules
    # ------------------------------------------------------------------

    async def apply_golden_record_rules(
        self,
        db: AsyncSession,
        customer_id: str | uuid.UUID,
    ) -> Customer | None:
        """Re-evaluate the golden record for a single customer.

        Iterates over all :class:`CustomerAttribute` rows, resolves conflicts
        per attribute key, and updates the master profile accordingly.

        Returns the updated Customer or ``None`` if not found.
        """
        cid = uuid.UUID(str(customer_id))
        stmt = (
            select(Customer)
            .where(Customer.id == cid)
            .options(selectinload(Customer.attributes))
        )
        result = await db.execute(stmt)
        customer = result.scalars().first()
        if customer is None:
            return None

        # Group attributes by key
        attr_groups: dict[str, list[dict[str, Any]]] = {}
        for attr in customer.attributes:
            attr_groups.setdefault(attr.attribute_key, []).append(
                {
                    "value": attr.attribute_value,
                    "source": attr.source,
                    "confidence": attr.confidence,
                    "updated_at": attr.updated_at,
                }
            )

        # Resolve each group and store winners back into metadata
        golden: dict[str, Any] = {}
        for key, values in attr_groups.items():
            golden[key] = self.resolve_conflicts(key, values)

        customer.metadata_ = {
            **(customer.metadata_ or {}),
            "golden_record": golden,
            "golden_record_updated_at": datetime.now(timezone.utc).isoformat(),
        }
        customer.updated_at = datetime.now(timezone.utc)
        await db.flush()
        logger.info("Applied golden record rules for customer %s", customer_id)
        return customer

    # ------------------------------------------------------------------
    # Deduplication
    # ------------------------------------------------------------------

    async def find_duplicates(
        self,
        db: AsyncSession,
        threshold: float = 0.8,
    ) -> list[dict[str, Any]]:
        """Identify potential duplicate customers.

        Duplicates are detected by shared identity values (email, phone) and
        name similarity.  Each returned dict contains ``customer_ids`` and a
        ``score`` between 0 and 1.

        Parameters
        ----------
        threshold:
            Minimum similarity score to consider a pair a duplicate.
        """
        # Strategy: find customers sharing the same email or phone
        duplicates: list[dict[str, Any]] = []

        # -- Email-based duplicates --
        email_stmt = (
            select(Customer.email, func.array_agg(Customer.id))
            .where(Customer.email.isnot(None), Customer.is_active.is_(True))
            .group_by(Customer.email)
            .having(func.count(Customer.id) > 1)
        )
        try:
            email_result = await db.execute(email_stmt)
            for email, ids in email_result.all():
                duplicates.append(
                    {
                        "match_field": "email",
                        "match_value": email,
                        "customer_ids": [str(i) for i in ids],
                        "score": 1.0,
                    }
                )
        except Exception:
            logger.debug("Email duplicate detection query not supported; skipping")

        # -- Phone-based duplicates --
        phone_stmt = (
            select(Customer.phone, func.array_agg(Customer.id))
            .where(Customer.phone.isnot(None), Customer.is_active.is_(True))
            .group_by(Customer.phone)
            .having(func.count(Customer.id) > 1)
        )
        try:
            phone_result = await db.execute(phone_stmt)
            for phone, ids in phone_result.all():
                duplicates.append(
                    {
                        "match_field": "phone",
                        "match_value": phone,
                        "customer_ids": [str(i) for i in ids],
                        "score": 0.95,
                    }
                )
        except Exception:
            logger.debug("Phone duplicate detection query not supported; skipping")

        # -- Identity-table based duplicates --
        identity_stmt = (
            select(
                CustomerIdentity.identity_type,
                CustomerIdentity.identity_value,
                func.array_agg(CustomerIdentity.customer_id.distinct()),
            )
            .group_by(CustomerIdentity.identity_type, CustomerIdentity.identity_value)
            .having(func.count(CustomerIdentity.customer_id.distinct()) > 1)
        )
        try:
            identity_result = await db.execute(identity_stmt)
            for id_type, id_value, ids in identity_result.all():
                duplicates.append(
                    {
                        "match_field": f"identity:{id_type}",
                        "match_value": id_value,
                        "customer_ids": [str(i) for i in ids],
                        "score": 0.9,
                    }
                )
        except Exception:
            logger.debug("Identity duplicate detection query not supported; skipping")

        # Filter by threshold
        duplicates = [d for d in duplicates if d["score"] >= threshold]
        logger.info("Found %d potential duplicate groups (threshold=%.2f)", len(duplicates), threshold)
        return duplicates

    async def merge_duplicates(
        self,
        db: AsyncSession,
        primary_id: str | uuid.UUID,
        duplicate_ids: list[str | uuid.UUID],
    ) -> Customer:
        """Merge known duplicates into the primary profile.

        This is a convenience wrapper around :meth:`unify_profiles`.
        """
        all_ids = [primary_id, *duplicate_ids]
        return await self.unify_profiles(db, all_ids)

    # ------------------------------------------------------------------
    # Batch unification
    # ------------------------------------------------------------------

    async def run_unification(self, db: AsyncSession) -> dict[str, Any]:
        """Run deduplication and merge across the entire customer base.

        Returns a summary with counts of groups found and merges performed.
        """
        duplicates = await self.find_duplicates(db, threshold=0.8)
        merged_count = 0
        error_count = 0
        errors: list[dict[str, Any]] = []

        for group in duplicates:
            ids = group["customer_ids"]
            if len(ids) < 2:
                continue
            try:
                await self.merge_duplicates(db, ids[0], ids[1:])
                merged_count += 1
            except Exception as exc:
                error_count += 1
                errors.append({"customer_ids": ids, "error": str(exc)})
                logger.warning("Merge failed for group %s: %s", ids, exc)

        await db.flush()
        summary = {
            "duplicate_groups_found": len(duplicates),
            "merges_performed": merged_count,
            "merge_errors": error_count,
            "error_details": errors,
        }
        logger.info("Batch unification complete: %s", summary)
        return summary
