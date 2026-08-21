"""The scheduling engine end to end, against real SQLite (§9, Phase 4).

Two kinds of test here, and the split is deliberate:

* **Built scenarios.** :func:`scenario` writes the smallest world that isolates one rule -- one
  candidate window, one interviewer, one booking -- so a failure names the rule rather than the
  dataset. The demo seed is realistic, which makes it a poor instrument: a slot missing from it
  could be any of six constraints.
* **The seeded gate.** The last section runs the completion gate of §12 Phase 4 against the real
  demo data, because "works on a fixture built for it" is not the claim that phase makes.

``now`` is passed explicitly everywhere. The engine takes the clock as a parameter precisely so
these assertions can be exact instants rather than "roughly a fortnight from whenever the suite
ran".
"""

from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from typing import NamedTuple
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

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
from talent_ops_mcp.db.seed import SEED_ANCHOR
from talent_ops_mcp.errors import DomainError, ErrorCode
from talent_ops_mcp.models.domain import (
    ApplicationStatus,
    AvailabilitySource,
    InterviewStatus,
    InterviewType,
    JobStatus,
    PartyType,
    Stage,
    WorkingHoursPolicy,
)
from talent_ops_mcp.models.outputs import SlotProposalResult, SlotUnavailableReason
from talent_ops_mcp.services.scheduling import SchedulingService, sqlite_scheduling_service
from talent_ops_mcp.settings import Settings

APPLICATION = "app_under_test"
NOW = datetime(2026, 9, 1, 9, 0, tzinfo=UTC)
"""Tuesday 2026-09-01. Built scenarios put their availability in the following week, so nothing is
accidentally in the past and no test depends on the day it is run."""

WEEK = date(2026, 9, 7)
"""Monday. ``DAY[n]`` below is ``WEEK + n``, matching the seed's ``day=0..4`` convention."""

DAY = [WEEK + timedelta(days=offset) for offset in range(7)]


def at(day: int, hour: int, minute: int = 0) -> datetime:
    """A UTC instant in the test week, by day offset from Monday 2026-09-07."""
    return datetime(2026, 9, 7 + day, hour, minute, tzinfo=UTC)


class Panellist(NamedTuple):
    """A pool member for :func:`scenario`, with the defaults most tests want.

    Named rather than a bare 4-tuple for the reason ``db/seed.py`` gives: ``("int_a", "UTC", (),
    False)`` is four anonymous positions, and a transposed pair of them is invisible in review.
    """

    ident: str
    timezone: str = "America/New_York"
    skills: tuple[str, ...] = ()
    active: bool = True


ONE_INTERVIEWER = (Panellist("int_one"),)


async def scenario(
    session: AsyncSession,
    *,
    duration_minutes: int = 60,
    panel_size: int = 1,
    minimum_lead_hours: int = 0,
    buffer_before: int = 0,
    buffer_after: int = 0,
    policy: WorkingHoursPolicy = WorkingHoursPolicy.EXTENDED_HOURS,
    required_skills: Sequence[tuple[str, int]] = (),
    candidate_timezone: str = "America/New_York",
    candidate_windows: Sequence[tuple[datetime, datetime]] = (),
    interviewers: Sequence[tuple[str, str, tuple[str, ...], bool]] = ONE_INTERVIEWER,
    interviewer_windows: Mapping[str, Sequence[tuple[datetime, datetime]]] | None = None,
    bookings: Sequence[tuple[Sequence[tuple[PartyType, str]], datetime, datetime, InterviewStatus]] = (),
    application_status: ApplicationStatus = ApplicationStatus.ACTIVE,
    plan_active: bool = True,
) -> None:
    """Write one minimal world: a job, a plan, a candidate, an application, and a panel pool.

    Defaults are chosen so that a test states only what it is about. ``EXTENDED_HOURS`` is the
    default policy for that reason -- most rules under test have nothing to do with business
    hours, and a 09:00-17:00 filter running underneath every one of them would turn unrelated
    failures into timezone puzzles.
    """
    session.add(
        Job(
            id="job_under_test",
            title="Test Role",
            department="Engineering",
            location="Remote",
            default_timezone="America/New_York",
            status=JobStatus.OPEN,
        )
    )
    session.add(
        Candidate(
            id="cand_under_test",
            first_name="Sam",
            last_name="Fixture",
            email="sam.fixture@talent-ops.test",
            timezone=candidate_timezone,
        )
    )
    await session.flush()

    session.add(
        Application(
            id=APPLICATION,
            candidate_id="cand_under_test",
            job_id="job_under_test",
            current_stage=Stage.TECHNICAL_SCREEN,
            status=application_status,
            coordinator_id="coord_test",
            last_activity_at=NOW,
        )
    )
    session.add(
        InterviewPlan(
            id="plan_under_test",
            job_id="job_under_test",
            stage=Stage.TECHNICAL_SCREEN,
            interview_type=InterviewType.TECHNICAL_SCREEN,
            duration_minutes=duration_minutes,
            panel_size=panel_size,
            minimum_lead_hours=minimum_lead_hours,
            buffer_before_minutes=buffer_before,
            buffer_after_minutes=buffer_after,
            working_hours_policy=policy,
            active=plan_active,
        )
    )
    for identifier, timezone, skills, active in interviewers:
        session.add(
            Interviewer(
                id=identifier,
                name=f"Panellist {identifier}",
                email=f"{identifier}@talent-ops.test",
                timezone=timezone,
                active=active,
            )
        )
        for code in skills:
            session.add(InterviewerSkill(interviewer_id=identifier, skill_code=code))
    await session.flush()

    for code, minimum in required_skills:
        session.add(
            InterviewPlanSkillRequirement(
                interview_plan_id="plan_under_test", skill_code=code, minimum_interviewers=minimum
            )
        )

    windows: list[tuple[PartyType, str, datetime, datetime]] = [
        (PartyType.CANDIDATE, "cand_under_test", start, end) for start, end in candidate_windows
    ]
    for owner, spans in (interviewer_windows or {}).items():
        windows.extend((PartyType.INTERVIEWER, owner, start, end) for start, end in spans)
    for index, (owner_type, owner_id, start, end) in enumerate(windows):
        session.add(
            AvailabilityWindow(
                id=f"avail_{index}",
                owner_type=owner_type,
                owner_id=owner_id,
                start_utc=start,
                end_utc=end,
                source=AvailabilitySource.DECLARED,
            )
        )

    for index, (attendees, start, end, status) in enumerate(bookings):
        session.add(
            Interview(
                id=f"iv_{index}",
                application_id=APPLICATION,
                interview_plan_id="plan_under_test",
                start_utc=start,
                end_utc=end,
                display_timezone="UTC",
                status=status,
                idempotency_key=f"key_{index}",
            )
        )
        await session.flush()
        for attendee_type, attendee_id in attendees:
            session.add(
                InterviewAttendee(interview_id=f"iv_{index}", attendee_type=attendee_type, attendee_id=attendee_id)
            )
    await session.flush()


