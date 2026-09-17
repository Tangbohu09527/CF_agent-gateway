# Production status

## Authority

Status: **production operating; scoped long-task acceptance passed; residual degradation remains**.

This is the authoritative summary of the latest supplied CF_agent-gateway site evidence,
not a generic deployment runbook or continuous remote inspection. Evidence was updated on
**2026-09-17** from user-provided terminal output and WeChat screenshots.

| Field | Authority or status value |
| --- | --- |
| Repository branch authority | `main`; live tip must be queried |
| Merged long-task fix baseline | PR #11 merge `9a1caa237a9053678c80f68fdb15d351d5bfecf8` |
| Latest site evidence dates | Application upgrade September 16; controlled recovery and long-task acceptance September 17, 2026 |
| Observed application Docker Image ID | `sha256:1cd7650543babe75d4fabe71e27e3cbc1d54585d34ffa853280606c2a3ddaa8b` |
| Startup read/execution budgets | `600/600` seconds in Dispatch startup logs |
| Dispatch stop grace | `3660` seconds in rendered Compose |
| Database schema | Repository head `20260823_04`; site database/migration runtime components reported `ok` |
| Runtime Controller | `/opt/cf-agent-gateway/deploy/wechat-runtime-control` |
| Controller evidence | `ready=true`, healthy Poll/Delivery containers, valid Token File contract |
| Latest accepted scenario | One minute-scale task persisted and delivered to WeChat; see the dated record below |
| Release label, production Tag, registry digest | New values not independently verified in this evidence set |
| Image-to-source build provenance | Merged code and observed Image ID recorded separately; complete mapping not independently verified |
| New offline evidence/rollback archive | Not independently verified; do not substitute the historical P1 archive |

The four application containers and Compose image configuration were checked against the
Image ID above. It is a Docker Image ID, **not** a proven registry manifest digest. PR #11
being merged and the site using the observed image do not alone establish reproducible
source-to-image provenance. No new source/image mapping, Release label or Tag is invented here.

Current operational evidence is in
[2026-09-17 long-task acceptance](validation/2026-09-17-hermes-long-task-acceptance.md).
This document change does not rebuild, migrate, deploy, restart or change any live service.
Historical GitHub CI runs remain tied to their original commits and are not evidence of
this documentation commit's checks.

## Accepted production state

The latest record confirms a database-recorded long dispatch beyond the old approximately
30-second boundary, a persisted response, a single receipted delivery attempt and a
matching WeChat screenshot. It also records audited termination of an old uncertain
Dispatch and delivery of the existing queued read-only test. These are bounded observations,
not an all-scenarios production or high-availability certification.

All three Workers were restored during the recorded sequence. Poll/Delivery were opened
through the Controller; Dispatch was independently stopped and resumed to avoid executing
unreviewed backlog. API, PostgreSQL and external `agent-wechat` were not restarted by that
recovery sequence. No fresh QR was required solely because the separate AI host rebooted.
This does not prove login survival after a CFserver reboot or `agent-wechat` recreation.

Residual Poll degradation is explained by three historical chat continuity failures in
the reviewed log window; the test chat was healthy. A preserved terminal `dead` record
also contributes to aggregate degradation. Exact identifiers and dated counts belong in
the acceptance record, not as continuously current totals here. Do not delete audit/queue
records or reset Checkpoints to make health appear green.

## Historical September 3 P1 release

The following is preserved historical evidence, **not the current observed image**:

