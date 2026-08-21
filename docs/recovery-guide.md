# Recovery Guide

This guide covers resident Worker restart, normal retry, checkpoint regression, and
ambiguous Hermes or reply-delivery failures. It assumes the incident boundary has already
been identified with [troubleshooting.md](troubleshooting.md).

## Recovery invariants

Keep these constraints true throughout an incident:

1. Run at most one resident polling Worker for a shared Gateway database.
2. Preserve the Gateway database before changing checkpoints, operation state, schema, or
   application version.
3. Treat a stale lease or network timeout as "result unknown", not "external call failed".
4. Keep correlation identifiers and operation records. Do not delete a Message to force a
   replay.
5. Verify recovery with one controlled message before returning the Worker to unattended
   operation.

## Resident Worker restart

The checkpoint, Message, workspace/thread binding, operation ledger, and worker runtime
state are persisted. The process itself owns no queue that must be reconstructed in memory.

1. Stop the deployment supervisor from immediately starting another copy.
2. Confirm no one-cycle command or second resident Worker is running against the same
   Gateway database.
3. Capture logs, `/health`, the affected checkpoint, and dispatch/delivery operation rows.
4. Correct a fatal cause such as invalid configuration, an incompatible database schema, a
   missing credential variable, or deterministic client-construction failure. Correct
   transient database or endpoint reachability separately; those use bounded retry.
5. Start one resident Worker under its supervisor:

   ```bash
   python -m cf_agent_gateway.runtime.worker
   ```

6. If the former process was killed, wait for or explicitly verify expiry of its durable
   resident lease before replacement. Do not bypass a fresh lease.
7. Confirm a fresh worker heartbeat and a completed cycle in `/health` and JSON logs.
8. Verify that completed per-message operations were not repeated and that failed or stale
   work followed the expected retry path.

The Worker handles `SIGINT` and `SIGTERM` gracefully. An in-progress synchronous external
call is allowed to return and cleanup completes before shutdown. A forced kill can leave an
operation `in_progress` until its lease is stale.

Messages whose checkpoint was committed do not normally replay. A message whose sink
succeeded but checkpoint write failed is replayed; the physical source-message unique key
and durable operation state are the duplicate-suppression boundary.

## Normal retry recovery

A thrown non-fatal cycle failure or a returned degraded `PollResult` increments the same
consecutive-failure count. The resident loop uses exponential delay starting at
`runtime.polling_interval_seconds` and capped by
`runtime.polling_retry_max_seconds`; only a healthy returned cycle resets the count. Fatal
startup/configuration failures exit instead of retrying forever.

For a failed message:

1. Find its polling stage and error code.
2. Confirm the conversation checkpoint did not advance past the failed `localId`.
3. Correct the dependency or data problem.
4. Leave one Worker running and observe the next cycle.
5. Confirm Message Store still contains one physical message and that each succeeded
   external operation remains single.

Do not reduce the polling interval or retry cap aggressively during an outage. Hermes has
no internal retry or circuit breaker; repeated degraded results intentionally increase the
Worker delay up to the configured cap.

### Failed and stale operation claims

Dispatch and delivery records use a 120-second lease so process death does not leave work
permanently owned. Apply this decision order:

1. `succeeded`: do not replay the external call.
2. `in_progress` with an unexpired lease: do not take it from the current owner.
3. `failed`: correct the recorded cause, then allow the bounded pre-poll recovery sweep to
   claim it without requiring source replay.
4. `in_progress` with an expired lease: determine whether the external system accepted the
   original request before allowing a reclaim.

Transport errors, timeouts, invalid successful responses, HTTP 408/429, and 5xx responses
remain `in_progress` for the full lease because the remote side may have completed the
call. Once stale, the bounded pre-poll recovery sweep automatically attempts a claim; stop
the Worker before expiry if operator reconciliation must happen first. Definite failures
are marked `failed` and are eligible for the same sweep without requiring the source
message to remain visible.

The operation lease is currently a code constant rather than a configuration setting.
Lease expiry provides crash recovery, not proof of exactly-once behavior. If Hermes accepted
a prompt but Gateway died before recording success, reclaim can execute the prompt twice.
The same ambiguity applies to a WeChat send accepted immediately before process death.

## Automatic checkpoint-regression recovery

No operator action is normally required when a rebuilt WeChat session restarts `localId`
below the stored checkpoint.

Given this state:

```text
stored checkpoint: 15
visible localIds:   10, 11, 12
```

the runtime:

1. logs `wechat checkpoint regression detected`;
2. atomically compare-and-set updates the checkpoint from `15` to `9`
   (`min(visible)-1`) and increments `regression_generation`;
3. logs `wechat checkpoint recovered` with `new_checkpoint=9` and the new
   `regression_generation`;
4. processes `10`, `11`, and `12` in ascending order;
5. advances the checkpoint normally after each successful sink completion.

Each successful advance also stores a SHA-256 fingerprint for that checkpoint message. If
the rebuilt session has already grown beyond the old checkpoint, for example old checkpoint
`15` with a new visible window `1..20`, the runtime compares the new message at local ID
`15` to the stored fingerprint. A mismatch triggers the same recovery and rewinds to
`0`, so `1..20` remain eligible. The fingerprint is one-way recovery metadata and does
not contain message plaintext.

