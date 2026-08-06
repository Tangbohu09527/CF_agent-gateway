from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from cf_agent_gateway.hermes import (
    ArtifactRefPart,
    HermesChatResult,
    HermesDispatchOutcome,
    ResponseEnvelope,
    TextPart,
)


def test_response_envelope_preserves_order_and_projects_legacy_text() -> None:
    envelope = ResponseEnvelope(
        response_id="response-001",
        parts=(
            TextPart(text="Report "),
            ArtifactRefPart(artifact_id="artifact-001"),
            TextPart(text="attached."),
        ),
    )

    assert envelope.assistant_content == "Report attached."
    assert envelope.artifact_ids == ("artifact-001",)
    assert [part.type for part in envelope.parts] == ["text", "artifact_ref", "text"]


def test_response_envelope_parses_discriminated_parts() -> None:
    envelope = ResponseEnvelope.model_validate(
        {
            "response_id": "response-001",
            "parts": [
                {"type": "text", "text": "Done"},
                {"type": "artifact_ref", "artifact_id": "artifact-001"},
            ],
        }
    )

    assert isinstance(envelope.parts[0], TextPart)
    assert isinstance(envelope.parts[1], ArtifactRefPart)


def test_artifact_reference_serialization_never_contains_a_path() -> None:
    envelope = ResponseEnvelope(
        response_id="response-001",
        parts=(ArtifactRefPart(artifact_id="artifact-001"),),
    )

    payload = envelope.model_dump(mode="json")
    serialized = json.dumps(payload)

    assert payload == {
        "response_id": "response-001",
        "parts": [{"type": "artifact_ref", "artifact_id": "artifact-001"}],
    }
    assert "path" not in serialized
    assert "storage_key" not in serialized


@pytest.mark.parametrize("path_field", ["path", "local_path", "storage_path", "storage_key"])
def test_artifact_reference_rejects_path_fields(path_field: str) -> None:
    with pytest.raises(ValidationError):
        ArtifactRefPart.model_validate(
            {
                "type": "artifact_ref",
                "artifact_id": "artifact-001",
                path_field: "C:/private/artifact.bin",
            }
        )


@pytest.mark.parametrize(
    "artifact_id",
    ["../artifact", "folder/artifact", "folder\\artifact", "C:\\private\\artifact"],
)
def test_artifact_reference_rejects_path_shaped_ids(artifact_id: str) -> None:
    with pytest.raises(ValidationError):
        ArtifactRefPart(artifact_id=artifact_id)


@pytest.mark.parametrize(
    "payload",
    [
        {"response_id": "response-001", "parts": [{"type": "unknown"}]},
        {
            "response_id": "response-001",
            "parts": [{"type": "artifact_ref", "artifact_id": "   "}],
        },
        {"response_id": "   ", "parts": [{"type": "text", "text": "value"}]},
        {"response_id": "response-001", "parts": []},
    ],
)
def test_response_envelope_rejects_invalid_identifiers_and_parts(payload: object) -> None:
    with pytest.raises(ValidationError):
        ResponseEnvelope.model_validate(payload)


def test_v2_response_can_build_legacy_compatible_results() -> None:
    envelope = ResponseEnvelope(
        response_id="response-001",
        parts=(
            TextPart(text="The report is ready."),
            ArtifactRefPart(artifact_id="artifact-001"),
        ),
    )

    chat_result = HermesChatResult.from_response(
        envelope,
        hermes_thread_id="hermes-thread-001",
    )
    dispatch = HermesDispatchOutcome.from_response(
        message_id=7,
        workspace_id="workspace-001",
        ai_thread_id="ai-thread-001",
        response=envelope,
    )

    assert chat_result.assistant_content == "The report is ready."
    assert chat_result.parts == envelope.parts
    assert dispatch.assistant_content == "The report is ready."
    assert dispatch.parts == envelope.parts


def test_legacy_text_result_projects_a_text_part() -> None:
    result = HermesChatResult(
        assistant_content="Legacy response",
        hermes_thread_id="hermes-thread-001",
    )

    assert result.response is None
    assert result.parts == (TextPart(text="Legacy response"),)
