"""Bounded Linux official product-chain runner; synthetic data, model and WeChat.
The file service, its TLS/auth/audit handlers, native binaries and actual Gateway
staging/claims/plugin middleware/Artifact/Delivery code run without substitutes.
"""

import argparse
import hashlib
import json
import os
import random
import struct
import subprocess
import sys
import time
import zipfile
import zlib
from pathlib import Path


def chunk(kind, raw):
    return struct.pack(">I", len(raw)) + kind + raw + struct.pack(">I", zlib.crc32(kind + raw))


def sample(path):
    rng = random.Random(20261018)
    with zipfile.ZipFile(path, "x", compression=zipfile.ZIP_STORED) as archive:
        for n in range(12):
            rows = b"".join(b"\x00" + rng.randbytes(1400 * 3) for _ in range(360))
            png = (
                b"\x89PNG\r\n\x1a\n"
                + chunk(b"IHDR", struct.pack(">IIBBBBB", 1400, 360, 8, 2, 0, 0, 0))
                + chunk(b"IDAT", zlib.compress(rows))
                + chunk(b"IEND", b"")
            )
            archive.writestr(f"合成 商品 ({n + 1})/图片 ({n + 1}).png", png)
    assert 18_000_000 < path.stat().st_size < 20_000_000


def main():
    p = argparse.ArgumentParser(description=__doc__)
    for name in ("filebridge", "hermes", "hermes-python", "client", "inbound-worker", "output"):
        p.add_argument("--" + name, type=Path, required=True)
    args = p.parse_args()
    assert os.name == "posix"
    for name in ("filebridge", "hermes", "hermes_python", "client", "inbound_worker", "output"):
        setattr(args, name, getattr(args, name).absolute())
    args.output.mkdir(mode=0o700, parents=True, exist_ok=False)
    fixture = args.output / "service"
    fixture.mkdir(mode=0o700)
    synthetic = args.output / "synthetic-complete-18mb.zip"
    sample(synthetic)
    environment = {**os.environ, "CF_PRODUCT_FIXTURE_DIR": str(fixture)}
    child = None
    with (
        (args.output / "service.stdout").open("xb") as out,
        (args.output / "service.stderr").open("xb") as err,
    ):
        child = subprocess.Popen(
            [
                "go",
                "test",
                "./http",
                "-run",
                "^TestProductMaterialsExternalFixture$",
                "-count=1",
                "-timeout=20m",
            ],
            cwd=args.filebridge / "backend",
            env=environment,
            stdout=out,
            stderr=err,
        )
        try:
            deadline = time.monotonic() + 180
            while not (fixture / "ready.json").is_file():
                assert child.poll() is None, "file-service fixture stopped before ready"
                assert time.monotonic() < deadline, "file-service ready timeout"
                time.sleep(0.2)
            ready = json.loads((fixture / "ready.json").read_text())
            command = [
                sys.executable,
                "-I",
                "-B",
                str(Path(__file__).with_name("hermes_product_zip_probe.py")),
                "--hermes-source",
                str(args.hermes),
                "--hermes-python",
                str(args.hermes_python),
                "--filebridge-source",
                str(args.filebridge),
                "--worker",
                str(args.client),
                "--inbound-worker",
                str(args.inbound_worker),
                "--output",
                str(args.output / "evidence"),
                "--file-client-config",
                ready["config_path"],
                "--source",
                ready["source"],
                "--root",
                ready["root"],
                "--archive",
                "/public/unused-direct-upload.zip",
                "--input-zip",
                str(synthetic),
                "--ingest",
            ]
            result = subprocess.run(command, timeout=480, check=False)
            assert result.returncode == 0, (
                "official product ingress chain failed; inspect safe-summary"
            )
            proof = {
                "filebridge_commit": subprocess.check_output(
                    ["git", "rev-parse", "HEAD"], cwd=args.filebridge, text=True
                ).strip(),
                "hermes_commit": subprocess.check_output(
                    ["git", "rev-parse", "HEAD"], cwd=args.hermes, text=True
                ).strip(),
                "client_sha256": hashlib.sha256(args.client.read_bytes()).hexdigest(),
                "inbound_worker_sha256": hashlib.sha256(
                    args.inbound_worker.read_bytes()
                ).hexdigest(),
                "input": "deterministic-synthetic-12-images",
                "input_bytes": synthetic.stat().st_size,
                "model": "substitute",
                "wechat": "substitute",
                "service_handlers": "real",
                "gateway_staging": "real-posix",
            }
            (args.output / "source-proof.json").write_text(
                json.dumps(proof, sort_keys=True, indent=2) + "\n"
            )
        finally:
            (fixture / "stop").write_text("stop\n")
            try:
                child.wait(timeout=15)
            except subprocess.TimeoutExpired:
                child.terminate()
                child.wait(timeout=5)
    assert child.returncode == 0, "file service fixture did not close cleanly"
    assert not (fixture / "fixture-token.txt").exists()


if __name__ == "__main__":
    main()
