# Official Hermes listener configuration probe

This explicit integration driver is separate from pytest collection. It checks
Hermes commit `0f4a98f87c17007b81500239d0bd5b9574027b73` on Python **3.14.7**,
including its real dotenv loader, environment override, plugin loader, and
Windows management implementation. A clean source checkout and twelve relevant
Git blob identities are verified before and after execution.

Windows runs the isolated interpreter's
`python -m hermes_cli.main gateway start` and `gateway stop`. The actual official
Windows manager detaches the `gateway run` child and drains it on stop. The
production `hermes.exe`/package-manager wrapper and installed service registration
are outside this test. No login task or Windows service is installed. The Linux
fallback uses foreground official `gateway run` and official `gateway stop`;
the new CI job specifically exercises Windows.

## Isolation and assertions

Each invocation requires a **new** output directory. All home/profile paths,
certificates, private keys, authentication keys, journal backups, logs and working
files are synthetic and remain there. It neither discovers nor copies real
credentials. It uses fresh dynamic ports, never production 8642/18642. During the
short negative case, the synthetic API really binds `0.0.0.0` on its random port
with random authentication. It receives only authenticated `GET /v1/models`.

A fail-closed `sitecustomize` audit hook runs in the management CLI and every
detached child. It permits reads only from the synthetic case, pinned source,
test/library/runtime files and limited OS runtime metadata; writes stay in the
case. Runtime outbound network connections and arbitrary subprocesses are
blocked. The narrowly allowed Windows CPython socketpair is an asyncio wakeup
mechanism. The parent performs the read-only HTTP requests. Gateway startup
model-metadata warmup is disabled in the synthetic config. The successful report
requires zero runtime outbound connection attempts.

The driver establishes:

1. YAML host `127.0.0.1` plus BOM-prefixed `.env` host `0.0.0.0` produces an actual
   wildcard native HTTP listener. The real return-plugin factory logs its
   loopback refusal, and the TLS port does not listen.
2. After an official managed stop, a fresh `plan_hermes`/private journal pins the
   persistent environment host. Unrelated fields, random credentials, BOM and
   CRLF bytes remain exact.
3. Two subsequent official managed starts each expose native HTTP and HTTPS on
   loopback in the **same PID**. PID, creation time, executable and synthetic
   `HERMES_HOME` are checked before management stop.
4. The original authentication works on both transports. HTTPS verifies the
   synthetic CA and `localhost`; wrong authentication returns 401, while wrong
   CA and wrong hostname fail with `CERTIFICATE_VERIFY_FAILED`.
5. The real CLI's process-wide truststore injection remains installed. The
   plugin's local stdlib server context works under that startup chain. The
   uncorrected injected Windows server context instead fails with
   `SSLCertVerificationError: Peer sent no certificates to verify`.
6. The host fix survives restart, all test processes are stopped, and the
   public report plus all test log/audit files contain neither random secret.

No business POST, inference, WeChat, delivery, Gate or remote CFserver operation
exists in this driver. Successful isolation is not production acceptance.

## Reproduction

Create a fresh source checkout with `core.autocrlf=false`; check out the exact
commit above. Use separate Gateway and Hermes virtual environments. Install
Gateway `.[dev]` plus `psutil==7.2.2` in the Gateway environment, and
[`../hermes_return_probe/requirements.txt`](../hermes_return_probe/requirements.txt)
in the Python 3.14.7 Hermes environment. Do not add these dependencies to the
real Hermes environment.

PowerShell 5.1, from the Gateway repository, with the isolated directories used
by this task:

```powershell
& '.\.venv\Scripts\python.exe' '.\tests\hermes_listener_probe\probe.py' `
  --hermes-source '.\.task-deps\hermes-source' `
  --hermes-python '.\.task-deps\hermes-venv\Scripts\python.exe' `
  --output '.\.task-artifacts\listener-probe-new-unique-case'
if ($LASTEXITCODE -ne 0) { throw 'Isolated listener probe failed' }
```

The runner must permit local sockets and detached processes. A network sandbox
can block Python's internal asyncio socketpair before Hermes starts; that is a
test-host failure, not listener acceptance. Preserve failed directories. Do not
rerun a case by deleting its state or reusing its output directory.

The CI job `Official Windows manager and persistent listener config (0f4a98f)`
builds both environments and runs the same command. Only `report.json` is
uploaded. It intentionally does not upload `.env`, journals, certificates,
private keys or complete runtime logs.

Local verified result on 2026-10-11: `.task-artifacts/listener-probe-10/report.json`,
exit 0; HTTP 53247 and HTTPS 53248; fixed PIDs 24656 then 31240; seven read-only
HTTP responses, two wrong-CA and two wrong-hostname rejections, zero outbound
runtime attempts, twenty log/audit files with zero credential matches. These are
historical **synthetic** PIDs and ports, never operator recovery targets.
