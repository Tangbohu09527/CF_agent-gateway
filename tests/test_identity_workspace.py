from __future__ import annotations

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from cf_agent_gateway.database import (
    create_database_engine,
    create_database_session_factory,
    initialize_database,
)
from cf_agent_gateway.identity.errors import (
    EmployeeIdConflictError,
    SourceIdentityConflictError,
)
from cf_agent_gateway.identity.models import (
    EnterpriseIdentity,
    IdentityStatus,
    SourceIdentityMapping,
)
from cf_agent_gateway.identity.schemas import IdentityResolutionStatus
from cf_agent_gateway.identity.service import IdentityService
from cf_agent_gateway.workspace.errors import ThreadUnavailableError, WorkspaceUnavailableError
from cf_agent_gateway.workspace.models import (
    AIThread,
    EmployeeWorkspace,
    ThreadSourceBinding,
    ThreadStatus,
    WorkspaceStatus,
)
from cf_agent_gateway.workspace.service import WorkspaceService
from cf_agent_gateway.workspace.thread_keys import (
    build_group_thread_key,
    build_private_thread_key,
)


@pytest.fixture
def session_factory() -> Iterator[sessionmaker[Session]]:
    engine = create_database_engine("sqlite+pysqlite:///:memory:")
    initialize_database(engine)
    factory = create_database_session_factory(engine)
    try:
        yield factory
    finally:
        engine.dispose()


def create_identity(session: Session, employee_id: str | None = "EMP-001") -> EnterpriseIdentity:
    return IdentityService(session).create_identity(
        employee_id=employee_id,
        display_name="Example Employee",
    )


def thread_request(
    identity_id: str,
    *,
    platform: str = "wechat",
    account_id: str = "bot-001",
    conversation_id: str = "group-001",
    conversation_type: str = "group",
    sender_id: str = "wxid-001",
) -> dict[str, str]:
    return {
        "enterprise_identity_id": identity_id,
        "platform": platform,
        "account_id": account_id,
        "physical_conversation_id": conversation_id,
        "conversation_type": conversation_type,
        "sender_id": sender_id,
    }


