"""Allowlisted native read operations only; never open chats, fetch media or send."""

from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx


class SourceError(RuntimeError):
    pass


@dataclass(frozen=True)
class SourceConnection:
    label: str
    url: str
    token: str
    archive_url: str | None = None
    max_pages: int = 500


class NativeReader:
    def __init__(self, connection: SourceConnection, transport=None):
        parsed = urlsplit(connection.url)
        if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password:
            raise ValueError("invalid operator source URL")
        self.connection = connection
        self.client = httpx.Client(
            base_url=connection.url.rstrip("/"),
            headers={"Authorization": "Bearer " + connection.token, "X-Session-Id": "default"},
            follow_redirects=False,
            trust_env=False,
            timeout=httpx.Timeout(15, connect=5),
            transport=transport,
        )

    def close(self):
        self.client.close()

    def read(self, endpoint, **params):
        if endpoint not in {"api/status/auth", "api/contacts", "api/chats"}:
            raise SourceError("source_operation_not_allowed")
        try:
            response = self.client.get(endpoint, params=params)
            if response.status_code in {401, 403}:
                raise SourceError("source_auth_denied")
            if response.status_code != 200:
                raise SourceError("source_http_error")
            value = response.json()
        except (httpx.HTTPError, ValueError):
            raise SourceError("source_connection_failed") from None
        if isinstance(value, dict) and (value.get("success") is False or value.get("error")):
            raise SourceError("source_read_failed")
        return value

    def account(self, *, allow_stored=False):
        value = self.read("api/status/auth")
        if not isinstance(value, dict):
            raise SourceError("bot_offline")
        self.bot_status = value.get("status")
        if self.bot_status != "logged_in" and not (
            allow_stored and self.bot_status == "app_not_running"
        ):
            raise SourceError("bot_offline")
        account = value.get("loggedInUser")
        if not isinstance(account, str) or not account or len(account) > 255:
            raise SourceError("source_identity_unavailable")
        return account

    def pages(self, endpoint, size):
        items, proof, seen = [], [], set()
        for page in range(self.connection.max_pages):
            offset = page * size
            value = self.read(endpoint, limit=size, offset=offset)
            if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
                raise SourceError("source_contract_invalid")
            proof.append({"offset": offset, "returned": len(value)})
            if not value:
                return items, proof
            identifiers = tuple(str(row.get("username", row.get("id", ""))) for row in value)
            if identifiers in seen:
                raise SourceError("source_pagination_not_advancing")
            seen.add(identifiers)
            items.extend(value)
            # Native contacts filters system names AFTER SQL pagination. A short page
            # is never used as the terminator. Empty responses remain qualified below.
        raise SourceError("source_pagination_limit_partial")

    def snapshot(self):
        account = self.account(allow_stored=True)
        initial_status = self.bot_status
        contacts, contact_pages = self.pages("api/contacts", 200)
        # Native Rust structs serialize using camelCase. Keep the Gateway cache
        # contract stable while also accepting older snake_case read adapters.
        aliases = {
            "nick_name": "nickName",
            "small_head_url": "smallHeadUrl",
            "contact_type": "contactType",
        }
        contacts = [
            {**row, **{key: row.get(key, row.get(native)) for key, native in aliases.items()}}
            for row in contacts
        ]
        chats, chat_pages = self.pages("api/chats", 50)
        if self.account(allow_stored=True) != account:
            raise SourceError("source_account_changed_during_sync")
        return (
            account,
            contacts,
            chats,
            {
                "contacts": contact_pages,
                "chats": chat_pages,
                "bot_status": self.bot_status if initial_status == "logged_in" else initial_status,
            },
        )
