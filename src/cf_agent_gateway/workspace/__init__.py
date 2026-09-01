"""Employee workspace and AI thread domain."""

from cf_agent_gateway.workspace.models import ThreadPolicy
from cf_agent_gateway.workspace.schemas import (
    AgentProfileRef,
    ConversationRef,
    SenderIdentityRef,
    SourceAccountRef,
    ThreadResolutionRequest,
)
from cf_agent_gateway.workspace.service import WorkspaceService
from cf_agent_gateway.workspace.thread_resolver import ThreadResolver

__all__ = [
    "AgentProfileRef",
    "ConversationRef",
    "SenderIdentityRef",
    "SourceAccountRef",
    "ThreadPolicy",
    "ThreadResolutionRequest",
    "ThreadResolver",
    "WorkspaceService",
]
