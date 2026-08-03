from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictStr


class HermesRequestModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class HermesUserMessage(HermesRequestModel):
    role: Literal["user"] = "user"
    content: StrictStr = Field(min_length=1)


class HermesChatCompletionRequest(HermesRequestModel):
    model: StrictStr = Field(min_length=1)
    messages: list[HermesUserMessage] = Field(min_length=1)


class HermesResponseModel(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


class HermesAssistantMessage(HermesResponseModel):
    role: Literal["assistant"]
    content: StrictStr


class HermesChatCompletionChoice(HermesResponseModel):
    message: HermesAssistantMessage


class HermesChatCompletionResponse(HermesResponseModel):
    choices: list[HermesChatCompletionChoice] = Field(min_length=1)


@dataclass(frozen=True, slots=True)
class HermesDispatchOutcome:
    message_id: int
    workspace_id: str
    ai_thread_id: str
    assistant_content: str


@dataclass(frozen=True, slots=True)
class HermesResponseDeliveryOutcome:
    message_id: int
    ai_thread_id: str
    conversation_id: str
