"""Command-line adapter for MCP Delgado."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Optional

from .manager import JobManager
from .schemas import DelegateTaskInput, JobState, RepairTaskInput, ReviewInput

TERMINAL_STATES = {
    JobState.SUCCEEDED,
    JobState.FAILED,
    JobState.POLICY_FAILED,
    JobState.CANCELLED,
    JobState.INTERRUPTED,
}


def _print_json(value: object) -> None:
    print(json.dumps(value, indent=2, default=str))


def _public_job(record, include_summary: bool = False) -> dict:
    payload = record.model_dump(mode="json")
    if not include_summary:
        payload.pop("summary", None)
        payload.pop("validation_results", None)
    return payload


def _wait(manager: JobManager, job_id: str):
    while True:
        record = manager.get(job_id)
        if record.state in TERMINAL_STATES:
            return record
        time.sleep(0.25)


def _runner_note(manager: JobManager) -> str:
    """Name the backend that will run the job, when the manager exposes one.

    The manager selects its runner from ``MODEL_WORKER_RUNNER`` when it is
    constructed, and the CLI constructs one per invocation. Reporting the choice
    on stderr is how an operator sees which backend a live checkout just used.
    """
    runner = getattr(manager, "runner", None)
    label = getattr(runner, "label", None) or getattr(runner, "name", None)
    return f" via {label}" if label else ""


def _add_model_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--provider", help="CodeWhale provider profile")
    parser.add_argument("--model", help="Model name in the provider profile")


def _add_state_dir_option(parser: argparse.ArgumentParser, *, top_level: bool = False) -> None:
    """Accept an explicit durable store so separate invocations share one job engine.

    A live checkout can point the CLI at the same store the MCP host uses, so
    ``status``, ``result``, ``diff``, and ``cancel`` from a second invocation
    inspect the job the MCP server is still running. The option is accepted both
    before and after the subcommand; the subparser default is ``SUPPRESS`` so a
    top-level value survives when the subcommand does not repeat it.
    """
    parser.add_argument(
        "--state-dir",
        dest="state_dir",
        metavar="DIR",
        default=None if top_level else argparse.SUPPRESS,
        help=(
            "Durable job store shared with the MCP host and other invocations "
            "(default: MODEL_WORKER_STATE_DIR, then ~/.mcp-delgado)"
        ),
    )


def _resolve_state_dir(raw: Optional[str]) -> Optional[Path]:
    """Return the explicit store path, or ``None`` to let the manager choose."""
    if not raw:
        return None
    return Path(raw).expanduser()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="delgado",
        description="Delegate bounded work to a fast model while the manager keeps control.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    _add_state_dir_option(parser, top_level=True)

    run = subparsers.add_parser("run", help="Run a scoped task and wait for its result")
    run.add_argument("task", help="The implementation task")
    run.add_argument("--workspace", default=".", help="Git workspace path")
    run.add_argument("--allow", action="append", required=True, dest="allowed_paths")
    run.add_argument("--accept", action="append", default=[], dest="acceptance_criteria")
    run.add_argument("--check", action="append", default=[], dest="required_commands")
    run.add_argument("--max-minutes", type=int, default=30)
    _add_model_options(run)
    _add_state_dir_option(run)

    review = subparsers.add_parser("review", help="Run a read-only model review")
    review.add_argument("request", help="The review request")
    review.add_argument("--workspace", default=".", help="Git workspace path")
    review.add_argument("--max-minutes", type=int, default=10)
    _add_model_options(review)
    _add_state_dir_option(review)

    for name, help_text in (
        ("status", "Read a compact job record"),
        ("result", "Read a complete job record"),
        ("diff", "Read a stored job patch"),
        ("cancel", "Cancel a queued or running job"),
        ("discard", "Delete a completed job record"),
    ):
        command = subparsers.add_parser(name, help=help_text)
        command.add_argument("job_id")
        _add_state_dir_option(command)

    repair = subparsers.add_parser("repair", help="Run a repair for an earlier job")
    repair.add_argument("job_id")
    repair.add_argument("feedback")
    repair.add_argument("--max-minutes", type=int, default=20)
    _add_state_dir_option(repair)
    return parser


def _run_command(args: argparse.Namespace, manager: JobManager) -> int:
    params = DelegateTaskInput(
        task=args.task,
        acceptance_criteria=args.acceptance_criteria,
        workspace_path=str(Path(args.workspace).resolve()),
        allowed_paths=args.allowed_paths,
        required_commands=args.required_commands,
        provider=args.provider,
        model=args.model,
        max_minutes=args.max_minutes,
    )
    record = manager.submit(params)
    print(
        f"Started job {record.job_id} with {record.provider}/{record.model}{_runner_note(manager)}.",
        file=sys.stderr,
    )
    print(f"State directory: {manager.state_dir}", file=sys.stderr)
    record = _wait(manager, record.job_id)
    _print_json(_public_job(record, include_summary=True))
    return 0 if record.state == JobState.SUCCEEDED else 1


def _review_command(args: argparse.Namespace, manager: JobManager) -> int:
    params = ReviewInput(
        request=args.request,
        workspace_path=str(Path(args.workspace).resolve()),
        provider=args.provider,
        model=args.model,
        max_minutes=args.max_minutes,
    )
    payload = manager.review(params)
    _print_json(payload)
    return 0 if payload.get("exit_code") == 0 else 1


def _repair_command(args: argparse.Namespace, manager: JobManager) -> int:
    params = RepairTaskInput(
        job_id=args.job_id,
        feedback=args.feedback,
        max_minutes=args.max_minutes,
    )
    previous = manager.get(params.job_id)
    task = DelegateTaskInput(
        task=f"Original task:\n{previous.task}\n\nManager findings:\n{params.feedback}",
        acceptance_criteria=previous.acceptance_criteria,
        workspace_path=previous.workspace_path,
        allowed_paths=previous.allowed_paths,
        required_commands=previous.required_commands,
        provider=previous.provider,
        model=previous.model,
        max_minutes=params.max_minutes,
    )
    record = manager.submit(task, parent_job_id=previous.job_id)
    print(f"Started repair job {record.job_id}{_runner_note(manager)}.", file=sys.stderr)
    print(f"State directory: {manager.state_dir}", file=sys.stderr)
    record = _wait(manager, record.job_id)
    _print_json(_public_job(record, include_summary=True))
    return 0 if record.state == JobState.SUCCEEDED else 1


def main() -> None:
    args = _build_parser().parse_args()
    try:
        # The manager is built per invocation, so this process reads
        # MODEL_WORKER_RUNNER, MODEL_WORKER_STATE_DIR, and the worker key from its
        # own environment every time it runs. A live checkout therefore picks up
        # an edited backend or a changed selector without restarting the MCP host.
        manager = JobManager(state_dir=_resolve_state_dir(getattr(args, "state_dir", None)))
        if args.command == "run":
            raise SystemExit(_run_command(args, manager))
        if args.command == "review":
            raise SystemExit(_review_command(args, manager))
        if args.command == "repair":
            raise SystemExit(_repair_command(args, manager))
        if args.command == "status":
            _print_json(_public_job(manager.get(args.job_id)))
        elif args.command == "result":
            _print_json(_public_job(manager.get(args.job_id), include_summary=True))
        elif args.command == "diff":
            print(manager.read_diff(args.job_id, 200_000))
        elif args.command == "cancel":
            _print_json(_public_job(manager.cancel(args.job_id)))
        elif args.command == "discard":
            manager.discard(args.job_id)
            _print_json({"job_id": args.job_id, "discarded": True})
    except (KeyError, RuntimeError, ValueError) as exc:
        print(f"delgado: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
