# Runtime health

## Three different signals

| Endpoint | Success | Failure semantics |
| --- | --- | --- |
| `GET /health` | `200 {"status":"ok"}` | HTTP process is not serving. |
| `GET /ready` | `200 {"status":"ready"}` | `503` while startup, database readiness, or startup schema gate is unavailable. |
| `GET /health/runtime` | Aggregated business snapshot | `503` only for top-level `unhealthy`; `degraded` remains `200` so operators can read details. |

These endpoints are public only within the current default loopback/private deployment
boundary. Do not expose them to an untrusted network without a reverse-proxy policy.

`/health` is not proof that polling, Hermes execution, or delivery works. Use
`/health/runtime` for that decision.

## Payload

The business snapshot has these top-level fields:

```json
{
  "status": "healthy",
  "checked_at": "2026-08-23T02:00:00Z",
  "components": {},
  "dispatch": {},
  "delivery": {}
}
```

It is intentionally redacted: no message body, token, cookie, connection string or
business payload is returned.

### Components

| Component | Representative status | Meaning |
| --- | --- | --- |
| `database` | `ok`, `unavailable` | `SELECT 1` succeeds. |
| `migration_schema` | `ok`, `mismatch` | Database is at the single packaged Alembic head. |
| `wechat_worker` | `ok`, `missing`, `stale_or_invalid`, `unknown`, `disabled` | Configured heartbeat file is fresh and healthy. |
| `dispatch_worker` | Same worker states | Dispatch heartbeat is fresh when worker/Hermes are enabled. |
| `delivery_worker` | Same worker states | Delivery heartbeat is fresh when WeChat is enabled. |
| `wechat_auth` | `logged_in`, `logged_out`, `unknown`, `disabled` | Last redacted WeChat worker auth observation. |
| `hermes` | `ok`, `degraded`, `disabled` | Endpoint/key configuration plus the last real Hermes operation observation. |
| `wechat_checkpoint_continuity` | `ok`, `degraded`, `unknown` | Count of nonzero checkpoints without a verified serverId anchor. |

The runtime reads heartbeat paths from:

```text
CF_GATEWAY_WECHAT_HEARTBEAT_PATH
CF_GATEWAY_DISPATCH_HEARTBEAT_PATH
CF_GATEWAY_DELIVERY_HEARTBEAT_PATH
CF_GATEWAY_RUNTIME_HEARTBEAT_MAX_AGE_SECONDS
```

Each enabled service must point the Gateway health process at the same heartbeat file
the corresponding worker writes. A missing path produces `unknown`, not fabricated
health.

The Hermes component reports `configuration` separately as `configured`,
`unconfigured`, or `disabled`. Its `connectivity` is
`last_operation_succeeded`, `last_operation_failed`, `unverified`, or `disabled`.
The dispatch worker publishes an observation only after a real Hermes call returns or
fails; process liveness alone never proves upstream connectivity. A configured worker
with no observed call is therefore `degraded/unverified`. The operation observation
expires under the same `CF_GATEWAY_RUNTIME_HEARTBEAT_MAX_AGE_SECONDS` freshness budget
as worker evidence, so periodic process heartbeats cannot keep an old success green.
This endpoint does not send a synthetic Hermes request.

### Dispatch metrics

| Field | Interpretation |
| --- | --- |
| `queued` | Waiting thread heads/tails. |
| `running` | Currently claimed records. |
| `failed` | Definite failures eligible for bounded retry or finalization. |
| `uncertain` | Ambiguous Hermes effects requiring manual recovery. |
| `dead` | Terminal records that did not succeed. |
| `stale_running` | Running records whose lease has expired. |
| `blocked_threads` | Distinct threads where an uncertain head blocks later work. |
| `missing_delivery` | Successful dispatches with persisted dispatch response but no delivery row. |
| `oldest_uncertain_age_seconds` | Time since the oldest record entered `uncertain` (its terminal transition timestamp), or null. |
| `oldest_backlog_age_seconds` | Age of the oldest queued/running/failed/uncertain record, or null. |

### Delivery metrics

| Field | Interpretation |
| --- | --- |
| `queued`, `delivering`, `delivered`, `failed`, `uncertain` | Existing outbox status counts. |
| `stale_delivering` | Deliveries claimed longer than the runtime stale threshold. |
| `missing_delivery` | Same reconciliation signal as the dispatch section. |
| `oldest_backlog_age_seconds` | Oldest nonterminal delivery age, or null. |

## Overall status

- `unhealthy`: database unavailable or migration schema mismatch; returns HTTP `503`.
- `degraded`: infrastructure is queryable but a worker/config/checkpoint component is
  missing, stale, unknown or degraded, or nonzero failure/uncertain/dead/stale/blocked/
  missing-delivery metrics require attention; returns HTTP `200`.
- `healthy`: no current degradation predicate is present; returns HTTP `200`.

A `dead` count remains a degraded historical signal until the site's retention and
acknowledgement policy accounts for it. Do not delete history merely to turn health green.

## Alerting guidance

Page immediately for `unhealthy`, a stale enabled worker, any `uncertain` dispatch,
blocked thread, stale running/delivery claim, or missing delivery. Alert on sustained
backlog age rather than only queue count. Treat `wechat_auth=logged_out` as an external
login incident. Treat checkpoint continuity `degraded` as a migration/session-continuity
investigation, not permission to rewind manually.

Treat Hermes `last_operation_failed` as observed upstream failure. Treat
`unverified` as missing connectivity evidence: use a controlled end-to-end validation
under the deployment checklist rather than assuming the heartbeat proves Hermes works.

The GitHub Actions health tests validate payload behavior with controlled fixtures.
Actual alert thresholds, dashboard conversion to `Asia/Shanghai`, and CFserver endpoint
monitoring are external responsibilities and were not validated against production.
