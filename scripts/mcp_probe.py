"""Call one Delgado MCP tool and report transport timing."""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Call a Delgado MCP tool through stdio.")
    parser.add_argument("tool", help="MCP tool name")
    parser.add_argument("arguments", help="JSON object with tool arguments")
    parser.add_argument("--output", type=Path, help="Optional JSON result path")
    return parser


async def _call(tool: str, arguments: dict) -> dict:
    root = Path(__file__).resolve().parents[1]
    server = StdioServerParameters(
        command=str(root / ".venv" / "Scripts" / "python.exe"),
        args=["-m", "mcp_delgado.server"],
        cwd=root,
    )
    started = time.perf_counter()
    async with stdio_client(server) as streams:
        connected = time.perf_counter()
        async with ClientSession(*streams) as session:
            await session.initialize()
            initialized = time.perf_counter()
            tools = await session.list_tools()
            result = await session.call_tool(tool, arguments=arguments)
            completed = time.perf_counter()
    return {
        "tool": tool,
        "available_tools": [item.name for item in tools.tools],
        "timing_seconds": {
            "process_start": round(connected - started, 3),
            "mcp_initialize": round(initialized - connected, 3),
            "tool_call": round(completed - initialized, 3),
            "total": round(completed - started, 3),
        },
        "is_error": result.isError,
        "content": [item.model_dump(mode="json") for item in result.content],
    }


def main() -> None:
    args = _parser().parse_args()
    arguments = json.loads(args.arguments)
    if not isinstance(arguments, dict):
        raise SystemExit("arguments must decode to a JSON object")
    payload = asyncio.run(_call(args.tool, arguments))
    rendered = json.dumps(payload, indent=2)
    if args.output:
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)


if __name__ == "__main__":
    main()
