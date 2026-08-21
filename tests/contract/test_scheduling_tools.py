"""``talent_ops_propose_interview_slots`` as a model actually meets it: over a client session.

``tests/integration/test_scheduling_service.py`` covers the scheduling rules. What is only
observable here is the contract around them -- the advertised schemas, the bounds the SDK enforces
before the tool body runs, and what a refusal looks like on the wire.

Every test drives a real ``build_server(settings)`` against a pre-seeded database file, so the
lifespan, engine, session factory, and foreign-key pragma are the production ones.

**These calls cannot pin the clock.** The tool reads ``datetime.now(UTC)`` -- the one place the
scheduling path is allowed to -- so a database seeded at the default anchor would start returning
nothing the moment that week fell into the past, and this file would go red on a calendar date
rather than on a code change. The fixtures below therefore reseed the demo dataset onto the week
after next, using the ``anchor`` parameter the seed already has for live demos. Exact instants stay
in the service tests, where ``now`` is an argument.
"""

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from mcp.client import Client
from mcp.types import CallToolResult

from talent_ops_mcp.db.seed import seed
from talent_ops_mcp.db.session import create_engine, create_session_factory, init_schema
from talent_ops_mcp.models.outputs import SlotUnavailableReason
from talent_ops_mcp.server import TOOL_PREFIX, build_server
from talent_ops_mcp.services.scheduling import MAX_OPTIONS
from talent_ops_mcp.settings import Settings

PROPOSE = f"{TOOL_PREFIX}_propose_interview_slots"


@pytest.fixture
def demo_monday() -> date:
    """The Monday of the week after next, whenever the suite runs.

    Between eight and fourteen days out, which clears the demo plans' longest lead time (72 hours)
    with room to spare no matter which day of the week the suite is run on.
    """
    today = datetime.now(UTC).date()
    return today + timedelta(days=14 - today.weekday())


@pytest.fixture
def week(demo_monday: date) -> dict[str, str]:
    """``start_date``/``end_date`` covering that Monday to Friday."""
    return {
        "start_date": demo_monday.isoformat(),
        "end_date": (demo_monday + timedelta(days=4)).isoformat(),
    }


@pytest.fixture
async def upcoming_settings(settings: Settings, demo_monday: date) -> Settings:
    """``seeded_settings``, but with the whole dataset shifted onto :func:`demo_monday`.

    Every seeded instant is an offset from the anchor, so shifting it preserves every relative
    relationship the demo encodes -- the booked interview, the cancelled one, the uneven
    availability -- while putting them all in front of the clock rather than behind it.
    """
    engine = create_engine(settings)
    try:
        await init_schema(engine)
        async with create_session_factory(engine)() as session:
            await seed(session, anchor=datetime(demo_monday.year, demo_monday.month, demo_monday.day, tzinfo=UTC))
            await session.commit()
    finally:
        await engine.dispose()
    return settings


async def call(settings: Settings, arguments: dict[str, Any]) -> CallToolResult:
    async with Client(build_server(settings)) as client:
        return await client.call_tool(PROPOSE, arguments)


async def structured(settings: Settings, arguments: dict[str, Any]) -> dict[str, Any]:
    result = await call(settings, arguments)
    assert result.is_error is False, result.content
    body = result.structured_content
    assert isinstance(body, dict)
    return body


def error_text(result: CallToolResult) -> str:
    assert result.is_error is True
    assert result.structured_content is None
    return "\n".join(block.text for block in result.content if block.type == "text")


# ------------------------------------------------------------------ discovery


async def test_the_tool_is_discoverable_with_read_only_hints(settings: Settings) -> None:
    """It computes and returns; it books nothing. The hints have to say so, or a client will
    prompt for confirmation on a tool that changes no state -- or worse, not prompt on one that
    does, once Phase 5 registers its neighbours here."""
    async with Client(build_server(settings)) as client:
        listed = await client.list_tools()

    tool = next(t for t in listed.tools if t.name == PROPOSE)
    assert tool.annotations is not None
    assert tool.annotations.read_only_hint is True
    assert tool.annotations.destructive_hint is False
    assert tool.annotations.idempotent_hint is True
    assert tool.annotations.open_world_hint is False
    assert tool.annotations.title == "Propose interview times"
    assert tool.output_schema is not None


