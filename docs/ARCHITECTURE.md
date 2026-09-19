# Architecture

MCP Delgado uses one delegation core with two adapters.

```text
Frontier agent                         Human or script
      |                                      |
      v                                      v
 MCP server                               CLI
      |                                      |
      +---------------+----------------------+
                      |
                      v
              JobManager and schemas
                      |
          +-----------+------------+
          |                        |
          v                        v
   CodeWhale worker          Durable job store
          |                        |
          v                        v
   Workspace changes       result, output, patch
```

## Manager responsibilities

The manager validates the workspace and allowed paths.
It creates a durable job record before execution.
It starts CodeWhale with a scoped worker prompt.
It records worker output and workspace changes.
It runs approved validation commands without a shell.
It marks changes outside the allowed paths as policy failures.
It writes a patch for later review.

The manager does not commit or accept changes.

## MCP adapter

The MCP adapter starts asynchronous jobs.
The client polls compact status records.
The client loads full results and patches only when needed.
This pattern keeps the manager model context small.

## CLI adapter

The CLI `run` and `repair` commands wait for completion.
They return a nonzero exit code when the job does not succeed.
Inspection commands read the same durable records as MCP.

## Durable state

The default state path is `~/.mcp-delgado/jobs`.
Each job has a unique directory.
The directory can contain these files:

* `job.json` stores the validated job record.
* `worker-output.txt` stores the bounded output tail.
* `changes.diff` stores the review patch.

Set `MODEL_WORKER_STATE_DIR` to move this state.

## Process recovery

A running record contains the CodeWhale process ID.
A new manager preserves a record when that process still exists.
It marks an abandoned record as `interrupted`.

## Trust boundary

Allowed paths are an audit rule after execution.
They do not create an operating system security boundary.
The CodeWhale sandbox supplies the primary execution limit.
Use a disposable Git worktree for stronger isolation.
