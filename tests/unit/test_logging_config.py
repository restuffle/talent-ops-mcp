import logging
import sys

from talent_ops_mcp.logging_config import configure_logging


def test_single_stderr_handler_is_installed() -> None:
    configure_logging("DEBUG")
    root = logging.getLogger()

    assert len(root.handlers) == 1
    handler = root.handlers[0]
    assert isinstance(handler, logging.StreamHandler)
    assert handler.stream is sys.stderr
    assert root.level == logging.DEBUG


def test_a_pre_existing_stdout_handler_is_removed() -> None:
    """stdout belongs to the MCP wire. Any handler pointed at it must go."""
    root = logging.getLogger()
    root.addHandler(logging.StreamHandler(sys.stdout))

    configure_logging("INFO")

    streams = [h.stream for h in root.handlers if isinstance(h, logging.StreamHandler)]
    assert sys.stdout not in streams
    assert streams == [sys.stderr]


def test_records_without_a_request_id_still_format() -> None:
    configure_logging("INFO")
    handler = logging.getLogger().handlers[0]
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "hello", None, None)

    assert handler.formatter is not None
    assert "[-] hello" in handler.formatter.format(record)


def test_repeated_calls_do_not_accumulate_handlers() -> None:
    configure_logging("INFO")
    configure_logging("INFO")
    configure_logging("WARNING")

    assert len(logging.getLogger().handlers) == 1
