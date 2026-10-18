# 2026-10-18 enterprise product ZIP candidate verification

This report covers the Gateway candidate on PR20's `ef3119aeecd987165255c066218441f6aef8fb0a` base. It records local candidate evidence, not a production deployment or real-model/WeChat acceptance. Exact product/CI SHA is the containing commit and subsequent parent execution record; earlier production remains a separate fact.

## Complete isolated chain

At 2026-10-18 11:12 UTC, `tests/hermes_product_zip_probe.py` completed an official Hermes load and API execution on fixed source `427d605d4985c2d49a795493ef6c5d66413057d1`, under the ordinary Admin token. HOME, state DB, ledger, work roots, input, Gateway DB, credentials and listeners were isolated. Audit recorded zero denied operations and a closed host. Only the product and artifact-return toolsets were exposed; the official deferred-tool bridge supplied real schemas and executed registered handlers/middleware.

The model was a deterministic HTTP substitute and WeChat was an isolated HTTP protocol substitute. FileBrowser was the coordinated candidate's real TLS/auth/resource/product/audit handlers with a temporary source, DB and non-admin token. Fixture-specific setup is not production identity or WeChat ingress evidence.

The source was the **complete** derived 米脂小米 product ZIP from the authorized read-only source collection: 18,166,503 bytes, 12 images, SHA256 `0e4456af887684854217dbc3d11d9104fad54dff2df82e3c41bd25e0c036c075`. No existing ZIP was present in the source. Four `.WeDrive` synchronization metadata files were excluded and separately inventoried by the FileBrowser task. The parent inventory covers all 176 ordinary source files; this probe does not substitute its own partial scan for that inventory.

Observed chain:

| Stage | Result and evidence |
| --- | --- |
| Stream upload | Complete derived ZIP uploaded once through the fixed native client; later probe reused that verified fixture input. |
| Inspect | Real member manifest and hashes returned from FileBrowser. |
| Preview | An actual image preview entered the model HTTP input; not inferred from download success. |
| Classify/import | All 12 images published in an isolated formal product version. Classification was deterministic fixture logic, **not real AI judgment**. |
| Export | New ZIP from selected main/detail members, original package and images retained. |
| Return | Trusted current-task reference → normal return tool → Gateway direct verified download → Artifact → completion ACK → Response. |
| Delivery | Normal production sender delivered to the explicit protocol substitute. Its actual nested `file.data` was decoded and compared byte-for-byte to the stored Artifact. |
| Members | All 12 output member sizes and SHA256 match the independently inspected original members. |
| Receipt | No real WeChat UI or user receipt is claimed. |

The output was **18,167,075 bytes**, SHA256 `77c3ad0cf868a6e3862fbe949c679b5cea73def4596ffcd3e4160f1e47add498`, filename `完整商品图 (official-product-08).zip`. A re-packed ZIP need not match the uploaded ZIP's outer hash. The FileBrowser agent subsequently repeated the same export through the actual fixture HTTP API and verified the same output descriptor and whole ZIP hash.

Isolated database IDs: Message 1, Dispatch 1 (one attempt), Delivery 1, Artifact `6a6d4002-54ae-5ccf-8c89-627820007620`. These IDs are **not production IDs**. Actual model-visible schema hashes were:

- `filebrowser_product_materials`: `75e2488bb64150cb64e36b3f99f0d91af737afdcf3c45bbb02ba9141efaa428e`
- `cf_return_current_chat`: `dc0e0a614bb13c9b7636b1ab3e80d5b49174c46564c4707d3fde2e1d58b412c5`

## Focused tests and failures retained

The new ZIP tests exercise real Gateway ASGI/SQL authorization, import, seal, Response/Delivery and protocol-substitute bytes, plus unsafe archives, mismatched identity/session/Profile/manifest/hash, revocation, replay and original-only authority. Controlled-task tests exercise normal admission, exact claim, capture without Delivery, duplicate input, runtime drift/expiry, identity revocation, own-thread continuation and restart capture. Existing affected return/plugin/config/media/admission/claim/response regressions are run separately and final exact totals recorded with the candidate handoff.

One focused set completed **61 passed** before the two additional controlled runtime/continuation tests. The final controlled-source plus normal admission/claim/response regression set completed **60 passed**, including those two added cases. The earlier affected return/config/media/plugin set completed **329 passed, 1 skipped**. Sets overlap and must not be added into a unique test count. The skip required Windows symlink privilege. A broader exploratory Windows invocation found three existing POSIX staging-root tests rejected by `invalid_media_staging_root`; the guard was not weakened. Linux CI is required for those platform cases. These results do not assert that the entire repository is green.

