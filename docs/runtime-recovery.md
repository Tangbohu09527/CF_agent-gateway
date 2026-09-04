# Runtime recovery

## Recovery rule

Use the same sequence for every production incident:

1. **Check** only redacted health, Controller status, Compose state, protected logs, and
   durable aggregate/Admin evidence.
2. **Classify** whether the failure is liveness, external dependency, authentication,
   Checkpoint continuity, Dispatch ambiguity, Delivery ambiguity, schema, or release.
3. **Operate** at the narrowest owned boundary. Close the Poll/Delivery Gate before any
   `agent-wechat` login or ambiguous intake work.
4. **Verify** health, heartbeats, queue totals, oldest ages, continuity, and absence of new
   duplicate/uncertain effects.
5. **Rollback** to an intact previous release when the current release cannot be made safe
   inside the approved incident window.

Never delete database rows to repair a queue. Never hand-edit a Checkpoint, generation, or
fingerprint to bypass fail-closed continuity. Never invent a Hermes response or delivery
receipt.

## Operator variables

The protected incident evidence directory must be created and permissioned by the site
process before recovery begins. Start the interactive administrative session once with
`sudo -v`; every later privileged command uses `sudo -n`.

```bash
sudo -v

export RELEASE_DIR="/opt/cf-agent-gateway"
export COMPOSE_FILE="${RELEASE_DIR}/docker-compose.prod.yml"
export CF_GATEWAY_ENV_FILE="${CF_GATEWAY_ENV_FILE:-${RELEASE_DIR}/.env}"
export CF_GATEWAY_CONFIG_FILE="${CF_GATEWAY_CONFIG_FILE:-${RELEASE_DIR}/config/production.yaml}"
export CONTROLLER="${RELEASE_DIR}/deploy/wechat-runtime-control"
export GATEWAY_URL="http://127.0.0.1:8080"
export EVIDENCE_DIR="/path/to/protected/incident-evidence"

COMPOSE=(
  sudo -n env
  "CF_GATEWAY_ENV_FILE=${CF_GATEWAY_ENV_FILE}"
  "CF_GATEWAY_CONFIG_FILE=${CF_GATEWAY_CONFIG_FILE}"
  docker compose
  --project-directory "${RELEASE_DIR}"
  --env-file "${CF_GATEWAY_ENV_FILE}"
  -f "${COMPOSE_FILE}"
  --profile worker
)

sudo -n test -d "${EVIDENCE_DIR}"
sudo -n test -w "${EVIDENCE_DIR}"
sudo -n test -r "${COMPOSE_FILE}"
sudo -n test -r "${CF_GATEWAY_ENV_FILE}"
sudo -n test -r "${CF_GATEWAY_CONFIG_FILE}"
sudo -n test -x "${CONTROLLER}"
```

Do not print the protected environment file, Token File, database URL, Authorization
header, message content, personal identity, or raw channel identifiers. Do not source the
env-file. Do not replace the `COMPOSE` array with an ordinary-user Docker command.

## Preserve evidence first

Before restarting or recreating a container:

```bash
date -u
"${COMPOSE[@]}" ps
sudo -n "${CONTROLLER}" status --timeout-seconds 30
curl --silent --show-error --max-time 5 "${GATEWAY_URL}/health/runtime"
```

Do not create the protected directory from this Runbook. Store command output only in the
pre-provisioned incident location. Preserve the relevant
structured container logs with the site's approved redaction and access controls. Record:

- active release and immutable image identity;
- Alembic revision;
- aggregate Message/Admission/Dispatch/Response/Delivery/Checkpoint counts;
- status counts and oldest ages;
- current Controller output and heartbeat timestamps;
- the incident/change reference.

Container recreation can replace the immediately accessible log window and heartbeat file.
Database facts remain authoritative, but losing logs may remove timing evidence.

## Controller not ready

**Check**

