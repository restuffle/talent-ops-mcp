"""The deterministic scheduling engine (§9, Phase 4).

**This is application logic, not model reasoning.** Every number it uses -- duration, panel size,
buffers, lead time, working-hours policy, required skills -- is read from the approved
:class:`~talent_ops_mcp.db.schema.InterviewPlan`. Nothing here is inferred, negotiated, or rounded
to make a slot fit. Given the same database and the same ``now``, it returns the same options in
the same order, which is what lets a test assert on them and a coordinator trust them.

``from __future__ import annotations`` is deliberately absent for the reason given in
:mod:`talent_ops_mcp.server`.

**Where the arithmetic happens.** All of it in UTC. Local time is used for exactly two things:
deciding whether a slot falls inside someone's working hours, and rendering the answer. Both are
UTC -> local, which is always single-valued -- so the engine never has to resolve a nonexistent or
ambiguous local time into an instant, and §9.3's daylight-saving rule is satisfied by construction
rather than by a special case. The one place a *local* input becomes an instant is the requested
date range; see :func:`_utc_bounds` for why the residual ambiguity there cannot change a result.

**What it does not do.** It ranks times, never people. Panel selection takes the first valid
combination in interviewer-id order: positional, reproducible, and carrying no judgement about
anyone (§2.3). It also reserves nothing -- §9.3 is explicit that an option is a proposal, and
Phase 5's confirmation re-runs every conflict check inside the write transaction.
"""

import logging
from collections import defaultdict
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from itertools import combinations
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy.ext.asyncio import AsyncSession

from talent_ops_mcp.db.schema import InterviewPlan
from talent_ops_mcp.errors import DomainError, ErrorCode
from talent_ops_mcp.models.domain import ApplicationStatus, PartyType, WorkingHoursPolicy
from talent_ops_mcp.models.outputs import (
    InterviewSlotOption,
    ProposedPanellist,
    SlotProposalResult,
    SlotUnavailableReason,
)
from talent_ops_mcp.repositories.protocols import (
    ApplicationRepository,
    AvailabilityRepository,
    CandidateRepository,
    InterviewerProfile,
    InterviewerRepository,
    InterviewPlanRepository,
    InterviewRepository,
    JobRepository,
    PartyInterval,
)
from talent_ops_mcp.repositories.sqlite import (
    SqliteApplicationRepository,
    SqliteAvailabilityRepository,
    SqliteCandidateRepository,
    SqliteInterviewerRepository,
    SqliteInterviewPlanRepository,
    SqliteInterviewRepository,
    SqliteJobRepository,
)
from talent_ops_mcp.settings import Settings

_log = logging.getLogger(__name__)

BUSINESS_DAY_START = time(9, 0)
BUSINESS_DAY_END = time(17, 0)
LAST_BUSINESS_WEEKDAY = 4
"""Friday. ``datetime.weekday()`` counts Monday as 0.

Business hours are a constant here, not configuration, and deliberately so: nobody has asked for
a second set, and the honest home for per-region or per-team hours is the approved plan -- which
already carries every other scheduling number (§8.1) -- rather than one global environment
variable that would be wrong for whichever office it was not set for. Phase 6 publishes these two
times through ``recruiting://policies/scheduling`` by importing them from here, so the policy a
human reads and the policy the engine applies stay the same object.

``WorkingHoursPolicy.EXTENDED_HOURS`` opts out of the window entirely rather than widening it:
under that policy declared availability *is* the policy, which is what an out-of-hours or
cross-hemisphere interview needs, and it keeps a second pair of magic numbers out of this module.
"""

DEFAULT_SEARCH_DAYS = 14
"""Days searched when the caller names no range. Clamped down by ``max_date_range_days``."""

DEFAULT_MAX_OPTIONS = 3
MAX_OPTIONS = 10
"""Ceiling on returned options. §9.2 step 12 requires a bounded maximum, and ten is past the point
where a human is choosing rather than being handed a wall of times."""

MAX_INTERVIEWER_POOL = 200
"""Active interviewers considered per request. The demo dataset has six.

ponytail: whole-pool scan. A real ATS with thousands of interviewers needs the skill filter pushed
into SQL and the pool narrowed by requirement before it reaches this module -- that is Phase 9-10
work, when a provider adapter exists to push it into. Truncation is logged by the repository
rather than swallowed, so an installation that outgrows this finds out.
"""

