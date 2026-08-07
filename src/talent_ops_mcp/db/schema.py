"""The thirteen tables of §8.1, with their constraints.

Constraints live in the database, not only in the services that write through them. A status
column with a ``CHECK`` cannot hold ``"activ"`` however the row arrived -- seed, repository,
future migration, or someone at a ``sqlite3`` prompt. §8.2 requires exactly that.

Two conventions worth knowing before reading:

* **Ids are prefixed strings** (``job_platform_eng``, ``interview_9f2c1a04b7de``) rather than
  autoincrementing integers. The seed can then use fixed, readable literals and stay
  byte-identical across runs, which is what makes its output safe to assert against.
* **Datetimes are UTC**, enforced by :class:`~talent_ops_mcp.db.base.UtcDateTime` via the
  declarative ``type_annotation_map``. Columns named ``*_utc`` are domain instants; ``created_at``
  and friends are bookkeeping. Both are aware.
"""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import CheckConstraint, ForeignKey, Index, String, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column, validates

from talent_ops_mcp.db.base import Base, enum_column, positive_duration
from talent_ops_mcp.models.domain import (
    ApplicationStatus,
    AuditResult,
    AvailabilitySource,
    ContactMethod,
    FeedbackStatus,
    InterviewStatus,
    InterviewType,
    JobStatus,
    PartyType,
    PreviewOperation,
    PreviewStatus,
    ResponseStatus,
    Stage,
    WorkingHoursPolicy,
)

ID = String(64)
SKILL_CODE = String(40)
TIMEZONE = String(64)


def validated_timezone(value: str) -> str:
    """Reject anything :mod:`zoneinfo` cannot resolve.

    A ``CHECK`` constraint cannot express "is a real IANA zone", and §9.3 forbids interpreting a
    candidate's time without one. Catching ``"America/Newyork"`` or ``"UTC+5"`` at write time
    turns a confusing scheduling-time failure into an obvious data-entry one.

    What this deliberately does *not* reject: the legacy fixed-offset aliases tzdata still ships
    -- ``EST``, ``MST``, ``HST`` all resolve. They are valid IANA names, so refusing them would be
    this function inventing policy the plan does not state. They are a footgun all the same:
    ``EST`` is permanently UTC-5 with no daylight saving, so a New York candidate stored as
    ``EST`` is an hour out for most of the year. Prefer ``America/New_York``. A future phase that
    wants region/city names enforced should do it as an explicit, documented rule -- and will
    have to exempt ``UTC``.
    """
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"not a valid IANA timezone: {value!r}") from exc
    return value


def _now() -> datetime:
    return datetime.now(UTC)


class Job(Base):
    """A requisition. Interview plans hang off it per stage."""

    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(ID, primary_key=True)
    title: Mapped[str]
    department: Mapped[str]
    location: Mapped[str]
    default_timezone: Mapped[str] = mapped_column(TIMEZONE)
    status: Mapped[JobStatus] = mapped_column(enum_column(JobStatus), default=JobStatus.OPEN)
    created_at: Mapped[datetime] = mapped_column(default=_now)
    updated_at: Mapped[datetime] = mapped_column(default=_now, onupdate=_now)

    @validates("default_timezone")
    def _check_timezone(self, _key: str, value: str) -> str:
        return validated_timezone(value)


class Candidate(Base):
    """A person in the pipeline.

    Carries only what coordination needs: how to reach them and what timezone to speak in.
    §8.2 keeps free-form notes out of the MVP entirely, so there is no column for a recruiter's
    impressions to accumulate in.
    """

    __tablename__ = "candidates"

    id: Mapped[str] = mapped_column(ID, primary_key=True)
    first_name: Mapped[str]
    last_name: Mapped[str]
    email: Mapped[str] = mapped_column(unique=True)
    phone: Mapped[str | None] = mapped_column(String(40), default=None)
    timezone: Mapped[str] = mapped_column(TIMEZONE)
    preferred_contact_method: Mapped[ContactMethod] = mapped_column(
        enum_column(ContactMethod), default=ContactMethod.EMAIL
    )
    created_at: Mapped[datetime] = mapped_column(default=_now)
    updated_at: Mapped[datetime] = mapped_column(default=_now, onupdate=_now)

    @validates("timezone")
    def _check_timezone(self, _key: str, value: str) -> str:
        return validated_timezone(value)


