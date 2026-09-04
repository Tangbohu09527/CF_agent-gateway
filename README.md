# CF_agent-gateway

CF_agent-gateway is the durable message and control plane between enterprise message
entry services and the external Hermes execution service.

## Current status

The V2 runtime and P1 observability changes are production validated. Production is
online on the release recorded in [Production status](docs/production-status.md).

- Repository main: `c5518aed12b90235f118ed81bb3cef75d0463443`
  (PR #8 docs-only merge commit)
- Production Release Git authority:
  `b488cf452584e73bc9b752564bf90ea153aa8d18` (PR #7 merge commit)
- Production image code snapshot:
  `f36c798294368263433f6132366ac9a864d9482b`
- Production image:
  `sha256:b9341ca7df6f952b4d81028c497574c1e22478e4408f98791a28bd9514b215f1`
- Release label: `p1-observability-main-b488cf452584-20260903`
- Database head: `20260823_04`
- Production log policy: Docker `json-file`, `64m` x `10` files per Compose service
- No new P1 Git release tag was created

PR #8 only updated documentation. It advanced repository `main` to `c5518aed12b9` but did
not rebuild or deploy the production image, change the database revision or release label,
or create a production tag. Current production remains defined by the Production Release
Git authority `b488cf452584`, the image code snapshot `f36c79829436`, and the immutable
image digest above. Repository `main` advancing does not mean production was redeployed.

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

## Known limitations

- General Provider routing and automatic Skill execution are not connected.
- ERP automation, knowledge retrieval, RAG, and Hermes behavior are outside the Gateway.
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
- [Production deployment](docs/deployment/production.md)
- [Runtime recovery](docs/runtime-recovery.md)
- [Runtime health](docs/runtime-health.md)
- [API](docs/api.md)
- [Migration runbook](migrations/README.md)
- [Production validation](docs/production-validation.md)

Historical V1, Alpha, staging, and systemd snapshots are indexed separately and are not
current production instructions.