MAX_PANEL_COMBINATIONS = 5000
"""Combinations examined before giving up on one slot's panel.

Reached only when many interviewers are free at once and no low-id combination covers the plan.
Giving up is reported as :attr:`SlotUnavailableReason.PANEL_COVERAGE` and logged; it is never
reported as a valid option.
"""

_GRID_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
"""Anchor for slot alignment. Any fixed instant works; this one keeps the arithmetic readable.

A grid anchored in UTC is also aligned in local time everywhere, for every interval the ``5..60``
minute settings bound allows: real UTC offsets are whole minutes, and the ones that are not whole
hours (+05:30, +05:45, +09:30) are all multiples of fifteen.
"""


# --------------------------------------------------------------------------- interval algebra


@dataclass(frozen=True, slots=True, order=True)
class Interval:
    """A half-open span of UTC time, ``[start, end)``.

    Half-open is what makes back-to-back bookings work: an interview ending at 10:00 and one
    starting at 10:00 do not overlap, and a person free 09:00-10:00 and 10:00-11:00 is free for
    ninety minutes from 09:30. §12 Phase 4 lists "boundary-touching intervals" as a required test
    for exactly this reason.

    Zero-length is rejected at construction. It is never meaningful here -- no interview lasts no
    time, no availability of no length is schedulable -- and allowing it would make
    :meth:`overlaps` and :meth:`covers` disagree about a degenerate case that only ever arrives as
    a bug.
    """

    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        if self.end <= self.start:
            raise ValueError(f"interval end must be after start: {self.start.isoformat()}..{self.end.isoformat()}")

    def overlaps(self, other: "Interval") -> bool:
        """True when the two share at least one instant. Touching is not overlapping."""
        return self.start < other.end and other.start < self.end

    def covers(self, other: "Interval") -> bool:
        """True when ``other`` fits entirely inside this one. Shared endpoints are inside."""
        return self.start <= other.start and other.end <= self.end


def merge(intervals: Iterable[Interval]) -> tuple[Interval, ...]:
    """Sorted, non-overlapping, coalesced (§9.2 step 4).

    Touching intervals are joined: someone free 09:00-10:00 and 10:00-11:00 is free 09:00-11:00,
    and leaving those separate would hide every slot that straddles the seam.
    """
    coalesced: list[Interval] = []
    for span in sorted(intervals):
        if coalesced and span.start <= coalesced[-1].end:
            if span.end > coalesced[-1].end:
                coalesced[-1] = Interval(coalesced[-1].start, span.end)
        else:
            coalesced.append(span)
    return tuple(coalesced)


def subtract(spans: Iterable[Interval], blocks: Iterable[Interval]) -> tuple[Interval, ...]:
    """What is left of ``spans`` once ``blocks`` are removed (§9.2 step 5).

    A block landing in the middle of a span splits it in two, which is why this cannot be a
    filter. Both inputs are merged first, so the result is merged too.
    """
    remaining = list(merge(spans))
    for block in merge(blocks):
        survivors: list[Interval] = []
        for span in remaining:
            if not span.overlaps(block):
                survivors.append(span)
                continue
            if span.start < block.start:
                survivors.append(Interval(span.start, block.start))
            if block.end < span.end:
                survivors.append(Interval(block.end, span.end))
        remaining = survivors
    return tuple(remaining)


def clamp(spans: Iterable[Interval], window: Interval) -> tuple[Interval, ...]:
    """``spans`` intersected with ``window`` (§9.2 step 6). Spans reduced to nothing are dropped."""
    clipped: list[Interval] = []
    for span in spans:
        start, end = max(span.start, window.start), min(span.end, window.end)
        if start < end:
            clipped.append(Interval(start, end))
    return tuple(clipped)


def covered_by(spans: Sequence[Interval], probe: Interval) -> bool:
    """True when one span contains ``probe`` whole.

    ``spans`` must be merged. Unmerged, two adjacent spans that jointly cover ``probe`` would be
    reported as not covering it -- which is why every producer of a span list here merges.
    """
    return any(span.covers(probe) for span in spans)


def overlaps_any(spans: Iterable[Interval], probe: Interval) -> bool:
    """True when any span shares an instant with ``probe``."""
    return any(span.overlaps(probe) for span in spans)


def align_up(moment: datetime, grid: timedelta) -> datetime:
    """The first grid point at or after ``moment`` (§9.2 step 7).

    Integer arithmetic on ``timedelta`` throughout: dividing as a float would round, and a slot
    half a microsecond off the grid is a slot that fails an equality assertion for reasons nobody
    enjoys finding.
    """
    steps, remainder = divmod(moment - _GRID_EPOCH, grid)
    return moment if not remainder else _GRID_EPOCH + (steps + 1) * grid