class Application(Base):
    """One candidate's pursuit of one job.

    The ``(candidate_id, job_id)`` uniqueness is a deliberate product rule: re-applying to the
    same requisition advances the existing application rather than forking a second history,
    which would make "the candidate's stage" ambiguous.
    """

    __tablename__ = "applications"
    __table_args__ = (
        UniqueConstraint("candidate_id", "job_id", name="uq_application_candidate_job"),
        Index("ix_applications_job_stage", "job_id", "current_stage"),
    )

    id: Mapped[str] = mapped_column(ID, primary_key=True)
    candidate_id: Mapped[str] = mapped_column(ForeignKey("candidates.id", ondelete="CASCADE"))
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"))
    current_stage: Mapped[Stage] = mapped_column(enum_column(Stage), default=Stage.APPLIED)
    status: Mapped[ApplicationStatus] = mapped_column(enum_column(ApplicationStatus), default=ApplicationStatus.ACTIVE)
    coordinator_id: Mapped[str] = mapped_column(ID)
    last_activity_at: Mapped[datetime] = mapped_column(default=_now)
    created_at: Mapped[datetime] = mapped_column(default=_now)
    updated_at: Mapped[datetime] = mapped_column(default=_now, onupdate=_now)


class InterviewPlan(Base):
    """The approved recipe for interviewing at one stage of one job.

    Everything the scheduler is allowed to assume lives here -- duration, panel size, buffers,
    lead time, working-hours policy -- so those numbers are approved data rather than constants
    a model or a service invented.

    Only one plan per ``(job, stage)`` may be active at a time. That is a *partial* unique index
    (``WHERE active = 1``) so superseded plans can be retained for history instead of deleted.
    """

    __tablename__ = "interview_plans"
    __table_args__ = (
        Index(
            "uq_active_plan_per_job_stage",
            "job_id",
            "stage",
            unique=True,
            sqlite_where=text("active = 1"),
        ),
        CheckConstraint("duration_minutes > 0", name="ck_plan_duration_positive"),
        CheckConstraint("panel_size >= 1", name="ck_plan_panel_size_positive"),
        CheckConstraint("minimum_lead_hours >= 0", name="ck_plan_lead_non_negative"),
        CheckConstraint("buffer_before_minutes >= 0", name="ck_plan_buffer_before_non_negative"),
        CheckConstraint("buffer_after_minutes >= 0", name="ck_plan_buffer_after_non_negative"),
    )

    id: Mapped[str] = mapped_column(ID, primary_key=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"))
    stage: Mapped[Stage] = mapped_column(enum_column(Stage))
    interview_type: Mapped[InterviewType] = mapped_column(enum_column(InterviewType))
    duration_minutes: Mapped[int]
    panel_size: Mapped[int]
    minimum_lead_hours: Mapped[int]
    buffer_before_minutes: Mapped[int]
    buffer_after_minutes: Mapped[int]
    working_hours_policy: Mapped[WorkingHoursPolicy] = mapped_column(enum_column(WorkingHoursPolicy))
    active: Mapped[bool] = mapped_column(default=True)


class InterviewPlanSkillRequirement(Base):
    """How many panellists must hold a given skill for a plan to be satisfiable (§9.2 step 9)."""

    __tablename__ = "interview_plan_skill_requirements"
    __table_args__ = (CheckConstraint("minimum_interviewers >= 1", name="ck_requirement_at_least_one"),)

    interview_plan_id: Mapped[str] = mapped_column(
        ForeignKey("interview_plans.id", ondelete="CASCADE"), primary_key=True
    )
    skill_code: Mapped[str] = mapped_column(SKILL_CODE, primary_key=True)
    minimum_interviewers: Mapped[int] = mapped_column(default=1)


class Interviewer(Base):
    """Someone who can sit on a panel. ``active`` gates eligibility (§9.2 step 3)."""

    __tablename__ = "interviewers"

    id: Mapped[str] = mapped_column(ID, primary_key=True)
    name: Mapped[str]
    email: Mapped[str] = mapped_column(unique=True)
    timezone: Mapped[str] = mapped_column(TIMEZONE)
    active: Mapped[bool] = mapped_column(default=True)

    @validates("timezone")
    def _check_timezone(self, _key: str, value: str) -> str:
        return validated_timezone(value)


class InterviewerSkill(Base):
    """What an interviewer is qualified to assess. Composite PK makes a duplicate impossible."""

    __tablename__ = "interviewer_skills"

    interviewer_id: Mapped[str] = mapped_column(ForeignKey("interviewers.id", ondelete="CASCADE"), primary_key=True)
    skill_code: Mapped[str] = mapped_column(SKILL_CODE, primary_key=True)


class AvailabilityWindow(Base):
    """A block of time its owner is *free*.

    Busy time is never stored. §9.2 derives it by subtracting existing interviews and their
    buffers from these windows, so there is exactly one representation of "when can this person
    meet" and no way for a free row and a busy row to contradict each other.

    ``owner_id`` is polymorphic over candidates and interviewers, so it carries no SQL foreign
    key. The alternative -- two nullable columns with a check that exactly one is set -- was
    rejected as more schema for the same guarantee; the repository resolves the reference and
    :func:`~talent_ops_mcp.db.seed.seed` only writes ids it just created.
    """

    __tablename__ = "availability_windows"
    __table_args__ = (
        positive_duration("start_utc", "end_utc"),
        Index("ix_availability_owner_window", "owner_type", "owner_id", "start_utc"),
    )

    id: Mapped[str] = mapped_column(ID, primary_key=True)
    owner_type: Mapped[PartyType] = mapped_column(enum_column(PartyType))
    owner_id: Mapped[str] = mapped_column(ID)
    start_utc: Mapped[datetime]
    end_utc: Mapped[datetime]
    source: Mapped[AvailabilitySource] = mapped_column(enum_column(AvailabilitySource))


class Interview(Base):
    """A booked interview.

    ``idempotency_key`` is unique and required. §10.2 creates interviews only inside a confirmed
    transaction, and this constraint is what makes a retried confirmation collide at the database
    instead of quietly booking the room twice.
    """

    __tablename__ = "interviews"
    __table_args__ = (
        positive_duration("start_utc", "end_utc"),
        Index("ix_interviews_window", "start_utc", "end_utc"),
        Index("ix_interviews_application", "application_id", "status"),
    )

    id: Mapped[str] = mapped_column(ID, primary_key=True)
    application_id: Mapped[str] = mapped_column(ForeignKey("applications.id", ondelete="CASCADE"))
    interview_plan_id: Mapped[str] = mapped_column(ForeignKey("interview_plans.id", ondelete="RESTRICT"))
    start_utc: Mapped[datetime]
    end_utc: Mapped[datetime]
    display_timezone: Mapped[str] = mapped_column(TIMEZONE)
    status: Mapped[InterviewStatus] = mapped_column(enum_column(InterviewStatus), default=InterviewStatus.SCHEDULED)
    external_event_id: Mapped[str | None] = mapped_column(default=None)
    idempotency_key: Mapped[str] = mapped_column(ID, unique=True)
    created_at: Mapped[datetime] = mapped_column(default=_now)

    @validates("display_timezone")
    def _check_timezone(self, _key: str, value: str) -> str:
        return validated_timezone(value)


class InterviewAttendee(Base):
    """Who is expected at an interview, and whether they have replied.

    ``attendee_id`` is polymorphic for the same reason as
    :attr:`AvailabilityWindow.owner_id`. The composite primary key means a person cannot be
    invited to the same interview twice.
    """

    __tablename__ = "interview_attendees"

    interview_id: Mapped[str] = mapped_column(ForeignKey("interviews.id", ondelete="CASCADE"), primary_key=True)
    attendee_type: Mapped[PartyType] = mapped_column(enum_column(PartyType), primary_key=True)
    attendee_id: Mapped[str] = mapped_column(ID, primary_key=True)
    response_status: Mapped[ResponseStatus] = mapped_column(
        enum_column(ResponseStatus), default=ResponseStatus.NEEDS_ACTION
    )


class FeedbackRequest(Base):
    """An interviewer's outstanding obligation to submit feedback.

    This server tracks *whether* feedback is outstanding, never its content. Storing the
    substance would put candidate evaluation inside a system whose stated boundary excludes it
    (§2.3), so there is no body column and Phase 6 reporting can only ever count and chase.
    """

    __tablename__ = "feedback_requests"
    __table_args__ = (UniqueConstraint("interview_id", "interviewer_id", name="uq_feedback_interview_interviewer"),)

    id: Mapped[str] = mapped_column(ID, primary_key=True)
    interview_id: Mapped[str] = mapped_column(ForeignKey("interviews.id", ondelete="CASCADE"))
    interviewer_id: Mapped[str] = mapped_column(ForeignKey("interviewers.id", ondelete="CASCADE"))
    due_at: Mapped[datetime]
    submitted_at: Mapped[datetime | None] = mapped_column(default=None)
    status: Mapped[FeedbackStatus] = mapped_column(enum_column(FeedbackStatus), default=FeedbackStatus.PENDING)


class ActionPreview(Base):
    """A minted, not-yet-confirmed state change (§10).

    Only the *hash* of the confirmation token is stored, so a reader of this table -- a backup, a
    log shipper, an attacker with file access -- cannot redeem anything. ``payload_hash`` binds
    the token to an exact proposal: if the application moves on between preview and confirm, the
    recomputed hash differs and the stale preview is refused rather than silently applied.
    """

    __tablename__ = "action_previews"
    __table_args__ = (Index("ix_previews_expiry", "status", "expires_at"),)

    id: Mapped[str] = mapped_column(ID, primary_key=True)
    operation: Mapped[PreviewOperation] = mapped_column(enum_column(PreviewOperation))
    actor_id: Mapped[str] = mapped_column(ID)
    payload_json: Mapped[str] = mapped_column(String(8000))
    payload_hash: Mapped[str] = mapped_column(String(64))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[datetime]
    used_at: Mapped[datetime | None] = mapped_column(default=None)
    status: Mapped[PreviewStatus] = mapped_column(enum_column(PreviewStatus), default=PreviewStatus.PENDING)
    created_at: Mapped[datetime] = mapped_column(default=_now)


class AuditEvent(Base):
    """Append-only record of what was attempted and what happened.

    Written for refusals and failures as well as successes -- an audit trail that only records
    what worked cannot answer the question it exists for. Nothing in the codebase updates or
    deletes a row here.
    """

    __tablename__ = "audit_events"
    __table_args__ = (Index("ix_audit_target", "target_type", "target_id", "created_at"),)

    id: Mapped[str] = mapped_column(ID, primary_key=True)
    request_id: Mapped[str] = mapped_column(ID)
    actor_id: Mapped[str] = mapped_column(ID)
    action: Mapped[str] = mapped_column(String(80))
    target_type: Mapped[str] = mapped_column(String(40))
    target_id: Mapped[str] = mapped_column(ID)
    result: Mapped[AuditResult] = mapped_column(enum_column(AuditResult))
    metadata_json: Mapped[str] = mapped_column(String(4000), default="{}")
    created_at: Mapped[datetime] = mapped_column(default=_now)
