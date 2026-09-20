from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Callable, Optional

import pytest

from mcp_delgado.manager import JobManager, JobOwnershipError, ReviewTimeoutError, WorkspaceBusyError
from mcp_delgado.runners import (
    CodeWhaleRunner,
    DirectDeepSeekRunner,
    ResponsesReply,
    ResponsesRequest,
    ReviewRun,
    RunnerResult,
    RunnerSelectionError,
    TaskRun,
    TransportError,
)
from mcp_delgado.schemas import DelegateTaskInput, JobRecord, JobState, ReviewInput

TERMINAL_STATES = {
    JobState.SUCCEEDED,
    JobState.FAILED,
    JobState.POLICY_FAILED,
    JobState.CANCELLED,
    JobState.INTERRUPTED,
}


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


def _make_repo(workspace: Path) -> Path:
    workspace.mkdir()
    _git(workspace, "init")
    _git(workspace, "config", "user.email", "test@example.com")
    _git(workspace, "config", "user.name", "Test")
    (workspace / "README.md").write_text("hello\n", encoding="utf-8")
    _git(workspace, "add", "README.md")
    _git(workspace, "commit", "-m", "initial")
    return workspace


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return _make_repo(tmp_path / "repo")


def test_allowed_path_matching(tmp_path: Path) -> None:
    manager = JobManager(state_dir=tmp_path / "state")
    assert manager._is_allowed("src/app.py", ["src"])
    assert manager._is_allowed("tests/test_app.py", ["tests/*.py"])
    assert not manager._is_allowed("README.md", ["src"])


def test_rejects_parent_traversal(tmp_path: Path) -> None:
    manager = JobManager(state_dir=tmp_path / "state")
    with pytest.raises(ValueError):
        manager._normalize_allowed_paths(["../secret"])


def test_status_reports_new_file(repo: Path, tmp_path: Path) -> None:
    manager = JobManager(state_dir=tmp_path / "state")
    before = manager._status(repo, ["new.txt"])
    (repo / "new.txt").write_text("new\n", encoding="utf-8")
    after = manager._status(repo, ["new.txt"])
    assert "new.txt" not in before
    assert after["new.txt"].startswith("??:")


def test_patch_includes_changed_existing_untracked_file(repo: Path, tmp_path: Path) -> None:
    manager = JobManager(state_dir=tmp_path / "state")
    path = repo / "draft.txt"
    path.write_text("before\n", encoding="utf-8")
    before = manager._status(repo, ["draft.txt"])
    path.write_text("after\n", encoding="utf-8")
    record = JobRecord(job_id="b" * 32, task="edit draft", workspace_path=str(repo),
                       state=JobState.SUCCEEDED, acceptance_criteria=[], required_commands=[],
                       allowed_paths=["draft.txt"], changed_paths=["draft.txt"],
                       provider="deepseek", model="deepseek-flash", max_minutes=1)
    manager._save(record)
    manager._write_patch(record, before)
    assert "+after" in manager.read_diff(record.job_id, 10000)


def test_status_detects_changes_to_an_already_dirty_file(repo: Path, tmp_path: Path) -> None:
    manager = JobManager(state_dir=tmp_path / "state")
    target = repo / "README.md"
    target.write_text("first change\n", encoding="utf-8")
    before = manager._status(repo)
    target.write_text("later change\n", encoding="utf-8")
    after = manager._status(repo)
    assert before["README.md"] != after["README.md"]


def test_status_ignores_tool_state_paths(repo: Path, tmp_path: Path) -> None:
    manager = JobManager(state_dir=tmp_path / "state")
    root_state = repo / ".codewhale" / "state"
    root_state.mkdir(parents=True)
    (root_state / "subagents.v1.lock").write_text("", encoding="utf-8")
    nested_state = repo / "tools" / ".codewhale" / "cache"
    nested_state.mkdir(parents=True)
    (nested_state / "session.json").write_text("{}\n", encoding="utf-8")
    (repo / "notes.md").write_text("note\n", encoding="utf-8")

    entries = manager._status(repo)

    assert "notes.md" in entries
    assert [path for path in entries if ".codewhale" in path] == []


