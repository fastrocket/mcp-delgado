from __future__ import annotations

from scripts.mcp_probe import _parser


def test_probe_parses_tool_and_json_arguments() -> None:
    args = _parser().parse_args(["model_worker_job_status", '{"params":{"job_id":"abc"}}'])

    assert args.tool == "model_worker_job_status"
    assert args.arguments.startswith("{")
