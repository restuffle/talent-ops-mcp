"""Stable, sanitised error codes and the single chokepoint that enforces them.

mcp 2.0.0 returns ``str(exception)`` from a tool body to the client **verbatim**
(docs/reference/mcp-v2-verified-api.md §5.4):

    except Exception as e:
        return CallToolResult(content=[TextContent(type="text", text=str(e))], is_error=True)

The SDK's auto-sanitisation lives at the protocol-handler layer, which is outside the tool
body and therefore does not protect us. A ``ValueError`` mentioning a connection string
would be handed straight to the model. So no exception may leave a tool adapter
unsanitised, and :func:`sanitized` is the one place that guarantees it.
"""

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from enum import StrEnum

_log = logging.getLogger(__name__)


class ErrorCode(StrEnum):
    """Codes the model may branch on. Stable across versions."""

    VALIDATION_ERROR = "VALIDATION_ERROR"
    NOT_FOUND = "NOT_FOUND"
    CONFLICT = "CONFLICT"
    UNAUTHORIZED = "UNAUTHORIZED"
    FORBIDDEN = "FORBIDDEN"
    PREVIEW_EXPIRED = "PREVIEW_EXPIRED"
    PREVIEW_ALREADY_USED = "PREVIEW_ALREADY_USED"
    UNSAFE_OPERATION = "UNSAFE_OPERATION"
    UPSTREAM_TIMEOUT = "UPSTREAM_TIMEOUT"
    UPSTREAM_UNAVAILABLE = "UPSTREAM_UNAVAILABLE"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class DomainError(Exception):
    """A failure the model is allowed to see. Its message must already be safe to disclose.

    Raise this from services and repositories. Never put a path, connection string,
    credential, SQL fragment, or full candidate record in ``message``.
    """

    def __init__(self, code: ErrorCode, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code.value}: {message}")


class ToolError(Exception):
    """Already sanitised. ``str()`` of this is exactly what the client receives."""


INTERNAL_ERROR_TEXT = (
    f"{ErrorCode.INTERNAL_ERROR.value}: the server hit an unexpected condition. "
    "Details are in the server's stderr log, not in this response."
)


@contextmanager
def sanitized(operation: str, request_id: str = "-") -> Iterator[None]:
    """Convert anything raised inside into a disclosure-safe :class:`ToolError`.

    Wrap every tool adapter body in this. ``BaseException`` is deliberately not caught, so
    the cancellation the v2 dispatcher raises on ``notifications/cancelled``
    (docs/reference/mcp-v2-verified-api.md §8.1) still propagates and unwinds normally.
    """
    extra = {"request_id": request_id}
    try:
        yield
    except DomainError as exc:
        _log.warning("operation=%s outcome=domain_error code=%s", operation, exc.code.value, extra=extra)
        raise ToolError(str(exc)) from None
    except Exception as exc:
        # exc_info goes to stderr; the client gets the generic text only.
        _log.error(
            "operation=%s outcome=internal_error exception=%s",
            operation,
            type(exc).__name__,
            exc_info=True,
            extra=extra,
        )
        raise ToolError(INTERNAL_ERROR_TEXT) from None
