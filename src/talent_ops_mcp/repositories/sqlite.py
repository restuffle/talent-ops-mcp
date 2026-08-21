"""SQLite implementations of the repository protocols.

Every query here is read-only and every list is bounded. Writes belong to the services that own
a transaction (§10.2), so nothing in this module commits.

Ordering is always explicit and always total -- a deterministic tiebreak on the primary key
follows every sort key. Without it SQLite may return rows in any order for equal keys, and
"page 2" could repeat or skip a row that page 1 already showed.
"""

import logging
from collections import defaultdict
from collections.abc import Sequence
from datetime import datetime
from typing import Any, TypeVar

from sqlalchemy import ColumnElement, Select, and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from talent_ops_mcp.db.base import Base
from talent_ops_mcp.db.schema import (
    Application,
    AvailabilityWindow,
    Candidate,
    Interview,
    InterviewAttendee,
    Interviewer,
    InterviewerSkill,
    InterviewPlan,
    InterviewPlanSkillRequirement,
    Job,
)
from talent_ops_mcp.errors import DomainError, ErrorCode
from talent_ops_mcp.models.domain import ApplicationStatus, InterviewStatus, JobStatus, PartyType, Stage
from talent_ops_mcp.repositories.protocols import (
    ActivePlan,
    ApplicationSummaryRow,
    InterviewerProfile,
    Page,
    PartyInterval,
)

_log = logging.getLogger(__name__)

_ModelT = TypeVar("_ModelT", bound=Base)

DEFAULT_MAX_PAGE_SIZE = 50

# `_` and `%` are LIKE wildcards. A search for "a_b" must mean the literal string, not "a<any>b",
# so they are escaped and the escape character is declared on the clause.
_LIKE_ESCAPE = "\\"


def _escape_like(term: str) -> str:
    return (
        term.replace(_LIKE_ESCAPE, _LIKE_ESCAPE * 2).replace("%", f"{_LIKE_ESCAPE}%").replace("_", f"{_LIKE_ESCAPE}_")
    )


class _Repository:
    """Shared pagination machinery.

    ``max_page_size`` comes from :class:`~talent_ops_mcp.settings.Settings` so the bound is
    configuration rather than a constant buried in a query.
    """

    def __init__(self, session: AsyncSession, max_page_size: int = DEFAULT_MAX_PAGE_SIZE) -> None:
        self._session = session
        self._max_page_size = max_page_size

    def _effective_limit(self, limit: int) -> int:
        if limit < 1:
            raise DomainError(ErrorCode.VALIDATION_ERROR, f"limit must be at least 1, got {limit}")
        return min(limit, self._max_page_size)

    @staticmethod
    def _check_offset(offset: int) -> int:
        if offset < 0:
            raise DomainError(ErrorCode.VALIDATION_ERROR, f"offset cannot be negative, got {offset}")
        return offset

    async def _count(self, statement: Select[Any]) -> int:
        """Rows ``statement`` would return, ignoring its window.

        Counting via a subquery of the filtered statement rather than rebuilding the filters keeps
        the two in step -- a filter added to one and forgotten in the other would produce a
        ``total`` that disagrees with ``items`` and a ``has_more`` that lies.
        """
        counted = select(func.count()).select_from(statement.order_by(None).subquery())
        return int((await self._session.execute(counted)).scalar_one())

    async def _paginate(
        self,
        statement: Select[tuple[_ModelT]],
        *,
        order_by: Sequence[Any],
        limit: int,
        offset: int,
    ) -> Page[_ModelT]:
        """Run ``statement`` twice: once counted, once windowed."""
        effective = self._effective_limit(limit)
        offset = self._check_offset(offset)

        total = await self._count(statement)
        rows = (
            (await self._session.execute(statement.order_by(*order_by).limit(effective).offset(offset))).scalars().all()
        )
        return Page(items=tuple(rows), total=total, limit=effective, offset=offset)

    async def _get(self, model: type[_ModelT], primary_key: str) -> _ModelT | None:
        return await self._session.get(model, primary_key)


class SqliteJobRepository(_Repository):
    """Reads over :class:`~talent_ops_mcp.db.schema.Job`."""

    async def get(self, job_id: str) -> Job | None:
        return await self._get(Job, job_id)

    async def list(
        self,
        *,
        query: str | None = None,
        status: JobStatus | None = None,
        department: str | None = None,
        limit: int = 25,
        offset: int = 0,
    ) -> Page[Job]:
        """Filter by free-text title, status, and department.

        ``query`` matches the title only. Searching the department and location text too was
        considered and rejected: "Engineering" would then match every role in the department as
        well as every role with the word in its title, and a caller who wanted the department has
        an exact filter for it.
        """
        statement = select(Job)
        if query:
            pattern = f"%{_escape_like(query.strip().lower())}%"
            statement = statement.where(func.lower(Job.title).like(pattern, escape=_LIKE_ESCAPE))
        if status is not None:
            statement = statement.where(Job.status == status)
        if department is not None:
            statement = statement.where(Job.department == department)
        return await self._paginate(statement, order_by=(Job.title, Job.id), limit=limit, offset=offset)


