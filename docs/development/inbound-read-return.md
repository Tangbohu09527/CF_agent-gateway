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

Deployment for this task enables PDF and PNG only and uses an isolated private
parser with the existing pinned anydoc and Pillow versions. It changes neither
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