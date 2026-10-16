# Native execution-session compatibility probe

This explicit probe targets official Hermes
`427d605d4985c2d49a795493ef6c5d66413057d1`.
It does not run during pytest collection, install dependencies, use a production
service, or call a paid model.

The real Gateway `HermesClient` drives the real official authenticated HTTP
handlers, Agent and SessionDB. Only the external model endpoint is a deterministic
loopback fixture. The probe creates a new HOME, database, work directory, listener
ports and in-memory service key. It inherits no application credentials, disables
plugins, tools, memory, compression and title generation, and reuses the existing
return probe's source verification and child I/O audit. The audit is a Python
boundary, not an OS sandbox. Only this probe's subprocesses are stopped.

Example in PowerShell (all arguments are paths; no credentials):

```powershell
$deps = 'C:\Users\Admin\.codex\worktrees\d02d\CF_agent-gateway\.task-deps'
& "$deps\gateway-venv\Scripts\python.exe" -B tests/hermes_execution_probe/probe.py `
  --hermes-source "$deps\hermes-new" `
  --hermes-python "$deps\hermes-venv\Scripts\python.exe" `
  --site-packages "$deps\hermes-venv\Lib\site-packages" `
  --output "$PWD\.task-artifacts\execution-session-probe-NEW"
```

The output directory must not already exist. Use the normal approval process
when the execution sandbox does not permit local loopback networking. The
interpreter/dependency paths above are existing read-only task dependencies.

The native run checks:

- A fresh root session leaves the legacy session's history, identity and lifecycle
  intact; it does not pretend to inherit hidden history.
- Explicit provider/model/options reach the local model; the reasoning-off option
  reaches it as `reasoning_effort=none`.
- Text, a fork with exact history verification, subsequent text, an actual process
  restart and a second fork retain the expected history and exact session tip.
- The same HTTP service authentication is retained across the isolated restart;
  unauthenticated session access is rejected.
- A failed primary is compared with a configured successful fallback, both with
  and without a `require_model_lock` body field on the OpenAI endpoint.
- The native session-chat lock is checked separately; that endpoint is not used
  as a substitute for Gateway dispatch.
- A second restart with both effective fallback configuration chains empty must
  yield `HermesResponseError` on primary failure, with no fallback invocation.

Observed on 2026-10-16 in
`.task-artifacts/execution-session-probe-r7/report.json`:

| Case | Native result |
| --- | --- |
| Fresh root, text, two forks, restart, legacy preservation | Passed |
| OpenAI primary failure with configured fallback | Primary once, fallback once, Gateway accepted success |
| OpenAI body `require_model_lock=true` | HTTP 200, fallback still invoked |
| Native session-chat confirmed lock with primary failure | No fallback, but HTTP 200 without failed/completed/error signal |
| Both fallback chains empty, primary failure | Primary once, fallback zero, Gateway `HermesResponseError` |
| Service-key scan of isolated output files | No matches |

An exit code of zero means these observations and boundary assertions completed.
It does **not** mean the existing OpenAI route guarantees no fallback. The report's
`no_fallback_on_openai_provider_failure=false` deliberately records that limitation.

The supported deployment boundary for a fixed future execution runtime therefore
requires protected configuration evidence that the dedicated execution Profile's
**effective** merged fallback chain is empty (both `fallback_providers` and legacy
`fallback_model`, including overlays). Public session/model APIs do not prove
this hidden configuration; a runtime digest or successful `/model` acknowledgment
is not a no-fallback lock. This evidence concerns future execution and does not
claim to attest every historical call.

The probe does not validate a deployed FileBridge plugin, real attachment bytes,
production Profile configuration, admission/claim/CAS transactions, or a live
WeChat conversation. Those have separate service tests and deployment acceptance.
