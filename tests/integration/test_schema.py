"""The schema's promises, exercised against a real SQLite file.

These tests assert on *database* behaviour, not on the Python objects in front of it. Several
deliberately bypass the ORM with raw SQL: a constraint that only holds when you go through
SQLAlchemy is not a constraint, and the ORM's own validation would mask its absence.
"""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import inspect, select, text
from sqlalchemy.exc import IntegrityError, StatementError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from talent_ops_mcp.db.base import Base
from talent_ops_mcp.db.schema import (
    Application,
    AvailabilityWindow,
    Candidate,
    Interview,
    InterviewPlan,
    Job,
    validated_timezone,
)
from talent_ops_mcp.models.domain import (
    ApplicationStatus,
    AvailabilitySource,
    InterviewType,
    PartyType,
    Stage,
    WorkingHoursPolicy,
)
from talent_ops_mcp.settings import Settings

ANCHOR = datetime(2026, 9, 7, 12, tzinfo=UTC)


async def _job(session: AsyncSession, job_id: str = "job_1") -> Job:
    job = Job(
        id=job_id,
        title="Engineer",
        department="Engineering",
        location="Remote",
        default_timezone="America/New_York",
    )
    session.add(job)
    await session.flush()
    return job


async def _candidate(session: AsyncSession, candidate_id: str = "cand_1", email: str = "a.b@x.test") -> Candidate:
    candidate = Candidate(id=candidate_id, first_name="A", last_name="B", email=email, timezone="America/New_York")
    session.add(candidate)
    await session.flush()
    return candidate


# ------------------------------------------------------------------ creation and teardown


async def test_create_all_builds_every_table(initialized_engine: AsyncEngine) -> None:
    async with initialized_engine.connect() as conn:
        tables = await conn.run_sync(lambda sync: set(inspect(sync).get_table_names()))
    assert tables == set(Base.metadata.tables)


async def test_init_schema_is_idempotent(initialized_engine: AsyncEngine) -> None:
    """Running it twice must not raise -- the server calls it on every startup."""
    from talent_ops_mcp.db.session import init_schema

    await init_schema(initialized_engine)
    async with initialized_engine.connect() as conn:
        tables = await conn.run_sync(lambda sync: set(inspect(sync).get_table_names()))
    assert tables == set(Base.metadata.tables)


async def test_drop_all_removes_every_table(initialized_engine: AsyncEngine) -> None:
    async with initialized_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    async with initialized_engine.connect() as conn:
        tables = await conn.run_sync(lambda sync: inspect(sync).get_table_names())
    assert tables == []


# ------------------------------------------------------------------ referential integrity


async def test_foreign_keys_are_actually_enforced(session: AsyncSession) -> None:
    """The pragma test. SQLite ignores foreign keys unless told otherwise, so without
    ``_enforce_sqlite_pragmas`` this insert would succeed and every other FK test would be
    passing for the wrong reason."""
    await _candidate(session)
    session.add(
        Application(
            id="app_1",
            candidate_id="cand_1",
            job_id="job_does_not_exist",
            coordinator_id="coord",
            last_activity_at=ANCHOR,
        )
    )
    with pytest.raises(IntegrityError, match="FOREIGN KEY"):
        await session.flush()


async def test_cascade_delete_removes_dependent_rows(session_factory: async_sessionmaker[AsyncSession]) -> None:
    async with session_factory() as setup:
        await _job(setup)
        await _candidate(setup)
        setup.add(
            Application(
                id="app_1",
                candidate_id="cand_1",
                job_id="job_1",
                coordinator_id="coord",
                last_activity_at=ANCHOR,
            )
        )
        await setup.commit()

    async with session_factory() as acting:
        job = await acting.get(Job, "job_1")
        assert job is not None
        await acting.delete(job)
        await acting.commit()

    async with session_factory() as checking:
        remaining = (await checking.execute(select(Application))).scalars().all()
        assert remaining == []


