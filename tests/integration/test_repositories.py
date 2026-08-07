"""Repository pagination, filtering, and the boundaries around both.

Run against the seeded dataset, so the expected values are the demo data's real contents rather
than rows invented by the test. If the seed changes, these fail -- which is the point: the seed
is a fixture other phases will build on, and a silent change to it should not go unnoticed.
"""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from talent_ops_mcp.db.schema import Candidate
from talent_ops_mcp.errors import DomainError, ErrorCode
from talent_ops_mcp.models.domain import ApplicationStatus, JobStatus, Stage
from talent_ops_mcp.repositories.sqlite import (
    SqliteApplicationRepository,
    SqliteCandidateRepository,
    SqliteInterviewerRepository,
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
