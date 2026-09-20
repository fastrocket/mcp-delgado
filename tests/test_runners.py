from __future__ import annotations

import asyncio
import io
import json
import os
import signal
import subprocess
import sys
import threading
import urllib.error
from pathlib import Path
from typing import Optional

import pytest

from mcp_delgado import runners
from mcp_delgado.runners import (
    CANCELLED_MESSAGE,
    DEEPSEEK_API_KEY_ENV,
    DEEPSEEK_RESPONSES_URL,
    DIRECT_HARD_MAX_RESPONSE_BYTES,
    DIRECT_HARD_MAX_STEPS,
    CancellationToken,
    CodeWhaleRunner,
    DirectDeepSeekRunner,
    ResponsesReply,
    ResponsesRequest,
    ReviewRun,
    RunnerCapabilityError,
    RunnerResult,
    RunnerSelectionError,
    TaskRun,
    TransportError,
    WORKER_OWNERSHIP_CHILD,
    WORKER_OWNERSHIP_IN_PROCESS,
    WorkerRunner,
    _read_bounded,
    deepseek_http_transport,
    select_runner,
    worker_ownership,
)


class _StubSubprocess:
    """The runner's subprocess module with a fake Popen and the real helpers."""

    def __init__(self, *processes: "_FakeProcess") -> None:
        self._processes = list(processes)
        self.commands: list[list[str]] = []

    def __getattr__(self, name: str) -> object:
        return getattr(subprocess, name)

    def Popen(self, command: list[str], **kwargs: object) -> "_FakeProcess":
        self.commands.append(command)
        return self._processes.pop(0)


class _FakeProcess:
    """A worker process stand-in driven exactly like the real child."""

    def __init__(
        self,
        pid: int = 5_000,
        output: str = "worker output\n",
        exit_code: int = 0,
        timeout_once: bool = False,
        blocking: bool = False,
    ) -> None:
        self.pid = pid
        self.output = output
        self.exit_code = exit_code
        self.timeout_once = timeout_once
        self.blocking = blocking
        self.returncode: int | None = None
        self.calls = 0
        self.waiting = threading.Event()
        self.release = threading.Event()

    def communicate(self, timeout: float | None = None) -> tuple[str, None]:
        self.calls += 1
        if self.timeout_once and self.calls == 1:
            raise subprocess.TimeoutExpired(cmd="codewhale", timeout=timeout or 0)
        if self.blocking:
            self.waiting.set()
            self.release.wait(timeout=30)
        if self.returncode is None:
            self.returncode = self.exit_code
        return self.output, None

    def poll(self) -> int | None:
        return self.returncode

    def stop(self, returncode: int = -1) -> None:
        """End the fake process the way a terminated worker would end."""
        self.returncode = returncode


class _FakeChild:
    """Only the attributes the tree kill reads from a spawned review child."""

    def __init__(self, pid: int = 4_242) -> None:
        self.pid = pid


def _task_run(
    workspace: Path, max_minutes: int = 5, cancel_token: Optional[CancellationToken] = None
) -> TaskRun:
    return TaskRun(
        job_id="a" * 32,
        prompt="Add the note",
        workspace=workspace,
        provider="deepseek",
        model="deepseek-flash",
        max_minutes=max_minutes,
        cancel_token=cancel_token,
    )


def _review_run(workspace: Path, max_minutes: int = 10) -> ReviewRun:
    return ReviewRun(
        request="Review the current code",
        workspace=workspace,
        provider="deepseek",
        model="deepseek-flash",
        max_minutes=max_minutes,
    )


