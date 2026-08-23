from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from threading import TIMEOUT_MAX
from typing import TYPE_CHECKING
from uuid import uuid4

from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from cf_agent_gateway.access import AuthorizationDecision, RequestFacts
from cf_agent_gateway.admission.enums import (
    AdmissionDecision,
    AdmissionEvidenceOrigin,
    AdmissionOutcomeState,
    AdmissionReason,
)
from cf_agent_gateway.admission.errors import (
    AdmissionInvariantError,
    AdmissionPendingError,
    AdmissionStateConflictError,
)
from cf_agent_gateway.admission.models import AdmissionOutcome, MessageAdmissionOutcome

if TYPE_CHECKING:
    from cf_agent_gateway.task.model.models import HermesDispatchRecord

DEFAULT_ADMISSION_LEASE_SECONDS = 60.0
_EVALUATION_ERROR_CODE = "admission_evaluation_failed"


@dataclass(frozen=True, slots=True)
class AdmissionClaim:
    outcome: MessageAdmissionOutcome
    claim_token: str | None
    created: bool

    @property
    def should_evaluate(self) -> bool:
        return self.claim_token is not None


class MessageAdmissionOutcomeStore:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get_by_message_id(self, message_id: int) -> MessageAdmissionOutcome | None:
        statement = (
            select(MessageAdmissionOutcome)
            .where(MessageAdmissionOutcome.message_id == message_id)
            .execution_options(populate_existing=True)
        )
        return self._session.scalar(statement)

    def create_pending(
        self,
        *,
        message_id: int,
        request: RequestFacts,
        lease_seconds: float = DEFAULT_ADMISSION_LEASE_SECONDS,
        now: datetime | None = None,
    ) -> AdmissionClaim:
        claimed_at = _validated_now(now)
        lease = _validated_lease_seconds(lease_seconds)
        claim_token = str(uuid4())
        pending = MessageAdmissionOutcome(
            message_id=message_id,
            state=AdmissionOutcomeState.PENDING,
            decision=None,
            evidence_origin=AdmissionEvidenceOrigin.RUNTIME,
            admission_reason=None,
            should_create_task=False,
            requested_scope=request.requested_scope,
            requested_skill_ids=request.requested_skill_ids,
            risk_level=request.risk_level.value,
            routing_mode="none",
            attempt_count=1,
            claim_token=claim_token,
            lease_expires_at=claimed_at + timedelta(seconds=lease),
        )
        self._session.add(pending)
        try:
            self._session.commit()
        except IntegrityError:
            self._session.rollback()
            existing = self.get_by_message_id(message_id)
            if existing is None:
                raise
            return self._claim_existing(
                existing,
                lease_seconds=lease,
                now=claimed_at,
            )
        except Exception:
            self._session.rollback()
            raise
        return AdmissionClaim(pending, claim_token, True)

    def claim_pending(
        self,
        outcome: MessageAdmissionOutcome,
        *,
        lease_seconds: float = DEFAULT_ADMISSION_LEASE_SECONDS,
        now: datetime | None = None,
    ) -> AdmissionClaim:
        if outcome.state is AdmissionOutcomeState.COMPLETED:
            return AdmissionClaim(outcome, None, False)
        return self._claim_existing(
            outcome,
            lease_seconds=_validated_lease_seconds(lease_seconds),
            now=_validated_now(now),
        )

    def request_facts(self, outcome: MessageAdmissionOutcome) -> RequestFacts:
        if (
            outcome.evidence_origin is not AdmissionEvidenceOrigin.RUNTIME
            or outcome.requested_scope is None
            or outcome.requested_skill_ids is None
            or outcome.risk_level is None
        ):
            raise AdmissionInvariantError("pending admission has no authoritative request snapshot")
        return RequestFacts(
            requested_scope=outcome.requested_scope,
            requested_skill_ids=outcome.requested_skill_ids,
            risk_level=outcome.risk_level,
        )

    def create_legacy_dispatch_outcome(
        self,
        dispatch_record: HermesDispatchRecord,
    ) -> tuple[MessageAdmissionOutcome, bool]:
        pending = MessageAdmissionOutcome(
            message_id=dispatch_record.message_id,
            state=AdmissionOutcomeState.COMPLETED,
            decision=AdmissionDecision.ALLOWED,
            evidence_origin=AdmissionEvidenceOrigin.LEGACY_DISPATCH,
            admission_reason=AdmissionReason.ALLOWED.value,
            should_create_task=True,
            enterprise_identity_id=dispatch_record.enterprise_identity_id,
            workspace_id=dispatch_record.workspace_id,
            ai_thread_id=dispatch_record.ai_thread_id,
            routing_mode="legacy",
            attempt_count=0,
            completed_at=func.now(),
        )
        self._session.add(pending)
        try:
            self._session.commit()
        except IntegrityError:
            self._session.rollback()
            existing = self.get_by_message_id(dispatch_record.message_id)
            if existing is None:
                raise
            return _compatible_legacy_dispatch_outcome(existing, dispatch_record), False
        except Exception:
            self._session.rollback()
            raise
        return _compatible_legacy_dispatch_outcome(pending, dispatch_record), True

    def complete_claim_from_legacy_dispatch(
        self,
        *,
        claim_token: str,
        dispatch_record: HermesDispatchRecord,
    ) -> MessageAdmissionOutcome:
        statement = (
            update(MessageAdmissionOutcome)
            .where(
                MessageAdmissionOutcome.message_id == dispatch_record.message_id,
                MessageAdmissionOutcome.state == AdmissionOutcomeState.PENDING,
                MessageAdmissionOutcome.claim_token == claim_token,
            )
            .values(
                state=AdmissionOutcomeState.COMPLETED,
                decision=AdmissionDecision.ALLOWED,
                evidence_origin=AdmissionEvidenceOrigin.LEGACY_DISPATCH,
                admission_reason=AdmissionReason.ALLOWED.value,
                should_create_task=True,
                authorization_reason_code=None,
                authorization_snapshot=None,
                policy_snapshot=None,
                requested_scope=None,
                requested_skill_ids=None,
                risk_level=None,
                gateway_policy_id=None,
                gateway_policy_updated_at=None,
                user_policy_id=None,
                user_policy_updated_at=None,
                enterprise_identity_id=dispatch_record.enterprise_identity_id,
                workspace_id=dispatch_record.workspace_id,
                ai_thread_id=dispatch_record.ai_thread_id,
                routing_mode="legacy",
                route_conversation_record_id=None,
                agent_profile_id=None,
                agent_profile_key=None,
                agent_profile_reference=None,
                agent_profile_revision=None,
                group_type_id=None,
                group_type_key=None,
                thread_policy=None,
                claim_token=None,
                lease_expires_at=None,
                last_error_code=None,
                completed_at=func.now(),
                updated_at=func.now(),
            )
            .execution_options(synchronize_session=False)
        )
        try:
            result = self._session.execute(statement)
            if result.rowcount != 1:
                self._session.rollback()
                existing = self.get_by_message_id(dispatch_record.message_id)
                if existing is None:
                    raise AdmissionStateConflictError(dispatch_record.message_id)
                return _compatible_legacy_dispatch_outcome(existing, dispatch_record)
            self._session.commit()
        except (AdmissionInvariantError, AdmissionStateConflictError):
            raise
        except Exception:
            self._session.rollback()
            raise
        completed = self.get_by_message_id(dispatch_record.message_id)
        if completed is None:
            raise AdmissionStateConflictError(dispatch_record.message_id)
        return _compatible_legacy_dispatch_outcome(completed, dispatch_record)

    def release_claim(
        self,
        *,
        message_id: int,
        claim_token: str,
        error_code: str = _EVALUATION_ERROR_CODE,
    ) -> None:
        statement = (
            update(MessageAdmissionOutcome)
            .where(
                MessageAdmissionOutcome.message_id == message_id,
                MessageAdmissionOutcome.state == AdmissionOutcomeState.PENDING,
                MessageAdmissionOutcome.claim_token == claim_token,
            )
            .values(
                claim_token=None,
                lease_expires_at=None,
                last_error_code=_validated_error_code(error_code),
                updated_at=func.now(),
            )
            .execution_options(synchronize_session=False)
        )
        try:
            self._session.execute(statement)
            self._session.commit()
        except Exception:
            self._session.rollback()
            raise

    def stage_completion(
        self,
        *,
        claim_token: str,
        admission: AdmissionOutcome,
    ) -> None:
        decision = _completed_decision(admission)
        authorization = admission.authorization
        enterprise_identity_id = admission.enterprise_identity_id
        if enterprise_identity_id is None and authorization is not None:
            enterprise_identity_id = authorization.enterprise_identity_id
        statement = (
            update(MessageAdmissionOutcome)
            .where(
                MessageAdmissionOutcome.message_id == admission.message_id,
                MessageAdmissionOutcome.state == AdmissionOutcomeState.PENDING,
                MessageAdmissionOutcome.claim_token == claim_token,
            )
            .values(
                state=AdmissionOutcomeState.COMPLETED,
                decision=decision,
                admission_reason=admission.reason.value,
                should_create_task=admission.should_create_task,
                authorization_reason_code=(
                    authorization.reason_code.value if authorization is not None else None
                ),
                authorization_snapshot=(
                    authorization.to_dict() if authorization is not None else None
                ),
                policy_snapshot=admission.policy_snapshot,
                gateway_policy_id=admission.gateway_policy_id,
                gateway_policy_updated_at=admission.gateway_policy_updated_at,
                user_policy_id=admission.user_policy_id,
                user_policy_updated_at=admission.user_policy_updated_at,
                enterprise_identity_id=enterprise_identity_id,
                workspace_id=admission.workspace_id,
                ai_thread_id=admission.ai_thread_id,
                routing_mode=admission.routing_mode,
                route_conversation_record_id=admission.route_conversation_record_id,
                agent_profile_id=admission.agent_profile_id,
                agent_profile_key=admission.agent_profile_key,
                agent_profile_reference=admission.agent_profile_reference,
                agent_profile_revision=admission.agent_profile_revision,
                group_type_id=admission.group_type_id,
                group_type_key=admission.group_type_key,
                thread_policy=admission.thread_policy,
                claim_token=None,
                lease_expires_at=None,
                last_error_code=None,
                completed_at=func.now(),
                updated_at=func.now(),
            )
            .execution_options(synchronize_session=False)
        )
        result = self._session.execute(statement)
        if result.rowcount != 1:
            raise AdmissionStateConflictError(admission.message_id)

    def complete_denied(
        self,
        *,
        claim_token: str,
        admission: AdmissionOutcome,
    ) -> MessageAdmissionOutcome:
        if admission.admitted or admission.should_create_task:
            raise AdmissionInvariantError("denied admission requested dispatch")
        try:
            self.stage_completion(claim_token=claim_token, admission=admission)
            self._session.commit()
        except Exception:
            self._session.rollback()
            raise
        completed = self.get_by_message_id(admission.message_id)
        if completed is None:
            raise AdmissionStateConflictError(admission.message_id)
        return completed

    def to_admission_outcome(self, stored: MessageAdmissionOutcome) -> AdmissionOutcome:
        if stored.state is not AdmissionOutcomeState.COMPLETED or stored.decision is None:
            raise AdmissionPendingError(stored.message_id)
        try:
            reason = AdmissionReason(stored.admission_reason)
            authorization = _authorization_from_snapshot(stored.authorization_snapshot)
        except (TypeError, ValueError, KeyError) as exc:
            raise AdmissionInvariantError("stored admission outcome is invalid") from exc
        admitted = stored.decision is AdmissionDecision.ALLOWED
        if admitted != stored.should_create_task:
            raise AdmissionInvariantError("stored admission decision and task target disagree")
        if admitted != (reason is AdmissionReason.ALLOWED):
            raise AdmissionInvariantError("stored admission decision and reason disagree")
        if (
            stored.decision is AdmissionDecision.UNRESOLVED
            and reason is not AdmissionReason.LEGACY_UNRESOLVED
        ):
            raise AdmissionInvariantError("stored unresolved admission reason is invalid")
        if authorization is not None and authorization.allowed != admitted:
            raise AdmissionInvariantError("stored authorization and admission decision disagree")
        return AdmissionOutcome(
            message_id=stored.message_id,
            admitted=admitted,
            should_create_task=stored.should_create_task,
            reason=reason,
            enterprise_identity_id=stored.enterprise_identity_id,
            workspace_id=stored.workspace_id,
            ai_thread_id=stored.ai_thread_id,
            authorization=authorization,
            policy_snapshot=stored.policy_snapshot,
            gateway_policy_id=stored.gateway_policy_id,
            gateway_policy_updated_at=stored.gateway_policy_updated_at,
            user_policy_id=stored.user_policy_id,
            user_policy_updated_at=stored.user_policy_updated_at,
            routing_mode=stored.routing_mode,
            route_conversation_record_id=stored.route_conversation_record_id,
            agent_profile_id=stored.agent_profile_id,
            agent_profile_key=stored.agent_profile_key,
            agent_profile_reference=stored.agent_profile_reference,
            agent_profile_revision=stored.agent_profile_revision,
            group_type_id=stored.group_type_id,
            group_type_key=stored.group_type_key,
            thread_policy=stored.thread_policy,
        )

    def _claim_existing(
        self,
        outcome: MessageAdmissionOutcome,
        *,
        lease_seconds: float,
        now: datetime,
    ) -> AdmissionClaim:
        if outcome.state is AdmissionOutcomeState.COMPLETED:
            return AdmissionClaim(outcome, None, False)
        claim_token = str(uuid4())
        statement = (
            update(MessageAdmissionOutcome)
            .where(
                MessageAdmissionOutcome.message_id == outcome.message_id,
                MessageAdmissionOutcome.state == AdmissionOutcomeState.PENDING,
                or_(
                    MessageAdmissionOutcome.claim_token.is_(None),
                    MessageAdmissionOutcome.lease_expires_at <= now,
                ),
            )
            .values(
                claim_token=claim_token,
                lease_expires_at=now + timedelta(seconds=lease_seconds),
                attempt_count=MessageAdmissionOutcome.attempt_count + 1,
                last_error_code=None,
                updated_at=func.now(),
            )
            .execution_options(synchronize_session=False)
        )
        try:
            result = self._session.execute(statement)
            if result.rowcount != 1:
                self._session.rollback()
                current = self.get_by_message_id(outcome.message_id)
                if current is not None and current.state is AdmissionOutcomeState.COMPLETED:
                    return AdmissionClaim(current, None, False)
                raise AdmissionPendingError(outcome.message_id)
            self._session.commit()
        except AdmissionPendingError:
            raise
        except Exception:
            self._session.rollback()
            raise
        claimed = self.get_by_message_id(outcome.message_id)
        if claimed is None:
            raise AdmissionStateConflictError(outcome.message_id)
        return AdmissionClaim(claimed, claim_token, False)


