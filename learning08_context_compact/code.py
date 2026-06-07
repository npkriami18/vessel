#!/usr/bin/env python3
"""
learning08: Context Compact — four-layer compaction pipeline inserted before LLM calls.

    L1: snip_compact       — trim middle messages when count > 50
    L2: micro_compact      — replace old tool_results with placeholders
    L3: tool_result_budget — persist large results to disk
    L4: compact_history    — LLM full summary (1 API call)

    Emergency: reactive_compact — when API still returns context_length_exceeded

    ┌─────────────────────────────────────────────────────────────┐
    │  messages[]                                                 │
    │    ↓                                                        │
    │  L3 budget ─→ L1 snip ─→ L2 micro ─→ [token > threshold?]  │
    │                                      ├─ No  → LLM          │
    │                                      └─ Yes → L4 summary   │
    │                                              ↓              │
    │                                          LLM call           │
    │                                    [context_too_long?]      │
    │                                      └─ Yes → reactive      │
    └─────────────────────────────────────────────────────────────┘

Core principle: cheap first, expensive last.
Execution order matches CC source: budget → snip → micro → auto.

Builds on learning07 (skill loading). Usage:

    python learning08_context_compact/code.py
    Needs: pip install openai python-dotenv pyyaml + Azure config in .env
"""

import ast
import json
import os
import subprocess
import time
from pathlib import Path

import yaml

try:
    import readline
    readline.parse_and_bind('set bind-tty-special-chars off')
    readline.parse_and_bind('set input-meta on')
    readline.parse_and_bind('set output-meta on')
    readline.parse_and_bind('set convert-meta off')
except ImportError:
    pass

from openai import AzureOpenAI
from dotenv import load_dotenv

load_dotenv(override=True)

api_version    = os.getenv("AZURE_API_VERSION")
endpoint       = os.getenv("AZURE_ENDPOINT")
subscription_key = os.getenv("AZURE_API_KEY")
deployment     = os.getenv("AZURE_DEPLOYMENT")

WORKDIR          = Path.cwd()
SKILLS_DIR       = WORKDIR / "skills"
TRANSCRIPT_DIR   = WORKDIR / ".transcripts"
TOOL_RESULTS_DIR = WORKDIR / ".task_outputs" / "tool-results"

client = AzureOpenAI(
    api_version=api_version,
    azure_endpoint=endpoint,
    api_key=subscription_key,
)
MODEL = deployment
CURRENT_TODOS: list[dict] = []


# ═══════════════════════════════════════════════════════════
#  FROM learning07 (unchanged): Skill Registry
# ═══════════════════════════════════════════════════════════

def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """Parse YAML frontmatter from SKILL.md. Returns (meta, body)."""
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    try:
        meta = yaml.safe_load(parts[1]) or {}
    except yaml.YAMLError:
        meta = {}
    return meta, parts[2].strip()


SKILL_REGISTRY: dict[str, dict] = {}


def _scan_skills():
    """Scan skills/ dir and populate SKILL_REGISTRY."""
    if not SKILLS_DIR.exists():
        return
    for d in sorted(SKILLS_DIR.iterdir()):
        if not d.is_dir():
            continue
        manifest = d / "SKILL.md"
        if manifest.exists():
            raw = manifest.read_text(encoding="utf-8")
            meta, _ = _parse_frontmatter(raw)
            name = meta.get("name", d.name)
            desc = meta.get("description", raw.split("\n")[0].lstrip("#").strip())
            SKILL_REGISTRY[name] = {"name": name, "description": desc, "content": raw}


_scan_skills()


def list_skills() -> str:
    if not SKILL_REGISTRY:
        return "(no skills found)"
    return "\n".join(
        f"- **{s['name']}**: {s['description']}"
        for s in SKILL_REGISTRY.values()
    )


def load_skill(name: str) -> str:
    skill = SKILL_REGISTRY.get(name)
    if not skill:
        return f"Skill not found: {name}"
    return skill["content"]


def build_system() -> str:
    catalog = list_skills()
    return (
        f"You are a coding agent at {WORKDIR}. Use tools to solve tasks. "
        "Act, don't explain. Destructive operations require user approval. "
        "Before starting any multi-step task, use todo_write to plan your steps. "
        "Update status as you go. For complex sub-problems, use the task tool.\n"
        f"Skills available:\n{catalog}\n"
        "Use load_skill to get full details when needed."
    )


