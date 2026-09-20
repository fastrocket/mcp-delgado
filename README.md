# MCP Delgado

MCP Delgado keeps an expensive frontier agent thin.
The frontier agent plans, delegates, reviews, and decides.
A fast worker model handles bounded implementation work.

DeepSeek Flash is the default worker model.
[CodeWhale](https://github.com/fastrocket/codewhale) supplies the agent loop and provider profiles.
MCP Delgado supplies scope control, durable records, validation, and review tools.

The project includes two interfaces over one core:

* An MCP server for agent-driven delegation.
* A CLI for operators, scripts, and direct inspection.

## Why Delgado?

A frontier model should spend its attention on high-value decisions.
It should not use premium tokens for every search, edit, or test run.

MCP Delgado moves suitable leaf tasks to a fast model.
It keeps the manager responsible for architecture and final review.
The worker cannot commit, push, merge, deploy, or access production by instruction.
The manager records the result and audits the changed paths.

This design reduces cost without hiding authority inside an autonomous swarm.

## What it does

MCP Delgado can:

* Run bounded coding tasks in a Git workspace.
* Limit each task to named paths or path patterns.
* Run approved validation commands without a shell.
* Store job state, worker output, and a review patch.
* Detect changes outside the allowed scope.
* Keep one implementation job per workspace with a durable lock.
* Ignore CodeWhale's own `.codewhale` state in the audit and the patch.
* Start a repair job with exact manager feedback.
* Run a read-only model review.
* Use any provider and model that CodeWhale supports.
* Opt in to a direct DeepSeek Responses API worker (`MODEL_WORKER_RUNNER=deepseek-api`) that has five bounded workspace file tools and no shell.

MCP Delgado does not accept or merge work automatically.
The manager must inspect each result.

## Requirements

Install these tools first:

* Python 3.11 or newer.
* Git.
* CodeWhale with a configured provider profile.

Do not store API keys in this repository.
Store each key in its CodeWhale provider profile.
The optional direct backend reads `DEEPSEEK_API_KEY` from the environment of the
process that runs the job, and never stores it.

## Install

```powershell
git clone https://github.com/fastrocket/mcp-delgado.git
cd mcp-delgado
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
```

Check the worker provider:

```powershell
codewhale config doctor
codewhale model resolve deepseek-flash
delgado --help
```

## MCP setup

Add this server to the MCP client configuration:

```toml
[mcp_servers.mcp_delgado]
command = "C:\\path\\to\\mcp-delgado\\.venv\\Scripts\\python.exe"
args = ["-m", "mcp_delgado.server"]
cwd = "C:\\path\\to\\mcp-delgado"
startup_timeout_sec = 20
tool_timeout_sec = 60
required = false
```

Restart the MCP client after you save the configuration.
Delegated jobs run asynchronously while the MCP server stays active.

### MCP workflow

1. Call `model_worker_delegate_task` with a narrow task.
2. Poll `model_worker_job_status` until the job stops.
3. Call `model_worker_job_result` for checks and policy results.
4. Call `model_worker_read_job_diff` before accepting changes.
5. Call `model_worker_repair_task` with exact findings when needed.

The MCP server also exposes `model_worker_review` for read-only analysis.
Reviews run on the server's event loop and own their CodeWhale process tree.
If the caller cancels the tool call, or the review passes `max_minutes`, the whole
tree is stopped before the call returns.
Cancellation propagates as a cancelled tool call; a timeout returns
`{"error": "ReviewTimeoutError", "timed_out": true, "output": ..., "diagnostics_tail": ...}`
with whatever the review printed before it was stopped.
A completed review still returns `exit_code`, `output`, and `diagnostics_tail`.

### One implementation job per workspace

An implementation job holds a durable lock on its workspace.
The lock lives in `MODEL_WORKER_STATE_DIR/locks`, so separate MCP server
processes that share one state directory see the same claim.
A second submit is refused quickly instead of editing the same checkout:

```json
{"error": "WorkspaceBusyError", "detail": "Workspace C:\\Code\\example is already running job JOB_ID (pid 1234). ..."}
```

Jobs in different workspaces still run at the same time.
The lock is released when the job succeeds, fails, times out, or is cancelled.
A lock left behind by a stopped process is recovered only when nothing it names
is still running.
A terminal or cancelled record does not free a workspace by itself: the owning
job thread keeps the checkout until it releases the lock, so a second submit
stays busy while a cancelled job is still returning from its stopped worker.

### Tool-owned state

CodeWhale creates and updates `.codewhale` inside the target repository.
A pytest validation run also writes transient state into the checkout:
the root `.pytest_cache` directory and any `.pytest-*` basetemp tree.
Delgado treats all of it as tool-owned:

* It never appears in `changed_paths`.
* It never raises a policy failure.
* It never enters the stored patch.

Callers do not need to list `.codewhale` in the allowed paths.
Only worker changes outside the allowed paths cause `policy_failed`.

The job store is not tool-owned state.
Keep `MODEL_WORKER_STATE_DIR` (and any `--state-dir`) outside every workspace you
delegate: a store inside a checkout is audited like worker output, so its records
would show up as changed paths, enter the stored patch, and put the records that
judge a job inside the paths that job may edit.
Delgado enforces this: a submit whose store is the delegated workspace or sits
below it is refused before any job record, lock, or worker exists, and the error
names `MODEL_WORKER_STATE_DIR` and `--state-dir`.

The exclusion stays narrow.
`.codewhale` matches at any depth, but the pytest names match only at the
repository root and only as the exact `.pytest_cache` name or a `.pytest-*`
tree.
A real source path such as `src/.pytest-cache/`, `.pytest_cache_helper/`, or
`pytest-c0/` is still audited and still fails policy when it falls outside the
allowed paths.

## CLI

The CLI uses the same manager, schemas, policy checks, and job store.
The `run` command stays attached until the worker completes.
This behavior makes terminal use clear and reliable.

```powershell
delgado run "Add parser tests for empty input" `
  --workspace C:\Code\example `
  --allow src/parser.py `
  --allow tests/test_parser.py `
  --accept "Empty input returns a validation error" `
  --check "python -m pytest tests/test_parser.py"
```

Review a workspace without allowing edits:

```powershell
delgado review "Find correctness defects in the current diff" --workspace C:\Code\example
```

The CLI runs the same review code path, so Ctrl+C stops the review process tree
instead of leaving CodeWhale behind.
A refused submit, such as a job in the same workspace, prints the reason and
exits with code 2.

Inspect a prior job:

```powershell
delgado status JOB_ID
delgado result JOB_ID
delgado diff JOB_ID
```

Request a focused repair:

```powershell
delgado repair JOB_ID "Fix the null case. Do not change the public API."
```

### One durable job store, one job engine

Every command accepts `--state-dir DIR`, before or after the subcommand.
It overrides `MODEL_WORKER_STATE_DIR` and points the invocation at an exact
store:

```powershell
delgado --state-dir $env:USERPROFILE\.mcp-delgado status JOB_ID
delgado status JOB_ID --state-dir $env:USERPROFILE\.mcp-delgado
```

Use the same value as the MCP host to inspect the job the server is running.
Keep that store outside every delegated workspace: the audit belongs to the jobs,
and manager state inside a checkout would be reported as worker-changed files.
`run` and `repair` stay in the foreground and keep owning their worker process,
so a second terminal can read the same durable records:

```powershell
delgado status JOB_ID --state-dir $env:USERPROFILE\.mcp-delgado
delgado result JOB_ID --state-dir $env:USERPROFILE\.mcp-delgado
delgado diff JOB_ID --state-dir $env:USERPROFILE\.mcp-delgado
delgado cancel JOB_ID --state-dir $env:USERPROFILE\.mcp-delgado
```

The CLI and the MCP server share one job engine and one store, including the
per-workspace lock. A submit is refused while the other interface holds the
workspace.

### Hot-reload a live checkout

To exercise edits to this checkout without restarting the MCP host, run the
checkout's own source against the shared store.
`PYTHONPATH` makes Python import `src/` directly;
the next invocation therefore imports the current code:

```powershell
$env:PYTHONPATH = "C:\Code\mcp-delgado\src"
C:\Code\mcp-delgado\.venv\Scripts\python.exe -m mcp_delgado.cli run `
  "Add parser tests for empty input" `
  --workspace C:\Code\example `
  --allow src/parser.py `
  --state-dir $env:USERPROFILE\.mcp-delgado
```

The command blocks until the worker finishes and prints the full record,
including the state directory it used.
From a second terminal, read or cancel that job in the same store:

```powershell
$env:PYTHONPATH = "C:\Code\mcp-delgado\src"
C:\Code\mcp-delgado\.venv\Scripts\python.exe -m mcp_delgado.cli status JOB_ID `
  --state-dir $env:USERPROFILE\.mcp-delgado
```

No MCP host restart is needed: the invocation imports `src/mcp_delgado` from
the checkout instead of the installed copy.
The MCP host keeps serving its long-running tools while the CLI works.

The same invocation also reads `MODEL_WORKER_RUNNER` (and `DEEPSEEK_API_KEY` for
the direct backend) from the shell that runs it. The CLI builds its manager per
command, so an edited runner or a changed selector is live on the next CLI
command, even while the MCP host keeps the environment it started with.

Probe the MCP transport directly:

```powershell
python scripts/mcp_probe.py model_worker_job_status `
  '{"params":{"job_id":"JOB_ID"}}'
```

The probe reports process, initialization, tool, and total timing.

## Configuration

The defaults use the `deepseek` provider and `deepseek-flash` model.

| Variable | Purpose | Default |
| --- | --- | --- |
| `MODEL_WORKER_RUNNER` | Worker backend: `codewhale` or `deepseek-api` | `codewhale` |
| `MODEL_WORKER_PROVIDER` | CodeWhale provider profile | `deepseek` |
| `MODEL_WORKER_MODEL` | Worker model | `deepseek-flash` |
| `MODEL_WORKER_CODEWHALE` | CodeWhale executable path | PATH lookup |
| `MODEL_WORKER_STATE_DIR` | Durable job directory | `~/.mcp-delgado` |
| `MODEL_WORKER_TIMEOUT_SECONDS` | Validation command limit | `1800` |
| `DEEPSEEK_API_KEY` | Key for the direct backend, read when a job starts | unset |

Command options override the provider and model defaults.
`--state-dir DIR` on any CLI command overrides `MODEL_WORKER_STATE_DIR`.

### Direct DeepSeek Responses API worker (opt-in)

CodeWhale stays the default harness. With `MODEL_WORKER_RUNNER=deepseek-api`,
an implementation job runs directly against the DeepSeek Responses API instead:

```powershell
$env:MODEL_WORKER_RUNNER = "deepseek-api"
$env:MODEL_WORKER_MODEL = "deepseek-chat"   # an API model id, not a CodeWhale alias
$env:DEEPSEEK_API_KEY = "..."               # read when a job starts, never stored
delgado run "Add parser tests for empty input" `
  --workspace C:\Code\example `
  --allow tests/test_parser.py `
  --check "python -m pytest tests/test_parser.py"
```

The direct worker has exactly five file tools — `list_files`, `search_text`,
`read_file`, `replace_text`, and `write_file` — all confined to the workspace and
checked against the job's allowed paths inside the loop. It has no shell, no
command tool, and no git. The manager still runs your `--check` commands, audits
every changed path, stores the patch, and owns the terminal state, so the review
workflow above is unchanged.

The loop is bounded in model turns, tool calls, tool output, response bytes, and
time, and it stops cleanly on a malformed, failed, incomplete, over-budget, timed
out, or cancelled answer. The key is sent only as an `Authorization` header and
is never written to the job store; a missing `DEEPSEEK_API_KEY` fails the job with
a clear message before any request is made. Reviews still require the CodeWhale
runner.

Your `--check` commands run without that key. Each validation command is started
with a copy of the manager's environment minus `DEEPSEEK_API_KEY`, and whatever it
captured is redacted against that name before it is stored, so a delegated check
cannot read the worker credential and a tool that prints its environment cannot
write it into a job record.

A direct job belongs to the manager process that started its loop, and the job
store says so: the record's `pid` stays empty and `runner.json` names that
process instead. Cancelling from the owning process sets that job's token;
cancelling from another process returns `JobOwnershipError` rather than stopping
the manager, and if the owning process dies, the next manager start marks the job
`interrupted` and frees its workspace lock.

The manager owns cancellation, not the runner. Each job gets a token before its
thread starts, so a job cancelled the instant it was submitted never sends a
request, never writes, and never runs a check: its record is `cancelled` and
`running` is never written over that decision. A request that is still waiting for
response headers cannot be interrupted by that token; it ends at its own request
timeout (300 seconds by default), and the job is cancelled once it does.

`MODEL_WORKER_RUNNER` is read when a process starts:

* The MCP host reads it once at server startup. Restart the host after you change
  it, and check the host's own environment: the server does not inherit a variable
  you set in another terminal.
* The CLI reads it on every command, and the hot-reload command above reads it
  from the shell that runs it. A live checkout therefore uses an edited backend or
  a changed selector immediately, with no MCP restart.

An unknown value stops the process with a clear error naming `codewhale` and
`deepseek-api`, instead of quietly running the default backend.

## Recommended operating model

Use the frontier agent as the manager.
Delegate only work that has a clear boundary and a testable result.

Good tasks include:

* Add tests for one module.
* Implement one small service behind an existing interface.
* Trace a defect and return evidence.
* Apply a mechanical change within named files.
* Review a diff for one risk class.

Keep these tasks with the frontier agent:

* Architecture choices.
* Security decisions.
* Ambiguous product behavior.
* Cross-system migrations.
* Final diff review and deployment.

Use MCP during an active agent session.
Use the CLI for human operation, scripts, debugging, and CI experiments.
Do not maintain separate MCP and CLI implementations.
Both interfaces must continue to call the shared manager.

See [Operating model](docs/OPERATING_MODEL.md) for the full policy.
See [Architecture](docs/ARCHITECTURE.md) for internal design details.
See [Runner choices](docs/RUNNERS.md) for CodeWhale and direct API guidance.
See [Agentic coding guide](docs/AGENTIC_CODING.md) for Codex, Claude Code, MCP,
and CLI operating workflows.
See [Launch content](docs/LAUNCH_CONTENT.md) for social posts and a video script.

## Safety model

MCP Delgado adds controls, but it is not a security boundary.
The worker runs on the local machine and can edit the workspace.
Use an isolated worktree when the task has meaningful risk.

The current controls include:

* A workspace-write sandbox request for implementation jobs.
* A read-only sandbox request for review jobs.
* Explicit allowed paths.
* A durable lock that allows one implementation job per workspace.
* A post-run changed-path audit that ignores tool-owned `.codewhale` state.
* A validation command allowlist.
* No shell execution for validation commands.
* Durable output and patch records.
* A direct backend whose worker sees five bounded file tools, no shell, and no command tool, and whose key is read at run time and redacted from everything it reports.

Review the stored patch before you commit any worker change.
Sandbox enforcement depends on the CodeWhale build and operating system.
Treat Windows execution as unsandboxed unless you verify an active boundary.
Use a container, virtual machine, or disposable worktree when stronger isolation matters.

## Development

```powershell
python -m pip install -e ".[dev]"
python -m pytest
python -m compileall src tests
```

Keep the core independent from the transport adapters.
Add core behavior to `manager.py` and `schemas.py`.
Keep MCP-specific behavior in `server.py`.
Keep terminal formatting and argument parsing in `cli.py`.

## License

This repository does not yet include an open-source license.
Public access does not grant reuse rights by itself.
Choose a license before you invite outside contributions.
