from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from cf_agent_gateway.admin.auth import get_authenticated_roles
from cf_agent_gateway.admission import AdmissionOutcome, AdmissionReason
from cf_agent_gateway.delivery.models import DeliveryOutboxRecord, DeliveryStatus
from cf_agent_gateway.hermes.models import HermesDispatchOutcome, ResponseEnvelope, TextPart
from cf_agent_gateway.identity.models import EnterpriseIdentity, SourceIdentityMapping
from cf_agent_gateway.identity.service import IdentityService
from cf_agent_gateway.message.models import Conversation, Message
from cf_agent_gateway.response import DeliveryTarget, ResponseStore
from cf_agent_gateway.task.model import HermesDispatchRecord, HermesDispatchRecordStore
from cf_agent_gateway.workspace.models import EmployeeWorkspace
from cf_agent_gateway.workspace.service import WorkspaceService


@dataclass(frozen=True, slots=True)
class ArchiveEntry:
    conversation_record_id: int
    message_id: int
    identity_id: str
    workspace_id: str
    thread_id: str
    dispatch_id: int
    response_id: str
    delivery_id: int


def database_session_factory(client: TestClient) -> sessionmaker[Session]:
    return client.app.state.database_session_factory


@contextmanager
def authenticated_roles(client: TestClient, *roles: str) -> Iterator[None]:
    client.app.dependency_overrides[get_authenticated_roles] = lambda: frozenset(roles)
    try:
        yield
    finally:
        client.app.dependency_overrides.pop(get_authenticated_roles, None)


def admin_get(
    client: TestClient,
    path: str,
    *,
    params: dict[str, object] | None = None,
):
    with authenticated_roles(client, "admin"):
        return client.get(path, params=params)


def non_admin_get(client: TestClient, path: str):
    with authenticated_roles(client, "member"):
        return client.get(path)


def admin_write_attempt(client: TestClient, method: str, path: str):
    with authenticated_roles(client, "admin"):
        return client.request(method, path, json={"status": "mutated"})


