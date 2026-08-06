from __future__ import annotations

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace

import pytest
from sqlalchemy import Engine, func, inspect, select
from sqlalchemy.orm import Session, sessionmaker

from cf_agent_gateway.database import (
    create_database_engine,
    create_database_session_factory,
    initialize_database,
)
from cf_agent_gateway.identity.models import EnterpriseIdentity
from cf_agent_gateway.identity.service import IdentityService
from cf_agent_gateway.message.models import Conversation
from cf_agent_gateway.workspace.models import (
    AIThread,
    EmployeeWorkspace,
    ThreadPolicy,
    ThreadType,
)
from cf_agent_gateway.workspace.schemas import (
    AgentProfileRef,
    ConversationRef,
    SenderIdentityRef,
    SourceAccountRef,
    ThreadResolutionRequest,
)
from cf_agent_gateway.workspace.service import WorkspaceService
from cf_agent_gateway.workspace.thread_keys import THREAD_KEY_MAX_LENGTH
from cf_agent_gateway.workspace.thread_resolver import ThreadResolver


@pytest.fixture
def database_engine() -> Iterator[Engine]:
    engine = create_database_engine("sqlite+pysqlite:///:memory:")
    initialize_database(engine)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def session_factory(database_engine: Engine) -> sessionmaker[Session]:
    return create_database_session_factory(database_engine)


def create_identity(session: Session, employee_id: str) -> EnterpriseIdentity:
    return IdentityService(session).create_identity(
        employee_id=employee_id,
        display_name=f"Employee {employee_id}",
    )


def resolution_request(
    identity_id: str,
    *,
    policy: ThreadPolicy,
    conversation_id: str = "conversation-001",
    conversation_type: ThreadType | None = None,
    platform: str = "wechat",
    account_id: str = "bot-001",
    profile_id: str = "assistant",
    profile_revision: str = "1",
) -> ThreadResolutionRequest:
    resolved_type = conversation_type
    if resolved_type is None:
        resolved_type = (
            ThreadType.PRIVATE if policy is ThreadPolicy.PRIVATE_SENDER else ThreadType.GROUP
        )
    return ThreadResolutionRequest(
        conversation=ConversationRef(
            conversation_id=conversation_id,
            conversation_type=resolved_type,
        ),
        source_account=SourceAccountRef(
            platform=platform,
            account_id=account_id,
        ),
        sender_identity=SenderIdentityRef(identity_id=identity_id),
        agent_profile=AgentProfileRef(
            profile_id=profile_id,
            revision=profile_revision,
        ),
        thread_policy=policy,
    )


