"""SQLAlchemy engine and session factory."""

from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import NullPool

from .config import settings


def _engine_options() -> dict:
    """Engine settings for the environment the app is actually running in.

    On a serverless platform each request may land on a fresh instance, and a
    pool held by an instance that is about to be frozen still occupies a slot
    on the database server. Postgres accounts have a hard connection limit, so
    a handful of idle pools is enough to start refusing real requests.
    NullPool opens a connection per session and closes it, leaving the pooling
    to the provider's own pooler (Neon, Supabase and friends all ship one).
    """
    if settings.database_url.startswith("sqlite"):
        return {"connect_args": {"check_same_thread": False}, "pool_pre_ping": True}
    return {"poolclass": NullPool, "pool_pre_ping": True}


engine = create_engine(settings.database_url, echo=False, **_engine_options())

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


class Base(DeclarativeBase):
    pass


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    from . import models  # noqa: F401  (registers the models on the metadata)

    Base.metadata.create_all(bind=engine)
    ensure_schema()


def ensure_schema() -> list[str]:
    """Add tables and columns the models have and the database does not.

    `create_all` creates missing tables but never touches an existing one, so
    a new column on `payments` would reach production code before it reached
    the database — and every query on that table would fail. This closes the
    gap for the one kind of change this project makes: a new column that is
    nullable or has a server default. Anything else (a type change, a rename,
    a dropped column) is a real migration and belongs to a migration tool.

    A table that does not exist at all is simply created — a new table has
    no rows to worry about, so that is always safe.

    Returns what it added, for the log.
    """
    from sqlalchemy import inspect, text

    from . import models  # noqa: F401

    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    added: list[str] = []
    with engine.begin() as connection:
        for table in Base.metadata.sorted_tables:
            if table.name not in existing_tables:
                table.create(bind=connection, checkfirst=True)
                added.append(table.name)
                continue
            present = {c["name"] for c in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in present:
                    continue
                if not column.nullable and column.server_default is None:
                    raise RuntimeError(
                        f"{table.name}.{column.name} is NOT NULL without a "
                        "server default; it needs a real migration."
                    )
                ddl = column.type.compile(dialect=engine.dialect)
                default = ""
                if column.server_default is not None:
                    default = f" DEFAULT '{column.server_default.arg}'"
                if_not_exists = (
                    " IF NOT EXISTS" if engine.dialect.name == "postgresql" else ""
                )
                connection.execute(
                    text(
                        f"ALTER TABLE {table.name} ADD COLUMN{if_not_exists} "
                        f"{column.name} {ddl}{default}"
                    )
                )
                added.append(f"{table.name}.{column.name}")
    return added