def create_archive_entry(
    client: TestClient,
    suffix: str,
    *,
    occurred_at: datetime,
    source: str = "wechat",
    source_account_id: str = "gateway-a",
    conversation_id: str | None = None,
    identity_id: str | None = None,
) -> ArchiveEntry:
    physical_conversation_id = conversation_id or f"conversation-{suffix}"
    created = client.post(
        "/internal/messages",
        json={
            "event_id": f"event-{suffix}",
            "source": source,
            "source_account_id": source_account_id,
            "source_message_id": f"source-message-{suffix}",
            "conversation_id": physical_conversation_id,
            "conversation_type": "private",
            "is_mentioned": None,
            "is_self": False,
            "conversation_name": f"Conversation {suffix}",
            "sender_type": "human",
            "sender_id": f"sender-{suffix}",
            "sender_name": f"Sender {suffix}",
            "message_type": "text",
            "content": f"Message {suffix}",
            "timestamp": occurred_at.isoformat(),
            "received_at": (occurred_at + timedelta(seconds=1)).isoformat(),
        },
    )
    assert created.status_code == 201, created.text
    message_id = created.json()["id"]

    with database_session_factory(client)() as session:
        message = session.get(Message, message_id)
        conversation = session.scalar(
            select(Conversation).where(
                Conversation.source == source,
                Conversation.source_account_id == source_account_id,
                Conversation.conversation_id == physical_conversation_id,
            )
        )
        assert message is not None
        assert conversation is not None

        identity_service = IdentityService(session)
        if identity_id is None:
            identity = identity_service.create_identity(
                employee_id=f"employee-{suffix}",
                display_name=f"Employee {suffix}",
            )
        else:
            identity = session.get(EnterpriseIdentity, identity_id)
            assert identity is not None

        identity_service.create_mapping(
            platform=source,
            account_id=source_account_id,
            sender_id=f"sender-{suffix}",
            enterprise_identity_id=identity.id,
        )

        thread = WorkspaceService(session).ensure_thread_for_authorized_request(
            enterprise_identity_id=identity.id,
            platform=source,
            account_id=source_account_id,
            physical_conversation_id=physical_conversation_id,
            conversation_type="private",
            sender_id=f"sender-{suffix}",
        )
        workspace = session.get(EmployeeWorkspace, thread.workspace_id)
        assert workspace is not None
        dispatch, dispatch_created = HermesDispatchRecordStore(session).enqueue(
            AdmissionOutcome(
                message_id=message.id,
                admitted=True,
                should_create_task=True,
                reason=AdmissionReason.ALLOWED,
                enterprise_identity_id=identity.id,
                workspace_id=workspace.id,
                ai_thread_id=thread.id,
            )
        )
        assert dispatch_created

        response, delivery, response_created = ResponseStore(session).save_generated(
            HermesDispatchOutcome.from_response(
                message_id=message.id,
                workspace_id=workspace.id,
                ai_thread_id=thread.id,
                response=ResponseEnvelope(
                    response_id=f"response-{suffix}",
                    parts=(TextPart(text=f"Response {suffix}"),),
                ),
            ),
            target=DeliveryTarget(
                channel=source,
                account_id=source_account_id,
                conversation_id=physical_conversation_id,
            ),
        )
        assert response_created

        message.timestamp = occurred_at
        message.occurred_at = occurred_at
        message.received_at = occurred_at + timedelta(seconds=1)
        message.created_at = occurred_at + timedelta(seconds=1)
        conversation.created_at = occurred_at
        conversation.updated_at = occurred_at
        dispatch.created_at = occurred_at + timedelta(seconds=2)
        dispatch.updated_at = occurred_at + timedelta(seconds=2)
        response.generated_at = occurred_at + timedelta(seconds=3)
        response.created_at = occurred_at + timedelta(seconds=3)
        response.updated_at = occurred_at + timedelta(seconds=3)
        delivery.available_at = occurred_at
        delivery.created_at = occurred_at
        delivery.updated_at = occurred_at
        session.commit()

        return ArchiveEntry(
            conversation_record_id=conversation.id,
            message_id=message.id,
            identity_id=identity.id,
            workspace_id=workspace.id,
            thread_id=thread.id,
            dispatch_id=dispatch.id,
            response_id=response.response_id,
            delivery_id=delivery.id,
        )


@pytest.mark.parametrize(
    "path",
    [
        "/admin/conversations",
        "/admin/messages",
        "/admin/threads/not-a-thread",
        "/admin/deliveries",
    ],
)
def test_admin_archive_endpoints_require_a_role(client: TestClient, path: str) -> None:
    response = client.get(path)

    assert response.status_code == 401


@pytest.mark.parametrize(
    "path",
    [
        "/admin/conversations",
        "/admin/messages",
        "/admin/threads/not-a-thread",
        "/admin/deliveries",
    ],
)
def test_admin_archive_endpoints_reject_non_admin_roles(
    client: TestClient,
    path: str,
) -> None:
    with authenticated_roles(client, "member"):
        response = client.get(path)

    assert response.status_code == 403


@pytest.mark.parametrize(
    ("path", "entry_attribute"),
    [
        ("/admin/conversations", "conversation_record_id"),
        ("/admin/messages", "message_id"),
        ("/admin/deliveries", "delivery_id"),
    ],
)
def test_admin_list_pagination_is_complete_and_stable(
    client: TestClient,
    path: str,
    entry_attribute: str,
) -> None:
    same_time = datetime(2026, 8, 10, 9, 0, tzinfo=UTC)
    entries = [
        create_archive_entry(client, f"page-{index}", occurred_at=same_time) for index in range(5)
    ]

    pages = [
        admin_get(
            client,
            path,
            params={"limit": 2, "offset": offset},
        )
        for offset in (0, 2, 4)
    ]

    assert [response.status_code for response in pages] == [200, 200, 200]
    bodies = [response.json() for response in pages]
    for offset, body in zip((0, 2, 4), bodies, strict=True):
        assert set(body) == {"items", "total", "limit", "offset"}
        assert body["total"] == 5
        assert body["limit"] == 2
        assert body["offset"] == offset
    assert [len(body["items"]) for body in bodies] == [2, 2, 1]

    actual_ids = [item["id"] for body in bodies for item in body["items"]]
    expected_ids = [getattr(entry, entry_attribute) for entry in entries]
    assert len(actual_ids) == len(set(actual_ids)) == 5
    assert set(actual_ids) == set(expected_ids)

    repeated = admin_get(
        client,
        path,
        params={"limit": 2, "offset": 0},
    )
    assert repeated.status_code == 200
    assert repeated.json()["items"] == bodies[0]["items"]


