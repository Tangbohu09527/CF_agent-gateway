# V2 production validation checklist

Use this checklist for release review and again for an authorized CFserver rollout.
Repository work does not complete the external steps automatically.

## Evidence labels

- **Implemented**: present in the release commit.
- **Locally tested**: exact command result is recorded in the replacement pull request.
- **GitHub Actions**: exact green run ID is recorded in the replacement pull request.
- **Not CFserver-validated**: requires the real deployment and remains open until the
  deployment owner signs it off.
- **External responsibility**: infrastructure or credential ownership outside this repo.
- **Manual action**: an operator must make and audit a decision.

Do not substitute PR #3's wrong-main test result for the V2 release evidence.

## Repository release gate

- [ ] Branch is based directly on `feat/v2-enterprise-runtime`; record base and head SHA.
- [ ] One linear Alembic chain reports head `20260823_02`.
- [ ] `python -m pytest -q` passes; record passed/skipped/warnings exactly.
- [ ] `ruff check .` passes.
- [ ] `ruff format --check .` passes.
- [ ] `git diff --check` passes.
- [ ] GitHub Actions quality and PostgreSQL/V2 runtime jobs are green; record run ID.
- [ ] Secret scan is green and no production value appears in the tree or logs.
- [ ] Replacement pull request documents PR #3 lineage, migration, rollback risk and
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
- [ ] Debian host is `Asia/Shanghai`; containers, PostgreSQL and persistence remain UTC.

## Migration gate

- [ ] `alembic upgrade head` completes through the packaged tree.
- [ ] `alembic current --verbose` reports `20260823_02`.
- [ ] `alembic heads` reports exactly one head.
- [ ] Schema-head application check succeeds before any worker starts.
- [ ] Message, dispatch, response, delivery and checkpoint row counts match the expected
  non-destructive migration result.
- [ ] Existing foreign keys and dispatch/delivery statuses remain unchanged.
- [ ] Existing nonzero checkpoints retain `last_local_id`, use generation zero, and do
  not receive a fabricated anchor.
- [ ] Any partial checkpoint-generation schema fails closed and is investigated.

## Service startup gate

- [ ] Gateway `/health` returns liveness.
- [ ] Gateway `/ready` returns readiness.
- [ ] `/health/runtime` reports database and migration schema `ok`.
- [ ] WeChat, dispatch and delivery heartbeat files are distinct and fresh.
- [ ] WeChat auth is `logged_in` after authorized account login.
- [ ] Hermes configuration is present; worker liveness is not treated as connectivity.
- [ ] A controlled Hermes operation changes connectivity from `unverified` to the
  expected observed result without exposing request/response content.
- [ ] No unexplained stale running/delivery claims exist.

## Controlled end-to-end gate

This section is **Not CFserver-validated** until performed by the deployment owner with
test identities and approved content.

- [ ] One new WeChat message is seen and persisted exactly once.
- [ ] Admission produces the expected allow/deny decision without content in logs.
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
- [ ] Legacy nonzero checkpoint without anchor reports degraded until safely enrolled.
- [ ] An `uncertain` dispatch blocks later same-thread work and never auto-retries.
- [ ] `retry-approved` requires authenticated operator/reference/reason and resumes work.
- [ ] `mark-dead` releases later work without a fake response.
- [ ] `confirm-success` rejects missing/mismatched evidence and is idempotent with valid
  persisted response evidence.
- [ ] A response missing delivery is reconciled without another Hermes call or send.
- [ ] Concurrent recovery requests produce one CAS winner and one audit fact.
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
