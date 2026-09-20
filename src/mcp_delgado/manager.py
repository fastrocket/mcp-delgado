"""Durable process manager for scoped Delgado jobs.

The manager owns policy, durable records, and the workspace audit. Worker
process construction and lifecycle belong to the injected ``WorkerRunner``,
which defaults to the CodeWhale adapter in ``runners.py``.
"""

from __future__ import annotations

import asyncio
import fnmatch
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Callable, Optional

from .runners import (
    CancellationToken,  # re-export: the manager owns one per implementation job
    CodeWhaleRunner,  # re-export: the default adapter and the documented seam
    DEEPSEEK_API_KEY_ENV,
    DirectDeepSeekRunner,  # re-export: the direct backend an operator opts into
    ReviewRun,
    ReviewTimeoutError,
    RunnerSelectionError,  # re-export: callers report a bad selector from the manager
    TaskRun,
    WORKER_OWNERSHIP_CHILD,
    WORKER_OWNERSHIP_IN_PROCESS,
    WORKER_OWNERSHIPS,
    WorkerRunner,
    codewhale_executable,  # re-export: callers imported this from the manager
    select_runner,
    worker_ownership,
)
from .schemas import DelegateTaskInput, JobRecord, JobState, ReviewInput, utc_now

DEFAULT_PROVIDER = "deepseek"
DEFAULT_MODEL = "deepseek-flash"
DEFAULT_TIMEOUT_SECONDS = 30 * 60
MAX_OUTPUT_BYTES = 2_000_000
PATCH_FILE_NAME = "changes.diff"
OUTPUT_FILE_NAME = "worker-output.txt"
RUNNER_FILE_NAME = "runner.json"
VALIDATION_EXECUTABLES = frozenset({
    "cargo", "go", "mypy", "node", "npm", "npm.cmd", "npx", "npx.cmd",
    "pnpm", "py", "py.exe", "pytest", "pytest.exe", "python", "python.exe",
    "python3", "ruff", "yarn",
})
PYTHON_VALIDATION_MODULES = frozenset({"compileall", "mypy", "py_compile", "pytest", "ruff"})
PYTHON_EXECUTABLES = frozenset({"py", "py.exe", "python", "python.exe", "python3"})
WINDOWS_COMMAND_SHIMS = frozenset({"npm", "npx", "pnpm", "yarn"})
SHELL_CONTROL_TOKENS = frozenset({"&", "&&", "|", "||", ";", "<", ">", ">>"})
REVIEW_TERMINATE_GRACE_SECONDS = 10.0
REVIEW_EXIT_POLL_SECONDS = 0.05
REVIEW_DRAIN_SECONDS = 5.0

# CodeWhale keeps its own state in a directory inside the target repository.
# That state is tool-owned, so it never counts as worker output.
TOOL_STATE_DIR_NAMES = frozenset({".codewhale"})

# pytest writes transient state into the checkout it validates: the root cache
# directory and any basetemp tree named `.pytest-*`. A worker that runs pytest
# as a required validation command therefore leaves tool-owned artifacts behind.
# The match is deliberately narrow: it applies to the repository root only, so a
# real source path that merely resembles one of these names stays auditable.
PYTEST_STATE_DIR_NAME = ".pytest_cache"
PYTEST_BASETEMP_PREFIX = ".pytest-"
GIT_DIFF_PATH_PREFIXES = ("a/", "b/")

LIVE_JOB_STATES = frozenset({JobState.QUEUED, JobState.RUNNING})
# One message for every job whose owning manager process is gone: startup
# recovery and a cancel that finds the owner dead report the same reason.
ORPHANED_JOB_ERROR = "The MCP server stopped before this job finished."
# One message for every job the operator cancelled, wherever that decision is
# published: the cancel call, and the job thread that finds its token set.
CANCELLED_JOB_ERROR = "The manager cancelled this job."
# Validation commands run inside the delegated workspace, so they are started with
# a copy of this process's environment minus the worker credential. The key
# belongs to the runner's own HTTP request and to nothing else, and any captured
# validation output is redacted against it before it becomes durable.
VALIDATION_ENV_EXCLUSIONS = frozenset({DEEPSEEK_API_KEY_ENV})
REDACTION_PLACEHOLDER = "<redacted>"
JOB_ID_PATTERN = re.compile(r"[a-f0-9]{32}")
WINDOWS_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
WINDOWS_STILL_ACTIVE = 259
WINDOWS_ERROR_ACCESS_DENIED = 5

# One implementation job owns one workspace. The lock lives in the shared state
# directory, so separate MCP server processes see the same claim.
LOCK_DIR_NAME = "locks"
LOCK_SUFFIX = ".lock"
LOCK_ACQUIRE_ATTEMPTS = 3
LOCK_PAYLOAD_READ_ATTEMPTS = 25
LOCK_PAYLOAD_READ_DELAY_SECONDS = 0.01

# A second invocation can read the store while a job writes its record, and the
# CLI escape hatch makes that overlap routine. On Windows a replace or an open
# can briefly fail with a sharing violation, so both sides retry for a moment
# instead of failing the job thread or the caller that is polling.
STATE_ATTEMPTS = 20
STATE_RETRY_DELAY_SECONDS = 0.05


# ``ReviewTimeoutError`` is defined with the runner boundary in ``runners.py``
# and re-exported here, because MCP and CLI callers catch it from the manager.


class WorkspaceBusyError(RuntimeError):
    """Raised when a workspace already holds an implementation job.

    ``submit`` raises this instead of starting a second worker in one checkout,
    including when the two callers use different MCP server processes that share
    ``MODEL_WORKER_STATE_DIR``.
    """

    def __init__(self, message: str, job_id: str = "", workspace_path: str = "", pid: Optional[int] = None) -> None:
        super().__init__(message)
        self.job_id = job_id
        self.workspace_path = workspace_path
        self.pid = pid


class JobOwnershipError(RuntimeError):
    """Raised when a job's worker belongs to another live manager process.

    ``cancel`` raises this for a worker that runs inside the process that
    started it: no other process can stop that work, and publishing CANCELLED
    while the loop may still be editing the workspace would be false. The
    message names the owning process, so an operator cancels from there.
    """

    def __init__(self, message: str, job_id: str = "", pid: Optional[int] = None) -> None:
        super().__init__(message)
        self.job_id = job_id
        self.pid = pid