Preserved probe attempts document actual failure boundaries:

- Fixture 1's upload HTTP500 was a temporary audit-store stub with an uninitialized map; no file or partial original was published. The fixture was corrected rather than the production audit checks removed.
- Probe 2 had an incomplete synthetic-message fixture; probe 3 used the parser-only Python rather than the full Hermes environment. Neither was a product business success.
- Probe 4 reached successful export/Artifact/ACK but the deterministic peer mishandled a subsequent no-tools official summary/title request. Probe 5 exposed same-name export conflict; FileBrowser now returns 409 for conflicts and produces deterministic identical exports for identical selections.
- Probe 6 confirmed the no-tools auxiliary request defect; probe 7 completed Delivery but its final test assertion read the protocol file envelope at the wrong nesting level. Probe 8 corrected that assertion and completed all checks. Prior attempts were preserved, not rewritten as successful.

## Evidence index and limits

Private evidence under the independent Gateway worktree `.task-artifacts/official-product-08/` includes `safe-summary.json`, `events.jsonl`, `host-ended.json`, native client audit, Gateway DB and Artifact bytes, and `loaded-filebridge-inventory.json` hashing the frozen plugin copies actually loaded. The FileBrowser worktree's `joint-fixture-4/repeated-export.json` records its repeat-export check. Raw credentials, model contents and business files are not committed. The fixture was stopped after the check; no production services were touched by this probe.

This document shares the sanitized execution facts. Raw local logs and fixture databases are **not remotely shared evidence**; readers cannot infer they have inspected them from a local path or hash alone. The parent report must add exact commit/PR/CI, built images, runtime installation proof, real-model operation evidence and any production snapshot. No candidate code here proves those later steps happened.

Related contract and minimal deployment boundary: [enterprise product ZIP integration](../development/enterprise-product-zip.md). Previously accepted PDF, text and PNG results, including PNG re-encoding and the retained Skill edit, remain in [the original read/return report](2026-10-18-inbound-read-return.md); this candidate does not replace those facts.


## Follow-up candidate and remaining ingress proof

The narrow read-only FileBrowser account resolves downloads relative to its account scope. Product commit `663a7680f13d5996b05b94188350ea1d539a33cd` adds an explicit `download_scope_prefix`, restricted to the exact authorized export root. Authorization and the producer descriptor keep their full path; only the fixed reader API request is translated. The targeted enterprise tests pass 22 cases, including unchanged bytes and rejected sibling prefixes. No token permission was widened.

