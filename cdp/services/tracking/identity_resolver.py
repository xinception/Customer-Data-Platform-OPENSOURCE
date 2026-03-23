"""Identity resolution service: merge anonymous visitors with known customer profiles.

Supports deterministic matching (email, phone, external ID) and probabilistic
matching (device fingerprint, behavioural signals).  Maintains an identity
graph so that a single customer can be linked to many identifiers.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from cdp.config import settings
from cdp.models.customer import (
    Customer,
    CustomerAttribute,
    CustomerEvent,
    CustomerIdentity,
    IdentityType,
)
from cdp.models.tracking import TrackingSession
from cdp.utils.helpers import hash_email, validate_email, validate_phone

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Confidence thresholds
# ---------------------------------------------------------------------------
DETERMINISTIC_CONFIDENCE = 1.0
PROBABILISTIC_CONFIDENCE_HIGH = 0.85
PROBABILISTIC_CONFIDENCE_MEDIUM = 0.6
PROBABILISTIC_CONFIDENCE_LOW = 0.4
MERGE_THRESHOLD = 0.7  # minimum confidence to auto-merge


class IdentityResolver:
    """Resolves anonymous visitor identities into known customer profiles.

    The resolver maintains an *identity graph* where each node is a customer
    and edges are identity links (email, phone, cookie, device fingerprint,
    etc.).  When an anonymous visitor provides enough identifying information
    the resolver either matches them to an existing customer or creates a new
    profile.

    All database operations are performed through an ``AsyncSession`` passed
    to each public method so the caller controls the transaction boundary.
    """

    # ------------------------------------------------------------------
    # Identity matching
    # ------------------------------------------------------------------

    async def find_matching_customer(
        self,
        db: AsyncSession,
        identity_data: dict[str, Any],
    ) -> tuple[Customer | None, float]:
        """Search for an existing customer that matches *identity_data*.

        Parameters
        ----------
        db:
            Active async database session.
        identity_data:
            Dict that may contain ``email``, ``phone``, ``external_id``,
            ``device_fingerprint``, or other identity hints.

        Returns
        -------
        tuple[Customer | None, float]
            The matched customer (or ``None``) and the confidence score of
            the match (0.0 – 1.0).
        """
        # 1. Deterministic: email
        email = identity_data.get("email")
        if email and validate_email(email):
            customer = await self._match_by_email(db, email)
            if customer is not None:
                logger.info("Deterministic match by email for customer %s", customer.id)
                return customer, DETERMINISTIC_CONFIDENCE

        # 2. Deterministic: phone
        phone = identity_data.get("phone")
        if phone and validate_phone(phone):
            customer = await self._match_by_phone(db, phone)
            if customer is not None:
                logger.info("Deterministic match by phone for customer %s", customer.id)
                return customer, DETERMINISTIC_CONFIDENCE

        # 3. Deterministic: external_id
        external_id = identity_data.get("external_id")
        if external_id:
            customer = await self._match_by_external_id(db, external_id)
            if customer is not None:
                logger.info("Deterministic match by external_id for customer %s", customer.id)
                return customer, DETERMINISTIC_CONFIDENCE

        # 4. Deterministic: identity table lookup (any type)
        for id_type in (IdentityType.DEVICE, IdentityType.COOKIE, IdentityType.SOCIAL):
            value = identity_data.get(id_type.value)
            if value:
                customer = await self._match_by_identity_link(db, id_type.value, value)
                if customer is not None:
                    logger.info("Identity-link match (%s) for customer %s", id_type.value, customer.id)
                    return customer, PROBABILISTIC_CONFIDENCE_HIGH

        # 5. Probabilistic: device fingerprint
        device_fp = identity_data.get("device_fingerprint")
        if device_fp:
            customer = await self._match_by_identity_link(db, IdentityType.DEVICE.value, device_fp)
            if customer is not None:
                logger.info("Probabilistic match by device fingerprint for customer %s", customer.id)
                return customer, PROBABILISTIC_CONFIDENCE_MEDIUM

        logger.debug("No matching customer found for identity data")
        return None, 0.0

    # ------------------------------------------------------------------
    # Identity resolution (high-level)
    # ------------------------------------------------------------------

    async def resolve_identity(
        self,
        db: AsyncSession,
        visitor_id: str,
        identity_data: dict[str, Any],
    ) -> dict[str, Any]:
        """Attempt to resolve a visitor to a customer profile.

        If a match is found, existing session/event data is linked to that
        customer.  If no match is found a new ``Customer`` is created when
        there is enough PII (email or phone).

        Returns a summary dict with ``customer_id``, ``is_new``, and
        ``confidence``.
        """
        customer, confidence = await self.find_matching_customer(db, identity_data)

        if customer is not None and confidence >= MERGE_THRESHOLD:
            # Link the visitor's tracking sessions to the known customer.
            await self._link_visitor_sessions(db, visitor_id, customer.id)
            # Add any new identity links.
            await self._add_identity_links(db, customer.id, identity_data)
            # Update customer profile fields if richer data is available.
            await self._update_customer_profile(db, customer, identity_data)
            await db.flush()
            logger.info(
                "Resolved visitor %s -> customer %s (confidence=%.2f)",
                visitor_id, customer.id, confidence,
            )
            return {
                "customer_id": str(customer.id),
                "is_new": False,
                "confidence": confidence,
                "visitor_id": visitor_id,
            }

        # No match — create a new customer if we have PII.
        email = identity_data.get("email")
        phone = identity_data.get("phone")
        if email or phone:
            new_customer = await self._create_customer(db, identity_data)
            await self._link_visitor_sessions(db, visitor_id, new_customer.id)
            await self._add_identity_links(db, new_customer.id, identity_data)
            await db.flush()
            logger.info(
                "Created new customer %s for visitor %s", new_customer.id, visitor_id,
            )
            return {
                "customer_id": str(new_customer.id),
                "is_new": True,
                "confidence": DETERMINISTIC_CONFIDENCE,
                "visitor_id": visitor_id,
            }

        # Not enough data to create a profile — remain anonymous.
        logger.debug("Visitor %s remains anonymous (insufficient identity data)", visitor_id)
        return {
            "customer_id": None,
            "is_new": False,
            "confidence": 0.0,
            "visitor_id": visitor_id,
        }

    # ------------------------------------------------------------------
    # Profile merging
    # ------------------------------------------------------------------

    async def merge_profiles(
        self,
        db: AsyncSession,
        source_id: str,
        target_id: str,
    ) -> dict[str, Any]:
        """Merge the *source* customer into the *target* customer.

        All identity links, events, attributes, and tracking sessions from
        the source are re-pointed to the target.  The source profile is then
        deactivated.

        Returns a summary dict.
        """
        source_uuid = uuid.UUID(source_id)
        target_uuid = uuid.UUID(target_id)

        # Re-assign identities
        stmt = (
            update(CustomerIdentity)
            .where(CustomerIdentity.customer_id == source_uuid)
            .values(customer_id=target_uuid)
        )
        await db.execute(stmt)

        # Re-assign events
        stmt = (
            update(CustomerEvent)
            .where(CustomerEvent.customer_id == source_uuid)
            .values(customer_id=target_uuid)
        )
        await db.execute(stmt)

        # Re-assign attributes (skip duplicates by key)
        stmt = (
            update(CustomerAttribute)
            .where(CustomerAttribute.customer_id == source_uuid)
            .values(customer_id=target_uuid)
        )
        await db.execute(stmt)

        # Re-assign tracking sessions
        stmt = (
            update(TrackingSession)
            .where(TrackingSession.customer_id == source_uuid)
            .values(customer_id=target_uuid)
        )
        await db.execute(stmt)

        # Deactivate source customer
        stmt = (
            update(Customer)
            .where(Customer.id == source_uuid)
            .values(is_active=False)
        )
        await db.execute(stmt)

        await db.flush()
        logger.info("Merged customer %s into %s", source_id, target_id)

        return {
            "source_id": source_id,
            "target_id": target_id,
            "status": "merged",
        }

    # ------------------------------------------------------------------
    # Identity linking
    # ------------------------------------------------------------------

    async def link_identity(
        self,
        db: AsyncSession,
        customer_id: str,
        identity_type: str,
        identity_value: str,
        *,
        confidence: float = DETERMINISTIC_CONFIDENCE,
        verified: bool = False,
    ) -> CustomerIdentity:
        """Create or update an identity link for a customer.

        If the ``(identity_type, identity_value)`` pair already exists for
        this customer the confidence score is updated.
        """
        customer_uuid = uuid.UUID(customer_id)

        # Check for existing link.
        result = await db.execute(
            select(CustomerIdentity).where(
                CustomerIdentity.identity_type == identity_type,
                CustomerIdentity.identity_value == identity_value,
            )
        )
        existing = result.scalar_one_or_none()

        if existing is not None:
            if existing.customer_id == customer_uuid:
                # Update confidence if higher.
                if confidence > (existing.confidence_score or 0):
                    existing.confidence_score = confidence
                    existing.verified = existing.verified or verified
                logger.debug(
                    "Updated identity link %s=%s for customer %s",
                    identity_type, identity_value, customer_id,
                )
                await db.flush()
                return existing
            else:
                # The identity is already linked to a *different* customer.
                # This may trigger a merge in the calling code.
                logger.warning(
                    "Identity %s=%s already linked to customer %s (requested %s)",
                    identity_type, identity_value, existing.customer_id, customer_id,
                )
                return existing

        identity = CustomerIdentity(
            customer_id=customer_uuid,
            identity_type=identity_type,
            identity_value=identity_value,
            confidence_score=confidence,
            verified=verified,
        )
        db.add(identity)
        await db.flush()
        logger.info(
            "Linked identity %s=%s to customer %s", identity_type, identity_value, customer_id,
        )
        return identity

    # ------------------------------------------------------------------
    # Convenience: resolve and merge in one call
    # ------------------------------------------------------------------

    async def resolve_and_merge(
        self,
        db: AsyncSession,
        visitor_id: str,
        identity_hints: dict[str, Any],
    ) -> dict[str, Any]:
        """End-to-end identity resolution with automatic merge detection.

        1. Resolve the visitor to a customer (or create one).
        2. Check whether the resolved customer should be merged with another
           profile that shares the same email/phone.
        3. Perform the merge if needed.

        Returns a summary dict.
        """
        result = await self.resolve_identity(db, visitor_id, identity_hints)
        customer_id = result.get("customer_id")

        if customer_id is None:
            return result

        # Check for merge candidates: another active customer sharing the
        # same email or phone.
        merge_candidate = await self._find_merge_candidate(db, customer_id, identity_hints)
        if merge_candidate is not None:
            merge_result = await self.merge_profiles(
                db, source_id=customer_id, target_id=str(merge_candidate.id),
            )
            result["merged_into"] = merge_result["target_id"]
            result["customer_id"] = merge_result["target_id"]
            logger.info(
                "Auto-merged customer %s into %s during resolution",
                customer_id, merge_result["target_id"],
            )

        return result

    # ------------------------------------------------------------------
    # Private DB helpers
    # ------------------------------------------------------------------

    @staticmethod
    async def _match_by_email(db: AsyncSession, email: str) -> Customer | None:
        normalized = email.strip().lower()
        result = await db.execute(
            select(Customer).where(
                Customer.email == normalized,
                Customer.is_active.is_(True),
            )
        )
        return result.scalar_one_or_none()

    @staticmethod
    async def _match_by_phone(db: AsyncSession, phone: str) -> Customer | None:
        result = await db.execute(
            select(Customer).where(
                Customer.phone == phone.strip(),
                Customer.is_active.is_(True),
            )
        )
        return result.scalar_one_or_none()

    @staticmethod
    async def _match_by_external_id(db: AsyncSession, external_id: str) -> Customer | None:
        result = await db.execute(
            select(Customer).where(
                Customer.external_id == external_id,
                Customer.is_active.is_(True),
            )
        )
        return result.scalar_one_or_none()

    @staticmethod
    async def _match_by_identity_link(
        db: AsyncSession,
        identity_type: str,
        identity_value: str,
    ) -> Customer | None:
        result = await db.execute(
            select(Customer)
            .join(CustomerIdentity, CustomerIdentity.customer_id == Customer.id)
            .where(
                CustomerIdentity.identity_type == identity_type,
                CustomerIdentity.identity_value == identity_value,
                Customer.is_active.is_(True),
            )
        )
        return result.scalar_one_or_none()

    @staticmethod
    async def _link_visitor_sessions(
        db: AsyncSession,
        visitor_id: str,
        customer_id: uuid.UUID,
    ) -> None:
        """Point all tracking sessions for *visitor_id* to *customer_id*."""
        try:
            visitor_uuid = uuid.UUID(visitor_id)
        except ValueError:
            logger.warning("Invalid visitor_id format: %s", visitor_id)
            return

        stmt = (
            update(TrackingSession)
            .where(
                TrackingSession.visitor_id == visitor_uuid,
                TrackingSession.customer_id.is_(None),
            )
            .values(customer_id=customer_id)
        )
        await db.execute(stmt)

    async def _add_identity_links(
        self,
        db: AsyncSession,
        customer_id: uuid.UUID,
        identity_data: dict[str, Any],
    ) -> None:
        """Create identity links for all identifiers present in *identity_data*."""
        cid = str(customer_id)

        email = identity_data.get("email")
        if email and validate_email(email):
            await self.link_identity(
                db, cid, IdentityType.EMAIL.value, email.strip().lower(),
                confidence=DETERMINISTIC_CONFIDENCE, verified=False,
            )

        phone = identity_data.get("phone")
        if phone and validate_phone(phone):
            await self.link_identity(
                db, cid, IdentityType.PHONE.value, phone.strip(),
                confidence=DETERMINISTIC_CONFIDENCE, verified=False,
            )

        device_fp = identity_data.get("device_fingerprint")
        if device_fp:
            await self.link_identity(
                db, cid, IdentityType.DEVICE.value, device_fp,
                confidence=PROBABILISTIC_CONFIDENCE_MEDIUM, verified=False,
            )

        cookie_id = identity_data.get("cookie")
        if cookie_id:
            await self.link_identity(
                db, cid, IdentityType.COOKIE.value, cookie_id,
                confidence=PROBABILISTIC_CONFIDENCE_HIGH, verified=False,
            )

        social_id = identity_data.get("social")
        if social_id:
            await self.link_identity(
                db, cid, IdentityType.SOCIAL.value, social_id,
                confidence=DETERMINISTIC_CONFIDENCE, verified=False,
            )

    @staticmethod
    async def _create_customer(
        db: AsyncSession,
        identity_data: dict[str, Any],
    ) -> Customer:
        """Create a new ``Customer`` from the available identity data."""
        customer = Customer(
            email=identity_data.get("email", "").strip().lower() or None,
            phone=identity_data.get("phone", "").strip() or None,
            first_name=identity_data.get("first_name"),
            last_name=identity_data.get("last_name"),
            external_id=identity_data.get("external_id"),
            source=identity_data.get("source", "tracking"),
        )
        db.add(customer)
        await db.flush()
        return customer

    @staticmethod
    async def _update_customer_profile(
        db: AsyncSession,
        customer: Customer,
        identity_data: dict[str, Any],
    ) -> None:
        """Fill in blank profile fields from *identity_data*."""
        changed = False
        if not customer.email and identity_data.get("email"):
            customer.email = identity_data["email"].strip().lower()
            changed = True
        if not customer.phone and identity_data.get("phone"):
            customer.phone = identity_data["phone"].strip()
            changed = True
        if not customer.first_name and identity_data.get("first_name"):
            customer.first_name = identity_data["first_name"]
            changed = True
        if not customer.last_name and identity_data.get("last_name"):
            customer.last_name = identity_data["last_name"]
            changed = True
        if changed:
            await db.flush()

    @staticmethod
    async def _find_merge_candidate(
        db: AsyncSession,
        current_customer_id: str,
        identity_data: dict[str, Any],
    ) -> Customer | None:
        """Find another active customer that shares an email or phone.

        Returns ``None`` when the current customer is the only match or when
        no shared identifier exists.
        """
        current_uuid = uuid.UUID(current_customer_id)

        email = identity_data.get("email")
        if email and validate_email(email):
            result = await db.execute(
                select(Customer).where(
                    Customer.email == email.strip().lower(),
                    Customer.is_active.is_(True),
                    Customer.id != current_uuid,
                )
            )
            candidate = result.scalar_one_or_none()
            if candidate is not None:
                return candidate

        phone = identity_data.get("phone")
        if phone and validate_phone(phone):
            result = await db.execute(
                select(Customer).where(
                    Customer.phone == phone.strip(),
                    Customer.is_active.is_(True),
                    Customer.id != current_uuid,
                )
            )
            candidate = result.scalar_one_or_none()
            if candidate is not None:
                return candidate

        return None
