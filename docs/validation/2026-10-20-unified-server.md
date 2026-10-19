# 2026-10-20 unified server candidate and native Hermes handoff

This records isolated development only. No production host/API, formal Hermes,
real WeChat, production credential or employee material was accessed. No Poll/
Delivery window, login, migration or production installation occurred. The old
PDF/PNG and October 19 normal ZIP acceptance remain in the
[product report](2026-10-18-enterprise-product-zip.md), including its final section.

## Sources, ownership and entry

The server candidate is Draft PR24, based on product PR21
`91a235fca08362d8c1440e808df56946be3cc1e8`. FileBrowser PR7 fixes source
`47876e3a3d62affe777de8623697f62791fa5fa4` (product PR5 plus timezone PR6 and
interrupted-prepare preservation); WeChat PR11 fixes management source
`e56c7e4b606f1614e54210a391906f4be7440f5e` (timezone PR10 plus explicit root
base preparation). Their own repositories contain the implementation.
Gateway only orchestrates existing stages. No product source was copied between
repositories or merged into main. Original worktrees and historical evidence remain.

The [formal entry and protected input template](../deployment/unified-server.md)
are versioned here. The root-authorized command is:

```sh
sudo bash deploy/install-server.sh install \
  --gateway-commit FULL_SHA_OF_THIS_REVIEWED_CHECKOUT \
  --inputs /root/cf-install-inputs/server.json
```

The base installer distinguishes ready services, pending WeChat login, pending
employee authorization and unproven configuration-only Hermes business flow.
It does not open gates or adopt existing unknown assets. Same-input retries keep
credentials and completed initialization; upgrades are a separate reviewed action.
Asia/Shanghai is the log/host default, PostgreSQL SQL TimeZone remains UTC.
The selected runtime image is the fixed upstream digest in
[the manifest](../../deploy/server-components.json), not the filename candidate.

## Actual changes and local verification

Gateway adds the entry, component manifest, input allowlist, bounded HTTPS proxy,
protected attempt journal/command logs and dedicated disposable-Debian acceptance.
The existing command helper gains optional root-only private output logging.
FileBrowser adds a protected intent before writing preparation assets; replay
verifies all existing bytes/identities and keeps the original generated secrets.
WeChat only adds root-managed checkout/configure and defers manager login setup.
No root capability or project executable is exposed to a business model.

Five portable Gateway checks pass, including interrupted image-stage reuse;
Ruff format/lint and diff checks pass. FileBrowser's five preparation tests and
its deployment-assets workflow pass at the fixed component SHA. Regular backend
lint still reports 111 existing issues; its backend tree is unchanged by PR7.
Prior FileBridge fixture HTTP410 is not declared fixed or silently discarded.
WeChat's component, Debian preparation, forced-QR and media checks pass at PR11's
fixed SHA, with no real login in this task.

## Actual installation attempts

