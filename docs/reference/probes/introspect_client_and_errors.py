"""Deeper introspection: client API, tool annotations, errors, context, stdio."""

from __future__ import annotations

import importlib
import inspect


def sig(obj: object, label: str) -> None:
    try:
        print(f"\n{label}{inspect.signature(obj)}")  # type: ignore[arg-type]
    except Exception as exc:  # noqa: BLE001
        print(f"\n{label} !! {exc!r}")


def head(t: str) -> None:
    print(f"\n\n{'=' * 78}\n== {t}\n{'=' * 78}")


head("mcp.client.Client")
from mcp.client import Client  # noqa: E402

sig(Client.__init__, "Client.__init__")
print("methods:", sorted(n for n in dir(Client) if not n.startswith("_")))
print("\ndocstring:\n", inspect.getdoc(Client))

head("mcp.client.Transport + transports")
import mcp.client as mc  # noqa: E402

print("Transport:", inspect.getdoc(mc.Transport))
try:
    print("\nTransport methods:", sorted(n for n in dir(mc.Transport) if not n.startswith("_")))
except Exception as exc:  # noqa: BLE001
    print(exc)

for modname in ("mcp.client.stdio", "mcp.client.session"):
    m = importlib.import_module(modname)
    print(f"\n{modname}:", sorted(n for n in dir(m) if not n.startswith("_")))

head("StdioTransport / stdio_client")
import mcp.client.stdio as cs  # noqa: E402

for n in ("stdio_client", "StdioTransport", "StdioServerParameters"):
    o = getattr(cs, n, None)
    if o is not None:
        sig(o if not isinstance(o, type) else o.__init__, f"{n}")

head("ClientSession")
from mcp.client.session import ClientSession  # noqa: E402

sig(ClientSession.__init__, "ClientSession.__init__")
print("methods:", sorted(n for n in dir(ClientSession) if not n.startswith("_")))

head("ToolAnnotations")
from mcp.types import ToolAnnotations  # noqa: E402

print("import: mcp.types.ToolAnnotations")
print("fields:", list(ToolAnnotations.model_fields))
for k, v in ToolAnnotations.model_fields.items():
    print(f"  {k}: {v.annotation}  alias={v.alias!r} default={v.default!r}")

head("Context detail")
from mcp.server.context import Context  # noqa: E402

for n in ("log", "report_progress", "notify", "lifespan", "meta", "session_id", "cancel_requested"):
    o = getattr(Context, n, None)
    if o is None:
        print(f"  {n}: MISSING")
    elif isinstance(o, property):
        print(f"  {n}: property -> {inspect.getdoc(o)!r:.200}")
    else:
        sig(o, f"  Context.{n}")
print("\nContext class doc:\n", inspect.getdoc(Context))
for stale in ("info", "debug", "warning", "error"):
    print(f"  has ctx.{stale}? {hasattr(Context, stale)}")

head("MCPError / error signalling")
from mcp.shared.exceptions import MCPError  # noqa: E402

sig(MCPError.__init__, "MCPError.__init__")
print(inspect.getdoc(MCPError))
import mcp.server.mcpserver as msv  # noqa: E402

print("\nmcp.server.mcpserver exports:", sorted(n for n in dir(msv) if not n.startswith("_")))

head("tool manager: how results/errors are built")
for modname in ("mcp.server.mcpserver", "mcp.server.runner", "mcp.server.stdio"):
    m = importlib.import_module(modname)
    print(f"\n-- {modname} --")
    print(sorted(n for n in dir(m) if not n.startswith("_")))

head("mcp.server.stdio source")
import mcp.server.stdio as ss  # noqa: E402

print(inspect.getsource(ss)[:4000])
