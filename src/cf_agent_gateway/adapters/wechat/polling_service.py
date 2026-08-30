from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from cf_agent_gateway.adapters.wechat.errors import WechatAdapterError
from cf_agent_gateway.adapters.wechat.normalized_models import NormalizedWechatMessage
from cf_agent_gateway.adapters.wechat.normalizer import (
    build_wechat_checkpoint_fingerprint,
    normalize_wechat_message,
)
from cf_agent_gateway.adapters.wechat.polling_errors import (
    InvalidBootstrapModeError,
    WechatChatIdentityError,
    WechatCheckpointContinuityError,
    WechatCheckpointStateConflictError,
    WechatConversationMismatchError,
    WechatLocalIdError,
    WechatPollingError,
)
from cf_agent_gateway.adapters.wechat.polling_models import (
    MAX_CHECKPOINT_LOCAL_ID,
    BootstrapMode,
    ChatPollResult,
    MessageSinkDisposition,
    PollFailure,
    PollFailureStage,
    PollResult,
)
from cf_agent_gateway.adapters.wechat.polling_store import WechatSyncCheckpointStore
from cf_agent_gateway.adapters.wechat.raw_models import AgentWechatAuthStatus, RawWechatMessage

logger = logging.getLogger(__name__)
_MAX_CONTINUITY_WARNING_KEYS = 1024


@dataclass(frozen=True, slots=True)
class _EmptyWindowMarker:
    source_account_id: str
    conversation_id: str
    checkpoint_last_local_id: int
    checkpoint_generation: int
    checkpoint_fingerprint: str | None
    observed_at: datetime


class WechatPollingClient(Protocol):
    def get_auth_status(self) -> AgentWechatAuthStatus: ...

    def list_chats(self) -> list[dict[str, Any]]: ...

    def list_messages(self, chat_id: str) -> list[RawWechatMessage | Mapping[str, Any]]: ...


class NormalizedMessageSink(Protocol):
    """Receive messages under an at-least-once delivery contract.

    Implementations must be idempotent: a successful handle followed by a failed
    checkpoint write can redeliver the same message. Future durable sinks can use
    ``event_id`` or the source physical-message identity as their uniqueness key.
    """

    def handle(self, message: NormalizedWechatMessage) -> None: ...