The first unified run
[39303826610](https://github.com/Tangbohu09527/CF_agent-gateway/actions/runs/39303826610)
at `297729718411886405764e794cb5bc5a8bf46e19` reached FileBrowser preparation.
The actual three installers completed host/dependency preparation, Controller,
WeChat checkout/configuration/no-login bootstrap, independent secrets and
FileBrowser image build. The validator correctly rejected the test harness's
default tmpfs mount permissions: `unsafe installation parent: /srv/storage`.
The failed attempt and second refusal are retained; neither proves the intended
port fault or a successful installation.

Test-only correction `7e3321ad67bee523d52e4df6fdf65617fe7b3550` gives the isolated
mount explicit mode0755 and requires the injected failure to be diagnosed as
port phase. The second run
[39305292066](https://github.com/Tangbohu09527/CF_agent-gateway/actions/runs/39305292066)
correctly rejected a loopback client CIDR in the harness at LAN validation. The
stronger assertion rejected this unexpected fault rather than calling it the
intended port test. The corrected input uses only its actual isolated Docker
network. The entry now preflights canonical unicast CIDRs and DNS labels using
the same contract, before initialization writes; five local checks pass.

The next run includes only allowlisted public source/image receipts in its
sanitized result. Automatic push/PR concurrency now shares a branch group to
avoid duplicate matrices. Accurate final CI is indexed in the cross-repository
closeout; these retained failures are not rewritten as success.

Run [39306797027 attempt2](https://github.com/Tangbohu09527/CF_agent-gateway/actions/runs/39306797027)
at `71c802da233cfd855d0b7436da543c823a937098` proved the intended port
rejection, then resumed preparation successfully. The next FileBrowser start
failed with a suppressed Docker error. No business-transfer or repeat success
is claimed for this attempt. The next test revision adds only fixed-project,
read-only container-state and bounded daemon diagnostics, redacting all generated
test secrets; no production implementation is changed to guess at the cause.

The diagnostic run
[39309184393](https://github.com/Tangbohu09527/CF_agent-gateway/actions/runs/39309184393)
at `e75705eeeda7291ba4d158da3131442088ea019f` captured the actual OCI failure:
the nested daemon's cgroup v2 `/docker` parent was in threaded mode, preventing
the unchanged FileBrowser CPU/memory/pids limits from being applied. The next
test-only revision enables delegation inside an explicit private cgroup namespace
before starting the daemon, following the cgroup portion of
[fixed official Moby DinD preparation](https://github.com/moby/moby/blob/4eee42c0243b1494b5df6411c1c208f7408461d8/hack/dind).
Retries are bounded and a resource-limited smoke container must succeed before
the application build. The test neither shares the host Docker socket/cgroup
namespace nor removes any product resource, authentication or TLS constraint.

Run [39311006559](https://github.com/Tangbohu09527/CF_agent-gateway/actions/runs/39311006559)
at `0d057c8c7f717e240ab0ad7f5fab8a7df37bf803` passed the private namespace
CPU/memory/pids smoke and no longer reported the OCI cgroup failure. FileBrowser
instead restarted with exit126. Its fixed bind-mounted entrypoint was checked
out under the root installer's 077 umask; Git executable mode therefore became
0700, incompatible with the unchanged runtime user10001. The next revision
validates and sets0755 on only that reviewed regular code file after the fixed
checkout. The global umask, other checkout files, credentials, ownership and
runtime identity remain unchanged. Unknown modes/symlinks/hardlinks stop.
Five portable checks pass locally; the new real-POSIX narrow-mode regression is
explicitly skipped on Windows and must run in Linux. The same service-start
reproduction and runtime identity/mode assertions remain in the unified job.

The test uses a pristine Debian13 container, independent real dockerd (no host
socket), real services and strict HTTPS, approximately 18 MB/12 synthetic members,
ordinary scoped business credentials, an injected failure/resume, service
recreation, management password locking and repeat entry. Four read-only systemd
observations are substituted, and storage is tmpfs test-disk. This cannot prove
booted systemd, durable-host/RAID storage, OS reboot or a published production
combination. Existing single-component clean-device A/B results are separate.

Run39313438306 at `da369b579716c1dc825d729829c9f172e417df46` completed
FileBrowser start/bootstrap-finish and Gateway build/configure/database/migrate/
start. HTTPS prepare completed, but HTTPS start reported an unhealthy container.
The same run showed unreadable public FileBrowser CSS/favicon assets under
UID10001. FileBrowser PR7 commit `47876e3a3d62affe777de8623697f62791fa5fa4`
normalizes only image-public asset modes and verifies the actual built image
under UID/GID10001 without mounts/network/write access. Its13 image-workflow
unit tests pass locally. The manifest fixes this revision; production
ownership, secrets, data and application identity are unchanged.

Run39316302951 at `045161992306e775d35eeaede66cc527df19b894`
confirmed the FileBrowser asset fix: its ordinary service was healthy and the
bounded startup permission-error list was empty. HTTPS actually restarted with
`open() "/etc/cf-https/nginx.conf" failed (13: Permission denied)`.
The new directory's requested0750 was masked to0700 by the unchanged protected
umask077. The next fix explicitly sets0750 only on that fixed root:10001
read-only mount directory. Parent state stays0700 and mounted files stay0440;
there is no TLS or identity bypass. A real-POSIX umask regression covers this.
The isolated revocation check now requires its generated management password to
work through sudo before clearing its ticket, locking it and rejecting the same
password; business credentials remain independent. Full-run outcome is indexed
in the cross-repository closeout.

## Official Hermes pure-configuration result

Dependency audit correction: the earlier successful probe installed a
selected dependency subset, not official427's complete supported installation.
Its Python3.12 Windows run is diagnostic only; official427 supports Python3.14.
PR23 then passed the fixed official installer's dependency stage and full
default extras in CI39315976156 and final-head CI39317205768. Python3.14.7,
53 applicable requirements, clean source and three lock/input hashes were
actually audited. This is dependency-stage evidence, not all official install
stages or new-Windows-machine acceptance. Earlier green subset probes do not
prove the official environment.

The independent Draft PR23 fixes official Hermes at
`427d605d4985c2d49a795493ef6c5d66413057d1` and FileBrowser at product
`3d556786068994826b8105ff4a1d11572b84b1b1` for its real scoped TLS HTTP fixture.
[Final source/report](https://github.com/Tangbohu09527/CF_agent-gateway/blob/57bb2c2830f292d5167f0bda449afafbe0a528fd/docs/validation/2026-10-20-native-http-mcp.md)
and [exact-head CI39317205768](https://github.com/Tangbohu09527/CF_agent-gateway/actions/runs/39317205768)
passed the boundary probe. The official client is unchanged, with a clean HOME,
no project plugin/EXE/PYTHONPATH and no production model key. The deterministic
model substitute actually selects tools; native text/image tool messages and
FileBrowser query/export execute. Twelve members of the approximately 18 MB
export are verified against the selected input members.

The official API, MCP and model substitute use isolated loopback HTTP. Real TLS
applies to the FileBrowser leg. This does not prove cross-machine MCP HTTPS or a
new Windows computer; those remain separate from the Linux dependency-stage test.

Small native configuration shape, **only for the proved transport/content layer**:

```yaml
mcp_servers:
  cf:
    url: https://tools.example.invalid/mcp
    headers:
      Authorization: "Bearer <restricted-test-transport-credential>"
    identity_header:
      name: X-Hermes-Profile
      value_from: profile
platform_toolsets:
  api_server: [mcp-cf]
```

The placeholder endpoint is not an implemented production Gateway MCP service.
Official model/API configuration is separate. Static profile/transport headers
are not current-task authority. Two official API sessions share one transport;
all eight tools/call metadata objects are empty. The fixed handler passes only
model arguments to session.call_tool, so the server cannot bind the current
Gateway task/session/employee securely. The diagnostic policy sentinel rejects
`trusted_task_context_missing`. It is not a production business handler.

Thus current-task authorization, Artifact/Response publication, WeChat substitute
delivery and the full cross-session/expiry/replay matrix remain **not passed**.
No shared Admin token, model-specified identity or weakened protection bypasses
this gap. A task-specific limited grant and dedicated official process/profile
would require a new trusted task lifecycle; it is a possible minimum alternative,
not implemented or claimed complete here. No client upgrade was attempted.

## Final boundaries and next work

The final user standard remains one official supported-Debian server entry plus
only official Hermes and a few AI-side parameters, independent of Codex/temporary
receipts and temporary maintenance credentials. Existing production plugins and
two EXEs remain unchanged and do not meet that final standard. A hidden client
installation is not an accepted fallback.

Normal WeChat ZIP inbound, filename preservation and archived-path refinement
remain separate unfinished work. Small library organization and initial file
Skills can proceed alongside deployment convergence; query/delivery, archive and
integrity-check rules still need defined product/version/category permissions and
real samples. No full library, Skills Runtime, RAG, group/multi-user flow, complete
chat history, broadcast or Gateway Web is delivered by this candidate.

No production credential or authorization was created, renewed or retained; no
production cleanup connection is required. Exact final CI, retained failure and
cross-repository documentation commit will be recorded at closeout.
