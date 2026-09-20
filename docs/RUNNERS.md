# Runner choices

MCP Delgado ships two worker backends behind one seam:

* `CodeWhaleRunner` (default) drives the CodeWhale CLI, which provides the model
  loop, workspace tools, edit operations, tool approvals, and result formatting.
* `DirectDeepSeekRunner` (opt-in, `MODEL_WORKER_RUNNER=deepseek-api`) talks to the
  DeepSeek Responses API over HTTP and runs its own bounded tool loop over the
  workspace tools in this repository.

CodeWhale stays the default, so an existing installation never changes behavior
on upgrade. Both backends pass the same gates: job scope, durable state, path
audits, stored patches, manager-run validations, and terminal job state.

## Backend boundary

The runner boundary lives in [`src/mcp_delgado/runners.py`](../src/mcp_delgado/runners.py).
It is the only place that knows how a worker process is built and supervised.

The protocol is `WorkerRunner`:

* `run_task(run, on_start)` runs one scoped implementation job.
* `worker_ownership` declares how the worker is held: `child` for a separate
  process tree the manager may record a pid for and signal, `in-process` for a
  loop that runs on a thread inside the manager process, which has no pid any
  process may signal.
* `cancel(job_id)` stops a job process this manager process holds, and reports
  whether it held one.
* `cancel_pid(pid)` stops a job process recorded by another manager process.
* `review_command(run)`, `review_timeout_seconds(run)`,
  `spawn_review_process(...)`, and `kill_review_tree(...)` supply the review
  argv, the budget, and the process control.

The requests and the result are small frozen dataclasses:

* `TaskRun` carries the job id, the manager's prompt, the workspace, the
  provider, the model, and the minute budget.
* `ReviewRun` carries the request, the workspace, the provider, the model, and
  the minute budget.
* `RunnerResult` reports the worker output, the exit code, an error, and whether
  the runner stopped the worker for exceeding its budget.

`CodeWhaleRunner` is the default adapter, and `JobManager` constructs it when no
runner is injected and no selector is set:

```python
manager = JobManager(state_dir=store)                    # CodeWhaleRunner
manager = JobManager(state_dir=store, runner=MyRunner()) # any WorkerRunner
```

The default comes from `select_runner()`, which reads `MODEL_WORKER_RUNNER`:

| Value | Backend |
| --- | --- |
| unset, empty, or `codewhale` | `CodeWhaleRunner` |
| `deepseek-api` | `DirectDeepSeekRunner` |

Any other value raises `RunnerSelectionError` (a `ValueError`) naming both known
values. The refusal is deliberate: a silent fallback would run a job on a backend
the operator did not choose. An explicitly injected runner is never second-guessed
by the selector.

Runner construction is an in-process seam for tests and for a second backend.
No dependency was added for it, and the MCP and CLI input schemas are unchanged.

## What each side owns

The runner owns the worker process:

* Executable discovery and argv construction for implementation and review runs.
* Starting the implementation child and awaiting it with its minute budget.
* Starting the review child as its own process group.
* Stopping the complete process tree on Windows and POSIX.

The manager owns policy and durable state:

* Durable job records, the workspace lock, and the patch store.
* The before/after scope audit and the policy violation decision.
* Manager-run validation commands, including their executable allowlist, the
  environment they inherit, and the redaction of what they captured.
* One cancellation token per job, created before its thread starts and read by
  the runner and by validation dispatch.
* Repair lineage, cancellation entry points, and terminal-state decisions.
* Review supervision: streaming both pipes, enforcing the runner's budget, and
  stopping the tree on a timeout or a cancelled caller.

A runner never decides job state. `RunnerResult` is evidence, and the manager
turns it into a terminal state together with its own scope audit.

## Cancellation and timeouts

Implementation cancellation and review cancellation stop the full process tree:

* Windows uses `taskkill /PID <pid> /T /F` for the job child, for a pid recorded
  by another process, and for a review child.
* POSIX terminates the job child, signals a recorded pid, and signals the review
  child's process group, which exists because the review child is started as a
  session leader.

A cancelled review returns a cancelled tool call. A review that passes
`max_minutes` returns
`{"error": "ReviewTimeoutError", "timed_out": true, "output": ..., "diagnostics_tail": ...}`
with whatever the review printed before it was stopped.
The timeout message names the runner label, so a second backend is identifiable
in diagnostics without a schema change.