def service(session: AsyncSession, settings: Settings) -> SchedulingService:
    return sqlite_scheduling_service(session, settings)


async def propose(
    session: AsyncSession,
    settings: Settings,
    *,
    now: datetime = NOW,
    first: date = DAY[0],
    last: date = DAY[4],
    **kwargs: object,
) -> SlotProposalResult:
    """Ask for options over the test week, with everything else defaulted."""
    return await service(session, settings).propose_interview_slots(
        application_id=kwargs.pop("application_id", APPLICATION),  # type: ignore[arg-type]
        now=now,
        start_date=first,
        end_date=last,
        **kwargs,  # type: ignore[arg-type]
    )


def starts(result: SlotProposalResult) -> list[datetime]:
    return [option.start_utc for option in result.options]


# ------------------------------------------------------------------ the basic shape


async def test_a_single_interviewer_panel(session: AsyncSession, settings: Settings) -> None:
    await scenario(
        session,
        candidate_windows=[(at(0, 14), at(0, 16))],
        interviewer_windows={"int_one": [(at(0, 9), at(0, 17))]},
    )
    result = await propose(session, settings, max_options=10)

    assert starts(result) == [at(0, 14), at(0, 14, 15), at(0, 14, 30), at(0, 14, 45), at(0, 15)]
    assert [option.panel[0].id for option in result.options] == ["int_one"] * 5
    assert result.no_options_code is None
    assert result.why_these_are_valid is not None


async def test_the_result_carries_the_context_the_next_step_needs(session: AsyncSession, settings: Settings) -> None:
    """Every field asserted against a known value, not merely present.

    The Phase 3 review found a transposition of two same-typed adjacent fields that the schema,
    the type checker, and the tool contract all passed. This model has several -- ``job_id`` and
    ``candidate_id``, ``searched_from_utc`` and ``searched_to_utc`` -- so the same lesson applies.
    """
    await scenario(
        session,
        duration_minutes=45,
        candidate_windows=[(at(0, 14), at(0, 16))],
        interviewer_windows={"int_one": [(at(0, 9), at(0, 17))]},
    )
    result = await propose(session, settings)

    assert result.application_id == APPLICATION
    assert result.candidate_id == "cand_under_test"
    assert result.candidate_name == "Sam Fixture"
    assert result.job_id == "job_under_test"
    assert result.job_title == "Test Role"
    assert result.stage is Stage.TECHNICAL_SCREEN
    assert result.interview_plan_id == "plan_under_test"
    assert result.duration_minutes == 45
    assert result.panel_size == 1
    assert result.display_timezone == "America/New_York"
    assert result.searched_from_utc == datetime(2026, 9, 7, 4, tzinfo=UTC)  # local midnight in New York
    assert result.searched_to_utc == datetime(2026, 9, 12, 4, tzinfo=UTC)  # midnight after the last day
    assert result.options[0].duration_minutes == 45
    assert result.options[0].end_utc - result.options[0].start_utc == timedelta(minutes=45)


async def test_local_times_are_the_same_instant_in_the_display_zone(session: AsyncSession, settings: Settings) -> None:
    await scenario(
        session,
        candidate_windows=[(at(0, 14), at(0, 16))],
        interviewer_windows={"int_one": [(at(0, 9), at(0, 17))]},
    )
    option = (await propose(session, settings)).options[0]

    assert option.start_local == option.start_utc
    assert option.start_local.utcoffset() == timedelta(hours=-4)
    assert option.start_local.hour == 10
    assert option.display_timezone == "America/New_York"