async def test_the_description_says_it_is_not_a_booking(settings: Settings) -> None:
    """The single most consequential thing a model can get wrong about this tool. §9.3 says an
    option reserves nothing, so the description has to say it too."""
    async with Client(build_server(settings)) as client:
        listed = await client.list_tools()

    description = next(t for t in listed.tools if t.name == PROPOSE).description or ""
    assert "Do not use:" in description
    assert "reserves nothing" in description
    assert "Nothing is held" in description


async def test_the_advertised_date_range_bound_is_the_one_the_server_honours(tmp_path: Path) -> None:
    """A schema promising 31 days on a server configured for 3 would be a lie the model cannot
    detect -- its request would simply be refused, with no way to have known."""
    narrow = Settings(env="test", data_dir=tmp_path, max_date_range_days=3)
    async with Client(build_server(narrow)) as client:
        listed = await client.list_tools()

    schema = next(t for t in listed.tools if t.name == PROPOSE).input_schema
    assert "3 days" in schema["properties"]["end_date"]["description"]


async def test_the_option_count_is_bounded_in_the_schema(settings: Settings) -> None:
    async with Client(build_server(settings)) as client:
        listed = await client.list_tools()

    schema = next(t for t in listed.tools if t.name == PROPOSE).input_schema
    assert schema["properties"]["max_options"]["maximum"] == MAX_OPTIONS
    assert schema["properties"]["max_options"]["minimum"] == 1
    assert schema["required"] == ["application_id"]


async def test_dates_are_advertised_as_dates(settings: Settings) -> None:
    """``format: date`` is what makes the SDK reject "next Tuesday" before the tool body runs,
    rather than the service having to parse whatever a model felt like sending."""
    async with Client(build_server(settings)) as client:
        listed = await client.list_tools()

    schema = next(t for t in listed.tools if t.name == PROPOSE).input_schema
    for field in ("start_date", "end_date"):
        variants = schema["properties"][field]["anyOf"]
        assert {"type": "string", "format": "date"} in variants


# ------------------------------------------------------------------ over the wire


async def test_proposing_slots_over_the_wire(upcoming_settings: Settings, week: dict[str, str]) -> None:
    body = await structured(upcoming_settings, {"application_id": "app_02", **week, "max_options": 5})

    assert body["application_id"] == "app_02"
    assert body["job_title"] == "Senior Platform Engineer"
    assert body["duration_minutes"] == 60
    assert body["display_timezone"] == "America/New_York"
    assert body["no_options_code"] is None
    assert body["why_these_are_valid"]
    assert len(body["options"]) >= 3
    for option in body["options"]:
        assert option["display_timezone"] == "America/New_York"
        assert len(option["panel"]) == 1
        assert option["panel"][0]["name"]


async def test_the_structured_content_matches_the_text_block(upcoming_settings: Settings, week: dict[str, str]) -> None:
    """v2 returns both. A client reading either must see the same proposal."""
    result = await call(upcoming_settings, {"application_id": "app_02", **week})
    assert result.is_error is False

    text = "\n".join(block.text for block in result.content if block.type == "text")
    assert json.loads(text) == result.structured_content


async def test_no_options_is_a_success_with_a_reason(upcoming_settings: Settings, week: dict[str, str]) -> None:
    """Not an error. No availability is an answer about the world, and a caller handed an error
    instead has to guess whether it asked wrongly or the diary is simply full."""
    body = await structured(upcoming_settings, {"application_id": "app_01", **week})

    assert body["options"] == []
    assert body["why_these_are_valid"] is None
    assert body["no_options_code"] in {reason.value for reason in SlotUnavailableReason}
    assert body["no_options_reason"]


