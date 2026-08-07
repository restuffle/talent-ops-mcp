# Verified MCP Python SDK v2 API Reference

**Status:** Frozen for Phase 0
**SDK under test:** `mcp==2.0.0`, `mcp-types==2.0.0` (released 2026-07-28)
**Python under test:** 3.11.15 (Windows 11, `uv` 0.11.32)
**Verified:** 2026-08-06
**Method:** the package was installed into a throwaway environment and exercised end to end with a live
in-process MCP client. Every statement below marked **VERIFIED** was produced by running code on this
machine, not by reading documentation.

> Why this document exists: `PLAN.md` §5.2 and `PROMPT.md` §4 were written on 2026-07-24, when v1.x was
> stable and v2 was pre-release. `mcp` 2.0.0 went GA four days later. The v2 API is a **major rework**, and
> several things the plan names (notably `FastMCP`) no longer exist. Phases 1–8 must be written against
> what is in this file, not against v1 idiom or the SDK's own stale docstrings.

---

## 1. The headline change: `FastMCP` no longer exists

`mcp.server.fastmcp` is **not importable** in 2.0.0. There is no alias and no deprecation shim, so v1 code
fails immediately with `ModuleNotFoundError` rather than warning.

**VERIFIED** — module presence probe:

```text
MISSING mcp.server.fastmcp  (ModuleNotFoundError: No module named 'mcp.server.fastmcp')
OK      mcp.server.mcpserver
OK      mcp.server.stdio
OK      mcp.shared.memory
OK      mcp_types
```

`src/mcp/server/__init__.py` exports exactly:

```python
__all__ = ["CacheHint", "Server", "ServerRequestContext", "MCPServer", "NotificationOptions", "InitializationOptions"]
```

Wherever `PLAN.md` or `PROMPT.md` says "FastMCP", read **`MCPServer`**.

---

## 2. Canonical imports

```python
from mcp.server import MCPServer  # the server class
from mcp.server.mcpserver import Context  # the Context tools/resources/prompts receive
from mcp.types import ToolAnnotations  # tool hints
from mcp.types import INVALID_PARAMS, INTERNAL_ERROR, INVALID_REQUEST  # JSON-RPC codes
from mcp.shared.exceptions import MCPError  # also re-exported as mcp.MCPError
from mcp.client import Client  # test client (also `from mcp import Client`)
```

### 2.1 There are two different `Context` classes — use the right one

This is a real trap. Both exist, both are named `Context`, and only one is what handlers receive.

| Class | Handlers receive it? | Has `.lifespan`? | Has `.info/.debug/.warning/.error`? |
|---|---|---|---|
| `mcp.server.mcpserver.Context` | **Yes** | No | Yes |
| `mcp.server.context.Context` | No — *"Not currently constructed by `ServerRunner`"* | Yes | No |

**VERIFIED** — a tool annotated `ctx: Context` receives `mcp.server.mcpserver.context.Context`, and
`ctx.lifespan` raises `AttributeError` on it. Import `Context` from **`mcp.server.mcpserver`**.

---

## 3. Server construction

Positional order changed in v2 (`title` and `description` were inserted before `instructions`). **Pass
everything except `name` by keyword.**

```python
MCPServer(
    name=None, title=None, description=None, instructions=None, website_url=None,
    icons=None, version="", auth_server_provider=None, token_verifier=None, *,
    tools=None, resources=None, extensions=None, debug=False, log_level="INFO",
    warn_on_duplicate_resources=True, warn_on_duplicate_tools=True,
    warn_on_duplicate_prompts=True, dependencies=None, lifespan=None, auth=None,
    resource_security=ResourceSecurity(reject_path_traversal=True, reject_absolute_paths=True,
                                       reject_null_bytes=True, exempt_params=frozenset()),
    request_state_security=None, cache_hints=None, subscriptions=None, middleware=None,
)
```

Two consequences for this project:

- `version` defaults to `""`, **not** the SDK version. It must be passed explicitly or `health_check` and
  `serverInfo` will report an empty string.
- `MCP_*` environment variables and `.env` files are **no longer read by the SDK** in v2. All configuration
  is ours to load via `pydantic-settings`. This is a benefit — see `docs/configuration.md`.

### 3.1 Capabilities are auto-declared

**VERIFIED** — with tools, resources and prompts registered, the client observed:

```text
prompts=PromptsCapability(list_changed=True)
resources=ResourcesCapability(subscribe=True, list_changed=True)
tools=ToolsCapability(list_changed=True)
logging=None  completions=None  tasks=None  experimental=None
```

