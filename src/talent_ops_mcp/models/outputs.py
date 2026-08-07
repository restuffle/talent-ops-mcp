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

Every timestamp is timezone-aware UTC, and every entity that a human will be shown a time for
carries its own IANA zone (§8.2), so a caller never has to guess which clock a datetime belongs
to.
"""

from datetime import datetime

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
