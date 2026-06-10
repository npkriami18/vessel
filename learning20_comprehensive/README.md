# learning20: Comprehensive Agent — All Mechanisms, One Loop

learning01 → ... → learning18 → learning19 → `learning20`
> *'many mechanisms, one loop'* — tools, permissions, memory, tasks, teams, worktrees, and plugins all hang off the same `while True`.
>
> **Harness Layer**: Comprehensive — all prior teaching mechanisms integrated into one runnable system.

---

## The Problem

The first 19 chapters each introduce one mechanism at a time.

That is the right way to learn.

But a real coding agent does not run with only one mechanism enabled.

A practical long-running harness needs many capabilities working together at once:

- tool dispatch and permission boundaries
- hook extension points
- todo planning and task graphs
- skills, memory, and runtime system prompt assembly
- compaction and error recovery
- background tasks and cron scheduling
- teams, protocols, and autonomous task claiming
- worktree isolation
- MCP external tool integration

The hard part is not just accumulating features.

The hard part is understanding where each mechanism belongs around the loop and how they interact without breaking the core structure.

By learning19, the harness can already do a lot.

What is still missing is the final integration step:

- one place where the earlier mechanisms coexist
- one runnable harness instead of isolated teaching slices
- one loop that shows how all capabilities attach to the model call and tool execution cycle

---

## The Solution

learning20 does not add one new isolated idea.

Instead, it integrates the prior chapters into one complete teaching harness.

The result is still the same fundamental agent shape:

```text
user input
  → UserPromptSubmit hooks
  → cron/background notification injection
  → compaction checks
  → memory + skills + MCP state assemble the system prompt
  → LLM
  → has tool_use block?
      no  → Stop hooks → return final response
      yes → PreToolUse hooks + permission checks
          → built-in handlers / MCP handlers / background dispatch
          → PostToolUse hooks
          → tool_result and task_notification appended to messages
          → next round
```

This is the key teaching point:

**the harness is still one agent loop. the complexity comes from the environment wrapped around that loop, not from replacing it with a different kind of architecture.**

Each earlier chapter contributed one layer.

learning20 shows how those layers fit together in one system.

---

## How It Works

### One integrated harness model

The comprehensive harness places earlier mechanisms at different points in the runtime cycle.

A useful way to view it is by position:

| Position | Component | Role |
|----------|-----------|------|
| around user input | `UserPromptSubmit` hooks | observe, log, or modify incoming user prompts |
| before the LLM call | cron queue | inject scheduled prompts into the conversation |
| before the LLM call | background notifications | inject completed background results as task notifications |
| before the LLM call | compaction pipeline | reduce oversized context and summarize when needed |
| before the LLM call | memory / skills / MCP state | assemble the runtime system prompt |
| during the LLM call | error recovery | retry and recover from common API failures |
| before tool execution | `PreToolUse` hooks + permission checks | block unsafe actions before handlers run |
| during tool execution | assembled tool pool | expose built-in and MCP tools together |
| during tool execution | background dispatch | move slow shell work off the main loop |
| after tool execution | `PostToolUse` hooks | audit, log, and annotate results |
| when no more tools are needed | `Stop` hooks | perform final cleanup and reporting |

The important point is that these are not separate mini-agents.

They are attachment points around one loop.

### Tool pool: Built-in tools and MCP tools together

By learning19, the harness can connect external MCP servers and expose discovered tools under generated names such as:

- `mcp__docs__search`
- `mcp__deploy__trigger_release`

learning20 keeps that mechanism and combines it with the full built-in tool set.

A representative built-in pool includes tools from many earlier chapters, such as:

```text
bash, read_file, write_file, edit_file, glob
todo_write, task, load_skill, compact
create_task, list_tasks, get_task, claim_task, complete_task
schedule_cron, list_crons, cancel_cron
spawn_teammate, send_message, check_inbox
request_shutdown, request_plan, review_plan
create_worktree, remove_worktree, keep_worktree
connect_mcp
```

The harness rebuilds the available tool pool each round so that newly connected MCP servers are reflected immediately.

That means the model sees a unified callable surface even though the capabilities come from different sources.

### Permissions and hooks sit before execution

The comprehensive harness restores the earlier lesson that permission logic does not need to be fused directly into every tool handler.

Instead, permission and policy checks can sit at a hook boundary before execution.

A simplified shape looks like this:

```python
blocked = trigger_hooks("PreToolUse", block)
if blocked:
	results.append(tool_result(block.id, blocked))
	continue
```

This matters because the same boundary can support:

- safety policy
- audit logging
- command filtering
- path restrictions
- special treatment for destructive tools

After the handler runs, `PostToolUse` hooks provide a symmetric extension point for logging, warnings, or result inspection.

### Planning exists at two levels

learning20 keeps two planning mechanisms because they solve different problems.

#### 1. `todo_write`

This is the lightweight session-local plan.

It helps a single agent avoid drifting during the current interaction.

#### 2. task graph tools

These are the persistent, dependency-aware task records created under the task system.

They support:

- parallel work
- task ownership
- blocked-by relationships
- teammate coordination across turns or threads

So the harness distinguishes between:

- **what I plan to do right now**
- **what the broader team or workflow needs tracked durably**

### Delegation: One-shot subagents and persistent teammates

The integrated harness includes both kinds of delegation introduced earlier.

#### One-shot subagents

The `task` tool creates an isolated subagent run with its own temporary message history and returns only its final answer.

This is useful for bounded delegated reasoning where intermediate context should not pollute the main thread.

#### Persistent teammates

The `spawn_teammate` flow creates long-lived teammates that communicate through inbox protocols and can discover and claim work from the shared task board.

These solve a different problem:

