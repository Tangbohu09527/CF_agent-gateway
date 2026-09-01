# V2 production validation checklist

Use this checklist for release review and again for an authorized CFserver rollout.
Repository work does not complete the external steps automatically.

## Evidence labels

- **Implemented**: present in the release commit.
- **Locally tested**: exact command result is recorded in pull request #4.
- **GitHub Actions**: exact green run ID is recorded in pull request #4.
- **Not CFserver-validated**: requires the real deployment and remains open until the
  deployment owner signs it off.
- **External responsibility**: infrastructure or credential ownership outside this repo.
- **Manual action**: an operator must make and audit a decision.

Do not substitute PR #3's wrong-main test result for the V2 release evidence.

## Repository release gate

- [ ] Branch is based directly on `feat/v2-enterprise-runtime`; record base and head SHA.
- [ ] One linear Alembic chain reports head `20260823_04`.
- [ ] `python -m pytest -q` passes; record passed/skipped/warnings exactly.
- [ ] `ruff check .` passes.
- [ ] `ruff format --check .` passes.
- [ ] `git diff --check` passes.
- [ ] GitHub Actions quality, PostgreSQL/V2 runtime, and production Compose container E2E
  jobs are green; record run ID.
- [ ] Secret scan is green and no production value appears in the tree or logs.
- [ ] Pull request #4 documents PR #3 lineage, migration, rollback risk and
  the CFserver validation gap.

## External pre-deployment gate

- [ ] Change owner, maintenance window and rollback authority are recorded.
- [ ] Immutable image digest matches the approved release SHA.
- [ ] PostgreSQL backup identifier and tested restore procedure are recorded.
- [ ] Current Alembic revision and pre-migration row counts are captured.
- [ ] Gateway API, WeChat polling, dispatch and delivery services are stopped for the
  exclusive migration step.
- [ ] Database, agent-wechat and Hermes endpoints are verified without printing secrets.
- [ ] Separate Message API and Admin recovery tokens are injected from protected storage.
- [ ] Artifact storage is durable, shared where required, writable by the service user,
  and included in backup policy.
- [ ] Long-running application containers use `10001:10001`, a read-only root filesystem,
  and no added Linux capabilities.
- [ ] The heartbeat volume initializer is one-shot, has no network/Secrets, and is the only
  root container; no resident service runs as root.
- [ ] Debian host is `Asia/Shanghai`; containers, PostgreSQL and persistence remain UTC.

## Migration gate

- [ ] `alembic upgrade head` completes through the packaged tree.
- [ ] `alembic current --verbose` reports `20260823_04`.
- [ ] `alembic heads` reports exactly one head.
- [ ] Schema-head application check succeeds before any worker starts.
- [ ] Message, dispatch, response, delivery and checkpoint row counts match the expected
  non-destructive migration result.
- [ ] Admission outcome count equals Message count after backfill.
- [ ] Existing dispatch-backed Messages are completed `allowed` with matching
  identity/Workspace/AIThread targets.
- [ ] Legacy Messages without dispatch are completed `legacy_unresolved`, not allowed or
  pending, and replay does not evaluate current policy.
- [ ] Existing foreign keys and dispatch/delivery statuses remain unchanged.
- [ ] Existing nonzero checkpoints retain `last_local_id`, use generation zero, and do
  not receive a fabricated anchor.
- [ ] Any partial checkpoint-generation schema fails closed and is investigated.
- [ ] Recovery-audit UPDATE/DELETE is rejected by the PostgreSQL trigger.
- [ ] Illegal manual-retry status and retry/mark-dead/confirm-success audit tuples are
  rejected by database constraints.
- [ ] Downgrade is tested only on a backup/fixture; runtime admission, audit, or
  reconciliation evidence makes the relevant downgrade fail closed.

## Service startup gate

- [ ] Gateway `/health` returns liveness.
- [ ] Gateway `/ready` returns readiness.
- [ ] `/health/runtime` reports database and migration schema `ok`.
- [ ] WeChat, dispatch and delivery heartbeat files are distinct and fresh.
- [ ] Shared heartbeat directory is `10001:10001` mode `0750`; heartbeat files are mode
  `0600`; the Gateway heartbeat mount is read-only.
- [ ] A controlled Worker restart republishes a healthy heartbeat, and SIGTERM produces a
  clean zero exit with a final `stopped` heartbeat.
- [ ] WeChat auth is `logged_in` after authorized account login.
- [ ] Hermes configuration is present; worker liveness is not treated as connectivity.
- [ ] Before a business call, configured healthy idle Hermes reports
  `ok/no_recent_observation`; this is not claimed as connectivity success.
- [ ] A controlled Hermes operation changes connectivity to the expected recent success or
  failure observation without exposing request/response content.
- [ ] No unexplained stale running/delivery claims exist.

## Controlled end-to-end gate