async def test_a_timezone_override_changes_the_rendering_not_the_instant(
    session: AsyncSession, settings: Settings
) -> None:
    await scenario(
        session,
        candidate_windows=[(at(0, 14), at(0, 16))],
        interviewer_windows={"int_one": [(at(0, 9), at(0, 17))]},
    )
    default = await propose(session, settings)
    overridden = await propose(session, settings, display_timezone="Europe/Dublin")

    assert starts(overridden) == starts(default)
    assert overridden.display_timezone == "Europe/Dublin"
    assert overridden.options[0].start_local.hour == 15


async def test_the_same_request_twice_returns_identical_options(session: AsyncSession, settings: Settings) -> None:
    """§12 Phase 4's determinism gate. Nothing in the engine reads a clock or a random source, so
    two runs over one database must agree down to the panel."""
    await scenario(
        session,
        panel_size=2,
        candidate_windows=[(at(0, 14), at(0, 18))],
        interviewers=(Panellist("int_b"), Panellist("int_a"), Panellist("int_c")),
        interviewer_windows={name: [(at(0, 9), at(0, 20))] for name in ("int_a", "int_b", "int_c")},
    )
    first = await propose(session, settings, max_options=10)
    second = await propose(session, settings, max_options=10)

    assert first.model_dump() == second.model_dump()
    # Chosen by id order, not by insertion order or by name.
    assert [member.id for member in first.options[0].panel] == ["int_a", "int_b"]


# ------------------------------------------------------------------ panels and skills


async def test_a_multi_person_panel_needs_everyone_free_at_once(session: AsyncSession, settings: Settings) -> None:
    await scenario(
        session,
        panel_size=2,
        candidate_windows=[(at(0, 12), at(0, 20))],
        interviewers=(Panellist("int_a"), Panellist("int_b")),
        interviewer_windows={
            "int_a": [(at(0, 12), at(0, 16))],
            "int_b": [(at(0, 15), at(0, 20))],
        },
    )
    result = await propose(session, settings, max_options=10)

    # Only the 15:00-16:00 overlap can hold both of them for a full hour.
    assert starts(result) == [at(0, 15)]


async def test_skill_coverage_picks_a_panel_that_covers_every_requirement(
    session: AsyncSession, settings: Settings
) -> None:
    await scenario(
        session,
        panel_size=2,
        required_skills=[("python", 1), ("system_design", 1)],
        candidate_windows=[(at(0, 14), at(0, 15))],
        interviewers=(
            Panellist("int_a", skills=("python",)),
            Panellist("int_b", skills=("python",)),
            Panellist("int_c", skills=("system_design",)),
        ),
        interviewer_windows={name: [(at(0, 9), at(0, 20))] for name in ("int_a", "int_b", "int_c")},
    )
    result = await propose(session, settings)

    # int_a + int_b is the first combination in id order and covers only python, so it is skipped.
    assert [member.id for member in result.options[0].panel] == ["int_a", "int_c"]


async def test_one_panellist_may_satisfy_two_requirements(session: AsyncSession, settings: Settings) -> None:
    """The plan asks for coverage, not for one specialist per skill. Reading it the strict way
    would make plans unstaffable that a recruiter approved as satisfiable."""
    await scenario(
        session,
        panel_size=1,
        required_skills=[("python", 1), ("system_design", 1)],
        candidate_windows=[(at(0, 14), at(0, 15))],
        interviewers=(Panellist("int_a", skills=("python", "system_design")),),
        interviewer_windows={"int_a": [(at(0, 9), at(0, 20))]},
    )
    assert len((await propose(session, settings)).options) == 1


async def test_a_requirement_for_two_holders_needs_two_people(session: AsyncSession, settings: Settings) -> None:
    await scenario(
        session,
        panel_size=2,
        required_skills=[("python", 2)],
        candidate_windows=[(at(0, 14), at(0, 15))],
        interviewers=(Panellist("int_a", skills=("python",)), Panellist("int_b", skills=("sql",))),
        interviewer_windows={name: [(at(0, 9), at(0, 20))] for name in ("int_a", "int_b")},
    )
    result = await propose(session, settings)

    assert result.options == []
    assert result.no_options_code is SlotUnavailableReason.NO_ELIGIBLE_INTERVIEWERS


async def test_too_few_eligible_interviewers_is_reported_before_any_date_is_searched(
    session: AsyncSession, settings: Settings
) -> None:
    await scenario(
        session,
        panel_size=3,
        candidate_windows=[(at(0, 14), at(0, 20))],
        interviewers=(Panellist("int_a"), Panellist("int_b")),
        interviewer_windows={name: [(at(0, 9), at(0, 20))] for name in ("int_a", "int_b")},
    )
    result = await propose(session, settings)

    assert result.no_options_code is SlotUnavailableReason.NO_ELIGIBLE_INTERVIEWERS
    assert result.no_options_reason is not None
    assert "will not help" in result.no_options_reason
    assert result.why_these_are_valid is None


async def test_an_inactive_interviewer_is_never_proposed(session: AsyncSession, settings: Settings) -> None:
    """§9.2 step 3. The skills match and the diary is empty; only ``active`` stands in the way."""
    await scenario(
        session,
        required_skills=[("python", 1)],
        candidate_windows=[(at(0, 14), at(0, 20))],
        interviewers=(Panellist("int_retired", skills=("python",), active=False),),
        interviewer_windows={"int_retired": [(at(0, 9), at(0, 20))]},
    )
    result = await propose(session, settings)

    assert result.no_options_code is SlotUnavailableReason.NO_ELIGIBLE_INTERVIEWERS


