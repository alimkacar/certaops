#!/usr/bin/env python3
"""MCP stdio tasimasini gercek bir istemciyle uctan uca dogrula.

Sunucuyu alt surec olarak baslatir ve initialize -> tools/list -> tools/call
zincirini uygular. Varsayilan cagri salt-okunur ``sap_connection_health``
tool'udur; aktif `.env` profili mock, API Hub veya QAS olabilir.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


async def smoke(tool_name: str, arguments: dict, timeout_s: int) -> int:
    try:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
    except ImportError:
        print(
            "MCP istemcisi icin: uv pip install --python .venv/bin/python '.[mcp]'", file=sys.stderr
        )
        return 2

    env = dict(os.environ)
    env["PYTHONPATH"] = str(PROJECT_ROOT / "src")
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "certaops.mcp_server"],
        cwd=PROJECT_ROOT,
        env=env,
    )

    async with (
        stdio_client(params) as (read_stream, write_stream),
        ClientSession(read_stream, write_stream) as session,
    ):
        initialized = await session.initialize()
        listed = await session.list_tools()
        names = sorted(tool.name for tool in listed.tools)

        if tool_name not in names:
            print(f"KALDI: '{tool_name}' MCP tarafindan bildirilmiyor.", file=sys.stderr)
            return 1
        forbidden = {"sap_pr_submit", "sap_generate_report"}.intersection(names)
        if forbidden:
            print(
                f"KALDI: salt-okunur profilde yan etkili tool acik: {sorted(forbidden)}",
                file=sys.stderr,
            )
            return 1

        result = await session.call_tool(
            tool_name,
            arguments,
            read_timeout_seconds=timedelta(seconds=timeout_s),
        )
        if result.isError:
            detail = " ".join(getattr(item, "text", "") for item in result.content)
            print(f"KALDI: {tool_name}: {detail[:500]}", file=sys.stderr)
            return 1

        print(
            "GECTI: "
            f"server={initialized.serverInfo.name} "
            f"protocol={initialized.protocolVersion} "
            f"tools={len(names)} call={tool_name}"
        )
        return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--tool", default="sap_connection_health")
    parser.add_argument("--arguments", default="{}", help="Tool argumanlari (JSON nesnesi).")
    parser.add_argument("--timeout", type=int, default=60)
    args = parser.parse_args(argv)

    try:
        arguments = json.loads(args.arguments)
    except json.JSONDecodeError as exc:
        print(f"Gecersiz --arguments JSON: {exc}", file=sys.stderr)
        return 2
    if not isinstance(arguments, dict):
        print("--arguments bir JSON nesnesi olmali.", file=sys.stderr)
        return 2

    try:
        import anyio
    except ImportError:
        print(
            "MCP istemcisi icin: uv pip install --python .venv/bin/python '.[mcp]'", file=sys.stderr
        )
        return 2
    return anyio.run(smoke, args.tool, arguments, args.timeout)


if __name__ == "__main__":
    raise SystemExit(main())
