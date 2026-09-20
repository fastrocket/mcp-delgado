from __future__ import annotations

import asyncio
import importlib
import json
import os
import subprocess
import uuid
from pathlib import Path

import pytest

from mcp_delgado.manager import JobManager, ReviewTimeoutError
from mcp_delgado.runners import DirectDeepSeekRunner
from mcp_delgado.schemas import DelegateTaskInput, JobIdInput, JobState, JobRecord, ReviewInput


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Import the MCP adapter against a throwaway state directory."""
    monkeypatch.setenv("MODEL_WORKER_STATE_DIR", str(tmp_path / "state"))
    return importlib.reload(importlib.import_module("mcp_delgado.server"))


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    for args in (
        ("init",),
        ("config", "user.email", "test@example.com"),
        ("config", "user.name", "Test"),
    ):
        subprocess.run(["git", "-C", str(workspace), *args], check=True, capture_output=True, text=True)
    (workspace / "README.md").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(workspace), "add", "README.md"], check=True, capture_output=True, text=True)
    subprocess.run(["git", "-C", str(workspace), "commit", "-m", "initial"], check=True, capture_output=True, text=True)
    return workspace


class StubManager:
    """Manager stand-in that records calls and replays a canned outcome."""

    def __init__(self, result: dict | None = None, error: Exception | None = None) -> None:
        self.result = result
        self.error = error
        self.calls: list[ReviewInput] = []

    async def review_async(self, params: ReviewInput) -> dict:
        self.calls.append(params)
        if self.error is not None:
            raise self.error
        assert self.result is not None
        return self.result


class StaticStream:
    """Pipe stand-in that yields its bytes once and then reports end of stream."""

    def __init__(self, data: bytes = b"") -> None:
        self.data = data

    async def read(self, size: int = -1) -> bytes:
        chunk, self.data = self.data[:size], self.data[size:]
        return chunk


class CompletedChild:
    """Child that finishes on its own so the tool can report its payload."""

    def __init__(self, stdout: bytes, stderr: bytes, exit_code: int = 0) -> None:
        self.pid = 4242
        self.returncode = exit_code
        self.stdout = StaticStream(stdout)
        self.stderr = StaticStream(stderr)

    async def wait(self) -> int:
        return self.returncode


class NeverExitingChild:
    """Child that only stops when the review tree is killed."""

    def __init__(self) -> None:
        self.pid = 4242
        self.returncode: int | None = None
        self.started = asyncio.Event()
        self.stdout = StaticStream()
        self.stderr = StaticStream()

    async def wait(self) -> int:
        self.started.set()
        await asyncio.Event().wait()
        raise AssertionError("a killed child must not report success")


def _params(workspace: Path) -> ReviewInput:
    return ReviewInput(request="Review the current code", workspace_path=str(workspace))


def _seed_direct_job(store: Path, workspace: Path, owner_pid: int) -> str:
    """Store a direct job whose owning manager process is ``owner_pid``."""
    manager = JobManager(state_dir=store)
    record = JobRecord(
        job_id=uuid.uuid4().hex,
        state=JobState.RUNNING,
        task="A direct job owned by the process that started its loop",
        acceptance_criteria=[],
        workspace_path=str(workspace),
        allowed_paths=["**"],
        required_commands=[],
        provider="deepseek",
        model="deepseek-chat",
        max_minutes=5,
    )
    manager._save(record)
    (manager.jobs_dir / record.job_id / "runner.json").write_text(
        json.dumps({
            "runner": "deepseek-api",
            "model": record.model,
            "worker_ownership": "in-process",
            "owner_pid": owner_pid,
        }),
        encoding="utf-8",
    )
    return record.job_id


def test_delegate_tool_reports_a_store_inside_the_workspace(
    server, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A store the worker could edit is refused as a tool error, with no job stored."""
    manager = JobManager(state_dir=repo)
    monkeypatch.setattr(server, "manager", manager)
    params = DelegateTaskInput(
        task="Add a note to the workspace for this test",
        workspace_path=str(repo),
        allowed_paths=["**"],
        max_minutes=5,
    )

    result = json.loads(asyncio.run(server.model_worker_delegate_task(params)))

    assert result["error"] == "ValueError"
    assert "outside this workspace" in result["detail"]
    assert list(manager.jobs_dir.glob("*/job.json")) == []


