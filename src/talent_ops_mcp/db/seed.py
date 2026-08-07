"""Deterministic demo data.

Three properties this module guarantees, each load-bearing for a different reason:

* **Fictional.** Every identity is invented and every address is on the reserved ``.test`` TLD
  (RFC 2606), which can never resolve. A demo seed that accidentally mails a real person is a
  failure mode worth designing out rather than apologising for.
* **Deterministic.** Ids are literals and every instant is derived from ``anchor`` by offset --
  no ``uuid4``, no ``now()``, no randomness. Two runs produce byte-identical rows, so tests can
  assert on specific values instead of merely counting them.
* **Idempotent.** Rows are merged by primary key, so re-running converges on the same state
  rather than duplicating or failing. Re-seeding is also how you *repair* a database someone has
  poked at: local edits to seeded rows are reset.

``anchor`` exists because those last two pull against each other. Fixed dates are reproducible
but go stale, and a demo whose interview slots are all in the past demonstrates nothing. The
default is a fixed Monday so tests are stable; ``scripts/seed_demo_data.py --anchor now`` moves
the whole dataset to the current week for a live demo, preserving every relative offset.

The datasets are :class:`typing.NamedTuple` rather than bare tuples. With twelve positional
fields on an interview plan, ``60, 1, 24, 10, 10`` is unreadable and a transposed pair is
invisible in review; named fields also stop the type checker conflating ``status`` across the
loops that consume them.

``action_previews`` and ``audit_events`` are deliberately left empty. Both are records of things
that *happened*, and fabricating an audit trail for events that never occurred would undermine
the one property an audit log needs.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import NamedTuple

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from talent_ops_mcp.db.base import Base
from talent_ops_mcp.db.schema import (
    Application,
    AvailabilityWindow,
    Candidate,
    FeedbackRequest,
    Interview,
    InterviewAttendee,
    Interviewer,
    InterviewerSkill,
    InterviewPlan,
    InterviewPlanSkillRequirement,
    Job,
)
from talent_ops_mcp.models.domain import (
    ApplicationStatus,
    AvailabilitySource,
    ContactMethod,
    FeedbackStatus,
    InterviewStatus,
    InterviewType,
    JobStatus,
    PartyType,
    ResponseStatus,
    Stage,
    WorkingHoursPolicy,
)

SEED_ANCHOR = datetime(2026, 9, 7, tzinfo=UTC)
"""Monday 2026-09-07 00:00 UTC. A Monday so that ``day=0..4`` is the working week."""

EMAIL_DOMAIN = "talent-ops.test"


def at(anchor: datetime, day: int, hour: int, minute: int = 0) -> datetime:
    """An instant ``day`` days after ``anchor``, at ``hour:minute`` UTC.

    Negative ``day`` reaches into the past, which is how the seed places already-completed
    interviews and their outstanding feedback.
    """
    return anchor + timedelta(days=day, hours=hour, minutes=minute)


@dataclass(frozen=True, slots=True)
class SeedSummary:
    """Row counts after seeding, for the CLI to report and tests to assert on."""

    jobs: int
    candidates: int
    applications: int
    interview_plans: int
    interviewers: int
    availability_windows: int
    interviews: int
    feedback_requests: int

    def total(self) -> int:
        return (
            self.jobs
            + self.candidates
            + self.applications
            + self.interview_plans
            + self.interviewers
            + self.availability_windows
            + self.interviews
            + self.feedback_requests
        )


# --------------------------------------------------------------------------- row shapes


class JobRow(NamedTuple):
    id: str
    title: str
    department: str
    location: str
    timezone: str
    status: JobStatus


class InterviewerRow(NamedTuple):
    id: str
    name: str
    timezone: str
    active: bool
    skills: tuple[str, ...]


class CandidateRow(NamedTuple):
    id: str
    first: str
    last: str
    timezone: str
    phone: str | None
    contact: ContactMethod


class ApplicationRow(NamedTuple):
    id: str
    candidate_id: str
    job_id: str
    stage: Stage
    status: ApplicationStatus
    coordinator: str


class PlanRow(NamedTuple):
    id: str
    job_id: str
    stage: Stage
    interview_type: InterviewType
    duration_minutes: int
    panel_size: int
    minimum_lead_hours: int
    buffer_before: int
    buffer_after: int
    policy: WorkingHoursPolicy
    active: bool
    skills: tuple[tuple[str, int], ...]


class WindowRow(NamedTuple):
    """A free block, in whole UTC hours relative to the anchor week."""

    owner_id: str
    day: int
    start_hour: int
    end_hour: int


class InterviewRow(NamedTuple):
    id: str
    application_id: str
    plan_id: str
    day: int
    hour: int
    minute: int
    duration_minutes: int
    status: InterviewStatus
    display_timezone: str
    panel: tuple[str, ...]


class FeedbackRow(NamedTuple):
    id: str
    interview_id: str
    interviewer_id: str
    due_day: int
    status: FeedbackStatus


# --------------------------------------------------------------------------- reference data

JOBS: tuple[JobRow, ...] = (
    JobRow(
        "job_platform_eng",
        "Senior Platform Engineer",
        "Engineering",
        "Bloomington, IN (hybrid)",
        "America/Indiana/Indianapolis",
        JobStatus.OPEN,
    ),
    JobRow(
        "job_clinical_data",
        "Clinical Data Analyst",
        "Clinical Affairs",
        "Remote (US)",
        "America/New_York",
        JobStatus.OPEN,
    ),
    # On hold on purpose: read tools must be able to show a job that is not currently
    # schedulable, and the status filter needs something to exclude.
    JobRow(
        "job_qa_automation", "QA Automation Engineer", "Quality", "Limerick, IE", "Europe/Dublin", JobStatus.ON_HOLD
    ),
)

INTERVIEWERS: tuple[InterviewerRow, ...] = (
    InterviewerRow(
        "int_avery",
        "Avery Demo",
        "America/Indiana/Indianapolis",
        True,
        ("python", "distributed_systems", "system_design"),
    ),
    InterviewerRow("int_blake", "Blake Sample", "America/New_York", True, ("python", "sql")),
    InterviewerRow("int_casey", "Casey Fixture", "Europe/Dublin", True, ("test_automation", "python")),
    InterviewerRow(
        "int_devon", "Devon Placeholder", "America/Los_Angeles", True, ("clinical_data", "sql", "regulatory")
    ),
    InterviewerRow("int_ellis", "Ellis Example", "America/Chicago", True, ("hiring_manager", "system_design")),
    # Inactive: §9.2 step 3 selects only active interviewers, and that filter needs a row it
    # must refuse to return even though the skills match.
    InterviewerRow("int_frankie", "Frankie Mock", "Europe/London", False, ("test_automation", "regulatory")),
)

CANDIDATES: tuple[CandidateRow, ...] = (
    CandidateRow("cand_01", "Alex", "Testfield", "America/Indiana/Indianapolis", "+1-555-0101", ContactMethod.EMAIL),
    CandidateRow("cand_02", "Bailey", "Sampleton", "America/New_York", "+1-555-0102", ContactMethod.PHONE),
    CandidateRow("cand_03", "Cameron", "Mockford", "America/Chicago", None, ContactMethod.EMAIL),
    CandidateRow("cand_04", "Dakota", "Placeholder", "America/Denver", None, ContactMethod.EMAIL),
    CandidateRow("cand_05", "Emerson", "Fictional", "America/Los_Angeles", "+1-555-0105", ContactMethod.EMAIL),
    CandidateRow("cand_06", "Finley", "Demoford", "America/New_York", "+1-555-0106", ContactMethod.EMAIL),
    CandidateRow("cand_07", "Gray", "Stubbs", "Europe/Dublin", None, ContactMethod.EMAIL),
    CandidateRow("cand_08", "Harper", "Fakename", "Australia/Sydney", "+61-555-0108", ContactMethod.PHONE),
    CandidateRow("cand_09", "Indigo", "Dummyton", "America/Chicago", None, ContactMethod.EMAIL),
    CandidateRow("cand_10", "Jordan", "Sampleby", "Europe/London", "+44-555-0110", ContactMethod.EMAIL),
    CandidateRow("cand_11", "Kai", "Testerly", "Asia/Tokyo", None, ContactMethod.EMAIL),
    CandidateRow("cand_12", "Logan", "Mockton", "America/New_York", None, ContactMethod.EMAIL),
)

APPLICATIONS: tuple[ApplicationRow, ...] = (
    ApplicationRow("app_01", "cand_01", "job_platform_eng", Stage.ONSITE, ApplicationStatus.ACTIVE, "coord_lane"),
    ApplicationRow(
        "app_02", "cand_02", "job_platform_eng", Stage.TECHNICAL_SCREEN, ApplicationStatus.ACTIVE, "coord_lane"
    ),
    ApplicationRow(
        "app_03", "cand_03", "job_platform_eng", Stage.RECRUITER_SCREEN, ApplicationStatus.ACTIVE, "coord_lane"
    ),
    ApplicationRow("app_04", "cand_04", "job_platform_eng", Stage.APPLIED, ApplicationStatus.ACTIVE, "coord_lane"),
    ApplicationRow("app_05", "cand_05", "job_platform_eng", Stage.OFFER, ApplicationStatus.ACTIVE, "coord_lane"),
    ApplicationRow(
        "app_06", "cand_06", "job_clinical_data", Stage.TECHNICAL_SCREEN, ApplicationStatus.ACTIVE, "coord_reese"
    ),
    ApplicationRow(
        "app_07", "cand_07", "job_clinical_data", Stage.RECRUITER_SCREEN, ApplicationStatus.ACTIVE, "coord_reese"
    ),
    ApplicationRow("app_08", "cand_08", "job_clinical_data", Stage.APPLIED, ApplicationStatus.ACTIVE, "coord_reese"),
    # Not schedulable: §9.2 step 1 rejects anything but ACTIVE, and that refusal needs a fixture.
    ApplicationRow(
        "app_09", "cand_09", "job_clinical_data", Stage.RECRUITER_SCREEN, ApplicationStatus.ON_HOLD, "coord_reese"
    ),
    ApplicationRow(
        "app_10", "cand_10", "job_qa_automation", Stage.TECHNICAL_SCREEN, ApplicationStatus.ACTIVE, "coord_reese"
    ),
    ApplicationRow("app_11", "cand_11", "job_qa_automation", Stage.APPLIED, ApplicationStatus.ACTIVE, "coord_reese"),
    # Withdrawn but still sitting at APPLIED: stage and status are independent axes, and a stage
    # filter that quietly implies "active" would be wrong.
    ApplicationRow("app_12", "cand_12", "job_qa_automation", Stage.APPLIED, ApplicationStatus.WITHDRAWN, "coord_lane"),
)

PLANS: tuple[PlanRow, ...] = (
    PlanRow(
        id="plan_pe_recruiter",
        job_id="job_platform_eng",
        stage=Stage.RECRUITER_SCREEN,
        interview_type=InterviewType.RECRUITER_SCREEN,
        duration_minutes=30,
        panel_size=1,
        minimum_lead_hours=24,
        buffer_before=10,
        buffer_after=10,
        policy=WorkingHoursPolicy.CANDIDATE_BUSINESS_HOURS,
        active=True,
        skills=(),
    ),
    PlanRow(
        id="plan_pe_technical",
        job_id="job_platform_eng",
        stage=Stage.TECHNICAL_SCREEN,
        interview_type=InterviewType.TECHNICAL_SCREEN,
        duration_minutes=60,
        panel_size=1,
        minimum_lead_hours=48,
        buffer_before=15,
        buffer_after=15,
        policy=WorkingHoursPolicy.BOTH_BUSINESS_HOURS,
        active=True,
        skills=(("python", 1),),
    ),
    PlanRow(
        id="plan_pe_onsite",
        job_id="job_platform_eng",
        stage=Stage.ONSITE,
        interview_type=InterviewType.ONSITE_PANEL,
        duration_minutes=90,
        panel_size=3,
        minimum_lead_hours=72,
        buffer_before=15,
        buffer_after=30,
        policy=WorkingHoursPolicy.BOTH_BUSINESS_HOURS,
        active=True,
        skills=(("python", 1), ("system_design", 1), ("hiring_manager", 1)),
    ),
    # Superseded, retained. Proves the "one active plan per (job, stage)" index is partial: this
    # row shares (job_platform_eng, onsite) with the active plan above and must coexist.
    PlanRow(
        id="plan_pe_onsite_v0",
        job_id="job_platform_eng",
        stage=Stage.ONSITE,
        interview_type=InterviewType.ONSITE_PANEL,
        duration_minutes=60,
        panel_size=2,
        minimum_lead_hours=48,
        buffer_before=10,
        buffer_after=10,
        policy=WorkingHoursPolicy.BOTH_BUSINESS_HOURS,
        active=False,
        skills=(("python", 1),),
    ),
    PlanRow(
        id="plan_cd_recruiter",
        job_id="job_clinical_data",
        stage=Stage.RECRUITER_SCREEN,
        interview_type=InterviewType.RECRUITER_SCREEN,
        duration_minutes=30,
        panel_size=1,
        minimum_lead_hours=24,
        buffer_before=10,
        buffer_after=10,
        policy=WorkingHoursPolicy.CANDIDATE_BUSINESS_HOURS,
        active=True,
        skills=(),
    ),
    PlanRow(
        id="plan_cd_technical",
        job_id="job_clinical_data",
        stage=Stage.TECHNICAL_SCREEN,
        interview_type=InterviewType.TECHNICAL_SCREEN,
        duration_minutes=45,
        panel_size=2,
        minimum_lead_hours=48,
        buffer_before=15,
        buffer_after=15,
        policy=WorkingHoursPolicy.BOTH_BUSINESS_HOURS,
        active=True,
        skills=(("clinical_data", 1), ("sql", 1)),
    ),
    PlanRow(
        id="plan_qa_technical",
        job_id="job_qa_automation",
        stage=Stage.TECHNICAL_SCREEN,
        interview_type=InterviewType.TECHNICAL_SCREEN,
        duration_minutes=60,
        panel_size=1,
        minimum_lead_hours=24,
        buffer_before=10,
        buffer_after=10,
        policy=WorkingHoursPolicy.INTERVIEWER_BUSINESS_HOURS,
        active=True,
        skills=(("test_automation", 1),),
    ),
)

# Deliberately uneven. A scheduler that only works when everyone shares one wide block is untested.
INTERVIEWER_AVAILABILITY: tuple[WindowRow, ...] = (
    WindowRow("int_avery", 0, 13, 21),
    WindowRow("int_avery", 1, 13, 21),
    WindowRow("int_avery", 2, 13, 17),
    WindowRow("int_avery", 3, 13, 21),
    WindowRow("int_avery", 4, 13, 17),
    WindowRow("int_blake", 0, 14, 22),
    WindowRow("int_blake", 1, 14, 22),
    WindowRow("int_blake", 2, 14, 22),
    WindowRow("int_blake", 3, 16, 22),
    WindowRow("int_casey", 0, 8, 16),
    WindowRow("int_casey", 1, 8, 16),
    WindowRow("int_casey", 2, 8, 16),
    WindowRow("int_casey", 3, 8, 12),
    WindowRow("int_casey", 4, 8, 16),
    WindowRow("int_devon", 0, 16, 24),
    WindowRow("int_devon", 1, 16, 24),
    WindowRow("int_devon", 2, 16, 24),
    WindowRow("int_devon", 4, 16, 20),
    WindowRow("int_ellis", 0, 14, 22),
    WindowRow("int_ellis", 2, 14, 22),
    WindowRow("int_ellis", 4, 14, 18),
    WindowRow("int_frankie", 1, 9, 17),
)

# Sparser than interviewer time, which is the realistic constraint.
CANDIDATE_AVAILABILITY: tuple[WindowRow, ...] = (
    WindowRow("cand_01", 1, 14, 18),
    WindowRow("cand_01", 3, 14, 20),
    WindowRow("cand_02", 0, 15, 19),
    WindowRow("cand_02", 2, 15, 19),
    WindowRow("cand_03", 2, 13, 17),
    WindowRow("cand_03", 4, 13, 17),
    WindowRow("cand_05", 1, 17, 21),
    WindowRow("cand_06", 1, 16, 20),
    WindowRow("cand_06", 3, 16, 20),
    WindowRow("cand_07", 0, 9, 13),
    WindowRow("cand_07", 2, 9, 13),
    WindowRow("cand_10", 1, 9, 15),
    WindowRow("cand_10", 3, 9, 15),
    # Sydney and Tokyo: their business hours barely intersect US ones, which is exactly the case
    # that makes timezone handling worth testing.
    WindowRow("cand_08", 2, 22, 24),
    WindowRow("cand_11", 2, 23, 24),
)

# Existing commitments -- what §9.2 step 5 subtracts. These overlap otherwise-free interviewer
# time on purpose; a commitment nobody is free for would prove nothing.
INTERVIEWS: tuple[InterviewRow, ...] = (
    InterviewRow(
        "iv_completed_pe_tech",
        "app_01",
        "plan_pe_technical",
        -7,
        15,
        0,
        60,
        InterviewStatus.COMPLETED,
        "America/Indiana/Indianapolis",
        ("int_avery", "int_blake"),
    ),
    InterviewRow(
        "iv_booked_pe_tech",
        "app_02",
        "plan_pe_technical",
        1,
        15,
        0,
        60,
        InterviewStatus.SCHEDULED,
        "America/New_York",
        ("int_avery",),
    ),
    InterviewRow(
        "iv_booked_cd_tech",
        "app_06",
        "plan_cd_technical",
        2,
        17,
        30,
        45,
        InterviewStatus.SCHEDULED,
        "America/New_York",
        ("int_devon", "int_blake"),
    ),
    # Cancelled interviews do not block time (§9.3): this slot must come back as bookable.
    InterviewRow(
        "iv_cancelled_qa",
        "app_10",
        "plan_qa_technical",
        1,
        10,
        0,
        60,
        InterviewStatus.CANCELLED,
        "Europe/London",
        ("int_casey",),
    ),
)

# One submitted, one still open, so reporting has both a satisfied and an unsatisfied obligation.
FEEDBACK: tuple[FeedbackRow, ...] = (
    FeedbackRow("fb_pe_avery", "iv_completed_pe_tech", "int_avery", -5, FeedbackStatus.SUBMITTED),
    FeedbackRow("fb_pe_blake", "iv_completed_pe_tech", "int_blake", -5, FeedbackStatus.PENDING),
)

_CANDIDATE_BY_APPLICATION = {row.id: row.candidate_id for row in APPLICATIONS}


# --------------------------------------------------------------------------- construction


def _build(anchor: datetime) -> list[Base]:
    """Every seed row as ORM objects, parents before children.

    Order matters: the foreign-key pragma is on, so a child merged before its parent would fail.
    """
    created = at(anchor, -30, 12)
    rows: list[Base] = []

    for job in JOBS:
        rows.append(
            Job(
                id=job.id,
                title=job.title,
                department=job.department,
                location=job.location,
                default_timezone=job.timezone,
                status=job.status,
                created_at=created,
                updated_at=created,
            )
        )

    for person in CANDIDATES:
        rows.append(
            Candidate(
                id=person.id,
                first_name=person.first,
                last_name=person.last,
                email=f"{person.first.lower()}.{person.last.lower()}@{EMAIL_DOMAIN}",
                phone=person.phone,
                timezone=person.timezone,
                preferred_contact_method=person.contact,
                created_at=created,
                updated_at=created,
            )
        )

    for panellist in INTERVIEWERS:
        given, _, family = panellist.name.partition(" ")
        rows.append(
            Interviewer(
                id=panellist.id,
                name=panellist.name,
                email=f"{given.lower()}.{family.lower()}@{EMAIL_DOMAIN}",
                timezone=panellist.timezone,
                active=panellist.active,
            )
        )
        rows.extend(InterviewerSkill(interviewer_id=panellist.id, skill_code=code) for code in panellist.skills)

    for application in APPLICATIONS:
        rows.append(
            Application(
                id=application.id,
                candidate_id=application.candidate_id,
                job_id=application.job_id,
                current_stage=application.stage,
                status=application.status,
                coordinator_id=application.coordinator,
                last_activity_at=at(anchor, -3, 12),
                created_at=created,
                updated_at=created,
            )
        )

    for plan in PLANS:
        rows.append(
            InterviewPlan(
                id=plan.id,
                job_id=plan.job_id,
                stage=plan.stage,
                interview_type=plan.interview_type,
                duration_minutes=plan.duration_minutes,
                panel_size=plan.panel_size,
                minimum_lead_hours=plan.minimum_lead_hours,
                buffer_before_minutes=plan.buffer_before,
                buffer_after_minutes=plan.buffer_after,
                working_hours_policy=plan.policy,
                active=plan.active,
            )
        )
        rows.extend(
            InterviewPlanSkillRequirement(interview_plan_id=plan.id, skill_code=code, minimum_interviewers=count)
            for code, count in plan.skills
        )

    for owner_type, windows in (
        (PartyType.INTERVIEWER, INTERVIEWER_AVAILABILITY),
        (PartyType.CANDIDATE, CANDIDATE_AVAILABILITY),
    ):
        rows.extend(
            AvailabilityWindow(
                id=f"avail_{window.owner_id}_{window.day}_{window.start_hour}",
                owner_type=owner_type,
                owner_id=window.owner_id,
                start_utc=at(anchor, window.day, window.start_hour),
                end_utc=at(anchor, window.day, window.end_hour),
                source=AvailabilitySource.DECLARED,
            )
            for window in windows
        )

    for booking in INTERVIEWS:
        start = at(anchor, booking.day, booking.hour, booking.minute)
        rows.append(
            Interview(
                id=booking.id,
                application_id=booking.application_id,
                interview_plan_id=booking.plan_id,
                start_utc=start,
                end_utc=start + timedelta(minutes=booking.duration_minutes),
                display_timezone=booking.display_timezone,
                status=booking.status,
                idempotency_key=f"seed_{booking.id}",
                created_at=created,
            )
        )
        rows.append(
            InterviewAttendee(
                interview_id=booking.id,
                attendee_type=PartyType.CANDIDATE,
                attendee_id=_CANDIDATE_BY_APPLICATION[booking.application_id],
                response_status=ResponseStatus.ACCEPTED,
            )
        )
        rows.extend(
            InterviewAttendee(
                interview_id=booking.id,
                attendee_type=PartyType.INTERVIEWER,
                attendee_id=panellist_id,
                response_status=ResponseStatus.ACCEPTED,
            )
            for panellist_id in booking.panel
        )

    rows.extend(
        FeedbackRequest(
            id=request.id,
            interview_id=request.interview_id,
            interviewer_id=request.interviewer_id,
            due_at=at(anchor, request.due_day, 23),
            submitted_at=(at(anchor, request.due_day - 1, 14) if request.status is FeedbackStatus.SUBMITTED else None),
            status=request.status,
        )
        for request in FEEDBACK
    )

    return rows


async def _count(session: AsyncSession, model: type[Base]) -> int:
    return int((await session.execute(select(func.count()).select_from(model))).scalar_one())


async def seed(session: AsyncSession, anchor: datetime = SEED_ANCHOR) -> SeedSummary:
    """Merge the demo dataset into ``session`` and report what is now there.

    Uses ``merge`` rather than ``add`` so a second run updates in place instead of colliding on
    the primary key. The caller owns the transaction -- nothing here commits, so a failure
    partway leaves no rows behind.
    """
    if anchor.tzinfo is None:
        raise ValueError("anchor must be timezone-aware; a naive one cannot be resolved to UTC")

    for row in _build(anchor):
        await session.merge(row)
    await session.flush()

    return SeedSummary(
        jobs=await _count(session, Job),
        candidates=await _count(session, Candidate),
        applications=await _count(session, Application),
        interview_plans=await _count(session, InterviewPlan),
        interviewers=await _count(session, Interviewer),
        availability_windows=await _count(session, AvailabilityWindow),
        interviews=await _count(session, Interview),
        feedback_requests=await _count(session, FeedbackRequest),
    )