SYSTEM = build_system()

SUB_SYSTEM = (
    f"You are a coding agent at {WORKDIR}. Use tools to solve the delegated task. "
    "Act, don't explain. Destructive operations require user approval. "
    "Complete the task you were given and return a concise conclusion. "
    "Do not delegate further."
)


# ═══════════════════════════════════════════════════════════
#  FROM learning02-learning07 (unchanged): Tool Implementations
# ═══════════════════════════════════════════════════════════

def safe_path(p: str) -> Path:
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"Path escapes workspace: {p}")
    return path


def run_bash(command: str) -> str:
    try:
        r = subprocess.run(
            command, shell=True, cwd=WORKDIR,
            capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=120,
        )
        out = (r.stdout + r.stderr).strip()
        return out[:50000] if out else "(no output)"
    except subprocess.TimeoutExpired:
        return "Error: Timeout (120s)"
    except (FileNotFoundError, OSError) as e:
        return f"Error: {e}"


def run_read(path: str, limit: int | None = None) -> str:
    try:
        lines = safe_path(path).read_text().splitlines()
        if limit and limit < len(lines):
            lines = lines[:limit] + [f"... ({len(lines) - limit} more lines)"]
        return "\n".join(lines)
    except Exception as e:
        return f"Error: {e}"


def run_write(path: str, content: str) -> str:
    try:
        file_path = safe_path(path)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content)
        return f"Wrote {len(content)} bytes to {path}"
    except Exception as e:
        return f"Error: {e}"


def run_edit(path: str, old_text: str, new_text: str) -> str:
    try:
        file_path = safe_path(path)
        text = file_path.read_text()
        if old_text not in text:
            return f"Error: text not found in {path}"
        file_path.write_text(text.replace(old_text, new_text, 1))
        return f"Edited {path}"
    except Exception as e:
        return f"Error: {e}"


def run_glob(pattern: str) -> str:
    import glob as g
    try:
        results = []
        for match in g.glob(pattern, root_dir=WORKDIR):
            if (WORKDIR / match).resolve().is_relative_to(WORKDIR):
                results.append(match)
        return "\n".join(results) if results else "(no matches)"
    except Exception as e:
        return f"Error: {e}"


def _normalize_todos(todos):
    if isinstance(todos, str):
        try:
            todos = json.loads(todos)
        except json.JSONDecodeError:
            try:
                todos = ast.literal_eval(todos)
            except (SyntaxError, ValueError):
                return None, "Error: todos must be a list or JSON array string"
    if not isinstance(todos, list):
        return None, "Error: todos must be a list"
    for i, todo in enumerate(todos):
        if not isinstance(todo, dict):
            return None, f"Error: todos[{i}] must be an object"
        if "content" not in todo or "status" not in todo:
            return None, f"Error: todos[{i}] missing 'content' or 'status'"
        if todo["status"] not in ("pending", "in_progress", "completed"):
            return None, f"Error: todos[{i}] has invalid status '{todo['status']}'"
    return todos, None


def run_todo_write(todos: list) -> str:
    global CURRENT_TODOS
    todos, error = _normalize_todos(todos)
    if error:
        return error
    CURRENT_TODOS = todos
    lines = ["\n\033[33m## Current Tasks\033[0m"]
    for todo in CURRENT_TODOS:
        icon = {
            "pending": " ",
            "in_progress": "\033[36m▸\033[0m",
            "completed": "\033[32m✓\033[0m",
        }[todo["status"]]
        lines.append(f"  [{icon}] {todo['content']}")
    print("\n".join(lines))
    return f"Updated {len(CURRENT_TODOS)} tasks"


def extract_text(content) -> str:
    if isinstance(content, str):
        return content
    if not content:
        return ""
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                if item.get("type") == "text":
                    parts.append(item.get("text", ""))
            else:
                if getattr(item, "type", None) == "text":
                    parts.append(getattr(item, "text", ""))
        return "\n".join(part for part in parts if part)
    return str(content)


# ═══════════════════════════════════════════════════════════
#  FROM learning04-learning07 (unchanged): Hook System
# ═══════════════════════════════════════════════════════════

HOOKS = {"UserPromptSubmit": [], "PreToolUse": [], "PostToolUse": [], "Stop": []}


