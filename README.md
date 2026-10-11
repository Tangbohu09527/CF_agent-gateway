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

Evidence updated: **2026-10-11**, from **user terminal reports (用户终端回传证据)**.
The current stop point is **new plans passed; no switch applied; post-reboot checks pending**.
See the [dated site summary](docs/validation/2026-10-11-gateway-site-status.md) first and
[Production status](docs/production-status.md) for the evidence boundaries.

- The f9 rollback on both hosts, `complete-rollback` (`assets_rewritten=false`), old
  Hermes startup and server `start-old` succeeded. These stages are complete.
- New deployment: `cf-return-env-loopback-20261011-r1`. Its release remains fixed at
  `d9bae61a043f1be4303e5c23661ec51904d5e708`, independently of this documentation commit.
- Built candidate Image ID:
  `sha256:bcb3e42fef5ba5a91232ca04e49a959cbecab4b45eb3bec0a0b9b3aadf717185`.
  Both Windows and server plans passed; new `apply/start/verify` have not run.
  Both migration heads are `20260928_01`; this switch needs no migration.
- Before reboot, old Hermes, Gateway API and Dispatch were running; Poll/Delivery were
  stopped, Gate closed and database healthy. Pending/in-flight/uncertain were zero;
  historical `dead=2` was preserved. These are **pre-reboot observations**.
- The user subsequently executed `sudo reboot` on CFserver only. Reboot completion,
  containers, Gate and WeChat login remain unverified; Windows shutdown is unconfirmed.
  Old PIDs and pre-reboot health cannot prove current state.

Remote GitHub state checked on 2026-10-11: `main` is
`7963bc0db5a01e38099e021959feabf648db7ede`;
[PR #14](https://github.com/Tangbohu09527/CF_agent-gateway/pull/14) is Draft/Open at
`f9c601cba25cbc5459bb03519d1954ae55995ded`, base `main`; the independent
`codex/fix-hermes-env-loopback` branch is at the fixed d9 release above. The d9 fix has
not entered `main` or PR #14. This documentation PR targets the independent fix branch.

Next, verify the post-reboot site and existing release/candidate/request/plan/receipt
materials. Reuse them when their bindings and prerequisites still hold; do not restart
the completed rollback, transfer, clone or build sequence. Follow the existing
[deployment handoff](docs/deployment/hermes-env-loopback-recovery.md#4-新计划按原交错顺序应用)
only after those checks and the applicable maintenance authorization.

WeChat file sending and receiving is the current priority. Keep these outcomes separate:

| Workflow | Evidence boundary |
| --- | --- |
| Text instruction → file API read → text reply | Two dated PDF/PNG reading tasks are closed in the [October 7 record](docs/validation/2026-10-07-wechat-file-api-text-closeout.md). |
| WeChat inbound attachment → actual original bytes | Independent intake boundary; reading a file already in FileBrowser does not prove WeChat attachment retrieval. |
| Actual PDF/PNG → original WeChat chat | [Official Hermes/isolated Gateway tests](docs/validation/2026-10-07-official-hermes-artifact-return.md) passed with a receiver substitute; live file/image receipt remains unverified. |

LAN permissions and a chat-history Web interface are later considerations, with no
development in this task. September production records remain dated historical evidence.

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
- Outbound response artifacts have isolated image/file delivery evidence; actual PDF/PNG
  receipt in the original WeChat chat is still pending the current site deployment and acceptance.
- Inbound image/file interpretation, OCR, and archive processing are not implemented as a
  general Gateway workflow. Message API attachment rows are metadata, not content upload.
  Opt-in durable WeChat byte intake and scoped Hermes HTTP reads are implemented;
  see the [intake contract and acceptance checklist](docs/development/inbound-media-stage3.md).
  This does not establish live WeChat download or FileBrowser archival acceptance.
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
- [October 11 site summary — user terminal reports](docs/validation/2026-10-11-gateway-site-status.md)
- [October 7 file API reading and text reply closeout](docs/validation/2026-10-07-wechat-file-api-text-closeout.md)
- [Attachment return deployment handoff](docs/deployment/artifact-return-site.md)
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
