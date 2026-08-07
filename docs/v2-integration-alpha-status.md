# V2 Integration Alpha Status

## Current Version

| Item | Value |
| --- | --- |
| Package version | `0.1.0` |
| Integration stage | V2 Integration Alpha |
| Branch | `feat/v2-integration` |
| Validated code baseline | `a8a2625` |
| Validation date | 2026-08-07 |
| Alembic head | `20260806_04` |

This alpha integrates the completed V2 foundations into one reviewed code line. It does
not yet provide the complete V2 runtime pipeline. The validated production path remains
the V1-compatible text round trip described below.

## Migration Status

The packaged Alembic tree has one root, one linear chain, and one head:

```text
20260806_01
  -> 20260806_0001
  -> 20260806_0002
  -> 20260806_02
  -> 20260806_03
  -> 20260806_04 (head)
```

| Revision | Scope |
| --- | --- |
| `20260806_01` | Migration foundation marker |
| `20260806_0001` | V1 main-schema baseline |
| `20260806_0002` | Message Archive schema |
| `20260806_02` | Agent Profile, Group Type, and conversation binding |
| `20260806_03` | Hermes dispatch record/outbox foundation |
| `20260806_04` | Artifact foundation |

The migration runner, packaged migration discovery, SQLite and PostgreSQL migration
locks, and SQLite foreign-key listener are active. SQLite online upgrades and PostgreSQL
offline DDL rendering are covered by tests. `Base.metadata.create_all()` is not the
application schema-evolution path.

## Completed Modules

### Runtime-connected capabilities

- WeChat polling, normalization, checkpointing, and self-message filtering.
- Message persistence before admission, with event and physical-message idempotency.
- Message Archive persistence for the first raw payload, direction, `occurred_at`, and
  first-received `received_at`.
- Identity and access-policy admission.
- V1-compatible Workspace/AIThread routing and source binding.
- Durable Hermes dispatch records with stable idempotency keys, one record per message,
  compare-and-swap claims, and `queued`, `running`, `success`, `failed`, and
  `uncertain` states.
- Inline Hermes dispatch through the persisted Message, Workspace, AIThread, and Hermes
  session binding.
- Hermes parsing for both the legacy completion response and V2 `ResponseEnvelope`.
- V1-compatible text response delivery to the bound WeChat conversation.

### Integrated foundations not yet connected end to end

- Agent Profile and Group Type persistence, immutable revisions, conversation binding,
  and `unknown_group` fallback.
- Thread Resolver V2 policies: `private_sender`, `group_shared`, and `group_sender`,
  including profile revision in the V2 thread key.
- Artifact model and repository lifecycle: create, lookup, list by response, mark ready,
  mark failed, expire, and read, with size, hash, path, symlink, and state validation.
- WeChat Media Adapter V2 for validated outbound image and file HTTP requests.
- Message delivery-attempt schema and constraints.

## Runtime Chain Audit

The requested V2 chain is not fully connected.

| Stage | Alpha status | Current behavior |
| --- | --- | --- |
| WeChat inbound | Connected | Polling and normalization feed the per-message admission sink. |
| Message Archive | Connected | Raw payload and archive timestamps/direction are persisted by `MessageStore`. |
| Profile Routing | Foundation only | `AgentProfileStore` and Group Type resolution are not called by active admission. |
| Thread Resolver V2 | Foundation only | Active admission still calls `WorkspaceService.ensure_thread_for_authorized_request` and the V1 `build_thread_key`. |
| Dispatch Record | Connected | The record is committed before an optional external Hermes call. |
| Hermes Client | Connected inline | The inline executor claims the record and calls the synchronous client. |
| Response Envelope | Parsed in memory | V2 parts are validated and carried in `HermesDispatchOutcome`, but no response record is persisted. |
| Artifact | Foundation only | No production service consumes `ArtifactRefPart` or invokes `ArtifactRepository`. |
| WeChat Delivery | Text only | `HermesResponseHandler` uses `send_text`; media delivery is not wired. |

The actual runtime path is:

```text
WeChat inbound
  -> Message Archive
  -> V1 Admission + WorkspaceService thread routing
  -> Hermes Dispatch Record
  -> inline HermesDispatchOutboxExecutor
  -> Hermes Client
  -> legacy text or ResponseEnvelope text projection
  -> WeChat send_text
```