def register_hook(event: str, callback):
    HOOKS[event].append(callback)


def trigger_hooks(event: str, *args):
    for callback in HOOKS[event]:
        result = callback(*args)
        if result is not None:
            return result
    return None


DENY_LIST = ["rm -rf /", "sudo", "shutdown", "reboot", "mkfs", "dd if=", "> /dev/sda"]
DESTRUCTIVE = ["rm ", "> /etc/", "chmod 777"]


class ToolBlock:
    def __init__(self, tool_id: str, name: str, input_data: dict):
        self.id = tool_id
        self.name = name
        self.input = input_data


def permission_hook(block):
    if block.name == "bash":
        for pattern in DENY_LIST:
            if pattern in block.input.get("command", ""):
                print(f"\n\033[31m⛔ Blocked: '{pattern}'\033[0m")
                return "Permission denied by deny list"
        for kw in DESTRUCTIVE:
            if kw in block.input.get("command", ""):
                print(f"\n\033[33m⚠ Potentially destructive command\033[0m")
                print(f"   Tool: {block.name}({block.input})")
                choice = input("   Allow? [y/N] ").strip().lower()
                if choice not in ("y", "yes"):
                    return "Permission denied by user"
    if block.name in ("write_file", "edit_file"):
        path = block.input.get("path", "")
        if not (WORKDIR / path).resolve().is_relative_to(WORKDIR):
            print(f"\n\033[33m⚠ Writing outside workspace\033[0m")
            print(f"   Tool: {block.name}({block.input})")
            choice = input("   Allow? [y/N] ").strip().lower()
            if choice not in ("y", "yes"):
                return "Permission denied by user"
    return None


def log_hook(block):
    args_preview = str(list(block.input.values())[:2])[:60]
    print(f"\033[90m[HOOK] {block.name}({args_preview})\033[0m")
    return None


def large_output_hook(block, output):
    if len(str(output)) > 100000:
        print(f"\033[33m[HOOK] ⚠ Large output from {block.name}: {len(str(output))} chars\033[0m")
    return None


def context_inject_hook(query: str):
    print(f"\033[90m[HOOK] UserPromptSubmit: working in {WORKDIR}\033[0m")
    return None


def summary_hook(messages: list):
    tool_count = sum(1 for m in messages if m.get("role") == "tool")
    print(f"\033[90m[HOOK] Stop: session used {tool_count} tool calls\033[0m")
    return None


register_hook("UserPromptSubmit", context_inject_hook)
register_hook("PreToolUse", permission_hook)
register_hook("PreToolUse", log_hook)
register_hook("PostToolUse", large_output_hook)
register_hook("Stop", summary_hook)


# ═══════════════════════════════════════════════════════════
#  NEW in learning08: Four-Layer Compaction Pipeline
# ═══════════════════════════════════════════════════════════

CONTEXT_LIMIT     = 50000   # char estimate; triggers L4 auto-compact
KEEP_RECENT       = 3       # tool result blocks to keep intact in L2
PERSIST_THRESHOLD = 30000   # chars; individual result persisted to disk in L3


def estimate_size(msgs) -> int:
    return len(str(msgs))


# L1: snip_compact — trim middle messages
# OpenAI requires every role="tool" message to be preceded by the assistant
# message that issued the tool_call. We therefore treat each
# (assistant-with-tool_calls + its following tool messages) as an atomic
# "turn group" and only drop whole groups, never split one apart.
def _group_messages(messages: list) -> list[list]:
    """
    Partition messages into atomic groups that must stay together.
    A group is: one assistant message that contains tool_calls, plus all
    immediately-following role="tool" messages that belong to it.
    Any other message (user, plain assistant, system) is its own group.
    """
    groups: list[list] = []
    i = 0
    while i < len(messages):
        msg = messages[i]
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            # Collect this assistant msg + all consecutive tool results
            group = [msg]
            i += 1
            while i < len(messages) and messages[i].get("role") == "tool":
                group.append(messages[i])
                i += 1
            groups.append(group)
        else:
            groups.append([msg])
            i += 1
    return groups


