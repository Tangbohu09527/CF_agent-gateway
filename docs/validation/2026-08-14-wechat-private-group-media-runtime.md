# 2026-08-14 WeChat Private, Group, Reply, and Media Validation

## Record scope

This record captures follow-up CFserver observations made on 2026-08-14 against the
deployed Gateway baseline at commit `2ac4c86`, tag
`v2-enterprise-runtime-20260811`. It follows the
[2026-08-13 baseline and unauthorized-path record](2026-08-13-wechat-runtime.md); it does
not rewrite that earlier observation.

The validation covered authorized private and explicitly mentioned group text paths,
thread reuse, application-process restart persistence, reply-message handling, an
evidence-guarded `uncertain` dispatch recovery, inbound image discovery, and the Hermes
profile-reference boundary. It did not validate a complete AI media path or host-level
disaster recovery.

No production credential, real WeChat identifier, conversation identifier, contact name,
host address, database connection string, test image, or image digest is included.

## Outcome summary

| Observation | Result |
| --- | --- |
| Authorized private text path through an actual WeChat reply | `FULL PRIVATE E2E PASSED` |
| Persistence across restart of four application processes in their existing containers | `APPLICATION RESTART PERSISTENCE PASSED` |
| Private context reuse after that restart | `THREAD REUSE AFTER RESTART PASSED` |
| Group message without a structured bot mention | Persisted and denied with `bot_not_mentioned`; no dispatch |
| Authorized, explicitly mentioned group text path through an actual WeChat reply | `FULL MENTIONED GROUP E2E PASSED` |
| Same-sender group context reuse | `GROUP THREAD REUSE PASSED` |
| Reply-message recognition and `reply_context` persistence | Verified for private and group messages |
| Inbound image discovery and independent `agent-wechat` media-byte retrieval | Verified within the boundary below |

## Private identity, access, and route setup

An earlier read-only count check on 2026-08-14 confirmed that
`user_access_policies` and `gateway_access_policies` each contained zero rows. Reviewed
provisioning then established this test route:

| Configuration object | Observed count or state |
| --- | ---: |
| Enterprise Identity | 1 |
| Source Identity Mapping | 1 |
| User Access Policy | 1 |
| Gateway Access Policy | 1 |
| Agent Profile | 1 |
| Private Conversation-AgentProfile Binding | 1 |
| Admission dry-run | `allowed` |
| Private Thread Policy | `private_sender` |

Identity mapping remained only an identity fact; the User and Gateway Access Policies were
both required for Admission. At configuration completion, the test had not pre-created an
Employee Workspace, AI Thread, Hermes Dispatch, business Response, or Delivery Outbox.
Those runtime objects were created only after the first real message passed Admission.

The count checks and route review did not retrieve or record database credentials, policy
contents, or source identity values.

## Authorized private text path

A new message in the authorized private conversation produced this observed path:

1. `wechat-worker` persisted the inbound message in Message Store.
2. Admission returned `allowed`.
3. V2 Routing created one Employee Workspace and one private AI Thread with
   `thread_policy=private_sender`.
4. The Hermes Dispatch succeeded and Hermes returned the expected text.
5. Response Persistence reached `delivered`.
6. The Delivery Outbox reached `delivered`.
7. The Delivery Attempt succeeded and one Delivery Receipt was recorded.
8. The reply was readable through the upstream `agent-wechat` messages and was received by
   the WeChat user.
9. The bot's outbound reply was not dispatched again as a new inbound request.

The resulting evidence is `FULL PRIVATE E2E PASSED`. This result covers the authorized
private **text** path; it is not evidence of image understanding or media delivery.

## Application-process restart persistence

The following application services were restarted under control:

- `gateway`
- `wechat-worker`
- `dispatch-worker`
- `delivery-worker`

PostgreSQL, `agent-wechat`, Hermes, and the CFserver host were not restarted. The four
application services returned healthy. Their container IDs did not change; only each
container's `StartedAt` value changed. The precise result is therefore process restart and
recovery inside the existing application containers, not container recreation.

Before and after the restart, the following remained identical:

- Workspace ID and status
- AI Thread ID and status
- Thread Key, Thread Type, and Thread Policy
- Agent Profile and Hermes Thread ID
- original Dispatch ID, state, and attempt count
- raw Hermes response
- Response ID and state
- Delivery ID and state
- Delivery Attempt and Delivery Receipt counts
- relevant table row totals

