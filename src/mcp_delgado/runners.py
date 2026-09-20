"""Worker runner boundary: how model work is built and supervised.

The manager owns job policy: durable records, workspace claims, path audits,
validation commands, stored patches, repair lineage, cancellation entry points,
and terminal-state decisions. A runner owns only the worker process itself:
which executable runs, which arguments carry the scoped prompt, how the child
leads its own process group, and how its whole tree is stopped.

Keeping that split explicit means one new backend arrives as one new adapter.
The default adapter below drives the CodeWhale CLI, and the manager never names
CodeWhale itself: it builds a scoped request, hands it to the injected runner,
and audits the workspace afterwards.

A second adapter, :class:`DirectDeepSeekRunner`, arrives here the same way. It
talks to the DeepSeek Responses API over HTTP with an injectable transport, runs
a bounded tool loop over :class:`~mcp_delgado.workspace_tools.WorkspaceTools`,
and owns no shell and no subprocess at all. It is selected only by an explicit
``MODEL_WORKER_RUNNER=deepseek-api``, so the default stays CodeWhale.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import subprocess
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Any, Callable, Optional, Protocol, runtime_checkable

from .workspace_tools import WorkspaceTools, tool_definitions

CODEWHALE_EXECUTABLE_ENV = "MODEL_WORKER_CODEWHALE"

# How a runner holds its worker. A runner declares this, and the manager stores it
# with the job, because a supervisor that stops a job is not necessarily the
# runner that process was configured with.
#
# ``child``: the worker is a separate process tree, so the pid the runner reports
# is a worker pid this process may signal. ``in-process``: the worker is a thread
# inside the manager process, so the pid the runner reports names the manager
# itself and must never be signalled or stored as a killable worker pid.
WORKER_OWNERSHIP_CHILD = "child"
WORKER_OWNERSHIP_IN_PROCESS = "in-process"
WORKER_OWNERSHIPS = frozenset({WORKER_OWNERSHIP_CHILD, WORKER_OWNERSHIP_IN_PROCESS})


def worker_ownership(runner: object) -> str:
    """Report how ``runner`` holds its worker, defaulting to a separate child.

    A runner that predates this declaration keeps the CodeWhale reading, because
    only an explicit ``in-process`` declaration, which
    :class:`DirectDeepSeekRunner` makes, stops a reported pid from being stored
    as a killable worker pid. An unknown value is read the same way.
    """
    declared = getattr(runner, "worker_ownership", WORKER_OWNERSHIP_CHILD)
    return declared if declared in WORKER_OWNERSHIPS else WORKER_OWNERSHIP_CHILD


class CancellationToken:
    """One job's cancellation flag, owned by the manager and read by a runner.

    The manager creates a token for every implementation job *before* its job
    thread starts, and sets it in the same locked step that publishes the
    terminal CANCELLED record. A runner consults the token before it does any
    work: before its first request and before every later turn. That ordering is
    what a runner-owned flag cannot provide -- a job cancelled the instant it was
    submitted never reaches a transport, because the manager can set the token
    before the runner has registered anything at all.

    The mechanism is one ``threading.Event``, whose ``set`` is atomic and
    idempotent, so the token is safe to share between the manager thread, the job
    thread, and a transport's own reader.
    """

    __slots__ = ("job_id", "event")

    def __init__(self, job_id: str) -> None:
        self.job_id = job_id
        self.event = threading.Event()

    @property
    def cancelled(self) -> bool:
        return self.event.is_set()

    def cancel(self) -> None:
        self.event.set()

    def __repr__(self) -> str:
        return f"CancellationToken(job_id={self.job_id!r}, cancelled={self.cancelled})"

# Runner selection. The default is the CodeWhale adapter; the direct API backend
# is opt-in, so an existing installation never changes behavior by upgrading.
RUNNER_ENV = "MODEL_WORKER_RUNNER"
CODEWHALE_RUNNER_NAME = "codewhale"
DIRECT_RUNNER_NAME = "deepseek-api"
DEEPSEEK_API_KEY_ENV = "DEEPSEEK_API_KEY"
DEEPSEEK_RESPONSES_URL = "https://api.deepseek.com/responses"

# Every bound the direct loop enforces. A construction parameter may raise one up
# to its ceiling, never past it, and the model cannot change any of them: one
# direct job stays finite in model turns, tool calls, tool output, response
# bytes, reported text, and wall-clock time.
DIRECT_MAX_STEPS = 12
DIRECT_HARD_MAX_STEPS = 64
DIRECT_MAX_TOOL_CALLS = 40
DIRECT_HARD_MAX_TOOL_CALLS = 200
DIRECT_MAX_TOOL_OUTPUT_BYTES = 400_000
DIRECT_HARD_MAX_TOOL_OUTPUT_BYTES = 4_000_000
DIRECT_MAX_RESPONSE_BYTES = 1_000_000
DIRECT_HARD_MAX_RESPONSE_BYTES = 8_000_000
DIRECT_MAX_TEXT_BYTES = 200_000
DIRECT_REQUEST_TIMEOUT_SECONDS = 300.0
DIRECT_HARD_REQUEST_TIMEOUT_SECONDS = 3_600.0
READ_CHUNK_BYTES = 65_536
COMPLETED_STATUS = "completed"
NON_TERMINAL_STATUSES = frozenset({"in_progress", "queued", "searching", "running"})
CANCELLED_STATUSES = frozenset({"cancelled", "canceled"})
TOKEN_COUNTERS = ("input_tokens", "output_tokens", "total_tokens")
# One message for every job the operator cancelled: the manager publishes the
# same text whether the decision came from ``cancel`` or from the job thread that
# found its token already set.
CANCELLED_MESSAGE = "The manager cancelled this job."


class ReviewTimeoutError(RuntimeError):
    """A review exceeded its max_minutes budget and its process tree was stopped."""

    def __init__(self, message: str, output: str = "", diagnostics_tail: str = "") -> None:
        super().__init__(message)
        self.output = output
        self.diagnostics_tail = diagnostics_tail


class RunnerSelectionError(ValueError):
    """Raised for a runner selector that names no known backend."""


class RunnerCapabilityError(RuntimeError):
    """Raised when a runner is asked for work its backend does not implement."""


class TransportError(RuntimeError):
    """An HTTP request failed without a response the caller can classify.

    ``timed_out`` separates a request that ran out of time from a connection that
    failed outright, so the loop can report the right stop reason.
    """

    def __init__(self, message: str, timed_out: bool = False) -> None:
        super().__init__(message)
        self.timed_out = timed_out


@dataclass(frozen=True)
class TaskRun:
    """One implementation run, already scoped by the manager.

    The prompt is the manager's instruction text: task, acceptance criteria,
    allowed paths, and required checks. A runner carries it to its worker as-is.
    ``allowed_paths`` are the manager-normalized patterns from the job record, so
    a runner that edits the workspace in-process can apply the same rule the
    manager's post-job audit applies.

    ``cancel_token`` is the manager's cancellation flag for this job. It exists
    before the job thread starts, so a token that is already set means the job
    was cancelled before its worker ran: the runner performs no request and no
    write, and reports the cancellation. A runner driven directly, without a
    manager, may leave it unset.
    """

    job_id: str
    prompt: str
    workspace: Path
    provider: str
    model: str
    max_minutes: int
    allowed_paths: tuple[str, ...] = ()
    cancel_token: Optional[CancellationToken] = None


@dataclass(frozen=True)
class ReviewRun:
    """One read-only review request, already scoped by the manager."""

    request: str
    workspace: Path
    provider: str
    model: str
    max_minutes: int


@dataclass(frozen=True)
class RunnerResult:
    """What a runner reports before the manager audits the workspace.

    The runner reports what the worker process did. It never decides job state:
    ``timed_out`` and ``exit_code`` are evidence, and the manager turns them into
    a terminal state together with its own scope audit. ``usage`` carries
    non-secret counters only, and the manager stores nothing else from it.
    """

    output: str = ""
    exit_code: Optional[int] = None
    error: str = ""
    timed_out: bool = False
    usage: dict[str, int] = field(default_factory=dict)


@runtime_checkable
class WorkerRunner(Protocol):
    """The boundary between job policy and worker process control.

    ``run_task`` blocks until the worker stops or exceeds its budget, so the
    manager can run it on the job thread. ``on_start`` hands the manager the live
    owner pid while the job is live, because a second MCP server process may need
    to cancel a job it does not own in memory. ``worker_ownership`` says how that
    pid must be read: only a ``child`` worker pid may be stored as a killable pid
    and signalled, while an ``in-process`` worker reports the manager itself and
    is recorded as ownership metadata instead.
    """

    name: str
    label: str
    worker_ownership: str

    def run_task(self, run: TaskRun, on_start: Callable[[int], None]) -> RunnerResult:
        ...

    def cancel(self, job_id: str) -> bool:
        """Stop the tracked process for a job.

        Returns ``True`` when this runner held a handle for the job, including a
        handle whose process already exited, and ``False`` when the job belongs
        to another process.
        """
        ...

    def cancel_pid(self, pid: int) -> None:
        """Stop a job process that this runner process does not hold."""
        ...

    def review_command(self, run: ReviewRun) -> list[str]:
        ...

    def review_timeout_seconds(self, run: ReviewRun) -> float:
        ...

    async def spawn_review_process(
        self, command: list[str], workspace: Path
    ) -> asyncio.subprocess.Process:
        """Start the review child so its whole tree can be stopped later."""
        ...

    def kill_review_tree(self, process: asyncio.subprocess.Process, force: bool) -> None:
        """Stop the review child and every process it started."""
        ...


def codewhale_executable() -> str:
    configured = os.environ.get(CODEWHALE_EXECUTABLE_ENV)
    if configured:
        return configured
    return shutil.which("codewhale.cmd" if os.name == "nt" else "codewhale") or "codewhale"


class CodeWhaleRunner:
    """Run model work through the CodeWhale CLI.

    Every CodeWhale-specific detail lives here: executable discovery, argv
    construction for implementation and review runs, process-group creation, and
    whole-tree termination on Windows and POSIX. The manager keeps the prompt,
    the scope, and the decision about what a finished run means.
    """

    name = "codewhale"
    label = "CodeWhale"
    # Every implementation run is a separate child process tree, so the pid
    # reported by ``on_start`` is a worker pid a manager process may stop.
    worker_ownership = WORKER_OWNERSHIP_CHILD

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._processes: dict[str, subprocess.Popen[str]] = {}

    # Implementation jobs ---------------------------------------------------

    def task_command(self, run: TaskRun) -> list[str]:
        return [
            codewhale_executable(),
            "--provider",
            run.provider,
            "--model",
            run.model,
            "--approval-policy",
            "never",
            "--sandbox-mode",
            "workspace-write",
            "--fresh",
            "-C",
            str(run.workspace),
            "exec",
            "--auto",
            "--json",
            run.prompt,
        ]

    def run_task(self, run: TaskRun, on_start: Callable[[int], None]) -> RunnerResult:
        """Run one implementation job to completion or to its budget.

        The child is tracked by job id so ``cancel`` can stop it from another
        thread, and its pid reaches the manager before it is awaited so the job
        record can be cancelled from another process as well. A run whose token is
        already set reports the cancellation without starting a child at all.
        """
        if run.cancel_token is not None and run.cancel_token.cancelled:
            return RunnerResult(error=CANCELLED_MESSAGE)
        process = subprocess.Popen(
            self.task_command(run),
            cwd=str(run.workspace),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=os.environ.copy(),
        )
        with self._lock:
            self._processes[run.job_id] = process
        try:
            on_start(process.pid)
            try:
                output, _ = process.communicate(timeout=run.max_minutes * 60)
            except subprocess.TimeoutExpired:
                # The tree is stopped before the pipe is drained, so the output
                # already produced is still reported with the timeout.
                self._terminate_process(process)
                output, _ = process.communicate()
                return RunnerResult(
                    output=output or "",
                    exit_code=process.returncode,
                    error=f"The worker exceeded {run.max_minutes} minutes.",
                    timed_out=True,
                )
            return RunnerResult(output=output or "", exit_code=process.returncode)
        finally:
            with self._lock:
                self._processes.pop(run.job_id, None)

    def cancel(self, job_id: str) -> bool:
        with self._lock:
            process = self._processes.get(job_id)
        if process is None:
            return False
        if process.poll() is None:
            self._terminate_process(process)
        return True

    def cancel_pid(self, pid: int) -> None:
        """Stop a worker process recorded by another manager process.

        A pid that names the calling process is refused: a recorded worker pid is
        never a manager pid, so stopping it would end the process that owns the
        job instead of the job.
        """
        if pid == os.getpid():
            raise RunnerCapabilityError(
                f"Refusing to stop pid {pid}: it is this process, not a worker. "
                "A recorded worker pid never names the manager that owns the job."
            )
        if os.name == "nt":
            result = subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=15,
            )
            if result.returncode != 0:
                raise RuntimeError(f"Could not stop process {pid}.")
            return
        os.kill(pid, signal.SIGTERM)

    @staticmethod
    def _terminate_process(process: subprocess.Popen[str]) -> None:
        """Stop a worker and every process it started."""
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=15,
            )
            return
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()

    # Reviews ---------------------------------------------------------------

    def review_command(self, run: ReviewRun) -> list[str]:
        return [
            codewhale_executable(),
            "--provider",
            run.provider,
            "--model",
            run.model,
            "--approval-policy",
            "never",
            "--sandbox-mode",
            "read-only",
            "--fresh",
            "-C",
            str(run.workspace),
            "exec",
            "--auto",
            "--output-format",
            "text",
            run.request,
        ]

    @staticmethod
    def review_timeout_seconds(run: ReviewRun) -> float:
        return run.max_minutes * 60

    @staticmethod
    async def spawn_review_process(
        command: list[str], workspace: Path
    ) -> asyncio.subprocess.Process:
        """Start the review child as its own process group so its tree can be stopped."""
        options: dict = {
            "cwd": str(workspace),
            "stdin": asyncio.subprocess.DEVNULL,
            "stdout": asyncio.subprocess.PIPE,
            "stderr": asyncio.subprocess.PIPE,
            "env": os.environ.copy(),
        }
        if os.name == "nt":
            options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            options["start_new_session"] = True
        return await asyncio.create_subprocess_exec(*command, **options)

    @staticmethod
    def kill_review_tree(process: asyncio.subprocess.Process, force: bool) -> None:
        """Stop the review child and every process it started.

        Windows uses ``taskkill /T``, the same convention as job cancellation, and
        that call is already forceful. POSIX signals the child's process group,
        which exists because the review child is started as a session leader;
        ``force`` selects SIGKILL over SIGTERM.
        """
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=15,
            )
            return
        signal_number = signal.SIGKILL if force else signal.SIGTERM
        try:
            os.killpg(os.getpgid(process.pid), signal_number)
        except ProcessLookupError:
            return
        except PermissionError:
            if force:
                process.kill()
            else:
                process.terminate()


# Direct DeepSeek Responses API backend --------------------------------------
#
# The direct backend speaks the documented Responses contract over HTTP with an
# injectable transport, so a test never opens a socket and a job never shells
# out. The key is read from the environment when a job starts, travels only as
# an Authorization header, and is redacted out of everything this module returns.


def _no_closer(_close: Callable[[], None]) -> None:
    """Default ``on_open`` for a transport with nothing to close."""


@dataclass(frozen=True)
class ResponsesRequest:
    """One bounded POST to the Responses API.

    ``api_key`` is excluded from ``repr`` and never becomes part of ``payload``.
    ``cancel_event`` and ``on_open`` are the hooks a transport uses to stop a
    request that is already in flight: the reader stops at the event, and
    ``on_open`` hands back a closer for a response that is still open. Both need
    an HTTP response to exist. A request that has not produced one yet -- still
    connecting, or waiting for the first response bytes -- cannot be interrupted
    by this module, so it ends at ``timeout_seconds`` instead, which is the
    lesser of the job's remaining budget and the configured request timeout. A
    transport that never blocks may ignore both hooks, which keeps a fake
    transport a one-line function.
    """

    url: str
    payload: dict[str, Any]
    api_key: str = field(default="", repr=False)
    timeout_seconds: float = DIRECT_REQUEST_TIMEOUT_SECONDS
    max_response_bytes: int = DIRECT_MAX_RESPONSE_BYTES
    cancel_event: threading.Event = field(default_factory=threading.Event, repr=False, compare=False)
    on_open: Callable[[Callable[[], None]], None] = field(default=_no_closer, repr=False, compare=False)


@dataclass(frozen=True)
class ResponsesReply:
    """One HTTP answer: its status, its bounded body, or a failure message."""

    status: int = 0
    body: str = ""
    error: str = ""


@runtime_checkable
class ResponsesTransport(Protocol):
    """The one HTTP operation the direct runner needs.

    A transport returns a :class:`ResponsesReply` for anything the service
    answered, including an error status, and raises :class:`TransportError` when
    no response exists. It must never raise an error whose text carries the key.
    """

    def __call__(self, request: ResponsesRequest) -> ResponsesReply:
        ...


def _read_bounded(stream: Any, request: ResponsesRequest) -> str:
    """Read a response in chunks, stopping at the byte cap or on cancellation.

    A cancelled request closes its response under this reader, which surfaces as
    an I/O error; the partial body is still returned and the loop classifies the
    stop from the cancel event or the deadline.
    """
    chunks: list[bytes] = []
    remaining = max(0, request.max_response_bytes)
    try:
        while remaining > 0 and not request.cancel_event.is_set():
            chunk = stream.read(min(READ_CHUNK_BYTES, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
    except (OSError, ValueError):
        pass
    return b"".join(chunks).decode("utf-8", errors="replace")


def deepseek_http_transport(request: ResponsesRequest) -> ResponsesReply:
    """POST one Responses request over HTTPS with the documented contract.

    The body is JSON and the key travels only in the ``Authorization`` header, so
    it can never appear in the payload, in the workspace, or in a durable record.
    This is the only place in the runner that opens a socket; an injected
    callable with the same signature replaces it completely.
    """
    body = json.dumps(request.payload, ensure_ascii=False).encode("utf-8")
    http_request = urllib.request.Request(
        request.url,
        data=body,
        headers={
            "Authorization": f"Bearer {request.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    timeout = max(1.0, request.timeout_seconds)
    try:
        with urllib.request.urlopen(http_request, timeout=timeout) as response:
            request.on_open(response.close)
            status = int(getattr(response, "status", 0) or 200)
            return ResponsesReply(status=status, body=_read_bounded(response, request))
    except urllib.error.HTTPError as exc:
        # An error status still carries a body the loop turns into a clear stop.
        return ResponsesReply(status=int(exc.code), body=_read_bounded(exc, request))
    except TimeoutError:
        raise TransportError(
            f"the Responses API did not answer within {timeout:.0f} seconds", timed_out=True
        ) from None
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, TimeoutError):
            raise TransportError(
                f"the Responses API did not answer within {timeout:.0f} seconds", timed_out=True
            ) from None
        raise TransportError(f"the Responses API request failed: {exc.reason}") from None
    except OSError as exc:
        raise TransportError(f"the Responses API request failed: {type(exc).__name__}") from None


@dataclass(frozen=True)
class _ToolCall:
    """One model tool call, still carrying its raw JSON argument text."""

    call_id: str
    name: str
    arguments: str


@dataclass(frozen=True)
class _ParsedReply:
    """What one answer yielded: text, tool calls, counters, or a stop reason."""

    error: str = ""
    texts: tuple[str, ...] = ()
    calls: tuple[_ToolCall, ...] = ()
    usage: dict[str, int] = field(default_factory=dict)


def _error_text(value: object) -> str:
    """Read the message out of the API's ``error`` object, if it has one."""
    if isinstance(value, dict):
        message = value.get("message") or value.get("code")
        if isinstance(message, str) and message.strip():
            return " ".join(message.split())[:400]
    if isinstance(value, str) and value.strip():
        return " ".join(value.split())[:400]
    return "the service reported a failure without a message"


