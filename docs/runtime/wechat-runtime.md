# WeChat Polling Runtime

## Purpose and current status

`wechat-worker` is the inbound WeChat process for CF_agent-gateway V2. It reads from
`agent-wechat`, maintains durable Checkpoints, stores normalized non-self messages, applies
Admission and V2 Routing, and creates a Hermes Dispatch Record only for an allowed request.
It does not call Hermes and does not send replies inline.

The 2026-08-13 CFserver validation proved login-aware polling, Token authentication,
Checkpoint creation, Message Store persistence, and unauthorized safe rejection. The
2026-08-14 follow-up live verified authorized private and explicitly mentioned group text
paths through Hermes and actual WeChat receipt, plus thread reuse; no reply echo was observed
to Dispatch again. See the [baseline record](../validation/2026-08-13-wechat-runtime.md) and
the
[follow-up record](../validation/2026-08-14-wechat-private-group-media-runtime.md).

## Startup and configuration

The process entry point is:

```console
python -m cf_agent_gateway.runtime.worker
```

The production runtime loads the path named by `CF_GATEWAY_CONFIG`. Its non-secret WeChat
settings have this shape:

```yaml
runtime:
  polling_interval_seconds: 3
  v2_routing_enabled: true

wechat:
  enabled: true
  base_url: http://cf-agent-wechat:6174
  bootstrap_mode: latest
  token_env: CF_AGENT_WECHAT_TOKEN
```

`token_env` names an environment variable; it is not the Token itself. The deployment
injects `<AGENT_WECHAT_TOKEN>` through its root-only environment file. Do not add a Token,
Token hash, Hermes API key, or database credential to this YAML.

Inside the production container network, use `http://cf-agent-wechat:6174`. A loopback URL
such as `http://127.0.0.1:6174` points back to the Gateway container and is not a valid
production address for `agent-wechat`.

## Poll cycle

The worker runs serialized cycles; cycles never overlap. One cycle performs:

1. `GET /api/status/auth` to determine login status and the active bot account.
2. If logged in, `GET /api/chats` to list visible conversations.
3. For each chat, `GET /api/messages/{chat_id}` to read the currently visible messages.
4. Validate that messages belong to the chat and order them by numeric `localId`.
5. Read or initialize the per-account/per-conversation Checkpoint.
6. For messages above the Checkpoint, filter self messages or normalize and pass the message
   to the persist-first ingestion sink.
7. Advance the Checkpoint only after that message's handling succeeds.

After a cycle completes, the worker waits `runtime.polling_interval_seconds` before starting
the next cycle. With the production value `3`, this means **three seconds after completion**,
not a fixed wall-clock poll every three seconds. Cycle execution time is additional.

If `agent-wechat` reports that no account is logged in, the cycle returns
`logged_in=false`, performs no chat polling, waits for the normal interval, and checks again.

## Checkpoint identity and ordering

A Checkpoint is keyed by both:

- `source_account_id`, for example `<BOT_WECHAT_ACCOUNT_ID>`
- `conversation_id`, for example `<PRIVATE_CONVERSATION_ID>`

It stores the largest source `localId` that was successfully handled or deliberately
accepted as a `latest` bootstrap baseline. Checkpoint advancement is monotonic. Messages at
or below the stored value are counted as visible but skipped. A normalization, persistence,
Admission execution, routing, dispatch enqueue, or Checkpoint error stops later messages in
that chat for the current cycle so source order is preserved. Other chats continue
independently.

## `bootstrap_mode=latest`

`latest` prevents a newly enabled worker from replaying all visible history:

1. When a chat has no Checkpoint, the worker validates and orders the visible raw messages.
2. It initializes the Checkpoint to the largest visible `localId`.
3. It returns without normalizing, storing, admitting, or dispatching any of that visible
   batch.
4. A later cycle processes only messages with a greater `localId`.

This behavior applies to every chat independently. It applies not only during the first
deployment poll, but also when a chat appears for the first time after the worker has been
running. Therefore, the first message that makes a previously unseen chat visible can be
part of the skipped baseline. Operators performing identity discovery should confirm that
the chat already has a Checkpoint before sending the unique discovery message.

An unseen chat with no visible messages initializes at `0`. Because its Checkpoint then
exists, its first later message is processed normally. `backfill` is supported by the code,
but it deliberately starts an unseen chat at `0` and replays its visible history; changing
to it in production requires an explicit review of volume, authorization, and reply risk.

The 2026-08-13 first production baseline saw 17 chats and 151 visible messages, created 17
Checkpoints, and processed zero messages. That is the expected `latest` result, not message
loss.

## Poll metrics