No manual capability declaration is required.

---

## 4. Transport and entrypoints

`run()` is **synchronous** and defaults to stdio:

```python
def run(self, transport: Literal["stdio", "sse", "streamable-http"] = "stdio", **kwargs: Any) -> None
```

Internally it calls **`anyio.run(self.run_stdio_async)`** — not `asyncio.run`. The async form
`await server.run_stdio_async()` is available if we need to own the event loop.

### 4.1 stdout is protected by the SDK

v2's `stdio_server()` claims file descriptors 0 and 1, serves the protocol from a *private duplicate*, and
diverts anything written to the original stdout to stderr. From the SDK source:

> Release deregisters only after `dup2(private_fd, fd)` restores the wire; a failed release keeps the
> claim, so successors are refused, never fed a diverted descriptor.

The `stdio_server()` docstring states it plainly:

> While serving, fd 0 points at the null device and fd 1 at stderr, so handlers and children read EOF and
> their stray output misses the wire; both descriptors are restored on exit. Explicit streams skip the
> claim, and a second concurrent `stdio_server()` raises `RuntimeError`.

This materially de-risks `PLAN.md` §5.3. It is a safety net, not a licence:

- Output flushed **before** serving begins still lands on the wire.
- A `print()` buffered until interpreter exit still lands on the wire.

**Rule for this project:** never call `print()`, and emit nothing at import time.

### 4.1.1 `MCPServer.__init__` already configures stderr logging

Constructing an `MCPServer` calls `logging.basicConfig()` with a handler pointed at stderr, at the level
passed as `log_level=`. Because `basicConfig()` never replaces existing handlers, **our own configuration
wins only if it runs before the server is constructed.**

**Rule for this project:** `logging_config.py` installs a `StreamHandler(sys.stderr)` and is invoked from
the entrypoint *before* `MCPServer(...)` is created. Never add a `StreamHandler(sys.stdout)`.

### 4.2 MCP protocol logging is deprecated — use stderr

**VERIFIED** — calling `ctx.log(...)` emits:

```text
MCPDeprecationWarning: The logging capability is deprecated as of 2026-07-28 (SEP-2577).
```

`MCPDeprecationWarning` subclasses `UserWarning` (not `DeprecationWarning`), so it is visible by default.
The plan's stderr-logging design is the recommended path; do not build on `ctx.log`/`ctx.info`.

---

## 5. Tools

```python
def tool(self, name=None, title=None, description=None,
         annotations: ToolAnnotations | None = None,
         icons=None, meta=None, structured_output: bool | None = None)
```

### 5.1 Structured output is automatic from the return annotation — **VERIFIED**

A Pydantic `BaseModel` return annotation produces a full `output_schema`, and the result carries
`structured_content` **plus** a JSON text block for backward compatibility. `Field(description=...)`,
`ge`, `le`, and `max_length` all propagate into the schemas.

Observed for `def jobs_search(query: str = Field(..., max_length=100), limit: int = Field(..., ge=1, le=50)) -> JobsSearchResult`:

```json
"input_schema": {"type":"object","properties":{
  "query":{"default":"","description":"Bounded search text.","maxLength":100,"type":"string"},
  "limit":{"default":10,"description":"Max results.","maximum":50,"minimum":1,"type":"integer"}}}
```

and the call result:

```json
{"content":[{"type":"text","text":"{ ... }"}],
 "structured_content":{"jobs":[{"job_id":"job_001","title":"Backend Engineer","status":"open"}],
                       "total":1,"next_cursor":null},
 "is_error": false, "result_type": "complete"}
```

A non-model return (e.g. `-> str`) is auto-wrapped as `{"result": ...}`.

**Decision: every tool returns a Pydantic `BaseModel`.** Never `TypedDict` — SDK issue #3224 injects
explicit `null`s for `NotRequired` keys, and #3227 breaks `NotRequired` on Python 3.10. Both are avoided
entirely by using `BaseModel`.

### 5.1.1 `Annotated[T, Field(...)]` parameters — **VERIFIED** (Phase 3)

Both spellings of a bounded parameter produce the same `input_schema`:

```python
limit: int = Field(default=25, ge=1, le=50)  # plain default
limit: Annotated[int, Field(ge=1, le=50)] = 25  # equivalent, and B008-clean
```