`hermes_product_zip_probe.py --ingest --inbound-worker …` now selects the normal Gateway ZIP admission, media worker and POSIX staging before the official HostAdapter consumer and native FileBridge download/upload. The native inbound worker and enterprise client are separate fixed binaries. The dedicated Linux CI starts the real FileBrowser TLS/auth/audit/product handlers and uses a deterministic 18 MiB, 12-image ZIP, model and WeChat substitutes. The new mode passed at `33224198ca7c699afcfcf07d3cd00673e1466dfc` in [official ZIP CI 39128302340](https://github.com/Tangbohu09527/CF_agent-gateway/actions/runs/39128302340). Windows explicitly rejects this mode before creating a case; its POSIX staging contract is not bypassed.

The previously completed full real-material enterprise chain remains the independent `official-product-08` evidence above. That run started with native upload, and does not establish incoming ZIP HostAdapter coverage. The FileBridge follow-up also corrects the missing ZIP MIME in the host-control contract; the initial `80fb038` candidate alone must not be described as complete inbound ZIP support.


## Official ZIP ingress closure and output capacity

The `33224198ca7c699afcfcf07d3cd00673e1466dfc` run used FileBridge `b63afa6375a65f592ea20b984ba248d84d558451` and official Hermes `427d605d4985c2d49a795493ef6c5d66413057d1`. The deterministic synthetic input was 18,156,370 bytes / 12 images. Normal Gateway ingress, one media attempt, POSIX staging, host lease, `filebrowser_ingest_product_zip`, preview, import, export and return all executed. The actual image preview entered the model HTTP request. Artifact/Response/Delivery succeeded against the protocol substitute, and all 12 member hashes matched. Output was 18,156,942 bytes, SHA256 `c37cd162e6ec1432a843891685c544a8ada64760cf55e1319a711fc74fa4bb4d`. The host closed through its official `/closed` callback (`closed` / `host_closed`) with authority cleared. These are isolated IDs Message/Dispatch/Delivery 1, not production records.

Three preceding probe defects are retained as failures: the synthetic host grant used Base64url where the real contract requires standard Base64; copying a fixture token produced mode 0644 on Linux and the client correctly refused it; and the final assertion incorrectly expected the worker-only revoked/completed state instead of actual host closure. Corrections changed isolated fixture setup or assertions, not runtime authentication, ACL enforcement or the production host lifecycle.

A subsequent product review found a separate capacity gap: enterprise imports bounded their own and return sizes, but did not check the current WeChat sender ceiling before creating an Artifact. The fix rejects oversized current-task exports with HTTP 413 and exposes the terminal tool code `return_too_large`, before downloading or creating an Artifact. The focused suite completed **26 passed**, including enterprise/return/sender ceilings, no download or Artifact on rejection, and no repeated network attempt after that terminal result. Its full official-chain CI must be attributed to the new fix SHA, not the earlier 3322419 run.

The intended deployment retains the sender's **25 MiB** ceiling, sufficient for the complete 18 MB real material package. The 128 MiB parsing/storage ceiling is not a claim of 128 MiB WeChat delivery support. No protocol body limit is silently widened; larger delivery requires its separately verified base64-envelope and upstream limits.

## Fixed capacity candidate and restart verification

Product source `b0517d2222fcf5a08dc6aed079ffbb8baa133203` closes the sender-capacity gap. Its exact-SHA checks completed successfully: [main CI 39129936224](https://github.com/Tangbohu09527/CF_agent-gateway/actions/runs/39129936224), [clean installation 39129936273](https://github.com/Tangbohu09527/CF_agent-gateway/actions/runs/39129936273), [official ZIP ingress chain 39129936265](https://github.com/Tangbohu09527/CF_agent-gateway/actions/runs/39129936265), [official inbound file return 39129936215](https://github.com/Tangbohu09527/CF_agent-gateway/actions/runs/39129936215), and [fixed Hermes compatibility 39129936232](https://github.com/Tangbohu09527/CF_agent-gateway/actions/runs/39129936232). Main CI includes quality/hygiene, the full Python/migration/V2 suite and isolated production-Compose container E2E. The ZIP job still uses the explicitly identified model and WeChat substitutes.

The sole production operator built this fixed source at **2026-10-18 13:15:41 UTC**, using the already verified original `db2b0e…` base. Actual new image identity is `sha256:0bbbb3c29b8117b000027adda522bdecbfb73c1756d44c08431fd1ee43fc1049`. Source archive SHA256 is `8c835370dd17845c1b932195730e9a15dc11babe9d877321fd0ea17b39fe9059`; installed-source proof SHA256 is `6af54d12a023e7c5bc9e503c6631e41ed0ca20ced03346753e7ed9e1927ee1c8`; dependency comparison is `3747cac772f0e3be7d8eec2a536b7aaa213c91131b9cff39a9244c91bcc2b81d`. The build receipt explicitly records no database access, configuration change or production switch. The earlier `663a768` / `533d3f…` image remains a preserved earlier candidate, not this build.

A subsequent **test-only** addition launches a separate Python process against the isolated persisted database after the first controlled task. A second new controlled message retains the same controlled thread and Hermes session, completes a fresh task, and leaves Delivery rows at zero. Its focused controlled-task set completed **11 passed**, with the model substituted. This extends the earlier same-process restart/capture check; neither is a real-model or WeChat receipt. The containing test/documentation commit must receive its own CI and is not a new product-image source.

Private preparation also exercises the fixed deployment transaction before use: the schema/Compose delta contract passes 12 checks; nine maintenance-stage cases cover partial stop, exact original-container recovery, refusal after configuration drift, migration failure and preserved stopped state after an uncertain replacement. Windows configuration checks preserve the model/provider, existing Skill settings, return TLS/work root and PDF/PNG tools while restricting API toolsets. The Windows transaction fixture validates an existing-file replacement, a new plugin file, owned-journal continuation, exact new binary ACL, and unknown-target rejection. Two earlier fixture failures exposed new-journal ACL identity naming and canonical ACE ordering; only new candidate directory/file descriptors were corrected. Existing production owner/DACL were not changed. These helper checks are local preparation, not evidence that installation or a service restart occurred.
