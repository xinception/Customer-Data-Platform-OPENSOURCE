"""Marketing automation services for campaign management, workflow execution,
audience building, and multi-channel delivery."""

from cdp.services.marketing.audience_builder import AudienceBuilder
from cdp.services.marketing.campaign_manager import CampaignManager, CampaignVariant
from cdp.services.marketing.channel_sender import ChannelSender
from cdp.services.marketing.workflow_engine import WorkflowEngine

__all__ = [
    "AudienceBuilder",
    "CampaignManager",
    "CampaignVariant",
    "ChannelSender",
    "WorkflowEngine",
]
