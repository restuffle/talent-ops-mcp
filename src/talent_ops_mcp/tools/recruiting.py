"""The four read-only recruiting tools of Phase 3 (§7.1).

``from __future__ import annotations`` is deliberately absent for the reason given in
:mod:`talent_ops_mcp.server`: mcp 2.0.0 builds both ``input_schema`` and ``output_schema`` by
introspecting these signatures at runtime, and stringised annotations break that.

**Every parameter carries a bound**, declared as ``Annotated[T, Field(...)]``. ``max_length`` on
text, ``ge``/``le`` on ``limit``, a closed enum on ``status`` and ``stage``. Those propagate into
the advertised ``input_schema``, and the SDK rejects a violating call *before* this module runs,
with a message naming the offending parameter. The ``max_length`` on the id parameters is the
same 64 characters the ``ID`` column allows, which also bounds what a not-found message can be
made to echo back.

``Annotated`` rather than ``param: T = Field(...)``: the plain-default form makes the bound a
mutable-looking argument default, which flake8-bugbear rightly objects to, and the two forms
produce identical schemas -- verified against 2.0.0 before this module was written.

**Every description says when *not* to use the tool.** §7.1.1 makes that binding, and here it
carries the §2.3 boundary too: these tools describe logistics, and each states plainly that it is
not a basis for judging a person. A contract test asserts both halves on every registered tool.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.types import ToolAnnotations
from pydantic import Field

from talent_ops_mcp.errors import sanitized
from talent_ops_mcp.models.domain import ApplicationStatus, JobStatus, Stage
from talent_ops_mcp.models.outputs import (
    ApplicationListResult,
    CandidateDetail,
    InterviewPlanDetail,
    JobSearchResult,
)
from talent_ops_mcp.server import TOOL_PREFIX, AppState, app_state
from talent_ops_mcp.services.pagination import CURSOR_ADVICE, CURSOR_MAX_LENGTH
from talent_ops_mcp.services.recruiting import DEFAULT_PAGE_SIZE, RecruitingService, sqlite_recruiting_service
from talent_ops_mcp.settings import Settings

ID_MAX_LENGTH = 64
"""Matches ``ID = String(64)`` in the schema. Nothing longer can name a real row."""

READ_ONLY = ToolAnnotations(
    read_only_hint=True,
    destructive_hint=False,
    idempotent_hint=True,
    # Local SQLite only. Flips to True in Phases 9-10 for any tool that reaches a calendar or ATS.
    open_world_hint=False,
)
"""Shared by all four tools; ``title`` is set per tool at registration."""

JobIdFilter = Annotated[
    str | None,
    Field(max_length=ID_MAX_LENGTH, description="Restrict to one job. Ids come from talent_ops_search_jobs."),
]
CandidateIdFilter = Annotated[
    str | None,
    Field(max_length=ID_MAX_LENGTH, description="Restrict to one candidate's applications."),
]
Cursor = Annotated[str | None, Field(max_length=CURSOR_MAX_LENGTH, description=CURSOR_ADVICE)]


def _annotations(title: str) -> ToolAnnotations:
    return READ_ONLY.model_copy(update={"title": title})


@asynccontextmanager
async def _service(ctx: Context[AppState]) -> AsyncIterator[RecruitingService]:
    """A recruiting service over a fresh session, closed when the tool returns.

    No commit: every operation in this module is a read, and a session that never commits cannot
    leave one behind by accident.
    """
    state = app_state(ctx)
    async with state.session_factory() as session:
        yield sqlite_recruiting_service(session, state.settings.max_page_size)


def register(mcp: MCPServer[AppState], settings: Settings) -> None:
    """Register the Phase 3 read tools on ``mcp``.

    ``settings`` is read at registration so ``limit``'s advertised maximum is the one this server
    will actually honour. A schema promising 50 on a server configured for 10 would be a lie the
    model has no way to detect -- its request would simply be clamped and it would never know.
    """
    # Spelled out at both call sites rather than bound to a local alias: an annotation that is a
    # local variable is not a type mypy will accept, and two `type: ignore[valid-type]` comments
    # cost more than one repeated Field.
    max_page_size = settings.max_page_size
    default_page_size = min(DEFAULT_PAGE_SIZE, max_page_size)
    limit_help = "Maximum rows in this page. A larger request is clamped to the maximum, not refused."

    @mcp.tool(
        name=f"{TOOL_PREFIX}_search_jobs",
        title="Search recruiting jobs",
        description=(
            "Find recruiting requisitions by title text, status, or department.\n\n"
            "Returns a page of jobs — id, title, department, location, the job's IANA timezone, and "
            "status — with total, count, has_more, and next_cursor so you can tell a partial answer "
            "from a complete one. `query` matches the job title only; use `department` for an exact "
            "department match. Only `open` jobs accept new interview activity.\n\n"
            "Use when: you need a job_id before calling another tool, the user names a role in words "
            "rather than by id, or you need to know whether a requisition is still open.\n"
            "Do not use: to find people — call talent_ops_list_applications for applications, or "
            "talent_ops_get_candidate for one person. This tool says nothing about any candidate, and "
            "job data must not be used to judge who should be put forward for a role."
        ),
        annotations=_annotations("Search recruiting jobs"),
    )
    async def search_jobs(
        ctx: Context[AppState],
        query: Annotated[
            str | None,
            Field(
                max_length=100,
                description="Case-insensitive text matched against the job title. Omit to list every job.",
            ),
        ] = None,
        status: Annotated[
            JobStatus | None,
            Field(description="Restrict to one status. Omit to include open, on-hold, and closed jobs."),
        ] = None,
        department: Annotated[
            str | None,
            Field(max_length=100, description="Exact department name, e.g. Engineering. Case-sensitive."),
        ] = None,
        limit: Annotated[int, Field(ge=1, le=max_page_size, description=limit_help)] = default_page_size,
        cursor: Cursor = None,
    ) -> JobSearchResult:
        with sanitized(f"{TOOL_PREFIX}_search_jobs", ctx.request_id):
            async with _service(ctx) as service:
                return await service.search_jobs(
                    query=query, status=status, department=department, limit=limit, cursor=cursor
                )

    @mcp.tool(
        name=f"{TOOL_PREFIX}_list_applications",
        title="List job applications",
        description=(
            "List applications — one candidate's pursuit of one job — filtered by job, candidate, "
            "pipeline stage, status, or whether they need scheduling work.\n\n"
            "Each row carries the candidate and job names so a list is readable without a follow-up "
            "call, plus total, count, has_more, and next_cursor. Set `awaiting_scheduling` to true for "
            "the coordination worklist: applications that are active, whose current stage has an "
            "approved interview plan, and that have no interview booked. Rows are ordered by most "
            "recent activity, which is a recency order and nothing more.\n\n"
            "Use when: building a list of interviews that still need arranging, checking where a "
            "candidate sits in a pipeline, or confirming an application is `active` before scheduling "
            "against it.\n"
            "Do not use: to rank, score, shortlist, or compare candidates — the ordering means nothing "
            "about any person, and this data must not be used to advise on hiring. It returns no email "
            "address or phone number; call talent_ops_get_candidate for the one person you need to "
            "contact."
        ),
        annotations=_annotations("List job applications"),
    )
    async def list_applications(
        ctx: Context[AppState],
        job_id: JobIdFilter = None,
        candidate_id: CandidateIdFilter = None,
        stage: Annotated[
            Stage | None,
            Field(
                description=(
                    "Restrict to one pipeline stage. Independent of status: a withdrawn application "
                    "still sits at a stage."
                )
            ),
        ] = None,
        status: Annotated[
            ApplicationStatus | None,
            Field(description="Restrict to one status. Only `active` applications may be scheduled against."),
        ] = None,
        awaiting_scheduling: Annotated[
            bool | None,
            Field(
                description=(
                    "True for applications needing coordination work, false for those not needing it. "
                    "Omit to include both."
                )
            ),
        ] = None,
        limit: Annotated[int, Field(ge=1, le=max_page_size, description=limit_help)] = default_page_size,
        cursor: Cursor = None,
    ) -> ApplicationListResult:
        with sanitized(f"{TOOL_PREFIX}_list_applications", ctx.request_id):
            async with _service(ctx) as service:
                return await service.list_applications(
                    job_id=job_id,
                    candidate_id=candidate_id,
                    stage=stage,
                    status=status,
                    awaiting_scheduling=awaiting_scheduling,
                    limit=limit,
                    cursor=cursor,
                )

    @mcp.tool(
        name=f"{TOOL_PREFIX}_get_candidate",
        title="Get one candidate",
        description=(
            "Retrieve one candidate by id: their name, contact details, IANA timezone, preferred "
            "contact method, and every application they hold.\n\n"
            "This is the only tool that returns an email address or phone number, and it is reachable "
            "only by exact id — there is no bulk read of candidate contact details, by design. Always "
            "quote a proposed time to the candidate in their own timezone.\n\n"
            "Use when: you are about to propose, confirm, or communicate an interview time and need "
            "that person's timezone or how to reach them.\n"
            "Do not use: to browse or search for people, or to assess one. The record carries no "
            "score, rating, or evaluative field, and their name, address, timezone, or contact "
            "preference must not be used to infer anything about them or to compare them with anyone "
            "else. Requests of that kind are refused."
        ),
        annotations=_annotations("Get one candidate"),
    )
    async def get_candidate(
        ctx: Context[AppState],
        candidate_id: Annotated[
            str,
            Field(
                max_length=ID_MAX_LENGTH,
                description="Exact candidate identifier, from talent_ops_list_applications. Cannot be guessed.",
            ),
        ],
    ) -> CandidateDetail:
        with sanitized(f"{TOOL_PREFIX}_get_candidate", ctx.request_id):
            async with _service(ctx) as service:
                return await service.get_candidate(candidate_id)

    @mcp.tool(
        name=f"{TOOL_PREFIX}_get_interview_plan",
        title="Get an approved interview plan",
        description=(
            "Retrieve the approved interview plan for one job at one pipeline stage.\n\n"
            "Every number scheduling is permitted to assume comes from here: duration, panel size, "
            "minimum lead time, buffers before and after, whose business hours bound a slot, and which "
            "skills the panel must cover. Only the currently approved plan is returned; superseded "
            "ones are retained for history and never surfaced.\n\n"
            "Use when: before proposing interview slots or previewing a schedule, so the proposal is "
            "built from approved policy rather than from assumption.\n"
            "Do not use: as a fallback when it returns NOT_FOUND. That means the stage has no approved "
            "plan, and you must not substitute a duration, panel size, or lead time of your own — stop "
            "and ask a human recruiter to approve one. This tool describes process, not people, and "
            "says nothing about any candidate."
        ),
        annotations=_annotations("Get an approved interview plan"),
    )
    async def get_interview_plan(
        ctx: Context[AppState],
        job_id: Annotated[
            str,
            Field(max_length=ID_MAX_LENGTH, description="Exact job identifier, from talent_ops_search_jobs."),
        ],
        stage: Annotated[Stage, Field(description="Pipeline stage the plan applies to.")],
    ) -> InterviewPlanDetail:
        with sanitized(f"{TOOL_PREFIX}_get_interview_plan", ctx.request_id):
            async with _service(ctx) as service:
                return await service.get_interview_plan(job_id, stage)