For a mixed V2 response, only the text projection is delivered and artifact references
are ignored. An artifact-only response has empty `assistant_content` and is rejected by
the current text response handler.

## Validation Results

The following checks were run from `feat/v2-integration` at code baseline `a8a2625`:

| Check | Result |
| --- | --- |
| `python -m alembic -c alembic.ini heads` | Passed; `20260806_04 (head)` |
| `python -m alembic -c alembic.ini history --verbose` | Passed; one linear chain |
| `python -m ruff check .` | Passed |
| `python -m ruff format --check .` | Passed; 138 files already formatted |
| `git diff --check` | Passed |
| Full pytest | 605 passed, 2 skipped, 0 failed |

Full pytest used a repository-local `--basetemp` because
`C:\Users\Admin\AppData\Local\Temp\pytest-of-Admin` retained invalid Windows ACLs
after an interrupted run. The isolated directory was removed after validation; no
business-code workaround was introduced.

The two skips are platform capability checks:

- Directory symlinks are unavailable, so the Artifact repository directory-symlink
  escape test was skipped.
- File symlinks are unavailable, so the Artifact repository file-symlink replacement
  test was skipped.

The run emitted one existing Starlette/httpx deprecation warning from FastAPI's test
client.

## Known Alpha Limitations

- Profile Routing and Thread Resolver V2 are tested standalone but are not wired into
  active WeChat admission. The current runtime still uses the V1 compatibility thread
  path.
- The Dispatch Outbox is an inline executor, not a standalone durable worker. There is
  no queued-record scanner, per-`ai_thread_id` ordering, lease or heartbeat, stale
  running-record recovery, retry backoff, or automatic `uncertain` reconciliation.
- If Hermes is disabled or the process stops after enqueue, records can remain queued
  without a consumer. A stop after claim can leave a running record without recovery.
- The stable local idempotency key is not yet an upstream Hermes idempotency contract.
- Dispatch is marked successful before WeChat delivery. A delivery failure does not
  revert dispatch state, and duplicate inbound delivery does not call Hermes or WeChat
  again.
- `message_delivery_attempts` is schema/model foundation only; runtime delivery does
  not write attempts or schedule delivery retries.
- `ResponseEnvelope` has no durable response repository. Artifact references are not
  fetched, associated with a dispatch/message, or persisted automatically.
- Artifact V2 is repository/model foundation only. There is no Artifact ingestion job,
  delivery job, delivery worker, retry flow, or lifecycle reconciliation.
- `WechatHttpMediaSender` is not selected by the active runtime and
  `HermesResponseHandler` does not process image/file parts. Media delivery has not
  received live WeChat end-to-end validation.
- Inbound image/file content, OCR, archive or ZIP processing, Context Builder, provider
  routing, and Skill execution are outside this alpha.
- PostgreSQL coverage currently validates offline migration DDL; production online
  runtime validation remains a later environment gate.
- Worker service management, production deployment automation, Artifact storage
  volumes, operational reconciliation, and complete observability are not finished.

## Next Phase

1. Wire Agent Profile and Group Type resolution into admission, then invoke Thread
   Resolver V2 with the selected profile revision and thread policy. Define the reviewed
   V1 binding compatibility and data-migration boundary.
2. Implement a standalone dispatch worker with queued scanning, atomic claims,
   per-thread ordering, retry/backoff, stale-claim recovery, and explicit uncertain-state
   reconciliation. Propagate the stable idempotency key to Hermes.
3. Add durable response persistence and idempotent `ResponseEnvelope` ingestion.
   Resolve `ArtifactRefPart` values and persist their metadata/content through
   `ArtifactRepository`.
4. Add a delivery outbox/worker that processes response parts in order, writes delivery
   attempts, and retries delivery without calling Hermes again.
5. Wire `WechatHttpMediaSender` into runtime delivery and cover text-only,
   artifact-only, and mixed-part responses, failure recovery, and source-account
   isolation.
6. Add worker and Artifact-storage configuration, persistent volumes, service-manager
   or Compose orchestration, metrics, and reconciliation tooling.
7. Add one complete V2 runtime integration suite and validate PostgreSQL online
   migrations/runtime behavior in an available PostgreSQL environment.

This branch is ready for human alpha review. It has not been merged to `main`, pushed,
or promoted as a production-ready V2 pipeline.