def aligned_starts(span: Interval, duration: timedelta, grid: timedelta) -> Iterator[datetime]:
    """Every grid-aligned start inside ``span`` that leaves room for ``duration``."""
    start = align_up(span.start, grid)
    while start + duration <= span.end:
        yield start
        start += grid


def within_business_hours(slot: Interval, zone: ZoneInfo) -> bool:
    """True when ``slot`` falls inside one weekday's business hours in ``zone`` (§9.2 step 8).

    The whole interview must fit, so a 16:30 start on a 60-minute plan fails against a 17:00 close
    rather than running over. An interview crossing local midnight fails the same test, because
    its local end lands on the next date and is therefore later than that date's close.

    The comparison is between aware datetimes, so it is decided on instants rather than on
    wall-clock strings, and a daylight-saving shift inside the slot cannot smuggle an extra hour
    past the close. On a spring-forward day this correctly shortens the bookable window: 09:00 and
    17:00 local are seven hours apart that day, not eight.
    """
    local_start = slot.start.astimezone(zone)
    if local_start.weekday() > LAST_BUSINESS_WEEKDAY:
        return False
    opens = local_start.replace(hour=BUSINESS_DAY_START.hour, minute=BUSINESS_DAY_START.minute, second=0, microsecond=0)
    closes = local_start.replace(hour=BUSINESS_DAY_END.hour, minute=BUSINESS_DAY_END.minute, second=0, microsecond=0)
    return opens <= local_start and slot.end.astimezone(zone) <= closes


# --------------------------------------------------------------------------- panel selection


def covers_requirements(panel: Sequence[InterviewerProfile], requirements: Sequence[tuple[str, int]]) -> bool:
    """True when ``panel`` satisfies every skill requirement at once (§9.2 step 9).

    One panellist may satisfy several requirements -- somebody holding both ``python`` and
    ``system_design`` counts toward both. The plan asks for coverage, not for one specialist per
    skill, and a stricter reading would make plans unstaffable that a recruiter approved as
    satisfiable.
    """
    return all(sum(1 for member in panel if code in member.skills) >= minimum for code, minimum in requirements)


def select_panel(
    available: Sequence[InterviewerProfile],
    size: int,
    requirements: Sequence[tuple[str, int]],
) -> tuple[InterviewerProfile, ...] | None:
    """The first valid panel in interviewer-id order, or ``None`` if there is none.

    "First in id order" is the whole selection rule, and it is chosen because it is *not* a
    judgement: §2.3 forbids this server ranking people, so the tiebreak has to be something
    arbitrary and reproducible rather than something a reader could mistake for merit. It also
    gives §9.2 step 10 for free -- one panel per slot means no two returned options can differ
    only in who is on them.
    """
    if len(available) < size:
        return None
    ordered = sorted(available, key=lambda member: member.id)
    for examined, panel in enumerate(combinations(ordered, size)):
        if examined >= MAX_PANEL_COMBINATIONS:
            # ponytail: brute force over combinations, capped. Reached only with a large free pool
            # and a plan no low-id combination satisfies; the upgrade is to match skills to seats
            # (bipartite matching) instead of enumerating. Reported as PANEL_COVERAGE and never as
            # a valid option, so giving up cannot turn into a wrong answer.
            _log.warning(
                "panel search gave up free=%d panel_size=%d after %d combinations",
                len(available),
                size,
                MAX_PANEL_COMBINATIONS,
            )
            return None
        if covers_requirements(panel, requirements):
            return panel
    return None


# --------------------------------------------------------------------------- helpers


def _zone(name: str) -> ZoneInfo:
    """Resolve a caller-supplied IANA zone, or refuse the request.

    Only the display timezone goes through this. Zones stored on a candidate, interviewer, or job
    were validated when written (:func:`~talent_ops_mcp.db.schema.validated_timezone`), so one
    failing to resolve is a broken installation -- a missing tz database -- and belongs in the
    stderr log as an internal error, not in a message telling the caller to fix their input.
    """
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise DomainError(
            ErrorCode.VALIDATION_ERROR,
            f"{name!r} is not an IANA timezone name. Use a region/city name such as "
            "America/New_York, or omit the parameter to use the candidate's own timezone.",
        ) from exc


