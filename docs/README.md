# Documentation index

This index separates current authoritative documentation from historical snapshots.
[Production status](production-status.md) summarizes the latest supplied site evidence.
Runbooks describe reusable procedures and are not proof of acceptance.

## Start here

- [Production status](production-status.md): latest observed image, evidence boundaries,
  historical P1 baseline, residual warnings and rollback limitations.
- [September 17 long-task acceptance](validation/2026-09-17-hermes-long-task-acceptance.md):
  real database duration, response/delivery/receipt evidence, WeChat receipt and remaining scope.
- [Repository README](../README.md): scope, runtime path and local entry.

## Architecture

- [Domain architecture](architecture.md): responsibility boundaries and Message-to-Delivery flow.
- [Runtime architecture](runtime-architecture.md): process ownership, Controller, CAS/FIFO,
  recovery, heartbeats, logging and restart semantics.

## Production operations

- [Production deployment](deployment/production.md): reusable controlled Compose procedure.
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

- [September 17 acceptance](validation/2026-09-17-hermes-long-task-acceptance.md): latest scoped
  long-task record, not a universal production sign-off.
- [Production validation](production-validation.md): historical September 3 P1 acceptance and
  a reusable unchecked future-release checklist. Its P1 image is not the new site image.
- [Production status](production-status.md): current summary and explicitly dated old archives.

## Development and staging

Local development starts in the [repository README](../README.md). Repository tests and
GitHub Actions establish only behavior for their specific commit/environment; a green CI
run does not replace external-system site evidence.

## Historical documents

These retain dated snapshots and must not be used as current production-image instructions:

- [V1 staging validation](v1-staging-validation.md)
- [V2 Integration Alpha](v2-integration-alpha-status.md)
- [Debian staging](deployment/staging-debian.md)
- [systemd alternative](systemd-deployment.md)
- [September 3 P1 validation](production-validation.md#completed-production-acceptance-record)
