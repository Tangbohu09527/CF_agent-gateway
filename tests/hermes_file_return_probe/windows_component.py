"""Ordinary Windows official427/two-plugin/native-worker component probe.

The Gateway endpoint below is an explicit HTTPS protocol substitute. This is
NOT the Linux real database/admission/Artifact/Delivery chain. Only synthetic
fixtures are used. No production HOME, service, credentials, ACL or data reads.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import importlib.util
import json
import os
import shutil
import sys
import threading
import time
from pathlib import Path
from urllib.parse import unquote
from uuid import NAMESPACE_URL, uuid5

_spec = importlib.util.spec_from_file_location(
    "cf_windows_joint_probe", Path(__file__).with_name("probe.py")
)
probe = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = probe
_spec.loader.exec_module(probe)


def capability(serial, session, *, mismatch=False):
    identity = f"10000000-0000-4000-8000-{serial:012d}"
    claims = {
        "schema": "cf-artifact-return/v1",
        "dispatch_id": serial,
        "message_id": serial,
        "identity_id": identity if not mismatch else "10000000-0000-4000-8000-999999999999",
        "thread_id": f"00000000-0000-4000-8000-{serial:012d}",
        "profile": "default",
        "revision": 1,
        "session_id": session,
        "claim": f"{serial:064x}",
        "expires": int(time.time()) + 120,
        "max_bytes": 1048576,
        "max_artifacts": 4,
    }
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    return claims, "Bearer " + payload + "." + "a" * 64


async def run(args, evidence):

    import httpx

    tests = args.filebridge_source / "integrations/hermes-filebridge/tests"
    sys.path[:0] = [str(tests), str(probe.REPO / "tests"), str(probe.REPO / "src")]
    from test_hermes_middleware import private_directory
    from test_hermes_tls import _test_certificates
    from test_inbound_host import SERVICE_SECRET, HostControlTests

    from cf_agent_gateway.hermes.tls import verified_ssl_context

    fixture_module = probe.load("cf_windows_return_fixtures", tests / "inbound_content_fixtures.py")
    model_module = probe.load("cf_windows_return_model", probe.HERE / "model.py")
    HostControlTests.worker = args.worker
    HostControlTests.worker_hash = probe.sha(args.worker.read_bytes())
    HostControlTests.worker_launches = []
    fixture = HostControlTests()
    fixture.make_adapter = False
    fixture.setUp()
    fixture.lease_seconds = 30
    case = fixture.root
    for name in (
        "profile",
        "empty-bundled",
        "tmp",
        "user",
        "certificates",
    ):
        (case / name).mkdir(exist_ok=True)
    private_directory(case / "return-work")
    private_directory(case / "inbound-work")
    certificates = _test_certificates(case / "certificates")
    # Both synthetic servers have independently created/fixed local certificates.
    # Trust only their exact test roots, never system or production trust settings.
    certificates[0].write_bytes(
        certificates[0].read_bytes() + (tests / "fixtures/cert.pem").read_bytes()
    )
    model = model_module.ModelServer()
    thread = threading.Thread(target=model.serve_forever, daemon=True)
    thread.start()
    keys = {"hermes": "synthetic-component-hermes-" + os.urandom(16).hex(), "host": SERVICE_SECRET}
    records, uploads, expected = {}, [], {}
    handler = fixture.server.RequestHandlerClass
    original_get = handler.do_GET

    def put(self):
        record = records.get(self.path)
        assert record and self.headers.get("Authorization") == record["authorization"]
        data = self.rfile.read(int(self.headers["Content-Length"]))
        assert self.headers.get("X-CF-Return-Intent") == "current-chat"
        assert hashlib.sha256(data).hexdigest() == self.headers["X-CF-Content-SHA256"]
        assert data == expected[record["case"]]
        claims = record["claims"]
        snapshot = {
            key: value for key, value in claims.items() if key not in {"schema", "session_id"}
        }
        fingerprint = hashlib.sha256(
            json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        response_id = f"return:{claims['dispatch_id']}:{claims['claim']}:{fingerprint}"
        value = {
            "artifact_id": str(uuid5(NAMESPACE_URL, f"cf-artifact-return/v1/{response_id}/0")),
            "response_id": response_id,
            "status": "ready",
            "filename": unquote(self.headers["X-CF-Filename"]),
            "kind": self.headers["X-CF-Artifact-Kind"],
            "mime_type": self.headers["Content-Type"],
            "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        }
        uploads.append({"case": record["case"], "sha256": value["sha256"], "size": len(data)})
        record["receipt"] = value
        self.send_json(value)

    def get(self):
        if self.path in records:
            record = records[self.path]
            assert self.headers.get("Authorization") == record["authorization"]
            self.send_json(
                record.get("receipt", {"detail": "missing"}), 200 if "receipt" in record else 404
            )
            return
        original_get(self)

    handler.do_PUT, handler.do_GET = put, get

    def transform(value):
        value["profile_reference"] = "default"
        value["attachments"][0]["filename"] = (
            "original.png" if fixture.mime == "image/png" else "original.pdf"
        )

    fixture.response_transform = transform
    import struct
    import zlib

    def chunk(name, data):
        return (
            struct.pack(">I", len(data)) + name + data + struct.pack(">I", zlib.crc32(name + data))
        )

    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(b"\0\xff\0\0"))
        + chunk(b"IEND", b"")
    )
    content = {"pdf": fixture_module.pdf_fixture(), "png": png}
    try:
        with probe.official_host(
            args, case, model, fixture.raw["gateway_origin"], certificates, keys
        ) as origin:
            async with httpx.AsyncClient(
                verify=verified_ssl_context(str(certificates[0])),
                trust_env=False,
                follow_redirects=False,
                timeout=75,
            ) as client:
                for serial, label in enumerate(("pdf", "png", "identity", "lease"), 1):
                    kind = "png" if label == "png" else "pdf"
                    fixture.body, fixture.mime = (
                        content[kind],
                        "image/png" if kind == "png" else "application/pdf",
                    )
                    session = "isolated-component-" + label
                    claims, authorization = capability(
                        serial, session, mismatch=label == "identity"
                    )
                    url = fixture.raw["gateway_origin"] + f"/internal/hermes/returns/{serial}"
                    records[f"/internal/hermes/returns/{serial}/artifacts/0"] = {
                        "claims": claims,
                        "authorization": authorization,
                        "case": label,
                    }
                    expected[label] = content[kind]

                    async def request(
                        session=session, url=url, authorization=authorization, label=label
                    ):
                        return await client.post(
                            origin + "/v1/chat/completions",
                            headers={
                                "Authorization": "Bearer " + keys["hermes"],
                                "X-Hermes-Session-Id": session,
                                "X-CF-Artifact-Return-URL": url,
                                "X-CF-Artifact-Return-Authorization": authorization,
                            },
                            json={
                                "model": "hermes-agent",
                                "stream": False,
                                "messages": [
                                    {
                                        "role": "user",
                                        "content": "file-return-case:"
                                        + label
                                        + ' {"attachment_id":5}',
                                    }
                                ],
                            },
                        )

                    pending = asyncio.create_task(request())
                    if label == "lease":
                        async with asyncio.timeout(30):
                            while not model.planner.lease_entered.is_set() and not pending.done():
                                await asyncio.sleep(0.02)
                        assert model.planner.lease_entered.is_set()
                        with fixture.lock:
                            fixture.records[session].update(
                                state="revoked", reason="isolated_test_cancel", sequence=2
                            )
                        async with asyncio.timeout(5):
                            while not fixture.records[session]["closed"]:
                                await asyncio.sleep(0.02)
                        model.planner.lease_release.set()
                    response = await pending
                    assert response.status_code == 200, {
                        "case": label,
                        "status": response.status_code,
                    }
                    assert response.headers["X-CF-Artifact-Return-Accepted"] == probe.sha(
                        authorization.encode()
                    )
                    async with asyncio.timeout(5):
                        while fixture.closed_calls < serial:
                            await asyncio.sleep(0.02)
                    assert len(uploads) == min(serial, 2), (
                        "negative identity/lease case published bytes"
                    )
                    # New requests may reuse an attachment number in a different
                    # task; only actual live descriptors count inside that task.
                    model.planner.prepared.clear()
                # Request/header session mismatch must fail before model/tools.
                before = len(model.planner.calls)
                response = await client.post(
                    origin + "/v1/chat/completions",
                    headers={
                        "Authorization": "Bearer " + keys["hermes"],
                        "X-Hermes-Session-Id": "wrong-session",
                        "X-CF-Artifact-Return-URL": url,
                        "X-CF-Artifact-Return-Authorization": authorization,
                    },
                    json={
                        "model": "hermes-agent",
                        "messages": [{"role": "user", "content": "session-negative"}],
                    },
                )
                assert response.status_code == 403 and len(model.planner.calls) == before
        assert not model.errors
        ended = json.loads((case / "host-ended.json").read_text())
        assert ended["closed"] and ended["ordinary_windows_token"] and not ended["audit_denials"]
        assert model.planner.pdf_text_seen
        assert any(
            base64.b64decode(value.split(",", 1)[1]) == content["png"]
            for value in model.planner.images
        )
        report = {
            "ok": True,
            "chain": "windows-official-hermes-two-plugins-protocol-gateway",
            "gateway_is_real": False,
            "artifact_db_and_delivery_tested": False,
            "ordinary_windows_token": True,
            "hermes_commit": probe.HERMES,
            "uploads": uploads,
            "actual_schema_sha256": model.planner.schema_hashes,
            "pdf_text_in_model_http": True,
            "png_bytes_in_model_http": True,
            "identity_mismatch_rejected": True,
            "session_mismatch_rejected": True,
            "inbound_revocation_prevents_return": True,
            "native_download_requests": fixture.download_calls,
            "owned_host_closed": True,
        }
        probe.save(evidence / "safe-summary.json", report)
        return report
    finally:
        model.planner.lease_release.set()
        model.shutdown()
        model.server_close()
        thread.join(5)
        for name in ("host-failure.json", "host-ended.json", "events.jsonl"):
            if (case / name).exists():
                shutil.copyfile(case / name, evidence / name)
        fixture.doCleanups()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hermes-source", type=Path, required=True)
    parser.add_argument("--hermes-python", type=Path, required=True)
    parser.add_argument("--filebridge-source", type=Path, required=True)
    parser.add_argument("--filebridge-commit", required=True)
    parser.add_argument("--worker", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--allow-dirty-source", action="store_true")
    args = parser.parse_args()
    assert os.name == "nt" and probe.ordinary_windows() is True
    for name in ("hermes_source", "hermes_python", "filebridge_source", "worker", "output"):
        setattr(args, name, getattr(args, name).absolute())
    verifier = probe.load(
        "cf_windows_return_verifier", probe.REPO / "tests/hermes_return_probe/probe.py"
    )
    verifier.verify_source(args.hermes_source, probe.HERMES)
    proof = probe.source_inventory(
        args.filebridge_source,
        args.filebridge_commit,
        ("integrations/hermes-filebridge/plugin",),
        allow_dirty=args.allow_dirty_source,
    )
    args.output.mkdir()
    probe.save(
        args.output / "source-proof.json",
        {"filebridge": proof, "worker_sha256": probe.sha(args.worker.read_bytes())},
    )
    print(json.dumps(asyncio.run(run(args, args.output))))


if __name__ == "__main__":
    main()