`Annotated` is what this project uses. `= Field(...)` puts a function call in an argument default,
which flake8-bugbear's B008 objects to; ruff's B008 exempts it only when the annotation is a type it
can prove immutable, so a `StrEnum`-typed parameter trips it and a `str`-typed one does not — an
inconsistency not worth carrying across a signature.

Observed for `Annotated[Colour | None, Field(description=...)] = None` where `Colour` is a `StrEnum`:

```json
"$defs": {"Colour": {"enum":["red","blue"], "title":"Colour", "type":"string"}},
"properties": {"colour": {"anyOf":[{"$ref":"#/$defs/Colour"},{"type":"null"}],
                          "default": null, "description": "A closed set."}}
```

`description`, `max_length`, `ge`, and `le` all propagate, and **the SDK enforces them before the
tool body runs** — an out-of-range `limit`, an over-long string, or an enum value that is not a
member all come back as `is_error=True` with a pydantic message naming the offending parameter. That
message quotes the rejected input (truncated), so it is caller-visible text, but it discloses nothing
about the server.

Reproduce: `docs/reference/probes/` pattern; the Phase 3 probe is
`tests/contract/test_recruiting_tools.py::test_input_outside_the_advertised_schema_is_refused`.

### 5.2 Tool annotations

`ToolAnnotations` fields are snake_case with camelCase wire aliases:
`title`, `read_only_hint`, `destructive_hint`, `idempotent_hint`, `open_world_hint`. **VERIFIED** to
round-trip to the client intact.

### 5.3 Sync handlers run on a worker thread

In v2 a plain `def` handler is executed via `anyio.to_thread.run_sync()` and cannot call
`asyncio.get_running_loop()`. With async SQLAlchemy + `aiosqlite` this is a silent runtime failure.

**Rule for this project: every handler that touches the database is `async def`.** Only pure-computation
helpers may be `def`.

### 5.4 ⚠ Exception messages inside a tool body reach the client verbatim

This is the single most important security finding in this document, and it contradicts a common
assumption that the SDK sanitizes errors.

**VERIFIED** — a tool that raises `ValueError("SECRET_CONNECTION_STRING=sqlite:///C:/secret/path.db")`
returned to the client:

```json
{"content":[{"type":"text",
  "text":"Error executing tool boom_plain: SECRET_CONNECTION_STRING=sqlite:///C:/secret/path.db"}],
 "is_error": true, "result_type": "complete"}
```

Tracebacks are **not** leaked, but the exception **message is**. The SDK's tool path is literally:

```python
except Exception as e:
    return CallToolResult(content=[TextContent(type="text", text=str(e))], is_error=True)
```

Auto-sanitisation to `"Internal server error"` exists only at the *protocol handler* layer
(`server/runner.py`), which is **outside** the tool body and therefore does not protect us.

**Rule for this project:** every tool adapter wraps its body in a `try/except` that converts any exception
into a sanitised, coded domain error. Nothing may propagate uncaught. This is enforced by tests in
Phase 7. See `docs/error-model.md`.

### 5.5 Two error tiers

| Mechanism | Wire result | Use for |
|---|---|---|
| return / raise handled inside the tool | `CallToolResult(is_error=True)` with our sanitised text | recoverable domain errors the model should read and retry differently |
| `raise MCPError(code, message, data=None)` | top-level JSON-RPC error | protocol-level refusals the model must not paper over |

`MCPError` is `MCPError(code: int, message: str, data: Any = None)` — the v1 `McpError(ErrorData(...))`
shape is gone. Codes are importable from `mcp.types`: `INVALID_PARAMS`, `INVALID_REQUEST`,
`INTERNAL_ERROR`, `METHOD_NOT_FOUND`, `PARSE_ERROR`, `REQUEST_TIMEOUT`, and others.

---

## 6. Resources

```python
def resource(self, uri: str, *, name=None, title=None, description=None,
             mime_type=None, icons=None, annotations=None, meta=None, security=None)
```

### 6.1 Custom URI schemes work with no configuration — **VERIFIED**

`Resource.uri` is a plain `str` in v2 (it was `AnyUrl` in v1), so there is no scheme allowlist and no host
parsing. Both project URI shapes were registered and read successfully:

```text
static:   recruiting://policies/scheduling            -> returned text/markdown, uri preserved exactly
template: recruiting://jobs/{job_id}/interview-plan   -> listed as uri_template, read returned job_id=job_001
```

### 6.2 ⚠ A static resource may not declare a `Context` parameter — fails at import

**VERIFIED** — decorating a non-templated URI with a handler that takes `Context` raises immediately:

