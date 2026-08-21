# Hermes and AI Execution Integration

## Scope and status

This document specifies only the Gateway side of the AI integration. It does not specify
Hermes internals, model runtime behavior, node discovery, node scheduling, or business
execution logic.

- **Implemented (已实现)**: the current Gateway code contains the capability.
- **Validated (已验证)**: automated contract tests or the recorded V1 Staging text path
  exercised the capability.
- **Unverified (未验证)**: a configurable path lacks validation in the stated environment.
- **Planned (规划)**: a target Gateway capability is not implemented.

**Not implemented** denotes absence without inventing a plan; it is not interchangeable
with Unverified or Planned.

The implemented AI boundary is one configured Hermes HTTP endpoint:

```text
Persisted and admitted Gateway Message
        |
        v
Gateway HermesDispatchService
        |
        v
Configured Hermes HTTP endpoint
        |
        v
AI execution nodes (opaque to Gateway)
```

Gateway does not connect directly to individual AI nodes. It has no node registry,
discovery, heartbeat, capability inventory, health probe, load balancer, placement policy,
or per-node credentials. Hermes is responsible for anything behind its endpoint; Gateway
only owns its outbound request, response validation, and local conversation/session state.

## Connection prerequisites

Hermes is disabled in `config/config.yaml` by default. To enable it for the Worker:

```yaml
hermes:
  enabled: true
  base_url: "https://hermes.example.internal/"
  api_key_env: "HERMES_API_KEY"
  model: "hermes-agent"
```

Set the named environment variable in the Worker process:

```bash
export HERMES_API_KEY='<hermes-api-key>'
```

The requirements are:

- `base_url` is a non-empty HTTP or HTTPS URL without embedded credentials, query, or
  fragment;
- `api_key_env` names a non-empty environment variable; the secret is not stored in YAML;
- the named variable contains a visible-ASCII bearer credential without whitespace;
- `model` is a non-empty string, trimmed at configuration/client boundaries and then sent
  in each request;
- the Worker has a direct route to the endpoint because the client ignores environment
  proxy settings;
- inbound polling and admission are configured, because Hermes dispatch is assembled only
  inside the one-cycle/resident Worker runtime.

A literal YAML `hermes.api_key` field is rejected. When `hermes.enabled` is `false`, the
Worker still polls, persists, and evaluates admission, but does not dispatch to Hermes or
relay an AI response.

The FastAPI HTTP process does not create a Hermes client. `POST /internal/messages` stores
a normalized event only and is not a Hermes dispatch endpoint.

## HTTP contract

Gateway creates a synchronous `httpx` client with:

- `Authorization: Bearer <api-key>`;
- `Accept: application/json`;
- redirects disabled;
- environment proxy settings disabled;
- connect timeout 5 seconds, read timeout 30 seconds, write timeout 15 seconds, and pool
  timeout 5 seconds.

For one eligible Message it calls:

```http
POST <hermes.base_url>/v1/chat/completions
Content-Type: application/json
Authorization: Bearer <api-key>
X-Hermes-Session-Id: <gateway-bound-session-id>
```

```json
{
  "model": "<hermes.model>",
  "messages": [
    {
      "role": "user",
      "content": "<persisted-message-content>"
    }
  ]
}
```

The request contains one persisted `content` string. Dispatch requires it to be non-empty
but does not require `message_type: text`; another admitted non-system message type can
therefore send its normalized content string. Gateway does not send attachment bytes, Task
state, business workflow state, Skill execution instructions, AI-node identity, or a
dispatch idempotency key. Only the text-message path has recorded end-to-end validation.

A successful response must be HTTP 2xx, valid JSON, contain a non-empty `choices` list, and
include a valid `X-Hermes-Session-Id` response header. Every choice must contain a message
with `role: "assistant"` and string `content`; Gateway uses the first choice. Extra JSON
fields are ignored. Streaming responses are not implemented.

Conceptual accepted shape:

```http
HTTP/1.1 200 OK
X-Hermes-Session-Id: <effective-session-id>
Content-Type: application/json
```

```json
{
  "choices": [
    {
      "message": {
        "role": "assistant",
        "content": "<assistant-text>"
      }
    }
  ]
}
```

This contract is **Implemented** and covered by automated tests. A real Hermes endpoint was
also exercised by the recorded V1 Staging text round trip. Every new environment must still
verify DNS, network policy, TLS, credential acceptance, response shape, and session-header
behavior independently.

