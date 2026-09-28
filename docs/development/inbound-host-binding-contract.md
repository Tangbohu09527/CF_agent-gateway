# Gateway ↔ Hermes host binding v1

Status: implemented, **disabled by default**. This is the Gateway side of
`cf-inbound-host-binding/v1`, for the pinned official Hermes commit
`4d55ca91656ac5f83e1506679b7f81e0238e5e16`. Companion requirements were read only
from FileBrowser commit `2b3894c1ae3146a5f6c29084068345f70e6892d9` on GitHub.
No FileBrowser local checkout or deployed plugin was changed. The integration
peer in Gateway tests is a **synthetic host**, not the FileBrowser plugin.

## Compatibility corrections and enablement gates

See [the real API probe](inbound-host-session-compatibility.md). A claim gets one
persisted, unique child session ID before any create/fork request. An existing
thread must fork its exact confirmed tip and retain history; a missing, ended,
unexpected tip or unverified history fails closed. A new thread can create its
first session. A deterministic placeholder is not evidence of existing history.

Fork preserves messages and system prompt but loses part of the runtime lock.
The safe session API projection cannot prove an arbitrary historical provider or
options value. Enabling this path therefore requires an approved execution
Profile's explicit model/provider/options and evidence that legacy history used
that configuration (`legacy_runtime_confirmed`). This flag is an operator's
deployment attestation, not an automatic proof of the hidden historical lock.
After fork, Gateway explicitly
persists the runtime lock, verifies its acknowledgment, and passes the approved
runtime values to the OpenAI request. `/model` alone is insufficient: the pinned
OpenAI route otherwise uses global runtime. The session `/chat` alternative does
not propagate model failure reliably and is not used. Unsupported provider
options must be rejected during deployment validation, not assumed effective.

Create/fork intent, preparation-start flag, expected history digest and runtime
digest persist before their corresponding side effects. A retry can inspect the
**same child ID**; it must never issue another random ID or repeat an uncertain
create/fork. Missing evidence stays UNCERTAIN for explicit recovery. Once a child
may exist, any preparation/chat ambiguity remains UNCERTAIN and blocks ordinary
FIFO recovery. The host close/lease barrier never decides business success.
The AIThread tip advances only with the original fenced result transaction.
Ordinary text keeps its existing session behavior.

The current v1 implementation rejects a changed/compressed tip during preparation
or after the attachment chat; it does not automatically adopt a different session
for an already prebound claim. The official old-header/live-tip probe proves why
the response header alone is insufficient. A compression fixture is created using
the official SessionDB publication primitive; the probe does not claim to exercise
automatic LLM summarization. UNCERTAIN recovery is not an automatic requeue or a
claim that a partially created fork can always be repaired.

Before enabling, an operator must separately verify:

1. A dedicated authenticated Hermes execution endpoint and approved Profile are
   restricted to Gateway callers. A shared general Hermes API key permits session
   spoofing; session/task strings alone cannot establish source authenticity.
2. The host service token is a new random secret of at least 32 characters, scoped
   to the configured `host_id` and exact `profile_reference`/`profile_revision`.
   It is not a Gateway API/admin key, Hermes execution key or FileBrowser runtime
   token. Configure only environment-variable names in YAML.
3. Gateway HTTPS is reachable from the AI host with verified certificates. Do not
   disable TLS verification or put Authorization in URLs. Loopback HTTP in tests
   is only an isolated synthetic fixture.
4. The actual FileBrowser host adapter consumes this protocol, establishes events
   before activation, uses its existing HostBridge/worker downloader, saves to an
   authorized task work directory, and verifies size/SHA-256. Receiving an ID,
   descriptor or filename does not prove a work copy exists.

## Prebinding and secret lifecycle

