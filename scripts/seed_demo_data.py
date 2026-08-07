"""Initialize and seed the local demo database.

    uv run python scripts/seed_demo_data.py                 # create if missing, then seed
    uv run python scripts/seed_demo_data.py --anchor now    # same data, moved to this week
    uv run python scripts/seed_demo_data.py --reset         # drop everything first

Reads the same ``TALENT_OPS_*`` configuration the server does, so it always writes the database
the server will read. Safe to re-run: the seed merges by primary key (see
:mod:`talent_ops_mcp.db.seed`), so a second run converges rather than duplicating, and is the
supported way to restore demo data someone has edited.

This is a developer tool, not the server, so it prints to stdout. The server itself never does --
stdout there carries the MCP protocol.
"""

import argparse
import asyncio
from datetime import UTC, datetime

from talent_ops_mcp.db.base import Base
from talent_ops_mcp.db.seed import SEED_ANCHOR, seed
from talent_ops_mcp.db.session import create_engine, create_session_factory, init_schema, session_scope
from talent_ops_mcp.logging_config import configure_logging
from talent_ops_mcp.settings import Settings


def parse_anchor(raw: str) -> datetime:
    """Resolve ``--anchor`` to a timezone-aware instant.

    ``fixed`` keeps the reproducible default; ``now`` snaps to today's UTC midnight so a demo has
    interview slots in the current week. An explicit ``YYYY-MM-DD`` is accepted for pinning a
    demo to a known date.
    """
    if raw == "fixed":
        return SEED_ANCHOR
    if raw == "now":
        return datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    try:
        return datetime.strptime(raw, "%Y-%m-%d").replace(tzinfo=UTC)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected 'fixed', 'now', or YYYY-MM-DD, got {raw!r}") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="seed_demo_data",
        description="Create and populate the Talent Ops demo database.",
    )
    parser.add_argument(
        "--anchor",
        type=parse_anchor,
        default="fixed",
        help="Week the dataset is built around: 'fixed' (default, reproducible), 'now', or YYYY-MM-DD.",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Drop every table before seeding. Destroys all local data, including anything not seeded.",
    )
    return parser


async def run(anchor: datetime, *, reset: bool) -> int:
    settings = Settings()
    configure_logging(settings.log_level)
    engine = create_engine(settings)
    try:
        if reset:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.drop_all)
            print("dropped all tables")
        await init_schema(engine)

        factory = create_session_factory(engine)
        async with session_scope(factory) as session:
            summary = await seed(session, anchor=anchor)
    finally:
        await engine.dispose()

    # The path is printed deliberately: the operator needs to know which file was written, and
    # unlike the server's responses this output never reaches a model.
    print(f"seeded {settings.resolved_database_path}")
    print(f"  anchor week      {anchor.date().isoformat()}")
    for field, value in (
        ("jobs", summary.jobs),
        ("candidates", summary.candidates),
        ("applications", summary.applications),
        ("interview plans", summary.interview_plans),
        ("interviewers", summary.interviewers),
        ("availability", summary.availability_windows),
        ("interviews", summary.interviews),
        ("feedback requests", summary.feedback_requests),
    ):
        print(f"  {field:<17}{value:>4}")
    print(f"  {'total':<17}{summary.total():>4}")
    return 0


def main() -> int:
    args = build_parser().parse_args()
    anchor = args.anchor if isinstance(args.anchor, datetime) else parse_anchor(args.anchor)
    return asyncio.run(run(anchor, reset=args.reset))


if __name__ == "__main__":
    raise SystemExit(main())
