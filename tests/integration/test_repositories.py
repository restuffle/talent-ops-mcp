"""Repository pagination, filtering, and the boundaries around both.

Run against the seeded dataset, so the expected values are the demo data's real contents rather
than rows invented by the test. If the seed changes, these fail -- which is the point: the seed
is a fixture other phases will build on, and a silent change to it should not go unnoticed.
"""

import logging
from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from talent_ops_mcp.db.schema import Candidate
from talent_ops_mcp.errors import DomainError, ErrorCode
from talent_ops_mcp.models.domain import ApplicationStatus, JobStatus, PartyType, Stage
from talent_ops_mcp.repositories.sqlite import (
    SqliteApplicationRepository,
    SqliteAvailabilityRepository,
    SqliteCandidateRepository,
    SqliteInterviewerRepository,
    SqliteInterviewRepository,
    SqliteJobRepository,
)

# ------------------------------------------------------------------ pagination


async def test_pages_partition_the_result_set_without_gaps_or_repeats(seeded_session: AsyncSession) -> None:
    """The property that actually matters: walking every page must yield each row exactly once."""
    repository = SqliteCandidateRepository(seeded_session)
    seen: list[str] = []
    offset = 0
    while True:
        page = await repository.search(limit=5, offset=offset)
        if not page.items:
            break
        seen.extend(candidate.id for candidate in page.items)
        offset += len(page.items)

    assert len(seen) == 12
    assert len(set(seen)) == 12


async def test_total_counts_matches_not_page_length(seeded_session: AsyncSession) -> None:
    page = await SqliteCandidateRepository(seeded_session).search(limit=5)
    assert len(page.items) == 5
    assert page.total == 12
    assert page.has_more is True


async def test_has_more_is_false_on_the_final_page(seeded_session: AsyncSession) -> None:
    page = await SqliteCandidateRepository(seeded_session).search(limit=5, offset=10)
    assert len(page.items) == 2
    assert page.has_more is False


async def test_a_page_that_fills_exactly_is_not_reported_as_having_more(seeded_session: AsyncSession) -> None:
    """The off-by-one that ``total`` exists to prevent: 12 rows, limit 12, nothing beyond."""
    page = await SqliteCandidateRepository(seeded_session).search(limit=12)
    assert len(page.items) == 12
    assert page.has_more is False


async def test_ordering_is_stable_across_repeated_reads(seeded_session: AsyncSession) -> None:
    """Surname order, not insertion order -- and identical on every read, which is what makes
    paging safe."""
    repository = SqliteCandidateRepository(seeded_session)
    first = (await repository.search(limit=12)).items
    second = (await repository.search(limit=12)).items
    assert [c.id for c in first] == [c.id for c in second]

    surnames = [(c.last_name, c.first_name) for c in first]
    assert surnames == sorted(surnames)


async def test_limit_above_the_maximum_is_clamped_not_rejected(seeded_session: AsyncSession) -> None:
    """Clamping keeps an over-eager caller useful; the honest ``limit`` on the page is how it
    learns it did not get everything it asked for."""
    page = await SqliteCandidateRepository(seeded_session, max_page_size=3).search(limit=1000)
    assert page.limit == 3
    assert len(page.items) == 3
    assert page.total == 12
    assert page.has_more is True


@pytest.mark.parametrize("limit", [0, -1, -100])
async def test_a_nonsensical_limit_raises(seeded_session: AsyncSession, limit: int) -> None:
    with pytest.raises(DomainError) as caught:
        await SqliteCandidateRepository(seeded_session).search(limit=limit)
    assert caught.value.code is ErrorCode.VALIDATION_ERROR


async def test_a_negative_offset_raises(seeded_session: AsyncSession) -> None:
    with pytest.raises(DomainError) as caught:
        await SqliteCandidateRepository(seeded_session).search(offset=-1)
    assert caught.value.code is ErrorCode.VALIDATION_ERROR


async def test_offset_beyond_the_end_yields_an_empty_page(seeded_session: AsyncSession) -> None:
    page = await SqliteCandidateRepository(seeded_session).search(offset=500)
    assert page.items == ()
    assert page.total == 12
    assert page.has_more is False


# ------------------------------------------------------------------ jobs


async def test_get_returns_none_for_an_unknown_id(seeded_session: AsyncSession) -> None:
    assert await SqliteJobRepository(seeded_session).get("job_nope") is None


async def test_jobs_filter_by_status(seeded_session: AsyncSession) -> None:
    repository = SqliteJobRepository(seeded_session)
    assert (await repository.list(status=JobStatus.OPEN)).total == 2
    assert (await repository.list(status=JobStatus.ON_HOLD)).total == 1
    assert (await repository.list(status=JobStatus.CLOSED)).total == 0


