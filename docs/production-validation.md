# Production validation

This document has two separate purposes:

1. the completed September 2026 production acceptance record;
2. a reusable unchecked checklist for future releases.

The current deployed facts are authoritative in
[Production status](production-status.md). A future checklist does not change that record
until a new release is actually accepted.

Repository `main` later advanced to `c5518aed12b90235f118ed81bb3cef75d0463443`
through the docs-only PR #8 merge. That merge did not rebuild or redeploy production, so
the completed acceptance record below retains its original production Release authority,
image, release label, database revision, rollback paths, and evidence run ID.

## Completed production acceptance record

### Candidate failure and rollback

The intermediate candidate `cd9990a` failed its authorized CFserver observation on
September 2, 2026. During 90 seconds it emitted 27 repeated continuity `WARNING` records,
27 failed Chat `INFO` summaries, and 27 failed Cycle `INFO` summaries for a stable
legacy-Checkpoint condition.

The candidate was rolled back in a controlled manner to the preserved pre-P1 release. The
historical rollback line was based on `7db3384`; it is not the current production
authority.

The root cause was lifecycle-state invalidation on a rejected/unavailable empty-window
Marker. That path cleared the continuity observation used to deduplicate an unchanged
failure signature, so every finite poll cycle treated the same condition as new.

Commit `6737636` preserved continuity observation across Marker-evidence rejection while
still invalidating untrusted Marker/history state. Commit
`f36c798294368263433f6132366ac9a864d9482b` completed the Marker-unavailable retention
model and became the production-validated image code snapshot.

### Shadow validation

The Stage-11G real legacy-Checkpoint shadow validation completed four cycles with:

- one total continuity `WARNING`;
- one total failed Chat `INFO` summary;
- one total failed Cycle `INFO` summary;
- zero Sink calls;
- zero Checkpoint mutation attempts;
- no candidate PostgreSQL connection.

This established the candidate's log lifecycle and fail-closed behavior without business
state changes.

### Merge and final production deployment

- [x] PR #7 merged into `main`.
- [x] Issue #6 was completed and closed.
- [x] Production Release Git authority is the PR #7 merge commit
  `b488cf452584e73bc9b752564bf90ea153aa8d18`.
- [x] Production image code snapshot is
  `f36c798294368263433f6132366ac9a864d9482b`.
- [x] No new P1 Git release tag was created.
- [x] Release label is `p1-observability-main-b488cf452584-20260903`.
- [x] Immutable image digest and local immutable tag were recorded.
- [x] Alembic current revision was `20260823_04`.
- [x] Long-running Gateway containers used `10001:10001`.
- [x] Docker logging used `json-file` with `64m` x `10` per service.
- [x] The Runtime Controller path and Token File contract were verified.

