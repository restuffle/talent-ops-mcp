# Talent Ops MCP

A local [Model Context Protocol](https://modelcontextprotocol.io/) server that lets an AI
assistant coordinate recruiting **logistics** — finding jobs and applications, computing
compatible interview times, and creating interviews after a human confirms the exact
proposed action.

> **Status: Phase 1 of 11 complete.** The package installs, the stdio server starts, an MCP
> client connects, and `health_check` works. The recruiting data model, tools, scheduling
> engine, resources and prompts land in Phases 2–6. See [PLAN.md](PLAN.md) for the checklist
> and [docs/reference/mcp-v2-verified-api.md](docs/reference/mcp-v2-verified-api.md) for the
> verified SDK behaviour every phase is written against.

## The boundary

This server is an administrative assistant. It is **not** a decision-maker.

**It will not**, by design and enforced in server code rather than prose:

- score, rank, compare, shortlist, or recommend candidates
- assess "culture fit", personality, emotion, or sentiment
- decide or suggest hiring, rejection, or advancement
- infer any protected characteristic (race, ethnicity, national origin, sex, gender,
  religion, disability, pregnancy, age, veteran status, genetic information), or use a proxy
  for one such as a name, photograph, address, or graduation year
- execute arbitrary SQL, shell commands, filesystem reads, or HTTP requests
- send real email or touch a real calendar or ATS (the MVP is fictional SQLite data only)

Requests of that kind fail closed with a stable `UNSAFE_OPERATION` error. Ranking proposed
*time slots* on operational criteria is fine; ranking people is not.

All retrieved content — candidate fields, job descriptions, notes, policy text — is treated
as untrusted data. Instructions embedded in it have no authority over server rules.

## Prerequisites

- **Python 3.11+** — 3.11.15 is what this is developed and verified against
- **[uv](https://docs.astral.sh/uv/)** 0.11+ — dependency and environment management
- Node.js only if you want to run MCP Inspector (Phase 8)

## Install

```powershell
git clone <your-remote> talent-ops-mcp
cd talent-ops-mcp
uv sync --all-groups
```

`uv sync` creates `.venv`, installs the pinned dependencies from `uv.lock`, and installs
this package in editable mode.

## Run the server

The server speaks MCP over **stdio**. It is not interesting to run by hand — an MCP client
drives it — but this is the exact command a client should use:

```powershell
uv run talent-ops-mcp
```

Equivalently, as a module:

```powershell
uv run python -m talent_ops_mcp
```

stdout is reserved for MCP protocol traffic. All logging goes to stderr.

## Configuration

Every setting is optional. Copy `.env.example` to `.env` to override, or set the variables
in the environment. `.env` is never committed.

| Variable | Default | Purpose |
|---|---|---|
| `TALENT_OPS_ENV` | `development` | `development`, `test`, or `production` |
| `TALENT_OPS_DATA_DIR` | `data` | Only directory the server may touch |
| `TALENT_OPS_DATABASE_PATH` | `<data_dir>/talent_ops.sqlite3` | SQLite file; must resolve inside `data_dir` |
| `TALENT_OPS_LOG_LEVEL` | `INFO` | stderr logging level |
| `TALENT_OPS_PREVIEW_TTL_SECONDS` | `600` | Confirmation-token lifetime (30–3600) |
| `TALENT_OPS_MAX_PAGE_SIZE` | `50` | Upper bound on any list page size (1–200) |
| `TALENT_OPS_MAX_DATE_RANGE_DAYS` | `31` | Upper bound on any date range (1–90) |
| `TALENT_OPS_SLOT_INTERVAL_MINUTES` | `15` | Scheduling grid (5–60) |

Invalid configuration fails at startup with exit code `2` and an actionable stderr message,
before the server serves anything. The database path is validated against `data_dir` with
symlinks resolved, so a traversal or symlinked-parent escape is rejected up front.

Relative paths resolve against the process working directory, so an MCP host config should
set `cwd` to the repository root (see Phase 8 for host configuration).

## MCP surface

Every tool is prefixed `talent_ops_` so it cannot collide with another MCP server running in
the same client.

| Tool | Type | State | Purpose |
|---|---|---|---|
| `talent_ops_health_check` | Read | None | Server version, environment, and a live database probe. No paths or credentials. |

Phases 3–6 add `talent_ops_search_jobs`, `talent_ops_list_applications`,
`talent_ops_get_candidate`, `talent_ops_get_interview_plan`,
`talent_ops_propose_interview_slots`, `talent_ops_preview_interview_schedule`,
`talent_ops_confirm_interview_schedule`, `talent_ops_list_upcoming_interviews`, and
`talent_ops_list_overdue_feedback`, plus the `recruiting://` policy and template resources
and three guided prompts.

### Preview and confirmation

Every state-changing operation is two steps, and a plain `confirm: true` boolean is
deliberately *not* sufficient:

1. **Preview** revalidates the whole proposal, builds the complete immutable payload,
   canonically serializes it, SHA-256 hashes it, mints a ≥256-bit random token, stores only
   the token's hash, and returns the exact proposed action plus the plaintext token once.
2. **Confirm** re-checks the token hash, actor, operation, expiry, unused state, and payload
   hash, re-runs every conflict check, then creates the interview, attendees, feedback
   requests, and audit event inside one transaction under a unique idempotency key.

Replay, tampering, expiry, actor mismatch, and post-preview conflicts all fail closed.
Implemented in Phase 5.

## Architecture

```text
MCP client (Claude, Inspector)
   │  stdio, JSON-RPC
   ▼
server.py          MCPServer metadata, instructions, lifespan, tool registration
   ▼
tools/             thin adapters: validate, delegate, translate errors    (Phase 3+)
   ▼
services/          recruiting, scheduling, confirmation, audit rules      (Phase 3+)
   ▼
repositories/      provider-neutral contracts                            (Phase 2)
   ▼
SQLite (aiosqlite) ── later: Greenhouse/Lever, Graph/Google Calendar
```

Layers stay separate so the provider swap in Phases 9–10 does not touch the MCP contract.
MCP-decorated functions never contain scheduling arithmetic, SQL, token generation, or
transaction logic.

### Current files

```text
src/talent_ops_mcp/
├── __init__.py          package version
├── __main__.py          console script + `python -m` entrypoint
├── server.py            MCPServer construction, lifespan, health_check
├── settings.py          validated TALENT_OPS_* configuration
├── logging_config.py    stderr-only logging
└── errors.py            stable error codes + the sanitisation chokepoint
tests/
├── conftest.py
├── unit/                settings, errors, logging
├── contract/            in-process MCP client against the real server
└── test_mcp_smoke.py    real stdio subprocess (the only framing coverage)
```

## SDK version and transport

- **`mcp==2.0.0`**, pinned exactly. v2 is a major rework: `FastMCP` no longer exists, wire
  model fields are snake_case, and — critically — **an exception message raised inside a tool
  body reaches the client verbatim**. Every behaviour this project depends on was verified
  empirically on this machine; see
  [docs/reference/mcp-v2-verified-api.md](docs/reference/mcp-v2-verified-api.md) and the
  re-runnable probes in `docs/reference/probes/`.
- **stdio only.** Streamable HTTP is deferred to Phase 11 and requires real authorization
  and tenant isolation first.

## Quality gates

```powershell
uv sync --all-groups
uv run ruff format --check .
uv run ruff check .
uv run mypy src tests
uv run pytest
uv build
```

A phase is not complete while any of these fail. Broad lint or type suppressions are not
used; each narrow `# noqa` carries its justification inline.

## Troubleshooting

| Symptom | Cause |
|---|---|
| `configuration is invalid` + exit 2 | A `TALENT_OPS_*` value is out of range, or the database path resolves outside `data_dir`. The message names the offending field. |
| `database: unavailable` from `health_check` | The data directory is not writable, or `TALENT_OPS_DATABASE_PATH` points somewhere the process cannot create. |
| Client hangs on connect | Something wrote to stdout before serving began. Never `print()`; never add a `StreamHandler(sys.stdout)`. |
| `ModuleNotFoundError: talent_ops_mcp` | Run through `uv run`, or `uv sync` first — the package must be installed, not just present. |
| `os error 396` on `uv build` | uv's hardlinking does not work under OneDrive. Already handled by `link-mode = "copy"` in `pyproject.toml`; if you moved the repo, that setting is now unnecessary but harmless. |

## Known limitations

- **No recruiting data yet.** Phase 1 delivers the foundation only; the schema, seed, and
  every recruiting tool are Phases 2–6.
- **Fictional data by design.** The MVP is SQLite with `.test` identities. No ATS, calendar,
  or email integration, and none is planned before Phases 9–10.
- **Single local user.** There is no authentication. Remote multi-user operation is Phase 11
  and needs security review before it happens.
- **`/mcp-builder` was not available** in the session that built this. The implementation
  follows the empirically verified SDK reference in `docs/reference/` instead.
- `SECURITY.md` and `LICENSE` are Phase 7 and Phase 8 deliverables respectively.

## License

Not yet chosen. Treat as all rights reserved until `LICENSE` exists.
