"""Dynamic audience builder that translates JSON criteria to SQLAlchemy queries.

Supports demographic, behavioural, transactional, engagement, and custom-attribute
criteria.  Audiences can be evaluated in real-time or saved for repeated use.
"""

from __future__ import annotations

import enum
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Sequence

from sqlalchemy import and_, case, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from cdp.models.customer import Customer, CustomerAttribute, CustomerEvent

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------

class CriteriaType(str, enum.Enum):
    """Top-level criteria categories."""

    DEMOGRAPHIC = "demographic"
    BEHAVIORAL = "behavioral"
    TRANSACTIONAL = "transactional"
    ENGAGEMENT = "engagement"
    CUSTOM = "custom"


class Operator(str, enum.Enum):
    """Comparison operators supported inside filter rules."""

    EQ = "eq"
    NEQ = "neq"
    GT = "gt"
    GTE = "gte"
    LT = "lt"
    LTE = "lte"
    IN = "in"
    NOT_IN = "not_in"
    CONTAINS = "contains"
    NOT_CONTAINS = "not_contains"
    STARTS_WITH = "starts_with"
    ENDS_WITH = "ends_with"
    IS_NULL = "is_null"
    IS_NOT_NULL = "is_not_null"
    BETWEEN = "between"


# ---------------------------------------------------------------------------
# Saved-audience lightweight container (no ORM table required)
# ---------------------------------------------------------------------------

class SavedAudience:
    """In-memory representation of a saved audience definition."""

    def __init__(self, audience_id: uuid.UUID, name: str, criteria: dict[str, Any]) -> None:
        self.id = audience_id
        self.name = name
        self.criteria = criteria
        self.created_at = datetime.now(timezone.utc)
        self.updated_at = self.created_at

    def __repr__(self) -> str:
        return f"<SavedAudience {self.name} id={self.id}>"


# ---------------------------------------------------------------------------
# AudienceBuilder
# ---------------------------------------------------------------------------