- long-running parallel execution
- repeated coordination
- autonomous polling for claimable tasks

learning20 keeps both because context isolation and persistent collaboration are not the same thing.

### Memory, skills, and runtime prompt assembly

The system prompt is no longer a fixed static string.

Instead, it is assembled each round from runtime state, including things like:

- identity and tool guidance
- workspace information
- skills catalog
- memory from `.memory/MEMORY.md`
- connected MCP server state

This preserves an important lesson from earlier chapters:

**the system prompt is part of the harness, and it can be assembled dynamically from environment state.**

Skills remain cataloged in the prompt, while full skill content is loaded on demand through `load_skill(name)`.

### Compaction and recovery keep the loop alive

Long-running agents accumulate context.

learning20 therefore integrates the compaction and recovery behaviors from earlier chapters.

A representative compaction flow looks like:

```text
tool_result_budget → snip_compact → micro_compact → compact_history
```

And the model call can recover from several common failure patterns:

- rate limits such as 429
- overload responses such as 529
- `max_tokens` truncation that requires continuation
- prompt-too-long errors that require reactive compaction and retry

These mechanisms are not new kinds of reasoning.

They are harness resilience features that help the agent keep operating across long sessions.

### Background tasks and cron scheduling inject work back into the loop

Some operations should not block the main interaction.

So the comprehensive harness includes:

- background task dispatch for slow operations
- cron scheduling for future reminders or timed prompts

The pattern is consistent with the rest of the design:

- a slow operation returns a placeholder result now
- later, completion is injected back into the conversation as a notification
- scheduled jobs similarly re-enter the loop as messages when their time arrives

That means asynchronous activity still feeds into the same core message loop rather than creating a separate orchestration model.

### Worktree isolation and MCP remain distinct layers

learning20 preserves two important separations from the previous chapters.

#### Worktrees own directory isolation

- `create_worktree(name, task_id)` creates an isolated branch and directory
- a task can be bound to that worktree
- when a teammate claims that task, its tool calls run inside the bound directory

#### MCP owns external capability discovery

- `connect_mcp(name)` connects a named server
- discovered tools are assembled into the tool pool
- generated names use the `mcp__server__tool` namespace

These solve different problems:

- worktrees prevent local filesystem collisions
- MCP makes external tool integration extensible

learning20 keeps both intact while placing them inside one full runtime.

### Putting it together

A typical comprehensive flow might look like this:

```text
1. the user asks for repo inspection and planning
2. the harness runs user-input hooks and assembles the current system prompt
3. the agent creates a todo list and reads files
4. the user asks to connect an MCP docs server
5. the agent calls connect_mcp('docs')
6. the next round exposes mcp__docs__... tools
7. the user asks to create parallel tasks
8. the agent creates tasks, prepares worktrees, and spawns teammates
9. teammates submit plans, claim approved tasks, and work in isolated directories
10. a slow shell command is dispatched in the background
11. later, its completion notification is injected into messages
12. a scheduled reminder fires and is also injected into the loop
13. compaction and recovery keep the conversation operational as context grows
```

The important result is that all prior mechanisms now hang off one operational harness.

---

## Changes from learning19

| Component | Before | After |
|-----------|--------|-------|
| chapter focus | MCP integration as a focused slice | full integration of learning01–learning19 mechanisms |
| tool pool | built-in + dynamic MCP tools | restored broad tool surface plus dynamic MCP tools |
| hooks | limited or omitted in recent focused chapters | `UserPromptSubmit`, `PreToolUse`, `PostToolUse`, `Stop` integrated |
| permissions | earlier isolated concept | restored as part of tool execution flow |
| planning | chapter-specific tools depending on lesson | session todo plus durable task system together |
| delegation | subagents or teammates covered separately | one-shot subagents and persistent teammates both present |
| prompt assembly | focused chapter-specific state | memory, skills, workspace, and MCP state assembled together |
| compaction | taught separately | integrated before model calls and during recovery |
| error recovery | simplified in focused lessons | retries, continuation, and prompt-too-long recovery together |
| async work | background and cron taught separately | both integrated into the main runtime loop |
| isolation | worktrees added in learning18 | worktrees preserved inside the full harness |
| extensibility | MCP added in learning19 | MCP preserved inside the full harness |

---

## Try It

```sh
cd learn-claude-code
python learning20_comprehensive/code.py
```

Try prompts like:

1. `Create a todo list for inspecting this repo, then list the Python files.`
2. `Connect to the docs MCP server and search for agent loop notes.`
3. `Create two tasks, create worktrees for them, then spawn alice and bob and ask them to submit plans before claiming tasks.`
4. `Remind me about the meeting in 3 minutes.`
5. `Run a slow shell task in the background and continue reading the README.`

What to observe:

- do tool calls pass through the hook and permission flow before execution?
- after `connect_mcp`, do MCP-prefixed tools appear on the next round?
- can background work return a placeholder result and later inject a completion notification?
- does the cron scheduler inject scheduled prompts back into the conversation?
- can teammates submit plans, wait for review, and then claim tasks?
- when a teammate claims a worktree-bound task, do later tools run in the isolated directory?
- does the harness continue functioning as context grows through compaction and recovery?

---

## What's Next

learning20 is the endpoint of the teaching sequence.

From learning01 through learning20, the harness became more capable, but the core remained the same:

```python
while True:
	response = LLM(messages, tools)
	if not has_tool_use(response.content):
		return
	results = execute_tools(response.content)
	messages.append(tool_results)
```

The main lesson is not that the agent needs a mysterious second brain.

The lesson is that a mature coding agent depends on a mature harness:

- tools
- permissions
- hooks
- planning
- memory
- teams
- isolation
- recovery
- extensibility

Many mechanisms.

One loop.
