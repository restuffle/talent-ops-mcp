"""MCP tool adapters (§6.1).

An adapter declares the tool's name, annotations, description, and typed parameters, opens a
session, and hands the work to a service. It contains no recruiting rules -- if a decision about
the domain is being made here, it is in the wrong layer.

``talent_ops_health_check`` stays in :mod:`talent_ops_mcp.server`. It reports on the server
itself rather than on recruiting data, it needs no session, and relocating a working, tested
tool would be churn with nothing behind it.

The three names below moved here in Phase 4, when a second adapter module needed them. They are
not a framework -- they are the two facts every read tool in this server shares, kept in one place
so a new adapter cannot quietly disagree about either.
"""

from mcp.types import ToolAnnotations

ID_MAX_LENGTH = 64
"""Matches ``ID = String(64)`` in the schema. Nothing longer can name a real row.

Applied to every identifier parameter, which also bounds what a not-found message can be made to
echo back at the caller.
"""

READ_ONLY = ToolAnnotations(
    read_only_hint=True,
    destructive_hint=False,
    idempotent_hint=True,
    # Local SQLite only. Flips to True in Phases 9-10 for any tool that reaches a calendar or ATS.
    open_world_hint=False,
)
"""All four hints of §7.1.1, set explicitly. ``title`` is filled in per tool at registration.

A hint left unset is not the same as a hint set to False: a client deciding whether to show a
confirmation prompt cannot tell "read-only" from "unknown".
"""


def read_only_annotations(title: str) -> ToolAnnotations:
    """:data:`READ_ONLY` with this tool's title."""
    return READ_ONLY.model_copy(update={"title": title})
