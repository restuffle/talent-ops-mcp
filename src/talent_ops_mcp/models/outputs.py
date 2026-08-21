"""Structured returns for the read-only recruiting tools (§7.1, Phase 3).

``from __future__ import annotations`` is deliberately absent for the reason given in
:mod:`talent_ops_mcp.server`: mcp 2.0.0 derives ``output_schema`` from runtime introspection of
these models, and stringised annotations are an avoidable failure mode there.

**What is absent is the design.** §8.2 returns sensitive candidate fields "only when required by
the tool", so an email address appears in :class:`CandidateDetail` -- where contacting the person
is the point -- and nowhere else. There is no score, rating, rank, ordinal, match percentage,
seniority judgement, or any other field a reader could mistake for an assessment of a person;
§2.3 restricts this server to logistics, and a schema that cannot express a ranking cannot be
prompted into emitting one. Names are present because coordination is impossible without them,
but nothing derived from a name is.

Every timestamp is timezone-aware, and every entity that a human will be shown a time for carries
its own IANA zone (§8.2), so a caller never has to guess which clock a datetime belongs to. Fields
named ``*_utc`` are UTC; the two named ``*_local`` on :class:`InterviewSlotOption` are the same
instants in the candidate's display zone, carrying the offset that tells a repeated daylight-saving
hour apart from its twin (§9.3).
"""

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field

from talent_ops_mcp.models.domain import (
    ApplicationStatus,
    ContactMethod,
    InterviewType,
    JobStatus,
    Stage,
    WorkingHoursPolicy,
)


class PageMeta(BaseModel):
    """The four fields §7.1.1 requires on every list tool.

    ``total`` and ``has_more`` are the additions the ``/mcp-builder`` review asked for. Items
    plus a cursor tells a model *that* it could continue; it does not tell it whether continuing
    is worth a round trip, so a model either stops early on a partial answer or pages to
    exhaustion to find out. These two make that decision cheap.
    """

    total: int = Field(description="Rows matching the filters, ignoring pagination.")
    count: int = Field(description="Rows in this page. Never larger than the effective limit.")
    has_more: bool = Field(description="True when rows remain after this page.")
    next_cursor: str | None = Field(
        description=(
            "Opaque token that fetches the next page. Pass it back unchanged as `cursor`. "
            "Null when has_more is false. Do not parse, construct, or do arithmetic on it."
        )
    )


class JobSummary(BaseModel):
    """A requisition as a search result.

    ``default_timezone`` is the zone interviews for this job are quoted in unless a participant's
    own zone overrides it, and it is included here rather than only on the detail read because a
    coordinator scanning results needs to know which clock the role runs on.
    """

    id: str = Field(description="Stable job identifier. Pass to other tools as job_id.")
    title: str
    department: str
    location: str
    default_timezone: str = Field(description="IANA timezone, e.g. America/New_York.")
    status: JobStatus = Field(description="Only `open` jobs accept new interview activity.")


class JobSearchResult(PageMeta):
    """One page of matching jobs."""

    jobs: list[JobSummary]


class ApplicationSummary(BaseModel):
    """One candidate's pursuit of one job, flattened for triage.

    ``candidate_name`` and ``job_title`` are denormalised onto the row deliberately: the
    alternative is the model issuing a ``get_candidate`` call per result purely to render a list,
    which costs a round trip each and pulls contact details it was not asked for. No contact
    field is included -- for those, call ``talent_ops_get_candidate`` for the one person you
    actually need.
    """

    id: str = Field(description="Stable application identifier.")
    candidate_id: str
    candidate_name: str = Field(description="Display name only. Never a basis for any judgement about the person.")
    job_id: str
    job_title: str
    current_stage: Stage
    status: ApplicationStatus = Field(description="Only `active` applications may be scheduled against.")
    awaiting_scheduling: bool = Field(
        description=(
            "True when this application is active, its stage has an approved interview plan, and "
            "no interview is currently scheduled against it -- that is, it needs coordination work."
        )
    )
    last_activity_at: datetime = Field(description="UTC. Most recently updated applications sort first.")


