# Enterprise product ZIP task integration

This candidate depends on PR20 (`ef3119aeecd987165255c066218441f6aef8fb0a`). It adds bounded ZIP ingress and generated-output return; it does not change a past Dispatch, checkpoint, model configuration or an original-file continuation's authority. The inherited migration target is `20261018_01`; this change adds no further schema migration.

## References and authority

The three kinds of references remain distinct:

1. An inbound attachment ID/handle identifies a current authorized work copy. FileBridge may ingest it through its trusted host adapter; it is not a file-service path.
2. Enterprise `source`/`path` identifies an approved resource. The product tool operates within its separately configured employee, account, private chat, thread and Profile binding.
3. Product export produces a server-verified ZIP descriptor. Trusted FileBridge code calls the return plugin during the official product-tool middleware scope and receives an opaque, current-task `file_ref`. The model passes that reference to `cf_return_current_chat`.

Only `filebrowser_product_materials` may prepare enterprise returns. Both it and `filebrowser_ingest_product_zip` may obtain trusted task context. The helpers are absent from model schemas. They require the current Dispatch, Message, identity, thread, Profile revision, session, account and private chat; revoked or closed contexts fail. PR20 `read-return` authority cannot be upgraded to a generated ZIP.

The new authenticated `POST /internal/hermes/returns/{dispatch}/artifacts/{slot}/enterprise-import` accepts a descriptor, never an arbitrary URL or credential. Gateway streams the fixed FileBrowser origin's `/api/resources/download?source=…&file=…`, checks TLS, outer bytes/hash, each actual ZIP member and current authorization, then stores an ordinary Artifact. The existing completion ACK, response seal, Response and Delivery mechanisms remain authoritative. An ambiguous request queries its same slot; conflicting output cannot silently replace it.

The export descriptor is exactly `source`, `path`, `name`, `mime_type`, `size_bytes`, `sha256`, `manifest`. Each manifest entry is exactly `path`, `size_bytes`, `sha256`. MIME is `application/zip`; bytes and archive structure, rather than server MIME alone, determine acceptance. The generated ZIP travels FileBrowser → Gateway, not through the AI host.

## Opt-in deployment configuration

Gateway requires a new fixed image and the complete return-plugin bundle, including new `enterprise_files.py` and `zip_contract.py`. FileBridge's coordinated candidate provides product tools, native client and opt-in server API. Deploying only one side is insufficient. Preserve the current Hermes `427d605d4985c2d49a795493ef6c5d66413057d1` and model/provider/options.

`enterprise_exports` is disabled by default. Enabling requires:

- `base_url`: verified FileBrowser HTTPS origin, with `ca_file` where needed. Plain HTTP is accepted only for loopback fixtures.
- `token_env`: separate scoped read token environment-variable name, default `CF_GATEWAY_ENTERPRISE_EXPORT_TOKEN`. No Gateway, Hermes, WeChat or signing credential is reused.
- `source`, `root`: exact approved source and canonical directory prefix.
- `download_scope_prefix`: optionally the same exact `root` when the separate read-only FileBrowser account is scoped there. Only that verified prefix is stripped from the API request; authorization and the signed export descriptor retain the producer path. The default empty prefix preserves a reader already scoped at the source root.
- `allowed_bindings`: exact `[account, private_chat, enterprise_identity]` rows.
- `max_bytes`: at most 134217728; `timeout_seconds`: 1–300, default 120.

Capacity configuration must be coordinated: `api.max_request_body_bytes`, `artifact_return.max_bytes`, `enterprise_exports.max_bytes`, `inbound_media.max_bytes`, and `wechat.max_media_bytes`. The candidate allows 128 MiB but preserves existing defaults (1 MiB return/API, 10 MiB ingress, 25 MiB sender). Generated output is rejected before download/Artifact creation when it exceeds the minimum of enterprise, return and current WeChat sender limits; the tool reports terminal `return_too_large`. The FileBridge and FileBrowser limits must match. A protocol receiving base64 JSON additionally needs bounded request-body capacity for approximately 4/3 expansion plus its envelope; 128 MiB binary is not 128 MiB JSON.

ZIP verification never extracts or executes members. Limits are 128 MiB archive, 32 MiB per member, 512 MiB expanded, 2,000 members and bounded compression ratio. It rejects traversal, absolute/drive paths, links/special files, controls, encrypted/unsupported compression, case-colliding names, parent-file conflicts and changed CRC/size. Chinese names, spaces and parentheses are retained. FileBrowser is responsible for staged extraction, classification, conflict-safe publication and recovery.

PR20's explicit old-attachment continuation remains PDF/PNG-only in this candidate. ZIP ingestion and product export do not imply that arbitrary natural-language references or historical ZIP continuation have been added.

## Explicit controlled input for a real model

`python -m cf_agent_gateway.controlled_test --config CONFIG --request REQUEST` is an operator CLI, not a model tool or public HTTP endpoint. It records source `cf-controlled-test`, origin `operator-cli` and `capture-only`; it cannot claim a WeChat employee actually sent the request.

The request has exact keys `run_id`, `account`, `chat`, `sender`, `identity`, `profile_id`, `content`. The protected config must allow the exact tuple in `artifact_return.controlled_test_bindings`. The CLI revalidates the existing active WeChat identity/private-chat/Profile mapping and access policy, then uses the formal identity/conversation/admission stores to create a separate controlled-source thread. Existing WeChat history stays intact and is not represented as inherited. Subsequent controlled messages use that same controlled thread.

Use an independent official Hermes Profile/HOME and authenticated endpoint with only the toolsets `cf_filebridge_products` and `cf_artifact_return`. The CLI checks actual `/v1/toolsets` inventory and rejects any extra model-visible tool, including Skill/code/terminal tools. It requires the existing explicit host runtime contract: fixed provider/model/options, matching Profile reference/revision, dedicated endpoint and verified empty fallback. Runtime values are frozen in the new message proof and supplied on every model request; drift or expired proof rejects before claim.

`--prepare-only` records the new normal Message/Admission/Dispatch and prints its thread ID without calling the model. This permits the operator to configure the product plugin's exact new thread binding before execution. A repeated identical `run_id` is idempotent; a changed input, already attempted or dead task is not retried. The ordinary live worker excludes this source; the CLI claims only its exact new queued task. The proof expires within 12 hours. This is separate from SSH/sudo authorization.

The worker uses the normal current-task capability, heartbeat, Artifact, ACK and response persistence, but captures Response parts without enqueueing WeChat Delivery. A real-model controlled test therefore proves neither real WeChat ingress nor receipt. Actual controlled Artifact bytes can be inspected through the existing authorized storage/receipt path. A successful result can be captured again after a process restart without another model call. A separate-process regression also submits a new controlled message after reopening the persisted database and verifies that it keeps the controlled thread and Hermes session while producing no Delivery row.

## Deployment and recovery boundary

Before stopping services, verify current image identities, formal Compose and actual mounts, backups and schema. Keep Poll/Delivery closed. Use a new plan and journal, migrate from the recorded live schema to `20261018_01`, update fixed images/bundles and narrowly scoped config, then verify actual plugin loading and tool inventory. Keep original credentials, unrelated config, old request/plan/journal and historical tasks.

Do not deploy from this document alone: the parent task records the exact built image, coordinated FileBrowser/client SHA, checks and approved plan. If new continuation or controlled records exist, do not downgrade schema, delete them or let an older worker unknowingly take them over. Stop new admission and preserve safe service state while selecting a verified recovery path. The protocol filename candidate is a separate component and must be matched to its actual binary; Gateway preserving a name does not prove the receiving client uses that name.
