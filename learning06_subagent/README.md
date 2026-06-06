# learning06: Subagent — Break Large Tasks into Smaller Ones with Clean Context

learning01 → learning02 → learning03 → learning04 → learning05 → `learning06` → learning07 → ... → learning20
> *"Break big tasks into smaller focused ones"* — A subagent gets its own fresh context, works on one subtask, and returns only the conclusion.
>
> **Harness Layer**: Subagent — context isolation without losing filesystem side effects.

---

## The Problem

By learning05, the Agent can plan with `todo_write`, but it still does all work inside one growing conversation.

That works for short jobs. But for larger tasks, the message history starts filling with temporary investigation work:

- tracing a call chain across many files,
- comparing several implementations,
- exploring a side question before deciding what to edit.

Those intermediate steps are useful while the Agent is doing them, but they are often irrelevant once the sub-problem is solved.

The result is context pollution: the main conversation keeps every detail, even when only the final conclusion matters.

Humans solve this naturally. You open another terminal, investigate one thing there, write down the answer, then go back to the main task. The Agent needs the same pattern.

---

## The Solution

![Subagent Overview](images/subagent-overview.en.svg)

learning05's loop, hooks, tool dispatch, and `todo_write` all remain in place. The new addition is a `task` tool that launches a subagent.

The subagent:

- starts with a fresh `messages[]`,
- runs its own agent loop,
- can use the normal base tools,
- returns only a text conclusion to the parent agent.

Its intermediate conversation is discarded when it finishes. But any side effects on the working directory — such as file writes, edits, or shell commands — remain.

This gives the parent Agent a way to delegate noisy subtasks without bloating its own context.

---

## How It Works

**Step 1**: Add a `spawn_subagent()` function. It creates a fresh message list for the subtask and runs a small independent loop.

```python
def spawn_subagent(description: str) -> str:
    sub_tools = [...]
    messages = [{"role": "user", "content": description}]

    for _ in range(30):
        response = client.messages.create(
            model=MODEL,
            system=SUB_SYSTEM,
            messages=messages,
            tools=sub_tools,
            max_tokens=8000,
        )
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason != "tool_use":
            break

        results = []
        for block in response.content:
            if block.type == "tool_use":
                blocked = trigger_hooks("PreToolUse", block)
                if blocked:
                    results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": str(blocked),
                    })
                    continue

                handler = SUB_HANDLERS.get(block.name)
                output = handler(**block.input) if handler else f"Unknown: {block.name}"
                trigger_hooks("PostToolUse", block, output)
                results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": output,
                })

        messages.append({"role": "user", "content": results})

    return extract_text(messages[-1]["content"])
```

The key idea is simple: the subagent has its **own** `messages[]`, not the parent's.

**Step 2**: Expose that function as a new tool named `task`.

```python
TOOLS = [
    {"name": "bash", ...},
    {"name": "read_file", ...},
    {"name": "write_file", ...},
    {"name": "edit_file", ...},
    {"name": "glob", ...},
    {"name": "todo_write", ...},
    {
        "name": "task",
        "description": "Launch a subagent to handle a complex subtask. Returns only the final conclusion.",
        "input_schema": {
            "type": "object",
            "properties": {
                "description": {"type": "string"}
            },
            "required": ["description"],
        },
    },
]

TOOL_HANDLERS["task"] = spawn_subagent
```

Just like learning02 and learning05: add one tool definition, add one handler mapping.

**Step 3**: Restrict the subagent's tool set. It gets the base tools, but not `task` itself.

```python
sub_tools = [bash, read_file, write_file, edit_file, glob]
```

This prevents recursive subagent spawning in the teaching version.

**Step 4**: Keep hooks and permissions active. Subagent tool calls still go through the same hook flow as the parent.

That means context isolation is not permission bypass. A subagent can focus on a subtask, but it still cannot escape the same execution controls.

A typical flow now looks like this:

