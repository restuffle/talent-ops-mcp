import logging

import pytest

from talent_ops_mcp.errors import INTERNAL_ERROR_TEXT, DomainError, ErrorCode, ToolError, sanitized

# What a careless exception might carry. Named to avoid tripping ruff's S105.
LEAKY_MESSAGE = "sqlite+aiosqlite:///C:/Users/private/talent_ops.sqlite3"


def test_domain_error_message_is_code_prefixed() -> None:
    exc = DomainError(ErrorCode.NOT_FOUND, "job does not exist")

    assert str(exc) == "NOT_FOUND: job does not exist"


def test_domain_error_passes_through_with_its_code() -> None:
    with pytest.raises(ToolError) as caught, sanitized("demo"):
        raise DomainError(ErrorCode.CONFLICT, "the application moved to another stage")

    assert str(caught.value) == "CONFLICT: the application moved to another stage"


def test_unexpected_exception_message_never_reaches_the_caller() -> None:
    """The reason this module exists: mcp 2.0.0 hands str(exception) to the client verbatim."""
    with pytest.raises(ToolError) as caught, sanitized("demo"):
        raise ValueError(LEAKY_MESSAGE)

    assert str(caught.value) == INTERNAL_ERROR_TEXT
    assert LEAKY_MESSAGE not in str(caught.value)
    assert "ValueError" not in str(caught.value)


def test_unexpected_exception_detail_is_logged_to_the_handler(caplog: pytest.LogCaptureFixture) -> None:
    with (
        caplog.at_level(logging.ERROR, logger="talent_ops_mcp.errors"),
        pytest.raises(ToolError),
        sanitized("demo", request_id="req-1"),
    ):
        raise ValueError(LEAKY_MESSAGE)

    record = caplog.records[-1]
    assert record.exc_info is not None
    assert getattr(record, "request_id", None) == "req-1"
    assert "outcome=internal_error" in record.getMessage()


def test_cancellation_is_not_swallowed() -> None:
    """anyio cancels handler tasks on notifications/cancelled; that must unwind normally."""
    with pytest.raises(KeyboardInterrupt), sanitized("demo"):
        raise KeyboardInterrupt


def test_success_path_is_transparent() -> None:
    with sanitized("demo"):
        value = 1 + 1

    assert value == 2


def test_every_documented_code_exists() -> None:
    assert {c.value for c in ErrorCode} == {
        "VALIDATION_ERROR",
        "NOT_FOUND",
        "CONFLICT",
        "UNAUTHORIZED",
        "FORBIDDEN",
        "PREVIEW_EXPIRED",
        "PREVIEW_ALREADY_USED",
        "UNSAFE_OPERATION",
        "UPSTREAM_TIMEOUT",
        "UPSTREAM_UNAVAILABLE",
        "INTERNAL_ERROR",
    }