A direct job has no child process: the loop runs on a thread inside the manager
process, and the manager cancels it through the job's own token rather than
through a process handle.

The token is what makes the decision durable. The manager creates one for every
implementation job *before* its job thread starts, and `cancel(job_id)` sets it
and then publishes the terminal CANCELLED record; the thread's check before
RUNNING and that publish share one lock, so the operator's decision is never
overwritten. The token is read at three points:

* **Before the runner starts.** A job cancelled the instant it was submitted
  never reaches a transport and never runs a validation: the thread sees the set
  token, publishes CANCELLED, and does no work at all. RUNNING is never written
  over that decision, because the check and the RUNNING transition are one locked
  step, and they share that lock with the cancel.
* **Before every model turn.** A loop between turns stops without sending another
  request.
* **Before every validation command.** A cancelled job starts no command, because
  each one would run in the workspace the operator just took back.

What cancellation cannot do is interrupt a request that has not produced a
response yet. Closing the live response ends a request that is already receiving
bytes, but a connection that is still waiting for response headers is bounded by
the request timeout instead: the lesser of the job's remaining budget and the
configured `request_timeout_seconds` (300 seconds by default, 3600 at the
ceiling). A cancelled job can therefore take up to that bound to stop, and the
job record says `cancelled` once it does. A validation command that is already
running is not interrupted either: it ends on its own or at its timeout, and its
result is recorded.

That loop belongs to one process, and the durable state says so instead of
pretending the job has a worker pid. `DirectDeepSeekRunner` declares
`worker_ownership = "in-process"`, so the manager records the owning process in
`runner.json` as `owner_pid` and never in the record's `pid` field, which stays
the only field a cancel may signal. The consequences are deliberate:

* Cancelling from the process that started the loop sets the job's token, closes a
  response that is already open, and stops the loop before its next request. A
  request still waiting for response headers is bounded by its own timeout
  instead, as described under cancellation above.
* Cancelling from any other process raises `JobOwnershipError` naming the owning
  pid: no other process can stop that work, and recording CANCELLED while the loop
  still edits the workspace would be false. Nothing is signalled, because the
  recorded owner is a manager process and not a worker.
* When the owning process is gone, nothing can still be running, so the job is
  recorded as `interrupted` with the message startup recovery uses and its
  workspace lock is released.

The direct runner's `cancel_pid` refuses outright, and the CodeWhale adapter
refuses a pid that names the calling process, so no path turns a manager pid into
an `os.kill` or a `taskkill`.

## Runner identity

The job record forbids extra fields, so the manager writes the runner identity
to `runner.json` inside the job directory, beside `job.json`, `changes.diff`,
and `worker-output.txt`.
The file names the runner and the worker model when the job is submitted, and is
rewritten with the runner's usage counters when the job stops: model turns, tool
calls, and the token counters the service reported. Only the runner name, the
model, non-negative integer counters, and the ownership described below are
written — never a credential, a prompt, or a response body.

It also carries the ownership the runner declares (`child` or `in-process`) and,
for an in-process worker, the `owner_pid` of the manager process that runs the
loop. Startup recovery and `cancel` read that file, because the process that stops
a job is not necessarily configured with the runner that started it: a second
manager process on the default backend still sees that the job's worker is
in-process, leaves a live owner alone, and refuses to signal anything. A store
written before this field existed is read as a child-backed worker, which is the
CodeWhale behavior.
The public MCP and CLI payloads keep their shape, and no migration is required.
An in-process job therefore reports no `pid`, because there is no worker process
to name: the owning manager process is in `runner.json`.

The job store belongs outside every workspace you delegate. It is the manager's
own durable state and nothing excludes it from the audit, so a state directory
inside a checkout would appear in that job's changed paths, its records would
enter the stored patch as if the worker had written them, and a worker allowed to
touch the checkout could rewrite the records that judge it. `submit` therefore
refuses a job whose state directory is the delegated workspace or sits below it,
before any record, lock, or thread exists, and names `MODEL_WORKER_STATE_DIR`
(and `--state-dir`) in the refusal. A store anywhere outside the checkout is
accepted, including one that delegates the workspace root itself.

## Workspace tools for a direct runner

