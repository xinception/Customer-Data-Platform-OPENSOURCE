"""Tracking services for cookie management, event collection, consent, and identity resolution."""

from cdp.services.tracking.consent_manager import ConsentManager
from cdp.services.tracking.cookie_manager import CookieManager
from cdp.services.tracking.event_collector import EventCollector
from cdp.services.tracking.identity_resolver import IdentityResolver

__all__ = [
    "CookieManager",
    "ConsentManager",
    "EventCollector",
    "IdentityResolver",
]