class WechatPollingService:
    """Run one finite polling cycle with durable, at-least-once delivery.

    Compare-and-swap checkpoint transitions fence concurrent pollers. The sink
    remains responsible for idempotency because persistence precedes advancement.
    """

    def __init__(
        self,
        client: WechatPollingClient,
        checkpoint_store: WechatSyncCheckpointStore,
        sink: NormalizedMessageSink,
        *,
        bootstrap_mode: BootstrapMode | str = BootstrapMode.LATEST,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._client = client
        self._checkpoint_store = checkpoint_store
        self._sink = sink
        self._clock = clock or (lambda: datetime.now(UTC))
        self._active_source_account_id: str | None = None
        self._empty_window_markers: dict[tuple[str, str], _EmptyWindowMarker] = {}
        self._continuity_warning_keys: set[str] = set()
        try:
            self._bootstrap_mode = BootstrapMode(bootstrap_mode)
        except (TypeError, ValueError):
            raise InvalidBootstrapModeError() from None

    def poll_once(self) -> PollResult:
        try:
            auth_status = self._client.get_auth_status()
        except Exception as error:
            self._invalidate_all_empty_window_markers()
            failure = _failure(PollFailureStage.AUTH, error)
            return PollResult(logged_in=False, failures=[failure])

        if auth_status.status != "logged_in":
            self._invalidate_all_empty_window_markers()
            return PollResult(logged_in=False)

        source_account_id = _nonempty_string(auth_status.logged_in_user)
        if source_account_id is None:
            self._invalidate_all_empty_window_markers()
            failure = PollFailure(
                stage=PollFailureStage.AUTH,
                code="wechat_auth_status_error",
            )
            return PollResult(logged_in=False, failures=[failure])
        if self._active_source_account_id != source_account_id:
            self._invalidate_all_empty_window_markers()
            self._active_source_account_id = source_account_id

        try:
            chats = self._client.list_chats()
        except Exception as error:
            self._invalidate_all_empty_window_markers()
            failure = _failure(PollFailureStage.LIST_CHATS, error)
            return PollResult(
                source_account_id=source_account_id,
                logged_in=True,
                failures=[failure],
            )

        failed_conversation_ids: set[str] = set()
        chat_results: list[ChatPollResult] = []
        for chat in chats:
            result = self._poll_chat(
                source_account_id,
                chat,
                failed_conversation_ids=failed_conversation_ids,
            )
            _log_chat_result(source_account_id, result)
            chat_results.append(result)
            if not result.succeeded and result.conversation_id is not None:
                failed_conversation_ids.add(result.conversation_id)
        failures = [failure for result in chat_results for failure in result.failures]
        return PollResult(
            source_account_id=source_account_id,
            logged_in=True,
            chats_seen=len(chats),
            chats_succeeded=sum(result.succeeded for result in chat_results),
            chats_failed=sum(not result.succeeded for result in chat_results),
            messages_seen=sum(result.messages_seen for result in chat_results),
            messages_processed=sum(result.messages_processed for result in chat_results),
            messages_new=sum(result.messages_new for result in chat_results),
            messages_duplicate=sum(result.messages_duplicate for result in chat_results),
            messages_failed=sum(result.messages_failed for result in chat_results),
            messages_skipped_by_checkpoint=sum(
                result.messages_skipped_by_checkpoint for result in chat_results
            ),
            messages_skipped_as_self=sum(
                result.messages_skipped_as_self for result in chat_results
            ),
            messages_without_server_id=sum(
                result.messages_without_server_id for result in chat_results
            ),
            bootstrapped_chats=sum(result.bootstrapped for result in chat_results),
            failures=failures,
            chat_results=chat_results,
        )

    def _invalidate_all_empty_window_markers(self) -> None:
        self._empty_window_markers.clear()
        self._active_source_account_id = None

    def _empty_window_marker_key(
        self,
        source_account_id: str,
        conversation_id: str,
    ) -> tuple[str, str]:
        return (source_account_id, conversation_id)

    def _record_empty_window_marker(
        self,
        *,
        source_account_id: str,
        conversation_id: str,
        checkpoint: object,
    ) -> bool:
        if not _valid_checkpoint_marker_state(checkpoint):
            return False
        try:
            observed_at = _aware_utc(self._clock())
        except Exception:
            return False
        if observed_at is None:
            return False
        key = self._empty_window_marker_key(source_account_id, conversation_id)
        marker = _EmptyWindowMarker(
            source_account_id=source_account_id,
            conversation_id=conversation_id,
            checkpoint_last_local_id=checkpoint.last_local_id,
            checkpoint_generation=checkpoint.regression_generation,
            checkpoint_fingerprint=checkpoint.last_message_fingerprint,
            observed_at=observed_at,
        )
        self._empty_window_markers[key] = marker
        return True

    def _take_empty_window_marker(
        self,
        *,
        source_account_id: str,
        conversation_id: str,
    ) -> _EmptyWindowMarker | None:
        return self._empty_window_markers.pop(
            self._empty_window_marker_key(source_account_id, conversation_id),
            None,
        )

    def _poll_chat(
        self,
        source_account_id: str,
        chat: Mapping[str, Any],
        *,
        failed_conversation_ids: set[str],
    ) -> ChatPollResult:
        try:
            conversation_id, conversation_name = _parse_chat(chat)
        except Exception as error:
            return ChatPollResult(
                succeeded=False,
                failures=[_failure(PollFailureStage.PARSE_CHAT, error)],
            )

        if conversation_id in failed_conversation_ids:
            self._take_empty_window_marker(
                source_account_id=source_account_id,
                conversation_id=conversation_id,
            )
            return ChatPollResult(
                conversation_id=conversation_id,
                conversation_name=conversation_name,
                succeeded=False,
                failures=[
                    PollFailure(
                        stage=PollFailureStage.POLL_CHAT,
                        code="wechat_conversation_failed_earlier_in_cycle",
                        conversation_id=conversation_id,
                    )
                ],
            )

        try:
            raw_messages = self._client.list_messages(conversation_id)
        except Exception as error:
            self._take_empty_window_marker(
                source_account_id=source_account_id,
                conversation_id=conversation_id,
            )
            return ChatPollResult(
                conversation_id=conversation_id,
                conversation_name=conversation_name,
                succeeded=False,
                failures=[
                    _failure(
                        PollFailureStage.LIST_MESSAGES,
                        error,
                        conversation_id=conversation_id,
                    )
                ],
            )

        messages_seen = len(raw_messages)
        try:
            ordered_messages = _validated_ordered_messages(
                raw_messages,
                conversation_id=conversation_id,
            )
        except Exception as error:
            self._take_empty_window_marker(
                source_account_id=source_account_id,
                conversation_id=conversation_id,
            )
            return ChatPollResult(
                conversation_id=conversation_id,
                conversation_name=conversation_name,
                succeeded=False,
                messages_seen=messages_seen,
                failures=[
                    _failure(
                        PollFailureStage.VALIDATE_MESSAGE,
                        error,
                        conversation_id=conversation_id,
                    )
                ],
            )

        messages_without_server_id = sum(
            not _message_has_usable_server_id(raw_message) for _, raw_message in ordered_messages
        )
        try:
            checkpoint = self._checkpoint_store.get(
                source_account_id=source_account_id,
                conversation_id=conversation_id,
            )
        except Exception as error:
            self._take_empty_window_marker(
                source_account_id=source_account_id,
                conversation_id=conversation_id,
            )
            return ChatPollResult(
                conversation_id=conversation_id,
                conversation_name=conversation_name,
                succeeded=False,
                messages_seen=messages_seen,
                messages_without_server_id=messages_without_server_id,
                failures=[
                    _failure(
                        PollFailureStage.CHECKPOINT,
                        error,
                        conversation_id=conversation_id,
                    )
                ],
            )

        if (
            self._bootstrap_mode is BootstrapMode.LATEST
            and checkpoint is not None
            and checkpoint.last_local_id > 0
            and not ordered_messages
        ):
            marker_recorded = self._record_empty_window_marker(
                source_account_id=source_account_id,
                conversation_id=conversation_id,
                checkpoint=checkpoint,
            )
            return _continuity_failure_result(
                source_account_id=source_account_id,
                conversation_id=conversation_id,
                conversation_name=conversation_name,
                messages_seen=messages_seen,
                messages_skipped=0,
                messages_without_server_id=messages_without_server_id,
                bootstrapped=False,
                checkpoint=checkpoint.last_local_id,
                generation=checkpoint.regression_generation,
                remote_first_local_id=0,
                remote_latest_local_id=0,
                recovery_action=(
                    "stop_chat_visible_window_empty"
                    if marker_recorded
                    else "stop_chat_empty_window_marker_unavailable"
                ),
                warning_keys=self._continuity_warning_keys,
            )

        empty_window_marker = self._take_empty_window_marker(
            source_account_id=source_account_id,
            conversation_id=conversation_id,
        )
        if empty_window_marker is not None and not _marker_matches_checkpoint(
            empty_window_marker,
            checkpoint,
        ):
            return _continuity_failure_result(
                source_account_id=source_account_id,
                conversation_id=conversation_id,
                conversation_name=conversation_name,
                messages_seen=messages_seen,
                messages_skipped=messages_seen,
                messages_without_server_id=messages_without_server_id,
                bootstrapped=False,
                checkpoint=empty_window_marker.checkpoint_last_local_id,
                generation=empty_window_marker.checkpoint_generation,
                remote_first_local_id=ordered_messages[0][0] if ordered_messages else 0,
                remote_latest_local_id=ordered_messages[-1][0] if ordered_messages else 0,
                recovery_action="stop_chat_empty_window_marker_mismatch",
                warning_keys=self._continuity_warning_keys,
            )

        bootstrapped = False
        if checkpoint is None:
            initial_local_id = (
                ordered_messages[-1][0]
                if self._bootstrap_mode is BootstrapMode.LATEST and ordered_messages
                else 0
            )
            try:
                initial_fingerprint = (
                    build_wechat_checkpoint_fingerprint(ordered_messages[-1][1])
                    if initial_local_id > 0
                    else None
                )
                checkpoint, bootstrapped = self._checkpoint_store.initialize(
                    source_account_id=source_account_id,
                    conversation_id=conversation_id,
                    last_local_id=initial_local_id,
                    last_message_fingerprint=initial_fingerprint,
                )
            except Exception as error:
                return ChatPollResult(
                    conversation_id=conversation_id,
                    conversation_name=conversation_name,
                    succeeded=False,
                    messages_seen=messages_seen,
                    messages_without_server_id=messages_without_server_id,
                    failures=[
                        _failure(
                            PollFailureStage.CHECKPOINT,
                            error,
                            conversation_id=conversation_id,
                        )
                    ],
                )
            if bootstrapped and self._bootstrap_mode is BootstrapMode.LATEST:
                logger.info(
                    "message skipped by bootstrap",
                    extra={
                        "fields": {
                            "source_account_id_ref": _redacted_reference(
                                "source_account", source_account_id
                            ),
                            "conversation_id_ref": _redacted_reference(
                                "conversation", conversation_id
                            ),
                            "messages_skipped": messages_seen,
                            "generation": checkpoint.regression_generation,
                        }
                    },
                )
                return ChatPollResult(
                    conversation_id=conversation_id,
                    conversation_name=conversation_name,
                    succeeded=True,
                    messages_seen=messages_seen,
                    messages_skipped_by_checkpoint=messages_seen,
                    messages_without_server_id=messages_without_server_id,
                    bootstrapped=True,
                )

        if (
            checkpoint.last_local_id > 0
            and ordered_messages
            and ordered_messages[0][0] <= checkpoint.last_local_id
        ):
            old_checkpoint = checkpoint.last_local_id
            old_generation = checkpoint.regression_generation
            remote_first_local_id = ordered_messages[0][0]
            remote_latest_local_id = ordered_messages[-1][0]
            skipped_visible = sum(local_id <= old_checkpoint for local_id, _ in ordered_messages)
            regression_detected = remote_latest_local_id < old_checkpoint
            anchor_match: bool | None = None

            if not regression_detected:
                anchor_candidates = [
                    raw_message
                    for local_id, raw_message in ordered_messages
                    if local_id == old_checkpoint
                ]
                if len(anchor_candidates) != 1:
                    return _continuity_failure_result(
                        source_account_id=source_account_id,
                        conversation_id=conversation_id,
                        conversation_name=conversation_name,
                        messages_seen=messages_seen,
                        messages_skipped=skipped_visible,
                        messages_without_server_id=messages_without_server_id,
                        bootstrapped=bootstrapped,
                        checkpoint=old_checkpoint,
                        generation=old_generation,
                        remote_first_local_id=remote_first_local_id,
                        remote_latest_local_id=remote_latest_local_id,
                        recovery_action="stop_chat_anchor_ambiguous",
                        warning_keys=self._continuity_warning_keys,
                    )

                remote_fingerprint = build_wechat_checkpoint_fingerprint(anchor_candidates[0])
                saved_fingerprint = checkpoint.last_message_fingerprint
                if saved_fingerprint is None:
                    if remote_fingerprint is None:
                        return _continuity_failure_result(
                            source_account_id=source_account_id,
                            conversation_id=conversation_id,
                            conversation_name=conversation_name,
                            messages_seen=messages_seen,
                            messages_skipped=skipped_visible,
                            messages_without_server_id=messages_without_server_id,
                            bootstrapped=bootstrapped,
                            checkpoint=old_checkpoint,
                            generation=old_generation,
                            remote_first_local_id=remote_first_local_id,
                            remote_latest_local_id=remote_latest_local_id,
                            recovery_action="stop_chat_anchor_unavailable",
                            warning_keys=self._continuity_warning_keys,
                        )
                    try:
                        _, enrolled = self._checkpoint_store.enroll_anchor(
                            source_account_id=source_account_id,
                            conversation_id=conversation_id,
                            expected_last_local_id=old_checkpoint,
                            expected_generation=old_generation,
                            last_message_fingerprint=remote_fingerprint,
                        )
                    except Exception as error:
                        return ChatPollResult(
                            conversation_id=conversation_id,
                            conversation_name=conversation_name,
                            succeeded=False,
                            messages_seen=messages_seen,
                            messages_skipped_by_checkpoint=skipped_visible,
                            messages_without_server_id=messages_without_server_id,
                            bootstrapped=bootstrapped,
                            failures=[
                                _failure(
                                    PollFailureStage.CHECKPOINT,
                                    error,
                                    conversation_id=conversation_id,
                                )
                            ],
                        )
                    _log_checkpoint_event(
                        "checkpoint anchor enrolled",
                        source_account_id=source_account_id,
                        conversation_id=conversation_id,
                        old_checkpoint=old_checkpoint,
                        remote_first_local_id=remote_first_local_id,
                        remote_latest_local_id=remote_latest_local_id,
                        old_generation=old_generation,
                        new_generation=old_generation,
                        anchor_match=None,
                        recovery_action="anchor_enrolled_wait_for_confirmation",
                        cas_result=enrolled,
                    )
                    return _continuity_failure(
                        conversation_id=conversation_id,
                        conversation_name=conversation_name,
                        messages_seen=messages_seen,
                        messages_skipped=skipped_visible,
                        messages_without_server_id=messages_without_server_id,
                        bootstrapped=bootstrapped,
                    )

                if remote_fingerprint is None:
                    return _continuity_failure_result(
                        source_account_id=source_account_id,
                        conversation_id=conversation_id,
                        conversation_name=conversation_name,
                        messages_seen=messages_seen,
                        messages_skipped=skipped_visible,
                        messages_without_server_id=messages_without_server_id,
                        bootstrapped=bootstrapped,
                        checkpoint=old_checkpoint,
                        generation=old_generation,
                        remote_first_local_id=remote_first_local_id,
                        remote_latest_local_id=remote_latest_local_id,
                        recovery_action="stop_chat_remote_anchor_unavailable",
                        warning_keys=self._continuity_warning_keys,
                    )
                anchor_match = saved_fingerprint == remote_fingerprint
                regression_detected = not anchor_match

            if regression_detected:
                live_suffix_start_index: int | None = None
                if self._bootstrap_mode is BootstrapMode.LATEST and empty_window_marker is not None:
                    try:
                        live_suffix_start_index = _live_suffix_start_index(
                            empty_window_marker,
                            ordered_messages,
                        )
                    except ValueError:
                        return _continuity_failure_result(
                            source_account_id=source_account_id,
                            conversation_id=conversation_id,
                            conversation_name=conversation_name,
                            messages_seen=messages_seen,
                            messages_skipped=messages_seen,
                            messages_without_server_id=messages_without_server_id,
                            bootstrapped=bootstrapped,
                            checkpoint=old_checkpoint,
                            generation=old_generation,
                            remote_first_local_id=remote_first_local_id,
                            remote_latest_local_id=remote_latest_local_id,
                            recovery_action="stop_chat_empty_window_time_unverified",
                            warning_keys=self._continuity_warning_keys,
                        )
                recovery_action = (
                    "process_live_suffix_after_empty_window"
                    if live_suffix_start_index is not None
                    else (
                        "rebase_latest_visible_window"
                        if self._bootstrap_mode is BootstrapMode.LATEST
                        else "rewind_visible_window"
                    )
                )
                _log_checkpoint_event(
                    "checkpoint regression detected",
                    source_account_id=source_account_id,
                    conversation_id=conversation_id,
                    old_checkpoint=old_checkpoint,
                    remote_first_local_id=remote_first_local_id,
                    remote_latest_local_id=remote_latest_local_id,
                    old_generation=old_generation,
                    new_generation=old_generation + 1,
                    anchor_match=anchor_match,
                    recovery_action=recovery_action,
                    cas_result=None,
                    messages_skipped=(
                        live_suffix_start_index
                        if live_suffix_start_index is not None
                        else (
                            messages_seen if self._bootstrap_mode is BootstrapMode.LATEST else None
                        )
                    ),
                )
                if self._bootstrap_mode is BootstrapMode.LATEST:
                    if live_suffix_start_index is not None:
                        first_live_local_id = ordered_messages[live_suffix_start_index][0]
                        baseline_fingerprint = (
                            build_wechat_checkpoint_fingerprint(
                                ordered_messages[live_suffix_start_index - 1][1]
                            )
                            if live_suffix_start_index > 0
                            else None
                        )
                        if live_suffix_start_index > 0 and baseline_fingerprint is None:
                            return _continuity_failure_result(
                                source_account_id=source_account_id,
                                conversation_id=conversation_id,
                                conversation_name=conversation_name,
                                messages_seen=messages_seen,
                                messages_skipped=messages_seen,
                                messages_without_server_id=messages_without_server_id,
                                bootstrapped=bootstrapped,
                                checkpoint=old_checkpoint,
                                generation=old_generation,
                                remote_first_local_id=remote_first_local_id,
                                remote_latest_local_id=remote_latest_local_id,
                                recovery_action="stop_chat_live_suffix_baseline_unavailable",
                                warning_keys=self._continuity_warning_keys,
                            )
                        try:
                            checkpoint, live_suffix_started = (
                                self._checkpoint_store.begin_live_suffix_after_empty_window(
                                    source_account_id=source_account_id,
                                    conversation_id=conversation_id,
                                    expected_last_local_id=old_checkpoint,
                                    expected_generation=old_generation,
                                    expected_message_fingerprint=(
                                        checkpoint.last_message_fingerprint
                                    ),
                                    first_live_local_id=first_live_local_id,
                                    baseline_message_fingerprint=baseline_fingerprint,
                                )
                            )
                        except Exception as error:
                            _log_checkpoint_event(
                                "checkpoint regression failed closed",
                                source_account_id=source_account_id,
                                conversation_id=conversation_id,
                                old_checkpoint=old_checkpoint,
                                remote_first_local_id=remote_first_local_id,
                                remote_latest_local_id=remote_latest_local_id,
                                old_generation=old_generation,
                                new_generation=old_generation,
                                anchor_match=anchor_match,
                                recovery_action="stop_chat_live_suffix_transition_error",
                                cas_result=None,
                                messages_skipped=messages_seen,
                            )
                            return ChatPollResult(
                                conversation_id=conversation_id,
                                conversation_name=conversation_name,
                                succeeded=False,
                                messages_seen=messages_seen,
                                messages_skipped_by_checkpoint=messages_seen,
                                messages_without_server_id=messages_without_server_id,
                                bootstrapped=bootstrapped,
                                failures=[
                                    _failure(
                                        PollFailureStage.CHECKPOINT,
                                        error,
                                        conversation_id=conversation_id,
                                    )
                                ],
                            )

                        _log_checkpoint_event(
                            (
                                "checkpoint regression live suffix started"
                                if live_suffix_started
                                else "checkpoint regression live suffix conflicted"
                            ),
                            source_account_id=source_account_id,
                            conversation_id=conversation_id,
                            old_checkpoint=old_checkpoint,
                            remote_first_local_id=remote_first_local_id,
                            remote_latest_local_id=remote_latest_local_id,
                            old_generation=old_generation,
                            new_generation=checkpoint.regression_generation,
                            anchor_match=anchor_match,
                            recovery_action=(
                                "process_live_suffix_after_empty_window"
                                if live_suffix_started
                                else "stop_chat_after_cas_loss"
                            ),
                            cas_result=live_suffix_started,
                            messages_skipped=live_suffix_start_index,
                        )
                        if not live_suffix_started:
                            return ChatPollResult(
                                conversation_id=conversation_id,
                                conversation_name=conversation_name,
                                succeeded=False,
                                messages_seen=messages_seen,
                                messages_skipped_by_checkpoint=messages_seen,
                                messages_without_server_id=messages_without_server_id,
                                bootstrapped=bootstrapped,
                                failures=[
                                    _failure(
                                        PollFailureStage.CHECKPOINT,
                                        WechatCheckpointStateConflictError(),
                                        conversation_id=conversation_id,
                                    )
                                ],
                            )
                        bootstrapped = True
                    else:
                        remote_latest_fingerprint = build_wechat_checkpoint_fingerprint(
                            ordered_messages[-1][1]
                        )
                        if remote_latest_fingerprint is None:
                            _log_checkpoint_event(
                                "checkpoint regression failed closed",
                                source_account_id=source_account_id,
                                conversation_id=conversation_id,
                                old_checkpoint=old_checkpoint,
                                remote_first_local_id=remote_first_local_id,
                                remote_latest_local_id=remote_latest_local_id,
                                old_generation=old_generation,
                                new_generation=old_generation,
                                anchor_match=anchor_match,
                                recovery_action="stop_chat_latest_fingerprint_unavailable",
                                cas_result=None,
                                messages_skipped=messages_seen,
                            )
                            return _continuity_failure(
                                conversation_id=conversation_id,
                                conversation_name=conversation_name,
                                messages_seen=messages_seen,
                                messages_skipped=messages_seen,
                                messages_without_server_id=messages_without_server_id,
                                bootstrapped=bootstrapped,
                            )
                        try:
                            checkpoint, rebased = (
                                self._checkpoint_store.rebase_latest_after_regression(
                                    source_account_id=source_account_id,
                                    conversation_id=conversation_id,
                                    expected_last_local_id=old_checkpoint,
                                    expected_generation=old_generation,
                                    expected_message_fingerprint=(
                                        checkpoint.last_message_fingerprint
                                    ),
                                    remote_latest_local_id=remote_latest_local_id,
                                    remote_latest_message_fingerprint=(remote_latest_fingerprint),
                                )
                            )
                        except Exception as error:
                            _log_checkpoint_event(
                                "checkpoint regression failed closed",
                                source_account_id=source_account_id,
                                conversation_id=conversation_id,
                                old_checkpoint=old_checkpoint,
                                remote_first_local_id=remote_first_local_id,
                                remote_latest_local_id=remote_latest_local_id,
                                old_generation=old_generation,
                                new_generation=old_generation,
                                anchor_match=anchor_match,
                                recovery_action="stop_chat_latest_rebase_error",
                                cas_result=None,
                                messages_skipped=messages_seen,
                            )
                            return ChatPollResult(
                                conversation_id=conversation_id,
                                conversation_name=conversation_name,
                                succeeded=False,
                                messages_seen=messages_seen,
                                messages_skipped_by_checkpoint=messages_seen,
                                messages_without_server_id=messages_without_server_id,
                                bootstrapped=bootstrapped,
                                failures=[
                                    _failure(
                                        PollFailureStage.CHECKPOINT,
                                        error,
                                        conversation_id=conversation_id,
                                    )
                                ],
                            )

                        _log_checkpoint_event(
                            (
                                "checkpoint regression rebased"
                                if rebased
                                else "checkpoint regression rebase conflicted"
                            ),
                            source_account_id=source_account_id,
                            conversation_id=conversation_id,
                            old_checkpoint=old_checkpoint,
                            remote_first_local_id=remote_first_local_id,
                            remote_latest_local_id=remote_latest_local_id,
                            old_generation=old_generation,
                            new_generation=checkpoint.regression_generation,
                            anchor_match=anchor_match,
                            recovery_action=(
                                "rebase_latest_visible_window"
                                if rebased
                                else "stop_chat_after_cas_loss"
                            ),
                            cas_result=rebased,
                            messages_skipped=messages_seen,
                        )
                        if not rebased:
                            return ChatPollResult(
                                conversation_id=conversation_id,
                                conversation_name=conversation_name,
                                succeeded=False,
                                messages_seen=messages_seen,
                                messages_skipped_by_checkpoint=messages_seen,
                                messages_without_server_id=messages_without_server_id,
                                bootstrapped=bootstrapped,
                                failures=[
                                    _failure(
                                        PollFailureStage.CHECKPOINT,
                                        WechatCheckpointStateConflictError(),
                                        conversation_id=conversation_id,
                                    )
                                ],
                            )
                        return ChatPollResult(
                            conversation_id=conversation_id,
                            conversation_name=conversation_name,
                            succeeded=True,
                            messages_seen=messages_seen,
                            messages_skipped_by_checkpoint=messages_seen,
                            messages_without_server_id=messages_without_server_id,
                            bootstrapped=True,
                        )

                else:
                    try:
                        checkpoint, rewound = self._checkpoint_store.rewind(
                            source_account_id=source_account_id,
                            conversation_id=conversation_id,
                            expected_last_local_id=old_checkpoint,
                            expected_generation=old_generation,
                            expected_message_fingerprint=checkpoint.last_message_fingerprint,
                            last_local_id=remote_first_local_id - 1,
                        )
                    except Exception as error:
                        return ChatPollResult(
                            conversation_id=conversation_id,
                            conversation_name=conversation_name,
                            succeeded=False,
                            messages_seen=messages_seen,
                            messages_without_server_id=messages_without_server_id,
                            bootstrapped=bootstrapped,
                            failures=[
                                _failure(
                                    PollFailureStage.CHECKPOINT,
                                    error,
                                    conversation_id=conversation_id,
                                )
                            ],
                        )
                    _log_checkpoint_event(
                        (
                            "checkpoint regression recovered"
                            if rewound
                            else "checkpoint regression recovery conflicted"
                        ),
                        source_account_id=source_account_id,
                        conversation_id=conversation_id,
                        old_checkpoint=old_checkpoint,
                        remote_first_local_id=remote_first_local_id,
                        remote_latest_local_id=remote_latest_local_id,
                        old_generation=old_generation,
                        new_generation=checkpoint.regression_generation,
                        anchor_match=anchor_match,
                        recovery_action=(
                            "rewind_visible_window" if rewound else "stop_chat_after_cas_loss"
                        ),
                        cas_result=rewound,
                    )
                    if not rewound:
                        return ChatPollResult(
                            conversation_id=conversation_id,
                            conversation_name=conversation_name,
                            succeeded=False,
                            messages_seen=messages_seen,
                            messages_without_server_id=messages_without_server_id,
                            bootstrapped=bootstrapped,
                            failures=[
                                _failure(
                                    PollFailureStage.CHECKPOINT,
                                    WechatCheckpointStateConflictError(),
                                    conversation_id=conversation_id,
                                )
                            ],
                        )

        current_local_id = checkpoint.last_local_id
        messages_processed = 0
        messages_new = 0
        messages_duplicate = 0
        messages_skipped = 0
        messages_skipped_as_self = 0
        for local_id, raw_message in ordered_messages:
            if local_id <= current_local_id:
                messages_skipped += 1
                _log_message_skip(
                    "message skipped by checkpoint",
                    source_account_id=source_account_id,
                    conversation_id=conversation_id,
                    local_id=local_id,
                    generation=checkpoint.regression_generation,
                )
                continue

            is_self = _is_self_message(raw_message)
            if not is_self:
                try:
                    normalized = _normalize_for_chat(
                        raw_message,
                        source_account_id=source_account_id,
                        conversation_id=conversation_id,
                        conversation_name=conversation_name,
                        regression_generation=checkpoint.regression_generation,
                    )
                except Exception as error:
                    failure = _failure(
                        PollFailureStage.NORMALIZE,
                        error,
                        conversation_id=conversation_id,
                        local_id=local_id,
                    )
                    return ChatPollResult(
                        conversation_id=conversation_id,
                        conversation_name=conversation_name,
                        succeeded=False,
                        messages_seen=messages_seen,
                        messages_processed=messages_processed,
                        messages_new=messages_new,
                        messages_duplicate=messages_duplicate,
                        messages_failed=1,
                        messages_skipped_by_checkpoint=messages_skipped,
                        messages_skipped_as_self=messages_skipped_as_self,
                        messages_without_server_id=messages_without_server_id,
                        bootstrapped=bootstrapped,
                        failures=[failure],
                    )

                try:
                    sink_disposition = _handle_with_disposition(self._sink, normalized)
                    if sink_disposition is MessageSinkDisposition.DUPLICATE:
                        messages_duplicate += 1
                    else:
                        messages_new += 1
                except Exception as error:
                    failure = _failure(
                        PollFailureStage.SINK,
                        error,
                        conversation_id=conversation_id,
                        local_id=local_id,
                    )
                    return ChatPollResult(
                        conversation_id=conversation_id,
                        conversation_name=conversation_name,
                        succeeded=False,
                        messages_seen=messages_seen,
                        messages_processed=messages_processed,
                        messages_new=messages_new,
                        messages_duplicate=messages_duplicate,
                        messages_failed=1,
                        messages_skipped_by_checkpoint=messages_skipped,
                        messages_skipped_as_self=messages_skipped_as_self,
                        messages_without_server_id=messages_without_server_id,
                        bootstrapped=bootstrapped,
                        failures=[failure],
                    )
            else:
                messages_skipped_as_self += 1
                _log_message_skip(
                    "message skipped as self",
                    source_account_id=source_account_id,
                    conversation_id=conversation_id,
                    local_id=local_id,
                    generation=checkpoint.regression_generation,
                )

            # A failed checkpoint write deliberately permits redelivery on the next poll.
            try:
                message_fingerprint = build_wechat_checkpoint_fingerprint(raw_message)
                expected_generation = checkpoint.regression_generation
                expected_fingerprint = checkpoint.last_message_fingerprint
                checkpoint, advanced = self._checkpoint_store.advance_cas(
                    source_account_id=source_account_id,
                    conversation_id=conversation_id,
                    expected_last_local_id=current_local_id,
                    expected_generation=expected_generation,
                    expected_message_fingerprint=expected_fingerprint,
                    last_local_id=local_id,
                    last_message_fingerprint=message_fingerprint,
                )
                if not advanced and not _compatible_concurrent_advance(
                    checkpoint,
                    expected_generation=expected_generation,
                    local_id=local_id,
                    message_fingerprint=message_fingerprint,
                ):
                    raise WechatCheckpointStateConflictError()
            except Exception as error:
                failure = _failure(
                    PollFailureStage.CHECKPOINT,
                    error,
                    conversation_id=conversation_id,
                    local_id=local_id,
                )
                return ChatPollResult(
                    conversation_id=conversation_id,
                    conversation_name=conversation_name,
                    succeeded=False,
                    messages_seen=messages_seen,
                    messages_processed=messages_processed,
                    messages_new=messages_new,
                    messages_duplicate=messages_duplicate,
                    messages_failed=1,
                    messages_skipped_by_checkpoint=messages_skipped,
                    messages_skipped_as_self=messages_skipped_as_self,
                    messages_without_server_id=messages_without_server_id,
                    bootstrapped=bootstrapped,
                    failures=[failure],
                )

            current_local_id = checkpoint.last_local_id
            if not is_self:
                messages_processed += 1

        return ChatPollResult(
            conversation_id=conversation_id,
            conversation_name=conversation_name,
            succeeded=True,
            messages_seen=messages_seen,
            messages_processed=messages_processed,
            messages_new=messages_new,
            messages_duplicate=messages_duplicate,
            messages_failed=0,
            messages_skipped_by_checkpoint=messages_skipped,
            messages_skipped_as_self=messages_skipped_as_self,
            messages_without_server_id=messages_without_server_id,
            bootstrapped=bootstrapped,
        )


def _parse_chat(chat: Mapping[str, Any]) -> tuple[str, str | None]:
    if not isinstance(chat, Mapping):
        raise WechatChatIdentityError()
    conversation_id = _nonempty_string(chat.get("id"))
    if conversation_id is None:
        conversation_id = _nonempty_string(chat.get("username"))
    if conversation_id is None:
        raise WechatChatIdentityError()
    return conversation_id, _nonempty_string(chat.get("name"))


def _handle_with_disposition(
    sink: NormalizedMessageSink,
    message: NormalizedWechatMessage,
) -> MessageSinkDisposition:
    disposition_handler = getattr(sink, "handle_with_disposition", None)
    if disposition_handler is None:
        sink.handle(message)
        return MessageSinkDisposition.CREATED
    disposition = disposition_handler(message)
    try:
        return MessageSinkDisposition(disposition)
    except (TypeError, ValueError):
        raise TypeError("message sink returned an invalid disposition") from None


def _numeric_local_id(message: RawWechatMessage | Mapping[str, Any]) -> int:
    value = (
        message.local_id
        if isinstance(message, RawWechatMessage)
        else message.get("localId")
        if isinstance(message, Mapping)
        else None
    )
    if isinstance(value, bool) or value is None:
        raise WechatLocalIdError()
    if isinstance(value, int):
        local_id = value
    elif isinstance(value, str):
        normalized = value.strip()
        if not normalized.isascii() or not normalized.isdigit():
            raise WechatLocalIdError()
        local_id = int(normalized)
    else:
        raise WechatLocalIdError()
    if not 0 < local_id <= MAX_CHECKPOINT_LOCAL_ID:
        raise WechatLocalIdError()
    return local_id


def _is_self_message(message: RawWechatMessage | Mapping[str, Any]) -> bool:
    value = (
        message.is_self
        if isinstance(message, RawWechatMessage)
        else message.get("isSelf")
        if isinstance(message, Mapping)
        else None
    )
    return value is True


def _message_has_usable_server_id(
    message: RawWechatMessage | Mapping[str, Any],
) -> bool:
    value = (
        message.server_id
        if isinstance(message, RawWechatMessage)
        else message.get("serverId")
        if isinstance(message, Mapping)
        else None
    )
    if isinstance(value, bool) or value is None:
        return False
    if isinstance(value, int):
        return value > 0
    if not isinstance(value, str):
        return False
    normalized = value.strip()
    return bool(normalized and normalized != "0")


def _validated_ordered_messages(
    raw_messages: Sequence[RawWechatMessage | Mapping[str, Any]],
    *,
    conversation_id: str,
) -> list[tuple[int, RawWechatMessage | Mapping[str, Any]]]:
    ordered_messages: list[tuple[int, RawWechatMessage | Mapping[str, Any]]] = []
    for message in raw_messages:
        message_chat_id = (
            message.chat_id
            if isinstance(message, RawWechatMessage)
            else message.get("chatId")
            if isinstance(message, Mapping)
            else None
        )
        if _nonempty_string(message_chat_id) != conversation_id:
            raise WechatConversationMismatchError()
        ordered_messages.append((_numeric_local_id(message), message))
    return sorted(ordered_messages, key=lambda item: item[0])


def _aware_utc(value: object) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    try:
        if value.tzinfo is None or value.utcoffset() is None:
            return None
        return value.astimezone(UTC)
    except (OverflowError, ValueError):
        return None


def _valid_checkpoint_marker_state(checkpoint: object) -> bool:
    last_local_id = getattr(checkpoint, "last_local_id", None)
    generation = getattr(checkpoint, "regression_generation", None)
    fingerprint = getattr(checkpoint, "last_message_fingerprint", None)
    if (
        isinstance(last_local_id, bool)
        or not isinstance(last_local_id, int)
        or not 0 < last_local_id <= MAX_CHECKPOINT_LOCAL_ID
        or isinstance(generation, bool)
        or not isinstance(generation, int)
        or not 0 <= generation <= MAX_CHECKPOINT_LOCAL_ID
    ):
        return False
    return (
        isinstance(fingerprint, str)
        and len(fingerprint) == 64
        and all(character in "0123456789abcdef" for character in fingerprint)
    )


def _marker_matches_checkpoint(
    marker: _EmptyWindowMarker,
    checkpoint: object,
) -> bool:
    return _valid_checkpoint_marker_state(checkpoint) and (
        checkpoint.last_local_id == marker.checkpoint_last_local_id
        and checkpoint.regression_generation == marker.checkpoint_generation
        and checkpoint.last_message_fingerprint == marker.checkpoint_fingerprint
    )


def _message_timestamp(message: RawWechatMessage | Mapping[str, Any]) -> datetime | None:
    try:
        validated = (
            message
            if isinstance(message, RawWechatMessage)
            else RawWechatMessage.model_validate(message)
        )
    except (TypeError, ValueError):
        return None
    return _aware_utc(validated.timestamp)


def _live_suffix_start_index(
    marker: _EmptyWindowMarker,
    ordered_messages: Sequence[tuple[int, RawWechatMessage | Mapping[str, Any]]],
) -> int | None:
    previous_local_id = 0
    previous_timestamp: datetime | None = None
    live_suffix_start: int | None = None
    for index, (local_id, message) in enumerate(ordered_messages):
        timestamp = _message_timestamp(message)
        if (
            local_id <= previous_local_id
            or timestamp is None
            or (previous_timestamp is not None and timestamp < previous_timestamp)
        ):
            raise ValueError
        if live_suffix_start is None and timestamp > marker.observed_at:
            live_suffix_start = index
        previous_local_id = local_id
        previous_timestamp = timestamp
    if live_suffix_start is not None:
        first_live_local_id = ordered_messages[live_suffix_start][0]
        if live_suffix_start == 0:
            if first_live_local_id != 1:
                raise ValueError
        elif ordered_messages[live_suffix_start - 1][0] + 1 != first_live_local_id:
            raise ValueError
    return live_suffix_start


def _normalize_for_chat(
    raw_message: RawWechatMessage | Mapping[str, Any],
    *,
    source_account_id: str,
    conversation_id: str,
    conversation_name: str | None,
    regression_generation: int,
) -> NormalizedWechatMessage:
    normalized = normalize_wechat_message(
        raw_message,
        source_account_id=source_account_id,
        conversation_name=conversation_name,
        regression_generation=regression_generation,
    )
    if normalized.conversation_id != conversation_id:
        raise WechatConversationMismatchError()
    return normalized


def _compatible_concurrent_advance(
    checkpoint: object,
    *,
    expected_generation: int,
    local_id: int,
    message_fingerprint: str | None,
) -> bool:
    if not hasattr(checkpoint, "regression_generation") or not hasattr(checkpoint, "last_local_id"):
        return False
    if checkpoint.regression_generation != expected_generation:
        return False
    if checkpoint.last_local_id < local_id:
        return False
    if checkpoint.last_local_id == local_id:
        return checkpoint.last_message_fingerprint == message_fingerprint
    return True


def _continuity_failure_result(
    *,
    source_account_id: str,
    conversation_id: str,
    conversation_name: str | None,
    messages_seen: int,
    messages_skipped: int,
    messages_without_server_id: int,
    bootstrapped: bool,
    checkpoint: int,
    generation: int,
    remote_first_local_id: int,
    remote_latest_local_id: int,
    recovery_action: str,
    warning_keys: set[str],
) -> ChatPollResult:
    warning_key = "|".join(
        (
            _redacted_reference("source_account", source_account_id),
            _redacted_reference("conversation", conversation_id),
            str(checkpoint),
            str(generation),
            str(remote_first_local_id),
            str(remote_latest_local_id),
            recovery_action,
        )
    )
    if warning_key not in warning_keys:
        if len(warning_keys) >= _MAX_CONTINUITY_WARNING_KEYS:
            warning_keys.clear()
        warning_keys.add(warning_key)
        _log_checkpoint_event(
            "checkpoint continuity unverified",
            source_account_id=source_account_id,
            conversation_id=conversation_id,
            old_checkpoint=checkpoint,
            remote_first_local_id=remote_first_local_id,
            remote_latest_local_id=remote_latest_local_id,
            old_generation=generation,
            new_generation=generation,
            anchor_match=None,
            recovery_action=recovery_action,
            cas_result=None,
        )
    return _continuity_failure(
        conversation_id=conversation_id,
        conversation_name=conversation_name,
        messages_seen=messages_seen,
        messages_skipped=messages_skipped,
        messages_without_server_id=messages_without_server_id,
        bootstrapped=bootstrapped,
    )


def _continuity_failure(
    *,
    conversation_id: str,
    conversation_name: str | None,
    messages_seen: int,
    messages_skipped: int,
    messages_without_server_id: int,
    bootstrapped: bool,
) -> ChatPollResult:
    return ChatPollResult(
        conversation_id=conversation_id,
        conversation_name=conversation_name,
        succeeded=False,
        messages_seen=messages_seen,
        messages_skipped_by_checkpoint=messages_skipped,
        messages_without_server_id=messages_without_server_id,
        bootstrapped=bootstrapped,
        failures=[
            _failure(
                PollFailureStage.CHECKPOINT,
                WechatCheckpointContinuityError(),
                conversation_id=conversation_id,
            )
        ],
    )


def _log_checkpoint_event(
    message: str,
    *,
    source_account_id: str,
    conversation_id: str,
    old_checkpoint: int,
    remote_first_local_id: int,
    remote_latest_local_id: int,
    old_generation: int,
    new_generation: int,
    anchor_match: bool | None,
    recovery_action: str,
    cas_result: bool | None,
    messages_skipped: int | None = None,
) -> None:
    fields: dict[str, object] = {
        "source_account_id_ref": _redacted_reference("source_account", source_account_id),
        "conversation_id_ref": _redacted_reference("conversation", conversation_id),
        "old_checkpoint": old_checkpoint,
        "remote_first_local_id": remote_first_local_id,
        "remote_latest_local_id": remote_latest_local_id,
        "old_generation": old_generation,
        "new_generation": new_generation,
        "anchor_match": anchor_match,
        "recovery_action": recovery_action,
        "cas_result": cas_result,
    }
    if messages_skipped is not None:
        fields["messages_skipped"] = messages_skipped
    logger.warning(
        message,
        extra={"fields": fields},
    )


def _log_message_skip(
    message: str,
    *,
    source_account_id: str,
    conversation_id: str,
    local_id: int,
    generation: int,
) -> None:
    logger.debug(
        message,
        extra={
            "fields": {
                "source_account_id_ref": _redacted_reference("source_account", source_account_id),
                "conversation_id_ref": _redacted_reference("conversation", conversation_id),
                "local_id": local_id,
                "generation": generation,
            }
        },
    )


def _log_chat_result(source_account_id: str, result: ChatPollResult) -> None:
    logger.info(
        "poll chat completed",
        extra={
            "fields": {
                "source_account_id_ref": _redacted_reference("source_account", source_account_id),
                "conversation_id_ref": (
                    _redacted_reference("conversation", result.conversation_id)
                    if result.conversation_id is not None
                    else None
                ),
                "succeeded": result.succeeded,
                "failure_count": len(result.failures),
                "messages_seen": result.messages_seen,
                "messages_processed": result.messages_processed,
                "messages_new": result.messages_new,
                "messages_duplicate": result.messages_duplicate,
                "messages_skipped_checkpoint": result.messages_skipped_by_checkpoint,
                "messages_skipped_self": result.messages_skipped_as_self,
                "messages_failed": result.messages_failed,
                "messages_without_server_id": result.messages_without_server_id,
                "bootstrapped": result.bootstrapped,
            }
        },
    )


def _redacted_reference(kind: str, value: str) -> str:
    digest = hashlib.sha256(f"{kind}:{value}".encode()).hexdigest()[:16]
    return f"{kind}:sha256:{digest}"


def _nonempty_string(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def _failure(
    stage: PollFailureStage,
    error: Exception,
    *,
    conversation_id: str | None = None,
    local_id: int | None = None,
) -> PollFailure:
    code = (
        error.code
        if isinstance(error, (WechatAdapterError, WechatPollingError))
        else f"wechat_{stage.value}_error"
    )
    return PollFailure(
        stage=stage,
        code=code,
        conversation_id=conversation_id,
        local_id=local_id,
    )