class SqliteCandidateRepository(_Repository):
    """Reads over :class:`~talent_ops_mcp.db.schema.Candidate`."""

    async def get(self, candidate_id: str) -> Candidate | None:
        return await self._get(Candidate, candidate_id)

    async def search(
        self,
        *,
        query: str | None = None,
        limit: int = 25,
        offset: int = 0,
    ) -> Page[Candidate]:
        """Case-insensitive substring match across name and email.

        Matching is over identifying fields only. There is no relevance score and no ordering by
        anything a reader could mistake for candidate quality -- results come back alphabetically,
        which is useful for finding a person and useless for ranking one.
        """
        statement = select(Candidate)
        if query:
            pattern = f"%{_escape_like(query.strip().lower())}%"
            statement = statement.where(
                func.lower(Candidate.first_name).like(pattern, escape=_LIKE_ESCAPE)
                | func.lower(Candidate.last_name).like(pattern, escape=_LIKE_ESCAPE)
                | func.lower(Candidate.email).like(pattern, escape=_LIKE_ESCAPE)
            )
        return await self._paginate(
            statement,
            order_by=(Candidate.last_name, Candidate.first_name, Candidate.id),
            limit=limit,
            offset=offset,
        )


def _awaiting_scheduling() -> ColumnElement[bool]:
    """True when an application needs coordination work.

    Three conditions, all required: the application is active, its current stage has an *approved*
    plan, and no interview is currently on the calendar for it. Derived on every read rather than
    stored, so confirming an interview flips it with no second write to keep in step -- see
    :class:`~talent_ops_mcp.repositories.protocols.ApplicationRepository`.

    The interview check is not narrowed to the current stage's plan. An application with any
    scheduled interview at all is one a coordinator has already touched, and treating it as
    outstanding would put it back on a worklist that exists to surface *untouched* work. A
    cancelled interview does not count, which is what makes a cancellation resurface the row.
    """
    plan_approved = (
        select(1)
        .select_from(InterviewPlan)
        .where(
            InterviewPlan.job_id == Application.job_id,
            InterviewPlan.stage == Application.current_stage,
            InterviewPlan.active.is_(True),
        )
        .exists()
    )
    already_booked = (
        select(1)
        .select_from(Interview)
        .where(
            Interview.application_id == Application.id,
            Interview.status == InterviewStatus.SCHEDULED,
        )
        .exists()
    )
    return and_(Application.status == ApplicationStatus.ACTIVE, plan_approved, ~already_booked)


