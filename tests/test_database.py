from sqlalchemy import text

from cf_agent_gateway.database import create_database_engine


def test_sqlite_engine() -> None:
    engine = create_database_engine("sqlite+pysqlite:///:memory:")
    try:
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT 1")) == 1
    finally:
        engine.dispose()


def test_postgresql_engine_configuration() -> None:
    engine = create_database_engine("postgresql+psycopg://user:password@localhost/gateway")
    try:
        assert engine.dialect.name == "postgresql"
        assert engine.driver == "psycopg"
    finally:
        engine.dispose()
