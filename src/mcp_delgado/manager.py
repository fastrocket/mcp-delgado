"""Durable process manager for scoped Delgado jobs."""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import signal
import shlex
import shutil
import subprocess
import threading
import uuid
from pathlib import Path
from typing import Callable, Optional

from .schemas import DelegateTaskInput, JobRecord, JobState, ReviewInput, utc_now

DEFAULT_PROVIDER = "deepseek"
DEFAULT_MODEL = "deepseek-flash"
DEFAULT_TIMEOUT_SECONDS = 30 * 60
MAX_OUTPUT_BYTES = 2_000_000
PATCH_FILE_NAME = "changes.diff"
OUTPUT_FILE_NAME = "worker-output.txt"
VALIDATION_EXECUTABLES = frozenset({
    "cargo", "go", "mypy", "node", "npm", "npm.cmd", "npx", "npx.cmd",
    "pnpm", "py", "py.exe", "pytest", "pytest.exe", "python", "python.exe",
    "python3", "ruff", "yarn",
})
PYTHON_VALIDATION_MODULES = frozenset({"compileall", "mypy", "py_compile", "pytest", "ruff"})
PYTHON_EXECUTABLES = frozenset({"py", "py.exe", "python", "python.exe", "python3"})
SHELL_CONTROL_TOKENS = frozenset({"&", "&&", "|", "||", ";", "<", ">", ">>"})


def codewhale_executable() -> str:
    configured = os.environ.get("MODEL_WORKER_CODEWHALE")
    if configured:
        return configured
    return shutil.which("codewhale.cmd" if os.name == "nt" else "codewhale") or "codewhale"


