# WeChat Media Adapter V2

## Boundary

`WechatMediaSender.send_media` is the unified Gateway boundary for outbound `image` and
`file` messages. `WechatHttpMediaSender` implements that boundary and retains the inherited
text sender without changing `WechatMessageSender` or its `send_text` contract.

Both media types call `POST /api/messages/send` with the same authorization-header
credential configuration as text delivery. The V2 wire payloads are:

```json
{"chatId":"...","image":{"data":"<base64>","mimeType":"image/png"}}
```

```json
{"chatId":"...","file":{"data":"<base64>","filename":"report.pdf"}}
```

The upstream file schema does not accept a MIME field. Gateway still requires and validates
the declared file MIME type before sending it.

## Inbound read boundary

`AgentWechatClient.get_media` can call
`GET /api/messages/{chat_id}/media/{local_id}`, recognize an upstream unsupported result,
and strictly Base64-decode supported media bytes. On 2026-08-14, an independent call for one
non-sensitive group image returned `supported=true`, `media_type=image`, `format=jpeg`,
a filename, and 5,712 bytes. The JPEG signature and SHA-256 digest were verified without
publishing the image or digest.

The resident WeChat poller does not call `get_media`. Its normalized inbound event currently
contains no Attachments, so an image or file message does not create an Attachment row,
write bytes to Gateway private storage, or create an Artifact. Hermes dispatch sends the
stored message content; it does not construct a multimodal request from the upstream media.

## Outbound adapter validation

- `media_type` is exactly `image` or `file`.
- Callers may provide raw bytes or canonical RFC 4648 Base64. Base64 is length-checked before
  decode, strictly decoded, and re-encoded to reject non-canonical padding and pad bits.
- Decoded media is limited to 25 MiB. Its Base64 representation remains below the upstream
  50 MiB request-body limit with JSON overhead.
- Images are restricted to PNG, JPEG, and GIF. The declared MIME type must match the content
  signature.
- Files require a cross-platform safe basename of at most 255 UTF-8 bytes. Known filename
  extensions require their concrete MIME type; unknown extensions use
  `application/octet-stream` for opaque data.
- Validation and adapter errors never include media data, response bodies, or authorization
  credentials.

## Integration status

The V2 delivery worker can call this adapter for an image or file response part only when a
persisted `ArtifactRefPart` already resolves to a readable Artifact. That is an implemented
and code-tested conditional outbound capability. The current inbound path does not create
such an Artifact, and the normal Hermes response path does not automatically materialize one.

Live WeChat image/file delivery and a complete media round trip have not been validated on
the current CFserver production deployment. In particular, the 2026-08-14 media API byte
retrieval does not mean that Hermes saw the image or that Gateway can return a generated
image to WeChat. See the
[dated validation boundary](validation/2026-08-14-wechat-private-group-media-runtime.md).