async def test_interview_plan_cannot_be_deleted_while_an_interview_references_it(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """``ondelete="RESTRICT"``: losing the plan would strip an interview of the approved rules
    it was booked under, so the delete is refused rather than cascaded."""
    async with session_factory() as setup:
        await _job(setup)
        await _candidate(setup)
        setup.add_all(
            [
                Application(
                    id="app_1",
                    candidate_id="cand_1",
                    job_id="job_1",
                    coordinator_id="coord",
                    last_activity_at=ANCHOR,
                ),
                InterviewPlan(
                    id="plan_1",
                    job_id="job_1",
                    stage=Stage.ONSITE,
                    interview_type=InterviewType.ONSITE_PANEL,
                    duration_minutes=60,
                    panel_size=1,
                    minimum_lead_hours=1,
                    buffer_before_minutes=0,
                    buffer_after_minutes=0,
                    working_hours_policy=WorkingHoursPolicy.EXTENDED_HOURS,
                ),
            ]
        )
        await setup.flush()
        setup.add(
            Interview(
                id="iv_1",
                application_id="app_1",
                interview_plan_id="plan_1",
                start_utc=ANCHOR,
                end_utc=ANCHOR + timedelta(hours=1),
                display_timezone="America/New_York",
                idempotency_key="k1",
            )
        )
        await setup.commit()

    async with session_factory() as acting:
        plan = await acting.get(InterviewPlan, "plan_1")
        assert plan is not None
        await acting.delete(plan)
        with pytest.raises(IntegrityError):
            await acting.commit()


# ------------------------------------------------------------------ uniqueness and status


async def test_duplicate_candidate_email_is_rejected(session: AsyncSession) -> None:
    await _candidate(session, "cand_1", "same@x.test")
    session.add(Candidate(id="cand_2", first_name="C", last_name="D", email="same@x.test", timezone="UTC"))
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_a_candidate_may_not_apply_to_one_job_twice(session: AsyncSession) -> None:
    await _job(session)
    await _candidate(session)
    session.add_all(
        [
            Application(
                id="app_1",
                candidate_id="cand_1",
                job_id="job_1",
                coordinator_id="coord",
                last_activity_at=ANCHOR,
            ),
            Application(
                id="app_2",
                candidate_id="cand_1",
                job_id="job_1",
                coordinator_id="coord",
                last_activity_at=ANCHOR,
            ),
        ]
    )
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_status_check_constraint_rejects_an_unknown_value(session: AsyncSession) -> None:
    """Raw SQL on purpose. The ORM's ``validate_strings`` would catch this in Python; this test
    is about whether the *database* would catch it if something else wrote the row."""
    with pytest.raises(IntegrityError, match="CHECK constraint failed"):
        await session.execute(
            text(
                "INSERT INTO jobs (id, title, department, location, default_timezone, status,"
                " created_at, updated_at)"
                " VALUES ('j', 't', 'd', 'l', 'UTC', 'not_a_status', '2026-01-01', '2026-01-01')"
            )
        )


async def test_interview_end_must_be_after_start(session: AsyncSession) -> None:
    with pytest.raises(IntegrityError, match="CHECK constraint failed"):
        await session.execute(
            text(
                "INSERT INTO availability_windows (id, owner_type, owner_id, start_utc, end_utc, source)"
                " VALUES ('w', 'candidate', 'c', '2026-09-07 12:00:00', '2026-09-07 11:00:00', 'declared')"
            )
        )


async def test_only_one_plan_per_job_and_stage_may_be_active(session: AsyncSession) -> None:
    await _job(session)
    for plan_id in ("plan_a", "plan_b"):
        session.add(
            InterviewPlan(
                id=plan_id,
                job_id="job_1",
                stage=Stage.ONSITE,
                interview_type=InterviewType.ONSITE_PANEL,
                duration_minutes=60,
                panel_size=1,
                minimum_lead_hours=1,
                buffer_before_minutes=0,
                buffer_after_minutes=0,
                working_hours_policy=WorkingHoursPolicy.EXTENDED_HOURS,
                active=True,
            )
        )
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_superseded_plans_may_share_a_job_and_stage(session: AsyncSession) -> None:
    """The index is partial (``WHERE active = 1``), which is what lets history be retained."""
    await _job(session)
    for plan_id, active in (("plan_current", True), ("plan_old_1", False), ("plan_old_2", False)):
        session.add(
            InterviewPlan(
                id=plan_id,
                job_id="job_1",
                stage=Stage.ONSITE,
                interview_type=InterviewType.ONSITE_PANEL,
                duration_minutes=60,
                panel_size=1,
                minimum_lead_hours=1,
                buffer_before_minutes=0,
                buffer_after_minutes=0,
                working_hours_policy=WorkingHoursPolicy.EXTENDED_HOURS,
                active=active,
            )
        )
    await session.flush()
    assert len((await session.execute(select(InterviewPlan))).scalars().all()) == 3


@pytest.mark.parametrize(
    ("column", "value"),
    [("duration_minutes", 0), ("panel_size", 0), ("minimum_lead_hours", -1), ("buffer_before_minutes", -5)],
)
async def test_plan_numeric_constraints(session: AsyncSession, column: str, value: int) -> None:
    await _job(session)
    fields = {
        "duration_minutes": 60,
        "panel_size": 1,
        "minimum_lead_hours": 1,
        "buffer_before_minutes": 0,
        "buffer_after_minutes": 0,
    }
    fields[column] = value
    session.add(
        InterviewPlan(
            id="plan_1",
            job_id="job_1",
            stage=Stage.ONSITE,
            interview_type=InterviewType.ONSITE_PANEL,
            working_hours_policy=WorkingHoursPolicy.EXTENDED_HOURS,
            **fields,
        )
    )
    with pytest.raises(IntegrityError, match="CHECK constraint failed"):
        await session.flush()


# ------------------------------------------------------------------ timezone-aware datetimes


async def test_naive_datetime_is_refused(session: AsyncSession) -> None:
    """The whole point of :class:`UtcDateTime`. A naive value has no defensible interpretation,
    so it is rejected rather than assumed to be UTC or local."""
    # DTZ001 bans naive datetimes project-wide; constructing one is precisely what is under test.
    naive_start = datetime(2026, 9, 7, 12)  # noqa: DTZ001
    naive_end = datetime(2026, 9, 7, 13)  # noqa: DTZ001
    session.add(
        AvailabilityWindow(
            id="w1",
            owner_type=PartyType.CANDIDATE,
            owner_id="c",
            start_utc=naive_start,
            end_utc=naive_end,
            source=AvailabilitySource.DECLARED,
        )
    )
    with pytest.raises(StatementError) as caught:
        await session.flush()
    assert "naive datetime rejected" in str(caught.value)


async def test_aware_datetimes_round_trip_as_utc(session_factory: async_sessionmaker[AsyncSession]) -> None:
    async with session_factory() as writing:
        writing.add(
            AvailabilityWindow(
                id="w1",
                owner_type=PartyType.CANDIDATE,
                owner_id="c",
                start_utc=ANCHOR,
                end_utc=ANCHOR + timedelta(hours=2),
                source=AvailabilitySource.DECLARED,
            )
        )
        await writing.commit()

    async with session_factory() as reading:
        window = await reading.get(AvailabilityWindow, "w1")
        assert window is not None
        assert window.start_utc.tzinfo is not None
        assert window.start_utc == ANCHOR
        assert window.start_utc.utcoffset() == timedelta(0)


async def test_non_utc_input_is_normalized_not_truncated(session_factory: async_sessionmaker[AsyncSession]) -> None:
    """A 09:00 New York start must come back as 13:00 UTC, the same instant -- not as 09:00 with
    the offset quietly discarded, which is what raw SQLite storage would do."""
    eastern = datetime(2026, 9, 7, 9, tzinfo=ZoneInfo("America/New_York"))
    async with session_factory() as writing:
        writing.add(
            AvailabilityWindow(
                id="w1",
                owner_type=PartyType.CANDIDATE,
                owner_id="c",
                start_utc=eastern,
                end_utc=eastern + timedelta(hours=1),
                source=AvailabilitySource.DECLARED,
            )
        )
        await writing.commit()

    async with session_factory() as reading:
        window = await reading.get(AvailabilityWindow, "w1")
        assert window is not None
        assert window.start_utc == eastern
        assert window.start_utc.hour == 13


@pytest.mark.parametrize("bad", ["America/Newyork", "Mars/Olympus", "", "UTC+5", "GMT+0500"])
def test_invalid_timezone_names_are_rejected(bad: str) -> None:
    with pytest.raises(ValueError, match="not a valid IANA timezone"):
        validated_timezone(bad)


@pytest.mark.parametrize("good", ["UTC", "America/New_York", "Europe/Dublin", "Asia/Tokyo"])
def test_valid_timezone_names_are_accepted(good: str) -> None:
    assert validated_timezone(good) == good


@pytest.mark.parametrize("legacy", ["EST", "MST", "HST"])
def test_legacy_fixed_offset_zones_are_accepted(legacy: str) -> None:
    """Documents a real gap rather than pretending it is closed.

    These are genuine tzdata entries, so the validator passes them, but they carry no daylight
    saving -- ``EST`` is UTC-5 in July, when New York is UTC-4. Nothing here can catch that; it is
    recorded so the next person does not read the validator as a guarantee of correctness.
    """
    assert validated_timezone(legacy) == legacy


async def test_invalid_timezone_on_a_model_is_rejected_at_assignment(session: AsyncSession) -> None:
    with pytest.raises(ValueError, match="not a valid IANA timezone"):
        Candidate(id="c", first_name="A", last_name="B", email="a@x.test", timezone="Mars/Olympus")


# ------------------------------------------------------------------ path containment


async def test_database_file_is_created_inside_data_dir(settings: Settings, initialized_engine: AsyncEngine) -> None:
    """Settings proves the path *resolves* inside ``data_dir``; this proves the engine actually
    writes there rather than somewhere the URL construction lost track of."""
    database_path = settings.resolved_database_path
    assert database_path.exists()
    assert database_path.is_relative_to(settings.data_dir)


async def test_engine_rejects_a_database_outside_data_dir(tmp_path_factory: pytest.TempPathFactory) -> None:
    """The containment check belongs to Settings, so an escape never reaches engine construction."""
    inside = tmp_path_factory.mktemp("inside")
    outside = tmp_path_factory.mktemp("outside") / "escaped.sqlite3"
    with pytest.raises(ValueError, match="must resolve inside data_dir"):
        Settings(env="test", data_dir=inside, database_path=outside)


async def test_status_and_stage_round_trip_as_enum_members(session_factory: async_sessionmaker[AsyncSession]) -> None:
    """Values are stored, names are not -- a row written as ``on_hold`` reads back as the member."""
    async with session_factory() as writing:
        await _job(writing)
        await _candidate(writing)
        writing.add(
            Application(
                id="app_1",
                candidate_id="cand_1",
                job_id="job_1",
                current_stage=Stage.TECHNICAL_SCREEN,
                status=ApplicationStatus.ON_HOLD,
                coordinator_id="coord",
                last_activity_at=ANCHOR,
            )
        )
        await writing.commit()

    async with session_factory() as reading:
        stored = (await reading.execute(text("SELECT status FROM applications WHERE id = 'app_1'"))).scalar_one()
        assert stored == "on_hold"
        application = await reading.get(Application, "app_1")
        assert application is not None
        assert application.status is ApplicationStatus.ON_HOLD
