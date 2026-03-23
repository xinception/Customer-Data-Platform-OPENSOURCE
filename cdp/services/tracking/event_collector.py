"""Event collection service with validation, enrichment, deduplication, and async queue processing."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
import uuid
from enum import Enum
from typing import Any

import redis.asyncio as aioredis
from pydantic import BaseModel, Field

from cdp.config import settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
EVENT_QUEUE_KEY = "cdp:event_queue"
PROCESSED_SET_KEY = "cdp:events:processed"
DEDUP_TTL_SECONDS = 3600  # keep fingerprints for 1 hour


class EventSource(str, Enum):
    WEB = "web"
    MOBILE = "mobile"
    SERVER = "server"


class EventType(str, Enum):
    PAGE_VIEW = "page_view"
    TRACK = "track"
    IDENTIFY = "identify"
    CUSTOM = "custom"


# ---------------------------------------------------------------------------
# Pydantic schemas for event data
# ---------------------------------------------------------------------------

class EventData(BaseModel):
    """Canonical event payload."""

    event_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    event_type: str = "track"
    event_name: str = ""
    visitor_id: str | None = None
    session_id: str | None = None
    customer_id: str | None = None
    source: str = EventSource.WEB
    timestamp: float = Field(default_factory=time.time)
    properties: dict[str, Any] = Field(default_factory=dict)
    context: dict[str, Any] = Field(default_factory=dict)


class PageViewData(BaseModel):
    url: str
    title: str = ""
    referrer: str = ""
    visitor_id: str | None = None
    session_id: str | None = None
    properties: dict[str, Any] = Field(default_factory=dict)


class IdentityData(BaseModel):
    visitor_id: str
    customer_id: str | None = None
    email: str | None = None
    phone: str | None = None
    traits: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# EventCollector
# ---------------------------------------------------------------------------

class EventCollector:
    """Collects, validates, enriches, deduplicates, and queues tracking events.

    Events are pushed to a Redis list so that a background worker can process
    them asynchronously (see :meth:`process_event_queue`).
    """

    def __init__(self, redis_url: str | None = None) -> None:
        self._redis_url = redis_url or settings.REDIS_URL
        self._redis: aioredis.Redis | None = None

    # ------------------------------------------------------------------
    # Redis lifecycle
    # ------------------------------------------------------------------

    async def _get_redis(self) -> aioredis.Redis:
        if self._redis is None:
            self._redis = aioredis.from_url(
                self._redis_url,
                decode_responses=True,
            )
        return self._redis

    async def close(self) -> None:
        if self._redis is not None:
            await self._redis.close()
            self._redis = None

    # ------------------------------------------------------------------
    # Event fingerprinting / deduplication
    # ------------------------------------------------------------------

    @staticmethod
    def _fingerprint(event: dict[str, Any]) -> str:
        """Create a SHA-256 fingerprint for dedup purposes.

        Uses visitor_id + event_name + key properties + rounded timestamp so
        that exact-duplicate events within the same second are caught.
        """
        parts = [
            event.get("visitor_id", ""),
            event.get("event_name", ""),
            event.get("event_type", ""),
            str(int(event.get("timestamp", 0))),
            json.dumps(event.get("properties", {}), sort_keys=True),
        ]
        raw = "|".join(parts)
        return hashlib.sha256(raw.encode()).hexdigest()

    async def _is_duplicate(self, fingerprint: str) -> bool:
        r = await self._get_redis()
        added = await r.set(
            f"{PROCESSED_SET_KEY}:{fingerprint}",
            "1",
            nx=True,
            ex=DEDUP_TTL_SECONDS,
        )
        # `added` is True when the key was newly set (i.e. NOT a duplicate).
        return added is not True

    # ------------------------------------------------------------------
    # Event enrichment
    # ------------------------------------------------------------------

    @staticmethod
    def _enrich_event(event: dict[str, Any]) -> dict[str, Any]:
        """Add server-side enrichment fields.

        In a full deployment this would call GeoIP and device-detection
        services.  Here we add placeholder structure.
        """
        context = event.setdefault("context", {})

        # Geo-IP placeholder
        if "geo" not in context:
            context["geo"] = {
                "country": None,
                "region": None,
                "city": None,
                "latitude": None,
                "longitude": None,
            }

        # Device detection placeholder
        if "device" not in context:
            ua = context.get("user_agent", "")
            context["device"] = {
                "user_agent": ua,
                "type": None,
                "browser": None,
                "os": None,
            }

        # Server-side timestamp
        event["server_timestamp"] = time.time()
        return event

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_event(event: dict[str, Any]) -> list[str]:
        """Return a list of validation errors (empty if valid)."""
        errors: list[str] = []
        if not event.get("event_type"):
            errors.append("event_type is required")
        if not event.get("visitor_id") and not event.get("customer_id"):
            errors.append("Either visitor_id or customer_id is required")
        return errors

    # ------------------------------------------------------------------
    # Public tracking methods
    # ------------------------------------------------------------------

    async def track_event(self, event_data: dict[str, Any]) -> dict[str, Any]:
        """Validate, enrich, deduplicate, and enqueue a single event.

        Returns a receipt dict with ``event_id`` and ``status``.
        """
        event = EventData(**event_data).model_dump()

        errors = self._validate_event(event)
        if errors:
            logger.warning("Event validation failed: %s", errors)
            return {"status": "rejected", "errors": errors}

        event = self._enrich_event(event)

        fingerprint = self._fingerprint(event)
        if await self._is_duplicate(fingerprint):
            logger.info("Duplicate event dropped: %s", fingerprint[:12])
            return {"status": "duplicate", "event_id": event["event_id"]}

        r = await self._get_redis()
        await r.rpush(EVENT_QUEUE_KEY, json.dumps(event))
        logger.info("Queued event %s (%s)", event["event_id"], event["event_type"])

        return {"status": "accepted", "event_id": event["event_id"]}

    async def track_page_view(self, page_data: dict[str, Any]) -> dict[str, Any]:
        """Convenience wrapper for page-view events."""
        parsed = PageViewData(**page_data)
        event = {
            "event_type": EventType.PAGE_VIEW,
            "event_name": "page_view",
            "visitor_id": parsed.visitor_id,
            "session_id": parsed.session_id,
            "properties": {
                "url": parsed.url,
                "title": parsed.title,
                "referrer": parsed.referrer,
                **parsed.properties,
            },
        }
        return await self.track_event(event)

    async def track_identify(self, identity_data: dict[str, Any]) -> dict[str, Any]:
        """Track an identify call that links a visitor to a known identity."""
        parsed = IdentityData(**identity_data)
        event = {
            "event_type": EventType.IDENTIFY,
            "event_name": "identify",
            "visitor_id": parsed.visitor_id,
            "customer_id": parsed.customer_id,
            "properties": {
                "email": parsed.email,
                "phone": parsed.phone,
                **parsed.traits,
            },
        }
        return await self.track_event(event)

    async def batch_track(self, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Track multiple events in one call.  Returns a list of receipts."""
        results: list[dict[str, Any]] = []
        for ev in events:
            result = await self.track_event(ev)
            results.append(result)
        return results

    # ------------------------------------------------------------------
    # Queue worker
    # ------------------------------------------------------------------

    async def process_event_queue(
        self,
        handler: Any | None = None,
        batch_size: int = 50,
        poll_interval: float = 1.0,
    ) -> None:
        """Long-running worker that drains the event queue.

        For each event it invokes *handler(event_dict)*.  If no handler is
        provided the events are logged.

        This method runs indefinitely; call it as an ``asyncio.Task``.
        """
        r = await self._get_redis()
        logger.info("Event queue worker started (batch_size=%d)", batch_size)

        while True:
            try:
                batch: list[str] = []
                for _ in range(batch_size):
                    raw = await r.lpop(EVENT_QUEUE_KEY)
                    if raw is None:
                        break
                    batch.append(raw)

                if not batch:
                    await asyncio.sleep(poll_interval)
                    continue

                for raw_event in batch:
                    try:
                        event = json.loads(raw_event)
                        if handler is not None:
                            await handler(event)
                        else:
                            logger.debug("Processed event %s", event.get("event_id"))
                    except Exception:
                        logger.exception("Error processing event")

            except asyncio.CancelledError:
                logger.info("Event queue worker stopping")
                break
            except Exception:
                logger.exception("Event queue worker error, retrying")
                await asyncio.sleep(poll_interval)