def _completed_decision(admission: AdmissionOutcome) -> AdmissionDecision:
    if admission.admitted:
        if admission.reason is not AdmissionReason.ALLOWED or not admission.should_create_task:
            raise AdmissionInvariantError("allowed admission has inconsistent state")
        if not all(
            (
                admission.enterprise_identity_id,
                admission.workspace_id,
                admission.ai_thread_id,
            )
        ):
            raise AdmissionInvariantError("allowed admission has no durable dispatch target")
        return AdmissionDecision.ALLOWED
    if admission.should_create_task or admission.reason is AdmissionReason.ALLOWED:
        raise AdmissionInvariantError("denied admission has inconsistent state")
    return AdmissionDecision.DENIED


def _authorization_from_snapshot(
    snapshot: dict[str, object] | None,
) -> AuthorizationDecision | None:
    if snapshot is None:
        return None
    return AuthorizationDecision(
        allowed=bool(snapshot["allowed"]),
        decision=str(snapshot["decision"]),
        reason_code=str(snapshot["reason_code"]),
        enterprise_identity_id=(
            str(snapshot["enterprise_identity_id"])
            if snapshot.get("enterprise_identity_id") is not None
            else None
        ),
        user_allowed=bool(snapshot["user_allowed"]),
        is_mentioned=(
            bool(snapshot["is_mentioned"]) if snapshot.get("is_mentioned") is not None else None
        ),
        permission_scope=frozenset(str(value) for value in snapshot["permission_scope"]),
        allowed_skills=frozenset(str(value) for value in snapshot["allowed_skills"]),
        risk_level=str(snapshot["risk_level"]),
    )


