from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from os import name as os_name
from pathlib import Path
from threading import Lock
from typing import BinaryIO

from sqlalchemy import Engine, text

from cf_agent_gateway.runtime.errors import WechatPollGateUnavailableError

_POSTGRES_POLL_LOCK_ID = int.from_bytes(b"CFAGPOLL", byteorder="big", signed=True)
_MEMORY_SQLITE_GATE = Lock()


@contextmanager
def acquire_wechat_poll_gate(engine: Engine) -> Iterator[None]:
    """Prevent resident and one-cycle pollers from overlapping.

    PostgreSQL advisory locks and OS file locks are released automatically when
    their owning connection or process exits.
    """

    if not isinstance(engine, Engine):
        yield
        return

    backend = engine.url.get_backend_name()
    if backend == "postgresql":
        with engine.connect() as connection:
            acquired = connection.scalar(
                text("SELECT pg_try_advisory_lock(:lock_id)"),
                {"lock_id": _POSTGRES_POLL_LOCK_ID},
            )
            if acquired is not True:
                raise WechatPollGateUnavailableError()
            try:
                yield
            finally:
                connection.execute(
                    text("SELECT pg_advisory_unlock(:lock_id)"),
                    {"lock_id": _POSTGRES_POLL_LOCK_ID},
                )
                connection.commit()
        return

    if backend != "sqlite":
        raise WechatPollGateUnavailableError()

    database_name = engine.url.database
    if database_name in {None, "", ":memory:"}:
        if not _MEMORY_SQLITE_GATE.acquire(blocking=False):
            raise WechatPollGateUnavailableError()
        try:
            yield
        finally:
            _MEMORY_SQLITE_GATE.release()
        return

    lock_path = Path(f"{Path(database_name).expanduser().resolve()}.wechat-poll.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as lock_file:
        if not _try_lock_file(lock_file):
            raise WechatPollGateUnavailableError()
        try:
            yield
        finally:
            _unlock_file(lock_file)


def _try_lock_file(lock_file: BinaryIO) -> bool:
    lock_file.seek(0, 2)
    if lock_file.tell() == 0:
        lock_file.write(b"\0")
        lock_file.flush()
    lock_file.seek(0)
    if os_name == "nt":
        import msvcrt

        try:
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True

    import fcntl

    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def _unlock_file(lock_file: BinaryIO) -> None:
    lock_file.seek(0)
    if os_name == "nt":
        import msvcrt

        msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
        return

    import fcntl

    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