def _utc_bounds(first: date, last: date, zone: ZoneInfo) -> Interval:
    """The requested local date range as a half-open UTC interval.

    This is the one place a local wall-clock time becomes an instant, and it is resolved
    explicitly rather than left to chance: both bounds are local midnight with ``fold=0``. A zone
    whose daylight-saving transition happens *at* midnight can therefore move a bound by up to an
    hour. That cannot change a result -- every returned slot is filtered by working hours or by
    declared availability, neither of which reaches midnight -- and the alternative, refusing the
    range outright, would make the tool unusable in those zones on the two days a year it applies.
    """
    return Interval(
        datetime.combine(first, time.min, tzinfo=zone).astimezone(UTC),
        datetime.combine(last + timedelta(days=1), time.min, tzinfo=zone).astimezone(UTC),
    )


def _by_party(rows: Iterable[PartyInterval]) -> dict[tuple[PartyType, str], tuple[Interval, ...]]:
    """Group repository rows into merged intervals per person.

    Keyed by ``(type, id)`` rather than by id alone, so a candidate and an interviewer who happen
    to share an identifier cannot inherit each other's calendar.
    """
    grouped: defaultdict[tuple[PartyType, str], list[Interval]] = defaultdict(list)
    for row in rows:
        grouped[(row.party_type, row.party_id)].append(Interval(row.start_utc, row.end_utc))
    return {party: merge(intervals) for party, intervals in grouped.items()}


def _hours_phrase(policy: WorkingHoursPolicy) -> str:
    """How the working-hours rule reads to a human, for the validity explanation."""
    window = f"{BUSINESS_DAY_START:%H:%M}-{BUSINESS_DAY_END:%H:%M} Monday to Friday"
    return {
        WorkingHoursPolicy.CANDIDATE_BUSINESS_HOURS: f"{window} in the candidate's own timezone",
        WorkingHoursPolicy.INTERVIEWER_BUSINESS_HOURS: f"{window} in each panellist's own timezone",
        WorkingHoursPolicy.BOTH_BUSINESS_HOURS: (
            f"{window} for the candidate and for every panellist, each in their own timezone"
        ),
        WorkingHoursPolicy.EXTENDED_HOURS: (
            "any hour every attendee has declared free -- this plan permits extended hours"
        ),
    }[policy]


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def _skills_phrase(requirements: Sequence[tuple[str, int]]) -> str:
    if not requirements:
        return "no specific skills"
    return ", ".join(f"{code} (at least {minimum})" for code, minimum in sorted(requirements))


def _why_valid(plan: InterviewPlan, candidate_name: str) -> str:
    """What every returned option was checked against, in words a coordinator can repeat.

    One sentence for the whole result rather than one per option: every option passed exactly the
    same checks, so repeating it per row would cost tokens to say nothing new.

    It describes *rules*, never anyone's diary. §8.2 keeps other people's calendars private, and
    "the panel is free" is the most this may say -- not who else was considered, not what they
    were doing instead, not when they were busy.
    """
    buffers = (
        f", keeps {plan.buffer_before_minutes} minutes before and {plan.buffer_after_minutes} after clear for the panel"
        if plan.buffer_before_minutes or plan.buffer_after_minutes
        else ""
    )
    lead = f", starts at least {_plural(plan.minimum_lead_hours, 'hour')} from now" if plan.minimum_lead_hours else ""
    return (
        f"Every option gives {candidate_name} and all {_plural(plan.panel_size, 'panellist')} the full "
        f"{plan.duration_minutes} minutes{buffers}{lead}, falls inside "
        f"{_hours_phrase(plan.working_hours_policy)}, and clashes with no interview any attendee "
        "already has booked. None of it is reserved -- an option is a proposal until a human "
        "confirms one, and another booking can still take the time."
    )