| Metric | Meaning |
| --- | --- |
| `chats_seen` | Number of chat entries returned by `agent-wechat` in this cycle. |
| `chats_failed` | Chat entries that could not complete parsing, message retrieval, validation, normalization, sink handling, or Checkpoint work. |
| `messages_seen` | Sum of raw messages returned for chats, including history below the Checkpoint, self messages, and messages in a chat that later fails. |
| `messages_skipped_by_checkpoint` | Raw messages whose `localId` is at or below the Checkpoint, including a `latest` bootstrap batch. |
| `messages_processed` | Non-self messages above the Checkpoint for which normalization, sink handling, and Checkpoint advancement all succeeded. |
| `bootstrapped_chats` | Chats whose Checkpoint was newly created during the cycle. |

`messages_processed` does not mean `Admission Allowed`, `Hermes called`, or `reply sent`.
An unauthorized message that was stored, safely denied, and checkpointed counts as processed.
A self message does not count as processed even though its Checkpoint advances.

## Self-message filtering

When a raw message has `isSelf=true`, the poller:

- does not normalize it;
- does not write it to Message Store;
- does not run Admission or V2 Routing;
- does not create a Hermes Dispatch Record;
- does advance the chat Checkpoint.

This filter is intentionally before Persist-first ingestion. It prevents an outbound bot
reply from being consumed as a new inbound request and prevents consecutive self messages
from being reconsidered on every cycle.

The rule is independent of raw message type. A self-originated text, reply, image, or file
advances its Checkpoint without entering Message Store, Admission, or Dispatch. The
2026-08-14 private and group text replies were observed upstream without being dispatched
again.

## Persist-first Admission

For each non-self message above the Checkpoint, the ingestion sink first commits the
normalized Message Store row. Admission starts only after that commit and uses a read-only
snapshot of the persisted message.

Admission then requires all applicable facts, including:

- a Source Identity Mapping for `<EMPLOYEE_WECHAT_SENDER_ID>` under the active bot account;
- an active Enterprise Identity;
- an active User Access Policy;
- an enabled Gateway Access Policy that allows the request risk level;
- an explicit bot mention for a group message;
- valid V2 Agent Profile and conversation routing bindings after access is allowed.

A Source Identity Mapping alone does not grant access. When identity or access is missing,
the Message Store row remains available for audit, but no Hermes Dispatch Record and no
Delivery Outbox record are created. This is a successful safe rejection, so the Checkpoint
advances. Adding a policy afterward does not automatically replay that denied message; send
a new test message after configuration is complete.

## Private and group message behavior

### Private messages

Private messages persist `is_mentioned=null`; they do not need a bot mention. On
2026-08-14, one configured private route passed both policy layers, selected its explicit
Conversation-AgentProfile Binding and `private_sender`, and completed the text path through
Response, Delivery, Receipt, and actual WeChat receipt. The first allowed message created
the Employee Workspace and private AI Thread; configuration alone had not created them.

After the four Gateway application processes were restarted in their existing containers,
a second context-dependent private request reused the same Workspace, AI Thread, Thread Key,
and Hermes Thread ID. This is live thread-reuse evidence, not a claim that
`reply_context` or a Context Snapshot supplied the prior context.

### Group messages and `is_mentioned`

Group messages persist an explicit boolean `is_mentioned`. Only a normalized literal
`true` satisfies Admission. Missing, false, or otherwise unusable values become
`is_mentioned=false`; Message Store does not infer a mention from message text, quoted
content, or manually typed `@` characters.

A live ordinary group message was persisted with `is_mentioned=false` and denied with
`bot_not_mentioned`, creating no Dispatch. After an active
`cf-authorized-group-sender` Group Type and Conversation-GroupType Binding selected
`group_sender`, a mention created with WeChat's member-selection function was stored as
`is_mentioned=true`, admitted, and completed the text reply path. It reused the employee
Workspace but used a group-specific AI Thread, Thread Key, and Hermes Thread ID. A second
mentioned request reused that group thread and received the correct context-dependent text.

### Reply messages

Raw reply messages can normalize to `message_type=reply`. Private and group examples were
live verified with non-empty `reply_context`, which retains a summary of quoted content.
That summary does not establish a stable Gateway message relationship, so
`reply_to_message_id` remains `null`.

The group reply did not include a structured mention; it remained
`is_mentioned=false` and created no Dispatch. The private reply passed Admission and reused
its `private_sender` thread. Its first Hermes attempt became `uncertain` while Hermes was
offline; after health restoration, backup, and strict evidence guards, a one-off manual
recovery allowed the same Dispatch to succeed on attempt two and deliver its text reply.
That was not automatic recovery. Current dispatch sends `message.content` only and does
not inject `reply_context` into the Hermes request.

### Image messages

