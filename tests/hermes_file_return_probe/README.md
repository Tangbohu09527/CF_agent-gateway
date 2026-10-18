# Isolated inbound read and return probe

The Linux workflow runs the actual Gateway admission, media intake and staging,
Hermes session API, both official-loader plugins, native download worker, PDF/PNG
byte parsers, returned Artifact persistence, response/outbox and delivery worker.
Only the business model and external WeChat endpoints are deterministic loopback
substitutes. The sequence is instruction, PDF, ordinary follow-up text on the same
managed session tip, then PNG. An unrelated attachment and a later replayed return
reference must fail without a new Artifact.

The model first sees the real HTTP tool schema (including the official deferred
tool_describe/tool_call bridge). It requests prepare_return, repeats the read to
check the stable descriptor, and passes only the resulting file_ref/kind/filename
to cf_return_current_chat. PDF text and original PNG image bytes must be present in
the actual model HTTP requests; request bodies are never exported as CI evidence.

`filebridge-commit.txt` fixes the reviewed cross-repository source. The workflow
checks out that commit and official Hermes 427d605d4985c2d49a795493ef6c5d66413057d1,
builds the native worker, and uses separate Gateway Python 3.12 and Hermes Python
3.14.7 environments. Only the PDF/PNG parser dependency subset is installed. A
manual dispatch may supply another exact reviewed commit; source drift is rejected.

Run `probe.py --help` for the explicit source, interpreter, worker and fresh output
paths. There is no default service, HOME or credential discovery. The host I/O guard
allows only test writes, loopback network and the pinned native worker/parser child
commands. Optional source-parent manifests and external .env files remain unreadable.
The `--allow-dirty-source` flag is for explicit local development only and records
the dirty state and file hashes; CI never enables it.

`windows_component.py` is a separate ordinary-token check of official Hermes,
both actual plugins, native downloader and Windows file guards. Its Gateway HTTPS
server is a protocol fixture: its report explicitly says `gateway_is_real=false`
and `artifact_db_and_delivery_tested=false`. It adds identity, session and active
lease-revocation negatives. Do not combine this report with Linux evidence to claim
a single Windows full Gateway chain or a production acceptance result.

Only the allowlisted safe summary, immutable source proof and safe host lifecycle
reports are CI artifacts. Synthetic model requests, isolated databases, capabilities
and event payloads remain local to the test directory. No paid model or production
process is used, and subprocess cleanup is restricted to processes this probe starts.
