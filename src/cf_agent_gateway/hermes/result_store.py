from __future__ import annotations

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from cf_agent_gateway.hermes.models import HermesDispatchOutcome, ResponseEnvelope
from cf_agent_gateway.hermes.result_models import HermesDispatchResponse
from cf_agent_gateway.task.model.errors import HermesDispatchStateConflictError
from cf_agent_gateway.task.model.models import HermesDispatchRecord, HermesDispatchStatus


class HermesDispatchResponseStore:
    """Persist a Hermes result and fence dispatch completion with its claim token."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get(self, response_id: int) -> HermesDispatchResponse | None:
        return self._session.get(HermesDispatchResponse, response_id)

    def get_by_dispatch_record_id(
        self,
        dispatch_record_id: int,
    ) -> HermesDispatchResponse | None:
        return self._session.scalar(
            select(HermesDispatchResponse).where(
                HermesDispatchResponse.dispatch_record_id == dispatch_record_id
            )
        )

    def complete_success(
        self,
        dispatch_record_id: int,
        *,
        claim_token: str,
        outcome: HermesDispatchOutcome,
    ) -> HermesDispatchResponse:
        envelope = outcome.response
        response = HermesDispatchResponse(
            dispatch_record_id=dispatch_record_id,
            hermes_response_id=envelope.response_id if envelope is not None else None,
            assistant_content=outcome.assistant_content,
            response_payload=(envelope.model_dump(mode="json") if envelope is not None else None),
        )
        statement = (
            update(HermesDispatchRecord)
            .where(
                HermesDispatchRecord.id == dispatch_record_id,
                HermesDispatchRecord.status == HermesDispatchStatus.RUNNING,
                HermesDispatchRecord.claim_token == claim_token,
                HermesDispatchRecord.lease_expires_at > func.now(),
                HermesDispatchRecord.message_id == outcome.message_id,
                HermesDispatchRecord.workspace_id == outcome.workspace_id,
                HermesDispatchRecord.ai_thread_id == outcome.ai_thread_id,
            )
            .values(
                status=HermesDispatchStatus.SUCCESS,
                claim_token=None,
                lease_expires_at=None,
                completed_at=func.now(),
                last_error_code=None,
                updated_at=func.now(),
            )
            .execution_options(synchronize_session=False)
        )
        try:
            result = self._session.execute(statement)
            if result.rowcount != 1:
                self._session.rollback()
                raise HermesDispatchStateConflictError(
                    record_id=dispatch_record_id,
                    expected_status=HermesDispatchStatus.RUNNING,
                )
            self._session.add(response)
            self._session.commit()
        except HermesDispatchStateConflictError:
            raise
        except IntegrityError:
            self._session.rollback()
            raise HermesDispatchStateConflictError(
                record_id=dispatch_record_id,
                expected_status=HermesDispatchStatus.RUNNING,
            ) from None
        except Exception:
            self._session.rollback()
            raise
        return response

    @staticmethod
    def to_outcome(
        response: HermesDispatchResponse,
        record: HermesDispatchRecord,
    ) -> HermesDispatchOutcome:
        envelope = (
            ResponseEnvelope.model_validate(response.response_payload)
            if response.response_payload is not None
            else None
        )
        return HermesDispatchOutcome(
            message_id=record.message_id,
            workspace_id=record.workspace_id,
            ai_thread_id=record.ai_thread_id,
            assistant_content=response.assistant_content,
            response=envelope,
        )