[`src/mcp_delgado/workspace_tools.py`](../src/mcp_delgado/workspace_tools.py)
holds the bounded file surface a direct-API worker will use. It is a standalone
class in this slice: no runner selects it, the manager does not construct it,
and the MCP and CLI schemas are unchanged.

```python
tools = WorkspaceTools(workspace, record.allowed_paths)
tools.call("read_file", {"path": "src/app/main.py"})
```

`workspace` is the checkout the manager already resolved. `allowed_paths` are
the manager-normalized patterns from the job record, and the tool layer applies
the same rule the post-job audit applies, so a write it accepts is a write the
audit also accepts. Configuration errors raise `ValueError`; a refusal raises a
`WorkspaceToolError` subclass, and `as_dict()` returns
`{"error": code, "message": ...}` with any detail the caller needs.

### Operations

| Operation | Arguments | Returns |
| --- | --- | --- |
| `list_files` | `path="."`, `max_results` | `path`, `entries` (`path`, `type`, file `size`), `count` |
| `search_text` | `query`, `path="."`, `max_matches`, `file_pattern`, `case_sensitive` | `matches` (`path`, `line`, `text`), `count`, `files_searched`, `bytes_scanned`, skipped counts |
| `read_file` | `path`, `max_bytes` | `path`, `text`, `bytes`, `lines`, `sha256` |
| `replace_text` | `path`, `old`, `new`, `expected_occurrences=1`, `expected_sha256` | `path`, `replacements`, `bytes`, `sha256` |
| `write_file` | `path`, `content`, `expected_sha256` | `path`, `created`, `bytes`, `sha256` |

`call(name, arguments)` runs one operation from a model-shaped argument object
and refuses an unknown name or an unexpected argument with a clear error,
`call_json(name, arguments)` takes the JSON text a model actually sends and
returns that same refusal as a structured mapping, `tool_definitions()` returns
the five operations as Responses-API function tools, and `tools` lists the five
names.

### Invariants

* Every path is repository-relative. Absolute, drive-qualified, UNC,
  home-relative, `:`-bearing, and NUL-bearing paths are refused before the
  filesystem is touched, and any `..` segment is refused even when it would
  resolve back inside the workspace.
* Every path is resolved through symlinks, junctions, and other reparse points,
  and the result must stay inside the workspace. A link that leaves the checkout
  is not listed, not walked, not read, and not written; an in-workspace link is
  followed and reported as the real path.
* A write must also match `allowed_paths`. Reads are workspace-wide apart from
  the exclusions below, because reading is how a worker learns context.
* An overwrite must carry the `expected_sha256` of the bytes it is replacing,
  and `replace_text` must state how many exact occurrences it expects, so stale
  model context cannot silently clobber a file.
* Writes are atomic. The payload lands in a temporary file beside the target, is
  flushed, and then replaces the target in one step, so a reader sees the old
  bytes or the new bytes and an existing file keeps its mode.
* No operation runs a shell, a subprocess, git, or any other program.

### Limits and refusals

| Limit | Default | Ceiling | Bounds |
| --- | --- | --- | --- |
| `max_results` | 200 | 2000 | Entries one `list_files` call returns |
| `max_matches` | 100 | 1000 | Matches one `search_text` call returns |
| `max_file_bytes` | 200000 | 1000000 | One file read or searched |
| `max_scan_bytes` | 4000000 | 16000000 | Bytes one `search_text` walk reads |
| `max_write_bytes` | 200000 | 1000000 | Bytes one write produces |

A request that passes the configured cap raises `LimitExceededError` or
`SizeLimitError` naming the cap, and never returns partial data as if it were
complete. A per-call argument may raise a limit up to its ceiling, not past it.

| Code | Meaning |
| --- | --- |
| `UnsafePathError` | Not repository-relative, or resolved outside the workspace |
| `ExcludedPathError` | Git metadata, tool state, or a secret-looking name |
| `PathDeniedError` | Outside the allowlist, or not the file or directory the operation needs |
| `MissingFileError` | The path does not exist |
| `BinaryFileError` | The contents are not UTF-8 text |
| `SizeLimitError` | A byte cap would be exceeded |
| `LimitExceededError` | A result or match count cap would be exceeded |
| `MatchCountError` | `replace_text` found a different number of occurrences |
| `StaleFileError` | The required SHA-256 is missing or does not match |