class AudienceBuilder:
    """Translates JSON audience criteria into SQLAlchemy queries and evaluates them.

    Criteria schema example::

        {
            "match": "all",          # "all" (AND) | "any" (OR)
            "rules": [
                {
                    "type": "demographic",
                    "field": "email",
                    "operator": "is_not_null"
                },
                {
                    "type": "behavioral",
                    "event_type": "page_view",
                    "operator": "gte",
                    "value": 5,
                    "time_window_days": 30
                },
                {
                    "type": "transactional",
                    "field": "lifetime_value",
                    "operator": "gte",
                    "value": 100.0
                },
                {
                    "type": "engagement",
                    "metric": "email_opens",
                    "operator": "gte",
                    "value": 3,
                    "time_window_days": 90
                },
                {
                    "type": "custom",
                    "attribute_key": "vip_tier",
                    "operator": "eq",
                    "value": "gold"
                }
            ]
        }
    """

    # Column map: field name -> Customer column
    _DEMOGRAPHIC_FIELDS: dict[str, str] = {
        "email": "email",
        "phone": "phone",
        "first_name": "first_name",
        "last_name": "last_name",
        "gender": "gender",
        "date_of_birth": "date_of_birth",
        "source": "source",
        "segment": "segment",
        "is_active": "is_active",
    }

    _TRANSACTIONAL_FIELDS: dict[str, str] = {
        "lifetime_value": "lifetime_value",
        "risk_score": "risk_score",
        "engagement_score": "engagement_score",
    }

    def __init__(self) -> None:
        self._saved_audiences: dict[uuid.UUID, SavedAudience] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def evaluate_audience(
        self,
        db_session: AsyncSession,
        criteria: dict[str, Any],
    ) -> list[Customer]:
        """Evaluate *criteria* and return matching :class:`Customer` objects."""
        stmt = self._build_query(criteria)
        result = await db_session.execute(stmt)
        customers: list[Customer] = list(result.scalars().all())
        logger.info("Audience evaluated: %d customers matched", len(customers))
        return customers

    async def build_audience(
        self,
        db_session: AsyncSession,
        criteria: dict[str, Any],
    ) -> list[uuid.UUID]:
        """Return a list of customer IDs matching *criteria*."""
        stmt = self._build_query(criteria).with_only_columns(Customer.id)
        result = await db_session.execute(stmt)
        ids: list[uuid.UUID] = [row[0] for row in result.fetchall()]
        logger.info("Audience built: %d customer IDs", len(ids))
        return ids

    async def estimate_audience_size(
        self,
        db_session: AsyncSession,
        criteria: dict[str, Any],
    ) -> int:
        """Return an estimated count without fetching full rows."""
        inner = self._build_query(criteria).with_only_columns(Customer.id).subquery()
        stmt = select(func.count()).select_from(inner)
        result = await db_session.execute(stmt)
        count: int = result.scalar_one()
        logger.info("Estimated audience size: %d", count)
        return count

    def save_audience(
        self,
        name: str,
        criteria: dict[str, Any],
        audience_id: uuid.UUID | None = None,
    ) -> SavedAudience:
        """Persist an audience definition (in-memory store)."""
        aid = audience_id or uuid.uuid4()
        audience = SavedAudience(audience_id=aid, name=name, criteria=criteria)
        self._saved_audiences[aid] = audience
        logger.info("Saved audience '%s' (%s)", name, aid)
        return audience

    async def get_audience_members(
        self,
        db_session: AsyncSession,
        audience_id: uuid.UUID,
    ) -> list[Customer]:
        """Resolve a saved audience by its ID and return matching customers."""
        audience = self._saved_audiences.get(audience_id)
        if audience is None:
            raise ValueError(f"Audience {audience_id} not found")
        return await self.evaluate_audience(db_session, audience.criteria)

    # ------------------------------------------------------------------
    # Query builder internals
    # ------------------------------------------------------------------

    def _build_query(self, criteria: dict[str, Any]):  # noqa: ANN202
        """Translate the top-level criteria dict into a SQLAlchemy ``Select``."""
        match_mode: str = criteria.get("match", "all")
        rules: list[dict[str, Any]] = criteria.get("rules", [])

        if not rules:
            return select(Customer).where(Customer.is_active.is_(True))

        clauses = [self._rule_to_clause(r) for r in rules]
        combiner = and_ if match_mode == "all" else or_
        return select(Customer).where(combiner(*clauses))

    def _rule_to_clause(self, rule: dict[str, Any]):  # noqa: ANN202
        """Dispatch a single rule dict to the appropriate clause builder."""
        criteria_type = rule.get("type", "demographic")

        if criteria_type == CriteriaType.DEMOGRAPHIC:
            return self._demographic_clause(rule)
        if criteria_type == CriteriaType.BEHAVIORAL:
            return self._behavioral_clause(rule)
        if criteria_type == CriteriaType.TRANSACTIONAL:
            return self._transactional_clause(rule)
        if criteria_type == CriteriaType.ENGAGEMENT:
            return self._engagement_clause(rule)
        if criteria_type == CriteriaType.CUSTOM:
            return self._custom_attribute_clause(rule)

        raise ValueError(f"Unknown criteria type: {criteria_type}")

    # -- demographic --------------------------------------------------

    def _demographic_clause(self, rule: dict[str, Any]):  # noqa: ANN202
        field_name = rule["field"]
        if field_name not in self._DEMOGRAPHIC_FIELDS:
            raise ValueError(f"Unsupported demographic field: {field_name}")
        column = getattr(Customer, self._DEMOGRAPHIC_FIELDS[field_name])
        return self._apply_operator(column, rule)

    # -- transactional ------------------------------------------------

    def _transactional_clause(self, rule: dict[str, Any]):  # noqa: ANN202
        field_name = rule["field"]
        if field_name not in self._TRANSACTIONAL_FIELDS:
            raise ValueError(f"Unsupported transactional field: {field_name}")
        column = getattr(Customer, self._TRANSACTIONAL_FIELDS[field_name])
        return self._apply_operator(column, rule)

    # -- behavioral (event counts) ------------------------------------

    def _behavioral_clause(self, rule: dict[str, Any]):  # noqa: ANN202
        """Build a sub-query counting events of a given type."""
        event_type: str = rule["event_type"]
        time_window_days: int | None = rule.get("time_window_days")

        filters = [
            CustomerEvent.customer_id == Customer.id,
            CustomerEvent.event_type == event_type,
        ]
        if time_window_days is not None:
            cutoff = datetime.now(timezone.utc) - timedelta(days=time_window_days)
            filters.append(CustomerEvent.timestamp >= cutoff)

        event_count = (
            select(func.count(CustomerEvent.id))
            .where(and_(*filters))
            .correlate(Customer)
            .scalar_subquery()
        )
        return self._apply_operator(event_count, rule)

    # -- engagement (email_opens, email_clicks, etc.) -----------------

    _ENGAGEMENT_EVENT_MAP: dict[str, str] = {
        "email_opens": "email_open",
        "email_clicks": "email_click",
        "push_opens": "push_open",
        "sms_replies": "sms_reply",
    }

    def _engagement_clause(self, rule: dict[str, Any]):  # noqa: ANN202
        metric: str = rule["metric"]
        mapped_event = self._ENGAGEMENT_EVENT_MAP.get(metric)
        if mapped_event is None:
            raise ValueError(f"Unsupported engagement metric: {metric}")

        time_window_days: int | None = rule.get("time_window_days")
        filters = [
            CustomerEvent.customer_id == Customer.id,
            CustomerEvent.event_type == mapped_event,
        ]
        if time_window_days is not None:
            cutoff = datetime.now(timezone.utc) - timedelta(days=time_window_days)
            filters.append(CustomerEvent.timestamp >= cutoff)

        metric_count = (
            select(func.count(CustomerEvent.id))
            .where(and_(*filters))
            .correlate(Customer)
            .scalar_subquery()
        )
        return self._apply_operator(metric_count, rule)

    # -- custom attributes --------------------------------------------

    def _custom_attribute_clause(self, rule: dict[str, Any]):  # noqa: ANN202
        attr_key: str = rule["attribute_key"]

        attr_value = (
            select(CustomerAttribute.attribute_value)
            .where(
                and_(
                    CustomerAttribute.customer_id == Customer.id,
                    CustomerAttribute.attribute_key == attr_key,
                )
            )
            .correlate(Customer)
            .scalar_subquery()
        )
        return self._apply_operator(attr_value, rule)

    # -- generic operator application ---------------------------------

    @staticmethod
    def _apply_operator(column, rule: dict[str, Any]):  # noqa: ANN202, ANN001
        """Apply a comparison operator from *rule* to *column*."""
        op = rule.get("operator", Operator.EQ)
        value = rule.get("value")

        if op == Operator.EQ:
            return column == value
        if op == Operator.NEQ:
            return column != value
        if op == Operator.GT:
            return column > value
        if op == Operator.GTE:
            return column >= value
        if op == Operator.LT:
            return column < value
        if op == Operator.LTE:
            return column <= value
        if op == Operator.IN:
            return column.in_(value)
        if op == Operator.NOT_IN:
            return column.not_in(value)
        if op == Operator.CONTAINS:
            return column.contains(value)
        if op == Operator.NOT_CONTAINS:
            return ~column.contains(value)
        if op == Operator.STARTS_WITH:
            return column.startswith(value)
        if op == Operator.ENDS_WITH:
            return column.endswith(value)
        if op == Operator.IS_NULL:
            return column.is_(None)
        if op == Operator.IS_NOT_NULL:
            return column.isnot(None)
        if op == Operator.BETWEEN:
            low, high = value
            return column.between(low, high)

        raise ValueError(f"Unsupported operator: {op}")
