# Operating Model

MCP Delgado works best as a delegation layer, not an autonomous manager.

## Roles

The frontier agent owns these decisions:

* Understand the user goal.
* Inspect the relevant architecture.
* Split work into bounded tasks.
* Define scope and acceptance criteria.
* Review all worker changes.
* Decide whether to repair, accept, or discard work.
* Commit, push, merge, and deploy.

The worker owns these actions:

* Inspect code inside the given workspace.
* Implement the bounded task.
* Run the required checks.
* Report changed files and unresolved issues.

## Delegation rule

Delegate a task only when you can state all four items:

1. One concrete outcome.
2. A narrow path scope.
3. Observable acceptance criteria.
4. Exact validation commands.

Keep the task with the manager when any item is unclear.

## Suggested sequence

1. The manager inspects the repository and its instructions.
2. The manager selects one independent leaf task.
3. The manager names the allowed paths.
4. The manager supplies exact checks.
5. Delgado starts the worker.
6. The manager reviews the result and patch.
7. The manager runs any additional checks.
8. The manager requests a focused repair when necessary.
9. The manager commits only reviewed work.

## MCP and CLI roles

MCP is the normal interface for a frontier agent.
It supports asynchronous delegation without leaving the agent session.
It also exposes structured tools that reduce command parsing errors.

The CLI is the normal interface for a human or script.
It is easier to inspect, reproduce, and debug from a terminal.
It also provides a stable path when an MCP host has integration problems.

The CLI does not replace MCP.
The two interfaces solve different integration needs.
They must share the same manager and job records.

## Repository policy

Treat this repository as the only source for Delgado behavior.
Do not copy its package back into application repositories.
Install it from a pinned release or a pinned Git revision.

Make delegation changes here first.
Add tests for every policy or process change.
Release a version after tests pass.
Then update each client to the new pinned version.

## Initial adoption plan

Start with low-risk tasks for two weeks.
Record the model, duration, result state, and repair count.
Compare total manager effort against direct frontier implementation.

Expand usage when the worker produces reviewable patches with few repairs.
Reduce usage when task definition costs more than direct implementation.

Good first categories include tests, local refactors, and narrow defect fixes.
Avoid migrations, security controls, and deployment code during the trial.
