"""Read-only recruiting queries, shaped for the tool layer (§6.1, Phase 3).

This is where a :class:`~talent_ops_mcp.repositories.protocols.Page` -- an offset window over
rows -- becomes the cursor-paginated, least-data output §7.1.1 requires. Nothing here writes,
and nothing here imports the MCP layer, so every rule below is exercisable in a plain test with
no client session.

**The repository fields are typed as protocols, not as the SQLite classes.** That is not
decoration: mypy structurally checks each ``Sqlite*Repository`` against the protocol at the
:func:`sqlite_recruiting_service` call below, so a signature that drifts between
``protocols.py`` and ``sqlite.py`` fails the type gate instead of failing at runtime in Phase
9-10 when a second implementation appears. :class:`RecruitingService` itself names no concrete
repository; only that factory does, and an ATS-backed sibling drops in beside it.

**Not-found is a domain error, not an empty result.** A filter that matches nothing returns an
empty page -- that is an answer. An id that does not exist is a caller mistake, and returning
``None`` or an empty record for it invites a model to carry on as though the entity were merely
uninteresting. Every message names the tool that finds a real id, per §7.1.1.
"""

from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from talent_ops_mcp.errors import DomainError, ErrorCode
from talent_ops_mcp.models.domain import ApplicationStatus, JobStatus, Stage
from talent_ops_mcp.models.outputs import (
    ApplicationListResult,
    ApplicationSummary,
    CandidateApplicationContext,
    CandidateDetail,
    InterviewPlanDetail,
    JobSearchResult,
    JobSummary,
    SkillRequirement,
)
from talent_ops_mcp.repositories.protocols import (
    ApplicationRepository,
    ApplicationSummaryRow,
    CandidateRepository,
    InterviewPlanRepository,
    JobRepository,
    Page,
)
from talent_ops_mcp.repositories.sqlite import (
    SqliteApplicationRepository,
    SqliteCandidateRepository,
    SqliteInterviewPlanRepository,
    SqliteJobRepository,
)
from talent_ops_mcp.services.pagination import decode_cursor, encode_cursor

DEFAULT_PAGE_SIZE = 25
"""Page size when a caller names none. Below the 50 default maximum, so the common call is cheap."""

CANDIDATE_APPLICATION_LIMIT = 100
"""Upper bound on the applications shown with one candidate.

The repository clamps this down to the configured page size, so it is a ceiling rather than a
promise. ``(candidate_id, job_id)`` is unique, so nobody can hold more applications than there
are requisitions -- a hundred is far past that, which is what lets
:attr:`~talent_ops_mcp.models.outputs.CandidateDetail.applications` go unpaginated honestly.
"""


def _next_cursor(page: Page[Any]) -> str | None:
    """Token for the page after ``page``, or ``None`` when there is none.

    Derived from the window actually served rather than from the requested limit: the repository
    clamps an oversized limit, and a cursor built from the unclamped value would skip rows.
    """
    return encode_cursor(page.offset + len(page.items)) if page.has_more else None


def _summary(row: ApplicationSummaryRow) -> ApplicationSummary:
    return ApplicationSummary(
        id=row.id,
        candidate_id=row.candidate_id,
        candidate_name=row.candidate_name,
        job_id=row.job_id,
        job_title=row.job_title,
        current_stage=row.current_stage,
        status=row.status,
        awaiting_scheduling=row.awaiting_scheduling,
        last_activity_at=row.last_activity_at,
    )


