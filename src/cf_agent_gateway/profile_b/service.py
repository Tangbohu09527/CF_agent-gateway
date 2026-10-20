from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from cf_agent_gateway.access.policy_models import UserAccessPolicy
from cf_agent_gateway.agent_profile.models import AgentProfile
from cf_agent_gateway.hermes.result_models import HermesDispatchResponse
from cf_agent_gateway.identity.models import (
    EnterpriseIdentity,
    IdentityStatus,
    SourceIdentityMapping,
)
from cf_agent_gateway.message.models import Conversation, Message
from cf_agent_gateway.profile_b.hermes import Connection, HermesNode, NodeError
from cf_agent_gateway.profile_b.models import (
    DerivedProfile,
    EmployeeSettings,
    ManagedSession,
    ManagementAudit,
    ManagementGroup,
    Node,
    RoomSettings,
    Template,
    TemplateVersion,
)
from cf_agent_gateway.task.model.models import HermesDispatchRecord, HermesDispatchStatus
from cf_agent_gateway.workspace.models import (
    AIThread,
    ThreadPolicy,
    ThreadSourceBinding,
    ThreadType,
)
from cf_agent_gateway.workspace.service import WorkspaceService

SOURCE = "wechat-test"


def new_id():
    return str(uuid.uuid4())


class ManagementError(RuntimeError):
    def __init__(self, code: str, status=409):
        self.code, self.status = code, status
        super().__init__(code)