class SqliteApplicationRepository(_Repository):
    """Reads over :class:`~talent_ops_mcp.db.schema.Application`."""

    async def get(self, application_id: str) -> Application | None:
        return await self._get(Application, application_id)

    @staticmethod
    def _filtered(
        statement: Select[Any],
        *,
        job_id: str | None,
        candidate_id: str | None,
        stage: Stage | None,
        status: ApplicationStatus | None,
        awaiting_scheduling: bool | None,
    ) -> Select[Any]:
        """Apply the shared filter set.

        Both :meth:`list` and :meth:`list_summaries` route through here so the two can never drift
        into answering the same question differently -- which would show a coordinator one count in
        a summary and another in a detail read.
        """
        if job_id is not None:
            statement = statement.where(Application.job_id == job_id)
        if candidate_id is not None:
            statement = statement.where(Application.candidate_id == candidate_id)
        if stage is not None:
            statement = statement.where(Application.current_stage == stage)
        if status is not None:
            statement = statement.where(Application.status == status)
        if awaiting_scheduling is not None:
            needs = _awaiting_scheduling()
            statement = statement.where(needs if awaiting_scheduling else ~needs)
        return statement

    async def list(
        self,
        *,
        job_id: str | None = None,
        candidate_id: str | None = None,
        stage: Stage | None = None,
        status: ApplicationStatus | None = None,
        awaiting_scheduling: bool | None = None,
        limit: int = 25,
        offset: int = 0,
    ) -> Page[Application]:
        statement = self._filtered(
            select(Application),
            job_id=job_id,
            candidate_id=candidate_id,
            stage=stage,
            status=status,
            awaiting_scheduling=awaiting_scheduling,
        )
        return await self._paginate(
            statement,
            order_by=(Application.last_activity_at.desc(), Application.id),
            limit=limit,
            offset=offset,
        )

    async def list_summaries(
        self,
        *,
        job_id: str | None = None,
        candidate_id: str | None = None,
        stage: Stage | None = None,
        status: ApplicationStatus | None = None,
        awaiting_scheduling: bool | None = None,
        limit: int = 25,
        offset: int = 0,
    ) -> Page[ApplicationSummaryRow]:
        """The same rows as :meth:`list`, joined to the candidate and job names.

        Both joins are inner: ``candidate_id`` and ``job_id`` are non-nullable foreign keys with
        the pragma enforced, so a row with no match cannot exist. An outer join would only add a
        ``None`` branch for a state the database forbids.
        """
        statement = self._filtered(
            select(
                Application.id,
                Application.candidate_id,
                Candidate.first_name,
                Candidate.last_name,
                Application.job_id,
                Job.title,
                Application.current_stage,
                Application.status,
                _awaiting_scheduling().label("awaiting_scheduling"),
                Application.last_activity_at,
            )
            .join(Candidate, Candidate.id == Application.candidate_id)
            .join(Job, Job.id == Application.job_id),
            job_id=job_id,
            candidate_id=candidate_id,
            stage=stage,
            status=status,
            awaiting_scheduling=awaiting_scheduling,
        )

        effective = self._effective_limit(limit)
        offset = self._check_offset(offset)
        total = await self._count(statement)
        windowed = (
            statement.order_by(Application.last_activity_at.desc(), Application.id).limit(effective).offset(offset)
        )
        rows = (await self._session.execute(windowed)).all()

        return Page(
            items=tuple(
                ApplicationSummaryRow(
                    id=row.id,
                    candidate_id=row.candidate_id,
                    candidate_name=f"{row.first_name} {row.last_name}",
                    job_id=row.job_id,
                    job_title=row.title,
                    current_stage=row.current_stage,
                    status=row.status,
                    # SQLite has no boolean type; the EXISTS arithmetic comes back as 0 or 1.
                    awaiting_scheduling=bool(row.awaiting_scheduling),
                    last_activity_at=row.last_activity_at,
                )
                for row in rows
            ),
            total=total,
            limit=effective,
            offset=offset,
        )


class SqliteInterviewPlanRepository(_Repository):
    """Reads over :class:`~talent_ops_mcp.db.schema.InterviewPlan`."""

    async def get_active(self, job_id: str, stage: Stage) -> ActivePlan | None:
        """The single active plan for a (job, stage), with its skill requirements.

        ``active`` is filtered in SQL rather than checked after the fetch: the partial unique index
        guarantees at most one active row per pair, so this returns one plan or nothing without a
        tiebreak. Superseded plans stay in the table and stay invisible here.
        """
        plan = (
            await self._session.execute(
                select(InterviewPlan).where(
                    InterviewPlan.job_id == job_id,
                    InterviewPlan.stage == stage,
                    InterviewPlan.active.is_(True),
                )
            )
        ).scalar_one_or_none()
        if plan is None:
            return None

        requirements = (
            (
                await self._session.execute(
                    select(InterviewPlanSkillRequirement)
                    .where(InterviewPlanSkillRequirement.interview_plan_id == plan.id)
                    .order_by(InterviewPlanSkillRequirement.skill_code)
                )
            )
            .scalars()
            .all()
        )
        return ActivePlan(plan=plan, requirements=tuple(requirements))


