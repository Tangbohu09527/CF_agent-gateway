import hashlib
import hmac
import secrets
from datetime import UTC, datetime, timedelta

from cf_agent_gateway.management_console.models import ConsoleSession

COOKIE = "cf_console_session"
ITERATIONS = 600000


def password_record(username, password):
    if not username or len(username) > 128 or len(password) < 12:
        raise ValueError("username required and password must contain at least 12 characters")
    salt = secrets.token_hex(24)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), ITERATIONS).hex()
    return {"username": username, "salt": salt, "hash": digest, "iterations": ITERATIONS}


def verify_password(record, username, password):
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode(), bytes.fromhex(record["salt"]), record["iterations"]
    ).hex()
    return hmac.compare_digest(
        username.encode(), record["username"].encode()
    ) and hmac.compare_digest(digest, record["hash"])


def csrf(cookie):
    return hmac.new(cookie.encode(), b"cf-console-csrf-v1", "sha256").hexdigest()


def session_key(cookie):
    return hashlib.sha256(cookie.encode()).hexdigest()


def login(session, username):
    cookie = secrets.token_urlsafe(48)
    session.add(
        ConsoleSession(
            digest=session_key(cookie),
            username=username,
            expires_at=datetime.now(UTC) + timedelta(hours=8),
        )
    )
    session.commit()
    return cookie


def authenticated(session, cookie):
    if not cookie or len(cookie) > 200:
        return None
    row = session.get(ConsoleSession, session_key(cookie))
    if row and row.expires_at.replace(tzinfo=UTC) > datetime.now(UTC):
        return row.username
    return None
