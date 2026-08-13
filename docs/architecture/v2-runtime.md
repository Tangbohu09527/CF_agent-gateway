# V2 WeChat Runtime Architecture

## Scope and status

The V2 WeChat runtime is the production message path in CF_agent-gateway at commit
`2ac4c86` (`v2-enterprise-runtime-20260811`). The code implements durable polling,
persist-first admission, V2 routing, Hermes dispatch, response persistence, and
asynchronous delivery. On 2026-08-13, the five-service CFserver deployment and the
unauthorized-message safety path were verified on a real host.

This status does **not** mean that an authorized message has completed the entire path in
production. The Enterprise Identity, access policies, Agent Profile, private-conversation
binding, V2 route, Hermes response, Delivery Outbox, and received WeChat reply still need
one end-to-end authorized validation. See
[the validation record](../validation/2026-08-13-wechat-runtime.md).

## Runtime flow

```mermaid
flowchart LR
    employee["Employee WeChat"]
    agentIn["agent-wechat<br/>inbound HTTP API"]

    subgraph gateway["CF_agent-gateway V2 runtime"]
        poller["wechat-worker"]
        messageStore["Message Store"]
        admission["Admission"]
        routing["V2 Routing"]
        dispatchRecord["Hermes Dispatch Record"]
        dispatcher["dispatch-worker"]
        response["Response Persistence"]
        deliveryOutbox["Delivery Outbox"]
        delivery["delivery-worker"]
    end

    hermes["Hermes"]
    agentOut["agent-wechat<br/>outbound HTTP API"]
    reply["WeChat reply"]
    postgres[("PostgreSQL")]

    employee --> agentIn --> poller --> messageStore --> admission --> routing
    routing --> dispatchRecord --> dispatcher --> hermes
    hermes --> response --> deliveryOutbox --> delivery --> agentOut --> reply

    poller -. "checkpoint and message state" .-> postgres
    dispatchRecord -. "queue, claim, lease, result" .-> postgres
    response -. "response parts" .-> postgres
    deliveryOutbox -. "attempts and receipts" .-> postgres
```

`agent-wechat` and Hermes are external boundaries. The Gateway reaches `agent-wechat`
through its container DNS URL, `http://cf-agent-wechat:6174`, and reaches Hermes through a
deployment-specific URL such as `http://<AI_HOST_LAN_IP>:<HERMES_PORT>`. Tokens and API
keys are supplied through environment variables; they are not stored in YAML.

## Worker responsibilities

| Process | Entry point | Responsibility |
| --- | --- | --- |
| `wechat-worker` | `python -m cf_agent_gateway.runtime.worker` | Poll login state, chats, and messages; maintain per-account/per-conversation Checkpoints; normalize non-self messages; commit Message Store records; run Admission and V2 Routing; enqueue allowed Hermes Dispatch Records. |
| `dispatch-worker` | `python -m cf_agent_gateway.runtime.dispatch_worker` | Claim durable dispatch records with a token and renewable lease; enforce per-thread FIFO; call Hermes with a stable idempotency key; persist the Hermes result; create the durable response and Delivery Outbox handoff. |
| `delivery-worker` | `python -m cf_agent_gateway.runtime.delivery_worker` | Claim WeChat Delivery Outbox records; send ordered text, image, or file parts through `agent-wechat`; record each Delivery Attempt and provider Receipt; retry only outcomes known to be retryable. |

Polling never calls Hermes or sends a reply inline. Once the corresponding Dispatch Record
or Delivery Outbox record has committed, queue and claim state survives process restarts in
PostgreSQL rather than process memory. The transaction boundary before Delivery Outbox
creation has a separate reconciliation limitation described below.

## Ingestion, Checkpoints, and persist-first

The poller owns a durable Checkpoint keyed by `source_account_id` and
`conversation_id`. Messages are validated and ordered by the source `localId`. For each
message above the Checkpoint:

1. A raw message with `isSelf=true` is filtered before normalization and Message Store,
   but its Checkpoint still advances. This prevents the bot's own reply from entering a
   response loop.
2. Every other message is normalized and inserted into Message Store. That insert commits
   before Admission begins.
3. Admission resolves source identity, user policy, gateway policy, mention state, and risk.
4. When allowed, V2 Routing resolves the Agent Profile, Group Type where applicable,
   thread policy, Workspace, and AI Thread. The selected profile revision and thread policy
   are bound as a route snapshot.
5. One durable Hermes Dispatch Record is committed for the stored message.
6. Only after the sink returns successfully does the poller advance the Checkpoint.

Consequently, an unauthorized non-self message remains in Message Store but creates no
Hermes Dispatch Record and no Delivery Outbox record. Identity mapping alone is not an
authorization grant. Adding authorization later does not automatically replay a denied
message whose Checkpoint has already advanced.

## Delivery guarantees and idempotency

### Polling: at least once

The sink runs before the Checkpoint write. If the sink commits and Checkpoint advancement
fails, the same source message is presented again on the next poll. Message Store uses
unique event and physical source-message identities, and dispatch enqueue uses a unique
key derived from the stored message ID, so this redelivery reuses existing durable state.
The contract is at-least-once processing with idempotent storage, not exactly-once source
delivery.