### What the model may not see

Reads and writes refuse git metadata (`.git` at any depth), CodeWhale's own
state (`.codewhale` at any depth), and root-level pytest state (`.pytest_cache`
and `.pytest-*` basetemp trees) — the same tool-owned state the audit excludes.
Names that look like secret containers are refused too: `.env` and `.env.*`,
`id_rsa`, `credentials.*`, `secrets.*`, and `*.key`, `*.pem`, `*.p12`, `*.pfx`,
`*.jks`, `*.keystore`, and `*.ppk`. The rule is deliberately fail-closed, so
`secrets.py` and `.env.example` are refused as well: one clear error costs less
than one leaked credential.

Generated trees (`.venv`, `node_modules`, `__pycache__`, caches) are hidden from
listings and walks so they cannot consume a cap, but an explicit read of a file
inside one is allowed, because `pyvenv.cfg` and a dependency's stubs are
legitimate context. Binary content is never returned or edited: a NUL byte in
the sniff window or a failed strict UTF-8 decode raises `BinaryFileError` on a
read, and `search_text` skips the file and counts it.

The rule keys on the resolved path, so a case variant, an 8.3 short name, or a
Windows trailing-dot alias still names the path it refers to and is still
refused. Two limits are inherent to a name-based rule: a hard link to a secret
file, or a secret copied under an unrelated name, looks like ordinary source.
The tools create no links, so that can only arrive from the checkout itself, and
the manager's audit still reports every path a job changed.

### How the direct runner uses this layer

`DirectDeepSeekRunner` constructs this class from a `TaskRun` — the workspace the
manager already resolved plus the manager-normalized `allowed_paths` — describes
the five operations to the model with `tool_definitions()`, and converts each
model tool call into `call_json(name, arguments)`:

```python
tools = WorkspaceTools(run.workspace, list(run.allowed_paths))
result = tools.call_json("read_file", '{"path": "src/app/main.py"}')
```

`call_json` parses the model's JSON argument text and returns the same structured
refusal `as_dict()` produces, so one malformed or refused call costs one tool
result the model can correct instead of the whole job. This layer stays the only
file surface the model has: the runner adds no filesystem call of its own, and no
shell, subprocess, or git exists anywhere in the direct path. The runner owns the
bounded step loop, its budgets, and its cancellation; the manager keeps the scope
audit, the stored patch, the validations, and the terminal state.

## Direct DeepSeek Responses API runner (opt-in)

A raw DeepSeek completion cannot replace CodeWhale for implementation jobs on its
own: a completion has no file tools and no edit loop. The direct backend supplies
that loop in this repository, so a direct job is now a supported option — once an
operator selects it explicitly.

```powershell
$env:MODEL_WORKER_RUNNER = "deepseek-api"
$env:MODEL_WORKER_MODEL = "deepseek-chat"   # an API model id, not a CodeWhale alias
$env:DEEPSEEK_API_KEY = "..."               # read when a job starts, never stored
delgado run "Add parser tests for empty input" `
  --workspace C:\Code\example `
  --allow tests/test_parser.py `
  --check "python -m pytest tests/test_parser.py"