@dataclass(frozen=True, slots=True)
class RecruitingService:
    """The four read operations of Phase 3."""

    jobs: JobRepository
    candidates: CandidateRepository
    applications: ApplicationRepository
    plans: InterviewPlanRepository

    async def search_jobs(
        self,
        *,
        query: str | None = None,
        status: JobStatus | None = None,
        department: str | None = None,
        limit: int = DEFAULT_PAGE_SIZE,
        cursor: str | None = None,
    ) -> JobSearchResult:
        """One page of requisitions matching every filter given."""
        page = await self.jobs.list(
            query=query,
            status=status,
            department=department,
            limit=limit,
            offset=decode_cursor(cursor),
        )
        return JobSearchResult(
            jobs=[
                JobSummary(
                    id=job.id,
                    title=job.title,
                    department=job.department,
                    location=job.location,
                    default_timezone=job.default_timezone,
                    status=job.status,
                )
                for job in page.items
            ],
            total=page.total,
            count=len(page.items),
            has_more=page.has_more,
            next_cursor=_next_cursor(page),
        )

    async def list_applications(
        self,
        *,
        job_id: str | None = None,
        candidate_id: str | None = None,
        stage: Stage | None = None,
        status: ApplicationStatus | None = None,
        awaiting_scheduling: bool | None = None,
        limit: int = DEFAULT_PAGE_SIZE,
        cursor: str | None = None,
    ) -> ApplicationListResult:
        """One page of applications, each carrying the candidate and job names needed to read it.

        An unknown ``job_id`` or ``candidate_id`` yields an empty page rather than a not-found
        error. A filter is a question about a set, and "no applications match" is a truthful
        answer to it; only a tool addressed *at* an entity can meaningfully fail to find one.
        """
        page = await self.applications.list_summaries(
            job_id=job_id,
            candidate_id=candidate_id,
            stage=stage,
            status=status,
            awaiting_scheduling=awaiting_scheduling,
            limit=limit,
            offset=decode_cursor(cursor),
        )
        return ApplicationListResult(
            applications=[_summary(row) for row in page.items],
            total=page.total,
            count=len(page.items),
            has_more=page.has_more,
            next_cursor=_next_cursor(page),
        )

    async def get_candidate(self, candidate_id: str) -> CandidateDetail:
        """One candidate with contact details and every pipeline they are in."""
        candidate = await self.candidates.get(candidate_id)
        if candidate is None:
            raise DomainError(
                ErrorCode.NOT_FOUND,
                f"no candidate with id {candidate_id!r}. Candidate ids come from "
                "talent_ops_list_applications; they cannot be guessed or constructed.",
            )

        held = await self.applications.list_summaries(candidate_id=candidate_id, limit=CANDIDATE_APPLICATION_LIMIT)
        return CandidateDetail(
            id=candidate.id,
            first_name=candidate.first_name,
            last_name=candidate.last_name,
            email=candidate.email,
            phone=candidate.phone,
            timezone=candidate.timezone,
            preferred_contact_method=candidate.preferred_contact_method,
            applications=[
                CandidateApplicationContext(
                    application_id=row.id,
                    job_id=row.job_id,
                    job_title=row.job_title,
                    current_stage=row.current_stage,
                    status=row.status,
                    awaiting_scheduling=row.awaiting_scheduling,
                    last_activity_at=row.last_activity_at,
                )
                for row in held.items
            ],
        )

    async def get_interview_plan(self, job_id: str, stage: Stage) -> InterviewPlanDetail:
        """The approved plan for one (job, stage).

        The job is resolved first so an unknown id and an unplanned stage give different
        messages: one means "you have the wrong id", the other means "this stage has no approved
        policy and scheduling must not proceed". A single merged error would let a caller retry
        the wrong thing.
        """
        job = await self.jobs.get(job_id)
        if job is None:
            raise DomainError(
                ErrorCode.NOT_FOUND,
                f"no job with id {job_id!r}. Use talent_ops_search_jobs to find the job first.",
            )

        active = await self.plans.get_active(job_id, stage)
        if active is None:
            raise DomainError(
                ErrorCode.NOT_FOUND,
                f"job {job_id!r} has no approved interview plan for the {stage.value!r} stage. "
                "Do not schedule against this stage and do not substitute a duration, panel size, "
                "or lead time of your own; ask a human recruiter to approve a plan first.",
            )

        plan = active.plan
        return InterviewPlanDetail(
            id=plan.id,
            job_id=plan.job_id,
            job_title=job.title,
            default_timezone=job.default_timezone,
            stage=plan.stage,
            interview_type=plan.interview_type,
            duration_minutes=plan.duration_minutes,
            panel_size=plan.panel_size,
            minimum_lead_hours=plan.minimum_lead_hours,
            buffer_before_minutes=plan.buffer_before_minutes,
            buffer_after_minutes=plan.buffer_after_minutes,
            working_hours_policy=plan.working_hours_policy,
            skill_requirements=[
                SkillRequirement(skill_code=req.skill_code, minimum_interviewers=req.minimum_interviewers)
                for req in active.requirements
            ],
        )


def sqlite_recruiting_service(session: AsyncSession, max_page_size: int) -> RecruitingService:
    """Wire the service to the SQLite readers over ``session``.

    The only place in the service layer that names a concrete repository. Phases 9-10 add an
    ATS-backed factory beside this one; nothing in :class:`RecruitingService` changes.
    """
    return RecruitingService(
        jobs=SqliteJobRepository(session, max_page_size),
        candidates=SqliteCandidateRepository(session, max_page_size),
        applications=SqliteApplicationRepository(session, max_page_size),
        plans=SqliteInterviewPlanRepository(session, max_page_size),
    )
