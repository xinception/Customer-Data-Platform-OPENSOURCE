"""CDP domain models – import all ORM classes for convenient access."""

from cdp.models.ai_model import AISegment, PredictionResult, Recommendation
from cdp.models.customer import (
    Customer,
    CustomerAttribute,
    CustomerEvent,
    CustomerIdentity,
)
from cdp.models.marketing import (
    AutomationWorkflow,
    Campaign,
    MarketingConsent,
    WorkflowExecution,
)
from cdp.models.tracking import CookieConsent, PageView, TrackingSession

__all__ = [
    "AISegment",
    "AutomationWorkflow",
    "Campaign",
    "CookieConsent",
    "Customer",
    "CustomerAttribute",
    "CustomerEvent",
    "CustomerIdentity",
    "MarketingConsent",
    "PageView",
    "PredictionResult",
    "Recommendation",
    "TrackingSession",
    "WorkflowExecution",
]
