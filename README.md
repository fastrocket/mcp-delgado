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
* Start a repair job with exact manager feedback.
* Run a read-only model review.
* Use any provider and model that CodeWhale supports.

MCP Delgado does not accept or merge work automatically.
The manager must inspect each result.

## Requirements

Install these tools first:

* Python 3.11 or newer.
* Git.
* CodeWhale with a configured provider profile.

Do not store API keys in this repository.
Store each key in its CodeWhale provider profile.

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
| `MODEL_WORKER_PROVIDER` | CodeWhale provider profile | `deepseek` |
| `MODEL_WORKER_MODEL` | Worker model | `deepseek-flash` |
| `MODEL_WORKER_CODEWHALE` | CodeWhale executable path | PATH lookup |
| `MODEL_WORKER_STATE_DIR` | Durable job directory | `~/.mcp-delgado` |
| `MODEL_WORKER_TIMEOUT_SECONDS` | Validation command limit | `1800` |

Command options override the provider and model defaults.

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
See [Launch content](docs/LAUNCH_CONTENT.md) for social posts and a video script.

## Safety model

MCP Delgado adds controls, but it is not a security boundary.
The worker runs on the local machine and can edit the workspace.
Use an isolated worktree when the task has meaningful risk.

The current controls include:

* A workspace-write sandbox request for implementation jobs.
* A read-only sandbox request for review jobs.
* Explicit allowed paths.
* A post-run changed-path audit.
* A validation command allowlist.
* No shell execution for validation commands.
* Durable output and patch records.

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