@pytest.mark.parametrize(
    ("path", "entry_attribute"),
    [
        ("/admin/conversations", "conversation_record_id"),
        ("/admin/messages", "message_id"),
        ("/admin/deliveries", "delivery_id"),
    ],
)
def test_admin_time_window_includes_start_and_excludes_end(
    client: TestClient,
    path: str,
    entry_attribute: str,
) -> None:
    start = datetime(2026, 8, 10, 10, 0, tzinfo=UTC)
    end = datetime(2026, 8, 10, 11, 0, tzinfo=UTC)
    before = create_archive_entry(
        client,
        f"time-before-{entry_attribute}",
        occurred_at=start - timedelta(microseconds=1),
    )
    at_start = create_archive_entry(
        client,
        f"time-start-{entry_attribute}",
        occurred_at=start,
    )
    inside = create_archive_entry(
        client,
        f"time-inside-{entry_attribute}",
        occurred_at=start + timedelta(minutes=30),
    )
    at_end = create_archive_entry(
        client,
        f"time-end-{entry_attribute}",
        occurred_at=end,
    )

    response = admin_get(
        client,
        path,
        params={"start_time": start.isoformat(), "end_time": end.isoformat()},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 2
    actual_ids = {item["id"] for item in body["items"]}
    assert actual_ids == {
        getattr(at_start, entry_attribute),
        getattr(inside, entry_attribute),
    }
    assert getattr(before, entry_attribute) not in actual_ids
    assert getattr(at_end, entry_attribute) not in actual_ids


@pytest.mark.parametrize(
    ("path", "entry_attribute"),
    [
        ("/admin/conversations", "conversation_record_id"),
        ("/admin/messages", "message_id"),
        ("/admin/deliveries", "delivery_id"),
    ],
)
def test_identity_and_composite_conversation_filters_are_anded_and_isolated(
    client: TestClient,
    path: str,
    entry_attribute: str,
) -> None:
    occurred_at = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)
    target = create_archive_entry(
        client,
        f"isolation-target-{entry_attribute}",
        occurred_at=occurred_at,
        source="wechat",
        source_account_id="gateway-a",
        conversation_id="shared-conversation",
    )
    create_archive_entry(
        client,
        f"isolation-identity-{entry_attribute}",
        occurred_at=occurred_at + timedelta(minutes=1),
        conversation_id="identity-other-conversation",
        identity_id=target.identity_id,
    )
    create_archive_entry(
        client,
        f"isolation-account-{entry_attribute}",
        occurred_at=occurred_at + timedelta(minutes=2),
        source="wechat",
        source_account_id="gateway-b",
        conversation_id="shared-conversation",
    )
    create_archive_entry(
        client,
        f"isolation-source-{entry_attribute}",
        occurred_at=occurred_at + timedelta(minutes=3),
        source="slack",
        source_account_id="gateway-a",
        conversation_id="shared-conversation",
    )

    response = admin_get(
        client,
        path,
        params={
            "identity_id": target.identity_id,
            "source": "wechat",
            "source_account_id": "gateway-a",
            "conversation_id": "shared-conversation",
        },
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 1
    assert [item["id"] for item in body["items"]] == [getattr(target, entry_attribute)]


def test_thread_detail_is_admin_only_and_timeline_is_isolated(
    client: TestClient,
) -> None:
    first_time = datetime(2026, 8, 10, 13, 0, tzinfo=UTC)
    first = create_archive_entry(
        client,
        "thread-first",
        occurred_at=first_time,
        conversation_id="thread-conversation",
    )
    second = create_archive_entry(
        client,
        "thread-second",
        occurred_at=first_time + timedelta(minutes=1),
        conversation_id="thread-conversation",
        identity_id=first.identity_id,
    )
    other = create_archive_entry(
        client,
        "thread-other",
        occurred_at=first_time + timedelta(minutes=2),
        conversation_id="other-thread-conversation",
    )
    assert second.thread_id == first.thread_id
    assert other.thread_id != first.thread_id

    with database_session_factory(client)() as session:
        first_delivery = session.get(DeliveryOutboxRecord, first.delivery_id)
        second_delivery = session.get(DeliveryOutboxRecord, second.delivery_id)
        assert first_delivery is not None
        assert second_delivery is not None
        first_delivery.status = DeliveryStatus.DELIVERED
        first_delivery.completed_at = first_time + timedelta(minutes=3)
        second_delivery.status = DeliveryStatus.FAILED
        second_delivery.completed_at = first_time + timedelta(minutes=4)
        second_delivery.last_error_code = "test_failure"
        session.commit()

    forbidden_existing = non_admin_get(client, f"/admin/threads/{first.thread_id}")
    forbidden_missing = non_admin_get(client, "/admin/threads/not-a-thread")
    missing = admin_get(client, "/admin/threads/not-a-thread")
    response = admin_get(
        client,
        f"/admin/threads/{first.thread_id}",
        params={"limit": 1, "offset": 0},
    )

    assert forbidden_existing.status_code == 403
    assert forbidden_missing.status_code == 403
    assert missing.status_code == 404
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["id"] == first.thread_id
    assert body["timeline"]["total"] == 2
    assert body["timeline"]["limit"] == 1
    assert body["timeline"]["offset"] == 0
    assert len(body["timeline"]["items"]) == 1

    complete_timeline = admin_get(
        client,
        f"/admin/threads/{first.thread_id}",
        params={"limit": 10, "offset": 0},
    ).json()["timeline"]
    timeline_ids = {item["id"] for item in complete_timeline["items"]}
    assert timeline_ids == {first.message_id, second.message_id}
    assert other.message_id not in timeline_ids
    assert body["delivery_summary"] == {
        "total": 2,
        "queued": 0,
        "delivering": 0,
        "delivered": 1,
        "failed": 1,
        "uncertain": 0,
    }
    assert {
        (binding["platform"], binding["account_id"], binding["physical_conversation_id"])
        for binding in body["source_bindings"]
    } == {("wechat", "gateway-a", "thread-conversation")}


def database_snapshot(client: TestClient) -> tuple[int, int, int, tuple[tuple[int, str], ...]]:
    with database_session_factory(client)() as session:
        conversation_count = session.scalar(select(func.count()).select_from(Conversation))
        message_count = session.scalar(select(func.count()).select_from(Message))
        dispatch_count = session.scalar(select(func.count()).select_from(HermesDispatchRecord))
        deliveries = tuple(
            session.execute(
                select(DeliveryOutboxRecord.id, DeliveryOutboxRecord.status).order_by(
                    DeliveryOutboxRecord.id
                )
            ).all()
        )
        return conversation_count or 0, message_count or 0, dispatch_count or 0, deliveries


def test_admin_archive_api_exposes_no_write_methods(client: TestClient) -> None:
    entry = create_archive_entry(
        client,
        "read-only",
        occurred_at=datetime(2026, 8, 10, 14, 0, tzinfo=UTC),
    )
    before = database_snapshot(client)
    paths = [
        "/admin/conversations",
        "/admin/messages",
        f"/admin/threads/{entry.thread_id}",
        "/admin/deliveries",
    ]

    responses = [
        admin_write_attempt(client, method, path)
        for path in paths
        for method in ("POST", "PUT", "PATCH", "DELETE")
    ]

    assert all(response.status_code == 405 for response in responses)
    assert database_snapshot(client) == before


def test_historical_dispatch_identity_precedes_current_sender_mapping(
    client: TestClient,
) -> None:
    occurred_at = datetime(2026, 8, 10, 15, 0, tzinfo=UTC)
    historical = create_archive_entry(
        client,
        "historical-identity-a",
        occurred_at=occurred_at,
        conversation_id="historical-conversation",
    )

    with database_session_factory(client)() as session:
        identity_b = IdentityService(session).create_identity(
            employee_id="employee-historical-b",
            display_name="Employee Historical B",
        )
        identity_b_id = identity_b.id
        current_mapping = session.scalar(
            select(SourceIdentityMapping).where(
                SourceIdentityMapping.platform == "wechat",
                SourceIdentityMapping.account_id == "gateway-a",
                SourceIdentityMapping.sender_id == "sender-historical-identity-a",
            )
        )
        dispatch = session.get(HermesDispatchRecord, historical.dispatch_id)
        assert current_mapping is not None
        assert dispatch is not None
        assert dispatch.enterprise_identity_id == historical.identity_id
        current_mapping.enterprise_identity_id = identity_b_id
        session.commit()

    denied = client.post(
        "/internal/messages",
        json={
            "event_id": "event-unexecuted-identity-b",
            "source": "wechat",
            "source_account_id": "gateway-a",
            "source_message_id": "source-message-unexecuted-identity-b",
            "conversation_id": "unexecuted-conversation",
            "conversation_type": "private",
            "is_mentioned": None,
            "is_self": False,
            "conversation_name": "Unexecuted conversation",
            "sender_type": "human",
            "sender_id": "sender-unexecuted-identity-b",
            "sender_name": "Unexecuted Sender",
            "message_type": "text",
            "content": "Archived before admission was denied",
            "timestamp": (occurred_at + timedelta(minutes=1)).isoformat(),
        },
    )
    assert denied.status_code == 201, denied.text
    denied_message_id = denied.json()["id"]

    with database_session_factory(client)() as session:
        IdentityService(session).create_mapping(
            platform="wechat",
            account_id="gateway-a",
            sender_id="sender-unexecuted-identity-b",
            enterprise_identity_id=identity_b_id,
        )
        denied_conversation = session.scalar(
            select(Conversation).where(
                Conversation.source == "wechat",
                Conversation.source_account_id == "gateway-a",
                Conversation.conversation_id == "unexecuted-conversation",
            )
        )
        current_mapping = session.scalar(
            select(SourceIdentityMapping).where(
                SourceIdentityMapping.platform == "wechat",
                SourceIdentityMapping.account_id == "gateway-a",
                SourceIdentityMapping.sender_id == "sender-historical-identity-a",
            )
        )
        assert denied_conversation is not None
        assert current_mapping is not None
        assert current_mapping.enterprise_identity_id == identity_b_id
        assert (
            session.scalar(
                select(HermesDispatchRecord).where(
                    HermesDispatchRecord.message_id == denied_message_id
                )
            )
            is None
        )
        denied_conversation_id = denied_conversation.id

    messages_a_response = admin_get(
        client, "/admin/messages", params={"identity_id": historical.identity_id}
    )
    conversations_a_response = admin_get(
        client, "/admin/conversations", params={"identity_id": historical.identity_id}
    )
    messages_b_response = admin_get(
        client, "/admin/messages", params={"identity_id": identity_b_id}
    )
    conversations_b_response = admin_get(
        client, "/admin/conversations", params={"identity_id": identity_b_id}
    )

    for response in (
        messages_a_response,
        conversations_a_response,
        messages_b_response,
        conversations_b_response,
    ):
        assert response.status_code == 200, response.text

    messages_a = messages_a_response.json()
    conversations_a = conversations_a_response.json()
    messages_b = messages_b_response.json()
    conversations_b = conversations_b_response.json()
    assert messages_a["total"] == 1
    assert [(item["id"], item["identity_id"]) for item in messages_a["items"]] == [
        (historical.message_id, historical.identity_id)
    ]
    assert [item["id"] for item in conversations_a["items"]] == [historical.conversation_record_id]
    assert messages_b["total"] == 1
    assert [(item["id"], item["identity_id"]) for item in messages_b["items"]] == [
        (denied_message_id, identity_b_id)
    ]
    assert historical.message_id not in {item["id"] for item in messages_b["items"]}
    assert [item["id"] for item in conversations_b["items"]] == [denied_conversation_id]
    assert historical.conversation_record_id not in {
        item["id"] for item in conversations_b["items"]
    }
