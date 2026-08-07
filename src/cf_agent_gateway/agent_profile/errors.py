from __future__ import annotations


class AgentProfileStoreError(RuntimeError):
    code = "agent_profile_store_error"


class AgentProfileNotFoundError(AgentProfileStoreError):
    code = "agent_profile_not_found"

    def __init__(self, agent_profile_id: str) -> None:
        self.agent_profile_id = agent_profile_id
        super().__init__(f"agent profile not found: {agent_profile_id}")


class AgentProfileRevisionConflictError(AgentProfileStoreError):
    code = "agent_profile_revision_conflict"

    def __init__(self, profile_key: str, revision: int) -> None:
        self.profile_key = profile_key
        self.revision = revision
        super().__init__(f"agent profile revision already exists: {profile_key}@{revision}")


class AgentProfileIdConflictError(AgentProfileStoreError):
    code = "agent_profile_id_conflict"

    def __init__(self, agent_profile_id: str) -> None:
        self.agent_profile_id = agent_profile_id
        super().__init__(f"agent profile id is already assigned: {agent_profile_id}")


class AgentProfileRevisionImmutableError(AgentProfileStoreError):
    code = "agent_profile_revision_immutable"

    def __init__(self, profile_key: str, revision: int, changed_fields: tuple[str, ...]) -> None:
        self.profile_key = profile_key
        self.revision = revision
        self.changed_fields = changed_fields
        fields = ", ".join(changed_fields)
        super().__init__(
            f"agent profile revision is immutable: {profile_key}@{revision} ({fields})"
        )


class GroupTypeNotFoundError(AgentProfileStoreError):
    code = "group_type_not_found"

    def __init__(self, group_type_id: str) -> None:
        self.group_type_id = group_type_id
        super().__init__(f"group type not found: {group_type_id}")


class GroupTypeConflictError(AgentProfileStoreError):
    code = "group_type_conflict"

    def __init__(self, type_key: str) -> None:
        self.type_key = type_key
        super().__init__(f"group type already exists with different values: {type_key}")


class GroupTypeIdConflictError(AgentProfileStoreError):
    code = "group_type_id_conflict"

    def __init__(self, group_type_id: str) -> None:
        self.group_type_id = group_type_id
        super().__init__(f"group type id is already assigned: {group_type_id}")


class InvalidGroupThreadPolicyError(AgentProfileStoreError):
    code = "invalid_group_thread_policy"

    def __init__(self, thread_policy: str) -> None:
        self.thread_policy = thread_policy
        super().__init__(f"thread policy is not valid for a group type: {thread_policy}")


class ConversationNotFoundError(AgentProfileStoreError):
    code = "conversation_not_found"

    def __init__(self, conversation_record_id: int) -> None:
        self.conversation_record_id = conversation_record_id
        super().__init__(f"conversation not found: {conversation_record_id}")


class ConversationNotGroupError(AgentProfileStoreError):
    code = "conversation_not_group"

    def __init__(self, conversation_record_id: int) -> None:
        self.conversation_record_id = conversation_record_id
        super().__init__(f"conversation is not a group: {conversation_record_id}")


class ConversationNotPrivateError(AgentProfileStoreError):
    code = "conversation_not_private"

    def __init__(self, conversation_record_id: int) -> None:
        self.conversation_record_id = conversation_record_id
        super().__init__(f"conversation is not private: {conversation_record_id}")


class PrivateConversationProfileNotConfiguredError(AgentProfileStoreError):
    code = "private_conversation_profile_not_configured"

    def __init__(self, conversation_record_id: int) -> None:
        self.conversation_record_id = conversation_record_id
        super().__init__(
            f"private conversation has no agent profile binding: {conversation_record_id}"
        )


class UnknownGroupTypeNotConfiguredError(AgentProfileStoreError):
    code = "unknown_group_type_not_configured"

    def __init__(self) -> None:
        super().__init__("the unknown_group group type is not configured")