async def test_a_free_panel_that_never_covers_the_skills_is_a_different_reason(
    session: AsyncSession, settings: Settings
) -> None:
    """Enough people, the right skills somewhere in the pool, never both at the same moment.

    ``NO_ELIGIBLE_INTERVIEWERS`` would be wrong here -- a valid panel does exist, so widening the
    dates is exactly the right advice, and that is what ``PANEL_COVERAGE`` says.
    """
    await scenario(
        session,
        panel_size=2,
        required_skills=[("python", 1), ("sql", 1)],
        candidate_windows=[(at(0, 14), at(0, 15))],
        interviewers=(
            Panellist("int_a", skills=("python",)),
            Panellist("int_b", skills=("python",)),
            Panellist("int_c", skills=("sql",)),
        ),
        interviewer_windows={
            "int_a": [(at(0, 9), at(0, 20))],
            "int_b": [(at(0, 9), at(0, 20))],
            "int_c": [(at(2, 9), at(2, 20))],  # qualified, but on the wrong day
        },
    )
    result = await propose(session, settings)

    assert result.no_options_code is SlotUnavailableReason.PANEL_COVERAGE


async def test_fewer_free_than_the_panel_needs_is_no_available_panel(session: AsyncSession, settings: Settings) -> None:
    await scenario(
        session,
        panel_size=2,
        candidate_windows=[(at(0, 14), at(0, 15))],
        interviewers=(Panellist("int_a"), Panellist("int_b")),
        interviewer_windows={
            "int_a": [(at(0, 9), at(0, 20))],
            "int_b": [(at(2, 9), at(2, 20))],
        },
    )
    result = await propose(session, settings)

    assert result.no_options_code is SlotUnavailableReason.NO_AVAILABLE_PANEL


# ------------------------------------------------------------------ conflicts and buffers


async def test_an_interviewer_conflict_removes_the_slot(session: AsyncSession, settings: Settings) -> None:
    await scenario(
        session,
        candidate_windows=[(at(0, 14), at(0, 17))],
        interviewer_windows={"int_one": [(at(0, 9), at(0, 20))]},
        bookings=[([(PartyType.INTERVIEWER, "int_one")], at(0, 15), at(0, 16), InterviewStatus.SCHEDULED)],
    )
    result = await propose(session, settings, max_options=10)

    assert starts(result) == [at(0, 14), at(0, 16)]


async def test_a_cancelled_interview_frees_its_time(session: AsyncSession, settings: Settings) -> None:
    """§9.3. Only a ``scheduled`` interview blocks; a cancellation is what puts the slot back."""
    await scenario(
        session,
        candidate_windows=[(at(0, 14), at(0, 16))],
        interviewer_windows={"int_one": [(at(0, 9), at(0, 20))]},
        bookings=[([(PartyType.INTERVIEWER, "int_one")], at(0, 14), at(0, 15), InterviewStatus.CANCELLED)],
    )
    assert at(0, 14) in starts(await propose(session, settings, max_options=10))


async def test_a_completed_interview_does_not_block_either(session: AsyncSession, settings: Settings) -> None:
    await scenario(
        session,
        candidate_windows=[(at(0, 14), at(0, 16))],
        interviewer_windows={"int_one": [(at(0, 9), at(0, 20))]},
        bookings=[([(PartyType.INTERVIEWER, "int_one")], at(0, 14), at(0, 15), InterviewStatus.COMPLETED)],
    )
    assert at(0, 14) in starts(await propose(session, settings, max_options=10))


async def test_a_booking_that_only_touches_the_slot_is_not_a_conflict(
    session: AsyncSession, settings: Settings
) -> None:
    await scenario(
        session,
        candidate_windows=[(at(0, 14), at(0, 15))],
        interviewer_windows={"int_one": [(at(0, 9), at(0, 20))]},
        bookings=[([(PartyType.INTERVIEWER, "int_one")], at(0, 13), at(0, 14), InterviewStatus.SCHEDULED)],
    )
    assert starts(await propose(session, settings)) == [at(0, 14)]


async def test_a_buffer_turns_a_touching_booking_into_a_conflict(session: AsyncSession, settings: Settings) -> None:
    """The same world as the test above, plus the plan's buffer. The interview still fits; the
    quarter hour the panel is owed afterwards does not."""
    await scenario(
        session,
        buffer_after=15,
        candidate_windows=[(at(0, 14), at(0, 15))],
        interviewer_windows={"int_one": [(at(0, 9), at(0, 20))]},
        bookings=[([(PartyType.INTERVIEWER, "int_one")], at(0, 15), at(0, 16), InterviewStatus.SCHEDULED)],
    )
    result = await propose(session, settings)

    assert result.options == []
    assert result.no_options_code is SlotUnavailableReason.NO_AVAILABLE_PANEL


async def test_an_interviewer_must_be_free_for_the_buffers_too(session: AsyncSession, settings: Settings) -> None:
    """§9.3: available "for the full interval plus required buffers". The window holds the hour
    exactly, so the buffer runs off its edge."""
    await scenario(
        session,
        buffer_before=15,
        buffer_after=15,
        candidate_windows=[(at(0, 14), at(0, 15))],
        interviewer_windows={"int_one": [(at(0, 14), at(0, 15))]},
    )
    assert (await propose(session, settings)).options == []