def _incomplete_text(document: dict[str, Any]) -> str:
    details = document.get("incomplete_details")
    if isinstance(details, dict):
        reason = details.get("reason")
        if isinstance(reason, str) and reason.strip():
            return reason.strip()[:200]
    return "the answer stopped before it finished"


def _parse_reply(reply: ResponsesReply) -> _ParsedReply:
    """Turn one HTTP answer into text, tool calls, counters, or a stop reason.

    Everything the model can get wrong is a stop, never a crash: an error status,
    a body that is not JSON, a missing or non-terminal status, an item that is
    not an object, and a tool call without a name or a call id.
    """
    if reply.error:
        return _ParsedReply(error=reply.error)
    if not 200 <= reply.status < 300:
        tail = " ".join(reply.body.split())[:400]
        detail = f": {tail}" if tail else ""
        return _ParsedReply(error=f"the Responses API returned HTTP {reply.status}{detail}")
    try:
        document = json.loads(reply.body)
    except ValueError:
        return _ParsedReply(error="the Responses API returned a body that is not JSON")
    if not isinstance(document, dict):
        return _ParsedReply(error="the Responses API returned a JSON value that is not an object")

    status = document.get("status")
    if status == "failed":
        return _ParsedReply(error=f"the model failed: {_error_text(document.get('error'))}")
    if status == "incomplete":
        return _ParsedReply(error=f"the model returned an incomplete response: {_incomplete_text(document)}")
    if isinstance(status, str) and status.casefold() in CANCELLED_STATUSES:
        return _ParsedReply(error="the model reported that the response was cancelled")
    if isinstance(status, str) and status.casefold() in NON_TERMINAL_STATUSES:
        return _ParsedReply(error=f"the response was still {status!r} when the request returned")
    if status != COMPLETED_STATUS:
        return _ParsedReply(error=f"the response status {status!r} is not a finished answer")

    output = document.get("output")
    if output is None:
        output = []
    if not isinstance(output, list):
        return _ParsedReply(error="the response output is not a list")

    texts: list[str] = []
    calls: list[_ToolCall] = []
    for index, item in enumerate(output):
        if not isinstance(item, dict):
            return _ParsedReply(error=f"output item {index} is not an object")
        kind = item.get("type")
        if kind == "function_call":
            name = item.get("name")
            call_id = item.get("call_id") or item.get("id")
            if not isinstance(name, str) or not name.strip():
                return _ParsedReply(error=f"output item {index} is a function call without a name")
            if not isinstance(call_id, str) or not call_id.strip():
                return _ParsedReply(error=f"output item {index} is a function call without a call id")
            arguments = item.get("arguments", "")
            if isinstance(arguments, dict):
                arguments = json.dumps(arguments, ensure_ascii=False)
            if not isinstance(arguments, str):
                return _ParsedReply(error=f"output item {index} carries function arguments that are not JSON text")
            calls.append(_ToolCall(call_id=call_id, name=name, arguments=arguments))
        elif kind == "message":
            content = item.get("content")
            if isinstance(content, list):
                for part in content:
                    if not isinstance(part, dict):
                        continue
                    if part.get("type") in {"output_text", "text"} and isinstance(part.get("text"), str):
                        texts.append(part["text"])
    if not texts and isinstance(document.get("output_text"), str) and document["output_text"].strip():
        texts.append(document["output_text"])

    return _ParsedReply(texts=tuple(texts), calls=tuple(calls), usage=_reply_usage(document))


