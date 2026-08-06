"""Empirical introspection of the installed MCP Python SDK.

Prints exact signatures so we never guess at API shape.
"""

from __future__ import annotations

import importlib
import importlib.metadata as md
import inspect


def sig(obj: object, label: str) -> None:
    try:
        print(f"\n### {label}\n{label.split('(')[0]}{inspect.signature(obj)}")  # type: ignore[arg-type]
    except Exception as exc:  # noqa: BLE001
        print(f"\n### {label}\n!! could not get signature: {exc!r}")


def head(title: str) -> None:
    print(f"\n\n{'=' * 78}\n== {title}\n{'=' * 78}")


print("mcp:", md.version("mcp"))
try:
    print("mcp-types:", md.version("mcp-types"))
except Exception:
    print("mcp-types: not installed")

head("MODULE PRESENCE")
for name in (
    "mcp.server.fastmcp",
    "mcp.server.mcpserver",
    "mcp.server.stdio",
    "mcp.server.runner",
    "mcp.server.context",
    "mcp.shared.memory",
    "mcp.client.session",
    "mcp.client.stdio",
    "mcp.types",
    "mcp_types",
):
    try:
        importlib.import_module(name)
        print(f"  OK      {name}")
    except Exception as exc:  # noqa: BLE001
        print(f"  MISSING {name}  ({type(exc).__name__}: {exc})")

head("mcp top-level exports")
import mcp  # noqa: E402

print(sorted(n for n in dir(mcp) if not n.startswith("_")))

head("MCPServer")
from mcp.server import MCPServer  # noqa: E402

sig(MCPServer.__init__, "MCPServer.__init__(")
print("\n-- public methods --")
print(sorted(n for n in dir(MCPServer) if not n.startswith("_")))

for meth in ("tool", "resource", "prompt", "run", "run_stdio_async", "completion", "custom_route"):
    fn = getattr(MCPServer, meth, None)
    if fn is not None:
        sig(fn, f"MCPServer.{meth}(")

head("MCPServer.tool docstring")
print(inspect.getdoc(MCPServer.tool))

head("MCPServer.resource docstring")
print(inspect.getdoc(MCPServer.resource))

head("MCPServer.prompt docstring")
print(inspect.getdoc(MCPServer.prompt))

head("MCPServer.run docstring")
print(inspect.getdoc(MCPServer.run))

head("Context")
try:
    from mcp.server.context import Context  # type: ignore[attr-defined]

    print("import path: mcp.server.context.Context")
    print(sorted(n for n in dir(Context) if not n.startswith("_")))
except Exception as exc:  # noqa: BLE001
    print("mcp.server.context.Context failed:", exc)
    import mcp.server.context as c

    print("mcp.server.context exports:", sorted(n for n in dir(c) if not n.startswith("_")))

head("errors / exceptions")
for modname in ("mcp.server.mcpserver", "mcp.shared.exceptions", "mcp.server.lowlevel"):
    try:
        m = importlib.import_module(modname)
        errs = [
            n
            for n in dir(m)
            if not n.startswith("_") and isinstance(getattr(m, n), type) and issubclass(getattr(m, n), BaseException)
        ]
        print(f"  {modname}: {errs}")
    except Exception as exc:  # noqa: BLE001
        print(f"  {modname}: {exc}")

head("mcp.shared.memory (in-process test client)")
try:
    import mcp.shared.memory as mem

    print(sorted(n for n in dir(mem) if not n.startswith("_")))
    for n in ("create_connected_server_and_client_session", "create_client_server_memory_streams"):
        f = getattr(mem, n, None)
        if f:
            sig(f, f"mcp.shared.memory.{n}(")
except Exception as exc:  # noqa: BLE001
    print("failed:", exc)

head("mcp.client exports")
try:
    import mcp.client as mc

    print(sorted(n for n in dir(mc) if not n.startswith("_")))
except Exception as exc:  # noqa: BLE001
    print("failed:", exc)