async def test_a_timezone_override_is_honoured(upcoming_settings: Settings, week: dict[str, str]) -> None:
    body = await structured(
        upcoming_settings, {"application_id": "app_02", **week, "timezone": "Europe/Dublin", "max_options": 1}
    )
    assert body["display_timezone"] == "Europe/Dublin"
    assert body["options"][0]["start_local"].endswith("+01:00")


# ------------------------------------------------------------------ refusals


@pytest.mark.parametrize(
    "arguments",
    [
        {"application_id": "app_02", "start_date": "next tuesday"},
        {"application_id": "app_02", "start_date": "2026-13-01"},
        {"application_id": "app_02", "max_options": 0},
        {"application_id": "app_02", "max_options": MAX_OPTIONS + 1},
        {"application_id": "x" * 65},
        {},
    ],
    ids=["prose date", "impossible month", "zero options", "too many options", "over-long id", "no application"],
)
async def test_input_outside_the_advertised_schema_is_refused(
    upcoming_settings: Settings, arguments: dict[str, Any]
) -> None:
    """Rejected by the SDK before the tool body runs, so none of these reaches the service."""
    assert (await call(upcoming_settings, arguments)).is_error is True


async def test_an_unknown_application_is_a_coded_not_found(upcoming_settings: Settings, week: dict[str, str]) -> None:
    text = error_text(await call(upcoming_settings, {"application_id": "app_nope", **week}))

    assert "NOT_FOUND:" in text
    assert "talent_ops_list_applications" in text


async def test_an_inactive_application_is_a_coded_conflict(upcoming_settings: Settings, week: dict[str, str]) -> None:
    """``app_09`` is on hold. §9.2 step 1 refuses it rather than proposing times a coordinator
    would then have to un-propose."""
    text = error_text(await call(upcoming_settings, {"application_id": "app_09", **week}))

    assert "CONFLICT:" in text
    assert "on_hold" in text
    assert "recruiter" in text


async def test_an_over_long_range_is_a_coded_validation_error(
    upcoming_settings: Settings, week: dict[str, str]
) -> None:
    text = error_text(
        await call(
            upcoming_settings, {"application_id": "app_02", "start_date": "2026-09-07", "end_date": "2026-12-07"}
        )
    )

    assert "VALIDATION_ERROR:" in text
    assert "31" in text


async def test_a_timezone_that_is_not_iana_is_refused_with_an_example(
    upcoming_settings: Settings, week: dict[str, str]
) -> None:
    text = error_text(await call(upcoming_settings, {"application_id": "app_02", **week, "timezone": "EST5EDT-bogus"}))

    assert "VALIDATION_ERROR:" in text
    assert "America/New_York" in text


async def test_a_failure_leaks_no_path_or_connection_string(upcoming_settings: Settings, week: dict[str, str]) -> None:
    text = error_text(await call(upcoming_settings, {"application_id": "app_nope", **week}))

    lowered = text.lower()
    assert "sqlite" not in lowered
    assert "aiosqlite" not in lowered
    assert str(upcoming_settings.data_dir).lower() not in lowered
    assert "traceback" not in lowered


# ------------------------------------------------------------------ the boundary


async def test_a_proposal_carries_no_contact_details_for_anyone(
    upcoming_settings: Settings, week: dict[str, str]
) -> None:
    """Least data (§8.2). Inviting people is Phase 5's job, through a confirmed operation; a
    proposal needs a name and a timezone and nothing else."""
    body = await structured(upcoming_settings, {"application_id": "app_02", **week, "max_options": 5})

    blob = json.dumps(body).lower()
    assert "@talent-ops.test" not in blob
    assert "email" not in blob
    assert "phone" not in blob


async def test_the_reason_for_no_options_names_nobody(upcoming_settings: Settings, week: dict[str, str]) -> None:
    """A reason may say a panel was not free. It may not say who was busy, or when -- that is
    somebody else's calendar, and the caller has no business with it."""
    body = await structured(upcoming_settings, {"application_id": "app_01", **week})

    reason = body["no_options_reason"]
    for name in ("Avery", "Blake", "Casey", "Devon", "Ellis", "Frankie"):
        assert name not in reason
