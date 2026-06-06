# learning05: TodoWrite — Make a List First, Then Execute

learning01 → learning02 → learning03 → learning04 → `learning05` → learning06 → ... → learning20
> *"Make a list first, then execute"* — A planning tool helps the Agent stay on track during multi-step tasks.
>
> **Harness Layer**: Planning — giving the Agent a way to externalize its plan.

---

## The Problem

By learning04, the Agent can use tools, check permissions, and run hooks. But when you give it a larger task — "refactor three files, run tests, and fix what breaks" — it still has no explicit place to track the plan.

So the Agent starts acting immediately. After a few tool calls, the conversation fills with file contents, command output, and intermediate reasoning. The original task is still somewhere in the message history, but its influence gets weaker as more context piles on.

That is when the Agent begins to drift:

- it completes step 1 and forgets step 4,
- it starts fixing a side issue and loses the main objective,
- it finishes some work but never verifies that all planned steps were completed.

The Agent does not need more execution power here. It needs a memory structure for the plan.

---

## The Solution

![Todo Overview](images/todo-overview.en.svg)

learning04's loop, hook system, and tool dispatch all stay intact. The only real addition is a new tool: `todo_write`.

Unlike `bash`, `read_file`, or `write_file`, `todo_write` does not interact with the filesystem or shell. It cannot execute anything. Its job is much simpler: let the model write down a task list, update statuses, and keep the current plan visible.

To reinforce this behavior, the teaching version also adds a lightweight reminder: if the Agent goes several rounds without updating its todos, the loop injects a reminder asking it to refresh the list.

---

## How It Works

**Step 1**: Add a state container for the current TODO list.

```python
CURRENT_TODOS: list[dict] = []
```

This lives in process memory. It is not persisted to disk.

**Step 2**: Implement the `todo_write` tool handler. It receives a list of todo items, stores them, prints a readable task board, and returns a short status message to the model.

```python
def run_todo_write(todos: list) -> str:
    global CURRENT_TODOS
    CURRENT_TODOS = todos

    lines = ["\n## Current Tasks"]
    for t in CURRENT_TODOS:
        icon = {"pending": " ", "in_progress": "▸", "completed": "✓"}[t["status"]]
        lines.append(f"  [{icon}] {t['content']}")
    print("\n".join(lines))
    return f"Updated {len(CURRENT_TODOS)} tasks"
```

Each todo item has two fields:

- `content`: the task description
- `status`: one of `pending`, `in_progress`, or `completed`

**Step 3**: Register the new tool in `TOOLS` and `TOOL_HANDLERS`.

```python
TOOLS = [
    {"name": "bash", ...},
    {"name": "read_file", ...},
    {"name": "write_file", ...},
    {"name": "edit_file", ...},
    {"name": "glob", ...},
    {
        "name": "todo_write",
        "description": "Create and manage a task list for the current task.",
        "input_schema": {
            "type": "object",
            "properties": {
                "todos": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "content": {"type": "string"},
                            "status": {
                                "type": "string",
                                "enum": ["pending", "in_progress", "completed"],
                            },
                        },
                        "required": ["content", "status"],
                    },
                },
            },
            "required": ["todos"],
        },
    },
]

TOOL_HANDLERS["todo_write"] = run_todo_write
```

Just like learning02: add one tool definition, add one handler mapping, and the dispatch system continues to work unchanged.

**Step 4**: Track how long it has been since the last todo update. If too many rounds pass without `todo_write`, inject a reminder before the next model call.

```python
if rounds_since_todo >= 3 and messages:
    messages.append({
        "role": "user",
        "content": "<reminder>Update your todos.</reminder>",
    })
    rounds_since_todo = 0
```

This reminder is a teaching mechanism. It nudges the model back toward planning behavior.

A typical execution flow now looks like this:

1. User gives a multi-step task
2. Agent calls `todo_write` with several `pending` items
3. Agent marks one item `in_progress`
4. Agent uses normal tools (`read_file`, `edit_file`, `bash`, etc.) to do the work
5. Agent marks the item `completed`
6. Agent moves to the next pending item

The important point is that `todo_write` adds **planning structure**, not **execution capability**.

---

## Quick Reference

| Concept | One-Liner |
|---------|-----------|
| `todo_write` | A planning tool that stores and updates the current task list |
| Todo status | `pending`, `in_progress`, `completed` |
| Reminder injection | If the Agent ignores todos for too long, inject a planning reminder |
| Core loop | Still the same loop and dispatch pattern from earlier chapters |

---

## Changes from learning04

| Component | Before (learning04) | After (learning05) |
|-----------|-------------|-------------|
| Tool count | 5 (bash, read, write, edit, glob) | 6 (+todo_write) |
| Planning support | None | In-memory TODO list |
| Loop behavior | Normal hook-based execution | Same loop + rounds_since_todo reminder logic |
| Agent guidance | Execute based on prompt only | Encouraged to plan first, then act |

---

## Try It

```sh
cd learn-claude-code
python learning05_todo_write/code.py
```

Try these prompts:

1. `Refactor example/hello.py: add type hints, a docstring, and a main guard`
2. `Create a small Python package under example/demo_pkg with __init__.py, utils.py, and tests/test_utils.py`
3. `Review the files under example and fix style issues, then summarize what changed`

What to watch for: Is the first major tool call `todo_write`? Does the Agent keep the task list updated as it moves through the work? After several non-todo rounds, does the reminder appear?

---

## What's Next

A todo list helps the Agent stay organized within one conversation. But for very large tasks, a single conversation still becomes crowded. At some point, planning alone is not enough — the work must be split.

→ learning06 Subagent: break a large task into smaller tasks, each handled with its own context.

<details>
<summary>Dive into CC Source Code</summary>

> The following is based on a review of CC source code around `TodoWriteTool.ts`, `tasks.ts`, and `utils/todo/types.ts`.

### 1. TodoWrite Is One of Two Task Systems

CC currently has two task-management approaches:

- **TodoWrite (V1)**: a simple in-memory todo list
- **Task System (V2)**: a richer file-persisted task model introduced later

The teaching version focuses only on the simpler TodoWrite path, because it is enough to show the core planning idea clearly.

### 2. In-Memory vs Persisted State

The teaching version stores todos in a process variable:

```python
CURRENT_TODOS = []
```

CC's newer task system persists data to disk, supports task identity, and tracks more metadata. That complexity is useful in production, but not necessary to understand why a planning tool matters.

### 3. Reminder Logic Is a Teaching Device

The teaching version uses a fixed heuristic: if the model goes 3 rounds without updating todos, inject a reminder.

CC source does not implement this exact fixed-round reminder. The real code has its own checks and nudges, but not this simple chapter-specific rule. This reminder exists to make the planning behavior visible and easy to test.

### 4. Why `todo_write` Matters

`todo_write` does not make the Agent stronger in the same way as `bash` or file tools. It makes the Agent more stable on long tasks. That difference matters:

- execution tools expand what the Agent **can do**
- planning tools improve how reliably the Agent **stays aligned with the task**

That is why TodoWrite appears after tools, permissions, and hooks. It builds on the existing harness, but changes the Agent's behavior in a different dimension.

</details>