def _compatible_legacy_dispatch_outcome(
    outcome: MessageAdmissionOutcome,
    dispatch_record: HermesDispatchRecord,
) -> MessageAdmissionOutcome:
    compatible = (
        outcome.message_id == dispatch_record.message_id
        and outcome.state is AdmissionOutcomeState.COMPLETED
        and outcome.decision is AdmissionDecision.ALLOWED
        and outcome.admission_reason == AdmissionReason.ALLOWED.value
        and outcome.should_create_task
        and outcome.enterprise_identity_id == dispatch_record.enterprise_identity_id
        and outcome.workspace_id == dispatch_record.workspace_id
        and outcome.ai_thread_id == dispatch_record.ai_thread_id
    )
    if not compatible:
        raise AdmissionInvariantError(
            "persisted admission outcome conflicts with an existing dispatch record"
        )
    return outcome


def _validated_lease_seconds(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("lease_seconds must be a finite positive number")
    seconds = float(value)
    if not math.isfinite(seconds) or seconds <= 0 or seconds > TIMEOUT_MAX:
        raise ValueError("lease_seconds must be a finite positive number")
    return seconds


def _validated_now(value: datetime | None) -> datetime:
    now = value if value is not None else datetime.now(UTC)
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be a timezone-aware datetime")
    return now.astimezone(UTC)


def _validated_error_code(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 128:
        raise ValueError("error_code must be a non-empty string up to 128 characters")
    return value
