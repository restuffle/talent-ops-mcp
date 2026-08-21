"""The interval algebra and time rules the scheduling engine is built on (§9.2 steps 4-8).

Pure computation, so these need no database and no session. They are separated from
``test_scheduling_service.py`` deliberately: when a proposal comes back wrong, the first question
is whether the arithmetic is wrong or the wiring is, and a failing test in this file answers it
without a fixture in sight.

Every instant here is written out in full rather than derived from a helper. A test whose expected
value is computed the same way the code computes it proves only that the computation is
self-consistent.
"""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from talent_ops_mcp.services.scheduling import (
    Interval,
    align_up,
    aligned_starts,
    clamp,
    covered_by,
    merge,
    overlaps_any,
    subtract,
    within_business_hours,
)

QUARTER_HOUR = timedelta(minutes=15)
HOUR = timedelta(hours=1)


def utc(day: int, hour: int, minute: int = 0) -> datetime:
    """An instant in September 2026, the week the demo dataset is anchored to."""
    return datetime(2026, 9, day, hour, minute, tzinfo=UTC)


def span(day: int, start_hour: int, end_hour: int) -> Interval:
    return Interval(utc(day, start_hour), utc(day, end_hour))


# ------------------------------------------------------------------ Interval


def test_an_interval_must_have_positive_length() -> None:
    with pytest.raises(ValueError, match="end must be after start"):
        Interval(utc(7, 9), utc(7, 9))
    with pytest.raises(ValueError, match="end must be after start"):
        Interval(utc(7, 10), utc(7, 9))


def test_touching_intervals_do_not_overlap() -> None:
    """The half-open rule, and the reason back-to-back interviews are bookable at all.

    §12 Phase 4 asks for this case by name. Getting it wrong in the other direction -- treating a
    shared endpoint as a clash -- would silently refuse every slot that begins the moment another
    ends, which is most of a working day.
    """
    assert span(7, 9, 10).overlaps(span(7, 10, 11)) is False
    assert span(7, 10, 11).overlaps(span(7, 9, 10)) is False
    assert span(7, 9, 11).overlaps(span(7, 10, 12)) is True


def test_one_minute_of_shared_time_is_an_overlap() -> None:
    assert Interval(utc(7, 9), utc(7, 10, 1)).overlaps(span(7, 10, 11)) is True


def test_covers_includes_shared_endpoints() -> None:
    """A slot that exactly fills a window is inside it. Requiring strict containment would refuse
    the interview somebody declared themselves free for."""
    assert span(7, 9, 17).covers(span(7, 9, 17)) is True
    assert span(7, 9, 17).covers(span(7, 9, 10)) is True
    assert span(7, 9, 17).covers(span(7, 16, 17)) is True
    assert span(7, 9, 17).covers(span(7, 16, 18)) is False


# ------------------------------------------------------------------ merge


def test_merge_sorts_and_leaves_real_gaps_alone() -> None:
    merged = merge([span(7, 14, 16), span(7, 9, 11)])
    assert merged == (span(7, 9, 11), span(7, 14, 16))


def test_merge_coalesces_overlapping_and_touching_spans() -> None:
    """Touching spans join, which is what makes a slot straddling the seam findable.

    Somebody free 09:00-10:00 and 10:00-11:00 is free for ninety minutes from 09:30; leaving the
    two separate would hide every option that crosses 10:00.
    """
    assert merge([span(7, 9, 10), span(7, 10, 11)]) == (span(7, 9, 11),)
    assert merge([span(7, 9, 12), span(7, 10, 11)]) == (span(7, 9, 12),)
    assert merge([span(7, 9, 11), span(7, 10, 13)]) == (span(7, 9, 13),)


def test_merge_of_nothing_is_nothing() -> None:
    assert merge([]) == ()


# ------------------------------------------------------------------ subtract


def test_a_block_in_the_middle_splits_a_span_in_two() -> None:
    assert subtract([span(7, 9, 17)], [span(7, 12, 13)]) == (span(7, 9, 12), span(7, 13, 17))


def test_a_block_covering_the_span_removes_it_entirely() -> None:
    assert subtract([span(7, 9, 17)], [span(7, 8, 18)]) == ()


def test_a_block_trims_an_edge_without_splitting() -> None:
    assert subtract([span(7, 9, 17)], [span(7, 8, 10)]) == (span(7, 10, 17),)
    assert subtract([span(7, 9, 17)], [span(7, 16, 18)]) == (span(7, 9, 16),)


