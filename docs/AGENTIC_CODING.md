# MCP Delgado for Codex and Claude Code

MCP Delgado lets a frontier coding agent delegate bounded implementation work
while retaining architecture, security, final review, commit, push, merge, and
deployment authority.

Use Delgado for leaf tasks with one concrete outcome, narrow allowed paths,
observable acceptance criteria, and exact validation commands. Keep architecture
decisions, security decisions, ambiguous product behavior, migrations,
deployment, and final acceptance with the managing agent.

Delgado is a control layer, not an operating-system security boundary. Use an
isolated or disposable worktree when a task has meaningful risk. On Windows,
assume the worker is unsandboxed unless an active boundary has been verified.

## Connect an agent

Delgado exposes a stdio MCP server. Configure the MCP client to launch:

* Command: the Python executable from the Delgado environment.
* Arguments: `-m mcp_delgado.server`.
* Working directory: the MCP Delgado checkout.
* Environment: at minimum, the same `MODEL_WORKER_STATE_DIR` that operator CLI
  commands will use.

The installed `mcp-delgado` command is an equivalent server entry point.

Restart the MCP client after changing its server configuration or runner
environment. The MCP host reads `MODEL_WORKER_RUNNER` when it starts.

Codex and Claude Code use the same Delgado tools and workflow after the stdio
server is connected. Their MCP configuration syntax is client-owned; Delgado
does not implement separate protocols or tool sets for the two clients.

## Safe MCP workflow

1. Inspect the target workspace and choose a bounded leaf task.
2. Call `model_worker_delegate_task` with one concrete `task`, the absolute
   `workspace_path`, narrow `allowed_paths`, observable `acceptance_criteria`,
   exact `required_commands`, and an appropriate `max_minutes`.
3. Save the returned job ID.
4. Poll `model_worker_job_status` while the state is `queued` or `running`.
5. When the job is terminal, call `model_worker_job_result`.
6. Reject work with policy violations or failed validations.
7. Always call `model_worker_read_job_diff` and inspect the patch before
   accepting, committing, or building on the changes.
8. If the patch needs correction, call `model_worker_repair_task` with exact,
   actionable findings. A repair reuses the original workspace, paths, checks,
   provider, and model.
9. Repeat result and diff review for the repair job.
10. Run any additional manager-selected checks warranted by the risk, then make
    the final acceptance decision.

Use `model_worker_cancel_job` to stop a queued or running job. Cancellation does
not revert file changes: inspect the result, stored diff, and workspace afterward.

`model_worker_review` is read-only and is appropriate for a focused review
request. Reviews currently require the CodeWhale runner.

Only one implementation job may hold a workspace at a time. Use another
worktree when independent jobs need to run concurrently.

## CLI workflow

The CLI and MCP server use the same manager, records, policy checks, patches, and
workspace locks. `delgado run` and `delgado repair` remain attached until
completion. Their job ID and state directory are printed when the job starts, so
another terminal can inspect or cancel the job through the same store.

### Windows PowerShell

```powershell
$delgadoStore = Join-Path $env:USERPROFILE ".mcp-delgado"

delgado run "Add parser tests for empty input" `
  --workspace "C:\Code\example" `
  --allow "src/parser.py" `
  --allow "tests/test_parser.py" `
  --accept "Empty input returns a validation error" `
  --check "python -m pytest tests/test_parser.py" `
  --state-dir $delgadoStore
```

From another terminal:

```powershell
delgado status JOB_ID --state-dir $delgadoStore
delgado result JOB_ID --state-dir $delgadoStore
delgado diff JOB_ID --state-dir $delgadoStore
delgado repair JOB_ID "Fix the null case. Do not change the public API." --state-dir $delgadoStore
delgado cancel JOB_ID --state-dir $delgadoStore
```

Read-only review:

```powershell
delgado review "Find correctness defects in the current diff" `
  --workspace "C:\Code\example" `
  --state-dir $delgadoStore
```

### POSIX shell

```sh
delgado_store="$HOME/.mcp-delgado"

delgado run "Add parser tests for empty input" \
  --workspace "/path/to/example" \
  --allow "src/parser.py" \
  --allow "tests/test_parser.py" \
  --accept "Empty input returns a validation error" \
  --check "python -m pytest tests/test_parser.py" \
  --state-dir "$delgado_store"
```

From another terminal:

```sh
delgado status JOB_ID --state-dir "$delgado_store"
delgado result JOB_ID --state-dir "$delgado_store"
delgado diff JOB_ID --state-dir "$delgado_store"
delgado repair JOB_ID "Fix the null case. Do not change the public API." --state-dir "$delgado_store"
delgado cancel JOB_ID --state-dir "$delgado_store"
```

## Choose a runner

`MODEL_WORKER_RUNNER` accepts:

* Unset, empty, or `codewhale`: the default CodeWhale runner.
* `deepseek-api`: the opt-in direct DeepSeek Responses API runner.

Use CodeWhale when the task needs its tool approvals, sandbox modes,
process-tree supervision, or read-only review.

Use `deepseek-api` for narrowly path-scoped implementation work where its
smaller inspectable loop is desirable. It exposes only five workspace file
operations and has no shell, command tool, or Git access. The manager still runs
validation commands and performs the scope audit.

The direct runner requires `DEEPSEEK_API_KEY` in the job-owning process
environment and an API model ID such as `deepseek-chat`. The key is read at job
start, removed from validation subprocess environments, and never stored.

The MCP host reads runner selection once at startup. Restart the host after
changing it. Each CLI invocation reads the current environment independently.

The direct runner uses low reasoning effort and a 32-turn limit. It preserves
reasoning protocol items between tool calls, without adding them to job logs.
Repair jobs include the original task and the new findings. Stored patches include
changed untracked files as full-file additions, including files present before the job.
These additions show the final file, not a job-relative delta. Inspect existing
user changes separately when reviewing a dirty workspace.

A direct job runs inside its manager process. Cancel it through the same MCP host
or owning CLI process. Cancellation from a second process is refused rather than
signalling the manager. CodeWhale child-process jobs support cross-process
cancellation through the shared job store.

## State directory

Keep `MODEL_WORKER_STATE_DIR`, and every explicit `--state-dir`, outside all
delegated workspaces. Delgado rejects a submit whose job store equals or is
nested below the target workspace.

Use one stable store for an MCP host and all CLI inspection commands that need to
see its jobs. The default is `~/.mcp-delgado`.

## Project instructions

To make another local project consistently use this guide, add the following to
its agent instruction file. For Codex, put it in `AGENTS.md`. For Claude Code,
put the same text in `CLAUDE.md`.

```markdown
## MCP Delgado

Before delegating implementation or review work, read the MCP Delgado operator
guide at `C:\Users\fastr\Code\mcp-delgado\docs\AGENTIC_CODING.md`.

Use Delgado only for bounded leaf tasks with one outcome, narrow allowed paths,
acceptance criteria, and exact checks. Keep architecture, security, migrations,
deployment, and final acceptance with the managing agent. Review the Delgado
result and stored diff before accepting worker changes.
```

Use an absolute local path unless the Delgado checkout has a stable
repository-relative location shared by every developer and agent.

## Acceptance checklist

Before accepting delegated work, confirm:

* The job reached `succeeded`.
* `policy_violations` is empty.
* All required validation results passed.
* Every changed path is intended.
* The stored diff matches the requested outcome.
* No unrelated existing workspace changes were overwritten.
* Any additional manager-selected checks passed.
* The managing agent, not the worker, makes the final commit or deployment
  decision.