- Run Controller `status`.
- Read `/health/runtime`.
- Inspect `worker` and `delivery-worker` state with Compose.
- Preserve both controlled-service logs.

**Classify**

- `token_contract_valid: false`: protected Token source or container mount/environment
  contract is invalid.
- health `starting`: startup is still inside its bounded readiness period.
- health `unhealthy`, `stopped`, or `not_created`: controlled container failure.
- null/stale `heartbeat_age`: Worker is not publishing a valid fresh heartbeat.

**Operate**

1. Keep the gate closed with
   `sudo -n "${CONTROLLER}" stop --timeout-seconds 30` and require
   `{"stopped":true}`.
2. Correct only the external Token File metadata/source or approved release configuration;
   never reveal the value.
3. Confirm the external `agent-wechat` session is ready.
4. Run `sudo -n "${CONTROLLER}" start --timeout-seconds 180`, then
   `sudo -n "${CONTROLLER}" status --timeout-seconds 30`.

**Verify**

`worker_health` and `delivery_health` are healthy, heartbeat age is fresh,
`token_contract_valid` and `ready` are true, and runtime health has no unexplained queue
state.

**Rollback**

If Controller preparation, launch, readiness, or rollback fails repeatedly for the release,
keep the gate closed and follow [Formal release rollback](#formal-release-rollback).

## Poll Worker stopped

**Check**

Controller status is authoritative for gated Worker readiness. Confirm whether the Poll
container is stopped/unhealthy or merely has a stale/invalid heartbeat. Read the last
Checkpoint and Message aggregate changes through approved read-only evidence.

**Classify**

- No new Messages and a stale Poll heartbeat: Poll liveness failure.
- Poll healthy but `wechat_auth: logged_out`: external session failure.
- Poll healthy with a continuity warning: fail-closed chat-level continuity, not general
  Worker death.

**Operate**

Preserve evidence, close the gate, resolve Token/session/dependency state, then use
Controller `start`. Do not start the Poll container directly.

**Verify**

Controller readiness, fresh Poll heartbeat, expected Checkpoint behavior, stable aggregate
counts, and one controlled intake test when authorized.

**Rollback**

If the Poll Worker from the current release cannot become ready without unsafe state
changes, keep the gate closed and roll back the application release.

## Delivery Worker stopped

**Check**

Read Controller status, Delivery heartbeat, `delivery` runtime metrics, oldest backlog
age, and protected Delivery logs. Distinguish queued work from `uncertain` sends.

**Classify**

- queued backlog with stopped/stale Worker: Delivery liveness failure;
- `failed`: definite terminal/provider/Artifact failure;
- `uncertain`: possible external send whose local result is ambiguous;
- `missing_delivery`: reconciliation is required from persisted successful Dispatch
  evidence.

**Operate**

Preserve evidence and use the Controller stop/start cycle for Poll and Delivery together.
Do not reset `next_part_ordinal`, delete attempts, or resend an `uncertain` part by hand.

**Verify**

Delivery heartbeat is fresh; backlog advances in order; attempts/receipts remain consistent;
no second Hermes call occurs; no duplicate outbound effect is observed.

**Rollback**

If the release cannot process known-safe queued work, close the gate and use the previous
application release only after verifying schema compatibility and retained Delivery state.

## Hermes unreachable

**Check**

Read Dispatch heartbeat, Hermes component `configuration` and `connectivity`, Dispatch
status counts, oldest backlog, stale-running count, and protected Dispatch logs.

**Classify**

- `unconfigured`: release configuration/secret injection failure;
- `last_operation_failed`: a fresh failed observation;
- `no_recent_observation`: no current connectivity proof, not itself a failure;
- growing `failed`: definite retryable failures;
- any `uncertain`: possible external effect requiring evidence-based manual resolution.

**Operate**

Close the Poll/Delivery Gate if intake should not grow. Restore the external Hermes service
or approved configuration. Let definite retryable records follow the durable retry budget.
Use Admin recovery only for an `uncertain` Dispatch after the external owner proves the
outcome.

**Verify**

A controlled Hermes operation produces the expected fresh observation, backlog advances
FIFO, and no `uncertain` item is automatically retried.

**Rollback**

Application rollback is appropriate only when the Gateway release caused incompatibility.
Do not roll back database state or retry ambiguity merely because the external Hermes
service is unavailable.

## agent-wechat offline after host reboot

**Check**

After a CFserver/Debian reboot, do not infer Session state from the old heartbeat or
container metadata. `agent-wechat` uses `restart="no"`, does not auto-start, and its old
Session does not automatically become active. Poll and Delivery may have restarted under
their own Docker policy, so first close the gate.

**Classify**

- CFserver/Debian reboot: fresh QR is required after the formal gate stop;
- `agent-wechat` container recreation: fresh QR is required after the formal gate stop;
- Gateway-only deployment with `agent-wechat` untouched: the active Session can remain;
- AI/Hermes host-only reboot with CFserver and `agent-wechat` untouched: no fresh QR;
- Token Contract invalid: Gateway-controlled mount/source contract, not QR state.

**Operate**

1. Run `sudo -n "${CONTROLLER}" stop --timeout-seconds 30`.
2. Require `{"stopped":true}` before starting `agent-wechat` or presenting a QR.
3. Let the external owner start `agent-wechat` and complete the fresh-QR process without
   exposing account/session evidence.
4. Verify the protected Token File contract.
5. Run `sudo -n "${CONTROLLER}" start --timeout-seconds 180`, then
   `sudo -n "${CONTROLLER}" status --timeout-seconds 30`.

**Verify**

Controller ready is true, runtime `wechat_auth` is `logged_in`, heartbeats are fresh, and
the controlled acceptance Message is processed once.

**Rollback**

A mandatory post-reboot QR is not a Gateway rollback reason. Roll back only if the
release's contract cannot operate after the external service has a new active Session.

## Pending or uncertain queue work

**Check**

Use runtime aggregate fields and authenticated Admin inspection. Record the Dispatch status,
attempts, lease, evidence flags, blocking status, and related delivery facts without
copying message content.

**Classify**

- pending Admission with an expired lease: recoverable from the stored request snapshot;
- running Dispatch with expired lease: reclaimable under the fenced retry rules;
- `failed`: definite failure governed by retry budget;
- `uncertain`: external effect unknown and same-Thread work blocked;
- `dead`: terminal Dispatch;
- Delivery `uncertain`: channel effect unknown and not equivalent to Dispatch ambiguity.

**Operate**

Allow automatic fenced recovery only where the state machine permits it. For Dispatch
`uncertain`, choose exactly one authenticated Admin action based on external evidence:
`retry-approved`, `mark-dead`, or `confirm-success`.

**Verify**

One compare-and-swap winner, one immutable audit fact, stable idempotency, expected same-
Thread release/blocking, and no fabricated response/delivery.

**Rollback**

Release rollback does not erase ambiguous queue work. The previous application must read
the retained state correctly; otherwise keep the current schema/application stopped and
escalate the evidence decision.

## Stale heartbeat

**Check**

Determine which of Poll, Dispatch, or Delivery heartbeat is missing, stale, or invalid.
Compare container health, process state, last operation details, and queue movement.

**Classify**

- Poll/Delivery heartbeat: gated Worker incident;
- Dispatch heartbeat: Dispatch Compose service incident, outside Controller control;
- fresh heartbeat with failed-cycle detail: Worker is alive but degraded.

**Operate**

For Poll/Delivery, preserve evidence and use Controller stop/start. For Dispatch, use the
approved Compose release procedure to recreate/start only `dispatch-worker` after
preserving evidence and checking for running/uncertain claims.

**Verify**

The correct heartbeat file advances, state becomes healthy, old claims follow fencing
rules, and queue movement is consistent.

**Rollback**

Roll back when the current Worker repeatedly cannot publish a valid heartbeat or safely
resume its durable state.

## Checkpoint continuity failed closed

**Check**

Record the redacted continuity error code/action, Checkpoint generation, whether an anchor
exists, visible-window shape, and whether the failure signature repeats. Do not record
message content or raw IDs.

**Classify**

- legacy nonzero Checkpoint without anchor;
- ambiguous/missing saved anchor in the visible window;
- proven local-ID regression;
- concurrent Checkpoint compare-and-swap conflict;
- empty-window Marker unavailable or invalid.

**Operate**

Keep the affected chat fail closed. Restore trustworthy upstream history/time/session
conditions and allow the runtime to enroll or confirm an anchor through its implemented
rules. If the external session is being rebuilt, close the Poll/Delivery Gate first.

**Verify**

No Sink call or Checkpoint mutation occurred during ambiguity; a subsequent proven window
advances exactly once; identical steady failure evidence remains deduplicated.

**Rollback**

Application rollback may restore previous code behavior but must retain the authoritative
Checkpoint row. Never rewind, delete, or fabricate the fingerprint to force progress.

## Empty-window Marker unavailable

**Check**

Confirm the visible window is empty and the persisted Marker is missing, mismatched,
expired, or based on untrusted/backwards time.

**Classify**

This is a continuity-evidence failure, not proof that history disappeared and not
permission to reset the cursor.

**Operate**

Leave the chat stopped. Restore trusted clock/Marker persistence or wait for a trustworthy
visible overlap according to the incident plan. A Worker restart resets in-memory log
deduplication but does not create continuity evidence.

**Verify**

Zero Sink calls, zero Checkpoint mutation attempts, zero business inserts, and bounded
warning/summary output while the signature is unchanged.

**Rollback**

Do not restore service by editing the Checkpoint. Use release rollback only for a verified
software regression and retain all continuity evidence.

## Duplicate or replay suspicion

**Check**

Close the Poll/Delivery Gate if another effect is possible. Preserve Message uniqueness,
Admission Outcome, Dispatch idempotency key/status, Hermes response, Response parts,
Delivery attempts/receipts, and Checkpoint facts through approved read-only inspection.

**Classify**

- duplicate inbound event resolved to one existing Message;
- repeated admission replay using one completed outcome;
- reclaimed Dispatch protected by upstream idempotency;
- ambiguous Hermes effect (`uncertain`);
- ambiguous channel effect (Delivery `uncertain`);
- confirmed external duplicate.

**Operate**

Resolve only the authoritative ambiguous state. Do not delete the later row, reset an
attempt counter, rewrite a receipt, or resend from a manual client.

**Verify**

Unique Message/Dispatch/Response/Delivery boundaries remain intact, external effect count is
understood, and later same-Thread work is released only after the correct resolution.

**Rollback**

Rollback does not remove an already-created duplicate. Preserve evidence and ensure the
previous application honors the same idempotency and audit boundaries.

## Formal release rollback

1. Close the Poll/Delivery Gate.
2. Stop Dispatch Worker and Gateway through the approved Compose command.
3. Preserve logs, health, queue/database aggregates, Controller status, image identity, and
   incident reference.
4. Select an intact previous release directory and immutable image.
5. Prefer application rollback with the current forward-compatible schema.
6. Restore a pre-upgrade database into a separately verified target only when an older
   schema is mandatory and an approved restore procedure exists.
7. Start Gateway and Dispatch Worker with the gate closed.
8. Verify readiness, schema, queue compatibility, and external session.
9. Open the gate through the previous release Controller and complete rollback acceptance.

See [Production deployment](deployment/production.md#formal-rollback) for the full release
procedure and [Production status](production-status.md#rollback-and-evidence) for the
current release's preserved rollback material.
