# Continuation deployment image selection — 2026-10-16

The production preparation stopped at `compose_old_image_changed` after building
and validating the `baa4aff959e4a8866f4b71f135a40c1f104d35c1` candidate. It did
not produce a completed deployment plan or switch the running services.

The subsequent fixed read-only image audit established the actual cause. The
four application services had explicit references to the old immutable image,
and their inspected container images matched those references. `migration` and
`heartbeat-init` instead inherited a shared tag that resolved to a different
image. `x-runtime` also differed from the four explicit application definitions.
Changing only `CF_GATEWAY_IMAGE` changed the two helpers and `x-runtime` to the
candidate; all four applications remained on their original image. The audit
found no changes outside those image fields.

This is not an image-alias false rejection. Earlier synthetic tag/reference tests
demonstrated a general comparison boundary only. They did not explain the actual
mixed production mapping. The audited formal Compose SHA256 was
`bd355c0dfd4de2fc2b93d64048d085fbfa1a934b69b7b9190a77474f80899c4e`;
the stopped prepare-result SHA256 was
`1bd9c7211fff4069d6d29c847cb678ea8f1b410045a55b5448d04a3e49fabf4c`.
The verified candidate image was
`sha256:5c8115976ddf27c2a56462251800676cb4bf1c87752c77cb08872d501a08b210`.

The existing `site_server.plan_compose` explains this shape: it explicitly pinned
only the four application images and retained the helper definitions. The
deployment entry's `validate_candidate_images` likewise checked only those four.
The new preparation helper incorrectly assumed all six existing references were
already the same old image and that replacing the environment selector would
select the candidate for all six services.

The code correction separates deployment image selection from runtime control.
`plan_compose` now binds the four applications plus `migration` and
`heartbeat-init` to one candidate image, and the candidate validator checks all
six. `APPS`, gated services and their start/stop behavior remain unchanged.
Helper environment, mounts, user, capabilities and command remain exact; the
existing application CA/signing changes retain their original scope. Copies of
the `services` container and individual modified services prevent YAML aliases
from also modifying a top-level extension.

The pure continuation helper separately verifies the approved before-reference
hashes and their current resolved image IDs, then checks the four actual runtime
images. Its image-only candidate explicitly sets all six service images while
preserving `x-runtime` and all other semantic values. It rejects an environment
override that leaves hardcoded application images unchanged, a stale helper
image, resolution/runtime drift, and any additional semantic change.

For this continuation, the next reviewed candidates must therefore be **Compose
plus application configuration**, replacing the earlier environment-plus-config
proposal. The existing `.env` and `x-runtime` can stay unchanged; the environment
selector is no longer evidence of the selected runtime image when explicit
service image fields exist. Formal Compose remains unchanged during candidate
preparation. The existing candidate renderer can inspect a private Compose copy,
but runtime/controller operations use the formal path: an overlay must not be
silently introduced as a second deployment owner.

The completed image can be reused only after its protected image ID, revision,
failed preparation records and current closed/idle baseline are verified. The
frozen r2 package and failed receipt remain evidence; they must not be patched,
rerun or relabeled complete. The already built image does not contain this later
deployment-helper revision, so a preparation continuation must explicitly pin
the reviewed helper bytes it uses. No production Compose replacement, migration,
session change or WeChat acceptance is performed by these code changes.

Validation: the three targeted deployment/configuration suites passed locally
with **173 tests passing**, including the two services-container alias regressions,
using synthetic files, container observations and commands. The tests cover mixed
before images, default/custom config sources, ineffective environment overrides,
all six candidate images, preserved unrelated semantics, missing/stale helpers
and YAML alias boundaries. Ruff checks passed. This is local verification, not
production switching or business acceptance.

The limited production continuation was already authorized. The latest
unattended instruction pauses new switching; execution waits for the user's
return and confirmation that they can cooperate immediately under that same
scope. No repeated authorization for that scope is required.