1. Parent Agent receives a large task
2. Parent may call `todo_write` to plan
3. Parent calls `task` for a focused sub-problem
4. Subagent investigates with a fresh context
5. Subagent returns only a short conclusion
6. Parent continues with the main task using that result

The important point is that subagents add **context isolation**, not **new execution primitives**.

---

## Quick Reference

| Concept | One-Liner |
|---------|-----------|
| `task` | A tool that launches a subagent for a focused subtask |
| Fresh context | The subagent gets its own independent `messages[]` |
| Return value | The parent receives only the final text conclusion |
| Side effects | File edits and shell changes remain in the working directory |
| Recursion guard | The teaching subagent has no `task` tool |

---

## Changes from learning05

| Component | Before (learning05) | After (learning06) |
|-----------|-------------|-------------|
| Tool count | 6 (bash, read, write, edit, glob, todo_write) | 7 (+task) |
| Large-task handling | One conversation holds everything | Subtasks can run in isolated contexts |
| New function | — | `spawn_subagent()` |
| Loop structure | Single agent loop | Parent loop unchanged; subagent runs its own loop |
| Context behavior | All intermediate work stays in parent history | Subagent intermediate work is discarded after completion |

---

## Try It

```sh
cd learn-claude-code
python learning06_subagent/code.py
```

Try these prompts:

1. `Use a subtask to find what testing framework this project uses`
2. `Delegate reading all Python files in example/ and summarize what each one does`
3. `Use a task to create example/string_tools.py with a slugify(text: str) function, then verify it from the parent agent`

What to watch for: Does the Agent explicitly delegate with `task`? Do subagent actions stay scoped to the subtask while the parent receives only the conclusion?

---

## What's Next

A subagent keeps the main conversation cleaner. But different kinds of work need different knowledge: frontend edits need UI conventions, database work needs schema knowledge, deployment work needs operational rules.

Putting all of that into one giant system prompt would waste context again.

→ learning07 Skill Loading: load specialized knowledge only when needed, instead of carrying everything all the time.

<details>
<summary>Dive into CC Source Code</summary>

> The following is based on a review of CC source code around `AgentTool.tsx`, `runAgent.ts`, `forkSubagent.ts`, and `forkedAgent.ts`.

### 1. The Teaching Version Shows Only One Execution Mode

The teaching version presents subagents as "fresh messages[] + run a loop + return the conclusion".

CC is more nuanced. It supports multiple execution paths depending on how the subagent is launched:

- a normal subagent path,
- a forked path optimized for prompt-cache reuse,
- and other variants depending on coordinator and background behavior.

The simplified chapter keeps only the cleanest mental model: isolated context.

### 2. Real CC Forking Can Optimize for Prompt Cache

In CC, some subagent flows are not purely about isolation. A forked subagent may intentionally preserve a cache-friendly message prefix so the API can reuse prompt cache across parent and child runs.

That optimization matters in production, but it would distract from the main lesson here: why a separate context is useful in the first place.

### 3. Isolation Is Real, but Not Absolute

The teaching version describes the subagent as independent. That is directionally correct, but the production system shares some state between parent and child depending on the execution path.

For example, file-read tracking and some control state can be propagated so the system behaves efficiently and consistently.

### 4. Recursion Protection Is More Sophisticated in CC

This chapter uses a simple rule: the subagent does not get the `task` tool.

CC uses a more complex set of controls to prevent unsafe or unbounded recursive spawning. The teaching version keeps the mechanism deliberately visible and easy to understand.

### 5. Permission Checks Still Bubble Through the System

The important production invariant is the same as in the teaching version: launching a subagent does not bypass safety checks.

Subagent tool use still flows through permission and hook mechanisms. Isolation of context is not isolation from policy.

### 6. Async Subagents Exist in CC

This chapter focuses on the synchronous case: the parent launches a subagent and waits for the result.

CC also supports asynchronous/background subagent execution in some paths. That is intentionally out of scope here so the chapter can focus on the core idea first.

</details>

<!-- translation-sync: en@v1 -->