def test_tool_state_never_reaches_the_audit_or_the_patch(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The worker's tool state is excluded even when the scope would cover it."""
    manager = JobManager(state_dir=tmp_path / "state")

    def work() -> None:
        state = repo / ".codewhale" / "state"
        state.mkdir(parents=True, exist_ok=True)
        (state / "subagents.v1.lock").write_text("held\n", encoding="utf-8")
        (repo / "notes.md").write_text("note\n", encoding="utf-8")

    _patch_worker(monkeypatch, _FakeWorkerFactory(work=work))

    record = _wait_terminal(manager, manager.submit(_delegate_params(repo, ["*.md"])).job_id)

    assert record.state == JobState.SUCCEEDED
    assert record.changed_paths == ["notes.md"]
    assert record.policy_violations == []
    patch = manager.read_diff(record.job_id, 60_000)
    assert "notes.md" in patch
    assert ".codewhale" not in patch


def test_stored_patch_excludes_tracked_tool_state(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = JobManager(state_dir=tmp_path / "state")
    tracked_state = repo / ".codewhale" / "config.json"
    tracked_state.parent.mkdir(parents=True)
    tracked_state.write_text('{"mode": "managed"}\n', encoding="utf-8")
    _git(repo, "add", ".codewhale/config.json")
    _git(repo, "commit", "-m", "track tool state")

    def work() -> None:
        (repo / ".codewhale" / "config.json").write_text('{"mode": "local"}\n', encoding="utf-8")
        (repo / "notes.md").write_text("note\n", encoding="utf-8")

    _patch_worker(monkeypatch, _FakeWorkerFactory(work=work))

    record = _wait_terminal(manager, manager.submit(_delegate_params(repo, ["."])).job_id)

    patch = manager.read_diff(record.job_id, 60_000)
    assert record.changed_paths == ["notes.md"]
    assert "notes.md" in patch
    assert ".codewhale" not in patch


def test_pytest_state_matching_is_narrow() -> None:
    """Tool-owned pytest state is root-level, exact, and never a substring match."""
    for transient in (
        ".pytest_cache",
        ".pytest_cache/v/cache/lastfailed",
        ".pytest-c0/test_status0",
        ".pytest-tmp-abc/tests/test_app.py",
        ".codewhale/state/session.json",
        "tools/.codewhale/cache/session.json",
    ):
        assert JobManager._is_tool_state_path(transient), transient

    for source in (
        ".pytest_cache_helper/keep.py",
        ".pytest_cache_backup.md",
        ".pytestx/settings.py",
        "src/.pytest_cache/v/cache/lastfailed",
        "tests/.pytest-c0/test_status0",
        "src/.pytest-tmp-abc/tests/test_app.py",
        "pytest-c0/test_status0",
    ):
        assert not JobManager._is_tool_state_path(source), source


def test_status_ignores_pytest_state_at_the_repository_root(repo: Path, tmp_path: Path) -> None:
    """A validation run leaves pytest artifacts behind without a policy failure."""
    manager = JobManager(state_dir=tmp_path / "state")
    for relative in (
        ".pytest_cache/v/cache/lastfailed",
        ".pytest-c0/test_status0",
        ".pytest-tmp-abc/tests/test_app.py",
    ):
        target = repo / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("transient\n", encoding="utf-8")
    (repo / "notes.md").write_text("note\n", encoding="utf-8")

    entries = manager._status(repo)

    assert "notes.md" in entries
    assert [path for path in entries if path.startswith(".pytest")] == []


def test_pytest_state_never_reaches_the_audit_or_the_patch(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = JobManager(state_dir=tmp_path / "state")

    def work() -> None:
        for relative in (".pytest_cache/v/cache/lastfailed", ".pytest-c0/test_status0"):
            target = repo / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("transient\n", encoding="utf-8")
        (repo / "notes.md").write_text("note\n", encoding="utf-8")

    _patch_worker(monkeypatch, _FakeWorkerFactory(work=work))

    record = _wait_terminal(manager, manager.submit(_delegate_params(repo, ["notes.md"])).job_id)

    assert record.state == JobState.SUCCEEDED
    assert record.changed_paths == ["notes.md"]
    assert record.policy_violations == []
    patch = manager.read_diff(record.job_id, 60_000)
    assert "notes.md" in patch
    assert ".pytest" not in patch


def test_stored_patch_excludes_tracked_pytest_state(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """git prefixes diff paths with a/, so the exclusion must strip that prefix."""
    manager = JobManager(state_dir=tmp_path / "state")
    tracked = repo / ".pytest-c0" / "cache.json"
    tracked.parent.mkdir(parents=True)
    tracked.write_text('{"v": 1}\n', encoding="utf-8")
    _git(repo, "add", ".pytest-c0/cache.json")
    _git(repo, "commit", "-m", "track pytest state")

    def work() -> None:
        tracked.write_text('{"v": 2}\n', encoding="utf-8")
        (repo / "notes.md").write_text("note\n", encoding="utf-8")

    _patch_worker(monkeypatch, _FakeWorkerFactory(work=work))

    # The scope covers the whole repository, so the tracked pytest artifact would
    # reach both the audit and the diff unless the exclusion matched it.
    record = _wait_terminal(manager, manager.submit(_delegate_params(repo, ["**"])).job_id)

    assert record.state == JobState.SUCCEEDED
    assert record.changed_paths == ["notes.md"]
    assert record.policy_violations == []
    patch = manager.read_diff(record.job_id, 60_000)
    assert "notes.md" in patch
    assert ".pytest" not in patch


def test_pytest_lookalike_source_paths_still_fail_policy(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The exclusion must not hide a real source path that merely resembles it."""
    manager = JobManager(state_dir=tmp_path / "state")

    def work() -> None:
        (repo / "notes.md").write_text("note\n", encoding="utf-8")
        for relative in (".pytest_cache_helper/keep.py", "src/.pytest-cache/keep.py"):
            target = repo / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("value = 1\n", encoding="utf-8")

    _patch_worker(monkeypatch, _FakeWorkerFactory(work=work))

    record = _wait_terminal(manager, manager.submit(_delegate_params(repo, ["notes.md"])).job_id)

    assert record.state == JobState.POLICY_FAILED
    assert sorted(record.policy_violations) == [".pytest_cache_helper/keep.py", "src/.pytest-cache/keep.py"]
    assert sorted(record.changed_paths) == [
        ".pytest_cache_helper/keep.py",
        "notes.md",
        "src/.pytest-cache/keep.py",
    ]
    patch = manager.read_diff(record.job_id, 60_000)
    assert "notes.md" in patch
    assert "src/.pytest-cache/keep.py" in patch


def test_state_write_survives_a_concurrent_reader(repo: Path, tmp_path: Path) -> None:
    """A second invocation may hold the record open while the job updates it."""
    state = tmp_path / "state"
    manager = JobManager(state_dir=state)
    record = _write_job_record(manager, repo, JobState.RUNNING, None)
    target = state / "jobs" / record.job_id / "job.json"
    failure: list[BaseException] = []

    def save() -> None:
        try:
            manager._save(record.model_copy(update={"summary": "updated"}))
        except BaseException as exc:  # reported by the assertion below
            failure.append(exc)

    with target.open("r", encoding="utf-8") as handle:
        handle.read()
        thread = threading.Thread(target=save)
        thread.start()
        thread.join(timeout=0.5)
    thread.join(timeout=10)

    assert not failure, failure
    assert not thread.is_alive(), "the state write never finished while a reader held the record"
    assert manager.get(record.job_id).summary == "updated"


def test_live_job_is_not_marked_interrupted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manager = JobManager(state_dir=tmp_path / "state")
    job_dir = manager.jobs_dir / ("a" * 32)
    job_dir.mkdir()
    (job_dir / "job.json").write_text("""{
      "job_id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
      "state": "running",
      "task": "A task long enough",
      "acceptance_criteria": [],
      "workspace_path": ".",
      "allowed_paths": ["src"],
      "required_commands": [],
      "provider": "deepseek",
      "model": "deepseek-flash",
      "max_minutes": 30,
      "created_at": "2026-01-01T00:00:00+00:00",
      "pid": 123,
      "summary": "",
      "error": "",
      "changed_paths": [],
      "policy_violations": [],
      "validation_results": []
    }""", encoding="utf-8")
    monkeypatch.setattr(JobManager, "_pid_is_running", staticmethod(lambda pid: True))

    JobManager(state_dir=tmp_path / "state")

    assert manager.get("a" * 32).state.value == "running"


def test_validation_command_uses_argument_list() -> None:
    assert JobManager._validation_args("python -m pytest tests/test_app.py") == [
        "python", "-m", "pytest", "tests/test_app.py",
    ]


@pytest.mark.skipif(os.name != "nt", reason="Windows package managers use cmd shims")
@pytest.mark.parametrize("command", ["npm", "npx", "pnpm", "yarn"])
def test_validation_resolves_windows_package_manager_shims(
    command: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "mcp_delgado.manager.shutil.which",
        lambda candidate: rf"C:\tools\{candidate}" if candidate.endswith(".cmd") else None,
    )
    resolved = JobManager._validation_executable(command)

    assert resolved == rf"C:\tools\{command}.cmd"


def _params_with_check(repo: Path, command: str) -> DelegateTaskInput:
    return DelegateTaskInput(
        task="Run the manager's validation command for this test",
        workspace_path=str(repo),
        allowed_paths=["**"],
        required_commands=[command],
        max_minutes=5,
    )


VALIDATION_KEY = "delgado-validation-key-must-not-leak"


def test_validations_run_without_the_worker_key(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A manager-run check must not be able to read the worker credential.

    The probe is a real allowed Python validation: it runs as its own process and
    prints what it can see. The key is in this process's environment, so the only
    reason the child cannot read it is the environment the manager passes.
    """
    monkeypatch.setenv("DEEPSEEK_API_KEY", VALIDATION_KEY)
    monkeypatch.setenv("DELGADO_VALIDATION_PROBE", "kept")
    probe = repo / "print_validation_env.py"
    probe.write_text(
        "import os\n"
        "print('DEEPSEEK_API_KEY=' + (os.environ.get('DEEPSEEK_API_KEY') or '<absent>'))\n"
        "print('DELGADO_VALIDATION_PROBE=' + os.environ.get('DELGADO_VALIDATION_PROBE', '<absent>'))\n",
        encoding="utf-8",
    )
    manager = JobManager(state_dir=tmp_path / "state", runner=_StubRunner())
    params = _params_with_check(repo, "python print_validation_env.py")

    record = _wait_terminal(manager, manager.submit(params).job_id)

    assert record.state == JobState.SUCCEEDED
    (validation,) = record.validation_results
    assert validation["exit_code"] == 0, validation["output_tail"]
    assert "DEEPSEEK_API_KEY=<absent>" in validation["output_tail"]
    assert "DELGADO_VALIDATION_PROBE=kept" in validation["output_tail"], "the rest of the environment is passed through"

    stored = [
        path.read_text(encoding="utf-8", errors="replace")
        for path in manager.state_dir.rglob("*")
        if path.is_file()
    ]
    assert all(VALIDATION_KEY not in text for text in stored), "the key reached the job store"
    assert VALIDATION_KEY not in json.dumps(record.model_dump(mode="json"))
    assert VALIDATION_KEY not in manager.read_output(record.job_id)
    assert VALIDATION_KEY not in manager.read_diff(record.job_id, 60_000)


def test_validation_output_that_echoes_the_key_is_redacted(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A validation tool that prints its parent environment still cannot store the key."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", VALIDATION_KEY)
    manager = JobManager(state_dir=tmp_path / "state", runner=_StubRunner())

    def echo(args, **kwargs):
        return subprocess.CompletedProcess(args, 1, f"env holds {VALIDATION_KEY}\n", f"trace {VALIDATION_KEY}\n")

    manager.command_runner = echo

    record = _wait_terminal(
        manager, manager.submit(_params_with_check(repo, "python -m compileall -q .")).job_id
    )

    (validation,) = record.validation_results
    assert VALIDATION_KEY not in validation["output_tail"]
    assert "<redacted>" in validation["output_tail"]
    assert VALIDATION_KEY not in json.dumps(record.model_dump(mode="json"))


class FakeReviewStream:
    """Minimal stand-in for an asyncio subprocess pipe."""

    def __init__(self, data: bytes) -> None:
        self.data = data

    async def read(self, size: int = -1) -> bytes:
        chunk, self.data = self.data[:size], self.data[size:]
        return chunk


class FakeReviewProcess:
    """Stand-in for the CodeWhale child: it only exits when something stops it."""

    def __init__(self, stdout: bytes = b"", stderr: bytes = b"", exit_code: int = 0) -> None:
        self.pid = 4242
        self.returncode: int | None = None
        self.stdout = FakeReviewStream(stdout)
        self.stderr = FakeReviewStream(stderr)
        self.exit_code = exit_code
        self.exited = asyncio.Event()
        self.waiting = asyncio.Event()

    def exit(self, returncode: int | None = None) -> None:
        self.returncode = self.exit_code if returncode is None else returncode
        self.exited.set()

    async def wait(self) -> int:
        self.waiting.set()
        await self.exited.wait()
        assert self.returncode is not None
        return self.returncode


def _spawn_fake_process(
    monkeypatch: pytest.MonkeyPatch,
    process: FakeReviewProcess,
    commands: list[list[str]] | None = None,
) -> None:
    async def fake_spawn(command: list[str], workspace: Path) -> FakeReviewProcess:
        if commands is not None:
            commands.append(command)
        return process

    monkeypatch.setattr(JobManager, "_spawn_review_process", staticmethod(fake_spawn))


def _record_tree_kills(
    monkeypatch: pytest.MonkeyPatch,
    process: FakeReviewProcess,
    stubborn: bool = False,
) -> list[bool]:
    """Replace the real process-tree kill and report every force flag it was called with."""
    kills: list[bool] = []

    def fake_kill(target: FakeReviewProcess, force: bool) -> None:
        kills.append(force)
        if force or not stubborn:
            target.exit(-9 if force else -15)

    monkeypatch.setattr(JobManager, "_kill_review_tree", staticmethod(fake_kill))
    return kills


def _review_params(repo: Path, max_minutes: int = 10) -> ReviewInput:
    return ReviewInput(
        request="Review the current code",
        workspace_path=str(repo),
        max_minutes=max_minutes,
    )


def test_review_async_returns_the_review_payload(repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manager = JobManager(state_dir=tmp_path / "state")
    process = FakeReviewProcess(
        stdout="review output\ncaf\u00e9\n".encode("utf-8"),
        stderr=b"tool trace\n",
        exit_code=0,
    )
    commands: list[list[str]] = []
    _spawn_fake_process(monkeypatch, process, commands)
    kills = _record_tree_kills(monkeypatch, process)
    process.exit()

    result = asyncio.run(manager.review_async(_review_params(repo)))

    assert result == {
        "exit_code": 0,
        "output": "review output\ncaf\u00e9",
        "diagnostics_tail": "tool trace",
    }
    assert kills == []
    assert commands[0][-1] == "Review the current code"
    assert "--output-format" in commands[0]
    assert "text" in commands[0]
    assert "--json" not in commands[0]
    assert "read-only" in commands[0]


def test_review_decodes_codewhale_output_as_utf8(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = JobManager(state_dir=tmp_path / "state")
    process = FakeReviewProcess(stdout=b"caf\xc3\xa9 \xff\n", stderr=b"tool trace\n")
    _spawn_fake_process(monkeypatch, process)
    _record_tree_kills(monkeypatch, process)
    process.exit()

    result = asyncio.run(manager.review_async(_review_params(repo)))

    assert result["output"] == "caf\u00e9 \ufffd"
    assert result["diagnostics_tail"] == "tool trace"


def test_review_async_reports_diagnostics_when_output_is_empty(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = JobManager(state_dir=tmp_path / "state")
    process = FakeReviewProcess(stderr=b"model unavailable\n", exit_code=2)
    _spawn_fake_process(monkeypatch, process)
    _record_tree_kills(monkeypatch, process)
    process.exit()

    result = asyncio.run(manager.review_async(_review_params(repo)))

    assert result == {
        "exit_code": 2,
        "output": "model unavailable",
        "diagnostics_tail": "model unavailable",
    }


def test_sync_review_delegates_to_the_async_runner(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = JobManager(state_dir=tmp_path / "state")
    process = FakeReviewProcess(stdout=b"review output\n")
    _spawn_fake_process(monkeypatch, process)
    _record_tree_kills(monkeypatch, process)
    process.exit()

    assert manager.review(_review_params(repo))["output"] == "review output"


def test_review_async_timeout_stops_the_process_tree(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = JobManager(state_dir=tmp_path / "state")
    process = FakeReviewProcess(stdout=b"partial output\n", stderr=b"still thinking\n")
    _spawn_fake_process(monkeypatch, process)
    kills = _record_tree_kills(monkeypatch, process)
    monkeypatch.setattr(CodeWhaleRunner, "review_timeout_seconds", staticmethod(lambda run: 0.01))

    with pytest.raises(ReviewTimeoutError) as caught:
        asyncio.run(manager.review_async(_review_params(repo, max_minutes=1)))

    assert kills == [False]
    assert process.returncode == -15
    assert "exceeded 1 minutes" in str(caught.value)
    assert caught.value.output == "partial output"
    assert caught.value.diagnostics_tail == "still thinking"


def test_review_async_timeout_escalates_when_the_child_ignores_sigterm(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = JobManager(state_dir=tmp_path / "state")
    process = FakeReviewProcess()
    _spawn_fake_process(monkeypatch, process)
    kills = _record_tree_kills(monkeypatch, process, stubborn=True)
    monkeypatch.setattr(CodeWhaleRunner, "review_timeout_seconds", staticmethod(lambda run: 0.01))
    monkeypatch.setattr("mcp_delgado.manager.REVIEW_TERMINATE_GRACE_SECONDS", 0.01)

    with pytest.raises(ReviewTimeoutError):
        asyncio.run(manager.review_async(_review_params(repo)))

    assert kills == [False, True]
    assert process.returncode == -9


def test_review_async_cancellation_stops_the_child_before_it_propagates(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = JobManager(state_dir=tmp_path / "state")
    process = FakeReviewProcess(stdout=b"partial output\n")
    _spawn_fake_process(monkeypatch, process)
    kills = _record_tree_kills(monkeypatch, process)

    async def scenario() -> None:
        task = asyncio.ensure_future(manager.review_async(_review_params(repo)))
        await asyncio.wait_for(process.waiting.wait(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())

    assert kills == [False]
    assert process.returncode == -15


_PARENT_SCRIPT = """
import subprocess
import sys
import time
from pathlib import Path

Path(sys.argv[1]).write_text("parent\\n", encoding="utf-8")
subprocess.Popen([sys.executable, "-c", sys.argv[2], sys.argv[1]])
time.sleep(600)
"""

_GRANDCHILD_SCRIPT = """
import sys
import time
from pathlib import Path

target = Path(sys.argv[1])
while True:
    with target.open("a", encoding="utf-8") as handle:
        handle.write("beat\\n")
    time.sleep(0.05)
"""


def test_review_cancellation_stops_a_real_process_tree(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Drive the real spawn and tree-kill code against a parent process and its own child.

    The manager supervises the review, so replacing only the runner's argv keeps
    the real process-group spawn and the real tree kill under test.
    """
    manager = JobManager(state_dir=tmp_path / "state")
    heartbeat = tmp_path / "heartbeat.txt"
    command = [sys.executable, "-c", _PARENT_SCRIPT, str(heartbeat), _GRANDCHILD_SCRIPT]
    monkeypatch.setattr(CodeWhaleRunner, "review_command", staticmethod(lambda run: command))

    async def scenario() -> None:
        task = asyncio.ensure_future(manager.review_async(_review_params(repo)))
        deadline = asyncio.get_running_loop().time() + 30
        while not (heartbeat.is_file() and "beat" in heartbeat.read_text(encoding="utf-8")):
            assert asyncio.get_running_loop().time() < deadline, "the fake review child never started"
            await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        frozen = heartbeat.read_text(encoding="utf-8")
        await asyncio.sleep(0.75)
        assert heartbeat.read_text(encoding="utf-8") == frozen

    asyncio.run(scenario())


@pytest.mark.parametrize("command", ["powershell Remove-Item file", "python -c 'print(1)'", "pytest ; whoami"])
def test_validation_command_rejects_unsafe_forms(command: str) -> None:
    with pytest.raises(ValueError):
        JobManager._validation_args(command)


# Workspace claims -----------------------------------------------------------


class _FakeWorkerProcess:
    """Stand-in for the CodeWhale child: the manager drives it like a real process."""

    def __init__(self, factory: "_FakeWorkerFactory", command: list[str], cwd: str, pid: int) -> None:
        self.command = command
        self.cwd = cwd
        self.pid = pid
        self.returncode: int | None = None
        self.stopped = threading.Event()
        self._factory = factory

    def communicate(self, timeout: float | None = None) -> tuple[str, None]:
        factory = self._factory
        factory.calls += 1
        if factory.timeout_first and factory.calls == 1:
            raise subprocess.TimeoutExpired(cmd=self.command, timeout=timeout or 0)
        if factory.blocking:
            factory.waiting.set()
            self._await_release()
        if factory.work is not None:
            factory.work()
        if self.returncode is None:
            self.returncode = factory.exit_code
        return "worker output\n", None

    def poll(self) -> int | None:
        return self.returncode

    def stop(self, returncode: int = -1) -> None:
        """End the fake process the way a terminated worker would end."""
        self.stopped.set()
        self.returncode = returncode

    def _await_release(self, timeout: float = 60.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._factory.release.is_set() or self.stopped.is_set():
                return
            time.sleep(0.01)
        raise AssertionError("the test never released the fake worker")


class _FakeWorkerFactory:
    """Build a fresh fake worker process for every submit, without CodeWhale."""

    def __init__(
        self,
        work: Optional[Callable[[], None]] = None,
        exit_code: int = 0,
        blocking: bool = False,
        timeout_first: bool = False,
    ) -> None:
        self.work = work
        self.exit_code = exit_code
        self.blocking = blocking
        self.timeout_first = timeout_first
        self.release = threading.Event()
        self.waiting = threading.Event()
        self.calls = 0
        self.processes: list[_FakeWorkerProcess] = []

    def __call__(self, command: list[str], **kwargs: object) -> _FakeWorkerProcess:
        process = _FakeWorkerProcess(self, command, str(kwargs.get("cwd", "")), 4_000_000 + len(self.processes))
        self.processes.append(process)
        return process


class _ManagerSubprocess:
    """The runner's subprocess module with a fake worker and the real helpers."""

    def __init__(self, factory: _FakeWorkerFactory) -> None:
        self.Popen = factory
        self.run = subprocess.run
        self.DEVNULL = subprocess.DEVNULL
        self.PIPE = subprocess.PIPE
        self.STDOUT = subprocess.STDOUT
        self.TimeoutExpired = subprocess.TimeoutExpired


def _patch_worker(monkeypatch: pytest.MonkeyPatch, factory: _FakeWorkerFactory) -> _FakeWorkerFactory:
    """Run job threads through the real runner code path without CodeWhale."""
    monkeypatch.setattr("mcp_delgado.runners.subprocess", _ManagerSubprocess(factory))
    monkeypatch.setattr(CodeWhaleRunner, "_terminate_process", staticmethod(lambda process: process.stop()))
    return factory


def _delegate_params(workspace: Path, allowed_paths: list[str] | None = None) -> DelegateTaskInput:
    return DelegateTaskInput(
        task="Add a short note to the workspace for this test",
        workspace_path=str(workspace),
        allowed_paths=allowed_paths or ["**"],
        max_minutes=5,
    )


def _wait_for(predicate: Callable[[], bool], timeout: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


def _wait_terminal(manager: JobManager, job_id: str) -> JobRecord:
    assert _wait_for(lambda: manager.get(job_id).state in TERMINAL_STATES), "the job never finished"
    return manager.get(job_id)


def _dead_pid() -> int:
    process = subprocess.Popen([sys.executable, "-c", "pass"])
    process.wait()
    assert not JobManager._pid_is_running(process.pid)
    return process.pid


def _write_lock_file(manager: JobManager, workspace: Path, job_id: str, pid: Optional[int]) -> Path:
    """Leave behind the lock file a crashed manager process would have written."""
    path = manager._workspace_lock_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "job_id": job_id,
        "workspace_path": str(workspace),
        "pid": pid,
        "created_at": "2026-01-01T00:00:00+00:00",
        "token": "abandoned",
    }), encoding="utf-8")
    return path


def _write_job_record(manager: JobManager, workspace: Path, state: JobState, pid: Optional[int]) -> JobRecord:
    """Store a job record that no live manager thread owns."""
    record = JobRecord(
        job_id=uuid.uuid4().hex,
        state=state,
        task="An orphaned job record for the workspace lock test",
        acceptance_criteria=[],
        workspace_path=str(workspace),
        allowed_paths=["**"],
        required_commands=[],
        provider="deepseek",
        model="deepseek-flash",
        max_minutes=5,
        pid=pid,
    )
    manager._save(record)
    return record


def _assert_workspace_is_reusable(manager: JobManager, workspace: Path, first_job_id: str) -> None:
    assert not manager._workspace_lock_path(workspace).is_file(), "the lock outlived the job"
    follow_up = _wait_terminal(manager, manager.submit(_delegate_params(workspace)).job_id)
    assert follow_up.job_id != first_job_id
    assert follow_up.state in TERMINAL_STATES


# Store placement ------------------------------------------------------------


def test_submit_refuses_a_store_inside_the_delegated_workspace(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A store a worker may edit is refused before any record, lock, or thread exists."""
    _patch_worker(monkeypatch, _FakeWorkerFactory())

    for store in (repo, repo / "store", repo / "nested" / "store"):
        manager = JobManager(state_dir=store)

        with pytest.raises(ValueError) as caught:
            manager.submit(_delegate_params(repo, ["."]))

        message = str(caught.value)
        assert str(store.resolve()) in message
        assert str(repo) in message
        assert "outside this workspace" in message
        assert list(manager.jobs_dir.glob("*/job.json")) == [], "a refused submit stored a job"
        assert not manager._workspace_lock_path(repo).is_file(), "a refused submit claimed the workspace"


def test_a_store_outside_the_workspace_runs_a_job_that_allows_the_root(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Allowing every path in a checkout is fine; only the store's placement is refused."""
    _patch_worker(monkeypatch, _FakeWorkerFactory())
    manager = JobManager(state_dir=tmp_path / "state")

    record = _wait_terminal(manager, manager.submit(_delegate_params(repo, ["."])).job_id)

    assert record.state == JobState.SUCCEEDED
    assert record.allowed_paths == ["."]


def test_second_submit_in_the_same_workspace_is_rejected(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "state"
    first_manager = JobManager(state_dir=state)
    second_manager = JobManager(state_dir=state)
    factory = _patch_worker(monkeypatch, _FakeWorkerFactory(blocking=True))

    first = first_manager.submit(_delegate_params(repo))
    assert _wait_for(lambda: first_manager.get(first.job_id).state == JobState.RUNNING)
    assert first_manager._workspace_lock_path(repo).is_file()

    started = time.monotonic()
    with pytest.raises(WorkspaceBusyError) as caught:
        second_manager.submit(_delegate_params(repo))
    elapsed = time.monotonic() - started

    assert elapsed < 10.0, "a refused submit must fail quickly instead of waiting for the running job"
    assert caught.value.job_id == first.job_id
    assert first.job_id in str(caught.value)
    assert str(repo) in str(caught.value)
    stored = sorted(path.parent.name for path in second_manager.jobs_dir.glob("*/job.json"))
    assert stored == [first.job_id], "a refused submit must not store a job record"

    factory.release.set()
    _wait_terminal(first_manager, first.job_id)

    assert not first_manager._workspace_lock_path(repo).is_file()
    follow_up = _wait_terminal(second_manager, second_manager.submit(_delegate_params(repo)).job_id)
    assert follow_up.job_id != first.job_id


def test_different_workspaces_run_concurrently(repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manager = JobManager(state_dir=tmp_path / "state")
    other_repo = _make_repo(tmp_path / "other")
    factory = _patch_worker(monkeypatch, _FakeWorkerFactory(blocking=True))

    first = manager.submit(_delegate_params(repo))
    second = manager.submit(_delegate_params(other_repo))

    assert _wait_for(lambda: manager.get(first.job_id).state == JobState.RUNNING)
    assert _wait_for(lambda: manager.get(second.job_id).state == JobState.RUNNING)
    assert manager._workspace_lock_path(repo).is_file()
    assert manager._workspace_lock_path(other_repo).is_file()
    assert manager._workspace_lock_path(repo) != manager._workspace_lock_path(other_repo)

    factory.release.set()
    _wait_terminal(manager, first.job_id)
    _wait_terminal(manager, second.job_id)


def test_lock_is_released_after_a_successful_job(repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manager = JobManager(state_dir=tmp_path / "state")
    _patch_worker(monkeypatch, _FakeWorkerFactory())

    record = _wait_terminal(manager, manager.submit(_delegate_params(repo)).job_id)

    assert record.state == JobState.SUCCEEDED
    _assert_workspace_is_reusable(manager, repo, record.job_id)


def test_lock_is_released_after_a_failed_job(repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manager = JobManager(state_dir=tmp_path / "state")
    _patch_worker(monkeypatch, _FakeWorkerFactory(exit_code=1))

    record = _wait_terminal(manager, manager.submit(_delegate_params(repo)).job_id)

    assert record.state == JobState.FAILED
    _assert_workspace_is_reusable(manager, repo, record.job_id)


def test_lock_is_released_after_a_worker_timeout(repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manager = JobManager(state_dir=tmp_path / "state")
    _patch_worker(monkeypatch, _FakeWorkerFactory(timeout_first=True))

    record = _wait_terminal(manager, manager.submit(_delegate_params(repo)).job_id)

    assert record.state == JobState.FAILED
    assert "exceeded" in record.error
    _assert_workspace_is_reusable(manager, repo, record.job_id)


def test_lock_is_released_after_cancel(repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manager = JobManager(state_dir=tmp_path / "state")
    factory = _patch_worker(monkeypatch, _FakeWorkerFactory(blocking=True))

    record = manager.submit(_delegate_params(repo))
    assert _wait_for(lambda: manager.get(record.job_id).state == JobState.RUNNING)
    assert factory.waiting.wait(timeout=30)

    manager.cancel(record.job_id)

    assert _wait_for(lambda: not manager._workspace_lock_path(repo).is_file()), "cancel kept the lock"
    factory.release.set()
    _assert_workspace_is_reusable(manager, repo, record.job_id)


class _StubbornDirectApi:
    """Answer only when the test releases it, ignoring the runner's close on purpose.

    A transport that honors ``on_open`` makes cancellation prompt. One that does
    not is the worst case a caller can build, and it is the case the workspace
    claim has to survive: the record is CANCELLED while the loop is still working.
    """

    def __init__(self) -> None:
        self.opened = threading.Event()
        self.release = threading.Event()
        self.requests: list[ResponsesRequest] = []

    def __call__(self, request: ResponsesRequest) -> ResponsesReply:
        self.requests.append(request)
        if len(self.requests) == 1:
            self.opened.set()
            assert self.release.wait(timeout=30), "the test never released the stubborn transport"
        return ResponsesReply(status=200, body=json.dumps(_api_body(_api_message("worked"))))


def test_a_cancelled_job_keeps_its_workspace_until_its_thread_exits(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A terminal record must not hand a live worker's checkout to a second job."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", FAKE_KEY)
    api = _StubbornDirectApi()
    commands: list[list[str]] = []
    manager = JobManager(state_dir=tmp_path / "state", runner=DirectDeepSeekRunner(api))
    manager.command_runner = lambda args, **kwargs: commands.append(args) or subprocess.CompletedProcess(
        args, 0, "ok\n", ""
    )

    record = manager.submit(_params_with_check(repo, "python -m pytest -q tests"))
    assert _wait_for(api.opened.is_set), "the loop never reached its request"

    cancelled = manager.cancel(record.job_id)

    assert cancelled.state == JobState.CANCELLED
    assert manager._workspace_lock_path(repo).is_file(), "a live owner keeps its workspace"
    started = time.monotonic()
    with pytest.raises(WorkspaceBusyError) as caught:
        manager.submit(_params_with_check(repo, "python -m pytest -q tests"))
    assert time.monotonic() - started < 10.0, "a refused submit must fail quickly"
    assert caught.value.job_id == record.job_id
    assert len(api.requests) == 1, "the second job must not reach the stopped loop's transport"

    api.release.set()

    assert _wait_for(lambda: not manager._workspace_lock_path(repo).is_file()), "a cancelled job kept its lock"
    terminal = manager.get(record.job_id)

    assert terminal.state == JobState.CANCELLED
    assert terminal.validation_results == [], "a cancelled job starts no validation command"
    assert commands == []

    follow_up = _wait_terminal(manager, manager.submit(_delegate_params(repo)).job_id)
    assert follow_up.job_id != record.job_id
    assert follow_up.state == JobState.SUCCEEDED


def test_terminal_record_keeps_its_lock_while_its_owner_is_alive(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A finished record is not evidence that its workspace is free.

    A release can fail, and a cancel publishes CANCELLED while the owning job
    thread is still returning from a stopped worker. The process the lock names is
    what decides, so the workspace stays claimed until that process is gone.
    """
    manager = JobManager(state_dir=tmp_path / "state")
    _patch_worker(monkeypatch, _FakeWorkerFactory())
    finished = _wait_terminal(manager, manager.submit(_delegate_params(repo)).job_id)
    lock = _write_lock_file(manager, repo, finished.job_id, os.getpid())

    with pytest.raises(WorkspaceBusyError) as caught:
        manager.submit(_delegate_params(repo))

    assert caught.value.job_id == finished.job_id
    assert lock.is_file(), "a live owner kept its lock"
    assert sorted(path.parent.name for path in manager.jobs_dir.glob("*/job.json")) == [finished.job_id]

    # The same lock is recoverable once nothing it names is running, without a restart.
    lock.write_text(json.dumps({
        "job_id": finished.job_id,
        "workspace_path": str(repo),
        "pid": _dead_pid(),
        "created_at": "2026-01-01T00:00:00+00:00",
        "token": "abandoned",
    }), encoding="utf-8")

    follow_up = _wait_terminal(manager, manager.submit(_delegate_params(repo)).job_id)

    assert follow_up.job_id != finished.job_id


def test_stale_lock_with_a_dead_owner_is_recovered(repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manager = JobManager(state_dir=tmp_path / "state")
    _patch_worker(monkeypatch, _FakeWorkerFactory())
    _write_lock_file(manager, repo, "b" * 32, _dead_pid())

    record = _wait_terminal(manager, manager.submit(_delegate_params(repo)).job_id)

    assert record.state == JobState.SUCCEEDED


def test_stale_lock_with_a_dead_worker_is_recovered(repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The manager is not rebuilt here, so recovery must come from the pid rule
    # rather than from the interrupted-job sweep in __init__.
    manager = JobManager(state_dir=tmp_path / "state")
    _patch_worker(monkeypatch, _FakeWorkerFactory())
    orphan = _write_job_record(manager, repo, JobState.RUNNING, _dead_pid())
    _write_lock_file(manager, repo, orphan.job_id, _dead_pid())

    record = _wait_terminal(manager, manager.submit(_delegate_params(repo)).job_id)

    assert record.state == JobState.SUCCEEDED


def test_unreadable_lock_is_recovered(repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manager = JobManager(state_dir=tmp_path / "state")
    _patch_worker(monkeypatch, _FakeWorkerFactory())
    lock = manager._workspace_lock_path(repo)
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("this lock was truncated mid-write\n", encoding="utf-8")

    record = _wait_terminal(manager, manager.submit(_delegate_params(repo)).job_id)

    assert record.state == JobState.SUCCEEDED


# Runner injection -----------------------------------------------------------


class _StubRunner:
    """A WorkerRunner stand-in that replaces CodeWhale without replacing policy.

    It performs the test's workspace work, records every scoped request the
    manager hands it, and reports a result the manager must still audit.
    """

    name = "stub"
    label = "Stub"

    def __init__(
        self,
        work: Optional[Callable[[], None]] = None,
        exit_code: int = 0,
        output: str = "stub runner output\n",
        timed_out: bool = False,
        blocking: bool = False,
        review_command: Optional[list[str]] = None,
        review_timeout_seconds: float = 600.0,
        review_process: Optional[object] = None,
        usage: Optional[dict] = None,
    ) -> None:
        self.work = work
        self.exit_code = exit_code
        self.output = output
        self.timed_out = timed_out
        self.blocking = blocking
        self.review_argv = review_command
        self.review_budget = review_timeout_seconds
        self.review_process = review_process
        self.usage = usage
        self.tasks: list[TaskRun] = []
        self.reviews: list[ReviewRun] = []
        self.cancelled: list[str] = []
        self.cancelled_pids: list[int] = []
        self.waiting = threading.Event()
        self.release = threading.Event()
        self.stopped = threading.Event()

    def run_task(self, run: TaskRun, on_start: Callable[[int], None]) -> RunnerResult:
        self.tasks.append(run)
        on_start(4_500_000 + len(self.tasks))
        if self.blocking:
            self.waiting.set()
            deadline = time.monotonic() + 30
            while not (self.release.is_set() or self.stopped.is_set()):
                assert time.monotonic() < deadline, "the test never released the stub worker"
                time.sleep(0.01)
        if self.work is not None:
            self.work()
        if self.timed_out:
            return RunnerResult(
                output=self.output,
                exit_code=self.exit_code,
                error=f"The worker exceeded {run.max_minutes} minutes.",
                timed_out=True,
                usage=dict(self.usage or {}),
            )
        return RunnerResult(output=self.output, exit_code=self.exit_code, usage=dict(self.usage or {}))

    def cancel(self, job_id: str) -> bool:
        self.cancelled.append(job_id)
        self.stopped.set()
        return True

    def cancel_pid(self, pid: int) -> None:
        self.cancelled_pids.append(pid)

    def review_command(self, run: ReviewRun) -> list[str]:
        self.reviews.append(run)
        return list(self.review_argv or [])

    def review_timeout_seconds(self, run: ReviewRun) -> float:
        return self.review_budget

    async def spawn_review_process(self, command: list[str], workspace: Path):
        if self.review_process is not None:
            return self.review_process
        return await CodeWhaleRunner.spawn_review_process(command, workspace)

    def kill_review_tree(self, process, force: bool) -> None:
        CodeWhaleRunner.kill_review_tree(process, force)


class _ForeignStubRunner(_StubRunner):
    """A runner in a manager process that does not hold a job in memory."""

    def cancel(self, job_id: str) -> bool:
        return False


def _no_codewhale() -> str:
    raise AssertionError("the manager must not build a CodeWhale command when a runner is injected")


def test_default_runner_is_the_codewhale_adapter(tmp_path: Path) -> None:
    manager = JobManager(state_dir=tmp_path / "state")

    assert isinstance(manager.runner, CodeWhaleRunner)
    assert manager.runner.name == "codewhale"


def test_injected_runner_runs_the_job_while_the_manager_keeps_the_audit(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second backend changes only how the worker runs, never what is enforced."""
    monkeypatch.setattr("mcp_delgado.runners.codewhale_executable", _no_codewhale)

    def work() -> None:
        (repo / "notes.md").write_text("note\n", encoding="utf-8")
        (repo / "outside.md").write_text("leak\n", encoding="utf-8")

    runner = _StubRunner(work=work)
    manager = JobManager(state_dir=tmp_path / "state", runner=runner)

    record = _wait_terminal(manager, manager.submit(_delegate_params(repo, ["notes.md"])).job_id)

    assert record.state == JobState.POLICY_FAILED
    assert record.changed_paths == ["notes.md", "outside.md"]
    assert record.policy_violations == ["outside.md"]
    assert record.exit_code == 0
    assert record.summary == "stub runner output"
    assert record.pid == 4_500_001, "the manager must store the pid the runner reports"
    assert runner.tasks[0].job_id == record.job_id
    assert runner.tasks[0].workspace == repo.resolve()
    assert "You are an implementation worker" in runner.tasks[0].prompt
    assert "notes.md" in runner.tasks[0].prompt
    assert "notes.md" in manager.read_diff(record.job_id, 60_000)
    identity = manager.jobs_dir / record.job_id / "runner.json"
    assert json.loads(identity.read_text(encoding="utf-8")) == {
        "runner": "stub",
        "model": record.model,
        "worker_ownership": "child",
    }


def test_injected_runner_reports_the_timed_out_job(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("mcp_delgado.runners.codewhale_executable", _no_codewhale)
    runner = _StubRunner(exit_code=0, timed_out=True)
    manager = JobManager(state_dir=tmp_path / "state", runner=runner)

    record = _wait_terminal(manager, manager.submit(_delegate_params(repo)).job_id)

    assert record.state == JobState.FAILED, "a timed-out run stays failed even when the child exits clean"
    assert record.error == "The worker exceeded 5 minutes."
    assert record.exit_code == 0


def test_manager_cancels_through_the_injected_runner(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("mcp_delgado.runners.codewhale_executable", _no_codewhale)
    runner = _StubRunner(blocking=True)
    manager = JobManager(state_dir=tmp_path / "state", runner=runner)

    record = manager.submit(_delegate_params(repo))
    assert _wait_for(lambda: manager.get(record.job_id).state == JobState.RUNNING)
    assert runner.waiting.wait(timeout=30)

    cancelled = manager.cancel(record.job_id)

    assert runner.cancelled == [record.job_id]
    assert cancelled.state == JobState.CANCELLED
    assert _wait_for(lambda: not manager._workspace_lock_path(repo).is_file()), "cancel kept the lock"
    assert manager.get(record.job_id).state == JobState.CANCELLED
    assert manager.get(record.job_id).error == "The manager cancelled this job."


def test_injected_runner_supplies_the_review_command_and_budget(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    process = FakeReviewProcess(stdout=b"stub review\n", stderr=b"trace\n")
    runner = _StubRunner(review_command=[sys.executable, "-c", "pass"], review_process=process)
    _record_tree_kills(monkeypatch, process)
    process.exit()
    manager = JobManager(state_dir=tmp_path / "state", runner=runner)

    result = asyncio.run(manager.review_async(_review_params(repo)))

    assert result == {"exit_code": 0, "output": "stub review", "diagnostics_tail": "trace"}
    assert runner.reviews[0].request == "Review the current code"
    assert runner.reviews[0].workspace == repo.resolve()


def test_review_timeout_names_the_injected_runner(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The runner label is the diagnostics that identify the backend in a timeout."""
    process = FakeReviewProcess(stdout=b"partial output\n")
    runner = _StubRunner(
        review_command=[sys.executable, "-c", "pass"],
        review_timeout_seconds=0.01,
        review_process=process,
    )
    kills = _record_tree_kills(monkeypatch, process)
    manager = JobManager(state_dir=tmp_path / "state", runner=runner)

    with pytest.raises(ReviewTimeoutError) as caught:
        asyncio.run(manager.review_async(_review_params(repo)))

    assert kills == [False]
    assert "the Stub process tree was terminated" in str(caught.value)
    assert caught.value.output == "partial output"


# Runner selection from the environment ---------------------------------------

FAKE_KEY = "unit-test-placeholder-key"


def test_default_runner_follows_the_environment_selector(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MODEL_WORKER_RUNNER", raising=False)
    assert isinstance(JobManager(state_dir=tmp_path / "state").runner, CodeWhaleRunner)

    monkeypatch.setenv("MODEL_WORKER_RUNNER", "deepseek-api")
    manager = JobManager(state_dir=tmp_path / "state")

    assert isinstance(manager.runner, DirectDeepSeekRunner)
    assert manager.runner.name == "deepseek-api"


def test_unknown_runner_selector_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODEL_WORKER_RUNNER", "deepseek-local")

    with pytest.raises(RunnerSelectionError) as caught:
        JobManager(state_dir=tmp_path / "state")

    message = str(caught.value)
    assert "deepseek-local" in message
    assert "'codewhale'" in message and "'deepseek-api'" in message


def test_injected_runner_wins_over_the_environment_selector(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MODEL_WORKER_RUNNER", "deepseek-local")
    runner = _StubRunner()

    manager = JobManager(state_dir=tmp_path / "state", runner=runner)

    assert manager.runner is runner, "an explicit injection is never second-guessed by the selector"


def test_runner_identity_records_only_integer_counters(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("mcp_delgado.runners.codewhale_executable", _no_codewhale)
    runner = _StubRunner(usage={"steps": 2, "tool_calls": 1, "model": "leaked", "note": None, "flag": True})
    manager = JobManager(state_dir=tmp_path / "state", runner=runner)

    record = _wait_terminal(manager, manager.submit(_delegate_params(repo)).job_id)

    identity = json.loads((manager.jobs_dir / record.job_id / "runner.json").read_text(encoding="utf-8"))
    assert identity == {
        "runner": "stub",
        "model": record.model,
        "worker_ownership": "child",
        "usage": {"steps": 2, "tool_calls": 1},
    }


# The direct backend through the manager ---------------------------------------


def _api_body(*output: dict, status: str = "completed") -> dict:
    return {"id": "resp_test", "status": status, "output": list(output)}


def _api_call(name: str, arguments: dict) -> dict:
    return {"type": "function_call", "call_id": "call_1", "name": name, "arguments": json.dumps(arguments)}


def _api_message(text: str) -> dict:
    return {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]}


class _DirectFakeApi:
    """A Responses API stand-in: it replays answers and records each request.

    ``work`` runs before the first answer, which is how a test lets something
    else in the checkout change while the job is live: the manager still has to
    audit that change, because the audit is between the workspace before the job
    and the workspace after it, whoever wrote the bytes.
    """

    def __init__(self, *replies: dict, work: Optional[Callable[[], None]] = None) -> None:
        self.replies = list(replies)
        self.work = work
        self.requests: list[ResponsesRequest] = []

    def __call__(self, request: ResponsesRequest) -> ResponsesReply:
        self.requests.append(request)
        if self.work is not None:
            self.work()
        assert self.replies, "the direct runner made an unexpected extra request"
        return ResponsesReply(status=200, body=json.dumps(self.replies.pop(0)))


class _BlockingDirectApi:
    """Hold the loop inside its request until the manager cancels the job."""

    def __init__(self) -> None:
        self.opened = threading.Event()
        self.closed = threading.Event()

    def __call__(self, request: ResponsesRequest) -> ResponsesReply:
        request.on_open(self.closed.set)
        self.opened.set()
        assert self.closed.wait(timeout=30), "cancel must close the live request"
        raise TransportError("the Responses API request failed: the response was closed")


def test_direct_job_keeps_the_manager_audit_and_stores_no_secret(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A direct job changes how the worker runs, never what the manager enforces."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", FAKE_KEY)

    def outside_write() -> None:
        # Something other than the loop changes the checkout while the job runs.
        (repo / "outside.md").write_text("leak\n", encoding="utf-8")

    api = _DirectFakeApi(
        _api_body(_api_call("write_file", {"path": "notes.md", "content": "note\n"})),
        _api_body(_api_call("write_file", {"path": "outside.md", "content": "leak\n"})),
        _api_body(_api_message("Wrote notes.md, and outside.md was refused.")),
        work=outside_write,
    )
    runner = DirectDeepSeekRunner(api)
    manager = JobManager(state_dir=tmp_path / "state", runner=runner)
    checked: list[list[str]] = []

    def fake_command_runner(args, **kwargs):
        checked.append(args)
        return subprocess.CompletedProcess(args, 0, "ok\n", "")

    manager.command_runner = fake_command_runner
    params = DelegateTaskInput(
        task="Add a note to the workspace for this test",
        workspace_path=str(repo),
        allowed_paths=["notes.md"],
        required_commands=["python -m pytest -q tests"],
        max_minutes=5,
    )

    record = _wait_terminal(manager, manager.submit(params).job_id)

    assert record.state == JobState.POLICY_FAILED
    assert record.exit_code == 0
    assert record.changed_paths == ["notes.md", "outside.md"]
    assert record.policy_violations == ["outside.md"]
    assert "- write_file: PathDeniedError" in record.summary, "the loop refused the out-of-scope write"
    assert (repo / "outside.md").read_text(encoding="utf-8") == "leak\n", "the refused call changed nothing"
    assert record.pid is None, "a direct job has no worker pid a process may stop"
    assert "Wrote notes.md" in record.summary
    assert checked and checked[0][1:] == ["-m", "pytest", "-q", "tests"], "validations stay the manager's"
    assert record.validation_results[0]["exit_code"] == 0
    patch = manager.read_diff(record.job_id, 60_000)
    assert "notes.md" in patch
    assert "outside.md" in patch, "the audit reports what the worker changed, allowed or not"

    identity = json.loads((manager.jobs_dir / record.job_id / "runner.json").read_text(encoding="utf-8"))
    assert identity["runner"] == "deepseek-api"
    assert identity["model"] == record.model
    assert identity["usage"] == {"steps": 3, "tool_calls": 2}
    assert identity["worker_ownership"] == "in-process"
    assert identity["owner_pid"] == os.getpid(), "ownership metadata names the owning manager"
    assert FAKE_KEY not in json.dumps(identity)

    stored = [
        path.read_text(encoding="utf-8", errors="replace")
        for path in (manager.jobs_dir / record.job_id).iterdir()
        if path.is_file()
    ]
    assert all(FAKE_KEY not in text for text in stored), "no durable artifact may carry the key"
    assert FAKE_KEY not in json.dumps(record.model_dump(mode="json"))
    assert all(FAKE_KEY not in json.dumps(request.payload) for request in api.requests)


def test_direct_job_is_cancelled_through_the_manager(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", FAKE_KEY)
    api = _BlockingDirectApi()
    manager = JobManager(state_dir=tmp_path / "state", runner=DirectDeepSeekRunner(api))

    record = manager.submit(_delegate_params(repo))
    assert _wait_for(api.opened.is_set), "the loop never reached its request"

    cancelled = manager.cancel(record.job_id)

    assert cancelled.state == JobState.CANCELLED
    assert cancelled.error == "The manager cancelled this job."
    assert _wait_for(lambda: not manager._workspace_lock_path(repo).is_file()), "cancel kept the lock"
    assert _wait_terminal(manager, record.job_id).state == JobState.CANCELLED


class _HoldingManager(JobManager):
    """A manager whose job thread is held back until the test releases it.

    Holding this seam is how a test reaches the state a caller races against: the
    job is submitted, claimed, and cancellable, and its thread has not run yet.
    """

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        self.held: list[tuple[str, dict[str, str]]] = []

    def _start_job_thread(self, record: JobRecord, before: dict[str, str]) -> None:
        self.held.append((record.job_id, before))

    def release_job_thread(self) -> None:
        job_id, before = self.held.pop(0)
        threading.Thread(target=self._run_job, args=(job_id, before), daemon=True).start()


def test_cancel_immediately_after_submit_runs_no_worker(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A job cancelled the instant it was submitted does no work at all.

    The cancel lands before the job thread starts, which is the race a
    runner-owned flag cannot close. The manager's token is what the thread reads
    first, so CANCELLED is published and never overwritten by RUNNING, and neither
    the loop nor a validation command ever runs.
    """
    monkeypatch.setenv("DEEPSEEK_API_KEY", FAKE_KEY)
    api = _DirectFakeApi()  # any request would be an unexpected extra request
    commands: list[list[str]] = []
    manager = _HoldingManager(state_dir=tmp_path / "state", runner=DirectDeepSeekRunner(api))
    manager.command_runner = lambda args, **kwargs: commands.append(args) or subprocess.CompletedProcess(
        args, 0, "ok\n", ""
    )

    record = manager.submit(_params_with_check(repo, "python -m pytest -q tests"))

    cancelled = manager.cancel(record.job_id)

    assert cancelled.state == JobState.CANCELLED
    assert [job_id for job_id, _ in manager.held] == [record.job_id], "the job thread has not run yet"

    manager.release_job_thread()

    assert _wait_for(lambda: not manager._workspace_lock_path(repo).is_file()), "a cancelled job kept its lock"
    terminal = manager.get(record.job_id)

    assert terminal.state == JobState.CANCELLED, "RUNNING must never overwrite CANCELLED"
    assert terminal.error == "The manager cancelled this job."
    assert terminal.started_at is None, "the job never started"
    assert terminal.changed_paths == []
    assert terminal.validation_results == []
    assert commands == [], "a cancelled job starts no validation command"
    assert api.requests == [], "a cancelled job makes no request"
    assert manager.read_output(record.job_id) == ""
    assert manager.read_diff(record.job_id, 60_000) == ""


def test_direct_job_without_a_key_fails_without_calling_the_api(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    api = _DirectFakeApi()
    manager = JobManager(state_dir=tmp_path / "state", runner=DirectDeepSeekRunner(api))

    record = _wait_terminal(manager, manager.submit(_delegate_params(repo)).job_id)

    assert record.state == JobState.FAILED
    assert record.exit_code is None
    assert "DEEPSEEK_API_KEY" in record.error
    assert api.requests == []
    assert manager.read_diff(record.job_id, 60_000) == ""
    assert _wait_for(lambda: not manager._workspace_lock_path(repo).is_file()), "a failed job kept the lock"


# Direct-runner ownership across manager processes -----------------------------


def _write_direct_job_record(
    manager: JobManager, workspace: Path, state: JobState, owner_pid: Optional[int]
) -> JobRecord:
    """Leave behind the record and ownership metadata a direct job writes."""
    record = _write_job_record(manager, workspace, state, None)
    (manager.jobs_dir / record.job_id / "runner.json").write_text(
        json.dumps({
            "runner": "deepseek-api",
            "model": record.model,
            "worker_ownership": "in-process",
            "owner_pid": owner_pid,
        }),
        encoding="utf-8",
    )
    return record


def test_cross_process_cancel_of_a_direct_job_is_refused(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second manager process refuses instead of publishing a false CANCELLED.

    The second manager stands in for another process: the job's loop is held by
    the first manager, so the second one holds no handle for it. Its own startup
    sweep must also leave the live job and its workspace lock alone.
    """
    monkeypatch.setenv("DEEPSEEK_API_KEY", FAKE_KEY)
    api = _BlockingDirectApi()
    store = tmp_path / "state"
    owner = JobManager(state_dir=store, runner=DirectDeepSeekRunner(api))
    record = owner.submit(_delegate_params(repo))
    assert _wait_for(api.opened.is_set), "the loop never reached its request"

    other = JobManager(state_dir=store, runner=DirectDeepSeekRunner())

    assert owner.get(record.job_id).pid is None, "a direct job records no killable worker pid"
    assert other.get(record.job_id).state == JobState.RUNNING, "an in-process job was swept"
    assert owner._workspace_lock_path(repo).is_file(), "a live job lost its workspace lock"

    with pytest.raises(JobOwnershipError) as caught:
        other.cancel(record.job_id)

    assert record.job_id in str(caught.value)
    assert str(os.getpid()) in str(caught.value), "the refusal must name the owning process"
    assert caught.value.job_id == record.job_id
    assert other.get(record.job_id).state == JobState.RUNNING, "work may still be running"
    assert not api.closed.is_set(), "a second process must not stop the loop"

    cancelled = owner.cancel(record.job_id)

    assert cancelled.state == JobState.CANCELLED
    assert cancelled.error == "The manager cancelled this job."
    assert api.closed.wait(timeout=30), "the owning process cancels its own job promptly"
    assert _wait_for(lambda: not owner._workspace_lock_path(repo).is_file()), "cancel kept the lock"
    assert _wait_terminal(owner, record.job_id).state == JobState.CANCELLED


def test_cross_process_cancel_never_signals_a_manager_pid(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second process on the default backend refuses instead of stopping the owner."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", FAKE_KEY)
    api = _BlockingDirectApi()
    store = tmp_path / "state"
    owner = JobManager(state_dir=store, runner=DirectDeepSeekRunner(api))
    record = owner.submit(_delegate_params(repo))
    assert _wait_for(api.opened.is_set), "the loop never reached its request"
    other = JobManager(state_dir=store, runner=_ForeignStubRunner())

    with pytest.raises(JobOwnershipError):
        other.cancel(record.job_id)

    assert other.runner.cancelled_pids == [], "a manager pid was signalled"
    assert not api.closed.is_set()
    assert other.get(record.job_id).state == JobState.RUNNING

    owner.cancel(record.job_id)
    assert api.closed.wait(timeout=30)


def test_startup_recovery_interrupts_a_direct_job_whose_owner_is_gone(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The next manager start interrupts the orphaned job and frees its workspace."""
    store = tmp_path / "state"
    manager = JobManager(state_dir=store)
    _patch_worker(monkeypatch, _FakeWorkerFactory())
    orphan = _write_direct_job_record(manager, repo, JobState.RUNNING, _dead_pid())
    lock = _write_lock_file(manager, repo, orphan.job_id, _dead_pid())

    recovered = JobManager(state_dir=store)

    record = recovered.get(orphan.job_id)
    assert record.state == JobState.INTERRUPTED
    assert record.error == "The MCP server stopped before this job finished."
    assert record.pid is None, "recovery must not invent a worker pid"
    assert not lock.is_file(), "startup recovery left the workspace locked"
    _assert_workspace_is_reusable(recovered, repo, orphan.job_id)


def test_cancel_of_an_orphaned_direct_job_records_it_interrupted(repo: Path, tmp_path: Path) -> None:
    """A cancel that finds the owning manager dead never claims the work was stopped."""
    manager = JobManager(state_dir=tmp_path / "state")
    orphan = _write_direct_job_record(manager, repo, JobState.RUNNING, _dead_pid())
    lock = _write_lock_file(manager, repo, orphan.job_id, _dead_pid())

    record = manager.cancel(orphan.job_id)

    assert record.state == JobState.INTERRUPTED
    assert record.error == "The MCP server stopped before this job finished."
    assert not lock.is_file(), "an orphaned job kept the workspace lock"


def test_cancel_refuses_a_recorded_pid_that_names_this_process(repo: Path, tmp_path: Path) -> None:
    """Corrupt state must not turn a cancel into a signal for the manager itself."""
    manager = JobManager(state_dir=tmp_path / "state")
    manager.runner = _ForeignStubRunner()
    stored = _write_job_record(manager, repo, JobState.RUNNING, os.getpid())

    with pytest.raises(JobOwnershipError) as caught:
        manager.cancel(stored.job_id)

    assert str(os.getpid()) in str(caught.value)
    assert manager.runner.cancelled_pids == [], "this process must never be signalled"
    assert manager.get(stored.job_id).state == JobState.RUNNING
