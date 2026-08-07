"""The three properties the seed claims: fictional, deterministic, idempotent.

The polymorphic-reference test earns its place specially. ``AvailabilityWindow.owner_id`` and
``InterviewAttendee.attendee_id`` carry no foreign key, by design, so the database cannot catch a
seed that points at a candidate id that does not exist. Nothing else would notice until the
scheduler returned an empty panel for no visible reason.
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from talent_ops_mcp.db.schema import (
    Application,
    AvailabilityWindow,
    Candidate,
    FeedbackRequest,
    Interview,
    InterviewAttendee,
    Interviewer,
    InterviewPlan,
    Job,
)
from talent_ops_mcp.db.seed import SEED_ANCHOR, seed
from talent_ops_mcp.models.domain import (
    ApplicationStatus,
    FeedbackStatus,
    InterviewStatus,
    JobStatus,
    PartyType,
)


async def test_seed_produces_the_documented_dataset(seeded_session: AsyncSession) -> None:
    """The §12 Phase 2 shopping list: three jobs, twelve candidates, six interviewers."""
    summary = await seed(seeded_session)
    assert summary.jobs == 3
    assert summary.candidates == 12
    assert summary.interviewers == 6
    assert summary.applications == 12
    assert summary.interviews == 4
    assert summary.feedback_requests == 2


async def test_seed_is_idempotent(session_factory: async_sessionmaker[AsyncSession]) -> None:
    async with session_factory() as first_run:
        first = await seed(first_run)
        await first_run.commit()
    async with session_factory() as second_run:
        second = await seed(second_run)
        await second_run.commit()
    async with session_factory() as third_run:
        third = await seed(third_run)
        await third_run.commit()
    assert first == second == third


async def test_reseeding_repairs_an_edited_row(session_factory: async_sessionmaker[AsyncSession]) -> None:
    """Merge-by-primary-key means the seed is also the repair tool."""
    async with session_factory() as setup:
        await seed(setup)
        await setup.commit()

    async with session_factory() as vandal:
        job = await vandal.get(Job, "job_platform_eng")
        assert job is not None
        job.title = "Chief Vibes Officer"
        await vandal.commit()

    async with session_factory() as repair:
        await seed(repair)
        await repair.commit()

    async with session_factory() as checking:
        job = await checking.get(Job, "job_platform_eng")
        assert job is not None
        assert job.title == "Senior Platform Engineer"


async def test_seed_is_deterministic_across_separate_databases(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """Two independent databases must end up byte-identical, not merely equal in row count."""
    from talent_ops_mcp.db.session import create_engine, create_session_factory, init_schema
    from talent_ops_mcp.settings import Settings

    async def fingerprint(directory: str) -> list[tuple[str, str, str, str]]:
        settings = Settings(env="test", data_dir=tmp_path_factory.mktemp(directory))
        engine = create_engine(settings)
        try:
            await init_schema(engine)
            factory = create_session_factory(engine)
            async with factory() as session:
                await seed(session)
                await session.commit()
                rows = (await session.execute(select(Interview).order_by(Interview.id))).scalars().all()
                return [(r.id, r.start_utc.isoformat(), r.end_utc.isoformat(), r.idempotency_key) for r in rows]
        finally:
            await engine.dispose()

    assert await fingerprint("one") == await fingerprint("two")


async def test_anchor_shifts_every_instant_by_the_same_offset(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """The knob must move the dataset, not reshape it: relative spacing is what the scheduling
    fixtures depend on."""
    from talent_ops_mcp.db.session import create_engine, create_session_factory, init_schema
    from talent_ops_mcp.settings import Settings

    async def starts(directory: str, anchor: datetime) -> list[datetime]:
        settings = Settings(env="test", data_dir=tmp_path_factory.mktemp(directory))
        engine = create_engine(settings)
        try:
            await init_schema(engine)
            factory = create_session_factory(engine)
            async with factory() as session:
                await seed(session, anchor=anchor)
                await session.commit()
                result = await session.execute(select(Interview.start_utc).order_by(Interview.id))
                return list(result.scalars().all())
        finally:
            await engine.dispose()

    shift = timedelta(days=14)
    base = await starts("base", SEED_ANCHOR)
    moved = await starts("moved", SEED_ANCHOR + shift)
    assert moved == [instant + shift for instant in base]


async def test_seed_rejects_a_naive_anchor(session: AsyncSession) -> None:
    with pytest.raises(ValueError, match="anchor must be timezone-aware"):
        await seed(session, anchor=datetime(2026, 9, 7))  # noqa: DTZ001 - the point of the test


# ------------------------------------------------------------------ fictional-data invariants


async def test_every_email_is_on_the_reserved_test_tld(seeded_session: AsyncSession) -> None:
    """RFC 2606 reserves ``.test`` so it can never resolve. A demo that could email a real person
    is the failure mode this guards."""
    candidates = (await seeded_session.execute(select(Candidate.email))).scalars().all()
    interviewers = (await seeded_session.execute(select(Interviewer.email))).scalars().all()
    assert candidates and interviewers
    for address in [*candidates, *interviewers]:
        assert address.endswith(".test"), address


async def test_every_stored_datetime_is_utc_aware(seeded_session: AsyncSession) -> None:
    windows = (await seeded_session.execute(select(AvailabilityWindow))).scalars().all()
    interviews = (await seeded_session.execute(select(Interview))).scalars().all()
    instants = [w.start_utc for w in windows] + [w.end_utc for w in windows]
    instants += [i.start_utc for i in interviews] + [i.end_utc for i in interviews]
    assert instants
    for instant in instants:
        assert instant.tzinfo is not None
        assert instant.utcoffset() == timedelta(0)


async def test_polymorphic_references_all_resolve(seeded_session: AsyncSession) -> None:
    """No foreign key covers these columns, so this test is the only thing standing between a
    typo'd owner id and a silently empty scheduling result."""
    candidate_ids = set((await seeded_session.execute(select(Candidate.id))).scalars().all())
    interviewer_ids = set((await seeded_session.execute(select(Interviewer.id))).scalars().all())
    known = {PartyType.CANDIDATE: candidate_ids, PartyType.INTERVIEWER: interviewer_ids}

    for window in (await seeded_session.execute(select(AvailabilityWindow))).scalars().all():
        assert window.owner_id in known[window.owner_type], f"dangling window owner {window.owner_id}"

    for attendee in (await seeded_session.execute(select(InterviewAttendee))).scalars().all():
        assert attendee.attendee_id in known[attendee.attendee_type], f"dangling attendee {attendee.attendee_id}"