The resulting evidence is `APPLICATION RESTART PERSISTENCE PASSED`. It does not establish
container deletion/recreation, PostgreSQL restart, CFserver reboot, or AI-host reboot
recovery.

## Private thread reuse after restart

After the application-process restart, the same private conversation sent a new request
that depended on its prior context. The runtime reused the same Employee Workspace,
AI Thread, Thread Key, and Hermes Thread ID. Workspace and AI Thread totals both remained
one, while the dispatch count on that private thread increased from one to two.

The second Hermes Dispatch succeeded, its Response and Delivery both reached `delivered`,
and the user received the correct context-dependent answer in WeChat. The resulting evidence
is `THREAD REUSE AFTER RESTART PASSED`.

This proves continuity through the reused Hermes thread. It is not evidence that a Context
Snapshot was created or read.

## Group admission and routing

### Message without a bot mention

A three-participant test group was correlated through a unique test marker because the
upstream chat listing did not return its display name. The real chat identifier is omitted.
An ordinary group message that did not use WeChat's member-selection mention function was:

- persisted in Message Store with `conversation_type=group`;
- stored with `is_mentioned=false` and its Raw Payload;
- denied without creating a Hermes Dispatch.

The dispatch count for that message was zero, which matches the `bot_not_mentioned`
fail-closed policy.

### Group Type and route

The test group was then assigned an active Group Type with key
`cf-authorized-group-sender`, Thread Policy `group_sender`, and the existing Gateway
Agent Profile. An explicit Conversation-GroupType Binding selected that route.

Admission preflight produced:

| Mention fact | Result |
| --- | --- |
| `is_mentioned=false` | `denied` / `bot_not_mentioned` |
| `is_mentioned=true` | `allowed` |

Creating the Group Type and binding did not itself create another Workspace, AI Thread, or
Dispatch.

### Explicitly mentioned group text path

The allowed test used WeChat's member-selection function to mention the bot; it did not rely
on manually typed `@` text. The mentioned group message was persisted with
`is_mentioned=true`. The earlier non-mentioned message still had no Dispatch.

V2 Routing selected the active `cf-authorized-group-sender` Group Type and
`thread_policy=group_sender`. It reused the same employee Workspace used by the private
conversation, but created a distinct group AI Thread, Thread Key, and Hermes Thread ID. The
employee Workspace count remained one, while the total AI Thread count increased from one
to two.

The first group-thread Dispatch succeeded, Hermes returned the expected text, Response and
Delivery reached `delivered`, and the WeChat group received the reply. The bot's reply was
not dispatched again. The resulting evidence is `FULL MENTIONED GROUP E2E PASSED`.

### Group thread reuse

The same authorized sender then used a real bot mention in the same group for a second,
context-dependent request. Both source messages were distinct and stored with
`is_mentioned=true`. The second request reused the group AI Thread, Thread Key, and Hermes
Thread ID with `thread_type=group` and `thread_policy=group_sender`.

The total AI Thread count remained two, while the Dispatch count on the group thread
increased from one to two. The second Dispatch succeeded, Response and Delivery reached
`delivered`, and the group received the correct context-dependent answer. The resulting
evidence is `GROUP THREAD REUSE PASSED`.

## Reply messages and controlled `uncertain` recovery

Private and group reply messages were both normalized with `message_type=reply`. Their
`reply_context` values were non-empty and retained a summary of the quoted content.

The group reply did not contain a structured bot mention. It remained
`is_mentioned=false`, was persisted, and created no Hermes Dispatch, as required by
`bot_not_mentioned`.

The private reply passed identity and Admission, reused the existing `private_sender`
AI Thread, and created a Dispatch. Its first attempt encountered `hermes_timeout_error`
because Hermes Gateway was not running. The Dispatch became `uncertain`; it created no
business Response or Delivery, no later Dispatch existed on the thread, and the Hermes
client contained no matching request or reply.

The Windows login startup entry existed, but that did not mean the Hermes Gateway process
was running. CFserver connectivity to
`http://<AI_HOST_LAN_IP>:<HERMES_GATEWAY_PORT>` timed out. After an operator started Hermes
Gateway, `/health` succeeded from both the Windows host and CFserver again.

### Evidence-guarded manual recovery

Recovery proceeded only after all of these conditions were checked:

- a PostgreSQL backup had been created;
- the Dispatch was still in the expected `uncertain` state;
- its attempt count was exactly one;
- no Hermes Dispatch Response or business Response existed;
- no later Dispatch existed on the same AI Thread;
- the Hermes session contained no matching request or reply; and
- Hermes `/health` had been restored.

