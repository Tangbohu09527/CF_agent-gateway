# Domain architecture

## Scope

CF_agent-gateway is a durable message and control plane. It connects an external channel
entry service to an external execution service while retaining message, authorization,
routing, dispatch, response, delivery, and recovery authority in PostgreSQL.

[Production status](production-status.md) records what has been validated in production.
[Runtime architecture](runtime-architecture.md) describes process and lifecycle behavior.

The terms used in this document have distinct evidence meanings:

- **Implemented** means present in repository code and schema.
- **Automated-test covered** means exercised by repository tests.
- **GitHub Actions verified** means the relevant workflow completed successfully for a
  specific commit.
- **Production validated** means exercised on CFserver and recorded in the production
  acceptance evidence.
- **External responsibility** means owned outside this repository.

## System boundary

```text
WeChat
  <-> external agent-wechat
  <-> CF_agent-gateway
  <-> external Hermes
```

The Gateway owns neither the WeChat login implementation nor Hermes execution. It owns
the durable facts that decide whether a message may be dispatched, which AI Thread is
used, whether an external effect is safe to retry, and whether a persisted response has
been delivered.

## Core entities

| Entity | Purpose |
| --- | --- |
| Conversation | Account-scoped channel conversation keyed by source, source account, and conversation ID. |
| Message | Immutable first-seen normalized message facts with event and physical-source idempotency. |
| Message raw payload | Canonical first-seen upstream JSON retained separately from the normalized Message. |
| Attachment metadata | Filename/type/MIME/size/path/hash metadata accepted by the Message API; not a content-upload API. |
| Identity and source mapping | Enterprise identity and the channel sender identity that resolves to it. |
| Access policy | Persisted user and Gateway authorization facts evaluated for each human message. |
| Agent Profile and Group Type | Persisted execution profile selection and group Thread policy. |
| Employee Workspace | Workspace owned by one authorized enterprise identity. |
| AI Thread | Versioned routing target with a profile revision and Thread policy snapshot. |
| Admission Outcome | One durable authority per Message for pending, allowed, denied, or unresolved admission. |
| Hermes Dispatch | Durable FIFO work record with stable idempotency key, claim, lease, attempts, and terminal state. |
| Dispatch recovery audit | Immutable record of an authenticated manual resolution of an ambiguous Dispatch. |
| Hermes response | Claim-fenced raw execution result associated with one Dispatch. |
| Response and parts | Normalized ordered text or Artifact-reference response. |
| Artifact | Durable response-owned image/file content with integrity metadata. |
| Delivery Outbox | Ordered channel delivery work with attempts, receipts, and terminal state. |
| WeChat Checkpoint | Per-account/per-conversation cursor, regression generation, and continuity anchor. |
| Context Snapshot | Append-only derived summary over a bounded Dispatch-ID interval. |

## Permission and routing model

Sender identity determines permission; a conversation does not grant permission.

1. A human Message must have a channel sender ID.
2. The source identity mapping resolves that sender to an enterprise Identity.
3. User and Gateway access policies are evaluated for the resolved Identity.
4. A private conversation uses the `private_sender` Thread policy.
5. A group Message must have an explicit structured mention before it is eligible.
6. The configured Group Type selects either `group_sender` or `group_shared`.
7. `group_sender` isolates authorized group members into different AI Threads.
8. `group_shared` deliberately reuses one AI Thread across authorized members.
9. The selected Agent Profile identity and revision are persisted with the route.

The production configuration enables V2 routing. The historical Alpha behavior that bound
all group senders to a conversation-scoped V1 Thread is not the current V2 behavior.

System Messages may be persisted without a sender identity but are not silently converted
into an authorized human sender. Self-originated WeChat Messages are filtered by the Poll
Worker before Message persistence and only advance the Checkpoint.

## Durable message flow

### Admission

1. The Poll Worker normalizes a channel Message after Checkpoint and self-message checks.
2. Message persistence commits before admission. Duplicate `event_id` or duplicate
   `(source, source_account_id, conversation_id, source_message_id)` resolves to the
   existing Message without overwriting it.
3. The Admission Outcome is claimed with a fenced lease. Completed outcomes replay their
   stored result rather than evaluating today's policy again.
4. A denied or unresolved outcome creates no Dispatch.
5. An allowed outcome persists its Identity, Workspace, AI Thread, Agent Profile, policy,
   and routing evidence.
6. The allowed outcome and its first Hermes Dispatch commit atomically.

### Dispatch and response

1. The Dispatch Worker selects an eligible AI Thread head in `(created_at, id)` order.
2. A compare-and-swap claim rechecks FIFO eligibility, thread idleness, retry budget, and
   the unique running-record invariant.
3. The worker reads the archived Message and route snapshot; it does not rewrite Message
   Archive.
4. Hermes receives the stable Dispatch idempotency key and persisted Thread/profile facts.
5. A definite retryable failure may requeue within budget. A possible external effect with
   an unknown result becomes `uncertain` and blocks later work on the same AI Thread.
6. A successful external result and the Hermes response are persisted under the active
   claim token before normalized Response and Delivery work are created.

### Delivery

1. Response reconciliation creates any missing normalized Response or Delivery record
   from already-persisted successful Dispatch evidence; it never calls Hermes.
2. The Delivery Worker claims one outbox record and sends parts in ordinal order.
3. Text uses the channel text sender. Ready response Artifacts use the outbound image/file
   media adapter.
4. Every part attempt and accepted receipt is durable.
5. Delivery failure never changes Dispatch success and never triggers another Hermes call.

## Context boundary

The Context runtime reads authorized, persisted Timeline facts by Dispatch ID. A Context
Snapshot is an append-only summary supplied by its caller over a complete, exclusive
`covered_until` cursor. Snapshot creation does not summarize automatically and does not
modify Message, Dispatch, Response, Artifact, or Delivery facts.

There is no embedding store, vector search, RAG pipeline, enterprise knowledge base, or
automatic Memory policy in this repository.

## Artifact and media boundary

The current response path can persist response-owned Artifacts and automated tests cover
ordered outbound image/file delivery through `agent-wechat`. That does not establish
general inbound attachment processing or real production media acceptance.

The active inbound polling-to-Hermes path does not provide general image understanding,
file interpretation, OCR, archive/ZIP processing, or arbitrary upload handling. The
Message API's attachment list stores metadata supplied by an authenticated caller; it does
not upload or fetch the referenced bytes.

## Non-goals and external ownership

The following are not current Gateway implementations:

- Hermes itself or AI inference;
- general AI Provider selection/registry routing;
- automatic Skill execution;
- ERP business behavior or automation;
- enterprise knowledge retrieval or RAG;
- general inbound image/file/archive understanding;
- `agent-wechat` login/session implementation;
- PostgreSQL service lifecycle, backup storage, TLS, or network policy;
- cross-repository automatic deployment.

These boundaries must not be inferred from reserved package names or future-facing model
fields.
