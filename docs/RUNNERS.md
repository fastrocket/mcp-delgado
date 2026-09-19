# Runner choices

MCP Delgado currently uses CodeWhale as its worker harness.
CodeWhale connects to the configured DeepSeek API provider.
This path is a direct DeepSeek API connection through a reusable agent loop.

CodeWhale provides more than an HTTP client. It provides the model loop,
workspace tools, edit operations, tool approvals, and result formatting.
MCP Delgado adds job scope, durable state, path audits, and validation.

## Direct DeepSeek API use

A raw DeepSeek completion cannot replace CodeWhale for implementation jobs.
The completion has no file tools or edit loop by itself.

A future direct runner should implement one clear interface. It must return a
standard result with output, changed paths, exit status, and usage data. It must
also define its own tool loop and workspace controls.

Use a direct runner for tasks that only need a model response. Examples include
classification, extraction, planning, or read-only analysis with supplied text.
Use an agent harness for tasks that must inspect, edit, and test a repository.

Do not copy the Delgado policy into each runner. Keep job records, path audits,
validation, repair links, and transport adapters in the shared manager.

## Recommended interface

Define a `WorkerRunner` protocol with two operations:

* `run_task(job, prompt)` for workspace implementation.
* `run_review(workspace, request)` for read-only analysis.

The CodeWhale adapter remains the default implementation.
A direct DeepSeek adapter can support response-only reviews first.
It must not claim implementation support until it has a tested tool loop.

This separation lets users choose a provider path without changing the MCP or
CLI contracts.
