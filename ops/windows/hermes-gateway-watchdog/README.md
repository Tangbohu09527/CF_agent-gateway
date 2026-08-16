# Hermes Gateway Watchdog for Windows

This directory contains a current-user PowerShell watchdog for Hermes Gateway on a
Windows AI host. It is an operational compensation for a known Windows Hermes Desktop
startup conflict. It is not a permanent upstream fix.

## Why the watchdog is needed

Hermes Desktop and Hermes Gateway are separate processes:

- Hermes Desktop provides the interactive desktop application.
- Hermes Gateway serves the network endpoint used by CF_agent-gateway and listens on the
  AI host LAN address, normally on TCP port `8642`.

Hermes Gateway can start automatically at Windows logon and return a healthy response from
`/health`. On the first subsequent launch of Hermes Desktop, the Desktop startup flow can
terminate the already-running standalone Gateway as part of the known Windows orphan-reap
behavior. After Desktop initialization finishes, running `hermes gateway start` again allows
Desktop and Gateway to coexist normally.

The watchdog waits for Desktop initialization to settle, confirms that the HTTP health
endpoint is still unhealthy, and then starts Gateway again. Users can open and use Hermes
Desktop normally. The watchdog does not prevent Desktop startup or alter Hermes source code,
executables, or installation files; it only invokes the installed Gateway start command.

## Scope and safety boundaries

The watchdog is deliberately external to Hermes and CF_agent-gateway production runtime:

- It does not modify Hermes source code or the Hermes installation directory.
- It does not change the Hermes `Hermes_Gateway` scheduled task, Startup VBS, configuration,
  or PowerShell profile.
- It does not stop or kill Hermes Gateway.
- It does not connect to or modify CFserver.
- It does not read or modify the CF_agent-gateway production database.
- It does not inspect WeChat messages or Hermes session content.

An upstream Hermes fix may eventually make this watchdog unnecessary. Until then, treat it
as a reversible Windows host operations measure, not as a claim that the upstream Desktop
startup issue has been fixed.

## Runtime behavior

The watchdog validates health through HTTP. A process ID or listening port alone is not
considered healthy. The configured URL must return a successful HTTP response whose JSON
body contains `"status": "ok"`.

The default cycle is:

1. Allow `45` seconds of startup grace after the watchdog starts.
2. Poll `/health` every `5` seconds with a `3` second connection timeout and a `5` second
   total request timeout.
3. When health first fails, wait a `15` second settle delay and check again.
4. If the second check is still unhealthy and maintenance pause is not active, run
   `hermes.exe gateway start` using the absolute executable path saved during installation.
5. Wait at most `30` seconds for the start command to exit, while suppressing overlapping start commands.
6. Check `/health` after the start attempt. Record recovery success only after three
   consecutive healthy checks.
7. On a failed recovery, use bounded exponential backoff, capped at `60` seconds, before
   another start attempt.

A per-user, per-configuration named mutex permits only one watchdog instance. This prevents
overlapping scheduled-task triggers and concurrent `hermes gateway start` calls. Repeating
the installer updates the same `CF_Hermes_Gateway_Watchdog` task instead of creating another
task.

## Requirements

- Windows PowerShell 5.1 or later
- A current-user Hermes installation with `hermes.exe` discoverable during installation
- A Hermes Gateway health endpoint reachable from the Windows AI host
- No administrator privileges

Run the scripts from a normal PowerShell session. Do not elevate solely for this watchdog.

## Install

From this directory, preview the installation without changing files or scheduled tasks:

```powershell
& .\Install-HermesGatewayWatchdog.ps1 `
  -HealthUrl 'http://<AI_HOST_LAN_IP>:8642/health' `
  -DryRun
```

PowerShell `-WhatIf` is also supported:

```powershell
& .\Install-HermesGatewayWatchdog.ps1 `
  -HealthUrl 'http://<AI_HOST_LAN_IP>:8642/health' `
  -WhatIf
```

Install for the current user:

```powershell
& .\Install-HermesGatewayWatchdog.ps1 `
  -HealthUrl 'http://<AI_HOST_LAN_IP>:8642/health'
