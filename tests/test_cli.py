from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from mcp_delgado import cli
from mcp_delgado.cli import _build_parser
from mcp_delgado.manager import JobManager
from mcp_delgado.runners import DirectDeepSeekRunner
from mcp_delgado.schemas import JobRecord, JobState

JOB_ID = "a" * 32


def _job_record(state: JobState = JobState.SUCCEEDED, job_id: str = JOB_ID) -> JobRecord:
    return JobRecord(
        job_id=job_id,
        task="Hold the CLI in the foreground until the worker finishes",
        workspace_path=str(Path.cwd()),
        allowed_paths=["src"],
        required_commands=[],
        acceptance_criteria=[],
        provider="deepseek",
        model="deepseek-flash",
        max_minutes=5,
        state=state,
    )


class _StubManager:
    """Record how the CLI drove the manager without starting a worker."""

    def __init__(self, records: list[JobRecord]) -> None:
        self.records = records
        self.state_dir = None
        self.submits = 0
        self.gets = 0

    def submit(self, params, parent_job_id=None):
        self.submits += 1
        return self.records[0]

    def get(self, job_id: str) -> JobRecord:
        self.gets += 1
        return self.records[min(self.gets - 1, len(self.records) - 1)]


def test_run_parses_repeatable_scope_and_checks() -> None:
    args = _build_parser().parse_args([
        "run",
        "Implement the requested change",
        "--allow", "src",
        "--allow", "tests/test_app.py",
        "--check", "python -m pytest",
    ])

    assert args.command == "run"
    assert args.allowed_paths == ["src", "tests/test_app.py"]
    assert args.required_commands == ["python -m pytest"]


def test_review_defaults_to_current_workspace() -> None:
    args = _build_parser().parse_args(["review", "Review the current changes"])

    assert args.command == "review"
    assert args.workspace == "."


# Shared state directory -----------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ["status", JOB_ID],
        ["result", JOB_ID],
        ["diff", JOB_ID],
        ["cancel", JOB_ID],
        ["discard", JOB_ID],
        ["run", "Implement the requested change", "--allow", "src"],
        ["review", "Review the current changes"],
        ["repair", JOB_ID, "Fix the null case"],
    ],
)
def test_state_dir_is_accepted_by_every_command(argv: list[str]) -> None:
    store = "C:/shared/mcp-delgado-store"

    before = _build_parser().parse_args(["--state-dir", store, *argv])
    after = _build_parser().parse_args([*argv, "--state-dir", store])

    assert before.state_dir == store
    assert after.state_dir == store
    assert after.command == argv[0]


def test_state_dir_is_unset_by_default() -> None:
    args = _build_parser().parse_args(["status", JOB_ID])

    assert args.state_dir is None


def test_state_dir_must_be_a_single_value() -> None:
    with pytest.raises(SystemExit):
        _build_parser().parse_args(["--state-dir"])


@pytest.mark.parametrize(
    "argv",
    [
        ["--state-dir", "{store}", "status", JOB_ID],
        ["status", JOB_ID, "--state-dir", "{store}"],
    ],
)
def test_status_reads_the_store_shared_with_the_mcp_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], argv: list[str]
) -> None:
    """A second invocation inspects the durable records another writer stored."""
    store = tmp_path / "mcp-store"
    manager = JobManager(state_dir=store)
    manager._save(_job_record())
    decoy = tmp_path / "decoy"
    monkeypatch.setenv("MODEL_WORKER_STATE_DIR", str(decoy))
    monkeypatch.setattr(sys, "argv", ["delgado", *[item.format(store=store) for item in argv]])

    cli.main()

    payload = json.loads(capsys.readouterr().out)
    assert payload["job_id"] == JOB_ID
    assert payload["state"] == JobState.SUCCEEDED.value
    assert not decoy.exists(), "the explicit store must win over the environment"


def test_run_stays_in_the_foreground_until_the_job_is_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`run` owns its worker: it waits for the terminal record before returning."""
    store = tmp_path / "mcp-store"
    manager = _StubManager([_job_record(JobState.QUEUED), _job_record(JobState.SUCCEEDED)])
    captured: list[tuple[tuple, dict]] = []

    def factory(*args, **kwargs):
        captured.append((args, kwargs))
        return manager

    monkeypatch.setattr(cli, "JobManager", factory)
    monkeypatch.setattr(sys, "argv", [
        "delgado", "run", "Implement the requested change", "--allow", "src", "--state-dir", str(store),
    ])

    with pytest.raises(SystemExit) as caught:
        cli.main()

    assert caught.value.code == 0
    assert captured[-1][1]["state_dir"] == store
    assert manager.submits == 1
    assert manager.gets >= 2, "run must poll the job store until the job is terminal"


def test_run_refuses_a_store_inside_the_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A store the worker could edit is refused before any job is submitted."""
    workspace = tmp_path / "repo"
    workspace.mkdir()
    subprocess.run(
        ["git", "-C", str(workspace), "init"], check=True, capture_output=True, text=True
    )
    monkeypatch.setattr(sys, "argv", [
        "delgado", "run", "Implement the requested change",
        "--allow", "src",
        "--workspace", str(workspace),
        "--state-dir", str(workspace / "store"),
    ])

    with pytest.raises(SystemExit) as caught:
        cli.main()

    captured = capsys.readouterr()
    assert caught.value.code == 2
    assert captured.out == "", "a refused submit prints no job record"
    assert "outside this workspace" in captured.err


# Runner selection -----------------------------------------------------------


def test_run_reports_the_selected_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The operator sees which backend ran the job, and it is read per invocation."""
    manager = _StubManager([_job_record(JobState.QUEUED), _job_record(JobState.SUCCEEDED)])
    manager.runner = DirectDeepSeekRunner()
    monkeypatch.setattr(cli, "JobManager", lambda *args, **kwargs: manager)
    monkeypatch.setattr(sys, "argv", [
        "delgado", "run", "Implement the requested change", "--allow", "src", "--state-dir", str(tmp_path),
    ])

    with pytest.raises(SystemExit):
        cli.main()

    assert "via DeepSeek Responses API" in capsys.readouterr().err


def test_unknown_runner_selector_is_refused_before_any_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("MODEL_WORKER_RUNNER", "deepseek-local")
    monkeypatch.setattr(sys, "argv", ["delgado", "status", JOB_ID, "--state-dir", str(tmp_path / "store")])

    with pytest.raises(SystemExit) as caught:
        cli.main()

    captured = capsys.readouterr()
    assert caught.value.code == 2
    assert captured.out == ""
    assert "MODEL_WORKER_RUNNER" in captured.err
    assert "deepseek-local" in captured.err
    assert "'codewhale'" in captured.err and "'deepseek-api'" in captured.err


def test_status_reads_the_store_with_the_direct_backend_selected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Selecting the direct backend does not break inspection, and needs no key."""
    store = tmp_path / "mcp-store"
    JobManager(state_dir=store)._save(_job_record())
    monkeypatch.setenv("MODEL_WORKER_RUNNER", "deepseek-api")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setattr(sys, "argv", ["delgado", "status", JOB_ID, "--state-dir", str(store)])

    cli.main()

    payload = json.loads(capsys.readouterr().out)
    assert payload["job_id"] == JOB_ID
    assert payload["state"] == JobState.SUCCEEDED.value