The Gateway creates `inbound_host_bindings` in migration `20260928_01`, with no
historical backfill or dead-task redispatch. One intent is unique per actual
Dispatch claim; its session ID is globally unique. The stored claim epoch is
opaque and is not the claim token. Source identity, Profile revision, thread,
ready media job and Attachment registration are checked before grant issuance
and again during resolve/read. Concurrent first resolution fixes the authenticated
host, process instance, nonce and actual session/task atomically under the same
Dispatch write fence used for revocation.

Grant material is encrypted with AES-256-GCM, a random 96-bit nonce and binding ID
as associated data. `encryption_key_env` supplies a base64-encoded 32-byte key from
the service secret store; it must not be saved beside the database. SQL parameter
logging is hidden. Logs, model messages, metadata, events and ordinary status
responses contain no descriptor or authorization value. Full grant plaintext
exists only in process memory and the authenticated resolve response; responses
are `Cache-Control: no-store`. Disable body capture in reverse proxies/APM.

This prevents **new** handoff credentials entering model history. Preserved old
sessions may contain descriptors written by the previous implementation; this
task does not edit historical conversations or claim they have been cleansed.
When host binding is enabled, the actual HTTP reader also rejects an otherwise
valid legacy capability that has no prebinding/events. While the feature is
disabled the existing claim-scoped read contract remains compatible, but the
new dispatch path does not issue prompt-delivered grants as a fallback.

Repeated resolve decrypts the **same grant**, preserving its token, budget scope
and original deadline. It does not call `grant_read` again. Missing ciphertext or
the wrong key fails closed; it never creates a replacement. Revocation/closed or
expiry removes ciphertext and the read-token hash. A periodic Gateway reaper also
cleans expired or invalid claims. Grant expiry is at most the existing 3660-second
capability limit; effective use is further bounded by the much shorter host lease
and the current claim. If every process is offline, expiry still denies access on
restart and cleanup runs then. Database WAL/backups may retain encrypted historical
pages: protect backups and retire old encryption keys under the secret-retention
policy; logical deletion does not claim to physically erase storage blocks.

## HTTP contract

All requests below use `Authorization: Bearer <host service token>` over verified
TLS. Malformed/unauthorized contexts do not receive binding details. Feature off
returns 404; authorization failures return opaque 403. JSON models forbid extra
fields. Service identity is taken from configuration/authentication, never from a
body-supplied host or Profile.

`POST /internal/hermes/inbound-bindings/resolve`:

```json
{
  "schema": "cf-inbound-host-binding/v1",
  "session_id": "cfgw-unique-persisted-child",
  "task_id": "cfgw-unique-persisted-child",
  "host_instance_id": "unique-process-boot-id",
  "host_nonce": "random-per-binding-at-least-32-characters"
}
```

The pinned execution entry requires actual `task_id == session_id`. Host middleware
must obtain both from trusted execution context, not model tool arguments. Nonce
and instance use `[A-Za-z0-9_-]`, at most 128 characters. A restart cannot take over
an already owned binding under a different instance/nonce. Wrong/old/unknown
session, new claim, inactive identity/Profile and unregistered media are rejected.

A successful response contains schema, binding/session/task/instance/nonce,
Dispatch/message/thread/identity, opaque `claim_epoch`, exact Profile,
`lease_valid_until`, `state: running`, `event_sequence`, stable
`budget_scope: message:<id>:attachment:<id>`, and `attachments` containing the
existing `cf-inbound-read/v1` descriptors. This response is private background
handoff; **never append it to model history**.

`GET /internal/hermes/inbound-bindings/{binding_id}/events` requires the service
Authorization plus these headers (none in query strings):

| Header | Value from resolve/trusted context |
| --- | --- |
| `X-CF-Session-Id` | exact session ID |
| `X-CF-Task-Id` | exact task ID |
| `X-CF-Host-Instance-Id` | owner process instance |
| `X-CF-Host-Nonce` | owner nonce |
| `X-CF-Claim-Epoch` | returned opaque epoch |
| `Last-Event-ID` | optional current event sequence; a gap fails closed |

