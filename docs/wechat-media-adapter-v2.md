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

## Validation

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

The current V2 delivery worker uses this adapter for ordered image and file response parts;
that is an implemented code capability. Live WeChat media delivery has not been validated
on the current CFserver production deployment, so this document does not claim a verified
media round trip.