```text
ValueError: Resource 'recruiting://policies/scheduling' has no URI template variables, but the handler
declares a Context parameter. Context injection for static resources is not supported. Add a template
variable to the URI or remove the Context parameter.
```

This is a **decoration-time** error, so it breaks at import, not at call time.

**Consequence for this project:** the four static resources
(`policies/scheduling`, `policies/candidate-communication`, `templates/interview-invitation`,
`templates/rescheduling`) have **no route to the database**. They must be served from module-level
constants or packaged files. This is acceptable and arguably correct — they are static approved policy
text. Only `recruiting://jobs/{job_id}/interview-plan` is templated and therefore DB-backed.

### 6.3 Template resources reach lifespan state — **VERIFIED**

```text
template read -> {"job_id": "j1", "lifespan": "AppState(db='engine-handle')"}
prompt        -> ok c1 AppState(db='engine-handle')
tool          -> {'result': "AppState(db='engine-handle')"}
```

Use **bare `ctx: Context`** in resources and prompts, not `Context[AppState]`. The docs state the
parameterised spelling is tool-only, and SDK issue #3235 tracks it failing at runtime in resource and
prompt handlers. Read state via `ctx.request_context.lifespan_context` and narrow it with a one-line
typed helper.

### 6.4 Path traversal in template parameters is rejected by default — **VERIFIED**

The default `ResourceSecurity(reject_path_traversal=True, reject_absolute_paths=True,
reject_null_bytes=True)` rejected an encoded traversal attempt with a generic, non-leaking error:

```text
recruiting://jobs/..%2F..%2Fetc%2Fpasswd/interview-plan
  -> REJECTED MCPError: Unknown resource: recruiting://jobs/..%2F..%2Fetc%2Fpasswd/interview-plan
```

Note it is *generic* — it does not disclose why. We keep this default and add our own `job_id` format
validation on top; unknown-but-well-formed ids are our responsibility (an unknown id currently reaches the
handler).

---

## 7. Prompts

```python
def prompt(self, name=None, title=None, description=None, icons=None)
```

Arguments are derived from the function signature and reported with `required` flags. **VERIFIED**:

```json
"arguments":[{"name":"candidate_id","required":true},{"name":"job_id","required":true}]
```

A `str` return becomes a single user-role text message. `Message` / `UserMessage` / `AssistantMessage`
live at `mcp.server.mcpserver.prompts.base` if richer shapes are needed.

---

## 8. Lifespan

```python
@contextlib.asynccontextmanager
async def lifespan(server: MCPServer[AppState]) -> AsyncIterator[AppState]:
    state = await open_resources()
    try:
        yield state
    finally:
        await close_resources(state)
```

**VERIFIED** — runs exactly once, entered before the first request and exited on shutdown
(`["startup(server_name=probe)", "shutdown"]`). Handlers read the yielded object via
`ctx.request_context.lifespan_context`. `ctx.request_context` is a
`mcp.server.context.ServerRequestContext`; `ctx.request_id` is also available.

If no lifespan is supplied the SDK's default yields an empty `dict`, so
`ctx.request_context.lifespan_context` is `{}` and never `None`. A missing-database bug therefore surfaces
as a `KeyError`, not an obvious `None` — our lifespan is mandatory and the typed accessor asserts on it.

### 8.1 Cancellation

`ctx.cancel_requested` exists only on the *unused* `mcp.server.context.Context`, not on the injected one.
In v2 the dispatcher applies `notifications/cancelled` itself and **cancels the handler task**, so a
long-running `async def` handler receives standard `anyio` cancellation. Handlers must therefore avoid
swallowing `BaseException`, and any database work must be committed or rolled back under
`try/finally`.

---

## 9. Wire model field names are snake_case in v2

A v1 → v2 rename that will otherwise cause `AttributeError` in tests.

| v1 | v2 |
|---|---|
| `Tool.inputSchema` | `Tool.input_schema` |
| `Tool.outputSchema` | `Tool.output_schema` |
| `CallToolResult.isError` | `CallToolResult.is_error` |
| `CallToolResult.structuredContent` | `CallToolResult.structured_content` |
| `Resource.mimeType` | `Resource.mime_type` |
| `ResourceTemplate.uriTemplate` | `ResourceTemplate.uri_template` |
| `nextCursor` | `next_cursor` |

Constructor kwargs still accept both spellings; `model_dump()` needs `by_alias=True` for wire format.
**VERIFIED** field lists:

