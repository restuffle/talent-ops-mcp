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

## Seed the demo database

One command creates the database and fills it with fictional demo data:

```powershell
uv run python scripts/seed_demo_data.py
```

Safe to re-run — rows are merged by primary key, so a second run converges on the same state
rather than duplicating. That also makes it the way to **restore** demo data you have edited.

| Flag | Effect |
|---|---|
| *(none)* | Create the schema if missing, then seed the fixed reference week (2026-09-07). |
| `--anchor now` | Same dataset, shifted so the interview slots fall in the current week. |
| `--anchor YYYY-MM-DD` | Same dataset, anchored to a specific Monday. |
| `--reset` | Drop every table first. Destroys all local data, seeded or not. |

The default anchor is fixed so tests are reproducible; `--anchor now` is for live demos, where
a dataset whose slots are all in the past demonstrates nothing. Every instant shifts by the
same offset, so relative spacing is preserved.

The seeded dataset is 83 rows: 3 jobs, 12 candidates, 12 applications across 5 pipeline
stages, 7 interview plans (one deliberately superseded), 6 interviewers (one inactive), 37
availability windows, 4 interviews (scheduled, completed, and cancelled), and 2 feedback
requests. Every identity is invented and every address uses the reserved `.test` TLD, which
cannot resolve — a demo seed must not be able to email a real person.

### Schema management

There is no migration tool. `create_all` runs on server startup and in the seed script, and
is idempotent. This is the documented strategy §12 permits: the database is a local
single-file store holding nothing but fictional demo data, so there is nothing for a
migration to preserve, and Alembic would add a second source of schema truth to keep in step
by hand. That trade flips at Phase 11, when a deployment first holds data someone would miss.

`create_all` creates missing tables; it does **not** alter existing ones. After changing a
column, re-run with `--reset`.

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
scripts/
└── seed_demo_data.py    one-command database init + demo seed
src/talent_ops_mcp/
├── __init__.py          package version
├── __main__.py          console script + `python -m` entrypoint
├── server.py            MCPServer construction, lifespan, health_check
├── settings.py          validated TALENT_OPS_* configuration
├── logging_config.py    stderr-only logging
├── errors.py            stable error codes + the sanitisation chokepoint
├── models/
│   └── domain.py        closed status/stage vocabularies (become CHECK constraints)
├── db/
│   ├── base.py          declarative base, UTC datetime type, enum + naming conventions
│   ├── schema.py        the 13 tables and their constraints
│   ├── session.py       engine factory, SQLite pragmas, schema init, transaction scope
│   └── seed.py          deterministic, idempotent demo dataset
└── repositories/
    ├── protocols.py     what the service layer may ask of storage
    └── sqlite.py        the only implementation today; paginated, read-only
tests/
├── conftest.py          config isolation + database fixtures
├── unit/                settings, errors, logging
├── contract/            in-process MCP client against the real server
├── integration/         schema constraints, seed properties, repository queries
└── test_mcp_smoke.py    real stdio subprocess (the only framing coverage)
```

Two invariants are enforced centrally rather than per-caller, because "enforced in every
caller" is the same as "not enforced":

- **Datetimes are timezone-aware UTC.** `UtcDateTime` refuses to persist a naive one. SQLite
  has no aware type and silently drops offsets, so a value written as `09:00-04:00` would
  otherwise read back as bare `09:00`.
- **Foreign keys are on.** SQLite ships with them *off*; `create_engine` attaches a
  `PRAGMA foreign_keys=ON` listener, without which every `ForeignKey` is inert decoration.

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
uv run mypy src tests scripts
uv run python -m pytest
uv build
```

A phase is not complete while any of these fail. Broad lint or type suppressions are not
used; each narrow `# noqa` carries its justification inline.

`python -m pytest` rather than `uv run pytest`: on a managed Windows machine, Application
Control can refuse to launch the generated `pytest.exe` shim (`os error 4551`) while allowing
`python.exe`. The module form is equivalent, so it is the documented command everywhere. `ruff`,
`mypy`, and the `talent-ops-mcp` console script were not affected.

## Troubleshooting

| Symptom | Cause |
|---|---|
| `configuration is invalid` + exit 2 | A `TALENT_OPS_*` value is out of range, or the database path resolves outside `data_dir`. The message names the offending field. |
| `database: unavailable` from `health_check` | The data directory is not writable, or `TALENT_OPS_DATABASE_PATH` points somewhere the process cannot create. |
| Client hangs on connect | Something wrote to stdout before serving began. Never `print()`; never add a `StreamHandler(sys.stdout)`. |
| `ModuleNotFoundError: talent_ops_mcp` | Run through `uv run`, or `uv sync` first — the package must be installed, not just present. |
| `os error 4551` / "Application Control policy has blocked this file" | A managed-Windows policy refused a generated console-script `.exe`. Run the module form instead — `uv run python -m pytest`. |
| `os error 396` on `uv build`, or `os error 5` on `uv sync` | uv's hardlinking and file replacement fail under OneDrive. Do not keep this repository in a synced folder; `.venv` and `.git` both get locked mid-operation. Fixed by moving it out — a lock landing mid-`git rebase` is the genuinely dangerous case. |
| Tests pass but changes seem to have no effect | The editable install may point at an old copy of the repository. Check `.venv/Lib/site-packages/talent_ops_mcp-*.dist-info/direct_url.json`; if the path is wrong, `rm -rf .venv` and `uv sync --all-groups`. |

## Known limitations

- **Data, but no tools over it yet.** Phase 2 delivers the schema, repositories, and seed.
  `health_check` is still the only MCP tool; the recruiting tools are Phases 3–6.
- **Repositories cover reads only, for four entities.** Jobs, candidates, applications, and
  interviewers. Availability and interview queries arrive with the scheduling engine in
  Phase 4, which is where their access patterns are actually specified.
- **`create_all`, not migrations.** Schema changes need `--reset`; see *Schema management*.
- **Legacy timezone aliases are accepted.** `EST`, `MST`, and `HST` are real IANA entries so
  the validator passes them, but they carry no daylight saving — `EST` is UTC-5 even in July.
  Prefer `America/New_York`. Nothing in the code can catch this.
- **Fictional data by design.** The MVP is SQLite with `.test` identities. No ATS, calendar,
  or email integration, and none is planned before Phases 9–10.
- **Single local user.** There is no authentication. Remote multi-user operation is Phase 11
  and needs security review before it happens.
- **`/mcp-builder` was not available** in the session that built this. The implementation
  follows the empirically verified SDK reference in `docs/reference/` instead.
- `SECURITY.md` and `LICENSE` are Phase 7 and Phase 8 deliverables respectively.

## License

Not yet chosen. Treat as all rights reserved until `LICENSE` exists.
