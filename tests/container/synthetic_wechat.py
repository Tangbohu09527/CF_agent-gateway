from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class SyntheticWechatHandler(BaseHTTPRequestHandler):
    server_version = "synthetic-agent-wechat"

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if self.path == "/health":
            self._send_json({"status": "ok"})
            return
        if self.path == "/api/status/auth":
            self._send_json(
                {
                    "status": "logged_in",
                    "loggedInUser": "wxid_container_e2e",
                }
            )
            return
        if self.path == "/api/chats":
            self._send_json({"chats": []})
            return
        self._send_json({"error": "not_found"}, status=404)

    def log_message(self, format: str, *args: object) -> None:
        del format, args

    def _send_json(self, payload: dict[str, object], *, status: int = 200) -> None:
        encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True).encode("ascii")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


def main() -> None:
    server = ThreadingHTTPServer(("0.0.0.0", 6174), SyntheticWechatHandler)
    server.serve_forever()


if __name__ == "__main__":
    main()
