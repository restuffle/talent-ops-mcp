"""Entrypoint for both ``talent-ops-mcp`` and ``python -m talent_ops_mcp``.

Order is load-bearing: logging is configured before ``MCPServer(...)`` is constructed
(docs/reference/mcp-v2-verified-api.md §4.1.1). Nothing is ever written to stdout -- that
belongs to the MCP wire.
"""

import logging

from pydantic import ValidationError

from talent_ops_mcp.logging_config import configure_logging
from talent_ops_mcp.server import build_server
from talent_ops_mcp.settings import Settings


def main() -> int:
    configure_logging("INFO")
    log = logging.getLogger("talent_ops_mcp")

    try:
        settings = Settings()
    except ValidationError as exc:
        log.error("configuration is invalid; fix the TALENT_OPS_* values or .env and retry:\n%s", exc)
        return 2

    configure_logging(settings.log_level)

    # run() is synchronous, defaults to stdio, and internally calls anyio.run().
    build_server(settings).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
