from pathlib import Path

import pytest
from pydantic import ValidationError

from talent_ops_mcp.settings import DEFAULT_DATABASE_FILENAME, Settings


def test_defaults_are_the_documented_ones(tmp_path: Path) -> None:
    s = Settings(data_dir=tmp_path)

    assert s.env == "development"
    assert s.log_level == "INFO"
    assert s.preview_ttl_seconds == 600
    assert s.max_page_size == 50
    assert s.max_date_range_days == 31
    assert s.slot_interval_minutes == 15
    assert s.resolved_database_path == tmp_path.resolve() / DEFAULT_DATABASE_FILENAME


def test_relative_database_path_resolves_under_data_dir(tmp_path: Path) -> None:
    s = Settings(data_dir=tmp_path, database_path=Path("nested/custom.sqlite3"))

    assert s.resolved_database_path == tmp_path.resolve() / "nested" / "custom.sqlite3"


def test_traversal_out_of_data_dir_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="must resolve inside data_dir"):
        Settings(data_dir=tmp_path / "data", database_path=Path("../escaped.sqlite3"))


def test_absolute_path_outside_data_dir_is_rejected(tmp_path: Path) -> None:
    outside = tmp_path / "outside" / "other.sqlite3"

    with pytest.raises(ValidationError, match="must resolve inside data_dir"):
        Settings(data_dir=tmp_path / "data", database_path=outside)


def test_symlinked_parent_pointing_outside_is_rejected(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    escape_target = tmp_path / "elsewhere"
    escape_target.mkdir()
    try:
        (data_dir / "link").symlink_to(escape_target, target_is_directory=True)
    except OSError:
        pytest.skip("creating a symlink requires elevated privileges on this machine")

    with pytest.raises(ValidationError, match="must resolve inside data_dir"):
        Settings(data_dir=data_dir, database_path=Path("link/escaped.sqlite3"))


@pytest.mark.parametrize(
    ("variable", "value"),
    [
        ("TALENT_OPS_LOG_LEVEL", "TRACE"),
        ("TALENT_OPS_ENV", "staging"),
        ("TALENT_OPS_PREVIEW_TTL_SECONDS", "10"),
        ("TALENT_OPS_PREVIEW_TTL_SECONDS", "100000"),
        ("TALENT_OPS_PREVIEW_TTL_SECONDS", "not-a-number"),
        ("TALENT_OPS_MAX_PAGE_SIZE", "0"),
        ("TALENT_OPS_MAX_PAGE_SIZE", "5000"),
        ("TALENT_OPS_MAX_DATE_RANGE_DAYS", "0"),
        ("TALENT_OPS_MAX_DATE_RANGE_DAYS", "3650"),
        ("TALENT_OPS_SLOT_INTERVAL_MINUTES", "1"),
        ("TALENT_OPS_SLOT_INTERVAL_MINUTES", "600"),
    ],
)
def test_bad_environment_values_fail_fast(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, variable: str, value: str
) -> None:
    """Driven through the environment, which is how a bad value actually arrives."""
    monkeypatch.setenv("TALENT_OPS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv(variable, value)

    with pytest.raises(ValidationError):
        Settings()


def test_unknown_key_in_env_file_is_rejected(tmp_path: Path) -> None:
    """A typo in .env fails loudly: the dotenv source reads every key, so extra="forbid" bites."""
    # The autouse fixture makes tmp_path the working directory, so this is the real ".env".
    (tmp_path / ".env").write_text(
        f"TALENT_OPS_DATA_DIR={tmp_path.as_posix()}\nTALENT_OPS_TOTALY_MADE_UP=1\n",
        encoding="utf-8",
    )

    with pytest.raises(ValidationError):
        Settings()


def test_unknown_shell_variable_is_ignored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Documents a real asymmetry rather than assuming symmetry.

    pydantic-settings resolves the environment by looking up each *known* field name, so it
    never sees an unknown TALENT_OPS_* variable and extra="forbid" cannot fire. A typo in the
    shell is therefore silently ignored where the same typo in .env is rejected. Worth knowing
    when a setting "isn't taking effect".
    """
    monkeypatch.setenv("TALENT_OPS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TALENT_OPS_TOTALY_MADE_UP", "1")

    assert Settings().data_dir == tmp_path.resolve()


def test_environment_variables_are_read_with_the_prefix(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TALENT_OPS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TALENT_OPS_MAX_PAGE_SIZE", "7")
    monkeypatch.setenv("TALENT_OPS_LOG_LEVEL", "WARNING")

    s = Settings()

    assert s.data_dir == tmp_path.resolve()
    assert s.max_page_size == 7
    assert s.log_level == "WARNING"


def test_database_url_is_posix_and_async(tmp_path: Path) -> None:
    url = Settings(data_dir=tmp_path).database_url

    assert url.startswith("sqlite+aiosqlite:///")
    assert "\\" not in url