def snip_compact(messages: list, max_messages: int = 50) -> list:
    if len(messages) <= max_messages:
        return messages
    groups = _group_messages(messages)
    if len(groups) <= 6:          # too few groups to safely snip
        return messages
    keep_head = 3                 # groups to keep at the start
    keep_tail = 3                 # groups to keep at the end
    if len(groups) <= keep_head + keep_tail:
        return messages
    middle = groups[keep_head:-keep_tail]
    snipped_count = sum(len(g) for g in middle)
    flat_head = [m for g in groups[:keep_head] for m in g]
    flat_tail = [m for g in groups[-keep_tail:] for m in g]
    return (
        flat_head
        + [{"role": "user", "content": f"[snipped {snipped_count} messages]"}]
        + flat_tail
    )


# L2: micro_compact — old result placeholders
def collect_tool_results(messages: list) -> list:
    blocks = []
    for msg in messages:
        if msg.get("role") == "tool":
            blocks.append(msg)
    return blocks


def micro_compact(messages: list) -> list:
    tool_msgs = collect_tool_results(messages)
    if len(tool_msgs) <= KEEP_RECENT:
        return messages
    to_compact = tool_msgs[:-KEEP_RECENT]
    compact_ids = {id(m) for m in to_compact}
    for msg in messages:
        if id(msg) in compact_ids:
            content = msg.get("content", "")
            if isinstance(content, str) and len(content) > 120:
                msg["content"] = "[Earlier tool result compacted. Re-run if needed.]"
    return messages


# L3: tool_result_budget — persist large results to disk
def persist_large_output(tool_call_id: str, output: str) -> str:
    if len(output) <= PERSIST_THRESHOLD:
        return output
    TOOL_RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    path = TOOL_RESULTS_DIR / f"{tool_call_id}.txt"
    if not path.exists():
        path.write_text(output)
    return f"<persisted-output>\nFull output: {path}\nPreview:\n{output[:2000]}\n</persisted-output>"


def tool_result_budget(messages: list, max_bytes: int = 200_000) -> list:
    # In OpenAI format, tool results are role="tool" messages
    tool_msgs = [m for m in messages if m.get("role") == "tool"]
    total = sum(len(str(m.get("content", ""))) for m in tool_msgs)
    if total <= max_bytes:
        return messages
    ranked = sorted(tool_msgs, key=lambda m: len(str(m.get("content", ""))), reverse=True)
    for msg in ranked:
        if total <= max_bytes:
            break
        content = str(msg.get("content", ""))
        if len(content) <= PERSIST_THRESHOLD:
            continue
        tid = msg.get("tool_call_id", "unknown")
        msg["content"] = persist_large_output(tid, content)
        total = sum(len(str(m.get("content", ""))) for m in tool_msgs)
    return messages


# L4: compact_history — LLM full summary
def write_transcript(messages: list) -> Path:
    TRANSCRIPT_DIR.mkdir(parents=True, exist_ok=True)
    path = TRANSCRIPT_DIR / f"transcript_{int(time.time())}.jsonl"
    with path.open("w") as f:
        for msg in messages:
            f.write(json.dumps(msg, default=str) + "\n")
    return path


def summarize_history(messages: list) -> str:
    conversation = json.dumps(messages, default=str)[:80000]
    prompt = (
        "Summarize this coding-agent conversation so work can continue.\n"
        "Preserve: 1. current goal, 2. key findings/decisions, 3. files read/changed, "
        "4. remaining work, 5. user constraints.\nBe compact but concrete.\n\n"
        + conversation
    )
    response = client.chat.completions.create(
        model=MODEL,
        messages=[{"role": "user", "content": prompt}],
        max_completion_tokens=2000,
    )
    return response.choices[0].message.content or "(empty summary)"


def compact_history(messages: list) -> list:
    transcript_path = write_transcript(messages)
    print(f"[transcript saved: {transcript_path}]")
    summary = summarize_history(messages)
    return [{"role": "user", "content": f"[Compacted]\n\n{summary}"}]


# Emergency: reactive_compact — on API context length error
def reactive_compact(messages: list) -> list:
    write_transcript(messages)
    summary = summarize_history(messages)
    # Keep the last 3 whole groups (never slice mid assistant/tool pair)
    groups = _group_messages(messages)
    tail = [m for g in groups[-3:] for m in g]
    return [
        {"role": "user", "content": f"[Reactive compact]\n\n{summary}"},
        *tail,
    ]


