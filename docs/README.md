# Documentation index

This index separates current authoritative documentation from historical snapshots.
[Production status](production-status.md) is the single authority for the currently
deployed release. Runbooks describe reusable procedures and must not be read as proof
that a future release has passed production acceptance.

## Start here

- [Production status](production-status.md): authoritative release, deployment, evidence,
  rollback, and current limitation record.
- [Repository README](../README.md): concise project scope, runtime path, and local entry.

## Architecture

- [Domain architecture](architecture.md): responsibility boundaries, core entities,
  permission model, and Message-to-Delivery data flow.
- [Runtime architecture](runtime-architecture.md): process ownership, Worker lifecycle,
  Controller, Checkpoint, CAS/FIFO, recovery, heartbeat, logging, and restart semantics.

## Production operations

- [Production deployment](deployment/production.md): reusable Compose deployment,
  controlled Worker gate, verification, and rollback procedure.
- [Runtime health](runtime-health.md): exact health endpoints, fields, status meanings, and
  Controller status contract.
- [Runtime recovery](runtime-recovery.md): fail-closed diagnosis and recovery flows.
- [Troubleshooting](troubleshooting.md): symptom-oriented read-only checks and safe actions.
- [Migration runbook](../migrations/README.md): Alembic chain, upgrade guards, downgrade
  limits, and backup boundary.

## API and contracts

- [HTTP and Admin API](api.md): current routes, authentication, limits, response fields,
  idempotency, and recovery audit behavior.
- [Gateway to agent-wechat contract](wechat-runtime-contract.md): Runtime Controller and
  protected Token File contract.
- [WeChat outbound media adapter](wechat-media-adapter-v2.md): implemented and tested
  outbound image/file adapter boundary and its production-validation limitation.

## Validation and release evidence

- [Production validation](production-validation.md): completed September 2026 acceptance
  record followed by a clean future-release checklist.
- [Production status](production-status.md): immutable image, real-log observations,
  retention record, rollback location, and final evidence location.

## Development and staging

- Current local development starts in the [repository README](../README.md).
- Production-like container checks are implemented in the repository test suite and
  GitHub Actions; a green run is test evidence, not external-system ownership.

## Historical documents

The following files are archived snapshots. They may retain old branch names, revisions,
or deployment models as historical evidence and must not be used as current production
instructions.

- [V1 staging validation](v1-staging-validation.md): historical text-only V1 staging
  acceptance record.
- [V2 Integration Alpha status](v2-integration-alpha-status.md): historical Alpha branch
  and schema snapshot superseded by the production V2 record.
- [Debian staging deployment](deployment/staging-debian.md): archived non-Compose staging
  layout.
- [systemd deployment](systemd-deployment.md): archived alternative process-manager
  guidance; current production uses Compose and the Runtime Controller.
