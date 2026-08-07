from __future__ import annotations


class RouteResolutionError(RuntimeError):
    code = "route_resolution_error"


class RouteConversationNotFoundError(RouteResolutionError):
    code = "route_conversation_not_found"

    def __init__(self) -> None:
        super().__init__("persisted conversation was not found for the route facts")


class RouteConversationTypeConflictError(RouteResolutionError):
    code = "route_conversation_type_conflict"

    def __init__(self, *, persisted_type: str, requested_type: str) -> None:
        self.persisted_type = persisted_type
        self.requested_type = requested_type
        super().__init__("persisted conversation type does not match the route facts")


class RouteGroupTypeUnavailableError(RouteResolutionError):
    code = "route_group_type_unavailable"

    def __init__(self, group_type_id: str, status: str) -> None:
        self.group_type_id = group_type_id
        self.status = status
        super().__init__(f"group type is not active: {status}")


class RouteAgentProfileUnavailableError(RouteResolutionError):
    code = "route_agent_profile_unavailable"

    def __init__(self, agent_profile_id: str, status: str) -> None:
        self.agent_profile_id = agent_profile_id
        self.status = status
        super().__init__(f"agent profile is not active: {status}")