```text
Tool:           name, title, description, input_schema, execution, output_schema, icons, annotations, meta
CallToolResult: meta, content, structured_content, is_error, result_type
Resource:       name, title, uri, description, mime_type, size, icons, annotations, meta
Prompt:         name, title, description, arguments, icons, meta
```

---

## 10. Testing

The v1 helper `create_connected_server_and_client_session` **no longer exists**. `mcp.shared.memory` now
exports only `create_client_server_memory_streams` and `create_context_streams`.

The v2 replacement is the in-memory `Client`, which accepts a server object directly:

```python
from mcp.client import Client

async with Client(mcp) as client:
    result = await client.call_tool("jobs_search", {"query": "backend", "limit": 5})
    assert result.structured_content["total"] == 1
```

`Client(server, *, raise_exceptions=False, read_timeout_seconds=None, mode="auto", ...)`.

⚠ **`raise_exceptions=True` does *not* make tool-body exceptions raise.** A tool that raises still returns
`is_error=True`; a `pytest.raises` around it fails with "DID NOT RAISE". The flag only un-sanitises
failures *outside* a tool body. Assert on `result.is_error` and our error code, not on Python exceptions.

`result.meta` carries a server-identity stamp
(`{'io.modelcontextprotocol/serverInfo': {...}}`), so whole-object equality needs `result.meta = None`
first.

`ClientSession.validate_tool_result(name, result)` re-validates a result against the tool's declared
output schema and raises `RuntimeError` on mismatch. `Client.call_tool` already calls it — but never for
`is_error` results.

The stdio subprocess smoke test still uses the familiar trio, all importable from `mcp`:

```python
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

params = StdioServerParameters(
    command=sys.executable,
    args=["-m", "recruiting_coordinator_mcp"],
    cwd=str(REPO_ROOT),
    env={**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"},
)
async with stdio_client(params) as (read, write):
    async with ClientSession(read, write) as session:
        init = await session.initialize()
```

`stdio_client(server, errlog=sys.stderr)` — pass `errlog` to capture our stderr logging and assert stdout
purity. `read_timeout_seconds` is now a `float`, no longer a `timedelta`.

### 10.1 The default in-process mode skips JSON-RPC framing

`mode="auto"` drives a direct dispatcher — no streams, no JSON-RPC framing, no `initialize` handshake.
So the in-process tests do **not** cover serialization. **The stdio subprocess smoke test is therefore not
redundant; it is the only coverage of framing.**

### 10.2 Dual-era serving is real and testable — **VERIFIED**

The same server object answered both protocol eras:

```text
mode=auto    -> OK, protocol_version=2026-07-28, tools=1
mode=legacy  -> OK, protocol_version=2025-11-25, tools=1
```

This is the strongest single argument for v2 over v1.29: one stdio server serves the current spec *and*
older hosts. We will assert both eras in the contract tests.

---

## 11. Known open SDK defects tracked for this project

| Issue | Summary | Our exposure | Mitigation |
|---|---|---|---|
| #3235 | Parameterised `Context[T]` loses request state in resource/prompt handlers; also documented as intended | templated interview-plan resource, all prompts | Use bare `ctx: Context` + a `cast` helper. Assume permanent. |
| #3216 | URI templates render no input fields in Inspector v2; untriaged, ownership unknown | Phase 8 Inspector acceptance | Prove the templated resource in pytest via `Client(mcp)`. Do not make Inspector its sole gate. |
| #3224 | `TypedDict` + `NotRequired` injects nulls into `structured_content` | none | We use Pydantic `BaseModel` everywhere. |
| #3227 | `NotRequired` raises on Python 3.10 | none | We target 3.11+, and use `BaseModel`. |
| #3244 | SDK's own test suite fails on non-UTF-8 Windows locale | none | We do not run the SDK's tests. |

As of 2026-08-06 there is no 2.0.1, 15 issues have been filed since GA with **none closed**, and the only
merged PRs since 2026-07-29 are documentation. Plan on self-mitigation rather than upstream fixes; the
exact pin plus lockfile keeps the blast radius small.

---

## 12. Reproducing this verification

The probe scripts used to produce every **VERIFIED** claim above are archived alongside this document at
`docs/reference/probes/`. Re-run them after any SDK bump:

```powershell
uv run --no-project --python 3.11 --with "mcp[cli]==2.0.0" --with "pydantic>=2" python docs/reference/probes/e2e_probe.py
uv run --no-project --python 3.11 --with "mcp[cli]==2.0.0" --with "pydantic>=2" python docs/reference/probes/context_and_resource_probe.py
```
