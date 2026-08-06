"""Validated runtime configuration.

mcp 2.0.0 dropped the SDK's own ``MCP_*`` / ``.env`` handling
(docs/reference/mcp-v2-verified-api.md §3), so this module is the single source of
configuration truth. Invalid configuration fails at construction, before the server
starts serving, so a bad value can never become a runtime surprise.
"""

from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Environment = Literal["development", "test", "production"]
LogLevel = Literal["CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"]

DEFAULT_DATABASE_FILENAME = "talent_ops.sqlite3"


class Settings(BaseSettings):
    """Server configuration, sourced from ``TALENT_OPS_*`` variables or a local ``.env``."""

    model_config = SettingsConfigDict(
        env_prefix="TALENT_OPS_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="forbid",
    )

    env: Environment = Field(default="development", description="Runtime environment.")

    data_dir: Path = Field(
        default=Path("data"),
        description="Only directory the server may read or write. The database must resolve inside it.",
    )
    database_path: Path | None = Field(
        default=None,
        description=(
            f"SQLite file. Relative paths resolve under data_dir. Defaults to <data_dir>/{DEFAULT_DATABASE_FILENAME}."
        ),
    )

    log_level: LogLevel = Field(default="INFO", description="stderr logging level. stdout is never used.")

    preview_ttl_seconds: int = Field(
        default=600, ge=30, le=3600, description="Lifetime of a scheduling preview's confirmation token."
    )
    max_page_size: int = Field(default=50, ge=1, le=200, description="Upper bound on any list tool's page size.")
    max_date_range_days: int = Field(
        default=31, ge=1, le=90, description="Upper bound on any scheduling or reporting date range."
    )
    slot_interval_minutes: int = Field(
        default=15, ge=5, le=60, description="Slot alignment grid used by the scheduling engine."
    )

    @model_validator(mode="after")
    def _resolve_and_contain_paths(self) -> "Settings":
        """Resolve both paths and prove the database sits inside ``data_dir``.

        ``resolve()`` follows symlinks, so a symlinked parent pointing outside ``data_dir``
        is caught here rather than at first write. Non-existent paths resolve fine -- the
        database file does not need to exist yet.
        """
        data_dir = self.data_dir.expanduser().resolve()

        candidate = self.database_path or Path(DEFAULT_DATABASE_FILENAME)
        candidate = candidate.expanduser()
        if not candidate.is_absolute():
            candidate = data_dir / candidate
        database_path = candidate.resolve()

        if not database_path.is_relative_to(data_dir):
            raise ValueError(
                f"database_path must resolve inside data_dir. "
                f"data_dir={data_dir} resolved database_path={database_path}"
            )

        self.data_dir = data_dir
        self.database_path = database_path
        return self

    @property
    def resolved_database_path(self) -> Path:
        """The validated absolute database path.

        ``database_path`` is typed optional only so it can be omitted by the caller; the
        model validator always populates it.
        """
        if self.database_path is None:  # pragma: no cover - validator guarantees this
            raise RuntimeError("settings were not validated")
        return self.database_path

    @property
    def database_url(self) -> str:
        """SQLAlchemy async URL. Never log this -- it embeds a filesystem path."""
        return f"sqlite+aiosqlite:///{self.resolved_database_path.as_posix()}"
