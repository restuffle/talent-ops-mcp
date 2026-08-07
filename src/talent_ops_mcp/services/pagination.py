"""The opaque cursor every list tool hands back (§7.1.1).

**What this is, and what it is not.** The cursor encodes an offset. §7.1.1 prefers cursors over
offsets because a cursor "stays correct when rows are inserted between pages", and an
offset-backed cursor does *not* deliver that -- a row inserted before the window still shifts
everything by one. What it does deliver is the other half of the argument: the caller cannot do
arithmetic on a token it cannot read, so paging stays a matter of passing the last token back,
and the storage-side representation can change to a keyset without a schema change to any tool.

That trade is deliberate and bounded. The upgrade is a keyset cursor carrying the last row's sort
key, which each repository's ``order_by`` already defines totally; it costs a per-query decode and
is worth doing when this server reads a live ATS (Phases 9-10), where concurrent writes are real.
Against a local single-writer SQLite file seeded with 83 rows, it would be machinery guarding
against something that cannot currently happen.

Encoding is base64url of a tagged decimal. The tag is what makes a corrupted or hand-built token
fail loudly here rather than decode into a plausible-looking offset somewhere downstream.
"""

import base64
import binascii

from talent_ops_mcp.errors import DomainError, ErrorCode

_TAG = "o"

CURSOR_MAX_LENGTH = 64
"""Length bound for the ``cursor`` tool parameter.

A token this module produces is base64 of the tag plus a decimal offset: even an offset larger
than any row count SQLite can hold encodes in under thirty characters. Sixty-four is generous
headroom and still stops a caller handing a megabyte of text to :func:`base64.urlsafe_b64decode`
before it can be rejected. Enforced in the input schema, so an over-long cursor never reaches
:func:`decode_cursor` at all.
"""

CURSOR_ADVICE = (
    "Pass back the exact next_cursor string from the previous response, or omit cursor to start "
    "from the first page. Cursors are opaque and must not be edited or constructed."
)


def encode_cursor(offset: int) -> str:
    """Token that resumes a listing at ``offset``."""
    return base64.urlsafe_b64encode(f"{_TAG}{offset}".encode()).decode("ascii")


def decode_cursor(cursor: str | None) -> int:
    """Offset carried by ``cursor``. ``None`` and ``""`` both mean "start at the beginning".

    Raises :class:`~talent_ops_mcp.errors.DomainError` with ``VALIDATION_ERROR`` on anything else.
    The message names the recovery step and never echoes the rejected token.

    **Why this one does not echo, when the not-found errors do.** A cursor is the only input a
    model is expected to *invent* -- it is handed one and told to pass it back -- so a malformed
    cursor is the input most likely to have been assembled from somewhere else. An identifier is
    different: §7.1.1 explicitly wants identifiers in error messages, echoing one is what makes
    "you have the wrong id" actionable, and ``max_length`` on those parameters bounds the echo to
    64 characters. So this is not the *only* reflection surface in the server, and claiming so
    would be a comforting overstatement -- it is the one where reflecting buys nothing, which is
    what makes refusing to do it free.
    """
    if not cursor:
        return 0
    try:
        decoded = base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
        raise DomainError(
            ErrorCode.VALIDATION_ERROR, f"cursor is not a valid pagination token. {CURSOR_ADVICE}"
        ) from exc

    # `isdigit` is deliberately narrower than `int()`, which accepts a leading sign, surrounding
    # whitespace, underscores as digit separators, and non-ASCII decimal digits. Only the exact
    # shape `encode_cursor` produces is accepted.
    body = decoded[len(_TAG) :]
    if not decoded.startswith(_TAG) or not body.isascii() or not body.isdigit():
        raise DomainError(ErrorCode.VALIDATION_ERROR, f"cursor is not a valid pagination token. {CURSOR_ADVICE}")
    return int(body)
