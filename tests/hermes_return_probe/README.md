# Official Hermes artifact-return probe

This is an explicit, opt-in companion to the existing Hermes host probe and
`test_artifact_return_integration.py`. It is not a new production executor,
client, downloader, installer, or service. Pytest never launches it implicitly.

The pinned official source is
`0f4a98f87c17007b81500239d0bd5b9574027b73`. Ten HTTP, plugin, middleware, and
execution-source blobs are checked before launch. Use the already selected
Python **3.14.7** and its existing site-packages directory; no dependency install
is performed. `HERMES_DISABLE_LAZY_INSTALLS=1` is passed only to the disposable
child to disable official automatic source-update completion.

## Executed path

1. Existing Gateway admission creates a new isolated Message/thread/Dispatch.
2. The actual `HermesClient` sends one business request over verified HTTPS.
3. A temporary official `APIServerAdapter` discovers the repository return
   plugin through the official plugin loader. It uses a new private Hermes home,
   empty bundled/project-plugin roots, no installed Skills, and only the
   `cf_artifact_return` toolset.
4. The plugin creates the request scope. A **test-only** `prepare_scope` callback
   copies its fixture into that scope's new work directory; it never uploads.
5. The actual Agent asks its model for a response and executes
   `cf_return_current_chat` through official tool middleware. The tool publishes
   bytes to Gateway over verified HTTPS; completion returns the final ACK.
6. Real Artifact/Response/Delivery repositories and the existing WeChat media
   sender deliver to the existing **loopback WeChat protocol receiver**.
7. The driver reads the original new session, verifies receiver bytes, retains
   the final answer, and scans every private evidence file for the in-memory
   model/service credential and capability values. Only match counts are saved.

The default model is explicitly synthetic. The Agent loop and HTTP service are
official Hermes; the model and WeChat receiver are substitutes. This proves
neither deployment to the production listener nor real WeChat receipt.

## Explicit synthetic invocation

Run with the existing Gateway test interpreter and dependencies:

```text
python tests/hermes_return_probe/probe.py \
  --hermes-source <verified-official-source> \
  --hermes-python <existing-python-3.14.7-executable> \
  --site-packages <selected-generation-site-packages> \
  --output <new-private-directory> \
  --kind file
```

`--kind image` checks group targeting; `--kind text` checks that a real Agent
request with a return context can complete without uploading or adding an
Artifact. Every output directory must be new; old evidence is never overwritten.
All listeners bind dynamic loopback ports. The probe never uses port 8642.
The request and host scope have a finite 600-second limit; the existing Gateway
grant remains limited to 900 seconds. The CLI's synthetic runs use a 3-second
Dispatch lease to exercise the unchanged real renewal thread and record whether
renewal occurred before the upload. Programmatic approved-model calls keep the
normal 60-second lease unless the caller explicitly supplies a test override.

The child audit restricts reads to the verified official source, this Gateway
repository, the existing runtime/dependencies, and its private directory. Writes
are restricted to that directory. Its network allowlist is loopback for the
synthetic model, with only the caller-supplied approved model endpoint added for
an explicitly authorized live-model run. Installed `.env` reads and subprocess
tools are refused. This Python audit is **not** a system sandbox for other
processes running as the same Windows user.

On Linux, exact read-only OS metadata files are also allowed, including
`/etc/os-release` and `/usr/lib/os-release` used by OpenAI 2.24.0's
`platform_headers -> distro.id -> os_release_attr` request header construction.
This does not grant reads of their parent directories or permit writes.
SDK platform detection runs before the HTTP listener becomes ready, and audit
denials retain their path only in the private `events.jsonl`, so a CI environment
mismatch fails early instead of waiting through provider retries.
The real POSIX return reader pins ancestor directories with read-only
`O_DIRECTORY | O_NOFOLLOW` handles. Since Python's `open` audit event omits
`dir_fd`, the probe recognizes that exact loaded reader function, its task root,
and its flags, and resolves its pinned descriptor through `/proc/self/fd`.
Only that task's ancestor handles and its own files qualify; arbitrary callers,
ancestor file reads, sibling tasks, and writes remain refused.

## Separately approved model/sample invocation

The main task may invoke `run_case(...)` with an already-approved `model_config`
dictionary held in memory and `agent_options={"reasoning_effort": ...,
"service_tier": ...}` copied from the approved non-sensitive settings.
This function does not discover or read production credential/configuration
references. The model key crosses an anonymous stdin pipe and is bound through
official `agent.secret_scope`, never a command argument, environment variable,
config file, or model message. Only the two named agent options are accepted.

`input_path` must hash to one of the two fixed approved samples in the module.
It is a **local test-input copy**, not a new FileBrowser download. The prompt
only asks the Agent to return the file/image, with no document or visual reading.
The driver never directly invokes the return tool, pre-uploads, injects an
answer, or retries a business request. Failure or uncertainty leaves the case
and its original request evidence in place.

Callers must add this repository's `src` and `tests` to their import path before
using the programmatic entry, just as the explicit CLI does. They should call
the PDF case once, inspect its complete result, then call PNG once. A synthetic
run is not evidence for either approved-model case.

## Evidence

- `intent.json`: frozen natural-language task, source/bridge hashes and input digest.
- `ready.json`: actual temporary TLS endpoint, interpreter and enabled plugin.
- `events.jsonl`: scope, actual Agent turn/tool calls/results, completion ACK and close.
- `http.json`: actual business request/response and ACK comparison booleans.
- `gateway-http.json`: actual PUT/query method, canonical path and status; no headers.
- `official-session.json`: read-only export of this new official HTTP session.
- `gateway.db`, private Artifact storage and `receiver.json`: durable state and receipts.
- `receiver-<filename>`: actual protocol-peer bytes, independently compared with the input.
- `credential-scan.json`: counts only, covering all files including SQLite/WAL/SHM.
- `report.json`: produced only after all success assertions and the offline scan pass.

The receiver is a substitute. PNG byte equality in this probe does not assert
that real WeChat avoids compression or transcoding. READY means pending delivery,
not “WeChat received.” Existing historical PDF pending and Skill deviations are
not changed by these results.

The existing `hermes-host-compatibility.yml` retains the earlier `4d55ca9`
compatibility matrix and adds a separate Linux job for this `0f4a98f` probe.
CI creates separate Gateway Python 3.12 and Hermes Python 3.14.7 environments,
then runs the file, image, and text cases with a loopback synthetic model. The
requirements file is used only by that CI environment; it does not install or
upgrade the existing Windows runtime or provide production deployment evidence.
