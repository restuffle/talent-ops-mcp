"""The Phase 3 read operations, against the seeded dataset.

Expected values are the demo data's real contents, not numbers the test invented -- see
``test_repositories.py`` for why. The ``awaiting_scheduling`` expectations are the interesting
ones: they are derived on every read from three conditions, so they are the assertions most
likely to catch a regression in Phase 4-5 when interviews start being written.
"""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from talent_ops_mcp.errors import DomainError, ErrorCode
from talent_ops_mcp.models.domain import ApplicationStatus, ContactMethod, JobStatus, Stage
from talent_ops_mcp.services.pagination import encode_cursor
from talent_ops_mcp.services.recruiting import RecruitingService, sqlite_recruiting_service


@pytest.fixture
def service(seeded_session: AsyncSession) -> RecruitingService:
    return sqlite_recruiting_service(seeded_session, max_page_size=50)


# ------------------------------------------------------------------ search_jobs


async def test_search_by_title_text(service: RecruitingService) -> None:
    result = await service.search_jobs(query="engineer")
    assert {job.id for job in result.jobs} == {"job_platform_eng", "job_qa_automation"}
    assert result.total == 2
    assert result.count == 2


async def test_search_by_title_is_case_insensitive(service: RecruitingService) -> None:
    assert (await service.search_jobs(query="PLATFORM")).total == 1


async def test_search_by_title_and_status_together(service: RecruitingService) -> None:
    """The completion-gate query: open roles matching a title, excluding the on-hold one."""
    result = await service.search_jobs(query="engineer", status=JobStatus.OPEN)
    assert [job.id for job in result.jobs] == ["job_platform_eng"]
    assert result.jobs[0].status is JobStatus.OPEN


async def test_search_matches_the_title_only_not_the_department(service: RecruitingService) -> None:
    """ "Quality" is the QA job's department, never a word in its title."""
    assert (await service.search_jobs(query="Quality")).total == 0
    assert (await service.search_jobs(department="Quality")).total == 1


async def test_search_with_no_filters_returns_every_job(service: RecruitingService) -> None:
    result = await service.search_jobs()
    assert result.total == 3
    assert result.has_more is False
    assert result.next_cursor is None


async def test_search_carries_the_job_timezone(service: RecruitingService) -> None:
    """A coordinator scanning results needs to know which clock the role runs on."""
    result = await service.search_jobs(query="clinical")
    assert result.jobs[0].default_timezone == "America/New_York"


async def test_every_job_field_is_mapped_to_the_right_column(service: RecruitingService) -> None:
    """Field-by-field, because same-typed neighbours are the ones that get transposed.

    Every attribute here is a `str`, so swapping `department` and `location` type-checks, passes
    a schema test, and produces a plausible-looking result. Only an assertion on the values
    catches it -- and one did not exist until a review agent silently transposed exactly that
    pair and the whole suite stayed green.
    """
    result = await service.search_jobs(query="platform")
    job = result.jobs[0]
    assert job.id == "job_platform_eng"
    assert job.title == "Senior Platform Engineer"
    assert job.department == "Engineering"
    assert job.location == "Bloomington, IN (hybrid)"
    assert job.default_timezone == "America/Indiana/Indianapolis"
    assert job.status is JobStatus.OPEN


async def test_zero_results_is_an_empty_page_not_an_error(service: RecruitingService) -> None:
    result = await service.search_jobs(query="zzz-no-such-role")
    assert result.jobs == []
    assert (result.total, result.count, result.has_more, result.next_cursor) == (0, 0, False, None)


# ------------------------------------------------------------------ pagination metadata


async def test_a_partial_page_reports_more_and_hands_back_a_cursor(service: RecruitingService) -> None:
    first = await service.search_jobs(limit=2)
    assert (first.total, first.count, first.has_more) == (3, 2, True)
    assert first.next_cursor is not None

    second = await service.search_jobs(limit=2, cursor=first.next_cursor)
    assert (second.total, second.count, second.has_more, second.next_cursor) == (3, 1, False, None)

    assert {job.id for job in first.jobs}.isdisjoint({job.id for job in second.jobs})


async def test_walking_the_cursor_yields_every_row_exactly_once(service: RecruitingService) -> None:
    seen: list[str] = []
    cursor: str | None = None
    while True:
        page = await service.list_applications(limit=5, cursor=cursor)
        seen.extend(row.id for row in page.applications)
        if not page.has_more:
            break
        cursor = page.next_cursor

    assert len(seen) == 12
    assert len(set(seen)) == 12


async def test_a_page_that_fills_exactly_does_not_claim_more(service: RecruitingService) -> None:
    result = await service.list_applications(limit=12)
    assert (result.count, result.has_more, result.next_cursor) == (12, False, None)


