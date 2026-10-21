#!/usr/bin/env python3
"""Local fixed-action controller. No TCP listener, shell, arbitrary target or argv.

Root installs this file and its root-owned config; Web receives only the Unix
socket, never Docker's socket or sudo. Existing workers cannot be started here.
"""

import argparse
import json
import os
import socket
import socketserver
import stat
import struct
import subprocess
import threading
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler
from pathlib import Path


def run(argv):
    return subprocess.run(
        argv, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=12, check=True
    ).stdout


class Controller:
    def __init__(self, config):
        self.config = config
        self.lock = threading.Lock()

    def target(self):
        row = json.loads(run(["/usr/bin/docker", "inspect", self.config["wechat_container"]]))[0]
        if row["Image"] != self.config["wechat_image_id"]:
            raise ValueError("wechat_image_identity_changed")
        return row

    def status(self):
        row = self.target()
        workers = {}
        for name in ("poll", "dispatch", "delivery"):
            container = self.config.get("workers", {}).get(name)
            if container:
                result = subprocess.run(
                    ["/usr/bin/docker", "inspect", "--format", "{{.State.Status}}", container],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                workers[name] = result.stdout.strip() if result.returncode == 0 else "not_found"
            else:
                workers[name] = "not_registered"
        return {
            "state": "available",
            "source": "fixed_local_controller",
            "observed_at": datetime.now(UTC).isoformat(),
            "wechat_service": row["State"]["Status"],
            "workers": workers,
            "app_start_supported": bool(self.config.get("app_start")),
        }

    def start(self, app=False):
        with self.lock:
            row = self.target()
            if not app:
                if row["State"]["Running"]:
                    return {"state": "already_running_preserved", "changed": False}
                run(["/usr/bin/docker", "start", row["Id"]])
                return {"state": "started", "changed": True}
            if not row["State"]["Running"]:
                return {"state": "failed", "error": "start_service_first"}
            # Only a reviewed immutable upstream supervisor action is supported.
            app_config = self.config.get("app_start")
            if app_config not in {"supervisor-wechat"}:
                return {"state": "unsupported", "error": "wechat_process_start_not_verified"}
            probe = subprocess.run(
                [
                    "/usr/bin/docker",
                    "exec",
                    row["Id"],
                    "/usr/bin/supervisorctl",
                    "status",
                    "wechat",
                ],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=12,
            )
            if probe.returncode not in {0, 3}:
                return {"state": "failed", "error": "wechat_process_status_failed"}
            active = probe.stdout
            if "RUNNING" in active:
                return {"state": "already_running_preserved", "changed": False}
            run(["/usr/bin/docker", "exec", row["Id"], "/usr/bin/supervisorctl", "start", "wechat"])
            return {"state": "started", "changed": True}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, type=Path)
    args = parser.parse_args()
    meta = args.config.lstat()
    if not stat.S_ISREG(meta.st_mode) or meta.st_uid != 0 or stat.S_IMODE(meta.st_mode) & 0o022:
        raise SystemExit("controller config must be root-owned, regular and not writable by others")
    config = json.loads(args.config.read_text())
    controller = Controller(config)
    address = Path(config["socket"])
    address.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
    os.chown(address.parent, 0, config["allowed_gid"])
    if address.exists():
        # Refuse a live second controller; remove only an abandoned socket.
        probe = socket.socket(socket.AF_UNIX)
        try:
            probe.connect(str(address))
        except ConnectionRefusedError:
            if not stat.S_ISSOCK(address.lstat().st_mode):
                raise SystemExit("controller path is not a socket") from None
            address.unlink()
        else:
            raise SystemExit("controller already running")
        finally:
            probe.close()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def reply(self, code, value):
            body = json.dumps(value).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def allowed(self):
            _, uid, gid = struct.unpack(
                "3i", self.connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
            )
            return uid in {0, config["allowed_uid"]} and (uid == 0 or gid == config["allowed_gid"])

        def do_GET(self):
            if not self.allowed():
                return self.reply(403, {"error": "controller_peer_denied"})
            if self.path != "/status":
                return self.reply(404, {"error": "fixed_action_only"})
            try:
                self.reply(200, controller.status())
            except Exception:
                self.reply(200, {"state": "unavailable", "error": "controller_inspection_failed"})

        def do_POST(self):
            if not self.allowed():
                return self.reply(403, {"error": "controller_peer_denied"})
            if self.path not in {"/wechat/start", "/wechat/app/start"}:
                return self.reply(404, {"error": "fixed_action_only"})
            size = self.headers.get("Content-Length", "0")
            if size not in {"0", "2", "4"}:
                return self.reply(400, {"error": "no_arguments_allowed"})
            body = self.rfile.read(int(size))
            if body not in {b"", b"{}", b"null"}:
                return self.reply(400, {"error": "no_arguments_allowed"})
            try:
                self.reply(200, controller.start(self.path == "/wechat/app/start"))
            except Exception:
                self.reply(200, {"state": "failed", "error": "fixed_start_failed"})

    class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
        daemon_threads = True

    with Server(str(address), Handler) as server:
        os.chown(address, 0, config["allowed_gid"])
        os.chmod(address, 0o660)
        server.serve_forever(poll_interval=0.5)


if __name__ == "__main__":
    main()