async def test_jobs_filter_by_department(seeded_session: AsyncSession) -> None:
    page = await SqliteJobRepository(seeded_session).list(department="Engineering")
    assert [job.id for job in page.items] == ["job_platform_eng"]


async def test_job_filters_combine_conjunctively(seeded_session: AsyncSession) -> None:
    page = await SqliteJobRepository(seeded_session).list(status=JobStatus.OPEN, department="Quality")
    assert page.total == 0, "the Quality job is on hold, so an OPEN+Quality filter matches nothing"


# ------------------------------------------------------------------ candidate search


async def test_search_matches_a_surname_case_insensitively(seeded_session: AsyncSession) -> None:
    page = await SqliteCandidateRepository(seeded_session).search(query="TESTFIELD")
    assert [c.id for c in page.items] == ["cand_01"]


async def test_search_matches_a_partial_email(seeded_session: AsyncSession) -> None:
    page = await SqliteCandidateRepository(seeded_session).search(query="bailey.sampleton@")
    assert [c.id for c in page.items] == ["cand_02"]


async def test_search_matches_a_forename(seeded_session: AsyncSession) -> None:
    page = await SqliteCandidateRepository(seeded_session).search(query="cameron")
    assert [c.id for c in page.items] == ["cand_03"]


async def test_search_with_no_query_returns_everyone(seeded_session: AsyncSession) -> None:
    assert (await SqliteCandidateRepository(seeded_session).search()).total == 12


async def test_search_for_an_absent_term_returns_nothing(seeded_session: AsyncSession) -> None:
    page = await SqliteCandidateRepository(seeded_session).search(query="zzz-not-a-person")
    assert page.items == ()
    assert page.total == 0


@pytest.mark.parametrize("wildcard", ["%", "_", "%%", "a%b"])
async def test_like_wildcards_in_a_query_are_treated_literally(seeded_session: AsyncSession, wildcard: str) -> None:
    """Unescaped, ``%`` becomes "match everything" and ``_`` becomes "match any character" --
    a search for a literal underscore would return the entire candidate list. No seeded name or
    address contains either character, so a correct implementation finds nothing."""
    page = await SqliteCandidateRepository(seeded_session).search(query=wildcard)
    assert page.total == 0, f"{wildcard!r} was interpreted as a wildcard"


async def test_a_literal_wildcard_character_is_still_findable(seeded_session: AsyncSession) -> None:
    """Escaping must not make the character unsearchable, only non-magical."""
    seeded_session.add(
        Candidate(
            id="cand_odd",
            first_name="Ro_bin",
            last_name="Underscore",
            email="ro_bin.underscore@talent-ops.test",
            timezone="UTC",
        )
    )
    await seeded_session.flush()

    page = await SqliteCandidateRepository(seeded_session).search(query="ro_bin")
    assert [c.id for c in page.items] == ["cand_odd"]


# ------------------------------------------------------------------ applications


async def test_applications_filter_by_job(seeded_session: AsyncSession) -> None:
    page = await SqliteApplicationRepository(seeded_session).list(job_id="job_platform_eng")
    assert page.total == 5


async def test_applications_filter_by_stage(seeded_session: AsyncSession) -> None:
    """Four, not three: the withdrawn application is still sitting at the APPLIED stage. Stage
    and status are independent axes, and a stage filter must not quietly imply an active one."""
    page = await SqliteApplicationRepository(seeded_session).list(stage=Stage.APPLIED)
    assert page.total == 4
    assert {a.id for a in page.items} == {"app_04", "app_08", "app_11", "app_12"}


async def test_applications_filter_by_status(seeded_session: AsyncSession) -> None:
    repository = SqliteApplicationRepository(seeded_session)
    assert (await repository.list(status=ApplicationStatus.ACTIVE)).total == 10
    assert (await repository.list(status=ApplicationStatus.ON_HOLD)).total == 1
    assert (await repository.list(status=ApplicationStatus.WITHDRAWN)).total == 1


async def test_application_filters_compose(seeded_session: AsyncSession) -> None:
    page = await SqliteApplicationRepository(seeded_session).list(
        job_id="job_qa_automation", status=ApplicationStatus.ACTIVE
    )
    assert {a.id for a in page.items} == {"app_10", "app_11"}


# ------------------------------------------------------------------ interviewers


async def test_interviewers_filter_by_active(seeded_session: AsyncSession) -> None:
    repository = SqliteInterviewerRepository(seeded_session)
    assert (await repository.list(active=True)).total == 5
    assert (await repository.list(active=False)).total == 1
    assert (await repository.list()).total == 6


async def test_a_single_skill_filter_matches_holders(seeded_session: AsyncSession) -> None:
    page = await SqliteInterviewerRepository(seeded_session).list(skills=("python",))
    assert {i.id for i in page.items} == {"int_avery", "int_blake", "int_casey"}


