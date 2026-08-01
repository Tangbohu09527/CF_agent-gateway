from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Protocol

from cf_agent_gateway.adapters.wechat.errors import WechatAdapterError
from cf_agent_gateway.adapters.wechat.normalized_models import NormalizedWechatMessage
from cf_agent_gateway.adapters.wechat.normalizer import normalize_wechat_message
from cf_agent_gateway.adapters.wechat.polling_errors import (
    InvalidBootstrapModeError,
    WechatChatIdentityError,
    WechatConversationMismatchError,
    WechatLocalIdError,
    WechatPollingError,
)
from cf_agent_gateway.adapters.wechat.polling_models import (
    MAX_CHECKPOINT_LOCAL_ID,
    BootstrapMode,
    ChatPollResult,
    PollFailure,
    PollFailureStage,
    PollResult,
)
from cf_agent_gateway.adapters.wechat.polling_store import WechatSyncCheckpointStore
from cf_agent_gateway.adapters.wechat.raw_models import AgentWechatAuthStatus, RawWechatMessage


class WechatPollingClient(Protocol):
    def get_auth_status(self) -> AgentWechatAuthStatus: ...

    def list_chats(self) -> list[dict[str, Any]]: ...

    def list_messages(self, chat_id: str) -> list[RawWechatMessage]: ...


class NormalizedMessageSink(Protocol):
    """Receive messages under an at-least-once delivery contract.

    Implementations must be idempotent: a successful handle followed by a failed
    checkpoint write can redeliver the same message. Future durable sinks can use
    ``event_id`` or the source physical-message identity as their uniqueness key.
    """

    def handle(self, message: NormalizedWechatMessage) -> None: ...


class WechatPollingService:
    """Run one finite polling cycle with durable, at-least-once delivery.

    V1 assumes one active poller per source account. Initialization races still
    treat the checkpoint returned by the store as authoritative.
    """

    def __init__(
        self,
        client: WechatPollingClient,
        checkpoint_store: WechatSyncCheckpointStore,
        sink: NormalizedMessageSink,
        *,
        bootstrap_mode: BootstrapMode | str = BootstrapMode.LATEST,
    ) -> None:
        self._client = client
        self._checkpoint_store = checkpoint_store
        self._sink = sink
        try:
            self._bootstrap_mode = BootstrapMode(bootstrap_mode)
        except (TypeError, ValueError):
            raise InvalidBootstrapModeError() from None

    def poll_once(self) -> PollResult:
        try:
            auth_status = self._client.get_auth_status()
        except Exception as error:
            failure = _failure(PollFailureStage.AUTH, error)
            return PollResult(logged_in=False, failures=[failure])

        if auth_status.status != "logged_in":
            return PollResult(logged_in=False)

        source_account_id = _nonempty_string(auth_status.logged_in_user)
        if source_account_id is None:
            failure = PollFailure(
                stage=PollFailureStage.AUTH,
                code="wechat_auth_status_error",
            )
            return PollResult(logged_in=False, failures=[failure])

        try:
            chats = self._client.list_chats()
        except Exception as error:
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
            messages_skipped_by_checkpoint=sum(
                result.messages_skipped_by_checkpoint for result in chat_results
            ),
            bootstrapped_chats=sum(result.bootstrapped for result in chat_results),
            failures=failures,
            chat_results=chat_results,
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

        try:
            checkpoint = self._checkpoint_store.get(
                source_account_id=source_account_id,
                conversation_id=conversation_id,
            )
        except Exception as error:
            return ChatPollResult(
                conversation_id=conversation_id,
                conversation_name=conversation_name,
                succeeded=False,
                messages_seen=messages_seen,
                failures=[
                    _failure(
                        PollFailureStage.CHECKPOINT,
                        error,
                        conversation_id=conversation_id,
                    )
                ],
            )

        bootstrapped = False
        if checkpoint is None:
            initial_local_id = (
                ordered_messages[-1][0]
                if self._bootstrap_mode is BootstrapMode.LATEST and ordered_messages
                else 0
            )
            try:
                checkpoint, bootstrapped = self._checkpoint_store.initialize(
                    source_account_id=source_account_id,
                    conversation_id=conversation_id,
                    last_local_id=initial_local_id,
                )
            except Exception as error:
                return ChatPollResult(
                    conversation_id=conversation_id,
                    conversation_name=conversation_name,
                    succeeded=False,
                    messages_seen=messages_seen,
                    failures=[
                        _failure(
                            PollFailureStage.CHECKPOINT,
                            error,
                            conversation_id=conversation_id,
                        )
                    ],
                )
            if bootstrapped and self._bootstrap_mode is BootstrapMode.LATEST:
                return ChatPollResult(
                    conversation_id=conversation_id,
                    conversation_name=conversation_name,
                    succeeded=True,
                    messages_seen=messages_seen,
                    messages_skipped_by_checkpoint=messages_seen,
                    bootstrapped=True,
                )

        current_local_id = checkpoint.last_local_id
        messages_processed = 0
        messages_skipped = 0
        for local_id, raw_message in ordered_messages:
            if local_id <= current_local_id:
                messages_skipped += 1
                continue

            try:
                normalized = _normalize_for_chat(
                    raw_message,
                    source_account_id=source_account_id,
                    conversation_id=conversation_id,
                    conversation_name=conversation_name,
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
                    messages_skipped_by_checkpoint=messages_skipped,
                    bootstrapped=bootstrapped,
                    failures=[failure],
                )

            try:
                self._sink.handle(normalized)
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
                    messages_skipped_by_checkpoint=messages_skipped,
                    bootstrapped=bootstrapped,
                    failures=[failure],
                )

            # A failed checkpoint write deliberately permits redelivery on the next poll.
            try:
                checkpoint = self._checkpoint_store.advance(
                    source_account_id=source_account_id,
                    conversation_id=conversation_id,
                    last_local_id=local_id,
                )
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
                    messages_skipped_by_checkpoint=messages_skipped,
                    bootstrapped=bootstrapped,
                    failures=[failure],
                )

            current_local_id = checkpoint.last_local_id
            messages_processed += 1

        return ChatPollResult(
            conversation_id=conversation_id,
            conversation_name=conversation_name,
            succeeded=True,
            messages_seen=messages_seen,
            messages_processed=messages_processed,
            messages_skipped_by_checkpoint=messages_skipped,
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


def _normalize_for_chat(
    raw_message: RawWechatMessage | Mapping[str, Any],
    *,
    source_account_id: str,
    conversation_id: str,
    conversation_name: str | None,
) -> NormalizedWechatMessage:
    normalized = normalize_wechat_message(
        raw_message,
        source_account_id=source_account_id,
        conversation_name=conversation_name,
    )
    if normalized.conversation_id != conversation_id:
        raise WechatConversationMismatchError()
    return normalized


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
