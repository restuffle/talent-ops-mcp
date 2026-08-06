"""Verify the critique's blocker claims empirically."""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from mcp.client import Client
from mcp.server import MCPServer
from mcp.server.mcpserver import Context


@dataclass
class AppState:
    db: str


@contextlib.asynccontextmanager
async def lifespan(server: MCPServer[AppState]) -> AsyncIterator[AppState]:
    yield AppState(db="engine-handle")


def check(label: str, fn: Any) -> None:
    try:
        fn()
        print(f"[OK]     {label}")
    except Exception as exc:  # noqa: BLE001
        print(f"[RAISED] {label}\n           {type(exc).__name__}: {str(exc)[:300]}")


# ---- A1: does a STATIC resource accept a Context param? -------------------
print("=== A1: Context on a STATIC resource (expect raise at decoration) ===")
s1: MCPServer[AppState] = MCPServer(name="a1", version="0", lifespan=lifespan)


def _static_with_ctx() -> None:
    @s1.resource("recruiting://policies/scheduling")
    def policy(ctx: Context) -> str:  # type: ignore[type-arg]
        return "x"


check("static resource + bare Context", _static_with_ctx)


def _static_no_ctx() -> None:
    @s1.resource("recruiting://policies/candidate-communication")
    def policy2() -> str:
        return "x"


check("static resource, no Context", _static_no_ctx)

print("\n=== A1b: Context on a TEMPLATE resource ===")
s2: MCPServer[AppState] = MCPServer(name="a1b", version="0", lifespan=lifespan)


def _tpl_bare_ctx() -> None:
    @s2.resource("recruiting://jobs/{job_id}/interview-plan")
    def plan(job_id: str, ctx: Context) -> str:  # type: ignore[type-arg]
        return job_id


check("template resource + bare Context", _tpl_bare_ctx)

s3: MCPServer[AppState] = MCPServer(name="a1c", version="0", lifespan=lifespan)


def _tpl_typed_ctx() -> None:
    @s3.resource("recruiting://jobs/{job_id}/plan2")
    def plan2(job_id: str, ctx: Context[AppState]) -> str:  # type: ignore[type-arg]
        return job_id


check("template resource + Context[AppState] (parameterized)", _tpl_typed_ctx)


# ---- runtime: can a template resource actually reach lifespan state? ------
async def main() -> None:
    print("\n=== A1c: template resource reaching lifespan at RUNTIME ===")
    srv: MCPServer[AppState] = MCPServer(name="rt", version="0", lifespan=lifespan)

    @srv.resource("recruiting://jobs/{job_id}/interview-plan")
    async def plan(job_id: str, ctx: Context) -> str:  # type: ignore[type-arg]
        try:
            st = ctx.request_context.lifespan_context
            return json.dumps({"job_id": job_id, "lifespan": repr(st)})
        except Exception as exc:  # noqa: BLE001
            return json.dumps({"job_id": job_id, "lifespan_ERROR": f"{type(exc).__name__}: {exc}"})

    @srv.prompt()
    async def guided(candidate_id: str, ctx: Context) -> str:  # type: ignore[type-arg]
        try:
            st = ctx.request_context.lifespan_context
            return f"ok {candidate_id} {st!r}"
        except Exception as exc:  # noqa: BLE001
            return f"PROMPT_ERROR {type(exc).__name__}: {exc}"

    @srv.tool()
    async def t(ctx: Context[AppState]) -> str:
        return repr(ctx.request_context.lifespan_context)

    async with Client(srv) as c:
        r = await c.read_resource("recruiting://jobs/j1/interview-plan")
        print("  template read ->", r.contents[0].text)  # type: ignore[union-attr]
        g = await c.get_prompt("guided", {"candidate_id": "c1"})
        print("  prompt        ->", g.messages[0].content.text)  # type: ignore[union-attr]
        tr = await c.call_tool("t", {})
        print("  tool          ->", tr.structured_content)

    print("\n=== A5: does default Client(mcp) mode skip JSON-RPC framing? ===")
    for mode in ("auto", "legacy"):
        try:
            async with Client(srv, mode=mode) as c:  # type: ignore[arg-type]
                tl = await c.list_tools()
                print(f"  mode={mode:7s} -> OK, protocol_version={c.protocol_version}, tools={len(tl.tools)}")
        except Exception as exc:  # noqa: BLE001
            print(f"  mode={mode:7s} -> {type(exc).__name__}: {str(exc)[:200]}")


asyncio.run(main())
