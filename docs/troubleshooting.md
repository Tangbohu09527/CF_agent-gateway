# Troubleshooting

## Safe inspection

Set only non-secret operator variables:

```bash
export RELEASE_DIR=/opt/cf-agent-gateway
export COMPOSE_FILE=docker-compose.prod.yml
export CONTROLLER="${RELEASE_DIR}/deploy/wechat-runtime-control"
export GATEWAY_URL=http://localhost:8080
cd "${RELEASE_DIR}"
```

Useful read-only checks:

```bash
docker compose -f "${COMPOSE_FILE}" --profile worker ps
sudo -n "${CONTROLLER}" status
curl --silent --show-error --max-time 3 "${GATEWAY_URL}/health"
curl --silent --show-error --max-time 3 "${GATEWAY_URL}/ready"
curl --silent --show-error --max-time 5 "${GATEWAY_URL}/health/runtime"
```

Preserve relevant structured logs in a protected evidence directory before container
recreation. Do not print the protected environment file, Token File, database URL,
Authorization header, message content, personal identity, or raw account/chat/conversation
ID.

## Fault table

| Symptom | Read-only checks | Likely classification | Safe action | Verification | Do not do |
| --- | --- | --- | --- | --- | --- |
| Message not ingested | Controller status; Poll heartbeat and last-cycle detail; `wechat_auth`; Checkpoint continuity; Message count delta; protected Poll logs | Gate closed; Poll stopped/stale; external session logged out; continuity failed closed; self/system/old Message filtered | Close gate if state is ambiguous. Restore external login/Token/dependency state. Start gated Workers only through Controller. For continuity, restore trustworthy history/Marker conditions and let the runtime decide | Controller ready; Poll heartbeat fresh; expected Checkpoint advances once; one authorized test Message persists once | Do not reset/delete the Checkpoint, edit local IDs, replay raw payloads manually, or expose message content |
| Admission denied | Authenticated Admin Message/Thread view; durable Admission Outcome reason/policy evidence; identity and route configuration references | Expected policy denial; unmapped Identity; group mention absent; Agent Profile/Group Type route unavailable; historical completed denial replay | Correct future identity/policy/route configuration through the owning process. Treat the completed outcome as authoritative for that Message | A new approved Message receives the expected result; the original completed denial remains unchanged on replay | Do not rewrite the old outcome, change the sender identity, or delete/reinsert the Message to force reevaluation |
| Hermes Dispatch queued | Dispatch heartbeat; Hermes configuration/connectivity; queued/running/failed/uncertain counts; oldest backlog; earlier same-Thread record | Worker stopped/stale; Hermes unavailable; FIFO blocked by earlier work; concurrency saturation | Restore Dispatch Worker or Hermes. Resolve any earlier `uncertain` record from evidence. Allow normal claim/FIFO logic to proceed | Dispatch heartbeat fresh; oldest backlog decreases; one Thread never has overlapping running work | Do not jump queue order, set status by SQL, or create a second Dispatch |
| Hermes Dispatch running too long | Lease timestamp and `stale_running`; Dispatch heartbeat; protected logs; external Hermes evidence | Active bounded call; stale lease after Worker loss; possible external effect | Let fenced lease recovery handle an expired running record. If outcome may be external/ambiguous, preserve evidence and expect `uncertain` | One claim token owns the terminal write; attempt count and status transition are consistent | Do not clear the lease, reuse a claim token, or manually invoke Hermes |
| Hermes Dispatch uncertain | Admin Dispatch inspection: response/evidence flags, blocking status, attempts, timestamps; external Hermes evidence | Possible external effect; same-Thread work intentionally blocked | Choose one authenticated action: `retry-approved` only after non-execution proof, `mark-dead`, or `confirm-success` with matching persisted evidence | One CAS winner, one immutable audit row, correct resulting status, no automatic retry | Do not auto-retry, paste assistant content into recovery, fabricate evidence, or delete the Dispatch |
| Response persisted but not delivered | `missing_delivery`; reconciliation backlog/deferred/poison; Delivery counts/heartbeat; Admin Delivery and response parts; Artifact state | Missing normalized Response/Delivery; deferred/quarantined reconciliation; Delivery Worker stopped; failed/uncertain channel send | Let reconciliation repair from persisted successful Dispatch evidence. Restore Delivery through Controller. Resolve Delivery ambiguity without another Hermes call | One Response/Delivery boundary, ordered parts, durable attempts/receipts, Dispatch remains success | Do not call Hermes again, delete the response, reset part ordinal, or resend an uncertain part manually |
| Worker heartbeat stale | Runtime components; Controller status for Poll/Delivery; Compose health; last operation/cycle detail; protected service logs | Worker process stopped; invalid/missing heartbeat; blocked external call; alive but failed last cycle | Preserve evidence. Poll/Delivery: Controller stop/start. Dispatch: approved Compose recreation after checking claims and ambiguity | Correct heartbeat file advances, runtime component returns ok, queue state remains consistent | Do not treat container existence as health or point two Workers at one heartbeat file |
| agent-wechat logged out | `wechat_auth`; Controller status; external owner's session check | External authenticated session lost, often after host reboot or session invalidation | Close Poll/Delivery Gate. Complete fresh QR through the external owner. Validate Token File contract. Open gate with Controller | `wechat_auth: logged_in`; Controller ready; controlled Message processed once | Do not leave Poll/Delivery running during fresh QR, expose QR/account/session data, or loosen Token File permissions |
| Controller `ready=false` | Exact five fields; Compose state; Poll/Delivery health; heartbeat age; protected Token source metadata | Controlled container not healthy; stale/missing heartbeat; invalid Token File mount/source; startup timeout | Controller stop; repair only the classified release/Token/session issue; Controller start and status | Both health fields healthy, heartbeat age fresh, Token Contract valid, ready true | Do not start controlled containers directly, add an ordinary user to the Docker group, or print the Token |
| Checkpoint continuity warning | Error code/action; generation; anchor-present flag; visible-window/Marker classification; Sink and mutation counters | Legacy anchor absent; ambiguous overlap; proven regression awaiting safe transition; Marker unavailable/invalid; CAS conflict | Keep affected chat fail closed. Restore trustworthy upstream window, session, and clock/Marker conditions. Allow code-managed enrollment/recovery | No Sink/mutation during ambiguity; proven continuity advances once; unchanged warning signature is deduplicated | Do not hand-edit Checkpoint/generation/fingerprint, infer continuity from message content, or delete history |
| Logs rotate too quickly | Docker log config for each service; host free space; actual protected daily volume; service count; current retention model | Wrong `max-size`/file count; traffic/log shape above model; disk pressure; unexpected routine logs | Preserve evidence, correct approved Compose settings, recalculate busiest-service retention, and redeploy through the normal release procedure | Every service uses `json-file` with `64m` x `10`; observed daily volume clears the required window and host capacity | Do not expand retention without disk review, copy logs to public storage, or treat logs as the only recovery authority |
| Database revision mismatch | `/ready`; runtime `migration_schema`; approved Alembic `current` and `heads`; release image identity | Migration job not run; wrong database/release; partial or unsupported schema; multiple heads | Close all application processing, verify target without printing URL, restore backup if required, run only the release migration service | One head `20260823_04`; Gateway/Workers in check mode; aggregate counts unchanged as expected | Do not stamp an unknown database, use `create_all`, run ad hoc DDL, delete evidence to permit downgrade, or start Workers |
| Failed deployment requires rollback | Current/previous release/image identity; schema compatibility; preserved health/log/queue evidence; backup identifier | Application incompatibility; migration failure; Controller contract failure; external dependency incident misclassified as release | Close gate, stop Gateway/Dispatch, preserve evidence, activate intact previous release and immutable image, prefer forward schema, verify before reopening gate | Previous release ready; schema accepted; queue/Checkpoint facts retained; external session ready; Controller ready | Do not reconstruct from a dirty tree, use `--remove-orphans` without topology review, delete rows, or claim a restore drill that was not run |