```

The installer:

- checks the PowerShell version;
- resolves `hermes.exe` and stores its absolute path;
- copies the watchdog files to
  `%LOCALAPPDATA%\CF\HermesGatewayWatchdog`;
- creates a local `watchdog.config.json`;
- creates or updates the current-user scheduled task
  `CF_Hermes_Gateway_Watchdog`;
- starts the task at user logon in a hidden PowerShell window; and
- configures scheduled-task restart behavior.

The first installation requires `-HealthUrl`. A repeated installation can reuse the saved
configuration. Rerun the installer after a Hermes update if the resolved `hermes.exe` path
has changed.

`watchdog.config.example.json` documents the configurable settings. The shipped defaults
are:

| Setting | Default |
| --- | ---: |
| `StartupGraceSeconds` | `45` |
| `PollIntervalSeconds` | `5` |
| `FailureSettleDelaySeconds` | `15` |
| `HealthConnectTimeoutSeconds` | `3` |
| `HealthTotalTimeoutSeconds` | `5` |
| `GatewayStartTimeoutSeconds` | `30` |
| `MaximumRestartBackoffSeconds` | `60` |
| `RecoveryVerificationTimeoutSeconds` | `45` |
| `RequiredConsecutiveHealthyChecks` | `3` |

Keep `HealthUrl` limited to the health endpoint. Do not put tokens, API keys, database URLs,
or other credentials in the watchdog configuration.

## Status

Run the installed status command:

```powershell
& "$env:LOCALAPPDATA\CF\HermesGatewayWatchdog\Get-HermesGatewayWatchdogStatus.ps1"
```

The status output reports whether the scheduled task exists, whether the watchdog process
is running, whether maintenance pause is active, current `/health` health, TCP `8642`
listener state, the Gateway process ID when identifiable, the most recent recovery time,
and the current log path. It does not output credentials.

The listener and process fields are diagnostic context only. HTTP plus JSON health remains
the recovery decision.

## Pause and resume

Pause before updating or repairing Hermes, changing its installation, or performing fault
investigation:

```powershell
& "$env:LOCALAPPDATA\CF\HermesGatewayWatchdog\Pause-HermesGatewayWatchdog.ps1"
```

Pause does not terminate the watchdog. It continues to run, observe health, and log its
state, but it never starts or restarts Gateway while paused.

After Hermes maintenance is complete, rerun the installer if the Hermes executable path
changed, then resume recovery behavior:

```powershell
& "$env:LOCALAPPDATA\CF\HermesGatewayWatchdog\Resume-HermesGatewayWatchdog.ps1"
```

Confirm the result with the status command. Pause and resume transitions are written to the
watchdog log.

## Logs and troubleshooting

Runtime logs are stored below:

```text
%LOCALAPPDATA%\CF\HermesGatewayWatchdog\Logs
```

Logs rotate by size and retain a bounded number of files. They include timestamps,
watchdog startup and shutdown, health transitions, Gateway start attempts and exit codes,
recovery outcomes, and pause or resume state. They must not contain tokens, API keys,
WeChat messages, Hermes conversations, or database connection strings.

Start troubleshooting with:

```powershell
& "$env:LOCALAPPDATA\CF\HermesGatewayWatchdog\Get-HermesGatewayWatchdogStatus.ps1"
Get-Content "$env:LOCALAPPDATA\CF\HermesGatewayWatchdog\Logs\watchdog.log" -Tail 100
```

Then check these conditions:

1. The health URL uses the AI host LAN address and ends in `/health`.
2. A direct request returns successful JSON with `status` equal to `ok`.
3. Windows Firewall permits the expected TCP `8642` traffic.
4. Maintenance pause is not still active.
5. The scheduled task is present and the watchdog process is running.
6. The saved `hermes.exe` path is still valid after a Hermes update.
7. The log shows bounded retry delays rather than repeated immediate starts.

Do not troubleshoot by killing Gateway from the watchdog scripts. The watchdog intentionally
has no process-reaping behavior.

## Uninstall

Preview uninstall without making system changes:

```powershell
& "$env:LOCALAPPDATA\CF\HermesGatewayWatchdog\Uninstall-HermesGatewayWatchdog.ps1" -DryRun
```

Uninstall the scheduled task and watchdog while retaining logs:

```powershell
& "$env:LOCALAPPDATA\CF\HermesGatewayWatchdog\Uninstall-HermesGatewayWatchdog.ps1"
```

Delete retained watchdog logs as part of uninstall only when they are no longer needed:

```powershell
& "$env:LOCALAPPDATA\CF\HermesGatewayWatchdog\Uninstall-HermesGatewayWatchdog.ps1" -RemoveLogs
```

Uninstall stops and removes only `CF_Hermes_Gateway_Watchdog` and its watchdog process. It
does not stop Hermes Gateway, remove the Hermes `Hermes_Gateway` startup entry, or modify
Hermes configuration, installation files, or PowerShell profile.

## Tests

Run the self-contained test script from a normal PowerShell session:

```powershell
& .\Test-HermesGatewayWatchdog.ps1
```

The tests use injected health and start behavior. They do not terminate a real Hermes
process, contact production CFserver, or require a live Gateway.

## Real-host validation still required

Automated tests validate the state machine and script safety boundaries, but they do not
establish production behavior. The following remain explicit Windows AI host validation
items:

- Watchdog startup after Windows logon
- Automatic Gateway recovery when Hermes Desktop is opened for the first time
- Repeated Hermes Desktop close and reopen cycles
- Pause and Resume behavior during a Hermes update
- Continuous reachability from CFserver to Hermes
- Real WeChat message behavior, including uninterrupted delivery or safe queuing during the
  recovery interval

Record those checks separately after controlled host validation. Do not treat the presence
of this watchdog or its unit tests as evidence that they have already passed.