async def test_buffers_do_not_eat_into_the_candidates_own_window(session: AsyncSession, settings: Settings) -> None:
    """The deliberate asymmetry. Somebody who says they are free from 15:00 can start at 15:00;
    silently requiring a quarter-hour of their evening would refuse a slot they offered."""
    await scenario(
        session,
        buffer_before=15,
        buffer_after=15,
        candidate_windows=[(at(0, 15), at(0, 16))],
        interviewer_windows={"int_one": [(at(0, 9), at(0, 20))]},
    )
    assert starts(await propose(session, settings)) == [at(0, 15)]


async def test_a_candidate_already_booked_at_every_free_time(session: AsyncSession, settings: Settings) -> None:
    await scenario(
        session,
        candidate_windows=[(at(0, 14), at(0, 15))],
        interviewer_windows={"int_one": [(at(0, 9), at(0, 20))]},
        bookings=[([(PartyType.CANDIDATE, "cand_under_test")], at(0, 14), at(0, 15), InterviewStatus.SCHEDULED)],
    )
    result = await propose(session, settings)

    assert result.options == []
    assert result.no_options_code is SlotUnavailableReason.ATTENDEE_CONFLICT
    assert result.no_options_reason is not None
    assert "already" in result.no_options_reason


# ------------------------------------------------------------------ lead time


async def test_minimum_lead_time_skips_the_slots_that_are_too_soon(session: AsyncSession, settings: Settings) -> None:
    await scenario(
        session,
        minimum_lead_hours=48,
        candidate_windows=[(at(0, 14), at(0, 15)), (at(3, 14), at(3, 15))],
        interviewer_windows={"int_one": [(at(0, 9), at(3, 20))]},
    )
    # 48 hours from Monday 09:00 is Wednesday 09:00, so Monday's window is out and Thursday's is in.
    result = await propose(session, settings, now=at(0, 9), max_options=10)

    assert starts(result) == [at(3, 14)]
    assert result.searched_from_utc == at(2, 9)


async def test_a_range_entirely_inside_the_lead_time(session: AsyncSession, settings: Settings) -> None:
    await scenario(
        session,
        minimum_lead_hours=72,
        candidate_windows=[(at(0, 14), at(0, 15))],
        interviewer_windows={"int_one": [(at(0, 9), at(0, 20))]},
    )
    result = await propose(session, settings, now=at(0, 9), first=DAY[0], last=DAY[1])

    assert result.no_options_code is SlotUnavailableReason.OUTSIDE_LEAD_TIME
    assert result.no_options_reason is not None
    assert "72 hours" in result.no_options_reason


async def test_availability_only_before_the_cutoff_is_a_lead_time_answer(
    session: AsyncSession, settings: Settings
) -> None:
    """The range itself is long enough; the candidate's declared time is all in the dead zone.
    Blaming their availability would send the caller chasing times they already gave."""
    await scenario(
        session,
        minimum_lead_hours=48,
        candidate_windows=[(at(0, 14), at(0, 16))],
        interviewer_windows={"int_one": [(at(0, 9), at(4, 20))]},
    )
    result = await propose(session, settings, now=at(0, 9))

    assert result.no_options_code is SlotUnavailableReason.OUTSIDE_LEAD_TIME


# ------------------------------------------------------------------ working hours


async def test_candidate_business_hours_are_judged_on_the_candidates_clock(
    session: AsyncSession, settings: Settings
) -> None:
    await scenario(
        session,
        policy=WorkingHoursPolicy.CANDIDATE_BUSINESS_HOURS,
        candidate_timezone="America/Chicago",
        candidate_windows=[(at(0, 13), at(0, 17))],
        interviewer_windows={"int_one": [(at(0, 0), at(1, 0))]},
    )
    result = await propose(session, settings, max_options=10)

    # 13:00 UTC is 08:00 in Chicago, before the working day; 14:00 UTC is 09:00.
    assert starts(result)[0] == at(0, 14)
    assert result.options[0].start_local.hour == 9


async def test_interviewer_business_hours_are_judged_on_each_panellists_clock(
    session: AsyncSession, settings: Settings
) -> None:
    await scenario(
        session,
        policy=WorkingHoursPolicy.INTERVIEWER_BUSINESS_HOURS,
        candidate_timezone="Europe/Dublin",
        candidate_windows=[(at(0, 8), at(0, 20))],
        interviewers=(Panellist("int_dublin", timezone="Europe/Dublin"),),
        interviewer_windows={"int_dublin": [(at(0, 0), at(1, 0))]},
    )
    result = await propose(session, settings, max_options=40)

    # Dublin is UTC+1 in September: the working day is 08:00-16:00 UTC.
    assert starts(result)[0] == at(0, 8)
    assert starts(result)[-1] == at(0, 15)


async def test_both_business_hours_needs_the_overlap_of_two_working_days(
    session: AsyncSession, settings: Settings
) -> None:
    """§12 Phase 4's cross-timezone case, in its unhappy form: two people whose working days do
    not meet. Sydney's 09:00-17:00 and New York's are on opposite sides of the clock."""
    await scenario(
        session,
        policy=WorkingHoursPolicy.BOTH_BUSINESS_HOURS,
        candidate_timezone="Australia/Sydney",
        candidate_windows=[(at(0, 0), at(1, 0))],
        interviewers=(Panellist("int_ny", timezone="America/New_York"),),
        interviewer_windows={"int_ny": [(at(0, 0), at(1, 0))]},
    )
    result = await propose(session, settings)

    assert result.options == []
    assert result.no_options_code is SlotUnavailableReason.NO_AVAILABLE_PANEL