If the compare-and-set loses a race, the conversation returns a checkpoint failure. Keep
only one resident Worker for the shared database and let the next cycle re-read
authoritative state. The runtime never treats the old high value as proof that the new
visible window is already processed.

Recovery intentionally replays the whole visible window. Stable source-message identity
deduplicates Messages and durable succeeded operation records suppress repeated completed
side effects.

Stable adapter `serverId` identity is unchanged by recovery. When it is absent, generation
zero retains the legacy `local:v1` fallback identity and a detected regression switches to
a generation-scoped `local:v2` identity. Reused local IDs in the rebuilt session therefore
do not collide with Messages or operation records from the earlier generation. Preserve
checkpoint and log evidence anyway: the path remains at least once, and an undetected reset
cannot be inferred from an empty adapter window.

The adapter exposes no session epoch. If a rebuilt counter has overtaken the checkpoint and
the old checkpoint local ID has already fallen outside the visible window, Gateway cannot
prove the reset from that window alone. Preserve a retention window large enough to include
the checkpoint anchor and alert on extended Worker downtime.

### Empty remote windows

An empty window has no `remote_latest_local_id`, so automatic regression detection cannot
run. First verify adapter login, conversation discovery, and adapter retention/window
behavior. Do not set the checkpoint to zero based only on an empty response.

### Manual checkpoint intervention

The repository has no supported checkpoint repair CLI or HTTP endpoint. Direct row edits
are an exceptional, reviewed database operation. Before considering one:

1. stop all Gateway writers and one-cycle commands;
2. take and verify a consistent database backup;
3. preserve the old row and the adapter evidence establishing the desired replay window;
4. assess whether Hermes or WeChat side effects already happened for messages in that
   window;
5. use a reviewed, transactional compare-and-set update appropriate to the configured
   database;
6. start one Worker and watch the first cycle before resuming normal supervision.

Never delete all checkpoints as a routine fix. With `latest` bootstrap, deletion can skip
the currently visible history; with `backfill`, it can replay a much larger window.

## Hermes recovery

### Definite failure

For a validated non-2xx response other than HTTP 408/429/5xx, or another definite local
failure:

1. correct endpoint, credential, model, response contract, or Hermes availability;
2. confirm the operation is `failed`, not freshly leased by another owner;
3. let the one resident Worker reclaim it through the bounded recovery sweep;
4. confirm the Hermes session binding and dispatch record become successful;
5. continue to delivery verification.

### Ambiguous timeout or crash

For a timeout, lost connection, or stale `in_progress` operation:

1. ask the Hermes operator to search by session ID and incident time;
2. determine whether the prompt completed and whether a response can be recovered;
3. prefer reconciling an accepted result over blindly dispatching again;
4. if replay is approved, record the duplicate-execution risk in the incident;
5. watch delivery and the AIThread session binding after reclaim.

Gateway does not send a general upstream idempotency key. A deterministic Hermes session ID
maintains context but does not by itself make repeated prompts exactly once.

## Reply-delivery recovery

For a definite adapter rejection, correct adapter login/connectivity or the source binding,
then allow the failed delivery to retry. For an ambiguous timeout or stale delivery lease:

1. inspect the source conversation for the expected reply around the incident time;
2. ask the adapter operator whether the send request was accepted;
3. do not retry a delivery already recorded as `succeeded`;
4. stop the Worker before the 120-second lease expires if reconciliation needs more time;
5. otherwise accept that stale replay reclaims automatically and can duplicate a reply;
6. verify the reply lands in the original source account and physical conversation.

Do not create a replacement AIThread or source binding merely to send a reply. A binding
mismatch is a data-integrity signal and should be investigated before recovery.

## Database and schema recovery

For SQLite, stop every HTTP and Worker writer before taking or restoring the database file.
Copying a live SQLite file is not the documented consistency procedure. For PostgreSQL, use
the database service's consistent backup and restore tooling.

`create_all` initializes missing objects but is not a schema migration system. Apply the
reviewed SQLite or PostgreSQL hardening script described in
[migrations/README.md](../migrations/README.md), or recreate only disposable development
data. The service never automatically deletes or migrates `gateway.db`. The supplied
scripts fail before persistent schema changes when a legacy checkpoint is nonzero; an
operator must provide a reviewed anchor migration or explicitly approve and prepare a
replay. Startup rejects an unanchored nonzero generation-zero checkpoint and incomplete
runtime metadata.

After restore:

1. start the HTTP process and verify database health;
2. inspect operation leases because restored timestamps may describe work from before the
   restore point;
3. run a database-backed Message API read/write check if that API is deployed;
4. start exactly one Worker;
5. observe checkpoint recovery, operation claims, and one controlled round trip.

See [deployment.md](deployment.md) for backup, restore, upgrade, and rollback boundaries.

## Recovery completion checklist

- `/health` reports an available database and a fresh Worker.
- The latest cycle completed without an unexplained failure.
- No second resident poller owns the same shared Gateway database.
- The affected source message maps to one Gateway Message.
- Checkpoint state matches the successfully processed visible sequence.
- Dispatch and delivery records have no unexplained failed or stale operation.
- One controlled WeChat message completed admission, Hermes dispatch, and reply delivery.
- The incident records any ambiguous or intentionally repeated external side effect.