class SqliteInterviewerRepository(_Repository):
    """Reads over :class:`~talent_ops_mcp.db.schema.Interviewer`."""

    async def get(self, interviewer_id: str) -> Interviewer | None:
        return await self._get(Interviewer, interviewer_id)

    async def list(
        self,
        *,
        active: bool | None = None,
        skills: tuple[str, ...] = (),
        limit: int = 25,
        offset: int = 0,
    ) -> Page[Interviewer]:
        """Filter by status and by *all* of ``skills``.

        The conjunction is expressed as "count of matching skill rows equals the number
        requested" rather than one ``EXISTS`` per code. Both are correct; this one keeps the
        statement a fixed shape regardless of how many skills a plan demands.
        """
        statement = select(Interviewer)
        if active is not None:
            statement = statement.where(Interviewer.active == active)
        if skills:
            wanted = set(skills)
            matching = (
                select(func.count())
                .select_from(InterviewerSkill)
                .where(
                    InterviewerSkill.interviewer_id == Interviewer.id,
                    InterviewerSkill.skill_code.in_(wanted),
                )
                .scalar_subquery()
            )
            statement = statement.where(matching == len(wanted))
        return await self._paginate(statement, order_by=(Interviewer.name, Interviewer.id), limit=limit, offset=offset)

    async def list_active_with_skills(self, *, limit: int) -> tuple[InterviewerProfile, ...]:
        """The panel-selection pool, ordered by id.

        Ordered by id rather than by name because the ordering is load-bearing: §9.2 step 11
        requires deterministic proposals, and the panel chosen for a slot is the first valid
        combination in this order. A name is editable and a person can share one; an id is neither.
        That also keeps the choice visibly positional rather than evaluative -- being first in id
        order says nothing about anyone (§2.3).

        Skills are fetched in a second statement rather than by joining and grouping. The join
        would return one row per (interviewer, skill) and force the caller to regroup anyway, and
        an interviewer holding no skills would need an outer join to survive it -- which is the
        row a plan with no skill requirements most needs.
        """
        statement = select(Interviewer).where(Interviewer.active.is_(True)).order_by(Interviewer.id)
        # One row past the bound, purely to detect that the bound bit. Silently returning `limit`
        # rows would make a truncated pool indistinguishable from a complete one, and the
        # proposals computed from it would look authoritative while ignoring real interviewers.
        rows = (await self._session.execute(statement.limit(limit + 1))).scalars().all()
        if len(rows) > limit:
            _log.warning(
                "interviewer pool truncated limit=%d; scheduling considered only the first %d active "
                "interviewers by id. Proposals may omit valid panels.",
                limit,
                limit,
            )
            rows = rows[:limit]

        held: defaultdict[str, set[str]] = defaultdict(set)
        skill_rows = (
            await self._session.execute(
                select(InterviewerSkill).where(InterviewerSkill.interviewer_id.in_([row.id for row in rows]))
            )
        ).scalars()
        for skill in skill_rows:
            held[skill.interviewer_id].add(skill.skill_code)

        return tuple(
            InterviewerProfile(
                id=row.id,
                name=row.name,
                timezone=row.timezone,
                skills=frozenset(held[row.id]),
            )
            for row in rows
        )


class SqliteAvailabilityRepository(_Repository):
    """Reads over :class:`~talent_ops_mcp.db.schema.AvailabilityWindow`."""

    async def list_windows(
        self,
        *,
        owner_type: PartyType,
        owner_ids: Sequence[str],
        start_utc: datetime,
        end_utc: datetime,
    ) -> tuple[PartyInterval, ...]:
        """Free windows of these owners overlapping the range.

        The overlap test is strict on both sides -- ``start_utc < end`` and ``end_utc > start`` --
        so a window that merely touches the range at an instant is excluded. It contributes no
        schedulable time, and a zero-length interval is not representable downstream.
        """
        if not owner_ids:
            return ()
        rows = (
            (
                await self._session.execute(
                    select(AvailabilityWindow)
                    .where(
                        AvailabilityWindow.owner_type == owner_type,
                        AvailabilityWindow.owner_id.in_(owner_ids),
                        AvailabilityWindow.start_utc < end_utc,
                        AvailabilityWindow.end_utc > start_utc,
                    )
                    .order_by(AvailabilityWindow.owner_id, AvailabilityWindow.start_utc, AvailabilityWindow.id)
                )
            )
            .scalars()
            .all()
        )
        return tuple(
            PartyInterval(
                party_type=owner_type,
                party_id=row.owner_id,
                start_utc=row.start_utc,
                end_utc=row.end_utc,
            )
            for row in rows
        )


class SqliteInterviewRepository(_Repository):
    """Reads over :class:`~talent_ops_mcp.db.schema.Interview` from the attendee side."""

    async def list_busy(
        self,
        *,
        parties: Sequence[tuple[PartyType, str]],
        start_utc: datetime,
        end_utc: datetime,
    ) -> tuple[PartyInterval, ...]:
        """Scheduled interviews overlapping the range, one row per requested attendee.

        The ``(type, id)`` pair is filtered in Python after an id-only ``IN``. SQL row-value
        comparison (``(a, b) IN ((...), ...)``) would express it in one clause, but it needs
        SQLite 3.15+ and this project does not control which SQLite the interpreter was linked
        against. Filtering an already-tiny result set costs nothing and depends on no version.
        """
        if not parties:
            return ()
        wanted = set(parties)
        rows = (
            await self._session.execute(
                select(
                    InterviewAttendee.attendee_type,
                    InterviewAttendee.attendee_id,
                    Interview.start_utc,
                    Interview.end_utc,
                )
                .join(Interview, Interview.id == InterviewAttendee.interview_id)
                .where(
                    InterviewAttendee.attendee_id.in_({party_id for _, party_id in parties}),
                    Interview.status == InterviewStatus.SCHEDULED,
                    Interview.start_utc < end_utc,
                    Interview.end_utc > start_utc,
                )
                .order_by(InterviewAttendee.attendee_id, Interview.start_utc, Interview.id)
            )
        ).all()
        return tuple(
            PartyInterval(
                party_type=row.attendee_type,
                party_id=row.attendee_id,
                start_utc=row.start_utc,
                end_utc=row.end_utc,
            )
            for row in rows
            if (row.attendee_type, row.attendee_id) in wanted
        )