| Field | September 3/4 historical baseline |
| --- | --- |
| Release label | `p1-observability-main-b488cf452584-20260903` |
| Production Release Git authority | `b488cf452584e73bc9b752564bf90ea153aa8d18` (PR #7 merge) |
| Validated image code snapshot | `f36c798294368263433f6132366ac9a864d9482b` |
| Recorded image identity | `sha256:b9341ca7df6f952b4d81028c497574c1e22478e4408f98791a28bd9514b215f1` |
| Local immutable tag | `cf-agent-gateway:p1-observability-main-b488cf452584-20260903` |
| Alembic revision | `20260823_04` |
| Long-running application user | `10001:10001` |
| Gateway log policy | Docker `json-file`, `64m` x `10` per Compose service |
| PR #8 documentation closeout baseline | `c5518aed12b90235f118ed81bb3cef75d0463443` |
| PR #8 baseline CI | Run `33853221731` succeeded |

PR #7 was merged and Issue #6 closed; no new P1 Git release tag was created. PR #8
subsequently changed documentation only and did not redeploy that historical release.
Those facts remain true about those changes, but do not negate the later September 16
application upgrade. A moving repository tip never by itself proves deployment.

After the historical P1 acceptance, Gateway, all Workers, PostgreSQL and external
`agent-wechat` had healthy container checks, Controller readiness was true, the Token
Contract was valid and outstanding work was zero. The P1 Gateway-only deployment preserved
the untouched active WeChat Session. Historical healthy containers did not mean every
chat-level continuity condition was resolved.

## Real legacy-Checkpoint acceptance

This section records **September 3 P1**, not a new September 17 log/retention acceptance.
One real legacy Checkpoint target was present. Worker startup emitted exactly one continuity
WARNING, one failed Chat INFO summary and one failed Cycle INFO summary.

During the subsequent 45-second steady observation there were zero duplicate continuity
signatures, repeated target warnings, repeated target failed/idle/history Chat INFO,
repeated failed/routine Cycle INFO, poll-cycle-start INFO, routine httpx/httpcore/Alembic
INFO, ERROR records and validation violations. Database/queue totals stayed consistent.

The real Stage-11G shadow validation completed four cycles with one WARNING, one Chat INFO,
one Cycle INFO, zero Sink calls, zero Checkpoint mutation attempts and no candidate
PostgreSQL connection. It proved bounded log lifecycle behavior without business effects;
it did not prove every continuity issue had been repaired.

## Log retention record

The historical seven-day model was 517,295,964 bytes, about 70.48 MiB/day: 8.17 days against
a 576 MiB safety budget. A service configured for `64m` x `10` has a 640 MiB maximum;
six services have a 3.75 GiB theoretical combined maximum.

This is a dated capacity model, not a current host-space measurement or a retention
promise. Recalculate after traffic, service count or logging changes. Immutable database
recovery facts remain authoritative after logs rotate. September 17 did not repeat this
capacity study or a complete structured-log privacy acceptance.

## Rollback and evidence

The following paths were recorded for the **historical P1 acceptance** and were not
rechecked for this long-task upgrade. Their existence in this document is not evidence
that the new image has an offline archive or that any restore drill succeeded.

Historical rollback directory:

```text
/opt/cf-agent-gateway.rollback-p1-f36c7982-20260903T095347Z-2837839
```

Historical image archive:

```text
/srv/storage/cf-agent-backups/gateway-wechat-r2/20260825T085214Z/cf-agent-gateway-p1-observability-main-b488cf452584-20260903-image.tar.gz
```

Recorded archive SHA-256:

```text
4176edf164f678086ce1231898ff6942b166046ab94118305e2172580eca4f24
```

Historical final evidence:

```text
/srv/storage/cf-agent-backups/gateway-wechat-r2/20260825T085214Z/FINAL-P1-RELEASE-p1-observability-main-b488cf452584-20260903.txt
```

Historical evidence run ID: `20260903T095347Z-2837839`.

September 17 evidence currently consists of the supplied terminal records/screenshots and
the linked redacted summary. A protected offline archive path/checksum has not been supplied
for independent verification. Future procedures must use approved release, backup and
evidence variables rather than blindly reuse these timestamped historical paths.

## Operational limitations

- General AI Provider routing and automatic enterprise Skill execution are not connected.
- ERP logic, enterprise knowledge retrieval, RAG, OCR and general archive processing remain outside the Gateway.
- Hermes raw tool execution evidence and installed Desktop build are not independently reviewed.
- Three historical chat continuity issues remain open; the scoped test chat is separate.
- Near-600-second waits, in-flight disconnect/reboot, long-task concurrency/FIFO/lease
  observation and full restart/recovery acceptance were not performed in the new site record.
- `agent-wechat` login and Hermes execution remain external responsibilities.
- Outbound image/file delivery is implemented/tested but not part of this site acceptance;
  general inbound image/file understanding is not implemented by the active polling path.
- Local-file tool testing does not deliver enterprise File Service, Skills, ERP or media integration.
- No cross-repository automatic deployment, complete build provenance or new backup/restore/rollback sign-off is claimed.

## Revalidation boundary

Every future release must separately revalidate Git/image provenance, one Alembic head,
configuration/Token contracts, API readiness, Worker heartbeats, Controller readiness,
queue and Checkpoint facts, controlled message idempotency/delivery, privacy and retention,
and intact backup/rollback material. A single long-task pass does not mark that full
checklist complete.

After CFserver reboot or `agent-wechat` recreation: formal Controller stop confirmation,
mandatory fresh QR and controlled gate reopen. For Gateway-only deployment leaving
`agent-wechat` untouched, verify Session preservation. For an AI-host-only reboot without
CFserver/WeChat restart, restore Hermes and verify health without requiring a fresh QR
solely because of that reboot.

Use [Production deployment](deployment/production.md),
[Production validation](production-validation.md) and [Runtime recovery](runtime-recovery.md)
for procedures; keep actual results in dated records.