There is one event connection per binding. The host must observe the first
`event: binding` running snapshot before activating HostBridge. Payloads contain
schema, binding ID, epoch, host instance/nonce, sequence, state, lease deadline and
a bounded reason;
no grant. Repeated equal-sequence snapshots are keepalives, not permission renewal.
State transitions increase sequence. Unexpected ordering, loss or disconnect
requires immediate local worker closure. The Gateway revokes on stream disconnect
or authenticated sequence gap. Reconnection cannot restore a revoked scope.

`POST /internal/hermes/inbound-bindings/{binding_id}/closed` uses the same JSON
context as resolve plus `claim_epoch`. Only that owner may acknowledge; duplicates
return the same closed result, including after Dispatch expiry. Host-initiated
close also revokes. No ACK may close another binding.

## Revocation, completion and retry bounds

v1 grants a **fixed, nonrenewable host lease of at most 30 seconds**, further
clamped to the real Dispatch lease. Duplicate resolve, events and restarts do not
extend it. Expiry may close download/tool permission while a long model call is
still running; this is deliberate and must be visible to the host. The descriptor
expiry must never be used instead of this shorter lease.

Normal completion, exception, failed HTTP-200 model response, cancellation/shutdown,
lease-renewal failure, identity/claim change, host exit/disconnect or event gap
revoke grants. New reads fail immediately. In-flight host work must be cancelled
and joined locally before `closed`. Gateway completion CAS and next-thread claim
wait for closed or the finite lease barrier; an absent ACK cannot block forever.
Permission closure does not prove a remote Hermes model has stopped computing.
The existing UNCERTAIN/manual-recovery FIFO semantics remain intact.

The existing download contract still permits only GET with Authorization header,
at most **4 attempts within 30 seconds**, retrying only 503 with Retry-After 1;
the host must additionally stop before its lease deadline. 403, corrupted data,
size/SHA mismatch, registration conflict or expired dispatch fail closed. A retry
does not reset either budget. Original-image quality remains the recorded
`declared_quality`/`original_comparison`; JPEG bytes alone do not prove original.
Formal archival and processed outputs continue through FileBrowser API as new
files, retaining originals and the READY Artifact/Delivery boundary.

## Consumer fixtures and acceptance

The public [JSON consumer fixture](../../tests/fixtures/inbound-host-binding-v1.json)
includes complete resolve request/response, event request headers and running/revoked
SSE snapshots, and closed request/response. Every credential is a fixed synthetic
example; its fixed 2026-09-28 timeline grants no live permission. Consume actual
Gateway values in memory, never substitute these examples into a live deployment.
[The fixture test](../../tests/test_host_binding_fixture.py) checks request schemas
and compares the complete field/type shape and security invariants with actual
Gateway authenticated HTTP, SQL-backed claims, loopback SSE and duplicate closed.

`tests/test_host_binding_http.py` exercises the actual Gateway app, SQLite claim
transactions and loopback SSE. `tests/test_host_binding_worker.py` exercises actual
worker completion/renewal/shutdown and FIFO barriers. `tests/hermes_host_probe/`
runs the pinned official Hermes API and middleware against a synthetic model.
No test result claims the deployed FileBrowser plugin is connected.

For isolated cross-project acceptance: create fresh synthetic identity/Profile
and attachment data; run the real Gateway worker against pinned dedicated Hermes;
have the plugin resolve from trusted context, establish events, then activate its
existing downloader. Verify actual task-directory bytes/size/hash, revoke while
downloading, lose events/ACK, restart either service, and verify old nonce/session
replay cannot read or refresh the budget. Repeat text/media/text FIFO and failed
HTTP-200 responses, checking tip/result transactions and absence of secrets in
model history and logs. Only then approve enablement; this development task does
not perform production acceptance or deployment.

The separate upstream PDF continuously-pending fault remains unresolved. This
protocol neither supplies missing upstream bytes nor proves original images.
