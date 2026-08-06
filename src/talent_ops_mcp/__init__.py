"""Talent Ops MCP -- recruiting coordination over the Model Context Protocol.

This server coordinates recruiting logistics: finding jobs and applications, computing
compatible interview times, and creating interviews after explicit human confirmation.

It does not score, rank, compare, recommend, hire, or reject candidates, and it does not
infer protected characteristics. That boundary is enforced in server code, not in prose.
"""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("talent-ops-mcp")
except PackageNotFoundError:  # pragma: no cover - source tree with no install
    __version__ = "0.0.0+unknown"

__all__ = ["__version__"]