## Dispatch eligibility

Gateway dispatches only when all of the following hold:

1. the inbound event has been normalized and committed to Message Store;
2. the sender is not self/system/senderless for the active path;
3. identity resolution and user plus Gateway policy admission return `ALLOWED`;
4. an active Employee Workspace and AIThread are resolved;
5. the persisted Message still matches the exact source account/conversation binding;
6. the Workspace belongs to the admitted enterprise identity;
7. the persisted message content is non-empty;
8. Hermes is enabled and its client initialized successfully.

The dispatch service reloads and verifies these facts from Gateway state. It does not trust
an arbitrary caller-supplied content or thread identifier.

## AIThread and Hermes session binding

Gateway owns the stable local `AIThread`; Hermes owns the effective remote session. The
binding is stored as `AIThread.hermes_thread_id` and is unique in the Gateway database.

Before first dispatch, Gateway atomically claims a deterministic value:

```text
v1:cf-agent-gateway:<gateway-ai-thread-id>
```

It sends that value in `X-Hermes-Session-Id`. Hermes must return an effective session ID in
the same response header. If Hermes returns a replacement, Gateway advances its binding
with a compare-and-swap update so a concurrent result cannot silently overwrite the winner.
Later messages use the currently bound value.

Claim, reuse, rotation, and compare-and-swap behavior are **Implemented** and validated by
automated concurrency/dispatch tests. The recorded V1 text path validates session binding
and reuse only; real-endpoint rotation and concurrency remain **Unverified**. This is
session continuity, not a durable Task lifecycle.

The target group-conversation design isolates a thread by source account, group, and
sender. The current implementation instead binds by source account plus physical
conversation, so authorized senders in one group share a Hermes session. This known
deviation is **Implemented and validated**, while sender-isolated correction is **Planned**.

## Response relay

After Hermes dispatch succeeds, Gateway:

1. commits the effective Hermes session binding;
2. reloads the originating Message and AIThread;
3. validates the exact source account and physical conversation binding;
4. creates an outbound sender scoped to that source account;
5. sends the assistant text to the bound conversation through Gateway's external
   message-adapter contract.

Only a successful Hermes dispatch is relayed. The response is text-only. Gateway does not
interpret or execute the assistant content as business logic.

The outbound contract and the recorded V1 text response are **Validated**. Media responses,
partial results, progress events, Task IDs, cancellation, and result polling are not part of
the implemented Hermes outcome.

## Failure and retry semantics

Hermes errors are classified as timeout, transport failure, non-2xx API response, invalid
successful response, dispatch invariant failure, or response-delivery failure. The error
objects carry stable sanitized metadata; runtime surfaces can collapse them into broader
polling failure codes and do not expose secrets or message bodies.

The Hermes client has no internal HTTP retry, circuit breaker, or upstream idempotency key.
The Worker and per-message ledger provide the broader retry boundary:

- a message/sink failure prevents its polling checkpoint from advancing;
- the same message can be processed again in a later cycle;
- Message Store idempotency prevents a duplicate stored Message;
- succeeded dispatch/delivery records suppress completed external calls;
- active leases reject concurrent replay, while failed or 120-second stale leases can be
  reclaimed with an incremented attempt count;
- transport, timeout, invalid 2xx response, HTTP 408, HTTP 429, and 5xx failures remain in
  progress until lease expiry because the external result is ambiguous;
- each resident cycle performs a bounded database recovery sweep before polling, so
  failed/stale dispatches and missing/failed/stale deliveries do not depend on the source
  message remaining in the adapter window;
- Gateway still has no upstream idempotency key, so external success followed by a crash
  before the local success commit can repeat after stale reclaim.

This is at-least-once inbound processing with durable suppression of locally recorded
success, not end-to-end exactly-once delivery. Stop the Worker before lease expiry when an
ambiguous external result must be reconciled before automatic stale reclaim.

Within the resident Worker, a degraded returned `PollResult` and a thrown non-fatal cycle
exception increment the same consecutive-failure count and use exponential delay capped by
`runtime.polling_retry_max_seconds`. Only a healthy returned result resets the count.
Missing required credentials, invalid configuration, a fresh competing resident lease, or
disabled polling stops the Worker.

## AI execution-node relationship

