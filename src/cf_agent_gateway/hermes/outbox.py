from __future__ import annotations

from contextlib import suppress
from uuid import uuid4

from sqlalchemy.orm import Session

from cf_agent_gateway.admission import AdmissionOutcome
from cf_agent_gateway.hermes.errors import (
    HermesAPIError,
    HermesDispatchError,
    HermesError,
    HermesResponseError,
    HermesTransportError,
)
from cf_agent_gateway.hermes.models import HermesDispatchOutcome
from cf_agent_gateway.hermes.service import HermesDispatcher
from cf_agent_gateway.task.model import HermesDispatchRecordStore, HermesDispatchStatus

_POST_CALL_DISPATCH_ERRORS = frozenset({"hermes_thread_advanced_concurrently"})


class HermesDispatchOutboxExecutor:
    """Run the synchronous compatibility path through a durable dispatch record."""

    manages_dispatch_records = True

    def __init__(
        self,
        session: Session,
        dispatcher: HermesDispatcher,
        *,
        record_store: HermesDispatchRecordStore | None = None,
    ) -> None:
        self._session = session
        self._dispatcher = dispatcher
        self._record_store = (
            record_store if record_store is not None else HermesDispatchRecordStore(session)
        )

    def dispatch(self, admission: AdmissionOutcome) -> HermesDispatchOutcome:
        record, _ = self._record_store.enqueue(admission)
        claim_token = str(uuid4())
        self._record_store.claim(record.id, claim_token=claim_token)

        try:
            outcome = self._dispatcher.dispatch(admission)
        except Exception as error:
            # A failed inner database operation may leave the shared session unusable.
            with suppress(Exception):
                self._session.rollback()
            error_code = _dispatch_error_code(error)
            status = _failure_status(error)
            if status is HermesDispatchStatus.FAILED:
                self._record_store.mark_failed(
                    record.id,
                    claim_token=claim_token,
                    error_code=error_code,
                )
            else:
                self._record_store.mark_uncertain(
                    record.id,
                    claim_token=claim_token,
                    error_code=error_code,
                )
            raise

        self._record_store.mark_success(record.id, claim_token=claim_token)
        return outcome


def _failure_status(error: Exception) -> HermesDispatchStatus:
    if isinstance(error, HermesDispatchError):
        if error.reason in _POST_CALL_DISPATCH_ERRORS:
            return HermesDispatchStatus.UNCERTAIN
        return HermesDispatchStatus.FAILED
    if isinstance(error, HermesAPIError):
        if error.status_code >= 500:
            return HermesDispatchStatus.UNCERTAIN
        return HermesDispatchStatus.FAILED
    if isinstance(error, (HermesTransportError, HermesResponseError)):
        return HermesDispatchStatus.UNCERTAIN
    return HermesDispatchStatus.UNCERTAIN


def _dispatch_error_code(error: Exception) -> str:
    if not isinstance(error, HermesError):
        return "unexpected_dispatch_error"
    code = error.code
    if isinstance(error, HermesDispatchError):
        return f"{code}:{error.reason}"
    if isinstance(error, HermesAPIError):
        return f"{code}:http_{error.status_code}"
    return code
