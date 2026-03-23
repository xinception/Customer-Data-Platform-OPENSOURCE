"""GDPR / CCPA compliant consent management with audit trail and data-deletion support."""

from __future__ import annotations

import logging
import time
import uuid
from enum import Enum
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from cdp.config import settings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Consent categories
# ---------------------------------------------------------------------------

class ConsentCategory(str, Enum):
    """Standard consent categories.

    ``NECESSARY`` cookies are always allowed and cannot be revoked.
    """

    NECESSARY = "necessary"
    ANALYTICS = "analytics"
    MARKETING = "marketing"
    PERSONALIZATION = "personalization"


ALL_OPTIONAL_CATEGORIES = {
    ConsentCategory.ANALYTICS,
    ConsentCategory.MARKETING,
    ConsentCategory.PERSONALIZATION,
}

# ---------------------------------------------------------------------------
# Lightweight in-memory / dict-based storage (swap for real DB models)
# ---------------------------------------------------------------------------
# In production the rows below map to database tables.  The manager accepts an
# ``AsyncSession`` so that it can be wired to SQLAlchemy models via dependency
# injection.  The private dicts serve as a fallback for unit-testing without a
# DB.

_consent_store: dict[str, dict[str, bool]] = {}
_audit_log: list[dict[str, Any]] = []
_deletion_requests: list[dict[str, Any]] = []


class ConsentManager:
    """Manages per-visitor consent preferences with a full audit trail.

    All public methods that touch persistent state accept an optional
    ``db`` parameter (``AsyncSession``).  When ``db`` is ``None`` the
    manager falls back to an in-memory store, which is handy for tests.
    """

    def __init__(self) -> None:
        self.consent_required: bool = settings.CONSENT_REQUIRED

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _record_audit(
        visitor_id: str,
        action: str,
        categories: list[str],
        *,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        entry = {
            "id": str(uuid.uuid4()),
            "visitor_id": visitor_id,
            "action": action,
            "categories": categories,
            "timestamp": time.time(),
            "metadata": metadata or {},
        }
        _audit_log.append(entry)
        logger.info("Consent audit: %s %s %s", action, visitor_id, categories)
        return entry

    @staticmethod
    def _ensure_visitor(visitor_id: str) -> dict[str, bool]:
        if visitor_id not in _consent_store:
            # NECESSARY is always granted.
            _consent_store[visitor_id] = {
                ConsentCategory.NECESSARY: True,
                ConsentCategory.ANALYTICS: False,
                ConsentCategory.MARKETING: False,
                ConsentCategory.PERSONALIZATION: False,
            }
        return _consent_store[visitor_id]

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def grant_consent(
        self,
        visitor_id: str,
        categories: list[str],
        *,
        db: AsyncSession | None = None,
    ) -> dict[str, bool]:
        """Grant consent for one or more categories.

        Returns the updated consent map.
        """
        record = self._ensure_visitor(visitor_id)
        granted: list[str] = []
        for cat in categories:
            if cat == ConsentCategory.NECESSARY:
                continue  # always on
            if cat in {c.value for c in ConsentCategory}:
                record[cat] = True
                granted.append(cat)

        self._record_audit(visitor_id, "grant", granted)

        if db is not None:
            await self._persist_consent(db, visitor_id, record)

        return dict(record)

    async def revoke_consent(
        self,
        visitor_id: str,
        categories: list[str],
        *,
        db: AsyncSession | None = None,
    ) -> dict[str, bool]:
        """Revoke consent for one or more categories.

        ``NECESSARY`` cannot be revoked.  Returns the updated consent map.
        """
        record = self._ensure_visitor(visitor_id)
        revoked: list[str] = []
        for cat in categories:
            if cat == ConsentCategory.NECESSARY:
                logger.warning("Cannot revoke necessary consent")
                continue
            if cat in record:
                record[cat] = False
                revoked.append(cat)

        self._record_audit(visitor_id, "revoke", revoked)

        if db is not None:
            await self._persist_consent(db, visitor_id, record)

        return dict(record)

    async def check_consent(
        self,
        visitor_id: str,
        category: str,
        *,
        db: AsyncSession | None = None,
    ) -> bool:
        """Check whether *visitor_id* has consented to *category*.

        If ``consent_required`` is ``False`` (e.g. internal analytics), all
        categories are treated as granted.
        """
        if not self.consent_required:
            return True
        if category == ConsentCategory.NECESSARY:
            return True
        record = self._ensure_visitor(visitor_id)
        return record.get(category, False)

    async def get_consent_status(
        self,
        visitor_id: str,
        *,
        db: AsyncSession | None = None,
    ) -> dict[str, bool]:
        """Return the full consent map for a visitor."""
        return dict(self._ensure_visitor(visitor_id))

    # ------------------------------------------------------------------
    # Audit trail
    # ------------------------------------------------------------------

    async def get_audit_trail(
        self,
        visitor_id: str,
        *,
        db: AsyncSession | None = None,
    ) -> list[dict[str, Any]]:
        """Retrieve all consent-related audit entries for a visitor."""
        return [e for e in _audit_log if e["visitor_id"] == visitor_id]

    # ------------------------------------------------------------------
    # Data deletion (GDPR Art. 17 / CCPA)
    # ------------------------------------------------------------------

    async def handle_deletion_request(
        self,
        customer_id: str,
        *,
        db: AsyncSession | None = None,
    ) -> dict[str, Any]:
        """Process a right-to-erasure / right-to-deletion request.

        In a full implementation this would:
        1. Remove or anonymise all PII linked to *customer_id*.
        2. Revoke all consent records.
        3. Delete events, profiles, and identity links.
        4. Notify downstream systems (warehouses, ad platforms, etc.).

        Here we record the request, purge the in-memory stores, and (when a
        ``db`` session is provided) delete rows from the database.
        """
        request_record = {
            "id": str(uuid.uuid4()),
            "customer_id": customer_id,
            "status": "processing",
            "requested_at": time.time(),
        }
        _deletion_requests.append(request_record)

        # Purge in-memory consent records that reference this customer_id.
        # (In production, visitor_id <-> customer_id mapping lives in DB.)
        visitors_to_purge = [
            vid for vid in _consent_store
            if vid == customer_id  # simplified lookup
        ]
        for vid in visitors_to_purge:
            del _consent_store[vid]

        if db is not None:
            await self._delete_customer_data(db, customer_id)

        request_record["status"] = "completed"
        request_record["completed_at"] = time.time()
        logger.info("Deletion request %s completed for customer %s", request_record["id"], customer_id)
        return request_record

    # ------------------------------------------------------------------
    # DB persistence helpers (stubs - wire to real models)
    # ------------------------------------------------------------------

    @staticmethod
    async def _persist_consent(
        db: AsyncSession,
        visitor_id: str,
        consent_map: dict[str, bool],
    ) -> None:
        """Persist the consent map to the database.

        Replace with actual model operations when ORM models are defined.
        """
        logger.debug("Persisting consent for %s: %s", visitor_id, consent_map)
        # Example:
        # stmt = insert(ConsentRecord).values(...)
        # .on_conflict_do_update(...)
        # await db.execute(stmt)
        # await db.flush()

    @staticmethod
    async def _delete_customer_data(db: AsyncSession, customer_id: str) -> None:
        """Delete all customer data from the database.

        Wire to actual models (events, profiles, identity links) when available.
        """
        logger.info("Deleting DB records for customer %s", customer_id)
        # Example:
        # await db.execute(delete(Event).where(Event.customer_id == customer_id))
        # await db.execute(delete(Profile).where(Profile.customer_id == customer_id))
        # await db.flush()