async def test_multiple_skills_are_required_together_not_alternatively(seeded_session: AsyncSession) -> None:
    """§9.2 step 3 needs panellists satisfying *all* requirements. An ``ANY`` match here would
    propose a panel that cannot cover the plan, and the proposal would look valid."""
    page = await SqliteInterviewerRepository(seeded_session).list(skills=("python", "system_design"))
    assert {i.id for i in page.items} == {"int_avery"}, "only Avery holds both"


async def test_an_unsatisfiable_skill_combination_returns_nothing(seeded_session: AsyncSession) -> None:
    page = await SqliteInterviewerRepository(seeded_session).list(skills=("python", "clinical_data"))
    assert page.total == 0


async def test_skill_and_active_filters_combine(seeded_session: AsyncSession) -> None:
    """Frankie holds test_automation but is inactive, so the pair must exclude them."""
    repository = SqliteInterviewerRepository(seeded_session)
    everyone = await repository.list(skills=("test_automation",))
    assert {i.id for i in everyone.items} == {"int_casey", "int_frankie"}

    eligible = await repository.list(skills=("test_automation",), active=True)
    assert {i.id for i in eligible.items} == {"int_casey"}


async def test_an_unknown_skill_matches_no_one(seeded_session: AsyncSession) -> None:
    page = await SqliteInterviewerRepository(seeded_session).list(skills=("cobol",))
    assert page.total == 0


# ------------------------------------------------------------------ scheduling reads (Phase 4)


async def test_the_panel_pool_carries_skills_and_excludes_the_inactive(seeded_session: AsyncSession) -> None:
    """The pool §9.2 step 3 draws from. Unlike ``list``, it is not filtered by skill -- a plan
    whose panel is larger than its requirements has seats anyone may fill."""
    pool = await SqliteInterviewerRepository(seeded_session).list_active_with_skills(limit=50)

    assert [member.id for member in pool] == ["int_avery", "int_blake", "int_casey", "int_devon", "int_ellis"]
    by_id = {member.id: member for member in pool}
    assert by_id["int_avery"].skills == frozenset({"python", "distributed_systems", "system_design"})
    assert by_id["int_avery"].name == "Avery Demo"
    assert by_id["int_avery"].timezone == "America/Indiana/Indianapolis"
    assert "int_frankie" not in by_id, "inactive interviewers are never eligible"


async def test_the_panel_pool_is_ordered_by_id_not_by_name(seeded_session: AsyncSession) -> None:
    """Load-bearing: the panel chosen for a slot is the first valid combination in this order, so
    a reordering here silently changes which people are proposed."""
    pool = await SqliteInterviewerRepository(seeded_session).list_active_with_skills(limit=50)
    assert [member.id for member in pool] == sorted(member.id for member in pool)


async def test_the_panel_pool_is_truncated_loudly(
    seeded_session: AsyncSession, caplog: pytest.LogCaptureFixture
) -> None:
    """A silently truncated pool would produce proposals that look authoritative while ignoring
    real interviewers. The bound stays; the silence does not."""
    with caplog.at_level(logging.WARNING):
        pool = await SqliteInterviewerRepository(seeded_session).list_active_with_skills(limit=2)

    assert [member.id for member in pool] == ["int_avery", "int_blake"]
    assert "truncated" in caplog.text


async def test_availability_windows_overlapping_the_range_are_returned(seeded_session: AsyncSession) -> None:
    """Overlapping, not contained: a window running from before the range into it is exactly the
    one a slot at the range's start needs."""
    repository = SqliteAvailabilityRepository(seeded_session)
    # Avery's Monday window is 13:00-21:00 UTC; ask for a range that starts inside it.
    windows = await repository.list_windows(
        owner_type=PartyType.INTERVIEWER,
        owner_ids=["int_avery"],
        start_utc=datetime(2026, 9, 7, 15, tzinfo=UTC),
        end_utc=datetime(2026, 9, 7, 16, tzinfo=UTC),
    )

    assert len(windows) == 1
    assert windows[0].start_utc == datetime(2026, 9, 7, 13, tzinfo=UTC)
    assert windows[0].end_utc == datetime(2026, 9, 7, 21, tzinfo=UTC)
    assert windows[0].party_type is PartyType.INTERVIEWER
    assert windows[0].party_id == "int_avery"


async def test_a_window_that_only_touches_the_range_is_excluded(seeded_session: AsyncSession) -> None:
    """It contributes no schedulable time, and a zero-length interval is not representable."""
    windows = await SqliteAvailabilityRepository(seeded_session).list_windows(
        owner_type=PartyType.INTERVIEWER,
        owner_ids=["int_avery"],
        start_utc=datetime(2026, 9, 7, 21, tzinfo=UTC),
        end_utc=datetime(2026, 9, 7, 22, tzinfo=UTC),
    )
    assert windows == ()