async def test_availability_windows_are_well_formed(seeded_session: AsyncSession) -> None:
    windows = (await seeded_session.execute(select(AvailabilityWindow))).scalars().all()
    assert len(windows) > 20
    for window in windows:
        assert window.end_utc > window.start_utc


async def test_the_dataset_covers_the_cases_later_phases_need(seeded_session: AsyncSession) -> None:
    """The seed exists to exercise branches, not to look plentiful. Each assertion here names a
    rule that would otherwise have no fixture to fail against."""
    stages = set((await seeded_session.execute(select(Application.current_stage))).scalars().all())
    assert len(stages) >= 4, "scheduling is stage-driven; one stage tests nothing"

    statuses = set((await seeded_session.execute(select(Application.status))).scalars().all())
    assert {ApplicationStatus.ACTIVE, ApplicationStatus.ON_HOLD, ApplicationStatus.WITHDRAWN} <= statuses

    job_statuses = set((await seeded_session.execute(select(Job.status))).scalars().all())
    assert {JobStatus.OPEN, JobStatus.ON_HOLD} <= job_statuses

    inactive = (await seeded_session.execute(select(Interviewer).where(Interviewer.active.is_(False)))).scalars().all()
    assert inactive, "§9.2 step 3 filters on active; that filter needs something to exclude"

    interview_statuses = set((await seeded_session.execute(select(Interview.status))).scalars().all())
    assert {InterviewStatus.SCHEDULED, InterviewStatus.COMPLETED, InterviewStatus.CANCELLED} <= interview_statuses

    feedback = set((await seeded_session.execute(select(FeedbackRequest.status))).scalars().all())
    assert {FeedbackStatus.PENDING, FeedbackStatus.SUBMITTED} <= feedback


async def test_exactly_one_active_plan_exists_per_job_and_stage(seeded_session: AsyncSession) -> None:
    """Also proves the superseded plan really is retained rather than quietly dropped."""
    plans = (await seeded_session.execute(select(InterviewPlan))).scalars().all()
    active_keys = [(p.job_id, p.stage) for p in plans if p.active]
    assert len(active_keys) == len(set(active_keys))

    superseded = [p for p in plans if not p.active]
    assert superseded, "the partial index is only meaningful if an inactive duplicate exists"
    assert any((p.job_id, p.stage) in set(active_keys) for p in superseded)


async def test_seeded_interviews_reference_a_plan_belonging_to_their_job(seeded_session: AsyncSession) -> None:
    """A cross-job plan reference would pass every foreign key and still be nonsense."""
    rows = (
        await seeded_session.execute(
            select(Interview.id, Application.job_id, InterviewPlan.job_id)
            .join(Application, Interview.application_id == Application.id)
            .join(InterviewPlan, Interview.interview_plan_id == InterviewPlan.id)
        )
    ).all()
    assert rows
    for interview_id, application_job, plan_job in rows:
        assert application_job == plan_job, f"{interview_id} books a plan from another job"


async def test_anchor_default_is_a_monday(seeded_session: AsyncSession) -> None:
    """``at(anchor, 0..4)`` is documented as the working week, which only holds from a Monday."""
    assert SEED_ANCHOR.weekday() == 0
    assert SEED_ANCHOR.tzinfo is UTC
