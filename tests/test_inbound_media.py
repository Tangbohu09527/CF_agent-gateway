"""Contract fixtures are synthetic; no production services or credentials."""

import base64
import hashlib
import json

import pytest

from cf_agent_gateway.adapters.wechat.inbound_media import (
    MAX_MEDIA_BYTES,
    ExpectedOriginal,
    InboundMediaError,
    MediaReadiness,
    OriginalComparison,
    parse_inbound_media,
)

PDF = b"%PDF-1.7\nsynthetic fixture, not a complete PDF\n"
JPEG = b"\xff\xd8\xffsynthetic image, not a complete JPEG\xff\xd9"
PNG = b"\x89PNG\r\n\x1a\nsynthetic image, not a complete PNG"


def response(data=PDF, kind="file", **extra):
    return {"type": kind, "data": base64.b64encode(data).decode("ascii"), **extra}


def expectation(data):
    return ExpectedOriginal(len(data), hashlib.sha256(data).hexdigest())


@pytest.mark.parametrize("kind", ["pending", "unsupported"])
@pytest.mark.parametrize("data", [None, ""])
def test_unready_is_explicit_and_does_not_prove_download_queued(kind, data):
    parsed = parse_inbound_media(
        {"type": kind, "format": "", "filename": "", "data": data},
        expected=expectation(PDF),
    )
    assert parsed.readiness == MediaReadiness(kind)
    assert parsed.data is None and parsed.size is None and parsed.sha256 is None
    assert parsed.original_comparison == OriginalComparison.NOT_CHECKED
    assert parsed.safe_summary()["upstream_download_queued_proven"] is False


def test_observed_pending_shape_without_data():
    parsed = parse_inbound_media({"type": "pending", "format": "", "filename": ""})
    assert parsed.readiness is MediaReadiness.PENDING


@pytest.mark.parametrize(
    "data,kind,sig",
    [(PDF, "file", "pdf"), (JPEG, "image", "jpeg"), (PNG, "image", "png")],
)
def test_independent_byte_and_digest_match(data, kind, sig):
    parsed = parse_inbound_media(response(data, kind), expected=expectation(data))
    assert parsed.readiness is MediaReadiness.READY
    assert parsed.data == data and parsed.signature == sig
    assert parsed.original_comparison is OriginalComparison.MATCH
    assert parsed.safe_summary()["durable_storage_verified"] is False


@pytest.mark.parametrize("quality", [None, "thumbnail", "standard", "full"])
def test_quality_claim_does_not_prove_original(quality):
    parsed = parse_inbound_media(response(JPEG, "image", quality=quality))
    assert parsed.declared_quality == quality
    assert parsed.original_comparison is OriginalComparison.NOT_CHECKED
    mismatch = parse_inbound_media(
        response(JPEG, "image", quality=quality), expected=expectation(JPEG + b"original")
    )
    assert mismatch.readiness is MediaReadiness.READY
    assert mismatch.original_comparison is OriginalComparison.DIFFERENT


def test_matching_hash_with_wrong_length_is_not_original_match():
    expected = ExpectedOriginal(len(PDF) + 1, hashlib.sha256(PDF).hexdigest())
    assert parse_inbound_media(response(), expected=expected).original_comparison == "different"


def test_original_expectation_cannot_be_self_attested_by_response():
    parsed = parse_inbound_media(
        response(sha256=hashlib.sha256(PDF).hexdigest(), original=True, originalBytes=len(PDF))
    )
    assert parsed.original_comparison is OriginalComparison.NOT_CHECKED


