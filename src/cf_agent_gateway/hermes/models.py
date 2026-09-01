from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

HERMES_CONTEXT_TOOL_NAMES = (
    "context.read",
    "context.search",
)


class HermesRequestModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class HermesUserMessage(HermesRequestModel):
    role: Literal["user"] = "user"
    content: StrictStr = Field(min_length=1)


class HermesChatCompletionRequest(HermesRequestModel):
    model: StrictStr = Field(min_length=1)
    messages: list[HermesUserMessage] = Field(min_length=1)
    profile_reference: StrictStr | None = Field(default=None, min_length=1, max_length=255)
    profile_revision: StrictInt | None = Field(default=None, gt=0)
    thread_id: StrictStr | None = Field(default=None, min_length=1, max_length=255)
    session_metadata: dict[str, JsonValue] | None = None

    @model_validator(mode="after")
    def validate_v2_invocation(self) -> Self:
        invocation_values = (
            self.profile_reference,
            self.profile_revision,
            self.thread_id,
            self.session_metadata,
        )
        if any(value is not None for value in invocation_values) and not all(
            value is not None for value in invocation_values
        ):
            raise ValueError(
                "profile_reference, profile_revision, thread_id, and session_metadata "
                "must be provided together"
            )
        return self


class HermesResponseModel(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


class ResponsePartModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class TextPart(ResponsePartModel):
    type: Literal["text"] = "text"
    text: StrictStr = Field(min_length=1)


class ArtifactRefPart(ResponsePartModel):
    type: Literal["artifact_ref"] = "artifact_ref"
    artifact_id: StrictStr = Field(min_length=1, max_length=36)

    @field_validator("artifact_id")
    @classmethod
    def validate_artifact_id(cls, value: str) -> str:
        artifact_id = value.strip()
        if not artifact_id:
            raise ValueError("artifact_id must not be empty")
        if (
            artifact_id in {".", ".."}
            or any(character in artifact_id for character in "/\\:")
            or any(ord(character) < 0x20 for character in artifact_id)
        ):
            raise ValueError("artifact_id must not contain a path")
        return artifact_id


type ResponsePart = Annotated[TextPart | ArtifactRefPart, Field(discriminator="type")]


class ResponseEnvelope(ResponsePartModel):
    response_id: StrictStr = Field(min_length=1, max_length=255)
    parts: tuple[ResponsePart, ...] = Field(min_length=1)

    @field_validator("response_id")
    @classmethod
    def validate_response_id(cls, value: str) -> str:
        response_id = value.strip()
        if not response_id:
            raise ValueError("response_id must not be empty")
        if any(ord(character) < 0x20 for character in response_id):
            raise ValueError("response_id contains invalid characters")
        return response_id

    @property
    def assistant_content(self) -> str:
        return "".join(part.text for part in self.parts if isinstance(part, TextPart))

    @property
    def artifact_ids(self) -> tuple[str, ...]:
        return tuple(part.artifact_id for part in self.parts if isinstance(part, ArtifactRefPart))


class HermesAssistantMessage(HermesResponseModel):
    role: Literal["assistant"]
    content: StrictStr


class HermesChatCompletionChoice(HermesResponseModel):
    message: HermesAssistantMessage


class HermesChatCompletionResponse(HermesResponseModel):
    choices: list[HermesChatCompletionChoice] = Field(min_length=1)


@dataclass(frozen=True, slots=True)
class HermesChatResult:
    assistant_content: str
    hermes_thread_id: str
    response: ResponseEnvelope | None = None

    @classmethod
    def from_response(
        cls,
        response: ResponseEnvelope,
        *,
        hermes_thread_id: str,
    ) -> HermesChatResult:
        return cls(
            assistant_content=response.assistant_content,
            hermes_thread_id=hermes_thread_id,
            response=response,
        )

    @property
    def parts(self) -> tuple[ResponsePart, ...]:
        if self.response is not None:
            return self.response.parts
        if self.assistant_content:
            return (TextPart(text=self.assistant_content),)
        return ()


@dataclass(frozen=True, slots=True)
class HermesDispatchOutcome:
    message_id: int
    workspace_id: str
    ai_thread_id: str
    assistant_content: str
    response: ResponseEnvelope | None = None

    @classmethod
    def from_response(
        cls,
        *,
        message_id: int,
        workspace_id: str,
        ai_thread_id: str,
        response: ResponseEnvelope,
    ) -> HermesDispatchOutcome:
        return cls(
            message_id=message_id,
            workspace_id=workspace_id,
            ai_thread_id=ai_thread_id,
            assistant_content=response.assistant_content,
            response=response,
        )

    @property
    def parts(self) -> tuple[ResponsePart, ...]:
        if self.response is not None:
            return self.response.parts
        if self.assistant_content:
            return (TextPart(text=self.assistant_content),)
        return ()


@dataclass(frozen=True, slots=True)
class HermesResponseDeliveryOutcome:
    message_id: int
    ai_thread_id: str
    conversation_id: str
