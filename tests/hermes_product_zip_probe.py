"""Explicit isolated official427 + real FileBridge service + Gateway ZIP probe.

Not a pytest test and never discovers a live service. The caller supplies the
isolated FileBrowser fixture and fixed binaries. Model and WeChat are substitutes;
model decisions here prove plumbing, not real product classification accuracy.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import importlib.util
import io
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import traceback
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    value = importlib.util.module_from_spec(spec)
    sys.modules[name] = value
    spec.loader.exec_module(value)
    return value


base = load("product_base_probe", REPO / "tests/hermes_file_return_probe/probe.py")
peer = load("product_model_peer", REPO / "tests/hermes_file_return_probe/model.py")
PRODUCT, RETURN = "filebrowser_product_materials", "cf_return_current_chat"
INGEST = "filebrowser_ingest_product_zip"


def new_grant_key():
    """Host binding stores a standard Base64 AES-256 key, not Base64url."""
    return base64.b64encode(secrets.token_bytes(32)).decode("ascii")


class Planner:
    def __init__(self, archive, case):
        self.archive = archive
        self.case = case
        self.schemas = {}
        self.actions = []
        self.image_seen = False
        self.manifest = None
        self.attachment_id = None

    def decide(self, body):
        try:
            return self._decide(body)
        except Exception as error:
            base.save(
                self.case / "planner-failure.json",
                {
                    "class": type(error).__name__,
                    "frames": [
                        {"file": Path(f.filename).name, "line": f.lineno}
                        for f in traceback.extract_tb(error.__traceback__)
                    ],
                },
            )
            raise

    def _decide(self, body):
        messages = body.get("messages", [])
        # Official Hermes may make an auxiliary title/summary request without
        # tools after the actual agent turn. It is not another business step.
        if not body.get("tools"):
            return {"role": "assistant", "content": "Isolated product ZIP task."}
        visible, schemas = peer.schemas(body, messages)
        assert not {"terminal", "execute_code", "skill_manage", "write_file"} & set(visible)
        required = [PRODUCT, RETURN, INGEST] if self.attachment_id else [PRODUCT, RETURN]
        if not set(required) <= schemas.keys():
            assert "tool_describe" in visible
            return peer.tool_call("tool_describe", {"names": required}, visible)
        for name in required:
            definition = schemas[name]
            assert definition["parameters"]["additionalProperties"] is False
            self.schemas[name] = hashlib.sha256(
                json.dumps(definition, sort_keys=True).encode()
            ).hexdigest()
        results = [
            obj
            for msg in messages
            if msg.get("role") == "tool"
            for obj in peer.objects(msg.get("content"))
        ]
        failures = [obj for obj in results if obj.get("ok") is False or obj.get("success") is False]
        if failures:
            base.save(
                self.case / "tool-failure-codes.json",
                [
                    {
                        key: value
                        for key, value in obj.items()
                        if key in {"ok", "success", "error", "command"}
                    }
                    for obj in failures
                ],
            )
            raise AssertionError("actual tool failed")
        receipts = [
            obj
            for obj in results
            if obj.get("status") == "pending_delivery" and obj.get("success") is True
        ]
        if receipts:
            return {
                "role": "assistant",
                "content": "Isolated product ZIP imported and returned; protocol substitute only.",
            }
        exports = [obj for obj in results if "return_preparation" in obj]
        if exports:
            ref = exports[-1]["return_preparation"]
            assert ref["kind"] == "file" and ref["file_ref"].startswith("enterprise-")
            self.actions.append("return")
            return peer.tool_call(
                RETURN, {k: ref[k] for k in ("file_ref", "kind", "filename")}, visible
            )
        imported = [
            obj
            for obj in results
            if obj.get("state") == "complete" and "original" in obj and "manifest" in obj
        ]
        if imported:
            self.actions.append("export")
            return peer.tool_call(
                PRODUCT,
                {
                    "action": "export",
                    "path": imported[-1]["path"],
                    "categories": ["main", "detail"],
                    "name": "完整商品图 (" + self.case.name + ").zip",
                },
                visible,
            )
        inspections = [
            obj
            for obj in results
            if "manifest" in obj and "sha256" in obj and "untrusted_content" in obj
        ]
        if inspections:
            result = inspections[-1]
            self.manifest = result["manifest"]
            if self.attachment_id:
                self.archive = result["path"]
            previews = [obj for obj in results if obj.get("vision") == "native_image_attached"]
            if not previews:
                self.actions.append("preview")
                return peer.tool_call(
                    PRODUCT,
                    {
                        "action": "preview",
                        "path": self.archive,
                        "sha256": result["sha256"],
                        "member": self.manifest[0]["path"],
                    },
                    visible,
                )
            self.image_seen = any(
                obj.get("type") == "image_url"
                for msg in messages
                for obj in peer.objects(msg.get("content"))
            )
            assert self.image_seen, "actual preview never reached model HTTP"
            self.actions.append("import")
            members = [
                {
                    **item,
                    "category": "main" if index == 0 else "detail",
                    "evidence": "Deterministic fixture classification; not real model judgment.",
                }
                for index, item in enumerate(self.manifest)
            ]
            return peer.tool_call(
                PRODUCT,
                {
                    "action": "import",
                    "path": self.archive,
                    "sha256": result["sha256"],
                    "brand": "隔离样本",
                    "product": "完整商品",
                    "specification": "fixture",
                    "version": self.case.name,
                    "members": members,
                },
                visible,
            )
        if self.attachment_id:
            self.actions.append("ingest")
            return peer.tool_call(INGEST, {"attachment_id": self.attachment_id}, visible)
        self.actions.append("inspect")
        return peer.tool_call(PRODUCT, {"action": "inspect", "path": self.archive}, visible)


async def run(args, case, model, certs, keys, file_config):
    import httpx
    import test_artifact_return_integration as protocol
    import uvicorn
    from sqlalchemy import select

    from cf_agent_gateway.access import AccessPolicyService, RiskLevel
    from cf_agent_gateway.adapters.wechat import NormalizedWechatMessage
    from cf_agent_gateway.adapters.wechat.inbound_media import parse_inbound_media
    from cf_agent_gateway.adapters.wechat.inbound_media_http import BoundMediaResult
    from cf_agent_gateway.adapters.wechat.inbound_media_staging import InboundMediaStaging
    from cf_agent_gateway.artifact import ArtifactRepository
    from cf_agent_gateway.artifact.enterprise_exports import EnterpriseExportSettings
    from cf_agent_gateway.artifact.models import Artifact
    from cf_agent_gateway.artifact.return_config import ArtifactReturnSettings
    from cf_agent_gateway.config import (
        APISettings,
        ArtifactSettings,
        DatabaseSettings,
        InboundMediaSettings,
        LoggingSettings,
        Settings,
        WorkerSettings,
    )
    from cf_agent_gateway.delivery.models import DeliveryOutboxRecord
    from cf_agent_gateway.gateway.app import create_app
    from cf_agent_gateway.hermes import HermesClient
    from cf_agent_gateway.hermes.return_bridge.zip_contract import verify_zip
    from cf_agent_gateway.identity.service import IdentityService
    from cf_agent_gateway.inbound.host_binding_config import HostBindingSettings
    from cf_agent_gateway.inbound.host_binding_models import InboundHostBinding
    from cf_agent_gateway.inbound.models import InboundMediaJob
    from cf_agent_gateway.inbound.worker import InboundMediaWorker
    from cf_agent_gateway.ingestion import MessageAdmissionService
    from cf_agent_gateway.response.models import ResponseRecord
    from cf_agent_gateway.runtime.dispatch_worker import build_dispatch_worker
    from cf_agent_gateway.task.model import HermesDispatchRecord

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    origin = f"https://localhost:{listener.getsockname()[1]}"
    settings = Settings(
        database=DatabaseSettings(f"sqlite:///{(case / 'gateway.db').as_posix()}"),
        logging=LoggingSettings("WARNING"),
        artifact=ArtifactSettings(storage_root=str(case / "artifacts")),
        api=APISettings(max_request_body_bytes=128 * 1024 * 1024),
        artifact_return=ArtifactReturnSettings(
            enabled=True,
            host_contract_confirmed=True,
            public_base_url=origin,
            profile_reference="default",
            signing_key_env="CF_FILE_RETURN_SIGNING",
            max_bytes=128 * 1024 * 1024,
        ),
        worker=WorkerSettings(enabled=True, concurrency=1, retry_limit=0, lease_seconds=600),
    )
    if args.ingest:
        settings = replace(
            settings,
            inbound_media=InboundMediaSettings(
                enabled=True,
                staging_root=str(case / "staging"),
                public_base_url=origin,
                max_bytes=128 * 1024 * 1024,
            ),
            host_binding=HostBindingSettings(
                enabled=True,
                dedicated_endpoint_confirmed=True,
                host_id="isolated-product-host",
                profile_reference="default",
                service_token_env=base.SERVICE_ENV,
                encryption_key_env="CF_FILE_RETURN_GRANT",
                runtime_model=model.config["default"],
                runtime_provider="custom",
                runtime_fallback_disabled_confirmed=True,
            ),
        )
    app = create_app(settings)
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            log_level="error",
            access_log=False,
            loop="asyncio",
            ssl_certfile=str(certs[1]),
            ssl_keyfile=str(certs[2]),
        )
    )
    serving = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        async with asyncio.timeout(15):
            while not server.started:
                assert not serving.done()
                await asyncio.sleep(0.02)
        factory = app.state.database_session_factory
        with factory() as session:
            identities = IdentityService(session)
            identity = identities.create_identity(employee_id="isolated-product-fixture")
            identities.create_mapping(
                platform="wechat",
                account_id=base.ACCOUNT,
                sender_id=base.CHAT,
                enterprise_identity_id=identity.id,
            )
            access = AccessPolicyService(session)
            access.upsert_user_policy(enterprise_identity_id=identity.id, enabled=True)
            access.upsert_gateway_policy(enabled=True, allowed_risk_levels={RiskLevel.NORMAL})
            item = MessageAdmissionService(session, inbound_media=settings.inbound_media).process(
                NormalizedWechatMessage.model_validate(
                    {
                        "source_account_id": base.ACCOUNT,
                        "source_message_id": "1",
                        "source_message_id_is_fallback": False,
                        "source_local_id": "1",
                        "source_server_id": "1",
                        "event_id": "isolated-product:1",
                        "conversation_id": base.CHAT,
                        "conversation_type": "private",
                        "sender_id": base.CHAT,
                        "sender_type": "human",
                        "is_mentioned": None,
                        "message_type": "file" if args.ingest else "text",
                        "raw_type": 49 if args.ingest else 1,
                        "content": (
                            "Isolated protocol substitute: inspect, preview, archive and return "
                            "the provided product ZIP. This is not an employee WeChat input."
                        ),
                        "timestamp": datetime.now(UTC),
                        "is_self": False,
                    }
                )
            )
            assert item.dispatch_record_id
            identity_id = identity.id
        if args.ingest:

            class MediaFetch:
                def fetch(self, source):
                    with args.input_zip.open("rb") as stream:
                        raw = stream.read(128 * 1024 * 1024 + 1)
                    assert len(raw) <= 128 * 1024 * 1024
                    return BoundMediaResult(
                        source.fingerprint,
                        parse_inbound_media(
                            {
                                "type": "file",
                                "filename": "完整商品 ZIP (1).zip",
                                "data": base64.b64encode(raw).decode(),
                            },
                            max_bytes=128 * 1024 * 1024,
                        ),
                    )

            intake = InboundMediaWorker(
                factory, MediaFetch(), InboundMediaStaging(case / "staging")
            )
            assert await asyncio.to_thread(intake.run_once) == "ready"
            with factory() as session:
                job = session.scalar(
                    select(InboundMediaJob).where(
                        InboundMediaJob.dispatch_id == item.dispatch_record_id
                    )
                )
                assert job is not None and job.state == "ready"
                model.planner.attachment_id = job.attachment_id
        settings = replace(
            settings,
            enterprise_exports=EnterpriseExportSettings(
                enabled=True,
                base_url=file_config["base_url"],
                token_env="CF_PRODUCT_FIXTURE_TOKEN",
                ca_file=file_config["ca_file"],
                source=args.source,
                root=args.root,
                allowed_bindings=((base.ACCOUNT, base.CHAT, identity_id),),
            ),
        )
        app.state.settings = settings
        product_settings = {
            "_probe_ingest": args.ingest,
            "product_enabled": True,
            "client_path": str(args.worker),
            "client_sha256": base.sha(args.worker.read_bytes()),
            "product_config_path": str(case / "file-client.json"),
            "product_source": args.source,
            "product_root": args.root,
            "product_allowed_bindings": [
                {
                    "enterprise_identity_id": identity_id,
                    "thread_id": item.ai_thread_id,
                    "account_id": base.ACCOUNT,
                    "conversation_id": base.CHAT,
                    "profile_reference": "default",
                    "profile_revision": 1,
                }
            ],
        }

        class ObservedHermesClient(HermesClient):
            async def _request_async(self, *positional, **kwargs):
                response = await super()._request_async(*positional, **kwargs)
                try:
                    value = response.json()
                except ValueError:
                    value = None
                base.save(
                    case
                    / (
                        "response-shape-"
                        + str(len(list(case.glob("response-shape-*.json"))))
                        + ".json"
                    ),
                    {
                        "status": response.status_code,
                        "endpoint": positional[1] if len(positional) > 1 else None,
                        "keys": sorted(value) if isinstance(value, dict) else [],
                        "error_type": value.get("error", {}).get("type")
                        if isinstance(value, dict) and isinstance(value.get("error"), dict)
                        else None,
                        "choices": [
                            {
                                "finish_reason": item.get("finish_reason"),
                                "content_present": bool(item.get("message", {}).get("content")),
                                "tool_call_count": len(
                                    item.get("message", {}).get("tool_calls") or []
                                ),
                            }
                            for item in value.get("choices", [])
                        ]
                        if isinstance(value, dict)
                        else [],
                        "model_actions": model.planner.actions,
                    },
                )
                return response

            def chat(self, *positional, **kwargs):
                try:
                    return super().chat(*positional, **kwargs)
                except Exception as error:
                    base.save(
                        case / "client-failure.json",
                        {
                            "type": type(error).__name__,
                            "operation": getattr(error, "operation", None),
                        },
                    )
                    raise

        with (
            base.official_host(
                args, case, model, origin, certs, keys, product_settings=product_settings
            ) as host,
            ObservedHermesClient(
                host,
                keys["hermes"],
                model.config["default"],
                ca_file=str(certs[0]),
                timeout=httpx.Timeout(300, connect=5, read=300, write=10, pool=5),
            ) as client,
        ):
            result = await asyncio.to_thread(
                build_dispatch_worker(
                    settings, session_factory=factory, hermes_client=client, sender_factory=None
                ).run_once
            )
            assert result is not None and result.status.value == "success", "normal dispatch failed"
        assert not model.errors
        receiver = protocol.WechatReceiver(base.ACCOUNT)
        with protocol.protocol_peer(receiver) as address:
            result = await asyncio.to_thread(protocol.deliver, settings, factory, address)
            assert result.status.value == "delivered"
        with factory() as session:
            artifacts = list(session.scalars(select(Artifact)))
            assert len(artifacts) == 1
            artifact = artifacts[0]
            raw = ArtifactRepository(
                session, settings.artifact.storage_root, max_artifact_bytes=128 * 1024 * 1024
            ).read(artifact.artifact_id)
            manifest = verify_zip(io.BytesIO(raw))
            assert sorted((r["size_bytes"], r["sha256"]) for r in manifest) == sorted(
                (r["size_bytes"], r["sha256"]) for r in model.planner.manifest
            )
            response = session.scalar(
                select(ResponseRecord).where(ResponseRecord.message_id == item.message_id)
            )
            delivery = session.scalar(
                select(DeliveryOutboxRecord).where(
                    DeliveryOutboxRecord.response_id == response.response_id
                )
            )
            dispatch = session.get(HermesDispatchRecord, item.dispatch_record_id)
            inbound = None
            if args.ingest:
                job = session.scalar(
                    select(InboundMediaJob).where(InboundMediaJob.dispatch_id == dispatch.id)
                )
                binding = session.scalar(
                    select(InboundHostBinding).where(InboundHostBinding.dispatch_id == dispatch.id)
                )
                assert job.state == "ready" and job.attempt_count == 1
                assert (
                    binding is not None
                    and binding.state == "revoked"
                    and binding.reason == "completed"
                    and binding.grant_ciphertext is None
                )
                inbound = {
                    "attachment": job.attachment_id,
                    "job": job.id,
                    "attempts": job.attempt_count,
                    "host_closed": True,
                    "model_invoked_ingest": model.planner.actions[0] == "ingest",
                }
            # The protocol's actual file envelope nests data/name under "file".
            sent = [request["file"] for request in receiver.requests if "file" in request]
            assert len(sent) == 1 and base64.b64decode(sent[0]["data"], validate=True) == raw
            return {
                "ok": True,
                "chain": "official427-real-FileBrowser-Gateway-protocol-substitute",
                "model": "deterministic-http-substitute",
                "wechat": "isolated-protocol-substitute",
                "actual_schema_sha256": model.planner.schemas,
                "actions": model.planner.actions,
                "inbound_zip": inbound,
                "preview_in_model_http": model.planner.image_seen,
                "message": item.message_id,
                "dispatch": dispatch.id,
                "attempts": dispatch.attempt_count,
                "response": response.response_id,
                "delivery": delivery.id,
                "artifact": artifact.artifact_id,
                "zip_size": len(raw),
                "zip_sha256": base.sha(raw),
                "members": len(manifest),
                "members_match_inspected_originals": True,
                "output_filename": artifact.filename,
                "ui_or_user_receipt": False,
            }
    finally:
        server.should_exit = True
        await asyncio.wait_for(serving, 20)
        listener.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for field in (
        "hermes-source",
        "hermes-python",
        "filebridge-source",
        "worker",
        "output",
        "file-client-config",
    ):
        parser.add_argument("--" + field, type=Path, required=True)
    parser.add_argument("--source", required=True)
    parser.add_argument("--root", required=True)
    parser.add_argument("--archive", required=True)
    parser.add_argument("--input-zip", type=Path, required=True)
    parser.add_argument("--reuse-upload-receipt", type=Path)
    parser.add_argument("--ingest", action="store_true")
    parser.add_argument("--inbound-worker", type=Path)
    args = parser.parse_args()
    if args.ingest:
        assert os.name == "posix", (
            "real Gateway inbound staging requires POSIX; never bypass its ACL checks"
        )
        assert (
            args.inbound_worker is not None
            and args.inbound_worker.is_file()
            and args.reuse_upload_receipt is None
        )
        args.inbound_worker = args.inbound_worker.resolve()
    for name in ("hermes_source", "filebridge_source", "worker", "output", "file_client_config"):
        setattr(args, name, getattr(args, name).resolve())
    args.hermes_python = args.hermes_python.absolute()
    original = load("product_original", REPO / "tests/hermes_return_probe/probe.py")
    original.verify_source(args.hermes_source, base.HERMES)
    case = args.output
    if os.name == "posix":
        # Fresh isolated copies must preserve the client credential contract.
        # This does not chmod a source credential or any production path.
        os.umask(0o077)
    case.mkdir(mode=0o700, parents=True)
    for folder in (
        "profile",
        "staging",
        "return-work",
        "inbound-work",
        "empty-bundled",
        "tmp",
        "user",
        "certificates",
    ):
        (case / folder).mkdir(mode=0o700)
    cfg = json.loads(args.file_client_config.read_text(encoding="utf-8"))
    # Copy only this explicitly supplied isolated fixture credential, never discover accounts.
    for field in ("token_file", "ca_file"):
        source = Path(cfg[field])
        if not source.is_absolute():
            source = args.file_client_config.parent / source
        destination = case / ("file-token.txt" if field == "token_file" else "file-ca.pem")
        shutil.copyfile(source, destination)
        cfg[field] = str(destination)
    cfg["audit_log"] = str(case / "file-client-audit.jsonl")
    base.save(case / "file-client.json", cfg)
    with args.input_zip.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    metadata = {
        "source": args.source,
        "path": args.archive,
        "operation_id": "joint-input-1",
        "expected_bytes": args.input_zip.stat().st_size,
        "expected_sha256": digest,
    }
    if args.ingest:
        base.save(
            case / "input-ingest-pending.json",
            {
                "sha256": digest,
                "size_bytes": args.input_zip.stat().st_size,
                "source": "isolated-protocol-substitute",
            },
        )
    elif args.reuse_upload_receipt:
        previous = json.loads(args.reuse_upload_receipt.read_text())
        assert previous["ok"] and previous["result"]["sha256"] == digest
        assert previous["result"]["path"] == args.archive
        assert previous["result"]["size_bytes"] == args.input_zip.stat().st_size
        base.save(
            case / "input-upload-reused.json",
            {"previous_receipt": str(args.reuse_upload_receipt), "sha256": digest},
        )
    else:
        upload_input(args, case, metadata)
    token = Path(cfg["token_file"]).read_text().strip()
    environment = original.isolated_environment(case)
    os.environ.clear()
    os.environ.update(environment)
    keys = {name: secrets.token_hex(32) for name in ("host", "hermes", "signing", "api")}
    os.environ.update(
        {
            base.SERVICE_ENV: keys["host"],
            "CF_FILE_RETURN_SIGNING": keys["signing"],
            "CF_GATEWAY_API_TOKEN": keys["api"],
            "CF_PRODUCT_FIXTURE_TOKEN": token,
            "CF_FILE_RETURN_GRANT": new_grant_key(),
        }
    )
    sys.path[:0] = [str(REPO / "src"), str(REPO / "tests")]
    from test_hermes_tls import _test_certificates

    model = peer.ModelServer()
    model.planner = Planner(args.archive, case)
    thread = threading.Thread(target=model.serve_forever, daemon=True)
    thread.start()
    try:
        report = asyncio.run(
            run(args, case, model, _test_certificates(case / "certificates"), keys, cfg)
        )
    except BaseException as error:
        report = {
            "ok": False,
            "class": type(error).__name__,
            "model_errors": model.errors,
            "model_actions": model.planner.actions,
            "diagnostics": {
                name: json.loads((case / name).read_text())
                for name in (
                    "response-shape.json",
                    "client-failure.json",
                    "planner-failure.json",
                    "tool-failure-codes.json",
                )
                if (case / name).exists()
            },
            "response_shapes": [
                json.loads(p.read_text()) for p in sorted(case.glob("response-shape-*.json"))
            ],
            "host_events": [
                {
                    k: v
                    for k, v in json.loads(line).items()
                    if k in {"kind", "stage", "reason", "status", "tool_name"}
                }
                for line in (case / "events.jsonl").read_text().splitlines()
            ]
            if (case / "events.jsonl").exists()
            else [],
            "frames": [
                {"file": Path(f.filename).name, "line": f.lineno}
                for f in traceback.extract_tb(error.__traceback__)
            ],
        }
    finally:
        model.shutdown()
        model.server_close()
        thread.join(5)
    base.save(case / "safe-summary.json", report)
    print(json.dumps(report, ensure_ascii=True))
    return 0 if report["ok"] else 1


def upload_input(args, case, metadata):
    upload = subprocess.Popen(
        [
            str(args.worker),
            "--config",
            str(case / "file-client.json"),
            "--input",
            "-",
            "--apply",
            "upload-stream",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        upload.stdin.write(json.dumps(metadata).encode() + b"\n")
        with args.input_zip.open("rb") as stream:
            shutil.copyfileobj(stream, upload.stdin, length=65536)
        upload.stdin.close()
        upload.stdin = None
        stdout, _ = upload.communicate(timeout=180)
        receipt = json.loads(stdout)
        base.save(case / "input-upload.json", receipt)
        assert upload.returncode == 0 and receipt["ok"] is True
    except BaseException:
        upload.kill()
        upload.communicate()
        raise


if __name__ == "__main__":
    raise SystemExit(main())
