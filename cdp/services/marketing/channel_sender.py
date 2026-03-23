"""Multi-channel message delivery with rate limiting, retries, and tracking.

Supports Email (SMTP), SMS (Twilio placeholder), Push notifications, and
outbound Webhooks.  Every send is tracked and can be retried with exponential
back-off.
"""

from __future__ import annotations

import asyncio
import enum
import json
import logging
import smtplib
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any

import aiohttp
from jinja2 import BaseLoader, Environment

from cdp.config import settings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------

class DeliveryStatus(str, enum.Enum):
    QUEUED = "queued"
    SENT = "sent"
    DELIVERED = "delivered"
    BOUNCED = "bounced"
    FAILED = "failed"
    OPENED = "opened"
    CLICKED = "clicked"


class ChannelType(str, enum.Enum):
    EMAIL = "email"
    SMS = "sms"
    PUSH = "push"
    WEBHOOK = "webhook"


@dataclass
class DeliveryRecord:
    """Immutable record of a single send attempt."""

    id: uuid.UUID
    channel: ChannelType
    recipient: str
    status: DeliveryStatus
    sent_at: datetime
    attempts: int = 1
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Rate limiter (token-bucket per channel)
# ---------------------------------------------------------------------------

class _TokenBucket:
    """Simple async-compatible token-bucket rate limiter."""

    def __init__(self, rate: float, capacity: int) -> None:
        self._rate = rate            # tokens per second
        self._capacity = capacity
        self._tokens = float(capacity)
        self._last_refill = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_refill
            self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)
            self._last_refill = now

            if self._tokens < 1:
                wait = (1 - self._tokens) / self._rate
                logger.debug("Rate limiter sleeping %.2fs", wait)
                await asyncio.sleep(wait)
                self._tokens = 0
            else:
                self._tokens -= 1


# ---------------------------------------------------------------------------
# ChannelSender
# ---------------------------------------------------------------------------