async def test_availability_never_crosses_the_owner_type(seeded_session: AsyncSession) -> None:
    """``owner_id`` is polymorphic and carries no foreign key, so the type is the only thing
    keeping a candidate's diary out of an interviewer's."""
    week = (datetime(2026, 9, 7, tzinfo=UTC), datetime(2026, 9, 12, tzinfo=UTC))
    repository = SqliteAvailabilityRepository(seeded_session)

    as_interviewer = await repository.list_windows(
        owner_type=PartyType.INTERVIEWER, owner_ids=["cand_01"], start_utc=week[0], end_utc=week[1]
    )
    as_candidate = await repository.list_windows(
        owner_type=PartyType.CANDIDATE, owner_ids=["cand_01"], start_utc=week[0], end_utc=week[1]
    )

    assert as_interviewer == ()
    assert len(as_candidate) == 2


async def test_no_owners_asks_no_question(seeded_session: AsyncSession) -> None:
    windows = await SqliteAvailabilityRepository(seeded_session).list_windows(
        owner_type=PartyType.CANDIDATE,
        owner_ids=[],
        start_utc=datetime(2026, 9, 7, tzinfo=UTC),
        end_utc=datetime(2026, 9, 12, tzinfo=UTC),
    )
    assert windows == ()


async def test_busy_blocks_come_back_per_attendee(seeded_session: AsyncSession) -> None:
    """One interview with two attendees is two rows here: the scheduler asks per person, and a
    single row would make one of them look free."""
    blocks = await SqliteInterviewRepository(seeded_session).list_busy(
        parties=[(PartyType.INTERVIEWER, "int_devon"), (PartyType.INTERVIEWER, "int_blake")],
        start_utc=datetime(2026, 9, 9, tzinfo=UTC),
        end_utc=datetime(2026, 9, 10, tzinfo=UTC),
    )

    # The clinical-data technical screen: Wednesday 17:30-18:15 UTC, with both on the panel.
    assert {block.party_id for block in blocks} == {"int_devon", "int_blake"}
    for block in blocks:
        assert block.party_type is PartyType.INTERVIEWER
        assert block.start_utc == datetime(2026, 9, 9, 17, 30, tzinfo=UTC)
        assert block.end_utc == datetime(2026, 9, 9, 18, 15, tzinfo=UTC)


async def test_only_scheduled_interviews_are_busy(seeded_session: AsyncSession) -> None:
    """§9.3: a cancelled interview frees its time, which is what makes a cancellation put the slot
    back on the market. The seed's cancelled QA screen is Casey's only booking that week."""
    blocks = await SqliteInterviewRepository(seeded_session).list_busy(
        parties=[(PartyType.INTERVIEWER, "int_casey")],
        start_utc=datetime(2026, 9, 7, tzinfo=UTC),
        end_utc=datetime(2026, 9, 12, tzinfo=UTC),
    )
    assert blocks == ()


async def test_a_busy_block_touching_the_range_is_excluded(seeded_session: AsyncSession) -> None:
    blocks = await SqliteInterviewRepository(seeded_session).list_busy(
        parties=[(PartyType.INTERVIEWER, "int_avery")],
        start_utc=datetime(2026, 9, 8, 16, tzinfo=UTC),
        end_utc=datetime(2026, 9, 8, 17, tzinfo=UTC),
    )
    assert blocks == ()


async def test_busy_blocks_never_cross_the_party_type(seeded_session: AsyncSession) -> None:
    """``attendee_id`` is polymorphic. Asking for an interviewer whose id happens to name a
    candidate must not import that candidate's bookings."""
    week = (datetime(2026, 9, 7, tzinfo=UTC), datetime(2026, 9, 12, tzinfo=UTC))
    repository = SqliteInterviewRepository(seeded_session)

    as_candidate = await repository.list_busy(
        parties=[(PartyType.CANDIDATE, "cand_02")], start_utc=week[0], end_utc=week[1]
    )
    as_interviewer = await repository.list_busy(
        parties=[(PartyType.INTERVIEWER, "cand_02")], start_utc=week[0], end_utc=week[1]
    )

    assert len(as_candidate) == 1
    assert as_interviewer == ()


async def test_no_parties_asks_no_question(seeded_session: AsyncSession) -> None:
    blocks = await SqliteInterviewRepository(seeded_session).list_busy(
        parties=[], start_utc=datetime(2026, 9, 7, tzinfo=UTC), end_utc=datetime(2026, 9, 12, tzinfo=UTC)
    )
    assert blocks == ()