def test_a_touching_block_removes_nothing() -> None:
    """A commitment ending exactly when a window opens does not eat into it."""
    assert subtract([span(7, 9, 17)], [span(7, 8, 9)]) == (span(7, 9, 17),)
    assert subtract([span(7, 9, 17)], [span(7, 17, 18)]) == (span(7, 9, 17),)


def test_several_blocks_apply_cumulatively() -> None:
    assert subtract([span(7, 9, 17)], [span(7, 11, 12), span(7, 14, 15)]) == (
        span(7, 9, 11),
        span(7, 12, 14),
        span(7, 15, 17),
    )


def test_subtracting_nothing_still_merges() -> None:
    assert subtract([span(7, 10, 11), span(7, 9, 10)], []) == (span(7, 9, 11),)


# ------------------------------------------------------------------ clamp, covered_by, overlaps_any


def test_clamp_trims_to_the_window_and_drops_what_falls_outside() -> None:
    assert clamp([span(7, 6, 20), span(8, 9, 10)], span(7, 9, 17)) == (span(7, 9, 17),)


def test_clamp_drops_a_span_that_only_touches_the_window() -> None:
    """Trimmed to nothing, not to a zero-length interval -- which is not representable anyway."""
    assert clamp([span(7, 8, 9)], span(7, 9, 17)) == ()


def test_covered_by_needs_one_span_to_hold_the_whole_probe() -> None:
    """Two spans that jointly cover a probe do not count, which is why callers merge first."""
    assert covered_by([span(7, 9, 12), span(7, 13, 17)], span(7, 10, 11)) is True
    assert covered_by([span(7, 9, 12), span(7, 13, 17)], span(7, 11, 14)) is False
    assert covered_by([], span(7, 10, 11)) is False


def test_overlaps_any_finds_a_clash_anywhere_in_the_list() -> None:
    assert overlaps_any([span(7, 9, 10), span(7, 15, 16)], span(7, 15, 17)) is True
    assert overlaps_any([span(7, 9, 10), span(7, 15, 16)], span(7, 11, 12)) is False


# ------------------------------------------------------------------ alignment


def test_an_already_aligned_instant_is_left_alone() -> None:
    assert align_up(utc(7, 9, 30), QUARTER_HOUR) == utc(7, 9, 30)


@pytest.mark.parametrize(
    ("minute", "expected"),
    [(1, 15), (14, 15), (16, 30), (31, 45), (46, 0)],
)
def test_an_unaligned_instant_rounds_forward(minute: int, expected: int) -> None:
    aligned = align_up(utc(7, 9, minute), QUARTER_HOUR)
    assert aligned.minute == expected
    assert aligned > utc(7, 9, minute)


def test_alignment_never_moves_backwards_into_unavailable_time() -> None:
    """Rounding down would propose a start before the window opened, or before the lead-time
    cutoff -- both of which the caller relies on this to enforce."""
    for minute in range(60):
        assert align_up(utc(7, 9, minute), QUARTER_HOUR) >= utc(7, 9, minute)


def test_aligned_starts_stop_leaving_room_for_the_full_duration() -> None:
    starts = list(aligned_starts(span(7, 9, 11), HOUR, QUARTER_HOUR))
    assert starts[0] == utc(7, 9)
    assert starts[-1] == utc(7, 10)
    assert len(starts) == 5


def test_a_window_shorter_than_the_interview_yields_no_start() -> None:
    assert list(aligned_starts(span(7, 9, 10), timedelta(minutes=90), QUARTER_HOUR)) == []


def test_a_window_exactly_the_interview_length_yields_one_start() -> None:
    assert list(aligned_starts(span(7, 9, 10), HOUR, QUARTER_HOUR)) == [utc(7, 9)]


def test_an_unaligned_window_starts_on_the_grid_not_at_its_own_edge() -> None:
    starts = list(aligned_starts(Interval(utc(7, 9, 5), utc(7, 11)), HOUR, QUARTER_HOUR))
    assert starts[0] == utc(7, 9, 15)


# ------------------------------------------------------------------ business hours

NEW_YORK = ZoneInfo("America/New_York")
KOLKATA = ZoneInfo("Asia/Kolkata")


def test_a_slot_inside_the_working_day_passes() -> None:
    # 14:00 UTC is 10:00 in New York on a Monday in September.
    assert within_business_hours(span(7, 14, 15), NEW_YORK) is True