class ChannelSender:
    """Unified interface for multi-channel message delivery.

    Parameters
    ----------
    max_retries:
        Maximum number of retry attempts on transient failures.
    base_backoff:
        Base delay in seconds for exponential back-off (``delay = base * 2^attempt``).
    rate_limits:
        Mapping of :class:`ChannelType` to ``(tokens_per_second, burst_capacity)``.
    """

    DEFAULT_RATE_LIMITS: dict[ChannelType, tuple[float, int]] = {
        ChannelType.EMAIL: (10.0, 50),
        ChannelType.SMS: (5.0, 20),
        ChannelType.PUSH: (20.0, 100),
        ChannelType.WEBHOOK: (15.0, 60),
    }

    def __init__(
        self,
        *,
        max_retries: int = 3,
        base_backoff: float = 1.0,
        rate_limits: dict[ChannelType, tuple[float, int]] | None = None,
    ) -> None:
        self._max_retries = max_retries
        self._base_backoff = base_backoff
        self._jinja_env = Environment(loader=BaseLoader(), autoescape=True)

        limits = rate_limits or self.DEFAULT_RATE_LIMITS
        self._buckets: dict[ChannelType, _TokenBucket] = {
            ch: _TokenBucket(rate, cap) for ch, (rate, cap) in limits.items()
        }

        # In-memory delivery log (swap for a DB-backed store in production)
        self._delivery_log: dict[uuid.UUID, DeliveryRecord] = {}

    # ------------------------------------------------------------------
    # Template rendering
    # ------------------------------------------------------------------

    def render_template(self, template_string: str, variables: dict[str, Any]) -> str:
        """Render a Jinja2 template string with the supplied variables."""
        template = self._jinja_env.from_string(template_string)
        return template.render(**variables)

    # ------------------------------------------------------------------
    # Email
    # ------------------------------------------------------------------

    async def send_email(
        self,
        to: str,
        subject: str,
        body: str,
        *,
        template_vars: dict[str, Any] | None = None,
        html: bool = True,
        from_addr: str | None = None,
    ) -> DeliveryRecord:
        """Send an email via SMTP with optional Jinja2 template rendering."""
        if template_vars:
            body = self.render_template(body, template_vars)
            subject = self.render_template(subject, template_vars)

        record = self._new_record(ChannelType.EMAIL, to)

        async def _send() -> None:
            msg = MIMEMultipart("alternative")
            msg["Subject"] = subject
            msg["From"] = from_addr or settings.SMTP_USER
            msg["To"] = to
            part = MIMEText(body, "html" if html else "plain", "utf-8")
            msg.attach(part)

            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, self._smtp_send, msg)

        await self._execute_with_retry(ChannelType.EMAIL, _send, record)
        return record

    def _smtp_send(self, msg: MIMEMultipart) -> None:
        """Blocking SMTP send, executed in a thread-pool."""
        host, port = settings.SMTP_HOST, settings.SMTP_PORT
        with smtplib.SMTP(host, port, timeout=30) as server:
            if settings.SMTP_USER and settings.SMTP_PASSWORD:
                server.starttls()
                server.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
            server.send_message(msg)

    # ------------------------------------------------------------------
    # SMS (Twilio placeholder)
    # ------------------------------------------------------------------

    async def send_sms(
        self,
        to: str,
        message: str,
        *,
        template_vars: dict[str, Any] | None = None,
    ) -> DeliveryRecord:
        """Send an SMS message (Twilio integration placeholder)."""
        if template_vars:
            message = self.render_template(message, template_vars)

        record = self._new_record(ChannelType.SMS, to)

        async def _send() -> None:
            # TODO: Replace with real Twilio client
            logger.info("SMS to %s: %s", to, message[:80])

        await self._execute_with_retry(ChannelType.SMS, _send, record)
        return record

    # ------------------------------------------------------------------
    # Push notifications
    # ------------------------------------------------------------------

    async def send_push(
        self,
        device_token: str,
        title: str,
        body: str,
        *,
        data: dict[str, Any] | None = None,
        template_vars: dict[str, Any] | None = None,
    ) -> DeliveryRecord:
        """Send a push notification (FCM/APNs placeholder)."""
        if template_vars:
            title = self.render_template(title, template_vars)
            body = self.render_template(body, template_vars)

        record = self._new_record(ChannelType.PUSH, device_token)

        async def _send() -> None:
            # TODO: Replace with real FCM / APNs client
            logger.info("Push to %s: %s - %s", device_token, title, body[:60])

        await self._execute_with_retry(ChannelType.PUSH, _send, record)
        return record

    # ------------------------------------------------------------------
    # Webhook
    # ------------------------------------------------------------------

    async def send_webhook(
        self,
        url: str,
        payload: dict[str, Any],
        *,
        headers: dict[str, str] | None = None,
        method: str = "POST",
    ) -> DeliveryRecord:
        """Fire an outbound webhook with JSON payload."""
        record = self._new_record(ChannelType.WEBHOOK, url)

        async def _send() -> None:
            req_headers = {"Content-Type": "application/json"}
            if headers:
                req_headers.update(headers)

            async with aiohttp.ClientSession() as session:
                async with session.request(
                    method,
                    url,
                    json=payload,
                    headers=req_headers,
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as resp:
                    if resp.status >= 400:
                        text = await resp.text()
                        raise RuntimeError(
                            f"Webhook returned {resp.status}: {text[:200]}"
                        )
                    record.metadata["response_status"] = resp.status

        await self._execute_with_retry(ChannelType.WEBHOOK, _send, record)
        return record

    # ------------------------------------------------------------------
    # Delivery tracking
    # ------------------------------------------------------------------

    def get_delivery_record(self, record_id: uuid.UUID) -> DeliveryRecord | None:
        return self._delivery_log.get(record_id)

    def update_delivery_status(
        self, record_id: uuid.UUID, status: DeliveryStatus
    ) -> None:
        record = self._delivery_log.get(record_id)
        if record:
            record.status = status
            logger.info("Delivery %s updated to %s", record_id, status.value)

    def get_delivery_stats(self) -> dict[str, Any]:
        """Aggregate delivery statistics across all channels."""
        stats: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        for rec in self._delivery_log.values():
            stats[rec.channel.value][rec.status.value] += 1
        return dict(stats)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _new_record(self, channel: ChannelType, recipient: str) -> DeliveryRecord:
        record = DeliveryRecord(
            id=uuid.uuid4(),
            channel=channel,
            recipient=recipient,
            status=DeliveryStatus.QUEUED,
            sent_at=datetime.now(timezone.utc),
        )
        self._delivery_log[record.id] = record
        return record

    async def _execute_with_retry(
        self,
        channel: ChannelType,
        send_fn: Any,
        record: DeliveryRecord,
    ) -> None:
        """Execute *send_fn* with rate limiting and exponential back-off retries."""
        bucket = self._buckets.get(channel)

        for attempt in range(1, self._max_retries + 1):
            try:
                if bucket:
                    await bucket.acquire()
                await send_fn()
                record.status = DeliveryStatus.SENT
                record.attempts = attempt
                logger.info(
                    "%s sent to %s (attempt %d)",
                    channel.value,
                    record.recipient,
                    attempt,
                )
                return
            except Exception as exc:
                delay = self._base_backoff * (2 ** (attempt - 1))
                logger.warning(
                    "%s attempt %d/%d failed for %s: %s – retrying in %.1fs",
                    channel.value,
                    attempt,
                    self._max_retries,
                    record.recipient,
                    exc,
                    delay,
                )
                record.error = str(exc)
                if attempt < self._max_retries:
                    await asyncio.sleep(delay)

        record.status = DeliveryStatus.FAILED
        record.attempts = self._max_retries
        logger.error(
            "%s delivery to %s failed after %d attempts: %s",
            channel.value,
            record.recipient,
            self._max_retries,
            record.error,
        )
