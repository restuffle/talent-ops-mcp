import os
from pathlib import Path

import pytest

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
