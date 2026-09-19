# MCP Delgado Launch Content

These drafts use a direct, skeptical, builder-focused voice.
Edit any personal claim before publishing.

## LinkedIn

What if your OpenAI Astra or Anthropic Fable plan lasted longer than one serious coding day?

I like frontier models. I do not like paying frontier-model prices for every grep, test run, and mechanical edit.

So I built MCP Delgado.

The idea is simple. Keep the frontier model thin.

Astra or Fable stays in charge of architecture, product judgment, security, and final review. Delgado sends bounded implementation work to DeepSeek Flash through CodeWhale.

The worker gets one task, narrow file access, acceptance criteria, and exact checks. It cannot commit, push, merge, or deploy. The frontier model reviews the stored diff before accepting anything.

Delgado includes both an MCP server and a CLI. I trust the CLI path too. It is easier to inspect, script, and debug. Both interfaces use the same manager and job records.

This is not a magic cost-cutting switch. Bad delegation wastes time. Broad tasks drift. Security and architecture still need the strongest model.

But many coding tasks are leaf work:

* Add tests for one module.
* Apply a mechanical refactor.
* Trace one defect.
* Review one diff for one risk class.
* Implement one service behind an existing interface.

Those tasks do not always need the most expensive intelligence in the room.

The project is public here:

https://github.com/fastrocket/mcp-delgado

I am using it now and publishing the failures too. The first real Windows run found an encoding defect after CodeWhale completed successfully. That is exactly why the manager must keep evidence and review the result.

The goal is not to replace Astra or Fable. The goal is to spend their attention where it matters.

## X thread

1/ If your Astra or Fable coding plan disappears in one day, you may be paying frontier-model prices for non-frontier work.

I built MCP Delgado to change that.

https://github.com/fastrocket/mcp-delgado

2/ The frontier model stays in charge.

It plans the work, chooses bounded tasks, reviews the diff, and decides what ships.

DeepSeek Flash handles suitable leaf work through CodeWhale.

3/ Every delegated job gets:

* one concrete outcome
* narrow allowed paths
* acceptance criteria
* exact validation commands
* a durable result and patch

The worker does not commit, push, merge, or deploy.

4/ I added both MCP and CLI interfaces.

MCP is better inside an agent session.

The CLI is better for humans, scripts, debugging, and CI.

They share one core. There are not two policy systems to maintain.

5/ This is not an autonomous swarm.

Architecture, security, migrations, and final acceptance stay with Astra or Fable.

Tests, narrow fixes, mechanical edits, and focused reviews are good Delgado work.

6/ The point is not "use a cheaper model for everything."

The point is to stop wasting expensive attention on work that has clear boundaries and observable checks.

Try it, break it, and tell me where the boundary should be.

## Substack

# How to Make Your Frontier Coding Plan Last Longer Than One Day

I keep running into the same problem with frontier coding agents.

The model is extremely capable. It is also spending expensive attention on work that does not always require frontier intelligence.

It searches for files. It runs tests. It applies mechanical edits. It traces a narrow defect. It reviews repetitive output.

I want Astra or Fable making the hard decisions. I do not need it personally typing every line.

That is why I built MCP Delgado.

## Keep the manager smart

Delgado does not replace the frontier model. It changes its job.

The frontier model becomes the manager. It understands the user goal, studies the architecture, defines a bounded task, and reviews the result.

DeepSeek Flash becomes the implementation worker. CodeWhale supplies its coding loop and provider connection.

This division matters. A cheaper model can do excellent work when the task has a narrow boundary. It can also drift when the task is vague.

Delgado therefore requires explicit allowed paths, acceptance criteria, and validation commands. It records the output and produces a patch for review.

The worker does not own commits or deployment.

## MCP or CLI?

I had the same concern many developers have. MCP feels useful, but I trust a terminal command because I can see it.

So Delgado has both.

The MCP server is the normal path for an active coding agent. The CLI is the normal path for a person, script, or CI job.

Both use the same manager, schemas, checks, and durable job store.

This avoids the worst outcome: one set of safety rules for MCP and another set for the terminal.

## What should you delegate?

Delegate work when you can state one outcome, narrow paths, observable acceptance criteria, and exact checks.

Good examples include adding tests, applying a contained refactor, tracing one defect, or reviewing a diff for one risk.

Keep architecture, security, migrations, ambiguous product behavior, and final acceptance with the frontier model.

The distinction is judgment versus execution.

## What happened in the first real run?

The MCP transport started quickly. CodeWhale then spent several minutes inspecting a large repository with DeepSeek Flash.

The model completed, but the Windows adapter decoded its output with the wrong character set. The process returned success while the report disappeared.

That defect is now covered by a regression test.

This failure reinforces the design. A delegated task needs durable evidence. A zero exit code is not enough.

## Direct DeepSeek API users

Delgado currently calls CodeWhale. CodeWhale can connect to the DeepSeek API directly through its provider profile.

Removing CodeWhale is not only an HTTP change. Someone still needs to supply the agent loop, workspace tools, sandbox behavior, and test execution.

A future runner interface can support other harnesses. A raw API adapter should not pretend that one completion equals a coding agent.

## Try it

The repository is public:

https://github.com/fastrocket/mcp-delgado

I am interested in one practical question: which tasks save frontier attention without increasing review time?

That answer needs measurements, not vibes.

## Video script

Target length: approximately 45 seconds.

> If your Astra or Fable coding plan disappears in one day, the problem might not be the plan. You may be spending frontier intelligence on every search, edit, and test run.

> I built MCP Delgado to keep the frontier model thin. Astra or Fable stays in charge of architecture and final review. Delgado sends bounded implementation work to DeepSeek Flash through CodeWhale.

> Every job gets narrow file access, acceptance criteria, and exact checks. The worker cannot commit or deploy. The frontier model reviews the patch before anything ships.

> You can use it through MCP inside your coding agent, or through a CLI when you want direct control. The project is public. Try it, break it, and help me find the right boundary.

## Future screen footage list

The current Promo Composer produces presenter scenes only.
A later product-proof stage should add these screen clips:

1. Open the Codex configuration file.
2. Add the `mcp_delgado` server block.
3. Open the global `~/.codex/AGENTS.md` policy.
4. Show a bounded delegation request.
5. Show the MCP job status.
6. Show the stored diff and validation result.
7. Show the same task through the `delgado` CLI.
8. End on the public GitHub repository.