Under those guards, an operator manually changed the original Dispatch from `uncertain` to
`failed` and restarted `dispatch-worker`. The same persisted Dispatch was reclaimed; its
attempt count increased from one to two. The second attempt succeeded, Hermes returned the
expected text, Response and Delivery reached `delivered`, and the user received the reply.

This recovery event involved the existing `dispatch-worker` application service only. It
does not establish container recreation or PostgreSQL, CFserver-host, or AI-host restart
recovery.

This was a one-off, evidence-reviewed incident action after backup. It was **not** automatic
recovery and is **not** a routine database-editing procedure. No SQL is provided here. The
current project has no supported `uncertain` management API or administrator recovery
command; guarded inspection and recovery must be implemented as a formal management
capability before this becomes a normal operation.

The reply boundary is:

| Capability | Status |
| --- | --- |
| Reply message-type recognition | Live verified |
| `reply_context` persistence | Live verified |
| Text response path for a private reply message | Live verified, including the guarded incident recovery above |
| Automatic injection of `reply_context` into the Hermes request | Not implemented |

## Inbound image discovery and media retrieval

One non-sensitive image and a later unique text marker were sent in the test group without
mentioning the bot. The image observation was:

- Message Store row persisted with `message_type=image` and `raw_type=3`;
- `is_mentioned=false`;
- non-empty source-local and source-server identifiers;
- Raw Payload retained;
- zero Attachment rows; and
- zero Dispatches for both the image and the text marker.

An independent call to the `agent-wechat` media API retrieved the image with
`supported=true`, `media_type=image`, `format=jpeg`, a filename, and 5,712 bytes. The JPEG
signature and a computed SHA-256 digest were verified. The image itself and the digest are
not retained or published by this record.

The accurate media boundary is:

| Capability | Status |
| --- | --- |
| WeChat image message recognition | Live verified |
| Message Store and Raw Payload persistence | Live verified |
| `agent-wechat` media API byte retrieval | Live verified |
| Attachment row creation from inbound polling | Not integrated |
| Inbound media storage in Gateway private storage | Not integrated |
| Image bytes in Hermes multimodal input | Not integrated |
| Hermes output Artifact materialization into Gateway | Not integrated |
| Hermes-generated image delivery back to WeChat | Not integrated and not live verified |

Retrieving valid JPEG bytes does not mean that Hermes saw or understood the image.

## Hermes profile-reference boundary

Hermes supports multiple independent profiles, each with its own configuration, skills, and
`SOUL.md`. Gateway does not provision or manage those profiles. It stores its own immutable
Agent Profile revision, uses `external_profile_ref` to select an already existing Hermes
profile, and sends that value as `profile_reference`. It does not create, clone, or modify a
Hermes profile.

The validated private and group routes used different Gateway AI Threads and Hermes Thread
IDs, but both selected `external_profile_ref=default`. The effective mapping was:

```text
private conversation -> Gateway Agent Profile -> Hermes external profile
group conversation -> Group Type -> Gateway Agent Profile -> Hermes external profile
```

The selected Hermes profile determines Hermes configuration, skills, and `SOUL.md`. The
Gateway Thread Policy independently determines context isolation. Profile selection and
thread isolation must not be treated as the same control.

## Remaining boundaries

The following remain unverified or not integrated:

- deletion and recreation of application containers;
- PostgreSQL restart recovery;
- CFserver whole-host restart recovery;
- AI-host restart followed by automatic Hermes Gateway recovery;
- QR-code login on a completely new WeChat device;
- automatic `reply_context` injection into Hermes;
- inbound WeChat Attachment persistence and private media storage;
- Hermes multimodal input;
- Hermes output Artifact materialization;
- complete WeChat image and file send/receive paths; and
- a supported management API or administrator command for `uncertain` dispatches.

The `group_shared` Thread Policy also was not part of this validation.

## Related documentation

- [2026-08-13 baseline and unauthorized-path validation](2026-08-13-wechat-runtime.md)
- [V2 Runtime architecture](../architecture/v2-runtime.md)
- [WeChat polling runtime](../runtime/wechat-runtime.md)
- [Identity, access, and V2 routing](../security/identity-access-routing.md)
- [CFserver production deployment](../deployment/cfserver-production.md)
- [WeChat Media Adapter V2](../wechat-media-adapter-v2.md)
