"""Live end-to-end probe of mcp 2.0.0 against every primitive this project needs.

Proves (or disproves) empirically:
  1. custom-scheme static resource  recruiting://policies/scheduling
  2. RFC6570 template resource      recruiting://jobs/{job_id}/interview-plan
  3. Pydantic return -> outputSchema + structuredContent
  4. ToolAnnotations round-trip (readOnlyHint / destructiveHint / idempotentHint)
  5. lifespan startup/shutdown + how a tool reads lifespan state
  6. what Context object a tool actually receives
  7. error signalling: plain exception vs MCPError -- and whether a traceback leaks
  8. prompts with arguments
  9. stdout purity under the in-memory client
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import sys
import traceback
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field

from mcp.client import Client
from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.types import ToolAnnotations

RESULTS: dict[str, Any] = {}


def record(key: str, value: Any) -> None:
    RESULTS[key] = value
    print(f"\n--- {key} ---")
    print(value if isinstance(value, str) else json.dumps(value, indent=2, default=str)[:3000])


# ---------------------------------------------------------------- lifespan


@dataclass
class AppState:
    db_handle: str
    started: bool


LIFESPAN_LOG: list[str] = []


@contextlib.asynccontextmanager
async def lifespan(server: MCPServer[AppState]) -> AsyncIterator[AppState]:
    LIFESPAN_LOG.append(f"startup(server_name={server.name})")
    state = AppState(db_handle="sqlite://memory", started=True)
    try:
        yield state
    finally:
        LIFESPAN_LOG.append("shutdown")


mcp: MCPServer[AppState] = MCPServer(
    name="probe",
    version="0.1.0",
    instructions="Probe server. Coordination only.",
    lifespan=lifespan,
)


# ---------------------------------------------------------------- models


class JobSummary(BaseModel):
    """A job summary."""

    job_id: str = Field(description="Stable job identifier.")
    title: str = Field(description="Job title.")
    status: str = Field(description="open or closed.")


class JobsSearchResult(BaseModel):
    """Structured output model."""

    jobs: list[JobSummary] = Field(default_factory=list, description="Matching jobs.")
    total: int = Field(ge=0, description="Count returned.")
    next_cursor: str | None = Field(default=None, description="Opaque cursor.")


# ---------------------------------------------------------------- tools


@mcp.tool(
    title="Search jobs",
    description="Search jobs. Read-only.",
    annotations=ToolAnnotations(
        title="Search jobs", read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
    ),
)
def jobs_search(
    query: str = Field(default="", max_length=100, description="Bounded search text."),
    limit: int = Field(default=10, ge=1, le=50, description="Max results."),
) -> JobsSearchResult:
    return JobsSearchResult(
        jobs=[JobSummary(job_id="job_001", title="Backend Engineer", status="open")][:limit],
        total=1,
    )


@mcp.tool(description="Reads lifespan state and the context object.")
async def probe_context(ctx: Context[AppState]) -> str:
    out: dict[str, Any] = {"ctx_type": f"{type(ctx).__module__}.{type(ctx).__name__}"}

    for label, get in {
        "ctx.lifespan": lambda: ctx.lifespan,
        "ctx.request_context.lifespan_context": lambda: ctx.request_context.lifespan_context,
        "ctx.request_id": lambda: ctx.request_id,
        "ctx.request_context type": lambda: (
            type(ctx.request_context).__module__ + "." + type(ctx.request_context).__name__
        ),
    }.items():
        try:
            out[label] = repr(get())
        except Exception as exc:  # noqa: BLE001
            out[label] = f"ERROR {type(exc).__name__}: {exc}"
    return json.dumps(out, indent=2)


@mcp.tool(description="Raises a plain Python exception with a secret in the message.")
def boom_plain() -> str:
    raise ValueError("SECRET_CONNECTION_STRING=sqlite:///C:/secret/path.db")


class DomainError(Exception):
    pass


@mcp.tool(description="Raises a custom domain exception.")
def boom_domain() -> str:
    raise DomainError("NOT_FOUND: job does not exist")


# ---------------------------------------------------------------- resources


@mcp.resource(
    "recruiting://policies/scheduling",
    name="scheduling-policy",
    title="Scheduling policy",
    description="Working hours and buffers.",
    mime_type="text/markdown",
)
def scheduling_policy() -> str:
    return "# Scheduling policy\n\nCoordination only."


@mcp.resource(
    "recruiting://jobs/{job_id}/interview-plan",
    name="interview-plan",
    title="Interview plan",
    mime_type="application/json",
)
def interview_plan(job_id: str) -> str:
    return json.dumps({"job_id": job_id, "duration_minutes": 60})


# ---------------------------------------------------------------- prompts


@mcp.prompt(title="Schedule a candidate interview")
def schedule_candidate_interview(candidate_id: str, job_id: str) -> str:
    return f"Coordinate scheduling for {candidate_id} on {job_id}. Do not evaluate the candidate."


# ---------------------------------------------------------------- driver


async def main() -> None:
    async with Client(mcp) as client:
        record("server_info", str(client.server_info))
        record("instructions", str(client.instructions))
        record("protocol_version", str(client.protocol_version))
        record("server_capabilities", str(client.server_capabilities))

        tools = await client.list_tools()
        record("Tool_model_field_names", list(type(tools.tools[0]).model_fields))
        record("tools", [t.model_dump(exclude_none=True) for t in tools.tools])

        r = await client.call_tool("jobs_search", {"query": "backend", "limit": 5})
        record("CallToolResult_field_names", list(type(r).model_fields))
        record("call_jobs_search", r.model_dump(exclude_none=True))

        r = await client.call_tool("probe_context", {})
        record("call_probe_context", r.model_dump(exclude_none=True))

        for name in ("boom_plain", "boom_domain"):
            try:
                r = await client.call_tool(name, {})
                payload: dict[str, Any] = {"raised": False, "result": r.model_dump(exclude_none=True)}
            except Exception as exc:  # noqa: BLE001
                payload = {"raised": True, "exc_type": type(exc).__name__, "exc": str(exc)}
            blob = json.dumps(payload, default=str)
            payload["LEAKS_SECRET"] = "SECRET_CONNECTION_STRING" in blob
            payload["LEAKS_TRACEBACK"] = "Traceback" in blob or "e2e_probe.py" in blob
            record(f"call_{name}", payload)

        res = await client.list_resources()
        record("Resource_field_names", list(type(res.resources[0]).model_fields))
        record("resources", [x.model_dump(exclude_none=True) for x in res.resources])

        tpl = await client.list_resource_templates()
        record("resource_templates", tpl.model_dump(exclude_none=True))

        rr = await client.read_resource("recruiting://policies/scheduling")
        record("read_static_resource", rr.model_dump(exclude_none=True))

        rr = await client.read_resource("recruiting://jobs/job_001/interview-plan")
        record("read_template_resource", rr.model_dump(exclude_none=True))

        for bad, why in [
            ("recruiting://jobs/..%2F..%2Fetc%2Fpasswd/interview-plan", "encoded traversal"),
            ("recruiting://jobs/nope/interview-plan", "unknown id"),
        ]:
            try:
                rr = await client.read_resource(bad)
                record(f"read_template[{why}]", rr.model_dump(exclude_none=True))
            except Exception as exc:  # noqa: BLE001
                record(f"read_template[{why}]", f"REJECTED {type(exc).__name__}: {exc}")

        pl = await client.list_prompts()
        record("Prompt_field_names", list(type(pl.prompts[0]).model_fields))
        record("prompts", [p.model_dump(exclude_none=True) for p in pl.prompts])

        gp = await client.get_prompt("schedule_candidate_interview", {"candidate_id": "c1", "job_id": "j1"})
        record("get_prompt", gp.model_dump(exclude_none=True))

    record("lifespan_log", LIFESPAN_LOG)


if __name__ == "__main__":
    captured = io.StringIO()
    real_stdout = sys.stdout
    try:
        sys.stdout = captured
        asyncio.run(main())
    except Exception:
        sys.stdout = real_stdout
        traceback.print_exc()
        print("\n\n===== CAPTURED BEFORE FAILURE =====")
        print(captured.getvalue())
        raise
    finally:
        sys.stdout = real_stdout
    print(captured.getvalue())