## Additional interpretations

### Runtime status is degraded but HTTP is 200

This is expected behavior. `/health/runtime` uses HTTP 503 only for database or migration
schema failure. A stale Worker, logged-out WeChat session, failed/uncertain queue item, or
missing Delivery produces top-level `degraded` with HTTP 200 so operators can read the
redacted diagnosis.

### Hermes says no recent observation

`connectivity: no_recent_observation` means no fresh operation result is available. It is
not proof of success and is not by itself a degraded status when Hermes is configured and
the Dispatch heartbeat is healthy. Use an approved controlled operation when current
connectivity proof is required.

### Queued work is nonzero

Queued or running work alone does not degrade runtime health. Compare oldest backlog age,
Worker freshness, FIFO blockers, and site thresholds. Zero is the accepted steady-state
production baseline recorded in [Production status](production-status.md), not a universal
requirement during active processing.

## Escalation evidence

An incident handoff should contain only protected, redacted references:

- release label, Git authority, and immutable image digest;
- Alembic revision;
- health/Controller status and aggregate counts/ages;
- stable error codes and hashed references already emitted by the runtime;
- exact safe actions taken and their timestamps;
- rollback/evidence directory references.

Use [Runtime recovery](runtime-recovery.md) for full decision flows and
[Production deployment](deployment/production.md#formal-rollback) for release rollback.
