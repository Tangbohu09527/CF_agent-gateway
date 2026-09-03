# Runtime health

## Health surfaces

The current runtime has four distinct health surfaces:

| Surface | Question answered |
| --- | --- |
| `GET /health` | Is the Gateway HTTP process alive? |
| `GET /ready` | Did startup complete, and is the cached database probe fresh/successful? |
| `GET /health/runtime` | Is the durable business chain healthy or degraded? |
| Runtime Controller `status` | Are the gated Poll/Delivery containers, heartbeats, and Token File contract ready? |

No one surface replaces the others. A live Gateway does not prove database readiness,
worker freshness, external login, Hermes connectivity, or an empty queue.

## Liveness

`GET /health`

```json
{"status":"ok"}
```

HTTP 200 means only that FastAPI handled the request.

## Readiness

`GET /ready`

Success:

```json
{"status":"ready"}
```

Failure:

```json
{"status":"not_ready"}
```

Failure uses HTTP 503. Readiness requires:

- application startup completed;
- database initialization/check completed;
- the background database probe's most recent result succeeded;
- the last successful probe is no more than 15 seconds old.

The probe normally runs every five seconds. The HTTP request reads cached state and does
not issue a database query itself.

## Runtime business health

`GET /health/runtime` returns exactly these top-level fields:

```json
{
  "status": "healthy",
  "checked_at": "2026-09-03T00:00:00Z",
  "components": {},
  "dispatch": {},
  "delivery": {}
}
```

`checked_at` is UTC. The example timestamp illustrates format only.

Top-level semantics:

| Status | HTTP | Meaning |
| --- | --- | --- |
| `healthy` | 200 | Database/schema are healthy and no configured component or durable metric triggers degradation. |
| `degraded` | 200 | Core database/schema remain usable, but a worker/external observation or queue metric needs attention. |
| `unhealthy` | 503 | Database is unavailable or the migration schema does not match the packaged head. |

There is no top-level `unavailable` status. `unavailable` is a component value used for
the database. If the runtime health service was not initialized, the endpoint returns
HTTP 503, top-level `unhealthy`, `checked_at: "unavailable"`, database
`unavailable`, migration schema `unknown`, and empty metric objects.

## Component fields

`components` contains:

| Component | Fields and values |
| --- | --- |
| `database` | `status: ok` or `unavailable` |
| `migration_schema` | `status: ok`, `mismatch`, or `unknown` |
| `wechat_worker` | `status`; when healthy also `state`, `updated_at`, and selected `details` |
| `dispatch_worker` | same Worker shape |
| `delivery_worker` | same Worker shape |
| `wechat_auth` | `status: logged_in`, `logged_out`, `unknown`, or `disabled` |
| `hermes` | `status`, `configuration`, and `connectivity` |
| `wechat_checkpoint_continuity` | `status` and `unverified_checkpoint_count` |

Enabled Worker status values are:

- `ok`: heartbeat exists, parses, and is fresh;
- `degraded`: heartbeat is fresh but reports a failed last cycle;
- `missing`: configured heartbeat path does not exist;
- `stale_or_invalid`: file exists but is stale or invalid;
- `unknown`: enabled Worker has no configured heartbeat path;
- `disabled`: the matching adapter/runtime is disabled.

For a valid heartbeat, `state` and `updated_at` are included. Only these detail keys may
be exposed:

- `phase`
- `cycle_sequence`
- `last_cycle_succeeded`
- `wechat_auth`
- `last_operation_succeeded`
- `last_operation_at`

The default freshness threshold is 30 seconds in production Compose.

Hermes component values:

| Situation | Result |
| --- | --- |
| Hermes disabled | `status: disabled`, configuration/connectivity `disabled` |
| Enabled but URL/key unavailable | `status: degraded`, configuration `unconfigured`, connectivity `unverified` |
| Configured and Dispatch heartbeat healthy, no fresh operation evidence | `status: ok`, connectivity `no_recent_observation` |
| Fresh successful operation | connectivity `last_operation_succeeded` |
| Fresh failed operation | `status: degraded`, connectivity `last_operation_failed` |

`no_recent_observation` is not proof that Hermes is reachable. It means no fresh
success/failure observation is available.

Checkpoint continuity is `degraded` when a nonzero Checkpoint lacks a continuity
fingerprint; `unverified_checkpoint_count` is the number of such rows. When the database
cannot be safely queried, its status is `unknown` and the count is null.

## Dispatch fields

`dispatch` always uses these fields:

- `queued`
- `running`
- `failed`
- `uncertain`
- `dead`
- `stale_running`
- `blocked_threads`
- `missing_delivery`
- `reconciliation_backlog`
- `reconciliation_deferred`
- `reconciliation_poison`
- `oldest_uncertain_age_seconds`
- `oldest_backlog_age_seconds`
- `oldest_reconciliation_age_seconds`

`stale_running` counts running Dispatch leases that have expired. `blocked_threads`
counts AI Threads where an `uncertain` Dispatch has later nonterminal work.
`missing_delivery` counts successful Dispatches with a persisted Hermes response but no
Delivery row. Reconciliation fields describe successful WeChat Dispatches still missing a
normalized Response or Delivery, including deferred and quarantined candidates.

Any nonzero `failed`, `uncertain`, `dead`, `stale_running`, `blocked_threads`, or
`reconciliation_poison` degrades top-level status. Queued/running work and a normal
reconciliation backlog are reported but do not alone change status.

## Delivery fields

`delivery` always uses:

- `queued`
- `delivering`
- `delivered`
- `failed`
- `uncertain`
- `stale_delivering`
- `missing_delivery`
- `oldest_backlog_age_seconds`

A Delivery is stale when it remains `delivering` with a claim at least 300 seconds old.
Any nonzero `failed`, `uncertain`, `stale_delivering`, or `missing_delivery`
degrades top-level status.

## Runtime Controller status

Run from the active release directory through the approved privileged execution path:

```bash
${CONTROLLER} status
```

The JSON object has exactly:

- `worker_health`
- `delivery_health`
- `heartbeat_age`
- `token_contract_valid`
- `ready`

`worker_health` and `delivery_health` are Docker health states:
`healthy`, `starting`, `unhealthy`, `stopped`, `not_created`, or `unknown`.
`heartbeat_age` is the larger Poll/Delivery heartbeat age in seconds, rounded to
milliseconds, or null when either cannot be read.

`token_contract_valid` verifies:

- the protected host Token source is a valid regular file;
- neither controlled container receives the development Token environment value;
- each receives exactly the Token File environment path;
- each has one read-only bind from the validated host source.

`ready` is true only when both controlled containers are Docker-healthy, both heartbeats
are within the configured maximum age, and the Token Contract is valid. The status command
exits nonzero when `ready` is not true.

`token_contract_valid`, `worker_health`, and `delivery_health` are Controller fields,
not `/health/runtime` fields.

## Operator interpretation

1. Check `/health` for process liveness.
2. Check `/ready` for database/startup readiness.
3. Read `/health/runtime` and classify every degraded component or nonzero failure metric.
4. Read Controller `status` for the Poll/Delivery gate and Token Contract.
5. Compare aggregate counts and oldest ages with the release baseline.
6. Follow [Runtime recovery](runtime-recovery.md) for any ambiguous or failed state.

Do not print response bodies that may contain Admin archive data into broad-access logs.
The health surfaces above are designed to be redacted aggregate evidence.