def _patch_terminate(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Replace the real tree stop and report every pid it was asked to stop."""
    stopped: list[int] = []

    def fake_terminate(process: _FakeProcess) -> None:
        stopped.append(process.pid)
        process.stop()

    monkeypatch.setattr(CodeWhaleRunner, "_terminate_process", staticmethod(fake_terminate))
    return stopped


def test_code_whale_runner_satisfies_the_worker_runner_protocol() -> None:
    assert isinstance(CodeWhaleRunner(), WorkerRunner)


def test_runners_declare_how_they_hold_their_worker() -> None:
    """Only a child-backed worker has a pid the manager may store and signal."""

    class _Undeclared:
        """A runner written before ownership was part of the protocol."""

    class _Unknown:
        worker_ownership = "sidecar"

    assert CodeWhaleRunner.worker_ownership == WORKER_OWNERSHIP_CHILD
    assert DirectDeepSeekRunner.worker_ownership == WORKER_OWNERSHIP_IN_PROCESS
    assert worker_ownership(CodeWhaleRunner()) == WORKER_OWNERSHIP_CHILD
    assert worker_ownership(DirectDeepSeekRunner()) == WORKER_OWNERSHIP_IN_PROCESS
    assert worker_ownership(_Undeclared()) == WORKER_OWNERSHIP_CHILD
    assert worker_ownership(_Unknown()) == WORKER_OWNERSHIP_CHILD


def test_task_command_carries_the_scoped_prompt_for_codewhale(tmp_path: Path) -> None:
    command = CodeWhaleRunner().task_command(_task_run(tmp_path, max_minutes=7))

    assert "codewhale" in Path(command[0]).name
    assert command[command.index("--provider") + 1] == "deepseek"
    assert command[command.index("--model") + 1] == "deepseek-flash"
    assert command[command.index("--approval-policy") + 1] == "never"
    assert command[command.index("--sandbox-mode") + 1] == "workspace-write"
    assert command[command.index("-C") + 1] == str(tmp_path)
    assert command[command.index("exec") + 1 : command.index("exec") + 3] == ["--auto", "--json"]
    assert command[-1] == "Add the note"


def test_task_command_uses_the_configured_executable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODEL_WORKER_CODEWHALE", r"C:\tools\codewhale.exe")

    command = CodeWhaleRunner().task_command(_task_run(tmp_path))

    assert command[0] == r"C:\tools\codewhale.exe"


def test_review_command_is_a_read_only_text_run(tmp_path: Path) -> None:
    command = CodeWhaleRunner().review_command(_review_run(tmp_path))

    assert "--output-format" in command
    assert command[command.index("--output-format") + 1] == "text"
    assert "--json" not in command
    assert command[command.index("--sandbox-mode") + 1] == "read-only"
    assert command[-1] == "Review the current code"


def test_review_timeout_seconds_uses_the_minute_budget(tmp_path: Path) -> None:
    assert CodeWhaleRunner.review_timeout_seconds(_review_run(tmp_path, max_minutes=3)) == 180.0


def test_run_task_reports_the_output_pid_and_exit_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    process = _FakeProcess(output="worker output\n", exit_code=0)
    stub = _StubSubprocess(process)
    monkeypatch.setattr(runners, "subprocess", stub)
    started: list[int] = []

    result = CodeWhaleRunner().run_task(_task_run(tmp_path), started.append)

    assert result == RunnerResult(output="worker output\n", exit_code=0)
    assert result.timed_out is False
    assert started == [process.pid]
    assert stub.commands[0][-1] == "Add the note"


def test_run_task_stops_the_tree_and_reports_the_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    process = _FakeProcess(timeout_once=True)
    monkeypatch.setattr(runners, "subprocess", _StubSubprocess(process))
    stopped = _patch_terminate(monkeypatch)

    result = CodeWhaleRunner().run_task(_task_run(tmp_path, max_minutes=3), lambda pid: None)

    assert result.timed_out is True
    assert result.error == "The worker exceeded 3 minutes."
    assert result.exit_code == -1
    assert result.output == "worker output\n", "output produced before the stop is still reported"
    assert stopped == [process.pid]


def test_run_task_forgets_the_process_when_it_finishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    process = _FakeProcess()
    monkeypatch.setattr(runners, "subprocess", _StubSubprocess(process))
    runner = CodeWhaleRunner()
    run = _task_run(tmp_path)

    runner.run_task(run, lambda pid: None)

    assert runner.cancel(run.job_id) is False


def test_cancel_stops_a_tracked_process_and_reports_ownership(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    process = _FakeProcess(blocking=True)
    monkeypatch.setattr(runners, "subprocess", _StubSubprocess(process))
    stopped = _patch_terminate(monkeypatch)
    runner = CodeWhaleRunner()
    run = _task_run(tmp_path)
    worker = threading.Thread(target=runner.run_task, args=(run, lambda pid: None), daemon=True)
    worker.start()

    assert process.waiting.wait(timeout=30), "the fake worker never started"
    assert runner.cancel("b" * 32) is False, "another job's id must not stop this process"
    assert runner.cancel(run.job_id) is True
    assert stopped == [process.pid]

    process.release.set()
    worker.join(timeout=30)
    assert not worker.is_alive()


def test_code_whale_runner_starts_no_child_for_a_cancelled_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A job cancelled before the worker starts never spawns one."""
    stub = _StubSubprocess()  # no fake process is queued: a Popen call would fail
    monkeypatch.setattr(runners, "subprocess", stub)
    token = CancellationToken("a" * 32)
    token.cancel()
    started: list[int] = []

    result = CodeWhaleRunner().run_task(_task_run(tmp_path, cancel_token=token), started.append)

    assert stub.commands == []
    assert started == [], "the manager is told about a worker only once one exists"
    assert result.exit_code is None
    assert result.error == CANCELLED_MESSAGE


@pytest.mark.skipif(os.name != "nt", reason="taskkill is the Windows tree stop")
def test_cancel_pid_uses_taskkill_on_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    class _Result:
        returncode = 0

    monkeypatch.setattr(
        "mcp_delgado.runners.subprocess.run",
        lambda args, **kwargs: calls.append(args) or _Result(),
    )

    CodeWhaleRunner().cancel_pid(4_242)

    assert calls == [["taskkill", "/PID", "4242", "/T", "/F"]]


@pytest.mark.skipif(os.name != "nt", reason="taskkill is the Windows tree stop")
def test_cancel_pid_reports_a_failed_taskkill(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Result:
        returncode = 1

    monkeypatch.setattr("mcp_delgado.runners.subprocess.run", lambda args, **kwargs: _Result())

    with pytest.raises(RuntimeError):
        CodeWhaleRunner().cancel_pid(4_242)


@pytest.mark.skipif(os.name == "nt", reason="POSIX signals the recorded pid")
def test_cancel_pid_signals_the_recorded_pid(monkeypatch: pytest.MonkeyPatch) -> None:
    signals: list[tuple[int, int]] = []
    monkeypatch.setattr("mcp_delgado.runners.os.kill", lambda pid, sig: signals.append((pid, sig)))

    CodeWhaleRunner().cancel_pid(4_242)

    assert signals == [(4_242, signal.SIGTERM)]


def test_cancel_pid_refuses_to_signal_the_calling_process(monkeypatch: pytest.MonkeyPatch) -> None:
    """A recorded worker pid is never the manager, so this pid is refused outright."""
    calls: list[object] = []
    monkeypatch.setattr(
        "mcp_delgado.runners.subprocess.run", lambda args, **kwargs: calls.append(args)
    )
    monkeypatch.setattr("mcp_delgado.runners.os.kill", lambda pid, sig: calls.append((pid, sig)))

    with pytest.raises(RunnerCapabilityError) as caught:
        CodeWhaleRunner().cancel_pid(os.getpid())

    assert str(os.getpid()) in str(caught.value)
    assert calls == [], "the process that owns the job must never be signalled"


def test_spawn_review_process_leads_its_own_process_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict = {}
    sentinel = object()

    async def fake_exec(*command: str, **kwargs: object) -> object:
        captured["command"] = command
        captured.update(kwargs)
        return sentinel

    monkeypatch.setattr("mcp_delgado.runners.asyncio.create_subprocess_exec", fake_exec)
    command = [sys.executable, "-c", "pass"]

    process = asyncio.run(CodeWhaleRunner().spawn_review_process(command, tmp_path))

    assert process is sentinel
    assert captured["command"] == tuple(command)
    assert captured["cwd"] == str(tmp_path)
    if os.name == "nt":
        assert captured["creationflags"] == subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        assert captured["start_new_session"] is True


@pytest.mark.skipif(os.name != "nt", reason="taskkill is the Windows tree stop")
def test_terminate_process_uses_taskkill_on_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    class _Result:
        returncode = 0

    monkeypatch.setattr(
        "mcp_delgado.runners.subprocess.run",
        lambda args, **kwargs: calls.append(args) or _Result(),
    )

    CodeWhaleRunner._terminate_process(_FakeChild())

    assert calls == [["taskkill", "/PID", "4242", "/T", "/F"]]


@pytest.mark.skipif(os.name == "nt", reason="POSIX terminates the child before force-killing it")
def test_terminate_process_escalates_on_posix() -> None:
    class _Process:
        pid = 4_242

        def __init__(self) -> None:
            self.terminated = 0
            self.killed = 0

        def terminate(self) -> None:
            self.terminated += 1

        def wait(self, timeout: float | None = None) -> None:
            raise subprocess.TimeoutExpired(cmd="codewhale", timeout=timeout or 0)

        def kill(self) -> None:
            self.killed += 1

    process = _Process()

    CodeWhaleRunner._terminate_process(process)

    assert (process.terminated, process.killed) == (1, 1)


@pytest.mark.skipif(os.name != "nt", reason="taskkill is the Windows tree stop")
def test_kill_review_tree_uses_taskkill(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    class _Result:
        returncode = 0

    monkeypatch.setattr(
        "mcp_delgado.runners.subprocess.run",
        lambda args, **kwargs: calls.append(args) or _Result(),
    )

    CodeWhaleRunner.kill_review_tree(_FakeChild(), force=False)

    assert calls == [["taskkill", "/PID", "4242", "/T", "/F"]]


@pytest.mark.skipif(os.name == "nt", reason="POSIX signals the child's process group")
def test_kill_review_tree_signals_the_process_group(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[int, int]] = []
    monkeypatch.setattr("mcp_delgado.runners.os.getpgid", lambda pid: pid + 1)
    monkeypatch.setattr("mcp_delgado.runners.os.killpg", lambda pgid, sig: calls.append((pgid, sig)))

    CodeWhaleRunner.kill_review_tree(_FakeChild(), force=True)

    assert calls == [(4_243, signal.SIGKILL)]


def test_real_review_child_is_stopped_with_its_tree(tmp_path: Path) -> None:
    """Exercise the real spawn and the real tree stop together."""
    runner = CodeWhaleRunner()

    async def scenario() -> int | None:
        process = await runner.spawn_review_process(
            [sys.executable, "-c", "import time; time.sleep(600)"], tmp_path
        )
        try:
            runner.kill_review_tree(process, False)
            await asyncio.wait_for(process.wait(), timeout=30)
        finally:
            if process.returncode is None:
                runner.kill_review_tree(process, True)
        return process.returncode

    assert asyncio.run(scenario()) != 0


# Direct DeepSeek Responses API runner ---------------------------------------
#
# Every test below drives the loop through an injected transport, so no test
# opens a socket and no test needs a credential. The key used here is a constant
# placeholder, never a real secret: the runner reads it from the environment at
# execution time, and the non-leakage tests assert it cannot escape.

FAKE_KEY = "unit-test-placeholder-key"
CALL_ID = "call_1"


class _RecordingTransport:
    """Replay canned answers and record every request the loop sent."""

    def __init__(self, *replies: object) -> None:
        self.replies = list(replies)
        self.requests: list[ResponsesRequest] = []

    def __call__(self, request: ResponsesRequest) -> ResponsesReply:
        self.requests.append(request)
        if not self.replies:
            raise AssertionError("the runner made an unexpected extra request")
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        if isinstance(reply, ResponsesReply):
            return reply
        return ResponsesReply(status=200, body=json.dumps(reply))

    def payload(self, index: int) -> dict:
        return self.requests[index].payload

    def tool_outputs(self, index: int) -> list[str]:
        return [
            item["output"]
            for item in self.payload(index)["input"]
            if isinstance(item, dict) and item.get("type") == "function_call_output"
        ]


class _BlockingTransport:
    """Answer only after the live request has been closed by cancellation."""

    def __init__(self) -> None:
        self.opened = threading.Event()
        self.closed = threading.Event()
        self.requests: list[ResponsesRequest] = []

    def __call__(self, request: ResponsesRequest) -> ResponsesReply:
        self.requests.append(request)
        request.on_open(self._close)
        self.opened.set()
        if not self.closed.wait(timeout=30):
            raise AssertionError("cancel must close the live request")
        raise TransportError("the Responses API request failed: the response was closed")

    def _close(self) -> None:
        self.closed.set()


class _FakeResponse:
    """The part of an ``http.client`` response the default transport reads."""

    def __init__(self, data: bytes, status: int = 200) -> None:
        self.status = status
        self.data = data
        self.closed = False

    def read(self, size: int = -1) -> bytes:
        chunk, self.data = self.data[:size], self.data[size:]
        return chunk

    def close(self) -> None:
        self.closed = True

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *exc: object) -> bool:
        self.close()
        return False


class _Clock:
    """A monotonic clock the test can move, so a deadline needs no sleeping."""

    def __init__(self, start: float = 1_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _body(*output: dict, status: str = "completed", **extra: object) -> dict:
    document: dict = {"id": "resp_test", "object": "response", "status": status, "output": list(output)}
    document.update(extra)
    return document


def _call(name: str, arguments: object = "", call_id: str = CALL_ID) -> dict:
    if not isinstance(arguments, str):
        arguments = json.dumps(arguments)
    return {"type": "function_call", "call_id": call_id, "name": name, "arguments": arguments}


def _message(text: str) -> dict:
    return {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]}


def _direct_run(
    workspace: Path,
    allowed_paths: tuple[str, ...] = ("**",),
    max_minutes: int = 5,
    cancel_token: Optional[CancellationToken] = None,
) -> TaskRun:
    return TaskRun(
        job_id="d" * 32,
        prompt="Add the note the manager asked for",
        workspace=workspace,
        provider="deepseek",
        model="deepseek-chat",
        max_minutes=max_minutes,
        allowed_paths=allowed_paths,
        cancel_token=cancel_token,
    )


@pytest.fixture
def api_key(monkeypatch: pytest.MonkeyPatch) -> str:
    """Provide the variable at execution time, with no real credential anywhere."""
    monkeypatch.setenv(DEEPSEEK_API_KEY_ENV, FAKE_KEY)
    return FAKE_KEY


@pytest.fixture
def direct_workspace(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "src" / "app.py").write_text("alpha\nbeta\n", encoding="utf-8")
    return root


def _run_direct(runner: DirectDeepSeekRunner, run: TaskRun) -> RunnerResult:
    """Run one job and assert the runner reported its owning process first."""
    started: list[int] = []
    result = runner.run_task(run, started.append)
    assert started == [os.getpid()], "the runner reports the live owner while the job runs"
    return result


# Selection ------------------------------------------------------------------


def test_runner_selection_defaults_to_codewhale(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MODEL_WORKER_RUNNER", raising=False)

    assert isinstance(select_runner(), CodeWhaleRunner)
    assert select_runner().name == "codewhale"
    assert isinstance(select_runner(""), CodeWhaleRunner)
    assert isinstance(select_runner("codewhale"), CodeWhaleRunner)


def test_runner_selection_accepts_the_direct_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODEL_WORKER_RUNNER", "deepseek-api")

    runner = select_runner()

    assert isinstance(runner, DirectDeepSeekRunner)
    assert runner.name == "deepseek-api"
    assert runner.label == "DeepSeek Responses API"
    assert isinstance(select_runner("  DeepSeek-API  "), DirectDeepSeekRunner)


def test_runner_selection_refuses_an_unknown_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODEL_WORKER_RUNNER", "deepseek-local")

    with pytest.raises(RunnerSelectionError) as caught:
        select_runner()

    message = str(caught.value)
    assert "MODEL_WORKER_RUNNER" in message
    assert "'deepseek-local'" in message
    assert "'codewhale'" in message and "'deepseek-api'" in message

    monkeypatch.delenv("MODEL_WORKER_RUNNER")
    with pytest.raises(RunnerSelectionError):
        select_runner("gpt-5")


def test_direct_runner_satisfies_the_worker_runner_protocol() -> None:
    assert isinstance(DirectDeepSeekRunner(), WorkerRunner)


def test_direct_runner_refuses_an_invalid_cap() -> None:
    for kwargs in (
        {"max_steps": 0},
        {"max_steps": DIRECT_HARD_MAX_STEPS + 1},
        {"max_tool_calls": -1},
        {"max_tool_output_bytes": 0},
        {"max_response_bytes": DIRECT_HARD_MAX_RESPONSE_BYTES + 1},
        {"max_output_tokens": 0},
        {"request_timeout_seconds": 0},
    ):
        with pytest.raises(ValueError):
            DirectDeepSeekRunner(**kwargs)  # type: ignore[arg-type]


def test_direct_runner_targets_the_documented_endpoint() -> None:
    runner = DirectDeepSeekRunner()

    assert runner.url == DEEPSEEK_RESPONSES_URL == "https://api.deepseek.com/responses"
    assert isinstance(runner.transport, type(deepseek_http_transport))


# Request contract -----------------------------------------------------------


def test_direct_replays_reasoning_before_tool_results_without_logging_it(api_key: str, direct_workspace: Path) -> None:
    reasoning = {"type": "reasoning", "id": "rs_test", "content": [
        {"type": "reasoning_text", "text": "private test reasoning"}], "summary": []}
    call = _call("read_file", {"path": "src/app.py"})
    transport = _RecordingTransport(_body(reasoning, call), _body(_message("Done.")))
    result = _run_direct(DirectDeepSeekRunner(transport), _direct_run(direct_workspace))
    assert result.exit_code == 0
    replay = transport.payload(1)["input"]
    assert replay[1:3] == [reasoning, call]
    assert replay[3]["type"] == "function_call_output"
    assert "private test reasoning" not in result.output


def test_direct_runner_sends_the_documented_responses_payload(api_key: str, direct_workspace: Path) -> None:
    transport = _RecordingTransport(_body(_message("Nothing to do.")))
    runner = DirectDeepSeekRunner(transport, max_output_tokens=1_024)

    result = _run_direct(runner, _direct_run(direct_workspace))

    assert result.exit_code == 0
    assert result.error == ""
    assert result.output.strip().endswith("Nothing to do.")
    request = transport.requests[0]
    assert request.url == "https://api.deepseek.com/responses"
    assert 0 < request.timeout_seconds <= 300
    payload = transport.payload(0)
    assert payload["model"] == "deepseek-chat"
    assert payload["store"] is False
    assert payload["tool_choice"] == "auto"
    assert payload["reasoning"] == {"effort": "low"}
    assert payload["max_output_tokens"] == 1_024
    assert "Add the note the manager asked for" in json.dumps(payload["input"])
    assert "no shell" in payload["instructions"]
    assert "the manager runs them itself" in payload["instructions"]
    assert "32 model turns" in payload["instructions"]
    tools = payload["tools"]
    assert [tool["name"] for tool in tools] == [
        "list_files", "read_file", "replace_text", "search_text", "write_file",
    ]
    assert all(tool["type"] == "function" for tool in tools)
    assert all(tool["parameters"]["type"] == "object" for tool in tools)
    assert api_key not in json.dumps(payload), "the key never belongs in a request body"
    assert api_key not in repr(request), "the key never belongs in a repr"
    assert result.usage == {"steps": 1, "tool_calls": 0}


def test_direct_runner_runs_a_multi_turn_tool_loop(api_key: str, direct_workspace: Path) -> None:
    transport = _RecordingTransport(
        _body(_call("read_file", {"path": "src/app.py"})),
        _body(_call("write_file", {"path": "src/notes.md", "content": "note\n"}, call_id="call_2")),
        _body(_message("Added src/notes.md after reading src/app.py.")),
    )
    runner = DirectDeepSeekRunner(transport)

    result = _run_direct(runner, _direct_run(direct_workspace))

    assert result.exit_code == 0
    assert (direct_workspace / "src" / "notes.md").read_text(encoding="utf-8") == "note\n"
    assert "Added src/notes.md" in result.output
    assert "- read_file: ok" in result.output
    assert "- write_file: ok" in result.output
    assert result.usage["steps"] == 3
    assert result.usage["tool_calls"] == 2

    second = transport.payload(1)["input"]
    assert {
        "type": "function_call",
        "call_id": CALL_ID,
        "name": "read_file",
        "arguments": json.dumps({"path": "src/app.py"}),
    } in second
    assert any(
        item.get("type") == "function_call_output" and "alpha" in item["output"] for item in second
    ), "the model must see the file it asked for"
    third = transport.payload(2)["input"]
    assert any(
        item.get("type") == "function_call_output" and "created" in item["output"] for item in third
    ), "the model must see the result of its write"


# Tool errors ----------------------------------------------------------------


def test_direct_runner_returns_structured_tool_errors_and_keeps_going(
    api_key: str, direct_workspace: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("outside the workspace\n", encoding="utf-8")
    transport = _RecordingTransport(
        _body(
            _call("read_file", {"path": "../outside.txt"}, call_id="call_1"),
            _call("run_shell", {"command": "git status"}, call_id="call_2"),
            _call("read_file", "{not json", call_id="call_3"),
            _call("write_file", {"path": "src/nested/../escape.txt", "content": "x"}, call_id="call_4"),
            _call("write_file", {"path": "outside.txt", "content": "leak"}, call_id="call_5"),
            _call("read_file", {"path": ".env"}, call_id="call_6"),
        ),
        _body(_message("Every call was refused, so nothing changed.")),
    )
    runner = DirectDeepSeekRunner(transport)

    result = _run_direct(runner, _direct_run(direct_workspace, allowed_paths=("src",)))

    assert result.exit_code == 0, "a refused tool call must not end the job"
    outputs = [json.loads(item) for item in transport.tool_outputs(1)]
    assert len(outputs) == 6
    assert outputs[0]["error"] == "UnsafePathError"
    assert outputs[1]["error"] == "WorkspaceToolError"
    assert outputs[1]["tools"] == [
        "list_files", "read_file", "replace_text", "search_text", "write_file",
    ]
    assert "Invalid arguments for read_file" in outputs[2]["message"]
    assert outputs[3]["error"] == "UnsafePathError"
    assert outputs[4]["error"] == "PathDeniedError"
    assert outputs[5]["error"] == "ExcludedPathError"
    assert not (direct_workspace / "escape.txt").exists()
    assert not (direct_workspace / "outside.txt").exists()
    assert outside.read_text(encoding="utf-8") == "outside the workspace\n"
    assert result.usage["tool_calls"] == 6
    assert "- run_shell: WorkspaceToolError" in result.output


# Budgets --------------------------------------------------------------------


def test_direct_runner_stops_at_the_step_cap(api_key: str, direct_workspace: Path) -> None:
    transport = _RecordingTransport(
        *[_body(_call("list_files", {}, call_id=f"call_{index}")) for index in range(3)]
    )
    runner = DirectDeepSeekRunner(transport, max_steps=2)

    result = _run_direct(runner, _direct_run(direct_workspace))

    assert len(transport.requests) == 2
    assert result.exit_code is None
    assert "2 model turns" in result.error
    assert result.timed_out is False
    assert result.usage == {"steps": 2, "tool_calls": 2}


def test_direct_runner_stops_at_the_tool_call_cap(api_key: str, direct_workspace: Path) -> None:
    transport = _RecordingTransport(
        _body(_call("list_files", {}, call_id="call_1"), _call("list_files", {}, call_id="call_2"))
    )
    runner = DirectDeepSeekRunner(transport, max_tool_calls=1)

    result = _run_direct(runner, _direct_run(direct_workspace))

    assert len(transport.requests) == 1
    assert result.exit_code is None
    assert "tool-call budget" in result.error
    assert result.usage["tool_calls"] == 1


def test_direct_runner_stops_at_the_tool_output_budget(api_key: str, direct_workspace: Path) -> None:
    transport = _RecordingTransport(_body(_call("read_file", {"path": "src/app.py"})))
    runner = DirectDeepSeekRunner(transport, max_tool_output_bytes=5)

    result = _run_direct(runner, _direct_run(direct_workspace))

    assert len(transport.requests) == 1
    assert result.exit_code is None
    assert "tool-output budget" in result.error


# Answers the loop must not trust --------------------------------------------


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        (ResponsesReply(status=200, body="not json at all"), "not JSON"),
        (ResponsesReply(status=200, body="[]"), "not an object"),
        (ResponsesReply(status=200, body=""), "not JSON"),
        ({"id": "resp", "output": []}, "is not a finished answer"),
        (_body(status="in_progress", output=[]), "still 'in_progress'"),
        (_body(status="incomplete", output=[], incomplete_details={"reason": "max_output_tokens"}), "max_output_tokens"),
        (_body(status="failed", output=[], error={"message": "model overloaded"}), "model overloaded"),
        (_body(status="cancelled", output=[]), "cancelled"),
        (_body(status="completed", output={"type": "message"}), "output is not a list"),
        (_body(42), "is not an object"),
        (_body({"type": "function_call", "call_id": "call_1", "arguments": "{}"}), "without a name"),
        (_body({"type": "function_call", "name": "read_file", "arguments": "{}"}), "without a call id"),
        (_body({"type": "function_call", "call_id": "call_1", "name": "read_file", "arguments": 4}), "not JSON text"),
    ],
)
def test_direct_runner_fails_safely_on_an_answer_it_cannot_use(
    api_key: str, direct_workspace: Path, reply: object, expected: str
) -> None:
    transport = _RecordingTransport(reply)

    result = _run_direct(DirectDeepSeekRunner(transport), _direct_run(direct_workspace))

    assert len(transport.requests) == 1
    assert result.exit_code is None
    assert result.timed_out is False
    assert expected in result.error


@pytest.mark.parametrize("status", [400, 401, 402, 429, 500, 503])
def test_direct_runner_reports_an_http_error_status(
    api_key: str, direct_workspace: Path, status: int
) -> None:
    transport = _RecordingTransport(
        ResponsesReply(status=status, body=json.dumps({"error": {"message": "request refused"}}))
    )

    result = _run_direct(DirectDeepSeekRunner(transport), _direct_run(direct_workspace))

    assert result.exit_code is None
    assert f"HTTP {status}" in result.error
    assert "request refused" in result.error


def test_direct_runner_reports_a_transport_failure_and_survives_a_broken_one(
    api_key: str, direct_workspace: Path
) -> None:
    refused = _RecordingTransport(TransportError("the Responses API request failed: connection refused"))
    broken = _RecordingTransport(RuntimeError("boom"))

    refused_result = _run_direct(DirectDeepSeekRunner(refused), _direct_run(direct_workspace))
    broken_result = _run_direct(DirectDeepSeekRunner(broken), _direct_run(direct_workspace))

    assert refused_result.exit_code is None
    assert "connection refused" in refused_result.error
    assert broken_result.exit_code is None
    assert "transport failed: RuntimeError" in broken_result.error


# Timeouts and cancellation ---------------------------------------------------


def test_direct_runner_stops_at_its_wall_clock_budget(api_key: str, direct_workspace: Path) -> None:
    clock = _Clock()
    recorded: list[ResponsesRequest] = []

    def transport(request: ResponsesRequest) -> ResponsesReply:
        recorded.append(request)
        clock.advance(61)  # the model answers, but the granted minute is already gone
        return ResponsesReply(status=200, body=json.dumps(_body(_call("list_files", {}))))

    runner = DirectDeepSeekRunner(transport, time_source=clock)

    result = _run_direct(runner, _direct_run(direct_workspace, max_minutes=1))

    assert len(recorded) == 1
    assert result.timed_out is True
    assert result.exit_code is None
    assert result.error == "The worker exceeded 1 minutes."


def test_direct_runner_reports_a_request_timeout(api_key: str, direct_workspace: Path) -> None:
    transport = _RecordingTransport(
        TransportError("the Responses API did not answer within 300 seconds", timed_out=True)
    )

    result = _run_direct(DirectDeepSeekRunner(transport), _direct_run(direct_workspace))

    assert result.timed_out is True
    assert result.exit_code is None
    assert "did not answer" in result.error
    assert 0 < transport.requests[0].timeout_seconds <= 300


def test_direct_runner_cancellation_is_prompt(api_key: str, direct_workspace: Path) -> None:
    transport = _BlockingTransport()
    runner = DirectDeepSeekRunner(transport)
    run = _direct_run(direct_workspace)
    results: list[RunnerResult] = []
    thread = threading.Thread(
        target=lambda: results.append(runner.run_task(run, lambda pid: None)), daemon=True
    )

    thread.start()
    assert transport.opened.wait(timeout=30), "the loop never reached its request"
    assert runner.cancel(run.job_id) is True
    thread.join(timeout=10)

    assert not thread.is_alive(), "cancellation must stop the loop promptly"
    assert len(transport.requests) == 1
    assert results[0].exit_code is None
    assert results[0].timed_out is False
    assert results[0].error == "The manager cancelled this job."
    assert runner.cancel(run.job_id) is False, "the runner no longer holds the job"


def test_direct_runner_performs_no_request_when_the_manager_cancelled_first(
    api_key: str, direct_workspace: Path
) -> None:
    """A run whose token is already set does nothing at all: no request, no write.

    This is the state a job cancelled the instant it was submitted reaches, even
    when the cancel arrived before the runner registered anything.
    """

    def never(request: ResponsesRequest) -> ResponsesReply:
        raise AssertionError("a cancelled run must not reach the transport")

    token = CancellationToken("d" * 32)
    token.cancel()

    result = _run_direct(
        DirectDeepSeekRunner(never), _direct_run(direct_workspace, cancel_token=token)
    )

    assert result.exit_code is None
    assert result.error == CANCELLED_MESSAGE
    assert sorted(path.name for path in direct_workspace.rglob("*")) == ["app.py", "src"]


def test_direct_runner_stops_on_the_manager_token_between_turns(
    api_key: str, direct_workspace: Path
) -> None:
    """The manager's token alone stops the loop, without the runner being asked."""
    token = CancellationToken("d" * 32)
    transport = _RecordingTransport(
        _body(_call("read_file", {"path": "src/app.py"})),
        _body(_message("this answer must never be requested")),
    )
    runner = DirectDeepSeekRunner(transport)

    def answer(request: ResponsesRequest) -> ResponsesReply:
        reply = transport(request)
        token.cancel()  # the manager cancels while the loop is working
        return reply

    runner.transport = answer
    result = _run_direct(runner, _direct_run(direct_workspace, cancel_token=token))

    assert len(transport.requests) == 1
    assert result.exit_code is None
    assert result.error == CANCELLED_MESSAGE


def test_direct_runner_refuses_writes_when_the_run_allows_no_paths(
    api_key: str, direct_workspace: Path
) -> None:
    """A run that carried no allowlist can still read, and cannot write."""
    transport = _RecordingTransport(
        _body(_call("write_file", {"path": "src/added.py", "content": "x = 1\n"})),
        _body(_message("Refused: no path was allowed.")),
    )
    run = TaskRun(
        job_id="e" * 32,
        prompt="Add the note the manager asked for",
        workspace=direct_workspace,
        provider="deepseek",
        model="deepseek-chat",
        max_minutes=5,
    )

    result = _run_direct(DirectDeepSeekRunner(transport), run)

    assert json.loads(transport.tool_outputs(1)[0])["error"] == "PathDeniedError"
    assert not (direct_workspace / "src" / "added.py").exists()
    assert result.exit_code == 0


# The key --------------------------------------------------------------------


def test_direct_runner_reads_the_key_when_the_job_runs(
    monkeypatch: pytest.MonkeyPatch, direct_workspace: Path
) -> None:
    monkeypatch.delenv(DEEPSEEK_API_KEY_ENV, raising=False)

    def never(request: ResponsesRequest) -> ResponsesReply:
        raise AssertionError("the runner must not call the API without a key")

    runner = DirectDeepSeekRunner(never)
    result = runner.run_task(_direct_run(direct_workspace), lambda pid: None)

    assert result.exit_code is None
    assert DEEPSEEK_API_KEY_ENV in result.error

    transport = _RecordingTransport(_body(_message("Now it can run.")))
    runner.transport = transport
    monkeypatch.setenv(DEEPSEEK_API_KEY_ENV, FAKE_KEY)

    second = _run_direct(runner, _direct_run(direct_workspace))

    assert second.exit_code == 0, "the key is read per run, not at construction"
    assert len(transport.requests) == 1


def test_direct_runner_redacts_the_key_from_everything_it_reports(
    api_key: str, direct_workspace: Path
) -> None:
    leaky = _body(
        status="failed",
        output=[_message(f"the proxy saw Bearer {api_key}")],
        error={"message": f"rejected Authorization: Bearer {api_key}"},
    )
    transport = _RecordingTransport(leaky)

    result = _run_direct(DirectDeepSeekRunner(transport), _direct_run(direct_workspace))

    assert api_key not in result.error
    assert api_key not in result.output
    assert api_key not in repr(result)
    assert "<redacted>" in result.error
    assert api_key not in json.dumps(transport.requests[0].payload)
    assert api_key not in repr(transport.requests[0])


def test_direct_runner_redacts_a_transport_error_that_echoes_the_key(
    api_key: str, direct_workspace: Path
) -> None:
    transport = _RecordingTransport(
        TransportError(f"connection failed while sending Bearer {api_key}")
    )

    result = _run_direct(DirectDeepSeekRunner(transport), _direct_run(direct_workspace))

    assert api_key not in result.error
    assert "<redacted>" in result.error


# No process, no review, no pid ------------------------------------------------


def test_direct_runner_never_spawns_a_process(
    api_key: str, direct_workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole direct path runs with process creation and sockets disabled."""

    def explode(*args: object, **kwargs: object) -> None:
        raise AssertionError("the direct runner must not spawn a process")

    monkeypatch.setattr(subprocess, "Popen", explode)
    monkeypatch.setattr(subprocess, "run", explode)
    monkeypatch.setattr(os, "system", explode)
    monkeypatch.setattr("mcp_delgado.runners.urllib.request.urlopen", explode)
    transport = _RecordingTransport(
        _body(_call("write_file", {"path": "src/added.py", "content": "value = 1\n"})),
        _body(_message("Created src/added.py.")),
    )

    result = _run_direct(DirectDeepSeekRunner(transport), _direct_run(direct_workspace))

    assert result.exit_code == 0
    assert (direct_workspace / "src" / "added.py").read_text(encoding="utf-8") == "value = 1\n"


def test_direct_runner_refuses_reviews_and_pid_cancellation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def explode(*args: object, **kwargs: object) -> None:
        raise AssertionError("the direct runner has no process to stop")

    monkeypatch.setattr(subprocess, "run", explode)
    runner = DirectDeepSeekRunner()
    run = _review_run(tmp_path)

    with pytest.raises(RunnerCapabilityError):
        runner.review_command(run)
    with pytest.raises(RunnerCapabilityError):
        runner.review_timeout_seconds(run)
    with pytest.raises(RunnerCapabilityError):
        asyncio.run(runner.spawn_review_process(["noop"], tmp_path))
    with pytest.raises(RunnerCapabilityError):
        runner.kill_review_tree(_FakeChild(), force=True)
    with pytest.raises(RunnerCapabilityError):
        runner.cancel_pid(4_242)


# The default transport -------------------------------------------------------


def test_default_transport_posts_the_key_in_the_header_only(api_key: str, monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[tuple[object, float]] = []
    response = _FakeResponse(b'{"status": "completed", "output": []}')

    def fake_urlopen(request: object, timeout: float | None = None) -> _FakeResponse:
        captured.append((request, timeout or 0))
        return response

    monkeypatch.setattr("mcp_delgado.runners.urllib.request.urlopen", fake_urlopen)
    request = ResponsesRequest(
        url=DEEPSEEK_RESPONSES_URL,
        payload={"model": "deepseek-chat", "input": []},
        api_key=api_key,
        timeout_seconds=12.5,
    )

    reply = deepseek_http_transport(request)

    http_request, timeout = captured[0]
    assert http_request.full_url == DEEPSEEK_RESPONSES_URL
    assert http_request.get_method() == "POST"
    headers = {key.casefold(): value for key, value in http_request.header_items()}
    assert headers["authorization"] == f"Bearer {api_key}"
    assert headers["content-type"] == "application/json"
    assert json.loads(http_request.data.decode("utf-8")) == {"model": "deepseek-chat", "input": []}
    assert timeout == 12.5
    assert reply.status == 200
    assert json.loads(reply.body)["status"] == "completed"


def test_default_transport_returns_an_error_status_with_its_body(
    api_key: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_urlopen(request: object, timeout: float | None = None) -> None:
        raise urllib.error.HTTPError(
            DEEPSEEK_RESPONSES_URL, 400, "Bad Request", {}, io.BytesIO(b'{"error": {"message": "bad model"}}')
        )

    monkeypatch.setattr("mcp_delgado.runners.urllib.request.urlopen", fake_urlopen)

    reply = deepseek_http_transport(ResponsesRequest(url=DEEPSEEK_RESPONSES_URL, payload={}, api_key=api_key))

    assert reply.status == 400
    assert "bad model" in reply.body


@pytest.mark.parametrize(
    ("failure", "timed_out"),
    [
        (TimeoutError("timed out"), True),
        (urllib.error.URLError(TimeoutError("timed out")), True),
        (urllib.error.URLError("getaddrinfo failed"), False),
        (OSError("connection reset"), False),
    ],
)
def test_default_transport_maps_failures_to_a_transport_error(
    api_key: str, monkeypatch: pytest.MonkeyPatch, failure: Exception, timed_out: bool
) -> None:
    def fake_urlopen(request: object, timeout: float | None = None) -> None:
        raise failure

    monkeypatch.setattr("mcp_delgado.runners.urllib.request.urlopen", fake_urlopen)

    with pytest.raises(TransportError) as caught:
        deepseek_http_transport(ResponsesRequest(url=DEEPSEEK_RESPONSES_URL, payload={}, api_key=api_key))

    assert caught.value.timed_out is timed_out
    assert api_key not in str(caught.value)


def test_read_bounded_stops_at_the_cap_on_a_closed_stream_and_on_cancellation() -> None:
    request = ResponsesRequest(url="https://api.invalid/responses", payload={}, max_response_bytes=5)

    assert _read_bounded(_FakeResponse(b"0123456789"), request) == "01234"

    class _Closed:
        def read(self, size: int = -1) -> bytes:
            raise ValueError("read of closed file")

    assert _read_bounded(_Closed(), request) == ""

    cancelled = threading.Event()
    cancelled.set()
    stopped = ResponsesRequest(
        url="https://api.invalid/responses", payload={}, cancel_event=cancelled
    )
    assert _read_bounded(_FakeResponse(b"0123456789"), stopped) == ""