class Service:
    def __init__(
        self,
        session: Session,
        connections: dict[str, Connection],
        actor="test-administrator",
        accounts=frozenset({"isolated-bot"}),
    ):
        self.s, self.connections, self.actor, self.accounts = session, connections, actor, accounts

    def audit(self, action, object_id, result="ok", version=None):
        self.s.add(
            ManagementAudit(
                id=new_id(),
                actor=self.actor,
                action=action,
                object_id=str(object_id),
                request_id=new_id(),
                result=result,
                version=version,
                created_at=datetime.now(UTC),
            )
        )

    def get(self, model, key):
        value = self.s.get(model, key)
        if value is None:
            raise ManagementError("object_not_found", 404)
        return value

    def commit(self):
        try:
            self.s.commit()
        except IntegrityError as error:
            self.s.rollback()
            raise ManagementError("duplicate_or_conflicting_object") from error

    def flush(self):
        try:
            self.s.flush()
        except IntegrityError as error:
            self.s.rollback()
            raise ManagementError("duplicate_or_conflicting_object") from error

    def group(self, group_id, kind):
        if not group_id:
            return None
        group = self.get(ManagementGroup, group_id)
        if group.kind != kind:
            raise ManagementError("group_kind_mismatch", 422)
        return group

    def account(self, account):
        if account not in self.accounts:
            raise ManagementError("source_account_not_registered", 403)

    def save_employee(self, body, identity_id=None):
        if identity_id:
            identity = self.get(EnterpriseIdentity, identity_id)
            settings = self.get(EmployeeSettings, identity.id)
        else:
            self.account(body.account_id)
            identity = self.s.scalar(
                select(EnterpriseIdentity).where(EnterpriseIdentity.employee_id == body.employee_id)
            )
            if identity is None:
                identity = EnterpriseIdentity(id=new_id(), employee_id=body.employee_id)
                self.s.add(identity)
                self.flush()
            if self.s.get(EmployeeSettings, identity.id):
                raise ManagementError("employee_already_enrolled")
            mapping = self.s.scalar(
                select(SourceIdentityMapping).where(
                    SourceIdentityMapping.platform == SOURCE,
                    SourceIdentityMapping.account_id == body.account_id,
                    SourceIdentityMapping.sender_id == body.sender_id,
                )
            )
            if mapping and mapping.enterprise_identity_id != identity.id:
                raise ManagementError("source_identity_already_bound")
            if not mapping:
                self.s.add(
                    SourceIdentityMapping(
                        id=new_id(),
                        platform=SOURCE,
                        account_id=body.account_id,
                        sender_id=body.sender_id,
                        enterprise_identity_id=identity.id,
                        enabled=True,
                    )
                )
            settings = EmployeeSettings(identity_id=identity.id)
            self.s.add(settings)
        self.group(body.group_id, "employee")
        identity.display_name = body.name
        identity.status = IdentityStatus.ACTIVE if body.enabled else IdentityStatus.DISABLED
        policy = self.s.scalar(
            select(UserAccessPolicy).where(UserAccessPolicy.enterprise_identity_id == identity.id)
        )
        if policy is None:
            policy = UserAccessPolicy(
                id=new_id(),
                enterprise_identity_id=identity.id,
                permission_scope=frozenset(),
                allowed_skills=frozenset(),
            )
            self.s.add(policy)
        # File scopes and skills are deliberately never changed by group/template/permission UI.
        policy.enabled = body.bot_allowed
        settings.group_allowed, settings.group_id = body.group_allowed, body.group_id
        self.audit("employee.update" if identity_id else "employee.enroll", identity.id)
        self.commit()
        self.mark_subject_change("employee", identity.id)
        return identity.id

    def save_template(self, body, template_id=None):
        if template_id:
            item = self.get(Template, template_id)
            if item.kind != body.kind:
                raise ManagementError("template_kind_immutable", 422)
            if body.expected_version != item.version:
                raise ManagementError("template_version_conflict")
            changed = self.s.execute(
                update(Template)
                .where(Template.id == item.id, Template.version == body.expected_version)
                .values(
                    name=body.name,
                    soul=body.soul,
                    language=body.language,
                    version=body.expected_version + 1,
                )
            )
            if changed.rowcount != 1:
                raise ManagementError("template_version_conflict")
            self.s.refresh(item)
        else:
            item = Template(
                id=new_id(),
                name=body.name,
                kind=body.kind,
                soul=body.soul,
                language=body.language,
                version=1,
            )
            self.s.add(item)
            self.flush()
        self.s.add(
            TemplateVersion(
                id=new_id(),
                template_id=item.id,
                version=item.version,
                soul=item.soul,
                language=item.language,
            )
        )
        self.audit(
            "template.update" if template_id else "template.create", item.id, version=item.version
        )
        self.commit()
        for binding in self.s.scalars(
            select(DerivedProfile).where(DerivedProfile.template_id == item.id)
        ):
            binding.desired_version = item.version
            if binding.applied_version != item.version and not binding.operation_token:
                binding.status = "sync_pending"
        self.commit()
        return item.id

    def save_node(self, body):
        if body.connection_ref not in self.connections:
            raise ManagementError("connection_reference_not_allowed", 422)
        item = Node(
            id=new_id(),
            name=body.name,
            connection_ref=body.connection_ref,
            status="unchecked",
            capabilities={},
        )
        self.s.add(item)
        self.audit("node.register", item.id)
        self.commit()
        return item.id

    def node_client(self, node_id):
        item = self.get(Node, node_id)
        connection = self.connections.get(item.connection_ref)
        if connection is None:
            raise ManagementError("connection_reference_unavailable", 503)
        return item, HermesNode(connection)

    def check_node(self, node_id):
        node, client = self.node_client(node_id)
        node.checked_at = datetime.now(UTC)
        try:
            node.capabilities = client.check()
            node.status, node.last_error = "ready", None
        except NodeError as error:
            node.status, node.last_error, node.capabilities = "unavailable", error.code, {}
        self.audit("node.check", node_id, node.last_error or "ok")
        self.commit()
        return {"status": node.status, "error": node.last_error}

    def save_group(self, body, group_id=None):
        template, node = self.get(Template, body.template_id), self.get(Node, body.node_id)
        if template.kind != body.kind:
            raise ManagementError("template_kind_mismatch", 422)
        if group_id:
            item = self.get(ManagementGroup, group_id)
            if item.kind != body.kind:
                raise ManagementError("group_kind_immutable", 422)
        else:
            item = ManagementGroup(id=new_id(), kind=body.kind)
            self.s.add(item)
        item.name, item.template_id, item.node_id = body.name, template.id, node.id
        self.audit("group.update" if group_id else "group.create", item.id)
        self.commit()
        for employee in self.s.scalars(
            select(EmployeeSettings).where(EmployeeSettings.group_id == item.id)
        ):
            self.mark_subject_change("employee", employee.identity_id)
        for room in self.s.scalars(select(RoomSettings).where(RoomSettings.group_id == item.id)):
            self.mark_subject_change("group", room.conversation_id)
        return item.id

    def save_room(self, body, room_id=None):
        self.group(body.group_id, "group")
        if room_id:
            room = self.get(RoomSettings, int(room_id))
            conv = self.get(Conversation, room.conversation_id)
        else:
            self.account(body.account_id)
            conv = self.s.scalar(
                select(Conversation).where(
                    Conversation.source == SOURCE,
                    Conversation.source_account_id == body.account_id,
                    Conversation.conversation_id == body.conversation_id,
                )
            )
            if conv is None:
                conv = Conversation(
                    source=SOURCE,
                    source_account_id=body.account_id,
                    conversation_id=body.conversation_id,
                    conversation_type="group",
                )
                self.s.add(conv)
                self.flush()
            if conv.conversation_type != "group" or self.s.get(RoomSettings, conv.id):
                raise ManagementError("conversation_already_bound_or_wrong_kind")
            room = RoomSettings(conversation_id=conv.id)
            self.s.add(room)
        conv.conversation_name, room.enabled, room.group_id = body.name, body.enabled, body.group_id
        self.audit("room.update" if room_id else "room.register", conv.id)
        self.commit()
        self.mark_subject_change("group", conv.id)
        return str(conv.id)

    def subject_group(self, kind, subject):
        if kind == "employee":
            settings = self.get(EmployeeSettings, subject)
        else:
            settings = self.get(RoomSettings, int(subject))
        if not settings.group_id:
            raise ManagementError("authorized_but_ungrouped", 409)
        return self.group(settings.group_id, kind)

    def binding(self, kind, subject):
        column = (
            DerivedProfile.identity_id if kind == "employee" else DerivedProfile.conversation_id
        )
        return self.s.scalar(
            select(DerivedProfile).where(
                column == (subject if kind == "employee" else int(subject))
            )
        )

    def mark_subject_change(self, kind, subject):
        binding = self.binding(kind, subject)
        if binding is None:
            return
        if binding.operation_token:
            return  # In-flight work is retained; a later ensure reconciles the new target.
        try:
            group = self.subject_group(kind, subject)
        except ManagementError:
            binding.status, binding.last_error = "blocked", "subject_ungrouped_data_retained"
            self.commit()
            return
        if group.node_id != binding.node_id:
            binding.status, binding.last_error = (
                "blocked_move",
                "cross_node_migration_unverified_data_retained",
            )
        else:
            template = self.get(Template, group.template_id)
            if (
                binding.status in {"create_failed", "sync_failed"}
                and binding.template_id == template.id
                and binding.desired_version == template.version
            ):
                return
            binding.template_id, binding.desired_version = template.id, template.version
            binding.status = (
                "ready"
                if (binding.applied_template_id, binding.applied_version)
                == (template.id, template.version)
                else "sync_pending"
            )
            binding.last_error = None
        self.commit()

    def acquire(self, binding_id, status):
        token, now = new_id(), datetime.now(UTC)
        # Expiry is diagnostic, never permission to replay an uncertain external operation.
        result = self.s.execute(
            update(DerivedProfile)
            .where(DerivedProfile.id == binding_id, DerivedProfile.operation_token.is_(None))
            .values(
                operation_token=token, operation_until=now + timedelta(minutes=10), status=status
            )
        )
        self.commit()
        if result.rowcount != 1:
            raise ManagementError("operation_in_progress")
        return token

    def derive(self, kind, subject):
        group = self.subject_group(kind, subject)
        if kind == "employee":
            self.authorize_identity(subject, False)
        elif not self.get(RoomSettings, int(subject)).enabled:
            raise ManagementError("room_disabled", 403)
        template, node = self.get(Template, group.template_id), self.get(Node, group.node_id)
        if node.status != "ready":
            raise ManagementError("node_capability_not_verified", 409)
        binding = self.binding(kind, subject)
        if binding is None:
            key = f"{kind}:{subject}"
            actual = (
                f"cf-{'e' if kind == 'employee' else 'g'}-"
                + hashlib.sha256(key.encode()).hexdigest()[:24]
            )
            agent = AgentProfile(
                id=new_id(),
                profile_key=actual,
                revision=1,
                provider="hermes",
                model="node-managed",
                external_profile_ref=actual,
            )
            self.s.add(agent)
            binding = DerivedProfile(
                id=new_id(),
                kind=kind,
                identity_id=subject if kind == "employee" else None,
                conversation_id=int(subject) if kind == "group" else None,
                node_id=node.id,
                template_id=template.id,
                actual_profile=actual,
                owner_marker="cf-owner:" + new_id(),
                agent_profile_id=agent.id,
                desired_version=template.version,
                status="pending",
            )
            self.s.add(binding)
            try:
                self.s.commit()
            except IntegrityError:
                self.s.rollback()
                binding = self.binding(kind, subject)
                if binding is None:
                    raise ManagementError("concurrent_binding_conflict") from None
        self.mark_subject_change(kind, subject)
        self.s.refresh(binding)
        if binding.operation_token:
            raise ManagementError("operation_in_progress")
        if binding.status == "blocked_move":
            raise ManagementError(binding.last_error)
        if binding.status == "ready":
            return binding.id
        token = self.acquire(
            binding.id, "creating" if binding.applied_version is None else "syncing"
        )
        self.s.refresh(binding)
        _, client = self.node_client(binding.node_id)
        try:
            client.ensure_created(
                binding.actual_profile,
                binding.owner_marker,
                initialize=binding.applied_version is None,
            )
            client.apply(binding.actual_profile, template.soul, template.language)
            binding.applied_template_id, binding.applied_version = template.id, template.version
            binding.status, binding.last_error = "ready", None
            # Live agents cache prompts. Saving is proven; old-session effectiveness is separate.
            for session in self.s.scalars(
                select(ManagedSession).where(ManagedSession.derived_id == binding.id)
            ):
                if (session.first_template_id, session.first_version) != (
                    template.id,
                    template.version,
                ):
                    session.effective_status = "existing_session_pending"
            self.audit("profile.ensure_sync", binding.id, version=template.version)
        except NodeError as error:
            binding.status = "create_failed" if binding.applied_version is None else "sync_failed"
            binding.last_error = error.code
            self.audit("profile.ensure_sync", binding.id, error.code, template.version)
        finally:
            if binding.operation_token == token:
                binding.operation_token, binding.operation_until = None, None
            self.commit()
        self.s.expire_all()
        self.mark_subject_change(kind, subject)
        return binding.id

    def authorize_identity(self, identity_id, group):
        identity = self.get(EnterpriseIdentity, identity_id)
        settings = self.get(EmployeeSettings, identity_id)
        policy = self.s.scalar(
            select(UserAccessPolicy).where(UserAccessPolicy.enterprise_identity_id == identity_id)
        )
        now = datetime.now(UTC)

        def aware(value):
            return (
                value.replace(tzinfo=UTC) if value is not None and value.tzinfo is None else value
            )

        if (
            identity.status != IdentityStatus.ACTIVE
            or policy is None
            or not policy.enabled
            or (policy.valid_from and aware(policy.valid_from) > now)
            or (policy.valid_until and aware(policy.valid_until) <= now)
        ):
            raise ManagementError("employee_bot_permission_denied", 403)
        if group and not settings.group_allowed:
            raise ManagementError("employee_group_permission_denied", 403)
        return identity

    def send(self, body):
        try:
            return self._send(body)
        except ManagementError as error:
            self.audit("message.rejected", body.event_id, error.code)
            self.commit()
            raise

    def _send(self, body):
        self.account(body.account_id)
        mapping = self.s.scalar(
            select(SourceIdentityMapping).where(
                SourceIdentityMapping.platform == SOURCE,
                SourceIdentityMapping.account_id == body.account_id,
                SourceIdentityMapping.sender_id == body.sender_id,
                SourceIdentityMapping.enabled.is_(True),
            )
        )
        if mapping is None:
            raise ManagementError("source_identity_unresolved", 403)
        if body.kind == "private" and body.conversation_id != mapping.sender_id:
            raise ManagementError("private_source_binding_mismatch", 403)
        if body.kind == "group" and not body.mentioned:
            raise ManagementError("bot_not_mentioned", 403)
        conversation = self.s.scalar(
            select(Conversation).where(
                Conversation.source == SOURCE,
                Conversation.source_account_id == body.account_id,
                Conversation.conversation_id == body.conversation_id,
            )
        )
        if conversation is None and body.kind == "private":
            conversation = Conversation(
                source=SOURCE,
                source_account_id=body.account_id,
                conversation_id=body.conversation_id,
                conversation_type="private",
            )
            self.s.add(conversation)
            self.commit()
        if conversation is None or conversation.conversation_type != body.kind:
            raise ManagementError("conversation_source_binding_mismatch", 403)
        event_id = "profile-b:" + body.event_id
        existing = self.s.scalar(select(Message).where(Message.event_id == event_id))
        if existing and (
            existing.source_account_id,
            existing.sender_id,
            existing.conversation_id,
            existing.content,
            existing.conversation_type,
        ) != (body.account_id, body.sender_id, body.conversation_id, body.text, body.kind):
            raise ManagementError("event_id_scope_conflict")
        message = existing
        if message is None:
            message = Message(
                event_id=event_id,
                source=SOURCE,
                source_account_id=body.account_id,
                source_message_id=body.event_id,
                conversation_id=body.conversation_id,
                conversation_type=body.kind,
                is_mentioned=body.mentioned if body.kind == "group" else None,
                is_self=False,
                sender_type="human",
                sender_id=body.sender_id,
                message_type="text",
                content=body.text,
                timestamp=datetime.now(UTC),
            )
            self.s.add(message)
            self.commit()
        identity = self.authorize_identity(mapping.enterprise_identity_id, body.kind == "group")
        if body.kind == "group":
            room = self.get(RoomSettings, conversation.id)
            if not room.enabled:
                raise ManagementError("room_disabled", 403)
        kind, subject = (
            ("employee", identity.id) if body.kind == "private" else ("group", str(conversation.id))
        )
        self.subject_group(kind, subject)
        self.mark_subject_change(kind, subject)
        binding = self.binding(kind, subject)
        if binding is None or binding.status != "ready":
            raise ManagementError("profile_not_ready_create_or_sync_required", 409)
        if existing:
            if (
                existing.source_account_id,
                existing.sender_id,
                existing.conversation_id,
                existing.content,
            ) != (body.account_id, body.sender_id, body.conversation_id, body.text):
                raise ManagementError("event_id_scope_conflict")
            task = self.s.scalar(
                select(HermesDispatchRecord).where(HermesDispatchRecord.message_id == existing.id)
            )
            receipt = (
                self.s.scalar(
                    select(HermesDispatchResponse).where(
                        HermesDispatchResponse.dispatch_record_id == task.id
                    )
                )
                if task
                else None
            )
            if receipt:
                return {**receipt.response_payload, "task_id": task.id, "replayed": True}
            raise ManagementError("request_already_recorded_no_automatic_replay")
        token = self.acquire(binding.id, "ready")
        task = None
        try:
            managed = self.s.get(ManagedSession, conversation.id)
            if managed is None:
                workspace = WorkspaceService(self.s).ensure_workspace_for_authorized_identity(
                    identity.id
                )
                thread = AIThread(
                    id=new_id(),
                    workspace_id=workspace.id,
                    agent_profile_id=binding.agent_profile_id,
                    thread_type=ThreadType(body.kind),
                    thread_policy=ThreadPolicy.PRIVATE_SENDER
                    if body.kind == "private"
                    else ThreadPolicy.GROUP_SHARED,
                    thread_key="profile-b:" + new_id(),
                )
                self.s.add(thread)
                self.flush()
                self.s.add(
                    ThreadSourceBinding(
                        id=new_id(),
                        ai_thread_id=thread.id,
                        platform=SOURCE,
                        account_id=body.account_id,
                        physical_conversation_id=body.conversation_id,
                        sender_id=body.sender_id if body.kind == "private" else None,
                    )
                )
                managed = ManagedSession(
                    conversation_id=conversation.id,
                    derived_id=binding.id,
                    thread_id=thread.id,
                    first_template_id=binding.applied_template_id,
                    first_version=binding.applied_version,
                    effective_status="not_started",
                )
                self.s.add(managed)
                self.commit()
            if managed.derived_id != binding.id:
                raise ManagementError("session_profile_mismatch_data_retained")
            thread = self.get(AIThread, managed.thread_id)
            now = datetime.now(UTC)
            task = HermesDispatchRecord(
                idempotency_key=event_id,
                message_id=message.id,
                enterprise_identity_id=identity.id,
                workspace_id=thread.workspace_id,
                ai_thread_id=thread.id,
                status=HermesDispatchStatus.RUNNING,
                attempt_count=1,
                claim_token=token,
                claimed_at=now,
                lease_expires_at=now + timedelta(minutes=3),
            )
            self.s.add(task)
            self.commit()

            def persist(stored, runtime):
                if thread.hermes_thread_id and thread.hermes_thread_id != stored:
                    raise ManagementError("hermes_stored_session_changed")
                thread.hermes_thread_id, managed.runtime_id = stored, runtime
                self.commit()

            _, client = self.node_client(binding.node_id)
            result = client.turn(
                binding.actual_profile,
                thread.hermes_thread_id,
                "cf:" + thread.id,
                body.text,
                persist,
            )
            if managed.effective_status == "not_started":
                managed.effective_status = "initial_version_applied"
            result.update(
                {
                    "derived_id": binding.id,
                    "thread_id": thread.id,
                    "task_id": task.id,
                    "template_saved_version": binding.applied_version,
                    "session_effective_status": managed.effective_status,
                }
            )
            self.s.add(
                HermesDispatchResponse(
                    dispatch_record_id=task.id,
                    hermes_response_id="cf-b:" + str(task.id),
                    assistant_content=str(result["text"]),
                    response_payload=result,
                )
            )
            task.status, task.completed_at = HermesDispatchStatus.SUCCESS, datetime.now(UTC)
            task.claim_token, task.lease_expires_at = None, None
            self.audit("message.dispatch", task.id, "ok", binding.applied_version)
            self.commit()
            return result
        except (NodeError, ManagementError) as error:
            if task:
                task.status, task.completed_at = HermesDispatchStatus.UNCERTAIN, datetime.now(UTC)
                task.claim_token, task.lease_expires_at, task.last_error_code = (
                    None,
                    None,
                    error.code,
                )
            self.audit("message.dispatch", task.id if task else binding.id, error.code)
            self.commit()
            raise ManagementError(error.code, 502) from error
        finally:
            self.s.execute(
                update(DerivedProfile)
                .where(DerivedProfile.id == binding.id, DerivedProfile.operation_token == token)
                .values(operation_token=None, operation_until=None)
            )
            self.commit()
            self.s.expire_all()
            self.mark_subject_change(kind, subject)