# ═══════════════════════════════════════════════════════════
#  Tool Definitions (learning07 format + compact tool)
# ═══════════════════════════════════════════════════════════

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": "Run a shell command.",
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read file contents.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path":  {"type": "string"},
                    "limit": {"type": "integer"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Write content to a file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path":    {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": "Replace exact text in a file once.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path":     {"type": "string"},
                    "old_text": {"type": "string"},
                    "new_text": {"type": "string"},
                },
                "required": ["path", "old_text", "new_text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "glob",
            "description": "Find files matching a glob pattern.",
            "parameters": {
                "type": "object",
                "properties": {"pattern": {"type": "string"}},
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "todo_write",
            "description": "Create and manage a task list for your current coding session.",
            "parameters": {
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
    },
    {
        "type": "function",
        "function": {
            "name": "task",
            "description": "Launch a subagent to handle a complex subtask. Returns only the final conclusion.",
            "parameters": {
                "type": "object",
                "properties": {"description": {"type": "string"}},
                "required": ["description"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "load_skill",
            "description": "Load the full content of a skill by name.",
            "parameters": {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
        },
    },
    # learning08: compact tool — triggers compact_history
    {
        "type": "function",
        "function": {
            "name": "compact",
            "description": "Summarize earlier conversation to free context space.",
            "parameters": {
                "type": "object",
                "properties": {"focus": {"type": "string"}},
            },
        },
    },
]

SUB_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": "Run a shell command.",
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read file contents.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}, "limit": {"type": "integer"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Write content to a file.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": "Replace exact text in a file once.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path":     {"type": "string"},
                    "old_text": {"type": "string"},
                    "new_text": {"type": "string"},
                },
                "required": ["path", "old_text", "new_text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "glob",
            "description": "Find files matching a glob pattern.",
            "parameters": {
                "type": "object",
                "properties": {"pattern": {"type": "string"}},
                "required": ["pattern"],
            },
        },
    },
]

TOOL_HANDLERS = {
    "bash":       run_bash,
    "read_file":  run_read,
    "write_file": run_write,
    "edit_file":  run_edit,
    "glob":       run_glob,
    "todo_write": run_todo_write,
    "load_skill": load_skill,
}

SUB_HANDLERS = {
    "bash":       run_bash,
    "read_file":  run_read,
    "write_file": run_write,
    "edit_file":  run_edit,
    "glob":       run_glob,
}


# ═══════════════════════════════════════════════════════════
#  FROM learning06-learning07 (unchanged): Subagent
# ═══════════════════════════════════════════════════════════

def run_subagent_loop(messages: list) -> str:
    for _ in range(30):
        api_messages = [{"role": "system", "content": SUB_SYSTEM}] + messages
        response = client.chat.completions.create(
            model=MODEL,
            messages=api_messages,
            tools=SUB_TOOLS,
            max_completion_tokens=20000,
        )
        msg = response.choices[0].message
        if not msg.tool_calls:
            final_text = msg.content or ""
            messages.append({"role": "assistant", "content": final_text})
            return final_text

        messages.append({
            "role": "assistant",
            "content": msg.content,
            "tool_calls": [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                }
                for tc in msg.tool_calls
            ],
        })

        for tool_call in msg.tool_calls:
            name = tool_call.function.name
            handler = SUB_HANDLERS.get(name)
            if not handler:
                output = f"Unknown: {name}"
            else:
                try:
                    args = json.loads(tool_call.function.arguments)
                except Exception as e:
                    output = f"Error parsing arguments: {e}"
                else:
                    block = ToolBlock(tool_call.id, name, args)
                    blocked = trigger_hooks("PreToolUse", block)
                    if blocked:
                        output = str(blocked)
                    else:
                        if name == "bash":
                            print(f"\033[35m[sub] $ {args.get('command', '')}\033[0m")
                        else:
                            print(f"\033[35m[sub] > {name}\033[0m")
                        output = handler(**args)
                        print(f"\033[90m{str(output)[:200]}\033[0m")
                        trigger_hooks("PostToolUse", block, output)
            messages.append({
                "role": "tool",
                "tool_call_id": tool_call.id,
                "content": output,
            })

    return "Subagent stopped after 30 turns without final answer."


def spawn_subagent(description: str) -> str:
    print("\033[35m[Subagent spawned]\033[0m")
    result = run_subagent_loop([{"role": "user", "content": description}])
    result = extract_text(result)
    if not result:
        result = "Subagent completed without a text conclusion."
    print("\033[35m[Subagent done]\033[0m")
    return result


