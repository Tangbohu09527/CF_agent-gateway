# Gateway to agent-wechat runtime contract

## Contract boundary

Contract version: **1**

This document defines the minimum stable runtime boundary exposed by
CF_agent-gateway to CF_agent-wechat. It does not define a general automation
platform, change the dispatch or delivery state machines, or prove operation on
a real CFserver. CFserver deployment and account acceptance remain external
validation work.

Run the control entry point from a deployed repository checkout:

```console
deploy/wechat-runtime-control <contract|stop|start|status>
```

## Published contract

`contract` performs no Docker operation and exits zero after printing only:

```json
{
  "contract_version": 1,
  "delivery_worker_service": "delivery-worker",
  "dispatch_worker_service": "dispatch-worker",
  "poll_worker_service": "worker",
  "token_container_path": "/run/secrets/cf-agent-wechat-auth-token",
  "token_mode": "file"
}
```

The JSON key order is not significant. No database address, credential, or
Secret metadata is part of the public contract.

## Execution identity

`contract` does not call Docker or read the Token. Any user who can read the
deployed repository may run it without elevated privileges.

`stop`, `start`, and `status` must be run by `root` or by a trusted
administrative identity that can access the host's rootful Docker daemon, read
the protected host Token File, and read the production Compose file and its
env-file. Missing any required access fails closed.

On CFserver, keep `linxi` out of the `docker` group. Establish sudo credentials
once, then use non-interactive sudo for each control operation:

```console
sudo -v
sudo -n /opt/cf-agent-gateway/deploy/wechat-runtime-control <stop|start|status>
```

Do not add ordinary users to the `docker` group or loosen the Token File's
established ownership or mode to make it readable.

## Token File

Production uses `CF_AGENT_WECHAT_TOKEN_FILE` as its only authoritative token
source. The production Compose contract sets it to:

```text
/run/secrets/cf-agent-wechat-auth-token
```

The default host source is:

```text
/srv/storage/cf-agent-wechat/secrets/auth-token
```

Set `CF_AGENT_WECHAT_TOKEN_HOST_FILE` on the Compose host only when a
site-specific source path is required. This variable contains a path, never the
Token. The bind mount is read-only and exists only on `worker` and
`delivery-worker`. The control entry point resolves the effective bind source
from structured `docker compose config` output, then validates that host path
with `lstat` and a non-following open immediately before `start`. This rejects a
symlink present at validation time; Docker later reopens the path, so its parent
directories must be trusted and non-writable by the service identity during `start`.

Provision the host file as a regular file owned by service UID/GID
`10001:10001`, with mode `0400` or `0600`. The runtime validates the file
with a non-following open and file-descriptor status check. It rejects:

- symlinks and non-regular files;
- files with more than one hard link;
- Linux modes other than `0400` or `0600`;
- files smaller than 1 byte or larger than 4096 bytes;
- bytes outside visible ASCII `0x21` through `0x7e`;
- whitespace, a trailing newline, NUL, and all control characters;
- a file changed between validation and bounded reading.

Create the value without a newline, for example with a protected provisioning
tool equivalent to `printf %s`. Never use `echo` for this file.

`CF_AGENT_WECHAT_TOKEN` remains a development compatibility source. If
`CF_AGENT_WECHAT_TOKEN` and `CF_AGENT_WECHAT_TOKEN_FILE` are both present,
even if either value is empty, startup fails closed. There is no precedence,
fallback, or silent override.

Token resolution failures expose only these fixed codes:

- `token_missing`
- `token_source_conflict`
- `token_file_invalid`

Errors and logs never include the Token, its hash, its length, file content, or
the configured file path.

## Controlled services

The control entry point may manage only:

- `worker`
- `delivery-worker`

It must never stop, start, or restart:

- `gateway`
- `postgres`
- `dispatch-worker`
- `migration`

The dispatch worker name is published for coordination only; it is not a
controllable service in contract version 1.

## Control behavior

### stop

`stop` asks Docker Compose to stop exactly `worker` and
`delivery-worker`, then inspects both containers until neither is running.
Other services are not command arguments. Success prints
`{"stopped":true}`.

### start

