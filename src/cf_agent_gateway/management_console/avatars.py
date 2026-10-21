"""Bounded, credential-free avatar fetch with DNS pinning and strict source hosts."""

import hashlib
import http.client
import ipaddress
import socket
import ssl
from pathlib import Path
from urllib.parse import urlsplit


def avatar_host(url):
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.username
        or parsed.password
        or parsed.port not in {None, 80, 443}
        or not any(
            host == suffix or host.endswith("." + suffix) for suffix in ("qlogo.cn", "qpic.cn")
        )
    ):
        raise ValueError("avatar_source_not_allowed")
    return parsed, host


class PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, host, address):
        super().__init__(host, timeout=4, context=ssl.create_default_context())
        self.address = address

    def connect(self):
        raw = socket.create_connection((self.address, 443), self.timeout)
        try:
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
        except BaseException:
            raw.close()
            raise


def fetch_avatar(url: str, cache: Path):
    parsed, host = avatar_host(url)
    cache.mkdir(parents=True, exist_ok=True)
    file = cache / hashlib.sha256(url.encode()).hexdigest()
    if file.exists() and file.stat().st_size <= 262144:
        data = file.read_bytes()
    else:
        addresses = {row[4][0] for row in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)}
        if not addresses or any(not ipaddress.ip_address(ip).is_global for ip in addresses):
            raise ValueError("avatar_address_not_public")
        connection = PinnedHTTPS(host, sorted(addresses)[0])
        try:
            path = parsed.path or "/"
            if parsed.query:
                path += "?" + parsed.query
            connection.request(
                "GET", path, headers={"Accept": "image/png,image/jpeg,image/webp,image/gif"}
            )
            response = connection.getresponse()
            if response.status != 200 or response.getheader("Content-Type", "").split(";")[
                0
            ] not in {"image/png", "image/jpeg", "image/webp", "image/gif"}:
                raise ValueError("avatar_response_rejected")
            data = response.read(262145)
            if len(data) > 262144:
                raise ValueError("avatar_too_large")
        finally:
            connection.close()
    media = (
        "image/png"
        if data.startswith(b"\x89PNG\r\n\x1a\n")
        else "image/jpeg"
        if data.startswith(b"\xff\xd8\xff")
        else "image/gif"
        if data.startswith((b"GIF87a", b"GIF89a"))
        else "image/webp"
        if data.startswith(b"RIFF") and data[8:12] == b"WEBP"
        else None
    )
    if not media:
        raise ValueError("avatar_invalid_image")
    if not file.exists():
        file.write_bytes(data)
    return data, media
