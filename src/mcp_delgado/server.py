"""Stdio MCP adapter for the scoped model worker."""

from __future__ import annotations

import json
import sys

from mcp.server.fastmcp import FastMCP

from .manager import JobManager, ReviewTimeoutError, RunnerSelectionError
from .schemas import DelegateTaskInput, JobIdInput, ReadDiffInput, RepairTaskInput, ReviewInput

INSTRUCTIONS = (
    "Use model_worker_delegate_task for bounded implementation work. Poll status, then review the stored diff. "
    "The manager keeps commit, push, merge, and deployment authority. Supply narrow allowed paths and exact checks."
)
RESPONSE_BYTE_CAP = 60_000

mcp = FastMCP("mcp_delgado", instructions=INSTRUCTIONS)


def _build_manager() -> JobManager:
    """Build the server's manager, or explain a bad runner selector and stop.

    An MCP host is long-lived, so this process reads ``MODEL_WORKER_RUNNER``
    once, when it starts: restart the host to change backends. The refusal is
    explicit because the alternative, quietly running every job on the default
    backend, would hide a typo in a selector the operator meant to use.
    """
    try:
        return JobManager()
    except RunnerSelectionError as exc:
        print(f"mcp-delgado: {exc}", file=sys.stderr)
        raise SystemExit(2) from None


manager = _build_manager()


def _json(payload: object) -> str:
    text = json.dumps(payload, indent=2, default=str)
    encoded = text.encode("utf-8")
    if len(encoded) <= RESPONSE_BYTE_CAP:
        return text
    return encoded[:RESPONSE_BYTE_CAP].decode("utf-8", errors="ignore") + "\n... truncated ..."


def _public_job(record, include_summary: bool = False) -> dict:
    payload = record.model_dump(mode="json")
    if not include_summary:
        payload.pop("summary", None)
        payload.pop("validation_results", None)
    return payload


@mcp.tool(name="model_worker_delegate_task")
async def model_worker_delegate_task(params: DelegateTaskInput) -> str:
    """Start a scoped coding job and return its durable job ID."""
    try:
        return _json(_public_job(manager.submit(params)))
    except Exception as exc:
        return _json({"error": type(exc).__name__, "detail": str(exc)})


@mcp.tool(name="model_worker_job_status")
async def model_worker_job_status(params: JobIdInput) -> str:
    """Read a compact job state without loading its full output or diff."""
    try:
        return _json(_public_job(manager.get(params.job_id)))
    except Exception as exc:
        return _json({"error": type(exc).__name__, "detail": str(exc)})


@mcp.tool(name="model_worker_job_result")
async def model_worker_job_result(params: JobIdInput) -> str:
    """Read the final summary, scope audit, and validation results."""
    try:
        return _json(_public_job(manager.get(params.job_id), include_summary=True))
    except Exception as exc:
        return _json({"error": type(exc).__name__, "detail": str(exc)})


@mcp.tool(name="model_worker_read_job_diff")
async def model_worker_read_job_diff(params: ReadDiffInput) -> str:
    """Read the stored patch for manager review."""
    try:
        return manager.read_diff(params.job_id, params.max_bytes)
    except Exception as exc:
        return _json({"error": type(exc).__name__, "detail": str(exc)})


@mcp.tool(name="model_worker_cancel_job")
async def model_worker_cancel_job(params: JobIdInput) -> str:
    """Stop a queued or running job. Existing file changes stay available."""
    try:
        return _json(_public_job(manager.cancel(params.job_id)))
    except Exception as exc:
        return _json({"error": type(exc).__name__, "detail": str(exc)})


@mcp.tool(name="model_worker_discard_job")
async def model_worker_discard_job(params: JobIdInput) -> str:
    """Delete stored job records. This tool does not change repository files."""
    try:
        manager.discard(params.job_id)
        return _json({"job_id": params.job_id, "discarded": True})
    except Exception as exc:
        return _json({"error": type(exc).__name__, "detail": str(exc)})


@mcp.tool(name="model_worker_repair_task")
async def model_worker_repair_task(params: RepairTaskInput) -> str:
    """Start a new scoped job that addresses exact findings from an earlier job."""
    try:
        previous = manager.get(params.job_id)
        task = DelegateTaskInput(
            task=f"Repair the earlier delegated task.\n\nManager findings:\n{params.feedback}",
            acceptance_criteria=previous.acceptance_criteria,
            workspace_path=previous.workspace_path,
            allowed_paths=previous.allowed_paths,
            required_commands=previous.required_commands,
            provider=previous.provider,
            model=previous.model,
            max_minutes=params.max_minutes,
        )
        return _json(_public_job(manager.submit(task, parent_job_id=previous.job_id)))
    except Exception as exc:
        return _json({"error": type(exc).__name__, "detail": str(exc)})


@mcp.tool(name="model_worker_review")
async def model_worker_review(params: ReviewInput) -> str:
    """Ask the configured model for a read-only review of a workspace."""
    try:
        return _json(await manager.review_async(params))
    except ReviewTimeoutError as exc:
        return _json({
            "error": "ReviewTimeoutError",
            "detail": str(exc),
            "timed_out": True,
            "output": exc.output,
            "diagnostics_tail": exc.diagnostics_tail,
        })
    except Exception as exc:
        return _json({"error": type(exc).__name__, "detail": str(exc)})


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
