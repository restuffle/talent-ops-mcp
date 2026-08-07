"""End-to-end stdio smoke test against a real subprocess.

This is the only coverage of JSON-RPC framing and stdout purity: the in-process client used
by the contract tests bypasses both (docs/reference/mcp-v2-verified-api.md §10.1). If the
handshake below succeeds, stdout carried nothing but protocol traffic -- any stray byte
would have desynchronised the stream.
"""

import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from talent_ops_mcp.server import SERVER_NAME, TOOL_PREFIX

REPO_ROOT = Path(__file__).resolve().parents[1]
READ_TIMEOUT_SECONDS = 60.0
HEALTH_CHECK = f"{TOOL_PREFIX}_health_check"


async def test_stdio_subprocess_handshake_and_health_check(tmp_path: Path, subprocess_env: dict[str, str]) -> None:
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "talent_ops_mcp"],
        cwd=str(REPO_ROOT),
        env=subprocess_env,
    )
    stderr_path = tmp_path / "server-stderr.log"

    with stderr_path.open("w", encoding="utf-8") as errlog:
        async with stdio_client(params, errlog=errlog) as (read, write):
            async with ClientSession(read, write, read_timeout_seconds=READ_TIMEOUT_SECONDS) as session:
                init = await session.initialize()
                listed = await session.list_tools()
                result = await session.call_tool(HEALTH_CHECK, {})

    assert init.server_info.name == SERVER_NAME
    assert init.server_info.version
    # Every tool survives real JSON-RPC framing, not just the one this test calls: a schema the
    # SDK cannot serialize would break `tools/list` for all of them at once.
    assert {t.name for t in listed.tools} == {
        HEALTH_CHECK,
        f"{TOOL_PREFIX}_search_jobs",
        f"{TOOL_PREFIX}_list_applications",
        f"{TOOL_PREFIX}_get_candidate",
        f"{TOOL_PREFIX}_get_interview_plan",
    }
    assert result.is_error is False
    assert result.structured_content is not None
    assert result.structured_content["status"] == "healthy"

    # Our logging went to stderr, where it belongs.
    stderr_text = stderr_path.read_text(encoding="utf-8")
    assert "startup" in stderr_text
    assert f"server={SERVER_NAME}" in stderr_text


async def test_invalid_configuration_exits_without_serving(tmp_path: Path, subprocess_env: dict[str, str]) -> None:
    """A bad value must fail fast with an actionable stderr message, not a half-started server."""
    import anyio

    bad_env = dict(subprocess_env)
    bad_env["TALENT_OPS_DATABASE_PATH"] = str(tmp_path.parent / "escaped.sqlite3")

    result = await anyio.run_process(
        [sys.executable, "-m", "talent_ops_mcp"],
        cwd=str(REPO_ROOT),
        env=bad_env,
        check=False,
        input=b"",
    )

    stderr_text = result.stderr.decode("utf-8", errors="replace")
    assert result.returncode == 2
    assert "configuration is invalid" in stderr_text
    assert "must resolve inside data_dir" in stderr_text
    assert result.stdout == b""
