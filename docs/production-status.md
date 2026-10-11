# Production status

## Authority

Status as of **2026-10-11**: **new plans passed; no switch applied; post-reboot checks pending**.

The latest site evidence is the user's terminal report, recorded in the
[October 11 site summary — 用户终端回传证据](validation/2026-10-11-gateway-site-status.md).
Codex has not connected to CFserver or inspected live services for this documentation update.
The last stop point is neither an ongoing f9 rollback nor a deployed d9 fix.

| Field | Current evidence and boundary |
| --- | --- |
| Completed f9 recovery | Both hosts' old configuration rollback, server `complete-rollback` (`assets_rewritten=false`), old Hermes startup and server `start-old` succeeded; do not restart those completed steps |
| New deployment | `cf-return-env-loopback-20261011-r1` |
| Fixed release | `d9bae61a043f1be4303e5c23661ec51904d5e708`; this documentation commit cannot replace it |
| Built candidate Image ID | `sha256:bcb3e42fef5ba5a91232ca04e49a959cbecab4b45eb3bec0a0b9b3aadf717185`; candidate only, not proof of a running image or registry digest |
| Windows plan | Passed; 13 items including the `.env` `API_SERVER_HOST` single-field change; original 17 toolsets preserved |
| Server plan | Passed; original `env_file` parsing and environment equivalence passed |
| Migration heads | Old and new both `20260928_01`; no migration needed for this switch |
| New activation | `apply/start/verify` not executed |
| Pre-reboot runtime | Old Hermes, Gateway API and Dispatch running; Poll/Delivery stopped, Gate closed, database healthy; pending/in-flight/uncertain zero, historical `dead=2` preserved |
| Subsequent host action | User executed `sudo reboot` on CFserver only; completion and post-reboot containers/Gate/WeChat login unverified; Windows shutdown unconfirmed |

The runtime row is a **pre-reboot snapshot**. Do not use old PIDs, old health or queue
counts to assert current state. Preserve historical dead records and all existing plans,
receipts, requests and journals.

GitHub was checked separately on 2026-10-11:

| Repository object | Remote state |
| --- | --- |
| `main` | `7963bc0db5a01e38099e021959feabf648db7ede` |
| [PR #14](https://github.com/Tangbohu09527/CF_agent-gateway/pull/14) | Draft/Open, head `f9c601cba25cbc5459bb03519d1954ae55995ded`, source `feat/wechat-inbound-media`, base `main`, not merged |
| Independent fix branch | `codex/fix-hermes-env-loopback` at `d9bae61a043f1be4303e5c23661ec51904d5e708` |
| Documentation branch / PR base | `codex/docs-gateway-status-20261011` / `codex/fix-hermes-env-loopback`, not `main` |

The d9 fix is not in `main` or PR #14. Branch/PR state, candidate construction, plan
success and site activation are separate facts. No existing functional PR or deployment
identity is changed by this documentation update.

## Next handoff and business scope

Start with the [dated summary's handoff navigation](validation/2026-10-11-gateway-site-status.md#下次接管导航):
verify the post-reboot site and existing materials, then reuse the fixed candidate and
plans if their bindings and prerequisites still hold. Do not repeat the completed
transfer, clone, build or f9 recovery. The
[existing two-host switch order](deployment/hermes-env-loopback-recovery.md#4-新计划按原交错顺序应用)
remains the later maintenance entry, after verification and applicable authorization.
The [server rollback resume](deployment/server-rollback-resume.md) is completed history.

| Business workflow | Evidence boundary |
| --- | --- |
| File API read followed by text reply | [October 7 closeout](validation/2026-10-07-wechat-file-api-text-closeout.md) covers two specific PDF/PNG reading tasks and original-chat text receipt, with the recorded Skill side effect preserved |
| Actual WeChat inbound attachment bytes | Independent intake boundary; FileBrowser reads and message attachment metadata do not prove original attachment acquisition |
| Actual PDF/PNG returned to the original chat | [Official Hermes/isolated Gateway evidence](validation/2026-10-07-official-hermes-artifact-return.md) uses a receiver substitute; real file/image messages and recipient receipt remain unverified |

WeChat file sending and receiving is the current priority. LAN permissions and a
chat-history Web interface remain later considerations, with no development in this task.
The October 7 and October 11 code/isolated evidence remains valid within its recorded
scope; no business acceptance is rerun for this Markdown-only update.

## Historical September 17 authority

The following records the **September 17 observation**, not current post-reboot runtime.
At that time production was operating, scoped long-task acceptance passed, and residual
degradation remained.

This historical evidence was recorded on
**2026-09-17** from user-provided terminal output and WeChat screenshots.

| Field | Authority or status value |
| --- | --- |
| Repository branch authority | `main`; live tip must be queried |
| Merged long-task fix baseline | PR #11 merge `9a1caa237a9053678c80f68fdb15d351d5bfecf8` |
| Historical site evidence dates | Application upgrade September 16; controlled recovery and long-task acceptance September 17, 2026 |
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

The September operational evidence is in
[2026-09-17 long-task acceptance](validation/2026-09-17-hermes-long-task-acceptance.md).
This document change does not rebuild, migrate, deploy, restart or change any live service.
Historical GitHub CI runs remain tied to their original commits and are not evidence of
this documentation commit's checks.

## Historical September 17 accepted production state

The September 17 record confirms a database-recorded long dispatch beyond the old approximately
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

These limitations describe the September 17 acceptance's scope. Use the October 11 business-scope
table above for the later file workflows; none of the dated evidence proves post-reboot health.

- General AI Provider routing and automatic enterprise Skill execution are not connected.
- ERP logic, enterprise knowledge retrieval, RAG, OCR and general archive processing remain outside the Gateway.
- Hermes raw tool execution evidence and installed Desktop build are not independently reviewed.
- Three historical chat continuity issues remain open; the scoped test chat is separate.
- Near-600-second waits, in-flight disconnect/reboot, long-task concurrency/FIFO/lease
  observation and full restart/recovery acceptance were not performed in the September site record.
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

For the user-reported October 11 CFserver reboot, completion, Gate and login have not
yet been checked. Apply the [existing reboot procedure](runtime-recovery.md#agent-wechat-offline-after-host-reboot)
within the pending deployment window; this text does not confirm fresh QR or gate reopen
has occurred, and the generic reopen step is not the current handoff starting point.

Use [Production deployment](deployment/production.md),
[Production validation](production-validation.md) and [Runtime recovery](runtime-recovery.md)
for procedures; keep actual results in dated records.
