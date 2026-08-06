from __future__ import annotations

from collections.abc import Mapping

from sqlalchemy.orm import Session

from cf_agent_gateway.workspace.errors import ThreadUnavailableError
from cf_agent_gateway.workspace.models import AIThread, ThreadPolicy, ThreadStatus
from cf_agent_gateway.workspace.schemas import (
    AgentProfileRef,
    ConversationRef,
    SenderIdentityRef,
    SourceAccountRef,
    ThreadResolutionRequest,
)
from cf_agent_gateway.workspace.service import WorkspaceService
from cf_agent_gateway.workspace.store import WorkspaceStore
from cf_agent_gateway.workspace.thread_keys import build_v2_thread_key


class ThreadResolver:
    """Resolve V2 route facts without using the legacy conversation-scoped binding.

    For ``group_shared``, ``id`` and ``thread_key`` are the shared identity. The legacy
    non-null ``workspace_id`` remains the workspace of the thread's first creator.
    """

    def __init__(self, session: Session) -> None:
        self._workspace_service = WorkspaceService(session)
        self._store = WorkspaceStore(session)

    def resolve(
        self,
        request: ThreadResolutionRequest | Mapping[str, object] | None = None,
        *,
        conversation: ConversationRef | Mapping[str, object] | object | None = None,
        source_account: SourceAccountRef | Mapping[str, object] | object | None = None,
        sender_identity: SenderIdentityRef | Mapping[str, object] | object | None = None,
        agent_profile: AgentProfileRef | Mapping[str, object] | object | None = None,
        thread_policy: ThreadPolicy | str | None = None,
    ) -> AIThread:
        if request is not None:
            if any(
                value is not None
                for value in (
                    conversation,
                    source_account,
                    sender_identity,
                    agent_profile,
                    thread_policy,
                )
            ):
                raise ValueError("request cannot be combined with individual resolver inputs")
            resolution = ThreadResolutionRequest.model_validate(request)
        else:
            resolution = ThreadResolutionRequest.model_validate(
                {
                    "conversation": conversation,
                    "source_account": source_account,
                    "sender_identity": sender_identity,
                    "agent_profile": agent_profile,
                    "thread_policy": thread_policy,
                }
            )

        workspace = self._workspace_service.ensure_workspace_for_authorized_identity(
            resolution.sender_identity.identity_id
        )
        thread_key = build_v2_thread_key(
            platform=resolution.source_account.platform,
            account_id=resolution.source_account.account_id,
            physical_conversation_id=resolution.conversation.conversation_id,
            conversation_type=resolution.conversation.conversation_type,
            sender_identity_id=resolution.sender_identity.identity_id,
            agent_profile_id=resolution.agent_profile.profile_id,
            agent_profile_revision=resolution.agent_profile.revision,
            thread_policy=resolution.thread_policy,
        )
        thread, _ = self._store.ensure_thread(
            workspace_id=workspace.id,
            thread_type=resolution.conversation.conversation_type,
            thread_key=thread_key,
        )
        if thread.status is not ThreadStatus.ACTIVE:
            raise ThreadUnavailableError(thread.id, thread.status.value)
        return self._store.touch_thread(thread)