async def test_the_weekend_is_outside_business_hours(session: AsyncSession, settings: Settings) -> None:
    await scenario(
        session,
        policy=WorkingHoursPolicy.CANDIDATE_BUSINESS_HOURS,
        candidate_windows=[(at(5, 13), at(5, 20))],  # Saturday
        interviewer_windows={"int_one": [(at(5, 0), at(6, 0))]},
    )
    result = await propose(session, settings, first=DAY[5], last=DAY[6])

    assert result.no_options_code is SlotUnavailableReason.OUTSIDE_WORKING_HOURS
    assert result.no_options_reason is not None
    assert "Monday to Friday" in result.no_options_reason


async def test_extended_hours_defers_entirely_to_declared_availability(
    session: AsyncSession, settings: Settings
) -> None:
    """The policy that opts out. A 22:00 interview is fine if both people said 22:00 suits them."""
    await scenario(
        session,
        policy=WorkingHoursPolicy.EXTENDED_HOURS,
        candidate_windows=[(at(5, 22), at(5, 23))],  # 18:00 Saturday in New York
        interviewer_windows={"int_one": [(at(5, 20), at(6, 2))]},
    )
    assert starts(await propose(session, settings, first=DAY[5], last=DAY[5])) == [at(5, 22)]


# ------------------------------------------------------------------ timezones and daylight saving


async def test_cross_timezone_scheduling_quotes_the_candidates_morning(
    session: AsyncSession, settings: Settings
) -> None:
    """Tokyo and Los Angeles: one instant, two very different local times, and the candidate's is
    the one returned."""
    await scenario(
        session,
        candidate_timezone="Asia/Tokyo",
        candidate_windows=[(datetime(2026, 9, 6, 23, tzinfo=UTC), datetime(2026, 9, 7, 1, tzinfo=UTC))],
        interviewers=(Panellist("int_la", timezone="America/Los_Angeles"),),
        interviewer_windows={"int_la": [(datetime(2026, 9, 6, 22, tzinfo=UTC), datetime(2026, 9, 7, 2, tzinfo=UTC))]},
    )
    result = await propose(session, settings, now=datetime(2026, 9, 1, tzinfo=UTC), first=DAY[0], last=DAY[0])

    first_option = result.options[0]
    assert first_option.start_utc == datetime(2026, 9, 6, 23, tzinfo=UTC)
    assert first_option.start_local.hour == 8  # 08:00 Tuesday in Tokyo
    assert first_option.start_local.utcoffset() == timedelta(hours=9)
    assert first_option.start_utc.astimezone(ZoneInfo("America/Los_Angeles")).hour == 16


async def test_a_half_hour_offset_zone(session: AsyncSession, settings: Settings) -> None:
    """§12 Phase 4 asks for one. Kolkata is UTC+5:30, so its working day starts at 03:30 UTC --
    an offset that a whole-hour assumption anywhere in the chain would round away."""
    await scenario(
        session,
        policy=WorkingHoursPolicy.CANDIDATE_BUSINESS_HOURS,
        candidate_timezone="Asia/Kolkata",
        candidate_windows=[(at(0, 3), at(0, 12))],
        interviewer_windows={"int_one": [(at(0, 0), at(1, 0))]},
    )
    result = await propose(session, settings, max_options=10)

    assert starts(result)[0] == at(0, 3, 30)
    assert result.options[0].start_local.hour == 9
    assert result.options[0].start_local.minute == 0
    assert result.options[0].start_local.utcoffset() == timedelta(hours=5, minutes=30)


async def test_spring_forward_never_proposes_a_local_time_that_does_not_exist(
    session: AsyncSession, settings: Settings
) -> None:
    """§12 Phase 4, spring forward. On 2026-03-08 New York jumps 02:00 -> 03:00.

    The engine walks UTC and converts outwards, so the missing hour cannot be proposed -- but the
    conversion has to be real for that to hold. The assertion is that consecutive grid points
    straddling the gap render as 01:45 and then 03:00, with no 02:xx anywhere.
    """
    transition_day = date(2026, 3, 8)
    await scenario(
        session,
        duration_minutes=30,
        policy=WorkingHoursPolicy.EXTENDED_HOURS,
        candidate_windows=[(datetime(2026, 3, 8, 6, tzinfo=UTC), datetime(2026, 3, 8, 9, tzinfo=UTC))],
        interviewer_windows={"int_one": [(datetime(2026, 3, 8, 0, tzinfo=UTC), datetime(2026, 3, 9, 0, tzinfo=UTC))]},
    )
    result = await propose(
        session,
        settings,
        now=datetime(2026, 3, 1, tzinfo=UTC),
        first=transition_day,
        last=transition_day,
        max_options=10,
    )

    local_times = [(option.start_local.hour, option.start_local.minute) for option in result.options]
    assert (2, 0) not in local_times
    assert (1, 45) in local_times
    assert (3, 0) in local_times
    # The two are consecutive grid points fifteen minutes apart in UTC, an hour apart on the wall.
    gap = [
        option.start_utc
        for option in result.options
        if (option.start_local.hour, option.start_local.minute) in ((1, 45), (3, 0))
    ]
    assert gap[1] - gap[0] == timedelta(minutes=15)


