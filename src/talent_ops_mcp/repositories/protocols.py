"""What the service layer may ask of storage.

These are :class:`typing.Protocol` classes, so an implementation conforms by shape and never
imports this module. That keeps the SQLite reader, a future ATS-backed reader (Phases 9-10), and
a hand-written test fake independent of one another.

Every list operation is paginated. §8.2 requires candidate searches to be bounded, and the same
bound is applied to every collection: an unbounded list is a tool that returns three rows in
development and thirty thousand in front of a model with a context limit.
"""

from dataclasses import dataclass
from typing import Generic, Protocol, TypeVar

from talent_ops_mcp.db.schema import Application, Candidate, Interviewer, Job
from talent_ops_mcp.models.domain import ApplicationStatus, JobStatus, Stage

# PEP 695 (`class Page[T]`) would read better but lands in 3.12; this project pins 3.11.
T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class Page(Generic[T]):
    """One bounded slice of a result set.

    ``total`` is the count matching the filters, not the length of ``items`` -- the caller needs
    it to tell "no more results" apart from "the page happened to fill exactly".

    ``limit`` reports the *effective* limit, which may be lower than the one requested; see
    :meth:`Paginated.list` implementations for the clamping rule.
    """

    items: tuple[T, ...]
    total: int
    limit: int
    offset: int

    @property
    def has_more(self) -> bool:
        return self.offset + len(self.items) < self.total


class Paginated(Protocol):
    """Marker for the shared pagination contract, documented once.

    Implementations clamp an oversized ``limit`` to the configured maximum rather than raising,
    and report the clamped value on the returned :class:`Page`. A caller that asks for more than
    it may have still gets a useful answer plus an honest ``has_more``, instead of an error it
    must interpret and retry. A ``limit`` below 1 is a caller bug and does raise.
    """


class JobRepository(Paginated, Protocol):
    """Reads over requisitions."""

    async def get(self, job_id: str) -> Job | None: ...

    async def list(
        self,
        *,
        status: JobStatus | None = None,
        department: str | None = None,
        limit: int = 25,
        offset: int = 0,
    ) -> Page[Job]: ...


class CandidateRepository(Paginated, Protocol):
    """Reads over candidates.

    Note what is absent: no ordering by anything evaluative, and no filter that could implement
    a shortlist. §2.3 restricts this server to logistics, and a repository that cannot express
    ranking cannot be talked into it later.
    """

    async def get(self, candidate_id: str) -> Candidate | None: ...

    async def search(
        self,
        *,
        query: str | None = None,
        limit: int = 25,
        offset: int = 0,
    ) -> Page[Candidate]: ...


class ApplicationRepository(Paginated, Protocol):
    """Reads over applications, the join between a candidate and a job."""

    async def get(self, application_id: str) -> Application | None: ...

    async def list(
        self,
        *,
        job_id: str | None = None,
        stage: Stage | None = None,
        status: ApplicationStatus | None = None,
        limit: int = 25,
        offset: int = 0,
    ) -> Page[Application]: ...


class InterviewerRepository(Paginated, Protocol):
    """Reads over interviewers and their skills.

    ``skills`` filters conjunctively -- an interviewer must hold *every* code given. §9.2 step 3
    selects panellists "capable of satisfying all skill requirements", and an ``ANY`` match would
    quietly propose a panel that cannot cover the plan.
    """

    async def get(self, interviewer_id: str) -> Interviewer | None: ...

    async def list(
        self,
        *,
        active: bool | None = None,
        skills: tuple[str, ...] = (),
        limit: int = 25,
        offset: int = 0,
    ) -> Page[Interviewer]: ...