def test_private_sender_isolates_senders_in_the_same_private_conversation(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as session:
        identity_a = create_identity(session, "EMP-A")
        identity_b = create_identity(session, "EMP-B")
        resolver = ThreadResolver(session)

        thread_a = resolver.resolve(
            resolution_request(identity_a.id, policy=ThreadPolicy.PRIVATE_SENDER)
        )
        thread_b = resolver.resolve(
            resolution_request(identity_b.id, policy=ThreadPolicy.PRIVATE_SENDER)
        )

        assert thread_a.id != thread_b.id
        assert thread_a.thread_key != thread_b.thread_key
        assert thread_a.workspace_id != thread_b.workspace_id
        assert session.scalar(select(func.count()).select_from(AIThread)) == 2


def test_group_shared_reuses_one_thread_across_senders(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as session:
        identity_a = create_identity(session, "EMP-A")
        identity_b = create_identity(session, "EMP-B")
        resolver = ThreadResolver(session)

        thread_a = resolver.resolve(
            resolution_request(identity_a.id, policy=ThreadPolicy.GROUP_SHARED)
        )
        thread_b = resolver.resolve(
            resolution_request(identity_b.id, policy=ThreadPolicy.GROUP_SHARED)
        )

        assert thread_b.id == thread_a.id
        assert thread_b.thread_key == thread_a.thread_key
        assert session.scalar(select(func.count()).select_from(EmployeeWorkspace)) == 2
        assert session.scalar(select(func.count()).select_from(AIThread)) == 1


def test_group_sender_isolates_group_members(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as session:
        identity_a = create_identity(session, "EMP-A")
        identity_b = create_identity(session, "EMP-B")
        resolver = ThreadResolver(session)

        thread_a = resolver.resolve(
            resolution_request(identity_a.id, policy=ThreadPolicy.GROUP_SENDER)
        )
        thread_b = resolver.resolve(
            resolution_request(identity_b.id, policy=ThreadPolicy.GROUP_SENDER)
        )

        assert thread_a.id != thread_b.id
        assert thread_a.thread_key != thread_b.thread_key
        assert session.scalar(select(func.count()).select_from(AIThread)) == 2


def test_repeated_resolution_returns_the_same_thread(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as session:
        identity = create_identity(session, "EMP-A")
        request = resolution_request(identity.id, policy=ThreadPolicy.GROUP_SENDER)
        resolver = ThreadResolver(session)

        first = resolver.resolve(request)
        duplicate = resolver.resolve(
            conversation=request.conversation,
            source_account=request.source_account,
            sender_identity=request.sender_identity,
            agent_profile=request.agent_profile,
            thread_policy=request.thread_policy,
        )

        assert duplicate.id == first.id
        assert duplicate.thread_key == first.thread_key
        assert session.scalar(select(func.count()).select_from(AIThread)) == 1


def test_concurrent_first_resolution_creates_one_thread(tmp_path: Path) -> None:
    database_path = tmp_path / "thread-resolver-concurrency.db"
    engine = create_database_engine(f"sqlite+pysqlite:///{database_path.as_posix()}")
    initialize_database(engine)
    factory = create_database_session_factory(engine)
    workers = 4
    barrier = Barrier(workers)
    try:
        with factory() as session:
            identity = create_identity(session, "EMP-A")
            identity_id = identity.id
            WorkspaceService(session).ensure_workspace_for_authorized_identity(identity_id)

        def resolve() -> str:
            with factory() as session:
                request = resolution_request(identity_id, policy=ThreadPolicy.GROUP_SENDER)
                barrier.wait(timeout=10)
                return ThreadResolver(session).resolve(request).id

        with ThreadPoolExecutor(max_workers=workers) as executor:
            thread_ids = list(executor.map(lambda _: resolve(), range(workers)))

        assert len(set(thread_ids)) == 1
        with factory() as session:
            assert session.scalar(select(func.count()).select_from(AIThread)) == 1
    finally:
        engine.dispose()


def test_concurrent_group_shared_resolution_creates_distinct_workspaces_and_one_thread(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "thread-resolver-group-shared-concurrency.db"
    engine = create_database_engine(f"sqlite+pysqlite:///{database_path.as_posix()}")
    initialize_database(engine)
    factory = create_database_session_factory(engine)
    barrier = Barrier(2)
    try:
        with factory() as session:
            identity_ids = (
                create_identity(session, "EMP-A").id,
                create_identity(session, "EMP-B").id,
            )
            assert session.scalar(select(func.count()).select_from(EmployeeWorkspace)) == 0

        def resolve(identity_id: str) -> tuple[str, str]:
            with factory() as session:
                request = resolution_request(identity_id, policy=ThreadPolicy.GROUP_SHARED)
                barrier.wait(timeout=10)
                thread = ThreadResolver(session).resolve(request)
                workspace_id = session.scalar(
                    select(EmployeeWorkspace.id).where(
                        EmployeeWorkspace.enterprise_identity_id == identity_id
                    )
                )
                assert workspace_id is not None
                return thread.id, workspace_id

        with ThreadPoolExecutor(max_workers=2) as executor:
            resolutions = list(executor.map(resolve, identity_ids))

        thread_ids = {thread_id for thread_id, _ in resolutions}
        workspace_ids = {workspace_id for _, workspace_id in resolutions}
        assert len(thread_ids) == 1
        assert len(workspace_ids) == 2
        with factory() as session:
            assert session.scalar(select(func.count()).select_from(EmployeeWorkspace)) == 2
            assert session.scalar(select(func.count()).select_from(AIThread)) == 1
            assert session.scalar(select(AIThread.id)) == next(iter(thread_ids))
    finally:
        engine.dispose()


def test_v1_and_v2_thread_keys_can_coexist(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as session:
        identity = create_identity(session, "EMP-A")
        v1_thread = WorkspaceService(session).ensure_thread_for_authorized_request(
            enterprise_identity_id=identity.id,
            platform="wechat",
            account_id="bot-001",
            physical_conversation_id="conversation-001",
            conversation_type=ThreadType.GROUP.value,
            sender_id="wxid-a",
        )
        v2_thread = ThreadResolver(session).resolve(
            resolution_request(identity.id, policy=ThreadPolicy.GROUP_SHARED)
        )

        assert v1_thread.id != v2_thread.id
        assert v1_thread.thread_key.startswith("v1:")
        assert v2_thread.thread_key.startswith("v2:")
        assert session.scalar(select(func.count()).select_from(AIThread)) == 2


@pytest.mark.parametrize(
    "policy",
    [
        ThreadPolicy.PRIVATE_SENDER,
        ThreadPolicy.GROUP_SHARED,
        ThreadPolicy.GROUP_SENDER,
    ],
)
def test_profile_revision_selects_a_new_thread_for_every_policy(
    session_factory: sessionmaker[Session],
    policy: ThreadPolicy,
) -> None:
    with session_factory() as session:
        identity = create_identity(session, "EMP-A")
        resolver = ThreadResolver(session)

        revision_one = resolver.resolve(
            resolution_request(
                identity.id,
                policy=policy,
                profile_revision="1",
            )
        )
        revision_two = resolver.resolve(
            resolution_request(
                identity.id,
                policy=policy,
                profile_revision="2",
            )
        )

        assert revision_one.id != revision_two.id
        assert revision_one.thread_key != revision_two.thread_key
        assert session.scalar(select(func.count()).select_from(AIThread)) == 2


def test_resolver_accepts_domain_objects_as_inputs(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as session:
        identity = create_identity(session, "EMP-A")
        conversation = Conversation(
            source="wechat",
            source_account_id="bot-001",
            conversation_id="group-001",
            conversation_type=ThreadType.GROUP.value,
        )
        profile = SimpleNamespace(id="profile-revision-id", revision=7)

        thread = ThreadResolver(session).resolve(
            conversation=conversation,
            source_account=conversation,
            sender_identity=identity,
            agent_profile=profile,
            thread_policy=ThreadPolicy.GROUP_SENDER,
        )

        assert thread.thread_key.startswith("v2:sha256:group_sender:")


@pytest.mark.parametrize(
    ("policy", "conversation_type"),
    [
        (ThreadPolicy.PRIVATE_SENDER, ThreadType.PRIVATE),
        (ThreadPolicy.GROUP_SHARED, ThreadType.GROUP),
        (ThreadPolicy.GROUP_SENDER, ThreadType.GROUP),
    ],
)
def test_v2_thread_key_has_versioned_bounded_format(
    session_factory: sessionmaker[Session],
    policy: ThreadPolicy,
    conversation_type: ThreadType,
) -> None:
    with session_factory() as session:
        identity = create_identity(session, "EMP-A")
        thread = ThreadResolver(session).resolve(
            resolution_request(
                identity.id,
                policy=policy,
                conversation_type=conversation_type,
                conversation_id="c" * 255,
                platform="P" * 64,
                account_id="a" * 255,
                profile_id="p" * 255,
                profile_revision="r" * 255,
            )
        )

        assert thread.thread_key.startswith(f"v2:sha256:{policy.value}:")
        assert len(thread.thread_key) <= THREAD_KEY_MAX_LENGTH


def test_ai_thread_database_metadata_uniquely_constrains_thread_key(
    database_engine: Engine,
) -> None:
    constraints = inspect(database_engine).get_unique_constraints(AIThread.__tablename__)
    thread_key_constraint = next(
        constraint
        for constraint in constraints
        if set(constraint.get("column_names") or ()) == {"thread_key"}
    )

    assert thread_key_constraint["name"] == "uq_ai_thread_key"