def _workspace_busy_error(workspace: Path, holder: dict) -> WorkspaceBusyError:
    """Name the active job and the workspace in one actionable refusal."""
    job_id = holder.get("job_id")
    job_id = job_id if isinstance(job_id, str) and job_id else "unknown"
    pid = holder.get("pid")
    owner = f"pid {pid}" if isinstance(pid, int) else "an unnamed process"
    return WorkspaceBusyError(
        f"Workspace {workspace} is already running job {job_id} ({owner}). "
        "Wait for that job to finish, cancel it, or delegate to a different workspace.",
        job_id=job_id,
        workspace_path=str(workspace),
        pid=pid if isinstance(pid, int) else None,
    )


def _windows_pid_is_running(pid: int) -> bool:
    """Query a Windows pid without disturbing the process it inspects.

    ``os.kill(pid, 0)`` terminates its target on Windows, and a recorded pid can
    belong to an unrelated process by the time old state is read.
    """
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel32.OpenProcess(WINDOWS_PROCESS_QUERY_LIMITED_INFORMATION, False, wintypes.DWORD(pid))
    if not handle:
        # Access is denied only for a process that exists but cannot be queried.
        return ctypes.get_last_error() == WINDOWS_ERROR_ACCESS_DENIED
    try:
        exit_code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return False
        return exit_code.value == WINDOWS_STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


