# WeChat outbound media adapter

## Status and boundary

The outbound Media Adapter V2 is implemented and automated-test covered. The Delivery
Worker uses it for response-owned Artifact parts whose kind is `image` or `file`.
Ordered text and Artifact parts are persisted before delivery.

The recorded CFserver production acceptance did not exercise a real outbound media
Message. This document therefore does not claim live production media acceptance.

This adapter does not provide general inbound image/file understanding, OCR, archive
processing, or arbitrary attachment ingestion. The lower-level `agent-wechat` media
client capability is not a general polling-to-Hermes file workflow.

## Outbound contract

`WechatMediaSender.send_media` is the Gateway sender boundary.
`WechatHttpMediaSender` implements it while retaining the text sender contract.

Both media types call:

```text
POST /api/messages/send
```

using the same protected Bearer configuration as text delivery.

Image payload:

```json
{"chatId":"<target>","image":{"data":"<base64>","mimeType":"image/png"}}
```

File payload:

```json
{"chatId":"<target>","file":{"data":"<base64>","filename":"report.pdf"}}
```

The upstream file payload has no MIME field. The Gateway still validates the Artifact's
declared MIME type and filename before sending it.

## Validation

- `media_type` must be exactly `image` or `file`.
- Callers may supply raw bytes or canonical RFC 4648 Base64.
- Base64 length is bounded before decode, strictly decoded, and re-encoded to reject
  non-canonical padding or pad bits.
- Decoded media is limited to 25 MiB by the adapter.
- Images are restricted to PNG, JPEG, and GIF; declared MIME must match the signature.
- Files require a cross-platform safe basename no longer than 255 UTF-8 bytes.
- Known filename extensions require the matching concrete MIME type; unknown extensions
  use `application/octet-stream` for opaque data.
- Validation and adapter errors do not include media bytes, response bodies, Bearer
  values, or target identifiers.

## Delivery semantics

The Delivery Worker reads a ready Artifact from durable storage and verifies:

- the Artifact exists and belongs to the current Response;
- status is `ready`;
- stored content passes integrity validation;
- the sender supports media delivery.

Image Artifacts are sent without a filename. File Artifacts include the persisted safe
filename. Every send is part of the durable Delivery attempt/receipt state machine.
Ambiguous channel effects become Delivery `uncertain`; they do not change Dispatch
success and do not call Hermes again.

See [Domain architecture](architecture.md#artifact-and-media-boundary) for the broader
capability boundary and [Production status](production-status.md#operational-limitations)
for the current acceptance limitation.
