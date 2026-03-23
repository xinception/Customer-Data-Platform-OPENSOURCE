"""Service layer for the Customer Data Platform.

Subpackages
-----------
- profile: Unified customer profile management, data unification, and enrichment.
- tracking: Event collection, cookie/consent management, and identity resolution.
- marketing: Audience building and multi-channel campaign delivery.
- ai: Segmentation, predictive models, and ML pipelines.
"""

from cdp.services.profile import (
    DataEnrichmentService,
    DataUnificationService,
    ProfileManager,
)

__all__ = [
    "ProfileManager",
    "DataUnificationService",
    "DataEnrichmentService",
]