class JobManager:
    def __init__(
        self,
        state_dir: Optional[Path] = None,
        command_runner: Optional[Callable[..., subprocess.CompletedProcess[str]]] = None,
        runner: Optional[WorkerRunner] = None,
    ) -> None:
        configured = os.environ.get("MODEL_WORKER_STATE_DIR")
        self.state_dir = (state_dir or Path(configured or Path.home() / ".mcp-delgado")).resolve()
        self.jobs_dir = self.state_dir / "jobs"
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self.locks_dir = self.state_dir / LOCK_DIR_NAME
        self.locks_dir.mkdir(parents=True, exist_ok=True)
        # The runner owns worker processes; the manager owns records and policy.
        # CodeWhale stays the default, and MODEL_WORKER_RUNNER opts into another
        # backend explicitly, so an unknown selector is refused instead of
        # silently running a job on the wrong one.
        self.runner: WorkerRunner = runner if runner is not None else select_runner()
        self.command_runner = command_runner or subprocess.run
        self._lock = threading.RLock()
        # One cancellation token per job this process started. It exists before
        # the job thread does, so cancel never depends on the runner having
        # registered the job yet.
        self._cancellations: dict[str, CancellationToken] = {}
        self._mark_stale_jobs()

    def _mark_stale_jobs(self) -> None:
        """Interrupt every job no live process can still be running.

        The reading of a job's pid depends on how its worker is held. A
        child-backed job (CodeWhale) is live while its recorded worker pid runs. A
        job whose worker runs inside the manager process is live while the manager
        process that recorded it runs, and the pid recorded beside it names that
        manager rather than a worker. A job whose owner is gone is marked
        interrupted here and its workspace lock is released, so the next submit can
        claim the workspace immediately instead of waiting for the lock rule.
        """
        for record_path in self.jobs_dir.glob("*/job.json"):
            try:
                record = JobRecord.model_validate_json(record_path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if record.state not in LIVE_JOB_STATES:
                continue
            if self._job_worker_is_live(record):
                continue
            self._mark_interrupted(record)

    def _job_worker_is_live(self, record: JobRecord) -> bool:
        """Report whether the process that owns this job's worker is still alive.

        The check never touches the process it inspects, so a pid that an old
        record names cannot be disturbed by asking about it.
        """
        if self._recorded_worker_ownership(record) == WORKER_OWNERSHIP_IN_PROCESS:
            owner = self._recorded_owner_pid(record)
            return isinstance(owner, int) and self._pid_is_running(owner)
        return bool(record.pid) and self._pid_is_running(record.pid)

    def _recorded_runner_metadata(self, record: JobRecord) -> dict:
        """Read the runner metadata written beside a job record.

        The process that stops a job is not necessarily the runner this process
        was configured with: a second manager process may run the default
        CodeWhale adapter while the job in the store belongs to the direct
        backend. The job's own metadata answers. An unreadable or older file is
        read as a child-backed worker, which is the CodeWhale behavior.
        """
        target = self._job_dir(record.job_id) / RUNNER_FILE_NAME
        try:
            payload = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def _recorded_worker_ownership(self, record: JobRecord) -> str:
        """Report how the process that owns this job holds its worker.

        ``in-process`` means the job runs on a thread inside its owning manager
        process and has no worker pid that any process may signal.
        """
        declared = self._recorded_runner_metadata(record).get("worker_ownership")
        return declared if declared in WORKER_OWNERSHIPS else WORKER_OWNERSHIP_CHILD

    def _recorded_owner_pid(self, record: JobRecord) -> Optional[int]:
        """Return the manager process that runs an in-process worker, if recorded."""
        pid = self._recorded_runner_metadata(record).get("owner_pid")
        if isinstance(pid, bool) or not isinstance(pid, int):
            return None
        return pid

    @staticmethod
    def _pid_is_running(pid: int) -> bool:
        """Report whether a process is still alive.

        The check must not touch the process it inspects: a pid stored in old
        state can name an unrelated process by the time that state is read.
        """
        if os.name == "nt":
            return _windows_pid_is_running(pid)
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False
        return True

    def _job_dir(self, job_id: str) -> Path:
        return self.jobs_dir / job_id

    def _save(self, record: JobRecord) -> None:
        job_dir = self._job_dir(record.job_id)
        job_dir.mkdir(parents=True, exist_ok=True)
        target = job_dir / "job.json"
        temporary = job_dir / "job.json.tmp"
        temporary.write_text(record.model_dump_json(indent=2), encoding="utf-8")
        self._replace_record(temporary, target)

    @staticmethod
    def _replace_record(temporary: Path, target: Path) -> None:
        """Publish a record atomically, tolerating a concurrent reader.

        ``os.replace`` fails on Windows while another handle holds the target
        open. A reader that only polls state releases it within microseconds, so
        a short retry keeps the job thread alive instead of losing the update.
        """
        for attempt in range(STATE_ATTEMPTS):
            try:
                os.replace(temporary, target)
                return
            except PermissionError:
                if attempt + 1 >= STATE_ATTEMPTS:
                    raise
                time.sleep(STATE_RETRY_DELAY_SECONDS)

    def get(self, job_id: str) -> JobRecord:
        target = self._job_dir(job_id) / "job.json"
        if not target.is_file():
            raise KeyError(f"Unknown job: {job_id}")
        return JobRecord.model_validate_json(self._read_record(target, job_id))

    @staticmethod
    def _read_record(target: Path, job_id: str) -> str:
        """Read a record, tolerating a concurrent atomic replace."""
        for attempt in range(STATE_ATTEMPTS):
            try:
                return target.read_text(encoding="utf-8")
            except FileNotFoundError:
                raise KeyError(f"Unknown job: {job_id}") from None
            except PermissionError:
                if attempt + 1 >= STATE_ATTEMPTS:
                    raise
                time.sleep(STATE_RETRY_DELAY_SECONDS)
        raise KeyError(f"Unknown job: {job_id}")

    @staticmethod
    def _workspace(raw_path: str) -> Path:
        workspace = Path(raw_path).expanduser().resolve()
        if not workspace.is_dir():
            raise ValueError(f"Workspace does not exist: {workspace}")
        if not (workspace / ".git").exists():
            result = subprocess.run(
                ["git", "-C", str(workspace), "rev-parse", "--is-inside-work-tree"],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=15,
            )
            if result.returncode != 0:
                raise ValueError(f"Workspace is not a Git checkout: {workspace}")
        return workspace

    @staticmethod
    def _normalize_allowed_paths(paths: list[str]) -> list[str]:
        normalized: list[str] = []
        for raw in paths:
            value = raw.replace("\\", "/").strip()
            while value.startswith("./"):
                value = value[2:]
            parts = value.split("/")
            if not value or value.startswith("/") or ".." in parts or ":" in parts[0]:
                raise ValueError(f"Invalid allowed path: {raw}")
            normalized.append(value.rstrip("/"))
        return normalized

    @staticmethod
    def _is_tool_state_path(path: str) -> bool:
        """Report whether tool-owned transient state owns this repository-relative path.

        ``.codewhale`` matches at any depth because CodeWhale may keep state in a
        nested directory. pytest state matches only at the repository root, and
        only as the exact ``.pytest_cache`` name or a ``.pytest-*`` basetemp
        tree, so ordinary source paths stay visible to the audit.
        """
        segments = path.replace("\\", "/").split("/")
        if any(segment in TOOL_STATE_DIR_NAMES for segment in segments):
            return True
        root = segments[0]
        return root == PYTEST_STATE_DIR_NAME or root.startswith(PYTEST_BASETEMP_PREFIX)

    @classmethod
    def _is_tool_state_diff_path(cls, token: str) -> bool:
        """Match a ``diff --git`` path token, which git prefixes with ``a/`` or ``b/``."""
        normalized = token.replace("\\", "/")
        for prefix in GIT_DIFF_PATH_PREFIXES:
            if normalized.startswith(prefix):
                normalized = normalized[len(prefix):]
                break
        return cls._is_tool_state_path(normalized)

    @classmethod
    def _status(cls, workspace: Path, allowed_paths: Optional[list[str]] = None) -> dict[str, str]:
        entries: dict[str, str] = {}
        command = [
            "git", "-C", str(workspace), "status", "--porcelain=v1", "-z", "--no-renames",
            "--untracked-files=all",
        ]
        result = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True, timeout=30)
        if result.returncode != 0:
            raise RuntimeError(result.stderr.decode("utf-8", errors="replace").strip())
        for item in result.stdout.decode("utf-8", errors="replace").split("\0"):
            if not item:
                continue
            status = item[:2]
            path = item[3:].replace("\\", "/")
            if " -> " in path:
                path = path.split(" -> ", 1)[1]
            if cls._is_tool_state_path(path):
                continue
            target = workspace / path
            if target.is_file():
                digest = hashlib.sha256(target.read_bytes()).hexdigest()
                status = f"{status}:{digest}"
            entries[path] = status
        return entries

    @staticmethod
    def _is_allowed(path: str, patterns: list[str]) -> bool:
        normalized = path.replace("\\", "/")
        for pattern in patterns:
            if fnmatch.fnmatch(normalized, pattern):
                return True
            if normalized == pattern or normalized.startswith(pattern + "/"):
                return True
        return False

    @staticmethod
    def _prompt(record: JobRecord) -> str:
        criteria = "\n".join(f"- {item}" for item in record.acceptance_criteria) or "- Follow the task exactly."
        paths = "\n".join(f"- {item}" for item in record.allowed_paths)
        commands = "\n".join(f"- {item}" for item in record.required_commands) or "- No manager-supplied checks."
        prompt = f"""You are an implementation worker. The manager owns architecture and deployment.

Task:
{record.task}

Acceptance criteria:
{criteria}

You may change only these paths:
{paths}

Required validation commands:
{commands}

Inspect existing conventions before editing. Do not change any other path.
Do not commit, push, merge, deploy, or access production systems.
Do not change dependencies unless the task explicitly requires that change.
Run the required checks after editing. Preserve useful work when a check fails.
End with a short summary, changed files, checks, and unresolved issues.
"""
        return " ".join(prompt.split())

    def _owned_cancellation(self, job_id: str) -> Optional[CancellationToken]:
        """Return this process's cancellation token for a job, if it started it.

        A token exists only in the process that submitted the job, so its absence
        is what tells ``cancel`` that the job belongs to another process and must
        go through the pid rules instead.
        """
        with self._lock:
            return self._cancellations.get(job_id)

    def _cancellation_token(self, job_id: str) -> CancellationToken:
        """Return the cancellation token for a job, creating it when it is absent.

        ``submit`` creates the token before the job thread starts, so a job thread
        normally finds its own. The fallback keeps a thread that was started by
        some other caller working instead of failing on missing bookkeeping.
        """
        with self._lock:
            token = self._cancellations.get(job_id)
            if token is None:
                token = CancellationToken(job_id)
                self._cancellations[job_id] = token
            return token

    def _forget_cancellation(self, job_id: str) -> None:
        """Drop a finished job's token once its thread has released the workspace."""
        with self._lock:
            self._cancellations.pop(job_id, None)

    def _workspace_lock_path(self, workspace: Path) -> Path:
        """Return the durable lock path shared by every process for one workspace."""
        key = os.path.normcase(str(workspace))
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]
        return self.locks_dir / f"{digest}{LOCK_SUFFIX}"

    def _acquire_workspace_lock(self, workspace: Path, job_id: str) -> None:
        """Atomically claim one workspace for one implementation job.

        The claim outlives the calling process, so a second submit is refused
        with ``WorkspaceBusyError`` until the owning job finishes, is cancelled,
        or is proven gone. Different workspaces use different lock files and
        therefore stay concurrent.
        """
        path = self._workspace_lock_path(workspace)
        payload = {
            "job_id": job_id,
            "workspace_path": str(workspace),
            "pid": os.getpid(),
            "created_at": utc_now(),
            "token": uuid.uuid4().hex,
        }
        with self._lock:
            for _ in range(LOCK_ACQUIRE_ATTEMPTS):
                try:
                    handle = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                except FileExistsError:
                    holder = self._read_lock_payload(path)
                    if holder is None:
                        continue  # the holder released it while we looked
                    if not self._lock_is_stale(holder):
                        raise _workspace_busy_error(workspace, holder)
                    if not self._drop_stale_lock(path, holder):
                        raise _workspace_busy_error(workspace, holder)
                    continue
                with os.fdopen(handle, "w", encoding="utf-8") as stream:
                    json.dump(payload, stream, indent=2)
                return
        raise _workspace_busy_error(workspace, {})

    def _read_lock_payload(self, path: Path, attempts: int = LOCK_PAYLOAD_READ_ATTEMPTS) -> Optional[dict]:
        """Read a lock payload.

        A holder publishes its payload just after it creates the file, so a
        reader retries briefly. Returns the payload mapping, an empty mapping
        when the holder never published one, or ``None`` when the file is gone.
        """
        for attempt in range(max(1, attempts)):
            try:
                text = path.read_text(encoding="utf-8")
            except FileNotFoundError:
                return None
            except OSError:
                text = ""
            if text.strip():
                try:
                    payload = json.loads(text)
                except ValueError:
                    payload = None
                if isinstance(payload, dict):
                    return payload
            if attempt + 1 < attempts:
                time.sleep(LOCK_PAYLOAD_READ_DELAY_SECONDS)
        return {}

    def _lock_is_stale(self, holder: dict) -> bool:
        """Report whether a lock may be recovered.

        A lock is recoverable only when every process it names is gone. A
        terminal or cancelled record is not evidence that the work stopped: the
        owning job thread keeps the workspace until it releases the lock itself,
        and a cancel publishes CANCELLED while that thread is still returning
        from a stopped worker. Treating a terminal record as recoverable would
        hand the checkout to a second job in the middle of the first one. A lock
        that names nobody is unreadable state, not a live holder, so it is
        recoverable as well.
        """
        record = self._lock_job_record(holder)
        pids = [holder.get("pid")]
        if record is not None:
            pids.append(record.pid)
            if self._recorded_worker_ownership(record) == WORKER_OWNERSHIP_IN_PROCESS:
                # An in-process worker has no worker pid: the process that runs its
                # loop is the only thing that can still be editing the checkout.
                pids.append(self._recorded_owner_pid(record))
        return not any(self._pid_is_running(pid) for pid in pids if isinstance(pid, int))

    def _lock_job_record(self, holder: dict) -> Optional[JobRecord]:
        job_id = holder.get("job_id")
        if not isinstance(job_id, str) or not JOB_ID_PATTERN.fullmatch(job_id):
            return None
        try:
            return self.get(job_id)
        except (KeyError, ValueError):
            return None

    def _drop_stale_lock(self, path: Path, stale: dict) -> bool:
        """Delete a stale lock unless it changed hands since it was read."""
        current = self._read_lock_payload(path, attempts=1)
        if current is None:
            return True
        if current != stale:
            return False
        try:
            path.unlink()
        except OSError:
            return False
        return True

    def _release_workspace_lock(self, workspace: str | Path, job_id: str) -> None:
        """Release the workspace lock while this job still owns it.

        The unlink is retried briefly, because a release that fails leaves a lock
        its own live owner still holds: with a terminal record no longer counting
        as recoverable, the next submit waits for that owner instead of taking the
        workspace over. Another holder's lock is never touched.
        """
        path = self._workspace_lock_path(Path(workspace))
        with self._lock:
            for attempt in range(STATE_ATTEMPTS):
                holder = self._read_lock_payload(path, attempts=1)
                if holder is None or holder.get("job_id") != job_id:
                    return
                try:
                    path.unlink()
                    return
                except FileNotFoundError:
                    return
                except OSError:
                    if attempt + 1 >= STATE_ATTEMPTS:
                        return
                    time.sleep(STATE_RETRY_DELAY_SECONDS)

    def submit(self, params: DelegateTaskInput, parent_job_id: Optional[str] = None) -> JobRecord:
        workspace = self._workspace(params.workspace_path)
        # The store placement is checked before a record, a lock, or a thread
        # exists, so a refused submit leaves the store and the workspace untouched.
        self._refuse_state_dir_inside_workspace(workspace)
        allowed_paths = self._normalize_allowed_paths(params.allowed_paths)
        self._validate_commands(params.required_commands)
        record = JobRecord(
            job_id=uuid.uuid4().hex,
            state=JobState.QUEUED,
            task=params.task,
            acceptance_criteria=params.acceptance_criteria,
            workspace_path=str(workspace),
            allowed_paths=allowed_paths,
            required_commands=params.required_commands,
            provider=params.provider or os.environ.get("MODEL_WORKER_PROVIDER", DEFAULT_PROVIDER),
            model=params.model or os.environ.get("MODEL_WORKER_MODEL", DEFAULT_MODEL),
            max_minutes=params.max_minutes,
            parent_job_id=parent_job_id,
        )
        self._acquire_workspace_lock(workspace, record.job_id)
        try:
            self._save(record)
            # Ownership is durable before the worker starts, so a second manager
            # process that reads this job while it is still queued already knows
            # whether its worker has a pid anyone may signal.
            self._write_runner_identity(record)
            # The cancellation token exists before the job thread does, so a
            # cancel that arrives the instant this submit returns is already
            # visible to the thread and to the runner's own loop.
            self._cancellation_token(record.job_id)
            before = self._status(workspace, allowed_paths)
            self._start_job_thread(record, before)
        except Exception:
            self._forget_cancellation(record.job_id)
            self._release_workspace_lock(workspace, record.job_id)
            raise
        return record

    def _start_job_thread(self, record: JobRecord, before: dict[str, str]) -> None:
        """Start the thread that runs one job.

        This named seam is the only place the manager starts a job thread, and it
        runs after the workspace lock, the durable record, the runner identity,
        and the cancellation token all exist: whoever holds this seam back is
        still holding a fully claimed job.
        """
        threading.Thread(target=self._run_job, args=(record.job_id, before), daemon=True).start()

    def _refuse_state_dir_inside_workspace(self, workspace: Path) -> None:
        """Refuse a job whose workspace contains this manager's durable state.

        The worker may change every path it is given, so a store at or below the
        delegated checkout would put job records, stored patches, and workspace
        locks inside the paths the worker is allowed to edit: the audit would
        report manager state as worker output, and the worker could rewrite the
        records that judge it. The default store is already outside a checkout;
        this catches an explicit ``MODEL_WORKER_STATE_DIR`` or ``--state-dir``
        that points inside one.
        """
        state_dir = Path(self.state_dir)
        if state_dir == workspace or workspace in state_dir.parents:
            raise ValueError(
                f"The job store {state_dir} is inside the delegated workspace {workspace}, "
                "and a worker is allowed to change the paths it is given. Point "
                "MODEL_WORKER_STATE_DIR (or --state-dir) at a directory outside this workspace."
            )

    def review(self, params: ReviewInput) -> dict:
        """Run a read-only review from synchronous code such as the CLI."""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.review_async(params))
        raise RuntimeError(
            "JobManager.review() cannot run inside a running event loop; await review_async() instead."
        )

    async def review_async(self, params: ReviewInput) -> dict:
        """Run a read-only review under asyncio ownership of the child process.

        The manager supervises the child: it drains both pipes, enforces the
        runner's budget, and stops the whole tree on a timeout or a cancelled
        caller before anything is reported back. The runner supplies the argv,
        the budget, and the process control. The payload matches ``review()``:
        exit code, trimmed stdout, stderr tail.
        """
        workspace = self._workspace(params.workspace_path)
        run = self._review_run(params, workspace)
        process = await self._spawn_review_process(self.runner.review_command(run), workspace)
        stdout_chunks: list[bytes] = []
        stderr_chunks: list[bytes] = []
        stdout_task = asyncio.ensure_future(self._read_stream(process.stdout, stdout_chunks))
        stderr_task = asyncio.ensure_future(self._read_stream(process.stderr, stderr_chunks))
        try:
            await asyncio.wait_for(process.wait(), timeout=self.runner.review_timeout_seconds(run))
        except asyncio.TimeoutError as exc:
            await self._terminate_review_process(process)
            await self._finish_stream_readers(stdout_task, stderr_task)
            payload = self._review_payload(process.returncode, stdout_chunks, stderr_chunks)
            raise ReviewTimeoutError(
                f"The review exceeded {run.max_minutes} minutes; "
                f"the {self.runner.label} process tree was terminated.",
                output=payload["output"],
                diagnostics_tail=payload["diagnostics_tail"],
            ) from exc
        except asyncio.CancelledError:
            await self._terminate_review_process(process)
            await self._finish_stream_readers(stdout_task, stderr_task)
            raise
        await self._finish_stream_readers(stdout_task, stderr_task)
        return self._review_payload(process.returncode, stdout_chunks, stderr_chunks)

    @staticmethod
    def _review_run(params: ReviewInput, workspace: Path) -> ReviewRun:
        """Resolve one review request into the runner's read-only request."""
        return ReviewRun(
            request=params.request,
            workspace=workspace,
            provider=params.provider or os.environ.get("MODEL_WORKER_PROVIDER", DEFAULT_PROVIDER),
            model=params.model or os.environ.get("MODEL_WORKER_MODEL", DEFAULT_MODEL),
            max_minutes=params.max_minutes,
        )

    @staticmethod
    def _review_payload(
        exit_code: Optional[int],
        stdout_chunks: list[bytes],
        stderr_chunks: list[bytes],
    ) -> dict:
        output = b"".join(stdout_chunks).decode("utf-8", errors="replace").strip()
        diagnostics = b"".join(stderr_chunks).decode("utf-8", errors="replace").strip()
        if exit_code != 0 and not output:
            output = diagnostics
        return {
            "exit_code": exit_code,
            "output": output[-50_000:],
            "diagnostics_tail": diagnostics[-20_000:],
        }

    async def _spawn_review_process(
        self, command: list[str], workspace: Path
    ) -> asyncio.subprocess.Process:
        """Start the runner's review child.

        This named seam is where the manager hands process control to the
        injected runner, and a caller may replace it to drive the review loop
        without a worker binary.
        """
        return await self.runner.spawn_review_process(command, workspace)

    @staticmethod
    async def _read_stream(stream: asyncio.StreamReader, sink: list[bytes]) -> None:
        while True:
            chunk = await stream.read(65_536)
            if not chunk:
                return
            sink.append(chunk)

    @staticmethod
    async def _finish_stream_readers(*tasks: asyncio.Task) -> None:
        """Let the readers drain, then stop any that a surviving descendant keeps open."""
        pending = [task for task in tasks if not task.done()]
        if not pending:
            return
        done, still_pending = await asyncio.wait(pending, timeout=REVIEW_DRAIN_SECONDS)
        for task in done:
            if not task.cancelled():
                task.exception()
        for task in still_pending:
            task.cancel()
        if still_pending:
            await asyncio.wait(still_pending)

    async def _terminate_review_process(self, process: asyncio.subprocess.Process) -> None:
        """Stop the review child and every descendant, then wait for it to exit."""
        if process.returncode is not None:
            return
        # The stop runs on a worker thread so a cancelled call keeps serving other tools.
        await asyncio.to_thread(self._kill_review_tree, process, False)
        if await self._await_review_exit(process, REVIEW_TERMINATE_GRACE_SECONDS):
            return
        await asyncio.to_thread(self._kill_review_tree, process, True)
        await self._await_review_exit(process, REVIEW_TERMINATE_GRACE_SECONDS)

    def _kill_review_tree(self, process: asyncio.subprocess.Process, force: bool) -> None:
        """Stop a review child and its descendants through the injected runner."""
        self.runner.kill_review_tree(process, force)

    @staticmethod
    async def _await_review_exit(process: asyncio.subprocess.Process, timeout: float) -> bool:
        """Poll for the stopped child to be reaped without disturbing the loop's waiters."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while process.returncode is None:
            if loop.time() >= deadline:
                return False
            await asyncio.sleep(REVIEW_EXIT_POLL_SECONDS)
        return True

    def _run_job(self, job_id: str, before: dict[str, str]) -> None:
        """Run one job and release its workspace lock on every terminal path.

        Success, failure, validation timeout, and cancellation all pass through
        this release, so the next submit can claim the workspace immediately. The
        job's cancellation token is dropped here as well: while this thread is
        alive the token is what makes a cancel visible, and the workspace lock is
        reclaimable only once this thread has released it.
        """
        record = self.get(job_id)
        try:
            self._execute_job(record, before)
        finally:
            self._release_workspace_lock(record.workspace_path, job_id)
            self._forget_cancellation(job_id)

    def _begin_running(self, record: JobRecord, token: CancellationToken) -> bool:
        """Publish RUNNING unless the job was cancelled first.

        The check and the state change are one locked step, so a cancel arriving
        at the same instant either sees a live job and publishes CANCELLED, or
        publishes CANCELLED first and is seen here. RUNNING can therefore never
        overwrite the operator's decision, and a job cancelled before its worker
        started runs no worker at all. Returns ``False`` for a job that is already
        cancelled, in this process or by another manager process that recorded
        CANCELLED before this thread began.
        """
        with self._lock:
            latest = self.get(record.job_id)
            if token.cancelled or latest.state == JobState.CANCELLED:
                latest.state = JobState.CANCELLED
                latest.finished_at = utc_now()
                latest.error = latest.error or CANCELLED_JOB_ERROR
                self._save(latest)
                return False
            record.state = JobState.RUNNING
            record.started_at = utc_now()
            self._save(record)
            self._write_runner_identity(record)
            return True

    def _execute_job(self, record: JobRecord, before: dict[str, str]) -> None:
        """Run one job through the injected runner, then audit what it changed.

        The runner reports what the worker process did. Every decision below is
        the manager's: job state, scope policy, validations, and the patch.

        The job's cancellation token is read before the runner starts and again
        before any validation command: a job cancelled before its worker ran does
        no work at all, and a job cancelled while it ran starts no new validation,
        because every command would run in the workspace the operator just took
        back.
        """
        job_id = record.job_id
        workspace = Path(record.workspace_path)
        token = self._cancellation_token(job_id)
        if not self._begin_running(record, token):
            return

        run = TaskRun(
            job_id=job_id,
            prompt=self._prompt(record),
            workspace=workspace,
            provider=record.provider,
            model=record.model,
            max_minutes=record.max_minutes,
            allowed_paths=tuple(record.allowed_paths),
            cancel_token=token,
        )
        output_path = self._job_dir(job_id) / OUTPUT_FILE_NAME
        try:
            result = self.runner.run_task(run, self._process_started(record, worker_ownership(self.runner)))
            output_path.write_text(result.output[-MAX_OUTPUT_BYTES:], encoding="utf-8")
            record.exit_code = result.exit_code
            record.error = result.error
            record.state = (
                JobState.SUCCEEDED
                if result.exit_code == 0 and not result.timed_out
                else JobState.FAILED
            )
            record.summary = self._compact_summary(result.output)
            self._write_runner_identity(record, result.usage)
        except Exception as exc:
            record.state = JobState.FAILED
            record.error = f"{type(exc).__name__}: {exc}"

        after = self._status(workspace, record.allowed_paths)
        changed = sorted(path for path, status in after.items() if before.get(path) != status)
        record.changed_paths = changed
        record.policy_violations = [
            path for path in changed if not self._is_allowed(path, record.allowed_paths)
        ]
        if record.policy_violations and record.state == JobState.SUCCEEDED:
            record.state = JobState.POLICY_FAILED

        if self._job_is_cancelled(job_id, token):
            # Fail closed: no validation command is started for a cancelled job,
            # because every command would run in the workspace the operator just
            # took back. A command that was already running is not interrupted: it
            # ends on its own or at its timeout, and the record says so afterwards.
            record.validation_results = []
        else:
            record.validation_results = self._run_validations(record, token)
            if any(item["exit_code"] != 0 for item in record.validation_results) and record.state == JobState.SUCCEEDED:
                record.state = JobState.FAILED
        self._write_patch(record, before)
        record.finished_at = utc_now()
        # Cancellation races with the worker thread returning from its stopped
        # child. Keep the user's terminal decision if cancel() published it
        # while this thread was auditing changes and running validations.
        with self._lock:
            latest = self.get(job_id)
            if latest.state == JobState.CANCELLED or token.cancelled:
                record.state = JobState.CANCELLED
                record.error = latest.error or CANCELLED_JOB_ERROR
            self._save(record)

    def _job_is_cancelled(self, job_id: str, token: CancellationToken) -> bool:
        """Report whether this job's cancellation is already decided.

        The token covers a cancel in this process, and the stored record covers a
        cancel published by another manager process, which cannot set a token it
        does not own. Either one means the job is terminal as CANCELLED and no new
        work may be started under its name.
        """
        if token.cancelled:
            return True
        try:
            return self.get(job_id).state == JobState.CANCELLED
        except (KeyError, ValueError):
            return False

    def _process_started(
        self, record: JobRecord, ownership: str = WORKER_OWNERSHIP_CHILD
    ) -> Callable[[int], None]:
        """Return the callback that records the live owner while a job runs.

        A child-backed worker gets a durable worker pid, so a second manager
        process can stop it by pid. An in-process worker has no worker pid: the pid
        it reports names this manager process, so it is never stored in the
        record's pid field and is never signalled. Ownership metadata names the
        owning process instead, which is what startup recovery and a
        cross-process cancel consult. A pid that names this very process is
        refused for the same reason, whatever the runner declared.
        """

        def report(pid: int) -> None:
            if ownership == WORKER_OWNERSHIP_IN_PROCESS or pid == os.getpid():
                return
            record.pid = pid
            self._save(record)

        return report

    def _write_runner_identity(self, record: JobRecord, usage: Optional[dict] = None) -> None:
        """Record which runner owns this job, beside the record and the patch.

        The job record forbids extra fields, so identity stays a job-scoped
        artifact and the public MCP and CLI payloads keep their shape. The file is
        written when the job is submitted and rewritten with the worker's usage
        counters when it stops. It also carries how the worker is held: a ``child`` worker
        is a process the manager may signal, while an ``in-process`` worker is a
        thread inside this manager process, whose pid is recorded as
        ``owner_pid`` for recovery and cancellation rather than as a killable
        worker pid. Only non-secret metadata is ever written: the runner name, the
        worker model, the ownership, a manager pid, and integer counters. Identity
        is diagnostics and the ownership record, so a failed write must not fail
        the job.
        """
        ownership = worker_ownership(self.runner)
        payload: dict = {
            "runner": self.runner.name,
            "model": record.model,
            "worker_ownership": ownership,
        }
        if ownership == WORKER_OWNERSHIP_IN_PROCESS:
            payload["owner_pid"] = os.getpid()
        counters = {
            key: value
            for key, value in (usage or {}).items()
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0
        }
        if counters:
            payload["usage"] = counters
        try:
            (self._job_dir(record.job_id) / RUNNER_FILE_NAME).write_text(
                json.dumps(payload, indent=2),
                encoding="utf-8",
            )
        except OSError:
            pass

    @staticmethod
    def _compact_summary(output: str) -> str:
        cleaned = output.strip()
        if not cleaned:
            return "The worker returned no output."
        lines = cleaned.splitlines()
        return "\n".join(lines[-80:])[-20_000:]

    @staticmethod
    def _validation_env() -> dict[str, str]:
        """Return the environment a validation command may inherit.

        The worker credential is removed rather than masked, because a validation
        command is another program running in the checkout and the key belongs to
        the runner's own HTTP request only. Every other variable this process was
        started with is passed through, so ordinary tooling keeps working.
        """
        return {
            name: value
            for name, value in os.environ.items()
            if name.upper() not in VALIDATION_ENV_EXCLUSIONS
        }

    @staticmethod
    def _redact_env_secrets(text: str) -> str:
        """Remove every configured worker credential from text about to be stored.

        A validation child never receives the key, and this is the backstop that
        keeps a tool which prints its environment from writing the value into a
        durable validation result or a stored error message.
        """
        for name in VALIDATION_ENV_EXCLUSIONS:
            secret = os.environ.get(name, "").strip()
            if secret:
                text = text.replace(secret, REDACTION_PLACEHOLDER)
        return text

    def _run_validations(
        self, record: JobRecord, token: Optional[CancellationToken] = None
    ) -> list[dict]:
        """Run the manager's validation commands without the worker credential.

        Each command runs with :meth:`_validation_env`, and whatever it captured
        is redacted against those same names before it becomes durable. The job's
        cancellation token is read before each command, so a cancelled job starts
        no further command; a command already running is not interrupted here.
        """
        results: list[dict] = []
        timeout = int(os.environ.get("MODEL_WORKER_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS))
        environment = self._validation_env()
        for command in record.required_commands:
            if token is not None and token.cancelled:
                break
            try:
                args = self._validation_args(command)
                args[0] = self._validation_executable(args[0])
                completed = self.command_runner(
                    args,
                    cwd=record.workspace_path,
                    env=environment,
                    stdin=subprocess.DEVNULL,
                    shell=False,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                )
                combined = "\n".join(part for part in (completed.stdout, completed.stderr) if part)
                results.append({
                    "command": command,
                    "exit_code": completed.returncode,
                    "output_tail": self._redact_env_secrets(combined)[-20_000:],
                })
            except Exception as exc:
                results.append({
                    "command": command,
                    "exit_code": -1,
                    "output_tail": self._redact_env_secrets(f"{type(exc).__name__}: {exc}")[-20_000:],
                })
        return results

    @staticmethod
    def _validation_args(command: str) -> list[str]:
        try:
            args = shlex.split(command, posix=True)
        except ValueError as exc:
            raise ValueError(f"Invalid validation command: {command}") from exc
        if not args or any(token in SHELL_CONTROL_TOKENS for token in args):
            raise ValueError(f"Invalid validation command: {command}")
        executable = Path(args[0]).name.lower()
        if executable not in VALIDATION_EXECUTABLES:
            raise ValueError(f"Validation executable is not allowed: {args[0]}")
        if executable in PYTHON_EXECUTABLES:
            if len(args) < 2 or args[1] in {"-", "-c"}:
                raise ValueError("Python validation must use an approved module or a script file")
            if args[1] == "-m":
                if len(args) < 3 or args[2] not in PYTHON_VALIDATION_MODULES:
                    raise ValueError(f"Python validation module is not allowed: {args[2] if len(args) > 2 else ''}")
            elif not args[1].lower().endswith(".py"):
                raise ValueError("Python validation must use an approved module or a script file")
        return args

    @staticmethod
    def _validation_executable(command: str) -> str:
        """Resolve JavaScript package-manager shims for shell-free Windows runs.

        npm and its peers are installed as ``.cmd`` launchers on Windows.
        ``subprocess.run(..., shell=False)`` does not apply cmd.exe's launcher
        lookup, so resolve the already-allowlisted executable explicitly while
        leaving every other validation command unchanged.
        """
        executable = Path(command).name.lower()
        if os.name != "nt" or executable not in WINDOWS_COMMAND_SHIMS:
            return command
        return shutil.which(f"{command}.cmd") or shutil.which(command) or command

    @classmethod
    def _validate_commands(cls, commands: list[str]) -> None:
        for command in commands:
            cls._validation_args(command)

    @classmethod
    def _strip_tool_state_diff(cls, text: str) -> str:
        """Drop every diff section that touches tool-owned transient state."""
        kept: list[str] = []
        keeping = True
        for line in text.splitlines(keepends=True):
            if line.startswith("diff --git "):
                paths = line[len("diff --git "):].replace('"', " ").split()
                keeping = not any(cls._is_tool_state_diff_path(path) for path in paths)
            if keeping:
                kept.append(line)
        return "".join(kept)

    def _write_patch(self, record: JobRecord, before: dict[str, str]) -> None:
        workspace = Path(record.workspace_path)
        parts: list[str] = []
        for args in (
            ["git", "-C", str(workspace), "diff", "--binary", "--no-ext-diff", "--", *record.allowed_paths],
            ["git", "-C", str(workspace), "diff", "--cached", "--binary", "--no-ext-diff", "--", *record.allowed_paths],
        ):
            result = subprocess.run(
                args,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=60,
            )
            if result.stdout:
                parts.append(self._strip_tool_state_diff(result.stdout))
        new_untracked = [
            path for path in record.changed_paths
            if path not in before
            and not self._is_tool_state_path(path)
            and (workspace / path).is_file()
        ]
        for path in new_untracked:
            content = (workspace / path).read_text(encoding="utf-8", errors="replace")
            lines = content.splitlines()
            parts.append(
                f"diff --git a/{path} b/{path}\nnew file mode 100644\n--- /dev/null\n+++ b/{path}\n"
                f"@@ -0,0 +1,{len(lines)} @@\n"
                + "\n".join("+" + line for line in lines)
                + "\n"
            )
        (self._job_dir(record.job_id) / PATCH_FILE_NAME).write_text("\n".join(parts), encoding="utf-8")

    def cancel(self, job_id: str) -> JobRecord:
        """Stop a job, or refuse rather than publish a state the worker will outlive.

        A job this process started is stopped through its manager-owned token:
        the token is set first, so the decision is visible to the job thread even
        when the runner has not registered the job yet, and the runner is then
        asked to stop whatever it holds, which closes a live direct request or
        stops a CodeWhale child. A job this process does not hold is either
        stopped by its recorded worker pid or refused, and the refusal is explicit
        because marking a job CANCELLED while its loop still edits the workspace
        would be a false record. A job whose owning process is gone cannot be
        running at all, so it is recorded as interrupted and its workspace lock is
        released instead.
        """
        record = self.get(job_id)
        token = self._owned_cancellation(job_id)
        if token is not None:
            token.cancel()
            self.runner.cancel(job_id)
        elif not self.runner.cancel(job_id):
            self._stop_or_refuse_a_foreign_job(record)
        # The publish is one locked step, so it cannot interleave with the job
        # thread's own RUNNING transition: whichever side wins, the CANCELLED
        # decision is never overwritten.
        with self._lock:
            latest = self.get(job_id)
            if latest.state in LIVE_JOB_STATES:
                latest.state = JobState.CANCELLED
                latest.finished_at = utc_now()
                latest.error = CANCELLED_JOB_ERROR
                self._save(latest)
            return latest

    def _stop_or_refuse_a_foreign_job(self, record: JobRecord) -> JobRecord:
        """Handle a live job whose worker this process does not hold in memory.

        A child-backed worker is stopped by the pid the record names, exactly as
        before. An in-process worker has no pid any other process may signal: when
        its owning manager is still running, this process refuses with the owner
        named, and when that owner is gone, nothing can still be running, so the
        job is marked interrupted here and its workspace lock is released.
        """
        if record.state not in LIVE_JOB_STATES:
            return record
        if self._recorded_worker_ownership(record) == WORKER_OWNERSHIP_IN_PROCESS:
            owner = self._recorded_owner_pid(record)
            if isinstance(owner, int) and self._pid_is_running(owner):
                raise JobOwnershipError(
                    f"Job {record.job_id} runs in-process inside manager process {owner}, which holds "
                    "its loop: cancel it from the manager that started it, or wait for it to finish. "
                    "This process will not signal a manager process or publish a cancelled job whose "
                    "work may continue.",
                    job_id=record.job_id,
                    pid=owner,
                )
            self._mark_interrupted(record)
            return self.get(record.job_id)
        if isinstance(record.pid, int) and record.pid == os.getpid():
            # A recorded worker pid never names this process, so this is not a
            # worker to stop: signalling it would end the process that owns the
            # job instead of the job.
            raise JobOwnershipError(
                f"Job {record.job_id} records pid {record.pid}, which is this process, "
                "not a worker: cancel it from the process that owns its worker.",
                job_id=record.job_id,
                pid=record.pid,
            )
        if record.pid:
            self.runner.cancel_pid(record.pid)
        return record

    def _mark_interrupted(self, record: JobRecord) -> None:
        """Record a job whose owning process is gone, and free its workspace."""
        record.state = JobState.INTERRUPTED
        record.finished_at = utc_now()
        record.error = ORPHANED_JOB_ERROR
        self._save(record)
        self._release_workspace_lock(record.workspace_path, record.job_id)

    def read_diff(self, job_id: str, max_bytes: int) -> str:
        self.get(job_id)
        path = self._job_dir(job_id) / PATCH_FILE_NAME
        if not path.is_file():
            return ""
        data = path.read_bytes()
        if len(data) <= max_bytes:
            return data.decode("utf-8", errors="replace")
        return data[:max_bytes].decode("utf-8", errors="replace") + "\n... truncated ..."

    def read_output(self, job_id: str, max_bytes: int = 60_000) -> str:
        self.get(job_id)
        path = self._job_dir(job_id) / OUTPUT_FILE_NAME
        if not path.is_file():
            return ""
        data = path.read_bytes()
        return data[-max_bytes:].decode("utf-8", errors="replace")

    def discard(self, job_id: str) -> None:
        record = self.get(job_id)
        if record.state in {JobState.QUEUED, JobState.RUNNING}:
            raise RuntimeError("Cancel the running job before you discard it.")
        shutil.rmtree(self._job_dir(job_id))
