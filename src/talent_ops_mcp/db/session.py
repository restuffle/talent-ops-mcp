"""Engine construction, connection pragmas, and schema initialization.

**Schema strategy: ``create_all``, no migration tool.** §12 Phase 2 permits "migrations or a
documented schema initialization strategy"; this is the documented one. The database is a local
single-file SQLite store that a developer can delete and reseed in one command, and every row in
it is fictional demo data, so there is nothing to preserve across a schema change and nothing for
a migration to migrate. Adding Alembic now would buy a versions directory and a second source of
schema truth that must be kept in step with :mod:`talent_ops_mcp.db.schema` by hand.

That trade flips the moment a deployment holds data someone would miss -- the remote/production
work in Phase 11. Adopt Alembic there, stamping the initial revision from this schema.

**Why the pragma matters.** SQLite ships with foreign keys *off*. Without the listener below,
every ``ForeignKey`` in the schema is inert decoration: a child row pointing at a deleted parent
inserts happily, and ``ondelete="CASCADE"`` never fires. The listener is attached to the engine
rather than executed once, because the pragma is per-connection and the pool opens more than one.
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from talent_ops_mcp.db.base import Base
from talent_ops_mcp.settings import Settings

_log = logging.getLogger(__name__)


def _enforce_sqlite_pragmas(dbapi_connection: Any, _record: Any) -> None:  # noqa: ANN401
    """Turn on foreign keys for this connection.

    Attached only to SQLite engines (see :func:`create_engine`), so it does no dialect check of
    its own. An earlier version guarded on ``isinstance(..., sqlite3.Connection)`` instead and
    silently did nothing: under ``aiosqlite`` the object handed to this event is an
    ``AsyncAdapt_aiosqlite_connection`` wrapping an ``aiosqlite.Connection``, and neither is a
    :class:`sqlite3.Connection`. The pragma never ran and every foreign key stayed inert --
    which is exactly the failure the guard was meant to prevent, so the decision now lives at
    engine construction where the dialect is known for certain.

    ``Any`` is not laziness here: this is a SQLAlchemy event callback, and the object it receives
    is whatever DBAPI wrapper the driver happens to use. SQLAlchemy's own signature for the hook
    is untyped for the same reason, so there is no narrower annotation to reach for.
    """
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
    finally:
        cursor.close()


def create_engine(settings: Settings) -> AsyncEngine:
    """An async engine for the configured database, with pragmas attached.

    Every engine in the process is built here. Constructing one directly with
    :func:`sqlalchemy.ext.asyncio.create_async_engine` would silently skip the foreign-key
    pragma, which is precisely the failure this function exists to make impossible.

    Creating the parent directory is part of the contract: ``data_dir`` is validated by
    :class:`~talent_ops_mcp.settings.Settings` but not required to exist, and SQLite will not
    create a missing directory for you.
    """
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    engine = create_async_engine(settings.database_url)
    if engine.dialect.name == "sqlite":
        event.listen(engine.sync_engine, "connect", _enforce_sqlite_pragmas)
    return engine


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Session factory for the given engine.

    ``expire_on_commit=False`` so a caller can still read an object's attributes after the
    transaction closes. Without it, every post-commit attribute access triggers a lazy refresh --
    which on an async session raises rather than quietly re-querying.
    """
    return async_sessionmaker(engine, expire_on_commit=False)


async def init_schema(engine: AsyncEngine) -> None:
    """Create any missing tables and indexes. Safe to call repeatedly.

    ``create_all`` emits ``IF NOT EXISTS`` and never alters an existing table, so this brings a
    fresh database up but will not notice that a column changed shape. See the module docstring
    for when that stops being good enough.
    """
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    _log.info("schema initialized tables=%d", len(Base.metadata.tables))


@asynccontextmanager
async def session_scope(factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[AsyncSession]:
    """A session wrapped in one transaction: commit on clean exit, roll back on any exception.

    §10.3 requires partial writes to roll back. Doing that here rather than in each caller means
    a service cannot leave half a scheduling operation committed by forgetting the ``except``.
    """
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