def _reply_usage(document: dict[str, Any]) -> dict[str, int]:
    """Keep the integer token counters the service reported, and nothing else."""
    usage = document.get("usage")
    counters: dict[str, int] = {}
    if isinstance(usage, dict):
        for key in TOKEN_COUNTERS:
            value = usage.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                counters[key] = value
    return counters


def _usage_counters(usage: dict[str, int], steps: int, tool_calls: int) -> dict[str, int]:
    """The job's non-secret counters: its own steps and calls, plus token totals."""
    counters = {"steps": steps, "tool_calls": tool_calls}
    counters.update(usage)
    return counters


def _sum_usage(total: dict[str, int], addition: dict[str, int]) -> dict[str, int]:
    merged = dict(total)
    for key, value in addition.items():
        merged[key] = merged.get(key, 0) + value
    return merged


def _redact(text: str, secret: str) -> str:
    """Remove a credential from any text this module is about to hand over.

    Nothing here should ever carry the key, and this is the backstop that keeps a
    chatty service, a proxy, or a transport bug from turning that should into a
    durable leak.
    """
    if secret and secret in text:
        return text.replace(secret, "<redacted>")
    return text


def _bounded_int(value: object, default: int, ceiling: int, name: str) -> int:
    """Validate one construction cap, refusing zero, negatives, and overshoot."""
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a positive integer")
    if value > ceiling:
        raise ValueError(f"{name} must not exceed {ceiling}")
    return value


