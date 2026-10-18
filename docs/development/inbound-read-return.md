# Protected inbound copy to current-chat return

The deployed FileBridge 0.3 entrypoint did not register the separately present
content reader. Its host adapter also lacked the reader helpers. Dispatch47
therefore downloaded a verified PDF but used its opaque inbound handle with the
enterprise file API; validation rejected that input before an ACL operation.
There was no content-reading or attachment-return success.

The repair keeps the existing downloader, parser, host binding and return
protocol. `filebrowser_read_inbound` accepts `prepare_return: true` only as an
explicit read-and-return request. It downloads internally, reads the protected
stream under the original lease and passes that same verified stream to a
trusted adapter. The adapter matches the current Dispatch, message, employee,
thread, Profile revision, session and official tool turn against the active
return scope. Employee and thread IDs are canonical UUID strings; Dispatch,
message and revision are integers.

The adapter exclusively creates a task-local return copy and produces a relative
`file_ref`. The model then calls the existing `cf_return_current_chat` tool.
Enterprise `source/path`, an attachment ID, an opaque inbound handle and a return
`file_ref` are distinct contracts. A handle is never a path. No protected path,
credential or authorization is added to model-visible output.

The original inbound lease remains attached to preparation and publication,
including repeated calls, upload recovery and cached receipt access. It is not
renewed by reading or by preparing a return. Ordinary generated-file returns
without an inbound scope keep their original return authorization. Failed reads
never prepare a return; a successful read with failed preparation reports both
facts and tells the model to stop guessing paths.

The initial Windows deployment enabled PDF and PNG only and uses an isolated private
parser with the existing pinned anydoc and Pillow versions. That installation changed neither
the Gateway application/image/schema nor the FileBrowser server, model settings
or existing owner/DACL. The previous installation records remain intact.

PNG is returned through the existing image contract. The original Artifact
bytes, protocol delivery bytes, image display and recipient-saved bytes are
separate observations; image display alone does not establish original-file
attachment delivery.

The joint verifier uses the fixed official Hermes427 loader, API handler and
tool middleware. The deterministic model consumes the actual HTTP-visible tool
schemas and tool results. Linux exercises real Gateway admission, staging,
Artifact and Delivery persistence; Windows additionally verifies the ordinary
Admin execution token, native worker and file semantics with an explicitly
labelled Gateway protocol fixture. Neither substitute result is real WeChat
acceptance. New live tests require new normal messages; Dispatch47 and historical
dead records are never replayed.
## Resolve clock contract follow-up

The Dispatch48 live failure exposed an independently reproducible clock-basis
defect: the client subtracted its local wall time from a server wall deadline
and rejected a remaining duration over 30.5 seconds. A small inter-host offset
can reject a valid 30-second server lease. The first historical exception was
not recorded, so this does not establish which check failed first.

Resolve v2 uses `cf-inbound-host-binding/resolve/v2`, adding only `server_now`
to the authenticated private response. Both timestamps come from the same
Gateway time sample; a client requires their duration to be positive and at most
30 seconds. It anchors this duration at local monotonic request start, consuming
all network/processing time. Repeated resolve retains the original server lease.
There is no automatic v1 fallback, retry, reconnect, new owner or renewal.

Gateway still accepts strict v1 resolve without changing its response fields.
Events and closed remain v1. Ownership, Profile, claim, attachment, budget scope,
revocation and authentication are unchanged. The native worker's absolute wall
expiry is conservatively shortened to the earlier of the original server lease
and local wall time plus remaining monotonic budget, rounded down. Its original
descriptor is unchanged. Host timer and source guards retain that same deadline
through read, preparation, upload and cached return receipts. This is not a claim
that an independent native worker is immune to arbitrary clock jumps.

This follow-up requires a new Gateway candidate plus the FileBridge control
client; it is outside the initial Windows-only production change. No database
migration or model/configuration change is needed. First deploy the compatible
Gateway while gates are closed and the queue is idle; then load the new client
and fixed-enum host diagnostics using the official same-version Hermes restart.
A new Gateway production approval is required after candidate verification.
Keep the old pair and exact backup manifests. Before any live task, a rollback
may restore the old client first, then the compatible Gateway image; after a
new task, inspect its state and preserve history instead of blind rollback.