The exact image identity and paths are in
[Production status](production-status.md#authority).

### Final real-log acceptance

One real legacy Checkpoint target was present at Worker startup. The startup observation
emitted exactly one continuity `WARNING`, one failed Chat `INFO` summary, and one failed
Cycle `INFO` summary.

The subsequent 45-second steady observation produced:

- [x] zero duplicate continuity signatures;
- [x] zero repeated target continuity warnings;
- [x] zero repeated target failed Chat summaries;
- [x] zero routine idle/history Chat INFO;
- [x] zero repeated target failed Cycle summaries;
- [x] zero routine Cycle INFO;
- [x] zero poll-cycle-start INFO;
- [x] zero routine `httpx`, `httpcore`, or Alembic INFO;
- [x] zero ERROR;
- [x] zero validation violations.

Database and queue totals stayed consistent and unchanged during the controlled
observation.

### Final state

- [x] Gateway healthy.
- [x] Poll Worker healthy.
- [x] Dispatch Worker healthy.
- [x] Delivery Worker healthy.
- [x] PostgreSQL healthy.
- [x] External `agent-wechat` healthy.
- [x] Runtime Controller `ready: true`.
- [x] Token Contract valid.
- [x] Outstanding queue work zero.
- [x] Production online.
- [x] Existing authenticated `agent-wechat` Session preserved during the Gateway-only P1
  deployment; `agent-wechat` was not restarted or recreated.
- [x] Formal pre-P1 rollback release preserved.
- [x] Offline production image archive and checksum recorded.
- [x] Final evidence file and evidence run ID recorded.

The completed acceptance did not include general media understanding, automatic Skill
execution, general Provider routing, ERP behavior, Hermes implementation validation,
cross-repository deployment automation, or a database backup restoration drill.

## Completed evidence boundary

Repository code and automated tests establish state-machine behavior under controlled
fixtures. GitHub Actions establishes only the checks executed for a specific commit. The
CFserver record above adds real host, Compose, PostgreSQL, Worker, `agent-wechat` session,
Token Contract, queue, and log observations for the accepted release.

It does not transfer ownership of PostgreSQL, `agent-wechat`, Hermes, host backup systems,
network policy, or secrets into this repository. It also does not prove untested inbound
media/OCR/archive flows or an actual backup restore.

## Reusable future-release checklist

Keep future results in the release evidence record. Do not commit environment-specific
identities, message content, endpoints, credentials, or connection strings.

### Repository and CI

- [ ] Record the intended base, release commit, merge authority, and immutable image digest.
- [ ] Confirm the release commit is based on current `main`.
- [ ] Confirm one Alembic head and record it.
- [ ] Run `python -m ruff check .`.
- [ ] Run `python -m ruff format --check .`.
- [ ] Run `python -m pytest -q`.
- [ ] Run `git diff --check`.
- [ ] Confirm required GitHub Actions checks are green and record their run URLs/IDs.
- [ ] Confirm the tree and captured output contain no secrets or private production data.

### Pre-deployment

- [ ] Record change owner, window, acceptance owner, and rollback authority.
- [ ] Verify the previous release directory and immutable image/archive are intact.
- [ ] Record current release/image, health, Controller status, schema, aggregate counts,
  queue states, oldest ages, and Checkpoint continuity.
- [ ] Record the approved PostgreSQL backup identifier and restore procedure.
- [ ] Preserve protected logs before container recreation.
- [ ] Verify protected environment/configuration and Token File metadata without reading
  their values.
- [ ] Verify host capacity for database backup, images, Artifacts, and log retention.
- [ ] Classify every stale, failed, uncertain, blocked, poison, missing, or unverified item.

### Migration

- [ ] Close the Poll/Delivery Gate through the Runtime Controller.
- [ ] Stop Gateway and Dispatch Worker for the exclusive application window.
- [ ] Run only the release migration service.
- [ ] Confirm `alembic current` equals the packaged head and `alembic heads` returns one
  head.
- [ ] Confirm expected non-destructive aggregate counts and constraints.
- [ ] Confirm long-running services remain in migration check mode.
- [ ] Abort and restore/escalate on partial schema, unexpected head, or inconsistent data.

### Staged startup

- [ ] Start Gateway and Dispatch Worker while Poll/Delivery remain stopped.
- [ ] Confirm `/health`, `/ready`, and database/migration runtime components.
- [ ] Classify Dispatch/reconciliation state before opening intake.
- [ ] For a Gateway-only release that leaves `agent-wechat` untouched, verify the
  existing active Session without forcing a QR.
- [ ] After a CFserver/Debian reboot or `agent-wechat` recreation, formally stop the
  Poll/Delivery Gate, confirm both Workers are stopped, and complete a mandatory fresh QR.
- [ ] After an AI/Hermes host-only reboot with CFserver and `agent-wechat` untouched,
  restore Hermes without forcing a QR.
- [ ] Start Poll/Delivery only through the Runtime Controller.
- [ ] Confirm both controlled Docker health states, fresh heartbeat age, valid Token
  Contract, and `ready: true`.
- [ ] Confirm all three Worker heartbeats are distinct and fresh.

### Business and recovery validation

- [ ] Process one approved unique inbound Message through persistence, admission, Dispatch,
  response, and Delivery.
- [ ] Confirm duplicate inbound submission does not duplicate Message, Dispatch, Response,
  or Delivery.
- [ ] Confirm self-originated input advances Checkpoint without entering Message Store.
- [ ] Confirm same-Thread FIFO and independent progress across different Threads.
- [ ] Confirm the configured group Thread policy produces the intended sender isolation or
  deliberate sharing.
- [ ] Confirm an unexplained `uncertain` Dispatch never auto-retries.
- [ ] Confirm Admin recovery authentication, compare-and-swap, evidence requirements,
  idempotency, and immutable audit behavior in a controlled fixture.
- [ ] Confirm reconciliation repairs persisted success without another Hermes call.
- [ ] Confirm Delivery retries/receipts preserve ordered parts without changing Dispatch
  success.

### Checkpoint and log acceptance

- [ ] Exercise the release-relevant legacy/continuity shape in a controlled or approved
  observation.
- [ ] Confirm ambiguous continuity has zero Sink calls and zero Checkpoint mutations.
- [ ] Confirm an unchanged failure signature does not repeat Warning/Chat/Cycle summaries
  beyond the accepted lifecycle behavior.
- [ ] Confirm routine idle/history, cycle-start, HTTP client, and migration logs remain at
  the intended levels.
- [ ] Confirm no ERROR or privacy violation appears.
- [ ] Measure daily volume, recalculate the busiest-service retention window, and confirm
  host free space.
- [ ] Confirm every Compose service uses the approved `json-file` rotation.

### Reboot, observation, and sign-off

- [ ] Verify Gateway and all Workers recover after a controlled host/Docker restart.
- [ ] Do not assume the Poll/Delivery Gate remained stopped after CFserver reboot; run the
  formal Controller stop and confirm stopped before starting `agent-wechat`.
- [ ] Confirm `agent-wechat` did not auto-start (`restart="no"`) and complete a
  mandatory fresh QR after every CFserver/Debian reboot or container recreation.
- [ ] Confirm Gateway-only deployment preserves an untouched active Session, while an
  AI/Hermes host-only reboot does not trigger a QR.
- [ ] Observe queue/database totals and oldest ages for the approved steady window.
- [ ] Confirm no unexplained stale claim, `uncertain`, blocked Thread, missing Delivery,
  reconciliation poison, or continuity warning remains.
- [ ] Record final release/image/schema, evidence directory, rollback directory/image, and
  acceptance time.
- [ ] Obtain separate sign-off from Gateway, database, `agent-wechat`, Hermes, and host
  operations owners as applicable.

Use [Production deployment](deployment/production.md) for the procedure and
[Runtime recovery](runtime-recovery.md) for failed acceptance.
