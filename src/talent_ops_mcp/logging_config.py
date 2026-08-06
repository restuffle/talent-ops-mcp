"""stderr-only logging.

Two SDK behaviours make the call order here load-bearing
(docs/reference/mcp-v2-verified-api.md §4.1.1):

* ``MCPServer.__init__`` calls ``logging.basicConfig()`` itself.
* ``basicConfig()`` never replaces handlers that already exist.

So :func:`configure_logging` must run *before* the server is constructed, otherwise the
SDK's handler wins. It also clears pre-existing handlers, which removes any
``StreamHandler(sys.stdout)`` a dependency may have installed -- stdout belongs to the
MCP wire and nothing else.
"""

import logging
import sys
from typing import Final

_FORMAT: Final = "%(asctime)s %(levelname)-8s %(name)s [%(request_id)s] %(message)s"


class RequestIdFormatter(logging.Formatter):
    """Supplies a placeholder ``request_id`` so third-party records still format."""

    def format(self, record: logging.LogRecord) -> str:
        if not hasattr(record, "request_id"):
            record.request_id = "-"
        return super().format(record)


def configure_logging(level: str) -> None:
    """Install a single stderr handler on the root logger. Idempotent."""
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(RequestIdFormatter(_FORMAT))

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level)