def _no_options_reason(
    code: SlotUnavailableReason,
    *,
    plan: InterviewPlan,
    first: date,
    last: date,
    requirements: Sequence[tuple[str, int]],
) -> str:
    """Why nothing could be proposed, and the one thing to change next.

    §7.1.1 requires a failure to name the caller's next step, and "no slots available" does not:
    it leaves a model choosing between widening the dates, chasing the candidate, and telling a
    recruiter the panel cannot be staffed, and it will usually pick by guessing.
    """
    window = f"between {first.isoformat()} and {last.isoformat()}"
    panel = _plural(plan.panel_size, "interviewer")
    if code is SlotUnavailableReason.NO_ELIGIBLE_INTERVIEWERS:
        return (
            f"No panel of {panel} drawn from the active interviewers can cover this plan's required "
            f"skills -- {_skills_phrase(requirements)} -- on any date. Widening the dates will not "
            "help. Ask a human recruiter to add a qualified interviewer or to revise the approved "
            "plan; do not propose a smaller or differently skilled panel."
        )
    if code is SlotUnavailableReason.OUTSIDE_LEAD_TIME:
        return (
            f"This plan requires {_plural(plan.minimum_lead_hours, 'hour')} of notice, which lands "
            f"after the searched window ends on {last.isoformat()}. Search a later range."
        )
    if code is SlotUnavailableReason.NO_CANDIDATE_AVAILABILITY:
        return (
            f"The candidate has declared no free block of at least {plan.duration_minutes} minutes "
            f"{window}. Ask them for more availability, or search a wider range."
        )
    if code is SlotUnavailableReason.NO_INTERVIEWER_AVAILABILITY:
        return (
            f"No active interviewer has declared any availability {window}. Search a wider range, or "
            "ask a recruiter to have the panel publish their times."
        )
    if code is SlotUnavailableReason.ATTENDEE_CONFLICT:
        return (
            f"Every time the candidate is free {window} collides with an interview they are already "
            "booked for. Search a wider range, or reschedule the existing booking first."
        )
    if code is SlotUnavailableReason.OUTSIDE_WORKING_HOURS:
        return (
            f"The candidate's free time {window} falls outside the hours this plan allows "
            f"({_hours_phrase(plan.working_hours_policy)}). Ask for availability inside those hours, "
            "or ask a recruiter whether a plan permitting extended hours applies."
        )
    if code is SlotUnavailableReason.NO_AVAILABLE_PANEL:
        return (
            f"Fewer than {panel} were free at any single time the candidate is available {window}. "
            "Search a wider range, or ask a recruiter to widen the eligible panel."
        )
    return (
        f"Interviewers were free {window}, but never {panel} whose skills together cover "
        f"{_skills_phrase(requirements)} at the same time. Search a wider range, or ask a recruiter "
        "whether another qualified interviewer can be added."
    )


@dataclass(frozen=True, slots=True)
class _Panellist:
    """One pool member with their zone resolved and their free time already computed."""

    profile: InterviewerProfile
    zone: ZoneInfo
    free: tuple[Interval, ...]


@dataclass(frozen=True, slots=True)
class _Attempt:
    """How far each slot got, so an empty answer can say which constraint actually bit.

    Counted during the single scan rather than reconstructed by re-running with constraints
    relaxed. Five integers cost nothing; a second pass would double the work and could disagree
    with the first.
    """

    slots: int = 0
    within_hours: int = 0
    candidate_free: int = 0
    panel_available: int = 0


# --------------------------------------------------------------------------- the service


