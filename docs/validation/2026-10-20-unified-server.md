# 2026-10-20 unified server candidate and native Hermes handoff

This records isolated development only. No production host/API, formal Hermes,
real WeChat, production credential or employee material was accessed. No Poll/
Delivery window, login, migration or production installation occurred. The old
PDF/PNG and October 19 normal ZIP acceptance remain in the
[product report](2026-10-18-enterprise-product-zip.md), including its final section.

## Sources, ownership and entry

The server candidate is Draft PR24, based on product PR21
`91a235fca08362d8c1440e808df56946be3cc1e8`. FileBrowser PR7 fixes source
`e9fc639d1916eb2d1aa61e869737bee2b38b8601` (product PR5 plus timezone PR6 and
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

The test uses a pristine Debian13 container, independent real dockerd (no host
socket), real services and strict HTTPS, approximately 18 MB/12 synthetic members,
ordinary scoped business credentials, an injected failure/resume, service
recreation, management password locking and repeat entry. Four read-only systemd
observations are substituted, and storage is tmpfs test-disk. This cannot prove
booted systemd, durable-host/RAID storage, OS reboot or a published production
combination. Existing single-component clean-device A/B results are separate.

## Official Hermes pure-configuration result

The independent Draft PR23 fixes official Hermes at
`427d605d4985c2d49a795493ef6c5d66413057d1` and FileBrowser at product
`3d556786068994826b8105ff4a1d11572b84b1b1` for its real scoped TLS HTTP fixture.
[Final source/report](https://github.com/Tangbohu09527/CF_agent-gateway/blob/2ada096c8a30ecbde771fb32f7536790709b0cd9/docs/validation/2026-10-20-native-http-mcp.md)
and [exact-head CI39302780142](https://github.com/Tangbohu09527/CF_agent-gateway/actions/runs/39302780142)
passed the boundary probe. The official client is unchanged, with a clean HOME,
no project plugin/EXE/PYTHONPATH and no production model key. The deterministic
model substitute actually selects tools; native text/image tool messages and
FileBrowser query/export execute. Twelve members of the approximately 18 MB
export are verified against the selected input members.

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