`start` first performs Token and rendered-Compose preflight. It reads the
actual service set from `docker compose config --services`. The controlled
set remains exactly `worker` and `delivery-worker`; every other defined
service is protected. The controller snapshots the Container IDs of those
protected services, then runs bounded Compose
`up --no-start --no-deps --force-recreate worker delivery-worker`. This prepare
step recreates only the controlled containers without starting them or their
dependencies. The controller requires exactly one unique Container ID for each
controlled service, verifies that the defined service set is unchanged after
prepare, and verifies that every protected Container ID is unchanged.

An external PostgreSQL container that is not a Compose service is never queried.
If an Overlay defines a `postgres` service, it automatically belongs to the
protected set and receives the same Container ID fencing as every other
non-controlled service.

The controller then runs `docker start` with those two exact Container IDs and
enters the existing readiness loop. A Compose CLI return is not readiness
evidence. Success is proved only when Docker reports both healthchecks as
`healthy`, both heartbeat files are fresh, and both container configurations
satisfy the Token File contract. Once `ready` is true, `start` returns the
existing five-field status object immediately.

Prepare, launch, and readiness share the operation deadline. Subprocesses receive
no stdin, capture output without forwarding it, and run in a separately
terminable process group so a timed-out Compose CLI cannot remain as a hidden
gate. A failure after prepare begins rolls back by stopping only `worker` and
`delivery-worker`; rollback failure is reported separately. The control script
issues no database or migration command. Once running, the Workers resume their
normal durable processing and may write business state.

This launch split still requires CFserver acceptance after deployment: repeat
fresh QR, confirm automatic release as soon as readiness is true, recheck the P0
database no-side-effect baseline, process one unique message through the full
chain, and verify Docker restart and host reboot behavior. That production
acceptance has not been completed by this repository change.

### status

`status` makes no state change and prints a redacted object with exactly these
fields:

```json
{
  "delivery_health": "healthy",
  "heartbeat_age": 2.5,
  "ready": true,
  "token_contract_valid": true,
  "worker_health": "healthy"
}
```

Health values are `healthy`, `starting`, `unhealthy`, `stopped`,
`not_created`, or `unknown`. `heartbeat_age` is the larger age in seconds
of the two worker heartbeat timestamps, rounded to milliseconds. It is
`null` if either heartbeat cannot be read. `token_contract_valid` verifies
the validated host file, the exact file environment entry, absence of the
development Token environment entry, and a read-only bind from that same host
source on both controlled containers. An invalid host file returns the same
five-field status schema with unknown health, `token_contract_valid: false`,
and `ready: false`; no path or file metadata is emitted.

`ready` is true only when both health values are `healthy`, both heartbeats
are within the freshness limit, and both Token File configurations are valid.

## Timeouts and exit codes

The default operation timeout is 180 seconds. Override it with
`--timeout-seconds N`, where `N` is greater than zero and no more than 600.
This is one wall-clock budget shared by the action's Docker calls, container
inspection, readiness polling, and heartbeat checks; each stage receives only
the remaining time.
Heartbeat freshness uses
`CF_GATEWAY_WORKER_HEARTBEAT_MAX_AGE_SECONDS`, defaulting to 30 seconds.

Exit codes are:

| Code | Meaning |
| --- | --- |
| 0 | The requested operation succeeded; for `status`, `ready` is true. |
| 1 | Docker was unavailable, the action failed or timed out, or `status` was not ready. |
| 2 | Command-line arguments were invalid. |

Operational failures print only a fixed JSON `error_code` to standard error.
Docker stdout, stderr, inspect payloads, and exception text are never forwarded.
Start-stage failures use `runtime_start_prepare_failed`,
`runtime_start_launch_failed`, `runtime_start_ready_timeout`, or
`runtime_start_rollback_failed`. Token preflight retains its existing stable
Token error codes.

## Secret management

Do not place the agent-wechat Token in Compose `environment`, `env_file`,
`command`, labels, healthchecks, logs, screenshots, or pull requests. Remove
the legacy development variable from production environment files before
deployment. Rotate the protected host file through the site secret-management
process, then restart the two controlled workers and require `status` to
return `ready: true`.

The Gateway API, PostgreSQL, Hermes, and CFserver credential lifecycles are
outside this versioned Token File contract.