| Concern | Current Gateway behavior | Status |
| --- | --- | --- |
| Endpoint selection | One configured `hermes.base_url` | Implemented and V1 text-path validated |
| Model selection | One configured `hermes.model` string per Worker settings | Implemented and contract-tested |
| Session continuity | Persisted Hermes ID per Gateway AIThread | Implemented and validated |
| Direct node address or credentials | None | Not implemented by design boundary |
| Node discovery/registration/heartbeat | None | Not implemented |
| Capability- or load-based routing | None | Planned general provider/node routing |
| Hermes endpoint connectivity | Side-effect-free `/health` probe; individual execution nodes remain opaque | Implemented for endpoint reachability only |
| Async Task/progress/cancel/result protocol | None | Planned Task lifecycle; no current contract |
| Multi-endpoint failover | None | Not implemented |

Do not configure an individual execution node as though Gateway understands its runtime
contract unless that endpoint itself satisfies the documented Hermes HTTP and session
contract. From Gateway's perspective, everything beyond Hermes is opaque.

## Connectivity verification

`GET /health` performs a side-effect-free Hermes connectivity probe and summarizes durable
dispatch operation state. It does not send a prompt or inspect AI nodes behind Hermes. The
one-cycle Worker command remains side-effecting: it can consume and dispatch real messages.

For a controlled environment:

1. validate the selected YAML without printing secret values;
2. confirm the Worker has a direct route to `hermes.base_url` and that TLS trust is valid;
3. confirm the environment variable named by `hermes.api_key_env` is present in the Worker
   process;
4. start exactly one resident Worker for the shared Gateway database;
5. submit a synthetic allowed text event through the normal polling path;
6. confirm one Message, the expected Workspace/AIThread, a Hermes session binding, one
   assistant response, and an advanced checkpoint;
7. send a second text in the same conversation and confirm session reuse;
8. stop/restart the Worker and confirm the persisted session and checkpoint are reused;
9. record the environment, time, result, and any unrun failure scenarios.

Never use production business content for an initial connectivity test. Do not log the API
key, Authorization header, full message body, or external identifiers.

## Troubleshooting from the Gateway side

| Symptom | Gateway-side checks |
| --- | --- |
| Worker exits at startup | Validate YAML, `wechat.enabled`, both named secret variables, database access, and base URLs |
| Polling continues but Hermes is never called | Confirm `hermes.enabled`, admission is `ALLOWED`, identity/policies are provisioned, and content is non-empty |
| `hermes_transport_error` | Check direct DNS/route/TLS; environment proxy variables are ignored |
| `hermes_timeout_error` | Check endpoint latency against the fixed client timeouts and inspect for an ambiguous completed request |
| `hermes_api_error` | Inspect the sanitized HTTP status and verify endpoint, bearer credential, and model with the Hermes operator |
| `hermes_response_error` | Verify JSON choice/message shape and `X-Hermes-Session-Id` response header |
| Dispatch invariant error | Check active Workspace/AIThread, admitted identity, persisted Message, and exact source binding |
| Response delivery failure | Check the source-account binding and outbound adapter reachability; assume Hermes may already have succeeded |

## Status summary

**Implemented:** single-endpoint Hermes client, bearer authentication, synchronous chat
completion contract, admission-gated dispatch of non-empty persisted content, persisted
session claim/reuse/rotation, per-message dispatch/delivery status and leases, text response
relay, side-effect-free health connectivity, sanitized errors, and Worker assembly.

**Validated:** automated client/dispatch/concurrency/response tests and the historical V1
Staging text round trip. That record does not validate every future endpoint or production
environment.

**Unverified:** production TLS/network policy, live endpoint behavior in a new environment,
load limits, and long-running reliability. Multi-endpoint failover is **Not implemented**;
direct AI-node operations are outside the Gateway integration boundary.

**Planned:** durable Task lifecycle, provider/node registry and routing, a general Task
outbox, sender-isolated group threads, and richer asynchronous execution contracts.

See [architecture.md](architecture.md) for Gateway ownership and state boundaries,
[deployment.md](deployment.md) for process operation and recovery, and
[v1-staging-validation.md](v1-staging-validation.md) for recorded evidence.
For active incidents, use [troubleshooting.md](troubleshooting.md) before the controlled
replay procedures in [recovery-guide.md](recovery-guide.md).