def test_create_enterprise_identity_with_optional_employee_id(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as session:
        first = create_identity(session)
        second = create_identity(session, employee_id=None)

        assert first.id != "EMP-001"
        assert first.employee_id == "EMP-001"
        assert first.status is IdentityStatus.ACTIVE
        assert second.employee_id is None
        assert second.id != first.id


def test_non_null_employee_id_is_unique(session_factory: sessionmaker[Session]) -> None:
    with session_factory() as session:
        existing = create_identity(session)

        with pytest.raises(EmployeeIdConflictError) as exc_info:
            create_identity(session)

        assert exc_info.value.code == "employee_id_conflict"
        assert exc_info.value.existing_identity_id == existing.id


def test_create_and_resolve_source_mapping(session_factory: sessionmaker[Session]) -> None:
    with session_factory() as session:
        identity = create_identity(session)
        service = IdentityService(session)
        mapping = service.create_mapping(
            platform="wechat",
            account_id="bot-001",
            sender_id="wxid-001",
            enterprise_identity_id=identity.id,
        )

        resolution = service.resolve_identity(
            platform="wechat", account_id="bot-001", sender_id="wxid-001"
        )

        assert mapping.enterprise_identity_id == identity.id
        assert resolution.status is IdentityResolutionStatus.RESOLVED
        assert resolution.enterprise_identity_id == identity.id
        assert resolution.employee_id == "EMP-001"
        assert resolution.is_executable


def test_resolution_uses_source_key_not_display_name(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as session:
        identity = create_identity(session)
        service = IdentityService(session)

        unresolved = service.resolve_identity(
            platform="wechat",
            account_id="bot-001",
            sender_id=identity.display_name or "",
        )

        assert unresolved.status is IdentityResolutionStatus.UNRESOLVED
        assert unresolved.enterprise_identity_id is None


def test_unmapped_and_disabled_mapping_resolution(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as session:
        identity = create_identity(session)
        service = IdentityService(session)
        unresolved = service.resolve_identity(
            platform="wechat", account_id="bot-001", sender_id="unknown"
        )
        service.create_mapping(
            platform="wechat",
            account_id="bot-001",
            sender_id="wxid-001",
            enterprise_identity_id=identity.id,
        )
        service.disable_mapping(platform="wechat", account_id="bot-001", sender_id="wxid-001")

        disabled = service.resolve_identity(
            platform="wechat", account_id="bot-001", sender_id="wxid-001"
        )

        assert unresolved.status is IdentityResolutionStatus.UNRESOLVED
        assert disabled.status is IdentityResolutionStatus.DISABLED
        assert not disabled.is_executable


@pytest.mark.parametrize("status", [IdentityStatus.DISABLED, IdentityStatus.ARCHIVED])
def test_inactive_enterprise_identity_is_not_resolved(
    session_factory: sessionmaker[Session], status: IdentityStatus
) -> None:
    with session_factory() as session:
        identity = create_identity(session)
        service = IdentityService(session)
        service.create_mapping(
            platform="wechat",
            account_id="bot-001",
            sender_id="wxid-001",
            enterprise_identity_id=identity.id,
        )
        identity.status = status
        session.commit()

        resolution = service.resolve_identity(
            platform="wechat", account_id="bot-001", sender_id="wxid-001"
        )

        assert resolution.status is IdentityResolutionStatus.DISABLED
        assert not resolution.is_executable


def test_same_source_mapping_is_idempotent_and_different_identity_conflicts(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as session:
        first_identity = create_identity(session)
        second_identity = create_identity(session, employee_id="EMP-002")
        service = IdentityService(session)
        first = service.create_mapping(
            platform="wechat",
            account_id="bot-001",
            sender_id="wxid-001",
            enterprise_identity_id=first_identity.id,
        )
        duplicate = service.create_mapping(
            platform="wechat",
            account_id="bot-001",
            sender_id="wxid-001",
            enterprise_identity_id=first_identity.id,
        )

        assert duplicate.id == first.id
        with pytest.raises(SourceIdentityConflictError) as exc_info:
            service.create_mapping(
                platform="wechat",
                account_id="bot-001",
                sender_id="wxid-001",
                enterprise_identity_id=second_identity.id,
            )
        assert exc_info.value.code == "source_identity_conflict"
        assert exc_info.value.existing_identity_id == first_identity.id


def test_workspace_ensure_is_stable(session_factory: sessionmaker[Session]) -> None:
    with session_factory() as session:
        identity = create_identity(session)
        service = WorkspaceService(session)

        first = service.ensure_workspace_for_authorized_identity(identity.id)
        second = service.ensure_workspace_for_authorized_identity(identity.id)

        assert second.id == first.id
        assert session.scalar(select(func.count()).select_from(EmployeeWorkspace)) == 1


def test_thread_key_is_stable_and_collision_resistant() -> None:
    private = build_private_thread_key(
        platform="wechat", account_id="bot-001", private_chat_id="chat-001"
    )
    assert private == build_private_thread_key(
        platform="wechat", account_id="bot-001", private_chat_id="chat-001"
    )
    assert private != build_private_thread_key(
        platform="wechat", account_id="bot-002", private_chat_id="chat-001"
    )
    assert private != build_private_thread_key(
        platform="slack", account_id="bot-001", private_chat_id="chat-001"
    )

    group = build_group_thread_key(
        platform="wechat",
        account_id="bot-001",
        group_chat_id="group|with|separators",
        sender_id="sender|one",
    )
    assert group == build_group_thread_key(
        platform="wechat",
        account_id="bot-001",
        group_chat_id="group|with|separators",
        sender_id="sender|one",
    )
    assert group != build_group_thread_key(
        platform="wechat",
        account_id="bot-001",
        group_chat_id="group|with|separators",
        sender_id="sender|two",
    )


def test_group_thread_isolation_and_source_binding(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as session:
        identity = create_identity(session)
        service = WorkspaceService(session)
        first = service.ensure_thread_for_authorized_request(**thread_request(identity.id))
        duplicate = service.ensure_thread_for_authorized_request(**thread_request(identity.id))
        other_sender = service.ensure_thread_for_authorized_request(
            **thread_request(identity.id, sender_id="wxid-002")
        )
        other_account = service.ensure_thread_for_authorized_request(
            **thread_request(identity.id, account_id="bot-002")
        )
        other_platform = service.ensure_thread_for_authorized_request(
            **thread_request(identity.id, platform="slack")
        )

        assert duplicate.id == first.id
        assert len({first.id, other_sender.id, other_account.id, other_platform.id}) == 4
        assert session.scalar(select(func.count()).select_from(AIThread)) == 4
        assert session.scalar(select(func.count()).select_from(ThreadSourceBinding)) == 4


def test_private_thread_does_not_depend_on_sender_id(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as session:
        identity = create_identity(session)
        service = WorkspaceService(session)
        first = service.ensure_thread_for_authorized_request(
            **thread_request(
                identity.id,
                conversation_id="private-001",
                conversation_type="private",
                sender_id="wxid-001",
            )
        )
        same_chat = service.ensure_thread_for_authorized_request(
            **thread_request(
                identity.id,
                conversation_id="private-001",
                conversation_type="private",
                sender_id="ignored-for-thread-key",
            )
        )

        assert same_chat.id == first.id


def test_hermes_thread_can_be_rebound_without_changing_ai_thread_id(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as session:
        identity = create_identity(session)
        service = WorkspaceService(session)
        thread = service.ensure_thread_for_authorized_request(**thread_request(identity.id))
        assert thread.hermes_thread_id is None

        first_id = thread.id
        assert service.bind_hermes_thread(first_id, "hermes-001").hermes_thread_id == "hermes-001"
        rebound = service.bind_hermes_thread(first_id, "hermes-002")
        cleared = service.bind_hermes_thread(first_id, None)

        assert rebound.id == first_id
        assert cleared.id == first_id
        assert cleared.hermes_thread_id is None
        assert session.scalar(select(func.count()).select_from(AIThread)) == 1


def test_disabled_workspace_and_thread_are_not_executable(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as session:
        identity = create_identity(session)
        service = WorkspaceService(session)
        thread = service.ensure_thread_for_authorized_request(**thread_request(identity.id))
        workspace = session.get(EmployeeWorkspace, thread.workspace_id)
        assert workspace is not None

        thread.status = ThreadStatus.DISABLED
        session.commit()
        with pytest.raises(ThreadUnavailableError):
            service.ensure_thread_for_authorized_request(**thread_request(identity.id))

        thread.status = ThreadStatus.ACTIVE
        workspace.status = WorkspaceStatus.ARCHIVED
        session.commit()
        with pytest.raises(WorkspaceUnavailableError):
            service.ensure_thread_for_authorized_request(**thread_request(identity.id))


def test_source_ids_are_not_copied_to_employee_id(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as session:
        identity = create_identity(session, employee_id=None)
        IdentityService(session).create_mapping(
            platform="wechat",
            account_id="bot-001",
            sender_id="wxid-001",
            enterprise_identity_id=identity.id,
        )

        assert identity.employee_id is None


def test_service_rejects_empty_source_keys(session_factory: sessionmaker[Session]) -> None:
    with session_factory() as session, pytest.raises(ValidationError):
        IdentityService(session).resolve_identity(
            platform="wechat", account_id="bot-001", sender_id=" "
        )


def test_concurrent_ensures_do_not_create_duplicate_rows(tmp_path: Path) -> None:
    database_path = tmp_path / "concurrency.db"
    engine = create_database_engine(f"sqlite+pysqlite:///{database_path.as_posix()}")
    initialize_database(engine)
    factory = create_database_session_factory(engine)
    try:
        with factory() as session:
            identity = create_identity(session)
            identity_id = identity.id

        def ensure() -> tuple[str, str, str]:
            with factory() as session:
                mapping = IdentityService(session).create_mapping(
                    platform="wechat",
                    account_id="bot-001",
                    sender_id="wxid-001",
                    enterprise_identity_id=identity_id,
                )
                service = WorkspaceService(session)
                workspace = service.ensure_workspace_for_authorized_identity(identity_id)
                thread = service.ensure_thread_for_authorized_request(**thread_request(identity_id))
                return mapping.id, workspace.id, thread.id

        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(lambda _: ensure(), range(8)))

        assert len({mapping_id for mapping_id, _, _ in results}) == 1
        assert len({workspace_id for _, workspace_id, _ in results}) == 1
        assert len({thread_id for _, _, thread_id in results}) == 1
        with factory() as session:
            assert session.scalar(select(func.count()).select_from(EmployeeWorkspace)) == 1
            assert session.scalar(select(func.count()).select_from(AIThread)) == 1
            assert session.scalar(select(func.count()).select_from(SourceIdentityMapping)) == 1
    finally:
        engine.dispose()