@dataclass(frozen=True, slots=True)
class SchedulingService:
    """Computes interview options. Reads only -- nothing in this class writes or reserves.

    Repositories are typed as protocols for the reason given in
    :mod:`talent_ops_mcp.services.recruiting`: mypy checks each SQLite implementation structurally
    at :func:`sqlite_scheduling_service`, so a signature drifting between the contract and the
    implementation fails the type gate rather than Phase 9-10.
    """

    applications: ApplicationRepository
    candidates: CandidateRepository
    jobs: JobRepository
    plans: InterviewPlanRepository
    interviewers: InterviewerRepository
    availability: AvailabilityRepository
    interviews: InterviewRepository
    slot_interval_minutes: int
    max_date_range_days: int

    async def propose_interview_slots(
        self,
        *,
        application_id: str,
        now: datetime,
        start_date: date | None = None,
        end_date: date | None = None,
        display_timezone: str | None = None,
        max_options: int = DEFAULT_MAX_OPTIONS,
    ) -> SlotProposalResult:
        """Valid, conflict-free times for one application's current stage (§9.2).

        ``now`` is a parameter rather than a call to the clock. The whole point of this engine is
        that its output is a function of its inputs, and a hidden read of the system clock would
        make "the same database gives the same answer" untestable.

        Returns an empty option list with a reason rather than raising when no time works: no
        availability is an answer about the world, and a caller that gets an error instead has to
        guess whether it asked wrongly or the diary is simply full. Errors are reserved for
        requests that cannot be answered at all -- an unknown application, one that may not be
        scheduled, a stage with no approved plan, a range that breaks a configured bound.
        """
        if now.tzinfo is None:
            raise ValueError("now must be timezone-aware; a naive instant cannot be compared against stored UTC")

        application = await self.applications.get(application_id)
        if application is None:
            raise DomainError(
                ErrorCode.NOT_FOUND,
                f"no application with id {application_id!r}. Application ids come from "
                "talent_ops_list_applications; they cannot be guessed or constructed.",
            )
        if application.status != ApplicationStatus.ACTIVE:
            raise DomainError(
                ErrorCode.CONFLICT,
                f"application {application_id!r} is {application.status.value!r}, and only active "
                "applications may be scheduled against. Do not propose times for it; ask a human "
                "recruiter whether it should be reactivated first.",
            )

        candidate = await self.candidates.get(application.candidate_id)
        job = await self.jobs.get(application.job_id)
        if candidate is None or job is None:  # pragma: no cover - foreign keys make this unreachable
            raise DomainError(
                ErrorCode.NOT_FOUND,
                f"application {application_id!r} points at a candidate or job that no longer exists.",
            )

        active = await self.plans.get_active(job.id, application.current_stage)
        if active is None:
            raise DomainError(
                ErrorCode.NOT_FOUND,
                f"job {job.id!r} has no approved interview plan for the "
                f"{application.current_stage.value!r} stage. Do not schedule against this stage and "
                "do not substitute a duration, panel size, or lead time of your own; ask a human "
                "recruiter to approve a plan first.",
            )
        plan, requirements = active.plan, active.requirements
        required_skills = tuple((row.skill_code, row.minimum_interviewers) for row in requirements)

        zone_name = display_timezone or candidate.timezone
        zone = _zone(zone_name)
        first, last = self._resolve_dates(start_date, end_date, now.astimezone(zone).date())
        requested = _utc_bounds(first, last, zone)
        earliest = max(requested.start, now + timedelta(hours=plan.minimum_lead_hours))

        candidate_name = f"{candidate.first_name} {candidate.last_name}"

        def answer(
            options: Sequence[tuple[Interval, tuple[InterviewerProfile, ...]]] = (),
            *,
            more: bool = False,
            code: SlotUnavailableReason | None = None,
        ) -> SlotProposalResult:
            """Assemble the one output model, from wherever the search decided to stop.

            A closure rather than a method: every exit shares the same dozen pieces of
            already-resolved context, and threading those through a signature would make each
            early return another place to get one of them wrong.
            """
            return SlotProposalResult(
                application_id=application_id,
                candidate_id=candidate.id,
                candidate_name=candidate_name,
                job_id=job.id,
                job_title=job.title,
                stage=application.current_stage,
                interview_plan_id=plan.id,
                duration_minutes=plan.duration_minutes,
                panel_size=plan.panel_size,
                display_timezone=zone_name,
                searched_from_utc=earliest,
                searched_to_utc=requested.end,
                options=[
                    InterviewSlotOption(
                        start_utc=slot.start,
                        end_utc=slot.end,
                        start_local=slot.start.astimezone(zone),
                        end_local=slot.end.astimezone(zone),
                        display_timezone=zone_name,
                        duration_minutes=plan.duration_minutes,
                        panel=[
                            ProposedPanellist(id=member.id, name=member.name, timezone=member.timezone)
                            for member in panel
                        ],
                    )
                    for slot, panel in options
                ],
                more_options_exist=more,
                why_these_are_valid=None if code is not None else _why_valid(plan, candidate_name),
                no_options_reason=(
                    None
                    if code is None
                    else _no_options_reason(code, plan=plan, first=first, last=last, requirements=required_skills)
                ),
                no_options_code=code,
            )

        if earliest >= requested.end:
            return answer(code=SlotUnavailableReason.OUTSIDE_LEAD_TIME)
        search = Interval(earliest, requested.end)

        pool = await self.interviewers.list_active_with_skills(limit=MAX_INTERVIEWER_POOL)
        if select_panel(pool, plan.panel_size, required_skills) is None:
            return answer(code=SlotUnavailableReason.NO_ELIGIBLE_INTERVIEWERS)

        # Buffers can push the reserved span past the requested range at either end, so the
        # availability and conflict reads are widened to match. Fetching only the range itself
        # would make a panellist look free during a commitment that starts one minute after it.
        padding = timedelta(minutes=plan.buffer_before_minutes + plan.buffer_after_minutes)

        candidate_windows = await self.availability.list_windows(
            owner_type=PartyType.CANDIDATE,
            owner_ids=(candidate.id,),
            start_utc=requested.start,
            end_utc=requested.end,
        )
        declared = _by_party(candidate_windows).get((PartyType.CANDIDATE, candidate.id), ())
        if not declared:
            return answer(code=SlotUnavailableReason.NO_CANDIDATE_AVAILABILITY)
        candidate_spans = clamp(declared, search)
        if not candidate_spans:
            # The candidate declared time inside the requested range, but all of it is too soon.
            return answer(code=SlotUnavailableReason.OUTSIDE_LEAD_TIME)

        pool_ids = [member.id for member in pool]
        interviewer_windows = await self.availability.list_windows(
            owner_type=PartyType.INTERVIEWER,
            owner_ids=pool_ids,
            start_utc=search.start - padding,
            end_utc=search.end + padding,
        )
        if not interviewer_windows:
            return answer(code=SlotUnavailableReason.NO_INTERVIEWER_AVAILABILITY)

        commitments = _by_party(
            await self.interviews.list_busy(
                parties=[(PartyType.CANDIDATE, candidate.id), *((PartyType.INTERVIEWER, i) for i in pool_ids)],
                start_utc=search.start - padding,
                end_utc=search.end + padding,
            )
        )
        free_windows = _by_party(interviewer_windows)
        panellists = tuple(
            _Panellist(
                profile=member,
                zone=ZoneInfo(member.timezone),
                free=subtract(
                    free_windows.get((PartyType.INTERVIEWER, member.id), ()),
                    commitments.get((PartyType.INTERVIEWER, member.id), ()),
                ),
            )
            for member in pool
        )

        options, attempt = self._scan(
            candidate_spans=candidate_spans,
            candidate_zone=ZoneInfo(candidate.timezone),
            candidate_busy=commitments.get((PartyType.CANDIDATE, candidate.id), ()),
            panellists=panellists,
            plan_duration=timedelta(minutes=plan.duration_minutes),
            buffer_before=timedelta(minutes=plan.buffer_before_minutes),
            buffer_after=timedelta(minutes=plan.buffer_after_minutes),
            policy=plan.working_hours_policy,
            panel_size=plan.panel_size,
            required_skills=required_skills,
            # One past the maximum, purely so `more_options_exist` can be answered honestly
            # without scanning the rest of the range.
            wanted=max_options + 1,
        )
        if not options:
            return answer(code=self._diagnose(attempt))
        return answer(options[:max_options], more=len(options) > max_options)

    def _resolve_dates(self, start_date: date | None, end_date: date | None, today: date) -> tuple[date, date]:
        """Apply the defaults and the configured range bound.

        An over-long range is refused rather than silently narrowed. Clamping is right for
        ``limit`` -- a short page is still a true answer with ``has_more`` to prove it -- but a
        silently shortened date range would return "no times available" for a fortnight the caller
        believes it searched, which is a wrong answer wearing the clothes of a right one.
        """
        first = start_date or today
        span = min(DEFAULT_SEARCH_DAYS, self.max_date_range_days)
        last = end_date or first + timedelta(days=span - 1)
        if last < first:
            raise DomainError(
                ErrorCode.VALIDATION_ERROR,
                f"end_date {last.isoformat()} is before start_date {first.isoformat()}.",
            )
        days = (last - first).days + 1
        if days > self.max_date_range_days:
            raise DomainError(
                ErrorCode.VALIDATION_ERROR,
                f"the requested range covers {days} days; this server allows at most "
                f"{self.max_date_range_days}. Ask for a shorter range, then search the next one "
                "if nothing suits.",
            )
        return first, last

    def _scan(
        self,
        *,
        candidate_spans: Sequence[Interval],
        candidate_zone: ZoneInfo,
        candidate_busy: Sequence[Interval],
        panellists: Sequence[_Panellist],
        plan_duration: timedelta,
        buffer_before: timedelta,
        buffer_after: timedelta,
        policy: WorkingHoursPolicy,
        panel_size: int,
        required_skills: Sequence[tuple[str, int]],
        wanted: int,
    ) -> tuple[list[tuple[Interval, tuple[InterviewerProfile, ...]]], _Attempt]:
        """Walk the grid over the candidate's free time and keep what survives every rule.

        Slots are generated *from* the candidate's merged free spans rather than from the whole
        range, so §9.2 step 6's intersection is structural: a start that exists here is already
        one the candidate declared, already after the lead-time cutoff, and already inside the
        requested range, because those bounds were applied to the spans.

        Buffers are asymmetric on purpose, and this is the only place it shows. §9.3 requires each
        *interviewer* to be free "for the full interval plus required buffers" -- their preparation
        and write-up time -- so a panellist's availability must cover the padded span. The
        candidate is asked only for the interview itself: somebody who says they are free from
        15:00 means they can start at 15:00, and quietly requiring a quarter-hour of their evening
        would refuse a slot they offered. Existing commitments block the padded span for everyone,
        because nobody can be in two places at once.
        """
        grid = timedelta(minutes=self.slot_interval_minutes)
        check_candidate_hours = policy in (
            WorkingHoursPolicy.CANDIDATE_BUSINESS_HOURS,
            WorkingHoursPolicy.BOTH_BUSINESS_HOURS,
        )
        check_interviewer_hours = policy in (
            WorkingHoursPolicy.INTERVIEWER_BUSINESS_HOURS,
            WorkingHoursPolicy.BOTH_BUSINESS_HOURS,
        )

        options: list[tuple[Interval, tuple[InterviewerProfile, ...]]] = []
        # Free sets repeat across neighbouring slots, and the same set always yields the same
        # panel, so the combination search runs once per distinct set rather than once per slot.
        chosen: dict[tuple[str, ...], tuple[InterviewerProfile, ...] | None] = {}
        slots = within_hours = candidate_free = panel_available = 0

        for span in candidate_spans:
            for start in aligned_starts(span, plan_duration, grid):
                slots += 1
                slot = Interval(start, start + plan_duration)
                if check_candidate_hours and not within_business_hours(slot, candidate_zone):
                    continue
                within_hours += 1

                reserved = Interval(slot.start - buffer_before, slot.end + buffer_after)
                if overlaps_any(candidate_busy, reserved):
                    continue
                candidate_free += 1

                free = [
                    member.profile
                    for member in panellists
                    if covered_by(member.free, reserved)
                    and (not check_interviewer_hours or within_business_hours(slot, member.zone))
                ]
                if len(free) < panel_size:
                    continue
                panel_available += 1

                key = tuple(member.id for member in free)
                if key not in chosen:
                    chosen[key] = select_panel(free, panel_size, required_skills)
                panel = chosen[key]
                if panel is None:
                    continue

                options.append((slot, panel))
                if len(options) >= wanted:
                    return options, _Attempt(slots, within_hours, candidate_free, panel_available)

        return options, _Attempt(slots, within_hours, candidate_free, panel_available)

    @staticmethod
    def _diagnose(attempt: _Attempt) -> SlotUnavailableReason:
        """The first constraint that eliminated every slot.

        Ordered the same way the scan applies its filters, so the reason names the earliest rule
        that emptied the field rather than the last one to be checked. Reporting the last would
        blame the panel for a candidate who is never free.
        """
        if attempt.slots == 0:
            return SlotUnavailableReason.NO_CANDIDATE_AVAILABILITY
        if attempt.within_hours == 0:
            return SlotUnavailableReason.OUTSIDE_WORKING_HOURS
        if attempt.candidate_free == 0:
            return SlotUnavailableReason.ATTENDEE_CONFLICT
        if attempt.panel_available == 0:
            return SlotUnavailableReason.NO_AVAILABLE_PANEL
        return SlotUnavailableReason.PANEL_COVERAGE


def sqlite_scheduling_service(session: AsyncSession, settings: Settings) -> SchedulingService:
    """Wire the engine to the SQLite readers over ``session``.

    The only place in this module that names a concrete repository, so an ATS-backed sibling
    (Phases 9-10) drops in beside it without :class:`SchedulingService` changing.
    """
    return SchedulingService(
        applications=SqliteApplicationRepository(session, settings.max_page_size),
        candidates=SqliteCandidateRepository(session, settings.max_page_size),
        jobs=SqliteJobRepository(session, settings.max_page_size),
        plans=SqliteInterviewPlanRepository(session, settings.max_page_size),
        interviewers=SqliteInterviewerRepository(session, settings.max_page_size),
        availability=SqliteAvailabilityRepository(session, settings.max_page_size),
        interviews=SqliteInterviewRepository(session, settings.max_page_size),
        slot_interval_minutes=settings.slot_interval_minutes,
        max_date_range_days=settings.max_date_range_days,
    )
