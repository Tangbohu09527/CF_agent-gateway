from __future__ import annotations

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
