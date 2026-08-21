"""The scheduling tools (§7.1). Phase 4 registers the first, which changes nothing.

``from __future__ import annotations`` is deliberately absent for the reason given in
:mod:`talent_ops_mcp.server`.

``talent_ops_propose_interview_slots`` is a *computation* tool: it reads approved policy and
declared availability, works out which times are valid, and returns them. It writes nothing and
reserves nothing, so its annotations are the same read-only set every Phase 3 tool carries. The
tools that do change state -- preview and confirm -- arrive in Phase 5 and will be registered
here beside it with a very different set.

Dates are ``date``-typed parameters, so the SDK advertises ``format: date`` and rejects
``"next tuesday"`` or ``"2026-13-01"`` before this module runs. They are read in the *display*
timezone, which is what a coordinator means by "Tuesday" -- see the parameter descriptions.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime
from typing import Annotated

from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from pydantic import Field

from talent_ops_mcp.errors import sanitized
from talent_ops_mcp.models.outputs import SlotProposalResult
from talent_ops_mcp.server import TOOL_PREFIX, AppState, app_state
from talent_ops_mcp.services.scheduling import (
    DEFAULT_MAX_OPTIONS,
    DEFAULT_SEARCH_DAYS,
    MAX_OPTIONS,
    SchedulingService,
    sqlite_scheduling_service,
)
from talent_ops_mcp.settings import Settings
from talent_ops_mcp.tools import ID_MAX_LENGTH, read_only_annotations

TIMEZONE_MAX_LENGTH = 64
"""Matches ``TIMEZONE = String(64)`` in the schema. The longest real IANA name is far shorter."""


@asynccontextmanager
async def _service(ctx: Context[AppState]) -> AsyncIterator[SchedulingService]:
    """A scheduling service over a fresh session, closed when the tool returns.

    No commit, for the same reason as the recruiting adapter: everything here is a read, and a
    session that never commits cannot leave a write behind by accident.
    """
    state = app_state(ctx)
    async with state.session_factory() as session:
        yield sqlite_scheduling_service(session, state.settings)


def register(mcp: MCPServer[AppState], settings: Settings) -> None:
    """Register the Phase 4 scheduling tool on ``mcp``.

    ``settings`` is read at registration so the advertised date-range bound is the one this server
    will actually enforce, for the reason given in :func:`talent_ops_mcp.tools.recruiting.register`.
    """
    max_days = settings.max_date_range_days
    default_days = min(DEFAULT_SEARCH_DAYS, max_days)

    @mcp.tool(
        name=f"{TOOL_PREFIX}_propose_interview_slots",
        title="Propose interview times",
        description=(
            "Compute interview times that work for the candidate and a qualified panel, for one "
            "application's current stage. Changes nothing and reserves nothing.\n\n"
            "Every option satisfies the stage's approved interview plan: its exact duration, panel "
            "size, required skills, buffers, minimum lead time, and working-hours policy. Options come "
            "back earliest first, each with the panel it assumes and the time in both UTC and the "
            "display timezone. When no time works, `options` is empty and `no_options_reason` plus "
            "`no_options_code` say which constraint bit and what to change. Ranges are capped at "
            f"{max_days} days; omit the dates to search the next {default_days}.\n\n"
            "Use when: a candidate needs an interview arranged and you want valid times to offer, or "
            "you need to know whether a stage can be scheduled at all before promising anything.\n"
            "Do not use: as a booking. Nothing is held — call talent_ops_preview_interview_schedule "
            "and have a human confirm it before telling anyone a time is theirs. Do not adjust a "
            "duration, shrink a panel, or ignore a lead time to make a slot appear; if there is no "
            "option, relay the reason. The panel is chosen positionally, so its membership says "
            "nothing about any interviewer's or candidate's suitability, and neither does the order "
            "of the times."
        ),
        annotations=read_only_annotations("Propose interview times"),
    )
    async def propose_interview_slots(
        ctx: Context[AppState],
        application_id: Annotated[
            str,
            Field(
                max_length=ID_MAX_LENGTH,
                description=(
                    "Exact application identifier, from talent_ops_list_applications. The plan used is "
                    "the approved one for that application's current stage."
                ),
            ),
        ],
        start_date: Annotated[
            date | None,
            Field(
                description=(
                    "First date to search, as YYYY-MM-DD, read in the display timezone. "
                    "Omit to start today. Times before the plan's minimum lead time are skipped even "
                    "when they fall inside the range."
                )
            ),
        ] = None,
        end_date: Annotated[
            date | None,
            Field(
                description=(
                    f"Last date to search, inclusive, as YYYY-MM-DD in the display timezone. Omit for "
                    f"{default_days} days from start_date. A range longer than {max_days} days is "
                    "refused rather than shortened, so an answer always covers what you asked for."
                )
            ),
        ] = None,
        timezone: Annotated[
            str | None,
            Field(
                max_length=TIMEZONE_MAX_LENGTH,
                description=(
                    "IANA timezone for the dates and the returned local times, e.g. America/New_York. "
                    "Omit to use the candidate's own timezone, which is what you should quote to them."
                ),
            ),
        ] = None,
        max_options: Annotated[
            int,
            Field(
                ge=1,
                le=MAX_OPTIONS,
                description=("Most options to return. more_options_exist tells you whether valid times were left out."),
            ),
        ] = DEFAULT_MAX_OPTIONS,
    ) -> SlotProposalResult:
        with sanitized(f"{TOOL_PREFIX}_propose_interview_slots", ctx.request_id):
            async with _service(ctx) as service:
                return await service.propose_interview_slots(
                    application_id=application_id,
                    # The only clock read in the scheduling path. The service takes `now` as a
                    # parameter so its output is a function of its inputs and a test can pin it.
                    now=datetime.now(UTC),
                    start_date=start_date,
                    end_date=end_date,
                    display_timezone=timezone,
                    max_options=max_options,
                )