async def test_fall_back_keeps_the_repeated_hour_as_two_distinct_options(
    session: AsyncSession, settings: Settings
) -> None:
    """§12 Phase 4, fall back. On 2026-11-01 New York's 01:00 hour happens twice.

    Both instants are real and bookable, and §9.3 requires the ambiguity to be resolved
    explicitly rather than guessed at. It is: the two options carry the same wall-clock hour and
    different UTC offsets, so a human reading them can tell which is which.
    """
    transition_day = date(2026, 11, 1)
    await scenario(
        session,
        policy=WorkingHoursPolicy.EXTENDED_HOURS,
        candidate_windows=[(datetime(2026, 11, 1, 5, tzinfo=UTC), datetime(2026, 11, 1, 7, tzinfo=UTC))],
        interviewer_windows={"int_one": [(datetime(2026, 11, 1, 0, tzinfo=UTC), datetime(2026, 11, 2, 0, tzinfo=UTC))]},
    )
    result = await propose(
        session,
        settings,
        now=datetime(2026, 10, 1, tzinfo=UTC),
        first=transition_day,
        last=transition_day,
        max_options=10,
    )

    one_oclock = [
        option for option in result.options if option.start_local.hour == 1 and option.start_local.minute == 0
    ]
    assert len(one_oclock) == 2
    assert one_oclock[0].start_utc != one_oclock[1].start_utc
    assert one_oclock[0].start_local.utcoffset() == timedelta(hours=-4)
    assert one_oclock[1].start_local.utcoffset() == timedelta(hours=-5)


# ------------------------------------------------------------------ bounds and refusals


async def test_the_option_count_is_bounded_and_says_when_it_bit(session: AsyncSession, settings: Settings) -> None:
    await scenario(
        session,
        candidate_windows=[(at(0, 12), at(0, 20))],
        interviewer_windows={"int_one": [(at(0, 9), at(0, 22))]},
    )
    capped = await propose(session, settings, max_options=3)
    assert len(capped.options) == 3
    assert capped.more_options_exist is True

    generous = await propose(session, settings, max_options=10)
    assert len(generous.options) == 10
    assert generous.options[:3] == capped.options


async def test_more_options_exist_is_false_when_the_answer_is_complete(
    session: AsyncSession, settings: Settings
) -> None:
    await scenario(
        session,
        candidate_windows=[(at(0, 14), at(0, 15))],
        interviewer_windows={"int_one": [(at(0, 9), at(0, 20))]},
    )
    result = await propose(session, settings, max_options=3)

    assert len(result.options) == 1
    assert result.more_options_exist is False


async def test_an_over_long_date_range_is_refused_not_shortened(session: AsyncSession, settings: Settings) -> None:
    """Clamping is right for a page size and wrong for a date range: a silently shortened search
    reports "nothing available" for a fortnight the caller believes it looked at."""
    await scenario(session, candidate_windows=[(at(0, 14), at(0, 15))])

    with pytest.raises(DomainError) as raised:
        await propose(session, settings, first=DAY[0], last=DAY[0] + timedelta(days=60))

    assert raised.value.code is ErrorCode.VALIDATION_ERROR
    assert str(settings.max_date_range_days) in raised.value.message


async def test_an_inverted_date_range_is_refused(session: AsyncSession, settings: Settings) -> None:
    await scenario(session, candidate_windows=[(at(0, 14), at(0, 15))])

    with pytest.raises(DomainError) as raised:
        await propose(session, settings, first=DAY[4], last=DAY[0])

    assert raised.value.code is ErrorCode.VALIDATION_ERROR


async def test_omitted_dates_search_forward_from_today(session: AsyncSession, settings: Settings) -> None:
    await scenario(
        session,
        candidate_windows=[(at(0, 14), at(0, 15))],
        interviewer_windows={"int_one": [(at(0, 9), at(0, 20))]},
    )
    result = await service(session, settings).propose_interview_slots(application_id=APPLICATION, now=at(0, 9))

    assert starts(result) == [at(0, 14)]
    assert result.searched_from_utc == at(0, 9)


async def test_a_timezone_that_is_not_iana_is_refused(session: AsyncSession, settings: Settings) -> None:
    await scenario(session, candidate_windows=[(at(0, 14), at(0, 15))])

    with pytest.raises(DomainError) as raised:
        await propose(session, settings, display_timezone="America/Newyork")

    assert raised.value.code is ErrorCode.VALIDATION_ERROR
    assert "America/New_York" in raised.value.message


async def test_an_unknown_application_is_not_found(session: AsyncSession, settings: Settings) -> None:
    await scenario(session, candidate_windows=[(at(0, 14), at(0, 15))])

    with pytest.raises(DomainError) as raised:
        await propose(session, settings, application_id="app_nope")

    assert raised.value.code is ErrorCode.NOT_FOUND
    assert "talent_ops_list_applications" in raised.value.message


@pytest.mark.parametrize("status", [ApplicationStatus.ON_HOLD, ApplicationStatus.WITHDRAWN, ApplicationStatus.CLOSED])
async def test_only_an_active_application_may_be_scheduled_against(
    session: AsyncSession, settings: Settings, status: ApplicationStatus
) -> None:
    """§9.2 step 1. A refusal, not an empty result: proposing times for a withdrawn candidate is
    not a scheduling problem to be worked around."""
    await scenario(
        session,
        application_status=status,
        candidate_windows=[(at(0, 14), at(0, 15))],
        interviewer_windows={"int_one": [(at(0, 9), at(0, 20))]},
    )
    with pytest.raises(DomainError) as raised:
        await propose(session, settings)

    assert raised.value.code is ErrorCode.CONFLICT
    assert status.value in raised.value.message