```

CodeWhale remains the default and no installation changes behavior by upgrading.
The selector is read through `select_runner()`, and an unknown value is refused
with the two known names instead of quietly running the default backend.

### The key

`DirectDeepSeekRunner` reads `DEEPSEEK_API_KEY` from the environment at execution
time, when a job starts. It never reads it at import or construction, never puts
it in a request body, never stores it on the instance, and never writes it to the
job store. It travels only as an `Authorization` header on the HTTP request.
Output and error text are redacted against the key before they leave the runner,
as a backstop for a chatty proxy or a transport bug, so a key cannot reach a
durable record, a summary, or a log through this path. A job with no key fails
immediately with a message naming the variable, before any request is made.

### The request contract

The runner POSTs to `https://api.deepseek.com/responses` with the documented
Responses shape: `model`, `instructions` (the loop's own tool discipline), `input`
(the manager's scoped prompt, then the whole conversation), `tools` (exactly the
five workspace operations), `tool_choice: "auto"`, `max_output_tokens`, and
`store: false`. Because the conversation is resent whole and nothing is stored
server-side, a direct job depends on no server state, and every request carries
the same tool table.

The HTTP transport is injected: `DirectDeepSeekRunner(transport)` accepts any
callable that takes a `ResponsesRequest` and returns a `ResponsesReply`, and the
default `deepseek_http_transport` is the only place that opens a socket. Every
test in the suite injects a fake transport, so the test suite never makes a real
API call and needs no credential.

### The loop and its budgets

One implementation job is one bounded conversation:

1. Send the conversation and the five tools.
2. Read the answer: assistant text and any function calls.
3. Run each function call through `WorkspaceTools.call_json`.
4. Append the call and its result to the conversation and repeat.

The loop stops when the model answers without a tool call, and it fails safely at
every other boundary: `max_steps` (12), `max_tool_calls` (40),
`max_tool_output_bytes` (400000), `max_response_bytes` (1000000), and the job's
wall-clock budget. Each request also carries a bounded timeout, so a stalled
connection cannot hold the job past its deadline. A cap may be lowered per runner
for a smaller job and raised only up to its documented ceiling; the model can
change none of them.

Stop conditions are explicit and distinguishable in the job record:

| Condition | Reported as |
| --- | --- |
| A final answer with no tool call | `exit_code` 0, succeeded |
| An HTTP error status | failed, with the status and the service message |
| A transport failure or a request timeout | failed, with the reason |
| The wall-clock budget | failed, timed out |
| A malformed, non-terminal, failed, incomplete, or cancelled answer | failed, with the reason |
| A step, tool-call, or tool-output budget | failed, with the budget that stopped it |
| `cancel(job_id)` | cancelled, with the manager's terminal decision |
| A token already set before the loop began | cancelled, with no request and no write |

### What the model may call

The model sees exactly five tools — `list_files`, `search_text`, `read_file`,
`replace_text`, and `write_file` — and nothing else. An unknown tool name, an
argument the operation does not accept, malformed JSON argument text, an
out-of-allowlist write, a path that escapes the workspace, a secret-looking name,
and every size or count cap all come back as a structured tool result the model
can read and correct. There is no shell, no command tool, no git, and no way to
run a validation command: the manager still runs `required_commands` itself after
the loop stops, under its own executable allowlist and without the worker
credential in the child's environment.

### What stays with the manager

Selection changes only how the worker runs. The manager still takes the workspace
lock, records `before`/`after` status, decides policy violations, writes the
stored patch, runs the required validations, writes `runner.json`, and owns the
terminal state — including the race where a cancellation arrives while the job
thread is auditing changes.

The workspace lock follows the same rule as the record it names: a lock is
recoverable only when nothing it names is still running. A terminal or cancelled
record does not free a workspace by itself, because the owning job thread keeps
the checkout until it releases the lock, and a cancel publishes CANCELLED while
that thread is still returning from a stopped worker. A second submit is refused
with `WorkspaceBusyError` until then, which is what keeps two jobs out of one
checkout in the window between the cancel and the thread's exit.

Validations are the manager's, and they run without the worker credential: each
command is started with a copy of this process's environment minus
`DEEPSEEK_API_KEY`, and whatever a command captured is redacted against that name
before it becomes durable. A validation cannot read the key, and a tool that
prints its environment anyway cannot store it.

Two limits are worth stating plainly:

* Reviews still require the CodeWhale runner. The direct runner refuses the
  review methods with `RunnerCapabilityError`, because its job loop is an
  implementation loop and the review path is process-shaped. Run
  `model_worker_review` with `MODEL_WORKER_RUNNER` unset (or `codewhale`).
* A direct job lives in the process that started it. Cancelling it from that
  process sets the token its loop and its validations read, so the job stops
  without another request or command; cancelling it from a second process is
  refused with `JobOwnershipError` rather than by stopping the manager process
  that owns the loop, and a job whose owning process died is marked `interrupted`
  by the next manager start, which also releases its workspace lock.

Use CodeWhale when a task needs the harness's own tool approvals, sandbox modes,
and process-tree guarantees. Use the direct backend for bounded, path-scoped
implementation work where a smaller, inspectable loop is the point. Do not copy
the Delgado policy into a runner: job records, path audits, validation, repair
links, and transport adapters stay in the shared manager, which is what lets both
interfaces offer either backend without changing the MCP or CLI contracts.
