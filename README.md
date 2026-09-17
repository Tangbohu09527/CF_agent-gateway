# CF_agent-gateway

CF_agent-gateway is the durable message and control plane between enterprise message
entry services and the external Hermes execution service.

## First installation

For a clean Debian 13 amd64 device, start with [the staged installation entry](docs/deployment/clean-device.md).
It covers fixed sources, Controller, WeChat Bootstrap, independent PostgreSQL, images,
credentials and core startup without employee lists or a configured bot ID. After login,
use the [business CLI](docs/deployment/initial-identity.md) to explicitly approve or disable
access from observed messages. [Hermes LAN setup](docs/deployment/hermes-lan.md)
records the missing Hermes source/version evidence. Clean-device AI-host installation
acceptance remains separate from the existing-host September 17 long-task record.
Existing installations use the separate upgrade runbook.

## Current status

Evidence updated: **2026-09-17**. The existing production chain is operating on the newly
observed application image, with a scoped long-task persistence and WeChat delivery
acceptance. Historical continuity warnings remain open; this is not an all-scenarios
healthy or high-availability sign-off.

- Repository branch authority: `main`; query its live tip instead of treating a dated
  snapshot as permanently current.
- Merged long-task fix: PR #11, `9a1caa237a9053678c80f68fdb15d351d5bfecf8`.
- Observed application Docker Image ID:
  `sha256:1cd7650543babe75d4fabe71e27e3cbc1d54585d34ffa853280606c2a3ddaa8b`.
- Dispatch startup read/execution budgets: `600/600` seconds; Dispatch stop grace: `3660` seconds.
- Repository schema head: `20260823_04`; site database/migration runtime checks reported `ok`.
- Scoped acceptance: one long task persisted and reached WeChat, with database-recorded
  dispatch duration **113.799 seconds**, one dispatch attempt and one receipted delivery attempt.

See [Production status](docs/production-status.md) for deployment authority and
[2026-09-17 acceptance](docs/validation/2026-09-17-hermes-long-task-acceptance.md) for dated
record IDs, timestamps, evidence provenance and remaining work. The Image ID is not a
verified registry manifest digest. The merged source baseline and observed image are
separate facts; complete build provenance, a new Release label/Tag and a new offline
rollback archive have not been independently verified in this record.

The September 3 P1 Release authority `b488cf452584e73bc9b752564bf90ea153aa8d18`, source
snapshot `f36c798294368263433f6132366ac9a864d9482b` and image `sha256:b9341ca7df6f952b4d81028c497574c1e22478e4408f98791a28bd9514b215f1`
are historical, not the newly observed application image. PR #8/#9 documentation closeout
remains historical evidence. This documentation change does not deploy production.

## Responsibility boundary

The Gateway implements:

- durable WeChat message ingestion, normalization, checkpointing, and idempotency;
- identity resolution, access admission, Agent Profile routing, and AI Thread selection;
- durable Hermes dispatch with FIFO, leases, claim fencing, retry, and audited recovery;
- durable response persistence, reconciliation, ordered delivery, and receipts;
- health, readiness, runtime health, Admin inspection, and Admin recovery APIs.

The Gateway does not implement Hermes, `agent-wechat`, AI inference, automatic Skill
execution, general AI Provider routing, ERP business logic, an enterprise knowledge base,
RAG, OCR, or general inbound file/archive understanding. `agent-wechat`, Hermes,
PostgreSQL lifecycle, secret storage, and host operations remain external responsibilities.
A local-file tool test through external Hermes is not enterprise File Service or Skills integration.

## Runtime path

```text
external agent-wechat
  -> Poll Worker
  -> Message + Admission Outcome + queued Dispatch
  -> Dispatch Worker
  -> external Hermes
  -> persisted Response + Delivery Outbox
  -> Delivery Worker
  -> external agent-wechat
```

Polling stops after the durable admission and dispatch transaction. It does not call
Hermes or send replies inline.

Production uses four independent application processes plus external PostgreSQL:

- Gateway API
- Poll Worker (`worker` in production Compose)
- Dispatch Worker (`dispatch-worker`)
- Delivery Worker (`delivery-worker`)

FastAPI does not host any worker as a background task. The Runtime Controller is the
formal lifecycle gate for the Poll and Delivery Workers. Gateway and Dispatch Worker can
remain online while that gate is closed, including during an `agent-wechat` fresh-QR
login boundary.

## Reliability guarantees

- Message and physical source-message identities are independently idempotent.
- One durable Admission Outcome is authoritative for each persisted Message.
- Allowed admission and the first Dispatch record commit atomically.
- One AI Thread has at most one running Hermes dispatch and preserves FIFO order.
- Claim tokens and leases fence stale Dispatch Worker writes.
- `uncertain` external effects fail closed and require authenticated, audited recovery.
- Response and Delivery state are durable; delivery failure does not call Hermes again.
- WeChat checkpoints use generation, continuity anchors, compare-and-swap updates, and
  fail-closed handling when continuity cannot be established.
- Worker heartbeats, runtime health, structured logs, and bounded log retention provide
  operational evidence without replacing database authority.

These implementation rules are not a claim that every failure/reboot/concurrency scenario
was exercised during the September 17 site acceptance.

## Known limitations

- General Provider routing and automatic Skill execution are not connected.
- ERP automation, knowledge retrieval, RAG, and Hermes behavior are outside the Gateway.
- Three historical chats had unresolved continuity failures in the reviewed site logs;
  the accepted test chat was not one of them. Do not reset Checkpoints to clear health warnings.
- Raw Hermes tool logs, the installed Desktop build, near-600-second execution, in-flight
  disconnect/reboot, and long-task concurrency/FIFO/lease observations remain separately unverified.
- V2 supports explicit `private_sender`, `group_sender`, and `group_shared` Thread
  policies. The old Alpha whole-group Thread limitation is not a current V2 limitation.
- Outbound response artifacts can be delivered as tested image/file parts, but that media
  path was not part of the recorded CFserver production acceptance.
- Inbound image/file interpretation, OCR, and archive processing are not implemented as a
  general Gateway workflow. Message API attachment rows are metadata, not content upload.
- Cross-repository deployment and backup restoration are operator procedures, not
  automated or proven by this repository.

## Local development

Python 3.12 or newer is required.

POSIX:

```bash
python -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"
.venv/bin/python -m cf_agent_gateway.main
```

Windows PowerShell:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m cf_agent_gateway.main
```

The default configuration is `config/config.yaml`; override it with
`CF_GATEWAY_CONFIG`. Local adapter integrations require their own non-committed test
configuration and credentials. After the service starts, run the health check from a
second terminal:

```bash
curl http://127.0.0.1:8080/health
```

In PowerShell, use `curl.exe http://127.0.0.1:8080/health`.

## Documentation

Start with the [documentation index](docs/README.md). The main operational entries are:

- [Production status](docs/production-status.md)
- [September 17 long-task acceptance](docs/validation/2026-09-17-hermes-long-task-acceptance.md)
- [Long-task runtime and evidence boundaries](docs/hermes-long-task-runtime.md)
- [Production deployment](docs/deployment/production.md)
- [Runtime recovery](docs/runtime-recovery.md)
- [Runtime health](docs/runtime-health.md)
- [API](docs/api.md)
- [Migration runbook](migrations/README.md)
- [Production validation](docs/production-validation.md)

Historical V1, Alpha, staging, systemd and September 3 P1 snapshots are not current
production-image instructions.