This section is **Not CFserver-validated** until performed by the deployment owner with
test identities and approved content.

- [ ] One new WeChat message is seen and persisted exactly once.
- [ ] Admission produces one authoritative completed allow/deny outcome without content in
  logs.
- [ ] Replay after a policy change returns the original completed denial.
- [ ] Allowed input creates exactly one dispatch for the correct AIThread.
- [ ] Dispatch worker creates exactly one persisted response.
- [ ] Exactly one delivery outbox row and one outbound reply result.
- [ ] A self-originated echo advances its checkpoint without creating a Message.
- [ ] A repeated inbound event does not duplicate Message, dispatch, response or delivery.
- [ ] A second queued record on the same AIThread remains FIFO ordered.
- [ ] Different AIThreads can progress independently.

## Recovery drills

Use controlled fixtures or a non-production staging database. Never manufacture an
ambiguous production effect just to test recovery.

- [ ] Checkpoint 15 with visible 10/11/12 performs one CAS generation rewind.
- [ ] Empty visible window does not rewind.
- [ ] A serverId-less Message stores a content-free anchor; its second poll confirms
  continuity and processes later messages.
- [ ] Legacy nonzero checkpoint without anchor reports degraded until a serverId or
  content-free fallback anchor is safely enrolled and confirmed.
- [ ] An ambiguous checkpoint anchor stops the chat and emits only one identical warning
  per poller process state.
- [ ] The 21-chat/95-visible-message steady shape (non-empty counts 9/14/20/50/1/1,
  all inside checkpoint) emits at most six chat summaries plus one cycle summary on
  first/change, then zero repeated chat/cycle INFO while unchanged. Aggregate
  `messages_seen`, `messages_new`, `messages_duplicate`,
  `messages_skipped_checkpoint`, `messages_skipped_self`, and `messages_failed`
  counters remain exact; per-message checkpoint/self skips are DEBUG.
- [ ] Five persistent `stop_chat_visible_window_empty` Chats, with a new finite polling
  service each cycle and one shared lifecycle state, emit five continuity WARNINGs, at most
  five chat INFO summaries, and at most one cycle INFO on first/change. Four unchanged
  cycles add zero repeated WARNING/chat INFO/cycle INFO; changing one checkpoint emits
  exactly one additional WARNING/chat INFO and one cycle INFO.
- [ ] Removing a Chat from `list_chats` prunes its empty marker, pending window, history,
  and continuity observation. Account change and a new Worker lifecycle reset state, and
  temporary Chat churn cannot exceed the documented 1,024-Chat cache bound.
- [ ] A live admission claim fails closed; an expired claim recovers from its stored
  request snapshot.
- [ ] A crash between allowed-dispatch staging and outcome completion commits neither,
  then replay creates one outcome and one dispatch.
- [ ] An `uncertain` dispatch blocks later same-thread work and never auto-retries.
- [ ] `retry-approved` requires authenticated operator/reference/reason and resumes work.
- [ ] `mark-dead` releases later work without a fake response.
- [ ] `confirm-success` rejects missing/mismatched evidence and is idempotent with valid
  persisted response evidence.
- [ ] A response missing delivery is reconciled without another Hermes call or send.
- [ ] A poison reconciliation candidate is persistently deferred at 30/60/120/240 seconds,
  quarantined on failure five, and does not block a later valid candidate.
- [ ] Runtime Health reports reconciliation backlog/deferred/poison and oldest age.
- [ ] Concurrent recovery requests produce one CAS winner and one audit fact.
- [ ] ORM and direct SQL cannot UPDATE/DELETE recovery audit history or insert an illegal
  action/status/evidence tuple.
- [ ] Invalid auth, oversize body, control characters and secret-like recovery values do
  not mutate state.

## Observation window and sign-off

- [ ] Queue counts and oldest ages remain within site thresholds.
- [ ] No checkpoint regression/continuity warnings remain unexplained.
- [ ] No `uncertain`, blocked thread, stale lease or missing delivery remains unowned.
- [ ] Structured logs contain no message text, credential, cookie or connection string.
- [ ] Container timestamps are UTC and the dashboard renders `Asia/Shanghai` correctly.
- [ ] Rollback decision point and monitoring owner are recorded.
- [ ] CFserver, PostgreSQL, WeChat and Hermes owners sign off separately.

Retain the completed checklist with the deployment record, not in a commit containing
environment-specific identities or secrets.

## Evidence boundary

The repository unit/integration suite proves the state transitions above against controlled
SQLite and PostgreSQL fixtures when its recorded run is green. A green separate Actions
container job proves the production image/Compose topology with PostgreSQL 16 and synthetic
endpoints. Neither proves real
agent-wechat session behavior, Hermes execution, outbound WeChat delivery, production
database scale/locks, infrastructure restart policy, dashboards, or backup restoration.
Those checkboxes remain **Not CFserver-validated** until the external deployment owner
performs and records them.