async def test_an_oversized_limit_is_clamped_and_the_cursor_follows_the_clamp(seeded_session: AsyncSession) -> None:
    """The cursor must be built from the window actually served. Built from the *requested* limit
    it would skip rows -- the page would say 3 and the next cursor would start at 100."""
    small = sqlite_recruiting_service(seeded_session, max_page_size=3)

    first = await small.list_applications(limit=100)
    assert first.count == 3
    assert first.has_more is True

    second = await small.list_applications(limit=100, cursor=first.next_cursor)
    assert {row.id for row in first.applications}.isdisjoint({row.id for row in second.applications})
    assert second.count == 3


async def test_the_maximum_page_size_is_honoured(seeded_session: AsyncSession) -> None:
    result = await sqlite_recruiting_service(seeded_session, max_page_size=2).search_jobs(limit=50)
    assert result.count == 2
    assert result.total == 3


async def test_a_cursor_past_the_end_is_an_empty_page(service: RecruitingService) -> None:
    result = await service.search_jobs(cursor=encode_cursor(500))
    assert result.jobs == []
    assert result.total == 3
    assert result.has_more is False


@pytest.mark.parametrize("limit", [0, -1])
async def test_a_nonsensical_limit_is_a_validation_error(service: RecruitingService, limit: int) -> None:
    with pytest.raises(DomainError) as caught:
        await service.search_jobs(limit=limit)
    assert caught.value.code is ErrorCode.VALIDATION_ERROR


async def test_a_malformed_cursor_is_a_validation_error(service: RecruitingService) -> None:
    with pytest.raises(DomainError) as caught:
        await service.search_jobs(cursor="not-a-real-cursor")
    assert caught.value.code is ErrorCode.VALIDATION_ERROR


# ------------------------------------------------------------------ list_applications


async def test_applications_filter_by_job(service: RecruitingService) -> None:
    result = await service.list_applications(job_id="job_platform_eng")
    assert result.total == 5
    assert {row.job_title for row in result.applications} == {"Senior Platform Engineer"}


async def test_applications_filter_by_stage_and_status_independently(service: RecruitingService) -> None:
    """app_12 is withdrawn but still sits at APPLIED; a stage filter must not imply an active one."""
    at_applied = await service.list_applications(stage=Stage.APPLIED)
    assert {row.id for row in at_applied.applications} == {"app_04", "app_08", "app_11", "app_12"}

    active_at_applied = await service.list_applications(stage=Stage.APPLIED, status=ApplicationStatus.ACTIVE)
    assert {row.id for row in active_at_applied.applications} == {"app_04", "app_08", "app_11"}


async def test_applications_carry_the_names_needed_to_read_them(service: RecruitingService) -> None:
    """Denormalised on purpose: rendering a list must not cost one get_candidate call per row.

    Asserted field by field for the reason in ``test_every_job_field_is_mapped_to_the_right_column``:
    ``id``, ``candidate_id``, and ``job_id`` are three strings in a row, and a transposition among
    them would look entirely normal.
    """
    result = await service.list_applications(candidate_id="cand_01")
    row = result.applications[0]
    assert row.id == "app_01"
    assert row.candidate_id == "cand_01"
    assert row.candidate_name == "Alex Testfield"
    assert row.job_id == "job_platform_eng"
    assert row.job_title == "Senior Platform Engineer"
    assert row.current_stage is Stage.ONSITE
    assert row.status is ApplicationStatus.ACTIVE


async def test_the_scheduling_worklist(service: RecruitingService) -> None:
    """Active, the current stage has an approved plan, and nothing is booked.

    * app_01 -- onsite, plan approved, its only interview is *completed*, so still outstanding.
    * app_03, app_07 -- recruiter screens with plans and no interview at all.
    * app_10 -- its interview was *cancelled*, which is what makes the row resurface (§9.3).
    """
    result = await service.list_applications(awaiting_scheduling=True)
    assert {row.id for row in result.applications} == {"app_01", "app_03", "app_07", "app_10"}
    assert all(row.awaiting_scheduling for row in result.applications)
    assert all(row.status is ApplicationStatus.ACTIVE for row in result.applications)


async def test_a_booked_interview_takes_an_application_off_the_worklist(service: RecruitingService) -> None:
    """app_02 has a scheduled interview; app_06 likewise."""
    result = await service.list_applications(awaiting_scheduling=False)
    assert {"app_02", "app_06"} <= {row.id for row in result.applications}
    assert not any(row.awaiting_scheduling for row in result.applications)


async def test_a_stage_with_no_approved_plan_is_not_outstanding_work(service: RecruitingService) -> None:
    """app_04 is active at APPLIED, a stage with no plan. There is nothing to schedule yet, so
    surfacing it would put un-actionable work on a coordinator's list."""
    result = await service.list_applications(job_id="job_platform_eng", awaiting_scheduling=True)
    assert "app_04" not in {row.id for row in result.applications}


async def test_the_two_awaiting_filters_partition_the_set(service: RecruitingService) -> None:
    everything = await service.list_applications()
    yes = await service.list_applications(awaiting_scheduling=True)
    no = await service.list_applications(awaiting_scheduling=False)
    assert yes.total + no.total == everything.total == 12