@pytest.mark.parametrize(
    "payload,code",
    [
        (None, "invalid_media_response"),
        ([], "invalid_media_response"),
        ({}, "unrecognized_media_type"),
        ({"type": "future-format"}, "unrecognized_media_type"),
        ({"type": ["image"]}, "unrecognized_media_type"),
        ({"type": "pending", "data": "abc"}, "inconsistent_media_response"),
        ({"type": "unsupported", "data": "abc"}, "inconsistent_media_response"),
        ({"type": "file"}, "inline_media_data_missing"),
        ({"type": "image", "data": ""}, "inline_media_data_missing"),
        ({"type": "file", "data": []}, "inline_media_data_missing"),
        ({"type": "file", "data": "中文"}, "invalid_media_base64"),
        ({"type": "file", "data": "@@=="}, "invalid_media_base64"),
        ({"type": "file", "data": "AB=="}, "invalid_media_base64"),
        ({"type": "file", "data": "YQ==\n"}, "invalid_media_base64"),
        (response(PDF, "image"), "unaccepted_image_signature"),
        (response(quality="full"), "inconsistent_media_response"),
        (response(JPEG, "image", quality="future"), "unrecognized_image_quality"),
        (response(success=False), "upstream_media_error"),
        (response(error="DO-NOT-ECHO-PRIVATE"), "upstream_media_error"),
    ],
)
def test_contract_errors_are_static(payload, code):
    with pytest.raises(InboundMediaError) as caught:
        parse_inbound_media(payload)
    assert caught.value.code == code
    assert str(caught.value) == code
    assert "DO-NOT-ECHO" not in repr(caught.value)


@pytest.mark.parametrize(
    "url",
    [
        "https://example.invalid/?token=DO-NOT-ECHO",
        "file:///etc/x",
        {"secret": "DO-NOT-ECHO"},
    ],
)
def test_urls_are_not_followed_or_echoed(url):
    with pytest.raises(InboundMediaError) as caught:
        parse_inbound_media(response(url=url))
    assert str(caught.value) == "media_url_requires_separate_contract"


@pytest.mark.parametrize("name", ["../secret", r"..\secret", "C:secret", ".", "..", "x\ny"])
def test_unsafe_filename_is_not_a_local_path(name):
    with pytest.raises(InboundMediaError):
        parse_inbound_media(response(filename=name))


def test_safe_summary_and_repr_do_not_contain_file_contents_or_filename():
    parsed = parse_inbound_media(response(filename="PRIVATE-NAME.pdf"))
    assert parsed.filename == "PRIVATE-NAME.pdf"
    assert "PRIVATE-NAME" not in repr(parsed)
    assert "synthetic fixture" not in repr(parsed)
    summary = json.dumps(parsed.safe_summary())
    assert "PRIVATE-NAME" not in summary and "synthetic fixture" not in summary


@pytest.mark.parametrize("limit", [True, 0, -1, MAX_MEDIA_BYTES + 1, "100"])
def test_invalid_limits(limit):
    with pytest.raises(InboundMediaError, match="invalid_media_limit"):
        parse_inbound_media(response(), max_bytes=limit)


def test_decoded_limit_even_with_same_base64_allocation():
    assert parse_inbound_media(response(b"a"), max_bytes=1).data == b"a"
    with pytest.raises(InboundMediaError, match="media_too_large"):
        parse_inbound_media(response(b"ab"), max_bytes=1)
    with pytest.raises(InboundMediaError, match="media_too_large"):
        parse_inbound_media(response(b"abcdef"), max_bytes=1)


@pytest.mark.parametrize(
    "size,digest",
    [(True, "0" * 64), (0, "0" * 64), (1, "F" * 64), (1, "x"), (1, None)],
)
def test_invalid_expectations(size, digest):
    with pytest.raises(InboundMediaError, match="invalid_original_expectation"):
        ExpectedOriginal(size, digest)


def test_expected_mapping_not_silently_used():
    with pytest.raises(InboundMediaError, match="invalid_original_expectation"):
        parse_inbound_media(response(), expected={"size": len(PDF), "sha256": "0" * 64})


def test_generic_file_header_is_not_full_content_validation():
    parsed = parse_inbound_media(response(b"not a known header"))
    assert parsed.signature == "other" and parsed.readiness is MediaReadiness.READY
    assert parsed.safe_summary()["durable_storage_verified"] is False
