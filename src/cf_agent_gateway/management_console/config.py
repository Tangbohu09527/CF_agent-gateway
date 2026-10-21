from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from cf_agent_gateway.management_console.upstream import NativeReader, SourceConnection, SourceError


@dataclass
class ConsoleConfig:
    runtime: Path
    mode: str
    sources: dict[str, SourceConnection]
    admin: dict
    hermes_connections: dict = field(default_factory=dict)
    reader_factory: object = NativeReader
    _archives: dict = field(default_factory=dict, init=False)

    def __post_init__(self):
        if self.mode not in {"demo", "read_only"}:
            raise ValueError("mode must be demo or read_only")
        if not self.sources:
            raise ValueError("at least one explicit source is required")

    def reader(self, key):
        return self.reader_factory(self.sources[key])

    @contextmanager
    def archive(self, key, candidate_session):
        if self.mode == "demo":
            yield candidate_session
            return
        url = self.sources[key].archive_url
        if not url:
            yield None
            return
        try:
            if key not in self._archives:
                parsed = make_url(url)
                if parsed.get_backend_name() == "sqlite":
                    path = Path(parsed.database).resolve()
                    if not path.is_file():
                        raise SourceError("archive_not_available")
                    engine = create_engine(
                        "sqlite:///file:" + path.as_posix() + "?mode=ro&uri=true"
                    )

                    @event.listens_for(engine, "connect")
                    def read_only(connection, _):
                        connection.execute("PRAGMA query_only=ON")
                elif parsed.get_backend_name() == "postgresql":
                    engine = create_engine(
                        parsed,
                        hide_parameters=True,
                        connect_args={"options": "-c default_transaction_read_only=on"},
                    )
                else:
                    raise SourceError("archive_backend_not_supported")
                self._archives[key] = engine
            with Session(self._archives[key], autoflush=False) as session:
                yield session
        except SQLAlchemyError:
            raise SourceError("archive_read_failed") from None