async def test_a_stage_with_no_approved_plan_refuses_rather_than_improvising(
    session: AsyncSession, settings: Settings
) -> None:
    await scenario(
        session,
        plan_active=False,
        candidate_windows=[(at(0, 14), at(0, 15))],
        interviewer_windows={"int_one": [(at(0, 9), at(0, 20))]},
    )
    with pytest.raises(DomainError) as raised:
        await propose(session, settings)

    assert raised.value.code is ErrorCode.NOT_FOUND
    assert "do not substitute" in raised.value.message


async def test_a_candidate_with_no_declared_time_is_a_reason_not_an_error(
    session: AsyncSession, settings: Settings
) -> None:
    await scenario(session, interviewer_windows={"int_one": [(at(0, 9), at(0, 20))]})
    result = await propose(session, settings)

    assert result.options == []
    assert result.no_options_code is SlotUnavailableReason.NO_CANDIDATE_AVAILABILITY


async def test_a_window_shorter_than_the_interview_is_no_availability(
    session: AsyncSession, settings: Settings
) -> None:
    await scenario(
        session,
        duration_minutes=90,
        candidate_windows=[(at(0, 14), at(0, 15))],
        interviewer_windows={"int_one": [(at(0, 9), at(0, 20))]},
    )
    result = await propose(session, settings)

    assert result.no_options_code is SlotUnavailableReason.NO_CANDIDATE_AVAILABILITY
    assert result.no_options_reason is not None
    assert "90 minutes" in result.no_options_reason


async def test_no_interviewer_availability_at_all(session: AsyncSession, settings: Settings) -> None:
    await scenario(session, candidate_windows=[(at(0, 14), at(0, 15))])
    result = await propose(session, settings)

    assert result.no_options_code is SlotUnavailableReason.NO_INTERVIEWER_AVAILABILITY


async def test_two_touching_candidate_windows_make_one_bookable_block(
    session: AsyncSession, settings: Settings
) -> None:
    """§12 Phase 4's boundary-touching case, where it actually matters: neither declared window is
    long enough on its own, and together they are."""
    await scenario(
        session,
        duration_minutes=90,
        candidate_windows=[(at(0, 14), at(0, 15)), (at(0, 15), at(0, 16))],
        interviewer_windows={"int_one": [(at(0, 9), at(0, 20))]},
    )
    assert starts(await propose(session, settings, max_options=10)) == [at(0, 14), at(0, 14, 15), at(0, 14, 30)]


# ------------------------------------------------------------------ the Phase 4 completion gate


async def test_the_phase_4_completion_gate(seeded_session: AsyncSession, settings: Settings) -> None:
    """§12 Phase 4: three or more correct, conflict-free options, identically ordered across runs.

    The plan names a "Backend Engineer scenario"; the demo dataset has no such job, and the open
    engineering role is Senior Platform Engineer (the same substitution Phase 3 recorded).
    ``app_02`` sits at its technical screen: 60 minutes, one panellist holding ``python``,
    48 hours' notice, quarter-hour buffers, and both parties' business hours.
    """
    scheduler = sqlite_scheduling_service(seeded_session, settings)
    request = {
        "application_id": "app_02",
        "now": SEED_ANCHOR.replace(hour=9),
        "start_date": date(2026, 9, 7),
        "end_date": date(2026, 9, 11),
        "max_options": 5,
    }
    result = await scheduler.propose_interview_slots(**request)  # type: ignore[arg-type]
    again = await scheduler.propose_interview_slots(**request)  # type: ignore[arg-type]

    assert len(result.options) >= 3
    assert result.model_dump() == again.model_dump()
    assert result.no_options_code is None

    for option in result.options:
        assert option.end_utc - option.start_utc == timedelta(minutes=60)
        assert len(option.panel) == 1
        # The plan requires python; only these three hold it, and one of them is inactive.
        assert option.panel[0].id in {"int_avery", "int_blake", "int_casey"}
        # 48 hours from Monday 09:00 UTC.
        assert option.start_utc >= datetime(2026, 9, 9, 9, tzinfo=UTC)
        # Both parties' business hours; the candidate is in New York.
        assert 9 <= option.start_local.hour < 17
        assert option.start_local.utcoffset() == timedelta(hours=-4)


async def test_the_gate_scenario_never_proposes_a_time_an_attendee_is_already_booked(
    seeded_session: AsyncSession, settings: Settings
) -> None:
    """``int_avery`` holds a scheduled interview on the Tuesday of the demo week. Nothing the
    engine returns may overlap it -- including its buffers."""
    scheduler = sqlite_scheduling_service(seeded_session, settings)
    result = await scheduler.propose_interview_slots(
        application_id="app_02",
        now=SEED_ANCHOR.replace(hour=9),
        start_date=date(2026, 9, 7),
        end_date=date(2026, 9, 11),
        max_options=10,
    )

    booked = (datetime(2026, 9, 8, 15, tzinfo=UTC), datetime(2026, 9, 8, 16, tzinfo=UTC))
    for option in result.options:
        if any(member.id == "int_avery" for member in option.panel):
            assert option.end_utc <= booked[0] or option.start_utc >= booked[1]