class JobManager:
    def __init__(
        self,
        state_dir: Optional[Path] = None,
        command_runner: Optional[Callable[..., subprocess.CompletedProcess[str]]] = None,
    ) -> None:
        configured = os.environ.get("MODEL_WORKER_STATE_DIR")
        self.state_dir = (state_dir or Path(configured or Path.home() / ".mcp-delgado")).resolve()
        self.jobs_dir = self.state_dir / "jobs"
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self.command_runner = command_runner or subprocess.run
        self._lock = threading.RLock()
        self._processes: dict[str, subprocess.Popen[str]] = {}
        self._mark_stale_jobs()

    def _mark_stale_jobs(self) -> None:
        for record_path in self.jobs_dir.glob("*/job.json"):
            try:
                record = JobRecord.model_validate_json(record_path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if record.state not in {JobState.QUEUED, JobState.RUNNING}:
                continue
            if record.pid and self._pid_is_running(record.pid):
                continue
            record.state = JobState.INTERRUPTED
            record.finished_at = utc_now()
            record.error = "The MCP server stopped before this job finished."
            self._save(record)

    @staticmethod
    def _pid_is_running(pid: int) -> bool:
        try:
            os.kill(pid, 0)
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
        temporary.replace(target)

    def get(self, job_id: str) -> JobRecord:
        target = self._job_dir(job_id) / "job.json"
        if not target.is_file():
            raise KeyError(f"Unknown job: {job_id}")
        return JobRecord.model_validate_json(target.read_text(encoding="utf-8"))

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
    def _status(workspace: Path, allowed_paths: Optional[list[str]] = None) -> dict[str, str]:
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

    def submit(self, params: DelegateTaskInput, parent_job_id: Optional[str] = None) -> JobRecord:
        workspace = self._workspace(params.workspace_path)
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
        self._save(record)
        before = self._status(workspace, allowed_paths)
        thread = threading.Thread(target=self._run_job, args=(record.job_id, before), daemon=True)
        thread.start()
        return record

    def review(self, params: ReviewInput) -> dict:
        workspace = self._workspace(params.workspace_path)
        provider = params.provider or os.environ.get("MODEL_WORKER_PROVIDER", DEFAULT_PROVIDER)
        model = params.model or os.environ.get("MODEL_WORKER_MODEL", DEFAULT_MODEL)
        command = [
            codewhale_executable(),
            "--provider", provider,
            "--model", model,
            "--approval-policy", "never",
            "--sandbox-mode", "read-only",
            "--fresh",
            "-C", str(workspace),
            "exec",
            "--auto",
            "--json",
            params.request,
        ]
        result = subprocess.run(
            command,
            cwd=str(workspace),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=params.max_minutes * 60,
        )
        output = "\n".join(part for part in (result.stdout, result.stderr) if part)
        return {"exit_code": result.returncode, "output": output[-50_000:]}

    def _run_job(self, job_id: str, before: dict[str, str]) -> None:
        record = self.get(job_id)
        workspace = Path(record.workspace_path)
        record.state = JobState.RUNNING
        record.started_at = utc_now()
        self._save(record)

        executable = codewhale_executable()
        command = [
            executable,
            "--provider",
            record.provider,
            "--model",
            record.model,
            "--approval-policy",
            "never",
            "--sandbox-mode",
            "workspace-write",
            "--fresh",
            "-C",
            str(workspace),
            "exec",
            "--auto",
            "--json",
            self._prompt(record),
        ]
        output_path = self._job_dir(job_id) / OUTPUT_FILE_NAME
        try:
            process = subprocess.Popen(
                command,
                cwd=str(workspace),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=os.environ.copy(),
            )
            with self._lock:
                self._processes[job_id] = process
            record.pid = process.pid
            self._save(record)
            try:
                output, _ = process.communicate(timeout=record.max_minutes * 60)
            except subprocess.TimeoutExpired:
                self._terminate_process(process)
                output, _ = process.communicate()
                record.error = f"The worker exceeded {record.max_minutes} minutes."
                record.state = JobState.FAILED
            output_path.write_text(output[-MAX_OUTPUT_BYTES:], encoding="utf-8")
            record.exit_code = process.returncode
            if record.state == JobState.RUNNING:
                record.state = JobState.SUCCEEDED if process.returncode == 0 else JobState.FAILED
            record.summary = self._compact_summary(output)
        except Exception as exc:
            record.state = JobState.FAILED
            record.error = f"{type(exc).__name__}: {exc}"
        finally:
            with self._lock:
                self._processes.pop(job_id, None)

        after = self._status(workspace, record.allowed_paths)
        changed = sorted(path for path, status in after.items() if before.get(path) != status)
        record.changed_paths = changed
        record.policy_violations = [
            path for path in changed if not self._is_allowed(path, record.allowed_paths)
        ]
        if record.policy_violations and record.state == JobState.SUCCEEDED:
            record.state = JobState.POLICY_FAILED

        record.validation_results = self._run_validations(record)
        if any(item["exit_code"] != 0 for item in record.validation_results) and record.state == JobState.SUCCEEDED:
            record.state = JobState.FAILED
        self._write_patch(record, before)
        record.finished_at = utc_now()
        self._save(record)

    @staticmethod
    def _compact_summary(output: str) -> str:
        cleaned = output.strip()
        if not cleaned:
            return "The worker returned no output."
        lines = cleaned.splitlines()
        return "\n".join(lines[-80:])[-20_000:]

    def _run_validations(self, record: JobRecord) -> list[dict]:
        results: list[dict] = []
        timeout = int(os.environ.get("MODEL_WORKER_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS))
        for command in record.required_commands:
            try:
                args = self._validation_args(command)
                completed = self.command_runner(
                    args,
                    cwd=record.workspace_path,
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
                    "output_tail": combined[-20_000:],
                })
            except Exception as exc:
                results.append({
                    "command": command,
                    "exit_code": -1,
                    "output_tail": f"{type(exc).__name__}: {exc}",
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

    @classmethod
    def _validate_commands(cls, commands: list[str]) -> None:
        for command in commands:
            cls._validation_args(command)

    @staticmethod
    def _terminate_process(process: subprocess.Popen[str]) -> None:
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

    @staticmethod
    def _terminate_pid(pid: int) -> None:
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
                parts.append(result.stdout)
        new_untracked = [
            path for path in record.changed_paths
            if path not in before and (workspace / path).is_file()
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
        record = self.get(job_id)
        with self._lock:
            process = self._processes.get(job_id)
        if process and process.poll() is None:
            self._terminate_process(process)
        elif record.pid and record.state in {JobState.QUEUED, JobState.RUNNING}:
            self._terminate_pid(record.pid)
        if record.state in {JobState.QUEUED, JobState.RUNNING}:
            record.state = JobState.CANCELLED
            record.finished_at = utc_now()
            record.error = "The manager cancelled this job."
            self._save(record)
        return record

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