class ApplicationListResult(PageMeta):
    """One page of matching applications."""

    applications: list[ApplicationSummary]


class CandidateApplicationContext(BaseModel):
    """A candidate's position in one pipeline, as shown on their detail record."""

    application_id: str
    job_id: str
    job_title: str
    current_stage: Stage
    status: ApplicationStatus
    awaiting_scheduling: bool
    last_activity_at: datetime = Field(description="UTC.")


class CandidateDetail(BaseModel):
    """One candidate with the context needed to contact and schedule them.

    This is the only output model carrying contact details, and it is reached only by explicit
    id -- there is no tool that returns email addresses in bulk.
    """

    id: str
    first_name: str
    last_name: str
    email: str = Field(description="Demo dataset uses .test addresses, which cannot receive mail.")
    phone: str | None = Field(description="Null when the candidate gave none.")
    timezone: str = Field(description="IANA timezone. Quote every proposed time to the candidate in this zone.")
    preferred_contact_method: ContactMethod
    applications: list[CandidateApplicationContext] = Field(
        description="Every application this candidate holds. Not paginated: the count is small by nature."
    )


class SkillRequirement(BaseModel):
    """How many panellists must hold one skill for a plan to be satisfiable."""

    skill_code: str
    minimum_interviewers: int


class InterviewPlanDetail(BaseModel):
    """The approved recipe for interviewing at one stage of one job.

    Every number the scheduler is permitted to assume comes from here. A duration or lead time
    invented by a model instead of read from this record is out of policy, however reasonable it
    looks.
    """

    id: str
    job_id: str
    job_title: str
    default_timezone: str = Field(description="IANA timezone the job runs in.")
    stage: Stage
    interview_type: InterviewType
    duration_minutes: int
    panel_size: int = Field(description="Number of interviewers required on the panel.")
    minimum_lead_hours: int = Field(description="An interview may not start sooner than this many hours from now.")
    buffer_before_minutes: int
    buffer_after_minutes: int
    working_hours_policy: WorkingHoursPolicy = Field(description="Whose business hours bound a proposed slot.")
    skill_requirements: list[SkillRequirement] = Field(
        description="Empty when the plan demands no specific skills. All requirements must be met together."
    )


class SlotUnavailableReason(StrEnum):
    """Why a proposal search returned nothing (§12 Phase 4, "a clear reason when no slot exists").

    A code as well as a sentence, because the two are for different readers. The sentence tells a
    model what to say and what to try next; the code is what a test can assert on and what a
    future client can branch on without matching prose. Every member names a *different* fix, which
    is the point -- "no slots" alone leaves a caller guessing between widening the dates, chasing
    the candidate for availability, and telling a recruiter the panel cannot be staffed.

    Not a :mod:`~talent_ops_mcp.models.domain` member: nothing here is ever written to a column, so
    it carries no ``CHECK`` constraint and no migration cost.

    None of these names anyone. §8.2 keeps other people's calendars private, so a reason may say
    that no panel was free, never who was busy or what they were doing.
    """

    NO_ELIGIBLE_INTERVIEWERS = "NO_ELIGIBLE_INTERVIEWERS"
    """No panel of the required size and skills exists among active interviewers, at any time."""

    OUTSIDE_LEAD_TIME = "OUTSIDE_LEAD_TIME"
    """The requested range ends before the plan's minimum lead time has elapsed."""

    NO_CANDIDATE_AVAILABILITY = "NO_CANDIDATE_AVAILABILITY"
    """The candidate declared no free block long enough for this interview inside the range."""

    NO_INTERVIEWER_AVAILABILITY = "NO_INTERVIEWER_AVAILABILITY"
    """No eligible interviewer declared any availability inside the range."""

    ATTENDEE_CONFLICT = "ATTENDEE_CONFLICT"
    """The candidate is already booked across every time that would otherwise have worked."""

    OUTSIDE_WORKING_HOURS = "OUTSIDE_WORKING_HOURS"
    """The candidate's free time falls outside the working hours this plan requires."""

    NO_AVAILABLE_PANEL = "NO_AVAILABLE_PANEL"
    """Fewer than the required number of interviewers were free at any one candidate-compatible time."""

    PANEL_COVERAGE = "PANEL_COVERAGE"
    """Enough interviewers were free, but never a combination covering every required skill."""