async def test_the_completion_gate_query(service: RecruitingService) -> None:
    """ "Active applications awaiting a technical interview" -- the second half of the Phase 3 gate."""
    result = await service.list_applications(
        stage=Stage.TECHNICAL_SCREEN, status=ApplicationStatus.ACTIVE, awaiting_scheduling=True
    )
    assert [row.id for row in result.applications] == ["app_10"]
    assert result.applications[0].job_title == "QA Automation Engineer"


async def test_an_unknown_filter_id_is_an_empty_page_not_a_not_found(service: RecruitingService) -> None:
    """A filter asks a question about a set; "nothing matches" answers it truthfully."""
    result = await service.list_applications(job_id="job_does_not_exist")
    assert result.applications == []
    assert result.total == 0


# ------------------------------------------------------------------ get_candidate


async def test_get_candidate_returns_contact_details_and_pipeline_context(service: RecruitingService) -> None:
    candidate = await service.get_candidate("cand_01")
    assert candidate.id == "cand_01"
    assert candidate.first_name == "Alex"
    assert candidate.last_name == "Testfield"
    assert candidate.email == "alex.testfield@talent-ops.test"
    assert candidate.phone == "+1-555-0101"
    assert candidate.preferred_contact_method is ContactMethod.EMAIL
    assert candidate.timezone == "America/Indiana/Indianapolis"
    assert [context.application_id for context in candidate.applications] == ["app_01"]
    assert candidate.applications[0].job_title == "Senior Platform Engineer"
    assert candidate.applications[0].awaiting_scheduling is True


async def test_a_candidate_with_no_phone_reports_null_not_an_empty_string(service: RecruitingService) -> None:
    assert (await service.get_candidate("cand_03")).phone is None


@pytest.mark.parametrize(
    "unknown",
    ["cand_99", "", "  ", "'; DROP TABLE candidates; --", "../../etc/passwd", "cand_01 OR 1=1"],
)
async def test_an_unknown_or_malformed_candidate_id_is_not_found(service: RecruitingService, unknown: str) -> None:
    with pytest.raises(DomainError) as caught:
        await service.get_candidate(unknown)
    assert caught.value.code is ErrorCode.NOT_FOUND


async def test_the_not_found_message_names_the_tool_that_finds_a_real_id(service: RecruitingService) -> None:
    with pytest.raises(DomainError) as caught:
        await service.get_candidate("cand_99")
    assert "talent_ops_list_applications" in caught.value.message


# ------------------------------------------------------------------ get_interview_plan


async def test_get_interview_plan_returns_the_approved_numbers(service: RecruitingService) -> None:
    plan = await service.get_interview_plan("job_platform_eng", Stage.ONSITE)
    assert plan.id == "plan_pe_onsite"
    assert plan.job_title == "Senior Platform Engineer"
    assert plan.default_timezone == "America/Indiana/Indianapolis"
    assert (plan.duration_minutes, plan.panel_size, plan.minimum_lead_hours) == (90, 3, 72)
    assert (plan.buffer_before_minutes, plan.buffer_after_minutes) == (15, 30)


async def test_a_superseded_plan_is_never_returned(service: RecruitingService) -> None:
    """plan_pe_onsite_v0 shares (job, stage) with the active plan and is inactive. Handing
    withdrawn policy to a scheduler would be worse than handing it nothing."""
    plan = await service.get_interview_plan("job_platform_eng", Stage.ONSITE)
    assert plan.id != "plan_pe_onsite_v0"
    assert plan.duration_minutes != 60


async def test_skill_requirements_come_back_with_the_plan(service: RecruitingService) -> None:
    plan = await service.get_interview_plan("job_clinical_data", Stage.TECHNICAL_SCREEN)
    assert [(req.skill_code, req.minimum_interviewers) for req in plan.skill_requirements] == [
        ("clinical_data", 1),
        ("sql", 1),
    ]


async def test_a_plan_with_no_skill_requirements_reports_an_empty_list(service: RecruitingService) -> None:
    plan = await service.get_interview_plan("job_platform_eng", Stage.RECRUITER_SCREEN)
    assert plan.skill_requirements == []


async def test_an_unknown_job_and_an_unplanned_stage_give_different_errors(service: RecruitingService) -> None:
    """One means "wrong id", the other means "no approved policy". Merging them would let a
    caller retry the wrong thing."""
    with pytest.raises(DomainError) as bad_job:
        await service.get_interview_plan("job_nope", Stage.ONSITE)
    assert bad_job.value.code is ErrorCode.NOT_FOUND
    assert "talent_ops_search_jobs" in bad_job.value.message

    with pytest.raises(DomainError) as no_plan:
        await service.get_interview_plan("job_platform_eng", Stage.OFFER)
    assert no_plan.value.code is ErrorCode.NOT_FOUND
    assert "approved interview plan" in no_plan.value.message


async def test_a_missing_plan_tells_the_caller_not_to_invent_one(service: RecruitingService) -> None:
    """The dangerous failure here is a model filling in a plausible 60 minutes of its own."""
    with pytest.raises(DomainError) as caught:
        await service.get_interview_plan("job_qa_automation", Stage.ONSITE)
    assert "Do not schedule" in caught.value.message
