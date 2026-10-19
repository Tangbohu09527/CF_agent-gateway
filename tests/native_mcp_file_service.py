"""Real FileBrowser fixture for the native MCP experiment; server-side only."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import subprocess
import time
import zipfile
from contextlib import contextmanager
from pathlib import Path

FILEBRIDGE = "3d556786068994826b8105ff4a1d11572b84b1b1"


@contextmanager
def fixture(source, output):
    """Start only the named fresh fixture, never discover an existing server."""
    import httpx

    assert (
        subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
        == FILEBRIDGE
    )
    output.mkdir()
    spec = importlib.util.spec_from_file_location(
        "native_zip_sample", Path(__file__).with_name("run_product_zip_joint.py")
    )
    sample = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sample)
    archive = output / "synthetic-18mb.zip"
    sample.sample(archive)
    with (
        (output / "service.stdout").open("wb") as out,
        (output / "service.stderr").open("wb") as err,
    ):
        child = subprocess.Popen(
            [
                "go",
                "test",
                "./http",
                "-run",
                "^TestProductMaterialsExternalFixture$",
                "-count=1",
                "-timeout=15m",
            ],
            cwd=source / "backend",
            env={**os.environ, "CF_PRODUCT_FIXTURE_DIR": str(output)},
            stdout=out,
            stderr=err,
        )
        client = None
        try:
            deadline = time.monotonic() + 300
            while not (output / "ready.json").is_file():
                assert child.poll() is None, "real file service exited before readiness"
                assert time.monotonic() < deadline, "real file service readiness timeout"
                time.sleep(0.2)
            ready = json.loads((output / "ready.json").read_text())
            cfg = json.loads(Path(ready["config_path"]).read_text())
            client = httpx.Client(
                base_url=ready["url"],
                verify=cfg["ca_file"],
                trust_env=False,
                timeout=120,
                headers={"Authorization": "Bearer " + Path(cfg["token_file"]).read_text().strip()},
            )
            raw = archive.read_bytes()
            uploaded = client.post(
                "/api/resources",
                params={"source": "source1", "path": "/public/native-input.zip"},
                content=raw,
            )
            uploaded.raise_for_status()

            def product(**data):
                response = client.post(
                    "/api/resources/products", json={"source": "source1", **data}
                )
                response.raise_for_status()
                return response.json()

            inspected = product(action="inspect", path="/public/native-input.zip")
            members = [
                {
                    **m,
                    "category": "main" if i == 0 else "detail",
                    "evidence": "Synthetic fixture category, not real model classification",
                }
                for i, m in enumerate(inspected["manifest"])
            ]
            imported = product(
                action="import",
                path="/public/native-input.zip",
                sha256=inspected["sha256"],
                destination="/public/products",
                brand="synthetic",
                product="native",
                specification="fixture",
                version="20261020",
                members=members,
            )
            proof = {
                "filebridge_commit": FILEBRIDGE,
                "input_bytes": len(raw),
                "input_sha256": hashlib.sha256(raw).hexdigest(),
                "fixture_user": "ordinary-core-file-audit-user",
                "query_calls": 0,
                "export_calls": 0,
            }

            def query():
                response = client.get(
                    "/api/resources", params={"source": "source1", "path": imported["path"]}
                )
                response.raise_for_status()
                proof["query_calls"] += 1
                return {
                    "employee_query": "complete",
                    "path": imported["path"],
                    "members": len(members),
                }

            def export():
                result = product(
                    action="export",
                    path=imported["path"],
                    destination="/public/exports",
                    name="native-complete.zip",
                    categories=["main", "detail"],
                )
                response = client.get(
                    "/api/resources/download", params={"source": "source1", "file": result["path"]}
                )
                response.raise_for_status()
                exported = response.content
                assert (
                    len(exported) == result["size_bytes"]
                    and hashlib.sha256(exported).hexdigest() == result["sha256"]
                )
                with zipfile.ZipFile(io.BytesIO(exported)) as z:
                    actual = sorted(
                        (i.file_size, hashlib.sha256(z.read(i)).hexdigest())
                        for i in z.infolist()
                        if not i.is_dir()
                    )
                expected = sorted((m["size_bytes"], m["sha256"]) for m in members)
                assert actual == expected and len(actual) == 12
                proof.update(
                    {
                        "export_calls": proof["export_calls"] + 1,
                        "export_bytes": len(exported),
                        "export_sha256": result["sha256"],
                        "members": len(actual),
                        "members_bytes_verified": True,
                        "transport": "real scoped TLS HTTP",
                        "task_return_authorized": False,
                    }
                )
                return {"employee_export": "complete", **proof}

            yield query, export, proof
        finally:
            if client:
                client.close()
            (output / "stop").write_text("stop\n")
            try:
                child.wait(timeout=20)
            except subprocess.TimeoutExpired:
                child.terminate()
                child.wait(timeout=10)
