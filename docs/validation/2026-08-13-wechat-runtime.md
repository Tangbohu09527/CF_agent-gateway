# 2026-08-13 WeChat Runtime Validation

## Record Scope

This record captures the observed CFserver production state on 2026-08-13. It separates
deployed code and basic runtime evidence from the authorization-dependent reply path that
has not yet been exercised. All identity values and message content are omitted or
represented by approved placeholders.

Related documents:

- [V2 Runtime architecture](../architecture/v2-runtime.md)
- [CFserver production deployment](../deployment/cfserver-production.md)
- [WeChat runtime operations](../runtime/wechat-runtime.md)
- [Identity, access, and routing](../security/identity-access-routing.md)

## Validated Baseline

| Item | Observed value |
| --- | --- |
| Commit | `2ac4c86` |
| Tag | `v2-enterprise-runtime-20260811` |
| Validation date | 2026-08-13 |
| PostgreSQL migration | `20260810_01` |
| Deployment state | Five Compose services healthy |

The deployed Compose services were:

- `postgres`
- `gateway`
- `wechat-worker`
- `dispatch-worker`
- `delivery-worker`

All five services were healthy during the observation window. The three workers also
published healthy heartbeat state.

## Network and Configuration Evidence

The services share the `cf-internal` network. Gateway runtime access to `agent-wechat` was
verified through container DNS at:

```text
http://cf-agent-wechat:6174
```

Using `http://127.0.0.1:6174` from a Gateway container would address that container itself
and is not the deployed `agent-wechat` route.

The active non-secret WeChat settings were equivalent to:

```yaml
wechat:
  enabled: true
  base_url: http://cf-agent-wechat:6174
  bootstrap_mode: latest
  token_env: CF_AGENT_WECHAT_TOKEN
```

Token authentication to `agent-wechat` succeeded. No credential value, digest, or
authorization header is included in this record.

Hermes Gateway version `0.20.0` ran on the Windows AI host. CFserver host access,
`dispatch-worker` container access, and the Hermes `/health` response were successful. The
endpoint and API key are intentionally omitted.

## Initial Latest Bootstrap

The first production polling baseline produced:

| Metric or table | Observed value |
| --- | ---: |
| `chats_seen` | 17 |
| `chats_failed` | 0 |
| `messages_seen` | 151 |
| `messages_processed` | 0 |
| `wechat_sync_checkpoints` | 17 |
| `messages` | 0 |
| `hermes_dispatch_records` | 0 |
| `hermes_responses` | 0 |
| `delivery_outbox` | 0 |

This is the expected `bootstrap_mode: latest` behavior. The worker inspected 151 visible
historical messages to locate each chat's newest position, established 17 checkpoints, and
did not re-consume the historical messages. Therefore `messages_seen` was 151 while
`messages_processed` remained zero. These counts describe the initial bootstrap phase, not
the later identity-discovery message.

A newly discovered chat has no checkpoint. Under `latest` mode, its first successful poll
establishes a checkpoint at the newest visible message instead of replaying that chat's
existing history. Messages arriving after that baseline are eligible for normal processing.

## Unauthorized Identity-Discovery Message

After the initial baseline, an existing private conversation sent one uniquely identifiable
test message. The record does not retain its text. The observed identifiers are represented
only as:

- source account: `<BOT_WECHAT_ACCOUNT_ID>`
- sender: `<EMPLOYEE_WECHAT_SENDER_ID>`
- private conversation: `<PRIVATE_CONVERSATION_ID>`

The following behavior was observed:

1. `agent-wechat` read the new message and `wechat-worker` retrieved it on a polling cycle.
2. Normalization identified the source account, sender, and private conversation correctly.
3. Message Store durably persisted the message before admission evaluated identity and
   access policy.
4. The conversation checkpoint advanced after successful processing.
5. No Source Identity Mapping existed for the sender.
6. Admission rejected the message as unauthorized.
7. No Hermes Dispatch record or Delivery Outbox record was created.
8. Hermes was not called and the WeChat account received no bot reply.

This is the expected persist-first, fail-closed result. Identity discovery and durable
message retention do not grant access.

## Empty Authorization and Routing Configuration

At validation time, the following business-configuration tables were empty:

- `enterprise_identities`
- `source_identity_mappings`
- `user_access_policies`
- `gateway_access_policies`
- `agent_profiles`
- `group_types`
- `conversation_agent_profile_bindings`
- `conversation_group_type_bindings`
- `employee_workspaces`
- `ai_threads`
- `thread_source_bindings`

### 2026-08-14 Read-only policy count confirmation

On 2026-08-14, a separate read-only SQL count check reconfirmed only these production
values:

| Table | Row count |
| --- | ---: |
| `user_access_policies` | 0 |
| `gateway_access_policies` | 0 |

The query used aggregate counts only; it did not retrieve policy-row contents or identity
values. No database URL, host, username, password, or other connection detail is included.
The other tables listed above were not independently rechecked on 2026-08-14.

These empty tables explain the unauthorized result and prevent this observation from being
used as evidence for an allowed V2 route.

## Evidence Boundary

The following were deployed and observed successfully:

- five-service health and worker heartbeats
- PostgreSQL at migration `20260810_01`
- `cf-internal` container networking
- Gateway-to-`agent-wechat` access and token authentication
- WeChat login-state checks, chat polling, and message polling
- per-conversation checkpoints and `latest` bootstrap behavior
- message normalization, Message Store persistence, and persist-first ordering
- fail-closed rejection of an unmapped sender without a Hermes call
- CFserver host and `dispatch-worker` connectivity to Hermes

The following authorization-dependent production path was not validated:

1. create a test Enterprise Identity
2. create a Source Identity Mapping
3. create User and Gateway Access Policies
4. create an Agent Profile
5. bind the private conversation to that profile
6. observe Admission Allowed and a V2 Route Snapshot
7. create and consume a Hermes Dispatch record
8. persist the Hermes response
9. create and consume a Delivery Outbox record
10. observe the actual reply in WeChat

No statement in this record should be interpreted as evidence that this complete allowed
message and AI reply loop has run successfully in production. Live media delivery was also
not part of this validation.

## Open Operational Issue

A Hermes login startup entry existed on the Windows AI host, but Hermes Gateway did not
start automatically after an AI-host restart. Running `hermes gateway start` manually
restored service and connectivity. Hermes Gateway startup reliability remains unresolved
and requires a separate operational fix and reboot verification.

## Data Safety

This document contains no real name, WeChat identifier, conversation identifier, test
message body, host LAN address, credential, credential digest, database password, or
production database URL.