class ProposedPanellist(BaseModel):
    """One interviewer on a proposed panel.

    Name and timezone only. No email -- an invitation is Phase 5's job and goes out through a
    confirmed operation, not through whatever the model does with a proposal -- and nothing
    evaluative, because the engine picks panels positionally and this schema must not suggest
    otherwise (§2.3).
    """

    id: str = Field(description="Stable interviewer identifier.")
    name: str = Field(description="Display name only. Never a basis for any judgement about the person.")
    timezone: str = Field(description="IANA timezone. Quote the time to this panellist in their own zone.")


class InterviewSlotOption(BaseModel):
    """One conflict-free time this interview could be held.

    A proposal, not a hold: §9.3 is explicit that options reserve nothing, so two of these may be
    valid now and only one bookable later. Phase 5's confirmation re-checks every conflict inside
    the write transaction rather than trusting this.
    """

    start_utc: datetime = Field(description="UTC start. This is the instant; the local fields are the same one.")
    end_utc: datetime = Field(description="UTC end.")
    start_local: datetime = Field(
        description=(
            "The same instant as start_utc, expressed in display_timezone with its UTC offset. "
            "Quote this to the candidate; the offset is what disambiguates a daylight-saving repeat."
        )
    )
    end_local: datetime = Field(description="The same instant as end_utc, expressed in display_timezone.")
    display_timezone: str = Field(description="IANA timezone the local fields are expressed in.")
    duration_minutes: int = Field(description="From the approved plan. Never adjusted to make a slot fit.")
    panel: list[ProposedPanellist] = Field(
        description="The interviewers this option assumes. Panel membership is part of the option, not a suggestion."
    )


class SlotProposalResult(BaseModel):
    """Proposed interview times for one application, or the reason there are none.

    Carries the plan and identifier context the next step needs, so preparing a preview does not
    require re-reading the application, the plan, and the candidate to find out what was proposed.
    """

    application_id: str
    candidate_id: str
    candidate_name: str = Field(description="Display name only. Never a basis for any judgement about the person.")
    job_id: str
    job_title: str
    stage: Stage = Field(description="Pipeline stage the plan applies to.")
    interview_plan_id: str = Field(description="The approved plan every option was computed from.")
    duration_minutes: int
    panel_size: int = Field(description="Interviewers each option must include.")
    display_timezone: str = Field(description="IANA timezone the local times are expressed in.")
    searched_from_utc: datetime = Field(
        description="Start of the window actually searched. Later than the requested start when lead time bit."
    )
    searched_to_utc: datetime = Field(description="End of the window actually searched.")
    options: list[InterviewSlotOption] = Field(
        description="Ordered earliest first. Empty when no valid time exists — see no_options_reason."
    )
    more_options_exist: bool = Field(
        description=(
            "True when valid times remain beyond max_options. Raise max_options or narrow the range to see them."
        )
    )
    why_these_are_valid: str | None = Field(
        description=(
            "What every option was checked against, in words you can repeat to a human. "
            "Null when options is empty. Says nothing about anyone's private calendar."
        )
    )
    no_options_reason: str | None = Field(
        description="Why nothing could be proposed, and what to change. Null when options is non-empty."
    )
    no_options_code: SlotUnavailableReason | None = Field(
        description="Stable code for no_options_reason, safe to branch on. Null when options is non-empty."
    )
