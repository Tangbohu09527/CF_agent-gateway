"""Bounded synthetic Hermes peer, NOT an installed Hermes download tool.

Only obtains/validates bytes in memory. Real task-directory publication belongs
to the independent FileBrowser project's adapter and remains an enablement gate.
"""

import asyncio
import hashlib
from datetime import UTC, datetime

import httpx


class PeerDownloadError(Exception):
    pass


async def fetch_verified(descriptor, *, on_response=lambda _status: None):
    policy = descriptor["download_policy"]
    attempts = min(4, policy["max_attempts"])
    budget = min(
        30,
        policy["total_timeout_seconds"],
        (datetime.fromisoformat(descriptor["expires_at"]) - datetime.now(UTC)).total_seconds(),
    )
    if attempts < 1 or budget <= 0:
        raise PeerDownloadError("expired_download_budget")
    deadline = asyncio.get_running_loop().time() + budget
    # One monotonic wall-clock budget includes connection, body and retry sleeps.
    async with asyncio.timeout(budget):
        async with httpx.AsyncClient(
            verify=True, trust_env=False, follow_redirects=False
        ) as client:
            for attempt in range(attempts):
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise TimeoutError("download_deadline_exhausted")
                async with client.stream(
                    "GET",
                    descriptor["url"],
                    headers={"Authorization": descriptor["authorization"]},
                    timeout=min(10, remaining),
                ) as response:
                    on_response(response.status_code)
                    if response.status_code == 200:
                        data = bytearray()
                        async for chunk in response.aiter_bytes():
                            data.extend(chunk)
                            if len(data) > descriptor["size"]:
                                raise PeerDownloadError("invalid_download_size")
                        if (
                            len(data) != descriptor["size"]
                            or hashlib.sha256(data).hexdigest() != descriptor["sha256"]
                        ):
                            raise PeerDownloadError("invalid_download_digest")
                        return bytes(data)
                    if response.status_code != 503:
                        raise PeerDownloadError("download_rejected")
                    if attempt + 1 == attempts:
                        raise PeerDownloadError("download_attempts_exhausted")
                    try:
                        delay = max(1, int(response.headers.get("retry-after", "1")))
                    except ValueError:
                        raise PeerDownloadError("invalid_retry_hint") from None
                # A long hint cannot extend the outer deadline or trigger an early retry.
                await asyncio.sleep(delay)
    raise AssertionError("unreachable")