`bootstrap_mode=latest` is intentionally different from redelivery: the first observation
of a chat initializes its Checkpoint to the largest currently visible `localId` and skips
that visible history without normalization or storage. This applies both at first startup
and when a new chat first appears while the worker is already running.

### Hermes dispatch: leases, FIFO, and stable calls

Each admitted message has one Dispatch Record and a stable idempotency key. Claiming changes
the record to `running`, increments its attempt count, and commits a claim token and lease.
The worker renews the lease during the Hermes call. Database predicates and a partial unique
index ensure that only the oldest eligible record for an AI Thread can run and that a thread
has at most one running dispatch.

A definite retryable failure becomes `failed` and may be claimed again while its retry
budget remains. An expired `running` lease is also reclaimable. `worker.retry_limit` counts
retries after the first call, so the maximum number of claims is `retry_limit + 1`.
Ambiguous outcomes, including transport timeouts and relevant post-call failures, become
`uncertain`; they are not retried automatically and block later records on the same thread.
Hermes receives the Dispatch Record's stable `Idempotency-Key` on every eligible call.

### Delivery: Outbox, Attempt, and Receipt

The Delivery Outbox stores an ordered cursor over response parts. Before sending a part,
the worker commits a Delivery Attempt. After `agent-wechat` accepts the send, one transaction
records the provider Receipt, marks the Attempt delivered, advances the part cursor, and,
for the final part, marks both delivery and response delivered.

Known retryable failures are requeued with bounded exponential backoff. Permanent failures
become `failed`. Timeouts, transport failures, invalid provider responses, and an accepted
send whose Receipt cannot be committed become `uncertain`, because automatically sending
again could duplicate a visible WeChat message. A stale pre-send claim is requeued; a stale
claim with an in-flight Attempt is quarantined as `uncertain`.

Response, delivery-target, and per-part Attempt identities are stable and constrained by
the database. The current `agent-wechat` send request does not carry the Gateway's per-part
provider idempotency key, so the outbound boundary must not be described as exactly once.
The Receipt ledger is the durable evidence of accepted parts.

## Transaction boundaries

The runtime deliberately uses several transactions and processes. The precise boundaries
matter during incident recovery:

1. **Message Store commit.** The normalized inbound message and its conversation metadata
   commit before Admission. An Admission or routing failure cannot erase the archive.
2. **Admission and dispatch enqueue.** Authorized routing state is resolved, and a unique
   Dispatch Record is committed before polling advances its Checkpoint.
3. **Dispatch claim.** The `running` transition, claim token, attempt count, and lease commit
   before the Hermes call.
4. **Hermes session advancement.** After a successful external call, the AI Thread's Hermes
   session progression commits in the dispatch execution session.
5. **Dispatch completion.** In a fresh, claim-token-fenced transaction, the raw Hermes result
   is inserted into `hermes_dispatch_responses` and the Dispatch Record changes from
   `running` to `success` atomically.
6. **Business response handoff.** After dispatch completion commits, a separate transaction
   creates `hermes_responses`, its ordered parts, and the matching `delivery_outbox` record
   atomically.
7. **Channel delivery.** Attempt creation commits before each provider call. Receipt and
   cursor advancement commit after provider acceptance.

There is a known handoff risk between steps 5 and 6. If business response persistence or
Delivery Outbox creation fails, the Dispatch Record remains `success` and the raw dispatch
response remains durable, but the dispatch worker does not reclaim that record and no
automatic reconciliation currently recreates the missing business response/outbox. The
worker logs a response-delivery error. Operations must detect and reconcile this state;
the message must not be resent to Hermes merely to repair the handoff.

## Shutdown and health

All three worker entry points translate `SIGINT` and `SIGTERM` into a stop event.

- `wechat-worker` finishes the current synchronous poll and exits; its interval wait is
  interruptible.
- `dispatch-worker` stops claiming new records, then waits for active Hermes calls to finish.
- `delivery-worker` finishes its current bounded drain before exiting.

Shutdown grace periods therefore need to exceed the configured network timeouts and expected
active-call duration. Abrupt termination is handled through Checkpoint redelivery, dispatch
lease expiry, or delivery stale-claim recovery, with ambiguous provider calls quarantined.

Each resident worker publishes an atomic JSON heartbeat. The default publish interval is
10 seconds and the default health maximum age is 30 seconds. Only `starting` and `running`
are healthy states. Dispatch and delivery use a resident heartbeat thread; the WeChat worker
does not renew its heartbeat while blocked inside a poll, so a stuck poll becomes unhealthy
rather than presenting stale liveness as success.

## Related documentation

- [Overall architecture and project boundaries](../architecture.md)
- [WeChat polling runtime](../runtime/wechat-runtime.md)
- [Identity, access, and V2 routing](../security/identity-access-routing.md)
- [CFserver production deployment](../deployment/cfserver-production.md)
- [2026-08-13 runtime validation](../validation/2026-08-13-wechat-runtime.md)
