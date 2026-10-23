"""Read existing, message-scoped evidence; never infer current employee rights."""

from sqlalchemy import select

from cf_agent_gateway.access.enums import ReasonCode
from cf_agent_gateway.admission.enums import AdmissionReason
from cf_agent_gateway.admission.models import MessageAdmissionOutcome
from cf_agent_gateway.delivery.models import DeliveryOutboxRecord
from cf_agent_gateway.management_console.live_models import ArchiveObservation
from cf_agent_gateway.management_console.service import iso, now
from cf_agent_gateway.response.models import ResponseRecord
from cf_agent_gateway.task.model.models import HermesDispatchRecord


def message_trace(service, archive, entry, message):
    """Only committed records for the already source/account-checked message."""
    journal = None
    if archive is service.s:
        journal = archive.scalar(
            select(ArchiveObservation).where(
                ArchiveObservation.source_id == entry.source_id,
                ArchiveObservation.account_id == entry.account_id,
                ArchiveObservation.message_id == message.id,
            )
        )
    admission = archive.scalar(
        select(MessageAdmissionOutcome).where(MessageAdmissionOutcome.message_id == message.id)
    )
    dispatch = archive.scalar(
        select(HermesDispatchRecord).where(HermesDispatchRecord.message_id == message.id)
    )
    response = archive.scalar(select(ResponseRecord).where(ResponseRecord.message_id == message.id))
    delivery = (
        archive.scalar(
            select(DeliveryOutboxRecord).where(
                DeliveryOutboxRecord.response_id == response.response_id,
                DeliveryOutboxRecord.account_id == entry.account_id,
                DeliveryOutboxRecord.conversation_id == entry.username,
            )
        )
        if response
        else None
    )
    archive_only = bool(journal and not any((admission, dispatch, response, delivery)))
    inactive = "disabled" if archive_only else "unverified"
    stages = [
        {"stage": "occurred", "state": "recorded", "at": iso(message.occurred_at)},
        {
            "stage": "upstream",
            "state": "recorded" if journal else "unverified",
            "at": iso(journal.upstream_readable_at) if journal else None,
        },
        {
            "stage": "archive",
            "state": "recorded",
            "at": iso(journal.archived_at if journal else message.created_at),
        },
        {
            "stage": "increment",
            "state": "recorded" if journal else "unverified",
            "sequence": journal.id if journal else None,
            "at": iso(journal.archived_at) if journal else None,
        },
        {
            "stage": "admission",
            "state": str(admission.decision or admission.state) if admission else inactive,
            "reason": admission.admission_reason
            if admission and admission.admission_reason in set(AdmissionReason)
            else None,
            "at": iso(admission.completed_at or admission.updated_at) if admission else None,
        },
        {
            "stage": "authorization",
            "state": "recorded"
            if admission and admission.authorization_reason_code
            else "unverified",
            "reason": admission.authorization_reason_code
            if admission and admission.authorization_reason_code in set(ReasonCode)
            else None,
            "at": iso(admission.completed_at) if admission else None,
        },
        {
            "stage": "routing",
            "state": "recorded" if dispatch else "unverified",
            "profile_recorded": bool(admission and admission.agent_profile_reference),
            "at": iso(dispatch.created_at) if dispatch else None,
        },
        {
            "stage": "hermes",
            "state": str(dispatch.status) if dispatch else inactive,
            "at": iso(dispatch.completed_at or dispatch.claimed_at or dispatch.created_at)
            if dispatch
            else None,
        },
        {"stage": "file_tools", "state": "unverified", "at": None},
        {
            "stage": "result",
            "state": str(response.status) if response else inactive,
            "at": iso(response.updated_at) if response else None,
        },
        {
            "stage": "delivery",
            "state": str(delivery.status) if delivery else inactive,
            "at": iso(delivery.completed_at or delivery.updated_at) if delivery else None,
        },
    ]
    return {
        "observed_at": iso(now()),
        "evidence": "candidate_archive_only" if archive_only else "read_only_message_archive",
        "stages": stages,
        "references": {
            "event": message.event_id,
            "message": message.id,
            "dispatch": dispatch.id if dispatch else None,
            "response": response.response_id if response else None,
            "delivery": delivery.id if delivery else None,
        },
    }
