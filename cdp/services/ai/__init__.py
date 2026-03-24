"""AI engine services for the Customer Data Platform.

Exports
-------
CustomerSegmentationEngine
    RFM-based customer segmentation via KMeans / DBSCAN clustering.
PredictionEngine
    Churn, lifetime-value, and next-purchase prediction using ensemble models.
RecommendationEngine
    Collaborative, content-based, and hybrid recommendation generation.
AnalyticsEngine
    Cohort analysis, funnel analysis, customer journey mapping, and real-time
    engagement metrics.
"""

from cdp.services.ai.analytics import AnalyticsEngine
from cdp.services.ai.prediction import PredictionEngine
from cdp.services.ai.recommendation import RecommendationEngine
from cdp.services.ai.segmentation import CustomerSegmentationEngine

__all__ = [
    "AnalyticsEngine",
    "CustomerSegmentationEngine",
    "PredictionEngine",
    "RecommendationEngine",
]
