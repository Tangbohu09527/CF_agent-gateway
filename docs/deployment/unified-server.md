# Unified server first-install candidate

This candidate supports Debian 13 amd64 with booted systemd, local rootful
Docker/Compose v2 and an independently mounted `/srv/storage`. It composes the
existing three installers. Their resource APIs and lifecycle rules remain
authoritative. This is a first-install and same-input resume entry, not an upgrade.
The final [clean-device acceptance conditions](clean-device.md) still apply.

## Fixed sources and protected inputs

Use a clean checkout of a reviewed full Gateway commit. Its
[`server-components.json`](../../deploy/server-components.json) fixes FileBrowser
and WeChat management sources, the upstream WeChat image digest and official
Hermes reference. Management source, protocol source observation and runtime image
are separate fields. This candidate does not enable the filename protocol change.

Copy [`server-inputs.example.json`](../../deploy/server-inputs.example.json) to an
administrator-controlled, root-owned `0600` JSON file. Set the separate manager,
storage evidence, AI HTTPS endpoint/model, LAN binding, allowed client CIDRs and
TLS certificate/key/CA paths. TLS files must also be protected `0600` files; the
existing FileBrowser validator checks chain, name, key and lifetime. Certificate
verification stays enabled. The entry never formats or mounts a disk.

`staging` builds FileBrowser from the fixed source and records its image ID.
`candidate` requires a supplied immutable published image whose revision label
matches the source, and reviewed RAID storage. Default Python, PostgreSQL and
HTTPS base images are resolved once to digests and retained. The installation
receipt, rather than those tags alone, identifies the resulting combination.

## One entry

From that clean checkout, substitute its reviewed 40-character Gateway SHA:

```sh
sudo bash deploy/install-server.sh install \
  --gateway-commit FULL_GATEWAY_SHA \
  --inputs /root/cf-install-inputs/server.json
```

The entry prepares the host and separate manager, WeChat base configuration,
FileBrowser image/HTTPS/storage/bootstrap, Gateway image/configuration/database/
migration/core and bounded HTTPS proxy. Each component owns its resources.
Generated FileBrowser bootstrap and Hermes API credentials are independent of
the management account and stored in protected server files. No employee, chat,
model provider key or trusted task grant is invented by installation. Logs
default to `Asia/Shanghai`; database `TimeZone` remains UTC.

The result distinguishes **base ready**, **WeChat login pending**, **employee
authorization pending**, and **configuration-only Hermes business unproven**.
Installation never logs in, creates a WeChat runtime or opens Poll/Delivery.
Existing explicit login and onboarding remain separately authorized operations.

## Resume and diagnosis

Repeat the same command with identical inputs and versions to resume.
`/var/lib/cf-suite-install/journal.json` records attempts and completed stages.
Completed initialization is skipped; start/readiness reconciles existing services.
Changed input, certificate, supplied credential or source is rejected as a
different installation. Preserve the journal and use a reviewed upgrade plan.
Unknown existing assets are not adopted, removed or overwritten. FileBrowser
retains its original generated secrets across interrupted preparation.

```sh
sudo bash deploy/install-server.sh status \
  --gateway-commit FULL_GATEWAY_SHA \
  --inputs /root/cf-install-inputs/server.json
```

Status does not clone missing components, log in or open gates. It invokes
FileBrowser validation and Gateway's `diagnose-base` (database, core health and
business configuration), and may save a protected log. It does not require or
probe an AI computer before base installation can finish; Hermes connectivity
and business acceptance remain separate. The original Gateway `diagnose` still
performs its explicit external connectivity check. Private command logs may
contain installation secrets: never publish them. Export only bounded, reviewed,
redacted facts. Existing component backup/restore and upgrade runbooks remain
authoritative; there is no destructive reset option.

## Verification boundary

`Unified server candidate` CI executes the entry in pristine Debian with its own
real Docker daemon, PostgreSQL, FileBrowser and Gateway. It tests a port-conflict
failure/resume, approximately 18 MB ZIP, scoped credentials, management-account
revocation, service recreation and repeat execution. Four read-only systemd
observations are substituted. This layer cannot prove booted systemd, host
reboot or a published production combination. Read outcomes at the exact commit;
the existence of tests is not a success claim.

Fixed official Hermes `427d605d4985c2d49a795493ef6c5d66413057d1` can connect to
HTTP MCP by configuration, but the native probe found missing trusted per-call
task/session context in the shared API process. No legacy plugin/EXE is silently
installed as a fallback. Artifact/Response/Delivery and clean AI computer
acceptance remain explicit gaps.