def test_a_slot_before_opening_or_after_closing_fails() -> None:
    assert within_business_hours(span(7, 12, 13), NEW_YORK) is False  # 08:00 local
    assert within_business_hours(span(7, 22, 23), NEW_YORK) is False  # 18:00 local


def test_a_slot_that_would_run_past_closing_fails() -> None:
    """The whole interview must fit. Half of it inside the day is not inside the day."""
    # 20:30 UTC is 16:30 local; a 60-minute interview would end at 17:30.
    assert within_business_hours(Interval(utc(7, 20, 30), utc(7, 21, 30)), NEW_YORK) is False
    assert within_business_hours(Interval(utc(7, 20), utc(7, 21)), NEW_YORK) is True


def test_a_slot_touching_the_open_or_close_passes() -> None:
    assert within_business_hours(Interval(utc(7, 13), utc(7, 14)), NEW_YORK) is True  # 09:00-10:00
    assert within_business_hours(Interval(utc(7, 20), utc(7, 21)), NEW_YORK) is True  # 16:00-17:00


def test_the_weekend_is_never_business_hours() -> None:
    # 2026-09-12 is a Saturday, 2026-09-13 a Sunday. 14:00 UTC is 10:00 local on both.
    assert within_business_hours(span(12, 14, 15), NEW_YORK) is False
    assert within_business_hours(span(13, 14, 15), NEW_YORK) is False


def test_a_half_hour_offset_zone_is_judged_on_its_own_clock() -> None:
    """§12 Phase 4 asks for a half-hour zone. Kolkata is UTC+5:30 all year, so 09:00 local is
    03:30 UTC -- a time that is the middle of the night in every zone this dataset otherwise uses."""
    assert within_business_hours(Interval(utc(7, 3, 30), utc(7, 4, 30)), KOLKATA) is True
    assert within_business_hours(Interval(utc(7, 3, 15), utc(7, 4, 15)), KOLKATA) is False  # 08:45 local
    assert within_business_hours(Interval(utc(7, 11), utc(7, 12)), KOLKATA) is False  # 16:30-17:30 local


def test_spring_forward_shortens_the_bookable_day() -> None:
    """§12 Phase 4, daylight-saving spring-forward. 2026-03-08 is the US transition, so New York
    that day runs 09:00 EST-less: the local working day is 13:00-21:00 UTC, an hour earlier than
    the day before, because the clocks went forward at 02:00.

    The check is on instants, so this needs no special case in the engine -- but it needs a test,
    because getting it wrong shifts every proposal by an hour for two weeks a year.
    """
    friday_before = datetime(2026, 3, 6, tzinfo=UTC)  # Friday 2026-03-06, still EST (UTC-5)
    assert within_business_hours(Interval(friday_before.replace(hour=14), friday_before.replace(hour=15)), NEW_YORK)
    assert not within_business_hours(Interval(friday_before.replace(hour=13), friday_before.replace(hour=14)), NEW_YORK)

    monday_after = datetime(2026, 3, 9, tzinfo=UTC)  # Monday after the switch, now EDT (UTC-4)
    assert within_business_hours(Interval(monday_after.replace(hour=13), monday_after.replace(hour=14)), NEW_YORK)
    assert not within_business_hours(Interval(monday_after.replace(hour=21), monday_after.replace(hour=22)), NEW_YORK)


def test_fall_back_repeats_an_hour_without_repeating_a_slot() -> None:
    """§12 Phase 4, daylight-saving fall-back. 2026-11-01 01:30 happens twice in New York.

    Both instants exist and are distinct, and each converts to a local time carrying its own
    offset -- which is what §9.3 means by resolving an ambiguous local time explicitly. Neither is
    inside business hours, so neither can be proposed; the point is that the two do not collapse
    into one another.
    """
    first_pass = datetime(2026, 11, 1, 5, 30, tzinfo=UTC)  # 01:30 EDT
    second_pass = datetime(2026, 11, 1, 6, 30, tzinfo=UTC)  # 01:30 EST, an hour later
    assert first_pass != second_pass
    assert first_pass.astimezone(NEW_YORK).hour == second_pass.astimezone(NEW_YORK).hour == 1
    assert first_pass.astimezone(NEW_YORK).utcoffset() != second_pass.astimezone(NEW_YORK).utcoffset()
    assert within_business_hours(Interval(first_pass, first_pass + HOUR), NEW_YORK) is False
    assert within_business_hours(Interval(second_pass, second_pass + HOUR), NEW_YORK) is False
