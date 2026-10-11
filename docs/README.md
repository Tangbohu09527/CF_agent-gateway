# Documentation index

This index separates current authoritative documentation from historical snapshots.
[Production status](production-status.md) summarizes the latest supplied site evidence.
Runbooks describe reusable procedures and are not proof of acceptance.

## Start here

- [October 11 site summary — user terminal reports](validation/2026-10-11-gateway-site-status.md):
  completed f9 recovery, fixed d9 candidate, both new plans passed, no switch applied,
  CFserver reboot issued and post-reboot checks pending. Start here for this handoff.
- [Production status](production-status.md): current stop point, separate main/PR #14/fix
  branch identities, business evidence boundaries and dated historical baselines.
- [September 17 long-task acceptance](validation/2026-09-17-hermes-long-task-acceptance.md):
  real database duration, response/delivery/receipt evidence, WeChat receipt and remaining scope.
- [Repository README](../README.md): scope, runtime path and local entry.

## Architecture

- [Domain architecture](architecture.md): responsibility boundaries and Message-to-Delivery flow.
- [Runtime architecture](runtime-architecture.md): process ownership, Controller, CAS/FIFO,
  recovery, heartbeats, logging and restart semantics.

## Production operations

- [Production deployment](deployment/production.md): reusable controlled Compose procedure.
- [Attachment return site handoff](deployment/artifact-return-site.md): existing two-host
  procedure; reuse the built candidate and plans after checking the post-reboot site.
- [Hermes environment/loopback recovery](deployment/hermes-env-loopback-recovery.md):
  completed f9 recovery and new-plan preparation history; section 4 is the later controlled switch.
- [Server rollback resume](deployment/server-rollback-resume.md): completed f9
  `complete-rollback` / old-core recovery history, not the current blocker.
- [Long-task runtime](hermes-long-task-runtime.md): finite wait configuration, safety semantics,
  code-level test history and the subsequent real-site acceptance boundary.
- [Runtime health](runtime-health.md): health fields and Controller contract.
- [Runtime recovery](runtime-recovery.md): fail-closed diagnosis and recovery.
- [Troubleshooting](troubleshooting.md): read-only checks and safe actions.
- [Migration runbook](../migrations/README.md): chain, guards and backup boundary.

## API and contracts

- [HTTP and Admin API](api.md): routes, authentication, idempotency and recovery audit.
- [Gateway to agent-wechat contract](wechat-runtime-contract.md): Controller and Token File.
- [WeChat outbound media](wechat-media-adapter-v2.md): implemented/tested behavior and unverified site scope.

## Validation and release evidence

- [October 11 site summary](validation/2026-10-11-gateway-site-status.md): user terminal
  evidence and remaining post-reboot verification; this documentation task performs no deployment.
- [October 11 Hermes HOST/TLS fix](validation/2026-10-11-hermes-env-loopback.md):
  code and isolated official-CLI evidence, distinct from the site summary.
- [October 11 baseline rollback fix](validation/2026-10-11-server-rollback-resume.md):
  recovery-controller tests and their limits, followed by the separate user-reported recovery.
- [October 7 file API reading and text reply closeout](validation/2026-10-07-wechat-file-api-text-closeout.md):
  two specific reading tasks and text receipt; neither WeChat inbound bytes nor outbound attachment receipt.
- [October 7 official Hermes artifact return](validation/2026-10-07-official-hermes-artifact-return.md):
  real official Agent and isolated Gateway, with a receiver substitute; no live WeChat attachment receipt.
- [October 7 isolated artifact return](validation/2026-10-07-artifact-return-isolated.md):
  earlier Gateway byte-transfer evidence, including its pre-final-ACK sample boundary.
- [September 17 acceptance](validation/2026-09-17-hermes-long-task-acceptance.md): latest scoped
  long-task record from September, not current runtime evidence or a universal production sign-off.
- [Production validation](production-validation.md): historical September 3 P1 acceptance and
  a reusable unchecked future-release checklist. Its P1 image is not the new site image.
- [Production status](production-status.md): current summary and explicitly dated old archives.

## Development and staging

Local development starts in the [repository README](../README.md). Repository tests and
GitHub Actions establish only behavior for their specific commit/environment; a green CI
run does not replace external-system site evidence.

- [Inbound media intake](development/inbound-media-stage3.md): actual WeChat attachment
  bytes are a separate boundary from files read through the file API.
- [Artifact return contract](development/artifact-return-contract.md),
  [official Hermes integration](development/hermes-artifact-return.md) and
  [enablement helper](development/hermes-artifact-return-enablement.md): implementation
  contracts, not evidence that the fixed d9 candidate has been applied.

Current priority: WeChat file sending and receiving. LAN permissions and a chat-history
Web interface remain later considerations; this update does not develop either.

## Historical documents

These retain dated snapshots and must not be used as current production-image instructions:

- [V1 staging validation](v1-staging-validation.md)
- [V2 Integration Alpha](v2-integration-alpha-status.md)
- [Debian staging](deployment/staging-debian.md)
- [systemd alternative](systemd-deployment.md)
- [September 3 P1 validation](production-validation.md#completed-production-acceptance-record)
