"""The closed value sets of the recruiting domain.

Every one of these becomes a ``VARCHAR`` plus a ``CHECK`` constraint in SQLite
(:mod:`talent_ops_mcp.db.schema`), so an invalid status is rejected by the database and not
merely by the code path that happened to write it. ``StrEnum`` members compare equal to their
own strings, which keeps repository filters and seed literals readable.

Skills are deliberately *not* an enum. A skill catalogue is data that a recruiting team edits;
freezing it in code would mean a release to add "rust". Statuses are the opposite -- each new
value needs handling logic, so the type system should force that conversation.
"""

from enum import StrEnum


class JobStatus(StrEnum):
    """Whether a job is accepting interview activity."""

    OPEN = "open"
    ON_HOLD = "on_hold"
    CLOSED = "closed"


class Stage(StrEnum):
    """Position in the interview pipeline.

    Shared by :class:`~talent_ops_mcp.db.schema.Application` and
    :class:`~talent_ops_mcp.db.schema.InterviewPlan`: resolving "the plan for this application"
    is a join on (job, stage), so the two must draw from one vocabulary.
    """

    APPLIED = "applied"
    RECRUITER_SCREEN = "recruiter_screen"
    TECHNICAL_SCREEN = "technical_screen"
    ONSITE = "onsite"
    OFFER = "offer"


class ApplicationStatus(StrEnum):
    """Whether an application may be scheduled against.

    Only ``ACTIVE`` is schedulable; §9.2 step 1 validates this before doing any work.
    """

    ACTIVE = "active"
    ON_HOLD = "on_hold"
    WITHDRAWN = "withdrawn"
    CLOSED = "closed"


class ContactMethod(StrEnum):
    """How a candidate has asked to be contacted."""

    EMAIL = "email"
    PHONE = "phone"


class InterviewType(StrEnum):
    """Shape of an interview, independent of which pipeline stage uses it."""

    RECRUITER_SCREEN = "recruiter_screen"
    TECHNICAL_SCREEN = "technical_screen"
    ONSITE_PANEL = "onsite_panel"
    HIRING_MANAGER = "hiring_manager"


class WorkingHoursPolicy(StrEnum):
    """Whose business hours bound a proposed slot (§9.2 step 8)."""

    CANDIDATE_BUSINESS_HOURS = "candidate_business_hours"
    INTERVIEWER_BUSINESS_HOURS = "interviewer_business_hours"
    BOTH_BUSINESS_HOURS = "both_business_hours"
    EXTENDED_HOURS = "extended_hours"


class PartyType(StrEnum):
    """Which table an ``owner_id`` / ``attendee_id`` points at.

    These columns are polymorphic, so they carry no SQL foreign key; the repository layer
    resolves them. See :mod:`talent_ops_mcp.db.schema` for why that trade was made.
    """

    CANDIDATE = "candidate"
    INTERVIEWER = "interviewer"


class AvailabilitySource(StrEnum):
    """Provenance of a free window.

    Availability rows are *free* time. Busy time is derived by subtracting existing
    interviews (§9.2 step 5), never stored as a window, so there is no free/busy flag here.
    """

    DECLARED = "declared"
    """Stated directly by the person, e.g. a candidate replying with times that suit them."""

    WORKING_HOURS = "working_hours"
    """Generated from a recurring working-hours pattern."""

    IMPORTED = "imported"
    """Came from an external calendar. Unused until Phase 9."""


class InterviewStatus(StrEnum):
    """Lifecycle of a scheduled interview.

    ``SCHEDULED`` is the only status that blocks a slot; §9.3 says an attendee may not hold two
    overlapping *active* interviews, and cancelled ones free their time.
    """

    SCHEDULED = "scheduled"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class ResponseStatus(StrEnum):
    """An attendee's reply to the invitation."""

    NEEDS_ACTION = "needs_action"
    ACCEPTED = "accepted"
    DECLINED = "declined"
    TENTATIVE = "tentative"


class FeedbackStatus(StrEnum):
    """State of an interviewer's outstanding feedback obligation.

    ``OVERDUE`` is not stored by the seed or set by a background job -- nothing in this server
    runs on a timer. Phase 6 reporting derives lateness by comparing ``due_at`` to the request
    time; this member exists for an explicit waive/close workflow.
    """

    PENDING = "pending"
    SUBMITTED = "submitted"
    OVERDUE = "overdue"
    WAIVED = "waived"


class PreviewOperation(StrEnum):
    """Which confirmable operation a preview describes.

    One member today. It exists as an enum because §10.2 step 2 verifies the operation type
    when redeeming a token: a preview minted for one operation must not confirm another, and
    that check needs something to compare against.
    """

    SCHEDULE_INTERVIEW = "schedule_interview"


class PreviewStatus(StrEnum):
    """Whether a preview token may still be redeemed."""

    PENDING = "pending"
    USED = "used"
    CANCELLED = "cancelled"


class AuditResult(StrEnum):
    """Outcome recorded on an audit event.

    ``DENIED`` is kept distinct from ``FAILURE``: a refused unsafe request and a crash are
    different events, and conflating them would hide the first inside the noise of the second.
    """

    SUCCESS = "success"
    FAILURE = "failure"
    DENIED = "denied"
