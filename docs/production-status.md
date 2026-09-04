# Production status

## Authority

Status: **production online**

This is the single authoritative record for the current CF_agent-gateway production
release. It records completed acceptance facts; it is not a generic deployment runbook.

| Field | Authority or status value |
| --- | --- |
| Repository status updated | `2026-09-04` |
| Repository branch authority | `main` |
| Documentation closeout baseline | PR #8 merge commit `c5518aed12b90235f118ed81bb3cef75d0463443` |
| PR #8 baseline CI | Run `33853221731` succeeded |
| Live repository tip | Query GitHub or `git rev-parse origin/main`; intentionally not hard-coded |
| Deployment date | September 3, 2026 |
| Repository | `Tangbohu09527/CF_agent-gateway` |
| Release label | `p1-observability-main-b488cf452584-20260903` |
| Production Release Git authority | `b488cf452584e73bc9b752564bf90ea153aa8d18` |
| Validated image code snapshot | `f36c798294368263433f6132366ac9a864d9482b` |
| Production image digest | `sha256:b9341ca7df6f952b4d81028c497574c1e22478e4408f98791a28bd9514b215f1` |
| Local immutable image tag | `cf-agent-gateway:p1-observability-main-b488cf452584-20260903` |
| Alembic revision | `20260823_04` |
| Runtime Controller | `/opt/cf-agent-gateway/deploy/wechat-runtime-control` |
| Long-running application user | `10001:10001` |
| Gateway log policy | Docker `json-file`, `64m` x `10` files per Compose service |

PR #7 is merged and Issue #6 is closed. Its merge commit is the current Production Release
Git authority; no new P1 Git release tag was created. The validated image code snapshot is
the PR head used to build the production image and is an ancestor of that release commit.

PR #8 subsequently merged documentation only. Its merge commit,
`c5518aed12b90235f118ed81bb3cef75d0463443`, is the documentation closeout baseline, not
a permanently current repository `main` tip. The live tip changes as later pull requests
merge and must be queried from GitHub or with `git rev-parse origin/main`.

No production redeployment occurred: PR #8 did not rebuild the production image, change
Alembic revision `20260823_04`, change the release label, or create a production tag.
Current production remains defined by Release Git authority
`b488cf452584e73bc9b752564bf90ea153aa8d18`, validated image code snapshot
`f36c798294368263433f6132366ac9a864d9482b`, and image digest
`sha256:b9341ca7df6f952b4d81028c497574c1e22478e4408f98791a28bd9514b215f1`.
Repository `main` advancing does not mean production was redeployed.

## Accepted production state

After acceptance, Gateway, Dispatch Worker, Poll Worker, Delivery Worker, PostgreSQL, and
external `agent-wechat` were healthy. Runtime Controller `ready` was `true`, the Token
Contract was valid, outstanding queue work was zero, production remained online, and the
authenticated `agent-wechat` Session was preserved during the Gateway-only P1 deployment.
The deployment did not restart or recreate `agent-wechat`; this fact does not imply
Session survival after a CFserver/Debian reboot.

PostgreSQL and `agent-wechat` are operationally external to the Gateway Compose lifecycle.
Hermes is also an external execution service; its implementation and capacity are not
owned by this repository.

## Real legacy-Checkpoint acceptance

One real legacy Checkpoint target was present. Worker startup emitted exactly:

- one continuity `WARNING`;
- one failed Chat `INFO` summary;
- one failed Cycle `INFO` summary.

During the subsequent 45-second steady observation, it emitted:

- zero duplicate continuity signatures;
- zero repeated target continuity `WARNING` records;
- zero repeated target failed Chat `INFO` or routine idle/history Chat `INFO` records;
- zero repeated target failed Cycle `INFO`, routine Cycle `INFO`, or poll-cycle-start
  `INFO` records;
- zero routine `httpx`, `httpcore`, or Alembic `INFO` records;
- zero `ERROR` records and zero validation violations.

Database and queue totals remained consistent and unchanged during the controlled
observation.

The real legacy-Checkpoint Stage-11G shadow validation completed four cycles with one
total `WARNING`, one total Chat `INFO`, one total Cycle `INFO`, zero Sink calls, zero
Checkpoint mutation attempts, and no candidate PostgreSQL connection. This shadow result
proved log lifecycle behavior without creating business effects.

## Log retention record

The accepted seven-day model is 517,295,964 bytes, about 70.48 MiB/day. It estimates
8.17 days against the 576 MiB safety budget. Each Compose service is configured for a
640 MiB maximum (`64m` x `10`); six Compose services therefore have a 3.75 GiB
theoretical combined maximum.

This is a capacity model, not a guarantee of host free space. Recalculate it when traffic,
logging behavior, service count, Docker settings, or the safety budget changes. Database
facts and immutable recovery audits remain authoritative after logs rotate.

## Rollback and evidence

The formal pre-P1 rollback release remains preserved. The final rollback directory for
this acceptance is:

```text
/opt/cf-agent-gateway.rollback-p1-f36c7982-20260903T095347Z-2837839
```

The offline image archive is:

```text
/srv/storage/cf-agent-backups/gateway-wechat-r2/20260825T085214Z/cf-agent-gateway-p1-observability-main-b488cf452584-20260903-image.tar.gz
```

Its SHA-256 is:

```text
4176edf164f678086ce1231898ff6942b166046ab94118305e2172580eca4f24
```

Final evidence is stored at:

```text
/srv/storage/cf-agent-backups/gateway-wechat-r2/20260825T085214Z/FINAL-P1-RELEASE-p1-observability-main-b488cf452584-20260903.txt
```

Final evidence run ID: `20260903T095347Z-2837839`.

These timestamped paths belong to this release record. Future deployment procedures must
use an approved release directory, backup root, evidence path, and immutable image as
variables rather than assuming these exact paths.

## Operational limitations

- General AI Provider routing is future work.
- Automatic Skill execution is not connected.
- ERP logic, enterprise knowledge retrieval, RAG, OCR, and general archive processing are
  outside the current Gateway.
- `agent-wechat` entry/login behavior and Hermes execution behavior remain external.
- Outbound image/file response delivery is implemented and automated-test covered, but it
  was not part of this recorded real production acceptance.
- General inbound image/file understanding and arbitrary attachment ingestion are not
  implemented by the active polling-to-Hermes path.
- No cross-repository automatic deployment is claimed.
- The preserved rollback material is evidence of rollback readiness; this record does not
  claim that a backup restoration drill was executed.

## Revalidation boundary

Every future release must revalidate, at minimum:

- approved Git authority, immutable image digest, and one Alembic head;
- protected configuration and Token File contracts without printing values;
- Gateway readiness, runtime health, all three worker heartbeats, and Controller readiness;
- queue totals, stale/uncertain/reconciliation/delivery state, and Checkpoint continuity;
- controlled real-message idempotency and end-to-end delivery when the release affects the
  business path;
- structured-log privacy, steady-state noise, rotation policy, and retention capacity;
- intact previous release, image archive, database backup procedure, and rollback decision;
- after every CFserver/Debian reboot or `agent-wechat` recreation, formal Controller stop
  confirmation followed by a mandatory fresh QR and controlled gate reopen;
- Session preservation for Gateway-only releases that leave `agent-wechat` untouched;
- no fresh QR for an AI/Hermes host-only reboot when CFserver and `agent-wechat` did not
  restart.

Use [Production deployment](deployment/production.md) for procedure and
[Production validation](production-validation.md) for the reusable checklist.
