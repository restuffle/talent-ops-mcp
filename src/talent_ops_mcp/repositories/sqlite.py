"""SQLite implementations of the repository protocols.

Every query here is read-only and every list is bounded. Writes belong to the services that own
a transaction (§10.2), so nothing in this module commits.

Ordering is always explicit and always total -- a deterministic tiebreak on the primary key
follows every sort key. Without it SQLite may return rows in any order for equal keys, and
"page 2" could repeat or skip a row that page 1 already showed.
"""

from collections.abc import Sequence
from typing import Any, TypeVar

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from talent_ops_mcp.db.base import Base
from talent_ops_mcp.db.schema import Application, Candidate, Interviewer, InterviewerSkill, Job
from talent_ops_mcp.errors import DomainError, ErrorCode
from talent_ops_mcp.models.domain import ApplicationStatus, JobStatus, Stage
from talent_ops_mcp.repositories.protocols import Page

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

    async def _paginate(
        self,
        statement: Select[tuple[_ModelT]],
        *,
        order_by: Sequence[Any],
        limit: int,
        offset: int,
    ) -> Page[_ModelT]:
        """Run ``statement`` twice: once counted, once windowed.

        Counting via a subquery of the filtered statement rather than rebuilding the filters
        keeps the two in step -- a filter added to one and forgotten in the other would produce a
        ``total`` that disagrees with ``items`` and a ``has_more`` that lies.
        """
        effective = self._effective_limit(limit)
        offset = self._check_offset(offset)

        total = int(
            (
                await self._session.execute(select(func.count()).select_from(statement.order_by(None).subquery()))
            ).scalar_one()
        )
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
        status: JobStatus | None = None,
        department: str | None = None,
        limit: int = 25,
        offset: int = 0,
    ) -> Page[Job]:
        statement = select(Job)
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


class SqliteApplicationRepository(_Repository):
    """Reads over :class:`~talent_ops_mcp.db.schema.Application`."""

    async def get(self, application_id: str) -> Application | None:
        return await self._get(Application, application_id)

    async def list(
        self,
        *,
        job_id: str | None = None,
        stage: Stage | None = None,
        status: ApplicationStatus | None = None,
        limit: int = 25,
        offset: int = 0,
    ) -> Page[Application]:
        statement = select(Application)
        if job_id is not None:
            statement = statement.where(Application.job_id == job_id)
        if stage is not None:
            statement = statement.where(Application.current_stage == stage)
        if status is not None:
            statement = statement.where(Application.status == status)
        return await self._paginate(
            statement,
            order_by=(Application.last_activity_at.desc(), Application.id),
            limit=limit,
            offset=offset,
        )


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