A live non-mentioned group image was classified as `message_type=image` with
`raw_type=3`, persisted with source identifiers and Raw Payload, and created neither an
Attachment nor a Dispatch. A separate `agent-wechat` media API call returned 5,712 valid
JPEG bytes and a verified, unpublished SHA-256 digest.

The resident poller does not call that media endpoint. It does not create an Attachment,
store inbound media in Gateway private storage, create an Artifact, or construct a Hermes
multimodal request. Therefore this observation proves image discovery and independent
media-byte retrieval only; it does not mean that Hermes saw the image.

## At-least-once behavior and idempotency

The Checkpoint advances after sink success. If the message commit succeeds but the
Checkpoint write fails, the source message is delivered to the sink again in a later cycle.
Message Store deduplicates by stable event and physical source-message identity. If an
allowed message already has a Dispatch Record, ingestion reuses its stable target instead
of enqueuing another Hermes call.

This is an at-least-once boundary with idempotent durable writes. It is not a claim that the
source or provider performs exactly-once delivery. See
[V2 Runtime Architecture](../architecture/v2-runtime.md) for the dispatch and Delivery
Outbox guarantees.

## Errors and retries

Expected polling failures are isolated and represented in the cycle result:

- Authentication status or chat-list failure ends the cycle; the next normal cycle retries.
- Message-list, validation, normalization, sink, or Checkpoint failure fails that chat for
  the cycle. Later messages in that chat wait, while other chats continue.
- Because the Checkpoint remains before the failed message, the next cycle retries it.
- Duplicate entries for a chat do not retry that failed chat again in the same cycle.

An unexpected exception escaping the finite poll is logged as `wechat_poll_execution_failed`;
the resident worker waits and retries another cycle. Invalid configuration, a disabled
WeChat runtime, or a missing Token environment variable is fatal and exits the process so
the deployment can surface a failed service instead of silently polling without credentials.

The worker's process heartbeat indicates liveness, not that every chat succeeded. A cycle
that returns a structured result can keep a healthy heartbeat while reporting
`chats_failed > 0`. Monitoring and incident review must inspect `chats_failed` and structured
poll failures in addition to service health.

## Heartbeat behavior

When `CF_GATEWAY_WORKER_HEARTBEAT_PATH` is configured, the process writes an atomic JSON
heartbeat. Default timing is:

- publish interval: 10 seconds;
- maximum healthy age: 30 seconds;
- healthy states: `starting` and `running`.

The worker publishes cycle phase and sequence information when polling begins and when it
enters the interval wait. It renews the heartbeat in bounded slices during that wait. It
does not renew while blocked inside the synchronous poll itself, so a stuck HTTP or database
operation eventually makes the container unhealthy. This is intentional failure detection.

## Safe shutdown and restart

`SIGINT` and `SIGTERM` set the worker's stop event. The interval wait ends immediately, but
an active synchronous poll is allowed to finish before process exit. A safe operational
stop is therefore:

```console
docker compose -f <COMPOSE_FILE> stop wechat-worker
```

Allow a shutdown grace period longer than the configured HTTP timeouts and normal database
work. Do not force-kill the process merely because it is finishing a cycle. If termination
occurs after sink commit but before Checkpoint commit, the next start redelivers that message
and the idempotency rules above apply.

Before restarting, verify that:

- the production configuration still uses `http://cf-agent-wechat:6174`;
- `<AGENT_WECHAT_TOKEN>` is available only through the configured environment variable;
- PostgreSQL is ready and at migration `20260810_01`;
- the existing Checkpoints are present unless a reviewed bootstrap reset is intended;
- worker logs and heartbeat health are monitored after startup.

On 2026-08-14, only the `gateway`, `wechat-worker`, `dispatch-worker`, and
`delivery-worker` processes were restarted in their existing containers. Their container
IDs remained unchanged and all four returned healthy. PostgreSQL, `agent-wechat`, Hermes,
and the CFserver host were not restarted. Persisted private route and delivery facts remained
unchanged, and the next private request reused its existing thread. This does not validate
container recreation, PostgreSQL restart, or host reboot.

## Related documentation

- [V2 Runtime Architecture](../architecture/v2-runtime.md)
- [Identity, access, and V2 routing](../security/identity-access-routing.md)
- [CFserver production deployment](../deployment/cfserver-production.md)
- [Runtime validation index](../validation/README.md)
- [2026-08-14 private, group, reply, and media validation](../validation/2026-08-14-wechat-private-group-media-runtime.md)
- [2026-08-13 baseline and unauthorized-path validation](../validation/2026-08-13-wechat-runtime.md)
- [WeChat Media Adapter V2](../wechat-media-adapter-v2.md)
