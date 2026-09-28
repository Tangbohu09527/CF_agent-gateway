"""Private handoff and finite host lease, fenced by the real durable claim."""

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
import uuid
from datetime import UTC, datetime, timedelta

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import exists, func, select, update

from cf_agent_gateway.adapters.wechat.inbound_media_http import MediaFetchError
from cf_agent_gateway.agent_profile import AgentProfile
from cf_agent_gateway.inbound.host_binding_models import InboundHostBinding
from cf_agent_gateway.inbound.models import InboundMediaJob
from cf_agent_gateway.inbound.store import authorized_source, aware
from cf_agent_gateway.task.model.models import HermesDispatchRecord, HermesDispatchStatus
from cf_agent_gateway.workspace.models import AIThread

SCHEMA = "cf-inbound-host-binding/v1"


class HostBindingError(RuntimeError):
    def __init__(self):
        super().__init__("host binding unavailable")


def digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def runtime_digest(settings):
    return digest(
        json.dumps(
            {
                "profile": settings.profile_reference,
                "revision": settings.profile_revision,
                "model": settings.runtime_model,
                "provider": settings.runtime_provider,
                "model_options": settings.runtime_model_options,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )


def _cipher(settings):
    try:
        key = base64.b64decode(os.environ[settings.encryption_key_env], validate=True)
        if len(key) != 32:
            raise ValueError()
        return AESGCM(key)
    except (KeyError, ValueError):
        raise HostBindingError() from None


def _encrypt(settings, binding_id, descriptor):
    nonce = secrets.token_bytes(12)
    return nonce + _cipher(settings).encrypt(
        nonce, json.dumps(descriptor, separators=(",", ":")).encode(), binding_id.encode()
    )


def _decrypt(settings, binding):
    data = binding.grant_ciphertext
    if data is None:
        raise HostBindingError()
    try:
        return json.loads(_cipher(settings).decrypt(data[:12], data[12:], binding.id.encode()))
    except (InvalidTag, ValueError, TypeError):
        raise HostBindingError() from None


def _fence(session, binding, *, lock=True):
    now = datetime.now(UTC)
    if lock:
        # Write fence serializes resolve/owner assignment on SQLite and PostgreSQL.
        result = session.execute(
            update(HermesDispatchRecord)
            .where(
                HermesDispatchRecord.id == binding.dispatch_id,
                HermesDispatchRecord.status == HermesDispatchStatus.RUNNING,
                HermesDispatchRecord.lease_expires_at > now,
            )
            .values(status=HermesDispatchStatus.RUNNING)
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            raise HostBindingError()
        now = datetime.now(UTC)  # the write lock may have waited beyond the old lease
    job = session.get(InboundMediaJob, binding.job_id, populate_existing=True)
    try:
        _, record = authorized_source(session, job)
    except (MediaFetchError, AttributeError):
        raise HostBindingError() from None
    if (
        record.status is not HermesDispatchStatus.RUNNING
        or not record.claim_token
        or digest(record.claim_token) != binding.claim_token_hash
        or record.lease_expires_at is None
        or aware(record.lease_expires_at) <= now
        or job.state != "ready"
        or job.dispatch_id != binding.dispatch_id
    ):
        raise HostBindingError()
    if binding.id is not None:
        if record.ai_thread_id != binding.ai_thread_id:
            raise HostBindingError()
        thread = session.get(AIThread, record.ai_thread_id, populate_existing=True)
        if thread.agent_profile_id is not None:
            profile = session.get(AgentProfile, thread.agent_profile_id, populate_existing=True)
            if (
                profile is None
                or profile.external_profile_ref != binding.profile_reference
                or profile.revision != binding.profile_revision
            ):
                raise HostBindingError()
    return job, record


def prepare_binding(
    session,
    job,
    *,
    settings,
    public_base_url,
    parent_session_id,
    profile_reference,
    profile_revision,
    expected_claim_token,
):
    from cf_agent_gateway.inbound.access import _authorized_manifest, grant_read

    if (
        not settings.enabled
        or profile_reference != settings.profile_reference
        or profile_revision != settings.profile_revision
    ):
        raise HostBindingError()
    _, record = authorized_source(session, job)
    if not expected_claim_token or record.claim_token != expected_claim_token:
        raise HostBindingError()
    claim_hash = digest(record.claim_token)
    # Obtain the dispatch write lock before looking up or creating the unique intent.
    probe = InboundHostBinding(dispatch_id=record.id, job_id=job.id, claim_token_hash=claim_hash)
    _fence(session, probe)
    session.refresh(record)
    if record.claim_token != expected_claim_token:
        raise HostBindingError()
    existing = session.scalar(
        select(InboundHostBinding)
        .where(
            InboundHostBinding.dispatch_id == record.id,
            InboundHostBinding.claim_token_hash == claim_hash,
        )
        .execution_options(populate_existing=True)
    )
    if existing is not None:
        _principal(existing, settings)
        if (
            existing.state != "preparing"
            or existing.grant_ciphertext is None
            or aware(existing.grant_expires_at) <= datetime.now(UTC)
        ):
            raise HostBindingError()
        session.commit()
        return existing
    now = datetime.now(UTC)
    binding_id = str(uuid.uuid4())
    descriptor = grant_read(session, job, public_base_url=public_base_url)
    _authorized_manifest(session, job.id, descriptor["authorization"])
    binding = InboundHostBinding(
        id=binding_id,
        dispatch_id=record.id,
        job_id=job.id,
        ai_thread_id=record.ai_thread_id,
        claim_token_hash=claim_hash,
        claim_epoch=str(uuid.uuid4()),
        session_id="cfgw-" + uuid.uuid4().hex,
        parent_session_id=parent_session_id,
        profile_reference=profile_reference,
        profile_revision=profile_revision,
        host_id=settings.host_id,
        state="preparing",
        created_at=now,
        grant_expires_at=job.read_expires_at,
        grant_ciphertext=_encrypt(settings, binding_id, descriptor),
        events_connected=False,
        sequence=1,
    )
    session.add(binding)
    session.commit()  # intent and encrypted grant must precede ANY external create/fork
    return binding


def mark_binding_running(session, binding):
    _fence(session, binding)
    session.refresh(binding)
    if not (
        binding.preparation_started_at and binding.history_digest and binding.runtime_config_digest
    ):
        raise HostBindingError()
    result = session.execute(
        update(InboundHostBinding)
        .where(
            InboundHostBinding.id == binding.id,
            InboundHostBinding.state == "preparing",
            InboundHostBinding.grant_ciphertext.is_not(None),
        )
        .values(state="running")
    )
    if result.rowcount != 1:
        raise HostBindingError()
    session.commit()


def _principal(binding, settings):
    if (
        binding.host_id != settings.host_id
        or binding.profile_reference != settings.profile_reference
        or binding.profile_revision != settings.profile_revision
        or (
            binding.runtime_config_digest is not None
            and binding.runtime_config_digest != runtime_digest(settings)
        )
    ):
        raise HostBindingError()


def _owner(binding, *, task_id, host_instance_id, host_nonce, claim_epoch=None):
    if (
        binding.task_id != task_id
        or binding.host_instance_id != host_instance_id
        or not binding.host_nonce_hash
        or not hmac.compare_digest(binding.host_nonce_hash, digest(host_nonce))
        or (claim_epoch is not None and binding.claim_epoch != claim_epoch)
    ):
        raise HostBindingError()


def resolve_binding(session, settings, *, session_id, task_id, host_instance_id, host_nonce):
    from cf_agent_gateway.inbound.access import _authorized_manifest

    binding = session.scalar(
        select(InboundHostBinding).where(InboundHostBinding.session_id == session_id)
    )
    if binding is None or task_id != session_id:
        raise HostBindingError()
    _principal(binding, settings)
    job, record = _fence(session, binding)
    session.refresh(binding)
    now = datetime.now(UTC)
    if (
        binding.state != "running"
        or aware(binding.grant_expires_at) <= now
        or (binding.host_lease_until is not None and aware(binding.host_lease_until) <= now)
        or aware(record.lease_expires_at) <= now
    ):
        raise HostBindingError()
    descriptor = _decrypt(settings, binding)
    # Validate source AND the actual registered attachment before every handoff.
    _authorized_manifest(session, job.id, descriptor["authorization"], check_host=False)
    if binding.host_instance_id is None:
        binding.host_instance_id = host_instance_id
        binding.host_nonce_hash = digest(host_nonce)
        binding.task_id = task_id
        binding.host_lease_until = min(
            now + timedelta(seconds=settings.lease_seconds),
            aware(record.lease_expires_at),
            aware(binding.grant_expires_at),
        )
    else:
        _owner(binding, task_id=task_id, host_instance_id=host_instance_id, host_nonce=host_nonce)
    result = {
        "schema": SCHEMA,
        "binding_id": binding.id,
        "session_id": binding.session_id,
        "task_id": binding.task_id,
        "host_instance_id": binding.host_instance_id,
        "host_nonce": host_nonce,
        "dispatch_id": binding.dispatch_id,
        "claim_epoch": binding.claim_epoch,
        "message_id": job.message_id,
        "thread_id": binding.ai_thread_id,
        "enterprise_identity_id": record.enterprise_identity_id,
        "profile_reference": binding.profile_reference,
        "profile_revision": binding.profile_revision,
        "lease_valid_until": aware(binding.host_lease_until).isoformat(),
        "state": "running",
        "budget_scope": f"message:{job.message_id}:attachment:{job.attachment_id}",
        "attachments": [descriptor],
        "event_sequence": binding.sequence,
    }
    session.commit()
    return result


def require_read_binding(session, job, record, *, required=False):
    binding = session.scalar(
        select(InboundHostBinding)
        .where(
            InboundHostBinding.dispatch_id == record.id,
            InboundHostBinding.claim_token_hash == digest(record.claim_token),
        )
        .execution_options(populate_existing=True)
    )
    # Legacy capabilities remain compatible while host binding is disabled.
    # Enabling host binding forbids an old capability lacking a prebinding.
    if required and binding is None:
        raise MediaFetchError("media_read_not_authorized")
    if binding is not None and (
        binding.state != "running"
        or not binding.events_connected
        or binding.host_lease_until is None
        or aware(binding.host_lease_until) <= datetime.now(UTC)
    ):
        raise MediaFetchError("media_read_not_authorized")
    if binding is not None:
        try:
            _fence(session, binding, lock=False)
        except HostBindingError:
            raise MediaFetchError("media_read_not_authorized") from None


def host_barrier_clear(*, dispatch_id=None, ai_thread_id=None, claim_token_hash=None):
    predicate = [
        InboundHostBinding.closed_at.is_(None),
        InboundHostBinding.host_lease_until > func.now(),
    ]
    if dispatch_id is not None:
        predicate.append(InboundHostBinding.dispatch_id == dispatch_id)
    if ai_thread_id is not None:
        predicate.append(InboundHostBinding.ai_thread_id == ai_thread_id)
    if claim_token_hash is not None:
        predicate.append(InboundHostBinding.claim_token_hash == claim_token_hash)
    return ~exists(select(InboundHostBinding.id).where(*predicate))


def revoke_dispatch(session, dispatch_id, reason, *, claim_token=None):
    lock_dispatch(session, dispatch_id)
    conditions = [InboundHostBinding.dispatch_id == dispatch_id]
    if claim_token is not None:
        conditions.append(InboundHostBinding.claim_token_hash == digest(claim_token))
    bindings = session.scalars(select(InboundHostBinding).where(*conditions)).all()
    for binding in bindings:
        # Atomic transition: do not increment the sequence on duplicate revocation.
        session.execute(
            update(InboundHostBinding)
            .where(
                InboundHostBinding.id == binding.id,
                InboundHostBinding.state.in_(("preparing", "running")),
            )
            .values(
                state="revoked",
                reason=reason[:64],
                grant_ciphertext=None,
                sequence=InboundHostBinding.sequence + 1,
            )
        )
        job = session.get(InboundMediaJob, binding.job_id, populate_existing=True)
        if (
            job
            and job.read_claim_token
            and digest(job.read_claim_token) == binding.claim_token_hash
        ):
            job.read_token_hash = None
            job.read_claim_token = None
            job.read_expires_at = None
    session.flush()


def revoke_and_wait(session_factory, dispatch_id, reason, *, claim_token=None):
    with session_factory() as session:
        revoke_dispatch(session, dispatch_id, reason, claim_token=claim_token)
        session.commit()
    deadline = time.monotonic() + 31
    while True:
        with session_factory() as session:
            if session.scalar(
                select(
                    host_barrier_clear(
                        dispatch_id=dispatch_id,
                        claim_token_hash=digest(claim_token) if claim_token else None,
                    )
                )
            ):
                return
        if time.monotonic() >= deadline:
            raise HostBindingError()  # completion CAS remains fenced, never bypass it
        time.sleep(0.05)


def expire_bindings(session, *, binding_id=None):
    now = datetime.now(UTC)
    query = (
        select(InboundHostBinding)
        .where(InboundHostBinding.state.in_(("preparing", "running")))
        .order_by(InboundHostBinding.dispatch_id)
    )
    if binding_id is not None:
        query = query.where(InboundHostBinding.id == binding_id)
    for binding in session.scalars(query).all():
        lock_dispatch(session, binding.dispatch_id)
        session.refresh(binding)
        if binding.state not in ("preparing", "running"):
            continue
        try:
            _fence(session, binding, lock=False)
            if aware(binding.grant_expires_at) <= now or (
                binding.host_lease_until and aware(binding.host_lease_until) <= now
            ):
                raise HostBindingError()
        except HostBindingError:
            # Use the stored claim hash here: no stale cleanup may touch a newer claim.
            session.execute(
                update(InboundHostBinding)
                .where(
                    InboundHostBinding.id == binding.id,
                    InboundHostBinding.state.in_(("preparing", "running")),
                )
                .values(
                    state="revoked",
                    reason="lease_or_claim_expired",
                    grant_ciphertext=None,
                    sequence=InboundHostBinding.sequence + 1,
                )
            )
            job = session.get(InboundMediaJob, binding.job_id, populate_existing=True)
            if (
                job
                and job.read_claim_token
                and digest(job.read_claim_token) == binding.claim_token_hash
            ):
                job.read_token_hash = job.read_claim_token = job.read_expires_at = None
    session.commit()


def lock_dispatch(session, dispatch_id):
    # All lifecycle mutations use Dispatch -> binding -> job lock order, including
    # cleanup of already expired claims. This serializes resolve against revocation.
    session.execute(
        update(HermesDispatchRecord)
        .where(HermesDispatchRecord.id == dispatch_id)
        .values(status=HermesDispatchRecord.status)
        .execution_options(synchronize_session=False)
    )