TOOL_HANDLERS["task"] = spawn_subagent


# ═══════════════════════════════════════════════════════════
#  agent_loop — learning08 core: compaction pipeline before LLM
# ═══════════════════════════════════════════════════════════

MAX_REACTIVE_RETRIES = 1
rounds_since_todo = 0


def agent_loop(messages: list):
    global rounds_since_todo
    reactive_retries = 0

    while True:
        # Todo reminder (inherited from learning07)
        if rounds_since_todo >= 3 and messages:
            messages.append({
                "role": "user",
                "content": "<reminder>Update your todos.</reminder>",
            })
            rounds_since_todo = 0

        # learning08: three cheap preprocessors (0 API calls), cheap first
        # Order: budget → snip → micro
        messages[:] = tool_result_budget(messages)   # L3: persist large results first
        messages[:] = snip_compact(messages)          # L1: trim middle
        messages[:] = micro_compact(messages)         # L2: old result placeholders

        # learning08: still over threshold → LLM summary (1 API call)
        if estimate_size(messages) > CONTEXT_LIMIT:
            print("[auto compact]")
            messages[:] = compact_history(messages)

        api_messages = (
            messages
            if messages and messages[0].get("role") == "system"
            else [{"role": "system", "content": SYSTEM}] + messages
        )

        try:
            response = client.chat.completions.create(
                model=MODEL,
                messages=api_messages,
                tools=TOOLS,
                max_completion_tokens=20000,
            )
            reactive_retries = 0  # reset on success
        except Exception as e:
            err = str(e).lower()
            is_too_long = (
                "context_length_exceeded" in err
                or "maximum context length" in err
                or "too many tokens" in err
            )
            if is_too_long and reactive_retries < MAX_REACTIVE_RETRIES:
                print("[reactive compact]")
                messages[:] = reactive_compact(messages)
                reactive_retries += 1
                continue
            raise

        msg = response.choices[0].message

        if not msg.tool_calls:
            final_text = msg.content or ""
            messages.append({"role": "assistant", "content": final_text})
            force = trigger_hooks("Stop", messages)
            if force:
                messages.append({"role": "user", "content": force})
                continue
            return final_text

        rounds_since_todo += 1

        messages.append({
            "role": "assistant",
            "content": msg.content,
            "tool_calls": [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                }
                for tc in msg.tool_calls
            ],
        })

        for tool_call in msg.tool_calls:
            name = tool_call.function.name
            print(f"\033[36m> {name}\033[0m")

            # learning08: compact tool triggers compact_history
            if name == "compact":
                # compact_history replaces messages[] with just the summary.
                # The assistant+tool_calls message that issued this call is now
                # gone, so we must NOT append a role="tool" result — that would
                # be an orphan with no preceding tool_calls and OpenAI rejects it.
                # Simply continue the loop; the summary is the new history.
                messages[:] = compact_history(messages)
                rounds_since_todo = 0
                break  # restart loop with compacted context

            handler = TOOL_HANDLERS.get(name)
            if not handler:
                output = f"Unknown: {name}"
            else:
                try:
                    args = json.loads(tool_call.function.arguments)
                except Exception as e:
                    output = f"Error parsing arguments: {e}"
                else:
                    block = ToolBlock(tool_call.id, name, args)
                    blocked = trigger_hooks("PreToolUse", block)
                    if blocked:
                        output = str(blocked)
                    else:
                        if name == "bash":
                            print(f"\033[33m$ {args.get('command', '')}\033[0m")
                        else:
                            print(f"\033[33m> {name}\033[0m")
                        output = handler(**args)
                        print(str(output)[:200])
                        trigger_hooks("PostToolUse", block, output)
                        if name == "todo_write":
                            rounds_since_todo = 0

            messages.append({
                "role": "tool",
                "tool_call_id": tool_call.id,
                "content": output,
            })


if __name__ == "__main__":
    print("learning08: Context Compact — four-layer compaction pipeline")
    print("Enter a prompt, or q to quit.\n")

    history = []
    while True:
        try:
            query = input("\033[36mlearning08 >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break

        if query.strip().lower() in ("q", "exit", ""):
            break

        trigger_hooks("UserPromptSubmit", query)
        history.append({"role": "user", "content": query})
        answer = agent_loop(history)

        if answer:
            print(answer)

        print()