"""Fixtures for the whole suite.

The database fixtures live here rather than in ``tests/integration/conftest.py`` for a boring
reason: this tree has no ``__init__.py`` files, so a second ``conftest`` resolves to the same
top-level module name and mypy refuses the duplicate. One conftest also matches how the suite
was already laid out. Fixtures are lazy, so a unit test that never asks for ``engine`` never
builds one.

§6.2 lists a ``tests/fixtures/`` directory for Phase 2. It is not created: everything it would
hold is here, and §6.2 itself warns against modules that exist only to match the diagram.
"""

import os
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from talent_ops_mcp.db.seed import seed
from talent_ops_mcp.db.session import create_engine, create_session_factory, init_schema
from talent_ops_mcp.settings import Settings


@pytest.fixture(autouse=True)
def isolate_configuration(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Cut every ambient configuration source.

    Clears ``TALENT_OPS_*`` from the environment, and moves the working directory to a
    throwaway one so a developer's local ``.env`` cannot change a test's meaning --
    ``Settings`` resolves ``env_file=".env"`` relative to the working directory.
    """
    for key in list(os.environ):
        if key.startswith("TALENT_OPS_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Settings pointed at a throwaway data directory."""
    return Settings(env="test", data_dir=tmp_path, log_level="DEBUG")


@pytest.fixture
def subprocess_env(tmp_path: Path) -> dict[str, str]:
    """Environment for a real stdio server subprocess."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("TALENT_OPS_")}
    env.update(
        {
            "TALENT_OPS_ENV": "test",
            "TALENT_OPS_DATA_DIR": str(tmp_path),
            "TALENT_OPS_LOG_LEVEL": "INFO",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUNBUFFERED": "1",
        }
    )
    return env


# ------------------------------------------------------------------------------- database
#
# Real SQLite files under tmp_path, not in-memory. In-memory would be faster but would skip the
# two things most worth checking here: that data_dir containment holds on disk, and that the
# foreign-key pragma survives a pooled reconnect.


@pytest.fixture
async def engine(settings: Settings) -> AsyncIterator[AsyncEngine]:
    """An engine over a fresh database file, disposed before the test directory is torn down.

    Disposal is not optional on Windows: an open SQLite handle makes the file undeletable, and
    ``tmp_path`` cleanup then fails with ``WinError 32`` several tests away from the cause.
    """
    created = create_engine(settings)
    try:
        yield created
    finally:
        await created.dispose()


@pytest.fixture
async def initialized_engine(engine: AsyncEngine) -> AsyncEngine:
    """An engine whose schema exists."""
    await init_schema(engine)
    return engine


@pytest.fixture
def session_factory(initialized_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return create_session_factory(initialized_engine)


@pytest.fixture
async def session(session_factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[AsyncSession]:
    """A session on an empty schema. Rolled back at teardown, never committed."""
    async with session_factory() as open_session:
        yield open_session
        await open_session.rollback()


@pytest.fixture
async def seeded_session(session_factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[AsyncSession]:
    """A session over the committed demo dataset.

    Committed rather than left pending so repository queries see the same rows a real caller
    would, including through a separate connection taken from the pool for a subquery.
    """
    async with session_factory() as open_session:
        await seed(open_session)
        await open_session.commit()
        yield open_session


@pytest.fixture
async def seeded_settings(settings: Settings) -> Settings:
    """Settings whose database file already holds the demo dataset.

    For the contract tests, which drive a real ``build_server(settings)``: that server builds its
    *own* engine in its lifespan, so seeding through the ``seeded_session`` fixture would populate
    a different connection pool over the same file and, on Windows, hold a handle the server then
    has to contend with. This seeds and disposes first, leaving only a file behind.
    """
    engine = create_engine(settings)
    try:
        await init_schema(engine)
        async with create_session_factory(engine)() as open_session:
            await seed(open_session)
            await open_session.commit()
    finally:
        await engine.dispose()
    return settings