def test_review_tool_returns_the_manager_payload(server, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {"exit_code": 0, "output": "looks fine", "diagnostics_tail": ""}
    manager = StubManager(result=payload)
    monkeypatch.setattr(server, "manager", manager)

    result = json.loads(asyncio.run(server.model_worker_review(_params(tmp_path))))

    assert result == payload
    assert manager.calls[0].request == "Review the current code"


def test_review_tool_returns_a_completed_review(server, repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    child = CompletedChild(stdout=b"review output\n", stderr=b"tool trace\n")
    manager = JobManager(state_dir=tmp_path / "state")

    async def fake_spawn(command: list[str], workspace: Path) -> CompletedChild:
        return child

    monkeypatch.setattr(JobManager, "_spawn_review_process", staticmethod(fake_spawn))
    monkeypatch.setattr(server, "manager", manager)

    result = json.loads(asyncio.run(server.model_worker_review(_params(repo))))

    assert result == {
        "exit_code": 0,
        "output": "review output",
        "diagnostics_tail": "tool trace",
    }


def test_review_tool_reports_a_timeout_result(server, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    error = ReviewTimeoutError(
        "The review exceeded 1 minutes; the CodeWhale process tree was terminated.",
        output="partial output",
        diagnostics_tail="still starting",
    )
    monkeypatch.setattr(server, "manager", StubManager(error=error))

    result = json.loads(asyncio.run(server.model_worker_review(_params(tmp_path))))

    assert result == {
        "error": "ReviewTimeoutError",
        "detail": "The review exceeded 1 minutes; the CodeWhale process tree was terminated.",
        "timed_out": True,
        "output": "partial output",
        "diagnostics_tail": "still starting",
    }


def test_review_tool_reports_unexpected_errors(server, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server, "manager", StubManager(error=ValueError("Workspace does not exist: C:\\missing")))

    result = json.loads(asyncio.run(server.model_worker_review(_params(tmp_path))))

    assert result == {"error": "ValueError", "detail": "Workspace does not exist: C:\\missing"}


def test_cancelled_review_tool_cannot_leave_the_child_running(
    server, repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = JobManager(state_dir=tmp_path / "state")
    child = NeverExitingChild()
    kills: list[bool] = []

    async def fake_spawn(command: list[str], workspace: Path) -> NeverExitingChild:
        return child

    def fake_kill(process: NeverExitingChild, force: bool) -> None:
        kills.append(force)
        process.returncode = -9

    monkeypatch.setattr(JobManager, "_spawn_review_process", staticmethod(fake_spawn))
    monkeypatch.setattr(JobManager, "_kill_review_tree", staticmethod(fake_kill))
    monkeypatch.setattr(server, "manager", manager)

    async def scenario() -> None:
        task = asyncio.ensure_future(server.model_worker_review(_params(repo)))
        await asyncio.wait_for(child.started.wait(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())

    assert kills == [False]
    assert child.returncode == -9


# Runner selection -----------------------------------------------------------


def test_server_refuses_an_unknown_runner_selector(
    server, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A host that cannot start must say why instead of running the wrong backend."""
    monkeypatch.setenv("MODEL_WORKER_RUNNER", "deepseek-local")

    with pytest.raises(SystemExit) as caught:
        server._build_manager()

    assert caught.value.code == 2
    error = capsys.readouterr().err
    assert "MODEL_WORKER_RUNNER" in error
    assert "deepseek-local" in error
    assert "'codewhale'" in error and "'deepseek-api'" in error


def test_server_builds_the_direct_backend_when_it_is_selected(
    server, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MODEL_WORKER_RUNNER", "deepseek-api")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    manager = server._build_manager()

    assert isinstance(manager.runner, DirectDeepSeekRunner), "no key is needed until a job runs"


def test_review_tool_reports_the_direct_runner_refusal(
    server, repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = JobManager(state_dir=tmp_path / "state", runner=DirectDeepSeekRunner())
    monkeypatch.setattr(server, "manager", manager)

    result = json.loads(asyncio.run(server.model_worker_review(_params(repo))))

    assert result["error"] == "RunnerCapabilityError"
    assert "codewhale" in result["detail"]


def test_cancel_tool_refuses_a_direct_job_owned_by_another_process(
    server, repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The MCP surface reports the refusal instead of claiming the job stopped."""
    manager = JobManager(state_dir=tmp_path / "state", runner=DirectDeepSeekRunner())
    monkeypatch.setattr(server, "manager", manager)
    job_id = _seed_direct_job(tmp_path / "state", repo, os.getpid())

    result = json.loads(asyncio.run(server.model_worker_cancel_job(JobIdInput(job_id=job_id))))

    assert result["error"] == "JobOwnershipError"
    assert job_id in result["detail"] and str(os.getpid()) in result["detail"]
    assert manager.get(job_id).state == JobState.RUNNING, "work may still be running"