_DIRECT_REVIEW_REFUSAL = (
    f"The {DIRECT_RUNNER_NAME} runner implements bounded implementation jobs only; "
    f"run reviews with the {CODEWHALE_RUNNER_NAME} runner."
)

_DIRECT_INSTRUCTIONS = """You are an implementation worker for a repository the manager owns.

You have exactly five file tools: list_files, search_text, read_file, replace_text, and write_file.
They work on repository-relative paths inside the checkout. There is no shell and no command tool.
The prompt lists the manager's required validation commands: the manager runs them itself after you
stop, so never try to run git, tests, or any other program.

Work in small steps. Inspect the files that matter, make the smallest change that satisfies the task,
then read your change back. Write only inside the allowed paths you were given. When the task is done,
reply with a short summary and no more tool calls: what changed, which files, what you read to check it,
and anything unresolved. If the task cannot be done inside the allowed paths, say why instead of
editing something unrelated."""


class DirectDeepSeekRunner:
    """Run one bounded tool loop against the DeepSeek Responses API.

    The manager still owns everything around the loop: the workspace lock, the
    before/after audit, the stored patch, the required validation commands, and
    the terminal job state. This runner owns one HTTP conversation, the five
    workspace tools the model may call inside it, its own budgets, and its
    cancellation path. It starts no shell and no subprocess at all.

    The key is read from ``DEEPSEEK_API_KEY`` when a job starts, is sent only as
    an ``Authorization`` header, is never stored on the instance, and is redacted
    out of the output and the error this runner reports.

    Cancellation is bounded honestly. The manager's token is read before the
    first request and before every later turn, so a job that was cancelled before
    its loop began performs no request at all, and a loop between turns stops
    without sending another one. A request already in flight stops when its
    response is closed, and a request that has not yet produced response headers
    is bounded by the request timeout instead.
    """

    name = DIRECT_RUNNER_NAME
    label = "DeepSeek Responses API"
    # The loop runs on a thread inside the manager process, so the live owner it
    # reports is that manager: there is no worker pid to store or to signal.
    worker_ownership = WORKER_OWNERSHIP_IN_PROCESS

    def __init__(
        self,
        transport: Optional[ResponsesTransport] = None,
        *,
        url: str = DEEPSEEK_RESPONSES_URL,
        max_steps: int = DIRECT_MAX_STEPS,
        max_tool_calls: int = DIRECT_MAX_TOOL_CALLS,
        max_tool_output_bytes: int = DIRECT_MAX_TOOL_OUTPUT_BYTES,
        max_response_bytes: int = DIRECT_MAX_RESPONSE_BYTES,
        max_output_tokens: int = 8_192,
        max_text_bytes: int = DIRECT_MAX_TEXT_BYTES,
        request_timeout_seconds: float = DIRECT_REQUEST_TIMEOUT_SECONDS,
        time_source: Callable[[], float] = time.monotonic,
    ) -> None:
        self.transport: ResponsesTransport = transport or deepseek_http_transport
        self.url = url
        self.max_steps = _bounded_int(max_steps, DIRECT_MAX_STEPS, DIRECT_HARD_MAX_STEPS, "max_steps")
        self.max_tool_calls = _bounded_int(
            max_tool_calls, DIRECT_MAX_TOOL_CALLS, DIRECT_HARD_MAX_TOOL_CALLS, "max_tool_calls"
        )
        self.max_tool_output_bytes = _bounded_int(
            max_tool_output_bytes,
            DIRECT_MAX_TOOL_OUTPUT_BYTES,
            DIRECT_HARD_MAX_TOOL_OUTPUT_BYTES,
            "max_tool_output_bytes",
        )
        self.max_response_bytes = _bounded_int(
            max_response_bytes,
            DIRECT_MAX_RESPONSE_BYTES,
            DIRECT_HARD_MAX_RESPONSE_BYTES,
            "max_response_bytes",
        )
        self.max_output_tokens = _bounded_int(max_output_tokens, 8_192, 32_768, "max_output_tokens")
        self.max_text_bytes = _bounded_int(max_text_bytes, DIRECT_MAX_TEXT_BYTES, DIRECT_MAX_TEXT_BYTES, "max_text_bytes")
        if not isinstance(request_timeout_seconds, (int, float)) or isinstance(request_timeout_seconds, bool):
            raise ValueError("request_timeout_seconds must be a positive number")
        if not 0 < float(request_timeout_seconds) <= DIRECT_HARD_REQUEST_TIMEOUT_SECONDS:
            raise ValueError(
                f"request_timeout_seconds must be above zero and at most {DIRECT_HARD_REQUEST_TIMEOUT_SECONDS:.0f}"
            )
        self.request_timeout_seconds = float(request_timeout_seconds)
        self.time_source = time_source
        self._lock = threading.RLock()
        self._cancelled: dict[str, threading.Event] = {}
        self._closers: dict[str, Callable[[], None]] = {}

    def __repr__(self) -> str:
        return (
            f"DirectDeepSeekRunner(url={self.url!r}, max_steps={self.max_steps}, "
            f"max_tool_calls={self.max_tool_calls})"
        )

    # Implementation jobs ---------------------------------------------------

    def run_task(self, run: TaskRun, on_start: Callable[[int], None]) -> RunnerResult:
        """Run one bounded conversation and report what it did.

        The loop never decides job state, never commits, and never runs a
        command: it returns evidence, and the manager turns that into a terminal
        state together with its own scope audit. The key is read here, at
        execution time, and lives only in this call's local scope.

        The run's cancellation token is the manager's, and it is read before any
        request and before every turn, so a job the manager cancelled before this
        call performs no request and no write at all.
        """
        token = run.cancel_token or CancellationToken(run.job_id)
        api_key = os.environ.get(DEEPSEEK_API_KEY_ENV, "").strip()
        if not api_key:
            return RunnerResult(
                error=(
                    f"{DEEPSEEK_API_KEY_ENV} is not set for this process; the {self.name} runner "
                    "reads the worker key from that variable when a job starts."
                )
            )
        try:
            tools = WorkspaceTools(run.workspace, list(run.allowed_paths))
        except ValueError as exc:
            return RunnerResult(error=f"{type(exc).__name__}: {exc}")

        with self._lock:
            # The job's flag is the manager's token when there is one, so the
            # manager's cancel and this runner's cancel are the same signal.
            self._cancelled[run.job_id] = token.event
        try:
            # The loop runs on this process's own thread, so the live owner is the
            # manager process itself. The in-process ownership above is what tells
            # the manager to record that as ownership metadata rather than as a
            # killable worker pid.
            on_start(os.getpid())
            result = self._converse(run, tools, api_key, token)
        except Exception as exc:
            result = RunnerResult(error=f"{type(exc).__name__}: {_redact(str(exc), api_key)}")
        finally:
            with self._lock:
                self._cancelled.pop(run.job_id, None)
                self._closers.pop(run.job_id, None)
        return RunnerResult(
            output=_redact(result.output, api_key)[-self.max_text_bytes:],
            exit_code=result.exit_code,
            error=_redact(result.error, api_key),
            timed_out=result.timed_out,
            usage=dict(result.usage),
        )

    def cancel(self, job_id: str) -> bool:
        """Stop a direct job in this process and close its live request.

        Setting the job's flag is the whole stop, and it is the same flag the
        manager's token carries. Closing the live response ends a request that is
        already waiting for its body; a request still waiting for response
        headers ends at its own timeout instead.

        Returns ``False`` when this process does not hold the job, which is the
        protocol's way of saying the job belongs to another process.
        """
        with self._lock:
            event = self._cancelled.get(job_id)
            closer = self._closers.pop(job_id, None)
        if event is None:
            return False
        event.set()
        if closer is not None:
            try:
                closer()
            except Exception:
                # Closing an already-closed response is not a failure.
                pass
        return True

    def cancel_pid(self, pid: int) -> None:
        """Refuse: a direct job has no worker process to stop.

        The loop runs on a thread inside the manager process that started it, so
        stopping a pid would mean stopping that manager. The refusal is explicit
        instead of a silent no-op, and it never touches a process.
        """
        raise RunnerCapabilityError(
            f"The {self.name} runner has no worker process for pid {pid}; a direct job is "
            "cancelled through cancel(job_id) in the process that started it."
        )

    # Reviews ---------------------------------------------------------------

    def review_command(self, run: ReviewRun) -> list[str]:
        raise RunnerCapabilityError(_DIRECT_REVIEW_REFUSAL)

    def review_timeout_seconds(self, run: ReviewRun) -> float:
        raise RunnerCapabilityError(_DIRECT_REVIEW_REFUSAL)

    async def spawn_review_process(
        self, command: list[str], workspace: Path
    ) -> asyncio.subprocess.Process:
        raise RunnerCapabilityError(_DIRECT_REVIEW_REFUSAL)

    def kill_review_tree(self, process: asyncio.subprocess.Process, force: bool) -> None:
        raise RunnerCapabilityError(_DIRECT_REVIEW_REFUSAL)

    # The bounded loop ------------------------------------------------------

    def _converse(
        self,
        run: TaskRun,
        tools: WorkspaceTools,
        api_key: str,
        token: CancellationToken,
    ) -> RunnerResult:
        """Drive the model through at most ``max_steps`` bounded turns."""
        deadline = self.time_source() + run.max_minutes * 60
        instructions = self._instructions()
        tool_table = tool_definitions()
        items: list[dict[str, Any]] = [
            {"role": "user", "content": [{"type": "input_text", "text": run.prompt}]}
        ]
        trace: list[str] = []
        texts: list[str] = []
        token_usage: dict[str, int] = {}
        tool_calls = 0
        tool_output_bytes = 0
        steps = 0

        while steps < self.max_steps:
            steps += 1
            if token.cancelled:
                return self._stopped(
                    CANCELLED_MESSAGE, trace, texts, token_usage, steps, tool_calls
                )
            remaining = deadline - self.time_source()
            if remaining <= 0:
                return self._exceeded(run, trace, texts, token_usage, steps, tool_calls)

            request = ResponsesRequest(
                url=self.url,
                payload=self._payload(run, instructions, tool_table, items, self.max_output_tokens),
                api_key=api_key,
                timeout_seconds=max(1.0, min(remaining, self.request_timeout_seconds)),
                max_response_bytes=self.max_response_bytes,
                cancel_event=token.event,
                on_open=partial(self._register_closer, run.job_id),
            )
            try:
                reply = self.transport(request)
            except TransportError as exc:
                if token.cancelled:
                    return self._stopped(
                        CANCELLED_MESSAGE, trace, texts, token_usage, steps, tool_calls
                    )
                if self.time_source() >= deadline:
                    return self._exceeded(run, trace, texts, token_usage, steps, tool_calls)
                # A bounded request that ran out of time before the job's own
                # budget did: report the request's reason, not the job's.
                return self._stopped(
                    str(exc), trace, texts, token_usage, steps, tool_calls, timed_out=exc.timed_out
                )
            except Exception as exc:
                # A transport bug must fail the job, not crash the job thread.
                return self._stopped(
                    f"the {self.label} transport failed: {type(exc).__name__}",
                    trace, texts, token_usage, steps, tool_calls,
                )
            finally:
                self._clear_closer(run.job_id)

            if token.cancelled:
                return self._stopped(
                    CANCELLED_MESSAGE, trace, texts, token_usage, steps, tool_calls
                )
            parsed = _parse_reply(reply)
            token_usage = _sum_usage(token_usage, parsed.usage)
            if parsed.error:
                return self._stopped(parsed.error, trace, texts, token_usage, steps, tool_calls)
            texts.extend(parsed.texts)
            if not parsed.calls:
                return RunnerResult(
                    output=self._compose(trace, texts),
                    exit_code=0,
                    usage=_usage_counters(token_usage, steps, tool_calls),
                )

            for call in parsed.calls:
                if tool_calls >= self.max_tool_calls:
                    return self._stopped(
                        f"the worker exceeded its {self.max_tool_calls} tool-call budget",
                        trace, texts, token_usage, steps, tool_calls,
                    )
                tool_calls += 1
                result = tools.call_json(call.name, call.arguments)
                serialized = json.dumps(result, ensure_ascii=False, default=str)
                tool_output_bytes += len(serialized.encode("utf-8"))
                if tool_output_bytes > self.max_tool_output_bytes:
                    return self._stopped(
                        f"the worker exceeded its {self.max_tool_output_bytes}-byte tool-output budget",
                        trace, texts, token_usage, steps, tool_calls,
                    )
                trace.append(f"{call.name}: {result.get('error', 'ok')}")
                items.append(
                    {
                        "type": "function_call",
                        "call_id": call.call_id,
                        "name": call.name,
                        "arguments": call.arguments,
                    }
                )
                items.append({"type": "function_call_output", "call_id": call.call_id, "output": serialized})

        return self._stopped(
            f"the worker stopped after {self.max_steps} model turns without a final answer",
            trace, texts, token_usage, steps, tool_calls,
        )

    def _instructions(self) -> str:
        return (
            f"{_DIRECT_INSTRUCTIONS}\n\nThis job allows at most {self.max_steps} model turns "
            f"and {self.max_tool_calls} tool calls."
        )

    @staticmethod
    def _payload(
        run: TaskRun,
        instructions: str,
        tool_table: tuple[dict[str, Any], ...],
        items: list[dict[str, Any]],
        max_output_tokens: int,
    ) -> dict[str, Any]:
        """Build one Responses payload from the conversation so far.

        The conversation is sent whole on every turn (``store`` is false), so a
        retry or a resumed job never depends on server-side state, and the tool
        table is always the same five operations.
        """
        return {
            "model": run.model,
            "instructions": instructions,
            "input": items,
            "tools": list(tool_table),
            "tool_choice": "auto",
            "parallel_tool_calls": True,
            "max_output_tokens": max_output_tokens,
            "store": False,
        }

    def _register_closer(self, job_id: str, closer: Callable[[], None]) -> None:
        """Let ``cancel`` close a response that is still open.

        A closer only exists once a response does, so a request that has not
        produced response headers is bounded by its own timeout rather than by
        this hook.
        """
        with self._lock:
            event = self._cancelled.get(job_id)
            if event is None or event.is_set():
                return
            self._closers[job_id] = closer

    def _clear_closer(self, job_id: str) -> None:
        with self._lock:
            self._closers.pop(job_id, None)

    def _compose(self, trace: list[str], texts: list[str]) -> str:
        """Report the tool trace first and the model's own words last."""
        parts: list[str] = []
        if trace:
            parts.append("Tool activity:\n" + "\n".join(f"- {line}" for line in trace))
        body = "\n\n".join(text.strip() for text in texts if text.strip())
        if body:
            parts.append(body)
        return "\n\n".join(parts)[-self.max_text_bytes:]

    def _stopped(
        self,
        error: str,
        trace: list[str],
        texts: list[str],
        usage: dict[str, int],
        steps: int,
        tool_calls: int,
        *,
        timed_out: bool = False,
    ) -> RunnerResult:
        """Report a loop that stopped without a final answer: the manager fails the job."""
        return RunnerResult(
            output=self._compose(trace, texts),
            exit_code=None,
            error=error,
            timed_out=timed_out,
            usage=_usage_counters(usage, steps, tool_calls),
        )

    def _exceeded(
        self,
        run: TaskRun,
        trace: list[str],
        texts: list[str],
        usage: dict[str, int],
        steps: int,
        tool_calls: int,
    ) -> RunnerResult:
        """Report a job that ran out of wall-clock budget."""
        return RunnerResult(
            output=self._compose(trace, texts),
            exit_code=None,
            error=f"The worker exceeded {run.max_minutes} minutes.",
            timed_out=True,
            usage=_usage_counters(usage, steps, tool_calls),
        )


def select_runner(selector: Optional[str] = None) -> WorkerRunner:
    """Build the runner named by ``selector`` or by ``MODEL_WORKER_RUNNER``.

    CodeWhale stays the default, so an unknown or empty value only changes the
    behavior when it is explicit. An unknown name is refused with the two known
    names in the message, because a silent fallback would run the wrong backend.
    """
    configured = os.environ.get(RUNNER_ENV) if selector is None else selector
    name = (configured or CODEWHALE_RUNNER_NAME).strip().casefold()
    if name == CODEWHALE_RUNNER_NAME:
        return CodeWhaleRunner()
    if name == DIRECT_RUNNER_NAME:
        return DirectDeepSeekRunner()
    raise RunnerSelectionError(
        f"Unknown {RUNNER_ENV} value {configured!r}: use {CODEWHALE_RUNNER_NAME!r} "
        f"(default) or {DIRECT_RUNNER_NAME!r}."
    )
