from __future__ import annotations

from dataclasses import dataclass

from cf_agent_gateway.workspace.models import ThreadPolicy, ThreadType


@dataclass(frozen=True, slots=True)
class ResolvedRoute:
    conversation_record_id: int
    source: str
    source_account_id: str
    conversation_id: str
    conversation_type: ThreadType
    enterprise_identity_id: str
    group_type_id: str | None
    group_type_key: str | None
    agent_profile_id: str
    agent_profile_key: str
    agent_profile_reference: str
    agent_profile_revision: int
    thread_policy: ThreadPolicy
